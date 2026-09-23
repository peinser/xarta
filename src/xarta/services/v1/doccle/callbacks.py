from __future__ import annotations

import hashlib
import hmac
import re

from typing import TYPE_CHECKING
from typing import Any

import orjson

if TYPE_CHECKING:
    from collections.abc import Iterable
    from collections.abc import Mapping

    from xarta.services.v1.doccle.models import CallbackResult
    from xarta.services.v1.doccle.repositories import ReceiverRepository
    from xarta.services.v1.doccle.service import DoccleComponents


class CallbackValidationError(ValueError):
    pass


def parse_certificate_fingerprints(value: str) -> frozenset[str]:
    fingerprints = set()
    for item in value.split(","):
        normalized = item.strip().lower().replace(":", "")
        if not re.fullmatch(r"[0-9a-f]{64}", normalized):
            raise ValueError("Doccle callback fingerprints must be SHA-256 hex values")
        fingerprints.add(normalized)
    if not fingerprints:
        raise ValueError("At least one Doccle callback certificate is required")
    return frozenset(fingerprints)


def certificate_is_allowed(
    peer_certificate: bytes | None, allowed_fingerprints: Iterable[str]
) -> bool:
    if not peer_certificate:
        return False
    actual = hashlib.sha256(peer_certificate).hexdigest()
    return any(
        hmac.compare_digest(actual, expected) for expected in allowed_fingerprints
    )


def callback_identity(payload: Mapping[str, Any]) -> str:
    canonical = orjson.dumps(payload, option=orjson.OPT_SORT_KEYS)
    return hashlib.sha256(canonical).hexdigest()


def callback_fields(payload: Mapping[str, Any]) -> tuple[str, str, bool]:
    event_type = payload.get(
        "event_type", payload.get("eventType", payload.get("event"))
    )
    if event_type != "RECEIVER_LINK_UNLINK":
        raise CallbackValidationError("Unsupported Doccle callback event")
    sender_name = payload.get("sender_name", payload.get("senderName"))
    receiver_id = payload.get(
        "receiverExternalReferenceId",
        payload.get(
            "external_receiver_id",
            payload.get("receiver_id", payload.get("receiverId")),
        ),
    )
    linked = payload.get("linked")
    if not isinstance(sender_name, str) or not sender_name:
        raise CallbackValidationError("Doccle callback sender_name is required")
    if not isinstance(receiver_id, str) or not receiver_id:
        raise CallbackValidationError("Doccle callback receiver ID is required")
    if not isinstance(linked, bool):
        raise CallbackValidationError("Doccle callback linked must be a boolean")
    return sender_name, receiver_id, linked


def destination_for_sender(components: DoccleComponents, sender_name: str) -> str:
    matches: set[str] = set()
    for resolved in components.destinations.iter_resolved("doccle"):
        # Retained revisions can still have provisioned Receivers and in-flight
        # callbacks. Correlate by every retained provider sender identity, not
        # only the revision currently selected for new work.
        if resolved.configuration.get("sender_name") == sender_name:
            matches.add(resolved.binding.destination)
    if len(matches) != 1:
        raise CallbackValidationError("Unknown or ambiguous Doccle callback sender")
    return matches.pop()


async def apply_receiver_link_callback(
    payload: Mapping[str, Any],
    *,
    components: DoccleComponents,
    repository: ReceiverRepository,
) -> CallbackResult:
    sender_name, external_receiver_id, linked = callback_fields(payload)
    destination = destination_for_sender(components, sender_name)
    receiver = await repository.load_by_external_id(destination, external_receiver_id)
    if receiver is None:
        raise LookupError("Unknown Doccle callback receiver")
    return await repository.apply_callback(
        destination=destination,
        callback_identity=callback_identity(payload),
        receiver_id=receiver.id,
        external_receiver_id=external_receiver_id,
        linked=linked,
        payload=dict(payload),
    )
