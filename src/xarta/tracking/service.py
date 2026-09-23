from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Any

from xarta.exceptions.protocol import TemporaryError
from xarta.logging import log_capability_lifecycle_change
from xarta.logging import logger
from xarta.logging import record_outcome_emitted
from xarta.tracking.models import AppliedReduction
from xarta.tracking.models import Reduction
from xarta.tracking.models import TrackedOperation
from xarta.tracking.models import TrackedOperationLifecycle
from xarta.tracking.store import InMemoryTrackingStore
from xarta.tracking.store import VersionConflictError

if TYPE_CHECKING:
    from collections.abc import Awaitable
    from collections.abc import Callable
    from collections.abc import Mapping
    from uuid import UUID

    from xarta.protocol.dag import NodeTask
    from xarta.tracking.destinations import DestinationRegistry


class AmbiguousSubmissionError(Exception):
    pass


class TrackedCapabilityService:
    def __init__(
        self,
        store: InMemoryTrackingStore,
        destinations: DestinationRegistry,
        publish_successors: Callable[[list[NodeTask]], Awaitable[None]] | None = None,
        log=None,
    ) -> None:
        self.store = store
        self.destinations = destinations
        self.publish_successors = publish_successors
        self.log = log or logger

    async def _log_operation_change(
        self,
        operation: TrackedOperation,
        *,
        action: str,
        previous_state: str | None,
        next_state: str,
        previous_version: int | None = None,
        next_version: int | None = None,
        source: str | None = None,
        duplicate: bool = False,
    ) -> None:
        context = await self.store.operation_logging_context(operation.id)
        await log_capability_lifecycle_change(
            self.log,
            **context,
            operation_id=operation.id,
            capability=operation.capability,
            adapter=operation.binding.adapter,
            configuration_revision=operation.binding.configuration_revision,
            execution_mode="tracked",
            lifecycle="tracked_operation",
            action=action,
            previous_state=previous_state,
            next_state=next_state,
            previous_version=previous_version,
            next_version=next_version,
            source=source,
            duplicate=duplicate,
        )

    async def prepare(
        self,
        task: NodeTask,
        capability: str,
        destination: str | None,
        initial_state: Mapping[str, Any],
        provider_account_reference: str | None = None,
    ) -> tuple[TrackedOperation, dict]:
        """Create or reload an operation and its pinned destination revision."""
        await self.store.accept(task)
        operation = await self.store.operation_for_execution(task.node_execution_id)
        if operation is None:
            resolved = self.destinations.resolve(capability, destination)
            operation = await self.store.create_operation(
                task.node_execution_id,
                capability,
                resolved.binding,
                initial_state,
                provider_account_reference,
            )
            await self._log_operation_change(
                operation,
                action="prepared",
                previous_state=None,
                next_state=operation.lifecycle.value,
                previous_version=None,
                next_version=operation.version,
            )
        resolved = self.destinations.resolve_binding(operation.binding)
        return operation, resolved.configuration

    async def checkpoint_operation(
        self,
        operation: TrackedOperation,
        state: Mapping[str, Any],
        provider_reference: str | None = None,
    ) -> TrackedOperation:
        previous_lifecycle = operation.lifecycle.value
        previous_version = operation.version
        updated = await self.store.checkpoint_operation(
            operation.id,
            previous_version,
            state,
            provider_reference,
        )
        await self._log_operation_change(
            updated,
            action="checkpointed",
            previous_state=previous_lifecycle,
            next_state=updated.lifecycle.value,
            previous_version=previous_version,
            next_version=updated.version,
        )
        return updated

    async def mark_waiting(
        self,
        operation_id: UUID,
        provider_reference: str | None,
        state: Mapping[str, Any],
        *,
        source: str | None = None,
    ) -> None:
        operation = await self.store.get_operation(operation_id)
        previous = operation.lifecycle.value
        await self.store.mark_waiting(operation_id, provider_reference, state)
        current = await self.store.get_operation(operation_id)
        if previous != current.lifecycle.value:
            await self._log_operation_change(
                current,
                action="waiting_for_feedback",
                previous_state=previous,
                next_state=current.lifecycle.value,
                previous_version=operation.version,
                next_version=current.version,
                source=source,
            )

    async def mark_uncertain(
        self, operation_id: UUID, *, source: str | None = None
    ) -> None:
        operation = await self.store.get_operation(operation_id)
        previous = operation.lifecycle.value
        await self.store.mark_uncertain(operation_id)
        current = await self.store.get_operation(operation_id)
        await self._log_operation_change(
            current,
            action="marked_uncertain",
            previous_state=previous,
            next_state=current.lifecycle.value,
            previous_version=operation.version,
            next_version=current.version,
            source=source,
        )

    async def start(
        self,
        task: NodeTask,
        capability: str,
        destination: str | None,
        initial_state: Mapping[str, Any],
        submit: Callable[
            [TrackedOperation, dict], Awaitable[tuple[str | None, Mapping[str, Any]]]
        ],
        ambiguous_side_effect: bool = False,
        provider_account_reference: str | None = None,
    ) -> TrackedOperation:
        await self.store.accept(task)
        existing = await self.store.operation_for_execution(task.node_execution_id)
        if (
            existing is not None
            and existing.lifecycle is TrackedOperationLifecycle.RESOLVED
        ):
            return existing
        if existing is not None and (
            existing.provider_reference is not None
            or existing.lifecycle is TrackedOperationLifecycle.UNCERTAIN
        ):
            # The operation owns its original binding; retries never re-resolve it.
            return existing
        if existing is not None:
            resolved = self.destinations.resolve_binding(existing.binding)
            operation = existing
        else:
            resolved = self.destinations.resolve(capability, destination)
            operation = await self.store.create_operation(
                task.node_execution_id,
                capability,
                resolved.binding,
                initial_state,
                provider_account_reference,
            )
            # A concurrent creator may have won with another valid revision.
            resolved = self.destinations.resolve_binding(operation.binding)
        if operation.provider_reference is not None:
            return operation
        if ambiguous_side_effect:
            # Non-idempotent providers must be uncertain before the side effect.
            await self.mark_uncertain(operation.id, source="submission")
        try:
            provider_reference, state = await submit(operation, resolved.configuration)
        except AmbiguousSubmissionError:
            await self.mark_uncertain(operation.id, source="submission")
            return await self.store.get_operation(operation.id)
        await self.mark_waiting(
            operation.id, provider_reference, state, source="submission"
        )
        return await self.store.get_operation(operation.id)

    async def feedback(
        self,
        operation_id: UUID,
        adapter: str,
        external_event_id: str,
        update: Any,
        reduce: Callable[[Mapping[str, Any], Any], Reduction],
        source: str,
        transaction_mutation: Callable[[Any, Any], Awaitable[None]] | None = None,
    ) -> AppliedReduction:
        for _ in range(3):
            operation = await self.store.get_operation(operation_id)
            if adapter != operation.binding.adapter:
                raise ValueError("Feedback adapter does not match pinned operation")
            reduction = (
                Reduction(
                    state=operation.state,
                    outcomes=(),
                    operation_resolved=True,
                )
                if operation.lifecycle is TrackedOperationLifecycle.RESOLVED
                else reduce(operation.state, update)
            )
            previous_lifecycle = operation.lifecycle.value
            previous_version = operation.version
            try:
                applied = await self.store.apply_reduction(
                    operation_id,
                    adapter,
                    external_event_id,
                    operation.version,
                    reduction,
                    source,
                    transaction_mutation,
                )
                if not applied.duplicate:
                    for outcome in applied.outcomes:
                        record_outcome_emitted(outcome)
                    await self._log_operation_change(
                        operation,
                        action="feedback_applied",
                        previous_state=previous_lifecycle,
                        next_state=(
                            TrackedOperationLifecycle.RESOLVED.value
                            if reduction.operation_resolved
                            else operation.lifecycle.value
                        ),
                        previous_version=previous_version,
                        next_version=previous_version + 1,
                        source=source,
                    )
                if applied.successor_tasks and self.publish_successors is not None:
                    try:
                        await self.publish_successors(list(applied.successor_tasks))
                    except Exception as ex:
                        raise TemporaryError(
                            "Could not publish capability successors", delay=1
                        ) from ex
                return applied
            except VersionConflictError:
                continue
        raise VersionConflictError("Could not apply concurrent capability update")
