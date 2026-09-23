from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Any

import orjson

from xarta.pricing.models import Money
from xarta.pricing.models import PricingRate
from xarta.pricing.models import decimal_value
from xarta.protocol.dag import Node


class PricingConfigurationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PricingCatalog:
    revision: str
    currency: str
    default_markup_multiplier: Decimal
    quote_ttl_seconds: int
    rates: Mapping[tuple[str, str, str | None], PricingRate]

    def require_rate(
        self, capability: str, adapter: str, destination: str | None
    ) -> PricingRate:
        destination_rate = self.rates.get((capability, adapter, destination))
        if destination_rate is not None:
            return destination_rate
        adapter_rate = self.rates.get((capability, adapter, None))
        if adapter_rate is not None:
            return adapter_rate
        raise PricingConfigurationError(
            f"No pricing rate for capability={capability}, adapter={adapter}, "
            f"destination={destination}"
        )


def parse_pricing_catalog(value: object) -> PricingCatalog:
    if not isinstance(value, Mapping):
        raise PricingConfigurationError("Pricing catalog must be an object")
    revision = value.get("revision")
    currency = value.get("currency")
    if not isinstance(revision, str) or not revision:
        raise PricingConfigurationError("Pricing revision must be non-empty")
    if (
        not isinstance(currency, str)
        or len(currency) != 3
        or not currency.isascii()
        or not currency.isupper()
        or not currency.isalpha()
    ):
        raise PricingConfigurationError(
            "Pricing currency must be an uppercase ISO 4217 code"
        )
    raw_default_markup = value.get("default_markup_multiplier")
    if not isinstance(raw_default_markup, str | Decimal):
        raise PricingConfigurationError(
            "default_markup_multiplier must be a decimal string"
        )
    default_markup = decimal_value(
        raw_default_markup, field="default_markup_multiplier"
    )
    if default_markup < 0:
        raise PricingConfigurationError(
            "default_markup_multiplier must not be negative"
        )
    quote_ttl = value.get("quote_ttl_seconds")
    if isinstance(quote_ttl, bool) or not isinstance(quote_ttl, int) or quote_ttl <= 0:
        raise PricingConfigurationError("quote_ttl_seconds must be a positive integer")
    raw_rates = value.get("rates")
    if not isinstance(raw_rates, list) or not raw_rates:
        raise PricingConfigurationError("Pricing rates must be a non-empty list")
    rates: dict[tuple[str, str, str | None], PricingRate] = {}
    for raw_rate in raw_rates:
        if not isinstance(raw_rate, Mapping):
            raise PricingConfigurationError("Each pricing rate must be an object")
        capability = raw_rate.get("capability")
        adapter = raw_rate.get("adapter")
        destination = raw_rate.get("destination")
        if not isinstance(capability, str) or not capability:
            raise PricingConfigurationError("Rate capability must be non-empty")
        if not isinstance(adapter, str) or not adapter:
            raise PricingConfigurationError("Rate adapter must be non-empty")
        if destination is not None and (
            not isinstance(destination, str) or not destination
        ):
            raise PricingConfigurationError("Rate destination must be non-empty")
        raw_estimated_cost = raw_rate.get("estimated_cost")
        if not isinstance(raw_estimated_cost, str | Decimal):
            raise PricingConfigurationError("estimated_cost must be a decimal string")
        estimated_cost = decimal_value(raw_estimated_cost, field="estimated_cost")
        if estimated_cost < 0:
            raise PricingConfigurationError("estimated_cost must not be negative")
        markup = raw_rate.get("markup_multiplier")
        markup_multiplier = (
            decimal_value(markup, field="markup_multiplier")
            if markup is not None
            else None
        )
        if markup_multiplier is not None and markup_multiplier < 0:
            raise PricingConfigurationError("markup_multiplier must not be negative")
        key = (capability, adapter, destination)
        if key in rates:
            raise PricingConfigurationError(f"Duplicate pricing rate: {key}")
        rates[key] = PricingRate(
            capability=capability,
            adapter=adapter,
            destination=destination,
            estimated_cost=Money(estimated_cost, currency),
            markup_multiplier=markup_multiplier,
        )
    return PricingCatalog(
        revision=revision,
        currency=currency,
        default_markup_multiplier=default_markup,
        quote_ttl_seconds=quote_ttl,
        rates=MappingProxyType(rates),
    )


def load_pricing_catalog(path: str) -> PricingCatalog:
    return parse_pricing_catalog(orjson.loads(Path(path).read_bytes()))


def parse_pricing_bindings(value: object):
    from xarta.pricing.engine import PricingBindingResolver
    from xarta.pricing.models import PricingBinding

    if not isinstance(value, Mapping):
        raise PricingConfigurationError("Pricing configuration must be an object")
    raw_bindings = value.get("bindings")
    if not isinstance(raw_bindings, list) or not raw_bindings:
        raise PricingConfigurationError("Pricing bindings must be a non-empty list")
    bindings: dict[tuple[str, str | None], PricingBinding] = {}
    for raw_binding in raw_bindings:
        if not isinstance(raw_binding, Mapping):
            raise PricingConfigurationError("Each pricing binding must be an object")
        capability = raw_binding.get("capability")
        destination = raw_binding.get("destination")
        adapter = raw_binding.get("adapter")
        revision = raw_binding.get("adapter_configuration_revision")
        if not isinstance(capability, str) or not capability:
            raise PricingConfigurationError("Binding capability must be non-empty")
        if destination is not None and (
            not isinstance(destination, str) or not destination
        ):
            raise PricingConfigurationError("Binding destination must be non-empty")
        if not isinstance(adapter, str) or not adapter:
            raise PricingConfigurationError("Binding adapter must be non-empty")
        if revision is not None and (not isinstance(revision, str) or not revision):
            raise PricingConfigurationError(
                "Binding adapter_configuration_revision must be non-empty"
            )
        key = (capability, destination)
        if key in bindings:
            raise PricingConfigurationError(f"Duplicate pricing binding: {key}")
        bindings[key] = PricingBinding(capability, destination, adapter, revision)

    def resolve(node: Node) -> PricingBinding:
        destination = getattr(node, "destination", None)
        binding = bindings.get((node.kind, destination)) or bindings.get(
            (node.kind, None)
        )
        if binding is None:
            raise PricingConfigurationError(
                f"No pricing binding for capability={node.kind}, "
                f"destination={destination}"
            )
        return binding

    return PricingBindingResolver(resolve)


def load_pricing_configuration(path: str):
    value = orjson.loads(Path(path).read_bytes())
    return parse_pricing_catalog(value), parse_pricing_bindings(value)
