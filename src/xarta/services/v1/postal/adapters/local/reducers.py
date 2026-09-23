from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from xarta.protocol.dag import OutcomeEmission
from xarta.services.v1.postal.adapters.local.carrier import CarrierStateInterpretation
from xarta.services.v1.postal.adapters.local.workflow import ScanMode
from xarta.tracking import Reduction


def reduce_station_scan(
    state: Mapping[str, Any], update: tuple[ScanMode, str]
) -> Reduction:
    mode, service = update
    phase = {
        ScanMode.START_PRODUCTION: "processing",
        ScanMode.READY_FOR_HANDOVER: "prepared",
        ScanMode.CONFIRM_HANDOVER: "handed_over",
    }[mode]
    outcomes = {
        ScanMode.START_PRODUCTION: (),
        ScanMode.READY_FOR_HANDOVER: (OutcomeEmission("prepared"),),
        ScanMode.CONFIRM_HANDOVER: (OutcomeEmission("handed_over"),),
    }[mode]
    return Reduction(
        state={**state, "phase": phase},
        outcomes=outcomes,
        operation_resolved=mode is ScanMode.CONFIRM_HANDOVER and service == "ordinary",
    )


def reduce_carrier_observation(
    state: Mapping[str, Any], update: CarrierStateInterpretation
) -> Reduction:
    if update.requires_reconciliation or update.semantic_state is None:
        return Reduction(
            state={
                **state,
                "carrier_provider_state": update.provider_state,
                "carrier_reconciliation_required": True,
            },
            outcomes=(),
            operation_resolved=False,
        )
    outcome = (OutcomeEmission(update.outcome),) if update.outcome else ()
    return Reduction(
        state={
            **state,
            "carrier_provider_state": update.provider_state,
            "carrier_state": update.semantic_state.value,
        },
        outcomes=outcome,
        operation_resolved=update.semantic_state.value in {"delivered", "returned"},
    )
