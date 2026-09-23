r"""
Logging utilities and configuration.
"""

from __future__ import annotations

import sys
import traceback

import structlog

from opentelemetry import trace


def _add_trace_context(_, __, event_dict: dict) -> dict:
    context = trace.get_current_span().get_span_context()
    if context.is_valid:
        event_dict["trace_id"] = f"{context.trace_id:032x}"
        event_dict["span_id"] = f"{context.span_id:016x}"
    return event_dict


structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,  # Ensure context vars are included
        _add_trace_context,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ],
    cache_logger_on_first_use=True,
)

logger = structlog.get_logger()


async def log_capability_lifecycle_change(
    log,
    *,
    flow_id,
    correlation_id,
    node_id,
    node_execution_id,
    capability: str,
    execution_mode: str,
    lifecycle: str,
    action: str,
    previous_state: str | None,
    next_state: str,
    previous_version: int | None = None,
    next_version: int | None = None,
    operation_id=None,
    adapter: str | None = None,
    configuration_revision: str | None = None,
    source: str | None = None,
    duplicate: bool = False,
) -> None:
    """Emit the allowlisted lifecycle fields shared by every capability."""
    fields = {
        "flow_id": str(flow_id),
        "correlation_id": str(correlation_id),
        "node_id": str(node_id),
        "node_execution_id": str(node_execution_id),
        "operation_id": str(operation_id) if operation_id is not None else None,
        "capability": capability,
        "adapter": adapter,
        "configuration_revision": configuration_revision,
        "execution_mode": execution_mode,
        "lifecycle": lifecycle,
        "action": action,
        "previous_state": previous_state,
        "next_state": next_state,
        "previous_version": previous_version,
        "next_version": next_version,
        "source": source,
        "duplicate": duplicate,
    }
    await log.ainfo("capability_lifecycle_state_changed", **fields)
    span = trace.get_current_span()
    if span.is_recording():
        span.set_attributes(
            {
                name: value
                for name, value in {
                    "xarta.flow.id": fields["flow_id"],
                    "xarta.correlation.id": fields["correlation_id"],
                    "xarta.node.id": fields["node_id"],
                    "xarta.node_execution.id": fields["node_execution_id"],
                    "xarta.operation.id": fields["operation_id"],
                    "xarta.capability": fields["capability"],
                    "xarta.adapter": fields["adapter"],
                    "xarta.configuration.revision": fields["configuration_revision"],
                    "xarta.execution.mode": fields["execution_mode"],
                }.items()
                if value is not None
            }
        )
        names = {
            "flow_id": "xarta.flow.id",
            "correlation_id": "xarta.correlation.id",
            "node_id": "xarta.node.id",
            "node_execution_id": "xarta.node_execution.id",
            "operation_id": "xarta.operation.id",
            "capability": "xarta.capability",
            "adapter": "xarta.adapter",
            "configuration_revision": "xarta.configuration.revision",
            "execution_mode": "xarta.execution.mode",
            "lifecycle": "xarta.lifecycle",
            "action": "xarta.lifecycle.action",
            "previous_state": "xarta.lifecycle.previous_state",
            "next_state": "xarta.lifecycle.next_state",
            "previous_version": "xarta.lifecycle.previous_version",
            "next_version": "xarta.lifecycle.next_version",
            "source": "xarta.lifecycle.source",
            "duplicate": "xarta.duplicate",
        }
        span.add_event(
            "xarta.capability.lifecycle",
            {names[key]: value for key, value in fields.items() if value is not None},
        )


async def log_capability_adapter_selected(log, binding, execution_mode: str) -> None:
    """Log a resolved adapter binding without its configuration or credentials."""
    await log.ainfo(
        "capability_adapter_selected",
        capability=binding.capability,
        adapter=binding.adapter,
        configuration_revision=binding.configuration_revision,
        execution_mode=execution_mode,
    )
    span = trace.get_current_span()
    if span.is_recording():
        attributes = {
            "xarta.capability": binding.capability,
            "xarta.adapter": binding.adapter,
            "xarta.configuration.revision": binding.configuration_revision,
            "xarta.execution.mode": execution_mode,
        }
        span.set_attributes(attributes)
        span.add_event("xarta.capability.adapter_selected", attributes)


def record_outcome_emitted(outcome) -> None:
    """Add a safe projection of an accepted semantic outcome to the active span."""
    span = trace.get_current_span()
    if not span.is_recording():
        return
    attributes = {
        "xarta.outcome": outcome.outcome,
        "xarta.outcome.event.id": str(outcome.id),
        "xarta.flow.id": str(outcome.flow_id),
        "xarta.node_execution.id": str(outcome.node_execution_id),
    }
    if outcome.subject is not None:
        attributes["xarta.outcome.subject.kind"] = outcome.subject.kind
    span.add_event("xarta.outcome.emitted", attributes)


def structured_exception() -> dict:
    r"""
    Formats a generic Exception into a structured format for
    logging purposes.
    """
    exc_type, exc_value, exc_tb = sys.exc_info()

    # Extract structured traceback
    tb_list = traceback.extract_tb(exc_tb)
    structured_tb = [
        {"filename": tb.filename, "lineno": tb.lineno, "name": tb.name, "line": tb.line}
        for tb in tb_list
    ]

    return {
        "type": str(exc_type.__name__),
        "message": str(exc_value),
        "traceback": structured_tb,
    }
