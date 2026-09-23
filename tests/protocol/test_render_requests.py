from __future__ import annotations

from tests.fixtures.jinja_selftest import jinja_selftest_template_engine_options
from xarta.protocol.document.request.render import DocumentRenderPayload
from xarta.protocol.document.request.render import DocumentRenderRequest
from xarta.protocol.document.type import DocumentTypeIdentifier
from xarta.protocol.template.engine import TemplateEngineIdentifier
from xarta.protocol.template.engine import TemplateEnginesOptions


def render_request(content_type: str | None = None) -> DocumentRenderRequest:
    return DocumentRenderRequest(
        document_type=DocumentTypeIdentifier("test"),
        payload=DocumentRenderPayload.fromdict(
            {"content_type": "application/json", "data": {}}
        ),
        template_engine=TemplateEngineIdentifier("jinja"),
        template_engines_options=TemplateEnginesOptions(
            jinja_selftest_template_engine_options()
        ),
        content_type=content_type,
    )


def test_render_request_dict_preserves_html_content_type() -> None:
    assert render_request("text/html").dict()["content_type"] == "text/html"


def test_render_request_dict_preserves_pdf_content_type() -> None:
    assert render_request("application/pdf").dict()["content_type"] == "application/pdf"


def test_render_request_dict_omits_unspecified_content_type() -> None:
    assert "content_type" not in render_request().dict()
