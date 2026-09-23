from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from xarta.logging import logger
from xarta.services.v1.postal.adapters.local.reducers import reduce_station_scan
from xarta.services.v1.postal.adapters.local.repository import LocalPostalRepository
from xarta.services.v1.postal.adapters.local.workflow import ScanMode
from xarta.services.v1.postal.adapters.local.workflow import reduce_scan
from xarta.tracking import TrackedCapabilityService
from xarta.tracking.models import AppliedReduction


@dataclass(frozen=True, slots=True)
class LocalPostalService:
    repository: LocalPostalRepository
    tracking: TrackedCapabilityService
    log: Any = logger

    async def scan(
        self,
        *,
        station_id: str,
        site_id: str,
        barcode: str,
        mode: ScanMode,
        occurred_at: datetime,
        handover_batch_id: UUID | None = None,
    ) -> tuple[Any, AppliedReduction]:
        target = await self.repository.scan_target(barcode, station_id, site_id)
        if mode is ScanMode.CONFIRM_HANDOVER and handover_batch_id is None:
            raise ValueError("confirm_handover requires a handover_batch_id")
        if mode is not ScanMode.CONFIRM_HANDOVER and handover_batch_id is not None:
            raise ValueError("Only confirm_handover accepts a handover_batch_id")
        external_event_id = ":".join(
            (
                "station-scan",
                str(target.task_id),
                str(target.generation),
                mode.value,
                str(handover_batch_id or "none"),
            )
        )

        async def transaction_mutation(connection: Any, context: Any) -> None:
            await self.repository.apply_scan(
                connection,
                context,
                target=target,
                mode=mode,
                station_id=station_id,
                occurred_at=occurred_at,
                handover_batch_id=handover_batch_id,
            )

        applied = await self.tracking.feedback(
            target.operation_id,
            target.adapter,
            external_event_id,
            (mode, target.service),
            reduce_station_scan,
            "postal-station",
            transaction_mutation=transaction_mutation,
        )
        if not applied.duplicate:
            transition = reduce_scan(
                target.physical_state,
                mode,
                already_recorded=False,
                handover_batch_matches=(
                    handover_batch_id is not None
                    if mode is ScanMode.CONFIRM_HANDOVER
                    else True
                ),
                generation=target.generation,
                current_generation=target.generation,
            )
            await self.log.ainfo(
                "postal_physical_state_changed",
                flow_id=str(target.flow_id),
                correlation_id=str(target.correlation_id),
                node_execution_id=str(target.node_execution_id),
                operation_id=str(target.operation_id),
                adapter=target.adapter,
                production_task_id=str(target.task_id),
                generation=target.generation,
                scan_mode=mode.value,
                previous_state=target.physical_state.value,
                next_state=transition.state.value,
                handover_batch_id=(
                    str(handover_batch_id) if handover_batch_id is not None else None
                ),
            )
        return target, applied
