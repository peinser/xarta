from __future__ import annotations

from types import MappingProxyType
from typing import TYPE_CHECKING
from typing import Any
from typing import Generic
from typing import Protocol
from typing import TypeVar

AdapterT = TypeVar("AdapterT")
AdapterT_co = TypeVar("AdapterT_co", covariant=True)


if TYPE_CHECKING:
    from collections.abc import Mapping


class AdapterFactory(Protocol[AdapterT_co]):
    """Constructs one capability adapter from validated configuration."""

    def validate(self, configuration: Mapping[str, Any]) -> None:
        """Validate configuration without contacting the external provider."""

    def create(self, configuration: Mapping[str, Any]) -> AdapterT_co:
        """Create an adapter for one operation or service runtime."""


class UnknownAdapterError(ValueError):
    """Raised when configuration references an unavailable adapter."""


class AdapterRegistry(Generic[AdapterT]):
    """Immutable collection of explicitly installed adapter factories."""

    def __init__(self, factories: Mapping[str, AdapterFactory[AdapterT]]) -> None:
        if any(not isinstance(name, str) or not name for name in factories):
            raise ValueError("Adapter names must be non-empty strings")
        self._factories = MappingProxyType(dict(factories))

    @property
    def names(self) -> frozenset[str]:
        return frozenset(self._factories)

    def supports(self, name: str) -> bool:
        return name in self._factories

    def validate(self, name: str, configuration: Mapping[str, Any]) -> None:
        factory = self._factory(name)
        configured_name = configuration.get("adapter")
        if configured_name is not None and configured_name != name:
            raise ValueError(
                f"Adapter configuration names {configured_name!r}, "
                f"but binding requires {name!r}"
            )
        factory.validate(configuration)

    def create(self, name: str, configuration: Mapping[str, Any]) -> AdapterT:
        factory = self._factory(name)
        self.validate(name, configuration)
        return factory.create(configuration)

    def _factory(self, name: str) -> AdapterFactory[AdapterT]:
        try:
            return self._factories[name]
        except KeyError as ex:
            available = ", ".join(sorted(self._factories)) or "none"
            raise UnknownAdapterError(
                f"Unknown adapter {name!r}; available adapters: {available}"
            ) from ex
