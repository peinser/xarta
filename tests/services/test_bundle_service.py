from __future__ import annotations

import io
import zipfile

from uuid import uuid4

import pytest

from xarta.protocol.dag.bundle import BundleNode
from xarta.protocol.dag.bundle import BundleNodeDocument
from xarta.protocol.document.source import DocumentSource
from xarta.protocol.document.source import DocumentSourceResult
from xarta.protocol.document.type import DocumentTypeIdentifier
from xarta.services.v1.bundle.nats import _dag_worker


class Source(DocumentSource):
    def __init__(self, result: DocumentSourceResult) -> None:
        super().__init__(id=result.id, source="test")
        self.result = result

    async def retrieve(self) -> DocumentSourceResult:  # type: ignore[override]
        return self.result


@pytest.mark.asyncio
async def test_bundle_worker_persists_zip_and_emits_success(monkeypatch) -> None:
    output_id = uuid4()
    documents = [
        BundleNodeDocument(
            source=Source(
                DocumentSourceResult(
                    id=uuid4(),
                    content_type="text/plain",
                    document_type=DocumentTypeIdentifier(value="test"),
                    data=b"one",
                )
            ),
            filename="one.txt",
        ),
        BundleNodeDocument(
            source=Source(
                DocumentSourceResult(
                    id=uuid4(),
                    content_type="text/plain",
                    document_type=DocumentTypeIdentifier(value="test"),
                    data=b"two",
                )
            ),
            filename="two.txt",
        ),
    ]
    node = BundleNode(
        documents=[{"source": "generate", "id": str(uuid4()), "filename": "one.txt"}],
        out=output_id,
    )
    monkeypatch.setattr(node, "interpret", lambda: documents)
    persisted = []

    async def persist(result: DocumentSourceResult) -> None:
        persisted.append(result)

    monkeypatch.setattr(DocumentSourceResult, "persist", persist)

    result = await _dag_worker(node)

    assert [outcome.outcome for outcome in result.outcomes] == ["success"]
    assert len(persisted) == 1
    output = persisted[0]
    assert output.id == output_id
    assert output.content_type == "application/zip"
    with zipfile.ZipFile(io.BytesIO(output.data)) as archive:
        assert archive.read("one.txt") == b"one"
        assert archive.read("two.txt") == b"two"
