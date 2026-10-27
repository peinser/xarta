from __future__ import annotations

import asyncio
import datetime

from contextlib import asynccontextmanager
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
from xarta.services.v1.archive.events import archive_event_id
from xarta.services.v1.archive.nats import publish_deletion_command
from xarta.services.v1.archive.reconciler import ArchiveDeletionReconciler
from xarta.services.v1.archive.reconciler import _command
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

    @asynccontextmanager
    async def acquire(self):
        self.connection.acquired += 1
        try:
            yield self.connection
        finally:
            self.connection.acquired -= 1


class DeletionConnection:
    def __init__(self, aggregate=None, versions=None) -> None:
        self.aggregate = aggregate
        self.versions = versions or []
        self.tombstone = None
        self.acquired = 0
        self.queries: list[tuple[str, tuple]] = []

    def transaction(self):
        return AsyncContext()

    async def fetchrow(self, query, *args):
        self.queries.append((query, args))
        if "SELECT deleted_head_version_id" in query:
            return self.tombstone
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


def command() -> DeletionCommand:
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
    current = command()
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


def test_removed_history_command_is_rejected() -> None:
    payload = {
        **command().dict(),
        "kind": "history",
        "history_policy": "retain-metadata",
    }
    with pytest.raises(ValueError, match="command shape"):
        _command(orjson.dumps(payload))


@pytest.mark.asyncio
async def test_active_document_command_never_deletes_content(monkeypatch) -> None:
    current = command()
    connection = DeletionConnection({"state": "available", "lifecycle": "active"})
    install_pool(monkeypatch, connection)
    backend = SimpleNamespace(delete=AsyncMock())
    complete = AsyncMock()
    monkeypatch.setattr(ArchivePostgresModel, "complete_deletion", complete)
    jetstream = AsyncMock()
    await ArchiveDeletionReconciler(
        SimpleNamespace(backend=lambda *_args: backend),  # type: ignore[arg-type]
        jetstream,
        300,
    ).process(current)
    backend.delete.assert_not_awaited()
    complete.assert_not_awaited()
    jetstream.publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconciler_deletes_then_transitions_then_publishes(monkeypatch) -> None:
    current = command()
    calls = []

    async def delete(_key):
        calls.append("storage")

    async def validate(_command):
        calls.append("validate")
        return True

    async def complete(_command):
        calls.append("database")
        return DeletionCompletion(
            representation_transitioned=True, version_transitioned=True, finalized=True
        )

    async def event(*_args, **_kwargs):
        calls.append("event")

    monkeypatch.setattr(ArchivePostgresModel, "validate_deletion_command", validate)
    monkeypatch.setattr(ArchivePostgresModel, "complete_deletion", complete)
    monkeypatch.setattr(ArchivePostgresModel, "publish_deleted_event", event)
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
    monkeypatch.setattr(ArchivePostgresModel, "publish_deleted_event", event)
    jetstream = object()

    await ArchiveDeletionReconciler(
        SimpleNamespace(backend=lambda *_args: backend),  # type: ignore[arg-type]
        jetstream,
        300,
    ).process(current)

    complete.assert_awaited_once_with(current)
    event.assert_awaited_once_with(
        jetstream,
        archive=current.archive,
        document_id=current.document_id,
        aggregate_id=current.aggregate_id,
    )


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
    install_pool(monkeypatch, DeletionConnection())
    jetstream = AsyncMock()

    await ArchiveDeletionReconciler(
        SimpleNamespace(backend=lambda *_args: backend),  # type: ignore[arg-type]
        jetstream,
        300,
    ).process(current)

    backend.delete.assert_not_awaited()
    complete.assert_not_awaited()
    jetstream.publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_transition_without_tombstone_does_not_publish(monkeypatch) -> None:
    current = command()
    monkeypatch.setattr(
        ArchivePostgresModel, "validate_deletion_command", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(
        ArchivePostgresModel,
        "complete_deletion",
        AsyncMock(return_value=DeletionCompletion()),
    )
    backend = SimpleNamespace(delete=AsyncMock())
    install_pool(monkeypatch, DeletionConnection())
    jetstream = AsyncMock()

    await ArchiveDeletionReconciler(
        SimpleNamespace(backend=lambda *_args: backend),  # type: ignore[arg-type]
        jetstream,
        300,
    ).process(current)

    jetstream.publish.assert_not_awaited()


def tombstone():
    return {
        "deleted_head_version_id": uuid4(),
        "deleted_head_version_internal_id": 29,
        "updated_at": datetime.datetime(2030, 1, 2, 12, 30, tzinfo=datetime.UTC),
    }


def delivery(current):
    return SimpleNamespace(
        data=orjson.dumps(current.dict()),
        metadata=SimpleNamespace(num_delivered=1),
        nak=AsyncMock(),
        ack=AsyncMock(),
        term=AsyncMock(),
        in_progress=AsyncMock(),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("accepted_before_failure", [False, True])
async def test_completed_deletion_redelivery_republishes_original_fact(
    monkeypatch, accepted_before_failure
) -> None:
    current = command()
    connection = DeletionConnection()
    install_pool(monkeypatch, connection)
    stored = tombstone()  # The authoritative HEAD differs from the command's HEAD.
    backend = SimpleNamespace(delete=AsyncMock())

    async def validate(_command):
        return connection.tombstone is None

    async def complete(_command):
        connection.tombstone = stored
        return DeletionCompletion(True, True, finalized=True)

    completion = AsyncMock(side_effect=complete)
    monkeypatch.setattr(ArchivePostgresModel, "validate_deletion_command", validate)
    monkeypatch.setattr(ArchivePostgresModel, "complete_deletion", completion)
    attempts = []
    accepted = []

    async def publish(subject, payload, *, headers):
        assert connection.acquired == 0
        assert connection.tombstone == stored
        if orjson.loads(payload)["type"] != "xarta.archive.document.deleted":
            return
        event = (subject, payload, headers["Nats-Msg-Id"])
        attempts.append(event)
        if len(attempts) == 1:
            if accepted_before_failure:
                accepted.append(event)
            raise TimeoutError("PubAck unavailable")
        accepted.append(event)

    reconciler = ArchiveDeletionReconciler(
        SimpleNamespace(backend=lambda *_args: backend),  # type: ignore[arg-type]
        SimpleNamespace(publish=publish),
        300,
    )
    first, retry = delivery(current), delivery(current)
    retry.metadata.num_delivered = 2
    await reconciler._process_message(first, asyncio.Semaphore(1))
    first.nak.assert_awaited_once_with(delay=1.0)
    first.ack.assert_not_awaited()
    first.term.assert_not_awaited()
    assert len(accepted) == int(accepted_before_failure)

    await reconciler._process_message(retry, asyncio.Semaphore(1))
    retry.ack.assert_awaited_once()
    retry.nak.assert_not_awaited()
    retry.term.assert_not_awaited()
    backend.delete.assert_awaited_once_with(current.storage_key)
    completion.assert_awaited_once_with(current)
    assert len(accepted) == 1 + int(accepted_before_failure)
    assert len(attempts) == 2
    assert attempts[0] == attempts[1]
    payload = orjson.loads(attempts[0][1])
    assert payload["time"] == "2030-01-02T12:30:00Z"
    assert payload["data"] == {
        "archive": current.archive,
        "id": str(current.document_id),
        "head": str(stored["deleted_head_version_id"]),
        "outcome": "deleted",
    }
    assert (
        payload["id"]
        == attempts[0][2]
        == str(
            archive_event_id(
                "xarta.archive.document.deleted",
                current.archive,
                current.document_id,
                current.aggregate_id,
                stored["deleted_head_version_internal_id"],
            )
        )
    )


@pytest.mark.asyncio
async def test_overlapping_redelivery_waits_for_publication_before_ack(
    monkeypatch,
) -> None:
    current = command()
    connection = DeletionConnection()
    install_pool(monkeypatch, connection)
    backend = SimpleNamespace(delete=AsyncMock())

    async def validate(_command):
        return connection.tombstone is None

    async def complete(_command):
        connection.tombstone = tombstone()
        return DeletionCompletion(True, True, finalized=True)

    monkeypatch.setattr(ArchivePostgresModel, "validate_deletion_command", validate)
    monkeypatch.setattr(ArchivePostgresModel, "complete_deletion", complete)
    started = [asyncio.Event(), asyncio.Event()]
    acknowledged = asyncio.Event()
    attempts = []

    async def publish(_subject, payload, *, headers):
        assert connection.acquired == 0
        attempts.append(payload)
        started[len(attempts) - 1].set()
        await acknowledged.wait()

    reconciler = ArchiveDeletionReconciler(
        SimpleNamespace(backend=lambda *_args: backend),  # type: ignore[arg-type]
        SimpleNamespace(publish=publish),
        300,
    )
    first, retry = delivery(current), delivery(current)
    tasks = [
        asyncio.create_task(reconciler._process_message(first, asyncio.Semaphore(1)))
    ]
    try:
        await asyncio.wait_for(started[0].wait(), timeout=1)
        tasks.append(
            asyncio.create_task(
                reconciler._process_message(retry, asyncio.Semaphore(1))
            )
        )
        await asyncio.wait_for(started[1].wait(), timeout=1)
        first.ack.assert_not_awaited()
        retry.ack.assert_not_awaited()
        acknowledged.set()
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=1)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert attempts[0] == attempts[1]
    backend.delete.assert_awaited_once()
    first.ack.assert_awaited_once()
    retry.ack.assert_awaited_once()


@pytest.mark.asyncio
async def test_deleted_event_lookup_requires_matching_identity_and_deleted_lifecycle(
    monkeypatch,
) -> None:
    current = command()
    connection = DeletionConnection()
    install_pool(monkeypatch, connection)
    jetstream = AsyncMock()

    await ArchivePostgresModel.publish_deleted_event(
        jetstream,
        aggregate_id=current.aggregate_id,
        archive=current.archive,
        document_id=current.document_id,
    )

    jetstream.publish.assert_not_awaited()
    query, args = connection.queries[0]
    assert "WHERE _id = $1 AND archive = $2" in query
    assert "AND document_id = $3 AND lifecycle = 'deleted'" in query
    assert args == (current.aggregate_id, current.archive, current.document_id)


@pytest.mark.asyncio
async def test_pending_scan_republishes_deterministic_commands(monkeypatch) -> None:
    pending = [command(), command()]
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
async def test_completion_finalizes_only_without_available_versions(
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
    assert "aggregate.lifecycle = 'deleting'" in sql
    assert "state = 'available'" in sql
    assert "lifecycle = 'deleted'" in sql
    assert "head_version_id = NULL" in sql
    assert "DELETE FROM archive_idempotency" in sql
    assert "DELETE FROM archive_document_versions" in sql
    assert "DELETE FROM archive_documents" not in sql
    query, args = next(
        (query, args)
        for query, args in connection.queries
        if "SET lifecycle = 'deleted'" in query
    )
    assert "deleted_head_version_id = head.version_id" in query
    assert "deleted_head_version_internal_id = head._id" in query
    assert "head._id = aggregate.head_version_id" in query
    assert args == (current.aggregate_id,)


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
    document_only = (archive / "deploy/00002.document-only-deletion.sql").read_text()
    assert "DROP COLUMN history_cleanup_policy" in document_only
    assert "DROP TYPE public.archive_history_cleanup_policy" in document_only
    assert "content removal requires logical document deletion" in document_only
    assert "CREATE TABLE public.archive_deletion_jobs" not in deploy
    assert "archive_event_outbox" not in verify
    assert "archive_deletion_jobs" not in verify
    assert "00011.archive-events" not in plan
    assert "00013.archive-history-deletion-jobs" not in plan
