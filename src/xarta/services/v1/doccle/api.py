from __future__ import annotations

import hmac
import ssl

from typing import TYPE_CHECKING
from typing import Any
from urllib.parse import unquote
from uuid import UUID

import orjson

from sanic import response

from xarta.services.v1.doccle.callbacks import apply_receiver_link_callback
from xarta.services.v1.doccle.callbacks import certificate_is_allowed
from xarta.services.v1.doccle.models import DoccleReceiver
from xarta.services.v1.doccle.models import DoccleReceiverProfile

if TYPE_CHECKING:
    from sanic import Request


MAX_CONTROL_BODY_BYTES = 32 * 1024
MAX_CALLBACK_BODY_BYTES = 64 * 1024


def bearer_is_authorized(header: str | None, expected_token: str) -> bool:
    if not header or not header.startswith("Bearer ") or not expected_token:
        return False
    return hmac.compare_digest(header[7:], expected_token)


def parse_json_object(body: bytes, *, limit: int) -> dict[str, Any]:
    if not body or len(body) > limit:
        raise ValueError("Request body is empty or exceeds its size limit")
    try:
        value = orjson.loads(body)
    except orjson.JSONDecodeError as ex:
        raise ValueError("Request body is not valid JSON") from ex
    if not isinstance(value, dict):
        raise ValueError("Request body must be a JSON object")
    return value


def parse_profile(value: object) -> DoccleReceiverProfile:
    if not isinstance(value, dict):
        raise ValueError("profile must be an object")
    allowed = {"label", "first_name", "last_name", "email", "language"}
    if set(value) - allowed:
        raise ValueError("profile contains unsupported fields")
    return DoccleReceiverProfile(**value)


def receiver_dict(receiver: DoccleReceiver) -> dict[str, Any]:
    return {
        "id": str(receiver.id),
        "subject": receiver.subject,
        "external_receiver_id": receiver.external_receiver_id,
        "state": receiver.state.value,
        "linked": receiver.linked,
        "error": receiver.error,
        "created_at": receiver.created_at.isoformat(),
        "updated_at": receiver.updated_at.isoformat(),
    }


def require_control_token(request: Request) -> bool:
    return bearer_is_authorized(
        request.headers.get("authorization"), request.app.ctx.doccle_control_plane_token
    )


def forwarded_client_certificate(value: str | None) -> bytes | None:
    """Decode a client certificate forwarded by the trusted TLS ingress."""
    if not value:
        return None
    try:
        return ssl.PEM_cert_to_DER_cert(unquote(value))
    except (ValueError, ssl.SSLError):
        return None


async def resolve_receiver(request: Request):
    if not require_control_token(request):
        return response.json({"error": "unauthorized"}, status=401)
    try:
        payload = parse_json_object(request.body, limit=MAX_CONTROL_BODY_BYTES)
        if set(payload) != {"subject"}:
            raise ValueError("resolve requires exactly subject")
        if not isinstance(payload["subject"], dict):
            raise ValueError("invalid subject")
        receiver = await request.app.ctx.doccle_receiver_service.resolve(
            payload["subject"]
        )
    except (TypeError, ValueError) as ex:
        return response.json({"error": str(ex)}, status=400)
    if receiver is None:
        return response.json({"error": "receiver not found"}, status=404)
    return response.json(receiver_dict(receiver))


async def ensure_receiver(request: Request):
    if not require_control_token(request):
        return response.json({"error": "unauthorized"}, status=401)
    try:
        payload = parse_json_object(request.body, limit=MAX_CONTROL_BODY_BYTES)
        if set(payload) != {"subject", "profile"}:
            raise ValueError("ensure requires exactly subject and profile")
        if not isinstance(payload["subject"], dict):
            raise ValueError("invalid subject")
        receiver = await request.app.ctx.doccle_receiver_service.ensure(
            payload["subject"], parse_profile(payload["profile"])
        )
    except (TypeError, ValueError) as ex:
        return response.json({"error": str(ex)}, status=400)
    return response.json(receiver_dict(receiver))


async def get_receiver(request: Request, receiver_id: str):
    if not require_control_token(request):
        return response.json({"error": "unauthorized"}, status=401)
    try:
        identifier = UUID(receiver_id)
    except ValueError:
        return response.json({"error": "invalid receiver ID"}, status=400)
    receiver = await request.app.ctx.doccle_receiver_repository.load(identifier)
    if receiver is None:
        return response.json({"error": "receiver not found"}, status=404)
    return response.json(receiver_dict(receiver))


async def receiver_callback(request: Request):
    if not request.app.ctx.doccle_callbacks_enabled:
        return response.json({"error": "not found"}, status=404)
    transport = request.transport
    certificate = transport.get_extra_info("peercert_binary") if transport else None
    if certificate is None and transport:
        ssl_object = transport.get_extra_info("ssl_object")
        certificate = ssl_object.getpeercert(binary_form=True) if ssl_object else None
    if certificate is None:
        certificate = forwarded_client_certificate(
            request.headers.get("ssl-client-cert")
        )
    if not certificate_is_allowed(
        certificate, request.app.ctx.doccle_callback_certificate_fingerprints
    ):
        return response.json({"error": "unauthorized"}, status=401)
    try:
        payload = parse_json_object(request.body, limit=MAX_CALLBACK_BODY_BYTES)
        result = await apply_receiver_link_callback(
            payload,
            components=request.app.ctx.doccle_components,
            repository=request.app.ctx.doccle_receiver_repository,
        )
    except ValueError as ex:
        return response.json({"error": str(ex)}, status=400)
    except LookupError as ex:
        return response.json({"error": str(ex)}, status=404)
    return response.json(
        {"receiver_id": str(result.receiver.id), "duplicate": result.duplicate}
    )
