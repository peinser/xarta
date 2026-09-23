from __future__ import annotations

import asyncio
import hashlib
import hmac

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import orjson
import pytest

from xarta.adapters import AdapterRegistry
from xarta.exceptions.protocol import TemporaryError
from xarta.protocol.dag import NodeTask
from xarta.protocol.dag.peppol import PeppolNode
from xarta.protocol.document.source import DocumentSourceResult
from xarta.services.v1.peppol.api import recommand_callback
from xarta.services.v1.peppol.inspection import PeppolDocumentInspector
from xarta.services.v1.peppol.models import PeppolAmbiguousError
from xarta.services.v1.peppol.models import PeppolDeliveryState
from xarta.services.v1.peppol.models import PeppolFailureCategory
from xarta.services.v1.peppol.models import PeppolParticipant
from xarta.services.v1.peppol.models import PeppolProviderDocument
from xarta.services.v1.peppol.models import PeppolProviderError
from xarta.services.v1.peppol.models import PeppolSenderNotConfiguredError
from xarta.services.v1.peppol.models import PeppolUpdate
from xarta.services.v1.peppol.nats import RecommandCallbackNATSModel
from xarta.services.v1.peppol.recommand import RecommandCompanyClient
from xarta.services.v1.peppol.recommand import RecommandConfiguration
from xarta.services.v1.peppol.recommand import RecommandPeppolAdapter
from xarta.services.v1.peppol.recommand import RecommandPeppolAdapterFactory
from xarta.services.v1.peppol.recommand.adapter import MAX_PROVIDER_RESPONSE_BYTES
from xarta.services.v1.peppol.reducer import reduce_peppol
from xarta.services.v1.peppol.service import PeppolService
from xarta.services.v1.peppol.service import build_peppol_components
from xarta.services.v1.peppol.webhooks import accept_recommand_webhook
from xarta.services.v1.peppol.webhooks import apply_recommand_callback_hint
from xarta.tracking import InMemoryTrackingStore
from xarta.tracking import TrackedOperationLifecycle

FIXTURES = Path(__file__).parents[1] / "fixtures" / "peppol"


def configuration(**overrides) -> dict:
    return {
        "base_url": "https://app.recommand.eu/api/v1",
        "api_key": "key_test",
        "api_secret": "api-secret",
        "webhook_secret": "webhook-secret",
        "companies": [
            {
                "company_id": "company-1",
                "peppol_ids": [{"scheme": "0208", "identifier": "0123456789"}],
            }
        ],
        "timeout": 10,
        **overrides,
    }


def destination_configuration(**overrides) -> dict:
    return {
        "adapter": "recommand-rest",
        "current_revision": "v1",
        "revisions": {"v1": configuration(**overrides)},
    }


def document() -> DocumentSourceResult:
    return DocumentSourceResult.parse(
        id=uuid4(),
        content_type="application/xml",
        data=(FIXTURES / "invoice-profile-01-valid.xml").read_bytes(),
    )


class Content:
    def __init__(self, body: bytes) -> None:
        self.body = body

    async def read(self, limit: int) -> bytes:
        return self.body


class Response:
    def __init__(self, status: int, body, *, delay: float = 0) -> None:
        self.status = status
        self.content = Content(body if isinstance(body, bytes) else orjson.dumps(body))
        self.delay = delay

    async def __aenter__(self):
        if self.delay:
            await asyncio.sleep(self.delay)
        return self

    async def __aexit__(self, *args):
        return None


class Session:
    def __init__(self, *responses: Response) -> None:
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


def adapter(session: Session, **overrides) -> RecommandPeppolAdapter:
    return RecommandPeppolAdapter(
        RecommandConfiguration.parse(configuration(**overrides)), session
    )


def test_recommand_configuration_and_sender_binding() -> None:
    configured = RecommandConfiguration.parse(configuration())
    bound = RecommandPeppolAdapter(configured, None).bind_sender(
        configured.companies[0].peppol_ids[0]
    )

    assert bound.provider_account_reference == "company-1"
    assert (
        RecommandPeppolAdapter(configured, None)
        .bind_account("company-1")
        .provider_account_reference
        == "company-1"
    )
    with pytest.raises(PeppolSenderNotConfiguredError):
        RecommandPeppolAdapter(configured, None).bind_sender(
            PeppolParticipant("0208", "unknown")
        )


def test_recommand_callback_ingress_is_exposed() -> None:
    template = (
        Path(__file__).parents[2]
        / "k8s/helm/charts/core/templates/ingresses/callback-peppol.yaml"
    ).read_text()
    assert "/api/v1/peppol/callbacks/recommand" in template


@pytest.mark.parametrize("name", ["api_key", "api_secret", "webhook_secret"])
def test_recommand_configuration_rejects_missing_credentials(name: str) -> None:
    with pytest.raises(ValueError, match=name):
        RecommandConfiguration.parse(configuration(**{name: ""}))


def test_recommand_configuration_rejects_unsafe_or_ambiguous_company_mappings() -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        RecommandConfiguration.parse(configuration(base_url="http://recommand.test"))
    duplicate_id = configuration()
    duplicate_id["companies"] *= 2
    with pytest.raises(ValueError, match="Duplicate Recommand company_id"):
        RecommandConfiguration.parse(duplicate_id)
    duplicate_sender = configuration()
    duplicate_sender["companies"].append(
        {
            "company_id": "company-2",
            "peppol_ids": [{"scheme": "0208", "identifier": "0123456789"}],
        }
    )
    with pytest.raises(ValueError, match="maps to both"):
        RecommandConfiguration.parse(duplicate_sender)


@pytest.mark.asyncio
async def test_account_identity_is_a_local_noop() -> None:
    session = Session()
    client = adapter(session).bind_account("company-1")

    await client.ensure_account_identity()

    assert not session.calls


@pytest.mark.asyncio
async def test_recipient_verification_uses_document_and_process_identifiers() -> None:
    session = Session(Response(200, {"success": True, "isValid": True}))
    client = adapter(session).bind_account("company-1")
    descriptor = PeppolDocumentInspector().inspect(document())

    lookup = await client.lookup_participant(descriptor.receiver, descriptor)

    assert lookup.registered is True
    method, url, request = session.calls[0]
    assert method == "POST"
    assert url.endswith("/verify-document-support")
    assert request["json"] == {
        "peppolAddress": "0088:1234567890123",
        "documentType": (
            "urn:oasis:names:specification:ubl:schema:xsd:Invoice-2::Invoice##"
            "urn:cen.eu:en16931:2017#compliant#"
            "urn:fdc:peppol.eu:2017:poacc:billing:3.0::2.1"
        ),
        "processId": "urn:fdc:peppol.eu:2017:poacc:billing:01:1.0",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"success": False, "isValid": False},
        {"success": True},
        {"success": True, "isValid": "false"},
    ],
)
async def test_recipient_verification_rejects_malformed_success(payload) -> None:
    client = adapter(Session(Response(200, payload))).bind_account("company-1")
    descriptor = PeppolDocumentInspector().inspect(document())

    with pytest.raises(PeppolProviderError) as error:
        await client.lookup_participant(descriptor.receiver, descriptor)

    assert error.value.failure.stage.value == "participant_lookup"
    assert error.value.failure.category is PeppolFailureCategory.PROVIDER_ERROR


@pytest.mark.asyncio
async def test_raw_xml_send_shape_and_document_mapping() -> None:
    session = Session(
        Response(
            200,
            {
                "success": True,
                "companyId": "company-1",
                "id": "doc-1",
                "sentOverPeppol": True,
            },
        )
    )
    client = adapter(session).bind_account("company-1")
    value = document()
    descriptor = PeppolDocumentInspector().inspect(value)

    result = await client.submit_document(value.data, descriptor)

    assert result.id == "doc-1"
    assert result.delivery_state is PeppolDeliveryState.DELIVERY_CONFIRMED
    payload = session.calls[0][2]["json"]
    assert payload["documentType"] == "xml"
    assert payload["document"].encode() == value.data
    assert payload["recipient"] == "0088:1234567890123"
    assert payload["doctypeId"].endswith("billing:3.0::2.1")
    assert payload["processId"] == descriptor.profile_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {
            "success": False,
            "companyId": "company-1",
            "id": "doc-1",
            "sentOverPeppol": True,
        },
        {"success": True, "companyId": "company-1", "id": "", "sentOverPeppol": True},
        {
            "success": True,
            "companyId": "company-2",
            "id": "doc-1",
            "sentOverPeppol": True,
        },
        {"success": True, "companyId": "company-1", "id": "doc-1"},
        {
            "success": True,
            "companyId": "company-1",
            "id": "doc-1",
            "sentOverPeppol": "true",
        },
    ],
)
async def test_send_rejects_unusable_success_evidence_as_ambiguous(payload) -> None:
    client = adapter(Session(Response(200, payload))).bind_account("company-1")
    value = document()

    with pytest.raises(PeppolAmbiguousError) as error:
        await client.submit_document(
            value.data, PeppolDocumentInspector().inspect(value)
        )

    assert error.value.failure.stage.value == "submission"
    assert error.value.failure.category is PeppolFailureCategory.AMBIGUOUS_RESULT


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 422])
async def test_send_validation_failures_are_invalid_and_do_not_expose_details(
    status: int,
) -> None:
    descriptor = PeppolDocumentInspector().inspect(document())
    client = adapter(
        Session(
            Response(
                status,
                {
                    "success": False,
                    "errors": {"document": ["secret customer invoice detail"]},
                },
            )
        )
    )

    with pytest.raises(PeppolProviderError) as error:
        await client.bind_account("company-1").submit_document(
            document().data, descriptor
        )

    assert error.value.failure.category is PeppolFailureCategory.INVALID_DOCUMENT
    assert "api-secret" not in str(error.value)
    assert "secret customer invoice detail" not in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 422])
async def test_send_rejects_malformed_validation_envelope_generically(
    status: int,
) -> None:
    client = adapter(Session(Response(status, {"success": False})))
    value = document()

    with pytest.raises(PeppolProviderError) as error:
        await client.bind_account("company-1").submit_document(
            value.data, PeppolDocumentInspector().inspect(value)
        )

    assert error.value.failure.category is PeppolFailureCategory.PROVIDER_REJECTED


@pytest.mark.asyncio
async def test_http_response_size_is_bounded() -> None:
    descriptor = PeppolDocumentInspector().inspect(document())

    oversized = adapter(
        Session(Response(200, b"x" * (MAX_PROVIDER_RESPONSE_BYTES + 1)))
    )
    with pytest.raises(PeppolAmbiguousError):
        await oversized.bind_account("company-1").submit_document(
            document().data, descriptor
        )


@pytest.mark.asyncio
async def test_direct_send_timeout_is_ambiguous_and_honors_configured_timeout() -> None:
    session = Session(Response(200, {}, delay=0.02))
    client = adapter(session, timeout=0.001).bind_account("company-1")
    value = document()

    with pytest.raises(PeppolAmbiguousError):
        await client.submit_document(
            value.data, PeppolDocumentInspector().inspect(value)
        )


def service(session: Session) -> tuple[PeppolService, InMemoryTrackingStore]:
    components = build_peppol_components(
        destination_configuration(),
        AdapterRegistry({"recommand-rest": RecommandPeppolAdapterFactory(session)}),
    )
    store = InMemoryTrackingStore()
    return PeppolService(store, components), store


@pytest.mark.asyncio
async def test_direct_submission_pins_document_and_does_not_emulate_a_draft() -> None:
    session = Session(
        Response(200, {"success": True, "isValid": True}),
        Response(
            200,
            {
                "success": True,
                "companyId": "company-1",
                "id": "doc-1",
                "sentOverPeppol": True,
            },
        ),
    )
    peppol, store = service(session)
    node = PeppolNode(document={"source": "archive", "id": str(uuid4())})
    task = NodeTask(uuid4(), node)

    result = await peppol.submit(task, node, document())
    operation = await store.operation_for_execution(task.node_execution_id)

    assert result.outcomes_persisted is True
    assert operation.provider_account_reference == "company-1"
    assert operation.provider_reference == "doc-1"
    assert operation.lifecycle is TrackedOperationLifecycle.RESOLVED
    assert [(call[0], call[1].rsplit("/", 1)[-1]) for call in session.calls] == [
        ("POST", "verify-document-support"),
        ("POST", "send"),
    ]
    assert operation.state["provider_delivery_state"] == "delivery_confirmed"
    assert list(store.outcomes.values())[-1].outcome == "delivery_confirmed"
    assert not await store.claim_peppol_reconciliations(1, 60)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("responses", "outcome"),
    [
        ((Response(401, {}),), "execution_failed"),
        (
            (Response(200, {"success": True, "isValid": False}),),
            "recipient_not_registered",
        ),
    ],
)
async def test_recipient_failures_map_to_existing_outcomes(responses, outcome) -> None:
    peppol, _ = service(Session(*responses))
    node = PeppolNode(document={"source": "archive", "id": str(uuid4())})

    result = await peppol.submit(NodeTask(uuid4(), node), node, document())

    assert result.outcomes[0].outcome == outcome
    assert result.outcomes[0].details["provider"] == "recommand"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 422])
async def test_send_validation_failures_emit_invalid_document(status: int) -> None:
    session = Session(
        Response(200, {"success": True, "isValid": True}),
        Response(
            status,
            {
                "success": False,
                "errors": {"document": ["secret customer invoice detail"]},
            },
        ),
    )
    peppol, store = service(session)
    node = PeppolNode(document={"source": "archive", "id": str(uuid4())})

    result = await peppol.submit(NodeTask(uuid4(), node), node, document())

    outcome = list(store.outcomes.values())[-1]
    assert result.outcomes_persisted is True
    assert outcome.outcome == "invalid_document"
    assert (
        "secret customer invoice detail" not in orjson.dumps(outcome.details).decode()
    )


@pytest.mark.asyncio
async def test_malformed_recipient_response_is_not_a_registration_negative() -> None:
    peppol, _ = service(Session(Response(200, {"success": True})))
    node = PeppolNode(document={"source": "archive", "id": str(uuid4())})

    with pytest.raises(TemporaryError, match="provider read failure"):
        await peppol.submit(NodeTask(uuid4(), node), node, document())


@pytest.mark.asyncio
async def test_ambiguous_direct_send_resolves_uncertain_without_retry() -> None:
    session = Session(
        Response(200, {"success": True, "isValid": True}),
        Response(503, {}),
    )
    peppol, store = service(session)
    node = PeppolNode(document={"source": "archive", "id": str(uuid4())})
    task = NodeTask(uuid4(), node)

    await peppol.submit(task, node, document())

    operation = await store.operation_for_execution(task.node_execution_id)
    assert operation.lifecycle is TrackedOperationLifecycle.RESOLVED
    assert list(store.outcomes.values())[-1].outcome == "outcome_uncertain"
    assert sum(call[1].endswith("/send") for call in session.calls) == 1

    await peppol.submit(task, node, document())
    assert sum(call[1].endswith("/send") for call in session.calls) == 1


@pytest.mark.asyncio
async def test_malformed_direct_send_response_resolves_uncertain_without_retry() -> (
    None
):
    session = Session(
        Response(200, {"success": True, "isValid": True}),
        Response(
            200,
            {"success": True, "companyId": "company-1", "id": "doc-1"},
        ),
    )
    peppol, store = service(session)
    node = PeppolNode(document={"source": "archive", "id": str(uuid4())})
    task = NodeTask(uuid4(), node)

    await peppol.submit(task, node, document())

    operation = await store.operation_for_execution(task.node_execution_id)
    assert operation.lifecycle is TrackedOperationLifecycle.RESOLVED
    assert operation.provider_reference is None
    assert list(store.outcomes.values())[-1].outcome == "outcome_uncertain"

    await peppol.submit(task, node, document())
    assert sum(call[1].endswith("/send") for call in session.calls) == 1


@pytest.mark.asyncio
async def test_submitting_phase_redelivery_does_not_repeat_send(monkeypatch) -> None:
    session = Session(Response(200, {"success": True, "isValid": True}))
    peppol, store = service(session)
    node = PeppolNode(document={"source": "archive", "id": str(uuid4())})
    task = NodeTask(uuid4(), node)
    value = document()

    async def process_loss(*args, **kwargs):
        raise RuntimeError("simulated process loss")

    monkeypatch.setattr(RecommandCompanyClient, "submit_document", process_loss)
    with pytest.raises(RuntimeError, match="process loss"):
        await peppol.submit(task, node, value)

    operation = await store.operation_for_execution(task.node_execution_id)
    assert operation.state["phase"] == "submitting"
    assert operation.provider_reference is None

    await peppol.submit(task, node, value)

    operation = await store.get_operation(operation.id)
    assert operation.lifecycle is TrackedOperationLifecycle.RESOLVED
    assert list(store.outcomes.values())[-1].outcome == "outcome_uncertain"
    assert len(session.calls) == 1


@pytest.mark.asyncio
async def test_direct_response_checkpoint_replays_without_provider_io(
    monkeypatch,
) -> None:
    session = Session(
        Response(200, {"success": True, "isValid": True}),
        Response(
            200,
            {
                "success": True,
                "companyId": "company-1",
                "id": "doc-1",
                "sentOverPeppol": True,
            },
        ),
    )
    peppol, store = service(session)
    node = PeppolNode(document={"source": "archive", "id": str(uuid4())})
    task = NodeTask(uuid4(), node)
    value = document()
    feedback = peppol.tracking.feedback

    async def crash(*args, **kwargs):
        raise RuntimeError("simulated process loss")

    monkeypatch.setattr(peppol.tracking, "feedback", crash)
    with pytest.raises(RuntimeError, match="process loss"):
        await peppol.submit(task, node, value)

    operation = await store.operation_for_execution(task.node_execution_id)
    assert operation.provider_reference == "doc-1"
    assert operation.state["phase"] == "provider_responded"
    assert operation.state["provider_delivery_state"] == "delivery_confirmed"
    assert not store.outcomes

    monkeypatch.setattr(peppol.tracking, "feedback", feedback)
    await peppol.submit(task, node, value)

    operation = await store.get_operation(operation.id)
    assert operation.lifecycle is TrackedOperationLifecycle.RESOLVED
    assert [event.outcome for event in store.outcomes.values()] == [
        "delivery_confirmed"
    ]
    assert len(session.calls) == 2


@pytest.mark.asyncio
async def test_reconciler_applies_checkpointed_direct_observation_locally() -> None:
    session = Session()
    peppol, store = service(session)
    node = PeppolNode(document={"source": "archive", "id": str(uuid4())})
    task = NodeTask(uuid4(), node)
    await store.accept(task)
    operation = await store.create_operation(
        task.node_execution_id,
        "peppol",
        peppol.components.current().binding,
        {
            "sender": {"scheme": "0208", "identifier": "0123456789"},
            "phase": "provider_responded",
            "provider_document_id": "doc-1",
            "provider_state": "sent_over_peppol",
            "provider_delivery_state": "delivery_confirmed",
        },
        "company-1",
    )
    operation = await store.checkpoint_operation(
        operation.id, operation.version, operation.state, "doc-1"
    )

    await peppol.reconcile(operation.id)

    operation = await store.get_operation(operation.id)
    assert operation.lifecycle is TrackedOperationLifecycle.RESOLVED
    assert list(store.outcomes.values())[-1].outcome == "delivery_confirmed"
    assert not session.calls


@pytest.mark.asyncio
async def test_definitive_non_peppol_send_is_terminal_failure() -> None:
    session = Session(
        Response(200, {"success": True, "isValid": True}),
        Response(
            200,
            {
                "success": True,
                "companyId": "company-1",
                "id": "doc-1",
                "sentOverPeppol": False,
            },
        ),
    )
    peppol, store = service(session)
    node = PeppolNode(document={"source": "archive", "id": str(uuid4())})
    task = NodeTask(uuid4(), node)

    await peppol.submit(task, node, document())

    operation = await store.operation_for_execution(task.node_execution_id)
    assert operation.lifecycle is TrackedOperationLifecycle.RESOLVED
    assert operation.provider_reference == "doc-1"
    assert operation.state["provider_delivery_state"] == "unknown"
    assert list(store.outcomes.values())[-1].outcome == "execution_failed"
    assert not await store.claim_peppol_reconciliations(1, 60)


@pytest.mark.parametrize(
    ("provider", "state", "outcome", "resolved"),
    [
        ("e-invoice.be", PeppolDeliveryState.SUBMITTED, "submitted", False),
        (
            "e-invoice.be",
            PeppolDeliveryState.DELIVERY_CONFIRMED,
            "delivery_confirmed",
            True,
        ),
        (
            "recommand",
            PeppolDeliveryState.DELIVERY_CONFIRMED,
            "delivery_confirmed",
            True,
        ),
        (
            "recommand",
            PeppolDeliveryState.DELIVERY_FAILED,
            "delivery_failed",
            True,
        ),
        ("recommand", PeppolDeliveryState.UNKNOWN, None, False),
    ],
)
def test_reducer_uses_provider_neutral_observations(
    provider, state, outcome, resolved
) -> None:
    reduction = reduce_peppol(
        {},
        PeppolUpdate(PeppolProviderDocument(provider, "doc-1", state.value, state)),
    )

    assert [event.outcome for event in reduction.outcomes] == (
        [outcome] if outcome else []
    )
    assert reduction.operation_resolved is resolved
    duplicate = reduce_peppol(
        reduction.state,
        PeppolUpdate(PeppolProviderDocument(provider, "doc-1", state.value, state)),
    )
    assert not duplicate.outcomes


def signed_webhook(event: dict, event_id: str = "event-1") -> tuple[bytes, dict]:
    raw = orjson.dumps(event)
    signature = hmac.new(b"webhook-secret", raw, hashlib.sha256).hexdigest()
    return raw, {
        "X-Signature": f"sha256={signature}",
        "X-Idempotency-Key": event_id,
    }


@pytest.mark.asyncio
async def test_authenticated_webhook_is_handed_off_without_provider_read() -> None:
    session = Session()
    peppol, store = service(session)
    node = PeppolNode(document={"source": "archive", "id": str(uuid4())})
    task = NodeTask(uuid4(), node)
    await store.accept(task)
    binding = peppol.components.current().binding
    operation = await store.create_operation(
        task.node_execution_id,
        "peppol",
        binding,
        {"sender": {"scheme": "0208", "identifier": "0123456789"}},
        "company-1",
    )
    await store.checkpoint_operation(
        operation.id, operation.version, operation.state, "doc-1"
    )
    raw, headers = signed_webhook(
        {
            "documentId": "doc-1",
            "teamId": "team-1",
            "companyId": "company-1",
        }
    )
    publish = AsyncMock()

    queued = await accept_recommand_webhook(
        raw,
        headers,
        store=store,
        components=peppol.components,
        publish=publish,
    )

    assert queued is True
    publish.assert_awaited_once_with(operation.id, "event-1", "company-1", "doc-1")
    assert not session.calls
    with pytest.raises(PermissionError):
        await accept_recommand_webhook(
            raw + b" ",
            headers,
            store=store,
            components=peppol.components,
            publish=publish,
        )


@pytest.mark.asyncio
async def test_recommand_callback_publication_contains_only_safe_routing_hint(
    monkeypatch,
) -> None:
    jetstream = SimpleNamespace(publish=AsyncMock())
    monkeypatch.setattr(
        RecommandCallbackNATSModel,
        "jetstream",
        classmethod(lambda cls: jetstream),
    )
    operation_id = uuid4()

    await RecommandCallbackNATSModel.publish(
        operation_id, "event-1", "company-1", "doc-1"
    )

    subject, body = jetstream.publish.await_args.args
    assert subject == "jobs.recommand-peppol-callback.hint"
    assert orjson.loads(body) == {
        "operation_id": str(operation_id),
        "event_id": "event-1",
        "company_id": "company-1",
        "provider_document_id": "doc-1",
    }
    assert jetstream.publish.await_args.kwargs["headers"]["Nats-Msg-Id"] == (
        "recommand:event-1"
    )


@pytest.mark.asyncio
async def test_webhook_consumer_applies_local_observation_and_deduplicates() -> None:
    session = Session()
    peppol, store = service(session)
    node = PeppolNode(document={"source": "archive", "id": str(uuid4())})
    task = NodeTask(uuid4(), node)
    await store.accept(task)
    operation = await store.create_operation(
        task.node_execution_id,
        "peppol",
        peppol.components.current().binding,
        {"sender": {"scheme": "0208", "identifier": "0123456789"}},
        "company-1",
    )
    await store.checkpoint_operation(
        operation.id, operation.version, operation.state, "doc-1"
    )

    first = await apply_recommand_callback_hint(
        operation.id,
        "event-1",
        "company-1",
        "doc-1",
        store=store,
        components=peppol.components,
    )
    duplicate = await apply_recommand_callback_hint(
        operation.id,
        "event-1",
        "company-1",
        "doc-1",
        store=store,
        components=peppol.components,
    )

    assert first.duplicate is False
    assert duplicate.duplicate is True
    assert len(store.outcomes) == 1
    assert not session.calls


@pytest.mark.asyncio
async def test_authenticated_irrelevant_callback_is_acknowledged_without_handoff() -> (
    None
):
    peppol, store = service(Session())
    raw, headers = signed_webhook(
        {
            "documentId": "doc-inbound",
            "teamId": "team-1",
            "companyId": "company-1",
        }
    )
    publish = AsyncMock()

    queued = await accept_recommand_webhook(
        raw,
        headers,
        store=store,
        components=peppol.components,
        publish=publish,
    )

    assert queued is False
    publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_authenticated_irrelevant_callback_returns_http_200() -> None:
    peppol, store = service(Session())
    raw, headers = signed_webhook({"documentId": "not-owned", "companyId": "company-1"})
    request = SimpleNamespace(
        body=raw,
        headers=headers,
        app=SimpleNamespace(
            ctx=SimpleNamespace(tracking=store, peppol_components=peppol.components)
        ),
    )

    result = await recommand_callback(request)

    assert result.status == 200
    assert orjson.loads(result.body) == {"accepted": True, "queued": False}


@pytest.mark.asyncio
async def test_webhook_rejects_invalid_requests_and_accepts_unknown_document() -> None:
    peppol, store = service(Session())
    publish = AsyncMock()
    with pytest.raises(ValueError, match="size limit"):
        await accept_recommand_webhook(
            b"x" * (64 * 1024 + 1),
            {},
            store=store,
            components=peppol.components,
            publish=publish,
        )

    unknown_company, headers = signed_webhook(
        {
            "documentId": "doc-1",
            "teamId": "team-1",
            "companyId": "unknown",
        }
    )
    with pytest.raises(PermissionError):
        await accept_recommand_webhook(
            unknown_company,
            headers,
            store=store,
            components=peppol.components,
            publish=publish,
        )

    unknown_document, headers = signed_webhook(
        {
            "documentId": "unknown",
            "teamId": "team-1",
            "companyId": "company-1",
        }
    )
    assert (
        await accept_recommand_webhook(
            unknown_document,
            headers,
            store=store,
            components=peppol.components,
            publish=publish,
        )
        is False
    )

    missing_document, headers = signed_webhook({"companyId": "company-1"})
    with pytest.raises(ValueError, match="documentId"):
        await accept_recommand_webhook(
            missing_document,
            headers,
            store=store,
            components=peppol.components,
            publish=publish,
        )


@pytest.mark.asyncio
async def test_callback_after_synchronous_resolution_emits_no_second_outcome() -> None:
    session = Session(
        Response(200, {"success": True, "isValid": True}),
        Response(
            200,
            {
                "success": True,
                "companyId": "company-1",
                "id": "doc-1",
                "sentOverPeppol": True,
            },
        ),
    )
    peppol, store = service(session)
    node = PeppolNode(document={"source": "archive", "id": str(uuid4())})
    task = NodeTask(uuid4(), node)
    await peppol.submit(task, node, document())
    operation = await store.operation_for_execution(task.node_execution_id)

    applied = await apply_recommand_callback_hint(
        operation.id,
        "event-after-resolution",
        "company-1",
        "doc-1",
        store=store,
        components=peppol.components,
    )

    assert applied.duplicate is True
    assert len(store.outcomes) == 1
    assert not any(call[1].endswith("/documents/doc-1") for call in session.calls)
