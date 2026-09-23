from __future__ import annotations

import builtins

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

import xarta.protocol.dag

from xarta.protocol.dag import Node
from xarta.protocol.document import source

if TYPE_CHECKING:
    from collections.abc import Mapping
    from typing import Any
    from typing import Final

    from xarta.protocol.document.source import DocumentSource


@dataclass(frozen=True)
class TransformConvert:
    document: DocumentSource
    output_id: UUID
    content_type: str


@dataclass(frozen=True)
class TransformMerge:
    documents: tuple[DocumentSource, ...]
    output_id: UUID


@dataclass(frozen=True)
class TransformSplitOutput:
    start: int
    end: int
    output_id: UUID


@dataclass(frozen=True)
class TransformSplit:
    document: DocumentSource
    outputs: tuple[TransformSplitOutput, ...]


class TransformNode(Node):
    KIND: Final[str] = "transform"
    OUTCOMES = frozenset({"success", "failure"})

    def __init__(
        self,
        convert: Mapping[str, Any] | None = None,
        merge: Mapping[str, Any] | None = None,
        split: Mapping[str, Any] | None = None,
        id: UUID | None = None,
        on: dict[str, list[Node]] | None = None,
        parent: Node | None = None,
    ) -> None:
        super().__init__(kind=self.KIND, id=id, on=on, parent=parent)
        operations = [value is not None for value in (convert, merge, split)]
        if sum(operations) != 1:
            raise ValueError(
                "A transform node requires exactly one of convert, merge, or split"
            )

        self._convert = dict(convert) if convert is not None else None
        self._merge = dict(merge) if merge is not None else None
        self._split = dict(split) if split is not None else None
        self.interpret()

    @property
    def convert(self) -> dict[str, Any] | None:
        return self._convert

    @property
    def merge(self) -> dict[str, Any] | None:
        return self._merge

    @property
    def split(self) -> dict[str, Any] | None:
        return self._split

    @staticmethod
    def _source(value: object) -> DocumentSource:
        if not isinstance(value, dict):
            raise ValueError("Transform document source must be an object")
        return source.parse(**value)

    @staticmethod
    def _output(value: object) -> UUID:
        try:
            return UUID(str(value))
        except (TypeError, ValueError) as ex:
            raise ValueError("Transform output must be a UUID") from ex

    @staticmethod
    def _reject_generated_collision(document: DocumentSource, output_id: UUID) -> None:
        if document.source == source.GenerateDocumentSource.IDENTIFIER and str(
            document.id
        ) == str(output_id):
            raise ValueError("Transform output cannot overwrite an input document")

    def interpret(self) -> TransformConvert | TransformMerge | TransformSplit:
        if self._convert is not None:
            document = self._source(self._convert.get("document"))
            output_id = self._output(self._convert.get("out"))
            content_type = self._convert.get("content_type")
            if content_type != "application/pdf":
                raise ValueError(
                    "Transform convert currently supports application/pdf output only"
                )
            self._reject_generated_collision(document, output_id)
            return TransformConvert(document, output_id, content_type)

        if self._merge is not None:
            raw_documents = self._merge.get("documents")
            if not isinstance(raw_documents, list) or not 2 <= len(raw_documents) <= 25:
                raise ValueError("Transform merge requires between 2 and 25 documents")
            documents = tuple(self._source(value) for value in raw_documents)
            output_id = self._output(self._merge.get("out"))
            for document in documents:
                self._reject_generated_collision(document, output_id)
            return TransformMerge(documents, output_id)

        assert self._split is not None
        document = self._source(self._split.get("document"))
        raw_outputs = self._split.get("outputs")
        if not isinstance(raw_outputs, list) or not 1 <= len(raw_outputs) <= 25:
            raise ValueError("Transform split requires between 1 and 25 outputs")
        outputs = []
        seen: set[UUID] = set()
        for raw in raw_outputs:
            if not isinstance(raw, dict):
                raise ValueError("Transform split output must be an object")
            pages = raw.get("pages")
            if not isinstance(pages, dict):
                raise ValueError("Transform split pages must be an object")
            start = pages.get("start")
            end = pages.get("end")
            if (
                isinstance(start, bool)
                or isinstance(end, bool)
                or not isinstance(start, int)
                or not isinstance(end, int)
                or start < 1
                or end < start
            ):
                raise ValueError("Transform split pages require positive start <= end")
            output_id = self._output(raw.get("out"))
            if output_id in seen:
                raise ValueError("Transform split output IDs must be unique")
            self._reject_generated_collision(document, output_id)
            seen.add(output_id)
            outputs.append(TransformSplitOutput(start, end, output_id))
        return TransformSplit(document, tuple(outputs))

    def dict(self) -> dict:
        specification = super().dict()
        if self._convert is not None:
            specification["convert"] = self._convert
        elif self._merge is not None:
            specification["merge"] = self._merge
        else:
            specification["split"] = self._split
        return specification

    @staticmethod
    def fromdict(
        _specification: builtins.dict,
        convert: Mapping[str, Any] | None = None,
        merge: Mapping[str, Any] | None = None,
        split: Mapping[str, Any] | None = None,
        **kwargs,
    ) -> TransformNode:
        return TransformNode(
            **xarta.protocol.dag.parse_common_fields(**_specification),
            convert=convert,
            merge=merge,
            split=split,
        )
