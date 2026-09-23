from __future__ import annotations

from typing import Any
from urllib.parse import quote

from xarta.mcp.service import ReadServiceClient


class DocumentTypeClient(ReadServiceClient):
    async def list(self, *, limit: int, cursor: str | None = None) -> dict[str, Any]:
        params: dict[str, str | int] = {"limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        return (await self.get("/", params=params)).dict()

    async def retrieve(self, identifier: str) -> dict[str, Any]:
        return (await self.get(f"/{quote(identifier, safe='')}")).dict()

    async def check(self, identifier: str, request: dict[str, Any]) -> dict[str, Any]:
        path = f"/{quote(identifier, safe='')}/check"
        return (await self.post(path, request)).dict()
