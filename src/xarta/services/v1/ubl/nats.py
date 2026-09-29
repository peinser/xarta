from __future__ import annotations

import asyncio
import contextlib
import hashlib

from typing import TYPE_CHECKING
from uuid import uuid5

import orjson

from xarta.exceptions.protocol import PermanentError
from xarta.nats.sanic import SanicNATSSynchronousRequestsConsumerModel
from xarta.protocol.dag import CapabilityResult
from xarta.protocol.dag import OutcomeEmission
from xarta.protocol.dag.ubl import UBLNode
from xarta.protocol.document.source import ArchiveDocumentSource
from xarta.protocol.document.source import DocumentSourceResult
from xarta.protocol.document.source import TemporaryDocumentSource
from xarta.storage import get_temporary_storage
from xarta.ubl.editor import UBLAttachment
from xarta.ubl.editor import UBLEditor
from xarta.ubl.editor import UBLError

if TYPE_CHECKING:
    from sanic import Sanic


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _encoded(value) -> bytes:
    return orjson.dumps(value, option=orjson.OPT_SORT_KEYS)


async def _snapshot(
    node: UBLNode, editor: UBLEditor
) -> tuple[dict, list[DocumentSourceResult]]:
    """Pin bytes before editing; the create-only manifest wins concurrent resolutions.

    Content-addressed inputs allow recovery after partial writes, without reusing
    an orphaned snapshot from a different version of a mutable source.
    """
    storage = get_temporary_storage()
    prefix = f"ubl/{node.output_id}"
    key = f"{prefix}/manifest.json"
    fingerprint = _digest(
        _encoded(
            {
                "version": 1,
                "document": node.document,
                "operations": node.operations,
            }
        )
    )
    try:
        manifest = orjson.loads(await storage.get(key))
    except FileNotFoundError:
        request = node.interpret()
        sources = (request.document, *(item.document for item in request.operations))
        specifications = (
            node.document,
            *(item["document"] for item in node.operations),
        )
        inputs = []
        total = 0
        for index, (source, specification) in enumerate(
            zip(sources, specifications, strict=True)
        ):
            result = await source.retrieve()
            total += len(result.data)
            editor.require_size(total)
            digest = _digest(result.data)
            await storage.put(
                f"{prefix}/inputs/{digest}", result.data, result.content_type
            )
            receipt = {
                "id": result.id or uuid5(node.output_id, f"input:{index}"),
                "content_type": result.content_type,
                "document_type": result.document_type.value,
                "metadata": result.metadata,
                "sha256": digest,
                "reference": specification,
            }
            if isinstance(source, ArchiveDocumentSource):
                receipt["resolved_version"] = source.resolved_version
                receipt["resolved_representation"] = source.resolved_representation
            inputs.append(receipt)
        manifest = {"fingerprint": fingerprint, "inputs": inputs}
        with contextlib.suppress(FileExistsError):
            await storage.put(key, _encoded(manifest), "application/json")
        manifest = orjson.loads(await storage.get(key))
    if manifest["fingerprint"] != fingerprint:
        raise UBLError(
            "UBL output ID is already bound to a different request",
            "ubl_output_conflict",
        )
    documents = []
    total = 0
    for item in manifest["inputs"]:
        data = await storage.get(f"{prefix}/inputs/{item['sha256']}")
        total += len(data)
        editor.require_size(total)
        if _digest(data) != item["sha256"]:
            raise UBLError("Pinned UBL input checksum mismatch", "ubl_input_corrupt")
        documents.append(
            DocumentSourceResult.parse(
                id=item["id"],
                data=data,
                content_type=item["content_type"],
                document_type=item["document_type"],
                metadata=item["metadata"],
            )
        )
    return manifest, documents


async def _edit(node: UBLNode, editor: UBLEditor) -> None:
    manifest, documents = await _snapshot(node, editor)
    request = node.interpret()
    original, *attachments = documents
    # Parsing, two XSD validations and base64 encoding are CPU-bound.
    binary = await asyncio.to_thread(
        editor.edit,
        original.data,
        tuple(
            UBLAttachment(
                item.id,
                item.filename,
                document.content_type,
                document.data,
                item.description,
            )
            for item, document in zip(request.operations, attachments, strict=True)
        ),
    )
    result = DocumentSourceResult(
        id=node.output_id,
        content_type="application/xml",
        data=binary,
        document_type=original.document_type,
        metadata={
            **(original.metadata or {}),
            "_xarta_ubl": {
                "fingerprint": manifest["fingerprint"],
                "inputs": [
                    {name: value for name, value in item.items() if name != "metadata"}
                    for item in manifest["inputs"]
                ],
            },
        },
    )
    try:
        await result.persist()
    except FileExistsError as ex:
        raise UBLError(
            "UBL output ID already contains different content", "ubl_output_conflict"
        ) from ex
    # Also verifies a previously completed output; identical create-only writes are safe.
    if await TemporaryDocumentSource(node.output_id).retrieve() != result:
        raise UBLError(
            "UBL output does not match the pinned edit", "ubl_output_conflict"
        )


async def _worker(node: UBLNode, editor: UBLEditor, **kwargs) -> CapabilityResult:
    try:
        await _edit(node, editor)
    except UBLError as ex:
        raise PermanentError(
            str(ex), classification=ex.code, error_code=ex.code
        ) from ex
    return CapabilityResult((OutcomeEmission("success"),))


class UBLNATSModel(SanicNATSSynchronousRequestsConsumerModel):
    @classmethod
    async def register(cls, app: Sanic, **kwargs) -> None:  # type: ignore[override]
        await super().register(app=app, fn=_worker, name="ubl", **kwargs)
