from __future__ import annotations

from typing import Any
from urllib.parse import quote

from xarta.mcp.service import ReadServiceClient


class PeppolClient(ReadServiceClient):
    async def participant_registration(
        self, scheme: str, identifier: str
    ) -> dict[str, Any]:
        if (
            not scheme
            or not identifier
            or scheme in {".", ".."}
            or identifier in {".", ".."}
        ):
            raise ValueError(
                "Participant scheme and identifier must be non-empty path segments"
            )
        path = f"/participants/{quote(scheme, safe='')}/{quote(identifier, safe='')}/registration"
        return (await self.get(path)).dict()
