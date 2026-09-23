from __future__ import annotations

import asyncio
import datetime
import hashlib
import os

from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from xarta.services.v1.archive.db import ArchiveConflictError
from xarta.services.v1.archive.db import ArchivePostgresModel
from xarta.services.v1.archive.db import RepresentationWrite
from xarta.services.v1.archive.db import VersionWrite
from xarta.services.v1.archive.storage import ArchivePolicy
from xarta.services.v1.archive.storage import ArchiveStorageRegistry
from xarta.services.v1.archive.storage import ArchiveStorageSessions
from xarta.services.v1.archive.storage import FilesystemStorageBackend
from xarta.services.v1.archive.storage import S3StorageBackend


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
        snapshot_fingerprint=None,
        lifecycle="active",
        commit_error: Exception | None = None,
    ) -> None:
        self.head_id = head_id
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
    candidate = write(snapshot_fingerprint=b"same" * 16)
    connection = Connection(
        head_id=head, snapshot_fingerprint=candidate.snapshot_fingerprint
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
    persist.assert_not_awaited()


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
    await backend.put(
        "archive/document/version/representation", b"first", "application/pdf"
    )
    await backend.put(
        "archive/document/version/representation", b"first", "application/pdf"
    )
    with pytest.raises(FileExistsError):
        await backend.put(
            "archive/document/version/representation", b"second", "application/pdf"
        )


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
                        "history": "retain-all",
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
    await backend.put(
        "payroll/document/version/representation", b"pdf", "application/pdf"
    )
    assert calls[0]["IfNoneMatch"] == "*"
    assert calls[0]["Key"] == "xarta/payroll/document/version/representation"


def test_archive_s3_backends_share_sessions_for_the_same_connection() -> None:
    sessions = ArchiveStorageSessions()
    primary = S3StorageBackend(
        {"bucket": "primary", "endpoint_url": "http://minio:9000"}, sessions
    )
    secondary = S3StorageBackend(
        {"bucket": "secondary", "endpoint_url": "http://minio:9000"}, sessions
    )
    assert primary._client_manager is secondary._client_manager
