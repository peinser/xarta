from __future__ import annotations

import asyncio
import datetime
import json
import os

from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from xarta.protocol.dag import Node
from xarta.protocol.dag import NodeTask
from xarta.protocol.dag import OutcomeEmission
from xarta.protocol.dag import TriggerContext
from xarta.protocol.dag.email import EmailNode
from xarta.tracking import AttemptDisposition
from xarta.tracking import DestinationBinding
from xarta.tracking import NodeExecutionIdentityConflictError
from xarta.tracking import PostgresTrackingStore
from xarta.tracking import Reduction
from xarta.tracking import VersionConflictError

pytestmark = pytest.mark.skipif(
    "TRACKING_POSTGRES_TEST_DSN" not in os.environ,
    reason="tracking PostgreSQL integration database is not configured",
)


@pytest_asyncio.fixture
async def store():
    pool = await asyncpg.create_pool(os.environ["TRACKING_POSTGRES_TEST_DSN"])
    async with pool.acquire() as connection:
        await connection.execute("""
            TRUNCATE provider_event_inbox, outcome_events, tracked_operations,
                execution_attempts, node_executions CASCADE
            """)
    yield PostgresTrackingStore(pool)
    await pool.close()


async def operation_for(store, node: Node):
    task = NodeTask(flow_id=uuid4(), node=node)
    await store.accept(task)
    operation = await store.create_operation(
        task.node_execution_id,
        node.kind,
        DestinationBinding("test", node.kind, "fake", "1"),
        {},
    )
    return task, operation


@pytest.mark.asyncio
async def test_postgres_duplicate_event_and_branch_activation_are_idempotent(
    store,
) -> None:
    successor = Node(kind="debug")
    task, operation = await operation_for(
        store, Node(kind="debug", on={"success": [successor, successor]})
    )
    reduction = Reduction({}, (OutcomeEmission("success"),), True)

    first, duplicate = await asyncio.gather(
        store.apply_reduction(
            operation.id, "fake", "provider-1", 0, reduction, "callback"
        ),
        store.apply_reduction(
            operation.id, "fake", "provider-1", 0, reduction, "callback"
        ),
    )

    applied = first if not first.duplicate else duplicate
    ignored = duplicate if not first.duplicate else first
    assert ignored.duplicate is True
    assert len(applied.outcomes) == 1
    assert applied.node_executions == ()
    assert len(applied.successor_tasks) == 1
    assert applied.successor_tasks[0].node.id == successor.id
    assert [task.node_execution_id for task in ignored.successor_tasks] == [
        task.node_execution_id for task in applied.successor_tasks
    ]

    async with store.pool.acquire() as connection:
        count = await connection.fetchval("SELECT count(*) FROM node_executions")
    assert count == 1


@pytest.mark.asyncio
async def test_postgres_tracked_child_accepts_unpersisted_synchronous_parent(
    store,
) -> None:
    flow_id = uuid4()
    parent_execution_id = uuid4()
    outcome_event_id = uuid4()
    child = EmailNode(
        to="recipient@example.com",
        sender="sender@example.com",
        body={},
    )
    task = NodeTask(
        flow_id=flow_id,
        node=child,
        trigger=TriggerContext(
            outcome_event_id=outcome_event_id,
            outcome="success",
            originating_node_execution_id=parent_execution_id,
        ),
    )

    await store.accept(task)

    async with store.pool.acquire() as connection:
        row = await connection.fetchrow(
            """
            SELECT trigger_outcome_event_id,
                   trigger_outcome_event_public_id,
                   trigger_originating_node_execution_id,
                   trigger_originating_node_execution_public_id
            FROM node_executions WHERE id = $1
            """,
            task.node_execution_id,
        )
        parent_count = await connection.fetchval(
            "SELECT count(*) FROM node_executions WHERE id = $1",
            parent_execution_id,
        )
        outcome_count = await connection.fetchval(
            "SELECT count(*) FROM outcome_events WHERE id = $1",
            outcome_event_id,
        )
    assert row["trigger_outcome_event_id"] is None
    assert row["trigger_outcome_event_public_id"] == outcome_event_id
    assert row["trigger_originating_node_execution_id"] is None
    assert row["trigger_originating_node_execution_public_id"] == parent_execution_id
    assert parent_count == 0
    assert outcome_count == 0


@pytest.mark.asyncio
async def test_postgres_execution_persists_payment_admission_provenance(store) -> None:
    admission = {
        "type": "x402",
        "network": "eip155:84532",
        "transaction": "0x" + "a" * 64,
        "asset": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
        "amount": "1250",
    }
    task = NodeTask(flow_id=uuid4(), node=Node(kind="debug"), admission=admission)

    await store.accept(task)

    async with store.pool.acquire() as connection:
        stored = await connection.fetchval(
            "SELECT admission FROM node_executions WHERE id = $1",
            task.node_execution_id,
        )
    assert (
        json.loads(stored) if isinstance(stored, str) else dict(stored)
    ) == admission


@pytest.mark.asyncio
async def test_postgres_optimistic_version_prevents_concurrent_corruption(
    store,
) -> None:
    _, operation = await operation_for(store, Node(kind="debug"))
    open_reduction = Reduction({}, (OutcomeEmission("success"),), False)
    await store.apply_reduction(
        operation.id, "fake", "first", 0, open_reduction, "callback"
    )

    with pytest.raises(VersionConflictError):
        await store.apply_reduction(
            operation.id, "fake", "second", 0, open_reduction, "callback"
        )


@pytest.mark.asyncio
async def test_postgres_callback_replay_preserves_task_correlation(store) -> None:
    correlation_id = uuid4()
    successor = Node(kind="debug")
    task = NodeTask(
        flow_id=uuid4(),
        correlation_id=correlation_id,
        node=Node(kind="debug", on={"success": [successor]}),
    )
    await store.accept(task)
    operation = await store.create_operation(
        task.node_execution_id,
        "debug",
        DestinationBinding("test", "debug", "fake", "1"),
        {},
    )
    reduction = Reduction({}, (OutcomeEmission("success"),), True)

    applied = await store.apply_reduction(
        operation.id, "fake", "provider-event", 0, reduction, "callback"
    )
    replayed = await store.apply_reduction(
        operation.id, "fake", "provider-event", 1, reduction, "callback"
    )

    assert applied.successor_tasks[0].correlation_id == correlation_id
    assert replayed.successor_tasks[0].dict() == applied.successor_tasks[0].dict()
    async with store.pool.acquire() as connection:
        stored = await connection.fetchval(
            "SELECT correlation_id FROM node_executions WHERE id = $1",
            task.node_execution_id,
        )
    assert stored == correlation_id


@pytest.mark.asyncio
async def test_postgres_execution_identity_collision_is_permanent(store) -> None:
    execution_id = uuid4()
    original = NodeTask(
        flow_id=uuid4(), node=Node(kind="debug"), node_execution_id=execution_id
    )
    await store.accept(original)
    await store.accept(NodeTask.fromdict(original.dict()))

    conflict = NodeTask(
        flow_id=original.flow_id,
        node=Node(kind="debug"),
        node_execution_id=execution_id,
    )
    with pytest.raises(NodeExecutionIdentityConflictError):
        await store.begin_attempt(conflict)


@pytest.mark.asyncio
async def test_postgres_operation_checkpoint_keeps_execution_active(store) -> None:
    task = NodeTask(flow_id=uuid4(), node=Node(kind="debug"))
    admission = await store.begin_attempt(task)
    operation = await store.create_operation(
        task.node_execution_id,
        "peppol",
        DestinationBinding("sender", "peppol", "provider", "v1"),
        {"provider_state": "creating"},
    )
    assert admission.disposition is AttemptDisposition.EXECUTE

    checkpoint = await store.checkpoint_operation(
        operation.id,
        operation.version,
        {"provider_state": "draft"},
        "doc_123",
    )

    assert checkpoint.version == operation.version + 1
    assert checkpoint.provider_reference == "doc_123"
    async with store.pool.acquire() as connection:
        state = await connection.fetchval(
            "SELECT state FROM node_executions WHERE id = $1", task.node_execution_id
        )
    assert state == "executing"


@pytest.mark.asyncio
async def test_postgres_provider_reference_is_unique_within_account(store) -> None:
    binding = DestinationBinding("internal", "peppol", "provider", "v1")
    operations = []
    for account in ("tenant-a", "tenant-b", "tenant-a"):
        task = NodeTask(flow_id=uuid4(), node=Node(kind="debug"))
        await store.accept(task)
        operations.append(
            await store.create_operation(
                task.node_execution_id, "peppol", binding, {}, account
            )
        )

    await store.checkpoint_operation(
        operations[0].id, operations[0].version, {}, "doc_123"
    )
    await store.checkpoint_operation(
        operations[1].id, operations[1].version, {}, "doc_123"
    )
    with pytest.raises(asyncpg.UniqueViolationError):
        await store.checkpoint_operation(
            operations[2].id, operations[2].version, {}, "doc_123"
        )


@pytest.mark.asyncio
async def test_postgres_reconciliation_claim_returns_public_ids(store) -> None:
    task = NodeTask(flow_id=uuid4(), node=Node(kind="debug"))
    await store.accept(task)
    operation = await store.create_operation(
        task.node_execution_id,
        "peppol",
        DestinationBinding("sender", "peppol", "provider", "v1"),
        {},
        "tenant-a",
    )
    await store.mark_waiting(operation.id, "document-1", {})

    claims = await store.claim_peppol_reconciliations(10, 30)

    assert len(claims) == 1
    assert claims[0].operation.id == operation.id
    assert claims[0].operation.node_execution_id == task.node_execution_id


@pytest.mark.asyncio
async def test_postgres_begin_attempt_returns_every_disposition_atomically(
    store,
) -> None:
    task = NodeTask(flow_id=uuid4(), node=Node(kind="debug"))

    first, concurrent = await asyncio.gather(
        store.begin_attempt(task), store.begin_attempt(task)
    )
    execute = first if first.disposition is AttemptDisposition.EXECUTE else concurrent
    waiting = concurrent if execute is first else first
    assert waiting.disposition is AttemptDisposition.ACTIVE_ATTEMPT
    assert execute.attempt is not None

    await store.complete_attempt(execute.attempt, {"retryable": True})
    retry = await store.begin_attempt(task)
    assert retry.disposition is AttemptDisposition.EXECUTE

    operation = await store.create_operation(
        task.node_execution_id,
        "smtp-like",
        DestinationBinding("mail", "smtp-like", "smtp-adapter", "v1"),
        {"status": "submitting"},
    )
    await store.mark_waiting(operation.id, "provider-reference", operation.state)
    feedback_wait = await store.begin_attempt(task)
    assert feedback_wait.disposition is AttemptDisposition.WAITING_FEEDBACK

    await store.apply_execution_outcomes(
        task, (OutcomeEmission("retry_exhausted"),), "retry"
    )
    terminal = await store.begin_attempt(task)
    assert terminal.disposition is AttemptDisposition.DUPLICATE_TERMINAL

    async with store.pool.acquire() as connection:
        row = await connection.fetchrow(
            """
            SELECT e.tracked_operation_id, o.adapter, o.lifecycle
            FROM outcome_events e
            JOIN tracked_operations o ON o.node_execution_id = e.node_execution_id
            JOIN node_executions n ON n._id = e.node_execution_id
            WHERE n.id = $1
            """,
            task.node_execution_id,
        )
    assert row["tracked_operation_id"] is None
    assert row["adapter"] == "smtp-adapter"
    assert row["lifecycle"] == "resolved"


@pytest.mark.asyncio
async def test_postgres_attempt_lease_reclaim_renewal_and_stale_cas(store) -> None:
    task = NodeTask(flow_id=uuid4(), node=Node(kind="debug"))
    first = await store.begin_attempt(task, lease_seconds=30)
    assert first.attempt is not None
    stale_token = first.attempt.lease_token

    assert (
        await store.begin_attempt(task, lease_seconds=30)
    ).disposition is AttemptDisposition.ACTIVE_ATTEMPT
    assert await store.renew_attempt_lease(first.attempt, lease_seconds=60) is True
    async with store.pool.acquire() as connection:
        await connection.execute(
            "UPDATE execution_attempts SET lease_until = $2 WHERE id = $1",
            first.attempt.id,
            datetime.datetime.now(datetime.UTC) - datetime.timedelta(seconds=1),
        )

    reclaimed = await store.begin_attempt(task, lease_seconds=30)
    assert reclaimed.attempt is not None
    assert reclaimed.attempt.number == 2
    assert reclaimed.attempt.lease_token != stale_token
    assert await store.complete_attempt(first.attempt) is False
    assert await store.complete_attempt(reclaimed.attempt) is True
