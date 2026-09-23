from __future__ import annotations

import hashlib
import mimetypes
import re

from typing import TYPE_CHECKING
from urllib.parse import quote

from sanic import response

from xarta.exceptions.http import InternalServerError
from xarta.exceptions.http import NotFoundError

from .base import bp
from .db import ArchivePostgresModel as DB
from .db import StorageReference

if TYPE_CHECKING:
    from uuid import UUID

    from sanic import HTTPResponse
    from sanic import Request


def _acceptable(request: Request, reference: StorageReference) -> bool:
    return any(
        accepted.q > 0 and accepted.match(reference.content_type)
        for accepted in request.accept
    )


def _select_representation(
    request: Request, references: tuple[StorageReference, ...]
) -> StorageReference | tuple[StorageReference, ...] | None:
    default = next(reference for reference in references if reference.default)
    accept_header = request.headers.get("accept")
    if accept_header is None or accept_header.strip() == "*/*":
        return default
    if _acceptable(request, default):
        return default

    ranked: list[tuple[int, StorageReference]] = []
    for reference in references:
        for rank, accepted in enumerate(request.accept):
            if accepted.q > 0 and accepted.match(reference.content_type):
                ranked.append((rank, reference))
                break
    if not ranked:
        return None
    best_rank = min(rank for rank, _ in ranked)
    matches = [reference for rank, reference in ranked if rank == best_rank]
    if len(matches) == 1:
        return matches[0]
    return tuple(matches)


def _content_disposition(reference: StorageReference, document_id: UUID) -> str:
    if reference.name:
        filename = reference.name
    else:
        extension = (
            mimetypes.guess_extension(reference.content_type, strict=False) or ".bin"
        )
        filename = f"{document_id}-{reference.version_id}-{reference.representation_id}{extension}"
    fallback = (
        re.sub(r"[^A-Za-z0-9._-]", "_", filename).strip(".") or "archive-content.bin"
    )
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename, safe='')}"


async def _content_response(
    request: Request,
    document_id: UUID,
    reference: StorageReference,
    *,
    negotiated: bool,
) -> HTTPResponse:
    try:
        backend = request.app.ctx.archive_storage.backend(
            reference.backend, reference.backend_revision
        )
        data = await backend.get(reference.storage_key)
    except (FileNotFoundError, KeyError) as ex:
        raise NotFoundError("Archived content is unavailable") from ex
    if (
        len(data) != reference.size
        or hashlib.sha512(data).digest() != reference.checksum
    ):
        raise InternalServerError("Archived content failed integrity verification")
    headers = {
        "content-type": reference.content_type,
        "content-disposition": _content_disposition(reference, document_id),
        "content-length": str(reference.size),
        "etag": f'"sha512-{reference.checksum.hex()}"',
        "x-content-type-options": "nosniff",
    }
    if negotiated:
        headers["vary"] = "Accept"
    return response.raw(headers=headers, body=data, status=200)


async def _retrieve(
    request: Request, archive: str, document_id: UUID, version_id: UUID | None
) -> HTTPResponse:
    references = await DB.find_sources(archive, document_id, version_id)
    selected = _select_representation(request, references)
    if selected is None:
        return response.json(
            {"error": "requested content type is not available"},
            status=406,
            headers={"vary": "Accept"},
        )
    if isinstance(selected, tuple):
        return response.json(
            {
                "error": "representation selection is ambiguous",
                "representations": [str(item.representation_id) for item in selected],
            },
            status=300,
            headers={"vary": "Accept"},
        )
    return await _content_response(request, document_id, selected, negotiated=True)


async def _retrieve_exact(
    request: Request,
    archive: str,
    document_id: UUID,
    version_id: UUID,
    representation_id: UUID,
) -> HTTPResponse:
    reference = await DB.find_source(
        archive, document_id, version_id, representation_id
    )
    return await _content_response(request, document_id, reference, negotiated=False)


@bp.get("/archives/<archive:str>/documents/<document_id:uuid>")
async def retrieve_head(
    request: Request, archive: str, document_id: UUID
) -> HTTPResponse:
    return await _retrieve(request, archive, document_id, None)


@bp.get(
    "/archives/<archive:str>/documents/<document_id:uuid>/versions/<version_id:uuid>"
)
async def retrieve_version(
    request: Request, archive: str, document_id: UUID, version_id: UUID
) -> HTTPResponse:
    return await _retrieve(request, archive, document_id, version_id)


@bp.get(
    "/archives/<archive:str>/documents/<document_id:uuid>/versions/<version_id:uuid>"
    "/representations/<representation_id:uuid>"
)
async def retrieve_representation(
    request: Request,
    archive: str,
    document_id: UUID,
    version_id: UUID,
    representation_id: UUID,
) -> HTTPResponse:
    return await _retrieve_exact(
        request, archive, document_id, version_id, representation_id
    )


@bp.get("/documents/<document_id:uuid>")
async def retrieve_default(request: Request, document_id: UUID) -> HTTPResponse:
    return await _retrieve(request, "default", document_id, None)


@bp.get("/documents/<document_id:uuid>/versions/<version_id:uuid>")
async def retrieve_default_version(
    request: Request, document_id: UUID, version_id: UUID
) -> HTTPResponse:
    return await _retrieve(request, "default", document_id, version_id)


@bp.get(
    "/documents/<document_id:uuid>/versions/<version_id:uuid>/representations/<representation_id:uuid>"
)
async def retrieve_default_representation(
    request: Request,
    document_id: UUID,
    version_id: UUID,
    representation_id: UUID,
) -> HTTPResponse:
    return await _retrieve_exact(
        request, "default", document_id, version_id, representation_id
    )
