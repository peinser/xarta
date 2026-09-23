from __future__ import annotations

import hashlib

from dataclasses import dataclass
from io import BytesIO
from typing import TYPE_CHECKING
from uuid import UUID
from uuid import uuid5

from pyhanko.pdf_utils.reader import PdfFileReader

if TYPE_CHECKING:
    from xarta.protocol.dag.postal import PostalDocumentReference
    from xarta.storage import TemporaryStorage


POSTAL_ARTIFACT_NAMESPACE = UUID("ea20230b-bd5f-5d55-a5e1-5a5ad64fa367")


@dataclass(frozen=True, slots=True)
class StoredDocument:
    id: UUID
    ordinal: int
    role: str
    source: str
    source_id: str
    source_version: str | None
    storage_reference: str
    sha256: str
    size: int
    page_count: int
    content: bytes


@dataclass(frozen=True, slots=True)
class PostalArtifactStore:
    storage: TemporaryStorage
    max_document_bytes: int = 50_000_000
    max_total_bytes: int = 200_000_000

    def __post_init__(self) -> None:
        if (
            self.max_document_bytes < 1
            or self.max_total_bytes < self.max_document_bytes
        ):
            raise ValueError("Postal document byte limits are invalid")

    async def snapshot_documents(
        self, operation_id: UUID, documents: tuple[PostalDocumentReference, ...]
    ) -> tuple[StoredDocument, ...]:
        snapshots: list[StoredDocument] = []
        total = 0
        for ordinal, document in enumerate(documents, 1):
            result = await document.source.retrieve()
            content_type = result.content_type.split(";", 1)[0].strip().lower()
            data = bytes(result.data)
            if content_type != "application/pdf" or not data.startswith(b"%PDF-"):
                raise ValueError("Postal source documents must contain PDF content")
            total += len(data)
            if len(data) > self.max_document_bytes or total > self.max_total_bytes:
                raise ValueError(
                    "Postal source documents exceed the configured byte limit"
                )
            try:
                reader = PdfFileReader(BytesIO(data), strict=True)
                page_count = int(reader.root["/Pages"]["/Count"])
            except Exception as ex:
                raise ValueError("Postal source document is not a valid PDF") from ex
            if page_count < 1:
                raise ValueError(
                    "Postal source document must contain at least one page"
                )
            digest = hashlib.sha256(data).hexdigest()
            artifact_id = uuid5(operation_id, f"source:{ordinal}:{digest}")
            key = f"postal-local/{operation_id}/source/{ordinal:03d}-{digest}.pdf"
            await self.storage.put(key, data, "application/pdf")
            source_version = getattr(document.source, "resolved_version", None)
            snapshots.append(
                StoredDocument(
                    artifact_id,
                    ordinal,
                    document.role or f"document-{ordinal}",
                    document.source.source,
                    str(document.source.id),
                    source_version,
                    key,
                    digest,
                    len(data),
                    page_count,
                    data,
                )
            )
        return tuple(snapshots)

    async def get(self, storage_reference: str) -> bytes:
        return await self.storage.get(storage_reference)
