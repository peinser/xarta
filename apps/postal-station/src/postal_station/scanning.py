"""Explicit, server-acknowledged traveller scanning."""

from __future__ import annotations

from collections.abc import Awaitable
from collections.abc import Callable
from dataclasses import dataclass

from postal_station.api_client import PostalStationApi
from postal_station.models import ScanAcknowledgement
from postal_station.models import ScanMode


@dataclass(frozen=True, slots=True)
class Scanner:
    api: PostalStationApi
    station_id: str

    async def scan(
        self,
        barcode: str,
        mode: ScanMode,
        advance: Callable[[ScanAcknowledgement], Awaitable[None]],
        *,
        handover_batch_id: str | None = None,
    ) -> ScanAcknowledgement:
        if not barcode.strip():
            raise ValueError("barcode must not be empty")
        if not isinstance(mode, ScanMode):
            raise TypeError("scan mode must be explicitly selected")
        if mode is ScanMode.CONFIRM_HANDOVER and handover_batch_id is None:
            raise ValueError("confirm_handover requires a handover batch")
        if mode is not ScanMode.CONFIRM_HANDOVER and handover_batch_id is not None:
            raise ValueError("handover batch is only valid for confirm_handover")
        acknowledgement = await self.api.submit_scan(
            self.station_id,
            barcode,
            mode,
            handover_batch_id=handover_batch_id,
        )
        if acknowledgement.mode is not mode:
            raise ValueError("server acknowledged a different scan mode")
        await advance(acknowledgement)
        return acknowledgement
