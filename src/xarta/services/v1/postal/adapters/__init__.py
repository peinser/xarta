from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING
from typing import Any
from typing import Protocol

from xarta.adapters import AdapterRegistry
from xarta.execution import ExecutionMode
from xarta.tracking import DestinationRegistry

if TYPE_CHECKING:
    from collections.abc import Mapping

    from xarta.protocol.dag import CapabilityResult
    from xarta.protocol.dag import NodeTask
    from xarta.protocol.dag.postal import PostalRequest


class PostalAdapter(Protocol):
    @property
    def execution_mode(self) -> ExecutionMode: ...

    async def submit(
        self, *, task: NodeTask, request: PostalRequest, logger: Any
    ) -> CapabilityResult: ...


@dataclass(frozen=True, slots=True)
class PostalComponents:
    destinations: DestinationRegistry
    adapters: AdapterRegistry[PostalAdapter]
    execution_modes: Mapping[str, ExecutionMode]


def build_postal_components(
    configurations: Mapping[str, Mapping[str, Any]],
    adapters: AdapterRegistry[PostalAdapter],
) -> PostalComponents:
    destinations = DestinationRegistry(configurations, require_explicit_revisions=True)
    execution_modes: dict[str, ExecutionMode] = {}
    for resolved in destinations.iter_resolved():
        binding = resolved.binding
        if binding.capability != "postal":
            raise ValueError(
                f"Destination {binding.destination} is not valid for postal"
            )
        adapters.validate(binding.adapter, resolved.configuration)
        mode = adapters.create(binding.adapter, resolved.configuration).execution_mode
        if not isinstance(mode, ExecutionMode):
            raise TypeError("Postal adapter returned an invalid execution mode")
        previous = execution_modes.setdefault(binding.destination, mode)
        if previous is not mode:
            raise ValueError(
                f"Postal destination {binding.destination} changes execution mode"
            )
    return PostalComponents(destinations, adapters, MappingProxyType(execution_modes))
