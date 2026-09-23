from __future__ import annotations

import datetime

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING
from typing import Any

import xarta.protocol.dag

from xarta.protocol.dag.node import Node

if TYPE_CHECKING:
    from uuid import UUID

    from xarta.protocol.document.source import DocumentSource


class DoccleOutcome(StrEnum):
    STORED = "stored"
    RECEIVER_NOT_FOUND = "receiver_not_found"
    RECEIVER_NOT_PROVISIONED = "receiver_not_provisioned"
    DOCUMENT_REJECTED = "document_rejected"
    UNSUPPORTED_DOCUMENT = "unsupported_document"


@dataclass(frozen=True)
class DoccleReceiverSelector:
    id: str | None = None
    subject: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if (self.id is None) == (self.subject is None):
            raise ValueError("Doccle receiver requires exactly one of id or subject")
        if self.id is not None and (not isinstance(self.id, str) or not self.id):
            raise ValueError("Doccle receiver id must be a non-empty string")
        if self.subject is not None:
            if not isinstance(self.subject, Mapping):
                raise TypeError("Doccle receiver subject must be an object")
            if not self.subject:
                raise ValueError("Doccle receiver subject must not be empty")

    @property
    def mode(self) -> str:
        return "explicit" if self.id is not None else "subject"

    def dict(self) -> dict[str, Any]:
        if self.id is not None:
            return {"id": self.id}
        return {"subject": dict(self.subject or {})}

    @staticmethod
    def fromdict(data: Mapping[str, Any]) -> DoccleReceiverSelector:
        if not isinstance(data, Mapping):
            raise TypeError("Doccle receiver must be an object")
        unknown = set(data) - {"id", "subject"}
        if unknown:
            raise ValueError("Doccle receiver contains unsupported fields")
        return DoccleReceiverSelector(id=data.get("id"), subject=data.get("subject"))


class DoccleNode(Node):
    KIND = "doccle"
    OUTCOMES = frozenset(outcome.value for outcome in DoccleOutcome)

    def __init__(
        self,
        receiver: DoccleReceiverSelector | Mapping[str, Any],
        document: Mapping[str, Any],
        document_type: str,
        name: Mapping[str, str] | None = None,
        published_at: datetime.datetime | str | None = None,
        id: UUID | None = None,
        on: dict[str, list[Node]] | None = None,
        parent: Node | None = None,
        **kwargs,
    ) -> None:
        if not isinstance(document, Mapping):
            raise TypeError("Doccle document must be an object")
        if not isinstance(document_type, str) or not document_type:
            raise ValueError("Doccle document_type must be a non-empty string")
        if name is not None and (
            not isinstance(name, Mapping)
            or not name
            or any(
                not isinstance(language, str)
                or not language
                or not isinstance(value, str)
                or not value
                for language, value in name.items()
            )
        ):
            raise ValueError("Doccle name must map languages to non-empty strings")
        if isinstance(published_at, str):
            try:
                published_at = datetime.datetime.fromisoformat(published_at)
            except ValueError as error:
                raise ValueError("Doccle published_at must be ISO 8601") from error
        if published_at is not None and (
            not isinstance(published_at, datetime.datetime)
            or published_at.tzinfo is None
            or published_at.utcoffset() is None
        ):
            raise ValueError("Doccle published_at must include a timezone")

        super().__init__(kind=self.KIND, id=id, on=on, parent=parent)
        self.receiver = (
            receiver
            if isinstance(receiver, DoccleReceiverSelector)
            else DoccleReceiverSelector.fromdict(receiver)
        )
        self.document = dict(document)
        self.document_type = document_type
        self.name = dict(name) if name is not None else None
        self.published_at = published_at

    def interpret(self) -> DocumentSource:
        import xarta.protocol.document.source

        return xarta.protocol.document.source.parse(**self.document)

    def dict(self) -> dict:
        specification = super().dict()
        specification.update(
            {
                "receiver": self.receiver.dict(),
                "document": self.document,
                "document_type": self.document_type,
            }
        )
        if self.name is not None:
            specification["name"] = self.name
        if self.published_at is not None:
            specification["published_at"] = self.published_at.isoformat()
        return specification

    @staticmethod
    def fromdict(
        _specification: Mapping[str, Any],
        receiver: Mapping[str, Any],
        document: Mapping[str, Any],
        document_type: str,
        name: Mapping[str, str] | None = None,
        published_at: str | None = None,
        **kwargs,
    ) -> DoccleNode:
        return DoccleNode(
            **xarta.protocol.dag.parse_common_fields(**dict(_specification)),
            receiver=receiver,
            document=document,
            document_type=document_type,
            name=name,
            published_at=published_at,
        )
