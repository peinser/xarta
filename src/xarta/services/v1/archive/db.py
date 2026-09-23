from __future__ import annotations

import asyncio

from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Any
from uuid import NAMESPACE_URL
from uuid import UUID
from uuid import uuid5

import orjson

from xarta.concurrency import bounded_map
from xarta.db.postgresql import SanicPostgresModel
from xarta.exceptions.protocol import ArchiveDocumentNotFound
from xarta.protocol.document import ArchiveDocumentVersion
from xarta.protocol.document import ArchiveRepresentation
from xarta.protocol.document.type import DocumentTypeIdentifier

from .events import publish_archive_event

if TYPE_CHECKING:
    import datetime

    from collections.abc import Awaitable
    from collections.abc import Callable

    from asyncpg import Connection  # type: ignore[import-untyped]

    from .storage import ArchivePolicy


class ArchiveConflictError(Exception):
    def __init__(self, outcome: str, message: str) -> None:
        super().__init__(message)
        self.outcome = outcome


@dataclass(frozen=True)
class RepresentationWrite:
    representation_id: UUID
    name: str | None
    content_type: str
    metadata: dict[str, Any]
    backend: str
    backend_revision: str
    storage_key: str
    checksum: bytes
    size: int

    def public_dict(self) -> dict[str, Any]:
        return {
            "representation_id": str(self.representation_id),
            "name": self.name,
            "content_type": self.content_type,
            "metadata": self.metadata,
            "checksum": {"sha512": self.checksum.hex()},
            "size": self.size,
            "state": "available",
        }


@dataclass(frozen=True)
class VersionWrite:
    archive: str
    document_id: UUID
    version_id: UUID
    parent_version_id: UUID | None
    idempotency_key: str
    fingerprint: bytes
    snapshot_fingerprint: bytes
    created: datetime.datetime
    expires: datetime.datetime | None
    document_type: str | None
    metadata: dict[str, Any]
    default_representation_id: UUID
    representations: tuple[RepresentationWrite, ...]


@dataclass(frozen=True)
class VersionResult:
    archive: str
    document_id: UUID
    version_id: UUID
    outcome: str
    default_representation_id: UUID
    representations: tuple[dict[str, Any], ...]
    replayed: bool = False

    def dict(self) -> dict[str, Any]:
        return {
            "archive": self.archive,
            "document_id": str(self.document_id),
            "version_id": str(self.version_id),
            "outcome": self.outcome,
            "default_representation_id": str(self.default_representation_id),
            "representations": list(self.representations),
            "replayed": self.replayed,
        }


@dataclass(frozen=True)
class StorageReference:
    version_id: UUID
    representation_id: UUID
    name: str | None
    backend: str
    backend_revision: str
    storage_key: str
    content_type: str
    checksum: bytes
    size: int
    default: bool


@dataclass(frozen=True)
class VersionPage:
    head_version_id: UUID
    items: tuple[ArchiveDocumentVersion, ...]
    has_more: bool


@dataclass(frozen=True)
class DeletionRequest:
    accepted: bool


@dataclass(frozen=True)
class DeletionCommand:
    aggregate_id: int
    version_internal_id: int
    representation_internal_id: int
    archive: str
    document_id: UUID
    version_id: UUID
    representation_id: UUID
    head_version_id: UUID
    head_version_internal_id: int
    backend: str
    backend_revision: str
    storage_key: str
    kind: str
    history_policy: str | None = None

    @property
    def message_id(self) -> UUID:
        identity = (
            f"{self.kind}:{self.aggregate_id}:{self.version_internal_id}:"
            f"{self.representation_internal_id}:{self.representation_id}:"
            f"{self.backend}:{self.backend_revision}:{self.storage_key}"
        )
        return uuid5(NAMESPACE_URL, f"urn:xarta:archive:deletion:{identity}")

    def dict(self) -> dict[str, str | None]:
        return {
            "aggregate_id": str(self.aggregate_id),
            "version_internal_id": str(self.version_internal_id),
            "representation_internal_id": str(self.representation_internal_id),
            "archive": self.archive,
            "document_id": str(self.document_id),
            "version_id": str(self.version_id),
            "representation_id": str(self.representation_id),
            "head_version_id": str(self.head_version_id),
            "head_version_internal_id": str(self.head_version_internal_id),
            "backend": self.backend,
            "backend_revision": self.backend_revision,
            "storage_key": self.storage_key,
            "kind": self.kind,
            "history_policy": self.history_policy,
        }


@dataclass(frozen=True)
class DeletionCompletion:
    representation_transitioned: bool = False
    version_transitioned: bool = False
    finalized: bool = False


class ArchivePostgresModel(SanicPostgresModel):
    ENV_PREFIX = "ARCHIVE"

    @staticmethod
    def _json(value: Any) -> Any:
        return orjson.loads(value) if isinstance(value, str) else value

    @classmethod
    def _version(cls, row) -> ArchiveDocumentVersion:
        representations = cls._json(row["representations"]) or []
        return ArchiveDocumentVersion(
            identifier=row["document_id"],
            version_id=row["version_id"],
            archive=row["archive"],
            parent_version_id=row["parent_version_id"],
            document_type=(
                DocumentTypeIdentifier(row["document_type"])
                if row["document_type"]
                else None
            ),
            metadata=cls._json(row["metadata"]) or {},
            created=row["created"],
            expires=row["expires"],
            default_representation_id=row["default_representation_id"],
            representations=tuple(
                ArchiveRepresentation(
                    representation_id=UUID(str(item["representation_id"])),
                    name=item["name"],
                    content_type=item["content_type"],
                    metadata=cls._json(item["metadata"]) or {},
                    checksum=(
                        item["checksum"]
                        if isinstance(item["checksum"], str)
                        else bytes(item["checksum"]).hex()
                    ),
                    size=item["size"],
                    state=item["state"],
                )
                for item in representations
            ),
            state=row["state"],
        )

    @staticmethod
    def _jetstream():
        from .nats import ArchiveNATSModel

        return ArchiveNATSModel.jetstream()

    @classmethod
    async def _publish_commands(cls, commands: list[DeletionCommand]) -> None:
        if not commands:
            return
        from .nats import publish_deletion_command

        jetstream = cls._jetstream()
        for command in commands:
            await publish_deletion_command(jetstream, command)

    @classmethod
    async def _replay(
        cls, connection: Connection, write: VersionWrite
    ) -> VersionResult | None:
        row = await connection.fetchrow(
            "SELECT replay.request_fingerprint, replay.response "
            "FROM archive_idempotency replay JOIN archive_documents aggregate "
            "ON aggregate._id = replay.aggregate_id "
            "WHERE replay.archive = $1 AND replay.idempotency_key = $2 "
            "AND aggregate.lifecycle = 'active'",
            write.archive,
            write.idempotency_key,
        )
        if row is None:
            row = await connection.fetchrow(
                "SELECT replay.request_fingerprint, replay.response "
                "FROM archive_idempotency replay JOIN archive_documents aggregate "
                "ON aggregate._id = replay.aggregate_id "
                "WHERE aggregate.archive = $1 AND aggregate.document_id = $2 "
                "AND replay.requested_version_id = $3 AND aggregate.lifecycle = 'active'",
                write.archive,
                write.document_id,
                write.version_id,
            )
        if row is None:
            return None
        if bytes(row["request_fingerprint"]) != write.fingerprint:
            raise ArchiveConflictError(
                "version_conflict",
                "Idempotency key or version identity was reused with changed semantics",
            )
        payload = cls._json(row["response"])
        return VersionResult(
            archive=write.archive,
            document_id=write.document_id,
            version_id=UUID(payload["version_id"]),
            outcome=payload["outcome"],
            default_representation_id=UUID(payload["default_representation_id"]),
            representations=tuple(payload["representations"]),
            replayed=True,
        )

    @staticmethod
    async def _persist_representations(
        representations: tuple[RepresentationWrite, ...],
        persist: Callable[[RepresentationWrite], Awaitable[bool]],
        created: list[RepresentationWrite],
        concurrency: int,
    ) -> None:
        async def store(representation: RepresentationWrite) -> None:
            if await persist(representation):
                created.append(representation)

        try:
            await bounded_map(representations, concurrency, store)
        finally:
            created_ids = {item.representation_id for item in created}
            created[:] = [
                item
                for item in representations
                if item.representation_id in created_ids
            ]

    @staticmethod
    async def _rollback_representations(
        representations: list[RepresentationWrite],
        rollback: Callable[[RepresentationWrite], Awaitable[None]],
    ) -> None:
        for representation in reversed(representations):
            await rollback(representation)

    @classmethod
    async def prepare_version(
        cls,
        write: VersionWrite,
        policy: ArchivePolicy,
        persist: Callable[[RepresentationWrite], Awaitable[bool]],
        rollback: Callable[[RepresentationWrite], Awaitable[None]],
        representation_concurrency: int = 1,
    ) -> VersionResult:
        created_storage: list[RepresentationWrite] = []
        transaction_ready = False

        async def tracked_persist() -> None:
            await cls._persist_representations(
                write.representations,
                persist,
                created_storage,
                representation_concurrency,
            )

        try:
            async with cls.pool().acquire() as connection, connection.transaction():
                result, commands, event = await cls._prepare_locked(
                    connection, write, policy, tracked_persist
                )
                transaction_ready = True
        except asyncio.CancelledError:
            if created_storage and not transaction_ready:
                await asyncio.shield(
                    cls._rollback_representations(created_storage, rollback)
                )
            raise
        except Exception:
            # Once the transaction body completed, a commit error is ambiguous:
            # PostgreSQL may reference every created object, so deleting any is unsafe.
            if created_storage and not transaction_ready:
                await cls._rollback_representations(created_storage, rollback)
            raise

        await cls._publish_commands(commands)
        if event is not None:
            await publish_archive_event(cls._jetstream(), **event)
        return result

    @classmethod
    async def _history_commands(
        cls, connection: Connection, aggregate_id: int
    ) -> list[DeletionCommand]:
        rows = await connection.fetch(
            "SELECT aggregate._id AS aggregate_id, version._id AS version_internal_id, "
            "representation._id AS representation_internal_id, aggregate.archive, "
            "aggregate.document_id, version.version_id, representation.representation_id, "
            "head.version_id AS head_version_id, head._id AS head_version_internal_id, "
            "representation.backend, representation.backend_revision, representation.storage_key, "
            "version.history_cleanup_policy::text AS history_policy "
            "FROM archive_documents aggregate JOIN archive_document_versions version "
            "ON version.aggregate_id = aggregate._id "
            "JOIN archive_document_representations representation "
            "ON representation.version_internal_id = version._id "
            "JOIN archive_document_versions head ON head._id = aggregate.head_version_id "
            "WHERE aggregate._id = $1 AND aggregate.lifecycle = 'active' "
            "AND version.history_cleanup_policy IS NOT NULL "
            "AND representation.state = 'available' "
            "ORDER BY version._id, representation._id",
            aggregate_id,
        )
        return [DeletionCommand(**dict(row), kind="history") for row in rows]

    @staticmethod
    def _write_event(
        write: VersionWrite,
        result: VersionResult,
        parent: UUID | None,
        aggregate_id: int,
        version_internal_id: int,
    ) -> dict[str, Any] | None:
        if result.outcome not in {"created", "version_created"}:
            return None
        return {
            "event_type": (
                "xarta.archive.document.version-created"
                if result.outcome == "version_created"
                else "xarta.archive.document.created"
            ),
            "archive": write.archive,
            "document_id": write.document_id,
            "aggregate_id": aggregate_id,
            "version_internal_id": version_internal_id,
            "version_id": result.version_id,
            "parent_version_id": parent,
            "outcome": result.outcome,
            "document_type": write.document_type,
            "metadata": write.metadata,
            "default_representation_id": result.default_representation_id,
            "representations": list(result.representations),
            "occurred_at": write.created,
        }

    @classmethod
    async def _replay_event(
        cls, connection: Connection, write: VersionWrite, result: VersionResult
    ) -> dict[str, Any] | None:
        if result.outcome not in {"created", "version_created"}:
            return None
        row = await connection.fetchrow(
            "SELECT aggregate._id AS aggregate_id, version._id AS version_internal_id, "
            "parent.version_id AS parent_version_id, version.metadata, version.document_type, "
            "version.created FROM archive_documents aggregate "
            "JOIN archive_document_versions version ON version.aggregate_id = aggregate._id "
            "LEFT JOIN archive_document_versions parent ON parent._id = version.parent_version_id "
            "WHERE aggregate.archive = $1 AND aggregate.document_id = $2 "
            "AND version.version_id = $3",
            write.archive,
            write.document_id,
            result.version_id,
        )
        if row is None:
            return None
        event = cls._write_event(
            write,
            result,
            row["parent_version_id"],
            row["aggregate_id"],
            row["version_internal_id"],
        )
        assert event is not None
        event.update(
            document_type=row["document_type"],
            metadata=cls._json(row["metadata"]) or {},
            occurred_at=row["created"],
        )
        return event

    @classmethod
    async def _prepare_locked(
        cls,
        connection: Connection,
        write: VersionWrite,
        policy: ArchivePolicy,
        persist: Callable[[], Awaitable[None]],
    ) -> tuple[VersionResult, list[DeletionCommand], dict[str, Any] | None]:
        replay = await cls._replay(connection, write)
        if replay:
            aggregate = await connection.fetchrow(
                "SELECT _id FROM archive_documents WHERE archive = $1 AND document_id = $2",
                write.archive,
                write.document_id,
            )
            commands = (
                await cls._history_commands(connection, aggregate["_id"])
                if aggregate
                else []
            )
            return replay, commands, await cls._replay_event(connection, write, replay)

        await connection.execute(
            "INSERT INTO archive_documents (archive, document_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
            write.archive,
            write.document_id,
        )
        aggregate = await connection.fetchrow(
            "SELECT aggregate._id, aggregate.head_version_id, aggregate.lifecycle, "
            "head.version_id AS head_public_version_id, head.snapshot_fingerprint "
            "FROM archive_documents aggregate LEFT JOIN archive_document_versions head "
            "ON head._id = aggregate.head_version_id "
            "WHERE aggregate.archive = $1 AND aggregate.document_id = $2 FOR UPDATE OF aggregate",
            write.archive,
            write.document_id,
        )
        replay = await cls._replay(connection, write)
        if replay:
            commands = await cls._history_commands(connection, aggregate["_id"])
            return replay, commands, await cls._replay_event(connection, write, replay)
        if aggregate["lifecycle"] != "active":
            outcome = (
                "document_deleted"
                if aggregate["lifecycle"] == "deleted"
                else "deletion_in_progress"
            )
            raise ArchiveConflictError(
                outcome,
                (
                    "Archive document identity is deleted"
                    if outcome == "document_deleted"
                    else "Archive document deletion is in progress"
                ),
            )

        head_internal_id = aggregate["head_version_id"]
        head_public_id = aggregate["head_public_version_id"]
        if (
            head_public_id is not None
            and policy.require_current_predecessor
            and write.parent_version_id is None
        ) or (
            write.parent_version_id is not None
            and write.parent_version_id != head_public_id
        ):
            raise ArchiveConflictError(
                "version_conflict", "parent version does not match HEAD"
            )

        if (
            head_public_id is not None
            and bytes(aggregate["snapshot_fingerprint"]) == write.snapshot_fingerprint
            and policy.duplicate_unchanged
        ):
            details = await cls._version_result(
                connection, aggregate["_id"], head_public_id
            )
            result = VersionResult(
                write.archive,
                write.document_id,
                head_public_id,
                "unchanged",
                details.default_representation_id,
                details.representations,
            )
            await cls._record_replay(connection, aggregate["_id"], write, result)
            return result, [], None

        if head_public_id is not None and policy.write == "create-only":
            raise ArchiveConflictError(
                "overwrite_rejected", "Archive policy only permits document creation"
            )

        await persist()
        version_internal_id = await connection.fetchval(
            "INSERT INTO archive_document_versions (aggregate_id, version_id, parent_version_id, "
            "created, expires, document_type, metadata, default_representation_id, "
            "snapshot_fingerprint) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9) RETURNING _id",
            aggregate["_id"],
            write.version_id,
            head_internal_id,
            write.created,
            write.expires,
            write.document_type,
            orjson.dumps(write.metadata).decode(),
            write.default_representation_id,
            write.snapshot_fingerprint,
        )
        await connection.executemany(
            "INSERT INTO archive_document_representations (version_internal_id, "
            "representation_id, name, content_type, metadata, backend, backend_revision, "
            "storage_key, checksum, size) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)",
            (
                (
                    version_internal_id,
                    representation.representation_id,
                    representation.name,
                    representation.content_type,
                    orjson.dumps(representation.metadata).decode(),
                    representation.backend,
                    representation.backend_revision,
                    representation.storage_key,
                    representation.checksum,
                    representation.size,
                )
                for representation in write.representations
            ),
        )
        await connection.execute(
            "UPDATE archive_document_versions SET sealed = true WHERE _id = $1",
            version_internal_id,
        )
        await connection.execute(
            "UPDATE archive_documents SET head_version_id = $2, updated_at = now() WHERE _id = $1",
            aggregate["_id"],
            version_internal_id,
        )
        commands = []
        if policy.history != "retain-all":
            await connection.execute(
                "UPDATE archive_document_versions SET history_cleanup_policy = $3 "
                "WHERE aggregate_id = $1 AND _id <> $2 AND state = 'available' "
                "AND history_cleanup_policy IS NULL",
                aggregate["_id"],
                version_internal_id,
                policy.history,
            )
            commands = await cls._history_commands(connection, aggregate["_id"])
        result = VersionResult(
            write.archive,
            write.document_id,
            write.version_id,
            "version_created" if head_public_id else "created",
            write.default_representation_id,
            tuple(item.public_dict() for item in write.representations),
        )
        await cls._record_replay(connection, aggregate["_id"], write, result)
        return (
            result,
            commands,
            cls._write_event(
                write,
                result,
                head_public_id,
                aggregate["_id"],
                version_internal_id,
            ),
        )

    @classmethod
    async def _version_result(
        cls, connection: Connection, aggregate_id: int, version_id: UUID
    ) -> VersionResult:
        row = await connection.fetchrow(
            "SELECT version.default_representation_id, "
            "jsonb_agg(jsonb_build_object('representation_id', representation.representation_id, "
            "'name', representation.name, 'content_type', representation.content_type, "
            "'metadata', representation.metadata, 'checksum', jsonb_build_object('sha512', "
            "encode(representation.checksum, 'hex')), 'size', representation.size, "
            "'state', representation.state) ORDER BY representation._id) AS representations "
            "FROM archive_document_versions version JOIN archive_document_representations representation "
            "ON representation.version_internal_id = version._id "
            "WHERE version.aggregate_id = $1 AND version.version_id = $2 "
            "GROUP BY version._id",
            aggregate_id,
            version_id,
        )
        return VersionResult(
            archive="",
            document_id=UUID(int=0),
            version_id=version_id,
            outcome="unchanged",
            default_representation_id=row["default_representation_id"],
            representations=tuple(cls._json(row["representations"])),
        )

    @staticmethod
    async def _record_replay(
        connection: Connection,
        aggregate_id: int,
        write: VersionWrite,
        result: VersionResult,
    ) -> None:
        await connection.execute(
            "INSERT INTO archive_idempotency (archive, aggregate_id, idempotency_key, "
            "requested_version_id, request_fingerprint, response) VALUES ($1, $2, $3, $4, $5, $6)",
            write.archive,
            aggregate_id,
            write.idempotency_key,
            write.version_id,
            write.fingerprint,
            orjson.dumps(result.dict()).decode(),
        )

    @classmethod
    async def find_sources(
        cls, archive: str, document_id: UUID, version_id: UUID | None = None
    ) -> tuple[StorageReference, ...]:
        async with cls.pool().acquire() as connection:
            rows = await connection.fetch(
                "SELECT version.version_id, representation.representation_id, representation.name, "
                "representation.backend, representation.backend_revision, representation.storage_key, "
                "representation.content_type, representation.checksum, representation.size, "
                "representation.representation_id = version.default_representation_id AS default "
                "FROM archive_documents aggregate JOIN archive_document_versions version "
                "ON version.aggregate_id = aggregate._id AND version._id = CASE "
                "WHEN $3::uuid IS NULL THEN aggregate.head_version_id ELSE (SELECT selected._id "
                "FROM archive_document_versions selected WHERE selected.aggregate_id = aggregate._id "
                "AND selected.version_id = $3) END JOIN archive_document_representations representation "
                "ON representation.version_internal_id = version._id "
                "WHERE aggregate.archive = $1 AND aggregate.document_id = $2 "
                "AND aggregate.lifecycle = 'active' AND representation.state = 'available'",
                archive,
                document_id,
                version_id,
            )
        if not rows:
            raise ArchiveDocumentNotFound(str(document_id))
        return tuple(StorageReference(**dict(row)) for row in rows)

    @classmethod
    async def find_source(
        cls,
        archive: str,
        document_id: UUID,
        version_id: UUID,
        representation_id: UUID,
    ) -> StorageReference:
        references = await cls.find_sources(archive, document_id, version_id)
        for reference in references:
            if reference.representation_id == representation_id:
                return reference
        raise ArchiveDocumentNotFound(str(representation_id))

    @classmethod
    async def find_one(
        cls, archive: str, document_id: UUID, version_id: UUID | None = None
    ) -> ArchiveDocumentVersion:
        async with cls.pool().acquire() as connection:
            row = await connection.fetchrow(
                "SELECT version.version_id, aggregate.archive, aggregate.document_id, "
                "parent.version_id AS parent_version_id, version.metadata, version.document_type, "
                "version.created, version.expires, version.default_representation_id, version.state, "
                "jsonb_agg(jsonb_build_object('representation_id', representation.representation_id, "
                "'name', representation.name, 'content_type', representation.content_type, "
                "'metadata', representation.metadata, 'checksum', encode(representation.checksum, 'hex'), "
                "'size', representation.size, 'state', representation.state) "
                "ORDER BY representation._id) AS representations "
                "FROM archive_documents aggregate JOIN archive_document_versions version "
                "ON version.aggregate_id = aggregate._id AND version._id = CASE "
                "WHEN $3::uuid IS NULL THEN aggregate.head_version_id ELSE (SELECT selected._id "
                "FROM archive_document_versions selected WHERE selected.aggregate_id = aggregate._id "
                "AND selected.version_id = $3) END LEFT JOIN archive_document_versions parent "
                "ON parent._id = version.parent_version_id JOIN archive_document_representations representation "
                "ON representation.version_internal_id = version._id "
                "WHERE aggregate.archive = $1 AND aggregate.document_id = $2 "
                "AND aggregate.lifecycle = 'active' GROUP BY version._id, aggregate.archive, "
                "aggregate.document_id, parent.version_id",
                archive,
                document_id,
                version_id,
            )
        if not row:
            raise ArchiveDocumentNotFound(str(document_id))
        return cls._version(row)

    @classmethod
    async def find_versions(
        cls,
        archive: str,
        document_id: UUID,
        *,
        limit: int,
        before: tuple[datetime.datetime, UUID] | None = None,
    ) -> VersionPage:
        before_created, before_version_id = before or (None, None)
        async with cls.pool().acquire() as connection:
            rows = await connection.fetch(
                "WITH aggregate AS (SELECT _id, head_version_id FROM archive_documents "
                "WHERE archive = $1 AND document_id = $2 AND lifecycle = 'active') "
                "SELECT head.version_id AS head_version_id, version.version_id, $1 AS archive, "
                "$2 AS document_id, parent.version_id AS parent_version_id, version.metadata, "
                "version.document_type, version.created, version.expires, "
                "version.default_representation_id, version.state, "
                "jsonb_agg(jsonb_build_object('representation_id', representation.representation_id, "
                "'name', representation.name, 'content_type', representation.content_type, "
                "'metadata', representation.metadata, 'checksum', encode(representation.checksum, 'hex'), "
                "'size', representation.size, 'state', representation.state) "
                "ORDER BY representation._id) FILTER (WHERE representation._id IS NOT NULL) AS representations "
                "FROM aggregate JOIN archive_document_versions head ON head._id = aggregate.head_version_id "
                "LEFT JOIN archive_document_versions version ON version.aggregate_id = aggregate._id "
                "AND ($3::timestamptz IS NULL OR (version.created, version.version_id) "
                "< ($3::timestamptz, $4::uuid)) LEFT JOIN archive_document_versions parent "
                "ON parent._id = version.parent_version_id LEFT JOIN archive_document_representations representation "
                "ON representation.version_internal_id = version._id GROUP BY head.version_id, "
                "version._id, parent.version_id ORDER BY version.created DESC, version.version_id DESC LIMIT $5",
                archive,
                document_id,
                before_created,
                before_version_id,
                limit + 1,
            )
        if not rows:
            raise ArchiveDocumentNotFound(str(document_id))
        versions = tuple(
            cls._version(row) for row in rows if row["version_id"] is not None
        )
        return VersionPage(
            head_version_id=rows[0]["head_version_id"],
            items=versions[:limit],
            has_more=len(versions) > limit,
        )

    @classmethod
    async def exists(cls, identifiers: set[UUID], archive: str = "default") -> bool:
        async with cls.pool().acquire() as connection:
            count = await connection.fetchval(
                "SELECT count(*) FROM archive_documents WHERE archive = $1 "
                "AND document_id = ANY($2) AND head_version_id IS NOT NULL "
                "AND lifecycle = 'active'",
                archive,
                list(identifiers),
            )
        return bool(count == len(identifiers))

    @classmethod
    async def request_deletion(
        cls,
        archive: str,
        document_id: UUID,
        *,
        safe: bool,
        now: datetime.datetime,
    ) -> DeletionRequest:
        from xarta.exceptions.http import ForbiddenError
        from xarta.exceptions.protocol import UnsafeArchiveDeleteError

        commands: list[DeletionCommand] = []
        finalized = False
        async with cls.pool().acquire() as connection, connection.transaction():
            aggregate = await connection.fetchrow(
                "SELECT aggregate._id, aggregate.lifecycle, head._id AS head_version_internal_id, "
                "head.version_id AS head_version_id, head.expires FROM archive_documents aggregate "
                "LEFT JOIN archive_document_versions head ON head._id = aggregate.head_version_id "
                "WHERE aggregate.archive = $1 AND aggregate.document_id = $2 FOR UPDATE OF aggregate",
                archive,
                document_id,
            )
            if aggregate is None or aggregate["lifecycle"] == "deleted":
                return DeletionRequest(accepted=False)
            if aggregate["lifecycle"] == "active":
                expires = aggregate["expires"]
                if safe and expires is None:
                    raise ForbiddenError(
                        "Cannot safely delete a document without expiration."
                    )
                if safe and expires > now:
                    raise UnsafeArchiveDeleteError(expires)
                await connection.execute(
                    "UPDATE archive_documents SET lifecycle = 'deleting', updated_at = now() WHERE _id = $1",
                    aggregate["_id"],
                )
            rows = await connection.fetch(
                "SELECT $4::bigint AS aggregate_id, version._id AS version_internal_id, "
                "representation._id AS representation_internal_id, $1::text AS archive, "
                "$2::uuid AS document_id, version.version_id, representation.representation_id, "
                "$3::uuid AS head_version_id, $5::bigint AS head_version_internal_id, "
                "representation.backend, representation.backend_revision, representation.storage_key "
                "FROM archive_document_versions version JOIN archive_document_representations representation "
                "ON representation.version_internal_id = version._id WHERE version.aggregate_id = $4 "
                "AND representation.state = 'available' AND representation.storage_key IS NOT NULL",
                archive,
                document_id,
                aggregate["head_version_id"],
                aggregate["_id"],
                aggregate["head_version_internal_id"],
            )
            commands = [DeletionCommand(**dict(row), kind="document") for row in rows]
            if not commands:
                await cls._finalize_tombstone(
                    connection,
                    aggregate["_id"],
                    aggregate["head_version_id"],
                    aggregate["head_version_internal_id"],
                )
                finalized = True

        await cls._publish_commands(commands)
        await publish_archive_event(
            cls._jetstream(),
            event_type="xarta.archive.document.deletion-requested",
            archive=archive,
            document_id=document_id,
            aggregate_id=aggregate["_id"],
            version_internal_id=aggregate["head_version_internal_id"],
            version_id=aggregate["head_version_id"],
            outcome="deletion_requested",
            occurred_at=now,
        )
        if finalized:
            await publish_archive_event(
                cls._jetstream(),
                event_type="xarta.archive.document.deleted",
                archive=archive,
                document_id=document_id,
                aggregate_id=aggregate["_id"],
                version_internal_id=aggregate["head_version_internal_id"],
                version_id=aggregate["head_version_id"],
                outcome="deleted",
                occurred_at=now,
            )
        return DeletionRequest(accepted=True)

    @staticmethod
    async def _finalize_tombstone(
        connection: Connection,
        aggregate_id: int,
        head_version_id: UUID,
        head_version_internal_id: int,
    ) -> None:
        await connection.execute(
            "UPDATE archive_documents SET lifecycle = 'deleted', head_version_id = NULL, "
            "deleted_head_version_id = $2, deleted_head_version_internal_id = $3, "
            "updated_at = now() WHERE _id = $1 AND lifecycle = 'deleting'",
            aggregate_id,
            head_version_id,
            head_version_internal_id,
        )
        await connection.execute(
            "DELETE FROM archive_idempotency WHERE aggregate_id = $1", aggregate_id
        )
        await connection.execute(
            "DELETE FROM archive_document_versions WHERE aggregate_id = $1",
            aggregate_id,
        )

    @classmethod
    async def pending_deletion_commands(
        cls, *, limit: int, after: tuple[int, int] | None = None
    ) -> list[DeletionCommand]:
        after_aggregate_id, after_representation_id = after or (None, None)
        async with cls.pool().acquire() as connection:
            rows = await connection.fetch(
                "WITH pending AS (SELECT aggregate._id AS aggregate_id, "
                "version._id AS version_internal_id, representation._id AS representation_internal_id, "
                "aggregate.archive, aggregate.document_id, version.version_id, "
                "representation.representation_id, head.version_id AS head_version_id, "
                "head._id AS head_version_internal_id, representation.backend, "
                "representation.backend_revision, representation.storage_key, 'document'::text AS kind, "
                "NULL::text AS history_policy FROM archive_documents aggregate "
                "JOIN archive_document_versions version ON version.aggregate_id = aggregate._id "
                "JOIN archive_document_representations representation "
                "ON representation.version_internal_id = version._id AND representation.state = 'available' "
                "JOIN archive_document_versions head ON head._id = aggregate.head_version_id "
                "WHERE aggregate.lifecycle = 'deleting' AND ($1::bigint IS NULL OR "
                "(aggregate._id, representation._id) > ($1::bigint, $2::bigint)) UNION ALL "
                "SELECT aggregate._id, version._id, representation._id, aggregate.archive, "
                "aggregate.document_id, version.version_id, representation.representation_id, "
                "head.version_id, head._id, representation.backend, representation.backend_revision, "
                "representation.storage_key, 'history'::text, version.history_cleanup_policy::text "
                "FROM archive_document_versions version JOIN archive_documents aggregate "
                "ON aggregate._id = version.aggregate_id JOIN archive_document_representations representation "
                "ON representation.version_internal_id = version._id AND representation.state = 'available' "
                "JOIN archive_document_versions head ON head._id = aggregate.head_version_id "
                "WHERE version.history_cleanup_policy IS NOT NULL AND aggregate.lifecycle = 'active' "
                "AND ($1::bigint IS NULL OR (aggregate._id, representation._id) > ($1::bigint, $2::bigint))) "
                "SELECT * FROM pending ORDER BY aggregate_id, representation_internal_id LIMIT $3",
                after_aggregate_id,
                after_representation_id,
                limit,
            )
        return [DeletionCommand(**dict(row)) for row in rows]

    @classmethod
    async def validate_deletion_command(cls, command: DeletionCommand) -> bool:
        async with cls.pool().acquire() as connection:
            row = await connection.fetchrow(
                "SELECT aggregate.archive, aggregate.document_id, aggregate.lifecycle, "
                "version.version_id, version.history_cleanup_policy::text AS history_cleanup_policy, "
                "version._id = aggregate.head_version_id AS head, representation.representation_id, "
                "representation.state, representation.backend, representation.backend_revision, "
                "representation.storage_key FROM archive_documents aggregate "
                "JOIN archive_document_versions version ON version.aggregate_id = aggregate._id "
                "AND version._id = $2 JOIN archive_document_representations representation "
                "ON representation.version_internal_id = version._id AND representation._id = $3 "
                "WHERE aggregate._id = $1",
                command.aggregate_id,
                command.version_internal_id,
                command.representation_internal_id,
            )
        if row is None or row["state"] == "metadata-only":
            return False
        if command.kind == "document" and row["lifecycle"] != "deleting":
            return False
        if command.kind == "history" and (
            row["lifecycle"] != "active"
            or row["head"]
            or row["history_cleanup_policy"] != command.history_policy
        ):
            return False
        identities = (
            row["archive"],
            row["document_id"],
            row["version_id"],
            row["representation_id"],
        )
        expected = (
            command.archive,
            command.document_id,
            command.version_id,
            command.representation_id,
        )
        if identities != expected:
            raise ValueError("Deletion command does not match public identities")
        coordinates = (row["backend"], row["backend_revision"], row["storage_key"])
        if coordinates != (
            command.backend,
            command.backend_revision,
            command.storage_key,
        ):
            raise ValueError(
                "Deletion command does not match pinned storage coordinates"
            )
        return True

    @classmethod
    async def complete_deletion(cls, command: DeletionCommand) -> DeletionCompletion:
        async with cls.pool().acquire() as connection, connection.transaction():
            aggregate = await connection.fetchrow(
                "SELECT aggregate._id, aggregate.lifecycle FROM archive_documents aggregate "
                "JOIN archive_document_versions version ON version.aggregate_id = aggregate._id "
                "AND version._id = $2 JOIN archive_document_representations representation "
                "ON representation.version_internal_id = version._id AND representation._id = $3 "
                "WHERE aggregate._id = $1 FOR UPDATE OF aggregate, version, representation",
                command.aggregate_id,
                command.version_internal_id,
                command.representation_internal_id,
            )
            if aggregate is None:
                return DeletionCompletion()
            representation_transitioned = await connection.fetchval(
                "UPDATE archive_document_representations SET state = 'metadata-only', storage_key = NULL "
                "WHERE version_internal_id = $1 AND _id = $2 AND state = 'available' "
                "AND backend = $3 AND backend_revision = $4 AND storage_key = $5 RETURNING true",
                command.version_internal_id,
                command.representation_internal_id,
                command.backend,
                command.backend_revision,
                command.storage_key,
            )
            if not representation_transitioned:
                return DeletionCompletion()
            remaining_in_version = await connection.fetchval(
                "SELECT EXISTS (SELECT 1 FROM archive_document_representations "
                "WHERE version_internal_id = $1 AND state = 'available')",
                command.version_internal_id,
            )
            version_transitioned = False
            if not remaining_in_version:
                version_transitioned = bool(
                    await connection.fetchval(
                        "UPDATE archive_document_versions SET state = 'metadata-only', "
                        "history_cleanup_policy = NULL WHERE _id = $1 AND state = 'available' RETURNING true",
                        command.version_internal_id,
                    )
                )
            if aggregate["lifecycle"] != "deleting":
                return DeletionCompletion(True, version_transitioned)
            remaining = await connection.fetchval(
                "SELECT EXISTS (SELECT 1 FROM archive_document_representations representation "
                "JOIN archive_document_versions version ON version._id = representation.version_internal_id "
                "WHERE version.aggregate_id = $1 AND representation.state = 'available')",
                command.aggregate_id,
            )
            if remaining:
                return DeletionCompletion(True, version_transitioned)
            await cls._finalize_tombstone(
                connection,
                command.aggregate_id,
                command.head_version_id,
                command.head_version_internal_id,
            )
            return DeletionCompletion(True, version_transitioned, finalized=True)
