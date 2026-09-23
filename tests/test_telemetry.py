from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import orjson
import pytest

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import NonRecordingSpan
from opentelemetry.trace import SpanContext
from opentelemetry.trace import StatusCode
from opentelemetry.trace import TraceFlags
from opentelemetry.trace import TraceState
from opentelemetry.trace import set_span_in_context
from yarl import URL

from xarta import logging
from xarta import telemetry
from xarta.http.sessions import _trace_url
from xarta.logging import log_capability_lifecycle_change
from xarta.logging import record_outcome_emitted
from xarta.nats.sanic import SanicNATSRequestsConsumerModel
from xarta.protocol.dag import Node
from xarta.protocol.dag import NodeTask
from xarta.protocol.dag import OutcomeEvent
from xarta.protocol.dag import OutcomeSubject


@pytest.fixture
def spans(monkeypatch):
    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource.create({"service.name": "test"}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(telemetry, "_provider", provider)
    monkeypatch.setattr(telemetry, "_tracer", provider.get_tracer("test"))
    yield exporter, provider.get_tracer("test")
    provider.shutdown()


@pytest.mark.asyncio
async def test_nats_trace_propagation_lifecycle_logs_and_redelivery(
    spans, monkeypatch
) -> None:
    exporter, tracer = spans
    jetstream = AsyncMock()
    monkeypatch.setattr(
        SanicNATSRequestsConsumerModel,
        "jetstream",
        classmethod(lambda cls: jetstream),
    )
    task = NodeTask(
        flow_id=uuid4(),
        correlation_id=uuid4(),
        node=Node(kind="debug"),
    )

    with tracer.start_as_current_span("incoming") as incoming:
        await SanicNATSRequestsConsumerModel._publish(
            task, headers={"caller-header": "preserved"}
        )
        incoming_trace_id = incoming.get_span_context().trace_id

    published = jetstream.publish.await_args.kwargs
    headers = published["headers"]
    assert headers["caller-header"] == "preserved"
    assert headers["Nats-Msg-Id"] == str(task.node_execution_id)
    assert headers["flow-id"] == str(task.flow_id)
    assert headers["correlation-id"] == str(task.correlation_id)
    assert headers["traceparent"].startswith("00-")

    active_contexts = []
    log = AsyncMock()
    operation_id = uuid4()

    async def handler(**_kwargs):
        context = trace.get_current_span().get_span_context()
        active_contexts.append(context)
        record = logging._add_trace_context(None, None, {"event": "inside"})
        assert record["trace_id"] == f"{context.trace_id:032x}"
        assert record["span_id"] == f"{context.span_id:016x}"
        await log_capability_lifecycle_change(
            log,
            flow_id=task.flow_id,
            correlation_id=task.correlation_id,
            node_id=task.node.id,
            node_execution_id=task.node_execution_id,
            operation_id=operation_id,
            capability=task.node.kind,
            adapter="test-adapter",
            configuration_revision="revision-1",
            execution_mode="synchronous",
            lifecycle="node_execution",
            action="started",
            previous_state="scheduled",
            next_state="executing",
            previous_version=1,
            next_version=2,
            source="callback",
            duplicate=True,
        )
        record_outcome_emitted(
            OutcomeEvent(
                flow_id=task.flow_id,
                node_execution_id=task.node_execution_id,
                outcome="success",
                source="debug",
                subject=OutcomeSubject("document", "private-subject"),
                details={"payload": "private-details"},
            )
        )

    message = SimpleNamespace(
        subject=published["subject"],
        headers=headers,
        data=b"secret document payload",
        metadata=SimpleNamespace(num_delivered=1),
        in_progress=AsyncMock(),
    )
    await SanicNATSRequestsConsumerModel._run_handler(message, handler)
    message.metadata.num_delivered = 2
    await SanicNATSRequestsConsumerModel._run_handler(message, handler)

    assert len(active_contexts) == 2
    assert {context.trace_id for context in active_contexts} == {incoming_trace_id}
    assert active_contexts[0].span_id != active_contexts[1].span_id
    consumer_spans = [
        span for span in exporter.get_finished_spans() if span.name.endswith(" process")
    ]
    producer = next(
        span for span in exporter.get_finished_spans() if span.name.endswith(" publish")
    )
    assert len(consumer_spans) == 2
    assert all(
        span.parent.span_id == producer.context.span_id for span in consumer_spans
    )
    assert [
        span.attributes["messaging.message.delivery_count"] for span in consumer_spans
    ] == [1, 2]
    assert all(
        any(event.name == "xarta.capability.lifecycle" for event in span.events)
        for span in consumer_spans
    )
    assert all(
        span.attributes["xarta.operation.id"] == str(operation_id)
        and span.attributes["xarta.flow.id"] == str(task.flow_id)
        and span.attributes["xarta.correlation.id"] == str(task.correlation_id)
        and span.attributes["xarta.node.id"] == str(task.node.id)
        and span.attributes["xarta.node_execution.id"] == str(task.node_execution_id)
        and span.attributes["xarta.capability"] == task.node.kind
        and span.attributes["xarta.adapter"] == "test-adapter"
        and span.attributes["xarta.configuration.revision"] == "revision-1"
        and span.attributes["xarta.execution.mode"] == "synchronous"
        for span in consumer_spans
    )
    assert all(
        not {
            "xarta.lifecycle.action",
            "xarta.lifecycle.previous_state",
            "xarta.lifecycle.next_state",
            "xarta.lifecycle.previous_version",
            "xarta.lifecycle.next_version",
            "xarta.lifecycle.source",
            "xarta.duplicate",
        }
        & span.attributes.keys()
        and next(
            event for event in span.events if event.name == "xarta.capability.lifecycle"
        ).attributes["xarta.lifecycle.action"]
        == "started"
        for span in consumer_spans
    )
    assert all(
        any(event.name == "xarta.outcome.emitted" for event in span.events)
        for span in consumer_spans
    )
    assert all(
        "secret document payload" not in str(span.attributes)
        and "secret document payload" not in str(span.events)
        and "private-subject" not in str(span.events)
        and "private-details" not in str(span.events)
        for span in exporter.get_finished_spans()
    )


def test_disabled_nats_tracing_is_noop(monkeypatch) -> None:
    monkeypatch.setattr(telemetry, "_provider", None)
    headers = {"Nats-Msg-Id": "message", "traceparent": "stale"}
    with telemetry.nats_producer_span("requests.test", headers):
        pass
    assert headers == {"Nats-Msg-Id": "message", "traceparent": "stale"}


def test_nats_request_span_names_are_stable_and_fixed_subjects_are_unchanged(
    spans,
) -> None:
    exporter, _ = spans
    request_subjects = (
        f"requests.{uuid4()}.tasks.{uuid4()}.generate",
        f"requests.{uuid4()}.tasks.{uuid4()}.generate",
    )
    for subject in request_subjects:
        with telemetry.nats_producer_span(subject, {}):
            pass
        with telemetry.nats_consumer_span(
            SimpleNamespace(subject=subject, headers={}, metadata=None)
        ):
            pass
    fixed_subject = "archive.commands.delete"
    with telemetry.nats_producer_span(fixed_subject, {}):
        pass
    with telemetry.nats_consumer_span(
        SimpleNamespace(subject=fixed_subject, headers={}, metadata=None)
    ):
        pass

    finished = exporter.get_finished_spans()
    assert [span.name for span in finished[:4]] == [
        "requests.*.tasks.*.generate publish",
        "requests.*.tasks.*.generate process",
        "requests.*.tasks.*.generate publish",
        "requests.*.tasks.*.generate process",
    ]
    assert [span.name for span in finished[4:]] == [
        "archive.commands.delete publish",
        "archive.commands.delete process",
    ]
    assert [span.attributes["messaging.destination.name"] for span in finished[:4]] == [
        request_subjects[0],
        request_subjects[0],
        request_subjects[1],
        request_subjects[1],
    ]


def test_tracing_requires_a_valid_explicit_endpoint(monkeypatch) -> None:
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)
    assert telemetry._configured_endpoint() is None

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    assert telemetry._configured_endpoint() is None

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "collector:4318")
    with pytest.raises(ValueError, match="absolute HTTP"):
        telemetry._configured_endpoint()


def test_standard_always_on_sampler_records_with_unsampled_remote_parent(
    monkeypatch,
) -> None:
    monkeypatch.setenv("OTEL_TRACES_SAMPLER", "always_on")
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    parent = SpanContext(
        trace_id=1,
        span_id=1,
        is_remote=True,
        trace_flags=TraceFlags(0),
        trace_state=TraceState(),
    )
    context: Context = set_span_in_context(NonRecordingSpan(parent))

    with provider.get_tracer("test").start_as_current_span(
        "xarta execution", context=context
    ) as span:
        assert span.is_recording()

    provider.shutdown()
    exported = exporter.get_finished_spans()
    assert len(exported) == 1
    assert exported[0].parent == parent
    assert exported[0].context.trace_flags.sampled


def test_resource_uses_standard_service_name_with_sanic_fallback(monkeypatch) -> None:
    app = SimpleNamespace(name="Sanic Service")
    monkeypatch.delenv("OTEL_SERVICE_NAME", raising=False)
    assert telemetry._resource(app).attributes["service.name"] == "Sanic Service"

    monkeypatch.setenv("OTEL_SERVICE_NAME", "xarta-intake")
    assert telemetry._resource(app).attributes["service.name"] == "xarta-intake"


def test_structlog_output_contains_active_trace_ids(spans, capsys) -> None:
    _, tracer = spans
    with tracer.start_as_current_span("active") as span:
        logging.logger.info("trace_correlated")
        context = span.get_span_context()

    record = orjson.loads(capsys.readouterr().out)
    assert record["trace_id"] == f"{context.trace_id:032x}"
    assert record["span_id"] == f"{context.span_id:016x}"


def test_http_client_trace_strips_query_string() -> None:
    url = URL("https://provider.test/send?token=secret#fragment")
    assert _trace_url(url) == "https://provider.test/send"


@pytest.mark.asyncio
async def test_http_server_error_response_closes_span(spans) -> None:
    exporter, tracer = spans
    middleware = {}
    app = SimpleNamespace(
        register_middleware=lambda fn, kind: middleware.setdefault(kind, fn)
    )
    telemetry._instrument_http_server(app)
    carrier = {}
    with tracer.start_as_current_span("caller"):
        telemetry._propagator.inject(carrier)
    request = SimpleNamespace(
        path="/documents/secret-id",
        method="POST",
        headers=carrier,
        route=SimpleNamespace(path="/documents/<document_id>"),
        ctx=SimpleNamespace(),
    )

    await middleware["request"](request)
    await middleware["response"](request, SimpleNamespace(status=500))

    health = SimpleNamespace(
        path="/.info/healthz",
        method="GET",
        headers={},
        route=SimpleNamespace(path="/.info/healthz"),
        ctx=SimpleNamespace(),
    )
    await middleware["request"](health)
    await middleware["response"](health, SimpleNamespace(status=200))
    assert not hasattr(health.ctx, "otel_span")

    server = next(
        span
        for span in exporter.get_finished_spans()
        if span.name == "POST /documents/<document_id>"
    )
    assert server.status.status_code is StatusCode.ERROR
    assert server.attributes["http.response.status_code"] == 500
    assert "secret-id" not in str(server.attributes)


@pytest.mark.asyncio
async def test_asgi_server_propagates_context_and_records_response(spans) -> None:
    exporter, tracer = spans
    carrier = {}
    with tracer.start_as_current_span("caller") as caller:
        telemetry._propagator.inject(carrier)
        caller_span_id = caller.get_span_context().span_id

    async def app(scope, receive, send) -> None:
        assert trace.get_current_span().get_span_context().is_valid
        await send({"type": "http.response.start", "status": 202, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    sent = []
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/mcp",
        "headers": [(key.encode(), value.encode()) for key, value in carrier.items()],
    }

    async def send(message) -> None:
        sent.append(message)

    await telemetry.instrument_asgi(app)(scope, AsyncMock(), send)

    server = next(
        span for span in exporter.get_finished_spans() if span.name == "POST /api/mcp"
    )
    assert server.parent.span_id == caller_span_id
    assert server.attributes["http.response.status_code"] == 202
    assert len(sent) == 2
