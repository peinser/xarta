r"""
Base definitions of the debug DAG node and its subsidiaries.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import xarta.protocol.dag

from xarta.protocol.dag import Node

if TYPE_CHECKING:
    from typing import Final
    from uuid import UUID


class DebugNode(Node):
    KIND: Final[str] = "debug"

    def __init__(
        self,
        id: UUID | None = None,
        on: dict[str, list[Node]] | None = None,
        parent: Node | None = None,
    ):
        super().__init__(
            kind=DebugNode.KIND,
            id=id,
            on=on,
            parent=parent,
        )

    @staticmethod
    def fromdict(
        _specification: dict,
        parent: Node | None = None,
        **kwargs,
    ) -> DebugNode:
        return DebugNode(**xarta.protocol.dag.parse_common_fields(**_specification))
