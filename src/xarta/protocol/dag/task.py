from __future__ import annotations

import datetime
import uuid

from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field
from typing import Any
from uuid import UUID

from xarta.protocol.dag.node import Node
from xarta.protocol.dag.outcome import OutcomeSubject


@dataclass(frozen=True)
class TriggerContext:
    outcome_event_id: UUID
    outcome: str
    originating_node_execution_id: UUID
    subject: OutcomeSubject | None = None

    def dict(self) -> dict:
        return {
            "outcome_event_id": self.outcome_event_id,
            "outcome": self.outcome,
            "originating_node_execution_id": self.originating_node_execution_id,
            "subject": self.subject.dict() if self.subject else None,
        }

    @staticmethod
    def fromdict(data: Mapping[str, Any]) -> TriggerContext:
        return TriggerContext(
            outcome_event_id=UUID(str(data["outcome_event_id"])),
            outcome=data["outcome"],
            originating_node_execution_id=UUID(
                str(data["originating_node_execution_id"])
            ),
            subject=(
                OutcomeSubject.fromdict(data["subject"])
                if data.get("subject")
                else None
            ),
        )


@dataclass(frozen=True)
class NodeTask:
    flow_id: UUID
    node: Node
    node_execution_id: UUID = field(default_factory=uuid.uuid4)
    trigger: TriggerContext | None = None
    correlation_id: UUID | None = None
    admission: Mapping[str, str] | None = None
    created_at: datetime.datetime = field(
        default_factory=lambda: datetime.datetime.fromtimestamp(0, datetime.UTC)
    )

    def __post_init__(self) -> None:
        if self.correlation_id is None:
            object.__setattr__(self, "correlation_id", self.flow_id)

    def dict(self) -> dict:
        return {
            "flow_id": self.flow_id,
            "node_execution_id": self.node_execution_id,
            "node": self.node.dict(),
            "trigger": self.trigger.dict() if self.trigger else None,
            "correlation_id": self.correlation_id,
            "admission": dict(self.admission) if self.admission else None,
            "created_at": self.created_at,
        }

    @staticmethod
    def fromdict(data: Mapping[str, Any]) -> NodeTask:
        from xarta.protocol.dag import parse

        return NodeTask(
            flow_id=UUID(str(data["flow_id"])),
            node_execution_id=UUID(str(data["node_execution_id"])),
            node=parse(data["node"]),
            trigger=(
                TriggerContext.fromdict(data["trigger"])
                if data.get("trigger")
                else None
            ),
            correlation_id=UUID(str(data.get("correlation_id", data["flow_id"]))),
            admission=(dict(data["admission"]) if data.get("admission") else None),
            created_at=(
                data["created_at"]
                if isinstance(data.get("created_at"), datetime.datetime)
                else (
                    datetime.datetime.fromisoformat(data["created_at"])
                    if data.get("created_at")
                    else datetime.datetime.fromtimestamp(0, datetime.UTC)
                )
            ),
        )
