r"""
Base definitions of the generate DAG node and its subsidiaries.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import xarta.protocol.dag

from xarta.protocol.dag import Node
from xarta.protocol.document import source
from xarta.protocol.document.source import DocumentSource

if TYPE_CHECKING:
    from typing import Final
    from uuid import UUID


class GenerateNode(Node):
    KIND: Final[str] = "generate"
    OUTCOMES = frozenset({"success", "failure"})

    def __init__(
        self,
        documents: list[dict],
        id: UUID | None = None,
        on: dict[str, list[Node]] | None = None,
        parent: Node | None = None,
    ):
        super().__init__(
            kind=GenerateNode.KIND,
            id=id,
            on=on,
            parent=parent,
        )

        self._documents = documents

    @property
    def documents(self) -> list[dict] | list[DocumentSource]:
        return self._documents

    def interpret(self) -> list[DocumentSource]:
        return [source.parse(**document) for document in self._documents]

    def dict(self) -> dict:
        specification = super().dict()
        specification["documents"] = self._documents

        return specification

    @staticmethod
    def fromdict(
        _specification: dict,
        documents: list[dict] | None = None,
        parent: Node | None = None,
        **kwargs,
    ) -> GenerateNode:
        return GenerateNode(
            **xarta.protocol.dag.parse_common_fields(**_specification),
            documents=documents or [],
        )
