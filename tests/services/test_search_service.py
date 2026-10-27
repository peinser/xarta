from __future__ import annotations

import asyncio
import hashlib

from contextlib import AbstractAsyncContextManager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import orjson
import pytest

from xarta.adapters import AdapterRegistry
from xarta.exceptions.protocol import ArchiveDocumentNotFound
from xarta.exceptions.protocol import TemporaryError
from xarta.protocol.dag import NodeTask
from xarta.protocol.dag.search import SearchIndexNode
from xarta.protocol.document.archive import ArchiveDocumentVersion
from xarta.protocol.document.archive import ArchiveRepresentation
from xarta.protocol.document.source import ArchiveDocumentSource
from xarta.services.v1.search.adapters import ElasticsearchSearchAdapter
from xarta.services.v1.search.adapters import ElasticsearchSearchAdapterFactory
from xarta.services.v1.search.adapters import PostgreSQLSearchAdapter
from xarta.services.v1.search.adapters import SearchTransportError
from xarta.services.v1.search.models import ArchiveSearchSource
from xarta.services.v1.search.models import DocumentSearchSource
from xarta.services.v1.search.models import IndexSubmission
from xarta.services.v1.search.models import SearchableDocument
from xarta.services.v1.search.models import SearchableRepresentation
from xarta.services.v1.search.nats import _archive_lifecycle_worker
from xarta.services.v1.search.nats import _worker
from xarta.services.v1.search.nats import build_search_components


def configuration() -> dict:
    return {
        "kind": "search-index",
        "adapter": "fake-search",
        "revision": "v1",
        "namespace": "documents",
    }


def revisioned(configuration_value: dict) -> dict:
    return {
        "current_revision": "v1",
        "revisions": {"v1": configuration_value},
    }


def searchable(digest: str | None = None) -> SearchableDocument:
    representation_id = uuid4()
    return SearchableDocument(
        source=ArchiveSearchSource("payroll", uuid4(), uuid4()),
        projection_revision="v1",
        digest=digest,
        content_type=None,
        document_type="invoice",
        metadata={"customer": "123"},
        default_representation_id=representation_id,
        representations=(
            SearchableRepresentation(
                representation_id,
                "invoice.txt",
                "text/plain",
                {"generator": "test"},
            ),
        ),
    )


class AsyncContext(AbstractAsyncContextManager):
    def __init__(self, value) -> None:
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *args):
        return None


class FakeConnection:
    def __init__(self, inserted=None, existing=None) -> None:
        self.inserted = inserted
        self.existing = existing
        self.arguments: tuple | None = None
        self.query: str | None = None

    def transaction(self):
        return AsyncContext(self)

    async def fetchval(self, query, *arguments):
        self.query = query
        self.arguments = arguments
        return self.inserted

    async def fetchrow(self, query, *arguments):
        self.query = query
        return self.existing

    async def execute(self, query, *arguments):
        self.query = query
        self.arguments = arguments


class FakePool:
    def __init__(self, connection) -> None:
        self.connection = connection

    def acquire(self):
        return AsyncContext(self.connection)


@pytest.mark.asyncio
async def test_current_archive_source_resolution_records_concrete_version(
    monkeypatch,
) -> None:
    document_id = uuid4()
    version_id = uuid4()
    content = b"current content"
    representation_id = uuid4()
    details = AsyncMock(
        return_value=ArchiveDocumentVersion(
            identifier=document_id,
            version_id=version_id,
            default_representation_id=representation_id,
            representations=(
                ArchiveRepresentation(
                    representation_id=representation_id,
                    content_type="text/plain",
                    checksum=hashlib.sha512(content).hexdigest(),
                    size=len(content),
                ),
            ),
            document_type=SimpleNamespace(value=None),
            metadata={"nested": {"value": True}},
        )
    )
    binary = AsyncMock(return_value=content)
    monkeypatch.setattr(ArchiveDocumentSource, "_details", details)
    monkeypatch.setattr(ArchiveDocumentSource, "_binary", binary)
    source = ArchiveDocumentSource(document_id, archive="payroll")

    retrieved = await source.retrieve()

    assert source.resolved_version == str(version_id)
    assert retrieved.metadata == {"nested": {"value": True}}
    assert binary.await_args.kwargs["version"] == str(version_id)
    assert binary.await_args.kwargs["representation"] is None


def test_elasticsearch_factory_validates_destination_configuration() -> None:
    factory = ElasticsearchSearchAdapterFactory(SimpleNamespace())

    with pytest.raises(ValueError, match="endpoint"):
        factory.validate({"index": "documents"})
    with pytest.raises(ValueError, match="username and password"):
        factory.validate(
            {
                "endpoint": "http://search:9200",
                "index": "documents",
                "username": "xarta",
            }
        )


@pytest.mark.asyncio
async def test_elasticsearch_adapter_indexes_and_replays_unchanged_projection() -> None:
    class Response:
        def __init__(self, status, payload=None):
            self.status = status
            self.payload = payload or {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def read(self):
            import orjson

            return orjson.dumps(self.payload)

    class Session:
        def __init__(self):
            self.conflict = False
            self.posts = []

        def put(self, *_args, **_kwargs):
            return Response(409 if self.conflict else 201)

        def get(self, *_args, **_kwargs):
            return Response(200, {"_source": {"digest": "a" * 64}})

        def post(self, url, **kwargs):
            self.posts.append((url, orjson.loads(kwargs["data"])))
            return Response(200)

    session = Session()
    adapter = ElasticsearchSearchAdapter(
        session,  # type: ignore[arg-type]
        {"endpoint": "http://search:9200", "index": "documents"},
    )
    document = searchable()

    assert (
        await adapter.index(idempotency_key=str(uuid4()), document=document)
    ).outcome == "indexed"
    session.conflict = True
    assert (
        await adapter.index(idempotency_key=str(uuid4()), document=document)
    ).outcome == "unchanged"
    assert session.posts == []


@pytest.mark.asyncio
async def test_postgresql_adapter_inserts_new_source_version() -> None:
    identifier = uuid4()
    connection = FakeConnection(inserted=identifier)
    adapter = PostgreSQLSearchAdapter(
        FakePool(connection),
        {"namespace": "documents"},
    )

    submission = await adapter.index(
        idempotency_key=str(uuid4()), document=searchable()
    )

    assert submission == IndexSubmission("indexed", str(identifier))
    assert connection.arguments is not None
    assert connection.arguments[1] == "documents"
    assert connection.arguments[4] == "payroll"


@pytest.mark.asyncio
async def test_postgresql_adapter_distinguishes_unchanged_and_digest_conflict() -> None:
    identifier = uuid4()
    document = SearchableDocument(
        source=DocumentSearchSource("temporary", uuid4(), "v1"),
        projection_revision="v1",
        digest="a" * 64,
        content_type="text/plain",
        document_type=None,
        metadata={},
    )
    unchanged = PostgreSQLSearchAdapter(
        FakePool(
            FakeConnection(existing={"id": identifier, "digest": document.digest})
        ),
        {"namespace": "documents"},
    )
    conflict = PostgreSQLSearchAdapter(
        FakePool(FakeConnection(existing={"id": identifier, "digest": "b" * 64})),
        {"namespace": "documents"},
    )

    assert (
        await unchanged.index(idempotency_key=str(uuid4()), document=document)
    ).outcome == "unchanged"
    rejected = await conflict.index(idempotency_key=str(uuid4()), document=document)
    assert rejected.outcome == "index_rejected"
    assert rejected.details == {"reason": "source_version_digest_conflict"}


@pytest.mark.asyncio
async def test_postgresql_archive_lifecycle_uses_exact_indexed_predicates() -> None:
    connection = FakeConnection()
    adapter = PostgreSQLSearchAdapter(FakePool(connection), {"namespace": "documents"})
    source = ArchiveSearchSource("payroll", uuid4(), uuid4())

    await adapter.remove_archive_document(
        archive=source.archive, document_id=source.document_id
    )
    assert connection.arguments == ("documents", "payroll", source.document_id)


@pytest.mark.asyncio
async def test_elasticsearch_archive_identity_and_lifecycle_are_archive_scoped() -> (
    None
):
    class Response:
        status = 201

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    class Session:
        def __init__(self):
            self.put_urls = []
            self.posts = []

        def put(self, url, **_kwargs):
            self.put_urls.append(url)
            return Response()

        def post(self, url, **kwargs):
            response = Response()
            response.status = 200
            self.posts.append((url, orjson.loads(kwargs["data"])))
            return response

    session = Session()
    adapter = ElasticsearchSearchAdapter(
        session,  # type: ignore[arg-type]
        {"endpoint": "http://search:9200", "index": "documents"},
    )
    document_id = uuid4()
    version_id = uuid4()
    first = searchable()
    source = ArchiveSearchSource("payroll", document_id, version_id)
    first = SearchableDocument(
        source=source,
        projection_revision=first.projection_revision,
        digest=first.digest,
        content_type=first.content_type,
        document_type=first.document_type,
        metadata=first.metadata,
        default_representation_id=first.default_representation_id,
        representations=first.representations,
    )
    second = SearchableDocument(
        source=ArchiveSearchSource("legal", document_id, version_id),
        projection_revision=first.projection_revision,
        digest=first.digest,
        content_type=first.content_type,
        document_type=first.document_type,
        metadata=first.metadata,
        default_representation_id=first.default_representation_id,
        representations=first.representations,
    )

    await adapter.index(idempotency_key=str(uuid4()), document=first)
    await adapter.index(idempotency_key=str(uuid4()), document=second)
    assert session.put_urls[0] != session.put_urls[1]

    await adapter.remove_archive_document(
        archive=source.archive, document_id=source.document_id
    )
    assert "_delete_by_query" in session.posts[-1][0]
    assert "conflicts=proceed" not in session.posts[-1][0]
    assert session.posts[-1][1] == {
        "query": {
            "bool": {
                "filter": [
                    {"term": {"source_kind.keyword": "archive"}},
                    {"term": {"source_archive.keyword": "payroll"}},
                    {"term": {"source_document_id.keyword": str(document_id)}},
                ]
            }
        }
    }


class FakeFactory:
    def __init__(self, outcome: str = "indexed") -> None:
        self.outcome = outcome
        self.fail = False
        self.documents: list[SearchableDocument] = []
        self.lifecycle: list[tuple] = []

    def validate(self, configuration) -> None:
        if not configuration.get("namespace"):
            raise ValueError("namespace required")

    def create(self, configuration):
        factory = self

        class Adapter:
            async def index(self, *, document, **kwargs):
                if factory.fail:
                    raise RuntimeError("index unavailable")
                factory.documents.append(document)
                return IndexSubmission(factory.outcome, "index-id")

            async def remove_archive_document(self, *, archive, document_id):
                if factory.fail:
                    raise SearchTransportError("index unavailable")
                factory.lifecycle.append(("document", archive, document_id))

        return Adapter()


@pytest.mark.asyncio
async def test_search_worker_indexes_and_returns_outcome(monkeypatch) -> None:
    factory = FakeFactory()
    components = build_search_components(
        {"documents": revisioned(configuration())},
        AdapterRegistry({"fake-search": factory}),
    )
    document_id = uuid4()
    version_id = uuid4()
    node = SearchIndexNode(
        document={
            "source": "archive",
            "archive": "payroll",
            "id": str(document_id),
            "version": str(version_id),
        },
        destination="documents",
    )
    source = ArchiveDocumentSource(
        document_id,
        archive="payroll",
        version=str(version_id),
    )
    representation_id = uuid4()
    details = AsyncMock(
        return_value=ArchiveDocumentVersion(
            identifier=document_id,
            version_id=version_id,
            default_representation_id=representation_id,
            representations=(
                ArchiveRepresentation(
                    representation_id=representation_id,
                    content_type="text/plain",
                    checksum=hashlib.sha512(b"invoice").hexdigest(),
                    size=len(b"invoice"),
                ),
            ),
            document_type=SimpleNamespace(value="invoice"),
            metadata={"customer": "123"},
        )
    )
    binary = AsyncMock()
    monkeypatch.setattr(ArchiveDocumentSource, "_details", details)
    monkeypatch.setattr(ArchiveDocumentSource, "_binary", binary)
    monkeypatch.setattr(SearchIndexNode, "interpret", lambda self: source)
    result = await _worker(
        node, NodeTask(flow_id=uuid4(), node=node), components=components
    )

    assert result.outcomes_persisted is False
    assert factory.documents[0].source == ArchiveSearchSource(
        "payroll", document_id, version_id
    )
    assert factory.documents[0].digest is None
    assert factory.documents[0].default_representation_id == representation_id
    assert factory.documents[0].representations[0].content_type == "text/plain"
    binary.assert_not_awaited()
    assert [outcome.outcome for outcome in result.outcomes] == ["indexed"]


@pytest.mark.asyncio
async def test_search_worker_indexes_pdf_metadata_without_extraction(
    monkeypatch,
) -> None:
    factory = FakeFactory()
    components = build_search_components(
        {"documents": revisioned(configuration())},
        AdapterRegistry({"fake-search": factory}),
    )
    document_id = uuid4()
    node = SearchIndexNode(
        document={"source": "temporary", "id": str(document_id), "version": "v1"},
        destination="documents",
    )
    source = SimpleNamespace(
        source="temporary",
        id=document_id,
        retrieve=AsyncMock(
            return_value=SimpleNamespace(
                id=document_id,
                data=b"%PDF",
                content_type="application/pdf",
                document_type=SimpleNamespace(value="invoice"),
                metadata={},
            )
        ),
    )
    monkeypatch.setattr(SearchIndexNode, "interpret", lambda self: source)
    result = await _worker(
        node, NodeTask(flow_id=uuid4(), node=node), components=components
    )

    assert factory.documents[0].content_type == "application/pdf"
    assert [outcome.outcome for outcome in result.outcomes] == ["indexed"]


def archive_event(
    event_type: str,
    document_id,
    version_id=None,
):
    data = {"archive": "payroll", "id": str(document_id)}
    if version_id is not None:
        data["head"] = str(version_id)
    return SimpleNamespace(data=orjson.dumps({"type": event_type, "data": data}))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event_type", "expected"),
    [
        ("xarta.archive.document.deletion-requested", "document"),
        ("xarta.archive.document.deleted", "document"),
    ],
)
async def test_archive_removal_events_are_idempotent(event_type, expected) -> None:
    factory = FakeFactory()
    components = build_search_components(
        {"documents": revisioned(configuration())},
        AdapterRegistry({"fake-search": factory}),
    )
    document_id = uuid4()
    version_id = uuid4()
    message = archive_event(event_type, document_id, version_id)

    await _archive_lifecycle_worker(msg=message, components=components)
    await _archive_lifecycle_worker(msg=message, components=components)

    assert [operation[0] for operation in factory.lifecycle] == [expected, expected]


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["postgresql", "elasticsearch"])
@pytest.mark.parametrize(
    "delete_before_write", [False, True], ids=["ordered-control", "late-index-race"]
)
async def test_archive_deletion_and_inflight_index_ordering(
    monkeypatch, backend, delete_before_write
) -> None:
    """Characterize the known race, not the desired convergence guarantee.

    Workers and adapters are real; archive state, SQL storage, and HTTP storage
    are doubles. Change the late-write expectation when convergence is fixed.
    """
    records = {}
    operations = []
    document = searchable()
    document_id = document.source.document_id
    late_version_id = uuid4()
    archive_deleted = False

    class Connection(FakeConnection):
        async def fetchval(self, query, *args):
            assert "INSERT INTO public.search_documents" in query
            records[str(args[0])] = {
                "namespace": args[1],
                "source_kind": args[3],
                "source_archive": args[4],
                "source_document_id": str(args[5]),
                "source_version_id": str(args[6]),
            }
            operations.append("index")
            return args[0]

        async def execute(self, query, *args):
            assert "DELETE FROM public.search_documents" in query
            for key, record in list(records.items()):
                if (
                    record["namespace"] == args[0]
                    and record["source_kind"] == "archive"
                    and record["source_archive"] == args[1]
                    and record["source_document_id"] == str(args[2])
                ):
                    del records[key]
            operations.append("delete")

    class Session:
        def put(self, url, **kwargs):
            assert "/_create/" in url
            key = url.rsplit("/", 1)[-1]
            assert key not in records
            records[key] = orjson.loads(kwargs["data"])
            operations.append("index")
            return AsyncContext(SimpleNamespace(status=201))

        def post(self, url, **kwargs):
            assert "/_delete_by_query" in url
            filters = orjson.loads(kwargs["data"])["query"]["bool"]["filter"]
            for key, record in list(records.items()):
                if all(
                    record.get(field.removesuffix(".keyword")) == value
                    for item in filters
                    for field, value in item["term"].items()
                ):
                    del records[key]
            operations.append("delete")
            return AsyncContext(SimpleNamespace(status=200))

    if backend == "postgresql":
        adapter = PostgreSQLSearchAdapter(
            FakePool(Connection()), {"namespace": "documents"}
        )
    else:
        adapter = ElasticsearchSearchAdapter(
            Session(),
            {"endpoint": "http://search:9200", "index": "documents"},
        )
    # An older projection proves that the deletion handler really removes data.
    await adapter.index(idempotency_key=str(uuid4()), document=document)
    assert len(records) == 1
    operations.clear()
    components = build_search_components(
        {"documents": revisioned(configuration())},
        AdapterRegistry(
            {
                "fake-search": SimpleNamespace(
                    validate=lambda _configuration: None,
                    create=lambda _configuration: adapter,
                )
            }
        ),
    )

    async def archive_details(**kwargs):
        if archive_deleted:
            raise ArchiveDocumentNotFound(str(document_id))
        operations.append("read")
        return ArchiveDocumentVersion(
            identifier=document_id,
            version_id=late_version_id,
            default_representation_id=document.default_representation_id,
            representations=tuple(
                ArchiveRepresentation(
                    representation_id=item.representation_id,
                    content_type=item.content_type,
                    checksum=hashlib.sha512(b"invoice").hexdigest(),
                    size=len(b"invoice"),
                )
                for item in document.representations
            ),
            document_type=SimpleNamespace(value="invoice"),
            metadata={"customer": "123"},
        )

    details = AsyncMock(side_effect=archive_details)
    monkeypatch.setattr(ArchiveDocumentSource, "_details", details)
    node = SearchIndexNode(
        document={
            "source": "archive",
            "archive": "payroll",
            "id": str(document_id),
            "version": str(late_version_id),
        },
        destination="documents",
    )
    ready, resume = asyncio.Event(), asyncio.Event()
    index = adapter.index

    async def paused_index(**kwargs):
        ready.set()
        await resume.wait()
        return await index(**kwargs)

    monkeypatch.setattr(adapter, "index", paused_index)
    task = asyncio.create_task(
        _worker(
            node,
            NodeTask(flow_id=uuid4(), node=node),
            components=components,
        )
    )
    deleted = archive_event(
        "xarta.archive.document.deleted", document_id, late_version_id
    )
    try:
        async with asyncio.timeout(5):
            await ready.wait()
            details.assert_awaited_once()
            assert operations == ["read"]
            if delete_before_write:
                archive_deleted = True
                await _archive_lifecycle_worker(msg=deleted, components=components)
                assert records == {}
            resume.set()
            result = await task
            if not delete_before_write:
                archive_deleted = True
                await _archive_lifecycle_worker(msg=deleted, components=components)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    details.assert_awaited_once()  # The worker never revalidated its cached snapshot.
    assert [outcome.outcome for outcome in result.outcomes] == ["indexed"]
    assert operations == (
        ["read", "delete", "index"]
        if delete_before_write
        else ["read", "index", "delete"]
    )
    if delete_before_write:
        # Known defect: successful deletion is followed by a successful stale write.
        assert len(records) == 1
        record = next(iter(records.values()))
        assert record["source_document_id"] == str(document_id)
        assert record["source_version_id"] == str(late_version_id)
    else:
        assert records == {}
    with pytest.raises(ArchiveDocumentNotFound):
        await ArchiveDocumentSource(document_id, archive="payroll").details()


@pytest.mark.asyncio
async def test_archive_lifecycle_backend_failure_is_retryable() -> None:
    factory = FakeFactory()
    factory.fail = True
    components = build_search_components(
        {"documents": revisioned(configuration())},
        AdapterRegistry({"fake-search": factory}),
    )

    with pytest.raises(TemporaryError):
        await _archive_lifecycle_worker(
            msg=archive_event("xarta.archive.document.deleted", uuid4(), uuid4()),
            components=components,
        )


def test_search_components_validate_all_retained_revisions() -> None:
    destinations = {
        "documents": {
            "kind": "search-index",
            "adapter": "fake-search",
            "current_revision": "v2",
            "revisions": {
                "v1": {"namespace": ""},
                "v2": {"namespace": "documents"},
            },
        }
    }

    with pytest.raises(ValueError, match="namespace"):
        build_search_components(
            destinations, AdapterRegistry({"fake-search": FakeFactory()})
        )


def test_search_components_require_explicit_revisions() -> None:
    with pytest.raises(ValueError, match="explicit current_revision and revisions"):
        build_search_components(
            {"documents": configuration()},
            AdapterRegistry({"fake-search": FakeFactory()}),
        )


@pytest.mark.asyncio
async def test_search_retry_resolves_current_configuration_revision(
    monkeypatch,
) -> None:
    factory = FakeFactory()
    factory.fail = True
    original = build_search_components(
        {"documents": revisioned(configuration())},
        AdapterRegistry({"fake-search": factory}),
    )
    document_id = uuid4()
    node = SearchIndexNode(
        document={
            "source": "temporary",
            "id": str(document_id),
            "version": "v1",
        },
        destination="documents",
    )
    source = SimpleNamespace(
        source="temporary",
        id=document_id,
        retrieve=AsyncMock(
            return_value=SimpleNamespace(
                id=document_id,
                data=b"ten bytes!",
                content_type="text/plain",
                document_type=SimpleNamespace(value="invoice"),
                metadata={},
            )
        ),
    )
    monkeypatch.setattr(SearchIndexNode, "interpret", lambda self: source)
    task = NodeTask(flow_id=uuid4(), node=node)

    with pytest.raises(RuntimeError, match="unavailable"):
        await _worker(node, task, components=original)

    factory.fail = False
    revised = build_search_components(
        {
            "documents": {
                "kind": "search-index",
                "adapter": "fake-search",
                "current_revision": "v2",
                "revisions": {
                    "v1": {"namespace": "documents"},
                    "v2": {"namespace": "documents"},
                },
            }
        },
        AdapterRegistry({"fake-search": factory}),
    )

    result = await _worker(node, task, components=revised)

    assert factory.documents[0].projection_revision == "v2"
    assert [outcome.outcome for outcome in result.outcomes] == ["indexed"]
