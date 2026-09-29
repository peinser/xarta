r"""
Base definitions of the signature DAG node and its subsidiaries.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

import xarta.protocol.dag

from xarta.crypto.sign.policy import validate_policy_id
from xarta.protocol.dag import Node
from xarta.protocol.document import source
from xarta.protocol.document.source import DocumentSource

if TYPE_CHECKING:
    import builtins

    from typing import Final


@dataclass(frozen=True)
class SignatureRequest:
    source: DocumentSource
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
        self.interpret()

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
        if not isinstance(self._documents, list):
            raise ValueError("Signature documents must be an array")
        requests = []
        outputs = set()
        for item in self._documents:
            if not isinstance(item, dict) or set(item) != {"document", "out"}:
                raise ValueError("Signature entry requires document and out")
            reference = item["document"]
            if not isinstance(reference, dict):
                raise ValueError("Signature document must be a document reference")
            document = source.parse(**reference)
            output_id = UUID(str(item["out"]))
            if output_id in outputs:
                raise ValueError("Signature output IDs must be unique")
            outputs.add(output_id)
            requests.append(SignatureRequest(document, output_id, self._policy))
        source.reject_input_overwrite((request.source for request in requests), outputs)
        return requests

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
