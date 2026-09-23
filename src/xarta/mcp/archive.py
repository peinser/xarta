from __future__ import annotations

from typing import Any
from urllib.parse import quote
from uuid import UUID

from xarta.mcp.service import ReadServiceClient


class ArchiveClient(ReadServiceClient):
    @staticmethod
    def _document_path(archive: str, document_id: UUID) -> str:
        return f"/archives/{quote(archive, safe='')}/documents/{quote(str(document_id), safe='')}"

    async def details(
        self,
        archive: str,
        document_id: UUID,
        version_id: UUID | None = None,
    ) -> dict[str, Any]:
        path = self._document_path(archive, document_id)
        if version_id is not None:
            path += f"/versions/{quote(str(version_id), safe='')}"
        return (await self.get(f"{path}/details")).dict()

    async def versions(
        self,
        archive: str,
        document_id: UUID,
        *,
        limit: int,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, str | int] = {"limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        path = f"{self._document_path(archive, document_id)}/versions"
        return (await self.get(path, params=params)).dict()

    async def content(
        self,
        archive: str,
        document_id: UUID,
        version_id: UUID,
        representation_id: UUID,
    ) -> tuple[int, bytes, str | None]:
        path = (
            f"{self._document_path(archive, document_id)}/versions/"
            f"{quote(str(version_id), safe='')}"
            f"/representations/{quote(str(representation_id), safe='')}"
        )
        return await self.get_content(path)
