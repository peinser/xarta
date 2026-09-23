from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import aiohttp
import orjson


@dataclass(frozen=True, slots=True)
class ServiceResponse:
    status: int
    body: Any

    def dict(self) -> dict[str, Any]:
        return {"status": self.status, "body": self.body}


class ReadServiceClient:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str,
        timeout_seconds: float,
    ) -> None:
        self.session = session
        self.base_url = base_url
        self.timeout = aiohttp.ClientTimeout(total=timeout_seconds)

    async def get(
        self, path: str, *, params: dict[str, str | int] | None = None
    ) -> ServiceResponse:
        return await self._request("GET", path, params=params)

    async def post(self, path: str, payload: dict[str, Any]) -> ServiceResponse:
        return await self._request("POST", path, payload=payload)

    async def get_content(self, path: str) -> tuple[int, bytes, str | None]:
        try:
            async with self.session.get(
                f"{self.base_url}{path}", timeout=self.timeout
            ) as upstream:
                return (
                    upstream.status,
                    await upstream.read(),
                    upstream.headers.get("content-type"),
                )
        except (aiohttp.ClientError, TimeoutError) as ex:
            raise RuntimeError("Xarta service is unavailable") from ex

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str | int] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> ServiceResponse:
        try:
            async with self.session.request(
                method,
                f"{self.base_url}{path}",
                params=params,
                json=payload,
                timeout=self.timeout,
            ) as upstream:
                raw = await upstream.read()
                try:
                    body: Any = orjson.loads(raw)
                except orjson.JSONDecodeError:
                    body = raw.decode("utf-8", errors="replace")
                return ServiceResponse(upstream.status, body)
        except (aiohttp.ClientError, TimeoutError) as ex:
            raise RuntimeError("Xarta service is unavailable") from ex
