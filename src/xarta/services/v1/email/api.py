from __future__ import annotations

from uuid import UUID

from sanic import response

from xarta.services.v1.email.resend_webhooks import apply_resend_webhook


async def resend_callback(request, destination: str):
    try:
        applied = await apply_resend_webhook(
            destination,
            request.body,
            request.headers,
            store=request.app.ctx.tracking,
            components=request.app.ctx.email_components,
        )
    except PermissionError:
        return response.json({"error": "unauthorized"}, status=401)
    except ValueError as ex:
        return response.json({"error": str(ex)}, status=400)
    except LookupError as ex:
        return response.json({"error": str(ex)}, status=404)
    return response.json({"duplicate": applied.duplicate})


async def email_operation_status(request, operation_id: str):
    try:
        identifier = UUID(operation_id)
        operation = await request.app.ctx.tracking.get_operation(identifier)
    except (KeyError, ValueError):
        return response.json({"error": "operation not found"}, status=404)
    if operation.capability != "email":
        return response.json({"error": "operation not found"}, status=404)
    events = await request.app.ctx.tracking.outcomes_for_operation(identifier)
    return response.json(
        {
            "id": str(operation.id),
            "node_execution_id": str(operation.node_execution_id),
            "lifecycle": operation.lifecycle.value,
            "destination": operation.binding.destination,
            "adapter": operation.binding.adapter,
            "configuration_revision": operation.binding.configuration_revision,
            "provider_account_reference": operation.provider_account_reference,
            "provider_email_id": operation.provider_reference,
            "smtp_message_id": operation.state.get("smtp_message_id"),
            "state": operation.state,
            "outcomes": [
                {
                    "id": str(event.id),
                    "outcome": event.outcome,
                    "subject": event.subject.dict() if event.subject else None,
                    "details": event.details,
                    "source": event.source,
                    "occurred_at": (
                        event.occurred_at.isoformat() if event.occurred_at else None
                    ),
                    "received_at": event.received_at.isoformat(),
                }
                for event in events
            ],
        }
    )
