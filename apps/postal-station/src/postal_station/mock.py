"""Safe development station that simulates the complete local postal lifecycle."""

from __future__ import annotations

import asyncio

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import uuid4
from zoneinfo import ZoneInfo

from postal_station.api_client import ApiError
from postal_station.api_client import PostalStationApi
from postal_station.configuration import StationConfig
from postal_station.models import ClaimedRun
from postal_station.models import ProductionStage
from postal_station.models import ScanMode


class RunProcessor(Protocol):
    async def process_run(self, run_id: str) -> None: ...


@dataclass(frozen=True, slots=True)
class MockStationResult:
    completed_runs: int
    completed_letters: int


@dataclass(slots=True)
class MockPostalStation:
    config: StationConfig
    api: PostalStationApi
    station: RunProcessor
    poll_interval_seconds: float = 1.0

    def __post_init__(self) -> None:
        if self.poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be greater than zero")

    async def process_run(self, run: ClaimedRun) -> None:
        if not run.task_ids:
            raise ValueError("mock station cannot process an empty run")
        await self.station.process_run(run.id)
        for task_id in run.task_ids:
            await self._scan(
                task_id, ScanMode.START_PRODUCTION, ProductionStage.PROCESSING
            )
        for task_id in run.task_ids:
            await self._scan(
                task_id, ScanMode.READY_FOR_HANDOVER, ProductionStage.PREPARED
            )
        batch = await self.api.create_handover_batch(
            datetime.now(ZoneInfo(self.config.timezone)).date(),
            run.task_ids,
            f"mock-station:{self.config.station_id}",
            f"mock-handover:{run.id}",
        )
        for task_id in run.task_ids:
            await self._scan(
                task_id,
                ScanMode.CONFIRM_HANDOVER,
                ProductionStage.HANDED_OVER,
                handover_batch_id=batch.id,
            )

    async def run(self, stop: asyncio.Event) -> MockStationResult:
        completed_runs = 0
        completed_letters = 0
        claim_key = self._new_claim_key()
        claimed: ClaimedRun | None = None
        while not stop.is_set() or claimed is not None:
            if claimed is None:
                try:
                    claimed = await self.api.claim_run(claim_key)
                except ApiError:
                    if await _wait(stop, self.poll_interval_seconds):
                        break
                    continue
                if not claimed.task_ids:
                    claimed = None
                    claim_key = self._new_claim_key()
                    if await _wait(stop, self.poll_interval_seconds):
                        break
                    continue
            try:
                await self.process_run(claimed)
            except ApiError:
                if stop.is_set():
                    raise
                await _wait(stop, self.poll_interval_seconds)
                continue
            completed_runs += 1
            completed_letters += len(claimed.task_ids)
            claimed = None
            claim_key = self._new_claim_key()
        return MockStationResult(completed_runs, completed_letters)

    async def _scan(
        self,
        task_id: str,
        mode: ScanMode,
        expected_stage: ProductionStage,
        *,
        handover_batch_id: str | None = None,
    ) -> None:
        acknowledgement = await self.api.submit_scan(
            self.config.station_id,
            task_id,
            mode,
            handover_batch_id=handover_batch_id,
        )
        if (
            acknowledgement.task_id != task_id
            or acknowledgement.mode is not mode
            or acknowledgement.stage is not expected_stage
        ):
            raise ValueError("server returned an invalid scan acknowledgement")

    def _new_claim_key(self) -> str:
        return f"mock-claim:{self.config.station_id}:{uuid4()}"


async def _wait(stop: asyncio.Event, timeout: float) -> bool:
    try:
        await asyncio.wait_for(stop.wait(), timeout)
    except TimeoutError:
        return False
    return True
