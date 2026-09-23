from __future__ import annotations

import datetime

from typing import TYPE_CHECKING

from sanic import response

from xarta.exceptions.http import BadRequestError

from .base import bp
from .db import ArchivePostgresModel as DB

if TYPE_CHECKING:
    from uuid import UUID

    from sanic import HTTPResponse
    from sanic import Request


_TRUE_FLAGS = {"true", "1", "yes"}
_FALSE_FLAGS = {"false", "0", "no"}


def _safe(value: str | None) -> bool:
    if value is None:
        return True
    normalized = value.lower()
    if normalized in _TRUE_FLAGS:
        return True
    if normalized in _FALSE_FLAGS:
        return False
    raise BadRequestError("safe must be true or false")


@bp.delete("/archives/<archive:str>/documents/<document_id:uuid>")
async def delete_scoped(
    request: Request, archive: str, document_id: UUID
) -> HTTPResponse:
    return await _delete(request, archive, document_id)


@bp.delete("/documents/<document_id:uuid>")
async def delete_default(request: Request, document_id: UUID) -> HTTPResponse:
    return await _delete(request, "default", document_id)


async def _delete(request: Request, archive: str, document_id: UUID) -> HTTPResponse:
    result = await DB.request_deletion(
        archive,
        document_id,
        safe=_safe(request.args.get("safe")),
        now=datetime.datetime.now(tz=datetime.UTC),
    )
    return response.empty(status=202 if result.accepted else 200)
