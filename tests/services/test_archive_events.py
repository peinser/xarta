from __future__ import annotations

import datetime

from unittest.mock import AsyncMock
from uuid import uuid4

import orjson
import pytest

from xarta.services.v1.archive.events import archive_event_id
from xarta.services.v1.archive.events import publish_archive_event


@pytest.mark.asyncio
async def test_direct_event_is_deterministic_cloudevent_without_storage_coordinates() -> (
    None
):
    jetstream = AsyncMock()
    document_id = uuid4()
    version_id = uuid4()
    representation_id = uuid4()
    occurred_at = datetime.datetime(2030, 1, 1, tzinfo=datetime.UTC)

    first = await publish_archive_event(
        jetstream,
        event_type="xarta.archive.document.created",
        archive="payroll.eu",
        document_id=document_id,
        aggregate_id=7,
        version_internal_id=11,
        version_id=version_id,
        outcome="created",
        document_type="payslip",
        metadata={"employee": "123"},
        default_representation_id=representation_id,
        representations=[
            {
                "representation_id": str(representation_id),
                "name": "payslip.pdf",
                "content_type": "application/pdf",
                "metadata": {"generator": "renderer"},
                "checksum": {"sha512": "ab" * 64},
                "size": 10,
                "state": "available",
                "storage_key": "must-not-leak",
            }
        ],
        occurred_at=occurred_at,
    )
    second = await publish_archive_event(
        jetstream,
        event_type="xarta.archive.document.created",
        archive="payroll.eu",
        document_id=document_id,
        aggregate_id=7,
        version_internal_id=11,
        version_id=version_id,
        document_type="payslip",
        metadata={"employee": "123"},
        default_representation_id=representation_id,
        representations=[
            {
                "representation_id": str(representation_id),
                "content_type": "application/pdf",
                "metadata": {},
                "checksum": {"sha512": "ab" * 64},
                "size": 10,
            }
        ],
        occurred_at=occurred_at,
    )

    assert (
        first
        == second
        == archive_event_id(
            "xarta.archive.document.created", "payroll.eu", document_id, 7, 11
        )
    )
    call = jetstream.publish.await_args_list[0]
    payload = orjson.loads(call.args[1])
    assert call.args[0].startswith("documents.archive.cGF5cm9sbC5ldQ.")
    assert call.kwargs["headers"] == {"Nats-Msg-Id": str(first)}
    assert payload["specversion"] == "1.0"
    assert payload["id"] == str(first)
    assert payload["time"] == "2030-01-01T00:00:00Z"
    assert payload["data"]["default_representation_id"] == str(representation_id)
    assert payload["data"]["representations"][0]["checksum"] == {"sha512": "ab" * 64}
    serialized = orjson.dumps(payload)
    for private_field in (b"storage_key", b"backend", b"credential", b"physical_path"):
        assert private_field not in serialized
    assert (
        archive_event_id(
            "xarta.archive.document.created", "payroll.eu", document_id, 8, 12
        )
        != first
    )


@pytest.mark.asyncio
async def test_direct_publication_failure_propagates_for_retry() -> None:
    error = RuntimeError("NATS unavailable")
    jetstream = AsyncMock()
    jetstream.publish.side_effect = error

    with pytest.raises(RuntimeError, match="NATS unavailable"):
        await publish_archive_event(
            jetstream,
            event_type="xarta.archive.document.deleted",
            archive="payroll",
            document_id=uuid4(),
            aggregate_id=7,
            version_internal_id=11,
        )


@pytest.mark.asyncio
async def test_mutable_metadata_event_is_removed() -> None:
    jetstream = AsyncMock()
    document_id = uuid4()
    version_id = uuid4()

    with pytest.raises(ValueError, match="Unsupported archive event type"):
        await publish_archive_event(
            jetstream,
            event_type="xarta.archive.document.metadata-updated",
            archive="payroll",
            document_id=document_id,
            aggregate_id=7,
            version_internal_id=11,
            version_id=version_id,
        )
    jetstream.publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_unsupported_event_is_rejected_before_publication() -> None:
    jetstream = AsyncMock()
    with pytest.raises(ValueError, match="Unsupported archive event type"):
        await publish_archive_event(
            jetstream,
            event_type="xarta.archive.document.unknown",
            archive="payroll",
            document_id=uuid4(),
            aggregate_id=7,
            version_internal_id=11,
        )
    jetstream.publish.assert_not_awaited()
