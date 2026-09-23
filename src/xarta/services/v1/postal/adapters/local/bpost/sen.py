from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from xarta.services.v1.postal.adapters.local.carrier import CarrierSemanticState
from xarta.services.v1.postal.adapters.local.carrier import CarrierStateInterpretation
from xarta.services.v1.postal.adapters.local.carrier import interpret_semantic_state


class AddressFeedbackCategory(StrEnum):
    ACCEPTED = "accepted"
    FORMATTING_ONLY = "formatting_only"
    MATERIAL_CHANGE = "material_change"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class SenContractMapping:
    revision: str
    state_mappings: Mapping[str, CarrierSemanticState]
    address_feedback_mappings: Mapping[str, AddressFeedbackCategory]

    def __post_init__(self) -> None:
        if (
            not self.revision
            or not self.state_mappings
            or not self.address_feedback_mappings
        ):
            raise ValueError("SEN mappings must be contract-configured and revisioned")
        if any(
            not code for code in (*self.state_mappings, *self.address_feedback_mappings)
        ):
            raise ValueError("SEN provider codes must be non-empty")
        if any(
            not isinstance(state, CarrierSemanticState)
            for state in self.state_mappings.values()
        ):
            raise ValueError("SEN state mappings must use carrier semantic states")
        if any(
            not isinstance(category, AddressFeedbackCategory)
            for category in self.address_feedback_mappings.values()
        ):
            raise ValueError(
                "SEN address mappings must use address feedback categories"
            )
        object.__setattr__(
            self, "state_mappings", MappingProxyType(dict(self.state_mappings))
        )
        object.__setattr__(
            self,
            "address_feedback_mappings",
            MappingProxyType(dict(self.address_feedback_mappings)),
        )


@dataclass(frozen=True, slots=True)
class SenSearchMatch:
    customer_reference: str
    provider_item_id: str
    provider_barcode: str


@dataclass(frozen=True, slots=True)
class AmbiguityReconciliation:
    match: SenSearchMatch | None
    requires_manual_reconciliation: bool
    reason: str | None


@dataclass(frozen=True, slots=True)
class AddressFeedback:
    provider_code: str
    category: AddressFeedbackCategory
    accepted: bool
    requires_reconciliation: bool


def interpret_provider_state(
    provider_state: str, mapping: SenContractMapping
) -> CarrierStateInterpretation:
    return interpret_semantic_state(
        provider_state, mapping.state_mappings.get(provider_state)
    )


def reconcile_ambiguous_announcement(
    customer_reference: str, matches: tuple[SenSearchMatch, ...]
) -> AmbiguityReconciliation:
    if not customer_reference:
        raise ValueError("Customer reference must be non-empty")
    exact = tuple(
        match for match in matches if match.customer_reference == customer_reference
    )
    if len(exact) == 1:
        return AmbiguityReconciliation(exact[0], False, None)
    reason = "no_exact_match" if not exact else "multiple_exact_matches"
    return AmbiguityReconciliation(None, True, reason)


def categorize_address_feedback(
    provider_code: str, mapping: SenContractMapping
) -> AddressFeedback:
    category = mapping.address_feedback_mappings.get(
        provider_code, AddressFeedbackCategory.UNKNOWN
    )
    if category in {
        AddressFeedbackCategory.ACCEPTED,
        AddressFeedbackCategory.FORMATTING_ONLY,
    }:
        return AddressFeedback(provider_code, category, True, False)
    if category is AddressFeedbackCategory.MATERIAL_CHANGE:
        return AddressFeedback(provider_code, category, False, False)
    return AddressFeedback(provider_code, category, False, True)
