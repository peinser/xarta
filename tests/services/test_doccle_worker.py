from __future__ import annotations

import datetime

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from xarta.exceptions.protocol import TemporaryError
from xarta.services.v1.doccle.adapters import DoccleAmbiguousTransportError
from xarta.services.v1.doccle.adapters import DoccleSafePreTransmissionError
from xarta.services.v1.doccle.models import DoccleReceiver
from xarta.services.v1.doccle.models import DoccleResult
from xarta.services.v1.doccle.models import DoccleResultCategory
from xarta.services.v1.doccle.models import ReceiverState
from xarta.services.v1.doccle.nats import _worker
from xarta.tracking import DestinationBinding

NOW = datetime.datetime(2026, 8, 28, tzinfo=datetime.UTC)


class Source:
    def __init__(self) -> None:
        self.result = SimpleNamespace(
            id=uuid4(),
            data=b"invoice",
            content_type="application/pdf",
            metadata={"filename": "invoice.pdf"},
        )
        self.retrieve = AsyncMock(return_value=self.result)


class Node:
    def __init__(self, source: Source, receiver) -> None:
        self.source = source
        self.receiver = receiver
        self.document_type = "invoice"
        self.name = {"en": "Invoice"}
        self.published_at = NOW

    def interpret(self):
        return self.source


class AdapterFactory:
    def __init__(self, results) -> None:
        self.results = iter(results)
        self.calls: list[tuple[str, Any]] = []

    def create(self, _name, _configuration):
        factory = self

        class Adapter:
            async def put_document(self, *, receiver_id, document):
                factory.calls.append((receiver_id, document))
                result = next(factory.results)
                if isinstance(result, BaseException):
                    raise result
                return result

        return Adapter()


def receiver(state=ReceiverState.PROVISIONED, external_id="provider-receiver"):
    return DoccleReceiver(
        uuid4(),
        "primary",
        {"customer": "42"},
        external_id,
        state,
        False,
        None,
        NOW,
        NOW,
    )


def setup_worker(*, results=(), local_receiver=None):
    source = Source()
    node = Node(
        source, SimpleNamespace(subject={"customer": "42"}, id=None, mode="subject")
    )
    adapter = AdapterFactory(results)
    resolved = SimpleNamespace(
        binding=DestinationBinding("primary", "doccle", "fake", "v1"),
        configuration={"document_types": {"invoice": "INVOICE"}},
    )
    components = SimpleNamespace(
        adapters=SimpleNamespace(create=adapter.create),
        resolve_sender=lambda: resolved,
    )
    receivers = SimpleNamespace(load_by_subject=AsyncMock(return_value=local_receiver))
    task = SimpleNamespace(node_execution_id=uuid4())
    return node, task, components, receivers, adapter, source


async def run_worker(state):
    node, task, components, receivers, adapter, source = state
    result = await _worker(
        node,
        task,
        components=components,
        receiver_repository=receivers,
    )
    assert result.waiting_feedback is False
    assert result.outcomes_persisted is False
    assert len(result.outcomes) == 1
    return result.outcomes[0]


@pytest.mark.asyncio
async def test_stored_is_synchronous_without_submission_persistence() -> None:
    state = setup_worker(
        results=[DoccleResult(DoccleResultCategory.STORED, "provider-document")],
        local_receiver=receiver(),
    )

    outcome = await run_worker(state)

    assert outcome.outcome == "stored"
    assert state[4].calls[0][1].document_id == str(state[1].node_execution_id)
    state[3].load_by_subject.assert_awaited_once()
    state[5].retrieve.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "category",
    [
        DoccleResultCategory.BUSINESS_REJECTION,
        DoccleResultCategory.AUTHENTICATION_FAILURE,
        DoccleResultCategory.CONFIGURATION_ERROR,
        DoccleResultCategory.PROTOCOL_ERROR,
    ],
)
async def test_provider_failures_map_to_document_rejected(category) -> None:
    outcome = await run_worker(
        setup_worker(
            results=[DoccleResult(category, provider_status="REJECTED")],
            local_receiver=receiver(),
        )
    )

    assert outcome.outcome == "document_rejected"
    assert outcome.details == {
        "reason": "provider_rejection",
        "category": category.value,
        "provider_status": "REJECTED",
        "selector": "subject",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("local_receiver", "expected"),
    [
        (None, "receiver_not_found"),
        (receiver(ReceiverState.PENDING), "receiver_not_provisioned"),
    ],
)
async def test_subject_resolution_failure_avoids_source_and_adapter(
    local_receiver, expected
) -> None:
    state = setup_worker(local_receiver=local_receiver)

    outcome = await run_worker(state)

    assert outcome.outcome == expected
    state[5].retrieve.assert_not_awaited()
    assert state[4].calls == []


@pytest.mark.asyncio
async def test_explicit_receiver_bypasses_subject_repository() -> None:
    state = setup_worker(
        results=[DoccleResult(DoccleResultCategory.STORED)], local_receiver=None
    )
    state[0].receiver = SimpleNamespace(
        subject=None, id="explicit-provider-id", mode="explicit"
    )

    outcome = await run_worker(state)

    assert outcome.outcome == "stored"
    state[3].load_by_subject.assert_not_awaited()
    assert state[4].calls[0][0] == "explicit-provider-id"


@pytest.mark.asyncio
async def test_safe_pretransmission_failure_uses_jetstream_retry() -> None:
    state = setup_worker(
        results=[DoccleSafePreTransmissionError("not sent")],
        local_receiver=receiver(),
    )

    with pytest.raises(TemporaryError):
        await run_worker(state)


@pytest.mark.asyncio
async def test_ambiguous_failure_is_terminal_uncertainty() -> None:
    state = setup_worker(
        results=[DoccleAmbiguousTransportError("maybe committed")],
        local_receiver=receiver(),
    )

    outcome = await run_worker(state)

    assert outcome.outcome == "outcome_uncertain"
