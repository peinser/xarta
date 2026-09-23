from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from xarta.services.v1.postal.adapters.local.models import decimal_quantity


@dataclass(frozen=True, slots=True)
class ResolvedSupply:
    reference: str
    description: str
    quantity: int

    def __post_init__(self) -> None:
        if not self.reference or not self.description or self.quantity < 1:
            raise ValueError(
                "A resolved supply requires a reference, description, and positive quantity"
            )


@dataclass(frozen=True, slots=True)
class ResolvedPriceLine:
    reference: str
    quantity: int
    unit_price: Decimal
    amount: Decimal

    def __post_init__(self) -> None:
        unit_price = decimal_quantity(self.unit_price, name="unit_price")
        amount = decimal_quantity(self.amount, name="amount")
        if not self.reference or self.quantity < 1 or unit_price <= 0:
            raise ValueError(
                "A resolved price line requires positive quantity and unit price"
            )
        if amount != self.quantity * unit_price:
            raise ValueError(
                "Resolved price line amount must equal quantity times unit price"
            )
        object.__setattr__(self, "unit_price", unit_price)
        object.__setattr__(self, "amount", amount)


@dataclass(frozen=True, slots=True)
class ResolvedProductionInstructions:
    provider_id: str
    provider_profile_id: str
    provider_profile_revision: str
    franking_method: str
    policy_revision: str
    supplies: tuple[ResolvedSupply, ...]
    tariff_revision: str
    currency: str
    pricing: tuple[ResolvedPriceLine, ...]
    total_postage: Decimal

    def __post_init__(self) -> None:
        total = decimal_quantity(self.total_postage, name="total_postage")
        values = (
            self.provider_id,
            self.provider_profile_id,
            self.provider_profile_revision,
            self.franking_method,
            self.policy_revision,
            self.tariff_revision,
            self.currency,
        )
        if any(not value for value in values) or not self.supplies or not self.pricing:
            raise ValueError(
                "Resolved production instructions require revisions, supplies, and pricing"
            )
        if tuple(supply.reference for supply in self.supplies) != tuple(
            line.reference for line in self.pricing
        ):
            raise ValueError(
                "Resolved supplies and pricing lines must correspond in order"
            )
        if any(
            supply.quantity != line.quantity
            for supply, line in zip(self.supplies, self.pricing, strict=True)
        ):
            raise ValueError("Resolved supply and pricing quantities must match")
        if total <= 0 or total != sum(
            (line.amount for line in self.pricing), Decimal(0)
        ):
            raise ValueError(
                "Total postage must be positive and equal the exact pricing line sum"
            )
        object.__setattr__(self, "total_postage", total)


class ProductionInstructionResolver(Protocol):
    def validate_profile(self, profile_id: str) -> None: ...

    def resolve(
        self,
        profile_id: str,
        *,
        service_type: str,
        speed: str | None,
        estimated_weight_g: Decimal,
    ) -> ResolvedProductionInstructions: ...
