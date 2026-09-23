r"""
Necessary objects describing a document render / preview request.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from jsonschema import validate

from xarta import env
from xarta.protocol.document.type import DocumentType
from xarta.protocol.document.type import DocumentTypeIdentifier
from xarta.protocol.template.engine import TemplateEngineIdentifier
from xarta.protocol.template.engine import TemplateEnginesOptions

from .base import DocumentRequest
from .schema import SCHEMA_DOCUMENT_RENDER_REQUEST

if TYPE_CHECKING:
    from typing import Final
    from uuid import UUID


RENDER_SERVICE_ENDPOINT: Final[str] = env.extract(
    key="RENDER_SERVICE_ENDPOINT",
    dtype=str,
)


RENDER_SERVICE_TIMEOUT: Final[float] = env.extract(
    key="RENDER_SERVICE_TIMEOUT",
    default="30.0",
    dtype=float,
)


class DocumentRenderParameters:

    def __init__(self, data: dict):
        self._source = data

    def dict(self) -> dict:
        return self._source

    @staticmethod
    def fromdict(data: dict) -> DocumentRenderParameters:
        return DocumentRenderParameters(data)


class DocumentRenderPayload:

    def __init__(self, data: dict):
        self._source = data

    @property
    def content_type(self) -> str:
        return self._source["content_type"]

    @property
    def data(self) -> dict | str:
        return self._source["data"]

    def dict(self) -> dict:
        return self._source

    @staticmethod
    def fromdict(data: dict) -> DocumentRenderPayload:
        return DocumentRenderPayload(data)


class DocumentRenderRequest(DocumentRequest):

    def __init__(
        self,
        document_type: DocumentTypeIdentifier,
        payload: DocumentRenderPayload,
        metadata: dict | None = None,
        template_engine: TemplateEngineIdentifier | None = None,
        template_engines_options: TemplateEnginesOptions | None = None,
        parameters: DocumentRenderParameters | None = None,
        id: UUID | None = None,
        correlation_id: UUID | None = None,
        content_type: str | None = None,
        **kwargs,
    ):
        super().__init__(id=id, correlation_id=correlation_id, **kwargs)

        self._document_type = document_type
        self._payload = payload
        self._parameters = parameters
        self._template_engine_options = (
            template_engines_options or TemplateEnginesOptions({})
        )
        self._template_engine = template_engine
        self._content_type = content_type
        self._metadata = dict(metadata or {})

    @property
    def content_type(self) -> str:
        return self._content_type

    @property
    def metadata(self) -> dict:
        return self._metadata

    @property
    def template_engine(self) -> TemplateEngineIdentifier:
        return self._template_engine

    @property
    def document_type(self) -> DocumentTypeIdentifier:
        return self._document_type

    @property
    def payload(self) -> DocumentRenderPayload:
        return self._payload

    @property
    def parameters(self) -> DocumentRenderParameters:
        return self._parameters

    @property
    def template_engine_options(self) -> TemplateEnginesOptions:
        return self._template_engine_options

    def enrich(self, document_type: DocumentType) -> None:
        r"""
        Enriches the document rendering options from the provided
        document type.
        """
        if not self._template_engine or not self._template_engine.value:
            self._template_engine = document_type.default_template_engine

        self._template_engine_options.enrich(document_type=document_type)

        if not self.content_type:
            self._content_type = document_type.default_content_type

        if self._metadata:
            self._metadata = document_type.default_metadata | self._metadata
        else:
            self._metadata = document_type.default_metadata

    def dict(self) -> dict:
        data = {
            "document_type": self._document_type.value,
            "template_engine_options": self._template_engine_options.dict(),
            "template_engine": (
                self._template_engine.value if self._template_engine else None
            ),
            "parameters": self._parameters.dict() if self._parameters else None,
            "metadata": self._metadata,
            "payload": self._payload.dict(),
        }
        if self._content_type is not None:
            data["content_type"] = self._content_type
        return data

    @staticmethod
    def validate(data: dict) -> None:
        validate(instance=data, schema=SCHEMA_DOCUMENT_RENDER_REQUEST)

    @staticmethod
    def fromdict(
        data: dict,
        validate: bool = True,
        correlation_id: UUID | None = None,
        id: UUID | None = None,
    ) -> DocumentRenderRequest:
        if validate:
            DocumentRenderRequest.validate(data)

        return DocumentRenderRequest(
            document_type=DocumentTypeIdentifier(data["document_type"]),
            payload=DocumentRenderPayload.fromdict(data["payload"]),
            parameters=(
                DocumentRenderParameters.fromdict(data["parameters"])
                if data.get("parameters")
                else None
            ),
            template_engines_options=TemplateEnginesOptions.fromdict(
                data.get("template_engine_options", {})
            ),
            template_engine=(
                TemplateEngineIdentifier(data["template_engine"])
                if data.get("template_engine")
                else None
            ),
            metadata=data.get("metadata", {}),
            content_type=data.get("content_type"),
            correlation_id=correlation_id,
            id=id,
        )
