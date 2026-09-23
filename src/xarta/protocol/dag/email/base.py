r"""
Base definitions of the email DAG node and its subsidiaries.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

import xarta.protocol.dag

from xarta.protocol.dag import Node
from xarta.protocol.document import source
from xarta.protocol.document.source import GenerateDocumentSource

if TYPE_CHECKING:
    from typing import Final
    from uuid import UUID

    from xarta.protocol.document.source import DocumentSource


@dataclass
class EmailAttachment(GenerateDocumentSource):
    filename: str | None = None

    @staticmethod
    def parse(id: UUID, filename: str | None) -> EmailAttachment:
        return EmailAttachment(
            source=GenerateDocumentSource.IDENTIFIER,
            id=id,
            filename=filename,
        )


class EmailOutcome(StrEnum):
    ACCEPTED = "accepted"
    DELIVERED = "delivered"
    MAILBOX_FULL = "mailbox_full"
    RECIPIENT_UNKNOWN = "recipient_unknown"
    MESSAGE_REJECTED = "message_rejected"
    DELIVERY_DELAYED = "delivery_delayed"
    COMPLAINED = "complained"
    ALL_DELIVERED = "all_delivered"
    ALL_ACCEPTED = "all_accepted"
    PARTIALLY_ACCEPTED = "partially_accepted"
    PARTIALLY_DELIVERED = "partially_delivered"
    ALL_FAILED = "all_failed"


class EmailNode(Node):
    KIND: Final[str] = "email"
    OUTCOMES = frozenset(outcome.value for outcome in EmailOutcome)

    def __init__(
        self,
        to: str,
        sender: str,
        body: dict,
        destination: str | None = None,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        attachments: list[dict] | None = None,
        reply_to: str | None = None,
        subject: str | None = None,
        id: UUID | None = None,
        on: dict[str, list[Node]] | None = None,
        parent: Node | None = None,
        **kwargs,
    ):
        super().__init__(
            kind=EmailNode.KIND,
            id=id,
            on=on,
            parent=parent,
        )

        self._to = to
        self._sender = sender
        self._subject = subject
        self._body = body
        self._cc = list(cc or [])
        self._bcc = list(bcc or [])
        self._attachments = list(attachments or [])
        self._reply_to = reply_to
        self.destination = destination

    def interpret(self) -> None:
        # Process the email body.
        self._plain = self._body.get("plain", None)
        self._html = self._body.get("html", None)
        if self._html and isinstance(self._html, dict):
            self._html = source.parse(**self._html)

        # Parse the EmailAttachment document sources.
        self._attachments = [
            (
                attachment
                if isinstance(attachment, EmailAttachment)
                else EmailAttachment.parse(**attachment)
            )
            for attachment in self._attachments
        ]

    @property
    def attachments(self) -> list[dict] | list[EmailAttachment]:
        return self._attachments

    @property
    def cc(self) -> list[str]:
        return self._cc

    @property
    def bcc(self) -> list[str]:
        return self._bcc

    @property
    def reply_to(self) -> str:
        return self._reply_to

    @property
    def to(self) -> str:
        return self._to

    @property
    def sender(self) -> str:
        return self._sender

    @property
    def subject(self) -> str | None:
        return self._subject

    @property
    def body(self) -> dict:
        return self._body

    @property
    def plain(self) -> str | None:
        return self._plain

    @property
    def html(self) -> dict | DocumentSource | None:
        return self._html

    def dict(self) -> dict:
        specification = super().dict()
        specification["to"] = self._to
        specification["sender"] = self._sender
        specification["subject"] = self._subject
        specification["body"] = self._body
        specification["cc"] = self._cc
        specification["bcc"] = self._bcc
        specification["reply_to"] = self._reply_to
        specification["attachments"] = self._attachments
        specification["destination"] = self.destination

        return specification

    @staticmethod
    def fromdict(
        _specification: dict,
        to: str | None = None,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        attachments: list[dict] | None = None,
        reply_to: str | None = None,
        sender: str | None = None,
        subject: str | None = None,
        body: dict | None = None,
        destination: str | None = None,
        **kwargs,
    ) -> EmailNode:
        return EmailNode(
            **xarta.protocol.dag.parse_common_fields(**_specification),
            to=to,
            cc=cc,
            bcc=bcc,
            sender=sender,
            subject=subject,
            body=body,
            attachments=attachments,
            reply_to=reply_to,
            destination=destination,
        )
