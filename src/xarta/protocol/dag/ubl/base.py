from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import xarta.protocol.dag

from xarta.protocol.dag.node import Node
from xarta.protocol.document import source
from xarta.protocol.document.source import DocumentSource


@dataclass(frozen=True)
class UBLAddAttachment:
    id: str
    document: DocumentSource
    filename: str
    description: str | None = None


@dataclass(frozen=True)
class UBLRequest:
    document: DocumentSource
    operations: tuple[UBLAddAttachment, ...]
    output_id: UUID


def _text(value: object, name: str, limit: int) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > limit
        or any(
            not (
                0x20 <= ord(char) <= 0xD7FF
                or 0xE000 <= ord(char) <= 0xFFFD
                or ord(char) >= 0x10000
            )
            for char in value
        )
    ):
        raise ValueError(
            f"UBL {name} must be non-empty text of at most {limit} characters"
        )
    return value


def _reference(value: object) -> DocumentSource:
    if not isinstance(value, Mapping):
        raise ValueError("UBL document must be a document reference")
    return source.parse(**dict(value))


class UBLNode(Node):
    """Apply an ordered, atomic set of semantic edits to one UBL document."""

    KIND = "ubl"

    def __init__(
        self,
        document: Mapping[str, Any],
        operations: list[Mapping[str, Any]],
        out: UUID | str,
        id: UUID | None = None,
        on: dict[str, list[Node]] | None = None,
        parent: Node | None = None,
    ) -> None:
        super().__init__(kind=self.KIND, id=id, on=on, parent=parent)
        if not isinstance(document, Mapping):
            raise ValueError("UBL document must be a document reference")
        self.document = deepcopy(dict(document))
        self.operations = deepcopy(operations)
        self.output_id = UUID(str(out))
        self.interpret()

    def interpret(self) -> UBLRequest:
        document = _reference(self.document)
        if not isinstance(self.operations, list) or not 1 <= len(self.operations) <= 25:
            raise ValueError("UBL requires between 1 and 25 operations")
        operations = []
        seen = set()
        for item in self.operations:
            if not isinstance(item, Mapping) or item.get("action") != "add_attachment":
                raise ValueError("UBL supports only add_attachment operations")
            if set(item) - {"action", "id", "document", "filename", "description"}:
                raise ValueError("UBL attachment contains unsupported fields")
            identifier = _text(item.get("id"), "attachment id", 256)
            if identifier.strip() in seen:
                raise ValueError("UBL attachment IDs must be unique")
            seen.add(identifier.strip())
            filename = _text(item.get("filename"), "attachment filename", 255)
            if "/" in filename or "\\" in filename or filename in {".", ".."}:
                raise ValueError("UBL attachment filename must be a basename")
            description = item.get("description")
            if description is not None:
                description = _text(description, "attachment description", 1024)
            operations.append(
                UBLAddAttachment(
                    identifier, _reference(item.get("document")), filename, description
                )
            )
        source.reject_input_overwrite(
            (document, *(item.document for item in operations)), {self.output_id}
        )
        return UBLRequest(document, tuple(operations), self.output_id)

    def dict(self) -> dict:
        return {
            **super().dict(),
            "document": deepcopy(self.document),
            "operations": deepcopy(self.operations),
            "out": self.output_id,
        }

    @staticmethod
    def fromdict(
        _specification: Mapping[str, Any], document, operations, out, **kwargs
    ) -> UBLNode:
        return UBLNode(
            **xarta.protocol.dag.parse_common_fields(**_specification),
            document=document,
            operations=operations,
            out=out,
        )
