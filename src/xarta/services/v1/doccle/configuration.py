from __future__ import annotations

from collections.abc import Mapping
from math import isfinite
from typing import Any
from urllib.parse import urlsplit

import aiofiles  # type: ignore[import-untyped]
import orjson

ADAPTER_NAME = "doccle-sender-rest"


def validate_doccle_configuration(configuration: Mapping[str, Any]) -> None:
    if configuration.get("adapter") not in (None, ADAPTER_NAME):
        raise ValueError(f"Doccle destination adapter must be {ADAPTER_NAME}")

    for field in ("sender_name", "endpoint"):
        value = configuration.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{ADAPTER_NAME} requires a non-empty {field}")

    endpoint = str(configuration["endpoint"])
    parsed_endpoint = urlsplit(endpoint)
    if parsed_endpoint.scheme != "https" or not parsed_endpoint.netloc:
        raise ValueError(f"{ADAPTER_NAME} endpoint must be an absolute HTTPS URL")
    if parsed_endpoint.username or parsed_endpoint.password:
        raise ValueError(f"{ADAPTER_NAME} endpoint must not contain credentials")

    for field in ("receiver_path", "document_path"):
        path = configuration.get(field)
        if path is not None and (
            not isinstance(path, str)
            or not path.startswith("/")
            or "{sender_name}" not in path
            or "{receiver_id}" not in path
        ):
            raise ValueError(
                f"{ADAPTER_NAME} {field} must be an absolute path containing "
                "{sender_name} and {receiver_id}"
            )
    document_path = configuration.get(
        "document_path",
        "/senders/{sender_name}/receivers/{receiver_id}/documents/{document_id}",
    )
    if "{document_id}" not in document_path:
        raise ValueError(f"{ADAPTER_NAME} document_path must contain {{document_id}}")

    timeout = configuration.get("timeout")
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, int | float)
        or not isfinite(float(timeout))
        or timeout <= 0
    ):
        raise ValueError(f"{ADAPTER_NAME} timeout must be a positive number")

    credentials = configuration.get("credentials")
    if not isinstance(credentials, Mapping):
        raise ValueError(f"{ADAPTER_NAME} requires a credentials object")
    for field in ("token_url", "client_id", "client_secret"):
        credential = credentials.get(field)
        if not isinstance(credential, str) or not credential:
            raise ValueError(f"{ADAPTER_NAME} credentials requires {field}")
    token_url = urlsplit(str(credentials["token_url"]))
    if token_url.scheme != "https" or not token_url.netloc:
        raise ValueError(f"{ADAPTER_NAME} token_url must be an absolute HTTPS URL")
    scope = credentials.get("scope")
    if scope is not None and (not isinstance(scope, str) or not scope):
        raise ValueError(f"{ADAPTER_NAME} credentials scope must be non-empty")

    document_types = configuration.get("document_types")
    if not isinstance(document_types, Mapping) or not document_types:
        raise ValueError(f"{ADAPTER_NAME} document_types must be a non-empty object")
    if any(
        not isinstance(source, str)
        or not source
        or not isinstance(destination, str)
        or not destination
        for source, destination in document_types.items()
    ):
        raise ValueError(
            f"{ADAPTER_NAME} document_types keys and values must be non-empty strings"
        )

    if "response_statuses" in configuration:
        raise ValueError(
            f"{ADAPTER_NAME} response_statuses is obsolete; HTTP and Doccle error contracts are fixed"
        )

    max_response_bytes = configuration.get("max_response_bytes", 1_000_000)
    if (
        isinstance(max_response_bytes, bool)
        or not isinstance(max_response_bytes, int)
        or max_response_bytes < 1
    ):
        raise ValueError(
            f"{ADAPTER_NAME} max_response_bytes must be a positive integer"
        )


def validate_doccle_destinations(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError("Doccle sender configuration must be an object")
    current_sender = value.get("current_sender")
    senders = value.get("senders")
    if not isinstance(current_sender, str) or not current_sender:
        raise ValueError("Doccle configuration requires current_sender")
    if not isinstance(senders, Mapping) or not senders:
        raise ValueError("Doccle configuration requires non-empty senders")
    if current_sender not in senders:
        raise ValueError("Doccle current_sender is unavailable")
    for sender, sender_configuration in senders.items():
        if not isinstance(sender, str) or not sender:
            raise ValueError("Doccle sender names must be non-empty strings")
        if not isinstance(sender_configuration, Mapping):
            raise TypeError(f"Doccle sender {sender} must be an object")
        current_revision = sender_configuration.get("current_revision")
        revisions = sender_configuration.get("revisions")
        if not isinstance(current_revision, str) or not current_revision:
            raise ValueError(f"Doccle sender {sender} requires current_revision")
        if not isinstance(revisions, Mapping) or not revisions:
            raise ValueError(f"Doccle sender {sender} requires non-empty revisions")
        if current_revision not in revisions:
            raise ValueError(f"Doccle sender {sender} current revision is unavailable")
        common = {
            key: item
            for key, item in sender_configuration.items()
            if key not in {"current_revision", "revisions"}
        }
        for revision, revision_configuration in revisions.items():
            if not isinstance(revision, str) or not revision:
                raise ValueError("Doccle revision names must be non-empty strings")
            if not isinstance(revision_configuration, Mapping):
                raise TypeError(
                    f"Doccle revision {sender}@{revision} must be an object"
                )
            validate_doccle_configuration(
                {**common, **dict(revision_configuration), "revision": revision}
            )
    return value


async def load_doccle_destinations(path: str) -> dict[str, Any]:
    async with aiofiles.open(path, "rb") as configuration:
        value = orjson.loads(await configuration.read())
    return validate_doccle_destinations(value)
