from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import xarta.protocol.dag
import xarta.protocol.document.source as source_module

from tests.fixtures.jinja_selftest import SELFTEST_TEMPLATE_ENGINE
from tests.fixtures.jinja_selftest import jinja_selftest_template_engine_options
from xarta.protocol.document.source import DocumentSourceResult
from xarta.protocol.document.source import RenderDocumentSource


class ResponseContext:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, *_):
        return None


class Session:
    def __init__(self, response):
        self.response = response
        self.kwargs = None

    def post(self, _url, **kwargs):
        self.kwargs = kwargs
        return ResponseContext(self.response)


async def test_render_source_translates_options_and_preserves_pdf_type(
    monkeypatch,
) -> None:
    response = SimpleNamespace(
        status=200,
        headers={"content-type": "application/pdf"},
        read=AsyncMock(return_value=b"%PDF-test"),
    )
    session = Session(response)
    monkeypatch.setattr(source_module.HTTPRequestManager, "__session__", session)
    identifier = uuid4()
    source = RenderDocumentSource.parse(
        document_type="test",
        id=identifier,
        content_type="application/pdf",
        template_engine=SELFTEST_TEMPLATE_ENGINE,
        template_engines_options=jinja_selftest_template_engine_options(),
        payload={"content_type": "application/json", "data": {}},
    )

    result = await source.retrieve()

    assert isinstance(result, DocumentSourceResult)
    assert result.id == identifier
    assert result.content_type == "application/pdf"
    assert result.data == b"%PDF-test"
    assert (
        session.kwargs["json"]["template_engine_options"]
        == jinja_selftest_template_engine_options()
    )
    assert session.kwargs["json"]["content_type"] == "application/pdf"
