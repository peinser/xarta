from __future__ import annotations

import datetime

from types import SimpleNamespace
from unittest.mock import AsyncMock

import orjson
import pytest

from xarta.http.pagination import decode_cursor
from xarta.protocol.document.type import DocumentType
from xarta.protocol.document.type import DocumentTypeIdentifier
from xarta.protocol.template.engine import TemplateEngineIdentifier
from xarta.services.v1.documenttype.db import DocumentTypePostgresModel
from xarta.services.v1.documenttype.read import DOCUMENT_TYPE_CURSOR_SCOPE
from xarta.services.v1.documenttype.read import check_document_type
from xarta.services.v1.documenttype.read import list_document_types


def document_type(identifier: str = "invoice") -> DocumentType:
    return DocumentType(
        identifier=DocumentTypeIdentifier(identifier),
        default_template_engine=TemplateEngineIdentifier("jinja"),
        default_content_type="application/pdf",
        metadata={"description": f"Render {identifier} documents"},
        default_template_engine_options={
            "jinja": {"kind": "jinja", "template": {"path": f"{identifier}.html"}}
        },
        default_metadata={"producer": "Xarta"},
    )


@pytest.mark.asyncio
async def test_document_type_collection_is_cursor_paginated(monkeypatch) -> None:
    find_many = AsyncMock(
        return_value=(document_type("invoice"), document_type("statement"))
    )
    monkeypatch.setattr(DocumentTypePostgresModel, "find_many", find_many)

    result = await list_document_types(SimpleNamespace(args={}))
    payload = orjson.loads(result.body)

    assert payload["items"][0] == {
        "identifier": "invoice",
        "metadata": {"description": "Render invoice documents"},
        "defaults": {
            "content_type": "application/pdf",
            "template_engine": "jinja",
            "retention": None,
        },
    }
    assert payload["next_cursor"] is None
    find_many.assert_awaited_once_with(limit=51, after=None)


@pytest.mark.asyncio
async def test_document_type_collection_returns_next_cursor(monkeypatch) -> None:
    items = tuple(document_type(f"type-{index:03}") for index in range(3))
    monkeypatch.setattr(
        DocumentTypePostgresModel, "find_many", AsyncMock(return_value=items)
    )

    result = await list_document_types(SimpleNamespace(args={"limit": "2"}))
    payload = orjson.loads(result.body)
    cursor = decode_cursor(
        payload["next_cursor"],
        scope=DOCUMENT_TYPE_CURSOR_SCOPE,
        keys=frozenset({"identifier"}),
    )

    assert [item["identifier"] for item in payload["items"]] == [
        "type-000",
        "type-001",
    ]
    assert cursor == {"identifier": "type-001"}


def test_document_type_serializes_retention_as_iso_duration() -> None:
    item = document_type()
    item.default_retention = datetime.timedelta(days=30)

    assert item.dict()["defaults"]["retention"] == "P30D"

    item.default_retention = datetime.timedelta(0)
    assert item.dict()["defaults"]["retention"] == "P0D"


@pytest.mark.asyncio
async def test_document_type_check_resolves_render_defaults(monkeypatch) -> None:
    monkeypatch.setattr(
        DocumentTypePostgresModel,
        "find_one",
        AsyncMock(return_value=document_type()),
    )
    request = SimpleNamespace(
        json={
            "payload": {
                "content_type": "application/json",
                "data": {"invoice_number": "INV-42"},
            },
            "metadata": {"title": "Invoice 42"},
        }
    )

    result = await check_document_type(request, "invoice")
    payload = orjson.loads(result.body)

    assert payload["valid"] is True
    assert payload["validation_scope"] == "render-request"
    assert payload["normalized_request"]["content_type"] == "application/pdf"
    assert payload["normalized_request"]["template_engine"] == "jinja"
    assert payload["normalized_request"]["metadata"] == {
        "producer": "Xarta",
        "title": "Invoice 42",
    }
    assert payload["normalized_request"]["template_engine_options"] == {
        "jinja": {"kind": "jinja", "template": {"path": "invoice.html"}}
    }


@pytest.mark.asyncio
async def test_document_type_check_returns_deterministic_schema_errors(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        DocumentTypePostgresModel,
        "find_one",
        AsyncMock(return_value=document_type()),
    )

    result = await check_document_type(SimpleNamespace(json={"payload": {}}), "invoice")
    payload = orjson.loads(result.body)

    assert payload["valid"] is False
    assert payload["validation_scope"] == "render-request"
    assert [error["path"] for error in payload["errors"]] == ["/payload", "/payload"]
    assert {error["keyword"] for error in payload["errors"]} == {"required"}


@pytest.mark.asyncio
async def test_document_type_check_treats_explicit_nulls_as_omitted(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        DocumentTypePostgresModel,
        "find_one",
        AsyncMock(return_value=document_type()),
    )
    request = SimpleNamespace(
        json={
            "payload": {"content_type": "application/json", "data": {}},
            "template_engine": None,
            "template_engine_options": None,
            "metadata": None,
        }
    )

    result = await check_document_type(request, "invoice")
    normalized = orjson.loads(result.body)["normalized_request"]

    assert normalized["content_type"] == "application/pdf"
    assert normalized["template_engine"] == "jinja"
    assert normalized["template_engine_options"]["jinja"]["kind"] == "jinja"
    assert normalized["metadata"] == {"producer": "Xarta"}
