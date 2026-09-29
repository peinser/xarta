from __future__ import annotations

import asyncio

from uuid import uuid4

import orjson
import pytest

from xarta.exceptions.protocol import PermanentError
from xarta.protocol.dag.ubl import UBLNode
from xarta.protocol.document.source import ArchiveDocumentSource
from xarta.protocol.document.source import DocumentSourceResult
from xarta.protocol.document.source import TemporaryDocumentSource
from xarta.protocol.document.type import DocumentTypeIdentifier
from xarta.services.v1.ubl.nats import _worker


async def prepare(invoice):
    source_id, attachment_id, out = uuid4(), uuid4(), uuid4()
    original = DocumentSourceResult(
        source_id,
        "application/xml",
        invoice(),
        DocumentTypeIdentifier("invoice"),
        {"customer": "example"},
    )
    await original.persist()
    await DocumentSourceResult(
        attachment_id, "application/pdf", b"%PDF-evidence", DocumentTypeIdentifier(None)
    ).persist()
    node = UBLNode(
        document={"id": str(source_id)},
        operations=[
            {
                "action": "add_attachment",
                "id": "evidence",
                "document": {"id": str(attachment_id)},
                "filename": "evidence.pdf",
            }
        ],
        out=out,
    )
    return node, original


async def test_publish_new_output_and_reuse_pinned_inputs(
    storage, editor, invoice, monkeypatch
):
    node, original = await prepare(invoice)
    result = await _worker(node, editor)
    assert [item.outcome for item in result.outcomes] == ["success"]
    output = await TemporaryDocumentSource(node.output_id).retrieve()
    assert output.content_type == "application/xml"
    assert output.document_type == original.document_type
    assert output.metadata["customer"] == "example"
    assert output.data != original.data
    assert await TemporaryDocumentSource(original.id).retrieve() == original
    provenance = output.metadata["_xarta_ubl"]
    assert len(provenance["inputs"]) == 2
    assert all(
        "resolved_version" not in item and "resolved_representation" not in item
        for item in provenance["inputs"]
    )
    real_retrieve = TemporaryDocumentSource.retrieve

    async def only_output(self, **kwargs):
        assert self.id == node.output_id, "A retry must not retrieve live inputs"
        return await real_retrieve(self)

    monkeypatch.setattr(TemporaryDocumentSource, "retrieve", only_output)
    await _worker(node, editor)
    assert await TemporaryDocumentSource(node.output_id).retrieve() == output


async def test_changed_intent_same_output_is_permanent_conflict(
    storage, editor, invoice
):
    node, _ = await prepare(invoice)
    await _worker(node, editor)
    node.operations[0]["filename"] = "different.pdf"
    with pytest.raises(PermanentError, match="different request"):
        await _worker(node, editor)


async def test_preexisting_output_cannot_be_overwritten(storage, editor, invoice):
    node, _ = await prepare(invoice)
    existing = DocumentSourceResult(
        node.output_id, "application/xml", b"other", DocumentTypeIdentifier(None)
    )
    await existing.persist()
    with pytest.raises(PermanentError, match="different content"):
        await _worker(node, editor)
    assert await TemporaryDocumentSource(node.output_id).retrieve() == existing


async def test_no_output_when_any_operation_fails(storage, editor, invoice):
    node, _ = await prepare(invoice)
    node.operations.append({**node.operations[0], "id": "other"})
    node.operations[0]["id"] = "INV-001"
    # Duplicate an existing document-reference ID only in the second operation.
    original = invoice(
        references="<cac:AdditionalDocumentReference><cbc:ID>other</cbc:ID></cac:AdditionalDocumentReference>"
    )
    source_id = uuid4()
    await DocumentSourceResult(
        source_id, "application/xml", original, DocumentTypeIdentifier(None)
    ).persist()
    node.document = {"id": str(source_id)}
    with pytest.raises(PermanentError, match="already exists"):
        await _worker(node, editor)
    with pytest.raises(FileNotFoundError):
        await TemporaryDocumentSource(node.output_id).retrieve()


async def test_output_metadata_failure_can_be_retried(
    storage, editor, invoice, monkeypatch
):
    node, _ = await prepare(invoice)
    put = storage.put
    failed = False

    async def fail_once(self, key, data, content_type):
        nonlocal failed
        if key.endswith(f"{node.output_id}.json") and not failed:
            failed = True
            raise OSError("storage unavailable")
        return await put(key, data, content_type)

    monkeypatch.setattr(type(storage), "put", fail_once)
    with pytest.raises(OSError):
        await _worker(node, editor)
    with pytest.raises(FileNotFoundError):
        await TemporaryDocumentSource(node.output_id).retrieve()
    await _worker(node, editor)
    assert (
        b"evidence.pdf"
        in (await TemporaryDocumentSource(node.output_id).retrieve()).data
    )


async def test_concurrent_delivery_reuses_one_manifest_and_output(
    storage, editor, invoice
):
    node, _ = await prepare(invoice)
    results = await asyncio.gather(*(_worker(node, editor) for _ in range(4)))
    assert all(result.outcomes[0].outcome == "success" for result in results)
    output = await TemporaryDocumentSource(node.output_id).retrieve()
    assert output.data.count(b"evidence.pdf") == 1


async def test_archive_resolution_recorded_only_for_archive_and_not_repeated(
    storage, editor, invoice, monkeypatch
):
    node, original = await prepare(invoice)
    node.document = {"source": "archive", "id": str(original.id)}
    version, representation = str(uuid4()), str(uuid4())
    calls = 0

    async def retrieve(self, **kwargs):
        nonlocal calls
        calls += 1
        assert calls == 1
        self.resolved_version = version
        self.resolved_representation = representation
        return original

    monkeypatch.setattr(ArchiveDocumentSource, "retrieve", retrieve)
    await _worker(node, editor)
    await _worker(node, editor)
    manifest = orjson.loads(await storage.get(f"ubl/{node.output_id}/manifest.json"))
    assert manifest["inputs"][0]["resolved_version"] == version
    assert manifest["inputs"][0]["resolved_representation"] == representation
    assert "resolved_version" not in manifest["inputs"][1]


async def test_manifest_failure_does_not_pin_partial_inputs(
    storage, editor, invoice, monkeypatch
):
    node, _ = await prepare(invoice)
    put = storage.put
    failed = False

    async def fail_once(self, key, data, content_type):
        nonlocal failed
        if key.endswith("manifest.json") and not failed:
            failed = True
            raise OSError("manifest unavailable")
        return await put(key, data, content_type)

    monkeypatch.setattr(type(storage), "put", fail_once)
    with pytest.raises(OSError):
        await _worker(node, editor)
    await _worker(node, editor)
    assert await TemporaryDocumentSource(node.output_id).retrieve()


async def test_concurrent_mutable_inputs_use_winning_snapshot(
    storage, editor, invoice, monkeypatch
):
    node, original = await prepare(invoice)
    node.document = {"source": "archive", "id": str(original.id)}
    gate = asyncio.Event()
    calls = 0

    async def retrieve(self, **kwargs):
        nonlocal calls
        calls += 1
        current = calls
        if calls == 2:
            gate.set()
        await gate.wait()
        self.resolved_version = str(uuid4())
        self.resolved_representation = str(uuid4())
        return DocumentSourceResult(
            original.id,
            original.content_type,
            original.data.replace(b"INV-001", f"INV-{current}".encode()),
            original.document_type,
            original.metadata,
        )

    monkeypatch.setattr(ArchiveDocumentSource, "retrieve", retrieve)
    async with asyncio.timeout(3):
        results = await asyncio.gather(_worker(node, editor), _worker(node, editor))
    assert all(result.outcomes[0].outcome == "success" for result in results)
    output = await TemporaryDocumentSource(node.output_id).retrieve()
    assert (b"INV-1" in output.data) != (b"INV-2" in output.data)


async def test_missing_pinned_input_is_not_resolved_again(
    storage, editor, invoice, monkeypatch
):
    node, _ = await prepare(invoice)
    await _worker(node, editor)
    manifest = orjson.loads(await storage.get(f"ubl/{node.output_id}/manifest.json"))
    key = f"ubl/{node.output_id}/inputs/{manifest['inputs'][0]['sha256']}"
    await storage.delete(key)

    async def no_resolution(self, **kwargs):
        pytest.fail("Expired snapshot must not fall back to a live source")

    monkeypatch.setattr(TemporaryDocumentSource, "retrieve", no_resolution)
    with pytest.raises(FileNotFoundError):
        await _worker(node, editor)
