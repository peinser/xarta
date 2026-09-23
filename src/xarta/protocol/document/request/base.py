r"""
Defines the base properties of a document request.
"""

from __future__ import annotations

import uuid

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from uuid import UUID


class DocumentRequest:

    def __init__(
        self,
        id: UUID | None = None,
        correlation_id: UUID | None = None,
    ):
        self._id = id if id else uuid.uuid4()
        self._correlation_id = correlation_id if correlation_id else uuid.uuid4()

    @property
    def id(self) -> UUID:
        return self._id

    @property
    def correlation_id(self) -> UUID:
        return self._correlation_id

    def dict(self) -> dict:
        raise NotImplementedError

    @staticmethod
    def fromdict(data: dict) -> DocumentRequest:
        raise NotImplementedError
