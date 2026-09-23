r"""
Base objects necessary for describing a document.
"""

from __future__ import annotations

import datetime

from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING

from xarta.protocol.document.type import DocumentTypeIdentifier

if TYPE_CHECKING:
    from uuid import UUID


@dataclass
class Document:
    identifier: UUID
    content_type: str
    document_type: DocumentTypeIdentifier | None = None
    metadata: dict = field(default_factory=dict)
    created: datetime.datetime = field(
        default_factory=lambda: datetime.datetime.now(tz=datetime.UTC)
    )
    expires: datetime.datetime | None = None
