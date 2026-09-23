from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import orjson
import pytest

from tests.fixtures.jinja_selftest import jinja_selftest_template_engine_options
from xarta.protocol.document.request.render import DocumentRenderParameters
from xarta.protocol.document.request.render import DocumentRenderPayload
from xarta.protocol.document.request.render import DocumentRenderRequest
from xarta.protocol.document.type import DocumentTypeIdentifier
from xarta.protocol.template.engine import GotenbergRenderOperation
from xarta.protocol.template.engine import GotenbergTemplateEngine
from xarta.protocol.template.engine import JinjaRenderOperation
from xarta.protocol.template.engine import TemplateEngine
from xarta.protocol.template.engine import TemplateEngineIdentifier
from xarta.protocol.template.engine import TemplateEngineKind
from xarta.protocol.template.engine import TemplateEnginesOptions


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
        self.url = None
        self.kwargs = None

    def post(self, url, **kwargs):
        self.url = url
        self.kwargs = kwargs
        return ResponseContext(self.response)


def request(
    parameters: DocumentRenderParameters | None = None,
) -> DocumentRenderRequest:
    return DocumentRenderRequest(
        document_type=DocumentTypeIdentifier("test"),
        payload=DocumentRenderPayload.fromdict(
            {"content_type": "application/json", "data": {"value": 1}}
        ),
        metadata={"source": "test"},
        template_engine=TemplateEngineIdentifier("jinja"),
        template_engines_options=TemplateEnginesOptions(
            jinja_selftest_template_engine_options()
        ),
        parameters=parameters,
        content_type="text/html",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "parameters", [None, DocumentRenderParameters({"title": "Test"})]
)
async def test_jinja_render_operation_sends_contract_request(parameters) -> None:
    downstream = SimpleNamespace(
        status=200,
        headers={"content-type": "text/html"},
        read=AsyncMock(return_value=b"ok"),
    )
    session = Session(downstream)
    engine = TemplateEngine(
        identifier=TemplateEngineIdentifier("jinja"),
        endpoint="http://jinja.example:8000",
        timeout=10,
        type=TemplateEngineKind.JINJA,
    )
    operation = JinjaRenderOperation(request(parameters), engine)

    assert operation._url == "http://jinja.example:8000?output=text/html"
    assert operation._body == {
        "options": jinja_selftest_template_engine_options()["jinja"],
        "payload": {"value": 1},
        "parameters": parameters.dict() if parameters is not None else {},
        "metadata": {"source": "test"},
    }
    await operation.execute(session)
    assert session.url == "http://jinja.example:8000?output=text/html"
    assert session.kwargs["json"] == operation._body
    assert session.kwargs["timeout"] == 10


def gotenberg_request(
    payload_content_type: str = "text/html",
    payload_data: object = "<html><body>Invoice</body></html>",
    content_type: str = "application/pdf",
    metadata: dict | None = None,
    properties: dict | None = None,
    wrapper: str | None = None,
) -> DocumentRenderRequest:
    options = {"gotenberg": {"kind": "gotenberg"}}
    if properties is not None:
        options["gotenberg"]["properties"] = properties
    if wrapper is not None:
        options["gotenberg"]["wrapper"] = wrapper

    return DocumentRenderRequest(
        document_type=DocumentTypeIdentifier("test"),
        payload=DocumentRenderPayload.fromdict(
            {"content_type": payload_content_type, "data": payload_data}
        ),
        metadata=metadata if metadata is not None else {},
        template_engine=TemplateEngineIdentifier("gotenberg"),
        template_engines_options=TemplateEnginesOptions(options),
        content_type=content_type,
    )


def gotenberg_engine() -> GotenbergTemplateEngine:
    return GotenbergTemplateEngine(
        identifier=TemplateEngineIdentifier("gotenberg"),
        endpoint="http://gotenberg.example:3000",
        timeout=30,
    )


def form_entries(form) -> dict[str, object]:
    return {field[0]["name"]: field[2] for field in form._fields}


def test_gotenberg_operation_targets_the_chromium_html_route() -> None:
    operation = gotenberg_engine().prepare(gotenberg_request())

    assert isinstance(operation, GotenbergRenderOperation)
    assert operation._url == "http://gotenberg.example:3000/forms/chromium/convert/html"
    assert [document.filename for document in operation._documents] == ["index.html"]
    assert operation._documents[0].content == b"<html><body>Invoice</body></html>"


def test_gotenberg_form_carries_the_document_properties_and_metadata() -> None:
    request = gotenberg_request(
        metadata={"title": "Invoice", "custom": "value"},
        properties={"paperWidth": "8.27", "printBackground": True, "scale": 0.9},
    )
    operation = GotenbergRenderOperation(request, gotenberg_engine())

    form = operation.form()
    entries = form_entries(form)

    assert entries["files"] == b"<html><body>Invoice</body></html>"
    assert form._fields[0][0]["filename"] == "index.html"
    assert entries["paperWidth"] == "8.27"
    assert entries["printBackground"] == "true"
    assert entries["scale"] == "0.9"
    assert orjson.loads(entries["metadata"]) == {"Title": "Invoice", "custom": "value"}


def test_gotenberg_form_omits_metadata_when_the_request_carries_none() -> None:
    operation = GotenbergRenderOperation(gotenberg_request(), gotenberg_engine())

    assert "metadata" not in form_entries(operation.form())


@pytest.mark.asyncio
async def test_gotenberg_operation_posts_the_multipart_conversion() -> None:
    downstream = SimpleNamespace(
        status=200,
        headers={"Content-Type": "application/pdf"},
        read=AsyncMock(return_value=b"%PDF-1.7"),
    )
    session = Session(downstream)
    request = gotenberg_request()
    operation = GotenbergRenderOperation(request, gotenberg_engine())

    result = await operation.execute(session)

    assert session.url == "http://gotenberg.example:3000/forms/chromium/convert/html"
    assert isinstance(session.kwargs["data"], aiohttp.FormData)
    assert session.kwargs["timeout"] == 30
    assert session.kwargs["headers"] == {"Gotenberg-Trace": str(request.correlation_id)}
    assert result.status == 200
    assert result.body == b"%PDF-1.7"
    assert result.headers["content-type"] == "application/pdf"


@pytest.mark.parametrize(
    ("payload_content_type", "payload_data", "content_type"),
    [
        ("application/json", "<html></html>", "application/pdf"),
        ("text/html", {"value": 1}, "application/pdf"),
        ("text/html", "<html></html>", "text/html"),
        ("text/markdown", "# Invoice", "text/html"),
    ],
)
def test_gotenberg_rejects_unsupported_content(
    payload_content_type, payload_data, content_type
) -> None:
    request = gotenberg_request(
        payload_content_type=payload_content_type,
        payload_data=payload_data,
        content_type=content_type,
    )

    with pytest.raises(ValueError):
        GotenbergRenderOperation(request, gotenberg_engine())


def test_gotenberg_renders_without_request_specific_engine_options() -> None:
    request = DocumentRenderRequest(
        document_type=DocumentTypeIdentifier("test"),
        payload=DocumentRenderPayload.fromdict(
            {"content_type": "text/html", "data": "<html></html>"}
        ),
        template_engine=TemplateEngineIdentifier("gotenberg"),
        content_type="application/pdf",
    )
    operation = GotenbergRenderOperation(request, gotenberg_engine())

    assert list(form_entries(operation.form())) == ["files"]


def test_gotenberg_markdown_wraps_the_payload_for_the_markdown_route() -> None:
    request = gotenberg_request(
        payload_content_type="text/markdown",
        payload_data="# Invoice",
    )
    operation = GotenbergRenderOperation(request, gotenberg_engine())

    assert (
        operation._url
        == "http://gotenberg.example:3000/forms/chromium/convert/markdown"
    )
    assert [document.filename for document in operation._documents] == [
        "index.html",
        "index.md",
    ]

    wrapper, markdown = operation._documents
    assert b'{{ toHTML "index.md" }}' in wrapper.content
    assert wrapper.content_type == "text/html"
    assert markdown.content == b"# Invoice"
    assert markdown.content_type == "text/markdown"

    form = operation.form()
    assert [field[0]["filename"] for field in form._fields] == [
        "index.html",
        "index.md",
    ]
    assert {field[0]["name"] for field in form._fields} == {"files"}


def test_gotenberg_markdown_uses_the_configured_wrapper() -> None:
    wrapper = '<html><head><style>body{color:red}</style></head><body>{{ toHTML "index.md" }}</body></html>'
    request = gotenberg_request(
        payload_content_type="text/markdown",
        payload_data="# Invoice",
        wrapper=wrapper,
    )
    operation = GotenbergRenderOperation(request, gotenberg_engine())

    assert operation._documents[0].content == wrapper.encode()


def test_gotenberg_markdown_rejects_a_wrapper_that_drops_the_markdown() -> None:
    request = gotenberg_request(
        payload_content_type="text/markdown",
        payload_data="# Invoice",
        wrapper="<html><body>No markdown here</body></html>",
    )

    with pytest.raises(ValueError):
        GotenbergRenderOperation(request, gotenberg_engine())
