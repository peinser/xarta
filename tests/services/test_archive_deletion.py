from __future__ import annotations

import asyncio
import datetime

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import orjson
import pytest

from xarta.exceptions.http import ForbiddenError
from xarta.exceptions.protocol import UnsafeArchiveDeleteError
from xarta.jobs.setup_nats.base import _archive_commands_stream_config
from xarta.services.v1.archive.db import ArchivePostgresModel
from xarta.services.v1.archive.db import DeletionCommand
from xarta.services.v1.archive.db import DeletionCompletion
from xarta.services.v1.archive.delete import _delete
from xarta.services.v1.archive.nats import publish_deletion_command
from xarta.services.v1.archive.reconciler import ArchiveDeletionReconciler
from xarta.services.v1.archive.reconciler import _start_reconciler
from xarta.services.v1.archive.reconciler import _stop_reconciler


class AsyncContext:
    def __init__(self, value=None) -> None:
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *_args):
        return None


class FakePool:
    def __init__(self, connection) -> None:
        self.connection = connection

    def acquire(self):
        return AsyncContext(self.connection)


class DeletionConnection:
    def __init__(self, aggregate=None, versions=None) -> None:
        self.aggregate = aggregate
        self.versions = versions or []
        self.queries: list[tuple[str, tuple]] = []

    def transaction(self):
        return AsyncContext()

    async def fetchrow(self, query, *args):
        self.queries.append((query, args))
        return self.aggregate

    async def fetch(self, query, *args):
        self.queries.append((query, args))
        return self.versions

    async def fetchval(self, query, *args):
        self.queries.append((query, args))
        return False

    async def execute(self, query, *args):
        self.queries.append((query, args))
        return "UPDATE 1"


def install_pool(monkeypatch, connection) -> None:
    monkeypatch.setattr(
        ArchivePostgresModel,
        "pool",
        classmethod(lambda cls: FakePool(connection)),
    )


def command(*, kind="document", policy=None) -> DeletionCommand:
    return DeletionCommand(
        aggregate_id=7,
        version_internal_id=11,
        representation_internal_id=12,
        archive="payroll",
        document_id=uuid4(),
        version_id=uuid4(),
        representation_id=uuid4(),
        head_version_id=uuid4(),
        head_version_internal_id=13,
        backend="primary",
        backend_revision="v1",
        storage_key="payroll/document/version",
        kind=kind,
        history_policy=policy,
    )


@pytest.mark.asyncio
async def test_delete_commits_lifecycle_then_publishes_pinned_commands(
    monkeypatch,
) -> None:
    document_id = uuid4()
    version_id = uuid4()
    connection = DeletionConnection(
        aggregate={
            "_id": 7,
            "lifecycle": "active",
            "head_version_id": version_id,
            "head_version_internal_id": 11,
            "expires": datetime.datetime(2029, 1, 1, tzinfo=datetime.UTC),
        },
        versions=[
            {
                "aggregate_id": 7,
                "version_internal_id": 11,
                "representation_internal_id": 12,
                "archive": "payroll",
                "document_id": document_id,
                "version_id": version_id,
                "representation_id": uuid4(),
                "head_version_id": version_id,
                "head_version_internal_id": 11,
                "backend": "primary",
                "backend_revision": "v1",
                "storage_key": "payroll/document/version",
            }
        ],
    )
    install_pool(monkeypatch, connection)
    publish_commands = AsyncMock()
    publish_event = AsyncMock()
    monkeypatch.setattr(ArchivePostgresModel, "_publish_commands", publish_commands)
    monkeypatch.setattr(
        "xarta.services.v1.archive.db.publish_archive_event", publish_event
    )
    monkeypatch.setattr(ArchivePostgresModel, "_jetstream", staticmethod(object))

    result = await ArchivePostgresModel.request_deletion(
        "payroll",
        document_id,
        safe=True,
        now=datetime.datetime(2030, 1, 1, tzinfo=datetime.UTC),
    )

    assert result.accepted is True
    queued = publish_commands.await_args.args[0]
    assert queued == [
        DeletionCommand(
            aggregate_id=7,
            version_internal_id=11,
            representation_internal_id=queued[0].representation_internal_id,
            archive="payroll",
            document_id=document_id,
            version_id=version_id,
            representation_id=queued[0].representation_id,
            head_version_id=version_id,
            head_version_internal_id=11,
            backend="primary",
            backend_revision="v1",
            storage_key="payroll/document/version",
            kind="document",
        )
    ]
    sql = " ".join(query for query, _ in connection.queries)
    assert "FOR UPDATE OF aggregate" in sql
    assert "lifecycle = 'deleting'" in sql
    assert "archive_deletion_jobs" not in sql
    publish_event.assert_awaited_once()


@pytest.mark.asyncio
async def test_repeated_delete_republishes_deterministic_commands(monkeypatch) -> None:
    current = command()
    connection = DeletionConnection(
        aggregate={
            "_id": 7,
            "lifecycle": "deleting",
            "head_version_id": current.head_version_id,
            "head_version_internal_id": current.head_version_internal_id,
            "expires": None,
        },
        versions=[
            {
                "aggregate_id": current.aggregate_id,
                "version_internal_id": current.version_internal_id,
                "representation_internal_id": current.representation_internal_id,
                "archive": current.archive,
                "document_id": current.document_id,
                "version_id": current.version_id,
                "representation_id": current.representation_id,
                "head_version_id": current.head_version_id,
                "head_version_internal_id": current.head_version_internal_id,
                "backend": current.backend,
                "backend_revision": current.backend_revision,
                "storage_key": current.storage_key,
            }
        ],
    )
    install_pool(monkeypatch, connection)
    publish = AsyncMock()
    monkeypatch.setattr(ArchivePostgresModel, "_publish_commands", publish)
    monkeypatch.setattr(
        "xarta.services.v1.archive.db.publish_archive_event", AsyncMock()
    )
    monkeypatch.setattr(ArchivePostgresModel, "_jetstream", staticmethod(object))

    result = await ArchivePostgresModel.request_deletion(
        current.archive,
        current.document_id,
        safe=True,
        now=datetime.datetime.now(tz=datetime.UTC),
    )

    assert result.accepted is True
    assert publish.await_args.args[0][0].message_id == current.message_id
    assert not any(
        "UPDATE archive_documents SET lifecycle" in query
        for query, _ in connection.queries
    )


@pytest.mark.asyncio
async def test_safe_delete_policy_is_checked_under_lock(monkeypatch) -> None:
    connection = DeletionConnection(
        {
            "_id": 1,
            "lifecycle": "active",
            "head_version_id": uuid4(),
            "head_version_internal_id": 2,
            "expires": None,
        }
    )
    install_pool(monkeypatch, connection)
    with pytest.raises(ForbiddenError):
        await ArchivePostgresModel.request_deletion(
            "payroll", uuid4(), safe=True, now=datetime.datetime.now(tz=datetime.UTC)
        )

    connection.aggregate["expires"] = datetime.datetime.now(
        tz=datetime.UTC
    ) + datetime.timedelta(days=1)
    with pytest.raises(UnsafeArchiveDeleteError):
        await ArchivePostgresModel.request_deletion(
            "payroll", uuid4(), safe=True, now=datetime.datetime.now(tz=datetime.UTC)
        )
    assert all("FOR UPDATE OF aggregate" in query for query, _ in connection.queries)


@pytest.mark.asyncio
async def test_endpoint_returns_202_without_object_io(monkeypatch) -> None:
    request_deletion = AsyncMock(return_value=SimpleNamespace(accepted=True))
    monkeypatch.setattr(ArchivePostgresModel, "request_deletion", request_deletion)
    request = SimpleNamespace(args={}, app=SimpleNamespace(ctx=SimpleNamespace()))

    result = await _delete(request, "payroll", uuid4())  # type: ignore[arg-type]

    assert result.status == 202
    request_deletion.assert_awaited_once()


@pytest.mark.asyncio
async def test_command_publication_has_explicit_deterministic_message_id() -> None:
    current = command(kind="history", policy="retain-metadata")
    jetstream = AsyncMock()

    await publish_deletion_command(jetstream, current)
    await publish_deletion_command(jetstream, current)

    calls = jetstream.publish.await_args_list
    assert {call.kwargs["headers"]["Nats-Msg-Id"] for call in calls} == {
        str(current.message_id)
    }
    assert orjson.loads(calls[0].args[1]) == current.dict()
    recreated = replace(
        current,
        aggregate_id=current.aggregate_id + 1,
        version_internal_id=current.version_internal_id + 1,
    )
    assert recreated.message_id != current.message_id


@pytest.mark.asyncio
async def test_reconciler_deletes_then_transitions_then_publishes(monkeypatch) -> None:
    current = command(kind="history", policy="retain-metadata")
    calls = []

    async def delete(_key):
        calls.append("storage")

    async def validate(_command):
        calls.append("validate")
        return True

    async def complete(_command):
        calls.append("database")
        return DeletionCompletion(
            representation_transitioned=True, version_transitioned=True
        )

    async def event(*_args, **_kwargs):
        calls.append("event")

    monkeypatch.setattr(ArchivePostgresModel, "validate_deletion_command", validate)
    monkeypatch.setattr(ArchivePostgresModel, "complete_deletion", complete)
    monkeypatch.setattr(
        "xarta.services.v1.archive.reconciler.publish_archive_event", event
    )
    registry = SimpleNamespace(backend=lambda *_args: SimpleNamespace(delete=delete))

    await ArchiveDeletionReconciler(registry, object(), 300).process(current)  # type: ignore[arg-type]

    assert calls == ["validate", "storage", "database", "event"]


@pytest.mark.asyncio
async def test_missing_object_is_success_and_can_finalize(monkeypatch) -> None:
    current = command()
    backend = SimpleNamespace(delete=AsyncMock(side_effect=FileNotFoundError()))
    monkeypatch.setattr(
        ArchivePostgresModel, "validate_deletion_command", AsyncMock(return_value=True)
    )
    complete = AsyncMock(
        return_value=DeletionCompletion(
            representation_transitioned=True, finalized=True
        )
    )
    monkeypatch.setattr(ArchivePostgresModel, "complete_deletion", complete)
    event = AsyncMock()
    monkeypatch.setattr(
        "xarta.services.v1.archive.reconciler.publish_archive_event", event
    )

    await ArchiveDeletionReconciler(
        SimpleNamespace(backend=lambda *_args: backend),  # type: ignore[arg-type]
        object(),
        300,
    ).process(current)

    complete.assert_awaited_once_with(current)
    event.assert_awaited_once()


@pytest.mark.asyncio
async def test_stale_command_is_acked_without_storage_or_completion(
    monkeypatch,
) -> None:
    current = command()
    backend = SimpleNamespace(delete=AsyncMock())
    monkeypatch.setattr(
        ArchivePostgresModel, "validate_deletion_command", AsyncMock(return_value=False)
    )
    complete = AsyncMock()
    monkeypatch.setattr(ArchivePostgresModel, "complete_deletion", complete)

    await ArchiveDeletionReconciler(
        SimpleNamespace(backend=lambda *_args: backend),  # type: ignore[arg-type]
        object(),
        300,
    ).process(current)

    backend.delete.assert_not_awaited()
    complete.assert_not_awaited()


@pytest.mark.asyncio
async def test_history_event_requires_actual_transition(monkeypatch) -> None:
    current = command(kind="history", policy="retain-metadata")
    monkeypatch.setattr(
        ArchivePostgresModel, "validate_deletion_command", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(
        ArchivePostgresModel,
        "complete_deletion",
        AsyncMock(return_value=DeletionCompletion()),
    )
    event = AsyncMock()
    monkeypatch.setattr(
        "xarta.services.v1.archive.reconciler.publish_archive_event", event
    )
    backend = SimpleNamespace(delete=AsyncMock())

    await ArchiveDeletionReconciler(
        SimpleNamespace(backend=lambda *_args: backend),  # type: ignore[arg-type]
        object(),
        300,
    ).process(current)

    event.assert_not_awaited()


@pytest.mark.asyncio
async def test_pending_scan_republishes_deterministic_commands(monkeypatch) -> None:
    pending = [command(), command(kind="history", policy="latest-only")]
    monkeypatch.setattr(
        ArchivePostgresModel,
        "pending_deletion_commands",
        AsyncMock(return_value=pending),
    )
    jetstream = AsyncMock()
    reconciler = ArchiveDeletionReconciler(None, jetstream, 300)  # type: ignore[arg-type]

    assert await reconciler.republish_pending() == 2

    assert [
        call.kwargs["headers"]["Nats-Msg-Id"]
        for call in jetstream.publish.await_args_list
    ] == [str(item.message_id) for item in pending]


@pytest.mark.asyncio
async def test_pending_scan_keyset_reaches_later_commands(monkeypatch) -> None:
    first = replace(command(), aggregate_id=1, representation_internal_id=1)
    second = replace(command(), aggregate_id=1, representation_internal_id=2)
    later = replace(command(), aggregate_id=2, representation_internal_id=1)
    pending = AsyncMock(side_effect=[[first, second], [later]])
    monkeypatch.setattr(ArchivePostgresModel, "pending_deletion_commands", pending)
    monkeypatch.setattr("xarta.services.v1.archive.reconciler.RECONCILER_BATCH_SIZE", 2)
    jetstream = AsyncMock()
    reconciler = ArchiveDeletionReconciler(None, jetstream, 300)  # type: ignore[arg-type]

    assert await reconciler.republish_pending() == 2
    assert await reconciler.republish_pending() == 1

    assert pending.await_args_list[0].kwargs["after"] is None
    assert pending.await_args_list[1].kwargs["after"] == (1, 2)
    assert jetstream.publish.await_args_list[-1].kwargs["headers"][
        "Nats-Msg-Id"
    ] == str(later.message_id)


@pytest.mark.asyncio
async def test_coordinate_mismatch_is_terminal_invalid(monkeypatch) -> None:
    current = command()
    connection = DeletionConnection(
        {
            "archive": current.archive,
            "document_id": current.document_id,
            "lifecycle": "deleting",
            "version_id": current.version_id,
            "representation_id": current.representation_id,
            "state": "available",
            "backend": current.backend,
            "backend_revision": current.backend_revision,
            "storage_key": "different/key",
            "history_cleanup_policy": None,
            "head": True,
        }
    )
    install_pool(monkeypatch, connection)

    with pytest.raises(ValueError, match="pinned storage coordinates"):
        await ArchivePostgresModel.validate_deletion_command(current)

    assert connection.queries[0][1] == (
        current.aggregate_id,
        current.version_internal_id,
        current.representation_internal_id,
    )


@pytest.mark.asyncio
async def test_completion_clears_intent_and_finalizes_only_without_available_versions(
    monkeypatch,
) -> None:
    current = command()

    class CompletionConnection(DeletionConnection):
        calls = 0

        async def fetchval(self, query, *args):
            self.queries.append((query, args))
            if "UPDATE archive_document_representations" in query:
                return True
            if "UPDATE archive_document_versions" in query:
                return True
            if "SELECT EXISTS" in query:
                return False
            raise AssertionError(query)

    connection = CompletionConnection(
        {"_id": current.aggregate_id, "lifecycle": "deleting"}
    )
    install_pool(monkeypatch, connection)

    completion = await ArchivePostgresModel.complete_deletion(current)

    assert completion == DeletionCompletion(
        representation_transitioned=True,
        version_transitioned=True,
        finalized=True,
    )
    sql = " ".join(query for query, _ in connection.queries)
    assert "history_cleanup_policy = NULL" in sql
    assert "state = 'available'" in sql
    assert "lifecycle = 'deleted'" in sql
    assert "head_version_id = NULL" in sql
    assert "DELETE FROM archive_idempotency" in sql
    assert "DELETE FROM archive_document_versions" in sql
    assert "DELETE FROM archive_documents" not in sql


@pytest.mark.asyncio
async def test_repeated_delete_of_tombstone_is_200_without_publication(
    monkeypatch,
) -> None:
    connection = DeletionConnection(
        {
            "_id": 7,
            "lifecycle": "deleted",
            "head_version_id": None,
            "head_version_internal_id": None,
            "expires": None,
        }
    )
    install_pool(monkeypatch, connection)
    publish = AsyncMock()
    monkeypatch.setattr(ArchivePostgresModel, "_publish_commands", publish)

    result = await ArchivePostgresModel.request_deletion(
        "payroll",
        uuid4(),
        safe=True,
        now=datetime.datetime.now(tz=datetime.UTC),
    )

    assert result.accepted is False
    publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_consumer_naks_failure_and_never_acks(monkeypatch) -> None:
    current = command()
    message = SimpleNamespace(
        data=orjson.dumps(current.dict()),
        metadata=SimpleNamespace(num_delivered=3),
        nak=AsyncMock(),
        ack=AsyncMock(),
        term=AsyncMock(),
        in_progress=AsyncMock(),
    )

    class Subscription:
        calls = 0

        async def fetch(self, *_args, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                return [message]
            raise asyncio.CancelledError

    reconciler = ArchiveDeletionReconciler(None, None, 300)  # type: ignore[arg-type]
    process = AsyncMock(side_effect=RuntimeError("db"))
    monkeypatch.setattr(ArchiveDeletionReconciler, "process", process)

    with pytest.raises(asyncio.CancelledError):
        await reconciler.run(Subscription())

    message.nak.assert_awaited_once_with(delay=4.0)
    message.ack.assert_not_awaited()
    message.term.assert_not_awaited()


@pytest.mark.asyncio
async def test_consumer_processes_batch_concurrently_with_independent_results(
    monkeypatch,
) -> None:
    commands = [command() for _ in range(3)]
    messages = [
        SimpleNamespace(
            data=orjson.dumps(current.dict()),
            metadata=SimpleNamespace(num_delivered=1),
            nak=AsyncMock(),
            ack=AsyncMock(),
            term=AsyncMock(),
            in_progress=AsyncMock(),
        )
        for current in commands
    ]

    class Subscription:
        calls = 0

        async def fetch(self, *_args, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                return messages
            raise asyncio.CancelledError

    active = 0
    maximum = 0

    async def process(_self, current):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.01)
        active -= 1
        if current.representation_id == commands[1].representation_id:
            raise RuntimeError("storage unavailable")

    monkeypatch.setattr(ArchiveDeletionReconciler, "process", process)
    monkeypatch.setattr(ArchiveDeletionReconciler, "republish_pending", AsyncMock())
    reconciler = ArchiveDeletionReconciler(
        None, None, max_backoff_seconds=300, concurrency=2  # type: ignore[arg-type]
    )

    with pytest.raises(asyncio.CancelledError):
        await reconciler.run(Subscription())

    assert maximum == 2
    messages[0].ack.assert_awaited_once()
    messages[1].nak.assert_awaited_once_with(delay=1.0)
    messages[1].ack.assert_not_awaited()
    messages[2].ack.assert_awaited_once()


@pytest.mark.asyncio
async def test_consumer_ignores_idle_fetch_timeout(monkeypatch) -> None:
    class Subscription:
        calls = 0

        async def fetch(self, *_args, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                raise TimeoutError
            raise asyncio.CancelledError

    monkeypatch.setattr(ArchiveDeletionReconciler, "republish_pending", AsyncMock())
    subscription = Subscription()

    with pytest.raises(asyncio.CancelledError):
        await ArchiveDeletionReconciler(None, None, 300).run(subscription)  # type: ignore[arg-type]

    assert subscription.calls == 2


@pytest.mark.asyncio
async def test_consumer_survives_fetch_and_ack_failures(monkeypatch) -> None:
    current = command()
    message = SimpleNamespace(
        data=orjson.dumps(current.dict()),
        metadata=SimpleNamespace(num_delivered=1),
        nak=AsyncMock(),
        ack=AsyncMock(side_effect=RuntimeError("ack unavailable")),
        term=AsyncMock(),
        in_progress=AsyncMock(),
    )

    class Subscription:
        calls = 0

        async def fetch(self, *_args, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("subscription unavailable")
            if self.calls == 2:
                return [message]
            raise asyncio.CancelledError

    monkeypatch.setattr(ArchiveDeletionReconciler, "republish_pending", AsyncMock())
    monkeypatch.setattr(ArchiveDeletionReconciler, "process", AsyncMock())
    subscription = Subscription()

    with pytest.raises(asyncio.CancelledError):
        await ArchiveDeletionReconciler(None, None, 300).run(subscription)  # type: ignore[arg-type]

    assert subscription.calls == 3
    message.ack.assert_awaited_once()


@pytest.mark.asyncio
async def test_existing_durable_consumer_is_not_added_again(monkeypatch) -> None:
    subscription = object()
    jetstream = SimpleNamespace(
        consumer_info=AsyncMock(return_value=object()),
        add_consumer=AsyncMock(),
        pull_subscribe=AsyncMock(return_value=subscription),
    )
    monkeypatch.setattr(
        "xarta.services.v1.archive.nats.ArchiveNATSModel.jetstream",
        classmethod(lambda cls: jetstream),
    )
    run = AsyncMock()
    monkeypatch.setattr(ArchiveDeletionReconciler, "run", run)
    app = SimpleNamespace(ctx=SimpleNamespace(archive_storage=object()))

    await _start_reconciler(app)
    await app.ctx.archive_deletion_reconciler_task
    await _stop_reconciler(app)

    jetstream.add_consumer.assert_not_awaited()
    jetstream.pull_subscribe.assert_awaited_once_with(
        stream="ARCHIVE_COMMANDS",
        subject="archive.commands.delete",
        durable="archive-deletion",
    )
    run.assert_awaited_once_with(subscription)


def test_command_stream_is_durable_work_queue_without_age_expiry() -> None:
    config = _archive_commands_stream_config()
    assert config.retention == "workqueue"
    assert config.storage == "file"
    assert config.max_age == 0
    assert config.subjects == ["archive.commands.delete"]


def test_migrations_remove_database_queues_and_use_native_lifecycle_enum() -> None:
    from pathlib import Path

    root = Path(__file__).parents[2]
    archive = root / "db/archive"
    deploy = (archive / "deploy/00000.archive.sql").read_text()
    verify = (archive / "verify/00000.archive.sql").read_text()
    plan = (archive / "sqitch.plan").read_text()

    assert "CREATE TYPE public.archive_lifecycle AS ENUM" in deploy
    assert "CREATE TYPE public.archive_history_cleanup_policy AS ENUM" in deploy
    assert "archive_document_versions_history_cleanup_idx" in deploy
    assert "CREATE TABLE public.archive_deletion_jobs" not in deploy
    assert "archive_event_outbox" not in verify
    assert "archive_deletion_jobs" not in verify
    assert "00011.archive-events" not in plan
    assert "00013.archive-history-deletion-jobs" not in plan
