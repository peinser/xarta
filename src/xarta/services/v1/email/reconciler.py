from __future__ import annotations

import asyncio
import contextlib
import datetime

from dataclasses import dataclass
from typing import Any

from xarta import env
from xarta.logging import logger
from xarta.services.v1.email.base import bp
from xarta.services.v1.email.nats import EmailNATSModel
from xarta.services.v1.email.resend import ResendEmailAdapter
from xarta.services.v1.email.resend_webhooks import normalize_resend_event
from xarta.services.v1.email.tracking import AsyncEmailEventType
from xarta.services.v1.email.tracking import AsyncEmailUpdate
from xarta.services.v1.email.tracking import reduce_async_email
from xarta.tracking import PostgresTrackingStore
from xarta.tracking import TrackedCapabilityService
from xarta.tracking import TrackingPostgresModel


def _positive(value: str) -> bool:
    return float(value) > 0


RECONCILIATION_BATCH_SIZE = env.extract(
    "EMAIL_RECONCILIATION_BATCH_SIZE", default="100", dtype=int, verify=_positive
)
RECONCILIATION_CONCURRENCY = env.extract(
    "EMAIL_RECONCILIATION_CONCURRENCY", default="10", dtype=int, verify=_positive
)
RECONCILIATION_INTERVAL_SECONDS = env.extract(
    "EMAIL_RECONCILIATION_INTERVAL_SECONDS", default="60", dtype=float, verify=_positive
)
RECONCILIATION_LEASE_SECONDS = env.extract(
    "EMAIL_RECONCILIATION_LEASE_SECONDS", default="120", dtype=float, verify=_positive
)
RECONCILIATION_MAX_BACKOFF_SECONDS = env.extract(
    "EMAIL_RECONCILIATION_MAX_BACKOFF_SECONDS",
    default="3600",
    dtype=float,
    verify=_positive,
)


@dataclass(frozen=True)
class ResendEmailReconciler:
    store: Any
    components: Any
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
                operation = await self.store.get_operation(claim.operation.id)
                tracking = TrackedCapabilityService(
                    self.store,
                    self.components.destinations,
                    EmailNATSModel.publish,
                )
                closes_at = operation.state.get("feedback_closes_at")
                if (
                    operation.state.get("delivery_aggregate_emitted")
                    and isinstance(closes_at, str)
                    and datetime.datetime.fromisoformat(closes_at)
                    <= datetime.datetime.now(datetime.UTC)
                ):
                    await tracking.feedback(
                        operation.id,
                        operation.binding.adapter,
                        f"feedback-window-closed:{closes_at}",
                        AsyncEmailUpdate(AsyncEmailEventType.CLOSE),
                        reduce_async_email,
                        "reconciliation",
                    )
                    await self.store.complete_reconciliation(
                        claim, self.interval_seconds
                    )
                    return
                resolved = self.components.destinations.resolve_binding(
                    operation.binding
                )
                adapter = self.components.adapters.create(
                    operation.binding.adapter, resolved.configuration
                )
                if not isinstance(adapter, ResendEmailAdapter):
                    raise ValueError("Email reconciliation requires the Resend adapter")
                provider = await adapter.retrieve(operation.provider_reference)
                last_event = provider.get("last_event")
                tracked_recipients = tuple(operation.state["recipients"])
                await tracking.feedback(
                    operation.id,
                    operation.binding.adapter,
                    f"reconciliation:{operation.provider_reference}:accepted",
                    AsyncEmailUpdate(AsyncEmailEventType.ACCEPTED, tracked_recipients),
                    reduce_async_email,
                    "reconciliation",
                )
                provider_recipients = _provider_recipients(provider)
                update = _safe_reconciliation_update(
                    last_event, provider, provider_recipients, tracked_recipients
                )
                if update is not None:
                    await tracking.feedback(
                        operation.id,
                        operation.binding.adapter,
                        f"reconciliation:{operation.provider_reference}:{last_event}",
                        update,
                        reduce_async_email,
                        "reconciliation",
                    )
            except Exception as ex:
                await self.store.fail_reconciliation(claim, self.max_backoff_seconds)
                logger.error(
                    "Resend email reconciliation failed",
                    **{key: str(value) for key, value in log_context.items()},
                    operation_id=str(claim.operation.id),
                    error_type=type(ex).__name__,
                )
                return
            await self.store.complete_reconciliation(claim, self.interval_seconds)

    async def run_once(self) -> int:
        claims = await self.store.claim_email_reconciliations(
            self.batch_size, self.lease_seconds
        )
        semaphore = asyncio.Semaphore(self.concurrency)
        await asyncio.gather(*(self._reconcile(claim, semaphore) for claim in claims))
        return len(claims)

    async def run(self) -> None:
        while True:
            try:
                await self.run_once()
            except Exception as ex:
                logger.error(
                    "Resend email reconciliation loop failed",
                    error_type=type(ex).__name__,
                )
            await asyncio.sleep(self.interval_seconds)


def _provider_recipients(value) -> tuple[str, ...]:
    recipients = []
    for field in ("to", "cc", "bcc"):
        configured = value.get(field, [])
        if isinstance(configured, list):
            recipients.extend(
                recipient
                for recipient in configured
                if isinstance(recipient, str) and recipient
            )
    return tuple(dict.fromkeys(recipient.casefold() for recipient in recipients))


def _safe_reconciliation_update(
    last_event,
    provider,
    provider_recipients: tuple[str, ...],
    tracked_recipients: tuple[str, ...],
):
    if (
        not isinstance(last_event, str)
        or len(tracked_recipients) != 1
        or provider_recipients != tracked_recipients
    ):
        return None
    return normalize_resend_event(f"email.{last_event}", provider_recipients, provider)


@bp.listener("after_server_start")
async def _start_reconciler(app) -> None:
    current = getattr(app.ctx, "resend_email_reconciler_task", None)
    if current is not None and not current.done():
        return
    store = PostgresTrackingStore(TrackingPostgresModel.pool())
    reconciler = ResendEmailReconciler(store, app.ctx.email_components)
    app.ctx.resend_email_reconciler_task = asyncio.create_task(
        reconciler.run(), name="resend-email-reconciler"
    )


@bp.listener("before_server_stop")
async def _stop_reconciler(app) -> None:
    task = getattr(app.ctx, "resend_email_reconciler_task", None)
    if task is None:
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
