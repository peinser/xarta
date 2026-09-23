from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class CarrierSemanticState(StrEnum):
    ANNOUNCED = "announced"
    CARRIER_ACCEPTED = "carrier_accepted"
    OUT_FOR_DELIVERY = "out_for_delivery"
    AVAILABLE_FOR_PICKUP = "available_for_pickup"
    DELIVERY_EXCEPTION = "delivery_exception"
    DELIVERED = "delivered"
    RETURNED = "returned"


@dataclass(frozen=True, slots=True)
class CarrierStateInterpretation:
    provider_state: str
    semantic_state: CarrierSemanticState | None
    outcome: str | None
    requires_reconciliation: bool


_GENERIC_OUTCOMES = {
    CarrierSemanticState.CARRIER_ACCEPTED: "carrier_accepted",
    CarrierSemanticState.OUT_FOR_DELIVERY: "out_for_delivery",
    CarrierSemanticState.AVAILABLE_FOR_PICKUP: "available_for_pickup",
    CarrierSemanticState.DELIVERY_EXCEPTION: "delivery_exception",
    CarrierSemanticState.DELIVERED: "delivered",
    CarrierSemanticState.RETURNED: "returned",
}


def carrier_outcome(semantic_state: CarrierSemanticState) -> str | None:
    return _GENERIC_OUTCOMES.get(semantic_state)


def interpret_semantic_state(
    provider_state: str, semantic_state: CarrierSemanticState | None
) -> CarrierStateInterpretation:
    if semantic_state is None:
        return CarrierStateInterpretation(provider_state, None, None, True)
    return CarrierStateInterpretation(
        provider_state,
        semantic_state,
        carrier_outcome(semantic_state),
        False,
    )
