r"""
A module describing document sources.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time

from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import quote
from uuid import UUID

import orjson

from xarta.concurrency import bounded_map
from xarta.exceptions.protocol import HTTPClientError
from xarta.http.sessions import HTTPRequestManager
from xarta.protocol.dag.archive.constants import ARCHIVE_SERVICE_ENDPOINT
from xarta.protocol.dag.archive.constants import ARCHIVE_SERVICE_TIMEOUT
from xarta.protocol.document.archive import ArchiveDocumentVersion
from xarta.protocol.document.archive import ArchiveRepresentation
from xarta.protocol.document.request.bundle import BUNDLE_SERVICE_ENDPOINT
from xarta.protocol.document.request.bundle import BUNDLE_SERVICE_TIMEOUT
from xarta.protocol.document.request.bundle import DocumentBundleRequest
from xarta.protocol.document.request.render import RENDER_SERVICE_ENDPOINT
from xarta.protocol.document.request.render import RENDER_SERVICE_TIMEOUT
from xarta.protocol.document.request.render import DocumentRenderParameters
from xarta.protocol.document.request.render import DocumentRenderPayload
from xarta.protocol.document.request.render import DocumentRenderRequest
from xarta.protocol.document.type import DocumentTypeIdentifier
from xarta.protocol.template.engine import TemplateEngineIdentifier
from xarta.protocol.template.engine import TemplateEnginesOptions
from xarta.storage import get_temporary_storage

if TYPE_CHECKING:
    from typing import Final

    from aiohttp import ClientSession


def parse(source: str = "generate", **kwargs) -> DocumentSource:
    r"""
    By default, we assume a `generate` document source, which implies
    the identifier refers to a document that has been generated in a
    generate DAG node.
    """
    match source:
        case ArchiveDocumentSource.IDENTIFIER:
            return ArchiveDocumentSource.parse(**kwargs)
        case BundleDocumentSource.IDENTIFIER:
            return BundleDocumentSource.parse(**kwargs)
        case GenerateDocumentSource.IDENTIFIER:
            return GenerateDocumentSource.parse(**kwargs)
        case RenderDocumentSource.IDENTIFIER:
            return RenderDocumentSource.parse(**kwargs)
        case _:
            raise ValueError


@dataclass
class DocumentSource:
    id: UUID
    source: str

    async def retrieve(**kwargs) -> DocumentSourceResult:
        raise NotImplementedError

    @staticmethod
    def parse(**kwargs) -> DocumentSource:
        raise NotImplementedError


@dataclass
class DocumentSourceResult:
    id: UUID
    content_type: str
    data: bytes
    document_type: DocumentTypeIdentifier
    metadata: dict | None = None

    async def persist(self) -> None:
        storage = get_temporary_storage()
        key = _storage_key(self.id)
        objects = (
            (
                f"{key}.json",
                orjson.dumps(
                    {
                        "id": self.id,
                        "content_type": self.content_type,
                        "document_type": self.document_type.value,
                        "metadata": self.metadata,
                    }
                ),
                "application/json",
            ),
            (key, self.data, self.content_type),
        )
        await bounded_map(objects, len(objects), lambda item: storage.put(*item))

    @staticmethod
    def parse(
        id: UUID | str,
        content_type: str,
        document_type: str | None = None,
        data: bytes | None = None,
        metadata: dict | None = None,
    ) -> DocumentSourceResult:
        return DocumentSourceResult(
            id=UUID(str(id)),
            document_type=DocumentTypeIdentifier(value=document_type),
            content_type=content_type,
            metadata=metadata,
            data=data,
        )


class BundleDocumentSource(DocumentSource):
    IDENTIFIER: Final[str] = "bundle"

    def __init__(self, id: UUID, request: dict) -> None:
        super().__init__(id=id, source=BundleDocumentSource.IDENTIFIER)
        self._request = request

    async def retrieve(self, **kwargs) -> DocumentSourceResult:
        http_session = HTTPRequestManager.__session__
        request = DocumentBundleRequest.fromdict(self._request)

        # Submit the bundle request.
        async with http_session.post(
            BUNDLE_SERVICE_ENDPOINT,
            json=request.dict(),
            timeout=BUNDLE_SERVICE_TIMEOUT,
        ) as response:
            if response.status != 202:
                raise HTTPClientError(
                    endpoint=BUNDLE_SERVICE_ENDPOINT, status=response.status
                )
            payload = await response.json()
            bundle_request = DocumentBundleRequest.fromdict(payload, validate=False)

        # Poll the status of the bundle request until it reaches a terminal state.
        t_0 = time.time()
        timeout = False
        while not timeout:
            # Wait for a while before continueing.
            await asyncio.sleep(1.0)
            # TODO Add exponention backoff and heuristic based on files. OR wait on reply subject with NATS.

            # Check if we surpassed the timeout criteria.
            timeout = (time.time() - t_0) >= BUNDLE_SERVICE_TIMEOUT

            # Submit the bundle request.
            async with http_session.get(
                f"{BUNDLE_SERVICE_ENDPOINT}/{bundle_request.id}",
                timeout=BUNDLE_SERVICE_TIMEOUT,
            ) as response:
                if response.status != 200:
                    raise HTTPClientError(
                        endpoint=BUNDLE_SERVICE_ENDPOINT, status=response.status
                    )

                # Check if a status update has been provided.
                if "application/json" in response.headers["Content-Type"]:
                    bundle_request = DocumentBundleRequest.fromdict(
                        await response.read(), validate=False
                    )
                    continue

                return DocumentSourceResult(
                    id=self.id,
                    document_type=DocumentTypeIdentifier(value=None),
                    content_type="application/zip",
                    data=await response.read(),
                )

        raise TimeoutError(
            f"Bundle {bundle_request.id} did not complete within {BUNDLE_SERVICE_TIMEOUT} seconds"
        )

    @staticmethod
    def parse(id: UUID, request: dict, **kwargs) -> DocumentSource:
        return BundleDocumentSource(id=id, request=request)


class ArchiveDocumentSource(DocumentSource):
    IDENTIFIER: Final[str] = "archive"

    def __init__(
        self,
        id: UUID,
        archive: str = "default",
        version: str | None = None,
        representation: str | None = None,
    ):
        super().__init__(id=id, source=ArchiveDocumentSource.IDENTIFIER)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", archive):
            raise ValueError("Archive source requires a valid archive name")
        self.archive = archive
        self.version = str(UUID(version)) if version else None
        if representation and not version:
            raise ValueError("An exact archive representation requires a version")
        self.representation = str(UUID(representation)) if representation else None
        self.resolved_version: str | None = None
        self.resolved_representation: str | None = None

    @staticmethod
    async def _binary(
        http_session: ClientSession,
        id: UUID,
        archive: str,
        version: str | None,
        representation: str | None,
    ) -> bytes:
        path = f"{ARCHIVE_SERVICE_ENDPOINT}/archives/{quote(archive, safe='')}/documents/{id}"
        if version:
            path = f"{path}/versions/{version}"
        if representation:
            path = f"{path}/representations/{representation}"
        async with http_session.get(
            path,
            timeout=ARCHIVE_SERVICE_TIMEOUT,
        ) as response:
            if response.status != 200:
                raise HTTPClientError(
                    endpoint=ARCHIVE_SERVICE_ENDPOINT, status=response.status
                )
            return await response.read()

    @staticmethod
    async def _details(
        http_session: ClientSession,
        id: UUID,
        archive: str,
        version: str | None,
    ) -> ArchiveDocumentVersion:
        path = f"{ARCHIVE_SERVICE_ENDPOINT}/archives/{quote(archive, safe='')}/documents/{id}"
        if version:
            path = f"{path}/versions/{version}"
        async with http_session.get(
            f"{path}/details",
            timeout=ARCHIVE_SERVICE_TIMEOUT,
        ) as response:
            if response.status != 200:
                raise HTTPClientError(
                    endpoint=ARCHIVE_SERVICE_ENDPOINT, status=response.status
                )
            return ArchiveDocumentVersion.fromdict(await response.json())

    @staticmethod
    def _representation(
        details: ArchiveDocumentVersion, representation_id: str | None
    ) -> ArchiveRepresentation:
        identity = (
            UUID(representation_id)
            if representation_id
            else details.default_representation_id
        )
        for representation in details.representations:
            if representation.representation_id == identity:
                return representation
        raise ValueError("Archive details do not contain the requested representation")

    async def _load(self, id: UUID) -> DocumentSourceResult:
        details = await self.details()
        representation = self._representation(details, self.representation)
        self.resolved_representation = str(representation.representation_id)
        http_session = HTTPRequestManager.__session__
        binary = await ArchiveDocumentSource._binary(
            http_session=http_session,
            id=id,
            archive=self.archive,
            version=self.resolved_version,
            representation=self.representation,
        )

        if (
            representation.checksum
            and hashlib.sha512(binary).hexdigest() != representation.checksum
        ):
            raise ValueError(
                "Archived content checksum does not match its version details"
            )

        return DocumentSourceResult(
            id=details.identifier,
            document_type=details.document_type or DocumentTypeIdentifier(value=None),
            content_type=representation.content_type,
            metadata=details.metadata,
            data=binary,
        )

    async def details(self) -> ArchiveDocumentVersion:
        details = await ArchiveDocumentSource._details(
            http_session=HTTPRequestManager.__session__,
            id=self.id,
            archive=self.archive,
            version=self.version,
        )
        self.resolved_version = str(details.version_id)
        return details

    async def retrieve(self, **kwargs) -> DocumentSourceResult:
        return await self._load(self.id)

    @staticmethod
    def parse(
        id: UUID,
        archive: str = "default",
        version: str | None = None,
        representation: str | None = None,
        **kwargs,
    ) -> DocumentSource:
        return ArchiveDocumentSource(
            id=id,
            archive=archive,
            version=version,
            representation=representation,
        )


class GenerateDocumentSource(DocumentSource):
    IDENTIFIER: Final[str] = "generate"

    def __init__(self, id: UUID):
        super().__init__(id=id, source=GenerateDocumentSource.IDENTIFIER)

    @staticmethod
    async def _load(id: UUID) -> DocumentSourceResult:
        storage = get_temporary_storage()
        key = _storage_key(id)
        binary, metadata = await asyncio.gather(
            storage.get(key),
            storage.get(f"{key}.json"),
        )

        metadata = orjson.loads(metadata)
        metadata["data"] = binary

        return DocumentSourceResult.parse(**metadata)

    async def retrieve(self, **kwargs) -> DocumentSourceResult:
        return await GenerateDocumentSource._load(self.id)

    @staticmethod
    def parse(id: UUID, **kwargs) -> DocumentSource:
        return GenerateDocumentSource(id=id)


def _storage_key(id: UUID) -> str:
    identifier = str(id)
    return "/".join(
        (*[identifier[index : index + 2] for index in range(0, 8, 2)], identifier)
    )


class RenderDocumentSource(DocumentSource):
    IDENTIFIER: Final[str] = "render"

    def __init__(
        self,
        document_type: DocumentTypeIdentifier,
        payload: DocumentRenderPayload,
        metadata: dict | None = None,
        id: UUID | None = None,
        template_engine: TemplateEngineIdentifier | None = None,
        template_engines_options: TemplateEnginesOptions | None = None,
        parameters: DocumentRenderParameters | None = None,
        content_type: str | None = None,
    ):
        super().__init__(
            source=RenderDocumentSource.IDENTIFIER,
            id=id,
        )

        self._document_type = document_type
        self._payload = payload
        self._template_engine = template_engine
        self._template_engines_options = template_engines_options
        self._parameters = parameters
        self._content_type = content_type
        self._metadata = metadata

    @property
    def document_type(self) -> DocumentTypeIdentifier:
        return self._document_type

    @property
    def metadata(self) -> dict:
        return self._metadata

    @property
    def payload(self) -> DocumentRenderPayload:
        return self._payload

    @property
    def template_engine(self) -> TemplateEngineIdentifier | None:
        return self._template_engine

    @property
    def template_engines_options(self) -> TemplateEnginesOptions | None:
        return self._template_engines_options

    @property
    def parameters(self) -> DocumentRenderParameters | None:
        return self._parameters

    @property
    def content_type(self) -> str:
        return self._content_type

    async def retrieve(self, **kwargs) -> DocumentSourceResult:
        render_request = DocumentRenderRequest(
            id=self.id,
            document_type=self.document_type,
            payload=self._payload,
            template_engine=self._template_engine,
            template_engines_options=self._template_engines_options,
            parameters=self._parameters,
            content_type=self._content_type,
            metadata=self._metadata,
        )

        # TODO Add correlation-id to headers.
        http_session = HTTPRequestManager.__session__

        async with http_session.post(
            RENDER_SERVICE_ENDPOINT,
            json=render_request.dict(),
            timeout=RENDER_SERVICE_TIMEOUT,
        ) as r:
            if r.status == 200:
                content_type = r.headers.get("content-type", None)
                if not content_type:
                    raise HTTPClientError(
                        endpoint=RENDER_SERVICE_ENDPOINT, status=r.status
                    )
            else:
                raise HTTPClientError(endpoint=RENDER_SERVICE_ENDPOINT, status=r.status)

            return DocumentSourceResult(
                id=self.id,
                document_type=self.document_type,
                content_type=content_type,
                data=await r.read(),
            )

    @staticmethod
    def parse(
        document_type: str,
        payload: dict,
        id: UUID | None = None,
        template_engine: str | None = None,
        metadata: dict | None = None,
        template_engines_options: dict | None = None,
        parameters: dict | None = None,
        content_type: str | None = None,
        **kwargs,
    ) -> DocumentSource:
        return RenderDocumentSource(
            id=id,
            document_type=DocumentTypeIdentifier(document_type),
            payload=DocumentRenderPayload.fromdict(payload),
            template_engine=(
                TemplateEngineIdentifier(template_engine) if template_engine else None
            ),
            template_engines_options=TemplateEnginesOptions.fromdict(
                template_engines_options or {}
            ),
            parameters=(
                DocumentRenderParameters.fromdict(parameters) if parameters else None
            ),
            content_type=content_type,
            metadata=metadata,
        )
