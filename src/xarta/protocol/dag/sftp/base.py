from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from pathlib import PurePosixPath
from typing import TYPE_CHECKING
from typing import Any

import xarta.protocol.dag

from xarta.protocol.dag import Node
from xarta.protocol.document import source

if TYPE_CHECKING:
    from uuid import UUID

    from xarta.protocol.document.source import DocumentSource


class SFTPOutcome(StrEnum):
    UPLOADED = "uploaded"
    PATH_CONFLICT = "path_conflict"
    UPLOAD_REJECTED = "upload_rejected"


class SFTPNode(Node):
    KIND = "sftp"
    OUTCOMES = frozenset(outcome.value for outcome in SFTPOutcome)

    def __init__(
        self,
        document: Mapping[str, Any],
        path: str,
        destination: str | None = None,
        id: UUID | None = None,
        on: dict[str, list[Node]] | None = None,
        parent: Node | None = None,
        **kwargs,
    ) -> None:
        super().__init__(kind=self.KIND, id=id, on=on, parent=parent)
        self.document = dict(document)
        self.path = self.validate_path(path)
        self.destination = destination

    @staticmethod
    def validate_path(value: str) -> str:
        if not value or "\x00" in value:
            raise ValueError("SFTP path must be a non-empty relative path")
        path = PurePosixPath(value)
        if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
            raise ValueError("SFTP path must not be absolute or contain traversal")
        return path.as_posix()

    def interpret(self) -> tuple[DocumentSource, str]:
        return source.parse(**self.document), self.path

    def dict(self) -> dict:
        specification = super().dict()
        specification.update(
            {
                "destination": self.destination,
                "document": self.document,
                "path": self.path,
            }
        )
        return specification

    @staticmethod
    def fromdict(
        _specification: Mapping[str, Any],
        document: Mapping[str, Any],
        path: str,
        destination: str | None = None,
        **kwargs,
    ) -> SFTPNode:
        return SFTPNode(
            **xarta.protocol.dag.parse_common_fields(**dict(_specification)),
            document=document,
            path=path,
            destination=destination,
        )
