from __future__ import annotations

import os

import aiohttp
import pytest

from xarta.protocol.document.request.render import DocumentRenderPayload
from xarta.protocol.document.request.render import DocumentRenderRequest
from xarta.protocol.document.type import DocumentTypeIdentifier
from xarta.protocol.template.engine import GotenbergTemplateEngine
from xarta.protocol.template.engine import TemplateEngineIdentifier
from xarta.protocol.template.engine import TemplateEnginesOptions

pytestmark = pytest.mark.gotenberg_contract


def contract_engine() -> GotenbergTemplateEngine:
    if os.getenv("GOTENBERG_CONTRACT_ENABLED", "false").lower() != "true":
        pytest.skip(
            "Set GOTENBERG_CONTRACT_ENABLED=true to run the Gotenberg contract tests"
        )

    return GotenbergTemplateEngine(
        identifier=TemplateEngineIdentifier("gotenberg"),
        endpoint=os.getenv(
            "GOTENBERG_CONTRACT_ENDPOINT", "http://template-engine-gotenberg:3000"
        ).rstrip("/"),
        timeout=30.0,
    )


HTML = "<html><body><h1>Invoice</h1><p>Amount due: 100 EUR</p></body></html>"
MARKDOWN = "# Invoice\n\nAmount due: **100 EUR**\n"


def contract_request(
    payload_content_type: str,
    data: str,
    properties: dict | None = None,
) -> DocumentRenderRequest:
    return DocumentRenderRequest(
        document_type=DocumentTypeIdentifier("test"),
        payload=DocumentRenderPayload.fromdict(
            {"content_type": payload_content_type, "data": data}
        ),
        metadata={"title": "Contract check", "author": "Xarta"},
        template_engine=TemplateEngineIdentifier("gotenberg"),
        template_engines_options=TemplateEnginesOptions(
            {
                "gotenberg": {
                    "kind": "gotenberg",
                    "properties": properties or {"printBackground": True},
                }
            }
        ),
        content_type="application/pdf",
    )


@pytest.mark.parametrize(
    ("payload_content_type", "data"),
    [
        (
            "text/html",
            "<html><body><h1>Invoice</h1><p>Amount due: 100 EUR</p></body></html>",
        ),
        ("text/markdown", "# Invoice\n\nAmount due: **100 EUR**\n"),
    ],
)
async def test_gotenberg_converts_the_payload_into_a_pdf(
    payload_content_type, data
) -> None:
    engine = contract_engine()
    operation = engine.prepare(contract_request(payload_content_type, data))

    async with aiohttp.ClientSession() as session:
        result = await operation.execute(session)

    assert result.status == 200
    assert result.headers["content-type"] == "application/pdf"
    assert result.body.startswith(b"%PDF-")

    # Proves the lowercase Xarta metadata keys reach Exiftool under the names it
    # writes into the PDF information dictionary.
    assert b"/Title (Contract check)" in result.body
    assert b"/Author (Xarta)" in result.body

    # A wrapper that never includes the markdown yields a valid but empty PDF of
    # roughly 900 bytes. Size is a coarse stand-in for "the document has content";
    # parse the content streams here if this ever needs to assert what was drawn.
    assert len(result.body) > 4000


@pytest.mark.parametrize(
    ("pdfa", "level"),
    [("PDF/A-1b", "1"), ("PDF/A-2b", "2"), ("PDF/A-3b", "3")],
)
async def test_gotenberg_applies_the_requested_pdf_a_format(pdfa, level) -> None:
    r"""
    PDF/A needs no Xarta code: `pdfa` is a Gotenberg form field that the page
    properties pass through. This fails if that pass-through ever stops working.
    """
    engine = contract_engine()
    operation = engine.prepare(contract_request("text/html", HTML, {"pdfa": pdfa}))

    async with aiohttp.ClientSession() as session:
        result = await operation.execute(session)

    assert result.status == 200
    assert result.body.startswith(b"%PDF-")

    # XMP carries the conformance level as an element or as an attribute.
    assert (
        f"pdfaid:part>{level}".encode() in result.body
        or f"pdfaid:part='{level}'".encode() in result.body
    )


async def test_gotenberg_rejects_an_unsupported_pdf_a_format() -> None:
    engine = contract_engine()
    operation = engine.prepare(
        contract_request("text/html", HTML, {"pdfa": "PDF/A-9z"})
    )

    async with aiohttp.ClientSession() as session:
        result = await operation.execute(session)

    assert result.status == 400
    assert b"not supported" in result.body
