from __future__ import annotations

import hashlib
import hmac

from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType

import orjson

from .compiler import validate_profile_template
from .models import FlowInputDefinition
from .models import FlowInputType
from .models import FlowProfile
from .models import FlowProfileError
from .models import FlowProfileReference
from .models import FlowProfileRegistry

_INPUT_FIELDS = frozenset(
    {
        "type",
        "required",
        "maximum_length",
        "maximum_items",
        "maximum_bytes",
        "allowed_sources",
        "require_version",
    }
)
MAX_FLOW_PROFILE_CONFIGURATION_BYTES = 2 * 1024 * 1024
MAX_FLOW_PROFILE_VERSIONS = 1_000


def load_flow_profiles(path: str) -> FlowProfileRegistry:
    try:
        raw = Path(path).read_bytes()
        if len(raw) > MAX_FLOW_PROFILE_CONFIGURATION_BYTES:
            raise FlowProfileError("Flow profile configuration exceeds its size limit")
        value = orjson.loads(raw)
    except (OSError, orjson.JSONDecodeError) as ex:
        raise FlowProfileError("Flow profile configuration is not valid JSON") from ex
    return parse_flow_profiles(value)


def parse_flow_profiles(value) -> FlowProfileRegistry:
    if not isinstance(value, Mapping) or set(value) != {"schema_version", "profiles"}:
        raise FlowProfileError(
            "Flow profile configuration requires schema_version and profiles"
        )
    if value["schema_version"] != 1:
        raise FlowProfileError("Unsupported flow profile schema_version")
    configured_profiles = value["profiles"]
    if not isinstance(configured_profiles, Mapping):
        raise FlowProfileError("Flow profiles must be an object")

    profiles = {}
    current_versions = {}
    for name, configured in configured_profiles.items():
        if not isinstance(configured, Mapping) or set(configured) != {
            "current_version",
            "versions",
        }:
            raise FlowProfileError(
                f"Flow profile {name} requires current_version and versions"
            )
        current_version = configured["current_version"]
        versions = configured["versions"]
        if isinstance(current_version, bool) or not isinstance(current_version, int):
            raise FlowProfileError(f"Flow profile {name} current_version is invalid")
        if not isinstance(versions, Mapping) or str(current_version) not in versions:
            raise FlowProfileError(
                f"Flow profile {name} current_version is unavailable"
            )
        current_versions[name] = current_version
        for raw_version, definition in versions.items():
            try:
                version = int(raw_version)
            except (TypeError, ValueError) as ex:
                raise FlowProfileError(
                    "Flow profile version keys must be integers"
                ) from ex
            reference = FlowProfileReference(str(name), version)
            profile = _parse_profile(reference, definition)
            if reference in profiles:
                raise FlowProfileError(f"Duplicate flow profile: {reference}")
            profiles[reference] = profile
            if len(profiles) > MAX_FLOW_PROFILE_VERSIONS:
                raise FlowProfileError("Flow profile version limit exceeded")
    return FlowProfileRegistry(
        MappingProxyType(profiles), MappingProxyType(current_versions)
    )


def calculate_flow_profile_fingerprint(name: str, version: int, value: Mapping) -> str:
    """Calculate the fingerprint pinned beside one immutable profile version."""
    return _parse_profile(
        FlowProfileReference(name, version), value, verify_fingerprint=False
    ).fingerprint


def _parse_profile(
    reference: FlowProfileReference, value, *, verify_fingerprint: bool = True
) -> FlowProfile:
    if not isinstance(value, Mapping):
        raise FlowProfileError(f"Flow profile {reference} must be an object")
    unknown = set(value) - {
        "description",
        "fingerprint",
        "inputs",
        "generated_values",
        "dag",
    }
    if unknown or "inputs" not in value or "dag" not in value:
        raise FlowProfileError(
            f"Flow profile {reference} requires fingerprint, inputs, and dag "
            "with optional description and generated_values"
        )
    description = value.get("description")
    if description is not None and (
        not isinstance(description, str) or not description.strip()
    ):
        raise FlowProfileError("Flow profile description must be non-empty")
    raw_inputs = value["inputs"]
    if not isinstance(raw_inputs, Mapping):
        raise FlowProfileError("Flow profile inputs must be an object")
    inputs = {
        str(name): _parse_input(str(name), definition)
        for name, definition in raw_inputs.items()
    }
    raw_generated = value.get("generated_values", {})
    if not isinstance(raw_generated, Mapping):
        raise FlowProfileError("Flow profile generated_values must be an object")
    generated_values = {}
    for name, definition in raw_generated.items():
        if (
            not isinstance(name, str)
            or not name
            or not name.replace("_", "").isalnum()
            or not name[0].isalpha()
            or not isinstance(definition, Mapping)
            or set(definition) != {"type"}
            or definition["type"] != "uuid"
        ):
            raise FlowProfileError(
                "Generated values require snake_case names and type uuid"
            )
        if name in inputs:
            raise FlowProfileError(
                f"Generated value conflicts with flow input name: {name}"
            )
        generated_values[name] = "uuid"
    template = value["dag"]
    validate_profile_template(template, frozenset(inputs), frozenset(generated_values))
    canonical_template = orjson.dumps(template, option=orjson.OPT_SORT_KEYS)
    semantics = orjson.dumps(
        {
            "name": reference.name,
            "version": reference.version,
            "inputs": {
                name: definition.contract() for name, definition in inputs.items()
            },
            "generated_values": generated_values,
            "dag": template,
        },
        option=orjson.OPT_SORT_KEYS,
    )
    fingerprint = (
        "sha256:" + hashlib.sha256(b"xarta-flow-profile-v1\0" + semantics).hexdigest()
    )
    if verify_fingerprint:
        expected = value.get("fingerprint")
        if not isinstance(expected, str) or not expected:
            raise FlowProfileError(
                f"Flow profile {reference} requires fingerprint {fingerprint}"
            )
        if not hmac.compare_digest(expected, fingerprint):
            raise FlowProfileError(
                f"Flow profile {reference} is immutable: expected fingerprint "
                f"{expected}, received {fingerprint}"
            )
    return FlowProfile(
        reference,
        description.strip() if description else None,
        MappingProxyType(inputs),
        MappingProxyType(generated_values),
        canonical_template,
        fingerprint,
    )


def _parse_input(name: str, value) -> FlowInputDefinition:
    if not name or not name.replace("_", "").isalnum() or not name[0].isalpha():
        raise FlowProfileError("Flow input names must be alphanumeric snake_case")
    if not isinstance(value, Mapping) or set(value) - _INPUT_FIELDS:
        raise FlowProfileError(f"Flow input {name} contains unsupported fields")
    raw_type = value.get("type")
    try:
        input_type = FlowInputType(raw_type) if isinstance(raw_type, str) else None
    except (TypeError, ValueError) as ex:
        raise FlowProfileError(f"Flow input {name} has an unsupported type") from ex
    if input_type is None:
        raise FlowProfileError(f"Flow input {name} has an unsupported type")
    required = value.get("required", True)
    require_version = value.get("require_version", False)
    if not isinstance(required, bool) or not isinstance(require_version, bool):
        raise FlowProfileError(f"Flow input {name} boolean constraints are invalid")
    constraints = {}
    for field in ("maximum_length", "maximum_items", "maximum_bytes"):
        constraint = value.get(field)
        if constraint is not None and (
            isinstance(constraint, bool)
            or not isinstance(constraint, int)
            or constraint <= 0
        ):
            raise FlowProfileError(f"Flow input {name} {field} must be positive")
        constraints[field] = constraint
    allowed_sources = value.get("allowed_sources", [])
    if not isinstance(allowed_sources, list) or any(
        not isinstance(source, str) or not source for source in allowed_sources
    ):
        raise FlowProfileError(f"Flow input {name} allowed_sources is invalid")
    if (
        allowed_sources or require_version
    ) and input_type is not FlowInputType.DOCUMENT_SOURCE:
        raise FlowProfileError(
            f"Flow input {name} document constraints require document-source type"
        )
    if input_type is FlowInputType.DOCUMENT_SOURCE and not allowed_sources:
        raise FlowProfileError(
            f"Flow input {name} document-source requires allowed_sources"
        )
    if constraints["maximum_length"] is not None and input_type not in {
        FlowInputType.STRING,
        FlowInputType.STRING_LIST,
        FlowInputType.LOCALIZED_STRINGS,
    }:
        raise FlowProfileError(
            f"Flow input {name} maximum_length is not valid for its type"
        )
    if constraints["maximum_items"] is not None and input_type not in {
        FlowInputType.STRING_LIST,
        FlowInputType.LOCALIZED_STRINGS,
    }:
        raise FlowProfileError(
            f"Flow input {name} maximum_items is not valid for its type"
        )
    if (
        constraints["maximum_bytes"] is not None
        and input_type is not FlowInputType.JSON_OBJECT
    ):
        raise FlowProfileError(
            f"Flow input {name} maximum_bytes is not valid for its type"
        )
    return FlowInputDefinition(
        input_type,
        required,
        constraints["maximum_length"],
        constraints["maximum_items"],
        constraints["maximum_bytes"],
        frozenset(allowed_sources),
        require_version,
    )
