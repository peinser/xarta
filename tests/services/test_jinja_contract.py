from __future__ import annotations

import os

from typing import TYPE_CHECKING

import aiohttp
import pytest

import xarta.protocol.dag
import xarta.protocol.document.source as source_module

from tests.fixtures.jinja_selftest import SELFTEST_DEFAULT_MARKERS
from tests.fixtures.jinja_selftest import SELFTEST_TEMPLATE_ENGINE
from tests.fixtures.jinja_selftest import jinja_selftest_template_engine_options
from xarta.http.sessions import HTTPRequestManager
from xarta.protocol.document.source import RenderDocumentSource

if TYPE_CHECKING:
    from xarta.protocol.document.source import DocumentSourceResult


pytestmark = pytest.mark.jinja_contract


def contract_enabled() -> None:
    if os.getenv("JINJA_CONTRACT_ENABLED", "false").lower() != "true":
        pytest.skip("Set JINJA_CONTRACT_ENABLED=true to run the Jinja contract tests")


def contract_endpoint(name: str, default: str) -> str:
    return os.getenv(name, default).rstrip("/")


def contract_document_type() -> str:
    return os.getenv("JINJA_CONTRACT_DOCUMENT_TYPE", "test")


@pytest.mark.asyncio
async def test_preview_renders_engine_selftest_as_html() -> None:
    contract_enabled()
    endpoint = contract_endpoint(
        "JINJA_CONTRACT_PREVIEW_ENDPOINT", "http://localhost:8000/api/v1/preview"
    )
    request = {
        "document_type": contract_document_type(),
        "content_type": "text/html",
        "template_engine": SELFTEST_TEMPLATE_ENGINE,
        "template_engine_options": jinja_selftest_template_engine_options(),
        "payload": {"content_type": "application/json", "data": {}},
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(endpoint, json=request) as response:
            body = await response.text()
            assert response.status == 200
            assert response.headers["content-type"].split(";", 1)[0] == "text/html"

    assert body
    for marker in SELFTEST_DEFAULT_MARKERS:
        assert marker in body

    expected_version = os.getenv("JINJA_CONTRACT_ENGINE_VERSION")
    if expected_version:
        assert expected_version in body


@pytest.mark.asyncio
async def test_generate_render_source_returns_selftest_pdf(monkeypatch) -> None:
    contract_enabled()
    endpoint = contract_endpoint(
        "JINJA_CONTRACT_RENDER_ENDPOINT", "http://localhost:8000/api/v1/render"
    )
    source = RenderDocumentSource.parse(
        document_type=contract_document_type(),
        content_type="application/pdf",
        template_engine=SELFTEST_TEMPLATE_ENGINE,
        template_engines_options=jinja_selftest_template_engine_options(),
        payload={"content_type": "application/json", "data": {}},
    )

    async with aiohttp.ClientSession() as session:
        monkeypatch.setattr(source_module, "RENDER_SERVICE_ENDPOINT", endpoint)
        monkeypatch.setattr(HTTPRequestManager, "__session__", session)
        result: DocumentSourceResult = await source.retrieve()

    assert result.content_type.split(";", 1)[0] == "application/pdf"
    assert result.data
    assert result.data.startswith(b"%PDF-")
