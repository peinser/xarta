from __future__ import annotations

import hashlib

from datetime import date
from decimal import Decimal

import pytest

from xarta.services.v1.postal.adapters.local.bpost import AddressFeedbackCategory
from xarta.services.v1.postal.adapters.local.bpost import BpostProductionInstructionResolver  # fmt: skip
from xarta.services.v1.postal.adapters.local.bpost import DepositItem
from xarta.services.v1.postal.adapters.local.bpost import PortPaidContext
from xarta.services.v1.postal.adapters.local.bpost import PortPaidEligibilityPolicy
from xarta.services.v1.postal.adapters.local.bpost import PortPaidProfile
from xarta.services.v1.postal.adapters.local.bpost import SenContractMapping
from xarta.services.v1.postal.adapters.local.bpost import SenSearchMatch
from xarta.services.v1.postal.adapters.local.bpost import categorize_address_feedback
from xarta.services.v1.postal.adapters.local.bpost import group_deposit_items
from xarta.services.v1.postal.adapters.local.bpost import interpret_provider_state
from xarta.services.v1.postal.adapters.local.bpost import plan_franking
from xarta.services.v1.postal.adapters.local.bpost import reconcile_ambiguous_announcement  # fmt: skip
from xarta.services.v1.postal.adapters.local.bpost import validate_port_paid_asset
from xarta.services.v1.postal.adapters.local.carrier import CarrierSemanticState
from xarta.services.v1.postal.adapters.local.models import FrankingMethod


def _stamp_profile() -> dict:
    return {
        "revision": "profile-7",
        "products": [
            {"reference": "unit", "description": "Bpost unit stamp"},
            {"reference": "supplement", "description": "Bpost supplement stamp"},
        ],
        "tariff": {
            "revision": "tariff-4",
            "currency": "EUR",
            "unit_prices": {"unit": "1.25", "supplement": "0.40"},
        },
        "policy": {
            "revision": "policy-3",
            "rules": [
                {
                    "service_type": "ordinary",
                    "speed": "priority",
                    "maximum_weight_g": "50",
                    "supplies": [{"reference": "unit", "quantity": 1}],
                },
                {
                    "service_type": "ordinary",
                    "speed": "priority",
                    "maximum_weight_g": "100",
                    "supplies": [
                        {"reference": "unit", "quantity": 2},
                        {"reference": "supplement", "quantity": 1},
                    ],
                },
            ],
        },
    }


def test_bpost_stamp_resolution_selects_boundary_and_snapshots_exact_prices() -> None:
    resolver = BpostProductionInstructionResolver({"letters": _stamp_profile()})
    light = resolver.resolve(
        "letters",
        service_type="ordinary",
        speed="priority",
        estimated_weight_g=Decimal(50),
    )
    heavy = resolver.resolve(
        "letters",
        service_type="ordinary",
        speed="priority",
        estimated_weight_g=Decimal("50.01"),
    )
    assert (
        light.provider_id,
        light.provider_profile_id,
        light.provider_profile_revision,
    ) == (
        "bpost",
        "letters",
        "profile-7",
    )
    assert (light.policy_revision, light.tariff_revision, light.currency) == (
        "policy-3",
        "tariff-4",
        "EUR",
    )
    assert [
        (line.quantity, line.unit_price, line.amount) for line in heavy.pricing
    ] == [
        (2, Decimal("1.25"), Decimal("2.50")),
        (1, Decimal("0.40"), Decimal("0.40")),
    ]
    assert heavy.total_postage == Decimal("2.90")
    assert [supply.reference for supply in heavy.supplies] == ["unit", "supplement"]
    with pytest.raises(ValueError, match="No Bpost manual stamp rule"):
        resolver.resolve(
            "letters",
            service_type="ordinary",
            speed="priority",
            estimated_weight_g=Decimal("100.01"),
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda profile: profile["products"].append(dict(profile["products"][0])),
            "Duplicate",
        ),
        (
            lambda profile: profile["policy"]["rules"][0]["supplies"].append(
                {"reference": "unknown", "quantity": 1}
            ),
            "unknown products",
        ),
        (
            lambda profile: profile["policy"]["rules"].insert(
                1, dict(profile["policy"]["rules"][0])
            ),
            "strictly increasing",
        ),
        (
            lambda profile: profile["policy"]["rules"][0].update(maximum_weight_g="0"),
            "rule is invalid",
        ),
        (
            lambda profile: profile["tariff"]["unit_prices"].pop("unit"),
            "missing products",
        ),
        (lambda profile: profile["tariff"]["unit_prices"].update(unit="0"), "positive"),
    ],
)
def test_bpost_stamp_configuration_fails_closed(mutation, message: str) -> None:
    profile = _stamp_profile()
    mutation(profile)
    with pytest.raises(ValueError, match=message):
        BpostProductionInstructionResolver({"letters": profile})


def _port_paid() -> tuple[PortPaidProfile, PortPaidEligibilityPolicy, bytes]:
    asset = b"approved-port-paid-mark"
    profile = PortPaidProfile(
        "pb-main",
        "7",
        True,
        "PB-CONTRACT-VALUE",
        "assets/pb.svg",
        hashlib.sha256(asset).hexdigest(),
        Decimal(30),
        Decimal(20),
        date(2026, 1, 1),
        date(2026, 12, 31),
    )
    policy = PortPaidEligibilityPolicy(
        "ordinary-be",
        "4",
        "ordinary",
        "non_priority",
        2,
        100,
        frozenset({"manual_e_masspost"}),
        frozenset({"brussels"}),
        (
            "service_type",
            "speed",
            "postal_format",
            "weight_band",
            "profile_id",
            "site",
            "channel",
            "service_date",
        ),
    )
    return profile, policy, asset


def _sen_mapping() -> SenContractMapping:
    return SenContractMapping(
        "contract-2026-1",
        {
            "IN_NETWORK": CarrierSemanticState.CARRIER_ACCEPTED,
            "DONE": CarrierSemanticState.DELIVERED,
        },
        {
            "OK": AddressFeedbackCategory.ACCEPTED,
            "NORMALIZED": AddressFeedbackCategory.FORMATTING_ONLY,
            "CHANGED": AddressFeedbackCategory.MATERIAL_CHANGE,
        },
    )


def test_port_paid_assets_eligibility_fallback_and_registered_fail_closed() -> None:
    profile, policy, asset = _port_paid()
    validate_port_paid_asset(
        profile,
        asset,
        measured_width_mm=Decimal(30),
        measured_height_mm=Decimal(20),
        on_date=date(2026, 6, 1),
    )
    with pytest.raises(ValueError, match="checksum"):
        validate_port_paid_asset(
            profile,
            b"tampered",
            measured_width_mm=Decimal(30),
            measured_height_mm=Decimal(20),
            on_date=date(2026, 6, 1),
        )

    eligible = PortPaidContext(
        "ordinary",
        "non_priority",
        10,
        "brussels",
        "manual_e_masspost",
        date(2026, 6, 1),
    )
    assert (
        plan_franking(
            profile=profile, policy=policy, context=eligible, stamps_fallback=True
        ).method
        is FrankingMethod.PORT_PAID
    )
    ineligible = PortPaidContext(
        "ordinary", "priority", 1, "brussels", "manual_e_masspost", date(2026, 6, 1)
    )
    assert (
        plan_franking(
            profile=profile, policy=policy, context=ineligible, stamps_fallback=True
        ).method
        is FrankingMethod.STAMPS
    )
    with pytest.raises(ValueError, match="No contract-configured"):
        plan_franking(
            profile=profile,
            policy=None,
            context=PortPaidContext(
                "registered",
                None,
                10,
                "brussels",
                "manual_e_masspost",
                date(2026, 6, 1),
            ),
            stamps_fallback=False,
        )


def test_port_paid_deposits_group_by_contract_policy_dimensions() -> None:
    _, policy, _ = _port_paid()
    common = (
        "ordinary",
        "non_priority",
        "small",
        "0-50g",
        "pb-main",
        "brussels",
        "manual_e_masspost",
    )
    items = (
        DepositItem("op-b", *common, date(2026, 6, 2)),
        DepositItem("op-a", *common, date(2026, 6, 2)),
        DepositItem("op-c", *common, date(2026, 6, 3)),
    )
    groups = group_deposit_items(items, policy)
    assert len(groups) == 2
    assert [item.operation_id for item in next(iter(groups.values()))] == [
        "op-a",
        "op-b",
    ]


@pytest.mark.parametrize("service_date", [None, date(2025, 12, 31), date(2027, 1, 1)])
def test_port_paid_planning_fails_closed_without_an_effective_profile(
    service_date: date | None,
) -> None:
    profile, policy, _ = _port_paid()
    context = PortPaidContext(
        "ordinary", "non_priority", 10, "brussels", "manual_e_masspost", service_date
    )
    with pytest.raises(ValueError, match="No contract-configured"):
        plan_franking(
            profile=profile,
            policy=policy,
            context=context,
            stamps_fallback=False,
        )
    assert (
        plan_franking(
            profile=profile,
            policy=policy,
            context=context,
            stamps_fallback=True,
        ).method
        is FrankingMethod.STAMPS
    )


def test_sen_mapping_ambiguity_and_address_feedback_fail_closed() -> None:
    mapping = _sen_mapping()
    assert interpret_provider_state("DONE", mapping).outcome == "delivered"
    unknown = interpret_provider_state("UNDOCUMENTED", mapping)
    assert unknown.semantic_state is None and unknown.requires_reconciliation

    match = SenSearchMatch("customer-1", "provider-1", "barcode-1")
    assert reconcile_ambiguous_announcement("customer-1", (match,)).match is match
    multiple = reconcile_ambiguous_announcement("customer-1", (match, match))
    assert multiple.match is None and multiple.reason == "multiple_exact_matches"
    assert categorize_address_feedback("NORMALIZED", mapping).accepted
    assert not categorize_address_feedback("CHANGED", mapping).accepted
    unconfigured = categorize_address_feedback("MYSTERY", mapping)
    assert not unconfigured.accepted and unconfigured.requires_reconciliation


def test_sen_contract_mappings_are_snapshotted() -> None:
    states = {"DONE": CarrierSemanticState.DELIVERED}
    feedback = {"OK": AddressFeedbackCategory.ACCEPTED}
    mapping = SenContractMapping("1", states, feedback)
    states["NEW"] = CarrierSemanticState.ANNOUNCED
    feedback["CHANGED"] = AddressFeedbackCategory.MATERIAL_CHANGE
    assert "NEW" not in mapping.state_mappings
    assert "CHANGED" not in mapping.address_feedback_mappings
