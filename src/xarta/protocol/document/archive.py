from __future__ import annotations

import datetime
import re

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

_ARCHIVE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def validate_archive_name(value: object) -> str:
    """Validate an archive namespace without coercing or normalizing its identity."""
    if not isinstance(value, str) or not _ARCHIVE_NAME.fullmatch(value):
        raise ValueError(
            "archive must start with an ASCII letter or digit and contain only letters, digits, dots, underscores, or hyphens"
        )
    return value


@dataclass(frozen=True)
class ArchiveRepresentation:
    representation_id: UUID
    content_type: str
    checksum: str  # SHA-512 hex digest of the original bytes.
    size: int
    metadata: dict[str, Any] = field(default_factory=dict)
    name: str | None = None
    state: str = "available"

    def dict(self) -> dict[str, Any]:
        return {
            "id": str(self.representation_id),
            "name": self.name,
            "metadata": self.metadata,
            "content": {
                "type": self.content_type,
                "checksum": {"sha512": self.checksum},
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
            checksum=representation["content"]["checksum"]["sha512"],
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
            "availability": self.state,
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
            state=representation["availability"],
            default_representation_id=UUID(
                representation["representations"]["default"]
            ),
            representations=tuple(
                ArchiveRepresentation.fromdict(item)
                for item in representation["representations"]["items"]
            ),
        )
