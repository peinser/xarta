"""Optional process tracing and W3C context propagation."""

from __future__ import annotations

import asyncio
import os

from contextlib import contextmanager
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.propagators.textmap import CarrierT
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import SpanKind
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from xarta.__version__ import __version__

if TYPE_CHECKING:
    from collections.abc import Iterator
    from collections.abc import Mapping
    from collections.abc import MutableMapping

    from sanic import Sanic
    from sanic.request import Request
    from sanic.response import HTTPResponse


_tracer = trace.get_tracer(__name__)
_propagator = TraceContextTextMapPropagator()
_provider: TracerProvider | None = None
_EXCLUDED_HTTP_PATHS = frozenset({"/.info/healthz", "/.info/readyz", "/.info/livez"})


def _configured_endpoint() -> str | None:
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT") or os.environ.get(
        "OTEL_EXPORTER_OTLP_ENDPOINT"
    )
    if not endpoint:
        return None
    parsed = urlsplit(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(
            "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT or OTEL_EXPORTER_OTLP_ENDPOINT "
            "must be an absolute HTTP(S) URL"
        )
    return endpoint


def _resource(service_name: str | Sanic) -> Resource:
    attributes = {"service.version": __version__}
    if not os.environ.get("OTEL_SERVICE_NAME"):
        attributes["service.name"] = (
            service_name if isinstance(service_name, str) else service_name.name
        )
    return Resource.create(attributes)


def initialize_process(service_name: str) -> None:
    """Enable tracing for a process when an OTLP endpoint is configured."""
    global _provider

    if _configured_endpoint() is None:
        return
    if _provider is None:
        _provider = TracerProvider(resource=_resource(service_name))
        _provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        trace.set_tracer_provider(_provider)


def shutdown() -> None:
    if _provider is not None:
        _provider.shutdown()


def initialize(app: Sanic) -> None:
    """Enable tracing and Sanic instrumentation when OTLP is configured."""
    initialize_process(app.name)
    if not is_enabled():
        return

    async def shutdown_tracing(_: Sanic) -> None:
        await asyncio.to_thread(shutdown)

    app.register_listener(shutdown_tracing, "after_server_stop")
    _instrument_http_server(app)


def is_enabled() -> bool:
    return _provider is not None


def _route(request: Request) -> str | None:
    route = getattr(request, "route", None)
    return getattr(route, "path", None)


def _instrument_http_server(app: Sanic) -> None:
    async def start_request_span(request: Request) -> None:
        if request.path in _EXCLUDED_HTTP_PATHS:
            return
        route = _route(request)
        attributes = {"http.request.method": request.method}
        if route is not None:
            attributes["http.route"] = route
        span = _tracer.start_span(
            f"{request.method} {route}" if route is not None else request.method,
            context=_propagator.extract(request.headers),
            kind=SpanKind.SERVER,
            attributes=attributes,
        )
        request.ctx.otel_span = span
        request.ctx.otel_scope = trace.use_span(span, end_on_exit=True)
        request.ctx.otel_scope.__enter__()

    async def finish_request_span(request: Request, response: HTTPResponse) -> None:
        span = getattr(request.ctx, "otel_span", None)
        if span is None:
            return
        route = _route(request)
        if route is not None:
            span.update_name(f"{request.method} {route}")
            span.set_attribute("http.route", route)
        span.set_attribute("http.response.status_code", response.status)
        if response.status >= 500:
            span.set_status(trace.StatusCode.ERROR)
        request.ctx.otel_scope.__exit__(None, None, None)

    app.register_middleware(start_request_span, "request")
    app.register_middleware(finish_request_span, "response")  # type: ignore[arg-type]


def instrument_asgi(app):
    """Wrap an ASGI application with HTTP server tracing."""

    async def traced(scope, receive, send):
        if not is_enabled() or scope["type"] != "http":
            await app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path in _EXCLUDED_HTTP_PATHS:
            await app(scope, receive, send)
            return

        method = scope.get("method", "GET")
        headers = {
            key.decode("latin-1"): value.decode("latin-1")
            for key, value in scope.get("headers", ())
        }
        with _tracer.start_as_current_span(
            f"{method} {path}",
            context=_propagator.extract(headers),
            kind=SpanKind.SERVER,
            attributes={"http.request.method": method, "http.route": path},
        ) as span:

            async def send_with_status(message) -> None:
                if message["type"] == "http.response.start":
                    status = message["status"]
                    span.set_attribute("http.response.status_code", status)
                    if status >= 500:
                        span.set_status(trace.StatusCode.ERROR)
                await send(message)

            try:
                await app(scope, receive, send_with_status)
            except BaseException as ex:
                span.record_exception(ex)
                span.set_status(trace.StatusCode.ERROR)
                raise

    return traced


def extract_nats_context(headers: CarrierT | None) -> Context:
    return _propagator.extract(headers or {}) if is_enabled() else Context()


def _header(headers: Mapping[str, str], name: str) -> str | None:
    for key, value in headers.items():
        if str(key).lower() == name:
            return str(value)
    return None


def _nats_attributes(subject: str, headers: Mapping[str, str], operation: str) -> dict:
    attributes = {
        "messaging.system": "nats",
        "messaging.destination.name": subject,
        "messaging.operation.type": operation,
    }
    identifiers = {
        "messaging.message.id": _header(headers, "nats-msg-id"),
        "xarta.flow.id": _header(headers, "flow-id"),
        "xarta.correlation.id": _header(headers, "correlation-id")
        or _header(headers, "flow-correlation-id"),
    }
    attributes.update({key: value for key, value in identifiers.items() if value})
    if ".tasks." in subject and identifiers["messaging.message.id"]:
        attributes["xarta.node_execution.id"] = identifiers["messaging.message.id"]
    return attributes


def _nats_span_subject(subject: str) -> str:
    parts = subject.split(".")
    if len(parts) == 5 and parts[0] == "requests" and parts[2] == "tasks":
        return f"requests.*.tasks.*.{parts[4]}"
    return subject


@contextmanager
def nats_producer_span(
    subject: str, headers: MutableMapping[str, str]
) -> Iterator[None]:
    """Trace one publish and replace any stale W3C propagation headers."""
    if not is_enabled():
        yield
        return
    for key in list(headers):
        if str(key).lower() in {"traceparent", "tracestate"}:
            del headers[key]
    with _tracer.start_as_current_span(
        f"{_nats_span_subject(subject)} publish",
        kind=SpanKind.PRODUCER,
        attributes=_nats_attributes(subject, headers, "publish"),
    ):
        _propagator.inject(headers)
        yield


@contextmanager
def nats_consumer_span(msg) -> Iterator[None]:
    """Trace one JetStream delivery attempt without inspecting its payload."""
    if not is_enabled():
        yield
        return
    headers = msg.headers or {}
    attributes = _nats_attributes(msg.subject, headers, "process")
    delivery_count = getattr(getattr(msg, "metadata", None), "num_delivered", None)
    if delivery_count is not None:
        attributes["messaging.message.delivery_count"] = delivery_count
    with _tracer.start_as_current_span(
        f"{_nats_span_subject(msg.subject)} process",
        context=extract_nats_context(headers),
        kind=SpanKind.CONSUMER,
        attributes=attributes,
    ):
        yield
