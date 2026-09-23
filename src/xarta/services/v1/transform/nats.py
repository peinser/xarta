from __future__ import annotations

from dataclasses import dataclass
from functools import partial

from xarta.exceptions.protocol import PermanentError
from xarta.nats.sanic import SanicNATSSynchronousRequestsConsumerModel
from xarta.protocol.dag import CapabilityResult
from xarta.protocol.dag import NodeTask
from xarta.protocol.dag import OutcomeEmission
from xarta.protocol.dag.transform import TransformConvert
from xarta.protocol.dag.transform import TransformMerge
from xarta.protocol.dag.transform import TransformNode
from xarta.protocol.dag.transform import TransformSplit
from xarta.protocol.dag.transform import TransformSplitOutput
from xarta.protocol.document.source import DocumentSourceResult
from xarta.protocol.document.source import GenerateDocumentSource
from xarta.protocol.document.type import DocumentTypeIdentifier

from .gotenberg import PDF_CONTENT_TYPE
from .gotenberg import GotenbergTransformClient

_CONVERT_EXTENSIONS = {
    "text/plain": ".txt",
    "text/csv": ".csv",
    "application/rtf": ".rtf",
    "text/rtf": ".rtf",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.ms-powerpoint": ".ppt",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/vnd.oasis.opendocument.text": ".odt",
    "application/vnd.oasis.opendocument.spreadsheet": ".ods",
    "application/vnd.oasis.opendocument.presentation": ".odp",
}


@dataclass(frozen=True)
class TransformComponents:
    client: GotenbergTransformClient
    max_bytes: int


def _content_type(value: str) -> str:
    return value.split(";", 1)[0].strip().lower()


def _require_size(data: bytes, max_bytes: int, *, total: int = 0) -> int:
    size = total + len(data)
    if size > max_bytes:
        raise PermanentError(
            "Document transform input exceeded its size limit",
            classification="transform_input_too_large",
            error_code="transform_input_too_large",
        )
    return size


async def _existing_output(
    output_id,
    *,
    document_type: DocumentTypeIdentifier,
    metadata: dict | None,
) -> DocumentSourceResult | None:
    try:
        existing = await GenerateDocumentSource(output_id).retrieve()
    except FileNotFoundError:
        return None
    if (
        _content_type(existing.content_type) != PDF_CONTENT_TYPE
        or existing.document_type.value != document_type.value
        or existing.metadata != metadata
    ):
        raise PermanentError(
            "Transform output ID already contains a different document",
            classification="transform_output_conflict",
            error_code="transform_output_conflict",
        )
    return existing


async def _persist_output(result: DocumentSourceResult) -> None:
    try:
        await result.persist()
    except FileExistsError:
        existing = await _existing_output(
            result.id,
            document_type=result.document_type,
            metadata=result.metadata,
        )
        if existing is None:
            raise


async def _convert(
    operation: TransformConvert,
    *,
    components: TransformComponents,
    trace: str,
) -> None:
    document = await operation.document.retrieve()
    _require_size(document.data, components.max_bytes)
    existing = await _existing_output(
        operation.output_id,
        document_type=document.document_type,
        metadata=document.metadata,
    )
    if existing is not None:
        return

    input_type = _content_type(document.content_type)
    extension = _CONVERT_EXTENSIONS.get(input_type)
    if extension is None:
        raise PermanentError(
            "Document content type is not supported for conversion",
            classification="transform_unsupported_input",
            error_code="transform_unsupported_input",
        )
    binary = await components.client.convert(
        document.data,
        filename=f"input{extension}",
        content_type=input_type,
        trace=trace,
    )
    await _persist_output(
        DocumentSourceResult(
            id=operation.output_id,
            document_type=document.document_type,
            content_type=PDF_CONTENT_TYPE,
            metadata=document.metadata,
            data=binary,
        )
    )


async def _merge(
    operation: TransformMerge,
    *,
    components: TransformComponents,
    trace: str,
) -> None:
    document_type = DocumentTypeIdentifier(value=None)  # type: ignore[arg-type]
    if (
        await _existing_output(
            operation.output_id,
            document_type=document_type,
            metadata=None,
        )
        is not None
    ):
        return

    documents: list[bytes] = []
    total = 0
    for source in operation.documents:
        document = await source.retrieve()
        if _content_type(document.content_type) != PDF_CONTENT_TYPE:
            raise PermanentError(
                "Transform merge accepts PDF documents only",
                classification="transform_unsupported_input",
                error_code="transform_merge_requires_pdf",
            )
        total = _require_size(document.data, components.max_bytes, total=total)
        documents.append(document.data)

    binary = await components.client.merge(documents, trace=trace)
    await _persist_output(
        DocumentSourceResult(
            id=operation.output_id,
            document_type=document_type,
            content_type=PDF_CONTENT_TYPE,
            metadata=None,
            data=binary,
        )
    )


async def _split(
    operation: TransformSplit,
    *,
    components: TransformComponents,
    trace: str,
) -> None:
    document_type = DocumentTypeIdentifier(value=None)  # type: ignore[arg-type]
    missing: list[TransformSplitOutput] = []
    for output in operation.outputs:
        if (
            await _existing_output(
                output.output_id,
                document_type=document_type,
                metadata=None,
            )
            is None
        ):
            missing.append(output)
    if not missing:
        return

    document = await operation.document.retrieve()
    if _content_type(document.content_type) != PDF_CONTENT_TYPE:
        raise PermanentError(
            "Transform split accepts a PDF document only",
            classification="transform_unsupported_input",
            error_code="transform_split_requires_pdf",
        )
    _require_size(document.data, components.max_bytes)

    # V1 intentionally performs one request per declared output. If large split fan-out
    # becomes a real workload, batch behind this contract rather than exposing Gotenberg
    # ZIP output or provider-specific page expressions.
    for output in missing:
        binary = await components.client.split(
            document.data,
            start=output.start,
            end=output.end,
            trace=trace,
        )
        await _persist_output(
            DocumentSourceResult(
                id=output.output_id,
                document_type=document_type,
                content_type=PDF_CONTENT_TYPE,
                metadata=None,
                data=binary,
            )
        )


async def _worker(
    node: TransformNode,
    task: NodeTask,
    *,
    components: TransformComponents,
    **kwargs,
) -> CapabilityResult:
    operation = node.interpret()
    trace = str(task.node_execution_id)
    if isinstance(operation, TransformConvert):
        await _convert(operation, components=components, trace=trace)
    elif isinstance(operation, TransformMerge):
        await _merge(operation, components=components, trace=trace)
    else:
        assert isinstance(operation, TransformSplit)
        await _split(operation, components=components, trace=trace)
    return CapabilityResult((OutcomeEmission(outcome="success"),))


class TransformNATSModel(SanicNATSSynchronousRequestsConsumerModel):
    @classmethod
    async def register(  # type: ignore[override]
        cls, app, components: TransformComponents, **kwargs
    ) -> None:
        await super().register(
            app=app,
            fn=partial(_worker, components=components),
            name=TransformNode.KIND,
            **kwargs,
        )
