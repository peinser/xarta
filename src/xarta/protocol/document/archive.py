from __future__ import annotations

import datetime

from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING
from typing import Any
from uuid import UUID

from xarta.protocol.document.type import DocumentTypeIdentifier

if TYPE_CHECKING:
    from collections.abc import Mapping


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
            "representation_id": str(self.representation_id),
            "name": self.name,
            "content_type": self.content_type,
            "metadata": self.metadata,
            "checksum": {"sha512": self.checksum} if self.checksum else None,
            "size": self.size,
            "state": self.state,
        }

    @staticmethod
    def fromdict(representation: Mapping[str, Any]) -> ArchiveRepresentation:
        return ArchiveRepresentation(
            representation_id=UUID(representation["representation_id"]),
            name=representation.get("name"),
            content_type=representation["content_type"],
            metadata=representation.get("metadata", {}),
            checksum=(representation.get("checksum") or {}).get("sha512"),
            size=representation.get("size"),
            state=representation.get("state", "available"),
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
            "version_id": str(self.version_id),
            "parent_version_id": (
                str(self.parent_version_id) if self.parent_version_id else None
            ),
            "document_type": self.document_type.value if self.document_type else None,
            "metadata": self.metadata,
            "created": self.created.isoformat(),
            "expires": self.expires.isoformat() if self.expires else None,
            "state": self.state,
            "default_representation_id": str(self.default_representation_id),
            "representations": [item.dict() for item in self.representations],
        }

    @staticmethod
    def fromdict(representation: Mapping[str, Any]) -> ArchiveDocumentVersion:
        expires = representation.get("expires")
        return ArchiveDocumentVersion(
            identifier=UUID(representation["id"]),
            archive=representation.get("archive", "default"),
            version_id=UUID(representation["version_id"]),
            parent_version_id=(
                UUID(representation["parent_version_id"])
                if representation.get("parent_version_id")
                else None
            ),
            document_type=(
                DocumentTypeIdentifier(representation["document_type"])
                if representation.get("document_type")
                else None
            ),
            metadata=representation.get("metadata", {}),
            created=datetime.datetime.fromisoformat(representation["created"]),
            expires=datetime.datetime.fromisoformat(expires) if expires else None,
            state=representation.get("state", "available"),
            default_representation_id=UUID(representation["default_representation_id"]),
            representations=tuple(
                ArchiveRepresentation.fromdict(item)
                for item in representation["representations"]
            ),
        )
