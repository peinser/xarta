from __future__ import annotations

from uuid import UUID

from xarta.services.v1.peppol.models import PeppolDeliveryState
from xarta.services.v1.peppol.models import PeppolParticipant
from xarta.services.v1.peppol.models import PeppolProviderDocument
from xarta.services.v1.peppol.models import PeppolUpdate
from xarta.services.v1.peppol.reducer import reduce_peppol
from xarta.tracking import TrackedCapabilityService

from .e_invoice_be.adapter import parse_untrusted_e_invoice_be_webhook
from .recommand.adapter import parse_untrusted_recommand_webhook


def _header(headers, name: str) -> str | None:
    for key, value in headers.items():
        if str(key).lower() == name:
            return str(value)
    return None


async def apply_e_invoice_be_webhook(raw_body, headers, *, store, components):
    """Authenticate one tenant callback, then re-read and reduce provider state."""
    from xarta.services.v1.peppol.nats import PeppolNATSModel

    payload, tenant_id, provider_document_id = parse_untrusted_e_invoice_be_webhook(
        raw_body
    )
    supplied_signature = headers.get("x-signature") or headers.get("X-Signature")
    candidates = components.callback_candidates("e-invoice-be-rest", tenant_id)
    authenticated = False
    operation = None
    webhook = None
    tenant_client = None
    for binding, adapter in candidates:
        try:
            candidate_webhook = adapter.authenticate_webhook(
                payload, supplied_signature
            )
        except PermissionError:
            continue
        authenticated = True
        candidate_operation = await store.operation_for_provider_reference(
            binding.adapter, tenant_id, provider_document_id
        )
        if candidate_operation is None or candidate_operation.binding != binding:
            continue
        operation = candidate_operation
        webhook = candidate_webhook
        tenant_client = components.bind(
            binding,
            PeppolParticipant(**operation.state["sender"]),
            tenant_id,
        )
        break
    if not authenticated:
        raise PermissionError("Invalid e-invoice.be webhook signature")
    if operation is None or webhook is None or tenant_client is None:
        raise LookupError("Unknown e-invoice.be provider document")

    provider_document = await tenant_client.get_document(provider_document_id)
    tracking = TrackedCapabilityService(
        store, components.destinations, PeppolNATSModel.publish
    )
    return await tracking.feedback(
        operation.id,
        operation.binding.adapter,
        webhook.event_id,
        PeppolUpdate(provider_document),
        reduce_peppol,
        "callback",
    )


async def accept_recommand_webhook(
    raw_body, headers, *, store, components, publish
) -> bool:
    """Authenticate and durably hand off one owned outbound document hint."""
    payload, company_id = parse_untrusted_recommand_webhook(raw_body)
    signature = _header(headers, "x-signature")
    event_id = _header(headers, "x-idempotency-key")
    authenticated = False
    for binding, adapter in components.callback_candidates(
        "recommand-rest", company_id
    ):
        try:
            webhook = adapter.authenticate_webhook(
                raw_body, payload, signature, event_id
            )
        except PermissionError:
            continue
        authenticated = True
        operation = await store.operation_for_provider_reference(
            binding.adapter,
            company_id,
            webhook.provider_document_id,
        )
        if operation is None or operation.binding != binding:
            continue
        await publish(
            operation.id,
            webhook.event_id,
            company_id,
            webhook.provider_document_id,
        )
        return True
    if not authenticated:
        raise PermissionError("Invalid Recommand webhook signature")
    return False


async def apply_recommand_callback_hint(
    operation_id: UUID,
    event_id: str,
    company_id: str,
    provider_document_id: str,
    *,
    store,
    components,
):
    from xarta.services.v1.peppol.nats import PeppolNATSModel

    operation = await store.get_operation(operation_id)
    if (
        operation.binding.adapter != "recommand-rest"
        or operation.provider_account_reference != company_id
        or operation.provider_reference != provider_document_id
    ):
        raise ValueError("Recommand callback does not match the pinned operation")
    provider_document = PeppolProviderDocument(
        "recommand",
        provider_document_id,
        "document_sent",
        PeppolDeliveryState.DELIVERY_CONFIRMED,
    )
    tracking = TrackedCapabilityService(
        store, components.destinations, PeppolNATSModel.publish
    )
    return await tracking.feedback(
        operation.id,
        operation.binding.adapter,
        event_id,
        PeppolUpdate(provider_document),
        reduce_peppol,
        "callback",
    )
