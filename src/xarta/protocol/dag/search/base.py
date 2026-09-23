from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import TYPE_CHECKING
from typing import Any

import xarta.protocol.dag

from xarta.protocol.dag import Node

if TYPE_CHECKING:
    from uuid import UUID

    from xarta.protocol.document.source import DocumentSource


class SearchIndexOutcome(StrEnum):
    INDEXED = "indexed"
    UNCHANGED = "unchanged"
    UNSUPPORTED_CONTENT = "unsupported_content"
    INDEX_REJECTED = "index_rejected"


class SearchIndexNode(Node):
    """Declaratively indexes exactly one immutable document source version."""

    KIND = "search-index"
    OUTCOMES = frozenset(outcome.value for outcome in SearchIndexOutcome)

    def __init__(
        self,
        document: Mapping[str, Any],
        destination: str,
        id: UUID | None = None,
        on: dict[str, list[Node]] | None = None,
        parent: Node | None = None,
        **kwargs,
    ) -> None:
        if not isinstance(document, Mapping):
            raise TypeError("Search index document must be an object")
        specification = dict(document)
        for name in ("source", "id"):
            if not isinstance(specification.get(name), str) or not specification[name]:
                raise ValueError(f"Search index document requires a non-empty {name}")
        version = specification.get("version")
        if version is not None and (not isinstance(version, str) or not version):
            raise ValueError("Search index document version must be a non-empty string")
        if not isinstance(destination, str) or not destination:
            raise ValueError("Search index destination must be a non-empty string")
        super().__init__(kind=self.KIND, id=id, on=on, parent=parent)
        self.document = specification
        self.destination = destination

    @property
    def source_version(self) -> str:
        return str(self.document.get("version", "latest"))

    def interpret(self) -> DocumentSource:
        from xarta.protocol.document import source

        return source.parse(**self.document)

    def dict(self) -> dict:
        specification = super().dict()
        specification.update(
            {"document": self.document, "destination": self.destination}
        )
        return specification

    @staticmethod
    def fromdict(
        _specification: Mapping[str, Any],
        document: Mapping[str, Any],
        destination: str,
        **kwargs,
    ) -> SearchIndexNode:
        return SearchIndexNode(
            **xarta.protocol.dag.parse_common_fields(**dict(_specification)),
            document=document,
            destination=destination,
        )
