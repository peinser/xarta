from __future__ import annotations

import hashlib
import re

from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING
from typing import cast
from uuid import UUID

import orjson

from xarta.exceptions.protocol import PermanentError
from xarta.exceptions.protocol import TemporaryError
from xarta.logging import log_capability_adapter_selected
from xarta.nats.constants import STREAM_DOCUMENTS
from xarta.nats.sanic import SanicNATSConsumerModel
from xarta.nats.sanic import SanicNATSSynchronousRequestsConsumerModel
from xarta.protocol.dag import CapabilityResult
from xarta.protocol.dag import NodeTask
from xarta.protocol.dag import OutcomeEmission
from xarta.protocol.dag import OutcomeSubject
from xarta.protocol.dag.search import SearchIndexNode
from xarta.protocol.dag.search import SearchIndexOutcome
from xarta.protocol.document.source import ArchiveDocumentSource
from xarta.services.v1.search.adapters import SearchTransportError
from xarta.services.v1.search.models import ArchiveSearchSource
from xarta.services.v1.search.models import DocumentSearchSource
from xarta.services.v1.search.models import SearchableDocument
from xarta.services.v1.search.models import SearchableRepresentation
from xarta.tracking import DestinationRegistry

if TYPE_CHECKING:
    from collections.abc import Mapping
    from typing import Any

    from xarta.adapters import AdapterRegistry
    from xarta.services.v1.search.adapters import SearchAdapter


@dataclass(frozen=True)
class SearchComponents:
    destinations: DestinationRegistry
    adapters: AdapterRegistry[SearchAdapter]


def build_search_components(
    configurations: Mapping[str, Mapping[str, Any]],
    adapters: AdapterRegistry[SearchAdapter],
) -> SearchComponents:
    destinations = DestinationRegistry(configurations, require_explicit_revisions=True)
    for resolved in destinations.iter_resolved():
        if resolved.binding.capability != "search-index":
            raise ValueError(
                f"Destination {resolved.binding.destination} is not valid for search-index"
            )
        adapters.validate(resolved.binding.adapter, resolved.configuration)
    return SearchComponents(destinations, adapters)


async def _worker(
    node: SearchIndexNode,
    task: NodeTask,
    *,
    components: SearchComponents,
    **kwargs,
) -> CapabilityResult:
    resolved = components.destinations.resolve("search-index", node.destination)
    source = node.interpret()
    if isinstance(source, ArchiveDocumentSource):
        version = await source.details()
        digest = None
        content_type = None
        document_type = getattr(version.document_type, "value", None)
        metadata = version.metadata
        default_representation_id = version.default_representation_id
        representations = tuple(
            SearchableRepresentation(
                representation_id=item.representation_id,
                name=item.name,
                content_type=item.content_type,
                metadata=item.metadata,
            )
            for item in version.representations
        )
    else:
        document = await source.retrieve()
        digest = hashlib.sha256(document.data).hexdigest()
        content_type = document.content_type
        document_type = getattr(document.document_type, "value", None)
        metadata = document.metadata or {}
        default_representation_id = None
        representations = ()
    source_version = (
        getattr(source, "resolved_version", None)
        or node.document.get("version")
        or digest
    )
    archive_source = cast(ArchiveDocumentSource, source)
    searchable_source = (
        ArchiveSearchSource(
            archive=archive_source.archive,
            document_id=source.id,
            version_id=UUID(str(source_version)),
        )
        if source.source == "archive"
        else DocumentSearchSource(
            kind=source.source,
            document_id=source.id,
            version=str(source_version),
        )
    )
    searchable = SearchableDocument(
        source=searchable_source,
        projection_revision=resolved.binding.configuration_revision,
        digest=digest,
        content_type=content_type,
        document_type=document_type,
        metadata=metadata,
        default_representation_id=default_representation_id,
        representations=representations,
    )
    adapter = components.adapters.create(
        resolved.binding.adapter, resolved.configuration
    )
    log = kwargs.get("logger")
    if log is not None:
        await log_capability_adapter_selected(log, resolved.binding, "synchronous")
    try:
        submission = await adapter.index(
            idempotency_key=str(task.node_execution_id), document=searchable
        )
    except SearchTransportError as ex:
        raise TemporaryError("Search index is temporarily unavailable", delay=5) from ex
    if submission.outcome not in {outcome.value for outcome in SearchIndexOutcome}:
        raise ValueError(
            f"Search adapter returned unsupported outcome: {submission.outcome}"
        )
    return CapabilityResult(
        (
            OutcomeEmission(
                outcome=submission.outcome,
                subject=OutcomeSubject(kind="document", id=str(node.document["id"])),
                details=dict(submission.details or {}) or None,
            ),
        )
    )


class SearchNATSModel(SanicNATSSynchronousRequestsConsumerModel):
    @classmethod
    async def register(  # type: ignore[override]
        cls, app, components: SearchComponents, **kwargs
    ) -> None:
        await super().register(
            app=app,
            fn=partial(_worker, components=components),
            name=SearchIndexNode.KIND,
            **kwargs,
        )


_ARCHIVE_EVENTS = {
    "xarta.archive.document.created",
    "xarta.archive.document.version-created",
    "xarta.archive.document.version-content-removed",
    "xarta.archive.document.deletion-requested",
    "xarta.archive.document.deleted",
}
_ARCHIVE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _archive_event(data: bytes) -> tuple[str, str, UUID, UUID | None]:
    try:
        event = orjson.loads(data)
        event_type = event["type"]
        payload = event["data"]
        archive = payload["archive"]
        document_id = UUID(payload["document_id"])
        version_value = payload.get("version_id")
        version_id = UUID(version_value) if version_value is not None else None
    except (KeyError, TypeError, ValueError, orjson.JSONDecodeError) as ex:
        raise PermanentError(
            "Malformed archive lifecycle event",
            classification="malformed",
            error_code="malformed_archive_event",
        ) from ex
    if (
        event_type not in _ARCHIVE_EVENTS
        or not isinstance(archive, str)
        or not _ARCHIVE_NAME.fullmatch(archive)
    ):
        raise PermanentError(
            "Malformed archive lifecycle event",
            classification="malformed",
            error_code="malformed_archive_event",
        )
    if (
        event_type
        in {
            "xarta.archive.document.version-content-removed",
        }
        and version_id is None
    ):
        raise PermanentError(
            "Archive version lifecycle event requires version_id",
            classification="malformed",
            error_code="malformed_archive_event",
        )
    return event_type, archive, document_id, version_id


async def _archive_lifecycle_worker(
    *, msg, components: SearchComponents, **kwargs
) -> None:
    event_type, archive, document_id, version_id = _archive_event(msg.data)
    if event_type in {
        "xarta.archive.document.created",
        "xarta.archive.document.version-created",
    }:
        return

    try:
        for resolved in components.destinations.iter_resolved("search-index"):
            adapter = components.adapters.create(
                resolved.binding.adapter, resolved.configuration
            )
            if event_type == "xarta.archive.document.version-content-removed":
                assert version_id is not None
                await adapter.remove_archive_version(
                    source=ArchiveSearchSource(archive, document_id, version_id)
                )
            else:
                await adapter.remove_archive_document(
                    archive=archive, document_id=document_id
                )
    except SearchTransportError as ex:
        raise TemporaryError("Search index is temporarily unavailable", delay=5) from ex


class SearchArchiveLifecycleNATSModel(SanicNATSConsumerModel):
    @classmethod
    async def register(  # type: ignore[override]
        cls, app, components: SearchComponents, **kwargs
    ) -> None:
        await super().register(
            app=app,
            stream=STREAM_DOCUMENTS,
            subject="documents.archive.>",
            durable="search-archive-lifecycle",
            fn=partial(_archive_lifecycle_worker, components=components),
            **kwargs,
        )
