from __future__ import annotations

import asyncio
import datetime
import os

from email.message import EmailMessage
from email.utils import parseaddr
from uuid import uuid4

import aiohttp
import pytest

from xarta.adapters import AdapterRegistry
from xarta.protocol.dag import NodeTask
from xarta.protocol.dag.email import EmailNode
from xarta.services.v1.email.base import EmailComponents
from xarta.services.v1.email.nats import EmailNATSModel
from xarta.services.v1.email.nats import _worker
from xarta.services.v1.email.resend import ResendConfiguration
from xarta.services.v1.email.resend import ResendEmailAdapter
from xarta.services.v1.email.resend import ResendEmailAdapterFactory
from xarta.tracking import DestinationRegistry
from xarta.tracking import InMemoryTrackingStore

pytestmark = pytest.mark.resend_contract


def contract_configuration() -> tuple[ResendConfiguration, str]:
    configured = {
        "api_key": os.getenv("RESEND_CONTRACT_API_KEY"),
        "webhook_secret": os.getenv("RESEND_CONTRACT_WEBHOOK_SECRET"),
        "account_id": os.getenv("RESEND_CONTRACT_ACCOUNT_ID"),
        "sender": os.getenv("RESEND_CONTRACT_FROM"),
    }
    missing = [name for name, value in configured.items() if not value]
    if missing:
        pytest.skip(
            "Resend contract credentials are not configured: "
            + ", ".join(sorted(missing))
        )
    return (
        ResendConfiguration.parse(
            {
                "base_url": os.getenv(
                    "RESEND_CONTRACT_BASE_URL", "https://api.resend.com"
                ),
                "api_key": configured["api_key"],
                "webhook_secret": configured["webhook_secret"],
                "account_id": configured["account_id"],
                "timeout": 30,
                "feedback_retention_seconds": 300,
            }
        ),
        str(configured["sender"]),
    )


def contract_message(sender: str, recipient: str, test_id: str) -> EmailMessage:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = f"Xarta Resend contract {test_id}"
    message.set_content(
        "Xarta opt-in provider contract test. This message contains no customer data."
    )
    return message


async def wait_for_last_event(
    adapter: ResendEmailAdapter,
    email_id: str,
    expected: frozenset[str],
    *,
    timeout_seconds: float = 90,
) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    last_observation = None
    while asyncio.get_running_loop().time() < deadline:
        last_observation = dict(await adapter.retrieve(email_id))
        if last_observation.get("last_event") in expected:
            return last_observation
        await asyncio.sleep(2)
    last_event = last_observation.get("last_event") if last_observation else None
    raise AssertionError(
        f"Resend email {email_id} did not reach {sorted(expected)} within "
        f"{timeout_seconds}s; last_event={last_event}"
    )


@pytest.mark.asyncio
async def test_live_resend_submission_idempotency_and_reconciliation() -> None:
    configuration, sender = contract_configuration()
    test_id = uuid4().hex
    recipient = f"delivered+xarta-{test_id}@resend.dev"
    message = contract_message(sender, recipient, test_id)

    async with aiohttp.ClientSession() as session:
        adapter = ResendEmailAdapter(configuration, session)
        destinations = DestinationRegistry(
            {
                parseaddr(sender)[1].casefold(): {
                    "kind": "email",
                    "adapter": "resend-rest",
                    "current_revision": "contract",
                    "revisions": {
                        "contract": {
                            "base_url": configuration.base_url,
                            "api_key": configuration.api_key,
                            "webhook_secret": configuration.webhook_secret,
                            "account_id": configuration.account_id,
                            "timeout": configuration.timeout,
                            "feedback_retention_seconds": 300,
                        }
                    },
                }
            }
        )
        components = EmailComponents(
            destinations,
            AdapterRegistry({"resend-rest": ResendEmailAdapterFactory(session)}),
        )
        node = EmailNode(
            to=recipient,
            sender=sender,
            subject=f"Xarta Resend contract {test_id}",
            body={"plain": message.get_body().get_content()},
        )
        task = NodeTask(flow_id=uuid4(), node=node)
        store = InMemoryTrackingStore()
        EmailNATSModel._tracking_store = store
        await _worker(node, task, components=components)
        operation = await store.operation_for_execution(task.node_execution_id)
        assert operation is not None

        async def checkpoint() -> None:
            return None

        repeated = await adapter.submit(
            idempotency_key=str(operation.id),
            idempotency_valid_until=datetime.datetime.fromisoformat(
                operation.state["idempotency_valid_until"]
            ),
            sender=sender,
            recipients=[recipient],
            message=message,
            before_submit=checkpoint,
        )
        assert operation.provider_reference == repeated.provider_reference
        observed = await wait_for_last_event(
            adapter, str(operation.provider_reference), frozenset({"delivered"})
        )
        EmailNATSModel._tracking_store = None

    assert observed["id"] == operation.provider_reference
    assert recipient in observed["to"]
    assert observed.get("message_id")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mailbox", "expected"),
    [
        ("bounced", frozenset({"bounced"})),
        ("complained", frozenset({"complained"})),
        ("suppressed", frozenset({"suppressed"})),
    ],
)
async def test_live_resend_deterministic_recipient_contract(
    mailbox: str, expected: frozenset[str]
) -> None:
    if os.getenv("RESEND_CONTRACT_ALL_RECIPIENTS", "false").lower() != "true":
        pytest.skip("Extended Resend deterministic-recipient tests are disabled")
    configuration, sender = contract_configuration()
    test_id = uuid4().hex
    recipient = f"{mailbox}+xarta-{test_id}@resend.dev"

    async def checkpoint() -> None:
        return None

    async with aiohttp.ClientSession() as session:
        adapter = ResendEmailAdapter(configuration, session)
        submitted = await adapter.submit(
            idempotency_key=str(uuid4()),
            idempotency_valid_until=datetime.datetime.now(datetime.UTC)
            + datetime.timedelta(hours=1),
            sender=sender,
            recipients=[recipient],
            message=contract_message(sender, recipient, test_id),
            before_submit=checkpoint,
        )
        observed = await wait_for_last_event(
            adapter, str(submitted.provider_reference), expected
        )

    assert observed["last_event"] in expected
