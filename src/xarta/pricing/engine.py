from __future__ import annotations

import datetime

from collections.abc import Callable
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

from xarta.pricing.calculators import CapabilityPricingCalculator
from xarta.pricing.calculators import FixedRatePricingCalculator
from xarta.pricing.configuration import PricingCatalog
from xarta.pricing.graph import GraphPricingLimits
from xarta.pricing.graph import collect_static_nodes
from xarta.pricing.graph import maximum_activation_bounds
from xarta.pricing.models import Money
from xarta.pricing.models import OperationPricingEstimate
from xarta.pricing.models import PriceQuote
from xarta.pricing.models import PricingBinding
from xarta.protocol.dag import Node


class PricingBindingError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PricingBindingResolver:
    resolve_node: Callable[[Node], PricingBinding]

    def resolve(self, node: Node) -> PricingBinding:
        binding = self.resolve_node(node)
        if binding.capability != node.kind:
            raise PricingBindingError(
                f"Pricing binding capability {binding.capability} does not match {node.kind}"
            )
        return binding


class PricingEngine:
    def __init__(
        self,
        catalog: PricingCatalog,
        binding_resolver: PricingBindingResolver,
        *,
        graph_limits: GraphPricingLimits = GraphPricingLimits(),
        calculators: (
            Mapping[tuple[str, str], CapabilityPricingCalculator] | None
        ) = None,
    ) -> None:
        self.catalog = catalog
        self.binding_resolver = binding_resolver
        self.graph_limits = graph_limits
        self.calculators = dict(calculators or {})
        self.fixed_rate_calculator = FixedRatePricingCalculator()

    def quote_flow(
        self,
        root: Node,
        *,
        resource: str,
        request_fingerprint: str,
        now: datetime.datetime | None = None,
    ) -> PriceQuote:
        created_at = now or datetime.datetime.now(datetime.UTC)
        nodes = collect_static_nodes(root, self.graph_limits)
        activation_bounds = maximum_activation_bounds(
            root, self.graph_limits, nodes=nodes
        )
        estimated_internal_cost = Money(Decimal(0), self.catalog.currency)
        selling_price = Money(Decimal(0), self.catalog.currency)
        operation_estimates = []
        for node_id, node in nodes.items():
            pricing_binding = self.binding_resolver.resolve(node)
            pricing_rate = self.catalog.require_rate(
                pricing_binding.capability,
                pricing_binding.adapter,
                pricing_binding.destination,
            )
            maximum_activations = activation_bounds[node_id]
            calculator = self.calculators.get(
                (pricing_binding.capability, pricing_binding.adapter),
                self.fixed_rate_calculator,
            )
            estimated_cost_per_activation = calculator.estimate(
                node, pricing_binding, pricing_rate
            )
            if estimated_cost_per_activation.currency != self.catalog.currency:
                raise ValueError(
                    "Pricing calculator currency does not match the catalog"
                )
            worst_case_estimated_cost = estimated_cost_per_activation.multiply(
                maximum_activations
            )
            markup_multiplier = pricing_rate.resolve_markup_multiplier(
                self.catalog.default_markup_multiplier
            )
            quoted_selling_price = worst_case_estimated_cost.multiply(markup_multiplier)
            estimated_internal_cost = estimated_internal_cost.add(
                worst_case_estimated_cost
            )
            selling_price = selling_price.add(quoted_selling_price)
            operation_estimates.append(
                OperationPricingEstimate(
                    node_id=node_id,
                    pricing_binding=pricing_binding,
                    maximum_activations=maximum_activations,
                    estimated_cost_per_activation=estimated_cost_per_activation,
                    worst_case_estimated_cost=worst_case_estimated_cost,
                    markup_multiplier=markup_multiplier,
                    quoted_selling_price=quoted_selling_price,
                )
            )
        return PriceQuote(
            resource=resource,
            request_fingerprint=request_fingerprint,
            pricing_revision=self.catalog.revision,
            estimated_internal_cost=estimated_internal_cost,
            selling_price=selling_price,
            created_at=created_at,
            expires_at=created_at
            + datetime.timedelta(seconds=self.catalog.quote_ttl_seconds),
            operations=tuple(operation_estimates),
        )
