from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Any

from xarta.protocol.dag import OutcomeEmission
from xarta.services.v1.peppol.details import failure_details
from xarta.services.v1.peppol.models import PeppolDeliveryState
from xarta.services.v1.peppol.models import PeppolFailure
from xarta.services.v1.peppol.models import PeppolFailureCategory
from xarta.services.v1.peppol.models import PeppolFailureStage
from xarta.services.v1.peppol.models import PeppolUpdate
from xarta.tracking import Reduction

if TYPE_CHECKING:
    from collections.abc import Mapping


def reduce_peppol(current_state: Mapping[str, Any], update: PeppolUpdate) -> Reduction:
    document = update.provider_document
    state = {
        **current_state,
        "provider_document_id": document.id,
        "provider_state": document.raw_state,
    }
    context = {
        "provider": document.provider,
        "provider_document_id": document.id,
        "provider_state": document.raw_state,
    }
    if document.delivery_state is PeppolDeliveryState.SUBMITTED:
        outcome = "submitted"
        resolved = False
    elif document.delivery_state is PeppolDeliveryState.DELIVERY_CONFIRMED:
        outcome = "delivery_confirmed"
        resolved = True
    elif document.delivery_state is PeppolDeliveryState.DELIVERY_FAILED:
        outcome = "delivery_failed"
        resolved = True
        failure = update.failure or PeppolFailure(
            PeppolFailureStage.DELIVERY,
            PeppolFailureCategory.DELIVERY_FAILED,
            document.provider_code,
            document.provider_message,
            provider=document.provider,
        )
        context = failure_details(
            failure,
            provider_document_id=document.id,
            provider_state=document.raw_state,
        )
        state["last_failure"] = context
    else:
        return Reduction(state=state, outcomes=(), operation_resolved=False)
    if current_state.get("semantic_state") == outcome:
        return Reduction(state=state, outcomes=(), operation_resolved=resolved)
    state["semantic_state"] = outcome
    return Reduction(
        state=state,
        outcomes=(OutcomeEmission(outcome, details=context),),
        operation_resolved=resolved,
    )
