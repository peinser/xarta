from __future__ import annotations

import asyncio

from dataclasses import dataclass
from dataclasses import field

import pytest

from package_factory import RUN_ID
from package_factory import package_bytes
from postal_station.api_client import DownloadedPackage
from postal_station.application import PostalStation
from postal_station.configuration import StationConfig
from postal_station.models import ActiveRun
from postal_station.models import PrintAttempt
from postal_station.models import PrintAttemptEvent
from postal_station.models import ScanAcknowledgement
from postal_station.models import ScanMode
from postal_station.packages import PackageVerifier
from postal_station.printing.base import PrinterSubmissionUncertain
from postal_station.printing.fake import FakePrinter


@dataclass
class FakeApi:
    calls: list[tuple] = field(default_factory=list)
    attempts: int = 0

    async def active_runs(self, station_id):
        self.calls.append(("active", station_id))
        return (ActiveRun(RUN_ID, True),)

    async def download_package(self, run_id):
        self.calls.append(("download", run_id))
        package, receipt = package_bytes()
        return DownloadedPackage(package, receipt)

    async def acknowledge_package(self, run_id, receipt):
        self.calls.append(("ack", run_id))

    async def create_print_attempt(
        self, run_id, job_id, generation, rendered_sha256, rendered_byte_count
    ):
        self.attempts += 1
        self.calls.append(("attempt", job_id))
        return PrintAttempt(
            f"55555555-5555-4555-8555-{self.attempts:012d}",
            job_id,
            generation,
            1,
            f"postal/{RUN_ID}/000001/letter/{generation}/1",
            rendered_sha256,
            rendered_byte_count,
        )

    async def report_print_event(
        self, attempt_id, event, *, printer_job_id=None, detail=None
    ):
        self.calls.append(("event", attempt_id, event, printer_job_id, detail))

    async def submit_scan(self, station_id, barcode, mode):
        raise AssertionError("unused")


def station(tmp_path, api, printer):
    config = StationConfig("https://example.test", "station-1", "printer", tmp_path)
    return PostalStation(config, api, printer, PackageVerifier())


async def test_prints_strictly_sequentially_with_server_job_names(tmp_path):
    api = FakeApi()
    active_submissions = 0
    max_active = 0
    names = []

    class ObservedPrinter:
        async def submit(self, document, *, job_name, color_mode, sides):
            nonlocal active_submissions, max_active
            active_submissions += 1
            max_active = max(max_active, active_submissions)
            names.append(job_name)
            await asyncio.sleep(0)
            active_submissions -= 1
            return f"cups-{len(names)}"

    await station(tmp_path, api, ObservedPrinter()).process_run(RUN_ID)

    assert max_active == 1
    assert names == [
        f"postal/{RUN_ID}/000001/letter/1/1",
    ]
    assert [call[0] for call in api.calls] == [
        "download",
        "ack",
        "attempt",
        "event",
    ]
    assert [call[2] for call in api.calls if call[0] == "event"] == [
        PrintAttemptEvent.COMPLETED,
    ]


async def test_uncertain_submission_is_reported_and_not_retried(tmp_path):
    api = FakeApi()

    class UncertainPrinter:
        calls = 0

        async def submit(self, document, *, job_name, color_mode, sides):
            self.calls += 1
            raise PrinterSubmissionUncertain("connection lost after submission")

    printer = UncertainPrinter()
    with pytest.raises(PrinterSubmissionUncertain):
        await station(tmp_path, api, printer).process_run(RUN_ID)

    assert printer.calls == 1
    event = next(call for call in api.calls if call[0] == "event")
    assert event[2] is PrintAttemptEvent.UNCERTAIN

    with pytest.raises(PrinterSubmissionUncertain, match="refusing to resubmit"):
        await station(tmp_path, api, printer).process_run(RUN_ID)
    assert printer.calls == 1


async def test_recovers_only_server_reported_active_runs(tmp_path):
    api = FakeApi()

    runs = await station(tmp_path, api, FakePrinter()).recover_active_runs()

    assert runs == (ActiveRun(RUN_ID, True),)
    assert api.calls == [("active", "station-1")]


async def test_acknowledged_recovery_does_not_acknowledge_package_again(tmp_path):
    api = FakeApi()

    await station(tmp_path, api, FakePrinter()).resume_run(ActiveRun(RUN_ID, True))

    assert not any(call[0] == "ack" for call in api.calls)


async def test_rejects_noncanonical_server_job_name_before_printing(tmp_path):
    api = FakeApi()
    original = api.create_print_attempt

    async def invalid_name(
        run_id, job_id, generation, rendered_sha256, rendered_byte_count
    ):
        attempt = await original(
            run_id, job_id, generation, rendered_sha256, rendered_byte_count
        )
        return PrintAttempt(
            attempt.id,
            attempt.job_id,
            attempt.generation,
            attempt.attempt,
            "operator supplied name",
            attempt.rendered_sha256,
            attempt.rendered_byte_count,
        )

    api.create_print_attempt = invalid_name
    printer = FakePrinter()

    with pytest.raises(ValueError, match="invalid stable job name"):
        await station(tmp_path, api, printer).process_run(RUN_ID)
    assert printer.submissions == []


@pytest.mark.parametrize("run_id", ["", ".", "..", "../outside", "nested/run"])
async def test_rejects_run_id_that_is_not_a_safe_cache_component(tmp_path, run_id):
    api = FakeApi()

    with pytest.raises(ValueError, match="safe cache path component"):
        await station(tmp_path, api, FakePrinter()).process_run(run_id)

    assert api.calls == []
