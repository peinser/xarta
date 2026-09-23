r"""
Base definitions of the signature DAG node and its subsidiaries.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import xarta.protocol.dag

from xarta.crypto.sign.policy import validate_policy_id
from xarta.protocol.dag import Node
from xarta.protocol.document.source import GenerateDocumentSource

if TYPE_CHECKING:
    import builtins

    from typing import Final
    from uuid import UUID


@dataclass(frozen=True)
class SignatureRequest:
    source: GenerateDocumentSource
    output_id: UUID
    policy: str | None


class SignatureNode(Node):
    KIND: Final[str] = "signature"
    OUTCOMES = frozenset({"success", "failure"})

    def __init__(
        self,
        documents: list[builtins.dict],
        policy: str | None = None,
        id: UUID | None = None,
        on: builtins.dict[str, list[Node]] | None = None,
        parent: Node | None = None,
        **kwargs,
    ):
        super().__init__(
            kind=SignatureNode.KIND,
            id=id,
            on=on,
            parent=parent,
        )

        self._documents = documents
        self.policy = policy

    @property
    def documents(self) -> list[builtins.dict]:
        return self._documents

    @property
    def policy(self) -> str | None:
        return self._policy

    @policy.setter
    def policy(self, value: str | None) -> None:
        self._policy = validate_policy_id(value) if value is not None else None

    def interpret(self) -> list[SignatureRequest]:
        return [
            SignatureRequest(
                source=GenerateDocumentSource(id=document["in"]),
                output_id=document["out"],
                policy=self._policy,
            )
            for document in self._documents
        ]

    def dict(self) -> dict:
        specification = super().dict()
        specification["documents"] = self._documents
        if self._policy is not None:
            specification["policy"] = self._policy

        return specification

    @staticmethod
    def fromdict(
        _specification: builtins.dict,
        documents: list[builtins.dict],
        policy: str | None = None,
        **kwargs,
    ) -> SignatureNode:
        return SignatureNode(
            **xarta.protocol.dag.parse_common_fields(**_specification),
            documents=documents,
            policy=policy,
        )
