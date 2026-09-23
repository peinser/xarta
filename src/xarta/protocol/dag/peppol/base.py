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


class PeppolOutcome(StrEnum):
    SUBMITTED = "submitted"
    INVALID_DOCUMENT = "invalid_document"
    UNSUPPORTED_PROFILE = "unsupported_profile"
    RECIPIENT_NOT_REGISTERED = "recipient_not_registered"
    DELIVERY_CONFIRMED = "delivery_confirmed"
    DELIVERY_FAILED = "delivery_failed"


class PeppolNode(Node):
    """Submit one existing UBL document through the deployed Peppol service."""

    KIND = "peppol"
    OUTCOMES = frozenset(outcome.value for outcome in PeppolOutcome)

    def __init__(
        self,
        document: Mapping[str, Any],
        id: UUID | None = None,
        on: dict[str, list[Node]] | None = None,
        parent: Node | None = None,
        **kwargs,
    ) -> None:
        if not isinstance(document, Mapping):
            raise TypeError("Peppol document must be an object")
        specification = dict(document)
        for name in ("source", "id"):
            if not isinstance(specification.get(name), str) or not specification[name]:
                raise ValueError(f"Peppol document requires a non-empty {name}")
        version = specification.get("version")
        if version is not None and (not isinstance(version, str) or not version):
            raise ValueError("Peppol document version must be a non-empty string")
        super().__init__(kind=self.KIND, id=id, on=on, parent=parent)
        self.document = specification

    def interpret(self) -> DocumentSource:
        from xarta.protocol.document import source

        return source.parse(**self.document)

    def dict(self) -> dict:
        specification = super().dict()
        specification.update({"document": self.document})
        return specification

    @staticmethod
    def fromdict(
        _specification: Mapping[str, Any],
        document: Mapping[str, Any],
        **kwargs,
    ) -> PeppolNode:
        if "destination" in _specification:
            raise ValueError(
                "Peppol destination is server configuration, not DAG intent"
            )
        return PeppolNode(
            **xarta.protocol.dag.parse_common_fields(**dict(_specification)),
            document=document,
        )
