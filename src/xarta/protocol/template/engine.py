r"""
Base definitions surrounding template engines and global utilities.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

import aiohttp
import xmltodict

from sanic import response

from xarta import json
from xarta.exceptions.protocol import UnsupportedPayloadContentType

if TYPE_CHECKING:
    from typing import Final

    from aiohttp import ClientSession
    from sanic import HTTPResponse

    from xarta.protocol.document.request.render import DocumentRenderParameters
    from xarta.protocol.document.request.render import DocumentRenderPayload
    from xarta.protocol.document.request.render import DocumentRenderRequest
    from xarta.protocol.document.type import DocumentType


GOTENBERG_HTML_ROUTE: Final[str] = "/forms/chromium/convert/html"
GOTENBERG_MARKDOWN_ROUTE: Final[str] = "/forms/chromium/convert/markdown"
GOTENBERG_INDEX_FILENAME: Final[str] = "index.html"
GOTENBERG_MARKDOWN_FILENAME: Final[str] = "index.md"
GOTENBERG_HTML_CONTENT_TYPE: Final[str] = "text/html"
GOTENBERG_MARKDOWN_CONTENT_TYPE: Final[str] = "text/markdown"
GOTENBERG_OUTPUT_CONTENT_TYPE: Final[str] = "application/pdf"

# The markdown route converts an HTML document that includes the markdown
# through Gotenberg's `toHTML` template function. A caller that provides no
# wrapper of its own therefore gets an unstyled document holding the markdown.
GOTENBERG_MARKDOWN_WRAPPER: Final[str] = (
    '<!DOCTYPE html><html><head><meta charset="utf-8"></head>'
    f'<body>{{{{ toHTML "{GOTENBERG_MARKDOWN_FILENAME}" }}}}</body></html>'
)

# Document metadata is expressed in the lowercase keys documented by the render
# request schema, while Gotenberg writes PDF metadata through Exiftool and
# expects its own capitalized names.
GOTENBERG_METADATA_KEYS: Final[dict[str, str]] = {
    "title": "Title",
    "author": "Author",
    "subject": "Subject",
    "keywords": "Keywords",
    "creator": "Creator",
    "producer": "Producer",
    "creationDate": "CreateDate",
    "modDate": "ModDate",
}


class TemplateEngineOptions:

    def __init__(
        self, options: dict, template_engine: TemplateEngineIdentifier | None = None
    ):
        self._options = dict(options)
        self._template_engine = template_engine
        self._template_engine_kind = TemplateEngineKind(self._options["kind"])

    @property
    def template_engine(self) -> TemplateEngineIdentifier:
        return self._template_engine

    @property
    def template_engine_kind(self) -> TemplateEngineKind:
        return self._template_engine_kind

    @property
    def template_path(self) -> str:
        return self._options["template"]["path"]


class TemplateEnginesOptions:

    def __init__(self, options: dict):
        self._source = dict(options or {})

    @property
    def engines(self) -> list[TemplateEngineIdentifier]:
        return [TemplateEngineIdentifier(value) for value in self._source]

    @property
    def engine_options(self) -> list[TemplateEngineOptions]:
        return [
            TemplateEngineOptions(
                options=options,
                template_engine=TemplateEngineIdentifier(identifier),
            )
            for identifier, options in self._source.items()
        ]

    def enrich(self, document_type: DocumentType) -> None:
        r"""
        Enriches the document rendering options from the provided
        document type.
        """
        defaults = document_type.default_template_engine_options or {}
        self._source = defaults | self._source

    def dict(self) -> dict:
        return dict(self._source)

    @staticmethod
    def fromdict(data: dict) -> TemplateEnginesOptions:
        return TemplateEnginesOptions(options=data)


class TemplateEngineKind(StrEnum):
    SCRIPTURA: str = "scriptura"
    JINJA: str = "jinja"
    GOTENBERG: str = "gotenberg"


@dataclass
class TemplateEngineIdentifier:
    value: str

    def __hash__(self):
        return hash(self.value)

    def __eq__(self, other: TemplateEngineIdentifier):
        return self.value == other.value


class RenderOperation:

    def __init__(self, template_engine: TemplateEngine):
        self._template_engine = template_engine

    @property
    def template_engine(self) -> TemplateEngine:
        return self._template_engine

    async def execute(self, session: ClientSession, **kwargs) -> HTTPResponse:
        raise NotImplementedError


@dataclass
class TemplateEngine:
    identifier: TemplateEngineIdentifier
    endpoint: str
    timeout: float
    type: TemplateEngineKind

    def prepare(self, request: DocumentRenderRequest) -> RenderOperation:
        raise NotImplementedError

    def __hash__(self):
        return hash(self.identifier)

    def __eq__(self, other: TemplateEngine):
        return self.identifier == other.identifier


class ScripturaRenderOperation(RenderOperation):

    def __init__(self, request: DocumentRenderRequest, template_engine: TemplateEngine):
        super().__init__(template_engine=template_engine)

        self._url = f"{self.template_engine.endpoint}/web/generate?OutputFormat={request.content_type}"
        template_location = request.template_engine_options.dict()[
            template_engine.identifier.value
        ]["template"]["path"]

        # Generate the Scriptura Payload & Parameters.
        payload = ScripturaRenderOperation.encode_payload(request.payload)
        metadata = ScripturaRenderOperation.encode_metadata(request.metadata)
        parameters = (
            ScripturaRenderOperation.encode_parameters(request.parameters)
            if request.parameters
            else b""
        )

        # Render the payload XML
        self._body = f"""<?xml version="1.0" encoding="UTF-8"?>
<GenerationRequest version="1.0">
    <TemplateLocation>{template_location}</TemplateLocation>
    <Payload>{payload}</Payload>
    <Parameters>{parameters}</Parameters>
    <MetaData>{metadata}</MetaData>
</GenerationRequest>"""

    async def execute(self, session: ClientSession, **kwargs) -> HTTPResponse:
        engine = self.template_engine

        async with session.get(self._url, timeout=engine.timeout, data=self._body) as r:
            # TODO Improve permance with chunked encoding?
            content_type = r.headers["Content-Type"]
            body = await r.read()

            # TODO Add Scriptura specific error handling.

            return response.raw(
                status=r.status,
                headers={"content-type": content_type},
                body=body,
            )

    @staticmethod
    def encode_parameters(payload: DocumentRenderParameters) -> bytes:
        return xmltodict.unparse(payload.dict(), full_document=False)

    @staticmethod
    def encode_metadata(metadata: dict) -> bytes:
        return xmltodict.unparse(metadata, full_document=False) if metadata else b""

    @staticmethod
    def encode_payload(payload: DocumentRenderPayload) -> bytes:
        match payload.content_type:
            case "application/json+xml":
                return xmltodict.unparse(payload.data, full_document=False)
            case "application/xml":
                return payload.data
            case "text/xml":
                return payload.data
            case _:
                raise UnsupportedPayloadContentType


@dataclass
class ScripturaTemplateEngine(TemplateEngine):
    type: Final[TemplateEngineKind] = TemplateEngineKind.SCRIPTURA

    def prepare(self, request: DocumentRenderRequest) -> RenderOperation:
        return ScripturaRenderOperation(request=request, template_engine=self)


class JinjaRenderOperation(RenderOperation):

    def __init__(
        self,
        request: DocumentRenderRequest,
        template_engine: TemplateEngine,
    ):
        super().__init__(template_engine=template_engine)

        self._body = {
            "options": request.template_engine_options._source[
                request.template_engine.value
            ],
            "payload": request.payload.data,
            "parameters": (
                request.parameters.dict() if request.parameters is not None else {}
            ),
            "metadata": request.metadata,
        }

        self._url = f"{self.template_engine.endpoint}?output={request.content_type}"

    async def execute(self, session: ClientSession, **kwargs) -> HTTPResponse:
        engine = self.template_engine

        async with session.post(
            self._url, timeout=engine.timeout, json=self._body
        ) as r:
            return response.raw(
                status=r.status,
                headers=r.headers,
                body=await r.read(),
            )


@dataclass
class JinjaTemplateEngine(TemplateEngine):
    type: Final[TemplateEngineKind] = TemplateEngineKind.JINJA

    def prepare(self, request: DocumentRenderRequest) -> RenderOperation:
        return JinjaRenderOperation(template_engine=self, request=request)


@dataclass(frozen=True)
class GotenbergDocument:
    r"""
    One file of a Gotenberg conversion. Gotenberg identifies the role of a file
    by its name, not by the multipart field name.
    """

    filename: str
    content: bytes
    content_type: str


class GotenbergRenderOperation(RenderOperation):
    r"""
    Converts a caller-provided HTML or markdown document into a PDF through the
    Chromium routes of a Gotenberg instance.

    Gotenberg does not template. The render payload therefore carries the final
    document, and the template engine options carry the Chromium page properties
    that Gotenberg accepts as multipart form fields.
    """

    def __init__(self, request: DocumentRenderRequest, template_engine: TemplateEngine):
        super().__init__(template_engine=template_engine)

        if request.content_type != GOTENBERG_OUTPUT_CONTENT_TYPE:
            raise ValueError(
                f"Gotenberg only renders {GOTENBERG_OUTPUT_CONTENT_TYPE}, "
                f"received {request.content_type}"
            )

        if not isinstance(request.payload.data, str):
            raise ValueError("Gotenberg requires the payload data to be a document")

        # Unlike the templating engines, Gotenberg has no template to locate,
        # which makes the per-request engine options optional.
        options = request.template_engine_options.dict().get(
            template_engine.identifier.value, {}
        )

        route, self._documents = GotenbergRenderOperation.compose(
            payload=request.payload,
            wrapper=options.get("wrapper"),
        )

        self._url = f"{template_engine.endpoint}{route}"
        self._properties = {
            name: GotenbergRenderOperation.encode_property(value)
            for name, value in options.get("properties", {}).items()
        }
        self._metadata = {
            GOTENBERG_METADATA_KEYS.get(key, key): value
            for key, value in request.metadata.items()
        }
        self._headers = {"Gotenberg-Trace": str(request.correlation_id)}

    async def execute(self, session: ClientSession, **kwargs) -> HTTPResponse:
        engine = self.template_engine

        async with session.post(
            self._url,
            timeout=engine.timeout,
            data=self.form(),
            headers=self._headers,
        ) as r:
            return response.raw(
                status=r.status,
                headers={"content-type": r.headers["Content-Type"]},
                body=await r.read(),
            )

    def form(self) -> aiohttp.FormData:
        r"""
        Builds the multipart body for one conversion. Every document is sent
        under the `files` field, which is how Gotenberg expects them.
        """
        form = aiohttp.FormData()

        for document in self._documents:
            form.add_field(
                "files",
                document.content,
                filename=document.filename,
                content_type=document.content_type,
            )

        for name, value in self._properties.items():
            form.add_field(name, value)

        if self._metadata:
            form.add_field("metadata", json.dumps(self._metadata).decode())

        return form

    @staticmethod
    def compose(
        payload: DocumentRenderPayload,
        wrapper: str | None,
    ) -> tuple[str, list[GotenbergDocument]]:
        r"""
        Selects the Chromium route for the payload and builds the documents that
        route expects. The markdown route additionally needs an `index.html`
        wrapper, which is what actually includes the markdown.
        """
        if payload.content_type == GOTENBERG_HTML_CONTENT_TYPE:
            return GOTENBERG_HTML_ROUTE, [
                GotenbergDocument(
                    filename=GOTENBERG_INDEX_FILENAME,
                    content=payload.data.encode(),
                    content_type=GOTENBERG_HTML_CONTENT_TYPE,
                )
            ]

        if payload.content_type != GOTENBERG_MARKDOWN_CONTENT_TYPE:
            raise ValueError(
                f"Gotenberg requires a {GOTENBERG_HTML_CONTENT_TYPE} or "
                f"{GOTENBERG_MARKDOWN_CONTENT_TYPE} payload, "
                f"received {payload.content_type}"
            )

        wrapper = wrapper if wrapper is not None else GOTENBERG_MARKDOWN_WRAPPER

        if GOTENBERG_MARKDOWN_FILENAME not in wrapper:
            raise ValueError(
                "The Gotenberg wrapper must include the markdown through "
                f'{{{{ toHTML "{GOTENBERG_MARKDOWN_FILENAME}" }}}}'
            )

        return GOTENBERG_MARKDOWN_ROUTE, [
            GotenbergDocument(
                filename=GOTENBERG_INDEX_FILENAME,
                content=wrapper.encode(),
                content_type=GOTENBERG_HTML_CONTENT_TYPE,
            ),
            GotenbergDocument(
                filename=GOTENBERG_MARKDOWN_FILENAME,
                content=payload.data.encode(),
                content_type=GOTENBERG_MARKDOWN_CONTENT_TYPE,
            ),
        ]

    @staticmethod
    def encode_property(value: object) -> str:
        r"""
        Gotenberg reads page properties as multipart form fields, so JSON
        booleans have to be sent in the lowercase form Go parses.
        """
        if isinstance(value, bool):
            return "true" if value else "false"

        return str(value)


@dataclass
class GotenbergTemplateEngine(TemplateEngine):
    type: Final[TemplateEngineKind] = TemplateEngineKind.GOTENBERG

    def prepare(self, request: DocumentRenderRequest) -> RenderOperation:
        return GotenbergRenderOperation(template_engine=self, request=request)
