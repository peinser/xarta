from __future__ import annotations

import hashlib

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from xarta.services.v1.doccle.api import bearer_is_authorized
from xarta.services.v1.doccle.api import parse_json_object
from xarta.services.v1.doccle.callbacks import CallbackValidationError
from xarta.services.v1.doccle.callbacks import apply_receiver_link_callback
from xarta.services.v1.doccle.callbacks import callback_fields
from xarta.services.v1.doccle.callbacks import callback_identity
from xarta.services.v1.doccle.callbacks import certificate_is_allowed
from xarta.services.v1.doccle.callbacks import parse_certificate_fingerprints


@dataclass(frozen=True)
class Binding:
    destination: str


@dataclass(frozen=True)
class Resolved:
    binding: Binding
    configuration: dict


class Destinations:
    def __init__(self) -> None:
        self.resolved = Resolved(Binding("primary"), {"sender_name": "sender"})

    def iter_resolved(self, capability):
        assert capability == "doccle"
        return iter((self.resolved,))

    def resolve(self, capability, destination):
        assert (capability, destination) == ("doccle", "primary")
        return self.resolved


def test_certificate_auth_and_control_token_are_constant_time_inputs() -> None:
    certificate = b"client certificate DER"
    fingerprint = hashlib.sha256(certificate).hexdigest()
    allowed = parse_certificate_fingerprints(
        ":".join(fingerprint[index : index + 2] for index in range(0, 64, 2))
    )
    assert certificate_is_allowed(certificate, allowed)
    assert not certificate_is_allowed(b"other", allowed)
    assert not certificate_is_allowed(None, allowed)
    assert bearer_is_authorized("Bearer secret", "secret")
    assert not bearer_is_authorized("Basic secret", "secret")


def test_strict_bounded_json() -> None:
    assert parse_json_object(b'{"a":1}', limit=10) == {"a": 1}
    with pytest.raises(ValueError, match="size"):
        parse_json_object(b'{"a":123}', limit=2)
    with pytest.raises(ValueError, match="object"):
        parse_json_object(b"[]", limit=10)


def test_callback_accepts_documented_receiver_reference_field() -> None:
    assert callback_fields(
        {
            "eventType": "RECEIVER_LINK_UNLINK",
            "senderName": "sender",
            "receiverExternalReferenceId": "receiver-1",
            "linked": True,
        }
    ) == ("sender", "receiver-1", True)


@pytest.mark.asyncio
async def test_link_unlink_sequence_is_idempotent_per_exact_event() -> None:
    receiver = SimpleNamespace(id=uuid4())
    repository: Any = SimpleNamespace(
        load_by_external_id=AsyncMock(return_value=receiver),
        apply_callback=AsyncMock(
            side_effect=["linked", "duplicate", "unlinked", "relinked"]
        ),
    )
    components: Any = SimpleNamespace(
        destinations=Destinations(), nats=AsyncMock(), tracking=AsyncMock()
    )
    linked = {
        "event": "RECEIVER_LINK_UNLINK",
        "senderName": "sender",
        "receiverId": "receiver-1",
        "linked": True,
        "providerSequence": 7,
    }
    unlinked = {**linked, "linked": False, "providerSequence": 8}
    relinked = {**linked, "providerSequence": 9}

    assert callback_identity(linked) == callback_identity(
        dict(reversed(linked.items()))
    )
    assert callback_identity(linked) != callback_identity(unlinked)
    assert (
        await apply_receiver_link_callback(
            linked, components=components, repository=repository
        )
        == "linked"
    )
    assert (
        await apply_receiver_link_callback(
            linked, components=components, repository=repository
        )
        == "duplicate"
    )
    assert (
        await apply_receiver_link_callback(
            unlinked, components=components, repository=repository
        )
        == "unlinked"
    )
    assert (
        await apply_receiver_link_callback(
            relinked, components=components, repository=repository
        )
        == "relinked"
    )
    assert repository.apply_callback.await_count == 4
    assert all(
        "task" not in call.kwargs and "outcome" not in call.kwargs
        for call in repository.apply_callback.await_args_list
    )
    components.nats.assert_not_awaited()
    components.tracking.assert_not_awaited()


@pytest.mark.asyncio
async def test_callback_rejects_unsupported_sender_and_unknown_receiver() -> None:
    components: Any = SimpleNamespace(destinations=Destinations())
    repository: Any = SimpleNamespace(load_by_external_id=AsyncMock(return_value=None))
    payload = {
        "event_type": "RECEIVER_LINK_UNLINK",
        "sender_name": "sender",
        "external_receiver_id": "missing",
        "linked": True,
    }
    with pytest.raises(LookupError):
        await apply_receiver_link_callback(
            payload, components=components, repository=repository
        )
    with pytest.raises(CallbackValidationError, match="Unsupported"):
        await apply_receiver_link_callback(
            {**payload, "event_type": "DOCUMENT"},
            components=components,
            repository=repository,
        )
    with pytest.raises(CallbackValidationError, match="sender"):
        await apply_receiver_link_callback(
            {**payload, "sender_name": "wrong"},
            components=components,
            repository=repository,
        )
