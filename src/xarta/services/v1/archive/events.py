from __future__ import annotations

import base64
import datetime

from typing import Any
from uuid import NAMESPACE_URL
from uuid import UUID
from uuid import uuid5

import orjson

from opentelemetry import trace

from xarta.telemetry import nats_producer_span

EVENT_TOKENS = {
    "xarta.archive.document.created": "created",
    "xarta.archive.document.version-created": "version-created",
    "xarta.archive.document.deletion-requested": "deletion-requested",
    "xarta.archive.document.deleted": "deleted",
    "xarta.archive.document.version-content-removed": "version-content-removed",
}

REPRESENTATION_EVENT_FIELDS = {
    "representation_id",
    "name",
    "content_type",
    "metadata",
    "checksum",
    "size",
    "state",
}


def _timestamp(value: datetime.datetime) -> str:
    return value.astimezone(datetime.UTC).isoformat().replace("+00:00", "Z")


def archive_event_id(
    event_type: str,
    archive: str,
    document_id: UUID,
    aggregate_id: int,
    version_internal_id: int,
) -> UUID:
    identity = (
        f"{event_type}:{aggregate_id}:{version_internal_id}:{archive}:{document_id}"
    )
    return uuid5(NAMESPACE_URL, f"urn:xarta:archive:event:{identity}")


def archive_event_subject(event_type: str, archive: str, document_id: UUID) -> str:
    encoded_archive = base64.urlsafe_b64encode(archive.encode()).decode().rstrip("=")
    return (
        f"documents.archive.{encoded_archive}.{document_id}.{EVENT_TOKENS[event_type]}"
    )


async def publish_archive_event(
    jetstream,
    *,
    event_type: str,
    archive: str,
    document_id: UUID,
    aggregate_id: int,
    version_internal_id: int,
    version_id: UUID | None = None,
    parent_version_id: UUID | None = None,
    outcome: str | None = None,
    document_type: str | None = None,
    metadata: dict[str, Any] | None = None,
    default_representation_id: UUID | None = None,
    representations: list[dict[str, Any]] | None = None,
    occurred_at: datetime.datetime | None = None,
) -> UUID:
    if event_type not in EVENT_TOKENS:
        raise ValueError(f"Unsupported archive event type: {event_type}")
    if event_type in {
        "xarta.archive.document.created",
        "xarta.archive.document.version-created",
    } and (
        version_id is None
        or metadata is None
        or default_representation_id is None
        or not representations
    ):
        raise ValueError("Archive version events require a complete snapshot")

    event_id = archive_event_id(
        event_type, archive, document_id, aggregate_id, version_internal_id
    )
    data: dict[str, Any] = {"archive": archive, "document_id": str(document_id)}
    optional = {
        "version_id": str(version_id) if version_id else None,
        "parent_version_id": str(parent_version_id) if parent_version_id else None,
        "outcome": outcome,
        "document_type": document_type,
        "metadata": metadata,
        "default_representation_id": (
            str(default_representation_id) if default_representation_id else None
        ),
        "representations": (
            [
                {
                    key: value
                    for key, value in representation.items()
                    if key in REPRESENTATION_EVENT_FIELDS
                }
                for representation in representations
            ]
            if representations is not None
            else None
        ),
    }
    data.update({key: value for key, value in optional.items() if value is not None})
    payload = {
        "specversion": "1.0",
        "id": str(event_id),
        "type": event_type,
        "source": "urn:xarta:archive",
        "subject": f"{archive}/{document_id}",
        "time": _timestamp(occurred_at or datetime.datetime.now(tz=datetime.UTC)),
        "datacontenttype": "application/json",
        "data": data,
    }
    subject = archive_event_subject(event_type, archive, document_id)
    headers = {"Nats-Msg-Id": str(event_id)}
    with nats_producer_span(subject, headers):
        span = trace.get_current_span()
        span.set_attribute("xarta.archive.name", archive)
        span.set_attribute("xarta.archive.document.id", str(document_id))
        if version_id is not None:
            span.set_attribute("xarta.archive.version.id", str(version_id))
        if default_representation_id is not None:
            span.set_attribute(
                "xarta.archive.representation.id", str(default_representation_id)
            )
        await jetstream.publish(subject, orjson.dumps(payload), headers=headers)
    return event_id
