"""Station orchestration with sequential rendering, printing, and fail-closed recovery."""

from __future__ import annotations

import shutil

from dataclasses import dataclass
from dataclasses import field

from postal_station.api_client import PostalStationApi
from postal_station.configuration import StationConfig
from postal_station.journal import JobJournal
from postal_station.models import ActiveRun
from postal_station.models import PackageLetter
from postal_station.models import PrintAttemptEvent
from postal_station.models import VerifiedPackage
from postal_station.packages import PackageVerifier
from postal_station.printing.base import Printer
from postal_station.printing.base import PrinterSubmissionError
from postal_station.printing.base import PrinterSubmissionUncertain
from postal_station.rendering import Renderer


@dataclass(frozen=True, slots=True)
class PostalStation:
    config: StationConfig
    api: PostalStationApi
    printer: Printer
    verifier: PackageVerifier
    renderer: Renderer = field(default_factory=Renderer)

    async def recover_active_runs(self) -> tuple[ActiveRun, ...]:
        return await self.api.active_runs(self.config.station_id)

    async def resume_run(self, run: ActiveRun) -> None:
        await self.process_run(run.id, acknowledge=not run.package_acknowledged)

    async def process_run(self, run_id: str, *, acknowledge: bool = True) -> None:
        _validate_run_id(run_id)
        downloaded = await self.api.download_package(run_id)
        if downloaded.receipt.byte_count != len(downloaded.content):
            raise ValueError("downloaded package byte count does not match receipt")
        run_directory = self.config.cache_dir / run_id
        run_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        extraction = run_directory / "packages" / downloaded.receipt.package_sha256
        if extraction.exists():
            shutil.rmtree(extraction)
        package = self.verifier.verify(
            downloaded.content, downloaded.receipt, extraction
        )
        if package.manifest.run_id != run_id:
            raise ValueError("package run id does not match requested run")
        if acknowledge:
            await self.api.acknowledge_package(run_id, downloaded.receipt)
        for letter in package.manifest.letters:
            await self._print_letter(run_id, run_directory, package, letter)

    async def _print_letter(
        self,
        run_id: str,
        run_directory,
        package: VerifiedPackage,
        letter: PackageLetter,
    ) -> None:
        journal = JobJournal(
            run_directory / "journal" / f"{letter.job.id}-g{letter.generation}.json"
        )
        previous = journal.read()
        if previous is not None:
            if previous["status"] == "printer_accepted":
                await self.api.report_print_event(
                    previous["attempt_id"],
                    PrintAttemptEvent.COMPLETED,
                    printer_job_id=previous["printer_job_id"],
                )
                journal.write(
                    "server_reported",
                    **{
                        key: value for key, value in previous.items() if key != "status"
                    },
                )
                return
            if previous["status"] == "server_reported":
                return
            if previous["status"] in {"submission_started", "uncertain"}:
                if previous["status"] == "submission_started":
                    journal.write(
                        "uncertain",
                        **{
                            key: value
                            for key, value in previous.items()
                            if key != "status"
                        },
                    )
                raise PrinterSubmissionUncertain(
                    "prior printer submission has no accepted result; refusing to resubmit"
                )
        rendered = self.renderer.render(package, letter, run_directory / "rendered")
        metadata = {
            "rendered_sha256": rendered.sha256,
            "rendered_byte_count": rendered.byte_count,
        }
        journal.write("rendered", **metadata)
        attempt = await self.api.create_print_attempt(
            run_id,
            letter.job.id,
            letter.generation,
            rendered.sha256,
            rendered.byte_count,
        )
        if (
            attempt.job_id != letter.job.id
            or attempt.generation != letter.generation
            or attempt.rendered_sha256 != rendered.sha256
            or attempt.rendered_byte_count != rendered.byte_count
        ):
            raise ValueError("server print attempt does not match rendered package job")
        expected_name = f"postal/{run_id}/{letter.job.sequence:06d}/{letter.job.kind.value}/{letter.generation}/{attempt.attempt}"
        if attempt.job_name != expected_name:
            raise ValueError("server print attempt has an invalid stable job name")
        values = {**metadata, "attempt_id": attempt.id}
        journal.write("submission_started", **values)
        try:
            printer_job_id = await self.printer.submit(
                rendered.path,
                job_name=attempt.job_name,
                color_mode=letter.print_instructions.color_mode,
                sides=letter.print_instructions.sides,
            )
        except PrinterSubmissionUncertain as error:
            journal.write("uncertain", **values)
            await self.api.report_print_event(
                attempt.id, PrintAttemptEvent.UNCERTAIN, detail=str(error)
            )
            raise
        except PrinterSubmissionError as error:
            await self.api.report_print_event(
                attempt.id, PrintAttemptEvent.FAILED, detail=str(error)
            )
            journal.write("server_reported", **values)
            raise
        accepted = {**values, "printer_job_id": printer_job_id}
        journal.write("printer_accepted", **accepted)
        await self.api.report_print_event(
            attempt.id, PrintAttemptEvent.COMPLETED, printer_job_id=printer_job_id
        )
        journal.write("server_reported", **accepted)


def _validate_run_id(run_id: str) -> None:
    if (
        not run_id
        or run_id in {".", ".."}
        or "/" in run_id
        or "\\" in run_id
        or "\0" in run_id
    ):
        raise ValueError("run id is not a safe cache path component")
