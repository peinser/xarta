from __future__ import annotations

import re

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

PROFILE_NAME = re.compile(r"^[a-z][a-z0-9-]{0,62}$")


class FlowProfileError(ValueError):
    pass


class UnknownFlowProfileError(FlowProfileError):
    pass


@dataclass(frozen=True, slots=True, order=True)
class FlowProfileReference:
    name: str
    version: int

    def __post_init__(self) -> None:
        if not PROFILE_NAME.fullmatch(self.name):
            raise FlowProfileError("Flow profile names must use lowercase kebab-case")
        if isinstance(self.version, bool) or self.version <= 0:
            raise FlowProfileError("Flow profile versions must be positive integers")

    @classmethod
    def parse(cls, value: str) -> FlowProfileReference:
        if not isinstance(value, str) or value.count("@") != 1:
            raise FlowProfileError(
                "delivery_profile must use an exact name@version reference"
            )
        name, raw_version = value.rsplit("@", 1)
        try:
            version = int(raw_version)
        except ValueError as ex:
            raise FlowProfileError("Flow profile version must be an integer") from ex
        return cls(name, version)

    def __str__(self) -> str:
        return f"{self.name}@{self.version}"


class FlowInputType(StrEnum):
    STRING = "string"
    STRING_LIST = "string-list"
    JSON_OBJECT = "json-object"
    DOCUMENT_SOURCE = "document-source"
    UUID = "uuid"
    INSTANT = "instant"
    LOCALIZED_STRINGS = "localized-strings"
    DOCCLE_RECEIVER = "doccle-receiver"


@dataclass(frozen=True, slots=True)
class FlowInputDefinition:
    type: FlowInputType
    required: bool = True
    maximum_length: int | None = None
    maximum_items: int | None = None
    maximum_bytes: int | None = None
    allowed_sources: frozenset[str] = frozenset()
    require_version: bool = False

    def contract(self) -> dict[str, object]:
        result: dict[str, object] = {
            "type": self.type.value,
            "required": self.required,
        }
        if self.maximum_length is not None:
            result["maximum_length"] = self.maximum_length
        if self.maximum_items is not None:
            result["maximum_items"] = self.maximum_items
        if self.maximum_bytes is not None:
            result["maximum_bytes"] = self.maximum_bytes
        if self.allowed_sources:
            result["allowed_sources"] = sorted(self.allowed_sources)
        if self.require_version:
            result["require_version"] = True
        return result


@dataclass(frozen=True, slots=True)
class FlowProfile:
    reference: FlowProfileReference
    description: str | None
    inputs: Mapping[str, FlowInputDefinition]
    generated_values: Mapping[str, str]
    canonical_template: bytes
    fingerprint: str


@dataclass(frozen=True, slots=True)
class FlowProfileRegistry:
    profiles: Mapping[FlowProfileReference, FlowProfile]
    current_versions: Mapping[str, int]

    @classmethod
    def empty(cls) -> FlowProfileRegistry:
        return cls(MappingProxyType({}), MappingProxyType({}))

    def resolve(self, reference: FlowProfileReference) -> FlowProfile:
        try:
            return self.profiles[reference]
        except KeyError as ex:
            raise UnknownFlowProfileError(f"Unknown flow profile: {reference}") from ex

    def contracts(self) -> tuple[dict, ...]:
        return tuple(
            {
                "delivery_profile": str(profile.reference),
                "name": profile.reference.name,
                "version": profile.reference.version,
                "current": self.current_versions[profile.reference.name]
                == profile.reference.version,
                "description": profile.description,
                "fingerprint": profile.fingerprint,
                "inputs": {
                    name: definition.contract()
                    for name, definition in profile.inputs.items()
                },
                "generated_values": dict(profile.generated_values),
            }
            for profile in sorted(
                self.profiles.values(), key=lambda item: item.reference
            )
        )
