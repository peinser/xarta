"""Authoritative, cursor-based search of logical metadata on current HEADs."""

from __future__ import annotations

import hashlib
import math

from typing import TYPE_CHECKING
from typing import Any
from uuid import UUID

import asyncpg  # type: ignore[import-untyped]
import orjson

from sanic import response
from sanic.exceptions import SanicException

from xarta.exceptions.http import BadRequestError
from xarta.exceptions.http import ServiceUnavailable
from xarta.http.pagination import Page
from xarta.http.pagination import decode_cursor
from xarta.http.pagination import encode_cursor
from xarta.http.pagination import parse_limit
from xarta.protocol.document.archive import validate_archive_name

from .base import bp
from .db import ArchivePostgresModel as DB

if TYPE_CHECKING:
    from sanic import HTTPResponse
    from sanic import Request

MAX_SEARCH_BYTES = 16 * 1024
MAX_METADATA_DEPTH = 8
DEFAULT_SEARCH_LIMIT = 50
MAX_SEARCH_LIMIT = 100


def _metadata_filter(value: dict[str, Any], depth: int = 0) -> None:
    if depth > MAX_METADATA_DEPTH:
        raise ValueError(
            f"metadata filters support at most {MAX_METADATA_DEPTH} nested object levels"
        )
    for key, child in value.items():
        if not isinstance(key, str) or "\0" in key:
            raise ValueError(
                "metadata filter keys must be strings without NUL characters"
            )
        if isinstance(child, dict):
            if not child:
                raise ValueError(
                    "nested metadata filter objects must contain scalar matches"
                )
            _metadata_filter(child, depth + 1)
        elif child is not None and not isinstance(child, (str, bool, int, float)):
            raise ValueError(
                "metadata filters accept scalar values and nested objects only"
            )
        elif isinstance(child, str) and "\0" in child:
            raise ValueError("metadata filter strings must not contain NUL characters")
        elif isinstance(child, float) and not math.isfinite(child):
            raise ValueError("metadata filter numbers must be finite")


def parse_filter(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("filter must be an object")
    unknown = value.keys() - {"document_type", "metadata"}
    if unknown:
        raise ValueError(f"Unsupported filter fields: {', '.join(sorted(unknown))}")
    filters = {}
    if "document_type" in value:
        document_type = value["document_type"]
        if document_type is not None and (
            not isinstance(document_type, str)
            or not document_type
            or "\0" in document_type
        ):
            raise ValueError(
                "document_type must be a nonempty string without NUL characters, or null"
            )
        filters["document_type"] = document_type
    if "metadata" in value:
        metadata = value["metadata"]
        if not isinstance(metadata, dict):
            raise ValueError("filter.metadata must be an object")
        _metadata_filter(metadata)
        # An empty root is no restriction, not an expensive empty GIN predicate.
        if metadata:
            filters["metadata"] = metadata
    return filters


def search_cursor_scope(archive: str, filters: dict[str, Any]) -> str:
    criteria = {
        "archive": archive,
        "filter": filters,
        "order": "document_id:asc",
        "versions": "head",
    }
    digest = hashlib.sha256(
        orjson.dumps(criteria, option=orjson.OPT_SORT_KEYS)
    ).hexdigest()
    return f"archive-head-metadata:v1:{digest}"


@bp.post("/archives/<archive:str>/documents/search")
async def search_scoped(request: Request, archive: str) -> HTTPResponse:
    return await _search(request, archive)


@bp.post("/documents/search")
async def search_default(request: Request) -> HTTPResponse:
    return await _search(request, "default")


async def _search(request: Request, archive: str) -> HTTPResponse:
    if request.content_type != "application/json":
        raise SanicException(
            "Send an application/json search request.", status_code=415
        )
    if len(request.body) > MAX_SEARCH_BYTES:
        raise SanicException(
            f"Search request exceeds {MAX_SEARCH_BYTES} bytes.", status_code=413
        )
    try:
        if request.args:
            raise ValueError(
                "Search parameters belong in the JSON body, not the query string"
            )
        validate_archive_name(archive)
        payload = orjson.loads(request.body)
        if not isinstance(payload, dict):
            raise ValueError("A search request must be a JSON object")
        unknown = payload.keys() - {"filter", "limit", "cursor"}
        if unknown:
            raise ValueError(f"Unsupported search fields: {', '.join(sorted(unknown))}")
        filters = parse_filter(payload.get("filter", {}))
        if "limit" in payload and type(payload["limit"]) is not int:
            raise ValueError("limit must be an integer")
        limit = parse_limit(
            payload.get("limit"), default=DEFAULT_SEARCH_LIMIT, maximum=MAX_SEARCH_LIMIT
        )
        scope = search_cursor_scope(archive, filters)
        after = None
        if "cursor" in payload:
            values = decode_cursor(
                payload["cursor"], scope=scope, keys=frozenset({"document_id"})
            )
            after = UUID(values["document_id"])
    except ValueError as ex:
        raise BadRequestError(str(ex)) from ex

    try:
        rows = await DB.search_heads(archive, filters, limit=limit, after=after)
    except (
        TimeoutError,
        asyncpg.QueryCanceledError,
        asyncpg.PostgresConnectionError,
    ) as ex:
        raise ServiceUnavailable(
            "Archive metadata search is temporarily unavailable."
        ) from ex
    items = rows[:limit]
    next_cursor = (
        encode_cursor(scope, {"document_id": items[-1]["id"]})
        if len(rows) > limit
        else None
    )
    return response.json(
        {"archive": archive, **Page(items, next_cursor, limit=limit).dict(dict)}
    )
