from __future__ import annotations

import asyncio

from types import SimpleNamespace
from typing import cast
from uuid import uuid4

import aiohttp
import pytest

from xarta.exceptions.protocol import PermanentError
from xarta.exceptions.protocol import TemporaryError
from xarta.protocol.dag import NodeTask
from xarta.protocol.dag.transform import TransformNode
from xarta.protocol.document.source import DocumentSourceResult
from xarta.protocol.document.source import GenerateDocumentSource
from xarta.protocol.document.type import DocumentTypeIdentifier
from xarta.services.v1.transform import nats as transform_nats
from xarta.services.v1.transform.gotenberg import GotenbergTransformClient
from xarta.services.v1.transform.nats import TransformComponents
from xarta.services.v1.transform.nats import _worker


class Stream:
    def __init__(self, body: bytes):
        self.body = body

    async def readexactly(self, maximum: int):
        if len(self.body) < maximum:
            raise asyncio.IncompleteReadError(self.body, maximum)
        return self.body[:maximum]


class ResponseContext:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, *_args):
        return None


class Session:
    def __init__(self, response):
        self.response = response
        self.url = None
        self.kwargs = None

    def post(self, url, **kwargs):
        self.url = url
        self.kwargs = kwargs
        return ResponseContext(self.response)


def response(
    status: int = 200,
    body: bytes = b"%PDF-1.7",
    content_type: str = "application/pdf",
):
    return SimpleNamespace(
        status=status,
        headers={"Content-Type": content_type},
        content=Stream(body),
    )


@pytest.mark.asyncio
async def test_gotenberg_convert_uses_libreoffice_route_and_trace() -> None:
    session = Session(response())
    client = GotenbergTransformClient(session, "http://gotenberg:3000", 30, 1024)

    result = await client.convert(
        b"hello",
        filename="input.docx",
        content_type=(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ),
        trace="execution-1",
    )

    assert result == b"%PDF-1.7"
    assert session.url == "http://gotenberg:3000/forms/libreoffice/convert"
    assert session.kwargs["headers"] == {"Gotenberg-Trace": "execution-1"}
    form = session.kwargs["data"]
    assert isinstance(form, aiohttp.FormData)
    assert form._fields[0][0]["name"] == "files"
    assert form._fields[0][0]["filename"] == "input.docx"


@pytest.mark.asyncio
async def test_gotenberg_merge_encodes_array_order_in_filenames() -> None:
    session = Session(response())
    client = GotenbergTransformClient(session, "http://gotenberg:3000", 30, 1024)

    await client.merge([b"first", b"second", b"third"], trace="execution-2")

    assert session.url == "http://gotenberg:3000/forms/pdfengines/merge"
    form = session.kwargs["data"]
    assert [field[0]["filename"] for field in form._fields] == [
        "000001.pdf",
        "000002.pdf",
        "000003.pdf",
    ]
    assert [field[2] for field in form._fields] == [b"first", b"second", b"third"]


@pytest.mark.asyncio
async def test_gotenberg_split_translates_structured_range() -> None:
    session = Session(response())
    client = GotenbergTransformClient(session, "http://gotenberg:3000", 30, 1024)

    await client.split(b"pdf", start=3, end=7, trace="execution-3")

    assert session.url == "http://gotenberg:3000/forms/pdfengines/split"
    values = {field[0]["name"]: field[2] for field in session.kwargs["data"]._fields}
    assert values["splitMode"] == "pages"
    assert values["splitSpan"] == "3-7"
    assert values["splitUnify"] == "true"


@pytest.mark.asyncio
async def test_gotenberg_split_single_page_uses_single_page_span() -> None:
    session = Session(response())
    client = GotenbergTransformClient(session, "http://gotenberg:3000", 30, 1024)

    await client.split(b"pdf", start=4, end=4, trace="execution-4")

    values = {field[0]["name"]: field[2] for field in session.kwargs["data"]._fields}
    assert values["splitSpan"] == "4"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 404])
async def test_gotenberg_deterministic_rejection_is_permanent(status: int) -> None:
    client = GotenbergTransformClient(
        Session(response(status=status)), "http://gotenberg", 30, 1024
    )

    with pytest.raises(PermanentError, match="rejected"):
        await client.merge([b"a", b"b"], trace="execution")


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 500, 503])
async def test_gotenberg_temporary_failures_are_retryable(status: int) -> None:
    client = GotenbergTransformClient(
        Session(response(status=status)), "http://gotenberg", 30, 1024
    )

    with pytest.raises(TemporaryError, match="temporarily unavailable"):
        await client.merge([b"a", b"b"], trace="execution")


@pytest.mark.asyncio
async def test_gotenberg_rejects_non_pdf_success() -> None:
    client = GotenbergTransformClient(
        Session(response(content_type="application/zip")),
        "http://gotenberg",
        30,
        1024,
    )

    with pytest.raises(PermanentError, match="unsupported representation"):
        await client.merge([b"a", b"b"], trace="execution")


@pytest.mark.asyncio
async def test_gotenberg_bounds_output_bytes() -> None:
    client = GotenbergTransformClient(
        Session(response(body=b"12345")), "http://gotenberg", 30, 4
    )

    with pytest.raises(PermanentError, match="size limit"):
        await client.merge([b"a", b"b"], trace="execution")


def source_result(
    data: bytes,
    content_type: str = "application/pdf",
    *,
    metadata=None,
):
    return DocumentSourceResult(
        id=uuid4(),
        content_type=content_type,
        document_type=DocumentTypeIdentifier("test"),
        metadata=metadata,
        data=data,
    )


class Client:
    def __init__(self):
        self.convert_calls = []
        self.merge_calls = []
        self.split_calls = []

    async def convert(self, data, **kwargs):
        self.convert_calls.append((data, kwargs))
        return b"converted"

    async def merge(self, documents, **kwargs):
        self.merge_calls.append((documents, kwargs))
        return b"merged"

    async def split(self, data, **kwargs):
        self.split_calls.append((data, kwargs))
        return b"split"


def components(client: Client) -> TransformComponents:
    return TransformComponents(
        client=cast(GotenbergTransformClient, client),
        max_bytes=1024,
    )


def task(node: TransformNode) -> NodeTask:
    return NodeTask(uuid4(), node)


@pytest.mark.asyncio
async def test_worker_convert_preserves_semantic_metadata(monkeypatch) -> None:
    input_id = uuid4()
    output_id = uuid4()
    document = source_result(
        b"docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        metadata={"customer": "one"},
    )
    node = TransformNode(
        convert={
            "document": {"source": "generate", "id": str(input_id)},
            "out": str(output_id),
            "content_type": "application/pdf",
        }
    )
    client = Client()
    persisted = []

    async def retrieve(_self, **_kwargs):
        return document

    async def missing(*_args, **_kwargs):
        return None

    async def persist(result):
        persisted.append(result)

    monkeypatch.setattr(GenerateDocumentSource, "retrieve", retrieve)
    monkeypatch.setattr(transform_nats, "_existing_output", missing)
    monkeypatch.setattr(transform_nats, "_persist_output", persist)

    result = await _worker(
        node=node,
        task=task(node),
        components=components(client),
    )

    assert [item.outcome for item in result.outcomes] == ["success"]
    assert client.convert_calls[0][1]["filename"] == "input.docx"
    assert persisted[0].id == output_id
    assert persisted[0].document_type.value == "test"
    assert persisted[0].metadata == {"customer": "one"}


@pytest.mark.asyncio
async def test_worker_merge_preserves_input_order(monkeypatch) -> None:
    ids = [uuid4(), uuid4()]
    output_id = uuid4()
    documents = {
        str(ids[0]): source_result(b"first"),
        str(ids[1]): source_result(b"second"),
    }
    node = TransformNode(
        merge={
            "documents": [{"source": "generate", "id": str(value)} for value in ids],
            "out": str(output_id),
        }
    )
    client = Client()

    async def retrieve(self, **_kwargs):
        return documents[str(self.id)]

    async def missing(*_args, **_kwargs):
        return None

    async def persist(_result):
        return None

    monkeypatch.setattr(GenerateDocumentSource, "retrieve", retrieve)
    monkeypatch.setattr(transform_nats, "_existing_output", missing)
    monkeypatch.setattr(transform_nats, "_persist_output", persist)

    await _worker(
        node=node,
        task=task(node),
        components=components(client),
    )

    assert client.merge_calls[0][0] == [b"first", b"second"]


@pytest.mark.asyncio
async def test_partial_split_replay_only_generates_missing_output(monkeypatch) -> None:
    input_id = uuid4()
    first = uuid4()
    second = uuid4()
    node = TransformNode(
        split={
            "document": {"source": "generate", "id": str(input_id)},
            "outputs": [
                {"pages": {"start": 1, "end": 1}, "out": str(first)},
                {"pages": {"start": 2, "end": 3}, "out": str(second)},
            ],
        }
    )
    client = Client()
    persisted = []

    async def retrieve(_self, **_kwargs):
        return source_result(b"pdf")

    async def existing(output_id, **_kwargs):
        return SimpleNamespace() if output_id == first else None

    async def persist(result):
        persisted.append(result.id)

    monkeypatch.setattr(GenerateDocumentSource, "retrieve", retrieve)
    monkeypatch.setattr(transform_nats, "_existing_output", existing)
    monkeypatch.setattr(transform_nats, "_persist_output", persist)

    await _worker(
        node=node,
        task=task(node),
        components=components(client),
    )

    assert len(client.split_calls) == 1
    assert client.split_calls[0][1]["start"] == 2
    assert client.split_calls[0][1]["end"] == 3
    assert persisted == [second]


@pytest.mark.asyncio
async def test_completed_merge_replay_skips_provider(monkeypatch) -> None:
    node = TransformNode(
        merge={
            "documents": [
                {"source": "generate", "id": str(uuid4())},
                {"source": "generate", "id": str(uuid4())},
            ],
            "out": str(uuid4()),
        }
    )
    client = Client()

    async def existing(*_args, **_kwargs):
        return SimpleNamespace()

    monkeypatch.setattr(transform_nats, "_existing_output", existing)

    await _worker(
        node=node,
        task=task(node),
        components=components(client),
    )

    assert client.merge_calls == []
