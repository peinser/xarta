from __future__ import annotations

import re

from collections.abc import Mapping

from xarta.protocol.dag import sanitize_outcome_message
from xarta.services.v1.email.nats import EmailNATSModel
from xarta.services.v1.email.resend import ResendEmailAdapter
from xarta.services.v1.email.tracking import AsyncEmailEventType
from xarta.services.v1.email.tracking import AsyncEmailUpdate
from xarta.services.v1.email.tracking import EmailRecipientStatus
from xarta.services.v1.email.tracking import reduce_async_email
from xarta.tracking import TrackedCapabilityService
from xarta.tracking.models import AppliedReduction


async def apply_resend_webhook(
    destination: str, raw_body: bytes, headers, *, store, components
) -> AppliedReduction:
    authenticated = False
    for resolved in components.destinations.iter_resolved("email"):
        if (
            resolved.binding.destination != destination
            or resolved.binding.adapter != "resend-rest"
        ):
            continue
        adapter = components.adapters.create(
            resolved.binding.adapter, resolved.configuration
        )
        if not isinstance(adapter, ResendEmailAdapter):
            continue
        try:
            event = adapter.authenticate_webhook(raw_body, headers)
        except PermissionError:
            continue
        authenticated = True
        operation = await store.operation_for_provider_reference(
            resolved.binding.adapter,
            adapter.configuration.account_id,
            event.email_id,
        )
        if operation is None or operation.binding != resolved.binding:
            continue
        update = normalize_resend_event(event.event_type, event.recipients, event.data)
        if update is None:
            return AppliedReduction(duplicate=True)
        update = AsyncEmailUpdate(
            update.event_type,
            update.recipients,
            update.failure_status,
            update.details,
            event.occurred_at,
            update.provider_message_id,
        )
        tracking = TrackedCapabilityService(
            store, components.destinations, EmailNATSModel.publish
        )
        return await tracking.feedback(
            operation.id,
            operation.binding.adapter,
            event.event_id,
            update,
            reduce_async_email,
            "callback",
        )
    if not authenticated:
        raise PermissionError("Invalid Resend webhook signature")
    raise LookupError("Unknown Resend email")


def normalize_resend_event(
    event_type: str, recipients: tuple[str, ...], data: Mapping
) -> AsyncEmailUpdate | None:
    details = {"provider": "resend", "provider_event": event_type}
    message_id = data.get("message_id")
    provider_message_id = (
        message_id if isinstance(message_id, str) and message_id else None
    )
    if event_type == "email.sent":
        return AsyncEmailUpdate(
            AsyncEmailEventType.ACCEPTED,
            recipients,
            details=details,
            provider_message_id=provider_message_id,
        )
    if event_type == "email.delivery_delayed":
        return AsyncEmailUpdate(
            AsyncEmailEventType.DELIVERY_DELAYED,
            recipients,
            details=details,
            provider_message_id=provider_message_id,
        )
    if event_type == "email.delivered":
        return AsyncEmailUpdate(
            AsyncEmailEventType.DELIVERED,
            recipients,
            details=details,
            provider_message_id=provider_message_id,
        )
    if event_type == "email.complained":
        return AsyncEmailUpdate(
            AsyncEmailEventType.COMPLAINED,
            recipients,
            details=details,
            provider_message_id=provider_message_id,
        )
    if event_type == "email.bounced":
        failure, evidence = _bounce_failure(data.get("bounce"))
        return AsyncEmailUpdate(
            AsyncEmailEventType.DELIVERY_FAILED,
            recipients,
            failure,
            {**details, **evidence},
            provider_message_id=provider_message_id,
        )
    if event_type in {"email.failed", "email.suppressed"}:
        evidence = data.get("failed") or data.get("suppressed") or {}
        reason = evidence.get("reason") or evidence.get("type")
        message = evidence.get("message")
        return AsyncEmailUpdate(
            AsyncEmailEventType.DELIVERY_FAILED,
            recipients,
            EmailRecipientStatus.MESSAGE_REJECTED,
            {
                **details,
                "reason": str(reason)[:200] if reason else None,
                "message": sanitize_outcome_message(str(message)) if message else None,
            },
            provider_message_id=provider_message_id,
        )
    return None


def _bounce_failure(value) -> tuple[EmailRecipientStatus, dict]:
    bounce = value if isinstance(value, Mapping) else {}
    diagnostics = bounce.get("diagnosticCode") or []
    if not isinstance(diagnostics, list):
        diagnostics = []
    combined = " ".join(str(item) for item in diagnostics)
    status_match = re.search(r"\b[245]\.\d{1,3}\.\d{1,3}\b", combined)
    enhanced_status = status_match.group(0) if status_match else None
    if enhanced_status in {"4.2.2", "5.2.2"}:
        failure = EmailRecipientStatus.MAILBOX_FULL
    elif enhanced_status == "5.1.1":
        failure = EmailRecipientStatus.RECIPIENT_UNKNOWN
    else:
        failure = EmailRecipientStatus.MESSAGE_REJECTED
    message = bounce.get("message")
    return failure, {
        "bounce_type": str(bounce.get("type"))[:100] if bounce.get("type") else None,
        "bounce_subtype": (
            str(bounce.get("subType"))[:100] if bounce.get("subType") else None
        ),
        "enhanced_status": enhanced_status,
        "message": sanitize_outcome_message(str(message)) if message else None,
    }
