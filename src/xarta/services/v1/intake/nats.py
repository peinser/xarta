r"""Eventing model and utilities for the intake service."""

from __future__ import annotations

import uuid

from typing import TYPE_CHECKING

import orjson

from xarta.nats.sanic import SanicNATSModel
from xarta.protocol.dag import NodeTask
from xarta.telemetry import nats_producer_span

if TYPE_CHECKING:
    from xarta.protocol.document.request.flow import DocumentFlowRequest


class IntakeNATSModel(SanicNATSModel):
    r"""NATS publisher owned by the intake service."""

    @classmethod
    async def schedule(
        cls,
        request: DocumentFlowRequest,
        admission: dict[str, str] | None = None,
    ) -> None:
        task = NodeTask(
            flow_id=request.id,
            node=request.dag,
            node_execution_id=uuid.uuid5(request.id, f"root:{request.dag.id}"),
            correlation_id=request.correlation_id,
            admission=admission,
        )
        subject = (
            f"requests.{task.flow_id}.tasks."
            f"{task.node_execution_id}.{task.node.kind}"
        )
        headers = {
            "flow-id": str(task.flow_id),
            "flow-correlation-id": str(request.correlation_id),
            "Nats-Msg-Id": str(task.node_execution_id),
        }
        with nats_producer_span(subject, headers):
            await cls.jetstream().publish(
                subject=subject,
                payload=orjson.dumps(task.dict(), default=str),
                headers=headers,
            )
