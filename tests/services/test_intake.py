from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from typing import cast
from unittest.mock import AsyncMock
from unittest.mock import Mock
from uuid import uuid4
from uuid import uuid5

import orjson
import pytest

from sanic.request.parameters import RequestParameters

from xarta.crypto.sign.policy import ELECTRONIC_SEAL_V1
from xarta.crypto.sign.policy import SignaturePolicyRegistry
from xarta.exceptions.http import BadRequestError
from xarta.exceptions.http import ServiceUnavailable
from xarta.nats.sanic import NATSRuntime
from xarta.protocol.dag import NodeTask
from xarta.protocol.dag.signature import SignatureNode
from xarta.services.v1.intake.base import _request_uploads
from xarta.services.v1.intake.base import commit_intake
from xarta.services.v1.intake.base import intake
from xarta.services.v1.intake.capabilities import CapabilityManifest
from xarta.services.v1.intake.capabilities import UnsupportedCapabilitiesError
from xarta.services.v1.intake.nats import IntakeNATSModel
from xarta.services.v1.intake.preparation import IntakeUpload
from xarta.services.v1.intake.preparation import IntakeValidationError
from xarta.services.v1.intake.preparation import prepare_intake

GENERATE_CAPABILITIES = CapabilityManifest(frozenset({"generate"}))
SIGNATURE_CAPABILITIES = CapabilityManifest(frozenset({"debug", "signature"}))


def generate_flow(**extra) -> dict:
    return {
        "dag": {"kind": "generate", "documents": []},
        **extra,
    }


def test_prepare_json_intake_has_no_side_effects() -> None:
    prepared = prepare_intake(
        generate_flow(),
        capabilities=GENERATE_CAPABILITIES,
    )

    assert prepared.flow.dag.kind == "generate"
    assert prepared.sources == ()


def test_prepare_intake_pins_nested_signature_policy_defaults() -> None:
    prepared = prepare_intake(
        {
            "dag": {
                "kind": "signature",
                "documents": [],
                "on": {
                    "success": [
                        {
                            "kind": "debug",
                            "on": {"failure": [{"kind": "signature", "documents": []}]},
                        }
                    ],
                    "failure": [
                        {
                            "kind": "signature",
                            "policy": "explicit-v1",
                            "documents": [],
                        }
                    ],
                },
            }
        },
        capabilities=SIGNATURE_CAPABILITIES,
        default_signature_policy="default-v1",
    )

    root = prepared.flow.dag
    assert isinstance(root, SignatureNode)
    assert root.policy == "default-v1"
    assert root.on is not None
    nested_debug = root.on["success"][0]
    assert nested_debug.on is not None
    nested_default = nested_debug.on["failure"][0]
    explicit = root.on["failure"][0]
    assert isinstance(nested_default, SignatureNode)
    assert nested_default.policy == "default-v1"
    assert isinstance(explicit, SignatureNode)
    assert explicit.policy == "explicit-v1"


def test_pinned_signature_policy_survives_default_change_and_task_round_trip() -> None:
    prepared = prepare_intake(
        {"dag": {"kind": "signature", "documents": []}},
        capabilities=CapabilityManifest(frozenset({"signature"})),
        default_signature_policy="xarta-dev-seal-v1",
    )
    task = NodeTask(flow_id=prepared.flow.id, node=prepared.flow.dag)

    restored = NodeTask.fromdict(task.dict())

    assert isinstance(restored.node, SignatureNode)
    assert restored.node.policy == "xarta-dev-seal-v1"
    assert restored.node.policy != "pades-b-lt-v1"


def test_prepare_intake_rejects_explicit_unavailable_signature_policy() -> None:
    with pytest.raises(IntakeValidationError, match="Unknown signature policy"):
        prepare_intake(
            {
                "dag": {
                    "kind": "signature",
                    "policy": "missing",
                    "documents": [],
                }
            },
            capabilities=CapabilityManifest(frozenset({"signature"})),
            default_signature_policy=ELECTRONIC_SEAL_V1.id,
            signature_policies=SignaturePolicyRegistry((ELECTRONIC_SEAL_V1,)),
        )


def test_prepare_multipart_intake_preserves_file_metadata() -> None:
    identifier = uuid4()
    prepared = prepare_intake(
        orjson.dumps(
            generate_flow(
                files={
                    str(identifier): {
                        "document_type": "invoice",
                        "metadata": {"customer": "example"},
                    }
                }
            )
        ),
        uploads=(
            IntakeUpload(
                name=str(identifier),
                content_type="application/pdf",
                body=b"document",
            ),
        ),
        capabilities=GENERATE_CAPABILITIES,
    )

    assert len(prepared.sources) == 1
    source = prepared.sources[0]
    assert source.id == identifier
    assert source.data == b"document"
    assert source.content_type == "application/pdf"
    assert source.document_type.value == "invoice"
    assert source.metadata == {"customer": "example"}


def test_manifest_rejects_unknown_protocol_capability() -> None:
    with pytest.raises(ValueError, match="unknown node kinds: imaginary"):
        CapabilityManifest.from_json('["generate", "imaginary"]')


def test_manifest_reports_all_unavailable_nested_capabilities() -> None:
    prepared = prepare_intake(
        {
            "dag": {
                "kind": "generate",
                "documents": [],
                "on": {
                    "success": [
                        {"kind": "debug"},
                        {
                            "kind": "webhook",
                            "url": "https://example.test",
                        },
                    ]
                },
            }
        },
        capabilities=CapabilityManifest(frozenset({"debug", "generate", "webhook"})),
    )

    with pytest.raises(UnsupportedCapabilitiesError) as captured:
        CapabilityManifest(frozenset({"generate"})).require_supported(prepared.flow.dag)

    assert captured.value.kinds == frozenset({"debug", "webhook"})


@pytest.mark.parametrize(
    "dag",
    (
        [],
        {
            "kind": "generate",
            "documents": [],
            "on": {"success": [42]},
        },
    ),
)
def test_prepare_rejects_non_object_dag_nodes(dag) -> None:
    with pytest.raises(IntakeValidationError, match="flow definition is invalid"):
        prepare_intake(
            {"dag": dag},
            capabilities=GENERATE_CAPABILITIES,
        )


def test_prepare_reports_actionable_node_validation_error() -> None:
    with pytest.raises(
        IntakeValidationError,
        match="SFTP path must not be absolute or contain traversal",
    ):
        prepare_intake(
            {
                "dag": {
                    "kind": "sftp",
                    "document": {
                        "source": "generate",
                        "id": "10000000-0000-0000-0000-000000000001",
                    },
                    "path": "../private/document.pdf",
                }
            },
            capabilities=CapabilityManifest(frozenset({"sftp"})),
        )


def test_multipart_requires_exactly_one_flow_file() -> None:
    request = SimpleNamespace(
        files=RequestParameters(
            {
                "flow": [
                    SimpleNamespace(body=orjson.dumps(generate_flow())),
                    SimpleNamespace(body=orjson.dumps(generate_flow())),
                ]
            }
        )
    )

    with pytest.raises(BadRequestError, match="exactly one flow file"):
        _request_uploads(cast("Any", request))


@pytest.mark.asyncio
async def test_commit_persists_every_upload_before_scheduling(
    monkeypatch,
) -> None:
    first_id = uuid4()
    second_id = uuid4()
    prepared = prepare_intake(
        generate_flow(),
        uploads=(
            IntakeUpload(str(first_id), "application/pdf", b"first"),
            IntakeUpload(str(second_id), "application/pdf", b"second"),
        ),
        capabilities=GENERATE_CAPABILITIES,
    )
    persisted = []

    async def persist(source) -> None:
        persisted.append(source.id)

    async def schedule(flow, admission=None) -> None:
        assert set(persisted) == {first_id, second_id}
        assert flow is prepared.flow
        assert admission is None

    monkeypatch.setattr(type(prepared.sources[0]), "persist", persist)
    monkeypatch.setattr(
        "xarta.services.v1.intake.base.NATS.schedule",
        schedule,
    )

    flow_id = await commit_intake(prepared)

    assert flow_id == prepared.flow.id


@pytest.mark.asyncio
async def test_route_rejects_unavailable_capability_before_commit(
    monkeypatch,
) -> None:
    commit = AsyncMock()
    monkeypatch.setattr("xarta.services.v1.intake.base.commit_intake", commit)
    request = SimpleNamespace(
        files={},
        json={"dag": {"kind": "debug"}},
        app=SimpleNamespace(
            ctx=SimpleNamespace(intake_capabilities=GENERATE_CAPABILITIES)
        ),
    )

    with pytest.raises(BadRequestError, match="debug"):
        await intake(request)

    commit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ("id", "correlation_id"))
async def test_route_rejects_non_string_flow_identifiers_before_commit(
    monkeypatch,
    field: str,
) -> None:
    commit = AsyncMock()
    monkeypatch.setattr("xarta.services.v1.intake.base.commit_intake", commit)
    request = SimpleNamespace(
        files={},
        json=generate_flow(**{field: 42}),
        app=SimpleNamespace(
            ctx=SimpleNamespace(intake_capabilities=GENERATE_CAPABILITIES)
        ),
    )

    with pytest.raises(BadRequestError, match=f"flow {field}"):
        await intake(request)

    commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_successful_route_returns_string_flow_id(monkeypatch) -> None:
    flow_id = uuid4()
    monkeypatch.setattr(
        "xarta.services.v1.intake.base.commit_intake",
        AsyncMock(return_value=flow_id),
    )
    request = SimpleNamespace(
        files={},
        json=generate_flow(),
        app=SimpleNamespace(
            ctx=SimpleNamespace(intake_capabilities=GENERATE_CAPABILITIES)
        ),
    )

    result = await intake(request)

    assert result.status == 202
    assert orjson.loads(result.body) == {"id": str(flow_id)}


def test_supplied_flow_generates_stable_canonical_identities() -> None:
    flow_id = uuid4()

    first = prepare_intake(
        generate_flow(id=str(flow_id)), capabilities=GENERATE_CAPABILITIES
    ).flow
    second = prepare_intake(
        generate_flow(id=str(flow_id)), capabilities=GENERATE_CAPABILITIES
    ).flow

    assert first.correlation_id == second.correlation_id
    assert first.dag.id == second.dag.id


@pytest.mark.asyncio
async def test_intake_publishes_root_directly_and_awaits_puback() -> None:
    prepared = prepare_intake(generate_flow(), capabilities=GENERATE_CAPABILITIES)
    puback = SimpleNamespace(stream="REQUESTS", seq=1)
    jetstream = SimpleNamespace(publish=AsyncMock(return_value=puback))
    IntakeNATSModel._runtime = NATSRuntime(
        client=cast("Any", SimpleNamespace(is_connected=True)),
        jetstream=cast("Any", jetstream),
    )

    await IntakeNATSModel.schedule(prepared.flow)

    call = jetstream.publish.await_args
    task = orjson.loads(call.kwargs["payload"])
    execution_id = call.kwargs["headers"]["Nats-Msg-Id"]
    assert call.kwargs["subject"] == (
        f"requests.{prepared.flow.id}.tasks.{execution_id}.generate"
    )
    assert call.kwargs["headers"] == {
        "flow-id": str(prepared.flow.id),
        "flow-correlation-id": str(prepared.flow.correlation_id),
        "Nats-Msg-Id": execution_id,
    }
    assert task["flow_id"] == str(prepared.flow.id)
    assert task["node_execution_id"] == execution_id
    IntakeNATSModel._runtime = None


@pytest.mark.asyncio
async def test_intake_registration_fails_when_nats_is_unavailable(monkeypatch) -> None:
    app = SimpleNamespace(ctx=SimpleNamespace(), register_listener=Mock())
    monkeypatch.setattr(
        IntakeNATSModel, "open", AsyncMock(side_effect=RuntimeError("offline"))
    )
    IntakeNATSModel._runtime = None

    with pytest.raises(RuntimeError, match="offline"):
        await IntakeNATSModel.register(cast("Any", app))

    assert app.ctx.nats_runtimes == {}
    assert not hasattr(app.ctx, "postgres_pools")
    app.register_listener.assert_not_called()


@pytest.mark.asyncio
async def test_intake_registration_does_not_register_postgresql(monkeypatch) -> None:
    jetstream = AsyncMock()
    client = SimpleNamespace(jetstream=lambda: jetstream)
    app = SimpleNamespace(ctx=SimpleNamespace(), register_listener=Mock())
    monkeypatch.setattr(IntakeNATSModel, "open", AsyncMock(return_value=client))
    IntakeNATSModel._runtime = None

    await IntakeNATSModel.register(cast("Any", app))

    assert not hasattr(app.ctx, "postgres_pools")
    assert next(iter(app.ctx.nats_runtimes.values())).jetstream is jetstream
    app.register_listener.assert_called_once()
    IntakeNATSModel._runtime = None


@pytest.mark.asyncio
async def test_publish_failure_is_service_unavailable(monkeypatch) -> None:
    prepared = prepare_intake(generate_flow(), capabilities=GENERATE_CAPABILITIES)
    monkeypatch.setattr(
        IntakeNATSModel,
        "schedule",
        AsyncMock(side_effect=RuntimeError("publish failed")),
    )

    with pytest.raises(ServiceUnavailable) as captured:
        await commit_intake(prepared)

    assert captured.value.status_code == 503


@pytest.mark.asyncio
async def test_supplied_flow_retry_republishes_identical_message() -> None:
    flow_id = uuid4()
    definition = generate_flow(id=str(flow_id))
    first = prepare_intake(definition, capabilities=GENERATE_CAPABILITIES)
    second = prepare_intake(definition, capabilities=GENERATE_CAPABILITIES)
    jetstream = SimpleNamespace(publish=AsyncMock())
    IntakeNATSModel._runtime = NATSRuntime(
        client=cast("Any", SimpleNamespace(is_connected=True)),
        jetstream=cast("Any", jetstream),
    )

    await IntakeNATSModel.schedule(first.flow)
    await IntakeNATSModel.schedule(second.flow)

    first_call, second_call = jetstream.publish.await_args_list
    assert first_call.kwargs == second_call.kwargs
    assert first_call.kwargs["headers"]["flow-id"] == str(flow_id)
    assert first_call.kwargs["headers"]["flow-correlation-id"] == str(
        first.flow.correlation_id
    )
    assert first_call.kwargs["headers"]["Nats-Msg-Id"] == str(
        uuid5(flow_id, f"root:{first.flow.dag.id}")
    )
    IntakeNATSModel._runtime = None
