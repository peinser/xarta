from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import aiohttp
import orjson

from xarta.payments.x402.http import PAYMENT_REQUIRED_HEADER
from xarta.payments.x402.http import PAYMENT_RESPONSE_HEADER
from xarta.payments.x402.http import PAYMENT_SIGNATURE_HEADER


@dataclass(frozen=True, slots=True)
class IntakeResponse:
    status: int
    body: Any
    payment_required: str | None = None
    payment_response: str | None = None

    def dict(self) -> dict[str, Any]:
        result = {"status": self.status, "body": self.body}
        if self.payment_required is not None:
            result["payment_required"] = self.payment_required
        if self.payment_response is not None:
            result["payment_response"] = self.payment_response
        return result


class IntakeClient:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str,
        timeout_seconds: float,
    ) -> None:
        self.session = session
        self.base_url = base_url
        self.timeout = aiohttp.ClientTimeout(total=timeout_seconds)

    async def get(self, path: str) -> dict[str, Any]:
        return (await self._request("GET", path)).dict()

    async def post(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        payment_signature: str | None = None,
    ) -> dict[str, Any]:
        headers = (
            {PAYMENT_SIGNATURE_HEADER: payment_signature}
            if payment_signature is not None
            else None
        )
        return (
            await self._request("POST", path, payload=payload, headers=headers)
        ).dict()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> IntakeResponse:
        try:
            async with self.session.request(
                method,
                f"{self.base_url}{path}",
                json=payload,
                headers=headers,
                timeout=self.timeout,
            ) as upstream:
                raw = await upstream.read()
                try:
                    body: Any = orjson.loads(raw)
                except orjson.JSONDecodeError:
                    body = raw.decode("utf-8", errors="replace")
                return IntakeResponse(
                    status=upstream.status,
                    body=body,
                    payment_required=upstream.headers.get(PAYMENT_REQUIRED_HEADER),
                    payment_response=upstream.headers.get(PAYMENT_RESPONSE_HEADER),
                )
        except (aiohttp.ClientError, TimeoutError) as ex:
            raise RuntimeError("Xarta intake is unavailable") from ex

    async def list_profiles(self) -> dict[str, Any]:
        return await self.get("/profiles")

    async def get_profile(self, name: str, version: int) -> dict[str, Any]:
        return await self.get(f"/profiles/{quote(name, safe='')}/{version}")

    async def prepare(self, flow: dict[str, Any]) -> dict[str, Any]:
        return await self.post("/prepare", flow)

    async def submit(
        self, flow: dict[str, Any], payment_signature: str | None
    ) -> dict[str, Any]:
        return await self.post("/", flow, payment_signature=payment_signature)

    async def submit_profile(
        self,
        name: str,
        version: int,
        payload: dict[str, Any],
        payment_signature: str | None,
    ) -> dict[str, Any]:
        path = f"/profiles/{quote(name, safe='')}/{version}"
        return await self.post(path, payload, payment_signature=payment_signature)
