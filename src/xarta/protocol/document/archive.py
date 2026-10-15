from __future__ import annotations

import datetime

from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING
from typing import Any
from uuid import UUID

from xarta import env
from xarta.protocol.document.type import DocumentTypeIdentifier

if TYPE_CHECKING:
    from collections.abc import Mapping


ARCHIVE_SERVICE_ENDPOINT = env.extract(key="ARCHIVE_SERVICE_ENDPOINT", dtype=str)
ARCHIVE_SERVICE_TIMEOUT = env.extract(
    key="ARCHIVE_SERVICE_TIMEOUT", default="30.0", dtype=float
)


@dataclass(frozen=True)
class ArchiveRepresentation:
    representation_id: UUID
    content_type: str
    metadata: dict[str, Any] = field(default_factory=dict)
    name: str | None = None
    checksum: str | None = None
    size: int | None = None
    state: str = "available"

    def dict(self) -> dict[str, Any]:
        return {
            "id": str(self.representation_id),
            "name": self.name,
            "metadata": self.metadata,
            "content": {
                "type": self.content_type,
                "checksum": {"sha512": self.checksum} if self.checksum else None,
                "size_bytes": self.size,
                "availability": self.state,
            },
        }

    @staticmethod
    def fromdict(representation: Mapping[str, Any]) -> ArchiveRepresentation:
        return ArchiveRepresentation(
            representation_id=UUID(representation["id"]),
            name=representation.get("name"),
            content_type=representation["content"]["type"],
            metadata=representation.get("metadata", {}),
            checksum=(representation["content"]["checksum"] or {}).get("sha512"),
            size=representation["content"]["size_bytes"],
            state=representation["content"]["availability"],
        )


@dataclass(frozen=True)
class ArchiveDocumentVersion:
    identifier: UUID
    version_id: UUID
    default_representation_id: UUID
    representations: tuple[ArchiveRepresentation, ...]
    archive: str = "default"
    parent_version_id: UUID | None = None
    document_type: DocumentTypeIdentifier | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    created: datetime.datetime = field(
        default_factory=lambda: datetime.datetime.now(tz=datetime.UTC)
    )
    expires: datetime.datetime | None = None
    state: str = "available"

    @property
    def expired(self) -> bool:
        return bool(
            self.expires and self.expires <= datetime.datetime.now(tz=datetime.UTC)
        )

    @property
    def default_representation(self) -> ArchiveRepresentation:
        for representation in self.representations:
            if representation.representation_id == self.default_representation_id:
                return representation
        raise ValueError("default representation does not belong to version")

    def dict(self) -> dict[str, Any]:
        return {
            "id": str(self.identifier),
            "archive": self.archive,
            "version": {
                "id": str(self.version_id),
                "parent": (
                    str(self.parent_version_id) if self.parent_version_id else None
                ),
            },
            "document_type": self.document_type.value if self.document_type else None,
            "metadata": self.metadata,
            "created_at": self.created.isoformat(),
            "expires_at": self.expires.isoformat() if self.expires else None,
            "content_availability": self.state,
            "representations": {
                "default": str(self.default_representation_id),
                "items": [item.dict() for item in self.representations],
            },
        }

    @staticmethod
    def fromdict(representation: Mapping[str, Any]) -> ArchiveDocumentVersion:
        expires = representation.get("expires_at")
        return ArchiveDocumentVersion(
            identifier=UUID(representation["id"]),
            archive=representation.get("archive", "default"),
            version_id=UUID(representation["version"]["id"]),
            parent_version_id=(
                UUID(representation["version"]["parent"])
                if representation["version"]["parent"]
                else None
            ),
            document_type=(
                DocumentTypeIdentifier(representation["document_type"])
                if representation.get("document_type")
                else None
            ),
            metadata=representation.get("metadata", {}),
            created=datetime.datetime.fromisoformat(representation["created_at"]),
            expires=datetime.datetime.fromisoformat(expires) if expires else None,
            state=representation["content_availability"],
            default_representation_id=UUID(
                representation["representations"]["default"]
            ),
            representations=tuple(
                ArchiveRepresentation.fromdict(item)
                for item in representation["representations"]["items"]
            ),
        )
