from __future__ import annotations

import asyncio
import contextlib

from dataclasses import dataclass
from typing import Any

from xarta import env
from xarta.logging import logger
from xarta.services.v1.peppol.base import bp
from xarta.services.v1.peppol.nats import PeppolNATSModel
from xarta.services.v1.peppol.service import PeppolService
from xarta.tracking import PostgresTrackingStore
from xarta.tracking import TrackingPostgresModel


def _positive(value: str) -> bool:
    return float(value) > 0


RECONCILIATION_BATCH_SIZE = env.extract(
    "PEPPOL_RECONCILIATION_BATCH_SIZE", default="100", dtype=int, verify=_positive
)
RECONCILIATION_CONCURRENCY = env.extract(
    "PEPPOL_RECONCILIATION_CONCURRENCY", default="10", dtype=int, verify=_positive
)
RECONCILIATION_INTERVAL_SECONDS = env.extract(
    "PEPPOL_RECONCILIATION_INTERVAL_SECONDS",
    default="30",
    dtype=float,
    verify=_positive,
)
RECONCILIATION_LEASE_SECONDS = env.extract(
    "PEPPOL_RECONCILIATION_LEASE_SECONDS",
    default="60",
    dtype=float,
    verify=_positive,
)
RECONCILIATION_MAX_BACKOFF_SECONDS = env.extract(
    "PEPPOL_RECONCILIATION_MAX_BACKOFF_SECONDS",
    default="3600",
    dtype=float,
    verify=_positive,
)


@dataclass(frozen=True)
class PeppolReconciler:
    store: Any
    service: PeppolService
    batch_size: int = RECONCILIATION_BATCH_SIZE
    concurrency: int = RECONCILIATION_CONCURRENCY
    lease_seconds: float = RECONCILIATION_LEASE_SECONDS
    interval_seconds: float = RECONCILIATION_INTERVAL_SECONDS
    max_backoff_seconds: float = RECONCILIATION_MAX_BACKOFF_SECONDS

    async def _reconcile(self, claim, semaphore: asyncio.Semaphore) -> None:
        async with semaphore:
            log_context = {}
            try:
                log_context = await self.store.operation_logging_context(
                    claim.operation.id
                )
                await self.service.reconcile(claim.operation.id)
            except Exception as ex:
                await self.store.fail_peppol_reconciliation(
                    claim, self.max_backoff_seconds
                )
                logger.error(
                    "Peppol reconciliation failed",
                    **{key: str(value) for key, value in log_context.items()},
                    operation_id=str(claim.operation.id),
                    error_type=type(ex).__name__,
                )
                return
            await self.store.complete_peppol_reconciliation(
                claim, self.interval_seconds
            )

    async def run_once(self) -> int:
        claims = await self.store.claim_peppol_reconciliations(
            self.batch_size, self.lease_seconds
        )
        semaphore = asyncio.Semaphore(self.concurrency)
        await asyncio.gather(*(self._reconcile(claim, semaphore) for claim in claims))
        return len(claims)

    async def run(self) -> None:
        while True:
            await self.run_once()
            await asyncio.sleep(self.interval_seconds)


@bp.listener("after_server_start")
async def _start_reconciler(app) -> None:
    current = getattr(app.ctx, "peppol_reconciler_task", None)
    if current is not None and not current.done():
        return
    store = PostgresTrackingStore(TrackingPostgresModel.pool())
    reconciler = PeppolReconciler(
        store=store,
        service=PeppolService(
            store, app.ctx.peppol_components, PeppolNATSModel.publish
        ),
    )
    app.ctx.peppol_reconciler_task = asyncio.create_task(
        reconciler.run(), name="peppol-reconciler"
    )


@bp.listener("before_server_stop")
async def _stop_reconciler(app) -> None:
    task = getattr(app.ctx, "peppol_reconciler_task", None)
    if task is None:
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
