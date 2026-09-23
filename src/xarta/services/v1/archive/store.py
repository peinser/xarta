from __future__ import annotations

import datetime
import hashlib
import re
import uuid

from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Any
from uuid import UUID

import asyncpg  # type: ignore[import-untyped]
import orjson

from sanic import response

from xarta import env
from xarta.exceptions.http import BadRequestError
from xarta.exceptions.http import NotFoundError
from xarta.exceptions.protocol import InvalidDocumentType
from xarta.protocol.document.type import DocumentTypeIdentifier
from xarta.protocol.document.type.utils import fetch as fetch_document_type

from .base import bp
from .db import ArchiveConflictError
from .db import ArchivePostgresModel as DB
from .db import RepresentationWrite
from .db import VersionWrite

if TYPE_CHECKING:
    from sanic import HTTPResponse
    from sanic import Request

    from .storage import ArchiveStorageRegistry


CONTENT_TYPE = re.compile(r"^[A-Za-z0-9!#$%&*+.^_`|~-]+/[A-Za-z0-9!#$%&*+.^_`|~-]+$")
REPRESENTATION_WRITE_CONCURRENCY = env.extract(
    key="ARCHIVE_REPRESENTATION_WRITE_CONCURRENCY",
    default="10",
    dtype=int,
    verify=lambda value: 1 <= int(value) <= 100,
)


@dataclass(frozen=True)
class ParsedRepresentation:
    write: RepresentationWrite
    data: bytes


@dataclass(frozen=True)
class ParsedSnapshotRequest:
    write: VersionWrite
    representations: tuple[ParsedRepresentation, ...]


def _uuid(value: Any, field: str, default: UUID) -> UUID:
    try:
        return UUID(str(value)) if value else default
    except (ValueError, TypeError) as ex:
        raise BadRequestError(f"{field} must be a valid UUID.") from ex


def _timestamp(value: Any, field: str) -> datetime.datetime | None:
    if not value:
        return None
    try:
        result = datetime.datetime.fromisoformat(str(value))
    except ValueError as ex:
        raise BadRequestError(f"{field} must be an ISO 8601 timestamp.") from ex
    if result.tzinfo is None:
        raise BadRequestError(f"{field} must include a timezone.")
    return result


def _object(value: Any, field: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise BadRequestError(f"{field} must be a JSON object.")
    return value


def _representation_semantics(item: ParsedRepresentation) -> dict[str, Any]:
    return {
        "name": item.write.name,
        "content_type": item.write.content_type,
        "metadata": item.write.metadata,
        "checksum": item.write.checksum.hex(),
        "size": item.write.size,
    }


def _digest(value: Any) -> bytes:
    return hashlib.sha512(orjson.dumps(value, option=orjson.OPT_SORT_KEYS)).digest()


async def _parse_snapshot(request: Request, archive: str) -> ParsedSnapshotRequest:
    form: Any = request.form or {}
    files: Any = request.files or {}
    manifest_input = form.get("manifest")
    if not manifest_input:
        raise BadRequestError("A snapshot manifest is required.")
    try:
        manifest = orjson.loads(manifest_input)
    except orjson.JSONDecodeError as ex:
        raise BadRequestError("manifest must contain valid JSON.") from ex
    if not isinstance(manifest, dict):
        raise BadRequestError("manifest must be a JSON object.")

    registry: ArchiveStorageRegistry = request.app.ctx.archive_storage
    try:
        target = registry.resolve(archive)
    except KeyError as ex:
        raise NotFoundError(str(ex)) from ex

    supplied_idempotency = "Idempotency-Key" in request.headers
    idempotency_key = request.headers.get("Idempotency-Key") or str(uuid.uuid4())
    if supplied_idempotency and not manifest.get("created"):
        raise BadRequestError("created is required when Idempotency-Key is supplied.")
    document_id = _uuid(
        manifest.get("document_id"),
        "document_id",
        uuid.uuid5(uuid.NAMESPACE_URL, f"{archive}:{idempotency_key}:document"),
    )
    version_id = _uuid(
        manifest.get("version_id"),
        "version_id",
        uuid.uuid5(
            uuid.NAMESPACE_URL, f"{archive}:{idempotency_key}:{document_id}:version"
        ),
    )
    parent_id = (
        _uuid(manifest["parent_version_id"], "parent_version_id", version_id)
        if manifest.get("parent_version_id")
        else None
    )
    created = _timestamp(manifest.get("created"), "created") or datetime.datetime.now(
        tz=datetime.UTC
    )
    expires = _timestamp(manifest.get("expires"), "expires")
    if expires and expires <= created:
        raise BadRequestError("expires must be later than created.")

    document_type_value = manifest.get("document_type")
    if document_type_value is not None and not isinstance(document_type_value, str):
        raise BadRequestError("document_type must be a string.")
    document_type = (
        DocumentTypeIdentifier(document_type_value) if document_type_value else None
    )
    if document_type:
        configuration = await fetch_document_type(document_type)
        if configuration is None:
            raise InvalidDocumentType(f"Unknown document type: {document_type.value}")
        if not expires and configuration.default_retention:
            expires = created + configuration.default_retention

    specifications = manifest.get("representations")
    if not isinstance(specifications, list) or not specifications:
        raise BadRequestError("representations must be a nonempty array.")
    parsed = []
    fields: set[str] = set()
    identities: set[UUID] = set()
    for index, specification in enumerate(specifications):
        if not isinstance(specification, dict):
            raise BadRequestError(f"representations[{index}] must be a JSON object.")
        field_name = specification.get("field")
        if not isinstance(field_name, str) or not field_name:
            raise BadRequestError(
                f"representations[{index}].field must be a nonempty string."
            )
        if field_name in fields:
            raise BadRequestError("representation multipart fields must be unique.")
        fields.add(field_name)
        uploaded = files.get(field_name)
        if uploaded is None:
            raise BadRequestError(
                f"representation manifest references missing multipart field: {field_name}"
            )
        representation_id = _uuid(
            specification.get("representation_id"),
            f"representations[{index}].representation_id",
            uuid.uuid5(version_id, f"representation:{index}:{field_name}"),
        )
        if representation_id in identities:
            raise BadRequestError("representation_id values must be unique.")
        identities.add(representation_id)
        name = specification.get("name")
        if name is not None and (
            not isinstance(name, str)
            or not name.strip()
            or any(ord(character) < 32 or ord(character) == 127 for character in name)
        ):
            raise BadRequestError(
                "representation name must be nonempty and contain no control characters."
            )
        content_type = specification.get("content_type")
        if not isinstance(content_type, str) or not CONTENT_TYPE.fullmatch(
            content_type
        ):
            raise BadRequestError("representation content_type is invalid.")
        data = uploaded.body
        checksum = hashlib.sha512(data).digest()
        storage_key = f"{archive}/{document_id}/{version_id}/{representation_id}"
        parsed.append(
            ParsedRepresentation(
                write=RepresentationWrite(
                    representation_id=representation_id,
                    name=name,
                    content_type=content_type,
                    metadata=_object(
                        specification.get("metadata"),
                        f"representations[{index}].metadata",
                    ),
                    backend=target.backend_name,
                    backend_revision=target.backend_revision,
                    storage_key=storage_key,
                    checksum=checksum,
                    size=len(data),
                ),
                data=data,
            )
        )

    default_input = manifest.get("default_representation_id")
    if default_input is None and len(parsed) != 1:
        raise BadRequestError(
            "default_representation_id is required when several representations are supplied."
        )
    default_id = (
        parsed[0].write.representation_id
        if default_input is None
        else _uuid(default_input, "default_representation_id", version_id)
    )
    if default_id not in identities:
        raise BadRequestError("default representation does not belong to version.")

    canonical = sorted(parsed, key=lambda item: str(item.write.representation_id))
    default = next(
        item for item in parsed if item.write.representation_id == default_id
    )
    metadata = _object(manifest.get("metadata"), "metadata")
    snapshot_semantics = {
        "expires": expires.isoformat() if expires else None,
        "document_type": document_type_value,
        "metadata": metadata,
        "default_representation": _representation_semantics(default),
        "representations": sorted(
            (_representation_semantics(item) for item in parsed),
            key=lambda item: orjson.dumps(item, option=orjson.OPT_SORT_KEYS),
        ),
    }
    request_semantics = {
        "archive": archive,
        "document_id": str(document_id),
        "version_id": str(version_id),
        "parent_version_id": str(parent_id) if parent_id else None,
        "created": created.isoformat(),
        **snapshot_semantics,
        "default_representation_id": str(default_id),
        "representations": [
            {
                "representation_id": str(item.write.representation_id),
                **_representation_semantics(item),
            }
            for item in canonical
        ],
    }
    write = VersionWrite(
        archive=archive,
        document_id=document_id,
        version_id=version_id,
        parent_version_id=parent_id,
        idempotency_key=idempotency_key,
        fingerprint=_digest(request_semantics),
        snapshot_fingerprint=_digest(snapshot_semantics),
        created=created,
        expires=expires,
        document_type=document_type_value,
        metadata=metadata,
        default_representation_id=default_id,
        representations=tuple(item.write for item in canonical),
    )
    return ParsedSnapshotRequest(write=write, representations=tuple(parsed))


@bp.put("/archives/<archive:str>/documents")
async def insert_scoped(request: Request, archive: str) -> HTTPResponse:
    return await _insert(request, archive)


@bp.put("/documents")
async def insert_default(request: Request) -> HTTPResponse:
    return await _insert(request, "default")


async def _insert(request: Request, archive: str) -> HTTPResponse:
    snapshot = await _parse_snapshot(request, archive)
    target = request.app.ctx.archive_storage.resolve(archive)
    contents = {
        item.write.representation_id: item.data for item in snapshot.representations
    }

    async def persist(representation: RepresentationWrite) -> bool:
        return bool(
            await target.backend.put(
                representation.storage_key,
                contents[representation.representation_id],
                representation.content_type,
            )
        )

    async def rollback(representation: RepresentationWrite) -> None:
        await target.backend.delete(representation.storage_key)

    try:
        result = await DB.prepare_version(
            snapshot.write,
            target.policy,
            persist,
            rollback,
            representation_concurrency=REPRESENTATION_WRITE_CONCURRENCY,
        )
    except ArchiveConflictError as ex:
        return response.json({"outcome": ex.outcome, "error": str(ex)}, status=409)
    except (asyncpg.UniqueViolationError, FileExistsError):
        return response.json(
            {"outcome": "version_conflict", "error": "Version identity already exists"},
            status=409,
        )

    return response.json(
        result.dict(),
        status=(
            201
            if result.outcome in {"created", "version_created"} and not result.replayed
            else 200
        ),
    )
