from __future__ import annotations

import base64
import datetime
import hashlib
import hmac

from email.message import EmailMessage
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import orjson
import pytest

from xarta.adapters import AdapterRegistry
from xarta.exceptions.protocol import TemporaryError
from xarta.protocol.dag import NodeTask
from xarta.protocol.dag.email import EmailNode
from xarta.services.v1.email.api import email_operation_status
from xarta.services.v1.email.base import EmailComponents
from xarta.services.v1.email.nats import _recipient_addresses
from xarta.services.v1.email.nats import _sender_address
from xarta.services.v1.email.reconciler import ResendEmailReconciler
from xarta.services.v1.email.reconciler import _safe_reconciliation_update
from xarta.services.v1.email.resend import ResendConfiguration
from xarta.services.v1.email.resend import ResendEmailAdapter
from xarta.services.v1.email.resend import ResendEmailAdapterFactory
from xarta.services.v1.email.resend_webhooks import apply_resend_webhook
from xarta.services.v1.email.resend_webhooks import normalize_resend_event
from xarta.services.v1.email.tracking import AsyncEmailEventType
from xarta.services.v1.email.tracking import AsyncEmailUpdate
from xarta.services.v1.email.tracking import initial_async_email_state
from xarta.services.v1.email.tracking import reduce_async_email
from xarta.tracking import AmbiguousSubmissionError
from xarta.tracking import DestinationRegistry
from xarta.tracking import InMemoryTrackingStore


def configuration(**overrides) -> dict:
    secret = base64.b64encode(b"resend-webhook-secret").decode()
    return {
        "base_url": "https://api.resend.com",
        "api_key": "re_test_key",
        "webhook_secret": f"whsec_{secret}",
        "account_id": "resend-account-1",
        "timeout": 10,
        "feedback_retention_seconds": 86400,
        **overrides,
    }


class Content:
    def __init__(self, body: bytes) -> None:
        self.body = body

    async def read(self, limit: int) -> bytes:
        return self.body


class Response:
    def __init__(self, status: int, body: dict) -> None:
        self.status = status
        self.content = Content(orjson.dumps(body))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None


class Session:
    def __init__(self, response: Response) -> None:
        self.response = response
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self.response

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self.response


def message() -> EmailMessage:
    value = EmailMessage()
    value["From"] = "Payroll <payroll@example.test>"
    value["To"] = "delivered+xarta-test-1@resend.dev"
    value["Cc"] = "bounced+xarta-test-2@resend.dev"
    value["Subject"] = "Payroll"
    value.set_content("Payroll attached")
    value.add_attachment(
        b"document", maintype="application", subtype="pdf", filename="payroll.pdf"
    )
    return value


def test_resend_configuration_rejects_empty_signing_secret() -> None:
    with pytest.raises(ValueError, match="too short"):
        ResendConfiguration.parse(configuration(webhook_secret="whsec_"))


def test_callback_ingress_exposes_only_provider_authenticated_paths() -> None:
    template = (
        Path(__file__).parents[2]
        / "k8s/helm/charts/core/templates/ingresses/callback-email.yaml"
    ).read_text()

    assert "/api/v1/email/callbacks/resend" in template
    assert "/api/email/callbacks/resend" in template
    assert "/operations" not in template


def test_resend_fingerprint_freezes_content_and_recipient_addresses() -> None:
    original = message()
    changed = message()
    changed.get_body(preferencelist=("plain",)).set_content("changed")

    assert ResendEmailAdapter.submission_fingerprint(
        original
    ) != ResendEmailAdapter.submission_fingerprint(changed)
    assert _recipient_addresses(
        ["Payroll User <delivered@resend.dev>", "bounced@resend.dev"]
    ) == ["delivered@resend.dev", "bounced@resend.dev"]
    assert _sender_address("Payroll <PAYROLL@example.com>") == "payroll@example.com"


def test_resend_size_limit_covers_complete_encoded_json_request() -> None:
    value = message()
    _, encoded = ResendEmailAdapter._serialize_payload(value)

    ResendEmailAdapter._serialize_payload(value, max_request_bytes=len(encoded))
    with pytest.raises(
        ValueError,
        match=(
            rf"estimated={len(encoded)} bytes, limit={len(encoded) - 1} bytes; "
            "the estimate includes base64 attachments"
        ),
    ):
        ResendEmailAdapter._serialize_payload(value, max_request_bytes=len(encoded) - 1)


@pytest.mark.asyncio
async def test_resend_submission_uses_durable_idempotency_key_and_returns_email_id() -> (
    None
):
    session = Session(Response(200, {"id": "email_123"}))
    adapter = ResendEmailAdapter(ResendConfiguration.parse(configuration()), session)
    checkpoints = []

    async def checkpoint():
        checkpoints.append("before-call")

    submission = await adapter.submit(
        idempotency_key="operation-123",
        idempotency_valid_until=datetime.datetime.now(datetime.UTC)
        + datetime.timedelta(hours=24),
        sender="payroll@example.test",
        recipients=[
            "delivered+xarta-test-1@resend.dev",
            "bounced+xarta-test-2@resend.dev",
        ],
        message=message(),
        before_submit=checkpoint,
    )

    assert submission.provider_reference == "email_123"
    assert submission.completion.value == "feedback"
    assert checkpoints == ["before-call"]
    method, url, request = session.calls[0]
    payload = orjson.loads(request["data"])
    assert method == "POST"
    assert url == "https://api.resend.com/emails"
    assert request["headers"]["Idempotency-Key"] == "operation-123"
    assert payload["to"] == ["delivered+xarta-test-1@resend.dev"]
    assert payload["cc"] == ["bounced+xarta-test-2@resend.dev"]
    assert (
        payload["attachments"][0]["content"] == base64.b64encode(b"document").decode()
    )


@pytest.mark.asyncio
async def test_resend_transient_failure_is_retryable_only_inside_idempotency_window() -> (
    None
):
    adapter = ResendEmailAdapter(
        ResendConfiguration.parse(configuration()),
        Session(Response(503, {"message": "temporary"})),
    )

    async def checkpoint():
        return None

    with pytest.raises(TemporaryError):
        await adapter.submit(
            idempotency_key="operation-123",
            idempotency_valid_until=datetime.datetime.now(datetime.UTC)
            + datetime.timedelta(minutes=5),
            sender="payroll@example.test",
            recipients=[
                "delivered+xarta-test-1@resend.dev",
                "bounced+xarta-test-2@resend.dev",
            ],
            message=message(),
            before_submit=checkpoint,
        )
    with pytest.raises(AmbiguousSubmissionError):
        await adapter.submit(
            idempotency_key="operation-123",
            idempotency_valid_until=datetime.datetime.now(datetime.UTC)
            - datetime.timedelta(seconds=1),
            sender="payroll@example.test",
            recipients=[
                "delivered+xarta-test-1@resend.dev",
                "bounced+xarta-test-2@resend.dev",
            ],
            message=message(),
            before_submit=checkpoint,
        )
    with pytest.raises(AmbiguousSubmissionError, match="safety window"):
        await adapter.submit(
            idempotency_key="operation-123",
            idempotency_valid_until=datetime.datetime.now(datetime.UTC)
            + datetime.timedelta(seconds=30),
            sender="payroll@example.test",
            recipients=[
                "delivered+xarta-test-1@resend.dev",
                "bounced+xarta-test-2@resend.dev",
            ],
            message=message(),
            before_submit=checkpoint,
        )


def signed_webhook(
    event: dict, *, event_id: str = "msg_123", timestamp: int = 1_788_000_000
) -> tuple[bytes, dict]:
    raw = orjson.dumps(event)
    secret = b"resend-webhook-secret"
    signed = f"{event_id}.{timestamp}.".encode() + raw
    signature = base64.b64encode(hmac.new(secret, signed, hashlib.sha256).digest())
    return raw, {
        "svix-id": event_id,
        "svix-timestamp": str(timestamp),
        "svix-signature": f"v1,{signature.decode()}",
    }


def test_resend_webhook_verifies_raw_svix_signature_and_timestamp() -> None:
    event = {
        "type": "email.delivered",
        "created_at": "2026-08-29T00:00:00+00:00",
        "data": {
            "email_id": "email_123",
            "to": ["delivered+xarta-test-1@resend.dev"],
        },
    }
    raw, headers = signed_webhook(event)
    adapter = ResendEmailAdapter(
        ResendConfiguration.parse(configuration()), Session(Response(200, {}))
    )
    now = datetime.datetime.fromtimestamp(1_788_000_000, datetime.UTC)

    parsed = adapter.authenticate_webhook(raw, headers, now=now)

    assert parsed.event_id == "msg_123"
    assert parsed.email_id == "email_123"
    with pytest.raises(PermissionError):
        adapter.authenticate_webhook(raw + b" ", headers, now=now)
    other = ResendEmailAdapter(
        ResendConfiguration.parse(
            configuration(
                webhook_secret=(
                    "whsec_" + base64.b64encode(b"another-webhook-secret").decode()
                )
            )
        ),
        Session(Response(200, {})),
    )
    with pytest.raises(PermissionError):
        other.authenticate_webhook(raw, headers, now=now)
    with pytest.raises(PermissionError, match="outside tolerance"):
        adapter.authenticate_webhook(
            raw, headers, now=now + datetime.timedelta(minutes=6)
        )


def test_resend_events_reduce_monotonically_and_complaint_implies_delivery() -> None:
    recipient = "complained+xarta-test-1@resend.dev"
    state = initial_async_email_state([recipient])
    accepted = reduce_async_email(
        state,
        AsyncEmailUpdate(AsyncEmailEventType.ACCEPTED, (recipient,)),
    )
    delayed = reduce_async_email(
        accepted.state,
        normalize_resend_event("email.delivery_delayed", (recipient,), {}),
    )
    complained = reduce_async_email(
        delayed.state,
        normalize_resend_event("email.complained", (recipient,), {}),
    )

    assert [event.outcome for event in accepted.outcomes] == [
        "accepted",
        "all_accepted",
    ]
    assert [event.outcome for event in delayed.outcomes] == ["delivery_delayed"]
    assert [event.outcome for event in complained.outcomes] == [
        "delivered",
        "complained",
        "all_delivered",
    ]
    assert complained.operation_resolved is False


def test_provider_message_id_is_correlation_metadata_and_cannot_silently_change() -> (
    None
):
    recipient = "delivered@resend.dev"
    first = reduce_async_email(
        initial_async_email_state([recipient]),
        AsyncEmailUpdate(
            AsyncEmailEventType.ACCEPTED,
            (recipient,),
            provider_message_id="<first@example.test>",
        ),
    )
    changed = reduce_async_email(
        first.state,
        AsyncEmailUpdate(
            AsyncEmailEventType.DELIVERED,
            (recipient,),
            provider_message_id="<other@example.test>",
        ),
    )

    assert changed.state["smtp_message_id"] == "<first@example.test>"
    assert changed.state["contradictions"][-1]["observed"] == (
        "provider_message_id_changed"
    )


@pytest.mark.parametrize(
    ("diagnostic", "expected"),
    [
        ("smtp; 550 5.1.1 user unknown", "recipient_unknown"),
        ("smtp; 550 5.2.2 mailbox full", "mailbox_full"),
        ("smtp; 550 5.7.1 rejected", "message_rejected"),
    ],
)
def test_resend_bounce_diagnostics_map_to_finite_semantics(
    diagnostic: str, expected: str
) -> None:
    update = normalize_resend_event(
        "email.bounced",
        ("bounced@resend.dev",),
        {"bounce": {"diagnosticCode": [diagnostic], "type": "Permanent"}},
    )

    assert update.failure_status.value == expected


def test_message_level_reconciliation_does_not_fabricate_recipient_results() -> None:
    recipients = ("first@example.test", "second@example.test")

    update = _safe_reconciliation_update(
        "delivered",
        {"last_event": "delivered", "to": list(recipients)},
        recipients,
        recipients,
    )

    assert update is None


@pytest.mark.asyncio
async def test_authenticated_resend_callback_correlates_and_deduplicates() -> None:
    configured = {
        "transactional": {
            "kind": "email",
            "adapter": "resend-rest",
            "current_revision": "v1",
            "revisions": {"v1": configuration()},
        }
    }
    session = Session(Response(200, {}))
    destinations = DestinationRegistry(configured, require_explicit_revisions=True)
    adapters = AdapterRegistry({"resend-rest": ResendEmailAdapterFactory(session)})
    components = EmailComponents(destinations, adapters)
    store = InMemoryTrackingStore()
    recipient = "delivered+xarta-test-1@resend.dev"
    node = EmailNode(
        to=recipient,
        sender="payroll@example.test",
        body={"plain": "message"},
        destination="transactional",
    )
    task = NodeTask(flow_id=uuid4(), node=node)
    await store.accept(task)
    resolved = destinations.resolve("email", "transactional")
    state = initial_async_email_state([recipient])
    state.update(
        {
            "completion": "feedback",
            "feedback_closes_at": (
                datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=24)
            ).isoformat(),
        }
    )
    operation = await store.create_operation(
        task.node_execution_id,
        "email",
        resolved.binding,
        state,
        "resend-account-1",
    )
    operation = await store.checkpoint_operation(
        operation.id, operation.version, operation.state, "email_123"
    )
    event = {
        "type": "email.delivered",
        "created_at": datetime.datetime.now(datetime.UTC).isoformat(),
        "data": {
            "email_id": "email_123",
            "message_id": "<resend-message-123@example.test>",
            "to": [recipient],
        },
    }
    raw, headers = signed_webhook(
        event, timestamp=int(datetime.datetime.now(datetime.UTC).timestamp())
    )

    first = await apply_resend_webhook(
        "transactional", raw, headers, store=store, components=components
    )
    duplicate = await apply_resend_webhook(
        "transactional", raw, headers, store=store, components=components
    )

    assert first.duplicate is False
    assert duplicate.duplicate is True
    assert [event.outcome for event in first.outcomes] == [
        "accepted",
        "delivered",
        "all_accepted",
        "all_delivered",
    ]
    assert (await store.get_operation(operation.id)).lifecycle.value == "open"
    stored = await store.get_operation(operation.id)
    assert stored.state["smtp_message_id"] == "<resend-message-123@example.test>"
    status = await email_operation_status(
        SimpleNamespace(app=SimpleNamespace(ctx=SimpleNamespace(tracking=store))),
        str(operation.id),
    )
    assert orjson.loads(status.body)["smtp_message_id"] == (
        "<resend-message-123@example.test>"
    )


@pytest.mark.asyncio
async def test_resend_reconciliation_converges_after_lost_webhook() -> None:
    configured = {
        "transactional": {
            "kind": "email",
            "adapter": "resend-rest",
            "current_revision": "v1",
            "revisions": {"v1": configuration()},
        }
    }
    recipient = "delivered+xarta-lost-webhook@resend.dev"
    session = Session(
        Response(
            200,
            {
                "id": "email_lost",
                "last_event": "delivered",
                "to": [recipient],
                "cc": [],
                "bcc": [],
            },
        )
    )
    destinations = DestinationRegistry(configured, require_explicit_revisions=True)
    components = EmailComponents(
        destinations,
        AdapterRegistry({"resend-rest": ResendEmailAdapterFactory(session)}),
    )
    store = InMemoryTrackingStore()
    node = EmailNode(
        to=recipient,
        sender="payroll@example.test",
        body={"plain": "message"},
        destination="transactional",
    )
    task = NodeTask(flow_id=uuid4(), node=node)
    await store.accept(task)
    state = initial_async_email_state([recipient])
    state.update(
        {
            "completion": "feedback",
            "feedback_retention_seconds": 86400,
        }
    )
    operation = await store.create_operation(
        task.node_execution_id,
        "email",
        destinations.resolve("email", "transactional").binding,
        state,
        "resend-account-1",
    )
    await store.checkpoint_operation(
        operation.id, operation.version, operation.state, "email_lost"
    )

    reconciler = ResendEmailReconciler(store, components, interval_seconds=0)
    assert await reconciler.run_once() == 1

    operation = await store.get_operation(operation.id)
    assert operation.lifecycle.value == "open"
    operation = await store.checkpoint_operation(
        operation.id,
        operation.version,
        {
            **operation.state,
            "feedback_closes_at": (
                datetime.datetime.now(datetime.UTC) - datetime.timedelta(seconds=1)
            ).isoformat(),
        },
    )
    assert await reconciler.run_once() == 1

    operation = await store.get_operation(operation.id)
    assert operation.lifecycle.value == "resolved"
    assert [event.outcome for event in store.outcomes.values()] == [
        "accepted",
        "all_accepted",
        "delivered",
        "all_delivered",
    ]
