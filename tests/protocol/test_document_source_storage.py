from __future__ import annotations

import asyncio

from typing import TYPE_CHECKING
from uuid import UUID

import pytest

import xarta.protocol.dag
import xarta.protocol.document.source as source_module

from xarta.protocol.document.source import DocumentSourceResult
from xarta.protocol.document.source import GenerateDocumentSource
from xarta.protocol.document.type import DocumentTypeIdentifier
from xarta.storage import FilesystemTemporaryStorage

if TYPE_CHECKING:
    from pathlib import Path


async def test_generated_document_source_round_trip(
    monkeypatch, tmp_path: Path
) -> None:
    identifier = UUID("12345678-1234-5678-1234-567812345678")
    storage = FilesystemTemporaryStorage(str(tmp_path))
    monkeypatch.setattr(source_module, "get_temporary_storage", lambda: storage)
    original = DocumentSourceResult(
        id=identifier,
        content_type="application/pdf",
        data=b"document",
        document_type=DocumentTypeIdentifier("invoice"),
        metadata={"tenant": "example"},
    )

    await original.persist()
    restored = await GenerateDocumentSource(identifier).retrieve()

    assert restored == original
    base = tmp_path / "12" / "34" / "56" / "78"
    assert (base / str(identifier)).read_bytes() == b"document"
    assert (base / f"{identifier}.json").is_file()


async def test_generated_document_persists_metadata_and_binary_concurrently(
    monkeypatch, tmp_path: Path
) -> None:
    calls: set[str] = set()
    both_started = asyncio.Event()

    class RecordingStorage(FilesystemTemporaryStorage):
        async def put(self, key: str, data: bytes, content_type: str) -> bool:
            calls.add(key)
            if len(calls) == 2:
                both_started.set()
            await both_started.wait()
            return await super().put(key, data, content_type)

    identifier = UUID("12345678-1234-5678-1234-567812345678")
    storage = RecordingStorage(str(tmp_path))
    monkeypatch.setattr(source_module, "get_temporary_storage", lambda: storage)

    async with asyncio.timeout(1):
        await DocumentSourceResult(
            id=identifier,
            content_type="application/pdf",
            data=b"document",
            document_type=DocumentTypeIdentifier("invoice"),
        ).persist()

    assert calls == {
        f"12/34/56/78/{identifier}.json",
        f"12/34/56/78/{identifier}",
    }


async def test_generated_document_retry_completes_after_binary_failure(
    monkeypatch, tmp_path: Path
) -> None:
    binary_write_failed = False

    class FailFirstBinaryWrite(FilesystemTemporaryStorage):
        async def put(self, key: str, data: bytes, content_type: str) -> bool:
            nonlocal binary_write_failed
            should_fail = not key.endswith(".json") and not binary_write_failed
            if should_fail:
                binary_write_failed = True
                raise OSError("binary write failed")
            return await super().put(key, data, content_type)

    identifier = UUID("12345678-1234-5678-1234-567812345678")
    storage = FailFirstBinaryWrite(str(tmp_path))
    monkeypatch.setattr(source_module, "get_temporary_storage", lambda: storage)
    result = DocumentSourceResult(
        id=identifier,
        content_type="application/pdf",
        data=b"document",
        document_type=DocumentTypeIdentifier("invoice"),
    )

    with pytest.raises(OSError, match="binary write failed"):
        await result.persist()
    await result.persist()

    assert await GenerateDocumentSource(identifier).retrieve() == result
