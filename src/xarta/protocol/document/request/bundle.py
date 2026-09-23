r"""
Definitions and global settings related to document bundle requests.
"""

from __future__ import annotations

import uuid
import zipfile

from dataclasses import dataclass
from dataclasses import field
from enum import StrEnum
from typing import TYPE_CHECKING
from uuid import UUID

from xarta import cache
from xarta import env

if TYPE_CHECKING:
    from typing import Final


BUNDLE_SERVICE_ENDPOINT: Final[str] = env.extract(
    key="BUNDLE_SERVICE_ENDPOINT",
    dtype=str,
)


BUNDLE_SERVICE_TIMEOUT: Final[float] = env.extract(
    key="BUNDLE_SERVICE_TIMEOUT",
    default="30.0",
    dtype=float,
)


BUNDLE_TIMEOUT: Final[float] = env.extract(
    key="BUNDLE_TIMEOUT",
    default="30.0",
    dtype=float,
)


BUNDLE_STORAGE: Final[str] = env.extract(
    key="BUNDLE_STORAGE",
    default="storage/bundle",
    dtype=str,
)


CACHE_BUNDLE_NAMESPACE: Final[str] = "bundle"


class DocumentBundleRequestCompressionMethod(StrEnum):
    r"""
    It should be noted Windows basically only supports "deflated". Yes, really. So practicioners beware.
    """

    STORED: str = "stored"
    DEFLATED: str = "deflated"
    BZIP2: str = "bzip2"
    LZMA: str = "lzma"

    @staticmethod
    def interpret(method: DocumentBundleRequestCompressionMethod) -> int:
        match method:
            case DocumentBundleRequestCompressionMethod.STORED:
                return zipfile.ZIP_STORED
            case DocumentBundleRequestCompressionMethod.DEFLATED:
                return zipfile.ZIP_DEFLATED
            case DocumentBundleRequestCompressionMethod.BZIP2:
                return zipfile.ZIP_BZIP2
            case DocumentBundleRequestCompressionMethod.LZMA:
                return zipfile.ZIP_LZMA
            case _:
                return zipfile.ZIP_DEFLATED


@dataclass
class DocumentBundleRequestCompressionOptions:
    method: DocumentBundleRequestCompressionMethod = (
        DocumentBundleRequestCompressionMethod.DEFLATED
    )
    level: int = 9

    def dict(self) -> dict:
        return {
            "method": self.method,
            "level": self.level,
        }

    @staticmethod
    def fromdict(payload: dict) -> DocumentBundleRequestCompressionOptions:
        try:
            options = DocumentBundleRequestCompressionOptions(
                method=DocumentBundleRequestCompressionMethod(
                    payload.get("method", "deflated")
                ),
                level=int(payload.get("level", 9)),
            )
        except (TypeError, ValueError) as ex:
            raise ValueError("Invalid bundle compression options.") from ex
        if options.level < 0 or options.level > 9:
            raise ValueError("Bundle compression level must be between 0 and 9.")
        return options


class DocumentBundleRequestState(StrEnum):
    QUEUED: str = "queued"
    PROCESSING: str = "processing"
    COMPLETED: str = "completed"
    FAILED: str = "failed"


@dataclass
class DocumentBundleRequestOptions:
    compression: DocumentBundleRequestCompressionOptions
    filename: str | None = None

    def dict(self) -> dict:
        return {
            "compression": self.compression.dict(),
            "filename": self.filename,
        }

    @staticmethod
    def fromdict(payload: dict) -> DocumentBundleRequestOptions:
        return DocumentBundleRequestOptions(
            filename=payload.get("filename"),
            compression=DocumentBundleRequestCompressionOptions.fromdict(
                payload.get("compression", {})
            ),
        )


@dataclass
class DocumentBundleRequestStatus:
    state: DocumentBundleRequestState = DocumentBundleRequestState.QUEUED
    details: dict = field(default_factory=dict)

    def dict(self) -> dict:
        return {
            "state": self.state,
            "details": self.details,
        }

    @staticmethod
    def fromdict(payload: dict) -> DocumentBundleRequestStatus:
        return DocumentBundleRequestStatus(
            state=DocumentBundleRequestState(
                payload.get("state", DocumentBundleRequestState.QUEUED)
            ),
            details=payload.get("details", {}),
        )


@dataclass(frozen=True)
class BundleDocument:
    id: UUID
    filename: str | None = None

    def dict(self) -> dict:
        return {
            "id": self.id,
            "filename": self.filename,
        }


@dataclass
class DocumentBundleRequest:
    id: UUID
    documents: set[BundleDocument]
    status: DocumentBundleRequestStatus
    options: DocumentBundleRequestOptions

    async def _update(self) -> None:
        r"""Updates the document bundle request with the centralized cache."""
        await cache.manager.set(
            self.id, self.dict(), ttl=BUNDLE_TIMEOUT, namespace=CACHE_BUNDLE_NAMESPACE
        )

    async def start(self) -> None:
        self.status.state = DocumentBundleRequestState.PROCESSING
        await self._update()

    async def complete(self) -> None:
        self.status.state = DocumentBundleRequestState.COMPLETED
        await self._update()

    async def fail(self, missing: list | None = None) -> None:
        self.status.state = DocumentBundleRequestState.FAILED

        # Check if the failure mode is due to missing files.
        if missing:
            self.status.details["missing"] = missing

        await self._update()

    def dict(self) -> dict:
        return {
            "id": str(self.id),
            "documents": [document.dict() for document in self.documents],
            "status": self.status.dict(),
            "options": self.options.dict(),
        }

    @staticmethod
    def fromdict(payload, validate: bool = True) -> DocumentBundleRequest:
        # Extract the job id from the provided payload, generate one if none exists.
        job_id = UUID(payload.get("id")) if "id" in payload else uuid.uuid4()

        # Extract the documetns from the provided payload.
        documents = {
            BundleDocument(**document) for document in payload.get("documents", [])
        }
        for document in documents:
            if document.filename is not None:
                validate_bundle_filename(document.filename)

        status = DocumentBundleRequestStatus.fromdict(payload.get("status", {}))
        options = DocumentBundleRequestOptions.fromdict(payload.get("options", {}))

        if not documents:
            raise ValueError("A bundle must contain at least one document.")
        if len(documents) > 25:
            raise ValueError("A bundle cannot contain more than 25 documents.")
        if options.filename:
            validate_bundle_filename(options.filename)

        if validate:
            # Verify whether the provided filenames are unique, throws an exception otherwise.
            _verify_unique_filenames(documents)

        return DocumentBundleRequest(
            id=job_id, documents=documents, status=status, options=options
        )


def _verify_unique_filenames(documents: set[BundleDocument]) -> None:
    non_empty_filenames = (
        doc.filename for doc in documents if doc.filename is not None
    )

    seen = set()
    for filename in non_empty_filenames:
        if filename in seen:
            raise ValueError(f"Bundle document filename is not unique: {filename}")
        seen.add(filename)


def validate_bundle_filename(filename: str) -> None:
    if (
        not filename
        or filename in {".", ".."}
        or "/" in filename
        or "\\" in filename
        or any(ord(character) < 32 for character in filename)
    ):
        raise ValueError("Bundle filenames must be plain file names without paths.")
