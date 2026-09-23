from __future__ import annotations

import datetime

from decimal import Decimal
from uuid import uuid4

import pytest

from xarta.flow_profiles import calculate_flow_profile_fingerprint
from xarta.flow_profiles import parse_flow_profiles
from xarta.pricing import GraphPricingLimits
from xarta.pricing import Money
from xarta.pricing import PricingBinding
from xarta.pricing import PricingBindingResolver
from xarta.pricing import PricingConfigurationError
from xarta.pricing import PricingEngine
from xarta.pricing import UnpriceableGraphError
from xarta.pricing import maximum_activation_bounds
from xarta.pricing import parse_pricing_bindings
from xarta.pricing import parse_pricing_catalog
from xarta.protocol.dag import Node
from xarta.protocol.dag.bundle import BundleNode
from xarta.protocol.dag.email import EmailNode
from xarta.protocol.dag.peppol import PeppolNode
from xarta.services.v1.intake.capabilities import CapabilityManifest
from xarta.services.v1.intake.preparation import prepare_intake


def catalog(*, markup: str = "1.50", rates: list[dict] | None = None):
    return parse_pricing_catalog(
        {
            "revision": "2026-08-28.1",
            "currency": "USD",
            "default_markup_multiplier": markup,
            "quote_ttl_seconds": 300,
            "rates": rates
            or [
                {
                    "capability": "generate",
                    "adapter": "internal-generate",
                    "estimated_cost": "1.00",
                }
            ],
        }
    )


def resolver(destination: str | None = None) -> PricingBindingResolver:
    return PricingBindingResolver(
        lambda node: PricingBinding(
            capability=node.kind,
            destination=destination,
            adapter={
                "generate": "internal-generate",
                "bundle": "internal-bundle",
                "email": "smtp",
                "peppol": "e-invoice-be-rest",
            }.get(node.kind, "internal"),
            adapter_configuration_revision="v1",
        )
    )


@pytest.mark.parametrize(
    ("multiplier", "expected"),
    [("1.50", "1.500"), ("1.00", "1.000"), ("0.50", "0.500"), ("0", "0.00")],
)
def test_markup_multiplier_uses_decimal_selling_price(
    multiplier: str, expected: str
) -> None:
    quote = PricingEngine(catalog(markup=multiplier), resolver()).quote_flow(
        Node(kind="generate"), resource="intake", request_fingerprint="abc"
    )

    assert quote.selling_price.amount == Decimal(expected)
    assert isinstance(quote.selling_price.amount, Decimal)


def test_negative_markup_and_binary_float_are_rejected() -> None:
    with pytest.raises(PricingConfigurationError, match="negative"):
        catalog(markup="-0.01")
    with pytest.raises(TypeError, match="binary float"):
        Money(1.1, "USD")  # type: ignore[arg-type]


def test_adapter_and_destination_specific_pricing_is_explicit() -> None:
    pricing_catalog = catalog(
        rates=[
            {
                "capability": "email",
                "adapter": "smtp",
                "estimated_cost": "0.10",
            },
            {
                "capability": "email",
                "adapter": "commercial-email",
                "estimated_cost": "0.20",
            },
            {
                "capability": "email",
                "adapter": "smtp",
                "destination": "subsidized",
                "estimated_cost": "0.01",
            },
        ]
    )

    assert pricing_catalog.require_rate(
        "email", "smtp", "other"
    ).estimated_cost.amount == Decimal("0.10")
    assert pricing_catalog.require_rate(
        "email", "commercial-email", None
    ).estimated_cost.amount == Decimal("0.20")
    assert pricing_catalog.require_rate(
        "email", "smtp", "subsidized"
    ).estimated_cost.amount == Decimal("0.01")
    with pytest.raises(PricingConfigurationError, match="No pricing rate"):
        pricing_catalog.require_rate("email", "unknown", None)


def test_quote_pins_catalog_revision_and_expiry() -> None:
    now = datetime.datetime(2026, 8, 28, tzinfo=datetime.UTC)
    quote = PricingEngine(catalog(), resolver()).quote_flow(
        Node(kind="generate"),
        resource="intake",
        request_fingerprint="request",
        now=now,
    )

    assert quote.pricing_revision == "2026-08-28.1"
    assert quote.expires_at == now + datetime.timedelta(seconds=300)


def test_deployment_pricing_bindings_resolve_node_destination() -> None:
    bindings = parse_pricing_bindings(
        {
            "bindings": [
                {
                    "capability": "email",
                    "destination": "billing",
                    "adapter": "resend-rest",
                    "adapter_configuration_revision": "v3",
                }
            ]
        }
    )
    node = EmailNode(
        destination="billing",
        to="buyer@example.com",
        sender="seller@example.com",
        body={},
    )

    assert bindings.resolve(node) == PricingBinding(
        capability="email",
        destination="billing",
        adapter="resend-rest",
        adapter_configuration_revision="v3",
    )


def test_linear_and_conservative_branch_activation_bounds() -> None:
    terminal = Node(kind="generate")
    success = Node(kind="generate", on={"success": [terminal]})
    failure = Node(kind="generate")
    root = Node(kind="generate", on={"success": [success], "failure": [failure]})

    bounds = maximum_activation_bounds(root)

    assert bounds[root.id] == 1
    assert bounds[success.id] == 1
    assert bounds[failure.id] == 1
    assert bounds[terminal.id] == 1


def test_bundle_pricing_resolves_explicit_binding_and_rate() -> None:
    successor = Node(kind="generate")
    root = BundleNode(
        documents=[{"source": "generate", "id": str(uuid4()), "filename": "input.txt"}],
        out=uuid4(),
        on={"success": [successor]},
    )
    pricing_catalog = catalog(
        markup="1.00",
        rates=[
            {
                "capability": "bundle",
                "adapter": "internal-bundle",
                "estimated_cost": "0.25",
            },
            {
                "capability": "generate",
                "adapter": "internal-generate",
                "estimated_cost": "0.10",
            },
        ],
    )

    quote = PricingEngine(pricing_catalog, resolver()).quote_flow(
        root, resource="intake", request_fingerprint="bundle"
    )

    bundle = quote.operations[0]
    assert bundle.pricing_binding == PricingBinding(
        capability="bundle",
        destination=None,
        adapter="internal-bundle",
        adapter_configuration_revision="v1",
    )
    assert bundle.maximum_activations == 1
    assert bundle.worst_case_estimated_cost.amount == Decimal("0.25")
    assert quote.estimated_internal_cost.amount == Decimal("0.35")


def test_same_successor_contributions_are_added() -> None:
    shared = Node(kind="generate")
    root = Node(kind="generate", on={"success": [shared], "failure": [shared]})

    assert maximum_activation_bounds(root)[shared.id] == 2


def test_duplicate_node_id_requires_one_semantic_specification() -> None:
    shared_id = uuid4()
    exact_a = Node(kind="generate", id=shared_id)
    exact_b = Node(kind="generate", id=shared_id)
    root = Node(kind="generate", on={"success": [exact_a], "failure": [exact_b]})
    assert maximum_activation_bounds(root)[shared_id] == 2

    changed_payload_a = PeppolNode(
        id=shared_id,
        document={"source": "archive", "id": str(uuid4())},
    )
    changed_payload_b = PeppolNode(
        id=shared_id,
        document={"source": "archive", "id": str(uuid4())},
    )
    with pytest.raises(UnpriceableGraphError, match="conflicting specifications"):
        maximum_activation_bounds(
            Node(
                kind="generate",
                on={"success": [changed_payload_a], "failure": [changed_payload_b]},
            )
        )

    changed_edges_a = Node(
        kind="generate", id=shared_id, on={"success": [Node(kind="generate")]}
    )
    changed_edges_b = Node(
        kind="generate", id=shared_id, on={"failure": [Node(kind="generate")]}
    )
    with pytest.raises(UnpriceableGraphError, match="conflicting specifications"):
        maximum_activation_bounds(
            Node(
                kind="generate",
                on={"success": [changed_edges_a], "failure": [changed_edges_b]},
            )
        )

    with pytest.raises(UnpriceableGraphError, match="conflicting specifications"):
        maximum_activation_bounds(
            Node(
                kind="generate",
                on={
                    "success": [Node(kind="generate", id=shared_id)],
                    "failure": [Node(kind="webhook", id=shared_id)],
                },
            )
        )


def test_email_recipient_outcome_multiplies_successor_activation() -> None:
    successor = Node(kind="generate")
    email = EmailNode(
        to="one@example.com",
        cc=["two@example.com"],
        bcc=["three@example.com"],
        sender="sender@example.com",
        body={},
        on={"mailbox_full": [successor], "all_failed": [Node(kind="generate")]},
    )

    bounds = maximum_activation_bounds(email)

    assert bounds[successor.id] == 3
    assert sum(value for node_id, value in bounds.items() if node_id != email.id) == 4


def test_peppol_submitted_and_terminal_branches_are_both_included() -> None:
    submitted = Node(kind="generate")
    delivered = Node(kind="generate")
    failed = Node(kind="generate")
    peppol = PeppolNode(
        document={"source": "archive", "id": str(uuid4())},
        on={
            "submitted": [submitted],
            "delivery_confirmed": [delivered],
            "delivery_failed": [failed],
        },
    )

    bounds = maximum_activation_bounds(peppol)

    assert bounds[submitted.id] == bounds[delivered.id] == bounds[failed.id] == 1


def test_peppol_pricing_uses_server_binding_without_destination() -> None:
    peppol = PeppolNode(document={"source": "archive", "id": str(uuid4())})
    quote = PricingEngine(
        catalog(
            rates=[
                {
                    "capability": "peppol",
                    "adapter": "e-invoice-be-rest",
                    "estimated_cost": "0.25",
                }
            ]
        ),
        resolver(),
    ).quote_flow(peppol, resource="intake", request_fingerprint="peppol")

    binding = quote.operations[0].pricing_binding
    assert binding.capability == "peppol"
    assert binding.destination is None
    assert binding.adapter == "e-invoice-be-rest"
    assert binding.adapter_configuration_revision == "v1"


def test_profile_pricing_uses_compiled_dag_and_intake_fingerprint() -> None:
    configured = {
        "schema_version": 1,
        "profiles": {
            "generated-standard": {
                "current_version": 1,
                "versions": {
                    "1": {
                        "inputs": {},
                        "dag": {
                            "node_key": "generate",
                            "kind": "generate",
                            "documents": [],
                        },
                    }
                },
            }
        },
    }
    profile_definition = configured["profiles"]["generated-standard"]["versions"]["1"]
    profile_definition["fingerprint"] = calculate_flow_profile_fingerprint(
        "generated-standard", 1, profile_definition
    )
    profiles = parse_flow_profiles(configured)
    prepared = prepare_intake(
        {
            "delivery_profile": "generated-standard@1",
            "delivery_values": {},
        },
        capabilities=CapabilityManifest(frozenset({"generate"})),
        profiles=profiles,
    )

    quote = PricingEngine(catalog(), resolver()).quote_flow(
        prepared.flow.dag,
        resource="intake",
        request_fingerprint=prepared.semantic_fingerprint,
    )

    assert quote.request_fingerprint == prepared.semantic_fingerprint
    assert quote.operations[0].pricing_binding.capability == "generate"


def test_cycle_unknown_cardinality_and_safety_limits_fail_closed() -> None:
    first = Node(kind="generate")
    second = Node(kind="generate", on={"success": [first]})
    first.on = {"success": [second]}
    with pytest.raises(UnpriceableGraphError, match="cycle"):
        maximum_activation_bounds(first)

    with pytest.raises(UnpriceableGraphError, match="No explicit"):
        maximum_activation_bounds(
            Node(kind="debug", on={"success": [Node(kind="generate")]})
        )

    root = Node(kind="generate", on={"success": [Node(kind="generate")]})
    with pytest.raises(UnpriceableGraphError, match="Static node"):
        maximum_activation_bounds(root, GraphPricingLimits(maximum_static_nodes=1))
