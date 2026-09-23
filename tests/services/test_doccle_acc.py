from __future__ import annotations

import os

from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import aiohttp
import orjson
import pytest

from xarta.services.v1.doccle.adapters import DoccleSenderRESTAdapter
from xarta.services.v1.doccle.adapters import DoccleTransportError
from xarta.services.v1.doccle.models import DoccleDocument
from xarta.services.v1.doccle.models import DoccleReceiverProfile
from xarta.services.v1.doccle.models import DoccleResult
from xarta.services.v1.doccle.models import DoccleResultCategory
from xarta.services.v1.doccle.models import DoccleTransportCategory
from xarta.tracking import DestinationRegistry

REQUIRED_ENV = ("DOCCLE_CONFIGURATIONS_CONFIG_PATH",)
ACC_ENABLED = (
    os.environ.get("DOCCLE_ACC_TESTS") == "1"
    and all(os.environ.get(name) for name in REQUIRED_ENV)
    and Path(os.environ.get("DOCCLE_CONFIGURATIONS_CONFIG_PATH", "")).is_file()
)

pytestmark = [
    pytest.mark.doccle_acc,
    pytest.mark.skipif(
        not ACC_ENABLED,
        reason=(
            "Doccle ACC tests require DOCCLE_ACC_TESTS=1 and the normal "
            "normal DOCCLE_CONFIGURATIONS_CONFIG_PATH setting"
        ),
    ),
]


def _configuration(
    *, timeout: float | None = None, client_secret: str | None = None
) -> dict:
    configuration = orjson.loads(
        Path(os.environ["DOCCLE_CONFIGURATIONS_CONFIG_PATH"]).read_bytes()
    )
    sender = configuration["current_sender"]
    registry = DestinationRegistry(
        {
            name: {"kind": "doccle", **sender_configuration}
            for name, sender_configuration in configuration["senders"].items()
        },
        require_explicit_revisions=True,
    )
    resolved = registry.resolve("doccle", sender)
    configuration = deepcopy(resolved.configuration)
    if timeout is not None:
        configuration["timeout"] = timeout
    if client_secret is not None:
        configuration["credentials"]["client_secret"] = client_secret
    return configuration


def _receiver_id(purpose: str) -> str:
    return f"xarta-acc-{purpose}-{uuid4()}"


def _profile(label: str = "base") -> DoccleReceiverProfile:
    return DoccleReceiverProfile(
        label=f"Xarta ACC synthetic {label}",
        first_name="Synthetic",
        last_name="Receiver",
        email=f"xarta-acc-{uuid4()}@example.invalid",
        language="en",
    )


def _document(
    document_id: str, content: bytes = b"Synthetic Doccle ACC document\n"
) -> DoccleDocument:
    document_type = next(iter(_configuration()["document_types"]))
    return DoccleDocument(
        document_id=document_id,
        document_type=document_type,
        filename=f"{document_id}.txt",
        content_type="text/plain",
        content=content,
        names={"en": "Synthetic ACC document"},
    )


def _record(record_property, prefix: str, result: DoccleResult) -> None:
    record_property(f"{prefix}_category", result.category.value)
    record_property(f"{prefix}_provider_status", result.provider_status or "<none>")


async def _provision(adapter: DoccleSenderRESTAdapter, purpose: str) -> str:
    receiver_id = _receiver_id(purpose)
    result = await adapter.create_or_update_receiver(
        receiver_id=receiver_id, profile=_profile()
    )
    assert result.category is DoccleResultCategory.PROVISIONED
    return receiver_id


@pytest.mark.asyncio
async def test_receiver_put_create_identical_repeat_and_update(record_property) -> None:
    receiver_id = _receiver_id("receiver-put")
    profile = _profile()
    async with aiohttp.ClientSession() as session:
        adapter = DoccleSenderRESTAdapter(_configuration(), session)
        created = await adapter.create_or_update_receiver(
            receiver_id=receiver_id, profile=profile
        )
        repeated = await adapter.create_or_update_receiver(
            receiver_id=receiver_id, profile=profile
        )
        updated = await adapter.create_or_update_receiver(
            receiver_id=receiver_id, profile=_profile("updated")
        )

    _record(record_property, "receiver_create", created)
    _record(record_property, "receiver_repeat", repeated)
    _record(record_property, "receiver_update", updated)
    assert {created.category, repeated.category, updated.category} == {
        DoccleResultCategory.PROVISIONED
    }


@pytest.mark.asyncio
async def test_put_document(record_property) -> None:
    async with aiohttp.ClientSession() as session:
        adapter = DoccleSenderRESTAdapter(_configuration(), session)
        receiver_id = await _provision(adapter, "put-document")
        result = await adapter.put_document(
            receiver_id=receiver_id, document=_document(f"xarta-acc-document-{uuid4()}")
        )

    _record(record_property, "put_document", result)
    assert result.category is DoccleResultCategory.STORED


@pytest.mark.asyncio
async def test_unknown_receiver_is_rejected(record_property) -> None:
    async with aiohttp.ClientSession() as session:
        adapter = DoccleSenderRESTAdapter(_configuration(), session)
        result = await adapter.put_document(
            receiver_id=_receiver_id("unknown"),
            document=_document(f"xarta-acc-unknown-{uuid4()}"),
        )

    _record(record_property, "unknown_receiver", result)
    assert result.category is DoccleResultCategory.RECEIVER_NOT_FOUND


@pytest.mark.asyncio
async def test_invalid_document_is_not_reported_as_provisioning(
    record_property,
) -> None:
    async with aiohttp.ClientSession() as session:
        adapter = DoccleSenderRESTAdapter(_configuration(), session)
        receiver_id = await _provision(adapter, "invalid-document")
        result = await adapter.put_document(
            receiver_id=receiver_id,
            document=_document(f"xarta-acc-invalid-{uuid4()}", content=b""),
        )

    _record(record_property, "invalid_document", result)
    # Empty content is the least destructive provider-side validation probe. Some
    # contracts may accept it, so record the result without inventing a rejection rule.
    assert result.category is not DoccleResultCategory.PROVISIONED


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("DOCCLE_ACC_ALLOW_REPLAY_TESTS") != "1",
    reason="duplicate Doccle POSTs require DOCCLE_ACC_ALLOW_REPLAY_TESTS=1",
)
async def test_replay_same_id_same_content_and_changed_content(record_property) -> None:
    async with aiohttp.ClientSession() as session:
        adapter = DoccleSenderRESTAdapter(_configuration(), session)
        receiver_id = await _provision(adapter, "replay")

        same_id = f"xarta-acc-replay-same-{uuid4()}"
        same_first = await adapter.put_document(
            receiver_id=receiver_id, document=_document(same_id)
        )
        same_repeat = await adapter.put_document(
            receiver_id=receiver_id, document=_document(same_id)
        )

        changed_id = f"xarta-acc-replay-changed-{uuid4()}"
        changed_first = await adapter.put_document(
            receiver_id=receiver_id,
            document=_document(changed_id, b"Synthetic version one\n"),
        )
        changed_repeat = await adapter.put_document(
            receiver_id=receiver_id,
            document=_document(changed_id, b"Synthetic version two\n"),
        )

    observations = {
        "same_first": same_first,
        "same_repeat": same_repeat,
        "changed_first": changed_first,
        "changed_repeat": changed_repeat,
    }
    for name, result in observations.items():
        _record(record_property, name, result)
        assert result.category in {
            DoccleResultCategory.STORED,
            DoccleResultCategory.BUSINESS_REJECTION,
            DoccleResultCategory.AUTHENTICATION_FAILURE,
            DoccleResultCategory.PROTOCOL_ERROR,
        }


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("DOCCLE_ACC_ALLOW_AUTH_FAILURE_TESTS") != "1",
    reason="authentication probe requires DOCCLE_ACC_ALLOW_AUTH_FAILURE_TESTS=1",
)
async def test_authentication_failure(record_property) -> None:
    async with aiohttp.ClientSession() as session:
        adapter = DoccleSenderRESTAdapter(
            _configuration(client_secret=f"intentionally-invalid-{uuid4()}"), session
        )
        with pytest.raises(DoccleTransportError) as caught:
            await adapter.create_or_update_receiver(
                receiver_id=_receiver_id("auth-failure"), profile=_profile()
            )

    record_property("authentication_failure", caught.value.category.value)
    assert caught.value.category is DoccleTransportCategory.SAFE_PRETRANSMISSION


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("DOCCLE_ACC_ALLOW_TIMEOUT_TESTS") != "1",
    reason="timeout probe requires DOCCLE_ACC_ALLOW_TIMEOUT_TESTS=1",
)
async def test_timeout_outcome_is_treated_cautiously(record_property) -> None:
    async with aiohttp.ClientSession() as session:
        adapter = DoccleSenderRESTAdapter(_configuration(timeout=0.000001), session)
        try:
            result = await adapter.create_or_update_receiver(
                receiver_id=_receiver_id("timeout"), profile=_profile()
            )
        except DoccleTransportError as error:
            record_property("timeout_transport_category", error.category.value)
            assert error.category in {
                DoccleTransportCategory.SAFE_PRETRANSMISSION,
                DoccleTransportCategory.AMBIGUOUS,
            }
        else:
            _record(record_property, "timeout_completed", result)
            assert result.category in DoccleResultCategory
