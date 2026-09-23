from __future__ import annotations

import asyncio
import copy
import datetime
import uuid

from collections.abc import Awaitable
from collections.abc import Callable
from collections.abc import Mapping
from typing import Any
from uuid import UUID

import orjson

from xarta.protocol.dag import COMMON_OUTCOMES
from xarta.protocol.dag import NodeTask
from xarta.protocol.dag import OutcomeEvent
from xarta.protocol.dag import OutcomeSubject
from xarta.protocol.dag import TriggerContext
from xarta.protocol.dag import resolve_next_nodes
from xarta.tracking.models import AppliedReduction
from xarta.tracking.models import AttemptAdmission
from xarta.tracking.models import AttemptDisposition
from xarta.tracking.models import DestinationBinding
from xarta.tracking.models import ExecutionAttempt
from xarta.tracking.models import NodeExecution
from xarta.tracking.models import NodeExecutionState
from xarta.tracking.models import ReconciliationClaim
from xarta.tracking.models import Reduction
from xarta.tracking.models import ReductionTransactionContext
from xarta.tracking.models import TrackedOperation
from xarta.tracking.models import TrackedOperationLifecycle


class VersionConflictError(Exception):
    pass


class NodeExecutionIdentityConflictError(Exception):
    """One execution ID was reused for a semantically different task."""


def validate_task_identity(execution: NodeExecution, task: NodeTask) -> None:
    """Prove that an incoming delivery is the task permanently bound to this ID."""
    trigger = task.trigger
    incoming_subject = trigger.subject.dict() if trigger and trigger.subject else None
    identity_matches = (
        execution.id == task.node_execution_id
        and execution.flow_id == task.flow_id
        and execution.correlation_id == task.correlation_id
        and execution.node_id == task.node.id
        and execution.node.kind == task.node.kind
        and execution.node.dict() == task.node.dict()
        and execution.trigger_outcome_event_id
        == (trigger.outcome_event_id if trigger else None)
        and execution.trigger_outcome == (trigger.outcome if trigger else None)
        and execution.trigger_originating_node_execution_id
        == (trigger.originating_node_execution_id if trigger else None)
        and execution.trigger_subject == incoming_subject
        and execution.admission == task.admission
    )
    if not identity_matches:
        raise NodeExecutionIdentityConflictError(
            f"NodeExecution {task.node_execution_id} is already bound to another task"
        )


def _successor_tasks(
    node,
    events: tuple[OutcomeEvent, ...] | list[OutcomeEvent],
    correlation_id: UUID,
    created_at: datetime.datetime,
    admission,
) -> tuple[NodeTask, ...]:
    tasks: dict[UUID, NodeTask] = {}
    for event in events:
        for successor in resolve_next_nodes(node, event.outcome):
            if successor.id is None:
                raise ValueError("A successor node must have a specification ID")
            execution_id = uuid.uuid5(event.id, str(successor.id))
            tasks.setdefault(
                execution_id,
                NodeTask(
                    flow_id=event.flow_id,
                    node=successor,
                    node_execution_id=execution_id,
                    trigger=TriggerContext(
                        outcome_event_id=event.id,
                        outcome=event.outcome,
                        originating_node_execution_id=event.node_execution_id,
                        subject=event.subject,
                    ),
                    correlation_id=correlation_id,
                    admission=admission,
                    created_at=created_at,
                ),
            )
    return tuple(tasks.values())


class InMemoryTrackingStore:
    """Transactional reference store used by deterministic runtime tests."""

    def __init__(self) -> None:
        self.node_executions: dict[UUID, NodeExecution] = {}
        self.attempts: dict[UUID, list[ExecutionAttempt]] = {}
        self.operations: dict[UUID, TrackedOperation] = {}
        self.outcomes: dict[UUID, OutcomeEvent] = {}
        self.inbox: set[tuple[UUID, str, str]] = set()
        self.reconciliation_leases: dict[
            UUID, tuple[UUID, datetime.datetime, datetime.datetime, int]
        ] = {}
        self._lock = asyncio.Lock()

    def _assert_provider_identity_available(
        self, operation: TrackedOperation, provider_reference: str | None
    ) -> None:
        if provider_reference is None:
            return
        for candidate in self.operations.values():
            if (
                candidate.id != operation.id
                and candidate.binding.adapter == operation.binding.adapter
                and candidate.provider_account_reference
                == operation.provider_account_reference
                and candidate.provider_reference == provider_reference
            ):
                raise ValueError(
                    "Provider reference is already tracked for this provider account"
                )

    async def accept(self, task: NodeTask) -> NodeExecution:
        async with self._lock:
            execution = self.node_executions.get(task.node_execution_id)
            if execution is None:
                execution = NodeExecution.from_task(task)
                self.node_executions[execution.id] = execution
            else:
                validate_task_identity(execution, task)
            return execution

    async def begin_attempt(
        self, task: NodeTask, lease_seconds: float = 120
    ) -> AttemptAdmission:
        if lease_seconds <= 0:
            raise ValueError("Attempt lease duration must be greater than zero")
        async with self._lock:
            now = datetime.datetime.now(datetime.UTC)
            execution = self.node_executions.get(task.node_execution_id)
            if execution is None:
                execution = NodeExecution.from_task(task)
                self.node_executions[execution.id] = execution
            else:
                validate_task_identity(execution, task)
            if execution.state in {
                NodeExecutionState.RESOLVED,
                NodeExecutionState.FAILED,
            }:
                return AttemptAdmission(AttemptDisposition.DUPLICATE_TERMINAL)
            attempts = self.attempts.setdefault(execution.id, [])
            if execution.state is NodeExecutionState.WAITING_FEEDBACK:
                return AttemptAdmission(AttemptDisposition.WAITING_FEEDBACK)
            active_attempt = attempts[-1] if attempts else None
            if (
                execution.state is NodeExecutionState.EXECUTING
                and active_attempt is not None
                and active_attempt.completed_at is None
                and active_attempt.lease_until is not None
                and active_attempt.lease_until > now
            ):
                return AttemptAdmission(
                    AttemptDisposition.ACTIVE_ATTEMPT,
                    lease_until=active_attempt.lease_until,
                )
            if attempts and attempts[-1].completed_at is None:
                attempts[-1].completed_at = now
                attempts[-1].error = {
                    "error": "execution attempt lease expired",
                    "retryable": True,
                }
                attempts[-1].lease_token = None
                attempts[-1].lease_until = None
            execution.state = NodeExecutionState.EXECUTING
            execution.updated_at = now
            lease_token = uuid.uuid4()
            lease_until = now + datetime.timedelta(seconds=lease_seconds)
            attempt = ExecutionAttempt(
                id=uuid.uuid4(),
                node_execution_id=execution.id,
                number=len(attempts) + 1,
                started_at=now,
                lease_token=lease_token,
                lease_until=lease_until,
            )
            attempts.append(attempt)
            return AttemptAdmission(
                AttemptDisposition.EXECUTE, attempt, lease_token, lease_until
            )

    async def create_operation(
        self,
        node_execution_id: UUID,
        capability: str,
        binding: DestinationBinding,
        state: Mapping[str, Any],
        provider_account_reference: str | None = None,
    ) -> TrackedOperation:
        async with self._lock:
            for operation in self.operations.values():
                if operation.node_execution_id == node_execution_id:
                    return operation
            operation = TrackedOperation(
                id=uuid.uuid4(),
                node_execution_id=node_execution_id,
                capability=capability,
                binding=binding,
                lifecycle=TrackedOperationLifecycle.OPEN,
                state=copy.deepcopy(state),
                provider_account_reference=provider_account_reference,
            )
            self.operations[operation.id] = operation
            return operation

    async def complete_attempt(
        self, attempt: ExecutionAttempt, error: Mapping[str, Any] | None = None
    ) -> bool:
        async with self._lock:
            stored = next(
                (
                    candidate
                    for candidate in self.attempts.get(attempt.node_execution_id, [])
                    if candidate.id == attempt.id
                ),
                None,
            )
            if (
                stored is None
                or stored.completed_at is not None
                or attempt.lease_token is None
                or stored.lease_token != attempt.lease_token
                or stored.lease_until is None
                or stored.lease_until <= datetime.datetime.now(datetime.UTC)
            ):
                return False
            completed_at = datetime.datetime.now(datetime.UTC)
            stored.completed_at = completed_at
            stored.error = copy.deepcopy(error)
            stored.lease_token = None
            stored.lease_until = None
            attempt.completed_at = completed_at
            attempt.error = copy.deepcopy(error)
            attempt.lease_token = None
            attempt.lease_until = None
            return True

    async def renew_attempt_lease(
        self, attempt: ExecutionAttempt, lease_seconds: float = 120
    ) -> bool:
        if lease_seconds <= 0:
            raise ValueError("Attempt lease duration must be greater than zero")
        async with self._lock:
            stored = next(
                (
                    candidate
                    for candidate in self.attempts.get(attempt.node_execution_id, [])
                    if candidate.id == attempt.id
                ),
                None,
            )
            if (
                stored is None
                or stored.completed_at is not None
                or attempt.lease_token is None
                or stored.lease_token != attempt.lease_token
                or stored.lease_until is None
                or stored.lease_until <= datetime.datetime.now(datetime.UTC)
            ):
                return False
            lease_until = datetime.datetime.now(datetime.UTC) + datetime.timedelta(
                seconds=lease_seconds
            )
            stored.lease_until = lease_until
            attempt.lease_until = lease_until
            return True

    async def operation_for_execution(
        self, node_execution_id: UUID
    ) -> TrackedOperation | None:
        async with self._lock:
            return next(
                (
                    operation
                    for operation in self.operations.values()
                    if operation.node_execution_id == node_execution_id
                ),
                None,
            )

    async def operation_for_provider_reference(
        self,
        adapter: str,
        provider_account_reference: str | None,
        provider_reference: str,
    ) -> TrackedOperation | None:
        async with self._lock:
            return next(
                (
                    operation
                    for operation in self.operations.values()
                    if operation.binding.adapter == adapter
                    and operation.provider_account_reference
                    == provider_account_reference
                    and operation.provider_reference == provider_reference
                ),
                None,
            )

    async def get_operation(self, operation_id: UUID) -> TrackedOperation:
        async with self._lock:
            return self.operations[operation_id]

    async def operation_logging_context(self, operation_id: UUID) -> dict[str, Any]:
        async with self._lock:
            operation = self.operations[operation_id]
            execution = self.node_executions[operation.node_execution_id]
            return {
                "flow_id": execution.flow_id,
                "correlation_id": execution.correlation_id,
                "node_id": execution.node_id,
                "node_execution_id": execution.id,
            }

    async def outcomes_for_operation(
        self, operation_id: UUID
    ) -> tuple[OutcomeEvent, ...]:
        async with self._lock:
            return tuple(
                sorted(
                    (
                        event
                        for event in self.outcomes.values()
                        if event.tracked_operation_id == operation_id
                    ),
                    key=lambda event: (event.received_at, event.id),
                )
            )

    async def claim_peppol_reconciliations(
        self, limit: int, lease_seconds: float
    ) -> tuple[ReconciliationClaim, ...]:
        async with self._lock:
            now = datetime.datetime.now(datetime.UTC)
            claims: list[ReconciliationClaim] = []
            for operation in sorted(
                self.operations.values(), key=lambda candidate: candidate.updated_at
            ):
                if len(claims) >= limit:
                    break
                lease = self.reconciliation_leases.get(operation.id)
                next_at = lease[2] if lease else operation.created_at
                if (
                    operation.capability != "peppol"
                    or operation.provider_reference is None
                    or operation.lifecycle is TrackedOperationLifecycle.RESOLVED
                    or next_at > now
                    or (lease is not None and lease[1] > now)
                ):
                    continue
                token = uuid.uuid4()
                attempts = lease[3] if lease else 0
                self.reconciliation_leases[operation.id] = (
                    token,
                    now + datetime.timedelta(seconds=lease_seconds),
                    next_at,
                    attempts,
                )
                claims.append(ReconciliationClaim(operation, token))
            return tuple(claims)

    async def claim_email_reconciliations(
        self, limit: int, lease_seconds: float
    ) -> tuple[ReconciliationClaim, ...]:
        async with self._lock:
            now = datetime.datetime.now(datetime.UTC)
            claims: list[ReconciliationClaim] = []
            for operation in sorted(
                self.operations.values(), key=lambda candidate: candidate.updated_at
            ):
                if len(claims) >= limit:
                    break
                lease = self.reconciliation_leases.get(operation.id)
                next_at = lease[2] if lease else operation.created_at
                if (
                    operation.capability != "email"
                    or operation.binding.adapter != "resend-rest"
                    or operation.provider_reference is None
                    or operation.lifecycle is TrackedOperationLifecycle.RESOLVED
                    or next_at > now
                    or (lease is not None and lease[1] > now)
                ):
                    continue
                token = uuid.uuid4()
                attempts = lease[3] if lease else 0
                self.reconciliation_leases[operation.id] = (
                    token,
                    now + datetime.timedelta(seconds=lease_seconds),
                    next_at,
                    attempts,
                )
                claims.append(ReconciliationClaim(operation, token))
            return tuple(claims)

    async def complete_peppol_reconciliation(
        self, claim: ReconciliationClaim, interval_seconds: float
    ) -> None:
        async with self._lock:
            lease = self.reconciliation_leases.get(claim.operation.id)
            if lease is None or lease[0] != claim.lease_token:
                return
            now = datetime.datetime.now(datetime.UTC)
            self.reconciliation_leases[claim.operation.id] = (
                claim.lease_token,
                now,
                now + datetime.timedelta(seconds=interval_seconds),
                0,
            )

    async def fail_peppol_reconciliation(
        self, claim: ReconciliationClaim, max_backoff_seconds: float
    ) -> None:
        async with self._lock:
            lease = self.reconciliation_leases.get(claim.operation.id)
            if lease is None or lease[0] != claim.lease_token:
                return
            attempts = lease[3] + 1
            now = datetime.datetime.now(datetime.UTC)
            delay = min(max_backoff_seconds, 2 ** min(attempts, 16))
            self.reconciliation_leases[claim.operation.id] = (
                claim.lease_token,
                now,
                now + datetime.timedelta(seconds=delay),
                attempts,
            )

    async def complete_reconciliation(
        self, claim: ReconciliationClaim, interval_seconds: float
    ) -> None:
        await self.complete_peppol_reconciliation(claim, interval_seconds)

    async def fail_reconciliation(
        self, claim: ReconciliationClaim, max_backoff_seconds: float
    ) -> None:
        await self.fail_peppol_reconciliation(claim, max_backoff_seconds)

    async def flow_settled(self, flow_id: UUID) -> bool:
        async with self._lock:
            execution_ids = {
                execution.id
                for execution in self.node_executions.values()
                if execution.flow_id == flow_id
            }
            executions_settled = all(
                self.node_executions[execution_id].state
                in {NodeExecutionState.RESOLVED, NodeExecutionState.FAILED}
                for execution_id in execution_ids
            )
            operations_settled = all(
                operation.lifecycle is TrackedOperationLifecycle.RESOLVED
                for operation in self.operations.values()
                if operation.node_execution_id in execution_ids
            )
            return executions_settled and operations_settled

    async def mark_waiting(
        self,
        operation_id: UUID,
        provider_reference: str | None,
        state: Mapping[str, Any],
    ) -> None:
        async with self._lock:
            operation = self.operations[operation_id]
            if operation.lifecycle is TrackedOperationLifecycle.RESOLVED:
                return
            self._assert_provider_identity_available(operation, provider_reference)
            operation.provider_reference = provider_reference
            operation.state = copy.deepcopy(state)
            operation.lifecycle = TrackedOperationLifecycle.OPEN
            operation.updated_at = datetime.datetime.now(datetime.UTC)
            execution = self.node_executions[operation.node_execution_id]
            execution.state = NodeExecutionState.WAITING_FEEDBACK
            execution.updated_at = operation.updated_at

    async def checkpoint_operation(
        self,
        operation_id: UUID,
        expected_version: int,
        state: Mapping[str, Any],
        provider_reference: str | None = None,
    ) -> TrackedOperation:
        """Durably checkpoint an open operation without ending its attempt."""
        async with self._lock:
            operation = self.operations[operation_id]
            if operation.version != expected_version:
                raise VersionConflictError
            if operation.lifecycle is not TrackedOperationLifecycle.OPEN:
                raise ValueError("Only open operations can be checkpointed")
            operation.state = copy.deepcopy(state)
            if provider_reference is not None:
                self._assert_provider_identity_available(operation, provider_reference)
                operation.provider_reference = provider_reference
            operation.version += 1
            operation.updated_at = datetime.datetime.now(datetime.UTC)
            return operation

    async def mark_uncertain(self, operation_id: UUID) -> None:
        async with self._lock:
            operation = self.operations[operation_id]
            operation.lifecycle = TrackedOperationLifecycle.UNCERTAIN
            execution = self.node_executions[operation.node_execution_id]
            execution.state = NodeExecutionState.WAITING_FEEDBACK

    async def apply_reduction(
        self,
        operation_id: UUID,
        adapter: str,
        external_event_id: str,
        expected_version: int,
        reduction: Reduction,
        source: str,
        transaction_mutation: (
            Callable[[Any, ReductionTransactionContext], Awaitable[None]] | None
        ) = None,
    ) -> AppliedReduction:
        async with self._lock:
            inbox_key = (operation_id, adapter, external_event_id)
            if inbox_key in self.inbox:
                operation = self.operations[operation_id]
                execution = self.node_executions[operation.node_execution_id]
                duplicate_events = tuple(
                    event
                    for event in self.outcomes.values()
                    if event.tracked_operation_id == operation_id
                    and event.external_event_id == external_event_id
                )
                return AppliedReduction(
                    duplicate=True,
                    outcomes=duplicate_events,
                    successor_tasks=_successor_tasks(
                        execution.node,
                        duplicate_events,
                        execution.correlation_id,
                        execution.created_at,
                        execution.admission,
                    ),
                )

            operation = self.operations[operation_id]
            if adapter != operation.binding.adapter:
                raise ValueError("Feedback adapter does not match pinned operation")
            if operation.lifecycle is TrackedOperationLifecycle.RESOLVED:
                self.inbox.add(inbox_key)
                return AppliedReduction(duplicate=True)
            if operation.version != expected_version:
                raise VersionConflictError

            # Snapshot first so every mutation below has one commit point.
            execution = self.node_executions[operation.node_execution_id]
            if transaction_mutation is not None:
                await transaction_mutation(
                    None,
                    ReductionTransactionContext(
                        operation_id=operation.id,
                        operation_internal_id=None,
                        node_execution_id=execution.id,
                        node_execution_internal_id=None,
                        external_event_id=external_event_id,
                        adapter=adapter,
                    ),
                )
            now = datetime.datetime.now(datetime.UTC)
            events = []
            successor_tasks: dict[UUID, NodeTask] = {}
            pending_activation_keys = set()
            for emission in reduction.outcomes:
                event = OutcomeEvent(
                    flow_id=execution.flow_id,
                    node_execution_id=execution.id,
                    tracked_operation_id=operation.id,
                    outcome=emission.outcome,
                    subject=emission.subject,
                    details=copy.deepcopy(emission.details),
                    source=source,
                    external_event_id=external_event_id,
                    occurred_at=emission.occurred_at,
                )
                events.append(event)
                for successor in resolve_next_nodes(execution.node, event.outcome):
                    if successor.id is None:
                        raise ValueError(
                            "A successor node must have a specification ID"
                        )
                    activation_key = (event.id, successor.id)
                    if activation_key in pending_activation_keys:
                        continue
                    task = NodeTask(
                        flow_id=execution.flow_id,
                        node=successor,
                        node_execution_id=uuid.uuid5(event.id, str(successor.id)),
                        trigger=TriggerContext(
                            outcome_event_id=event.id,
                            outcome=event.outcome,
                            subject=event.subject,
                            originating_node_execution_id=execution.id,
                        ),
                        correlation_id=execution.correlation_id,
                        admission=execution.admission,
                        created_at=execution.created_at,
                    )
                    successor_tasks.setdefault(task.node_execution_id, task)
                    pending_activation_keys.add(activation_key)

            self.inbox.add(inbox_key)
            operation.state = copy.deepcopy(reduction.state)
            operation.version += 1
            operation.updated_at = now
            if reduction.operation_resolved:
                operation.lifecycle = TrackedOperationLifecycle.RESOLVED
                execution.state = (
                    NodeExecutionState.FAILED
                    if any(
                        outcome.outcome in COMMON_OUTCOMES
                        for outcome in reduction.outcomes
                    )
                    else NodeExecutionState.RESOLVED
                )
            else:
                execution.state = NodeExecutionState.WAITING_FEEDBACK
            execution.updated_at = now
            for event in events:
                self.outcomes[event.id] = event
            return AppliedReduction(
                duplicate=False,
                outcomes=tuple(events),
                successor_tasks=tuple(successor_tasks.values()),
            )

    async def apply_execution_outcomes(
        self,
        task: NodeTask,
        outcomes: tuple,
        source: str,
    ) -> AppliedReduction:
        async with self._lock:
            execution = self.node_executions.get(task.node_execution_id)
            if execution is None:
                execution = NodeExecution.from_task(task)
                self.node_executions[execution.id] = execution
            else:
                validate_task_identity(execution, task)
            external_event_id = f"execution:{task.node_execution_id}"
            if any(
                event.node_execution_id == execution.id
                and event.tracked_operation_id is None
                and event.external_event_id == external_event_id
                for event in self.outcomes.values()
            ):
                duplicate_events = tuple(
                    event
                    for event in self.outcomes.values()
                    if event.node_execution_id == execution.id
                    and event.tracked_operation_id is None
                    and event.external_event_id == external_event_id
                )
                return AppliedReduction(
                    duplicate=True,
                    outcomes=duplicate_events,
                    successor_tasks=_successor_tasks(
                        execution.node,
                        duplicate_events,
                        execution.correlation_id,
                        execution.created_at,
                        execution.admission,
                    ),
                )

            events = []
            successor_tasks: dict[UUID, NodeTask] = {}
            pending_activation_keys = set()
            for emission in outcomes:
                event = OutcomeEvent(
                    flow_id=execution.flow_id,
                    node_execution_id=execution.id,
                    outcome=emission.outcome,
                    subject=emission.subject,
                    details=copy.deepcopy(emission.details),
                    source=source,
                    external_event_id=external_event_id,
                    occurred_at=emission.occurred_at,
                )
                events.append(event)
                for successor in resolve_next_nodes(execution.node, event.outcome):
                    if successor.id is None:
                        raise ValueError(
                            "A successor node must have a specification ID"
                        )
                    activation_key = (event.id, successor.id)
                    if activation_key in pending_activation_keys:
                        continue
                    child_task = NodeTask(
                        flow_id=execution.flow_id,
                        node=successor,
                        node_execution_id=uuid.uuid5(event.id, str(successor.id)),
                        trigger=TriggerContext(
                            outcome_event_id=event.id,
                            outcome=event.outcome,
                            subject=event.subject,
                            originating_node_execution_id=execution.id,
                        ),
                        correlation_id=task.correlation_id,
                        admission=execution.admission,
                    )
                    successor_tasks.setdefault(child_task.node_execution_id, child_task)
                    pending_activation_keys.add(activation_key)

            now = datetime.datetime.now(datetime.UTC)
            execution.state = (
                NodeExecutionState.FAILED
                if any(event.outcome in COMMON_OUTCOMES for event in events)
                else NodeExecutionState.RESOLVED
            )
            execution.updated_at = now
            for operation in self.operations.values():
                if operation.node_execution_id == execution.id:
                    operation.lifecycle = TrackedOperationLifecycle.RESOLVED
                    operation.updated_at = now
            for event in events:
                self.outcomes[event.id] = event
            return AppliedReduction(
                False,
                tuple(events),
                successor_tasks=tuple(successor_tasks.values()),
            )

    async def replay_successors(self, task: NodeTask) -> tuple[NodeTask, ...]:
        execution = self.node_executions[task.node_execution_id]
        events = tuple(
            event
            for event in self.outcomes.values()
            if event.node_execution_id == task.node_execution_id
        )
        return _successor_tasks(
            execution.node,
            events,
            execution.correlation_id,
            execution.created_at,
            execution.admission,
        )

    async def record_outcomes(
        self, task: NodeTask, outcomes: tuple, source: str
    ) -> AppliedReduction:
        return await self.apply_execution_outcomes(task, outcomes, source)


class PostgresTrackingStore:
    """Production store; each reduction is persisted in one PostgreSQL transaction."""

    def __init__(self, pool) -> None:
        self.pool = pool

    @staticmethod
    def _json(value: Any) -> str:
        return orjson.dumps(value, default=str).decode()

    @staticmethod
    def _decode(value: Any) -> Any:
        return orjson.loads(value) if isinstance(value, str) else value

    def _operation_from_row(self, row) -> TrackedOperation:
        return TrackedOperation(
            id=row["id"],
            node_execution_id=row["node_execution_public_id"],
            capability=row["capability"],
            binding=DestinationBinding(
                row["destination"],
                row["capability"],
                row["adapter"],
                row["adapter_configuration_revision"],
            ),
            lifecycle=TrackedOperationLifecycle(row["lifecycle"]),
            state=dict(self._decode(row["state"])),
            version=row["version"],
            provider_account_reference=row["provider_account_reference"],
            provider_reference=row["provider_reference"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    async def _register_execution(self, connection, task: NodeTask):
        """Insert an execution once, then validate every redelivery under lock."""
        trigger = task.trigger
        await connection.execute(
            """
            INSERT INTO node_executions (
                id, flow_id, correlation_id, node_id, kind, node,
                trigger_outcome_event_id, trigger_outcome_event_public_id,
                trigger_outcome, trigger_originating_node_execution_id,
                trigger_originating_node_execution_public_id, trigger_subject,
                admission, state
            ) VALUES (
                $1, $2, $3, $4, $5, $6::jsonb,
                (SELECT _id FROM outcome_events WHERE id = $7), $7, $8,
                (SELECT _id FROM node_executions WHERE id = $9), $9,
                $10::jsonb, $11::jsonb, 'scheduled'
            )
            ON CONFLICT (id) DO NOTHING
            """,
            task.node_execution_id,
            task.flow_id,
            task.correlation_id,
            task.node.id,
            task.node.kind,
            self._json(task.node.dict()),
            trigger.outcome_event_id if trigger else None,
            trigger.outcome if trigger else None,
            trigger.originating_node_execution_id if trigger else None,
            self._json(trigger.subject.dict()) if trigger and trigger.subject else None,
            self._json(task.admission) if task.admission else None,
        )
        row = await connection.fetchrow(
            """
            SELECT execution.*
            FROM node_executions execution
            WHERE execution.id = $1
            FOR UPDATE OF execution
            """,
            task.node_execution_id,
        )
        incoming_subject = (
            trigger.subject.dict() if trigger and trigger.subject else None
        )
        stored_node = self._decode(row["node"])
        incoming_node = self._decode(self._json(task.node.dict()))
        identity_matches = (
            row["flow_id"] == task.flow_id
            and row["correlation_id"] == task.correlation_id
            and row["node_id"] == task.node.id
            and row["kind"] == task.node.kind
            # A successor is serialized with its DAG parent, then parsed as the
            # root of its own task. Trigger identity is the authoritative link.
            and {**stored_node, "parent": None} == {**incoming_node, "parent": None}
            and row["trigger_outcome_event_public_id"]
            == (trigger.outcome_event_id if trigger else None)
            and row["trigger_outcome"] == (trigger.outcome if trigger else None)
            and row["trigger_originating_node_execution_public_id"]
            == (trigger.originating_node_execution_id if trigger else None)
            and self._decode(row["trigger_subject"]) == incoming_subject
            and self._decode(row["admission"]) == task.admission
        )
        if not identity_matches:
            raise NodeExecutionIdentityConflictError(
                f"NodeExecution {task.node_execution_id} is already bound to another task"
            )
        return row

    async def accept(self, task: NodeTask) -> None:
        async with self.pool.acquire() as connection, connection.transaction():
            await self._register_execution(connection, task)

    async def begin_attempt(
        self, task: NodeTask, lease_seconds: float = 120
    ) -> AttemptAdmission:
        if lease_seconds <= 0:
            raise ValueError("Attempt lease duration must be greater than zero")
        async with self.pool.acquire() as connection, connection.transaction():
            execution_row = await self._register_execution(connection, task)
            state = NodeExecutionState(execution_row["state"])
            if state in {NodeExecutionState.RESOLVED, NodeExecutionState.FAILED}:
                return AttemptAdmission(AttemptDisposition.DUPLICATE_TERMINAL)
            latest = await connection.fetchrow(
                """
                    SELECT id, completed_at, lease_until
                    FROM execution_attempts
                    WHERE node_execution_id = $1
                    ORDER BY attempt DESC
                    LIMIT 1
                    """,
                execution_row["_id"],
            )
            now = await connection.fetchval("SELECT now()")
            if state is NodeExecutionState.WAITING_FEEDBACK:
                return AttemptAdmission(AttemptDisposition.WAITING_FEEDBACK)
            if (
                state is NodeExecutionState.EXECUTING
                and latest is not None
                and latest["completed_at"] is None
                and latest["lease_until"] > now
            ):
                return AttemptAdmission(
                    AttemptDisposition.ACTIVE_ATTEMPT,
                    lease_until=latest["lease_until"],
                )
            if latest is not None and latest["completed_at"] is None:
                await connection.execute(
                    """
                        UPDATE execution_attempts
                        SET completed_at = now(),
                            error = '{"error": "execution attempt lease expired", "retryable": true}'::jsonb,
                            lease_token = NULL,
                            lease_until = NULL
                        WHERE id = $1 AND completed_at IS NULL
                        """,
                    latest["id"],
                )
            number = await connection.fetchval(
                """
                    SELECT COALESCE(MAX(attempt), 0) + 1 AS attempt
                    FROM execution_attempts
                    WHERE node_execution_id = $1
                    """,
                execution_row["_id"],
            )
            lease_token = uuid.uuid4()
            lease_until = now + datetime.timedelta(seconds=lease_seconds)
            attempt = ExecutionAttempt(
                id=uuid.uuid4(),
                node_execution_id=task.node_execution_id,
                number=number,
                started_at=now,
                lease_token=lease_token,
                lease_until=lease_until,
            )
            await connection.execute(
                """
                    UPDATE node_executions
                    SET state = 'executing', updated_at = now()
                    WHERE _id = $1
                    """,
                execution_row["_id"],
            )
            await connection.execute(
                """
                    INSERT INTO execution_attempts (
                        id, node_execution_id, attempt, started_at,
                        lease_token, lease_until
                    ) VALUES ($1, $2, $3, $4, $5, $6)
                    """,
                attempt.id,
                execution_row["_id"],
                attempt.number,
                attempt.started_at,
                attempt.lease_token,
                attempt.lease_until,
            )
        return AttemptAdmission(
            AttemptDisposition.EXECUTE, attempt, lease_token, lease_until
        )

    async def create_operation(
        self,
        node_execution_id: UUID,
        capability: str,
        binding: DestinationBinding,
        state: Mapping[str, Any],
        provider_account_reference: str | None = None,
    ) -> TrackedOperation:
        operation_id = uuid.uuid4()
        async with self.pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                WITH operation AS (
                    INSERT INTO tracked_operations (
                        id, node_execution_id, capability, destination, adapter,
                        adapter_configuration_revision, provider_account_reference,
                        lifecycle, state
                    )
                    SELECT $1, execution._id, $3, $4, $5, $6, $7,
                           'open', $8::jsonb
                    FROM node_executions execution
                    WHERE execution.id = $2
                    ON CONFLICT (node_execution_id) DO UPDATE
                        SET node_execution_id = EXCLUDED.node_execution_id
                    RETURNING *
                )
                SELECT operation.*, execution.id AS node_execution_public_id
                FROM operation
                JOIN node_executions execution
                    ON execution._id = operation.node_execution_id
                """,
                operation_id,
                node_execution_id,
                capability,
                binding.destination,
                binding.adapter,
                binding.configuration_revision,
                provider_account_reference,
                self._json(state),
            )
        return self._operation_from_row(row)

    async def complete_attempt(
        self, attempt: ExecutionAttempt, error: Mapping[str, Any] | None = None
    ) -> bool:
        if attempt.lease_token is None:
            return False
        async with self.pool.acquire() as connection:
            completed = await connection.fetchval(
                """
                UPDATE execution_attempts
                SET completed_at = now(), error = $3::jsonb,
                    lease_token = NULL, lease_until = NULL
                WHERE id = $1 AND lease_token = $2 AND completed_at IS NULL
                  AND lease_until > now()
                RETURNING TRUE
                """,
                attempt.id,
                attempt.lease_token,
                self._json(error) if error else None,
            )
        if completed:
            attempt.completed_at = datetime.datetime.now(datetime.UTC)
            attempt.error = copy.deepcopy(error)
            attempt.lease_token = None
            attempt.lease_until = None
        return bool(completed)

    async def renew_attempt_lease(
        self, attempt: ExecutionAttempt, lease_seconds: float = 120
    ) -> bool:
        if lease_seconds <= 0:
            raise ValueError("Attempt lease duration must be greater than zero")
        if attempt.lease_token is None:
            return False
        async with self.pool.acquire() as connection:
            lease_until = await connection.fetchval(
                """
                UPDATE execution_attempts
                SET lease_until = now() + ($3::double precision * interval '1 second')
                WHERE id = $1 AND lease_token = $2 AND completed_at IS NULL
                  AND lease_until > now()
                RETURNING lease_until
                """,
                attempt.id,
                attempt.lease_token,
                lease_seconds,
            )
        if lease_until is not None:
            attempt.lease_until = lease_until
        return lease_until is not None

    async def operation_for_execution(
        self, node_execution_id: UUID
    ) -> TrackedOperation | None:
        async with self.pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT operation.*, execution.id AS node_execution_public_id
                FROM tracked_operations operation
                JOIN node_executions execution
                    ON execution._id = operation.node_execution_id
                WHERE execution.id = $1
                """,
                node_execution_id,
            )
        if row is None:
            return None
        return self._operation_from_row(row)

    async def operation_for_provider_reference(
        self,
        adapter: str,
        provider_account_reference: str | None,
        provider_reference: str,
    ) -> TrackedOperation | None:
        async with self.pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT id FROM tracked_operations
                WHERE adapter = $1
                  AND provider_account_reference IS NOT DISTINCT FROM $2
                  AND provider_reference = $3
                """,
                adapter,
                provider_account_reference,
                provider_reference,
            )
        if row is None:
            return None
        return await self.get_operation(row["id"])

    async def get_operation(self, operation_id: UUID) -> TrackedOperation:
        async with self.pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT operation.*, execution.id AS node_execution_public_id
                FROM tracked_operations operation
                JOIN node_executions execution
                    ON execution._id = operation.node_execution_id
                WHERE operation.id = $1
                """,
                operation_id,
            )
        if row is None:
            raise KeyError(operation_id)
        return self._operation_from_row(row)

    async def operation_logging_context(self, operation_id: UUID) -> dict[str, Any]:
        async with self.pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT execution.flow_id, execution.correlation_id,
                       execution.node_id, execution.id AS node_execution_id
                FROM tracked_operations operation
                JOIN node_executions execution
                    ON execution._id = operation.node_execution_id
                WHERE operation.id = $1
                """,
                operation_id,
            )
        if row is None:
            raise KeyError(operation_id)
        return dict(row)

    async def outcomes_for_operation(
        self, operation_id: UUID
    ) -> tuple[OutcomeEvent, ...]:
        async with self.pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT event.*, execution.id AS node_execution_public_id,
                       operation.id AS tracked_operation_public_id
                FROM outcome_events event
                JOIN node_executions execution
                    ON execution._id = event.node_execution_id
                JOIN tracked_operations operation
                    ON operation._id = event.tracked_operation_id
                WHERE operation.id = $1
                ORDER BY event.received_at, event._id
                """,
                operation_id,
            )
        return tuple(
            OutcomeEvent(
                id=row["id"],
                flow_id=row["flow_id"],
                node_execution_id=row["node_execution_public_id"],
                tracked_operation_id=row["tracked_operation_public_id"],
                outcome=row["outcome"],
                subject=(
                    OutcomeSubject.fromdict(self._decode(row["subject"]))
                    if row["subject"] is not None
                    else None
                ),
                details=(
                    dict(self._decode(row["details"]))
                    if row["details"] is not None
                    else None
                ),
                source=row["source"],
                external_event_id=row["external_event_id"],
                occurred_at=row["occurred_at"],
                received_at=row["received_at"],
            )
            for row in rows
        )

    async def claim_peppol_reconciliations(
        self, limit: int, lease_seconds: float
    ) -> tuple[ReconciliationClaim, ...]:
        token = uuid.uuid4()
        async with self.pool.acquire() as connection, connection.transaction():
            rows = await connection.fetch(
                """
                WITH candidates AS (
                    SELECT operation._id,
                           execution.id AS node_execution_public_id
                    FROM tracked_operations operation
                    JOIN node_executions execution
                        ON execution._id = operation.node_execution_id
                    WHERE operation.capability = 'peppol'
                      AND operation.provider_reference IS NOT NULL
                      AND operation.lifecycle IN ('open', 'uncertain')
                      AND operation.reconciliation_next_at <= now()
                      AND (
                          operation.reconciliation_lease_until IS NULL
                          OR operation.reconciliation_lease_until <= now()
                      )
                    ORDER BY operation.reconciliation_next_at,
                             operation.updated_at, operation._id
                    FOR UPDATE OF operation SKIP LOCKED
                    LIMIT $1
                )
                UPDATE tracked_operations operation
                SET reconciliation_lease_token = $2,
                    reconciliation_lease_until =
                        now() + ($3::double precision * interval '1 second')
                FROM candidates
                WHERE operation._id = candidates._id
                RETURNING operation.*, candidates.node_execution_public_id
                """,
                limit,
                token,
                lease_seconds,
            )
        return tuple(
            ReconciliationClaim(self._operation_from_row(row), token) for row in rows
        )

    async def claim_email_reconciliations(
        self, limit: int, lease_seconds: float
    ) -> tuple[ReconciliationClaim, ...]:
        token = uuid.uuid4()
        async with self.pool.acquire() as connection, connection.transaction():
            rows = await connection.fetch(
                """
                WITH candidates AS (
                    SELECT operation._id,
                           execution.id AS node_execution_public_id
                    FROM tracked_operations operation
                    JOIN node_executions execution
                        ON execution._id = operation.node_execution_id
                    WHERE operation.capability = 'email'
                      AND operation.adapter = 'resend-rest'
                      AND operation.provider_reference IS NOT NULL
                      AND operation.lifecycle IN ('open', 'uncertain')
                      AND operation.reconciliation_next_at <= now()
                      AND (
                          operation.reconciliation_lease_until IS NULL
                          OR operation.reconciliation_lease_until <= now()
                      )
                    ORDER BY operation.reconciliation_next_at,
                             operation.updated_at, operation._id
                    FOR UPDATE OF operation SKIP LOCKED
                    LIMIT $1
                )
                UPDATE tracked_operations operation
                SET reconciliation_lease_token = $2,
                    reconciliation_lease_until =
                        now() + ($3::double precision * interval '1 second')
                FROM candidates
                WHERE operation._id = candidates._id
                RETURNING operation.*, candidates.node_execution_public_id
                """,
                limit,
                token,
                lease_seconds,
            )
        return tuple(
            ReconciliationClaim(self._operation_from_row(row), token) for row in rows
        )

    async def complete_peppol_reconciliation(
        self, claim: ReconciliationClaim, interval_seconds: float
    ) -> None:
        async with self.pool.acquire() as connection:
            await connection.execute(
                """
                UPDATE tracked_operations
                SET reconciliation_lease_token = NULL,
                    reconciliation_lease_until = NULL,
                    reconciliation_next_at =
                        now() + ($3::double precision * interval '1 second'),
                    reconciliation_attempts = 0
                WHERE id = $1 AND reconciliation_lease_token = $2
                """,
                claim.operation.id,
                claim.lease_token,
                interval_seconds,
            )

    async def fail_peppol_reconciliation(
        self, claim: ReconciliationClaim, max_backoff_seconds: float
    ) -> None:
        async with self.pool.acquire() as connection:
            await connection.execute(
                """
                UPDATE tracked_operations
                SET reconciliation_lease_token = NULL,
                    reconciliation_lease_until = NULL,
                    reconciliation_attempts = reconciliation_attempts + 1,
                    reconciliation_next_at = now() + (
                        LEAST(
                            $3::double precision,
                            power(2, LEAST(reconciliation_attempts + 1, 16))
                        ) * interval '1 second'
                    )
                WHERE id = $1 AND reconciliation_lease_token = $2
                """,
                claim.operation.id,
                claim.lease_token,
                max_backoff_seconds,
            )

    async def complete_reconciliation(
        self, claim: ReconciliationClaim, interval_seconds: float
    ) -> None:
        await self.complete_peppol_reconciliation(claim, interval_seconds)

    async def fail_reconciliation(
        self, claim: ReconciliationClaim, max_backoff_seconds: float
    ) -> None:
        await self.fail_peppol_reconciliation(claim, max_backoff_seconds)

    async def flow_settled(self, flow_id: UUID) -> bool:
        async with self.pool.acquire() as connection:
            active = await connection.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1 FROM node_executions e
                    LEFT JOIN tracked_operations o ON o.node_execution_id = e._id
                    WHERE e.flow_id = $1
                      AND (
                        e.state NOT IN ('resolved', 'failed')
                        OR (o.id IS NOT NULL AND o.lifecycle <> 'resolved')
                      )
                )
                """,
                flow_id,
            )
        return not active

    async def mark_waiting(
        self,
        operation_id: UUID,
        provider_reference: str | None,
        state: Mapping[str, Any],
    ) -> None:
        async with self.pool.acquire() as connection, connection.transaction():
            execution_id = await connection.fetchval(
                """
                    UPDATE tracked_operations
                    SET provider_reference = $2, state = $3::jsonb,
                        lifecycle = 'open', updated_at = now()
                    WHERE id = $1
                    RETURNING node_execution_id
                    """,
                operation_id,
                provider_reference,
                self._json(state),
            )
            if execution_id is None:
                return
            await connection.execute(
                """
                    UPDATE node_executions
                    SET state = 'waiting_feedback', updated_at = now()
                    WHERE _id = $1
                    """,
                execution_id,
            )

    async def checkpoint_operation(
        self,
        operation_id: UUID,
        expected_version: int,
        state: Mapping[str, Any],
        provider_reference: str | None = None,
    ) -> TrackedOperation:
        """CAS an intermediate provider checkpoint while execution continues."""
        async with self.pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                WITH operation AS (
                    UPDATE tracked_operations
                    SET state = $3::jsonb,
                        provider_reference = COALESCE($4, provider_reference),
                        version = version + 1,
                        updated_at = now()
                    WHERE id = $1 AND version = $2 AND lifecycle = 'open'
                    RETURNING *
                )
                SELECT operation.*, execution.id AS node_execution_public_id
                FROM operation
                JOIN node_executions execution
                    ON execution._id = operation.node_execution_id
                """,
                operation_id,
                expected_version,
                self._json(state),
                provider_reference,
            )
        if row is None:
            operation = await self.get_operation(operation_id)
            if operation.version != expected_version:
                raise VersionConflictError
            raise ValueError("Only open operations can be checkpointed")
        return self._operation_from_row(row)

    async def mark_uncertain(self, operation_id: UUID) -> None:
        async with self.pool.acquire() as connection, connection.transaction():
            execution_id = await connection.fetchval(
                """
                    UPDATE tracked_operations
                    SET lifecycle = 'uncertain', updated_at = now()
                    WHERE id = $1 RETURNING node_execution_id
                    """,
                operation_id,
            )
            await connection.execute(
                """
                    UPDATE node_executions
                    SET state = 'waiting_feedback', updated_at = now()
                    WHERE _id = $1
                    """,
                execution_id,
            )

    async def apply_reduction(
        self,
        operation_id: UUID,
        adapter: str,
        external_event_id: str,
        expected_version: int,
        reduction: Reduction,
        source: str,
        transaction_mutation: (
            Callable[[Any, ReductionTransactionContext], Awaitable[None]] | None
        ) = None,
    ) -> AppliedReduction:
        async with self.pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                """
                    SELECT o.*, e.flow_id, e.correlation_id, e.node, e.admission,
                           e.id AS execution_id,
                           e._id AS execution_internal_id,
                           e.created_at AS execution_created_at,
                           o._id AS operation_internal_id
                    FROM tracked_operations o
                    JOIN node_executions e ON e._id = o.node_execution_id
                    WHERE o.id = $1
                    FOR UPDATE
                    """,
                operation_id,
            )
            inserted = await connection.fetchval(
                """
                    INSERT INTO provider_event_inbox (
                        tracked_operation_id, adapter, external_event_id
                    )
                    SELECT _id, $2, $3
                    FROM tracked_operations
                    WHERE id = $1
                    ON CONFLICT DO NOTHING
                    RETURNING TRUE
                    """,
                operation_id,
                adapter,
                external_event_id,
            )
            if not inserted:
                event_rows = await connection.fetch(
                    """
                    SELECT * FROM outcome_events
                    WHERE tracked_operation_id = $1 AND external_event_id = $2
                    ORDER BY received_at, _id
                    """,
                    row["operation_internal_id"],
                    external_event_id,
                )
                duplicate_events = tuple(
                    OutcomeEvent(
                        id=event_row["id"],
                        flow_id=event_row["flow_id"],
                        node_execution_id=row["execution_id"],
                        tracked_operation_id=operation_id,
                        outcome=event_row["outcome"],
                        subject=(
                            OutcomeSubject.fromdict(self._decode(event_row["subject"]))
                            if event_row["subject"] is not None
                            else None
                        ),
                        details=(
                            dict(self._decode(event_row["details"]))
                            if event_row["details"] is not None
                            else None
                        ),
                        source=event_row["source"],
                        external_event_id=event_row["external_event_id"],
                        occurred_at=event_row["occurred_at"],
                        received_at=event_row["received_at"],
                    )
                    for event_row in event_rows
                )
                node = NodeTask.fromdict(
                    {
                        "flow_id": row["flow_id"],
                        "node_execution_id": row["execution_id"],
                        "node": self._decode(row["node"]),
                    }
                ).node
                return AppliedReduction(
                    duplicate=True,
                    outcomes=duplicate_events,
                    successor_tasks=_successor_tasks(
                        node,
                        duplicate_events,
                        row["correlation_id"],
                        row["execution_created_at"],
                        self._decode(row["admission"]),
                    ),
                )
            if row["version"] != expected_version:
                raise VersionConflictError
            if row["adapter"] != adapter:
                raise ValueError("Feedback adapter does not match pinned operation")
            if (
                TrackedOperationLifecycle(row["lifecycle"])
                is TrackedOperationLifecycle.RESOLVED
            ):
                return AppliedReduction(duplicate=True)
            node = NodeTask.fromdict(
                {
                    "flow_id": row["flow_id"],
                    "node_execution_id": row["execution_id"],
                    "node": self._decode(row["node"]),
                }
            ).node

            if transaction_mutation is not None:
                await transaction_mutation(
                    connection,
                    ReductionTransactionContext(
                        operation_id=operation_id,
                        operation_internal_id=row["operation_internal_id"],
                        node_execution_id=row["execution_id"],
                        node_execution_internal_id=row["execution_internal_id"],
                        external_event_id=external_event_id,
                        adapter=adapter,
                    ),
                )

            events = []
            successor_tasks: dict[UUID, NodeTask] = {}
            for emission in reduction.outcomes:
                event = OutcomeEvent(
                    flow_id=row["flow_id"],
                    node_execution_id=row["execution_id"],
                    tracked_operation_id=operation_id,
                    outcome=emission.outcome,
                    subject=emission.subject,
                    details=copy.deepcopy(emission.details),
                    source=source,
                    external_event_id=external_event_id,
                    occurred_at=emission.occurred_at,
                )
                await connection.execute(
                    """
                        INSERT INTO outcome_events (
                            id, flow_id, node_execution_id, tracked_operation_id,
                            outcome, subject, details, source, external_event_id,
                            occurred_at, received_at
                        ) VALUES (
                            $1, $2, $3, $4, $5, $6::jsonb, $7::jsonb,
                            $8, $9, $10, $11
                        )
                        """,
                    event.id,
                    event.flow_id,
                    row["execution_internal_id"],
                    row["operation_internal_id"],
                    event.outcome,
                    self._json(event.subject.dict()) if event.subject else None,
                    self._json(event.details) if event.details else None,
                    event.source,
                    event.external_event_id,
                    event.occurred_at,
                    event.received_at,
                )
                events.append(event)
                for successor in resolve_next_nodes(node, event.outcome):
                    if successor.id is None:
                        raise ValueError(
                            "A successor node must have a specification ID"
                        )
                    task = NodeTask(
                        flow_id=event.flow_id,
                        node=successor,
                        node_execution_id=uuid.uuid5(event.id, str(successor.id)),
                        trigger=TriggerContext(
                            outcome_event_id=event.id,
                            outcome=event.outcome,
                            subject=event.subject,
                            originating_node_execution_id=event.node_execution_id,
                        ),
                        correlation_id=row["correlation_id"],
                        admission=self._decode(row["admission"]),
                        created_at=row["execution_created_at"],
                    )
                    successor_tasks.setdefault(task.node_execution_id, task)

            lifecycle = (
                TrackedOperationLifecycle.RESOLVED
                if reduction.operation_resolved
                else TrackedOperationLifecycle(row["lifecycle"])
            )
            failed = any(
                outcome.outcome in COMMON_OUTCOMES for outcome in reduction.outcomes
            )
            if not reduction.operation_resolved:
                state = NodeExecutionState.WAITING_FEEDBACK
            elif failed:
                state = NodeExecutionState.FAILED
            else:
                state = NodeExecutionState.RESOLVED
            await connection.execute(
                """
                    UPDATE tracked_operations
                    SET state = $2::jsonb, version = version + 1,
                        lifecycle = $3, updated_at = now()
                    WHERE id = $1
                    """,
                operation_id,
                self._json(reduction.state),
                lifecycle,
            )
            await connection.execute(
                """
                    UPDATE node_executions SET state = $2, updated_at = now()
                    WHERE _id = $1
                    """,
                row["execution_internal_id"],
                state,
            )
            return AppliedReduction(
                duplicate=False,
                outcomes=tuple(events),
                successor_tasks=tuple(successor_tasks.values()),
            )

    async def apply_execution_outcomes(
        self,
        task: NodeTask,
        outcomes: tuple,
        source: str,
    ) -> AppliedReduction:
        external_event_id = f"execution:{task.node_execution_id}"
        async with self.pool.acquire() as connection, connection.transaction():
            row = await self._register_execution(connection, task)
            duplicate = await connection.fetchval(
                """
                    SELECT EXISTS (
                        SELECT 1 FROM outcome_events
                        WHERE node_execution_id = $1
                          AND tracked_operation_id IS NULL
                          AND external_event_id = $2
                    )
                    """,
                row["_id"],
                external_event_id,
            )
            if duplicate:
                event_rows = await connection.fetch(
                    """
                    SELECT * FROM outcome_events
                    WHERE node_execution_id = $1
                      AND tracked_operation_id IS NULL
                      AND external_event_id = $2
                    ORDER BY received_at, _id
                    """,
                    row["_id"],
                    external_event_id,
                )
                duplicate_events = tuple(
                    OutcomeEvent(
                        id=event_row["id"],
                        flow_id=event_row["flow_id"],
                        node_execution_id=task.node_execution_id,
                        outcome=event_row["outcome"],
                        subject=(
                            OutcomeSubject.fromdict(self._decode(event_row["subject"]))
                            if event_row["subject"] is not None
                            else None
                        ),
                        details=(
                            dict(self._decode(event_row["details"]))
                            if event_row["details"] is not None
                            else None
                        ),
                        source=event_row["source"],
                        external_event_id=event_row["external_event_id"],
                        occurred_at=event_row["occurred_at"],
                        received_at=event_row["received_at"],
                    )
                    for event_row in event_rows
                )
                node = NodeTask.fromdict(
                    {
                        "flow_id": row["flow_id"],
                        "node_execution_id": task.node_execution_id,
                        "node": self._decode(row["node"]),
                    }
                ).node
                return AppliedReduction(
                    duplicate=True,
                    outcomes=duplicate_events,
                    successor_tasks=_successor_tasks(
                        node,
                        duplicate_events,
                        row["correlation_id"],
                        row["created_at"],
                        self._decode(row["admission"]),
                    ),
                )

            node = NodeTask.fromdict(
                {
                    "flow_id": row["flow_id"],
                    "node_execution_id": task.node_execution_id,
                    "node": self._decode(row["node"]),
                }
            ).node
            events = []
            successor_tasks: dict[UUID, NodeTask] = {}
            for emission in outcomes:
                event = OutcomeEvent(
                    flow_id=row["flow_id"],
                    node_execution_id=task.node_execution_id,
                    outcome=emission.outcome,
                    subject=emission.subject,
                    details=copy.deepcopy(emission.details),
                    source=source,
                    external_event_id=external_event_id,
                    occurred_at=emission.occurred_at,
                )
                await connection.execute(
                    """
                        INSERT INTO outcome_events (
                            id, flow_id, node_execution_id, tracked_operation_id,
                            outcome, subject, details, source, external_event_id,
                            occurred_at, received_at
                        ) VALUES (
                            $1, $2, $3, NULL, $4, $5::jsonb, $6::jsonb,
                            $7, $8, $9, $10
                        )
                        """,
                    event.id,
                    event.flow_id,
                    row["_id"],
                    event.outcome,
                    self._json(event.subject.dict()) if event.subject else None,
                    self._json(event.details) if event.details else None,
                    event.source,
                    event.external_event_id,
                    event.occurred_at,
                    event.received_at,
                )
                events.append(event)
                for successor in resolve_next_nodes(node, event.outcome):
                    if successor.id is None:
                        raise ValueError(
                            "A successor node must have a specification ID"
                        )
                    child_task = NodeTask(
                        flow_id=event.flow_id,
                        node=successor,
                        node_execution_id=uuid.uuid5(event.id, str(successor.id)),
                        trigger=TriggerContext(
                            outcome_event_id=event.id,
                            outcome=event.outcome,
                            subject=event.subject,
                            originating_node_execution_id=event.node_execution_id,
                        ),
                        correlation_id=row["correlation_id"],
                        admission=self._decode(row["admission"]),
                        created_at=row["created_at"],
                    )
                    successor_tasks.setdefault(child_task.node_execution_id, child_task)

            state = (
                NodeExecutionState.FAILED
                if any(event.outcome in COMMON_OUTCOMES for event in events)
                else NodeExecutionState.RESOLVED
            )
            await connection.execute(
                """
                    UPDATE node_executions
                    SET state = $2, updated_at = now()
                    WHERE _id = $1
                    """,
                row["_id"],
                state,
            )
            await connection.execute(
                """
                    UPDATE tracked_operations
                    SET lifecycle = 'resolved', updated_at = now()
                    WHERE node_execution_id = $1
                    """,
                row["_id"],
            )
            return AppliedReduction(
                False,
                tuple(events),
                successor_tasks=tuple(successor_tasks.values()),
            )

    async def replay_successors(self, task: NodeTask) -> tuple[NodeTask, ...]:
        async with self.pool.acquire() as connection:
            row = await connection.fetchrow(
                "SELECT node, correlation_id, admission, created_at "
                "FROM node_executions WHERE id = $1",
                task.node_execution_id,
            )
            event_rows = await connection.fetch(
                """
                SELECT * FROM outcome_events
                WHERE node_execution_id = (
                    SELECT _id FROM node_executions WHERE id = $1
                )
                ORDER BY received_at, _id
                """,
                task.node_execution_id,
            )
        node = NodeTask.fromdict(
            {
                "flow_id": task.flow_id,
                "node_execution_id": task.node_execution_id,
                "node": self._decode(row["node"]),
            }
        ).node
        events = tuple(
            OutcomeEvent(
                id=event_row["id"],
                flow_id=event_row["flow_id"],
                node_execution_id=task.node_execution_id,
                outcome=event_row["outcome"],
                subject=(
                    OutcomeSubject.fromdict(self._decode(event_row["subject"]))
                    if event_row["subject"] is not None
                    else None
                ),
                details=(
                    dict(self._decode(event_row["details"]))
                    if event_row["details"] is not None
                    else None
                ),
                source=event_row["source"],
                external_event_id=event_row["external_event_id"],
                occurred_at=event_row["occurred_at"],
                received_at=event_row["received_at"],
            )
            for event_row in event_rows
        )
        return _successor_tasks(
            node,
            events,
            row["correlation_id"],
            row["created_at"],
            self._decode(row["admission"]),
        )

    async def record_outcomes(
        self, task: NodeTask, outcomes: tuple, source: str
    ) -> AppliedReduction:
        return await self.apply_execution_outcomes(task, outcomes, source)
