"""Printer abstraction and submission outcomes."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from postal_station.models import PrintColorMode
from postal_station.models import PrintSides


class PrinterSubmissionError(RuntimeError):
    """A printer definitively rejected a submission."""


class PrinterSubmissionUncertain(RuntimeError):
    """A submission may have reached the printer and must not be retried automatically."""


class Printer(Protocol):
    async def submit(
        self,
        document: Path,
        *,
        job_name: str,
        color_mode: PrintColorMode,
        sides: PrintSides,
    ) -> str: ...
