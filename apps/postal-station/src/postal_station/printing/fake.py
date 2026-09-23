"""Deterministic printer backend for station tests and dry runs."""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from pathlib import Path

from postal_station.models import PrintColorMode
from postal_station.models import PrintSides


@dataclass(frozen=True, slots=True)
class SubmittedPrint:
    document: Path
    job_name: str
    color_mode: PrintColorMode
    sides: PrintSides


@dataclass(slots=True)
class FakePrinter:
    submissions: list[SubmittedPrint] = field(default_factory=list)

    async def submit(
        self,
        document: Path,
        *,
        job_name: str,
        color_mode: PrintColorMode,
        sides: PrintSides,
    ) -> str:
        self.submissions.append(SubmittedPrint(document, job_name, color_mode, sides))
        return f"fake-{len(self.submissions)}"
