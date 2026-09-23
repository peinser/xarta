r"""
Utilities and specification of a document flow request.
"""

from __future__ import annotations

import uuid

from typing import TYPE_CHECKING

import xarta.protocol.dag

from xarta.protocol.document.request.base import DocumentRequest

if TYPE_CHECKING:
    from uuid import UUID

    from xarta.protocol.dag import Node


class DocumentFlowRequest(DocumentRequest):

    def __init__(
        self,
        dag: Node,
        id: UUID | None = None,
        correlation_id: UUID | None = None,
    ):
        super().__init__(id=id, correlation_id=correlation_id)
        self._dag = dag

    @property
    def dag(self) -> Node:
        return self._dag

    def dict(self) -> dict:
        return {
            "id": str(self.id),
            "correlation_id": str(self.correlation_id),
            "dag": self.dag.dict(),
        }

    @staticmethod
    def fromdict(data: dict, **kwargs) -> DocumentFlowRequest:
        return DocumentFlowRequest(
            id=uuid.UUID(data["id"]) if data.get("id") else None,
            correlation_id=(
                uuid.UUID(data["correlation_id"])
                if data.get("correlation_id")
                else None
            ),
            dag=xarta.protocol.dag.parse(data["dag"]),
        )
