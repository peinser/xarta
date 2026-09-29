from __future__ import annotations

import re

from typing import Any

import aiohttp

from xarta.mcp.service import ReadServiceClient


class UBLClient(ReadServiceClient):
    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str,
        timeout_seconds: float,
        max_document_bytes: int,
    ) -> None:
        super().__init__(session, base_url, timeout_seconds)
        self.max_document_bytes = max_document_bytes

    async def profiles(self) -> dict[str, Any]:
        return (await self.get("/validation/profiles")).dict()

    def _document(self, xml: str) -> bytes:
        # MCP strings are Unicode; preserve text by requiring a compatible XML
        # declaration rather than silently sending UTF-8 under another encoding.
        declaration = re.match(r"\ufeff?<\?xml\b(.*?)\?>", xml, re.DOTALL)
        encoding = (
            re.search(r"""\bencoding\s*=\s*(['"])(.*?)\1""", declaration[1])
            if declaration
            else None
        )
        if encoding and encoding[2].lower() not in {"utf-8", "utf8"}:
            raise ValueError(
                "MCP XML strings must declare UTF-8 or omit the encoding declaration"
            )
        document = xml.encode("utf-8")
        if len(document) > self.max_document_bytes:
            raise ValueError("UBL document exceeds the MCP UTF-8 byte limit")
        return document

    async def validate_schema(self, xml: str) -> dict[str, Any]:
        return (await self.post_xml("/validate/schema", self._document(xml))).dict()

    async def validate_business_rules(self, xml: str, profile: str) -> dict[str, Any]:
        return (
            await self.post_xml(
                "/validate/business-rules",
                self._document(xml),
                params={"profile": profile},
            )
        ).dict()
