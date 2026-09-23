from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING
from uuid import UUID

import orjson

from xarta.exceptions.protocol import PermanentError
from xarta.execution import ExecutionMode
from xarta.nats.sanic import SanicNATSJobsConsumerModel
from xarta.nats.sanic import SanicNATSRequestsConsumerModel
from xarta.protocol.dag.peppol import PeppolNode
from xarta.services.v1.peppol.service import PeppolComponents
from xarta.services.v1.peppol.service import PeppolService
from xarta.services.v1.peppol.webhooks import apply_recommand_callback_hint
from xarta.telemetry import nats_producer_span

if TYPE_CHECKING:
    from xarta.protocol.dag import NodeTask


async def _worker(node: PeppolNode, task: NodeTask, *, components, **kwargs):
    document = await node.interpret().retrieve()
    service = PeppolService(
        PeppolNATSModel.tracking_store(), components, PeppolNATSModel.publish
    )
    return await service.submit(task, node, document, log=kwargs.get("logger"))


class PeppolNATSModel(SanicNATSRequestsConsumerModel):
    @staticmethod
    def _resolve_task_mode(
        task: NodeTask, components: PeppolComponents
    ) -> ExecutionMode:
        if not isinstance(task.node, PeppolNode):
            raise TypeError("Peppol consumer received a non-Peppol task")
        return components.execution_mode

    @classmethod
    async def register(cls, app, components, **kwargs) -> None:  # type: ignore[override]
        await super().register(
            app=app,
            fn=partial(_worker, components=components),
            name=PeppolNode.KIND,
            execution_mode_resolver=partial(
                cls._resolve_task_mode, components=components
            ),
            **kwargs,
        )


async def _recommand_callback_worker(msg, *, components, **kwargs):
    try:
        payload = orjson.loads(msg.data)
        if not isinstance(payload, dict) or set(payload) != {
            "operation_id",
            "event_id",
            "company_id",
            "provider_document_id",
        }:
            raise ValueError
        operation_id = UUID(payload["operation_id"])
        event_id = payload["event_id"]
        company_id = payload["company_id"]
        provider_document_id = payload["provider_document_id"]
        if not all(
            isinstance(value, str) and value
            for value in (event_id, company_id, provider_document_id)
        ):
            raise ValueError
    except (KeyError, TypeError, ValueError, orjson.JSONDecodeError) as ex:
        raise PermanentError(
            "Recommand callback hint is malformed",
            classification="malformed",
            error_code="malformed_recommand_callback",
        ) from ex
    try:
        return await apply_recommand_callback_hint(
            operation_id,
            event_id,
            company_id,
            provider_document_id,
            store=PeppolNATSModel.tracking_store(),
            components=components,
        )
    except (KeyError, ValueError) as ex:
        raise PermanentError(
            "Recommand callback does not match a tracked operation",
            classification="poison",
            error_code="recommand_callback_operation_mismatch",
        ) from ex


class RecommandCallbackNATSModel(SanicNATSJobsConsumerModel):
    @classmethod
    async def publish(
        cls,
        operation_id: UUID,
        event_id: str,
        company_id: str,
        provider_document_id: str,
    ) -> None:
        subject = "jobs.recommand-peppol-callback.hint"
        headers = {"Nats-Msg-Id": f"recommand:{event_id}"}
        payload = {
            "operation_id": str(operation_id),
            "event_id": event_id,
            "company_id": company_id,
            "provider_document_id": provider_document_id,
        }
        with nats_producer_span(subject, headers):
            await cls.jetstream().publish(
                subject, orjson.dumps(payload), headers=headers
            )

    @classmethod
    async def register(cls, app, components, **kwargs) -> None:  # type: ignore[override]
        await super().register(
            app=app,
            name="recommand-peppol-callback",
            fn=partial(_recommand_callback_worker, components=components),
            **kwargs,
        )
