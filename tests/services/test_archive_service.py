from __future__ import annotations

import asyncio
import datetime

from dataclasses import replace
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock
from uuid import uuid4

import orjson
import pytest

from aiohttp import ClientConnectionError
from aiohttp import ClientPayloadError
from aiohttp import ClientSession
from aiohttp import ServerDisconnectedError

from xarta.adapters import AdapterRegistry
from xarta.adapters import UnknownAdapterError
from xarta.exceptions.protocol import TemporaryError
from xarta.protocol.dag import NodeTask
from xarta.protocol.dag.archive import ArchiveNode
from xarta.services.v1.archive.adapters import ArchiveRepresentationSubmission
from xarta.services.v1.archive.adapters import ArchiveSubmission
from xarta.services.v1.archive.adapters import ArchiveTransportError
from xarta.services.v1.archive.adapters import ArchiveVersionSubmission
from xarta.services.v1.archive.adapters import XartaHTTPArchiveAdapter
from xarta.services.v1.archive.adapters import XartaHTTPArchiveAdapterFactory
from xarta.services.v1.archive.base import ArchiveComponents
from xarta.services.v1.archive.base import create_archive_components
from xarta.services.v1.archive.nats import _submission_result
from xarta.services.v1.archive.nats import _worker
from xarta.tracking import DestinationRegistry


def document():
    return SimpleNamespace(
        id=uuid4(),
        document_type=SimpleNamespace(value="invoice"),
        metadata={"customer": "123"},
        content_type="application/pdf",
        data=b"pdf",
        created=datetime.datetime(2029, 1, 1, tzinfo=datetime.UTC),
    )


def archive_item(archived_document):
    representation_id = uuid4()
    return ArchiveVersionSubmission(
        archive="payroll",
        document_id=archived_document.id,
        version_id=uuid4(),
        parent_version_id=None,
        created=archived_document.created,
        expires=None,
        document_type=archived_document.document_type.value,
        metadata=archived_document.metadata,
        default_representation_id=representation_id,
        representations=(
            ArchiveRepresentationSubmission(
                representation_id=representation_id,
                content_type=archived_document.content_type,
                data=archived_document.data,
                name=None,
                metadata={},
            ),
        ),
    )


def test_archive_components_reject_unknown_adapter_at_startup() -> None:
    with pytest.raises(UnknownAdapterError, match="missing-archive-adapter"):
        create_archive_components(
            {
                "default-archive": {
                    "kind": "archive",
                    "adapter": "missing-archive-adapter",
                    "current_revision": "v1",
                    "revisions": {
                        "v1": {
                            "endpoint": "https://archive.example.test",
                            "timeout": 5,
                        }
                    },
                }
            }
        )


def test_archive_components_require_explicit_revisions() -> None:
    with pytest.raises(ValueError, match="explicit current_revision and revisions"):
        create_archive_components(
            {
                "default-archive": {
                    "kind": "archive",
                    "adapter": "xarta-http-archive",
                    "endpoint": "https://archive.example.test",
                    "timeout": 5,
                }
            }
        )


def test_archive_components_validate_retained_revisions() -> None:
    with pytest.raises(ValueError, match="non-empty endpoint"):
        create_archive_components(
            {
                "default-archive": {
                    "kind": "archive",
                    "adapter": "xarta-http-archive",
                    "current_revision": "v2",
                    "revisions": {
                        "v1": {"endpoint": "", "timeout": 5},
                        "v2": {
                            "endpoint": "https://archive.example.test",
                            "timeout": 5,
                        },
                    },
                }
            }
        )


@pytest.mark.asyncio
async def test_xarta_http_adapter_constructs_archive_request() -> None:
    captured: dict = {}
    archived_document = document()

    class Response:
        status = 200

        async def read(self):
            return orjson.dumps(
                {
                    "document_id": str(archived_document.id),
                    "version_id": str(version_id),
                    "outcome": "created",
                }
            )

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    class Session:
        def put(self, url, **kwargs):
            captured.update(url=url, **kwargs)
            return Response()

    adapter = XartaHTTPArchiveAdapter(
        {"endpoint": "https://archive.example.test/", "timeout": 7},
        Session(),  # type: ignore[arg-type]
    )
    expire = datetime.datetime(2030, 1, 1, tzinfo=datetime.UTC)
    version_id = uuid4()
    item = replace(
        archive_item(archived_document), version_id=version_id, expires=expire
    )

    submission = await adapter.submit(
        idempotency_key="operation-1",
        versions=[item],
    )

    fields = {field[0]["name"]: field[2] for field in captured["data"]._fields}
    assert captured["url"] == "https://archive.example.test/archives/payroll/documents"
    assert captured["timeout"].total == 7
    assert captured["headers"] == {
        "Idempotency-Key": f"operation-1:{archived_document.id}:{version_id}"
    }
    manifest = orjson.loads(fields.pop("manifest"))
    assert fields == {"representation-0": b"pdf"}
    assert manifest == {
        "document_id": str(archived_document.id),
        "version_id": str(version_id),
        "parent_version_id": None,
        "created": archived_document.created.isoformat(),
        "expires": expire.isoformat(),
        "document_type": "invoice",
        "metadata": {"customer": "123"},
        "default_representation_id": str(item.default_representation_id),
        "representations": [
            {
                "representation_id": str(item.default_representation_id),
                "name": None,
                "content_type": "application/pdf",
                "metadata": {},
                "field": "representation-0",
            }
        ],
    }
    assert "destination" not in fields
    assert submission == ArchiveSubmission(
        "operation-1",
        {
            "documents": "stored",
            "outcomes": ["created"],
            "results": [
                {
                    "document_id": str(archived_document.id),
                    "version_id": str(version_id),
                    "outcome": "created",
                }
            ],
            "errors": [],
        },
    )


@pytest.mark.asyncio
async def test_xarta_http_adapter_bounds_independent_documents_and_preserves_order(
    monkeypatch,
) -> None:
    first = archive_item(document())
    second = archive_item(document())
    first_successor = replace(first, version_id=uuid4())
    third = archive_item(document())
    versions = [first, second, first_successor, third]
    active = 0
    maximum = 0
    calls = []

    async def submit_version(_self, _session, _key, version):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        calls.append(("start", version.version_id))
        await asyncio.sleep(0.01 if version is first else 0)
        calls.append(("complete", version.version_id))
        active -= 1
        return {
            "document_id": str(version.document_id),
            "version_id": str(version.version_id),
            "outcome": "created",
        }, None

    monkeypatch.setattr(XartaHTTPArchiveAdapter, "_submit_version", submit_version)
    adapter = XartaHTTPArchiveAdapter(
        {
            "endpoint": "https://archive.example.test",
            "timeout": 7,
            "concurrency": 2,
        },
        SimpleNamespace(),  # type: ignore[arg-type]
    )

    submission = await adapter.submit(idempotency_key="operation-1", versions=versions)

    assert maximum == 2
    assert [result["version_id"] for result in submission.state["results"]] == [
        str(version.version_id) for version in versions
    ]
    assert calls.index(("complete", first.version_id)) < calls.index(
        ("start", first_successor.version_id)
    )


def test_xarta_http_adapter_requires_positive_concurrency() -> None:
    with pytest.raises(ValueError, match="concurrency must be positive"):
        XartaHTTPArchiveAdapterFactory.validate(
            {
                "endpoint": "https://archive.example.test",
                "timeout": 7,
                "concurrency": 0,
            }
        )


@pytest.mark.asyncio
async def test_xarta_http_adapter_maps_semantic_conflicts() -> None:
    archived_document = document()
    version_id = uuid4()

    class Response:
        status = 409

        async def read(self):
            return orjson.dumps(
                {"outcome": "version_conflict", "error": "stale predecessor"}
            )

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    class Session:
        def put(self, *_args, **_kwargs):
            return Response()

    adapter = XartaHTTPArchiveAdapter(
        {"endpoint": "https://archive.example.test", "timeout": 7},
        Session(),  # type: ignore[arg-type]
    )
    submission = await adapter.submit(
        idempotency_key="operation-1",
        versions=[
            replace(
                archive_item(archived_document),
                version_id=version_id,
                parent_version_id=uuid4(),
            )
        ],
    )
    assert submission.state["documents"] == "rejected"
    assert submission.state["outcomes"] == ["version_conflict"]
    assert submission.state["results"] == [
        {
            "document_id": str(archived_document.id),
            "version_id": str(version_id),
            "outcome": "version_conflict",
        }
    ]
    assert submission.state["errors"] == [
        {
            "document_id": str(archived_document.id),
            "version_id": str(version_id),
            "outcome": "version_conflict",
            "error": "stale predecessor",
        }
    ]


@pytest.mark.asyncio
async def test_xarta_http_adapter_accepts_resolved_version_for_unchanged() -> None:
    archived_document = document()
    requested_version_id = uuid4()
    current_version_id = uuid4()

    class Response:
        status = 200

        async def read(self):
            return orjson.dumps(
                {
                    "document_id": str(archived_document.id),
                    "version_id": str(current_version_id),
                    "outcome": "unchanged",
                }
            )

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    adapter = XartaHTTPArchiveAdapter(
        {"endpoint": "https://archive.example.test", "timeout": 7},
        SimpleNamespace(put=lambda *_args, **_kwargs: Response()),  # type: ignore[arg-type]
    )

    submission = await adapter.submit(
        idempotency_key="operation-1",
        versions=[
            replace(archive_item(archived_document), version_id=requested_version_id)
        ],
    )

    assert submission.state["results"] == [
        {
            "document_id": str(archived_document.id),
            "version_id": str(current_version_id),
            "outcome": "unchanged",
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [408, 425, 429, 500, 503])
async def test_xarta_http_adapter_classifies_retryable_statuses(status) -> None:
    class Response:
        def __init__(self, response_status):
            self.status = response_status

        async def read(self):
            return b"temporary"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    response = Response(status)
    session = SimpleNamespace(put=lambda *_args, **_kwargs: response)
    adapter = XartaHTTPArchiveAdapter(
        {"endpoint": "https://archive.example.test", "timeout": 7},
        cast("ClientSession", session),
    )

    with pytest.raises(ArchiveTransportError):
        await adapter.submit(
            idempotency_key="operation-1",
            versions=[archive_item(document())],
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error", [ClientConnectionError("unavailable"), TimeoutError()]
)
async def test_xarta_http_adapter_classifies_transport_errors(error) -> None:
    def fail(*_args, **_kwargs):
        raise error

    adapter = XartaHTTPArchiveAdapter(
        {"endpoint": "https://archive.example.test", "timeout": 7},
        cast("ClientSession", SimpleNamespace(put=fail)),
    )

    with pytest.raises(ArchiveTransportError):
        await adapter.submit(
            idempotency_key="operation-1", versions=[archive_item(document())]
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error", [ClientPayloadError("truncated body"), ServerDisconnectedError()]
)
async def test_xarta_http_adapter_classifies_response_body_transport_errors(
    error,
) -> None:
    class Response:
        status = 200

        async def read(self):
            raise error

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    adapter = XartaHTTPArchiveAdapter(
        {"endpoint": "https://archive.example.test", "timeout": 7},
        cast(
            "ClientSession",
            SimpleNamespace(put=lambda *_args, **_kwargs: Response()),
        ),
    )

    with pytest.raises(ArchiveTransportError):
        await adapter.submit(
            idempotency_key="operation-1", versions=[archive_item(document())]
        )


@pytest.mark.asyncio
async def test_xarta_http_adapter_rejects_malformed_success_permanently() -> None:
    class Response:
        status = 200

        async def read(self):
            return b"not-json"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    adapter = XartaHTTPArchiveAdapter(
        {"endpoint": "https://archive.example.test", "timeout": 7},
        cast(
            "ClientSession",
            SimpleNamespace(put=lambda *_args, **_kwargs: Response()),
        ),
    )

    with pytest.raises(ValueError, match="valid JSON"):
        await adapter.submit(
            idempotency_key="operation-1", versions=[archive_item(document())]
        )


class RecordingFactory:
    def __init__(self) -> None:
        self.configurations: list[dict] = []
        self.fail = False

    def validate(self, configuration) -> None:
        pass

    def create(self, configuration):
        self.configurations.append(dict(configuration))
        factory = self

        class Adapter:
            async def submit(self, **kwargs):
                if factory.fail:
                    raise RuntimeError("submission failed")
                return ArchiveSubmission("archive-reference", {"documents": "stored"})

        return Adapter()


def components(configurations, factories) -> ArchiveComponents:
    return ArchiveComponents(
        destinations=DestinationRegistry(configurations),
        adapters=AdapterRegistry(factories),
    )


def archive_task(monkeypatch, destination="archive-destination"):
    archived_document = document()
    node = ArchiveNode(
        destination=destination,
        documents=[
            {
                "metadata": {"explicit": True},
                "document_type": "invoice",
                "representations": [
                    {
                        "source": {
                            "source": "generate",
                            "id": str(archived_document.id),
                        }
                    }
                ],
            }
        ],
    )
    monkeypatch.setattr(
        node.documents[0].representations[0].source,
        "retrieve",
        AsyncMock(return_value=archived_document),
    )
    return node, NodeTask(flow_id=uuid4(), node=node)


@pytest.mark.asyncio
async def test_archive_worker_selects_destination_adapter(monkeypatch) -> None:
    selected = RecordingFactory()
    unused = RecordingFactory()
    archive_components = components(
        {
            "archive-destination": {
                "kind": "archive",
                "adapter": "selected",
                "revision": "v1",
            }
        },
        {"selected": selected, "unused": unused},
    )
    node, task = archive_task(monkeypatch)
    log = SimpleNamespace(ainfo=AsyncMock())
    result = await _worker(node, task, archive_components, logger=log)

    assert result.outcomes_persisted is False
    assert selected.configurations[0]["revision"] == "v1"
    assert unused.configurations == []
    assert [outcome.outcome for outcome in result.outcomes] == ["success"]
    log.ainfo.assert_awaited_once_with(
        "capability_adapter_selected",
        capability="archive",
        adapter="selected",
        configuration_revision="v1",
        execution_mode="synchronous",
    )


@pytest.mark.asyncio
async def test_archive_worker_resolves_current_destination_revision(
    monkeypatch,
) -> None:
    factory = RecordingFactory()
    factory.fail = True
    node, task = archive_task(monkeypatch)
    original = components(
        {
            "archive-destination": {
                "kind": "archive",
                "adapter": "recording",
                "revision": "v1",
                "endpoint": "https://v1.example.test",
            }
        },
        {"recording": factory},
    )

    with pytest.raises(RuntimeError, match="submission failed"):
        await _worker(node, task, original)

    factory.fail = False
    revised = components(
        {
            "archive-destination": {
                "kind": "archive",
                "adapter": "recording",
                "current_revision": "v2",
                "revisions": {
                    "v1": {"endpoint": "https://v1.example.test"},
                    "v2": {"endpoint": "https://v2.example.test"},
                },
            }
        },
        {"recording": factory},
    )

    await _worker(node, task, revised)

    assert factory.configurations[-1]["revision"] == "v2"
    assert factory.configurations[-1]["endpoint"] == "https://v2.example.test"


def test_archive_reduction_preserves_document_and_aggregate_failure_outcomes() -> None:
    result = _submission_result(
        {
            "results": [
                {
                    "document_id": "document-1",
                    "version_id": "version-1",
                    "outcome": "created",
                },
                {
                    "document_id": "document-2",
                    "version_id": "version-2",
                    "outcome": "version_conflict",
                },
            ],
            "errors": [{"document_id": "document-2"}],
        }
    )

    assert [outcome.outcome for outcome in result.outcomes] == [
        "created",
        "version_conflict",
        "failure",
    ]
    assert [outcome.subject.id for outcome in result.outcomes if outcome.subject] == [
        "document-1",
        "document-2",
    ]


@pytest.mark.asyncio
async def test_archive_worker_maps_transport_failure_to_temporary_error(
    monkeypatch,
) -> None:
    class Factory(RecordingFactory):
        def create(self, configuration):
            class Adapter:
                async def submit(self, **kwargs):
                    raise ArchiveTransportError("unavailable")

            return Adapter()

    node, task = archive_task(monkeypatch)
    archive_components = components(
        {
            "archive-destination": {
                "kind": "archive",
                "adapter": "recording",
                "revision": "v1",
            }
        },
        {"recording": Factory()},
    )

    with pytest.raises(TemporaryError, match="temporarily unavailable"):
        await _worker(node, task, archive_components)
