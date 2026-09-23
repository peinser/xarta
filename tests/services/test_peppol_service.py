from __future__ import annotations

import hashlib
import hmac
import json

from pathlib import Path
from uuid import uuid4

import orjson
import pytest

from xarta.adapters import AdapterRegistry
from xarta.execution import ExecutionMode
from xarta.protocol.dag import MAX_OUTCOME_DETAIL_MESSAGE_LENGTH
from xarta.protocol.dag import NodeTask
from xarta.protocol.dag.peppol import PeppolNode
from xarta.protocol.document.source import DocumentSourceResult
from xarta.services.v1.peppol.details import validation_details
from xarta.services.v1.peppol.e_invoice_be import EInvoiceBeDocumentState
from xarta.services.v1.peppol.inspection import PeppolDocumentError
from xarta.services.v1.peppol.inspection import PeppolDocumentInspector
from xarta.services.v1.peppol.models import PeppolAmbiguousError
from xarta.services.v1.peppol.models import PeppolDeliveryState
from xarta.services.v1.peppol.models import PeppolFailure
from xarta.services.v1.peppol.models import PeppolFailureCategory
from xarta.services.v1.peppol.models import PeppolFailureStage
from xarta.services.v1.peppol.models import PeppolParticipantLookup
from xarta.services.v1.peppol.models import PeppolProviderDocument
from xarta.services.v1.peppol.models import PeppolSenderNotConfiguredError
from xarta.services.v1.peppol.models import PeppolSubmissionMode
from xarta.services.v1.peppol.models import PeppolUpdate
from xarta.services.v1.peppol.models import PeppolValidationIssue
from xarta.services.v1.peppol.models import PeppolValidationResult
from xarta.services.v1.peppol.reducer import reduce_peppol
from xarta.services.v1.peppol.service import PeppolService
from xarta.services.v1.peppol.service import build_peppol_components
from xarta.tracking import InMemoryTrackingStore
from xarta.tracking import NodeExecutionState
from xarta.tracking import TrackedOperationLifecycle

FIXTURES = Path(__file__).parents[1] / "fixtures" / "peppol"


def document(name: str = "invoice-profile-01-valid.xml") -> DocumentSourceResult:
    return DocumentSourceResult.parse(
        id=uuid4(),
        content_type="application/xml",
        data=(FIXTURES / name).read_bytes(),
    )


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("invoice-profile-01-valid.xml", "invoice"),
        ("credit-note-profile-01-valid.xml", "credit_note"),
    ],
)
def test_inspector_extracts_authoritative_ubl_identity(name: str, kind: str) -> None:
    descriptor = PeppolDocumentInspector().inspect(document(name))

    assert descriptor.document_kind == kind
    assert descriptor.sender.scheme == "0208"
    assert descriptor.sender.identifier == "0123456789"
    assert descriptor.receiver.scheme == "0088"
    assert descriptor.receiver.identifier == "1234567890123"


@pytest.mark.parametrize(
    "payload",
    [b"<!DOCTYPE Invoice><Invoice/>", b"<Invoice>", b""],
)
def test_inspector_rejects_unsafe_or_malformed_xml(payload: bytes) -> None:
    value = DocumentSourceResult.parse(
        id=uuid4(), content_type="application/xml", data=payload
    )
    with pytest.raises(PeppolDocumentError):
        PeppolDocumentInspector().inspect(value)


def test_validation_details_are_bounded_and_report_truncation() -> None:
    issues = tuple(
        PeppolValidationIssue(f"RULE-{index}", "error", "x" * 5000)
        for index in range(37)
    )
    details = validation_details(issues)

    assert details["total_issue_count"] == 37
    assert 0 < details["recorded_issue_count"] <= 10
    assert details["truncated"] is True
    assert len(details["issues"][0]["message"]) == MAX_OUTCOME_DETAIL_MESSAGE_LENGTH


class FakeAdapter:
    execution_mode = ExecutionMode.TRACKED
    submission_mode = PeppolSubmissionMode.STAGED
    provider = "e-invoice.be"

    def __init__(self) -> None:
        self.created = 0
        self.sent = []
        self.state = EInvoiceBeDocumentState.DRAFT

    @property
    def provider_account_reference(self):
        return "ten_123"

    @property
    def account_references(self):
        return ("ten_123",)

    def bind_sender(self, sender):
        if (sender.scheme, sender.identifier) != ("0208", "0123456789"):
            raise PeppolSenderNotConfiguredError(sender)
        return self

    def bind_account(self, account_reference):
        if account_reference != self.provider_account_reference:
            raise KeyError(account_reference)
        return self

    async def ensure_account_identity(self):
        return None

    async def validate_ubl(self, content):
        return PeppolValidationResult(True)

    async def lookup_participant(self, participant, descriptor):
        return PeppolParticipantLookup(True)

    async def create_document(self, content):
        self.created += 1
        self.state = EInvoiceBeDocumentState.DRAFT
        return provider_document("doc_123", self.state)

    async def send_document(self, provider_document_id, sender, receiver):
        self.sent.append((provider_document_id, sender, receiver))
        self.state = EInvoiceBeDocumentState.TRANSIT
        return provider_document(provider_document_id, self.state)

    async def get_document(self, provider_document_id):
        return provider_document(provider_document_id, self.state)

    def parse_webhook(self, raw_body, headers):
        raise NotImplementedError


class FakeFactory:
    def __init__(self, adapter) -> None:
        self.adapter = adapter

    def validate(self, configuration) -> None:
        pass

    def create(self, configuration):
        return self.adapter


def configuration():
    return {
        "adapter": "e-invoice-be-rest",
        "current_revision": "v1",
        "revisions": {
            "v1": {
                "base_url": "https://api.e-invoice.be",
                "tenants": [
                    {
                        "tenant_id": "ten_123",
                        "api_key": "secret",
                        "webhook_secret": "callback-secret",
                        "peppol_ids": [{"scheme": "0208", "identifier": "0123456789"}],
                    }
                ],
            }
        },
    }


def provider_document(identifier, state, code=None, message=None):
    semantic = {
        EInvoiceBeDocumentState.DRAFT: PeppolDeliveryState.STAGED,
        EInvoiceBeDocumentState.TRANSIT: PeppolDeliveryState.SUBMITTED,
        EInvoiceBeDocumentState.SENT: PeppolDeliveryState.DELIVERY_CONFIRMED,
        EInvoiceBeDocumentState.FAILED: PeppolDeliveryState.DELIVERY_FAILED,
        EInvoiceBeDocumentState.UNKNOWN: PeppolDeliveryState.UNKNOWN,
    }[state]
    return PeppolProviderDocument(
        "e-invoice.be", identifier, state.value.upper(), semantic, code, message
    )


def test_peppol_pricing_binding_uses_deployed_configuration_not_destination() -> None:
    components = build_peppol_components(
        configuration(),
        AdapterRegistry({"e-invoice-be-rest": FakeFactory(FakeAdapter())}),
    )

    binding = components.current_pricing_binding()

    assert binding.capability == "peppol"
    assert binding.destination is None
    assert binding.adapter == "e-invoice-be-rest"
    assert binding.adapter_configuration_revision == "v1"


@pytest.mark.asyncio
async def test_happy_path_checkpoints_draft_and_passes_explicit_routing() -> None:
    adapter = FakeAdapter()
    components = build_peppol_components(
        configuration(), AdapterRegistry({"e-invoice-be-rest": FakeFactory(adapter)})
    )
    store = InMemoryTrackingStore()
    node = PeppolNode(
        document={"source": "archive", "id": str(uuid4()), "version": str(uuid4())},
    )
    task = NodeTask(uuid4(), node)
    result = await PeppolService(store, components).submit(task, node, document())
    operation = await store.operation_for_execution(task.node_execution_id)

    assert result.outcomes_persisted is True
    assert operation is not None
    assert operation.provider_reference == "doc_123"
    assert operation.state["sha256"] == hashlib.sha256(document().data).hexdigest()
    assert operation.lifecycle is TrackedOperationLifecycle.OPEN
    assert (
        store.node_executions[task.node_execution_id].state
        is NodeExecutionState.WAITING_FEEDBACK
    )
    assert adapter.created == 1
    assert adapter.sent[0][1].scheme == "0208"
    assert adapter.sent[0][1].identifier == "0123456789"
    assert adapter.sent[0][2].scheme == "0088"
    assert adapter.sent[0][2].identifier == "1234567890123"
    submitted = list(store.outcomes.values())[-1]
    assert submitted.outcome == "submitted"
    assert submitted.details["provider_state"] == "TRANSIT"
    persisted_history = await store.outcomes_for_operation(operation.id)
    assert persisted_history == (submitted,)
    assert persisted_history[0].details["provider_document_id"] == "doc_123"


@pytest.mark.asyncio
async def test_sender_mismatch_and_profile_02_never_create_provider_document() -> None:
    adapter = FakeAdapter()
    components = build_peppol_components(
        configuration(), AdapterRegistry({"e-invoice-be-rest": FakeFactory(adapter)})
    )
    node = PeppolNode(
        document={"source": "archive", "id": str(uuid4())},
    )
    profile_02 = document()
    profile_02.data = profile_02.data.replace(b"billing:01:1.0", b"billing:02:1.0")
    result = await PeppolService(InMemoryTrackingStore(), components).submit(
        NodeTask(uuid4(), node), node, profile_02
    )
    assert result.outcomes[0].outcome == "unsupported_profile"

    mismatch = document()
    mismatch.data = mismatch.data.replace(b"0123456789", b"9999999999", 1)
    result = await PeppolService(InMemoryTrackingStore(), components).submit(
        NodeTask(uuid4(), node), node, mismatch
    )
    assert result.outcomes[0].outcome == "execution_failed"
    assert result.outcomes[0].details == {
        "provider": "e-invoice.be",
        "stage": "validation",
        "category": "configuration_error",
        "message": "No provider account is configured for the UBL sender",
        "sender": "0208:9999999999",
    }
    assert adapter.created == 0


def test_webhook_hmac_uses_provider_canonical_json() -> None:
    from xarta.services.v1.peppol.e_invoice_be import EInvoiceBeConfiguration
    from xarta.services.v1.peppol.e_invoice_be import EInvoiceBePeppolAdapter

    event = {
        "id": "evt_1",
        "tenant_id": "ten_123",
        "created_at": 1762780249,
        "type": "document.sent",
        "data": {"document_id": "doc_123"},
    }
    signature = (
        "sha256="
        + hmac.new(
            b"callback-secret",
            json.dumps(event, sort_keys=True).encode(),
            hashlib.sha256,
        ).hexdigest()
    )
    configured = EInvoiceBeConfiguration.parse(configuration()["revisions"]["v1"])
    instance = EInvoiceBePeppolAdapter(configured, None)

    authenticated = instance.authenticate_webhook(event, signature)
    assert authenticated.event_id == "evt_1"
    reordered = json.loads(json.dumps(event, indent=2))
    assert instance.authenticate_webhook(reordered, signature).event_id == "evt_1"
    changed = {**event, "type": "document.sent.failed"}
    with pytest.raises(PermissionError):
        instance.authenticate_webhook(changed, signature)


def test_official_provider_webhook_signature_fixture() -> None:
    from xarta.services.v1.peppol.e_invoice_be import EInvoiceBeConfiguration
    from xarta.services.v1.peppol.e_invoice_be import EInvoiceBePeppolAdapter

    event = {
        "id": "evt-e7wyc7gtpqx4z73x2wqmwhebhbb3r8n3ovfhcsdbulxr3s2awf49de76yglrnri3",
        "tenant_id": "ten-abc123",
        "created_at": 1762780249,
        "type": "document.sent",
        "data": {"document_id": "doc-1"},
        "text": '⚡️ New webhook event: document.sent\n\n{\n  "document_id": "doc-1"\n}',
    }
    configured = EInvoiceBeConfiguration.parse(
        {
            "base_url": "https://api.e-invoice.be",
            "tenants": [
                {
                    "tenant_id": "ten-abc123",
                    "api_key": "unused",
                    "webhook_secret": "secret",
                    "peppol_ids": [{"scheme": "0208", "identifier": "0123456789"}],
                }
            ],
        }
    )
    adapter = EInvoiceBePeppolAdapter(configured, None)

    webhook = adapter.authenticate_webhook(
        event,
        "sha256=2f8ec8fab5adedd8f82a2b4064f559c40b14aed0c6da27ed394e51176b10bd1b",
    )

    assert webhook.tenant_id == "ten-abc123"


def test_malformed_webhook_json_is_rejected_before_authentication() -> None:
    from xarta.services.v1.peppol import e_invoice_be

    with pytest.raises(ValueError, match="valid JSON"):
        e_invoice_be.parse_untrusted_e_invoice_be_webhook(b"{")


def test_provider_failed_reduction_retains_native_failure_evidence() -> None:
    reduction = reduce_peppol(
        {"phase": "waiting_feedback"},
        PeppolUpdate(
            provider_document(
                "doc_123",
                EInvoiceBeDocumentState.FAILED,
                "SOME_CODE",
                "Destination could not be reached\x00",
            )
        ),
    )

    assert reduction.operation_resolved is True
    assert reduction.outcomes[0].outcome == "delivery_failed"
    assert reduction.outcomes[0].details["provider"] == "e-invoice.be"
    assert reduction.outcomes[0].details["provider_state"] == "FAILED"
    assert reduction.outcomes[0].details["provider_code"] == "SOME_CODE"
    assert (
        reduction.outcomes[0].details["message"] == "Destination could not be reached"
    )


@pytest.mark.asyncio
async def test_recovered_provider_created_phase_reuses_draft_without_recreating() -> (
    None
):
    adapter = FakeAdapter()
    components = build_peppol_components(
        configuration(), AdapterRegistry({"e-invoice-be-rest": FakeFactory(adapter)})
    )
    store = InMemoryTrackingStore()
    node = PeppolNode(
        document={"source": "archive", "id": str(uuid4())},
    )
    task = NodeTask(uuid4(), node)
    value = document()
    descriptor = PeppolDocumentInspector().inspect(value)
    await store.accept(task)
    resolved = components.current()
    operation = await store.create_operation(
        task.node_execution_id,
        "peppol",
        resolved.binding,
        {
            "phase": "provider_created",
            "source_document_id": str(value.id),
            "source_document_version": None,
            "sha256": hashlib.sha256(value.data).hexdigest(),
            "business_document_id": descriptor.business_document_id,
            "issue_date": descriptor.issue_date.isoformat(),
            "customization_id": descriptor.customization_id,
            "profile_id": descriptor.profile_id,
            "document_kind": descriptor.document_kind,
            "sender": descriptor.sender.dict(),
            "receiver": descriptor.receiver.dict(),
            "provider_document_id": "doc_123",
            "provider_state": "draft",
        },
    )
    await store.checkpoint_operation(
        operation.id, operation.version, operation.state, "doc_123"
    )

    await PeppolService(store, components).submit(task, node, value)

    assert adapter.created == 0
    assert len(adapter.sent) == 1
    assert adapter.sent[0][0] == "doc_123"


@pytest.mark.asyncio
async def test_ambiguous_send_reconciles_transit_before_any_retry() -> None:
    class AmbiguousSendAdapter(FakeAdapter):
        async def send_document(self, provider_document_id, sender, receiver):
            self.sent.append((provider_document_id, sender, receiver))
            self.state = EInvoiceBeDocumentState.TRANSIT
            raise PeppolAmbiguousError(
                PeppolFailure(
                    PeppolFailureStage.SUBMISSION,
                    PeppolFailureCategory.AMBIGUOUS_RESULT,
                )
            )

    adapter = AmbiguousSendAdapter()
    components = build_peppol_components(
        configuration(), AdapterRegistry({"e-invoice-be-rest": FakeFactory(adapter)})
    )
    store = InMemoryTrackingStore()
    node = PeppolNode(
        document={"source": "archive", "id": str(uuid4())},
    )
    task = NodeTask(uuid4(), node)

    await PeppolService(store, components).submit(task, node, document())

    assert len(adapter.sent) == 1
    assert list(store.outcomes.values())[-1].outcome == "submitted"


@pytest.mark.asyncio
async def test_known_send_uncertainty_stays_reconcilable_until_provider_advances() -> (
    None
):
    class AmbiguousDraftAdapter(FakeAdapter):
        async def send_document(self, provider_document_id, sender, receiver):
            self.sent.append((provider_document_id, sender, receiver))
            self.state = EInvoiceBeDocumentState.DRAFT
            raise PeppolAmbiguousError(
                PeppolFailure(
                    PeppolFailureStage.SUBMISSION,
                    PeppolFailureCategory.AMBIGUOUS_RESULT,
                )
            )

    adapter = AmbiguousDraftAdapter()
    components = build_peppol_components(
        configuration(), AdapterRegistry({"e-invoice-be-rest": FakeFactory(adapter)})
    )
    store = InMemoryTrackingStore()
    node = PeppolNode(document={"source": "archive", "id": str(uuid4())})
    task = NodeTask(uuid4(), node)
    service = PeppolService(store, components)

    result = await service.submit(task, node, document())
    operation = await store.operation_for_execution(task.node_execution_id)

    assert result.waiting_feedback is True
    assert operation is not None
    assert operation.lifecycle is TrackedOperationLifecycle.UNCERTAIN
    assert not store.outcomes

    await service.reconcile(operation.id)
    operation = await store.get_operation(operation.id)
    assert operation.lifecycle is TrackedOperationLifecycle.UNCERTAIN

    adapter.state = EInvoiceBeDocumentState.TRANSIT
    await service.reconcile(operation.id)
    operation = await store.get_operation(operation.id)
    assert operation.lifecycle is TrackedOperationLifecycle.OPEN
    assert list(store.outcomes.values())[-1].outcome == "submitted"

    adapter.state = EInvoiceBeDocumentState.SENT
    await service.reconcile(operation.id)
    operation = await store.get_operation(operation.id)
    assert operation.lifecycle is TrackedOperationLifecycle.RESOLVED
    assert list(store.outcomes.values())[-1].outcome == "delivery_confirmed"


@pytest.mark.asyncio
async def test_periodic_reconciler_converges_when_webhook_is_lost() -> None:
    from xarta.services.v1.peppol.reconciler import PeppolReconciler

    adapter = FakeAdapter()
    components = build_peppol_components(
        configuration(), AdapterRegistry({"e-invoice-be-rest": FakeFactory(adapter)})
    )
    store = InMemoryTrackingStore()
    node = PeppolNode(document={"source": "archive", "id": str(uuid4())})
    task = NodeTask(uuid4(), node)
    service = PeppolService(store, components)
    await service.submit(task, node, document())
    operation = await store.operation_for_execution(task.node_execution_id)
    assert operation is not None
    adapter.state = EInvoiceBeDocumentState.SENT

    reconciler = PeppolReconciler(
        store,
        service,
        interval_seconds=30,
        lease_seconds=60,
    )
    assert await reconciler.run_once() == 1

    operation = await store.get_operation(operation.id)
    assert operation.lifecycle is TrackedOperationLifecycle.RESOLVED
    assert list(store.outcomes.values())[-1].outcome == "delivery_confirmed"


@pytest.mark.asyncio
async def test_tenant_webhook_secret_cannot_authorize_another_tenant(
    monkeypatch,
) -> None:
    from xarta.services.v1.peppol.e_invoice_be import EInvoiceBePeppolAdapterFactory
    from xarta.services.v1.peppol.e_invoice_be.adapter import EInvoiceBeTenantClient
    from xarta.services.v1.peppol.webhooks import apply_e_invoice_be_webhook

    value = configuration()
    value["revisions"]["v1"]["tenants"] = [
        {
            "tenant_id": "tenant-a",
            "api_key": "key-a",
            "webhook_secret": "secret-a",
            "peppol_ids": [{"scheme": "0208", "identifier": "1111111111"}],
        },
        {
            "tenant_id": "tenant-b",
            "api_key": "key-b",
            "webhook_secret": "secret-b",
            "peppol_ids": [{"scheme": "0208", "identifier": "2222222222"}],
        },
    ]
    components = build_peppol_components(
        value,
        AdapterRegistry({"e-invoice-be-rest": EInvoiceBePeppolAdapterFactory(None)}),
    )
    store = InMemoryTrackingStore()
    binding = components.current().binding
    operations = {}
    for tenant_id, sender in (
        ("tenant-a", "1111111111"),
        ("tenant-b", "2222222222"),
    ):
        node = PeppolNode(document={"source": "archive", "id": str(uuid4())})
        task = NodeTask(uuid4(), node)
        await store.accept(task)
        operation = await store.create_operation(
            task.node_execution_id,
            "peppol",
            binding,
            {"sender": {"scheme": "0208", "identifier": sender}},
            tenant_id,
        )
        await store.checkpoint_operation(
            operation.id, operation.version, operation.state, "doc_123"
        )
        operations[tenant_id] = operation

    async def get_document(self, provider_document_id):
        return provider_document(provider_document_id, EInvoiceBeDocumentState.SENT)

    monkeypatch.setattr(EInvoiceBeTenantClient, "get_document", get_document)
    event = {
        "id": "evt_1",
        "tenant_id": "tenant-b",
        "type": "document.sent",
        "data": {"document_id": "doc_123"},
    }

    def signature(secret):
        return (
            "sha256="
            + hmac.new(
                secret.encode(),
                json.dumps(event, sort_keys=True).encode(),
                hashlib.sha256,
            ).hexdigest()
        )

    with pytest.raises(PermissionError):
        await apply_e_invoice_be_webhook(
            json.dumps(event).encode(),
            {"X-Signature": signature("secret-a")},
            store=store,
            components=components,
        )

    await apply_e_invoice_be_webhook(
        json.dumps(event, indent=2).encode(),
        {"X-Signature": signature("secret-b")},
        store=store,
        components=components,
    )

    assert (
        await store.get_operation(operations["tenant-a"].id)
    ).lifecycle is TrackedOperationLifecycle.OPEN
    assert (
        await store.get_operation(operations["tenant-b"].id)
    ).lifecycle is TrackedOperationLifecycle.RESOLVED


@pytest.mark.asyncio
async def test_recovered_unknown_draft_creation_is_not_blindly_retried() -> None:
    adapter = FakeAdapter()
    components = build_peppol_components(
        configuration(), AdapterRegistry({"e-invoice-be-rest": FakeFactory(adapter)})
    )
    store = InMemoryTrackingStore()
    node = PeppolNode(
        document={"source": "archive", "id": str(uuid4())},
    )
    task = NodeTask(uuid4(), node)
    value = document()
    descriptor = PeppolDocumentInspector().inspect(value)
    await store.accept(task)
    resolved = components.current()
    await store.create_operation(
        task.node_execution_id,
        "peppol",
        resolved.binding,
        {
            "phase": "creating",
            "source_document_id": str(value.id),
            "source_document_version": None,
            "sha256": hashlib.sha256(value.data).hexdigest(),
            "business_document_id": descriptor.business_document_id,
            "issue_date": descriptor.issue_date.isoformat(),
            "customization_id": descriptor.customization_id,
            "profile_id": descriptor.profile_id,
            "document_kind": descriptor.document_kind,
            "sender": descriptor.sender.dict(),
            "receiver": descriptor.receiver.dict(),
            "provider_document_id": None,
            "provider_state": "creating",
        },
    )

    await PeppolService(store, components).submit(task, node, value)

    assert adapter.created == 0
    event = list(store.outcomes.values())[-1]
    assert event.outcome == "outcome_uncertain"
    assert event.details["category"] == "ambiguous_result"
