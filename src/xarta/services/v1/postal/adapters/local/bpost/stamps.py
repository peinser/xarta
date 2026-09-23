from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from decimal import InvalidOperation
from typing import Any

from xarta.services.v1.postal.adapters.local.instructions import ResolvedPriceLine
from xarta.services.v1.postal.adapters.local.instructions import ResolvedProductionInstructions  # fmt: skip
from xarta.services.v1.postal.adapters.local.instructions import ResolvedSupply
from xarta.services.v1.postal.adapters.local.models import decimal_quantity


def _configuration_decimal(value: Any, *, name: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError(f"{name} must be an exact decimal string or integer")
    try:
        return decimal_quantity(Decimal(value), name=name)
    except (InvalidOperation, TypeError, ValueError) as ex:
        raise ValueError(f"{name} is invalid") from ex


@dataclass(frozen=True, slots=True)
class BpostStampAllocation:
    product_reference: str
    quantity: int

    def __post_init__(self) -> None:
        if not self.product_reference or self.quantity < 1:
            raise ValueError(
                "Bpost stamp allocation requires a product and positive quantity"
            )


@dataclass(frozen=True, slots=True)
class BpostManualStampRule:
    service_type: str
    speed: str | None
    maximum_weight_g: Decimal
    allocations: tuple[BpostStampAllocation, ...]

    def __post_init__(self) -> None:
        maximum = decimal_quantity(self.maximum_weight_g, name="maximum_weight_g")
        if not self.service_type or maximum <= 0 or not self.allocations:
            raise ValueError(
                "Bpost stamp rule requires service, weight, and allocations"
            )
        if len(
            {allocation.product_reference for allocation in self.allocations}
        ) != len(self.allocations):
            raise ValueError("Bpost stamp rule contains a duplicate product")
        object.__setattr__(self, "maximum_weight_g", maximum)


@dataclass(frozen=True, slots=True)
class _Product:
    reference: str
    description: str


@dataclass(frozen=True, slots=True)
class _Profile:
    identifier: str
    revision: str
    policy_revision: str
    tariff_revision: str
    currency: str
    products: Mapping[str, _Product]
    prices: Mapping[str, Decimal]
    rules: tuple[BpostManualStampRule, ...]


class BpostProductionInstructionResolver:
    def __init__(self, profiles: Mapping[str, Mapping[str, Any]]) -> None:
        self._profiles = {
            identifier: self._parse_profile(identifier, value)
            for identifier, value in profiles.items()
        }

    def validate_profile(self, profile_id: str) -> None:
        if profile_id not in self._profiles:
            raise ValueError("Local postal profile references an unknown Bpost profile")

    def resolve(
        self,
        profile_id: str,
        *,
        service_type: str,
        speed: str | None,
        estimated_weight_g: Decimal,
    ) -> ResolvedProductionInstructions:
        self.validate_profile(profile_id)
        profile = self._profiles[profile_id]
        weight = decimal_quantity(estimated_weight_g, name="estimated_weight_g")
        matches = [
            rule
            for rule in profile.rules
            if rule.service_type == service_type
            and rule.speed == speed
            and weight <= rule.maximum_weight_g
        ]
        if not matches:
            raise ValueError(
                "No Bpost manual stamp rule covers this service and estimated weight"
            )
        selected = min(matches, key=lambda rule: rule.maximum_weight_g)
        supplies = tuple(
            ResolvedSupply(
                allocation.product_reference,
                profile.products[allocation.product_reference].description,
                allocation.quantity,
            )
            for allocation in selected.allocations
        )
        pricing = tuple(
            ResolvedPriceLine(
                allocation.product_reference,
                allocation.quantity,
                profile.prices[allocation.product_reference],
                allocation.quantity * profile.prices[allocation.product_reference],
            )
            for allocation in selected.allocations
        )
        return ResolvedProductionInstructions(
            "bpost",
            profile.identifier,
            profile.revision,
            "stamps",
            profile.policy_revision,
            supplies,
            profile.tariff_revision,
            profile.currency,
            pricing,
            sum((line.amount for line in pricing), Decimal(0)),
        )

    @staticmethod
    def _parse_profile(identifier: str, value: Mapping[str, Any]) -> _Profile:
        revision = value.get("revision")
        products_raw = value.get("products")
        tariff = value.get("tariff")
        policy = value.get("policy")
        if not identifier or not isinstance(revision, str) or not revision:
            raise ValueError("Bpost profile requires an identifier and revision")
        if not isinstance(products_raw, list) or not products_raw:
            raise ValueError("Bpost profile requires stamp products")
        if not isinstance(tariff, Mapping) or not isinstance(policy, Mapping):
            raise ValueError("Bpost profile requires a tariff and stamp policy")

        products: dict[str, _Product] = {}
        for raw in products_raw:
            if not isinstance(raw, Mapping):
                raise ValueError("Bpost stamp product is invalid")
            reference = raw.get("reference")
            description = raw.get("description")
            if (
                not isinstance(reference, str)
                or not reference
                or not isinstance(description, str)
                or not description
            ):
                raise ValueError(
                    "Bpost stamp product requires a reference and description"
                )
            if reference in products:
                raise ValueError(f"Duplicate Bpost stamp product: {reference}")
            products[reference] = _Product(reference, description)

        tariff_revision = tariff.get("revision")
        currency = tariff.get("currency")
        prices_raw = tariff.get("unit_prices")
        if (
            not isinstance(tariff_revision, str)
            or not tariff_revision
            or not isinstance(currency, str)
            or not currency
            or not isinstance(prices_raw, Mapping)
        ):
            raise ValueError(
                "Bpost tariff requires revision, currency, and unit prices"
            )
        prices: dict[str, Decimal] = {}
        for reference, raw_price in prices_raw.items():
            if not isinstance(reference, str) or reference not in products:
                raise ValueError(
                    f"Bpost tariff references unknown product: {reference}"
                )
            try:
                price = _configuration_decimal(
                    raw_price, name=f"{reference} unit price"
                )
            except ValueError as ex:
                raise ValueError(f"Bpost tariff price is invalid: {reference}") from ex
            if price <= 0:
                raise ValueError("Bpost tariff unit prices must be positive")
            prices[reference] = price
        missing_product_prices = products.keys() - prices.keys()
        if missing_product_prices:
            raise ValueError(
                f"Bpost tariff is missing products: {sorted(missing_product_prices)}"
            )

        policy_revision = policy.get("revision")
        rules_raw = policy.get("rules")
        if (
            not isinstance(policy_revision, str)
            or not policy_revision
            or not isinstance(rules_raw, list)
            or not rules_raw
        ):
            raise ValueError("Bpost stamp policy requires revision and rules")
        rules: list[BpostManualStampRule] = []
        ceilings: defaultdict[tuple[str, str | None], list[Decimal]] = defaultdict(list)
        for raw in rules_raw:
            if not isinstance(raw, Mapping) or not isinstance(
                raw.get("supplies"), list
            ):
                raise ValueError("Bpost stamp policy rule is invalid")
            try:
                allocations = []
                for item in raw["supplies"]:
                    if not isinstance(item, Mapping):
                        raise ValueError
                    reference = item["reference"]
                    quantity = item["quantity"]
                    if (
                        not isinstance(reference, str)
                        or isinstance(quantity, bool)
                        or not isinstance(quantity, int)
                    ):
                        raise ValueError
                    allocations.append(BpostStampAllocation(reference, quantity))
                speed = raw.get("speed")
                if speed is not None and not isinstance(speed, str):
                    raise ValueError
                rule = BpostManualStampRule(
                    str(raw.get("service_type", "")),
                    speed,
                    _configuration_decimal(
                        raw["maximum_weight_g"], name="maximum_weight_g"
                    ),
                    tuple(allocations),
                )
            except (InvalidOperation, KeyError, TypeError, ValueError) as ex:
                raise ValueError("Bpost stamp policy rule is invalid") from ex
            unknown = {
                item.product_reference for item in rule.allocations
            } - products.keys()
            if unknown:
                raise ValueError(
                    f"Bpost stamp policy references unknown products: {sorted(unknown)}"
                )
            missing_prices = {
                item.product_reference for item in rule.allocations
            } - prices.keys()
            if missing_prices:
                raise ValueError(
                    f"Bpost tariff is missing products: {sorted(missing_prices)}"
                )
            key = (rule.service_type, rule.speed)
            if ceilings[key] and rule.maximum_weight_g <= ceilings[key][-1]:
                raise ValueError(
                    "Bpost stamp weight ceilings must be positive and strictly increasing"
                )
            ceilings[key].append(rule.maximum_weight_g)
            rules.append(rule)
        return _Profile(
            identifier,
            revision,
            policy_revision,
            tariff_revision,
            currency,
            products,
            prices,
            tuple(rules),
        )
