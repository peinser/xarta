from __future__ import annotations

import copy
import datetime
import re
import uuid

from collections.abc import Mapping

import orjson

from xarta.protocol.dag import Node
from xarta.protocol.dag import parse

from .models import FlowInputDefinition
from .models import FlowInputType
from .models import FlowProfile
from .models import FlowProfileError

_NODE_KEY = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_MAX_PROFILE_NODES = 1_000
_MECHANICS_FIELDS = frozenset(
    {
        "adapter",
        "auth",
        "destination",
        "document_type",
        "headers",
        "kind",
        "method",
        "multipart",
        "sender",
        "template_engine",
        "url",
    }
)
_NODE_FIELDS = {
    "archive": {"documents", "destination"},
    "debug": set(),
    "doccle": {"receiver", "document", "document_type", "name", "published_at"},
    "email": {
        "to",
        "sender",
        "body",
        "destination",
        "cc",
        "bcc",
        "attachments",
        "reply_to",
        "subject",
    },
    "generate": {"documents"},
    "bundle": {"documents", "out", "compression"},
    "peppol": {"document"},
    "postal": {"destination", "mailpiece"},
    "search-index": {"document", "destination"},
    "sftp": {"document", "path", "destination"},
    "signature": {"documents", "policy"},
    "transform": {"convert", "merge", "split"},
    "wait-for": {"backoffs", "documents"},
    "webhook": {"url", "method", "data", "auth", "headers", "multipart"},
}


def validate_profile_template(
    template,
    declared_inputs: frozenset[str],
    declared_generated: frozenset[str],
) -> None:
    node_keys = set()
    references = set()
    generated_references = set()

    def visit(value, *, path: tuple[str, ...], node: bool = False) -> None:
        if isinstance(value, Mapping):
            if set(value) == {"$input"}:
                if node:
                    raise FlowProfileError("Flow inputs cannot replace DAG nodes")
                name = value["$input"]
                if not isinstance(name, str) or name not in declared_inputs:
                    raise FlowProfileError(
                        "Flow profile references an undeclared input"
                    )
                if any(segment in _MECHANICS_FIELDS for segment in path):
                    raise FlowProfileError(
                        f"Flow inputs cannot replace execution mechanics field {path[-1]}"
                    )
                references.add(name)
                return
            if set(value) == {"$generated"}:
                if node:
                    raise FlowProfileError("Generated values cannot replace DAG nodes")
                name = value["$generated"]
                if not isinstance(name, str) or name not in declared_generated:
                    raise FlowProfileError(
                        "Flow profile references an undeclared generated value"
                    )
                if any(segment in _MECHANICS_FIELDS for segment in path):
                    raise FlowProfileError(
                        f"Generated values cannot replace execution mechanics field {path[-1]}"
                    )
                generated_references.add(name)
                return
            if "$input" in value or "$generated" in value:
                raise FlowProfileError("A flow value slot cannot contain other fields")
            if node:
                unknown_identity = {"id", "parent"} & set(value)
                if unknown_identity:
                    raise FlowProfileError(
                        "Profile DAG nodes cannot declare id or parent"
                    )
                key = value.get("node_key")
                if not isinstance(key, str) or not _NODE_KEY.fullmatch(key):
                    raise FlowProfileError(
                        "Every profile DAG node requires a kebab-case node_key"
                    )
                if key in node_keys:
                    raise FlowProfileError(f"Duplicate profile node_key: {key}")
                node_keys.add(key)
                if len(node_keys) > _MAX_PROFILE_NODES:
                    raise FlowProfileError("Flow profile node limit exceeded")
                if not isinstance(value.get("kind"), str):
                    raise FlowProfileError("Every profile DAG node requires kind")
                kind = value["kind"]
                allowed_fields = _NODE_FIELDS.get(kind)
                if allowed_fields is None:
                    raise FlowProfileError(f"Unknown profile DAG node kind: {kind}")
                if kind == "signature" and isinstance(value.get("policy"), Mapping):
                    raise FlowProfileError(
                        "Flow inputs cannot replace execution mechanics field policy"
                    )
                unknown_fields = set(value) - {
                    "node_key",
                    "kind",
                    "on",
                    *allowed_fields,
                }
                if unknown_fields:
                    raise FlowProfileError(
                        f"Profile {kind} node contains unsupported fields: "
                        f"{', '.join(sorted(unknown_fields))}"
                    )
            for name, item in value.items():
                if name == "node_key":
                    continue
                if name == "on" and isinstance(item, Mapping):
                    for outcome, successors in item.items():
                        if not isinstance(successors, list):
                            raise FlowProfileError(
                                "Profile DAG successors must be arrays"
                            )
                        for successor in successors:
                            visit(successor, path=("on", str(outcome)), node=True)
                    continue
                visit(item, path=(*path, str(name)))
        elif isinstance(value, list):
            for item in value:
                visit(item, path=path)

    if not isinstance(template, Mapping):
        raise FlowProfileError("Flow profile dag must be an object")
    visit(template, path=(), node=True)
    unused = declared_inputs - references
    if unused:
        raise FlowProfileError(
            f"Flow profile declares unused inputs: {', '.join(sorted(unused))}"
        )
    unused_generated = declared_generated - generated_references
    if unused_generated:
        raise FlowProfileError(
            "Flow profile declares unused generated values: "
            f"{', '.join(sorted(unused_generated))}"
        )


def compile_flow_profile(
    profile: FlowProfile, *, flow_id: uuid.UUID, values: Mapping[str, object]
) -> Node:
    if not isinstance(values, Mapping):
        raise FlowProfileError("Flow profile inputs must be an object")
    unknown = set(values) - set(profile.inputs)
    if unknown:
        raise FlowProfileError(f"Unknown flow inputs: {', '.join(sorted(unknown))}")
    normalized: dict[str, object] = {}
    for name, definition in profile.inputs.items():
        if name not in values:
            if definition.required:
                raise FlowProfileError(f"Missing required flow input: {name}")
            normalized[name] = None
            continue
        normalized[name] = _normalize_input(name, definition, values[name])
    generated = {
        name: str(
            uuid.uuid5(
                flow_id,
                f"xarta:flow-profile:{profile.reference}:{profile.fingerprint}:generated:{name}",
            )
        )
        for name in profile.generated_values
    }

    template = orjson.loads(profile.canonical_template)

    def substitute(value):
        if isinstance(value, Mapping):
            if set(value) == {"$input"}:
                name = value["$input"]
                return copy.deepcopy(normalized[name])
            if set(value) == {"$generated"}:
                return generated[value["$generated"]]
            return {name: substitute(item) for name, item in value.items()}
        if isinstance(value, list):
            return [substitute(item) for item in value]
        return value

    compiled = substitute(template)

    def assign_node_ids(node) -> None:
        key = node.pop("node_key")
        node["id"] = str(
            uuid.uuid5(
                flow_id,
                f"xarta:flow-profile:{profile.reference}:{profile.fingerprint}:node:{key}",
            )
        )
        for successors in (node.get("on") or {}).values():
            for successor in successors:
                assign_node_ids(successor)

    assign_node_ids(compiled)
    try:
        return parse(compiled)
    except (KeyError, TypeError, ValueError) as ex:
        raise FlowProfileError(
            f"Flow profile {profile.reference} compiled to an invalid DAG"
        ) from ex


def profile_validation_values(profile: FlowProfile) -> dict[str, object]:
    values: dict[str, object] = {}
    for name, definition in profile.inputs.items():
        if definition.type is FlowInputType.STRING:
            values[name] = "value"
        elif definition.type is FlowInputType.STRING_LIST:
            values[name] = ["value"]
        elif definition.type is FlowInputType.UUID:
            values[name] = "00000000-0000-0000-0000-000000000001"
        elif definition.type is FlowInputType.INSTANT:
            values[name] = "2026-01-01T00:00:00+00:00"
        elif definition.type is FlowInputType.DOCUMENT_SOURCE:
            values[name] = {
                "source": next(iter(sorted(definition.allowed_sources)), "generate"),
                "id": "00000000-0000-0000-0000-000000000002",
                **(
                    {"version": "00000000-0000-0000-0000-000000000003"}
                    if definition.require_version
                    else {}
                ),
            }
        elif definition.type is FlowInputType.LOCALIZED_STRINGS:
            values[name] = {"en": "value"}
        elif definition.type is FlowInputType.DOCCLE_RECEIVER:
            values[name] = {"subject": {"reference": "value"}}
        else:
            values[name] = {"value": "example"}
    return values


def _normalize_input(name: str, definition: FlowInputDefinition, value):
    if definition.type is FlowInputType.STRING:
        if not isinstance(value, str) or not value:
            raise FlowProfileError(f"Flow input {name} must be a non-empty string")
        _check_string_length(name, definition, value)
        return value
    if definition.type is FlowInputType.STRING_LIST:
        if not isinstance(value, list) or any(
            not isinstance(item, str) or not item for item in value
        ):
            raise FlowProfileError(f"Flow input {name} must be an array of strings")
        if (
            definition.maximum_items is not None
            and len(value) > definition.maximum_items
        ):
            raise FlowProfileError(f"Flow input {name} has too many items")
        for item in value:
            _check_string_length(name, definition, item)
        return list(value)
    if definition.type is FlowInputType.UUID:
        try:
            return str(uuid.UUID(str(value)))
        except (TypeError, ValueError) as ex:
            raise FlowProfileError(f"Flow input {name} must be a UUID") from ex
    if definition.type is FlowInputType.INSTANT:
        if not isinstance(value, str):
            raise FlowProfileError(f"Flow input {name} must be an ISO 8601 instant")
        try:
            instant = datetime.datetime.fromisoformat(value)
        except ValueError as ex:
            raise FlowProfileError(
                f"Flow input {name} must be an ISO 8601 instant"
            ) from ex
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise FlowProfileError(f"Flow input {name} must include a timezone")
        return instant.isoformat()
    if definition.type is FlowInputType.DOCUMENT_SOURCE:
        if not isinstance(value, Mapping):
            raise FlowProfileError(f"Flow input {name} must be a document source")
        if set(value) - {"source", "id", "version"}:
            raise FlowProfileError(
                f"Flow input {name} document source has unknown fields"
            )
        source = value.get("source")
        identifier = value.get("id")
        version = value.get("version")
        if not isinstance(source, str) or not source:
            raise FlowProfileError(f"Flow input {name} requires source")
        if definition.allowed_sources and source not in definition.allowed_sources:
            raise FlowProfileError(f"Flow input {name} uses a disallowed source")
        if not isinstance(identifier, str) or not identifier:
            raise FlowProfileError(f"Flow input {name} requires id")
        if definition.require_version and (not isinstance(version, str) or not version):
            raise FlowProfileError(f"Flow input {name} requires version")
        return dict(value)
    if definition.type is FlowInputType.LOCALIZED_STRINGS:
        if (
            not isinstance(value, Mapping)
            or not value
            or any(
                not isinstance(language, str)
                or not language
                or not isinstance(text, str)
                or not text
                for language, text in value.items()
            )
        ):
            raise FlowProfileError(
                f"Flow input {name} must map languages to non-empty strings"
            )
        if (
            definition.maximum_items is not None
            and len(value) > definition.maximum_items
        ):
            raise FlowProfileError(f"Flow input {name} has too many languages")
        for text in value.values():
            _check_string_length(name, definition, text)
        return dict(value)
    if definition.type is FlowInputType.DOCCLE_RECEIVER:
        if not isinstance(value, Mapping) or bool(value.get("id")) == bool(
            value.get("subject")
        ):
            raise FlowProfileError(
                f"Flow input {name} requires exactly one receiver id or subject"
            )
        return copy.deepcopy(dict(value))
    if definition.type is FlowInputType.JSON_OBJECT:
        if not isinstance(value, Mapping):
            raise FlowProfileError(f"Flow input {name} must be an object")
        encoded = orjson.dumps(value)
        limit = definition.maximum_bytes or 16 * 1024
        if len(encoded) > limit:
            raise FlowProfileError(f"Flow input {name} exceeds its byte limit")
        return copy.deepcopy(dict(value))
    raise FlowProfileError(f"Flow input {name} has unsupported type")


def _check_string_length(
    name: str, definition: FlowInputDefinition, value: str
) -> None:
    if definition.maximum_length is not None and len(value) > definition.maximum_length:
        raise FlowProfileError(f"Flow input {name} exceeds its length limit")
