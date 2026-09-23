from __future__ import annotations

import datetime

from typing import TYPE_CHECKING
from uuid import UUID

from sanic import response

from xarta.exceptions.http import BadRequestError
from xarta.http.pagination import Page
from xarta.http.pagination import PaginationError
from xarta.http.pagination import decode_cursor
from xarta.http.pagination import encode_cursor
from xarta.http.pagination import parse_limit

from .base import bp
from .db import ArchivePostgresModel as DB

if TYPE_CHECKING:
    from sanic import HTTPResponse
    from sanic import Request


DEFAULT_VERSION_LIMIT = 50
MAX_VERSION_LIMIT = 100


def _cursor_scope(archive: str, document_id: UUID) -> str:
    return f"archive:{archive}:document:{document_id}:versions"


def _encode_version_cursor(
    archive: str, document_id: UUID, created: datetime.datetime, version_id: UUID
) -> str:
    return encode_cursor(
        _cursor_scope(archive, document_id),
        {"created": created.isoformat(), "version_id": str(version_id)},
    )


def _decode_cursor(
    value: str | None, archive: str, document_id: UUID
) -> tuple[datetime.datetime, UUID] | None:
    if value is None:
        return None
    try:
        cursor = decode_cursor(
            value,
            scope=_cursor_scope(archive, document_id),
            keys=frozenset({"created", "version_id"}),
        )
        created = datetime.datetime.fromisoformat(cursor["created"])
        if created.tzinfo is None:
            raise ValueError("cursor timestamp has no timezone")
        return created, UUID(cursor["version_id"])
    except (PaginationError, ValueError) as ex:
        raise BadRequestError("cursor is invalid") from ex


@bp.get("/archives/<archive:str>/documents/<document_id:uuid>/details")
async def retrieve_current_details(
    _: Request, archive: str, document_id: UUID
) -> HTTPResponse:
    return response.json((await DB.find_one(archive, document_id)).dict())


@bp.get(
    "/archives/<archive:str>/documents/<document_id:uuid>/versions/<version_id:uuid>/details"
)
async def retrieve_version_details(
    _: Request, archive: str, document_id: UUID, version_id: UUID
) -> HTTPResponse:
    return await _retrieve_version_details(archive, document_id, version_id)


async def _retrieve_version_details(
    archive: str, document_id: UUID, version_id: UUID
) -> HTTPResponse:
    return response.json((await DB.find_one(archive, document_id, version_id)).dict())


@bp.get("/archives/<archive:str>/documents/<document_id:uuid>/versions")
async def retrieve_versions(
    request: Request, archive: str, document_id: UUID
) -> HTTPResponse:
    return await _retrieve_versions(request, archive, document_id)


async def _retrieve_versions(
    request: Request, archive: str, document_id: UUID
) -> HTTPResponse:
    try:
        limit = parse_limit(
            request.args.get("limit"),
            default=DEFAULT_VERSION_LIMIT,
            maximum=MAX_VERSION_LIMIT,
        )
    except PaginationError as ex:
        raise BadRequestError(str(ex)) from ex
    page = await DB.find_versions(
        archive,
        document_id,
        limit=limit,
        before=_decode_cursor(request.args.get("cursor"), archive, document_id),
    )
    next_cursor = None
    if page.has_more:
        last = page.items[-1]
        assert last.version_id is not None
        next_cursor = _encode_version_cursor(
            archive, document_id, last.created, last.version_id
        )
    representation = Page(page.items, next_cursor).dict(lambda version: version.dict())
    return response.json(
        {
            "archive": archive,
            "document_id": str(document_id),
            "head_version_id": str(page.head_version_id),
            **representation,
        }
    )


@bp.get("/documents/<document_id:uuid>/details")
async def retrieve_default_details(_: Request, document_id: UUID) -> HTTPResponse:
    return response.json((await DB.find_one("default", document_id)).dict())


@bp.get("/documents/<document_id:uuid>/versions")
async def retrieve_default_versions(
    request: Request, document_id: UUID
) -> HTTPResponse:
    return await _retrieve_versions(request, "default", document_id)


@bp.get("/documents/<document_id:uuid>/versions/<version_id:uuid>/details")
async def retrieve_default_version_details(
    _: Request, document_id: UUID, version_id: UUID
) -> HTTPResponse:
    return await _retrieve_version_details("default", document_id, version_id)


async def _retrieve_representations(
    archive: str, document_id: UUID, version_id: UUID | None
) -> HTTPResponse:
    version = await DB.find_one(archive, document_id, version_id)
    return response.json(
        {
            "archive": archive,
            "document_id": str(document_id),
            "version_id": str(version.version_id),
            "default_representation_id": str(version.default_representation_id),
            "representations": [item.dict() for item in version.representations],
        }
    )


@bp.get("/archives/<archive:str>/documents/<document_id:uuid>/representations")
async def retrieve_head_representations(
    _: Request, archive: str, document_id: UUID
) -> HTTPResponse:
    return await _retrieve_representations(archive, document_id, None)


@bp.get(
    "/archives/<archive:str>/documents/<document_id:uuid>/versions/<version_id:uuid>/representations"
)
async def retrieve_version_representations(
    _: Request, archive: str, document_id: UUID, version_id: UUID
) -> HTTPResponse:
    return await _retrieve_representations(archive, document_id, version_id)


@bp.get("/documents/<document_id:uuid>/representations")
async def retrieve_default_head_representations(
    _: Request, document_id: UUID
) -> HTTPResponse:
    return await _retrieve_representations("default", document_id, None)


@bp.get("/documents/<document_id:uuid>/versions/<version_id:uuid>/representations")
async def retrieve_default_version_representations(
    _: Request, document_id: UUID, version_id: UUID
) -> HTTPResponse:
    return await _retrieve_representations("default", document_id, version_id)
