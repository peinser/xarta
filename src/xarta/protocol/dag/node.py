r"""
Base definitions representing a Directed-Acyclic Graph.
"""

from __future__ import annotations

import uuid

from dataclasses import dataclass
from dataclasses import field
from typing import Any
from typing import ClassVar
from uuid import UUID


@dataclass
class Node:
    OUTCOMES: ClassVar[frozenset[str]] = frozenset({"success", "failure"})
    kind: str
    id: UUID | None = field(default_factory=uuid.uuid4)
    on: dict[str, list[Node]] | None = None
    parent: Node | None = None

    def __post_init__(self) -> None:
        if self.id is None:
            self.id = uuid.uuid4()
        if self.on is None:
            return

        from xarta.protocol.dag.outcome import COMMON_OUTCOMES

        invalid_outcomes = set(self.on) - self.OUTCOMES - COMMON_OUTCOMES
        if invalid_outcomes:
            invalid = ", ".join(sorted(invalid_outcomes))
            raise ValueError(f"Invalid outcome for {self.kind}: {invalid}")

    def terminal(self) -> bool:
        return not any((self.on or {}).values())

    def dict(self) -> dict:
        on = (
            {
                outcome: [node.dict() for node in successors]
                for outcome, successors in self.on.items()
            }
            if self.on is not None
            else None
        )

        return {
            "id": self.id,
            "kind": self.kind,
            "on": on,
            "parent": self.parent.id if self.parent else None,
        }

    def interpret(self) -> Any:
        r"""
        The purpose of this method is to only interpret the contents of the node
        whenever they are necessary. It prevents wasting CPU cycles whenever an
        interpretation of the node contents are not necessary
        """
        raise NotImplementedError
