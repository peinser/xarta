from __future__ import annotations

import pytest

from tests.scenarios.common import _validate_split_trace


def span(
    span_id: str,
    process_id: str,
    operation: str,
    flow_id: str,
    message_id: str,
    parent_id: str | None = None,
) -> dict:
    references = (
        [{"refType": "CHILD_OF", "traceID": "trace", "spanID": parent_id}]
        if parent_id
        else []
    )
    return {
        "traceID": "trace",
        "spanID": span_id,
        "processID": process_id,
        "operationName": operation,
        "references": references,
        "tags": [
            {"key": "xarta.flow.id", "value": flow_id},
            {"key": "messaging.operation.type", "value": operation},
            {"key": "messaging.destination.name", "value": "requests.flow.task"},
            {"key": "messaging.message.id", "value": message_id},
        ],
    }


def test_validate_split_trace_checks_services_and_nats_continuity() -> None:
    trace = {
        "traceID": "trace",
        "processes": {
            "p1": {"serviceName": "xarta-intake"},
            "p2": {"serviceName": "xarta-generate"},
        },
        "spans": [
            span("producer", "p1", "publish", "flow", "message"),
            span("consumer", "p2", "process", "flow", "message", "producer"),
        ],
    }

    _validate_split_trace(trace, "flow", {"xarta-intake", "xarta-generate"})

    trace["processes"]["p2"]["serviceName"] = "wrong-service"
    with pytest.raises(RuntimeError, match="Trace services differ"):
        _validate_split_trace(trace, "flow", {"xarta-intake", "xarta-generate"})
