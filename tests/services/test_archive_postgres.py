"""Opt-in PostgreSQL/Sqitch checks; each test owns a disposable database."""

from __future__ import annotations

import asyncio
import dataclasses
import datetime
import hashlib
import os
import shlex

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlsplit
from uuid import UUID
from uuid import uuid4

import asyncpg
import orjson
import pytest
import pytest_asyncio

from xarta.services.v1.archive.db import ArchiveConflictError
from xarta.services.v1.archive.db import ArchivePostgresModel
from xarta.services.v1.archive.db import RepresentationWrite
from xarta.services.v1.archive.db import VersionWrite
from xarta.services.v1.archive.reconciler import ArchiveDeletionReconciler
from xarta.services.v1.archive.reconciler import _command
from xarta.services.v1.archive.search import search_scoped
from xarta.services.v1.archive.storage import ArchivePolicy
from xarta.services.v1.archive.storage import FilesystemStorageBackend

pytestmark = pytest.mark.skipif(
    "ARCHIVE_POSTGRES_TEST_DSN" not in os.environ,
    reason="disposable archive PostgreSQL integration databases are not configured",
)
PROJECT = Path(__file__).resolve().parents[2] / "db/archive"
INDEX = "archive_document_versions_metadata_gin_idx"


@pytest_asyncio.fixture
async def database(monkeypatch):
    source = urlsplit(os.environ["ARCHIVE_POSTGRES_TEST_DSN"])
    name = f"xarta_archive_test_{uuid4().hex}"
    admin = await asyncpg.connect(source.geturl())
    await admin.execute(f'CREATE DATABASE "{name}"')
    dsn = source._replace(path=f"/{name}").geturl()
    pool = None

    async def migrate(*arguments, success=True):
        command = [
            *shlex.split(os.environ.get("SQITCH", "sqitch")),
            "--chdir",
            str(PROJECT),
            *arguments,
        ]
        command += [f"db:pg:{dsn.split(':', 1)[1]}"]
        process = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        try:
            output, _ = await asyncio.wait_for(process.communicate(), timeout=60)
        except BaseException:
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise
        assert (process.returncode == 0) is success, output.decode()
        return output.decode()

    try:
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=3)
        monkeypatch.setattr(ArchivePostgresModel, "pool", classmethod(lambda cls: pool))
        yield pool, migrate
    finally:
        if pool is not None:
            await pool.close()
        await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        await admin.close()


async def add_version(
    pool,
    archive,
    document_id,
    metadata,
    document_type="invoice",
    representation_metadata=None,
):
    version_id, representation_id = uuid4(), uuid4()
    async with pool.acquire() as connection, connection.transaction():
        await connection.execute(
            "INSERT INTO archive_documents (archive, document_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
            archive,
            document_id,
        )
        aggregate = await connection.fetchrow(
            "SELECT _id, head_version_id FROM archive_documents WHERE archive = $1 AND document_id = $2",
            archive,
            document_id,
        )
        internal = await connection.fetchval(
            "INSERT INTO archive_document_versions (aggregate_id, version_id, parent_version_id, created, "
            "document_type, metadata, default_representation_id, snapshot_fingerprint) "
            "VALUES ($1, $2, $3, now(), $4, $5::jsonb, $6, $7) RETURNING _id",
            aggregate["_id"],
            version_id,
            aggregate["head_version_id"],
            document_type,
            orjson.dumps(metadata).decode(),
            representation_id,
            hashlib.sha512(orjson.dumps(metadata)).digest(),
        )
        await connection.execute(
            "INSERT INTO archive_document_representations (version_internal_id, representation_id, "
            "content_type, metadata, backend, backend_revision, storage_key, checksum, size) "
            "VALUES ($1, $2, 'text/plain', $3::jsonb, 'test', 'v1', $4, $5, 7)",
            internal,
            representation_id,
            orjson.dumps(representation_metadata or {}).decode(),
            f"{archive}/{document_id}/{version_id}/{representation_id}.{uuid4().hex}",
            hashlib.sha512(b"invoice").digest(),
        )
        await connection.execute(
            "UPDATE archive_document_versions SET sealed = true WHERE _id = $1",
            internal,
        )
        await connection.execute(
            "UPDATE archive_documents SET head_version_id = $2 WHERE _id = $1",
            aggregate["_id"],
            internal,
        )
    return version_id


async def query(archive="invoices", **payload):
    request = SimpleNamespace(
        content_type="application/json", body=orjson.dumps(payload), args={}
    )
    return orjson.loads((await search_scoped(request, archive)).body)


async def test_authoritative_heads_types_and_keyset_traversal(database):
    pool, migrate = database
    await migrate("deploy", "--verify")
    metadata = {
        "customer_id": "CUST-0042",
        "year": 2026,
        "approved": False,
        "optional": None,
        "address": {"country": "BE"},
        "tags": ["invoice"],
    }
    old = await add_version(
        pool, "invoices", UUID(int=1), {"customer_id": "OLD-CUSTOMER"}
    )
    head = await add_version(pool, "invoices", UUID(int=1), metadata)
    await add_version(pool, "invoices", UUID(int=2), metadata)
    await add_version(
        pool,
        "invoices",
        UUID(int=3),
        {"customer_id": "OTHER"},
        representation_metadata={
            "customer_id": "CUST-0042",
            "representation_only": True,
        },
    )
    await add_version(pool, "invoices", UUID(int=4), metadata)
    await pool.execute(
        "UPDATE archive_documents SET lifecycle = 'deleting' WHERE archive = 'invoices' AND document_id = $1",
        UUID(int=4),
    )
    await add_version(
        pool,
        "invoices",
        UUID(int=5),
        {"year": "2026", "approved": "false"},
        document_type=None,
    )
    await add_version(pool, "payroll", UUID(int=1), metadata)

    filters = {"document_type": "invoice", "metadata": {"customer_id": "CUST-0042"}}
    first = await query(filter=filters, limit=1)
    assert [item["id"] for item in first["items"]] == [str(UUID(int=1))]
    assert first["items"][0]["version"] == {"id": str(head), "parent": str(old)}
    assert "representations" not in first["items"][0]
    assert first["pagination"]["limit"] == 1
    assert first["pagination"]["next"] is not None

    # A HEAD update cannot move the document past the stable document-ID cursor.
    await add_version(pool, "invoices", UUID(int=1), {"customer_id": "NEW-CUSTOMER"})
    second = await query(filter=filters, limit=2, cursor=first["pagination"]["next"])
    assert [item["id"] for item in second["items"]] == [str(UUID(int=2))]
    assert second["pagination"] == {"limit": 2, "next": None}
    assert (
        await ArchivePostgresModel.find_one("invoices", UUID(int=1), head)
    ).metadata == metadata
    assert (await query(filter={"metadata": {"customer_id": "OLD-CUSTOMER"}}))[
        "items"
    ] == []
    assert (await query(filter={"metadata": {"representation_only": True}}))[
        "items"
    ] == []
    for field_filter, expected in (
        ({"year": 2026}, 2),
        ({"year": "2026"}, 5),
        ({"approved": False}, 2),
        ({"approved": "false"}, 5),
        ({"optional": None}, 2),
        ({"address": {"country": "BE"}}, 2),
    ):
        assert [
            item["id"]
            for item in (await query(filter={"metadata": field_filter}))["items"]
        ] == [str(UUID(int=expected))]
    assert (await query(filter={"metadata": {"tags": "invoice"}}))["items"] == []
    assert [
        item["id"] for item in (await query(filter={"document_type": None}))["items"]
    ] == [str(UUID(int=5))]
    assert (await query("empty-archive"))["pagination"] == {"limit": 50, "next": None}

    # Traverse every active HEAD with several batch sizes; no duplicates or gaps.
    for limit in (1, 2, 3, 100):
        cursor = None
        identities = []
        while True:
            result = await query(limit=limit, **({"cursor": cursor} if cursor else {}))
            assert len(result["items"]) <= limit
            identities.extend(item["id"] for item in result["items"])
            cursor = result["pagination"]["next"]
            if cursor is None:
                break
        assert identities == [str(UUID(int=value)) for value in (1, 2, 3, 5)]

    async with pool.acquire() as connection, connection.transaction():
        await connection.execute("SET LOCAL enable_seqscan = off")
        plan = await connection.fetchval(
            "EXPLAIN (FORMAT JSON) SELECT _id FROM archive_document_versions WHERE metadata @> $1::jsonb",
            '{"customer_id":"CUST-0042"}',
        )
        assert (
            INDEX in plan
        )  # Operator/index eligibility, not a production latency benchmark.


async def snapshot(pool):
    return tuple(
        [
            await pool.fetchval(
                f"SELECT jsonb_agg(to_jsonb(row) - 'history_cleanup_policy' ORDER BY _id) FROM public.{table} row"
            )
            for table in (
                "archive_documents",
                "archive_document_versions",
                "archive_document_representations",
            )
        ]
    )


async def test_sqitch_upgrade_revert_and_interrupted_build_recovery(database):
    pool, migrate = database
    await migrate("deploy", "--verify", "--to", "00000.archive")
    await add_version(pool, "invoices", UUID(int=1), {"customer_id": "CUST-0042"})
    await add_version(pool, "invoices", UUID(int=1), {"customer_id": "CUST-0043"})
    original = await snapshot(pool)
    await migrate("deploy", "--verify")
    await migrate("verify")
    assert await snapshot(pool) == original
    await migrate("revert", "-y", "--to", "00000.archive")
    assert await pool.fetchval("SELECT to_regclass($1)", f"public.{INDEX}") is None
    assert await snapshot(pool) == original

    # Produce a real invalid concurrent index: the first scan waits for this writer.
    async with pool.acquire() as writer, pool.acquire() as builder:
        transaction = writer.transaction()
        await transaction.start()
        build = None
        try:
            await writer.execute(
                "LOCK TABLE archive_document_versions IN ROW EXCLUSIVE MODE"
            )
            pid = await builder.fetchval("SELECT pg_backend_pid()")
            build = asyncio.create_task(
                builder.execute(
                    f"CREATE INDEX CONCURRENTLY {INDEX} ON archive_document_versions USING gin (metadata jsonb_path_ops)"
                )
            )
            async with asyncio.timeout(10):
                while (
                    await pool.fetchval("SELECT to_regclass($1)", f"public.{INDEX}")
                    is None
                ):
                    await asyncio.sleep(0.01)
            # Do not drop an index whose original concurrent build is still alive.
            output = await migrate("deploy", success=False)
            assert "still running" in output
            assert await pool.fetchval("SELECT pg_cancel_backend($1)", pid)
            with pytest.raises(asyncpg.QueryCanceledError):
                await build
        finally:
            if build is not None and not build.done():
                build.cancel()
                await asyncio.gather(build, return_exceptions=True)
            await transaction.rollback()
    assert (
        await pool.fetchval(
            "SELECT indisvalid FROM pg_index WHERE indexrelid = $1::regclass",
            f"public.{INDEX}",
        )
        is False
    )
    await migrate("deploy", "--verify")
    assert await snapshot(pool) == original

    # A correct valid index from an unlogged completed build is reused, not rebuilt.
    await migrate("revert", "-y", "--to", "00000.archive")
    await pool.execute(
        f"CREATE INDEX CONCURRENTLY {INDEX} ON archive_document_versions USING gin (metadata jsonb_path_ops)"
    )
    existing_oid = await pool.fetchval("SELECT $1::regclass::oid", f"public.{INDEX}")
    await migrate("deploy", "--verify")
    assert (
        await pool.fetchval("SELECT $1::regclass::oid", f"public.{INDEX}")
        == existing_oid
    )

    # A conflicting definition must fail, not be accepted by IF NOT EXISTS.
    await migrate("revert", "-y", "--to", "00000.archive")
    await pool.execute(
        f"CREATE INDEX {INDEX} ON archive_document_versions (document_type)"
    )
    await migrate("deploy", success=False)
    await pool.execute(f"DROP INDEX {INDEX}")
    await migrate("deploy", "--verify")
    await migrate("verify")
    assert await snapshot(pool) == original


async def test_document_only_upgrade_cancels_pending_history_cleanup(database):
    pool, migrate = database
    await migrate("deploy", "--verify", "--to", "00001.archive-metadata-search")
    document_id = uuid4()
    removed = await add_version(pool, "invoices", document_id, {"revision": 1})
    pending = await add_version(pool, "invoices", document_id, {"revision": 2})
    await add_version(pool, "invoices", document_id, {"revision": 3})
    await pool.execute(
        "UPDATE archive_document_representations SET state = 'metadata-only', storage_key = NULL "
        "WHERE version_internal_id = (SELECT _id FROM archive_document_versions WHERE version_id = $1)",
        removed,
    )
    await pool.execute(
        "UPDATE archive_document_versions SET state = 'metadata-only' WHERE version_id = $1",
        removed,
    )
    await pool.execute(
        "UPDATE archive_document_versions SET history_cleanup_policy = 'retain-metadata' WHERE version_id = $1",
        pending,
    )
    references = await ArchivePostgresModel.find_sources(
        "invoices", document_id, pending
    )
    await migrate("deploy", "--verify")
    await migrate("verify")
    assert await ArchivePostgresModel.pending_deletion_commands(limit=100) == []
    assert (
        await ArchivePostgresModel.find_sources("invoices", document_id, pending)
        == references
    )
    assert (
        await pool.fetchval(
            "SELECT state::text FROM archive_document_versions WHERE version_id = $1",
            removed,
        )
        == "metadata-only"
    )
    await migrate("revert", "-y", "--to", "00001.archive-metadata-search")
    assert (
        await pool.fetchval(
            "SELECT history_cleanup_policy FROM archive_document_versions WHERE version_id = $1",
            pending,
        )
        is None
    )


@pytest.mark.parametrize("lifecycle", ["active", "deleting"])
async def test_empty_non_deleted_document_fails_without_publication_or_state_change(
    database, monkeypatch, lifecycle
):
    pool, migrate = database
    await migrate("deploy", "--verify")
    document_id = uuid4()
    version_id = await add_version(pool, "invoices", document_id, {"revision": 1})
    # Deliberately create an inconsistent state using SQL, bypassing the worker
    # that would finalize the tombstone in this same transaction.
    async with pool.acquire() as connection, connection.transaction():
        await connection.execute(
            "UPDATE archive_documents SET lifecycle = 'deleting' WHERE document_id = $1",
            document_id,
        )
        await connection.execute(
            "UPDATE archive_document_representations SET state = 'metadata-only', storage_key = NULL "
            "WHERE version_internal_id = (SELECT _id FROM archive_document_versions WHERE version_id = $1)",
            version_id,
        )
        await connection.execute(
            "UPDATE archive_document_versions SET state = 'metadata-only' WHERE version_id = $1",
            version_id,
        )
        await connection.execute(
            "UPDATE archive_documents SET lifecycle = $2 WHERE document_id = $1",
            document_id,
            lifecycle,
        )
    before = await snapshot(pool)
    jetstream = AsyncMock()
    monkeypatch.setattr(
        ArchivePostgresModel, "_jetstream", staticmethod(lambda: jetstream)
    )

    with pytest.raises(RuntimeError, match="no available representations"):
        await ArchivePostgresModel.request_deletion(
            "invoices",
            document_id,
            safe=False,
            now=datetime.datetime.now(tz=datetime.UTC),
        )

    # Includes lifecycle, updated_at, HEAD, and all version/representation rows.
    assert await snapshot(pool) == before
    jetstream.publish.assert_not_awaited()


async def test_all_versions_retained_until_logical_document_deletion(
    database, monkeypatch, tmp_path
):
    pool, migrate = database
    await migrate("deploy", "--verify")
    backend = FilesystemStorageBackend(str(tmp_path))
    jetstream = AsyncMock()
    monkeypatch.setattr(
        ArchivePostgresModel, "_jetstream", staticmethod(lambda: jetstream)
    )
    document_id = uuid4()
    parent = None
    writes = []
    content = b"invoice content"

    async def persist(representation):
        return await backend.put(
            representation.storage_key, content, representation.content_type
        )

    async def rollback(representation):
        await backend.delete(representation.storage_key)

    for number in (1, 2):
        version_id, representation_id = uuid4(), uuid4()
        representation = RepresentationWrite(
            representation_id=representation_id,
            name="invoice.txt",
            content_type="text/plain",
            metadata={},
            backend="test",
            backend_revision="v1",
            storage_key=f"{document_id}/{version_id}/{representation_id}",
            checksum=hashlib.sha512(content).digest(),
            size=len(content),
        )
        candidate = VersionWrite(
            archive="invoices",
            document_id=document_id,
            version_id=version_id,
            parent_version_id=parent,
            idempotency_key=f"invoice-version-{number}",
            fingerprint=hashlib.sha512(str(number).encode()).digest(),
            snapshot_fingerprint=hashlib.sha512(str(number).encode()).digest(),
            created=datetime.datetime(2030, 1, number, tzinfo=datetime.UTC),
            expires=None,
            document_type="invoice",
            metadata={"revision": number},
            default_representation_id=representation_id,
            representations=(representation,),
        )
        result = await ArchivePostgresModel.prepare_version(
            candidate, ArchivePolicy(), persist, rollback
        )
        assert result.outcome == ("created" if number == 1 else "version_created")
        writes.append(candidate)
        parent = version_id

    assert all(
        not call.args[0].startswith("archive.commands.")
        for call in jetstream.publish.await_args_list
    )
    assert await ArchivePostgresModel.pending_deletion_commands(limit=100) == []
    for candidate in writes:
        references = await ArchivePostgresModel.find_sources(
            "invoices", document_id, candidate.version_id
        )
        assert len(references) == 1
        assert await backend.get(references[0].storage_key) == content

    jetstream.reset_mock()
    await ArchivePostgresModel.request_deletion(
        "invoices",
        document_id,
        safe=False,
        now=datetime.datetime.now(tz=datetime.UTC),
    )
    commands = [
        _command(call.args[1])
        for call in jetstream.publish.await_args_list
        if call.args[0] == "archive.commands.delete"
    ]
    assert {item.version_id for item in commands} == {
        item.version_id for item in writes
    }
    reconciler = ArchiveDeletionReconciler(
        SimpleNamespace(backend=lambda *_args: backend),
        jetstream,
        300,
    )
    for item in commands:
        await reconciler.process(item)
        with pytest.raises(FileNotFoundError):
            await backend.get(item.storage_key)
    assert (
        await pool.fetchval(
            "SELECT lifecycle::text FROM archive_documents WHERE document_id = $1",
            document_id,
        )
        == "deleted"
    )
    assert await pool.fetchval("SELECT count(*) FROM archive_document_versions") == 0
    assert (
        await pool.fetchval("SELECT count(*) FROM archive_document_representations")
        == 0
    )
    assert await pool.fetchval("SELECT count(*) FROM archive_idempotency") == 0
    assert await ArchivePostgresModel.pending_deletion_commands(limit=100) == []


def invoice_write(
    document_id, version_id, representation_id, *, number, key, parent=None
) -> VersionWrite:
    """One attempt of an invoice snapshot request; storage keys are attempt-private."""
    content = f"invoice revision {number}".encode()
    return VersionWrite(
        archive="invoices",
        document_id=document_id,
        version_id=version_id,
        parent_version_id=parent,
        idempotency_key=key,
        fingerprint=hashlib.sha512(f"request {number}".encode()).digest(),
        snapshot_fingerprint=hashlib.sha512(content).digest(),
        created=datetime.datetime(2030, 1, number, tzinfo=datetime.UTC),
        expires=None,
        document_type="invoice",
        metadata={"revision": number},
        default_representation_id=representation_id,
        representations=(
            RepresentationWrite(
                representation_id=representation_id,
                name="invoice.txt",
                content_type="text/plain",
                metadata={},
                backend="test",
                backend_revision="v1",
                storage_key=f"{document_id}/{version_id}/{representation_id}.{uuid4().hex}",
                checksum=hashlib.sha512(content).digest(),
                size=len(content),
            ),
        ),
    )


@pytest_asyncio.fixture
async def archive(database, monkeypatch, tmp_path):
    """A migrated archive database with filesystem storage and no NATS."""
    pool, migrate = database
    await migrate("deploy", "--verify")
    monkeypatch.setattr(
        ArchivePostgresModel, "_jetstream", staticmethod(lambda: AsyncMock())
    )
    backend = FilesystemStorageBackend(str(tmp_path))

    async def persist(representation):
        return await backend.put(
            representation.storage_key, b"content", representation.content_type
        )

    async def rollback(representation):
        await backend.delete(representation.storage_key)

    return pool, backend, persist, rollback


async def test_concurrent_attempt_of_same_request_returns_committed_result(archive):
    pool, backend, persist, rollback = archive
    document_id, version_id, representation_id = uuid4(), uuid4(), uuid4()
    first = invoice_write(
        document_id, version_id, representation_id, number=1, key="invoice-0042"
    )
    second = invoice_write(
        document_id, version_id, representation_id, number=1, key="invoice-0042"
    )
    uploaded, resume = asyncio.Event(), asyncio.Event()

    async def persist_then_wait(representation):
        created = await persist(representation)
        uploaded.set()
        await resume.wait()
        return created

    # The first attempt uploads and then stalls before its commit transaction, while
    # the second attempt of the same request runs to completion.
    stalled = asyncio.create_task(
        ArchivePostgresModel.prepare_version(
            first, ArchivePolicy(), persist_then_wait, rollback
        )
    )
    await asyncio.wait_for(uploaded.wait(), timeout=5)
    committed = await ArchivePostgresModel.prepare_version(
        second, ArchivePolicy(), persist, rollback
    )
    resume.set()
    late = await asyncio.wait_for(stalled, timeout=5)

    assert committed.outcome == "created" and not committed.replayed
    # Under the lock, the late attempt finds the committed retry record instead of
    # reporting a conflict with the HEAD its own request created.
    assert late.replayed
    assert late.dict() == committed.dict()
    assert await pool.fetchval("SELECT count(*) FROM archive_document_versions") == 1
    assert await backend.get(second.representations[0].storage_key) == b"content"
    with pytest.raises(FileNotFoundError):
        await backend.get(first.representations[0].storage_key)


@pytest.mark.parametrize("existing_document", [False, True])
async def test_upload_holds_no_database_connection(archive, existing_document):
    pool, _backend, persist, rollback = archive
    document_id, parent = uuid4(), None
    if existing_document:
        parent = uuid4()
        await ArchivePostgresModel.prepare_version(
            invoice_write(document_id, parent, uuid4(), number=1, key="invoice-1"),
            ArchivePolicy(),
            persist,
            rollback,
        )
    candidate = invoice_write(
        document_id, uuid4(), uuid4(), number=2, key="invoice-2", parent=parent
    )

    async def persist_with_every_connection(representation):
        # The pool holds 3 connections; this only succeeds if the write holds none.
        async with (
            asyncio.timeout(5),
            pool.acquire(),
            pool.acquire(),
            pool.acquire(),
        ):
            return await persist(representation)

    result = await ArchivePostgresModel.prepare_version(
        candidate, ArchivePolicy(), persist_with_every_connection, rollback
    )
    assert result.outcome == ("version_created" if existing_document else "created")


async def test_idempotency_key_takes_precedence_over_version_identity(archive):
    _pool, _backend, persist, rollback = archive
    document_id, first_version, second_version = uuid4(), uuid4(), uuid4()
    await ArchivePostgresModel.prepare_version(
        invoice_write(document_id, first_version, uuid4(), number=1, key="invoice-1"),
        ArchivePolicy(),
        persist,
        rollback,
    )
    second = invoice_write(
        document_id,
        second_version,
        uuid4(),
        number=2,
        key="invoice-2",
        parent=first_version,
    )
    await ArchivePostgresModel.prepare_version(
        second, ArchivePolicy(), persist, rollback
    )

    # The same request under a new key is recognised by its version identity.
    under_new_key = dataclasses.replace(second, idempotency_key="invoice-2-retry")
    replay = await ArchivePostgresModel.prepare_version(
        under_new_key, ArchivePolicy(), AsyncMock(), rollback
    )
    assert replay.replayed and replay.version_id == second_version

    # A key that already belongs to another request wins over a version identity
    # that would match: reusing "invoice-1" for this request is a conflict.
    reused_key = dataclasses.replace(second, idempotency_key="invoice-1")
    with pytest.raises(ArchiveConflictError, match="changed semantics"):
        await ArchivePostgresModel.prepare_version(
            reused_key, ArchivePolicy(), AsyncMock(), rollback
        )
