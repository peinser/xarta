"""Load-test archive snapshot insertion against PostgreSQL and archive storage.

Drives the real write path in process on uvloop: manifest validation and hashing
(`store._insert`), the storage backend (local filesystem with fsync by default, or
any backend given by --storage-config, such as S3), and the database reads and
transactions of `ArchivePostgresModel.prepare_version`. It deploys the archive
Sqitch project into a disposable database, runs each scenario with concurrent
writers, verifies the stored state, and prints a Markdown report. The report is
also appended to $GITHUB_STEP_SUMMARY when that variable is set.

Not measured: Sanic's HTTP and multipart parsing, network transfer, and NATS
event publication, which is replaced by a no-op. Shared CI runners make latency
indicative only, so the run fails on incorrect results and, only when
--max-p95-ms is given, on slow requests.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shlex
import subprocess
import sys
import tempfile
import time
import uuid

from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import asyncpg
import orjson
import uvloop

from xarta.services.v1.archive import store
from xarta.services.v1.archive.base import _keep_session
from xarta.services.v1.archive.db import ArchivePostgresModel
from xarta.services.v1.archive.storage import ArchiveStorageRegistry
from xarta.services.v1.archive.storage import ArchiveStorageSessions

PROJECT = Path(__file__).resolve().parents[1] / "db/archive"
CREATED_AT = "2026-10-27T09:00:00+00:00"


class LoadTestError(Exception):
    pass


@dataclass
class Timings:
    """Durations in seconds, collected for the scenario that is running."""

    requests: list[float] = field(default_factory=list)
    uploads: list[float] = field(default_factory=list)
    # Time inside each transaction body: the lock is held for about this long.
    decisions: list[float] = field(default_factory=list)
    commits: list[float] = field(default_factory=list)


current = Timings()


class NullJetStream:
    async def publish(self, *_args, **_kwargs) -> None:
        return None


def instrument(registry: ArchiveStorageRegistry) -> None:
    backend = registry.resolve("default").backend
    put = backend.put

    async def timed_put(key: str, data: bytes, content_type: str) -> bool:
        started = time.perf_counter()
        try:
            return await put(key, data, content_type)
        finally:
            current.uploads.append(time.perf_counter() - started)

    backend.put = timed_put  # type: ignore[method-assign]

    prepare_locked = ArchivePostgresModel._prepare_locked.__func__  # type: ignore[attr-defined]

    async def timed_prepare_locked(cls, connection, write, policy, **options):
        started = time.perf_counter()
        try:
            return await prepare_locked(cls, connection, write, policy, **options)
        finally:
            target = current.commits if options["storage_ready"] else current.decisions
            target.append(time.perf_counter() - started)

    ArchivePostgresModel._prepare_locked = classmethod(timed_prepare_locked)  # type: ignore[method-assign,assignment]
    ArchivePostgresModel._jetstream = staticmethod(NullJetStream)  # type: ignore[method-assign,assignment]


@dataclass(frozen=True)
class Snapshot:
    document_id: uuid.UUID
    version_id: uuid.UUID
    manifest: dict
    idempotency_key: str


def snapshot(
    document_id: uuid.UUID, parent: uuid.UUID | None, revision: int
) -> Snapshot:
    """A two-representation invoice: a PDF for people and UBL XML for software."""
    version_id = uuid.uuid4()
    pdf_id, xml_id = uuid.uuid4(), uuid.uuid4()
    manifest = {
        "id": str(document_id),
        "version": {"id": str(version_id), "parent": str(parent) if parent else None},
        "created_at": CREATED_AT,
        "metadata": {
            "invoice_number": f"INV-2026-{document_id.hex[:8]}",
            "customer_id": "CUST-0042",
            "revision": revision,
        },
        "representations": {
            "default": str(pdf_id),
            "items": [
                {
                    "id": str(pdf_id),
                    "field": "pdf",
                    "name": "invoice.pdf",
                    "content": {"type": "application/pdf"},
                },
                {
                    "id": str(xml_id),
                    "field": "xml",
                    "name": "invoice.xml",
                    "content": {"type": "application/xml"},
                },
            ],
        },
    }
    return Snapshot(document_id, version_id, manifest, f"load-{uuid.uuid4()}")


class Writer:
    def __init__(self, registry: ArchiveStorageRegistry, files: dict[str, bytes]):
        self.registry = registry
        self.files = {name: SimpleNamespace(body=data) for name, data in files.items()}

    async def put(self, item: Snapshot, expected: set[tuple[int, str]]) -> dict:
        request = SimpleNamespace(
            form={"manifest": orjson.dumps(item.manifest).decode()},
            files=self.files,
            headers={"Idempotency-Key": item.idempotency_key},
            app=SimpleNamespace(ctx=SimpleNamespace(archive_storage=self.registry)),
        )
        response = await store._insert(request, "default")  # type: ignore[arg-type]
        body = orjson.loads(response.body)
        outcome = body.get("outcome") or body.get("error", {}).get("code")
        if (response.status, outcome) not in expected:
            raise LoadTestError(
                f"Unexpected archive response {response.status} {body} for {item.document_id}"
            )
        return body


async def run(count: int, writers: int, operation) -> float:
    """Run operation(index) for every index with `writers` concurrent workers."""
    indexes = iter(range(count))

    async def worker() -> None:
        for index in indexes:
            started = time.perf_counter()
            await operation(index)
            current.requests.append(time.perf_counter() - started)

    started = time.perf_counter()
    await asyncio.gather(*(worker() for _ in range(writers)))
    return time.perf_counter() - started


def percentile(values: list[float], percent: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(percent / 100 * (len(ordered) - 1)))]


def milliseconds(value: float | None) -> str:
    return "-" if value is None else f"{value * 1000:.1f}"


@dataclass(frozen=True)
class Result:
    name: str
    count: int
    writers: int
    elapsed: float
    timings: Timings


async def scenarios(arguments, writer: Writer) -> list[Result]:
    results = []

    async def measure(name: str, count: int, writers: int, operation) -> None:
        global current
        current = Timings()
        elapsed = await run(count, writers, operation)
        results.append(Result(name, count, writers, elapsed, current))

    documents = [uuid.uuid4() for _ in range(arguments.documents)]
    created: list[Snapshot] = [snapshot(item, None, 1) for item in documents]
    await measure(
        "create (new document)",
        len(created),
        arguments.writers,
        lambda index: writer.put(created[index], {(201, "created")}),
    )

    versions = [snapshot(item.document_id, item.version_id, 2) for item in created]
    await measure(
        "new version (parent = HEAD)",
        len(versions),
        arguments.writers,
        lambda index: writer.put(versions[index], {(201, "version_created")}),
    )

    # Same content as HEAD with new identities and keys: one transaction, no upload.
    unchanged = [snapshot(item.document_id, item.version_id, 2) for item in versions]
    await measure(
        "unchanged resubmission",
        len(unchanged),
        arguments.writers,
        lambda index: writer.put(unchanged[index], {(200, "unchanged")}),
    )

    await measure(
        "exact retry (same key)",
        len(versions),
        arguments.writers,
        lambda index: writer.put(versions[index], {(200, "version_created")}),
    )

    # Every writer appends to one document: all of them queue on its row lock.
    hot_document = uuid.uuid4()
    contended = [
        snapshot(hot_document, None, revision)
        for revision in range(arguments.contended)
    ]
    await measure(
        "contended (one document)",
        len(contended),
        arguments.writers,
        lambda index: writer.put(
            contended[index], {(201, "created"), (201, "version_created")}
        ),
    )
    return results


async def verify(pool, root: Path | None, arguments, expected_heads: int) -> list[str]:
    """Check that the run stored exactly what it claimed, with no orphan files.

    Stored files are only counted for the default local storage root.
    """
    stored_versions = 2 * arguments.documents + arguments.contended
    async with pool.acquire() as connection:
        versions = await connection.fetchval(
            "SELECT count(*) FROM archive_document_versions"
        )
        representations = await connection.fetchval(
            "SELECT count(*) FROM archive_document_representations"
        )
        replay_records = await connection.fetchval(
            "SELECT count(*) FROM archive_idempotency"
        )
        heads = await connection.fetchval(
            "SELECT count(*) FROM archive_documents WHERE head_version_id IS NOT NULL"
        )
    checks = {
        "versions": (versions, stored_versions),
        "representations": (representations, 2 * stored_versions),
        "replay records": (replay_records, stored_versions + arguments.documents),
        "documents with HEAD": (heads, expected_heads),
    }
    if root is not None:
        files = [path for path in root.rglob("*") if path.is_file()]
        temporary = [path for path in files if path.name.endswith(".tmp")]
        checks["stored files"] = (len(files), 2 * stored_versions)
        checks["leftover temporary files"] = (len(temporary), 0)
    return [
        f"{name}: expected {expected}, found {actual}"
        for name, (actual, expected) in checks.items()
        if actual != expected
    ]


async def delete_stored_objects(pool, registry: ArchiveStorageRegistry) -> None:
    """Remove what this run stored in a configured, possibly shared, backend."""
    async with pool.acquire() as connection:
        rows = await connection.fetch(
            "SELECT backend, backend_revision, storage_key "
            "FROM archive_document_representations"
        )
    for row in rows:
        backend = registry.backend(row["backend"], row["backend_revision"])
        await backend.delete(row["storage_key"])


def report(results: list[Result], arguments) -> str:
    lines = [
        "## Archive insertion load test",
        "",
        (
            f"Pool size {arguments.pool_size}; {arguments.writers} concurrent writers; "
            f"two representations per snapshot ({arguments.pdf_kib} KiB PDF, "
            f"{arguments.xml_kib} KiB XML); "
            + (
                f"storage from {Path(arguments.storage_config).name}"
                if arguments.storage_config
                else "local filesystem storage with fsync"
            )
            + "; PostgreSQL. Transaction columns are the time spent inside each locked "
            "transaction body, which is how long the document lock is held: the "
            "unchanged check, and the commit of a new version. "
            "Upload is per representation."
        ),
        "",
        (
            "| Scenario | Requests | Throughput (req/s) | p50 ms | p95 ms | p99 ms "
            "| max ms | Unchanged txn p50/p95 ms | Commit txn p50/p95 ms | Upload p50/p95 ms |"
        ),
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for result in results:
        timings = result.timings
        lines.append(
            f"| {result.name} | {result.count} | {result.count / result.elapsed:.0f} "
            f"| {milliseconds(percentile(timings.requests, 50))} "
            f"| {milliseconds(percentile(timings.requests, 95))} "
            f"| {milliseconds(percentile(timings.requests, 99))} "
            f"| {milliseconds(max(timings.requests, default=None))} "
            f"| {milliseconds(percentile(timings.decisions, 50))} / "
            f"{milliseconds(percentile(timings.decisions, 95))} "
            f"| {milliseconds(percentile(timings.commits, 50))} / "
            f"{milliseconds(percentile(timings.commits, 95))} "
            f"| {milliseconds(percentile(timings.uploads, 50))} / "
            f"{milliseconds(percentile(timings.uploads, 95))} |"
        )
    return "\n".join(lines) + "\n"


def migrate(dsn: str) -> None:
    command = [
        *shlex.split(os.environ.get("SQITCH", "sqitch")),
        "--chdir",
        str(PROJECT),
        "deploy",
        "--verify",
        f"db:pg:{dsn.split(':', 1)[1]}",
    ]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise LoadTestError(
            f"Sqitch deploy failed:\n{completed.stdout}{completed.stderr}"
        )


async def main(arguments) -> tuple[str, list[str]]:
    """Run every scenario and return the report and any failures."""
    source = urlsplit(arguments.dsn)
    name = f"xarta_archive_load_{uuid.uuid4().hex}"
    admin = await asyncpg.connect(source.geturl())
    await admin.execute(f'CREATE DATABASE "{name}"')
    dsn = source._replace(path=f"/{name}").geturl()
    pool = None
    sessions = ArchiveStorageSessions()
    try:
        await asyncio.to_thread(migrate, dsn)
        # Same pool behaviour as the archive service, apart from its size settings.
        pool = await asyncpg.create_pool(
            dsn, min_size=1, max_size=arguments.pool_size, reset=_keep_session
        )
        ArchivePostgresModel.__pool__ = pool
        with tempfile.TemporaryDirectory(dir=arguments.storage_dir) as directory:
            root = Path(directory)
            configuration = (
                orjson.loads(Path(arguments.storage_config).read_bytes())
                if arguments.storage_config
                else {}
            )
            registry = ArchiveStorageRegistry(configuration, str(root), sessions)
            instrument(registry)
            writer = Writer(
                registry,
                {
                    "pdf": os.urandom(arguments.pdf_kib * 1024),
                    "xml": os.urandom(arguments.xml_kib * 1024),
                },
            )
            try:
                results = await scenarios(arguments, writer)
                failures = await verify(
                    pool,
                    None if arguments.storage_config else root,
                    arguments,
                    arguments.documents + 1,
                )
            finally:
                if arguments.storage_config:
                    await delete_stored_objects(pool, registry)
    finally:
        await sessions.close()
        if pool is not None:
            await pool.close()
        await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        await admin.close()

    if arguments.max_p95_ms is not None:
        for result in results:
            p95 = percentile(result.timings.requests, 95)
            if p95 is not None and p95 * 1000 > arguments.max_p95_ms:
                failures.append(
                    f"{result.name}: p95 {p95 * 1000:.1f} ms exceeds {arguments.max_p95_ms} ms"
                )
    return report(results, arguments), failures


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--dsn",
        default=os.environ.get("ARCHIVE_POSTGRES_TEST_DSN"),
        help="Administrative DSN used to create a disposable database "
        "(default: $ARCHIVE_POSTGRES_TEST_DSN)",
    )
    parser.add_argument("--documents", type=int, default=500)
    parser.add_argument("--contended", type=int, default=200)
    parser.add_argument("--writers", type=int, default=32)
    parser.add_argument("--pool-size", type=int, default=20)
    parser.add_argument("--pdf-kib", type=int, default=256)
    parser.add_argument("--xml-kib", type=int, default=16)
    parser.add_argument(
        "--storage-dir", help="Parent directory for the temporary storage root"
    )
    parser.add_argument(
        "--storage-config",
        help="Archive storage configuration JSON, as for ARCHIVE_STORAGE_CONFIG_PATH; "
        "its 'default' archive is used and the objects written are deleted afterwards",
    )
    parser.add_argument("--max-p95-ms", type=float)
    arguments = parser.parse_args()
    if not arguments.dsn:
        parser.error("--dsn or ARCHIVE_POSTGRES_TEST_DSN is required")
    # uvloop is the event loop Sanic runs the archive service on.
    text, failures = uvloop.run(main(arguments))
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as stream:
            stream.write(text)
    for failure in failures:
        print(f"FAILED: {failure}", file=sys.stderr)
    sys.exit(1 if failures else 0)
