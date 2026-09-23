from __future__ import annotations

import asyncio

from dataclasses import dataclass
from dataclasses import field

import pytest

from package_factory import RUN_ID
from package_factory import TASK_ID
from postal_station.configuration import StationConfig
from postal_station.mock import MockPostalStation
from postal_station.models import ClaimedRun
from postal_station.models import HandoverBatch
from postal_station.models import ProductionStage
from postal_station.models import ScanAcknowledgement
from postal_station.models import ScanMode


@dataclass
class Api:
    claims: list[ClaimedRun] = field(default_factory=list)
    calls: list[tuple] = field(default_factory=list)
    stop: asyncio.Event | None = None

    async def claim_run(self, idempotency_key, limits=None):
        self.calls.append(("claim", idempotency_key, limits))
        claim = self.claims.pop(0)
        if not claim.task_ids and self.stop is not None:
            self.stop.set()
        return claim

    async def submit_scan(self, station_id, barcode, mode, *, handover_batch_id=None):
        self.calls.append(("scan", station_id, barcode, mode, handover_batch_id))
        stage = {
            ScanMode.START_PRODUCTION: ProductionStage.PROCESSING,
            ScanMode.READY_FOR_HANDOVER: ProductionStage.PREPARED,
            ScanMode.CONFIRM_HANDOVER: ProductionStage.HANDED_OVER,
        }[mode]
        return ScanAcknowledgement(barcode, mode, stage, False)

    async def create_handover_batch(
        self, service_date, task_ids, operator_reference, idempotency_key
    ):
        self.calls.append(
            (
                "batch",
                service_date,
                task_ids,
                operator_reference,
                idempotency_key,
            )
        )
        return HandoverBatch("55555555-5555-4555-8555-555555555555", False)


@dataclass
class Station:
    calls: list[str] = field(default_factory=list)
    stop: asyncio.Event | None = None

    async def process_run(self, run_id):
        self.calls.append(run_id)
        if self.stop is not None:
            self.stop.set()


def mock_station(tmp_path, api, station, *, interval=0.001):
    config = StationConfig("https://example.test", "station-1", "fake", tmp_path)
    return MockPostalStation(config, api, station, interval)


async def test_process_run_advances_every_letter_through_handover(tmp_path):
    second_task = "44444444-4444-4444-8444-444444444444"
    api = Api()
    station = Station()
    run = ClaimedRun(RUN_ID, "token", (TASK_ID, second_task), False)

    await mock_station(tmp_path, api, station).process_run(run)

    assert station.calls == [RUN_ID]
    assert [call[3] for call in api.calls if call[0] == "scan"] == [
        ScanMode.START_PRODUCTION,
        ScanMode.START_PRODUCTION,
        ScanMode.READY_FOR_HANDOVER,
        ScanMode.READY_FOR_HANDOVER,
        ScanMode.CONFIRM_HANDOVER,
        ScanMode.CONFIRM_HANDOVER,
    ]
    batch = next(call for call in api.calls if call[0] == "batch")
    assert batch[2] == (TASK_ID, second_task)
    assert batch[3:] == ("mock-station:station-1", f"mock-handover:{RUN_ID}")
    assert all(
        call[4] == "55555555-5555-4555-8555-555555555555"
        for call in api.calls
        if call[0] == "scan" and call[3] is ScanMode.CONFIRM_HANDOVER
    )


async def test_invalid_scan_acknowledgement_stops_the_cycle(tmp_path):
    class InvalidApi(Api):
        async def submit_scan(
            self, station_id, barcode, mode, *, handover_batch_id=None
        ):
            return ScanAcknowledgement(
                "66666666-6666-4666-8666-666666666666",
                mode,
                ProductionStage.PROCESSING,
                False,
            )

    api = InvalidApi()

    with pytest.raises(ValueError, match="invalid scan acknowledgement"):
        await mock_station(tmp_path, api, Station()).process_run(
            ClaimedRun(RUN_ID, "token", (TASK_ID,), False)
        )

    assert not any(call[0] == "batch" for call in api.calls)


async def test_daemon_processes_work_then_stops_from_idle_wait(tmp_path):
    stop = asyncio.Event()
    api = Api(
        claims=[
            ClaimedRun(RUN_ID, "token", (TASK_ID,), False),
            ClaimedRun("77777777-7777-4777-8777-777777777777", "idle", (), False),
        ],
        stop=stop,
    )

    result = await mock_station(tmp_path, api, Station()).run(stop)

    assert result.completed_runs == 1
    assert result.completed_letters == 1
    assert len([call for call in api.calls if call[0] == "claim"]) == 2


async def test_daemon_drains_active_cycle_before_stopping(tmp_path):
    stop = asyncio.Event()
    api = Api(claims=[ClaimedRun(RUN_ID, "token", (TASK_ID,), False)])
    station = Station(stop=stop)

    result = await mock_station(tmp_path, api, station).run(stop)

    assert result.completed_runs == 1
    assert len([call for call in api.calls if call[0] == "claim"]) == 1
    assert any(
        call[0] == "scan" and call[3] is ScanMode.CONFIRM_HANDOVER for call in api.calls
    )
