from __future__ import annotations

from uuid import UUID

from sanic import response

from xarta.services.v1.peppol.nats import RecommandCallbackNATSModel
from xarta.services.v1.peppol.webhooks import accept_recommand_webhook
from xarta.services.v1.peppol.webhooks import apply_e_invoice_be_webhook


async def e_invoice_be_callback(request):
    try:
        applied = await apply_e_invoice_be_webhook(
            request.body,
            request.headers,
            store=request.app.ctx.tracking,
            components=request.app.ctx.peppol_components,
        )
    except PermissionError:
        return response.json({"error": "unauthorized"}, status=401)
    except ValueError as ex:
        return response.json({"error": str(ex)}, status=400)
    except LookupError as ex:
        return response.json({"error": str(ex)}, status=404)
    return response.json({"duplicate": applied.duplicate})


async def recommand_callback(request):
    try:
        queued = await accept_recommand_webhook(
            request.body,
            request.headers,
            store=request.app.ctx.tracking,
            components=request.app.ctx.peppol_components,
            publish=RecommandCallbackNATSModel.publish,
        )
    except PermissionError:
        return response.json({"error": "unauthorized"}, status=401)
    except ValueError as ex:
        return response.json({"error": str(ex)}, status=400)
    return response.json({"accepted": True, "queued": queued})


async def peppol_operation_status(request, operation_id: str):
    try:
        identifier = UUID(operation_id)
        operation = await request.app.ctx.tracking.get_operation(identifier)
    except (KeyError, ValueError):
        return response.json({"error": "operation not found"}, status=404)
    if operation.capability != "peppol":
        return response.json({"error": "operation not found"}, status=404)
    events = await request.app.ctx.tracking.outcomes_for_operation(identifier)
    return response.json(
        {
            "id": str(operation.id),
            "node_execution_id": str(operation.node_execution_id),
            "lifecycle": operation.lifecycle.value,
            "adapter": operation.binding.adapter,
            "configuration_revision": operation.binding.configuration_revision,
            "provider_account_reference": operation.provider_account_reference,
            "provider_document_id": operation.provider_reference,
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
