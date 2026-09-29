from __future__ import annotations

from copy import deepcopy
from uuid import uuid4

import jsonschema
import pytest

from xarta.flow_profiles import calculate_flow_profile_fingerprint
from xarta.flow_profiles import compile_flow_profile
from xarta.flow_profiles import parse_flow_profiles
from xarta.flow_profiles.models import FlowProfileReference
from xarta.pricing.graph import maximum_outcome_emissions
from xarta.protocol.dag import parse
from xarta.protocol.dag.ubl import UBLNode
from xarta.protocol.document.source import TemporaryDocumentSource
from xarta.services.v1.intake.capabilities import CapabilityManifest


def specification():
    return {
        "kind": "ubl",
        "document": {"id": str(uuid4())},
        "operations": [
            {
                "action": "add_attachment",
                "id": "evidence",
                "document": {"id": str(uuid4())},
                "filename": "evidence.pdf",
            }
        ],
        "out": str(uuid4()),
        "on": {"success": [{"kind": "debug"}]},
    }


def test_roundtrip_schema_and_default_references():
    value = specification()
    node = parse(value)
    assert isinstance(node, UBLNode)
    assert isinstance(node.interpret().document, TemporaryDocumentSource)
    assert isinstance(node.interpret().operations[0].document, TemporaryDocumentSource)
    assert parse(node.dict()).dict() == node.dict()
    assert node.on["success"][0].parent is node
    jsonschema.validate(
        {"dag": value}, CapabilityManifest(frozenset({"ubl", "debug"})).schema()
    )


@pytest.mark.parametrize(
    "update",
    [
        {"operations": []},
        {"out": "invalid"},
        {"document": "invalid"},
        {"operations": [{"action": "patch_xml"}]},
    ],
)
def test_invalid_node(update):
    with pytest.raises((ValueError, TypeError)):
        parse({**specification(), **update})


@pytest.mark.parametrize(
    "update",
    [
        {"filename": "../file.pdf"},
        {"filename": "dir\\file.pdf"},
        {"filename": ""},
        {"id": " "},
        {"description": "x\x00"},
        {"description": "\ud800"},
        {"id": "\uffff"},
        {"unexpected": True},
        {"document": None},
    ],
)
def test_invalid_attachment(update):
    value = specification()
    value["operations"][0].update(update)
    with pytest.raises((ValueError, TypeError)):
        parse(value)


def test_reject_duplicates_and_all_input_collisions():
    value = specification()
    value["operations"] *= 2
    with pytest.raises(ValueError, match="unique"):
        parse(value)
    for reference in ("document", "attachment"):
        value = specification()
        if reference == "document":
            value["out"] = value["document"]["id"]
        else:
            value["out"] = value["operations"][0]["document"]["id"]
        with pytest.raises(ValueError, match="overwrite"):
            parse(value)


def test_node_copies_operation_payload():
    value = specification()
    original = deepcopy(value)
    node = parse(value)
    value["operations"][0]["filename"] = "mutated.pdf"
    assert node.operations == original["operations"]


def test_profile_compilation_and_pricing_cardinality():
    template = specification()
    template.pop("on")
    template["node_key"] = "attach-evidence"
    template["document"] = {"$input": "invoice"}
    template["operations"][0]["document"] = {"$input": "evidence"}
    template["out"] = {"$generated": "enriched"}
    definition = {
        "inputs": {
            name: {"type": "document-source", "allowed_sources": ["temporary"]}
            for name in ("invoice", "evidence")
        },
        "generated_values": {"enriched": {"type": "uuid"}},
        "dag": template,
    }
    definition["fingerprint"] = calculate_flow_profile_fingerprint(
        "enrich", 1, definition
    )
    registry = parse_flow_profiles(
        {
            "schema_version": 1,
            "profiles": {
                "enrich": {"current_version": 1, "versions": {"1": definition}}
            },
        }
    )
    profile = registry.profiles[FlowProfileReference("enrich", 1)]
    flow_id = uuid4()
    values = {name: {"id": str(uuid4())} for name in ("invoice", "evidence")}
    first = compile_flow_profile(profile, flow_id=flow_id, values=values)
    second = compile_flow_profile(profile, flow_id=flow_id, values=values)
    assert isinstance(first, UBLNode)
    assert first.dict() == second.dict()
    assert first.interpret().document.source == "temporary"
    assert maximum_outcome_emissions(first, "success") == 1
    assert maximum_outcome_emissions(first, "failure") == 1
