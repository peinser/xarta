from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from xarta.pricing.models import Money
from xarta.pricing.models import PricingBinding
from xarta.pricing.models import PricingRate
from xarta.protocol.dag import Node


class CapabilityPricingCalculator(Protocol):
    def estimate(
        self, node: Node, binding: PricingBinding, rate: PricingRate
    ) -> Money: ...


@dataclass(frozen=True, slots=True)
class FixedRatePricingCalculator:
    def estimate(self, node: Node, binding: PricingBinding, rate: PricingRate) -> Money:
        del node, binding
        return rate.estimated_cost
