r"""
Base definitions of a node which waits for the presence of archived documents.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

import xarta.protocol.dag

from xarta.protocol.dag import Node
from xarta.protocol.document.source import ArchiveDocumentSource

if TYPE_CHECKING:
    from typing import Final


class WaitForNode(Node):
    KIND: Final[str] = "wait-for"
    OUTCOMES = frozenset({"success", "failure"})

    def __init__(
        self,
        id: UUID | None = None,
        on: dict[str, list[Node]] | None = None,
        parent: Node | None = None,
        backoffs: list[float] | None = None,
        documents: list[dict] | None = None,
    ):
        super().__init__(
            kind=WaitForNode.KIND,
            id=id,
            on=on,
            parent=parent,
        )

        self._backoffs = backoffs  # In seconds
        self._documents = list(documents or [])

    @property
    def documents(self) -> list[dict]:
        return self._documents

    @property
    def backoffs(self) -> list[float]:
        return self._backoffs

    def interpret(self) -> list[ArchiveDocumentSource]:
        return [ArchiveDocumentSource(**document) for document in self._documents]

    def dict(self) -> dict:
        specification = super().dict()
        specification["documents"] = self._documents
        specification["backoffs"] = self._backoffs

        return specification

    @staticmethod
    def fromdict(
        _specification: dict,
        parent: Node | None = None,
        documents: list[dict] | None = None,
        backoffs: list[float] | None = None,
        **kwargs,
    ) -> WaitForNode:
        return WaitForNode(
            **xarta.protocol.dag.parse_common_fields(**_specification),
            documents=documents or [],
            backoffs=backoffs,
        )
