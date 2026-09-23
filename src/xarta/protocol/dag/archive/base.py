from __future__ import annotations

import datetime
import re

from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING
from typing import Any
from uuid import UUID

import xarta.protocol.dag

from xarta.protocol.dag import Node
from xarta.protocol.document.source import DocumentSource
from xarta.protocol.document.source import parse as parse_source

if TYPE_CHECKING:
    from collections.abc import Mapping
    from typing import Final


@dataclass(frozen=True)
class ArchiveRepresentationSpecification:
    source: DocumentSource
    source_specification: dict[str, Any]
    representation_id: UUID | None = None
    name: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def fromdict(
        specification: Mapping[str, Any], index: int
    ) -> ArchiveRepresentationSpecification:
        source = specification.get("source")
        if not isinstance(source, dict):
            raise ValueError(f"representations[{index}].source must be an object")
        metadata = specification.get("metadata", {})
        if not isinstance(metadata, dict):
            raise ValueError(f"representations[{index}].metadata must be an object")
        name = specification.get("name")
        if name is not None and (not isinstance(name, str) or not name.strip()):
            raise ValueError(f"representations[{index}].name must not be empty")
        representation_id = specification.get("representation_id")
        return ArchiveRepresentationSpecification(
            source=parse_source(**source),
            source_specification=dict(source),
            representation_id=(
                UUID(str(representation_id)) if representation_id else None
            ),
            name=name,
            metadata=metadata,
        )

    def dict(self) -> dict[str, Any]:
        return {
            "representation_id": (
                str(self.representation_id) if self.representation_id else None
            ),
            "name": self.name,
            "metadata": self.metadata,
            "source": self.source_specification,
        }


@dataclass(frozen=True)
class ArchiveVersionSpecification:
    representations: tuple[ArchiveRepresentationSpecification, ...]
    archive: str | None = None
    document_id: UUID | None = None
    version_id: UUID | None = None
    parent_version_id: UUID | None = None
    created: datetime.datetime | None = None
    expires: datetime.datetime | None = None
    document_type: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    default_representation_id: UUID | None = None

    @staticmethod
    def fromdict(specification: Mapping[str, Any]) -> ArchiveVersionSpecification:
        archive = specification.get("archive")
        if archive is not None and (
            not isinstance(archive, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", archive)
        ):
            raise ValueError("Archive snapshot requires a valid archive name")
        metadata = specification.get("metadata", {})
        if not isinstance(metadata, dict):
            raise ValueError("Archive snapshot metadata must be an object")
        values = specification.get("representations")
        if not isinstance(values, list) or not values:
            raise ValueError("Archive snapshot requires at least one representation")
        representations = tuple(
            ArchiveRepresentationSpecification.fromdict(item, index)
            for index, item in enumerate(values)
        )
        default_value = specification.get("default_representation_id")
        default_id = UUID(str(default_value)) if default_value else None
        if len(representations) > 1:
            if default_id is None:
                raise ValueError(
                    "Multiple representations require default_representation_id"
                )
            if any(item.representation_id is None for item in representations):
                raise ValueError(
                    "Multiple representations require explicit representation_id values"
                )
        identities = {item.representation_id for item in representations}
        if default_id is not None and default_id not in identities:
            raise ValueError("default representation does not belong to version")
        created = _timestamp(specification.get("created"), "created")
        expires = _timestamp(specification.get("expires"), "expires")
        if created and expires and expires <= created:
            raise ValueError("expires must be later than created")
        document_type = specification.get("document_type")
        if document_type is not None and (
            not isinstance(document_type, str) or not document_type
        ):
            raise ValueError("document_type must be a nonempty string")
        return ArchiveVersionSpecification(
            archive=archive,
            document_id=_optional_uuid(specification.get("document_id")),
            version_id=_optional_uuid(specification.get("version_id")),
            parent_version_id=_optional_uuid(specification.get("parent_version_id")),
            created=created,
            expires=expires,
            document_type=document_type,
            metadata=metadata,
            default_representation_id=default_id,
            representations=representations,
        )

    def dict(self) -> dict[str, Any]:
        return {
            "archive": self.archive,
            "document_id": str(self.document_id) if self.document_id else None,
            "version_id": str(self.version_id) if self.version_id else None,
            "parent_version_id": (
                str(self.parent_version_id) if self.parent_version_id else None
            ),
            "created": self.created.isoformat() if self.created else None,
            "expires": self.expires.isoformat() if self.expires else None,
            "document_type": self.document_type,
            "metadata": self.metadata,
            "default_representation_id": (
                str(self.default_representation_id)
                if self.default_representation_id
                else None
            ),
            "representations": [item.dict() for item in self.representations],
        }


def _optional_uuid(value: Any) -> UUID | None:
    return UUID(str(value)) if value else None


def _timestamp(value: Any, field_name: str) -> datetime.datetime | None:
    if value is None:
        return None
    result = datetime.datetime.fromisoformat(str(value))
    if result.tzinfo is None:
        raise ValueError(f"{field_name} must include a timezone")
    return result


class ArchiveNode(Node):
    KIND: Final[str] = "archive"
    OUTCOMES = frozenset(
        {
            "success",
            "failure",
            "created",
            "version_created",
            "unchanged",
            "overwrite_rejected",
            "version_conflict",
        }
    )

    def __init__(
        self,
        id: UUID | None = None,
        on: dict[str, list[Node]] | None = None,
        parent: Node | None = None,
        documents: list[Mapping[str, Any]] | None = None,
        destination: str | None = None,
    ):
        super().__init__(kind=ArchiveNode.KIND, id=id, on=on, parent=parent)
        self._documents = tuple(
            ArchiveVersionSpecification.fromdict(document)
            for document in documents or []
        )
        self.destination = destination

    @property
    def documents(self) -> tuple[ArchiveVersionSpecification, ...]:
        return self._documents

    def interpret(self) -> tuple[ArchiveVersionSpecification, ...]:
        return self._documents

    def dict(self) -> dict[str, Any]:
        specification = super().dict()
        specification["documents"] = [item.dict() for item in self._documents]
        specification["destination"] = self.destination
        return specification

    @staticmethod
    def fromdict(
        _specification: Mapping[str, Any],
        parent: Node | None = None,
        documents: list[Mapping[str, Any]] | None = None,
        destination: str | None = None,
        **kwargs,
    ) -> ArchiveNode:
        return ArchiveNode(
            **xarta.protocol.dag.parse_common_fields(**_specification),
            documents=documents,
            destination=destination,
        )
