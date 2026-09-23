r"""
Endpoints related to retrieving document types.
"""

# ruff: noqa: I001

from __future__ import annotations

import urllib.parse

from typing import TYPE_CHECKING

from jsonschema import Draft7Validator  # type: ignore[import-untyped]
from sanic import response

from xarta import cache
from xarta.exceptions.http import BadRequestError
from xarta.exceptions.http import NotFoundError
from xarta.http.pagination import Page
from xarta.http.pagination import PaginationError
from xarta.http.pagination import decode_cursor
from xarta.http.pagination import encode_cursor
from xarta.http.pagination import parse_limit
from xarta.protocol.document.request.schema import SCHEMA_DOCUMENT_RENDER_REQUEST
from xarta.protocol.document.type import DocumentTypeIdentifier
from xarta.protocol.document.type.constants import DOCUMENT_TYPE_CACHE_NAMESPACE_READ
from xarta.protocol.document.type.constants import (
    DOCUMENT_TYPE_CACHE_NAMESPACE_READ_TTL,
)

from .base import DB
from .base import bp

if TYPE_CHECKING:
    from typing import Any

    from xarta.protocol.document.type import DocumentType

    from sanic import HTTPResponse
    from sanic import Request


DEFAULT_DOCUMENT_TYPE_LIMIT = 50
MAX_DOCUMENT_TYPE_LIMIT = 100
DOCUMENT_TYPE_CURSOR_SCOPE = "document-types"
_render_request_validator = Draft7Validator(SCHEMA_DOCUMENT_RENDER_REQUEST)


def _decode_cursor(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return decode_cursor(
            value,
            scope=DOCUMENT_TYPE_CURSOR_SCOPE,
            keys=frozenset({"identifier"}),
        )["identifier"]
    except PaginationError as ex:
        raise BadRequestError("cursor is invalid") from ex


def _summary(document_type: DocumentType) -> dict[str, Any]:
    representation = document_type.dict()
    return {
        "identifier": document_type.identifier.value,
        "metadata": document_type.metadata,
        "defaults": {
            "content_type": document_type.default_content_type,
            "template_engine": document_type.default_template_engine.value,
            "retention": representation["defaults"]["retention"],
        },
    }


@bp.get("/")
async def list_document_types(request: Request) -> HTTPResponse:
    try:
        limit = parse_limit(
            request.args.get("limit"),
            default=DEFAULT_DOCUMENT_TYPE_LIMIT,
            maximum=MAX_DOCUMENT_TYPE_LIMIT,
        )
    except PaginationError as ex:
        raise BadRequestError(str(ex)) from ex
    documents = await DB.find_many(
        limit=limit + 1,
        after=_decode_cursor(request.args.get("cursor")),
    )
    items = documents[:limit]
    next_cursor = None
    if len(documents) > limit:
        next_cursor = encode_cursor(
            DOCUMENT_TYPE_CURSOR_SCOPE,
            {"identifier": items[-1].identifier.value},
        )
    return response.json(Page(items, next_cursor).dict(_summary))


@bp.route("/<identifier:str>", methods=["GET"])
async def retrieve(_: Request, identifier: str) -> HTTPResponse:
    # Decode the identifier
    identifier = urllib.parse.unquote(identifier)

    # Attempt to retrieve cached document type representation
    cached_representation = await cache.manager.get(
        identifier, namespace=DOCUMENT_TYPE_CACHE_NAMESPACE_READ
    )
    if cached_representation:
        return response.json(cached_representation)

    # Fetch document type from the database if not cached
    document_type = await DB.find_one(DocumentTypeIdentifier(identifier))
    if not document_type:
        raise NotFoundError

    # Convert to dictionary and cache the representation
    representation = document_type.dict()
    r = response.json(representation)

    await cache.manager.set(
        key=identifier,
        value=r.body,
        ttl=DOCUMENT_TYPE_CACHE_NAMESPACE_READ_TTL,
        namespace=DOCUMENT_TYPE_CACHE_NAMESPACE_READ,
    )

    return r


def _json_pointer(parts) -> str:
    encoded = [str(part).replace("~", "~0").replace("/", "~1") for part in parts]
    return "/" + "/".join(encoded) if encoded else ""


@bp.post("/<identifier:str>/check")
async def check_document_type(request: Request, identifier: str) -> HTTPResponse:
    identifier = urllib.parse.unquote(identifier)
    document_type = await DB.find_one(DocumentTypeIdentifier(identifier))
    if not document_type:
        raise NotFoundError
    if not isinstance(request.json, dict):
        raise BadRequestError("A document check must be a JSON object.")

    candidate = {"document_type": identifier, **request.json}
    if candidate["document_type"] != identifier:
        raise BadRequestError("The document_type must match the requested identifier.")
    errors = sorted(
        _render_request_validator.iter_errors(candidate),
        key=lambda error: (
            tuple(str(part) for part in error.absolute_path),
            error.message,
        ),
    )
    if errors:
        return response.json(
            {
                "identifier": identifier,
                "validation_scope": "render-request",
                "valid": False,
                "errors": [
                    {
                        "path": _json_pointer(error.absolute_path),
                        "schema_path": _json_pointer(error.absolute_schema_path),
                        "keyword": error.validator,
                        "message": error.message,
                    }
                    for error in errors[:100]
                ],
            }
        )

    normalized = dict(candidate)
    normalized["content_type"] = (
        candidate.get("content_type") or document_type.default_content_type
    )
    normalized["template_engine"] = (
        candidate.get("template_engine") or document_type.default_template_engine.value
    )
    normalized["template_engine_options"] = (
        document_type.default_template_engine_options
        | (candidate.get("template_engine_options") or {})
    )
    normalized["metadata"] = document_type.default_metadata | (
        candidate.get("metadata") or {}
    )
    return response.json(
        {
            "identifier": identifier,
            "validation_scope": "render-request",
            "valid": True,
            "normalized_request": normalized,
        }
    )
