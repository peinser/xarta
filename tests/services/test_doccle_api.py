from __future__ import annotations

import datetime
import hashlib

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import orjson
import pytest

from xarta.services.v1.doccle.api import ensure_receiver
from xarta.services.v1.doccle.api import forwarded_client_certificate
from xarta.services.v1.doccle.api import get_receiver
from xarta.services.v1.doccle.api import receiver_callback
from xarta.services.v1.doccle.api import resolve_receiver
from xarta.services.v1.doccle.models import DoccleReceiver
from xarta.services.v1.doccle.models import ReceiverState


def test_forwarded_client_certificate_decodes_ingress_header() -> None:
    assert (
        forwarded_client_certificate(
            "-----BEGIN%20CERTIFICATE-----%0AAQID%0A-----END%20CERTIFICATE-----%0A"
        )
        == b"\x01\x02\x03"
    )
    assert forwarded_client_certificate("not-a-certificate") is None


def receiver():
    now = datetime.datetime(2026, 8, 28, tzinfo=datetime.UTC)
    return DoccleReceiver(
        uuid4(),
        "server-default",
        {"customer": "42"},
        "provider-id",
        ReceiverState.PROVISIONED,
        False,
        None,
        now,
        now,
    )


def request(payload=None, *, token="Bearer secret", found=None):
    service = SimpleNamespace(
        ensure=AsyncMock(return_value=found), resolve=AsyncMock(return_value=found)
    )
    repository = SimpleNamespace(load=AsyncMock(return_value=found))
    ctx = SimpleNamespace(
        doccle_control_plane_token="secret",
        doccle_receiver_service=service,
        doccle_receiver_repository=repository,
    )
    value = SimpleNamespace(
        headers={"authorization": token} if token is not None else {},
        body=orjson.dumps(payload) if payload is not None else b"",
        app=SimpleNamespace(ctx=ctx),
    )
    return value, service, repository


def body(response):
    return orjson.loads(response.body)


@pytest.mark.asyncio
async def test_ensure_rejects_client_destination_and_uses_server_default() -> None:
    expected = receiver()
    invalid, invalid_service, _ = request(
        {
            "destination": "client-choice",
            "subject": {"customer": "42"},
            "profile": {"label": "Customer"},
        },
        found=expected,
    )
    rejected = await ensure_receiver(invalid)
    assert rejected.status == 400
    invalid_service.ensure.assert_not_awaited()

    valid, service, _ = request(
        {"subject": {"customer": "42"}, "profile": {"label": "Customer"}},
        found=expected,
    )
    response = await ensure_receiver(valid)
    assert response.status == 200
    subject, profile = service.ensure.await_args.args
    assert subject == {"customer": "42"}
    assert profile.label == "Customer"
    assert "destination" not in body(response)


@pytest.mark.asyncio
async def test_resolve_rejects_destination_and_reports_not_found() -> None:
    invalid, service, _ = request(
        {"subject": {"customer": "42"}, "destination": "client-choice"}
    )
    assert (await resolve_receiver(invalid)).status == 400
    service.resolve.assert_not_awaited()

    missing, _, _ = request({"subject": {"customer": "missing"}})
    response = await resolve_receiver(missing)
    assert response.status == 404
    assert body(response) == {"error": "receiver not found"}


@pytest.mark.asyncio
@pytest.mark.parametrize("token", [None, "Basic secret", "Bearer wrong"])
async def test_handlers_require_bearer_authentication(token) -> None:
    value, service, _ = request(
        {"subject": {"customer": "42"}, "profile": {"label": "Customer"}},
        token=token,
    )
    response = await ensure_receiver(value)
    assert response.status == 401
    service.ensure.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "profile",
    [None, {}, {"label": ""}, {"label": "Customer", "destination": "primary"}],
)
async def test_ensure_validates_profile(profile) -> None:
    value, service, _ = request({"subject": {"customer": "42"}, "profile": profile})
    response = await ensure_receiver(value)
    assert response.status == 400
    service.ensure.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_validates_identifier_and_returns_not_found() -> None:
    value, _, repository = request(found=None)
    invalid = await get_receiver(value, "not-a-uuid")
    assert invalid.status == 400
    repository.load.assert_not_awaited()

    missing = await get_receiver(value, str(uuid4()))
    assert missing.status == 404
    repository.load.assert_awaited_once()


class Transport:
    def __init__(self, certificate: bytes | None) -> None:
        self.certificate = certificate

    def get_extra_info(self, name):
        return self.certificate if name == "peercert_binary" else None


@pytest.mark.asyncio
async def test_callback_rejects_auth_and_malformed_json_without_runtime_interaction() -> (
    None
):
    certificate = b"doccle-certificate"
    repository = SimpleNamespace(
        load_by_external_id=AsyncMock(), apply_callback=AsyncMock()
    )
    nats = AsyncMock()
    tracking = AsyncMock()
    ctx = SimpleNamespace(
        doccle_callbacks_enabled=True,
        doccle_callback_certificate_fingerprints={
            hashlib.sha256(certificate).hexdigest()
        },
        doccle_components=SimpleNamespace(),
        doccle_receiver_repository=repository,
        nats=nats,
        tracking=tracking,
    )

    unauthorized = SimpleNamespace(
        transport=Transport(b"wrong"),
        body=b"{}",
        app=SimpleNamespace(ctx=ctx),
    )
    assert (await receiver_callback(unauthorized)).status == 401  # type: ignore[arg-type]

    malformed = SimpleNamespace(
        transport=Transport(certificate),
        body=b"not-json",
        app=SimpleNamespace(ctx=ctx),
    )
    assert (await receiver_callback(malformed)).status == 400  # type: ignore[arg-type]
    repository.load_by_external_id.assert_not_awaited()
    repository.apply_callback.assert_not_awaited()
    nats.assert_not_awaited()
    tracking.assert_not_awaited()


@pytest.mark.asyncio
async def test_callback_is_not_available_when_disabled() -> None:
    value = SimpleNamespace(
        app=SimpleNamespace(ctx=SimpleNamespace(doccle_callbacks_enabled=False))
    )

    callback_response = await receiver_callback(value)  # type: ignore[arg-type]

    assert callback_response.status == 404
