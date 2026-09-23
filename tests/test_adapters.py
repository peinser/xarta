from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Any

import pytest

from xarta.adapters import AdapterRegistry
from xarta.adapters import UnknownAdapterError

if TYPE_CHECKING:
    from collections.abc import Mapping


class StringFactory:
    def validate(self, configuration: Mapping[str, Any]) -> None:
        if not configuration.get("value"):
            raise ValueError("value is required")

    def create(self, configuration: Mapping[str, Any]) -> str:
        return str(configuration["value"])


def test_adapter_registry_is_immutable_from_source_mapping() -> None:
    factories = {"example": StringFactory()}
    registry = AdapterRegistry[str](factories)
    factories.clear()

    assert registry.names == frozenset({"example"})
    assert registry.create("example", {"adapter": "example", "value": "ok"}) == "ok"


def test_adapter_registry_rejects_unknown_or_mismatched_adapter() -> None:
    registry = AdapterRegistry[str]({"example": StringFactory()})

    with pytest.raises(UnknownAdapterError, match="missing"):
        registry.create("missing", {})
    with pytest.raises(ValueError, match="binding requires"):
        registry.create("example", {"adapter": "other", "value": "ok"})


def test_adapter_registry_runs_factory_validation() -> None:
    registry = AdapterRegistry[str]({"example": StringFactory()})

    with pytest.raises(ValueError, match="value is required"):
        registry.validate("example", {"adapter": "example"})
