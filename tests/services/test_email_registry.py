from __future__ import annotations

import datetime

from typing import Any
from uuid import uuid4

import pytest

from xarta.adapters import AdapterRegistry
from xarta.exceptions.protocol import TemporaryError
from xarta.execution import ExecutionMode
from xarta.protocol.dag import NodeTask
from xarta.protocol.dag.email import EmailNode
from xarta.services.v1.email.adapters import EmailCompletionMode
from xarta.services.v1.email.adapters import EmailSideEffectBoundary
from xarta.services.v1.email.adapters import EmailSubmission
from xarta.services.v1.email.adapters import EmailSubmissionPlan
from xarta.services.v1.email.adapters import SMTPEmailAdapterFactory
from xarta.services.v1.email.base import EmailComponents
from xarta.services.v1.email.base import build_email_components
from xarta.services.v1.email.base import normalize_email_configurations
from xarta.services.v1.email.nats import EmailNATSModel
from xarta.services.v1.email.nats import _worker
from xarta.services.v1.email.tracking import initial_async_email_state
from xarta.services.v1.email.tracking import initial_email_state
from xarta.tracking import DestinationRegistry
from xarta.tracking import InMemoryTrackingStore
from xarta.tracking import TrackedOperationLifecycle


def smtp_configuration(**values: Any) -> dict[str, Any]:
    return {
        "server": {"hostname": "smtp.example.test", "port": 587},
        "password": "secret",
        **values,
    }


def test_legacy_email_configuration_is_normalized_without_overrides() -> None:
    source = {
        "sender@example.test": smtp_configuration(),
        "conflict": smtp_configuration(kind="sftp", adapter="custom"),
    }

    normalized = normalize_email_configurations(source)

    assert normalized["sender@example.test"]["kind"] == "email"
    assert normalized["sender@example.test"]["adapter"] == "smtp"
    assert normalized["conflict"]["kind"] == "sftp"
    assert normalized["conflict"]["adapter"] == "custom"
    assert "kind" not in source["sender@example.test"]


def test_email_components_validate_every_retained_revision() -> None:
    configurations = {
        "sender@example.test": {
            "current_revision": "v2",
            "revisions": {
                "v1": smtp_configuration(server={"unsupported": True}),
                "v2": smtp_configuration(),
            },
        }
    }

    with pytest.raises(ValueError, match="non-empty hostname"):
        build_email_components(configurations)


def test_email_components_require_explicit_revisions() -> None:
    with pytest.raises(ValueError, match="explicit current_revision and revisions"):
        build_email_components({"sender@example.test": smtp_configuration()})


def test_email_execution_mode_is_selected_by_adapter() -> None:
    components = build_email_components(
        {
            "sender@example.test": {
                "current_revision": "v1",
                "revisions": {"v1": smtp_configuration()},
            }
        }
    )
    node = EmailNode(
        to="recipient@example.test",
        sender="sender@example.test",
        body={"plain": "hello"},
    )

    assert (
        components.execution_modes["sender@example.test"] is ExecutionMode.SYNCHRONOUS
    )
    assert (
        EmailNATSModel._resolve_task_mode(
            NodeTask(flow_id=uuid4(), node=node), components
        )
        is ExecutionMode.SYNCHRONOUS
    )


def test_email_destination_cannot_change_execution_mode_between_revisions() -> None:
    class ModeAdapter:
        def __init__(self, configuration):
            self.execution_mode = ExecutionMode(configuration["mode"])

    class ModeFactory:
        def validate(self, configuration):
            ExecutionMode(configuration["mode"])

        def create(self, configuration):
            return ModeAdapter(configuration)

    configurations = {
        "sender@example.test": {
            "kind": "email",
            "adapter": "mode",
            "current_revision": "tracked",
            "revisions": {
                "synchronous": {"mode": "synchronous"},
                "tracked": {"mode": "tracked"},
            },
        }
    }

    with pytest.raises(ValueError, match="changes execution mode"):
        build_email_components(configurations, AdapterRegistry({"mode": ModeFactory()}))


def test_smtp_factory_validation_does_not_construct_client(monkeypatch) -> None:
    constructed = False

    def smtp_client(**kwargs):
        nonlocal constructed
        constructed = True
        raise AssertionError("SMTP client must not be constructed during validation")

    monkeypatch.setattr("aiosmtplib.SMTP", smtp_client)

    SMTPEmailAdapterFactory().validate(smtp_configuration())

    assert constructed is False


@pytest.mark.asyncio
async def test_synchronous_worker_resolves_current_revision_without_tracking() -> None:
    sender = "sender@example.test"
    created_revisions = []
    fail_first_submission = True

    class FakeAdapter:
        execution_mode = ExecutionMode.SYNCHRONOUS

        def __init__(self, configuration):
            self.configuration = configuration

        @property
        def provider_account_reference(self):
            return None

        def initial_state(self, recipients):
            return initial_email_state(recipients)

        def plan_submission(self, current_state, recipients, message, now):
            return EmailSubmissionPlan(
                initial_email_state(recipients),
                EmailSideEffectBoundary.MARK_UNCERTAIN,
            )

        async def submit(self, **kwargs):
            nonlocal fail_first_submission
            if fail_first_submission:
                fail_first_submission = False
                raise TemporaryError("temporary refusal")
            recipient = kwargs["recipients"][0]
            return EmailSubmission(
                provider_reference=kwargs["idempotency_key"],
                state={
                    "submission_outcomes": [
                        {
                            "outcome": "accepted",
                            "subject": {"kind": "recipient", "id": recipient},
                            "details": None,
                        }
                    ]
                },
            )

    class FakeFactory:
        def validate(self, configuration):
            pass

        def create(self, configuration):
            created_revisions.append(configuration["revision"])
            return FakeAdapter(configuration)

    factory = FakeFactory()

    def components(current_revision: str) -> EmailComponents:
        destinations = DestinationRegistry(
            {
                sender: {
                    "kind": "email",
                    "adapter": "fake",
                    "current_revision": current_revision,
                    "revisions": {
                        "v1": {"value": "old"},
                        "v2": {"value": "new"},
                    },
                }
            }
        )
        return EmailComponents(destinations, AdapterRegistry({"fake": factory}))

    node = EmailNode(
        to="recipient@example.test",
        sender=sender,
        body={"plain": "message"},
    )
    task = NodeTask(flow_id=uuid4(), node=node)
    store = InMemoryTrackingStore()
    EmailNATSModel._tracking_store = store

    with pytest.raises(TemporaryError):
        await _worker(node, task, components=components("v1"))

    result = await _worker(node, task, components=components("v2"))

    assert result.outcomes_persisted is False
    assert created_revisions == ["v1", "v2"]
    operation = await store.operation_for_execution(task.node_execution_id)
    assert operation is None


@pytest.mark.asyncio
async def test_generic_worker_accepts_another_feedback_capable_adapter() -> None:
    sender = "sender@example.test"

    class MockFeedbackAdapter:
        execution_mode = ExecutionMode.TRACKED

        @property
        def provider_account_reference(self):
            return "mock-provider-account"

        def initial_state(self, recipients):
            return initial_async_email_state(recipients)

        def plan_submission(self, current_state, recipients, message, now):
            state = initial_async_email_state(recipients)
            state["mock_checkpoint"] = True
            return EmailSubmissionPlan(
                state,
                EmailSideEffectBoundary.CHECKPOINTED_IDEMPOTENCY,
                now + datetime.timedelta(hours=1),
            )

        async def submit(self, **kwargs):
            return EmailSubmission(
                "mock-email-123",
                {"feedback_mode": "asynchronous"},
                EmailCompletionMode.FEEDBACK,
            )

    class MockFactory:
        def validate(self, configuration):
            return None

        def create(self, configuration):
            return MockFeedbackAdapter()

    destinations = DestinationRegistry(
        {
            sender: {
                "kind": "email",
                "adapter": "mock-feedback",
                "current_revision": "v1",
                "revisions": {"v1": {}},
            }
        }
    )
    components = EmailComponents(
        destinations, AdapterRegistry({"mock-feedback": MockFactory()})
    )
    node = EmailNode(to="recipient@example.test", sender=sender, body={"plain": "hi"})
    task = NodeTask(flow_id=uuid4(), node=node)
    store = InMemoryTrackingStore()
    EmailNATSModel._tracking_store = store

    result = await _worker(node, task, components=components)
    operation = await store.operation_for_execution(task.node_execution_id)

    assert result.waiting_feedback is True
    assert operation.binding.adapter == "mock-feedback"
    assert operation.provider_account_reference == "mock-provider-account"
    assert operation.provider_reference == "mock-email-123"
    assert operation.lifecycle is TrackedOperationLifecycle.OPEN
    assert [event.outcome for event in store.outcomes.values()] == [
        "accepted",
        "all_accepted",
    ]
    EmailNATSModel._tracking_store = None
