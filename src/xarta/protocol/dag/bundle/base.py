from __future__ import annotations

import builtins

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

import xarta.protocol.dag

from xarta.protocol.dag import Node
from xarta.protocol.document import source
from xarta.protocol.document.request import bundle as bundle_protocol

if TYPE_CHECKING:
    from collections.abc import Mapping
    from typing import Any
    from typing import Final

    from xarta.protocol.document.source import DocumentSource


@dataclass(frozen=True)
class BundleNodeDocument:
    source: DocumentSource
    filename: str


class BundleNode(Node):
    KIND: Final[str] = "bundle"
    OUTCOMES = frozenset({"success", "failure"})

    def __init__(
        self,
        documents: list[Mapping[str, Any]],
        out: UUID | str,
        compression: Mapping[str, Any] | None = None,
        id: UUID | None = None,
        on: dict[str, list[Node]] | None = None,
        parent: Node | None = None,
    ) -> None:
        super().__init__(kind=self.KIND, id=id, on=on, parent=parent)
        if not documents:
            raise ValueError("A bundle node must contain at least one document")
        if len(documents) > 25:
            raise ValueError("A bundle node cannot contain more than 25 documents")

        filenames = []
        for document in documents:
            filename = document.get("filename")
            if not isinstance(filename, str):
                raise ValueError("Every bundle document requires a filename")
            bundle_protocol.validate_bundle_filename(filename)
            filenames.append(filename)
        if len(filenames) != len(set(filenames)):
            raise ValueError("Bundle document filenames must be unique")

        self._documents = [dict(document) for document in documents]
        self.output_id = UUID(str(out))
        self.compression = (
            bundle_protocol.DocumentBundleRequestCompressionOptions.fromdict(
                dict(compression or {})
            )
        )
        interpreted = self.interpret()
        if any(
            document.source.source == "generate"
            and UUID(str(document.source.id)) == self.output_id
            for document in interpreted
        ):
            raise ValueError("Bundle output cannot overwrite an input document")

    @property
    def documents(self) -> list[dict[str, Any]]:
        return self._documents

    def interpret(self) -> list[BundleNodeDocument]:
        return [
            BundleNodeDocument(
                source=source.parse(
                    **{
                        key: value
                        for key, value in document.items()
                        if key != "filename"
                    }
                ),
                filename=document["filename"],
            )
            for document in self._documents
        ]

    def dict(self) -> dict:
        specification = super().dict()
        specification.update(
            {
                "documents": self._documents,
                "out": self.output_id,
                "compression": self.compression.dict(),
            }
        )
        return specification

    @staticmethod
    def fromdict(
        _specification: builtins.dict,
        documents: list[Mapping[str, Any]],
        out: UUID | str,
        compression: Mapping[str, Any] | None = None,
        **kwargs,
    ) -> BundleNode:
        return BundleNode(
            **xarta.protocol.dag.parse_common_fields(**_specification),
            documents=documents,
            out=out,
            compression=compression,
        )
