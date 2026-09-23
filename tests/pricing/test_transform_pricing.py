from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

from xarta.pricing import PricingEngine
from xarta.pricing import parse_pricing_bindings
from xarta.pricing import parse_pricing_catalog
from xarta.protocol.dag import Node
from xarta.protocol.dag.transform import TransformNode


def test_transform_pricing_resolves_gotenberg_binding_and_single_activation() -> None:
    root = TransformNode(
        convert={
            "document": {"source": "generate", "id": str(uuid4())},
            "out": str(uuid4()),
            "content_type": "application/pdf",
        },
        on={"success": [Node(kind="generate")]},
    )
    configuration = {
        "revision": "transform-v1",
        "currency": "USD",
        "default_markup_multiplier": "1.00",
        "quote_ttl_seconds": 300,
        "rates": [
            {
                "capability": "transform",
                "adapter": "gotenberg-rest",
                "estimated_cost": "0.05",
            },
            {
                "capability": "generate",
                "adapter": "internal-generate",
                "estimated_cost": "0.01",
            },
        ],
        "bindings": [
            {
                "capability": "transform",
                "adapter": "gotenberg-rest",
                "adapter_configuration_revision": "v1",
            },
            {
                "capability": "generate",
                "adapter": "internal-generate",
                "adapter_configuration_revision": "v1",
            },
        ],
    }

    quote = PricingEngine(
        parse_pricing_catalog(configuration),
        parse_pricing_bindings(configuration),
    ).quote_flow(root, resource="intake", request_fingerprint="transform")

    transform = quote.operations[0]
    assert transform.pricing_binding.capability == "transform"
    assert transform.pricing_binding.adapter == "gotenberg-rest"
    assert transform.maximum_activations == 1
    assert transform.worst_case_estimated_cost.amount == Decimal("0.05")
    assert quote.estimated_internal_cost.amount == Decimal("0.06")
