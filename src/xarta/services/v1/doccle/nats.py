from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

from xarta.exceptions.protocol import TemporaryError
from xarta.logging import log_capability_adapter_selected
from xarta.nats.sanic import SanicNATSSynchronousRequestsConsumerModel
from xarta.protocol.dag import CapabilityResult
from xarta.protocol.dag import CommonOutcome
from xarta.protocol.dag import NodeTask
from xarta.protocol.dag import OutcomeEmission
from xarta.protocol.dag.doccle import DoccleNode
from xarta.services.v1.doccle.adapters import DoccleAmbiguousTransportError
from xarta.services.v1.doccle.adapters import DoccleSafePreTransmissionError
from xarta.services.v1.doccle.models import DoccleDocument
from xarta.services.v1.doccle.models import DoccleResultCategory
from xarta.services.v1.doccle.models import ReceiverState

if TYPE_CHECKING:
    from xarta.services.v1.doccle.repositories import ReceiverRepository
    from xarta.services.v1.doccle.service import DoccleComponents


def _result(outcome: str, details: dict | None = None) -> CapabilityResult:
    return CapabilityResult((OutcomeEmission(outcome=outcome, details=details),))


async def _worker(
    node: DoccleNode,
    task: NodeTask,
    *,
    components: DoccleComponents,
    receiver_repository: ReceiverRepository,
    **kwargs,
) -> CapabilityResult:
    resolved = components.resolve_sender()
    if node.document_type not in resolved.configuration["document_types"]:
        return _result(
            "unsupported_document", {"reason": "document_type_not_configured"}
        )

    if node.receiver.subject is not None:
        receiver = await receiver_repository.load_by_subject(
            resolved.binding.destination, dict(node.receiver.subject)
        )
        if receiver is None:
            return _result(
                "receiver_not_found",
                {"reason": "exact_subject_not_registered", "selector": "subject"},
            )
        if receiver.state is not ReceiverState.PROVISIONED:
            return _result(
                "receiver_not_provisioned",
                {
                    "reason": "receiver_not_provisioned",
                    "provisioning_state": receiver.state.value,
                    "selector": "subject",
                },
            )
        external_receiver_id = receiver.external_receiver_id
    else:
        explicit_receiver_id = node.receiver.id
        if explicit_receiver_id is None:  # guarded by the protocol model
            raise ValueError("Explicit Doccle receiver ID is missing")
        external_receiver_id = explicit_receiver_id

    retrieved = await node.interpret().retrieve()

    document = DoccleDocument(
        document_id=str(task.node_execution_id),
        document_type=node.document_type,
        filename=_filename(node, retrieved),
        content_type=retrieved.content_type,
        content=retrieved.data,
        names=node.name,
        published_at=node.published_at,
    )
    adapter = components.adapters.create(
        resolved.binding.adapter, resolved.configuration
    )
    log = kwargs.get("logger")
    if log is not None:
        await log_capability_adapter_selected(log, resolved.binding, "synchronous")
    try:
        provider_result = await adapter.put_document(
            receiver_id=external_receiver_id, document=document
        )
    except DoccleSafePreTransmissionError as ex:
        raise TemporaryError(
            "Doccle request failed before transmission", delay=5
        ) from ex
    except DoccleAmbiguousTransportError:
        return _result(
            CommonOutcome.OUTCOME_UNCERTAIN,
            {"reason": "provider_result_unknown", "category": "ambiguous_transport"},
        )

    if provider_result.category is DoccleResultCategory.STORED:
        outcome = "stored"
    elif provider_result.category is DoccleResultCategory.RECEIVER_NOT_FOUND:
        outcome = "receiver_not_found"
    else:
        outcome = "document_rejected"
    failure_details = (
        None
        if outcome == "stored"
        else {
            "reason": "provider_rejection",
            "category": provider_result.category.value,
            "provider_status": provider_result.provider_status,
            "selector": node.receiver.mode,
        }
    )
    return _result(outcome, failure_details)


def _filename(node: DoccleNode, retrieved) -> str:
    metadata = retrieved.metadata or {}
    filename = metadata.get("filename")
    if isinstance(filename, str) and filename:
        return filename
    if node.name:
        return next(iter(node.name.values()))
    return f"{retrieved.id}"


class DoccleNATSModel(SanicNATSSynchronousRequestsConsumerModel):
    @classmethod
    async def register(  # type: ignore[override]
        cls,
        app,
        *,
        components: DoccleComponents,
        receiver_repository: ReceiverRepository,
        **kwargs,
    ) -> None:
        await super().register(
            app=app,
            fn=partial(
                _worker,
                components=components,
                receiver_repository=receiver_repository,
            ),
            name=DoccleNode.KIND,
            **kwargs,
        )
