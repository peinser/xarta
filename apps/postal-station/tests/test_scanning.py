from __future__ import annotations

import asyncio

import pytest

from postal_station.models import ProductionStage
from postal_station.models import ScanAcknowledgement
from postal_station.models import ScanMode
from postal_station.scanning import Scanner


class ScanApi:
    def __init__(self):
        self.release = asyncio.Event()

    async def submit_scan(self, station_id, barcode, mode, **kwargs):
        await self.release.wait()
        return ScanAcknowledgement("task-1", mode, ProductionStage.PROCESSING, False)


async def test_ui_advances_only_after_server_acknowledgement():
    api = ScanApi()
    advanced = []

    async def advance(acknowledgement):
        advanced.append(acknowledgement)

    task = asyncio.create_task(
        Scanner(api, "station-1").scan(
            "traveller-1", ScanMode.START_PRODUCTION, advance
        )
    )
    await asyncio.sleep(0)
    assert advanced == []

    api.release.set()
    acknowledgement = await task
    assert advanced == [acknowledgement]


async def test_scan_requires_explicit_mode():
    api = ScanApi()

    with pytest.raises(TypeError, match="explicitly selected"):
        await Scanner(api, "station-1").scan(
            "traveller-1", "start_production", lambda acknowledgement: None
        )


async def test_server_failure_does_not_advance_ui():
    advanced = []

    class FailedApi:
        async def submit_scan(self, station_id, barcode, mode, **kwargs):
            raise RuntimeError("server unavailable")

    with pytest.raises(RuntimeError, match="server unavailable"):
        await Scanner(FailedApi(), "station-1").scan(
            "traveller-1",
            ScanMode.READY_FOR_HANDOVER,
            lambda acknowledgement: advanced.append(acknowledgement),
        )
    assert advanced == []


async def test_confirm_handover_requires_explicit_batch():
    api = ScanApi()
    with pytest.raises(ValueError, match="handover batch"):
        await Scanner(api, "station-1").scan(
            "traveller-1", ScanMode.CONFIRM_HANDOVER, lambda acknowledgement: None
        )
