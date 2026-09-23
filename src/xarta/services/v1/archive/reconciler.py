from __future__ import annotations

import asyncio
import contextlib
import time

from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING
from uuid import UUID

import nats
import orjson

from nats.js.api import AckPolicy
from nats.js.api import ConsumerConfig

from xarta import env
from xarta.logging import logger
from xarta.nats.constants import STREAM_ARCHIVE_COMMANDS
from xarta.telemetry import nats_consumer_span

from .base import bp
from .db import ArchivePostgresModel as DB
from .db import DeletionCommand
from .events import publish_archive_event

if TYPE_CHECKING:
    from sanic import Sanic

    from .storage import ArchiveStorageRegistry


def _positive(value: str) -> bool:
    return float(value) > 0


RECONCILER_ENABLED = env.extract(
    key="ARCHIVE_DELETION_RECONCILER_ENABLED",
    default="true",
    dtype=str,
    verify=lambda value: value.strip().lower()
    in {"true", "false", "1", "0", "yes", "no"},
).strip().lower() in {"true", "1", "yes"}
RECONCILER_BATCH_SIZE = env.extract(
    key="ARCHIVE_DELETION_RECONCILER_BATCH_SIZE",
    default="100",
    dtype=int,
    verify=_positive,
)
RECONCILER_CONCURRENCY = env.extract(
    key="ARCHIVE_DELETION_RECONCILER_CONCURRENCY",
    default="25",
    dtype=int,
    verify=lambda value: 1 <= int(value) <= 100,
)
RECONCILER_ACK_WAIT_SECONDS = env.extract(
    key="ARCHIVE_DELETION_RECONCILER_ACK_WAIT_SECONDS",
    default="120",
    dtype=float,
    verify=_positive,
)
RECONCILER_MAX_BACKOFF_SECONDS = env.extract(
    key="ARCHIVE_DELETION_RECONCILER_MAX_BACKOFF_SECONDS",
    default="3600",
    dtype=float,
    verify=_positive,
)
RECONCILER_SCAN_SECONDS = env.extract(
    key="ARCHIVE_DELETION_RECONCILER_SCAN_SECONDS",
    default="5",
    dtype=float,
    verify=_positive,
)


def _command(data: bytes) -> DeletionCommand:
    payload = orjson.loads(data)
    required = {
        "aggregate_id",
        "version_internal_id",
        "representation_internal_id",
        "archive",
        "document_id",
        "version_id",
        "representation_id",
        "head_version_id",
        "head_version_internal_id",
        "backend",
        "backend_revision",
        "storage_key",
        "kind",
        "history_policy",
    }
    if not isinstance(payload, dict) or set(payload) != required:
        raise ValueError("Invalid archive deletion command shape")
    if payload["kind"] not in {"document", "history"}:
        raise ValueError("Invalid archive deletion command kind")
    if payload["kind"] == "document" and payload["history_policy"] is not None:
        raise ValueError("Document deletion command cannot have a history policy")
    if payload["kind"] == "history" and payload["history_policy"] not in {
        "retain-metadata",
        "latest-only",
    }:
        raise ValueError("Invalid archive history policy")
    command = DeletionCommand(
        aggregate_id=int(payload["aggregate_id"]),
        version_internal_id=int(payload["version_internal_id"]),
        representation_internal_id=int(payload["representation_internal_id"]),
        archive=payload["archive"],
        document_id=UUID(payload["document_id"]),
        version_id=UUID(payload["version_id"]),
        representation_id=UUID(payload["representation_id"]),
        head_version_id=UUID(payload["head_version_id"]),
        head_version_internal_id=int(payload["head_version_internal_id"]),
        backend=payload["backend"],
        backend_revision=payload["backend_revision"],
        storage_key=payload["storage_key"],
        kind=payload["kind"],
        history_policy=payload["history_policy"],
    )
    if (
        command.aggregate_id < 1
        or command.version_internal_id < 1
        or command.representation_internal_id < 1
        or command.head_version_internal_id < 1
    ):
        raise ValueError("Invalid archive deletion command identity")
    return command


@dataclass
class ArchiveDeletionReconciler:
    storage: ArchiveStorageRegistry
    jetstream: object
    max_backoff_seconds: float
    concurrency: int = 1
    _scan_cursor: tuple[int, int] | None = field(default=None, init=False, repr=False)

    async def process(self, command: DeletionCommand) -> None:
        should_delete = await DB.validate_deletion_command(command)
        if not should_delete:
            return
        backend = self.storage.backend(command.backend, command.backend_revision)
        with contextlib.suppress(FileNotFoundError):
            await backend.delete(command.storage_key)

        completion = await DB.complete_deletion(command)
        if command.kind == "history" and completion.version_transitioned:
            await publish_archive_event(
                self.jetstream,
                event_type="xarta.archive.document.version-content-removed",
                archive=command.archive,
                document_id=command.document_id,
                aggregate_id=command.aggregate_id,
                version_internal_id=command.version_internal_id,
                version_id=command.version_id,
                outcome=command.history_policy,
            )
        if completion.finalized:
            await publish_archive_event(
                self.jetstream,
                event_type="xarta.archive.document.deleted",
                archive=command.archive,
                document_id=command.document_id,
                aggregate_id=command.aggregate_id,
                version_internal_id=command.head_version_internal_id,
                version_id=command.head_version_id,
                outcome="deleted",
            )

    async def republish_pending(self) -> int:
        from .nats import publish_deletion_command

        commands = await DB.pending_deletion_commands(
            limit=RECONCILER_BATCH_SIZE, after=self._scan_cursor
        )
        if not commands and self._scan_cursor is not None:
            self._scan_cursor = None
            commands = await DB.pending_deletion_commands(
                limit=RECONCILER_BATCH_SIZE, after=None
            )
        for command in commands:
            await publish_deletion_command(self.jetstream, command)
        if commands:
            last = commands[-1]
            self._scan_cursor = (
                (last.aggregate_id, last.representation_internal_id)
                if len(commands) == RECONCILER_BATCH_SIZE
                else None
            )
        return len(commands)

    @staticmethod
    async def _heartbeat(message) -> None:
        while True:
            await asyncio.sleep(RECONCILER_ACK_WAIT_SECONDS / 3)
            try:
                await message.in_progress()
            except asyncio.CancelledError:
                raise
            except Exception:
                await logger.aexception("Archive deletion progress heartbeat failed")

    async def _process_message(self, message, semaphore: asyncio.Semaphore) -> None:
        with nats_consumer_span(message):
            heartbeat = asyncio.create_task(self._heartbeat(message))
            try:
                async with semaphore:
                    command = _command(message.data)
                    await self.process(command)
            except asyncio.CancelledError:
                with contextlib.suppress(Exception):
                    await message.nak()
                raise
            except (ValueError, orjson.JSONDecodeError):
                await logger.aexception("Discarding invalid archive deletion command")
                with contextlib.suppress(Exception):
                    await message.term()
            except Exception:
                delivery = getattr(
                    getattr(message, "metadata", None), "num_delivered", 1
                )
                delay = min(
                    self.max_backoff_seconds,
                    float(2 ** min(delivery - 1, 30)),
                )
                await logger.aexception("Archive deletion command failed")
                with contextlib.suppress(Exception):
                    await message.nak(delay=delay)
            else:
                try:
                    await message.ack()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    await logger.aexception("Archive deletion command ACK failed")
            finally:
                heartbeat.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await heartbeat

    async def run(self, subscription) -> None:
        next_scan = 0.0
        semaphore = asyncio.Semaphore(self.concurrency)
        while True:
            if time.monotonic() >= next_scan:
                try:
                    await self.republish_pending()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    await logger.aexception(
                        "Failed to republish pending archive deletion commands"
                    )
                next_scan = time.monotonic() + RECONCILER_SCAN_SECONDS
            try:
                messages = await subscription.fetch(RECONCILER_BATCH_SIZE, timeout=1)
            except TimeoutError:
                continue
            except asyncio.CancelledError:
                raise
            except Exception:
                await logger.aexception("Archive deletion subscription fetch failed")
                await asyncio.sleep(1)
                continue
            tasks = [
                asyncio.create_task(self._process_message(message, semaphore))
                for message in messages
            ]
            try:
                await asyncio.gather(*tasks)
            except BaseException:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise


@bp.listener("after_server_start")
async def _start_reconciler(app: Sanic) -> None:
    if not RECONCILER_ENABLED:
        return
    current = getattr(app.ctx, "archive_deletion_reconciler_task", None)
    if current is not None and not current.done():
        return

    from .nats import ArchiveNATSModel

    jetstream = ArchiveNATSModel.jetstream()
    config = ConsumerConfig(
        durable_name="archive-deletion",
        ack_policy=AckPolicy.EXPLICIT,
        ack_wait=RECONCILER_ACK_WAIT_SECONDS,
        max_deliver=-1,
        filter_subject="archive.commands.delete",
    )
    try:
        await jetstream.consumer_info(STREAM_ARCHIVE_COMMANDS, "archive-deletion")
    except nats.js.errors.NotFoundError:
        subscription = await jetstream.pull_subscribe(
            stream=STREAM_ARCHIVE_COMMANDS,
            subject="archive.commands.delete",
            durable="archive-deletion",
            config=config,
        )
    else:
        subscription = await jetstream.pull_subscribe(
            stream=STREAM_ARCHIVE_COMMANDS,
            subject="archive.commands.delete",
            durable="archive-deletion",
        )
    reconciler = ArchiveDeletionReconciler(
        storage=app.ctx.archive_storage,
        jetstream=jetstream,
        max_backoff_seconds=RECONCILER_MAX_BACKOFF_SECONDS,
        concurrency=RECONCILER_CONCURRENCY,
    )
    app.ctx.archive_deletion_reconciler_task = asyncio.create_task(
        reconciler.run(subscription), name="archive-deletion-reconciler"
    )


@bp.listener("before_server_stop")
async def _stop_reconciler(app: Sanic) -> None:
    task = getattr(app.ctx, "archive_deletion_reconciler_task", None)
    if task is None:
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
