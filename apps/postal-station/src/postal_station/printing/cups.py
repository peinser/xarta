"""CUPS backend using the command-line client without invoking a shell."""

from __future__ import annotations

import asyncio

from dataclasses import dataclass
from pathlib import Path

from postal_station.models import PrintColorMode
from postal_station.models import PrintSides
from postal_station.printing.base import PrinterSubmissionError
from postal_station.printing.base import PrinterSubmissionUncertain


@dataclass(frozen=True, slots=True)
class CupsPrinter:
    printer: str
    command: str = "lp"

    async def submit(
        self,
        document: Path,
        *,
        job_name: str,
        color_mode: PrintColorMode,
        sides: PrintSides,
    ) -> str:
        arguments = (
            self.command,
            "-d",
            self.printer,
            "-t",
            job_name,
            "-o",
            _color_option(color_mode),
            "-o",
            _sides_option(sides),
            "--",
            str(document),
        )
        try:
            process = await asyncio.create_subprocess_exec(
                *arguments,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as error:
            raise PrinterSubmissionError(
                f"could not start CUPS lp command: {error}"
            ) from error
        try:
            stdout, stderr = await process.communicate()
        except asyncio.CancelledError:
            process.kill()
            await process.wait()
            raise PrinterSubmissionUncertain(
                "CUPS submission was interrupted"
            ) from None
        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()
            raise PrinterSubmissionError(f"CUPS rejected print job: {detail}")
        response = stdout.decode("utf-8", errors="replace").strip()
        return response or job_name


def _color_option(color_mode: PrintColorMode) -> str:
    if color_mode is PrintColorMode.MONOCHROME:
        return "print-color-mode=monochrome"
    return "print-color-mode=color"


def _sides_option(sides: PrintSides) -> str:
    if sides is PrintSides.SIMPLEX:
        return "sides=one-sided"
    if sides is PrintSides.DUPLEX_LONG_EDGE:
        return "sides=two-sided-long-edge"
    return "sides=two-sided-short-edge"
