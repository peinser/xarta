from __future__ import annotations

import datetime

from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field
from enum import StrEnum
from typing import Any
from typing import Protocol
from uuid import UUID

from xarta.protocol.dag import Node
from xarta.protocol.dag import NodeTask
from xarta.protocol.dag import OutcomeEmission
from xarta.protocol.dag import OutcomeEvent


class NodeExecutionState(StrEnum):
    SCHEDULED = "scheduled"
    EXECUTING = "executing"
    WAITING_FEEDBACK = "waiting_feedback"
    RESOLVED = "resolved"
    FAILED = "failed"


class AttemptDisposition(StrEnum):
    EXECUTE = "execute"
    ACTIVE_ATTEMPT = "active_attempt"
    WAITING_FEEDBACK = "waiting_feedback"
    DUPLICATE_TERMINAL = "duplicate_terminal"


class TrackedOperationLifecycle(StrEnum):
    OPEN = "open"
    UNCERTAIN = "uncertain"
    RESOLVED = "resolved"


@dataclass(frozen=True)
class DestinationBinding:
    destination: str
    capability: str
    adapter: str
    configuration_revision: str


@dataclass
class NodeExecution:
    id: UUID
    flow_id: UUID
    correlation_id: UUID
    node_id: UUID
    node: Node
    state: NodeExecutionState = NodeExecutionState.SCHEDULED
    trigger_outcome_event_id: UUID | None = None
    trigger_outcome: str | None = None
    trigger_originating_node_execution_id: UUID | None = None
    trigger_subject: Mapping[str, str] | None = None
    admission: Mapping[str, str] | None = None
    created_at: datetime.datetime = field(
        default_factory=lambda: datetime.datetime.now(datetime.UTC)
    )
    updated_at: datetime.datetime = field(
        default_factory=lambda: datetime.datetime.now(datetime.UTC)
    )

    @staticmethod
    def from_task(task: NodeTask) -> NodeExecution:
        if task.node.id is None:
            raise ValueError("A scheduled node must have a specification ID")
        return NodeExecution(
            id=task.node_execution_id,
            flow_id=task.flow_id,
            correlation_id=task.correlation_id or task.flow_id,
            node_id=task.node.id,
            node=task.node,
            trigger_outcome_event_id=(
                task.trigger.outcome_event_id if task.trigger else None
            ),
            trigger_outcome=(task.trigger.outcome if task.trigger else None),
            trigger_originating_node_execution_id=(
                task.trigger.originating_node_execution_id if task.trigger else None
            ),
            trigger_subject=(
                task.trigger.subject.dict()
                if task.trigger and task.trigger.subject
                else None
            ),
            admission=task.admission,
        )


@dataclass
class ExecutionAttempt:
    id: UUID
    node_execution_id: UUID
    number: int
    started_at: datetime.datetime
    lease_token: UUID | None
    lease_until: datetime.datetime | None
    completed_at: datetime.datetime | None = None
    error: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class AttemptAdmission:
    disposition: AttemptDisposition
    attempt: ExecutionAttempt | None = None
    lease_token: UUID | None = None
    lease_until: datetime.datetime | None = None


@dataclass
class TrackedOperation:
    id: UUID
    node_execution_id: UUID
    capability: str
    binding: DestinationBinding
    lifecycle: TrackedOperationLifecycle
    state: Mapping[str, Any]
    version: int = 0
    provider_account_reference: str | None = None
    provider_reference: str | None = None
    created_at: datetime.datetime = field(
        default_factory=lambda: datetime.datetime.now(datetime.UTC)
    )
    updated_at: datetime.datetime = field(
        default_factory=lambda: datetime.datetime.now(datetime.UTC)
    )


@dataclass(frozen=True)
class Reduction:
    state: Mapping[str, Any]
    outcomes: tuple[OutcomeEmission, ...]
    operation_resolved: bool


@dataclass(frozen=True)
class ReductionTransactionContext:
    operation_id: UUID
    operation_internal_id: int | None
    node_execution_id: UUID
    node_execution_internal_id: int | None
    external_event_id: str
    adapter: str


class CapabilityReducer(Protocol):
    def reduce(self, current_state: Mapping[str, Any], update: Any) -> Reduction: ...


@dataclass(frozen=True)
class AppliedReduction:
    duplicate: bool
    outcomes: tuple[OutcomeEvent, ...] = ()
    node_executions: tuple[NodeExecution, ...] = ()
    successor_tasks: tuple[NodeTask, ...] = ()


@dataclass(frozen=True)
class ReconciliationClaim:
    operation: TrackedOperation
    lease_token: UUID
