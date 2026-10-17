from __future__ import annotations

import asyncio
import datetime
import hashlib
import os

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import patch
from uuid import UUID
from uuid import uuid4

import orjson
import pytest

from botocore.exceptions import ClientError  # type: ignore[import-untyped]

from xarta.services.v1.archive.db import ArchiveConflictError
from xarta.services.v1.archive.db import ArchivePostgresModel
from xarta.services.v1.archive.db import RepresentationWrite
from xarta.services.v1.archive.db import VersionWrite
from xarta.services.v1.archive.storage import ArchivePolicy
from xarta.services.v1.archive.storage import ArchiveStorageRegistry
from xarta.services.v1.archive.storage import ArchiveStorageSessions
from xarta.services.v1.archive.storage import FilesystemStorageBackend
from xarta.services.v1.archive.storage import S3StorageBackend
from xarta.services.v1.archive.store import _parse_snapshot


class AsyncContext:
    def __init__(self, value=None, error: Exception | None = None) -> None:
        self.value = value
        self.error = error

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *_args):
        if self.error:
            raise self.error


class FakePool:
    def __init__(self, connection) -> None:
        self.connection = connection

    def acquire(self):
        return AsyncContext(self.connection)


class Connection:
    def __init__(
        self,
        *,
        head_id=None,
        head_parent_id=None,
        snapshot_fingerprint=None,
        lifecycle="active",
        commit_error: Exception | None = None,
    ) -> None:
        self.head_id = head_id
        self.head_parent_id = head_parent_id
        self.snapshot_fingerprint = snapshot_fingerprint
        self.lifecycle = lifecycle
        self.commit_error = commit_error
        self.executed: list[tuple[str, tuple]] = []
        self.insert_error: BaseException | None = None
        self.jetstream = AsyncMock()

    def transaction(self):
        return AsyncContext(error=self.commit_error)

    async def fetchrow(self, query, *_args):
        if "archive_idempotency" in query:
            return None
        if "SELECT _id FROM archive_documents" in query:
            return {"_id": 1}
        if "FOR UPDATE OF aggregate" in query:
            return {
                "_id": 1,
                "head_version_id": 10 if self.head_id else None,
                "head_public_version_id": self.head_id,
                "snapshot_fingerprint": self.snapshot_fingerprint,
                "lifecycle": self.lifecycle,
            }
        if "jsonb_agg" in query:
            return {
                "default_representation_id": self.default_representation_id,
                "parent_version_id": self.head_parent_id,
                "representations": [
                    item.public_dict() for item in self.representations
                ],
            }
        raise AssertionError(query)

    async def fetch(self, query, *_args):
        assert "representation" in query
        return []

    async def fetchval(self, query, *args):
        self.executed.append((query, args))
        if self.insert_error:
            raise self.insert_error
        return 20

    async def execute(self, query, *args):
        self.executed.append((query, args))

    async def executemany(self, query, args):
        self.executed.append((query, tuple(args)))


def representation(content: bytes, content_type: str) -> RepresentationWrite:
    identity = uuid4()
    return RepresentationWrite(
        representation_id=identity,
        name=None,
        content_type=content_type,
        metadata={},
        backend="primary",
        backend_revision="v1",
        storage_key=f"default/document/version/{identity}",
        checksum=hashlib.sha512(content).digest(),
        size=len(content),
    )


def write(*, parent=None, snapshot_fingerprint=b"s" * 64) -> VersionWrite:
    representations = (
        representation(b"pdf", "application/pdf"),
        representation(b"xml", "application/xml"),
    )
    return VersionWrite(
        archive="payroll",
        document_id=uuid4(),
        version_id=uuid4(),
        parent_version_id=parent,
        idempotency_key="submission:document:version",
        fingerprint=b"f" * 64,
        snapshot_fingerprint=snapshot_fingerprint,
        created=datetime.datetime(2030, 1, 1, tzinfo=datetime.UTC),
        expires=datetime.datetime(2040, 1, 1, tzinfo=datetime.UTC),
        document_type="payslip",
        metadata={"employee": "123"},
        default_representation_id=representations[0].representation_id,
        representations=representations,
    )


def install(monkeypatch, connection: Connection) -> None:
    monkeypatch.setattr(
        ArchivePostgresModel,
        "pool",
        classmethod(lambda cls: FakePool(connection)),
    )
    monkeypatch.setattr(
        ArchivePostgresModel,
        "_jetstream",
        staticmethod(lambda: connection.jetstream),
    )


@pytest.mark.asyncio
async def test_complete_snapshot_persists_every_representation_before_sealing(
    monkeypatch,
) -> None:
    connection = Connection()
    install(monkeypatch, connection)
    candidate = write()
    persisted = []

    async def persist(item):
        persisted.append(item.representation_id)
        return True

    result = await ArchivePostgresModel.prepare_version(
        candidate, ArchivePolicy(), persist, AsyncMock()
    )

    assert result.outcome == "created"
    assert set(result.dict()) == {"archive", "id", "version", "outcome"}
    assert result.dict()["id"] == str(candidate.document_id)
    assert result.dict()["version"] == {"id": str(candidate.version_id), "parent": None}
    assert persisted == [item.representation_id for item in candidate.representations]
    queries = [query for query, _ in connection.executed]
    representation_inserts = [
        arguments
        for query, arguments in connection.executed
        if "INSERT INTO archive_document_representations" in query
    ]
    assert len(representation_inserts) == 1
    assert len(representation_inserts[0]) == 2
    assert next(query for query in queries if "SET sealed = true" in query)
    assert next(query for query in queries if "SET head_version_id" in query)
    assert queries.index(
        next(query for query in queries if "SET sealed = true" in query)
    ) < queries.index(
        next(query for query in queries if "SET head_version_id" in query)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("backend_kind", ["filesystem", "s3"])
async def test_failed_write_cleanup_cannot_delete_committed_retry(
    monkeypatch, tmp_path, backend_kind
) -> None:
    if backend_kind == "filesystem":
        backend = FilesystemStorageBackend(os.fspath(tmp_path))
    else:
        objects = {}

        async def put_object(**kwargs):
            key = kwargs["Key"]
            if key in objects:
                raise ClientError(
                    {
                        "ResponseMetadata": {"HTTPStatusCode": 412},
                        "Error": {"Code": "PreconditionFailed"},
                    },
                    "PutObject",
                )
            objects[key] = kwargs["Body"]

        async def get_object(**kwargs):
            key = kwargs["Key"]
            if key not in objects:
                raise ClientError(
                    {
                        "ResponseMetadata": {"HTTPStatusCode": 404},
                        "Error": {"Code": "NoSuchKey"},
                    },
                    "GetObject",
                )
            return {
                "Body": AsyncContext(
                    SimpleNamespace(read=AsyncMock(return_value=objects[key]))
                )
            }

        async def delete_object(**kwargs):
            objects.pop(kwargs["Key"], None)

        backend = S3StorageBackend({"bucket": "archive", "prefix": "xarta"})
        backend._client_manager = SimpleNamespace(
            get=AsyncMock(
                return_value=SimpleNamespace(
                    put_object=put_object,
                    get_object=get_object,
                    delete_object=delete_object,
                )
            )
        )

    target = SimpleNamespace(
        backend=backend, backend_name="primary", backend_revision="v1"
    )
    data = b"invoice bytes"
    request = SimpleNamespace(
        form={
            "manifest": orjson.dumps(
                {
                    "created_at": "2030-01-01T00:00:00+00:00",
                    "metadata": {"invoice_number": "INV-2030-0042"},
                    "representations": {
                        "items": [
                            {"field": "invoice", "content": {"type": "text/plain"}}
                        ]
                    },
                }
            )
        },
        files={"invoice": SimpleNamespace(body=data)},
        headers={"Idempotency-Key": "cleanup-race-0042"},
        app=SimpleNamespace(
            ctx=SimpleNamespace(
                archive_storage=SimpleNamespace(resolve=lambda _: target)
            )
        ),
    )
    first = (await _parse_snapshot(request, "default")).write
    second = (await _parse_snapshot(request, "default")).write
    assert first.document_id == second.document_id
    assert first.version_id == second.version_id
    assert first.fingerprint == second.fingerprint
    assert first.snapshot_fingerprint == second.snapshot_fingerprint
    assert first.idempotency_key == second.idempotency_key

    async def persist(item):
        return await backend.put(item.storage_key, data, item.content_type)

    cleanup_started = asyncio.Event()
    allow_cleanup = asyncio.Event()

    async def delayed_cleanup(item):
        cleanup_started.set()
        await allow_cleanup.wait()
        await backend.delete(item.storage_key)

    failed_connection = Connection()
    failed_connection.insert_error = RuntimeError("database write failed")
    install(monkeypatch, failed_connection)
    attempt = asyncio.create_task(
        ArchivePostgresModel.prepare_version(
            first,
            ArchivePolicy(),
            persist,
            delayed_cleanup,
        )
    )
    try:
        # A has left its failed transaction, but compensation has not deleted bytes.
        await asyncio.wait_for(cleanup_started.wait(), timeout=5)
        committed_connection = Connection()
        install(monkeypatch, committed_connection)
        result = await ArchivePostgresModel.prepare_version(
            second, ArchivePolicy(), persist, AsyncMock()
        )
        assert result.outcome == "created"
        inserted = next(
            rows
            for query, rows in committed_connection.executed
            if "INSERT INTO archive_document_representations" in query
        )
        committed_key = inserted[0][7]  # storage_key column in the actual INSERT.
    finally:
        # B has committed its reference before A is allowed to compensate.
        allow_cleanup.set()
        with pytest.raises(RuntimeError, match="database write failed"):
            await asyncio.wait_for(attempt, timeout=5)

    assert await backend.get(committed_key) == data
    with pytest.raises(FileNotFoundError):
        await backend.get(first.representations[0].storage_key)

    recorded = next(
        arguments[-1]
        for query, arguments in committed_connection.executed
        if "INSERT INTO archive_idempotency" in query
    )
    replay_connection = Connection()
    original_fetchrow = replay_connection.fetchrow

    async def fetch_recorded_result(query, *args):
        if "FROM archive_idempotency" in query:
            return {
                "request_fingerprint": second.fingerprint,
                "response": recorded,
                "parent_version_id": None,
            }
        if "SELECT aggregate._id AS aggregate_id" in query:
            return {
                "aggregate_id": 1,
                "version_internal_id": 20,
                "parent_version_id": None,
                "metadata": second.metadata,
                "document_type": second.document_type,
                "created": second.created,
            }
        return await original_fetchrow(query, *args)

    monkeypatch.setattr(replay_connection, "fetchrow", fetch_recorded_result)
    install(monkeypatch, replay_connection)
    third = (await _parse_snapshot(request, "default")).write
    replay_persist = AsyncMock()
    replay = await ArchivePostgresModel.prepare_version(
        third, ArchivePolicy(), replay_persist, AsyncMock()
    )
    assert replay.replayed
    assert replay.dict() == result.dict()
    replay_persist.assert_not_awaited()
    with pytest.raises(FileNotFoundError):
        await backend.get(third.representations[0].storage_key)


@pytest.mark.asyncio
async def test_partial_storage_failure_rolls_back_only_created_objects(
    monkeypatch,
) -> None:
    connection = Connection()
    install(monkeypatch, connection)
    candidate = write()
    rolled_back = []

    async def persist(item):
        if item is candidate.representations[1]:
            raise RuntimeError("storage failed")
        return True

    async def rollback(item):
        rolled_back.append(item.representation_id)

    with pytest.raises(RuntimeError, match="storage failed"):
        await ArchivePostgresModel.prepare_version(
            candidate, ArchivePolicy(), persist, rollback
        )
    assert rolled_back == [candidate.representations[0].representation_id]


@pytest.mark.asyncio
async def test_representation_writes_are_bounded_and_settle_before_rollback(
    monkeypatch,
) -> None:
    connection = Connection()
    install(monkeypatch, connection)
    candidate = write()
    both_started = asyncio.Event()
    active = 0
    maximum = 0
    rolled_back = []

    async def persist(item):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        if active == 2:
            both_started.set()
        await both_started.wait()
        active -= 1
        if item is candidate.representations[1]:
            raise RuntimeError("storage failed")
        return True

    async def rollback(item):
        rolled_back.append(item.representation_id)

    with pytest.raises(RuntimeError, match="storage failed"):
        await ArchivePostgresModel.prepare_version(
            candidate,
            ArchivePolicy(),
            persist,
            rollback,
            representation_concurrency=2,
        )

    assert maximum == 2
    assert active == 0
    assert rolled_back == [candidate.representations[0].representation_id]


@pytest.mark.asyncio
async def test_database_failure_rolls_back_all_new_representation_objects(
    monkeypatch,
) -> None:
    connection = Connection()
    connection.insert_error = RuntimeError("database write failed")
    install(monkeypatch, connection)
    candidate = write()
    rolled_back = []

    async def rollback(item):
        rolled_back.append(item.representation_id)

    with pytest.raises(RuntimeError, match="database write failed"):
        await ArchivePostgresModel.prepare_version(
            candidate, ArchivePolicy(), AsyncMock(return_value=True), rollback
        )
    assert rolled_back == [
        item.representation_id for item in reversed(candidate.representations)
    ]


@pytest.mark.asyncio
async def test_ambiguous_commit_preserves_all_representation_objects(
    monkeypatch,
) -> None:
    connection = Connection(commit_error=RuntimeError("database commit failed"))
    install(monkeypatch, connection)
    rollback = AsyncMock()

    with pytest.raises(RuntimeError, match="database commit failed"):
        await ArchivePostgresModel.prepare_version(
            write(), ArchivePolicy(), AsyncMock(return_value=True), rollback
        )
    rollback.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancelled_database_write_rolls_back_created_objects(monkeypatch) -> None:
    connection = Connection()
    connection.insert_error = asyncio.CancelledError()
    install(monkeypatch, connection)
    rollback = AsyncMock()
    with pytest.raises(asyncio.CancelledError):
        await ArchivePostgresModel.prepare_version(
            write(), ArchivePolicy(), AsyncMock(return_value=True), rollback
        )
    assert rollback.await_count == 2


@pytest.mark.asyncio
async def test_unchanged_snapshot_ignores_new_identity_and_skips_storage(
    monkeypatch,
) -> None:
    head = uuid4()
    parent = uuid4()
    candidate = write(snapshot_fingerprint=b"same" * 16)
    connection = Connection(
        head_id=head,
        head_parent_id=parent,
        snapshot_fingerprint=candidate.snapshot_fingerprint,
    )
    connection.default_representation_id = candidate.default_representation_id
    connection.representations = candidate.representations
    install(monkeypatch, connection)
    persist = AsyncMock()

    result = await ArchivePostgresModel.prepare_version(
        candidate, ArchivePolicy(duplicate_unchanged=True), persist, AsyncMock()
    )
    assert result.outcome == "unchanged"
    assert result.version_id == head
    assert result.dict()["version"] == {"id": str(head), "parent": str(parent)}
    persist.assert_not_awaited()

    recorded = next(
        arguments[-1]
        for query, arguments in connection.executed
        if "INSERT INTO archive_idempotency" in query
    )
    replay_connection = AsyncMock()
    replay_connection.fetchrow.return_value = {
        "request_fingerprint": candidate.fingerprint,
        "response": orjson.loads(recorded),
        "parent_version_id": parent,
    }
    replay = await ArchivePostgresModel._replay(replay_connection, candidate)
    assert replay is not None and replay.replayed
    assert replay.dict() == result.dict()
    assert "replayed" not in replay.dict()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("policy", "parent", "outcome"),
    [
        (ArchivePolicy(write="create-only"), None, "overwrite_rejected"),
        (ArchivePolicy(require_current_predecessor=True), None, "version_conflict"),
        (ArchivePolicy(), uuid4(), "version_conflict"),
    ],
)
async def test_head_parent_and_write_policy_conflicts(
    monkeypatch, policy, parent, outcome
) -> None:
    head = uuid4()
    candidate = write(parent=parent, snapshot_fingerprint=b"new" * 21 + b"x")
    connection = Connection(head_id=head, snapshot_fingerprint=b"old" * 21 + b"x")
    install(monkeypatch, connection)
    with pytest.raises(ArchiveConflictError) as raised:
        await ArchivePostgresModel.prepare_version(
            candidate, policy, AsyncMock(), AsyncMock()
        )
    assert raised.value.outcome == outcome


@pytest.mark.asyncio
async def test_filesystem_backend_never_overwrites(tmp_path) -> None:
    backend = FilesystemStorageBackend(os.fspath(tmp_path))
    document = UUID("c3be90e0-24ab-5d38-a39d-3149db97ae22")
    version = UUID("6518eb3c-c1eb-5f49-a1bf-67c91308e31e")
    representation = UUID("a5f2a8ae-6f5d-59f8-bcff-51e359176286")
    prefix = backend.representation_key_prefix("invoices", document, version)
    assert prefix == f"invoices/66/6a/05/{document}-{version}-"
    # Both recorded historical keys and new sharded keys are opaque to storage I/O.
    keys = (
        "archive/document/version/representation",
        f"invoices/6a/05/{document}-{version}-{representation}.{uuid4().hex}",
        f"{prefix}{representation}",
        f"{prefix}{representation}.{uuid4().hex}",
    )
    with patch(
        "xarta.services.v1.archive.storage.crc32",
        side_effect=AssertionError("Storage I/O must not recalculate shards"),
    ):
        for key in keys:
            assert await backend.put(key, b"first", "application/pdf") is True
            assert await backend.put(key, b"first", "application/pdf") is False
            with pytest.raises(FileExistsError):
                await backend.put(key, b"second", "application/pdf")
            assert await backend.get(key) == b"first"
            assert (tmp_path / key).read_bytes() == b"first"
        for key in keys:
            await backend.delete(key)
            await backend.delete(key)
            assert not (tmp_path / key).exists()


def test_archive_schema_is_representation_aware_and_immutable() -> None:
    root = Path(__file__).parents[2]
    archive = root / "db/archive"
    deploy = (archive / "deploy/00000.archive.sql").read_text()
    plan = (archive / "sqitch.plan").read_text()

    assert "head_version_id bigint" in deploy
    assert "parent_version_id bigint" in deploy
    assert "CREATE TABLE public.archive_document_representations" in deploy
    assert "default_representation_id uuid NOT NULL" in deploy
    assert "archive_document_versions_default_representation_fk" in deploy
    assert "archive version representation membership is immutable" in deploy
    assert "archive representations are immutable" in deploy
    assert "metadata_revision" not in deploy
    assert " relation " not in deploy
    assert "mutable-version-metadata" not in plan


def test_destination_selects_server_policy_and_pinned_backend(tmp_path) -> None:
    registry = ArchiveStorageRegistry(
        {
            "backends": {
                "cold": {
                    "kind": "filesystem",
                    "root": os.fspath(tmp_path),
                    "revision": "2026-08",
                }
            },
            "archives": {
                "payroll": {
                    "backend": "cold",
                    "policy": {
                        "write": "create-only",
                        "require_current_predecessor": True,
                    },
                }
            },
        },
        os.fspath(tmp_path),
    )
    target = registry.resolve("payroll")
    assert target.policy.require_current_predecessor is True
    assert registry.backend("cold", "2026-08") is target.backend


@pytest.mark.parametrize("history", ["retain-all", "retain-metadata", "latest-only"])
def test_removed_history_policy_is_rejected(tmp_path, history) -> None:
    with pytest.raises(ValueError, match="history policy was removed"):
        ArchiveStorageRegistry(
            {
                "archives": {
                    "default": {"backend": "filesystem", "policy": {"history": history}}
                }
            },
            os.fspath(tmp_path),
        )


@pytest.mark.asyncio
async def test_s3_backend_uses_conditional_put_for_representation_key() -> None:
    backend = S3StorageBackend(
        {"bucket": "archive", "endpoint_url": "http://minio:9000", "prefix": "xarta"}
    )
    calls = []

    class Client:
        async def put_object(self, **kwargs):
            calls.append(kwargs)

    class Manager:
        async def get(self):
            return Client()

    backend._client_manager = Manager()  # type: ignore[assignment]
    document, version, representation = uuid4(), uuid4(), uuid4()
    allocation = uuid4().hex
    with patch(
        "xarta.services.v1.archive.storage.crc32",
        side_effect=AssertionError("S3 must not calculate filesystem shards"),
    ):
        key = (
            backend.representation_key_prefix("payroll", document, version)
            + str(representation)
            + f".{allocation}"
        )
        assert key == f"payroll/{document}/{version}/{representation}.{allocation}"
        await backend.put(key, b"pdf", "application/pdf")
    assert calls[0]["IfNoneMatch"] == "*"
    assert calls[0]["Key"] == f"xarta/{key}"


@pytest.mark.asyncio
async def test_s3_backend_reports_different_bytes_under_existing_key() -> None:
    backend = S3StorageBackend({"bucket": "archive"})

    class Body:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def read(self):
            return b"stored"

    class Client:
        async def put_object(self, **kwargs):
            raise ClientError(
                {
                    "Error": {"Code": "PreconditionFailed"},
                    "ResponseMetadata": {"HTTPStatusCode": 412},
                },
                "PutObject",
            )

        async def get_object(self, **kwargs):
            return {"Body": Body()}

    class Manager:
        async def get(self):
            return Client()

    backend._client_manager = Manager()  # type: ignore[assignment]
    assert await backend.put("key", b"stored", "application/pdf") is False
    with pytest.raises(FileExistsError):
        await backend.put("key", b"different", "application/pdf")


def test_archive_s3_backends_share_sessions_for_the_same_connection() -> None:
    sessions = ArchiveStorageSessions()
    primary = S3StorageBackend(
        {"bucket": "primary", "endpoint_url": "http://minio:9000"}, sessions
    )
    secondary = S3StorageBackend(
        {"bucket": "secondary", "endpoint_url": "http://minio:9000"}, sessions
    )
    assert primary._client_manager is secondary._client_manager
