from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import orjson
import pytest

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from xarta import telemetry
from xarta.jobs.replay_dead_letters.base import replay_dead_letters


def dead_letter(**headers) -> SimpleNamespace:
    return SimpleNamespace(
        data=orjson.dumps({"stream_sequence": 42, "original_message_id": "execution"}),
        headers={
            "X-Xarta-Retryable": "true",
            "X-Xarta-Auto-Replay": "true",
            "X-Xarta-Failure-Class": "retry_exhausted",
            **headers,
        },
        subject="requests.dead-letter.archive",
        metadata=SimpleNamespace(num_delivered=1),
        ack_sync=AsyncMock(),
    )


@pytest.fixture
def spans(monkeypatch):
    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource.create({"service.name": "test"}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(telemetry, "_provider", provider)
    monkeypatch.setattr(telemetry, "_tracer", provider.get_tracer("test"))
    yield exporter
    provider.shutdown()


@pytest.mark.asyncio
async def test_replays_eligible_dead_letter_with_bounded_attempt_headers(spans) -> None:
    message = dead_letter()
    subscription = SimpleNamespace(fetch=AsyncMock(return_value=[message]))
    original = SimpleNamespace(
        subject="requests.flow.tasks.execution.archive",
        data=b"request",
        headers={
            "Nats-Msg-Id": "execution",
            "TraceParent": "00-11111111111111111111111111111111-1111111111111111-01",
            "tracestate": "historical=value",
        },
    )
    jetstream = SimpleNamespace(
        get_msg=AsyncMock(return_value=original),
        publish=AsyncMock(),
    )

    counts = await replay_dead_letters(
        jetstream, subscription, batch_size=10, max_attempts=3
    )

    assert counts == {"examined": 1, "replayed": 1, "ineligible": 0, "exhausted": 0}
    jetstream.get_msg.assert_awaited_once_with("REQUESTS", seq=42)
    headers = jetstream.publish.await_args.kwargs["headers"]
    assert headers["Nats-Msg-Id"] == "execution:periodic-replay:1"
    assert headers["X-Xarta-Original-Message-Id"] == "execution"
    assert headers["X-Xarta-Periodic-Replay-Count"] == "1"
    assert headers["X-Xarta-Periodic-Replay-Limit"] == "3"
    assert headers["X-Xarta-Periodic-Retry-Final"] == "false"
    assert "TraceParent" not in headers
    assert headers["traceparent"] != original.headers["TraceParent"]
    assert "tracestate" not in headers
    message.ack_sync.assert_awaited_once_with()
    producer, consumer = spans.get_finished_spans()
    assert consumer.name == "requests.dead-letter.archive process"
    assert producer.name == "requests.*.tasks.*.archive publish"
    assert producer.parent.span_id == consumer.context.span_id


@pytest.mark.asyncio
async def test_replay_removes_stale_trace_context_when_tracing_is_disabled(
    monkeypatch,
) -> None:
    monkeypatch.setattr(telemetry, "_provider", None)
    message = dead_letter()
    subscription = SimpleNamespace(fetch=AsyncMock(return_value=[message]))
    original = SimpleNamespace(
        subject="requests.flow.tasks.execution.archive",
        data=b"request",
        headers={
            "Nats-Msg-Id": "execution",
            "TraceParent": "historical",
            "tracestate": "historical=value",
        },
    )
    jetstream = SimpleNamespace(
        get_msg=AsyncMock(return_value=original),
        publish=AsyncMock(),
    )

    await replay_dead_letters(jetstream, subscription, batch_size=10, max_attempts=3)

    headers = jetstream.publish.await_args.kwargs["headers"]
    assert not {"traceparent", "tracestate"} & {key.lower() for key in headers}
    assert headers["Nats-Msg-Id"] == "execution:periodic-replay:1"
    assert headers["X-Xarta-Original-Message-Id"] == "execution"
    assert headers["X-Xarta-Periodic-Replay-Count"] == "1"
    assert headers["X-Xarta-Periodic-Replay-Limit"] == "3"
    assert headers["X-Xarta-Periodic-Retry-Final"] == "false"


@pytest.mark.asyncio
async def test_marks_last_periodic_replay_as_final() -> None:
    message = dead_letter(**{"X-Xarta-Periodic-Replay-Count": "2"})
    subscription = SimpleNamespace(fetch=AsyncMock(return_value=[message]))
    original = SimpleNamespace(subject="requests.source", data=b"request", headers={})
    jetstream = SimpleNamespace(
        get_msg=AsyncMock(return_value=original),
        publish=AsyncMock(),
    )

    await replay_dead_letters(jetstream, subscription, batch_size=10, max_attempts=3)

    assert (
        jetstream.publish.await_args.kwargs["headers"]["X-Xarta-Periodic-Retry-Final"]
        == "true"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        {"X-Xarta-Retryable": "false"},
        {"X-Xarta-Auto-Replay": "false"},
        {"X-Xarta-Failure-Class": "permanent"},
    ],
)
async def test_acknowledges_ineligible_dead_letters_without_replay(headers) -> None:
    message = dead_letter(**headers)
    subscription = SimpleNamespace(fetch=AsyncMock(return_value=[message]))
    jetstream = SimpleNamespace(get_msg=AsyncMock(), publish=AsyncMock())

    counts = await replay_dead_letters(
        jetstream, subscription, batch_size=10, max_attempts=3
    )

    assert counts["ineligible"] == 1
    jetstream.publish.assert_not_awaited()
    message.ack_sync.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_does_not_replay_after_periodic_attempt_limit() -> None:
    message = dead_letter(
        **{
            "X-Xarta-Periodic-Replay-Count": "3",
            "X-Xarta-Periodic-Replay-Limit": "3",
            "X-Xarta-Periodic-Retry-Final": "true",
        }
    )
    subscription = SimpleNamespace(fetch=AsyncMock(return_value=[message]))
    jetstream = SimpleNamespace(get_msg=AsyncMock(), publish=AsyncMock())

    counts = await replay_dead_letters(
        jetstream, subscription, batch_size=10, max_attempts=3
    )

    assert counts["exhausted"] == 1
    jetstream.publish.assert_not_awaited()
    message.ack_sync.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_invalid_replay_count_is_not_replayed() -> None:
    message = dead_letter(**{"X-Xarta-Periodic-Replay-Count": "invalid"})
    subscription = SimpleNamespace(fetch=AsyncMock(return_value=[message]))
    jetstream = SimpleNamespace(get_msg=AsyncMock(), publish=AsyncMock())

    counts = await replay_dead_letters(
        jetstream, subscription, batch_size=10, max_attempts=3
    )

    assert counts["ineligible"] == 1
    jetstream.publish.assert_not_awaited()
    message.ack_sync.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_pinned_limit_is_not_changed_by_later_configuration() -> None:
    message = dead_letter(
        **{
            "X-Xarta-Periodic-Replay-Count": "1",
            "X-Xarta-Periodic-Replay-Limit": "3",
        }
    )
    subscription = SimpleNamespace(fetch=AsyncMock(return_value=[message]))
    original = SimpleNamespace(subject="requests.source", data=b"request", headers={})
    jetstream = SimpleNamespace(
        get_msg=AsyncMock(return_value=original),
        publish=AsyncMock(),
    )

    await replay_dead_letters(jetstream, subscription, batch_size=10, max_attempts=1)

    headers = jetstream.publish.await_args.kwargs["headers"]
    assert headers["X-Xarta-Periodic-Replay-Count"] == "2"
    assert headers["X-Xarta-Periodic-Replay-Limit"] == "3"
    assert headers["X-Xarta-Periodic-Retry-Final"] == "false"


@pytest.mark.asyncio
async def test_inconsistent_final_header_is_not_replayed() -> None:
    message = dead_letter(
        **{
            "X-Xarta-Periodic-Replay-Count": "1",
            "X-Xarta-Periodic-Replay-Limit": "3",
            "X-Xarta-Periodic-Retry-Final": "true",
        }
    )
    subscription = SimpleNamespace(fetch=AsyncMock(return_value=[message]))
    jetstream = SimpleNamespace(get_msg=AsyncMock(), publish=AsyncMock())

    counts = await replay_dead_letters(
        jetstream, subscription, batch_size=10, max_attempts=3
    )

    assert counts["ineligible"] == 1
    jetstream.publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_malformed_envelope_does_not_abort_replay_batch() -> None:
    malformed = dead_letter()
    malformed.data = b"not-json"
    eligible = dead_letter()
    subscription = SimpleNamespace(fetch=AsyncMock(return_value=[malformed, eligible]))
    original = SimpleNamespace(subject="requests.source", data=b"request", headers={})
    jetstream = SimpleNamespace(
        get_msg=AsyncMock(return_value=original),
        publish=AsyncMock(),
    )

    counts = await replay_dead_letters(
        jetstream, subscription, batch_size=10, max_attempts=3
    )

    assert counts == {"examined": 2, "replayed": 1, "ineligible": 1, "exhausted": 0}
    jetstream.publish.assert_awaited_once()
