from __future__ import annotations

import asyncio

from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import Mock
from uuid import uuid4

import orjson
import pytest

from xarta.exceptions.protocol import PermanentError
from xarta.exceptions.protocol import TemporaryError
from xarta.execution import ExecutionMode
from xarta.nats.sanic import NATSRuntime
from xarta.nats.sanic import SanicNATSConsumerModel
from xarta.nats.sanic import SanicNATSModel
from xarta.nats.sanic import SanicNATSRequestsConsumerModel
from xarta.nats.sanic import SanicNATSSynchronousRequestsConsumerModel
from xarta.protocol.dag import CapabilityResult
from xarta.protocol.dag import Node
from xarta.protocol.dag import NodeTask
from xarta.protocol.dag import OutcomeEmission
from xarta.protocol.dag.email import EmailNode
from xarta.tracking import AttemptDisposition
from xarta.tracking import DestinationBinding
from xarta.tracking import DestinationRegistry
from xarta.tracking import InMemoryTrackingStore
from xarta.tracking import NodeExecutionState
from xarta.tracking import TrackedCapabilityService


class FakeSubscription:
    def __init__(self, runtime: NATSRuntime, message: SimpleNamespace) -> None:
        self.runtime = runtime
        self.message = message

    async def fetch(self, *_args, **_kwargs):
        self.runtime.stopping.set()
        return [self.message]


def message() -> SimpleNamespace:
    return SimpleNamespace(
        ack=AsyncMock(),
        in_progress=AsyncMock(),
        nak=AsyncMock(),
        term=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_temporary_handler_failure_naks_message() -> None:
    msg = message()
    runtime = NATSRuntime(client=AsyncMock(), jetstream=AsyncMock())
    runtime.subscription = FakeSubscription(runtime, msg)

    async def handler(**_kwargs):
        raise TemporaryError("retry", delay=2)

    await SanicNATSConsumerModel._worker(runtime=runtime, fn=handler)

    msg.nak.assert_awaited_once_with(delay=2)
    msg.ack.assert_not_awaited()
    msg.term.assert_not_awaited()


@pytest.mark.asyncio
async def test_temporary_handler_failure_uses_capped_exponential_backoff() -> None:
    msg = request_message(Node(kind="debug"), deliveries=3)
    runtime = NATSRuntime(client=AsyncMock(), jetstream=AsyncMock())
    runtime.subscription = FakeSubscription(runtime, msg)

    async def handler(**_kwargs):
        raise TemporaryError("retry", delay=2, max_delay=7)

    await SanicNATSConsumerModel._worker(runtime=runtime, fn=handler)

    msg.nak.assert_awaited_once_with(delay=7)


@pytest.mark.asyncio
async def test_permanent_handler_failure_is_dead_lettered_before_termination() -> None:
    msg = request_message(Node(kind="debug"))
    msg.headers["PAYMENT-SIGNATURE"] = "secret"
    runtime = NATSRuntime(client=AsyncMock(), jetstream=AsyncMock())
    runtime.dead_letter_subject = "requests.dead-letter.debug"
    runtime.subscription = FakeSubscription(runtime, msg)
    order = []

    async def publish(*_args, **_kwargs):
        order.append("dead-letter")

    async def term():
        order.append("term")

    runtime.jetstream.publish.side_effect = publish
    msg.term.side_effect = term

    async def handler(**_kwargs):
        raise PermanentError(
            "poison message",
            classification="poison",
            error_code="invalid_identity",
        )

    await SanicNATSConsumerModel._worker(runtime=runtime, fn=handler)

    assert order == ["dead-letter", "term"]
    msg.term.assert_awaited_once_with()
    msg.ack.assert_not_awaited()
    msg.nak.assert_not_awaited()
    published = runtime.jetstream.publish.await_args
    assert published.args[0] == "requests.dead-letter.debug"
    assert published.kwargs["headers"]["X-Xarta-Failure-Class"] == "poison"
    assert published.kwargs["headers"]["X-Xarta-Retryable"] == "false"
    assert "PAYMENT-SIGNATURE" not in published.kwargs["headers"]
    envelope = orjson.loads(published.args[1])
    assert envelope["schema_version"] == 1
    assert envelope["classification"] == "poison"
    assert envelope["error_code"] == "invalid_identity"
    assert envelope["flow_id"] is not None
    assert "payload" not in envelope


@pytest.mark.asyncio
async def test_unexpected_handler_failure_retries_before_dead_letter() -> None:
    msg = request_message(Node(kind="debug"), deliveries=1)
    runtime = NATSRuntime(client=AsyncMock(), jetstream=AsyncMock())
    runtime.dead_letter_subject = "requests.dead-letter.debug"
    runtime.subscription = FakeSubscription(runtime, msg)

    async def handler(**_kwargs):
        raise ValueError("unexpected bug")

    await SanicNATSConsumerModel._worker(runtime=runtime, fn=handler)

    msg.nak.assert_awaited_once_with(delay=1)
    msg.term.assert_not_awaited()
    runtime.jetstream.publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_unexpected_handler_failure_is_dead_lettered_after_three_attempts() -> (
    None
):
    msg = request_message(Node(kind="debug"), deliveries=3)
    runtime = NATSRuntime(client=AsyncMock(), jetstream=AsyncMock())
    runtime.dead_letter_subject = "requests.dead-letter.debug"
    runtime.subscription = FakeSubscription(runtime, msg)

    async def handler(**_kwargs):
        raise ValueError("unexpected bug")

    await SanicNATSConsumerModel._worker(runtime=runtime, fn=handler)

    msg.term.assert_awaited_once_with()
    msg.nak.assert_not_awaited()
    published = runtime.jetstream.publish.await_args
    assert (
        published.kwargs["headers"]["X-Xarta-Failure-Class"]
        == "unexpected_retry_exhausted"
    )
    assert published.kwargs["headers"]["X-Xarta-Retryable"] == "true"
    assert published.kwargs["headers"]["X-Xarta-Delivery-Count"] == "3"
    assert "unexpected bug" not in orjson.loads(published.args[1])["error"]


@pytest.mark.asyncio
async def test_registration_is_idempotent_for_blueprint_aliases(monkeypatch) -> None:
    client = SimpleNamespace(jetstream=lambda: AsyncMock())
    app = SimpleNamespace(
        ctx=SimpleNamespace(),
        register_listener=Mock(),
    )

    monkeypatch.setattr(SanicNATSModel, "open", AsyncMock(return_value=client))
    SanicNATSModel._runtime = None

    await SanicNATSModel.register(app)
    await SanicNATSModel.register(app)

    SanicNATSModel.open.assert_awaited_once_with()
    app.register_listener.assert_called_once()
    SanicNATSModel._runtime = None


def request_message(node: Node, deliveries: int = 1) -> SimpleNamespace:
    task = NodeTask(flow_id=uuid4(), node=node)
    msg = message()
    msg.data = orjson.dumps(task.dict(), default=str)
    msg.headers = {"flow-id": str(task.flow_id)}
    msg.subject = "requests.flow.tasks.node.email"
    msg.metadata = SimpleNamespace(
        num_delivered=deliveries,
        sequence=SimpleNamespace(stream=1),
    )
    return msg


@pytest.mark.asyncio
async def test_request_logger_binds_flow_correlation_and_execution_ids(
    monkeypatch,
) -> None:
    correlation_id = uuid4()
    task = NodeTask(
        flow_id=uuid4(), correlation_id=correlation_id, node=Node(kind="debug")
    )
    msg = message()
    msg.data = orjson.dumps(task.dict(), default=str)
    msg.headers = {}
    bound = {}
    events = []

    class Log:
        def bind(self, **fields):
            bound.update(fields)
            return self

        async def ainfo(self, event, **fields):
            events.append((event, fields))

    monkeypatch.setattr("xarta.nats.sanic.logger", Log())
    monkeypatch.setattr(
        SanicNATSSynchronousRequestsConsumerModel,
        "_publish_outcomes",
        AsyncMock(),
    )

    async def handler(**_kwargs):
        return CapabilityResult()

    await SanicNATSSynchronousRequestsConsumerModel._handle_synchronous_request(
        NATSRuntime(client=AsyncMock(), jetstream=AsyncMock()), msg, handler, {}
    )

    assert bound == {
        "flow_id": str(task.flow_id),
        "correlation_id": str(correlation_id),
        "node_id": str(task.node.id),
        "node_execution_id": str(task.node_execution_id),
        "kind": "debug",
    }
    lifecycle = [
        fields
        for event, fields in events
        if event == "capability_lifecycle_state_changed"
    ]
    assert [(event["previous_state"], event["next_state"]) for event in lifecycle] == [
        ("scheduled", "executing"),
        ("executing", "resolved"),
    ]
    assert all(event["flow_id"] == str(task.flow_id) for event in lifecycle)
    assert all(event["correlation_id"] == str(correlation_id) for event in lifecycle)


@pytest.mark.asyncio
async def test_unexpected_request_handler_exception_emits_execution_failed(
    monkeypatch,
) -> None:
    failure = Node(kind="debug")
    msg = request_message(
        Node(kind="debug", on={"execution_failed": [failure]}), deliveries=3
    )
    runtime = NATSRuntime(client=AsyncMock(), jetstream=AsyncMock())
    runtime.dead_letter_subject = "requests.dead-letter.debug"
    runtime.subscription = FakeSubscription(runtime, msg)
    publish = AsyncMock()
    monkeypatch.setattr(SanicNATSRequestsConsumerModel, "_publish", publish)
    SanicNATSRequestsConsumerModel._tracking_store = InMemoryTrackingStore()

    async def handler(**_kwargs):
        raise KeyError("bug")

    await SanicNATSRequestsConsumerModel._worker(runtime=runtime, fn=handler)

    assert publish.await_args.kwargs["task"].node.id == failure.id
    msg.term.assert_awaited_once_with()
    msg.ack.assert_not_awaited()
    runtime.jetstream.publish.assert_awaited_once()


@pytest.mark.asyncio
async def test_malformed_request_is_dead_lettered_without_handler() -> None:
    msg = message()
    msg.data = b"not-json"
    msg.headers = {"Nats-Msg-Id": "malformed-message"}
    msg.subject = "requests.invalid.tasks.invalid.debug"
    msg.metadata = SimpleNamespace(
        num_delivered=1,
        sequence=SimpleNamespace(stream=42),
    )
    runtime = NATSRuntime(client=AsyncMock(), jetstream=AsyncMock())
    runtime.dead_letter_subject = "requests.dead-letter.debug"
    runtime.subscription = FakeSubscription(runtime, msg)
    handler = AsyncMock()

    await SanicNATSSynchronousRequestsConsumerModel._worker(runtime=runtime, fn=handler)

    handler.assert_not_awaited()
    msg.term.assert_awaited_once_with()
    msg.ack.assert_not_awaited()
    published = runtime.jetstream.publish.await_args
    assert published.kwargs["headers"]["X-Xarta-Failure-Class"] == "malformed"
    assert published.kwargs["headers"]["Nats-Msg-Id"] == (
        "malformed-message:dead-letter"
    )
    envelope = orjson.loads(published.args[1])
    assert envelope["stream_sequence"] == 42
    assert envelope["flow_id"] is None


@pytest.mark.asyncio
async def test_unknown_node_kind_is_dead_lettered_as_malformed() -> None:
    task = NodeTask(flow_id=uuid4(), node=Node(kind="debug")).dict()
    task["node"]["kind"] = "unknown-kind"
    msg = message()
    msg.data = orjson.dumps(task, default=str)
    msg.headers = {"Nats-Msg-Id": "unknown-kind"}
    msg.subject = "requests.flow.tasks.execution.unknown-kind"
    msg.metadata = SimpleNamespace(
        num_delivered=1,
        sequence=SimpleNamespace(stream=43),
    )
    runtime = NATSRuntime(client=AsyncMock(), jetstream=AsyncMock())
    runtime.dead_letter_subject = "requests.dead-letter.unknown-kind"
    runtime.subscription = FakeSubscription(runtime, msg)
    handler = AsyncMock()

    await SanicNATSSynchronousRequestsConsumerModel._worker(runtime=runtime, fn=handler)

    handler.assert_not_awaited()
    msg.term.assert_awaited_once_with()
    assert (
        runtime.jetstream.publish.await_args.kwargs["headers"]["X-Xarta-Failure-Class"]
        == "malformed"
    )


@pytest.mark.asyncio
async def test_permanent_request_failure_publishes_outcome_and_dead_letter(
    monkeypatch,
) -> None:
    successor = Node(kind="debug")
    msg = request_message(Node(kind="debug", on={"execution_failed": [successor]}))
    runtime = NATSRuntime(client=AsyncMock(), jetstream=AsyncMock())
    runtime.dead_letter_subject = "requests.dead-letter.debug"
    runtime.subscription = FakeSubscription(runtime, msg)
    publish = AsyncMock()
    monkeypatch.setattr(SanicNATSSynchronousRequestsConsumerModel, "_publish", publish)

    async def handler(**_kwargs):
        raise PermanentError("invalid contract", error_code="invalid_contract")

    await SanicNATSSynchronousRequestsConsumerModel._worker(runtime=runtime, fn=handler)

    assert publish.await_args.args[0].node.id == successor.id
    envelope = orjson.loads(runtime.jetstream.publish.await_args.args[1])
    assert envelope["outcome"]["outcome"] == "execution_failed"
    msg.term.assert_awaited_once_with()
    msg.ack.assert_not_awaited()


@pytest.mark.asyncio
async def test_permanent_outcome_publication_retry_preserves_terminal_outcome(
    monkeypatch,
) -> None:
    msg = request_message(Node(kind="debug"))
    runtime = NATSRuntime(client=AsyncMock(), jetstream=AsyncMock())
    runtime.dead_letter_subject = "requests.dead-letter.debug"
    monkeypatch.setattr(
        SanicNATSSynchronousRequestsConsumerModel,
        "_publish_outcomes",
        AsyncMock(side_effect=RuntimeError("NATS unavailable")),
    )

    async def handler(**_kwargs):
        raise PermanentError("invalid contract", error_code="invalid_contract")

    with pytest.raises(TemporaryError) as captured:
        await SanicNATSSynchronousRequestsConsumerModel._handle_synchronous_request(
            runtime, msg, handler, {}
        )

    assert captured.value.classification == "permanent"
    assert captured.value.error_code == "invalid_contract"
    assert captured.value.outcome.outcome == "execution_failed"


@pytest.mark.asyncio
async def test_dead_letter_publication_retries_while_message_is_owned(
    monkeypatch,
) -> None:
    msg = request_message(Node(kind="debug"))
    runtime = NATSRuntime(client=AsyncMock(), jetstream=AsyncMock())
    runtime.dead_letter_subject = "requests.dead-letter.debug"
    runtime.jetstream.publish.side_effect = [RuntimeError("NATS unavailable"), None]
    sleep = AsyncMock()
    monkeypatch.setattr("xarta.nats.sanic.asyncio.sleep", sleep)

    await SanicNATSConsumerModel._handle_permanent_failure(
        runtime, msg, PermanentError("invalid contract")
    )

    assert runtime.jetstream.publish.await_count == 2
    msg.in_progress.assert_awaited_once_with()
    sleep.assert_awaited_once_with(1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("terminal_outcome", "expected_branch"),
    [
        (None, "retry"),
        (
            OutcomeEmission(outcome="mailbox_full"),
            "named",
        ),
    ],
)
async def test_exhausted_request_retry_resolves_terminal_outcome(
    monkeypatch, terminal_outcome, expected_branch
) -> None:
    retry = Node(kind="debug")
    named = Node(kind="debug")
    root = EmailNode(
        to="recipient@example.com",
        sender="sender@example.com",
        body={},
        on={"retry_exhausted": [retry], "mailbox_full": [named]},
    )
    expected_id = named.id if expected_branch == "named" else retry.id
    msg = request_message(root)
    runtime = NATSRuntime(client=AsyncMock(), jetstream=AsyncMock())
    runtime.dead_letter_subject = "requests.dead-letter.test"
    publish = AsyncMock()
    monkeypatch.setattr(SanicNATSRequestsConsumerModel, "_publish", publish)
    SanicNATSRequestsConsumerModel._tracking_store = InMemoryTrackingStore()

    await SanicNATSRequestsConsumerModel._handle_exhausted_retry(
        runtime,
        msg,
        TemporaryError("retry exhausted", outcome=terminal_outcome),
    )

    assert publish.await_args.kwargs["task"].node.id == expected_id
    runtime.jetstream.publish.assert_awaited_once()


@pytest.mark.asyncio
async def test_auto_replay_defers_terminal_outcome_until_final_periodic_attempt(
    monkeypatch,
) -> None:
    successor = Node(kind="debug")
    root = Node(kind="debug", on={"retry_exhausted": [successor]})
    msg = request_message(root)
    runtime = NATSRuntime(client=AsyncMock(), jetstream=AsyncMock())
    runtime.dead_letter_subject = "requests.dead-letter.debug"
    publish = AsyncMock()
    monkeypatch.setattr(SanicNATSSynchronousRequestsConsumerModel, "_publish", publish)
    error = TemporaryError("retry", auto_replay=True)

    await SanicNATSSynchronousRequestsConsumerModel._handle_exhausted_retry(
        runtime, msg, error
    )

    publish.assert_not_awaited()
    assert (
        runtime.jetstream.publish.await_args.kwargs["headers"]["X-Xarta-Auto-Replay"]
        == "true"
    )

    msg.headers["X-Xarta-Periodic-Retry-Final"] = "true"
    msg.headers["X-Xarta-Periodic-Replay-Count"] = "3"
    msg.headers["X-Xarta-Periodic-Replay-Limit"] = "3"
    await SanicNATSSynchronousRequestsConsumerModel._handle_exhausted_retry(
        runtime, msg, error
    )

    assert publish.await_args.args[0].node.id == successor.id


@pytest.mark.asyncio
async def test_exhausted_retry_coexists_with_adapter_bound_operation(
    monkeypatch,
) -> None:
    successor = Node(kind="debug")
    root = Node(kind="debug", on={"retry_exhausted": [successor]})
    msg = request_message(root)
    task = NodeTask.fromdict(orjson.loads(msg.data))
    runtime = NATSRuntime(client=AsyncMock(), jetstream=AsyncMock())
    runtime.dead_letter_subject = "requests.dead-letter.test"
    monkeypatch.setattr(SanicNATSRequestsConsumerModel, "_publish", AsyncMock())
    store = InMemoryTrackingStore()
    await store.accept(task)
    operation = await store.create_operation(
        task.node_execution_id,
        "sftp-like",
        DestinationBinding("partner", "sftp-like", "sftp-adapter", "v1"),
        {"status": "uploading"},
    )
    SanicNATSRequestsConsumerModel._tracking_store = store

    await SanicNATSRequestsConsumerModel._handle_exhausted_retry(
        runtime, msg, TemporaryError("retry exhausted")
    )

    assert store.operations[operation.id].binding.adapter == "sftp-adapter"
    assert next(iter(store.outcomes.values())).tracked_operation_id is None


@pytest.mark.asyncio
async def test_tracked_retry_exhaustion_dead_letters_before_terminal_state(
    monkeypatch,
) -> None:
    successor = Node(kind="debug")
    root = Node(kind="debug", on={"retry_exhausted": [successor]})
    msg = request_message(root)
    task = NodeTask.fromdict(orjson.loads(msg.data))
    runtime = NATSRuntime(client=AsyncMock(), jetstream=AsyncMock())
    runtime.dead_letter_subject = "requests.dead-letter.debug"
    order = []

    async def dead_letter(*_args, **_kwargs):
        order.append("dead-letter")

    async def publish(*_args, **_kwargs):
        order.append("successor")

    runtime.jetstream.publish.side_effect = dead_letter
    monkeypatch.setattr(SanicNATSRequestsConsumerModel, "publish", publish)
    store = InMemoryTrackingStore()
    await store.accept(task)
    SanicNATSRequestsConsumerModel._tracking_store = store

    await SanicNATSRequestsConsumerModel._handle_exhausted_retry(
        runtime, msg, TemporaryError("retry exhausted")
    )

    assert order == ["dead-letter", "successor"]


@pytest.mark.asyncio
async def test_tracked_permanent_persistence_retry_preserves_failure_class() -> None:
    class FailingOutcomeStore(InMemoryTrackingStore):
        async def apply_execution_outcomes(self, *args, **kwargs):
            raise RuntimeError("database unavailable")

    msg = request_message(Node(kind="debug"))
    runtime = NATSRuntime(client=AsyncMock(), jetstream=AsyncMock())
    runtime.dead_letter_subject = "requests.dead-letter.debug"
    runtime.subscription = FakeSubscription(runtime, msg)
    store = FailingOutcomeStore()
    SanicNATSRequestsConsumerModel._tracking_store = store

    async def handler(**_kwargs):
        raise PermanentError("invalid contract", error_code="invalid_contract")

    await SanicNATSRequestsConsumerModel._worker(runtime=runtime, fn=handler)

    msg.nak.assert_awaited_once_with(delay=1)
    msg.term.assert_not_awaited()
    envelope = orjson.loads(runtime.jetstream.publish.await_args.args[1])
    assert envelope["classification"] == "permanent"
    assert envelope["outcome"]["outcome"] == "execution_failed"


@pytest.mark.asyncio
async def test_nats_message_identity_uses_node_execution_id(monkeypatch) -> None:
    task = NodeTask(flow_id=uuid4(), node=Node(kind="debug"))
    jetstream = AsyncMock()
    monkeypatch.setattr(
        SanicNATSRequestsConsumerModel,
        "jetstream",
        classmethod(lambda cls: jetstream),
    )

    await SanicNATSRequestsConsumerModel._publish(
        task,
        headers={
            "X-Xarta-Periodic-Retry-Final": "true",
            "caller-header": "preserved",
        },
    )

    assert str(task.node_execution_id) in jetstream.publish.await_args.kwargs["subject"]
    assert jetstream.publish.await_args.kwargs["headers"]["Nats-Msg-Id"] == str(
        task.node_execution_id
    )
    assert "X-Xarta-Periodic-Retry-Final" not in (
        jetstream.publish.await_args.kwargs["headers"]
    )
    assert (
        jetstream.publish.await_args.kwargs["headers"]["caller-header"] == "preserved"
    )
    assert task.node_execution_id != task.node.id


@pytest.mark.asyncio
async def test_tracked_submission_acks_while_waiting_without_success_outcome() -> None:
    root = Node(kind="debug")
    task = NodeTask(flow_id=uuid4(), node=root)
    msg = message()
    msg.data = orjson.dumps(task.dict(), default=str)
    msg.headers = {"flow-id": str(task.flow_id)}
    msg.subject = f"requests.{task.flow_id}.tasks.{task.node_execution_id}.debug"
    msg.metadata = SimpleNamespace(num_delivered=1, sequence=SimpleNamespace(stream=1))
    runtime = NATSRuntime(client=AsyncMock(), jetstream=AsyncMock())
    runtime.subscription = FakeSubscription(runtime, msg)
    store = InMemoryTrackingStore()
    SanicNATSRequestsConsumerModel._tracking_store = store

    async def handler(task, **_kwargs):
        service = TrackedCapabilityService(
            store,
            DestinationRegistry(
                {"test": {"kind": "debug", "adapter": "fake", "revision": "1"}}
            ),
        )

        async def submit(operation, configuration):
            return "provider-reference", {"provider": "accepted"}

        await service.start(task, "debug", "test", {}, submit)
        return CapabilityResult(waiting_feedback=True)

    await SanicNATSRequestsConsumerModel._worker(runtime=runtime, fn=handler)

    msg.ack.assert_awaited_once_with()
    assert (
        store.node_executions[task.node_execution_id].state
        == NodeExecutionState.WAITING_FEEDBACK
    )
    assert store.outcomes == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    [
        NodeExecutionState.WAITING_FEEDBACK,
        NodeExecutionState.RESOLVED,
        NodeExecutionState.FAILED,
    ],
)
async def test_duplicate_execution_state_acks_without_invoking_handler(state) -> None:
    root = Node(kind="debug")
    task = NodeTask(flow_id=uuid4(), node=root)
    msg = message()
    msg.data = orjson.dumps(task.dict(), default=str)
    msg.headers = {"flow-id": str(task.flow_id)}
    msg.subject = f"requests.{task.flow_id}.tasks.{task.node_execution_id}.debug"
    msg.metadata = SimpleNamespace(num_delivered=2, sequence=SimpleNamespace(stream=1))
    runtime = NATSRuntime(client=AsyncMock(), jetstream=AsyncMock())
    runtime.dead_letter_subject = "requests.dead-letter.debug"
    runtime.subscription = FakeSubscription(runtime, msg)
    store = InMemoryTrackingStore()
    execution = await store.accept(task)
    execution.state = state
    SanicNATSRequestsConsumerModel._tracking_store = store
    handler = AsyncMock()

    await SanicNATSRequestsConsumerModel._worker(runtime=runtime, fn=handler)

    handler.assert_not_awaited()
    msg.ack.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_active_attempt_duplicate_is_deferred_not_acked() -> None:
    root = Node(kind="debug")
    task = NodeTask(flow_id=uuid4(), node=root)
    msg = message()
    msg.data = orjson.dumps(task.dict(), default=str)
    msg.headers = {"flow-id": str(task.flow_id)}
    msg.subject = f"requests.{task.flow_id}.tasks.{task.node_execution_id}.debug"
    msg.metadata = SimpleNamespace(num_delivered=2, sequence=SimpleNamespace(stream=1))
    runtime = NATSRuntime(client=AsyncMock(), jetstream=AsyncMock())
    runtime.subscription = FakeSubscription(runtime, msg)
    store = InMemoryTrackingStore()
    first = await store.begin_attempt(task, lease_seconds=30)
    assert first.disposition is AttemptDisposition.EXECUTE
    SanicNATSRequestsConsumerModel._tracking_store = store
    handler = AsyncMock()

    await SanicNATSRequestsConsumerModel._worker(runtime=runtime, fn=handler)

    handler.assert_not_awaited()
    msg.nak.assert_awaited_once()
    assert msg.nak.await_args.kwargs["delay"] > 0
    msg.ack.assert_not_awaited()
    msg.term.assert_not_awaited()


@pytest.mark.asyncio
async def test_conflicting_execution_identity_is_terminated_without_handler() -> None:
    execution_id = uuid4()
    original = NodeTask(
        flow_id=uuid4(), node=Node(kind="debug"), node_execution_id=execution_id
    )
    conflicting = NodeTask(
        flow_id=original.flow_id,
        node=Node(kind="debug"),
        node_execution_id=execution_id,
    )
    msg = message()
    msg.data = orjson.dumps(conflicting.dict(), default=str)
    msg.headers = {"flow-id": str(conflicting.flow_id)}
    msg.subject = f"requests.{conflicting.flow_id}.tasks.{execution_id}.debug"
    msg.metadata = SimpleNamespace(num_delivered=2, sequence=SimpleNamespace(stream=1))
    runtime = NATSRuntime(client=AsyncMock(), jetstream=AsyncMock())
    runtime.dead_letter_subject = "requests.dead-letter.debug"
    runtime.subscription = FakeSubscription(runtime, msg)
    store = InMemoryTrackingStore()
    await store.accept(original)
    SanicNATSRequestsConsumerModel._tracking_store = store
    handler = AsyncMock()

    await SanicNATSRequestsConsumerModel._worker(runtime=runtime, fn=handler)

    handler.assert_not_awaited()
    msg.term.assert_awaited_once_with()
    msg.ack.assert_not_awaited()
    msg.nak.assert_not_awaited()
    assert (
        runtime.jetstream.publish.await_args.kwargs["headers"]["X-Xarta-Failure-Class"]
        == "poison"
    )


@pytest.mark.asyncio
async def test_synchronous_capability_emits_and_schedules_success(monkeypatch) -> None:
    successor = Node(kind="debug")
    msg = request_message(Node(kind="debug", on={"success": [successor]}))
    runtime = NATSRuntime(client=AsyncMock(), jetstream=AsyncMock())
    runtime.subscription = FakeSubscription(runtime, msg)
    publish = AsyncMock()
    monkeypatch.setattr(SanicNATSRequestsConsumerModel, "_publish", publish)
    SanicNATSRequestsConsumerModel._tracking_store = InMemoryTrackingStore()

    async def handler(**_kwargs):
        return CapabilityResult((OutcomeEmission("success"),))

    await SanicNATSRequestsConsumerModel._worker(runtime=runtime, fn=handler)

    assert publish.await_args.kwargs["task"].node.id == successor.id
    msg.ack.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_synchronous_consumer_publishes_before_ack_without_tracking(
    monkeypatch,
) -> None:
    successor = Node(kind="debug")
    msg = request_message(Node(kind="debug", on={"success": [successor]}))
    runtime = NATSRuntime(client=AsyncMock(), jetstream=AsyncMock())
    runtime.subscription = FakeSubscription(runtime, msg)
    order = []

    async def publish(*_args, **_kwargs):
        order.append("publish")

    async def ack():
        order.append("ack")

    msg.ack.side_effect = ack
    monkeypatch.setattr(SanicNATSSynchronousRequestsConsumerModel, "_publish", publish)

    async def handler(**_kwargs):
        return CapabilityResult((OutcomeEmission("success"),))

    await SanicNATSSynchronousRequestsConsumerModel._worker(runtime=runtime, fn=handler)

    assert order == ["publish", "ack"]


@pytest.mark.asyncio
async def test_adapter_selected_synchronous_route_never_admits_tracking(
    monkeypatch,
) -> None:
    successor = Node(kind="debug")
    msg = request_message(Node(kind="debug", on={"success": [successor]}))
    runtime = NATSRuntime(client=AsyncMock(), jetstream=AsyncMock())
    runtime.execution_mode_resolver = lambda _task: ExecutionMode.SYNCHRONOUS
    runtime.subscription = FakeSubscription(runtime, msg)
    store = AsyncMock()
    store.begin_attempt.side_effect = AssertionError("tracking admission is forbidden")
    SanicNATSRequestsConsumerModel._tracking_store = store
    publish = AsyncMock()
    monkeypatch.setattr(SanicNATSRequestsConsumerModel, "_publish", publish)

    async def handler(**_kwargs):
        return CapabilityResult((OutcomeEmission("success"),))

    await SanicNATSRequestsConsumerModel._worker(runtime=runtime, fn=handler)

    store.begin_attempt.assert_not_awaited()
    assert publish.await_args.args[0].node.id == successor.id
    msg.ack.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_synchronous_redelivery_republishes_same_successor_identity(
    monkeypatch,
) -> None:
    successor = Node(kind="debug")
    root = Node(kind="debug", on={"success": [successor]})
    original = request_message(root)
    published = []

    async def publish(task, **_kwargs):
        published.append(task)

    monkeypatch.setattr(SanicNATSSynchronousRequestsConsumerModel, "_publish", publish)

    async def handler(**_kwargs):
        return CapabilityResult((OutcomeEmission("success"),))

    for _ in range(2):
        msg = message()
        msg.data = original.data
        msg.headers = original.headers
        msg.subject = original.subject
        msg.metadata = original.metadata
        runtime = NATSRuntime(client=AsyncMock(), jetstream=AsyncMock())
        runtime.subscription = FakeSubscription(runtime, msg)
        await SanicNATSSynchronousRequestsConsumerModel._worker(
            runtime=runtime, fn=handler
        )

    assert len(published) == 2
    assert published[0].dict() == published[1].dict()


@pytest.mark.asyncio
async def test_synchronous_successor_publication_failure_naks_parent(
    monkeypatch,
) -> None:
    successor = Node(kind="debug")
    msg = request_message(Node(kind="debug", on={"success": [successor]}))
    runtime = NATSRuntime(client=AsyncMock(), jetstream=AsyncMock())
    runtime.subscription = FakeSubscription(runtime, msg)
    monkeypatch.setattr(
        SanicNATSSynchronousRequestsConsumerModel,
        "_publish",
        AsyncMock(side_effect=RuntimeError("NATS unavailable")),
    )

    async def handler(**_kwargs):
        return CapabilityResult((OutcomeEmission("success"),))

    await SanicNATSSynchronousRequestsConsumerModel._worker(runtime=runtime, fn=handler)

    msg.nak.assert_awaited_once_with(delay=1)
    msg.ack.assert_not_awaited()


@pytest.mark.asyncio
async def test_attempt_bookkeeping_failure_does_not_replace_handler_retry() -> None:
    class FailingAttemptStore(InMemoryTrackingStore):
        async def complete_attempt(self, attempt, error=None):
            raise RuntimeError("database temporarily unavailable")

    msg = request_message(Node(kind="debug"))
    runtime = NATSRuntime(client=AsyncMock(), jetstream=AsyncMock())
    runtime.subscription = FakeSubscription(runtime, msg)
    SanicNATSRequestsConsumerModel._tracking_store = FailingAttemptStore()

    async def handler(**_kwargs):
        raise TemporaryError("retry", delay=3)

    await SanicNATSRequestsConsumerModel._worker(runtime=runtime, fn=handler)

    msg.nak.assert_awaited_once_with(delay=3)
    msg.term.assert_not_awaited()


@pytest.mark.asyncio
async def test_handler_longer_than_initial_lease_stays_owned() -> None:
    msg = request_message(Node(kind="debug"))
    task = NodeTask.fromdict(orjson.loads(msg.data))
    runtime = NATSRuntime(client=AsyncMock(), jetstream=AsyncMock())
    runtime.subscription = FakeSubscription(runtime, msg)
    runtime.heartbeat_interval = 0.01
    runtime.execution_attempt_lease_seconds = 0.03
    store = InMemoryTrackingStore()
    SanicNATSRequestsConsumerModel._tracking_store = store

    async def handler(**_kwargs):
        await asyncio.sleep(0.06)
        duplicate = await store.begin_attempt(task, lease_seconds=0.03)
        assert duplicate.disposition is AttemptDisposition.ACTIVE_ATTEMPT
        return CapabilityResult((OutcomeEmission("success"),))

    await SanicNATSRequestsConsumerModel._worker(runtime=runtime, fn=handler)

    assert len(store.attempts[task.node_execution_id]) == 1
    assert store.attempts[task.node_execution_id][0].completed_at is not None
    msg.ack.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_cancelled_handler_stops_renewal_and_expired_lease_recovers() -> None:
    store = InMemoryTrackingStore()
    task = NodeTask(flow_id=uuid4(), node=Node(kind="debug"))
    admission = await store.begin_attempt(task, lease_seconds=0.03)
    assert admission.attempt is not None
    SanicNATSRequestsConsumerModel._tracking_store = store
    renewal = asyncio.create_task(
        SanicNATSRequestsConsumerModel._renew_attempt_lease(
            admission.attempt, lease_seconds=0.03, interval=0.01
        )
    )
    await asyncio.sleep(0.015)
    renewal.cancel()
    with pytest.raises(asyncio.CancelledError):
        await renewal

    await asyncio.sleep(0.04)
    recovered = await store.begin_attempt(task, lease_seconds=30)
    assert recovered.disposition.value == "execute"
    assert recovered.attempt is not None
    assert recovered.attempt.number == 2
