from __future__ import annotations

from pathlib import Path

import pytest

from postal_station.models import PrintColorMode
from postal_station.models import PrintSides
from postal_station.printing.cups import CupsPrinter


async def test_cups_uses_exec_arguments_without_shell(monkeypatch):
    captured = None

    class Process:
        returncode = 0

        async def communicate(self):
            return b"request id is mailroom-42\n", b""

    async def create(*arguments, **options):
        nonlocal captured
        captured = (arguments, options)
        return Process()

    monkeypatch.setattr(
        "postal_station.printing.cups.asyncio.create_subprocess_exec", create
    )
    result = await CupsPrinter("mailroom").submit(
        Path("letter.pdf"),
        job_name="postal/run/1/content/1/1",
        color_mode=PrintColorMode.COLOR,
        sides=PrintSides.DUPLEX_SHORT_EDGE,
    )

    assert result == "request id is mailroom-42"
    arguments, options = captured
    assert arguments == (
        "lp",
        "-d",
        "mailroom",
        "-t",
        "postal/run/1/content/1/1",
        "-o",
        "print-color-mode=color",
        "-o",
        "sides=two-sided-short-edge",
        "--",
        "letter.pdf",
    )
    assert options.keys() == {"stdout", "stderr"}
