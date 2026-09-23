from __future__ import annotations

import hashlib

from collections.abc import Iterator
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

import orjson

from xarta.tracking.models import DestinationBinding


@dataclass(frozen=True)
class ResolvedDestination:
    binding: DestinationBinding
    configuration: dict[str, Any]


class DestinationRegistry:
    def __init__(
        self,
        destinations: Mapping[str, Mapping[str, Any]],
        defaults: Mapping[str, str] | None = None,
        *,
        require_explicit_revisions: bool = False,
    ):
        copied = {}
        for name, configuration in destinations.items():
            if not isinstance(name, str) or not name:
                raise ValueError("Destination names must be non-empty strings")
            if not isinstance(configuration, Mapping):
                raise ValueError(f"Destination {name} must be an object")
            if require_explicit_revisions and not {
                "current_revision",
                "revisions",
            }.issubset(configuration):
                raise ValueError(
                    f"Destination {name} requires explicit current_revision and revisions"
                )
            copied[name] = deepcopy(dict(configuration))
        self._destinations = MappingProxyType(copied)
        self._defaults = MappingProxyType(dict(defaults or {}))
        for capability, destination in self._defaults.items():
            if destination not in self._destinations:
                raise ValueError(
                    f"Default {capability} destination is unavailable: {destination}"
                )
        # Validate every retained revision because pending operations may still own it.
        tuple(self.iter_resolved())
        for capability, destination in self._defaults.items():
            self.resolve(capability, destination)

    def resolve(self, capability: str, destination: str | None) -> ResolvedDestination:
        name = destination or self._defaults.get(capability)
        if name is None or name not in self._destinations:
            raise ValueError(f"Unknown {capability} destination: {name}")

        configured = dict(self._destinations[name])
        current_revision = configured.get("current_revision")
        if current_revision is not None:
            configuration = self._revision_configuration(
                name, configured, current_revision
            )
        else:
            configuration = configured
        resolved = self._resolved(name, configuration)
        if resolved.binding.capability != capability:
            raise ValueError(f"Destination {name} is not valid for {capability}")
        return resolved

    def resolve_binding(self, binding: DestinationBinding) -> ResolvedDestination:
        configured = self._destinations.get(binding.destination)
        if configured is None:
            raise ValueError(
                f"Pinned destination is unavailable: {binding.destination}"
            )
        configured = dict(configured)
        if "revisions" in configured:
            configuration = self._revision_configuration(
                binding.destination,
                configured,
                binding.configuration_revision,
            )
        else:
            configuration = configured
        resolved = self._resolved(binding.destination, configuration)
        if resolved.binding != binding:
            raise ValueError("Pinned destination binding is no longer available")
        return resolved

    def iter_resolved(
        self, capability: str | None = None
    ) -> Iterator[ResolvedDestination]:
        """Yield every current and retained destination revision."""
        for name, configured_value in self._destinations.items():
            configured = dict(configured_value)
            if "revisions" in configured or "current_revision" in configured:
                revisions = configured.get("revisions")
                current_revision = configured.get("current_revision")
                if not isinstance(revisions, Mapping) or not revisions:
                    raise ValueError(f"Destination {name} revisions must be an object")
                if not isinstance(current_revision, str) or not current_revision:
                    raise ValueError(
                        f"Destination {name} requires a non-empty current_revision"
                    )
                if current_revision not in revisions:
                    raise ValueError(
                        f"Destination {name} current revision is unavailable: "
                        f"{current_revision}"
                    )
                revision_names: tuple[Any, ...] = tuple(revisions)
            else:
                revision_names = (None,)

            for revision in revision_names:
                if revision is not None and (
                    not isinstance(revision, str) or not revision
                ):
                    raise ValueError(
                        f"Destination {name} revision names must be non-empty strings"
                    )
                configuration = (
                    self._revision_configuration(name, configured, revision)
                    if revision is not None
                    else configured
                )
                resolved = self._resolved(name, configuration)
                if capability is None or resolved.binding.capability == capability:
                    yield resolved

    def _revision_configuration(
        self, name: str, configured: dict, revision: str
    ) -> dict:
        revisions = configured.get("revisions", {})
        if revision not in revisions:
            raise ValueError(
                f"Pinned destination revision is unavailable: {name}@{revision}"
            )
        common = {
            key: value
            for key, value in configured.items()
            if key not in {"current_revision", "revisions"}
        }
        revision_configuration = revisions[revision]
        if not isinstance(revision_configuration, Mapping):
            raise ValueError(
                f"Destination revision {name}@{revision} must be an object"
            )
        return {
            **common,
            **deepcopy(dict(revision_configuration)),
            "revision": revision,
        }

    def _resolved(self, name: str, configuration: dict) -> ResolvedDestination:
        capability = configuration.get("kind")
        adapter = configuration.get("adapter")
        if not isinstance(capability, str) or not capability:
            raise ValueError(f"Destination {name} is incomplete")
        if not isinstance(adapter, str) or not adapter:
            raise ValueError(f"Destination {name} is incomplete")
        revision = (
            configuration.get("revision")
            or hashlib.sha256(
                orjson.dumps(configuration, option=orjson.OPT_SORT_KEYS)
            ).hexdigest()
        )
        return ResolvedDestination(
            binding=DestinationBinding(
                destination=name,
                capability=capability,
                adapter=adapter,
                configuration_revision=revision,
            ),
            configuration=configuration,
        )
