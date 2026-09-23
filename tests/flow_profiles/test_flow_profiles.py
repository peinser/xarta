from __future__ import annotations

import copy

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import orjson
import pytest

from xarta.exceptions.http import BadRequestError
from xarta.flow_profiles import FlowProfileError
from xarta.flow_profiles import FlowProfileReference
from xarta.flow_profiles import calculate_flow_profile_fingerprint
from xarta.flow_profiles import compile_flow_profile
from xarta.flow_profiles import parse_flow_profiles
from xarta.flow_profiles.compiler import validate_profile_template
from xarta.services.v1.intake.base import submit_flow_profile
from xarta.services.v1.intake.capabilities import CapabilityManifest
from xarta.services.v1.intake.preparation import IntakeValidationError
from xarta.services.v1.intake.preparation import prepare_intake


def configuration(*, dynamic_url: bool = False, pin: bool = True) -> dict:
    webhook_url = {"$input": "payload"} if dynamic_url else "https://example.test/event"
    configured = {
        "schema_version": 1,
        "profiles": {
            "payroll-standard": {
                "current_version": 7,
                "versions": {
                    "7": {
                        "description": "Deliver one payroll document",
                        "inputs": {
                            "document": {
                                "type": "document-source",
                                "allowed_sources": ["generate"],
                            },
                            "payload": {
                                "type": "json-object",
                                "maximum_bytes": 1024,
                            },
                        },
                        "generated_values": {"delivery_attempt_id": {"type": "uuid"}},
                        "dag": {
                            "node_key": "generate-document",
                            "kind": "generate",
                            "documents": [{"$input": "document"}],
                            "on": {
                                "success": [
                                    {
                                        "node_key": "notify-caller",
                                        "kind": "webhook",
                                        "url": webhook_url,
                                        "data": {
                                            "payload": {"$input": "payload"},
                                            "delivery_attempt_id": {
                                                "$generated": "delivery_attempt_id"
                                            },
                                        },
                                    }
                                ]
                            },
                        },
                    }
                },
            }
        },
    }
    if pin:
        profile = configured["profiles"]["payroll-standard"]["versions"]["7"]
        profile["fingerprint"] = calculate_flow_profile_fingerprint(
            "payroll-standard", 7, profile
        )
    return configured


def test_signature_policy_is_literal_profile_mechanics() -> None:
    validate_profile_template(
        {
            "node_key": "sign-document",
            "kind": "signature",
            "policy": "pades-b-lt-v1",
            "documents": [],
        },
        declared_inputs=frozenset(),
        declared_generated=frozenset(),
    )

    with pytest.raises(FlowProfileError, match="execution mechanics field policy"):
        validate_profile_template(
            {
                "node_key": "sign-document",
                "kind": "signature",
                "policy": {"$input": "policy"},
                "documents": [],
            },
            declared_inputs=frozenset({"policy"}),
            declared_generated=frozenset(),
        )

    validate_profile_template(
        {
            "node_key": "notify-caller",
            "kind": "webhook",
            "url": "https://example.test",
            "data": {"policy": {"$input": "policy"}},
        },
        declared_inputs=frozenset({"policy"}),
        declared_generated=frozenset(),
    )


def values() -> dict:
    return {
        "document": {"source": "generate", "id": str(uuid4())},
        "payload": {"payroll_run": "2026-08"},
    }


def test_registry_exposes_contract_without_exposing_dag() -> None:
    registry = parse_flow_profiles(configuration())

    contract = registry.contracts()[0]

    assert contract["delivery_profile"] == "payroll-standard@7"
    assert contract["current"] is True
    assert contract["inputs"]["document"]["type"] == "document-source"
    assert contract["generated_values"] == {"delivery_attempt_id": "uuid"}
    assert "dag" not in contract
    with pytest.raises(TypeError):
        registry.profiles[FlowProfileReference("other", 1)] = None


def test_profile_version_fingerprint_rejects_semantic_mutation() -> None:
    configured = configuration()
    definition = configured["profiles"]["payroll-standard"]["versions"]["7"]
    expected = definition["fingerprint"]
    definition["dag"]["on"]["success"][0]["url"] = "https://example.test/changed"
    received = calculate_flow_profile_fingerprint("payroll-standard", 7, definition)

    with pytest.raises(
        FlowProfileError,
        match=f"expected fingerprint {expected}, received {received}",
    ):
        parse_flow_profiles(configured)


def test_profile_fingerprint_is_deterministic_and_new_version_is_allowed() -> None:
    configured = configuration()
    version_seven = configured["profiles"]["payroll-standard"]["versions"]["7"]
    reordered = orjson.loads(orjson.dumps(version_seven, option=orjson.OPT_SORT_KEYS))
    assert calculate_flow_profile_fingerprint(
        "payroll-standard", 7, version_seven
    ) == calculate_flow_profile_fingerprint("payroll-standard", 7, reordered)

    version_eight = copy.deepcopy(version_seven)
    version_eight.pop("fingerprint")
    version_eight["dag"]["on"]["success"][0]["url"] = "https://example.test/version-8"
    version_eight["fingerprint"] = calculate_flow_profile_fingerprint(
        "payroll-standard", 8, version_eight
    )
    profile = configured["profiles"]["payroll-standard"]
    profile["versions"]["8"] = version_eight
    profile["current_version"] = 8

    registry = parse_flow_profiles(configured)

    assert registry.resolve(FlowProfileReference("payroll-standard", 7))
    assert registry.resolve(FlowProfileReference("payroll-standard", 8))


def test_duplicate_normalized_profile_versions_are_rejected() -> None:
    configured = configuration()
    configured["profiles"]["payroll-standard"]["versions"]["07"] = copy.deepcopy(
        configured["profiles"]["payroll-standard"]["versions"]["7"]
    )

    with pytest.raises(FlowProfileError, match="Duplicate flow profile"):
        parse_flow_profiles(configured)


def test_profile_requires_exact_version_and_server_owned_mechanics() -> None:
    with pytest.raises(FlowProfileError, match="name@version"):
        FlowProfileReference.parse("payroll-standard")
    with pytest.raises(FlowProfileError, match="execution mechanics field url"):
        parse_flow_profiles(configuration(dynamic_url=True, pin=False))

    nested_header = configuration(pin=False)
    version = nested_header["profiles"]["payroll-standard"]["versions"]["7"]
    version["inputs"]["token"] = {"type": "string"}
    webhook = version["dag"]["on"]["success"][0]
    webhook["headers"] = {"Authorization": {"$input": "token"}}
    with pytest.raises(FlowProfileError, match="execution mechanics field"):
        parse_flow_profiles(nested_header)

    misspelled = configuration(pin=False)
    misspelled["profiles"]["payroll-standard"]["versions"]["7"]["dag"]["documnts"] = []
    with pytest.raises(FlowProfileError, match="unsupported fields: documnts"):
        parse_flow_profiles(misspelled)


def test_compilation_is_deterministic_and_returns_fresh_dags() -> None:
    profile = parse_flow_profiles(configuration()).resolve(
        FlowProfileReference("payroll-standard", 7)
    )
    flow_id = uuid4()

    first = compile_flow_profile(profile, flow_id=flow_id, values=values())
    second_values = values()
    second_values["document"] = first.documents[0]
    second = compile_flow_profile(profile, flow_id=flow_id, values=second_values)
    third = compile_flow_profile(profile, flow_id=uuid4(), values=values())

    assert first.id == second.id
    assert first.on["success"][0].id == second.on["success"][0].id
    assert (
        first.on["success"][0].dict()["data"]["delivery_attempt_id"]
        == second.on["success"][0].dict()["data"]["delivery_attempt_id"]
    )
    assert first is not second
    assert first.on["success"][0] is not second.on["success"][0]
    assert first.id != third.id
    assert (
        first.on["success"][0].dict()["data"]["delivery_attempt_id"]
        != third.on["success"][0].dict()["data"]["delivery_attempt_id"]
    )

    changed_configuration = copy.deepcopy(configuration())
    configured_profile = changed_configuration["profiles"]["payroll-standard"]
    changed_version = copy.deepcopy(configured_profile["versions"]["7"])
    changed_version.pop("fingerprint")
    changed_version["dag"]["on"]["success"][0]["url"] = "https://example.test/changed"
    changed_version["fingerprint"] = calculate_flow_profile_fingerprint(
        "payroll-standard", 8, changed_version
    )
    configured_profile["versions"]["8"] = changed_version
    configured_profile["current_version"] = 8
    changed_profile = parse_flow_profiles(changed_configuration).resolve(
        FlowProfileReference("payroll-standard", 8)
    )
    changed = compile_flow_profile(
        changed_profile, flow_id=flow_id, values=second_values
    )
    assert first.id != changed.id


def test_inserted_caller_json_is_not_evaluated_as_profile_syntax() -> None:
    profile = parse_flow_profiles(configuration()).resolve(
        FlowProfileReference("payroll-standard", 7)
    )
    supplied = values()
    supplied["payload"] = {"literal": {"$input": "not-a-real-input"}}

    compiled = compile_flow_profile(profile, flow_id=uuid4(), values=supplied)

    assert compiled.on["success"][0].dict()["data"]["payload"] == supplied["payload"]


def test_generated_document_id_connects_producer_and_consumer() -> None:
    configured = {
        "schema_version": 1,
        "profiles": {
            "render-and-send": {
                "current_version": 1,
                "versions": {
                    "1": {
                        "inputs": {
                            "render_payload": {
                                "type": "json-object",
                                "maximum_bytes": 4096,
                            }
                        },
                        "generated_values": {"rendered_document_id": {"type": "uuid"}},
                        "dag": {
                            "node_key": "render-document",
                            "kind": "generate",
                            "documents": [
                                {
                                    "source": "render",
                                    "id": {"$generated": "rendered_document_id"},
                                    "request": {
                                        "payload": {"$input": "render_payload"}
                                    },
                                }
                            ],
                            "on": {
                                "success": [
                                    {
                                        "node_key": "send-peppol",
                                        "kind": "peppol",
                                        "document": {
                                            "source": "generate",
                                            "id": {
                                                "$generated": "rendered_document_id"
                                            },
                                        },
                                    }
                                ]
                            },
                        },
                    }
                },
            }
        },
    }
    profile_definition = configured["profiles"]["render-and-send"]["versions"]["1"]
    profile_definition["fingerprint"] = calculate_flow_profile_fingerprint(
        "render-and-send", 1, profile_definition
    )
    registry = parse_flow_profiles(configured)
    profile = registry.resolve(FlowProfileReference("render-and-send", 1))

    compiled = compile_flow_profile(
        profile,
        flow_id=uuid4(),
        values={"render_payload": {"invoice": "INV-1"}},
    )

    produced_id = compiled.documents[0]["id"]
    consumed_id = compiled.on["success"][0].document["id"]
    assert produced_id == consumed_id


def test_prepare_profile_compiles_before_capability_validation() -> None:
    registry = parse_flow_profiles(configuration())
    flow_id = uuid4()
    prepared = prepare_intake(
        {
            "id": str(flow_id),
            "delivery_profile": "payroll-standard@7",
            "delivery_values": values(),
        },
        capabilities=CapabilityManifest(frozenset({"generate", "webhook"})),
        profiles=registry,
    )

    assert prepared.profile_reference == FlowProfileReference("payroll-standard", 7)
    assert prepared.profile_fingerprint.startswith("sha256:")
    assert prepared.semantic_fingerprint.startswith("sha256:")
    assert prepared.flow.dag.kind == "generate"
    assert prepared.flow.dag.on["success"][0].kind == "webhook"

    with pytest.raises(IntakeValidationError, match="exactly one"):
        prepare_intake(
            {
                "dag": {"kind": "generate", "documents": []},
                "delivery_profile": "payroll-standard@7",
            },
            capabilities=CapabilityManifest(frozenset({"generate", "webhook"})),
            profiles=registry,
        )


def test_semantic_fingerprint_binds_compiled_values_not_correlation_id() -> None:
    registry = parse_flow_profiles(configuration())
    flow_id = uuid4()
    delivery_values = values()

    first = prepare_intake(
        {
            "id": str(flow_id),
            "correlation_id": str(uuid4()),
            "delivery_profile": "payroll-standard@7",
            "delivery_values": delivery_values,
        },
        capabilities=CapabilityManifest(frozenset({"generate", "webhook"})),
        profiles=registry,
    )
    second = prepare_intake(
        {
            "id": str(flow_id),
            "correlation_id": str(uuid4()),
            "delivery_profile": "payroll-standard@7",
            "delivery_values": delivery_values,
        },
        capabilities=CapabilityManifest(frozenset({"generate", "webhook"})),
        profiles=registry,
    )

    assert first.semantic_fingerprint == second.semantic_fingerprint


@pytest.mark.asyncio
async def test_profile_specific_api_accepts_only_declared_input_envelope(
    monkeypatch,
) -> None:
    registry = parse_flow_profiles(configuration())
    commit = AsyncMock(return_value=uuid4())
    monkeypatch.setattr("xarta.services.v1.intake.base.commit_intake", commit)
    request = SimpleNamespace(
        files={},
        json={"inputs": values()},
        app=SimpleNamespace(
            ctx=SimpleNamespace(
                intake_capabilities=CapabilityManifest(
                    frozenset({"generate", "webhook"})
                ),
                flow_profiles=registry,
            )
        ),
    )

    result = await submit_flow_profile(request, "payroll-standard", 7)

    assert result.status == 202
    assert orjson.loads(result.body)["delivery_profile"] == "payroll-standard@7"
    commit.assert_awaited_once()

    request.json = {"inputs": values(), "dag": {}}
    with pytest.raises(BadRequestError, match="Unsupported"):
        await submit_flow_profile(request, "payroll-standard", 7)
