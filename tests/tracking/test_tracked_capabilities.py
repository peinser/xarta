from __future__ import annotations

import asyncio
import copy
import datetime

from pathlib import Path
from uuid import UUID
from uuid import uuid4

import orjson
import pytest

from xarta.exceptions.protocol import TemporaryError
from xarta.protocol.dag import Node
from xarta.protocol.dag import NodeTask
from xarta.protocol.dag import OutcomeEmission
from xarta.protocol.dag import OutcomeSubject
from xarta.protocol.dag import TriggerContext
from xarta.protocol.dag.email import EmailNode
from xarta.protocol.dag.peppol import PeppolNode
from xarta.protocol.document.request.flow import DocumentFlowRequest
from xarta.services.v1.email.tracking import EmailRecipientStatus
from xarta.services.v1.email.tracking import EmailUpdate
from xarta.services.v1.email.tracking import initial_email_state
from xarta.services.v1.email.tracking import reduce_email
from xarta.services.v1.email.tracking import reduce_smtp_submission
from xarta.tracking import AmbiguousSubmissionError
from xarta.tracking import AttemptDisposition
from xarta.tracking import DestinationBinding
from xarta.tracking import DestinationRegistry
from xarta.tracking import InMemoryTrackingStore
from xarta.tracking import NodeExecutionIdentityConflictError
from xarta.tracking import NodeExecutionState
from xarta.tracking import Reduction
from xarta.tracking import TrackedCapabilityService
from xarta.tracking import TrackedOperationLifecycle
from xarta.tracking import VersionConflictError

EXAMPLES = Path(__file__).parents[2] / "examples" / "protocol"


def load(name: str) -> dict:
    return orjson.loads((EXAMPLES / name).read_bytes())


def registry() -> DestinationRegistry:
    return DestinationRegistry(
        {
            "transactional-email": {
                "kind": "email",
                "adapter": "fake-email",
                "revision": "email-v1",
            },
            "archive-abc123": {
                "kind": "archive",
                "adapter": "archive-adapter-a",
                "revision": "archive-a-v1",
            },
        }
    )


class RecordingLog:
    def __init__(self) -> None:
        self.events = []

    async def ainfo(self, event, **fields) -> None:
        self.events.append((event, fields))


@pytest.mark.asyncio
async def test_tracked_lifecycle_logs_correlation_without_operation_state() -> None:
    store = InMemoryTrackingStore()
    log = RecordingLog()
    service = TrackedCapabilityService(store, registry(), log=log)
    task = NodeTask(
        flow_id=uuid4(),
        correlation_id=uuid4(),
        node=Node(kind="email"),
    )

    operation, _ = await service.prepare(
        task,
        "email",
        "transactional-email",
        {"recipient": "sensitive@example.com"},
    )
    operation = await service.checkpoint_operation(
        operation,
        {"recipient": "sensitive@example.com", "provider_payload": "secret"},
    )
    await service.mark_uncertain(operation.id, source="submission")
    await service.feedback(
        operation.id,
        operation.binding.adapter,
        "provider-sensitive-event",
        {"provider_payload": "secret"},
        lambda state, update: Reduction(
            state={**state, **update},
            outcomes=(OutcomeEmission("delivered"),),
            operation_resolved=True,
        ),
        "callback",
    )

    lifecycle = [
        fields
        for event, fields in log.events
        if event == "capability_lifecycle_state_changed"
    ]
    assert [(event["previous_state"], event["next_state"]) for event in lifecycle] == [
        (None, "open"),
        ("open", "open"),
        ("open", "uncertain"),
        ("uncertain", "resolved"),
    ]
    assert [
        (event["previous_version"], event["next_version"]) for event in lifecycle
    ] == [(None, 0), (0, 1), (1, 1), (1, 2)]
    assert all(event["flow_id"] == str(task.flow_id) for event in lifecycle)
    assert all(
        event["correlation_id"] == str(task.correlation_id) for event in lifecycle
    )
    encoded = orjson.dumps(lifecycle)
    assert b"sensitive@example.com" not in encoded
    assert b"provider-sensitive-event" not in encoded
    assert b"provider_payload" not in encoded


@pytest.mark.asyncio
async def test_provider_reference_identity_is_scoped_by_provider_account() -> None:
    store = InMemoryTrackingStore()
    binding = DestinationBinding("internal", "peppol", "provider", "v1")
    operations = []
    for account in ("tenant-a", "tenant-b", "tenant-a"):
        task = NodeTask(uuid4(), Node(kind="debug"))
        await store.accept(task)
        operations.append(
            await store.create_operation(
                task.node_execution_id,
                "peppol",
                binding,
                {},
                account,
            )
        )

    await store.checkpoint_operation(
        operations[0].id, operations[0].version, {}, "doc_123"
    )
    await store.checkpoint_operation(
        operations[1].id, operations[1].version, {}, "doc_123"
    )
    with pytest.raises(ValueError, match="already tracked"):
        await store.checkpoint_operation(
            operations[2].id, operations[2].version, {}, "doc_123"
        )


async def start_email(store, request, publisher=None):
    task = NodeTask(flow_id=request.id, node=request.dag)
    service = TrackedCapabilityService(store, registry(), publisher)

    async def submit(operation, configuration):
        return "provider-email-1", initial_email_state(
            [request.dag.to, *request.dag.cc, *request.dag.bcc]
        )

    operation = await service.start(
        task,
        "email",
        request.dag.destination,
        initial_email_state([request.dag.to, *request.dag.cc, *request.dag.bcc]),
        submit,
    )
    return task, service, operation


@pytest.mark.asyncio
async def test_executable_email_fixture_and_duplicate_feedback() -> None:
    request = DocumentFlowRequest.fromdict(load("email-tracked.json"))
    expected = load("email-tracked-events.json")["steps"]
    store = InMemoryTrackingStore()
    task, service, operation = await start_email(store, request)

    assert (
        store.node_executions[task.node_execution_id].state
        == NodeExecutionState.WAITING_FEEDBACK
    )
    assert await store.flow_settled(task.flow_id) is False
    assert expected[0]["expected_scheduled_node_ids"] == []

    alice = await service.feedback(
        operation.id,
        "fake-email",
        "email-event-001",
        EmailUpdate("alice@example.com", EmailRecipientStatus.DELIVERED),
        reduce_email,
        "callback",
    )
    assert [event.outcome for event in alice.outcomes] == ["delivered"]
    assert alice.outcomes[0].subject == OutcomeSubject("recipient", "alice@example.com")
    assert (
        store.node_executions[task.node_execution_id].state
        == NodeExecutionState.WAITING_FEEDBACK
    )

    bob = await service.feedback(
        operation.id,
        "fake-email",
        "email-event-002",
        EmailUpdate("bob@example.com", EmailRecipientStatus.MAILBOX_FULL),
        reduce_email,
        "callback",
    )
    assert [event.outcome for event in bob.outcomes] == [
        "mailbox_full",
        "partially_delivered",
    ]
    assert {child.node.id for child in bob.successor_tasks} == {
        UUID(value) for value in expected[2]["expected_scheduled_node_ids"]
    }
    assert (
        store.node_executions[task.node_execution_id].state
        == NodeExecutionState.RESOLVED
    )
    # Synchronous children have no PostgreSQL-style in-memory execution state.
    assert await store.flow_settled(task.flow_id) is True

    duplicate = await service.feedback(
        operation.id,
        "fake-email",
        "email-event-002",
        EmailUpdate("bob@example.com", EmailRecipientStatus.MAILBOX_FULL),
        reduce_email,
        "callback",
    )
    assert duplicate.duplicate is True
    assert [event.id for event in duplicate.outcomes] == [
        event.id for event in bob.outcomes
    ]
    assert duplicate.node_executions == ()
    assert [child.node_execution_id for child in duplicate.successor_tasks] == [
        child.node_execution_id for child in bob.successor_tasks
    ]


@pytest.mark.asyncio
async def test_two_recipient_events_activate_same_node_twice() -> None:
    fallback = Node(kind="debug")
    email = EmailNode(
        to="bob@example.com",
        cc=["carol@example.com"],
        sender="sender@example.com",
        body={},
        on={"mailbox_full": [fallback]},
    )
    task = NodeTask(flow_id=uuid4(), node=email)
    store = InMemoryTrackingStore()
    service = TrackedCapabilityService(store, registry())

    async def submit(operation, configuration):
        return "provider-2", initial_email_state(
            ["bob@example.com", "carol@example.com"]
        )

    operation = await service.start(
        task,
        "email",
        "transactional-email",
        initial_email_state(["bob@example.com", "carol@example.com"]),
        submit,
    )
    bob = await service.feedback(
        operation.id,
        "fake-email",
        "bob",
        EmailUpdate("bob@example.com", EmailRecipientStatus.MAILBOX_FULL),
        reduce_email,
        "callback",
    )
    carol = await service.feedback(
        operation.id,
        "fake-email",
        "carol",
        EmailUpdate("carol@example.com", EmailRecipientStatus.MAILBOX_FULL),
        reduce_email,
        "callback",
    )

    tasks = [*bob.successor_tasks, *carol.successor_tasks]
    assert [child.node.id for child in tasks] == [fallback.id, fallback.id]
    assert tasks[0].node_execution_id != tasks[1].node_execution_id
    assert tasks[0].trigger.subject.id == "bob@example.com"
    assert tasks[1].trigger.subject.id == "carol@example.com"


@pytest.mark.asyncio
async def test_one_outcome_event_schedules_each_successor_only_once() -> None:
    successor = Node(kind="debug")
    root = Node(kind="debug", on={"success": [successor, successor]})
    task = NodeTask(flow_id=uuid4(), node=root)
    store = InMemoryTrackingStore()

    applied = await store.record_outcomes(task, (OutcomeEmission("success"),), "test")

    assert len(applied.outcomes) == 1
    assert applied.node_executions == ()
    assert len(applied.successor_tasks) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "details"),
    [
        (
            "mailbox_full",
            {
                "smtp_code": 550,
                "enhanced_status_code": "5.2.2",
                "message": "Mailbox full",
            },
        ),
        (
            "delivery_failed",
            {
                "provider": "e-invoice.be",
                "provider_state": "FAILED",
                "provider_code": "PROVIDER_NATIVE_CODE",
            },
        ),
    ],
)
async def test_outcome_details_are_snapshotted_and_routing_uses_only_outcome(
    outcome, details
) -> None:
    successor = Node(kind="debug")
    node = (
        EmailNode(
            to="recipient@example.com",
            sender="sender@example.com",
            body={},
            on={outcome: [successor]},
        )
        if outcome == "mailbox_full"
        else PeppolNode(
            document={"source": "archive", "id": str(uuid4())},
            on={outcome: [successor]},
        )
    )
    task = NodeTask(flow_id=uuid4(), node=node)
    store = InMemoryTrackingStore()

    applied = await store.record_outcomes(
        task, (OutcomeEmission(outcome, details=details),), "generic-details-test"
    )
    details["mutated_after_persistence"] = True

    assert applied.outcomes[0].details is not None
    assert "mutated_after_persistence" not in applied.outcomes[0].details
    assert applied.successor_tasks[0].node.id == successor.id


@pytest.mark.asyncio
async def test_attempt_admission_serializes_execution_and_allows_completed_retry() -> (
    None
):
    task = NodeTask(flow_id=uuid4(), node=Node(kind="debug"))
    store = InMemoryTrackingStore()

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
    assert retry.attempt is not None
    assert retry.attempt.number == 2


@pytest.mark.asyncio
async def test_attempt_lease_expiry_reclaims_and_rejects_stale_completion() -> None:
    task = NodeTask(flow_id=uuid4(), node=Node(kind="debug"))
    store = InMemoryTrackingStore()
    first = await store.begin_attempt(task, lease_seconds=30)
    assert first.attempt is not None
    stale_owner = copy.copy(first.attempt)

    waiting = await store.begin_attempt(task, lease_seconds=30)
    assert waiting.disposition is AttemptDisposition.ACTIVE_ATTEMPT
    first.attempt.lease_until = datetime.datetime.now(
        datetime.UTC
    ) - datetime.timedelta(seconds=1)
    assert await store.complete_attempt(stale_owner) is False

    reclaimed = await store.begin_attempt(task, lease_seconds=30)
    assert reclaimed.disposition is AttemptDisposition.EXECUTE
    assert reclaimed.attempt is not None
    assert reclaimed.attempt.number == 2
    assert reclaimed.lease_token != first.lease_token
    assert await store.complete_attempt(stale_owner) is False
    assert await store.complete_attempt(reclaimed.attempt) is True
    assert reclaimed.attempt.lease_token is None
    assert reclaimed.attempt.lease_until is None


@pytest.mark.asyncio
async def test_attempt_lease_renewal_extends_only_current_owner() -> None:
    task = NodeTask(flow_id=uuid4(), node=Node(kind="debug"))
    store = InMemoryTrackingStore()
    admission = await store.begin_attempt(task, lease_seconds=0.05)
    assert admission.attempt is not None
    original_until = admission.attempt.lease_until
    assert original_until is not None

    await asyncio.sleep(0.01)
    assert await store.renew_attempt_lease(admission.attempt, lease_seconds=30) is True
    assert admission.attempt.lease_until is not None
    assert admission.attempt.lease_until > original_until
    assert (
        await store.begin_attempt(task, lease_seconds=30)
    ).disposition is AttemptDisposition.ACTIVE_ATTEMPT


@pytest.mark.asyncio
async def test_node_execution_identity_is_immutable_on_redelivery() -> None:
    execution_id = uuid4()
    original = NodeTask(
        flow_id=uuid4(), node=Node(kind="debug"), node_execution_id=execution_id
    )
    store = InMemoryTrackingStore()
    await store.accept(original)

    identical = NodeTask.fromdict(original.dict())
    assert await store.accept(identical) is store.node_executions[execution_id]

    conflicts = [
        NodeTask(
            flow_id=uuid4(),
            node=original.node,
            node_execution_id=execution_id,
        ),
        NodeTask(
            flow_id=original.flow_id,
            node=Node(kind="debug"),
            node_execution_id=execution_id,
        ),
        NodeTask(
            flow_id=original.flow_id,
            node=Node(kind="webhook", id=original.node.id),
            node_execution_id=execution_id,
        ),
    ]
    for conflict in conflicts:
        with pytest.raises(NodeExecutionIdentityConflictError):
            await store.begin_attempt(conflict)


@pytest.mark.asyncio
async def test_node_execution_trigger_provenance_is_immutable() -> None:
    execution_id = uuid4()
    node = Node(kind="debug")
    trigger = TriggerContext(uuid4(), "success", uuid4(), OutcomeSubject("item", "1"))
    original = NodeTask(uuid4(), node, execution_id, trigger)
    store = InMemoryTrackingStore()
    await store.accept(original)

    changed = NodeTask(
        original.flow_id,
        node,
        execution_id,
        TriggerContext(
            trigger.outcome_event_id,
            "failure",
            trigger.originating_node_execution_id,
            trigger.subject,
        ),
    )
    with pytest.raises(NodeExecutionIdentityConflictError):
        await store.begin_attempt(changed)


@pytest.mark.asyncio
async def test_operation_checkpoint_preserves_active_execution_and_versions() -> None:
    task = NodeTask(flow_id=uuid4(), node=Node(kind="debug"))
    store = InMemoryTrackingStore()
    admission = await store.begin_attempt(task)
    assert admission.disposition is AttemptDisposition.EXECUTE
    operation = await store.create_operation(
        task.node_execution_id,
        "peppol",
        DestinationBinding("sender", "peppol", "provider", "v1"),
        {"provider_state": "creating"},
    )

    checkpoint = await store.checkpoint_operation(
        operation.id,
        operation.version,
        {"provider_state": "draft", "provider_document_id": "doc_123"},
        "doc_123",
    )

    assert checkpoint.version == 1
    assert checkpoint.provider_reference == "doc_123"
    assert checkpoint.lifecycle is TrackedOperationLifecycle.OPEN
    assert (
        store.node_executions[task.node_execution_id].state
        is NodeExecutionState.EXECUTING
    )
    assert store.outcomes == {}
    with pytest.raises(VersionConflictError):
        await store.checkpoint_operation(
            operation.id,
            0,
            {"provider_state": "wrong"},
        )


@pytest.mark.asyncio
async def test_execution_outcome_coexists_with_real_tracked_operation() -> None:
    successor = Node(kind="debug")
    task = NodeTask(
        flow_id=uuid4(),
        node=Node(kind="debug", on={"retry_exhausted": [successor]}),
    )
    store = InMemoryTrackingStore()
    await store.accept(task)
    operation = await store.create_operation(
        task.node_execution_id,
        "smtp-like",
        DestinationBinding("mail", "smtp-like", "smtp-adapter", "v1"),
        {"status": "submitting"},
    )

    first = await store.apply_execution_outcomes(
        task, (OutcomeEmission("retry_exhausted"),), "retry"
    )
    duplicate = await store.apply_execution_outcomes(
        task, (OutcomeEmission("retry_exhausted"),), "retry"
    )

    assert duplicate.duplicate is True
    assert len(first.outcomes) == 1
    assert first.outcomes[0].tracked_operation_id is None
    assert first.node_executions == ()
    assert first.successor_tasks[0].node.id == successor.id
    assert store.operations[operation.id].binding.adapter == "smtp-adapter"
    assert (
        store.operations[operation.id].lifecycle is TrackedOperationLifecycle.RESOLVED
    )


@pytest.mark.asyncio
async def test_successor_publication_retry_does_not_rerun_reducer() -> None:
    request = DocumentFlowRequest.fromdict(load("email-tracked.json"))
    store = InMemoryTrackingStore()
    fail_publication = True
    published = []

    async def publisher(tasks):
        if fail_publication:
            raise RuntimeError("NATS unavailable")
        published.extend(tasks)

    task, service, operation = await start_email(store, request, publisher)
    calls = 0

    def reducer(state, update):
        nonlocal calls
        calls += 1
        return reduce_email(state, update)

    await service.feedback(
        operation.id,
        "fake-email",
        "event-1",
        EmailUpdate("alice@example.com", EmailRecipientStatus.DELIVERED),
        reducer,
        "callback",
    )
    with pytest.raises(TemporaryError):
        await service.feedback(
            operation.id,
            "fake-email",
            "event-2",
            EmailUpdate("bob@example.com", EmailRecipientStatus.MAILBOX_FULL),
            reducer,
            "callback",
        )

    fail_publication = False
    duplicate = await service.feedback(
        operation.id,
        "fake-email",
        "event-2",
        EmailUpdate("bob@example.com", EmailRecipientStatus.MAILBOX_FULL),
        reducer,
        "callback",
    )
    assert calls == 2
    assert duplicate.duplicate is True
    assert published


@pytest.mark.asyncio
async def test_destination_binding_is_pinned_and_ambiguous_submission_is_uncertain() -> (
    None
):
    archive = DocumentFlowRequest.fromdict(load("archive-destination.json"))
    archive_expected = load("archive-destination-events.json")
    store = InMemoryTrackingStore()
    service = TrackedCapabilityService(store, registry())
    task = NodeTask(flow_id=archive.id, node=archive.dag)

    async def submit(operation, configuration):
        return f"archive-ref-{operation.id}", {"status": "submitted"}

    operation = await service.start(
        task, "archive", archive.dag.destination, {}, submit
    )
    first_binding = operation.binding
    service.destinations = DestinationRegistry(
        {
            "archive-abc123": {
                "kind": "archive",
                "adapter": "archive-adapter-b",
                "revision": "archive-b-v2",
            }
        }
    )
    retried = await service.start(task, "archive", archive.dag.destination, {}, submit)
    assert retried.binding == first_binding
    assert retried.binding.adapter == "archive-adapter-a"
    assert (
        archive_expected["steps"][0]["expected_binding"]["adapter"]
        == retried.binding.adapter
    )

    uncertain_task = NodeTask(flow_id=uuid4(), node=archive.dag)

    async def uncertain(operation, configuration):
        raise AmbiguousSubmissionError

    uncertain_operation = await TrackedCapabilityService(store, registry()).start(
        uncertain_task, "archive", archive.dag.destination, {}, uncertain
    )
    assert uncertain_operation.lifecycle == TrackedOperationLifecycle.UNCERTAIN

    conservative_task = NodeTask(flow_id=uuid4(), node=archive.dag)
    conservative = await TrackedCapabilityService(store, registry()).start(
        conservative_task,
        "archive",
        archive.dag.destination,
        {},
        submit,
        ambiguous_side_effect=True,
    )
    assert conservative.lifecycle == TrackedOperationLifecycle.OPEN

    pending_task = NodeTask(flow_id=uuid4(), node=archive.dag)
    pending_service = TrackedCapabilityService(store, registry())

    async def unavailable(operation, configuration):
        raise RuntimeError("provider unavailable before submission")

    with pytest.raises(RuntimeError):
        await pending_service.start(
            pending_task, "archive", archive.dag.destination, {}, unavailable
        )
    pending_service.destinations = DestinationRegistry(
        {
            "archive-abc123": {
                "kind": "archive",
                "adapter": "archive-adapter-b",
                "revision": "archive-b-v2",
            }
        }
    )
    with pytest.raises(ValueError, match="Pinned destination"):
        await pending_service.start(
            pending_task, "archive", archive.dag.destination, {}, submit
        )


@pytest.mark.asyncio
async def test_destination_revision_history_keeps_retry_on_pinned_configuration() -> (
    None
):
    node = Node(kind="debug")
    task = NodeTask(flow_id=uuid4(), node=node)
    store = InMemoryTrackingStore()
    first_registry = DestinationRegistry(
        {
            "partner": {
                "kind": "debug",
                "adapter": "fake",
                "current_revision": "v1",
                "revisions": {
                    "v1": {"endpoint": "first.example.test"},
                    "v2": {"endpoint": "second.example.test"},
                },
            }
        }
    )
    service = TrackedCapabilityService(store, first_registry)

    async def unavailable(operation, configuration):
        raise RuntimeError("retry later")

    with pytest.raises(RuntimeError):
        await service.start(task, "debug", "partner", {}, unavailable)

    service.destinations = DestinationRegistry(
        {
            "partner": {
                "kind": "debug",
                "adapter": "fake",
                "current_revision": "v2",
                "revisions": {
                    "v1": {"endpoint": "first.example.test"},
                    "v2": {"endpoint": "second.example.test"},
                },
            }
        }
    )
    used_configuration = None

    async def submit(operation, configuration):
        nonlocal used_configuration
        used_configuration = configuration
        return "provider-reference", {}

    operation = await service.start(task, "debug", "partner", {}, submit)

    assert operation.binding.configuration_revision == "v1"
    assert used_configuration["endpoint"] == "first.example.test"


@pytest.mark.asyncio
async def test_concurrent_and_out_of_order_updates_are_deterministic() -> None:
    request = DocumentFlowRequest.fromdict(load("email-tracked.json"))
    store = InMemoryTrackingStore()
    _, service, operation = await start_email(store, request)
    await asyncio.gather(
        service.feedback(
            operation.id,
            "fake-email",
            "e2",
            EmailUpdate(
                "bob@example.com",
                EmailRecipientStatus.MAILBOX_FULL,
                provider_sequence=2,
            ),
            reduce_email,
            "callback",
        ),
        service.feedback(
            operation.id,
            "fake-email",
            "e1",
            EmailUpdate(
                "alice@example.com", EmailRecipientStatus.DELIVERED, provider_sequence=2
            ),
            reduce_email,
            "callback",
        ),
    )
    stale = await service.feedback(
        operation.id,
        "fake-email",
        "stale",
        EmailUpdate(
            "alice@example.com", EmailRecipientStatus.ACCEPTED, provider_sequence=1
        ),
        reduce_email,
        "callback",
    )
    assert stale.outcomes == ()
    assert (
        store.operations[operation.id].state["recipients"]["alice@example.com"]
        == "delivered"
    )


def test_email_initial_state_tracks_to_cc_and_bcc_independently() -> None:
    state = initial_email_state(["to@example.com", "cc@example.com", "bcc@example.com"])

    assert state["recipients"] == {
        "to@example.com": "pending",
        "cc@example.com": "pending",
        "bcc@example.com": "pending",
    }


def test_email_same_status_advances_sequence_and_pending_feedback_is_invalid() -> None:
    state = initial_email_state(["alice@example.com"])
    accepted = reduce_email(
        state,
        EmailUpdate(
            "alice@example.com", EmailRecipientStatus.ACCEPTED, provider_sequence=3
        ),
    )
    repeated = reduce_email(
        accepted.state,
        EmailUpdate(
            "alice@example.com", EmailRecipientStatus.ACCEPTED, provider_sequence=4
        ),
    )

    assert repeated.state["provider_sequences"]["alice@example.com"] == 4
    with pytest.raises(ValueError, match="pending is internal"):
        reduce_email(
            repeated.state,
            EmailUpdate(
                "alice@example.com", EmailRecipientStatus.PENDING, provider_sequence=5
            ),
        )


def test_smtp_partial_acceptance_preserves_each_recipient() -> None:
    state = initial_email_state(["alice@example.com", "bob@example.com"])
    reduction = reduce_smtp_submission(
        state,
        (
            OutcomeEmission(
                "accepted", OutcomeSubject("recipient", "alice@example.com")
            ),
            OutcomeEmission(
                "mailbox_full", OutcomeSubject("recipient", "bob@example.com")
            ),
        ),
    )

    assert reduction.state["recipients"] == {
        "alice@example.com": "accepted",
        "bob@example.com": "mailbox_full",
    }
    assert [outcome.outcome for outcome in reduction.outcomes] == [
        "accepted",
        "mailbox_full",
        "partially_accepted",
    ]
    assert reduction.operation_resolved is True


@pytest.mark.asyncio
async def test_feedback_is_bound_to_adapter_and_terminal_operation_is_immutable() -> (
    None
):
    request = DocumentFlowRequest.fromdict(load("email-tracked.json"))
    store = InMemoryTrackingStore()
    _, service, operation = await start_email(store, request)

    with pytest.raises(ValueError, match="pinned operation"):
        await service.feedback(
            operation.id,
            "different-adapter",
            "wrong-adapter",
            EmailUpdate("alice@example.com", EmailRecipientStatus.DELIVERED),
            reduce_email,
            "callback",
        )

    await service.feedback(
        operation.id,
        "fake-email",
        "alice",
        EmailUpdate("alice@example.com", EmailRecipientStatus.DELIVERED),
        reduce_email,
        "callback",
    )
    await service.feedback(
        operation.id,
        "fake-email",
        "bob",
        EmailUpdate("bob@example.com", EmailRecipientStatus.MAILBOX_FULL),
        reduce_email,
        "callback",
    )
    version = store.operations[operation.id].version
    late = await service.feedback(
        operation.id,
        "fake-email",
        "late",
        EmailUpdate("bob@example.com", EmailRecipientStatus.DELIVERED),
        reduce_email,
        "callback",
    )
    assert late.duplicate is True
    assert store.operations[operation.id].version == version


@pytest.mark.asyncio
async def test_reduction_transaction_mutation_runs_once_after_deduplication() -> None:
    request = DocumentFlowRequest.fromdict(load("email-tracked.json"))
    store = InMemoryTrackingStore()
    _, service, operation = await start_email(store, request)
    mutations = []

    async def mutation(connection, context) -> None:
        mutations.append((connection, context))

    def reducer(state, update) -> Reduction:
        return Reduction(
            state={**state, "phase": "observed"}, outcomes=(), operation_resolved=False
        )

    first = await service.feedback(
        operation.id,
        "fake-email",
        "transaction-mutation",
        None,
        reducer,
        "test",
        transaction_mutation=mutation,
    )
    replay = await service.feedback(
        operation.id,
        "fake-email",
        "transaction-mutation",
        None,
        reducer,
        "test",
        transaction_mutation=mutation,
    )

    assert not first.duplicate
    assert replay.duplicate
    assert len(mutations) == 1
    assert mutations[0][0] is None
    assert mutations[0][1].operation_id == operation.id
