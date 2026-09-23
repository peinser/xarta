from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from xarta.services.v1.postal.adapters.local.models import decimal_quantity


@dataclass(frozen=True, slots=True)
class PriceLine:
    component: str
    quantity: Decimal
    unit_price: Decimal
    amount: Decimal

    def __post_init__(self) -> None:
        if not self.component:
            raise ValueError("Price component must be non-empty")
        quantity = decimal_quantity(self.quantity, name="quantity")
        unit_price = decimal_quantity(self.unit_price, name="unit_price")
        amount = decimal_quantity(self.amount, name="amount")
        if quantity < 0 or unit_price < 0 or amount != quantity * unit_price:
            raise ValueError(
                "Price line must contain non-negative Decimal values and an exact amount"
            )
        object.__setattr__(self, "quantity", quantity)
        object.__setattr__(self, "unit_price", unit_price)
        object.__setattr__(self, "amount", amount)


@dataclass(frozen=True, slots=True)
class ProductionBounds:
    maximum_pages: int
    maximum_sheets: int
    maximum_weight_g: Decimal
    maximum_envelope_cost: Decimal
    maximum_postage: Decimal

    def __post_init__(self) -> None:
        if self.maximum_pages < 1 or self.maximum_sheets < 1:
            raise ValueError("Page and sheet bounds must be positive")
        for name in ("maximum_weight_g", "maximum_envelope_cost", "maximum_postage"):
            value = decimal_quantity(getattr(self, name), name=name)
            if value < 0:
                raise ValueError(f"{name} must not be negative")
            object.__setattr__(self, name, value)


@dataclass(frozen=True, slots=True)
class ActualProduction:
    pages: int
    sheets: int
    weight_g: Decimal
    envelope_cost: Decimal
    postage: Decimal

    def __post_init__(self) -> None:
        if self.pages < 1 or self.sheets < 1:
            raise ValueError("Actual page and sheet counts must be positive")
        for name in ("weight_g", "envelope_cost", "postage"):
            value = decimal_quantity(getattr(self, name), name=name)
            if value < 0:
                raise ValueError(f"{name} must not be negative")
            object.__setattr__(self, name, value)


def validate_production_bounds(
    bounds: ProductionBounds, actual: ActualProduction
) -> None:
    checks = (
        (actual.pages, bounds.maximum_pages, "pages"),
        (actual.sheets, bounds.maximum_sheets, "sheets"),
        (
            decimal_quantity(actual.weight_g, name="weight_g"),
            bounds.maximum_weight_g,
            "weight",
        ),
        (
            decimal_quantity(actual.envelope_cost, name="envelope_cost"),
            bounds.maximum_envelope_cost,
            "envelope cost",
        ),
        (
            decimal_quantity(actual.postage, name="postage"),
            bounds.maximum_postage,
            "postage",
        ),
    )
    exceeded = [name for value, maximum, name in checks if value > maximum]
    if exceeded:
        raise ValueError(
            f"Actual production exceeds quoted bounds: {', '.join(exceeded)}"
        )
