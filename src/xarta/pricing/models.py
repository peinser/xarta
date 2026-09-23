from __future__ import annotations

import datetime

from dataclasses import dataclass
from decimal import Decimal
from decimal import InvalidOperation
from uuid import UUID


def decimal_value(value: str | Decimal, *, field: str) -> Decimal:
    if isinstance(value, float):
        raise TypeError(f"{field} must not use binary float")
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, TypeError) as ex:
        raise ValueError(f"{field} must be a decimal string") from ex
    if not parsed.is_finite():
        raise ValueError(f"{field} must be finite")
    return parsed


@dataclass(frozen=True, slots=True)
class Money:
    amount: Decimal
    currency: str

    def __post_init__(self) -> None:
        amount = decimal_value(self.amount, field="money amount")
        if amount < 0:
            raise ValueError("Money amount must not be negative")
        if not isinstance(self.currency, str) or not self.currency:
            raise ValueError("Money currency must be non-empty")
        object.__setattr__(self, "amount", amount)

    def multiply(self, multiplier: Decimal | int) -> Money:
        value = decimal_value(
            multiplier if isinstance(multiplier, Decimal) else Decimal(multiplier),
            field="money multiplier",
        )
        if value < 0:
            raise ValueError("Money multiplier must not be negative")
        return Money(self.amount * value, self.currency)

    def add(self, other: Money) -> Money:
        if self.currency != other.currency:
            raise ValueError("Money currencies must match")
        return Money(self.amount + other.amount, self.currency)


@dataclass(frozen=True, slots=True)
class PricingBinding:
    capability: str
    destination: str | None
    adapter: str
    adapter_configuration_revision: str | None


@dataclass(frozen=True, slots=True)
class PricingRate:
    capability: str
    adapter: str
    estimated_cost: Money
    destination: str | None = None
    markup_multiplier: Decimal | None = None

    def resolve_markup_multiplier(self, default: Decimal) -> Decimal:
        return self.markup_multiplier if self.markup_multiplier is not None else default


@dataclass(frozen=True, slots=True)
class OperationPricingEstimate:
    node_id: UUID
    pricing_binding: PricingBinding
    maximum_activations: int
    estimated_cost_per_activation: Money
    worst_case_estimated_cost: Money
    markup_multiplier: Decimal
    quoted_selling_price: Money


@dataclass(frozen=True, slots=True)
class PriceQuote:
    resource: str
    request_fingerprint: str
    pricing_revision: str
    estimated_internal_cost: Money
    selling_price: Money
    created_at: datetime.datetime
    expires_at: datetime.datetime
    operations: tuple[OperationPricingEstimate, ...]

    def __post_init__(self) -> None:
        if (
            not self.resource
            or not self.request_fingerprint
            or not self.pricing_revision
        ):
            raise ValueError("Quote identity fields must be non-empty")
        if self.created_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValueError("Quote timestamps must be timezone-aware")
        if self.expires_at <= self.created_at:
            raise ValueError("Quote expiry must be after creation")
