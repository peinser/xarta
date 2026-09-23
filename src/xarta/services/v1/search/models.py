from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Any
from uuid import UUID

if TYPE_CHECKING:
    from collections.abc import Mapping


@dataclass(frozen=True)
class ArchiveSearchSource:
    archive: str
    document_id: UUID
    version_id: UUID


@dataclass(frozen=True)
class DocumentSearchSource:
    kind: str
    document_id: UUID
    version: str


SearchSource = ArchiveSearchSource | DocumentSearchSource


@dataclass(frozen=True)
class SearchableRepresentation:
    representation_id: UUID
    name: str | None
    content_type: str
    metadata: Mapping[str, Any]


@dataclass(frozen=True)
class SearchableDocument:
    """Backend-neutral representation submitted to a search index."""

    source: SearchSource
    projection_revision: str
    digest: str | None
    content_type: str | None
    document_type: str | None
    metadata: Mapping[str, Any]
    default_representation_id: UUID | None = None
    representations: tuple[SearchableRepresentation, ...] = ()


@dataclass(frozen=True)
class IndexSubmission:
    outcome: str
    provider_reference: str | None
    details: Mapping[str, Any] | None = None
