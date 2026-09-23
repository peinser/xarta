from __future__ import annotations

import asyncio

from typing import TYPE_CHECKING

import aiohttp

from xarta.exceptions.protocol import PermanentError
from xarta.exceptions.protocol import TemporaryError

if TYPE_CHECKING:
    from aiohttp import ClientSession


PDF_CONTENT_TYPE = "application/pdf"


class GotenbergTransformClient:
    def __init__(
        self,
        session: ClientSession,
        endpoint: str,
        timeout: float,
        max_bytes: int,
    ) -> None:
        if timeout <= 0:
            raise ValueError("Transform timeout must be positive")
        if max_bytes <= 0:
            raise ValueError("Transform byte limit must be positive")
        self.session = session
        self.endpoint = endpoint.rstrip("/")
        self.timeout = timeout
        self.max_bytes = max_bytes

    async def convert(
        self,
        data: bytes,
        *,
        filename: str,
        content_type: str,
        trace: str,
    ) -> bytes:
        form = aiohttp.FormData()
        form.add_field("files", data, filename=filename, content_type=content_type)
        return await self._post("/forms/libreoffice/convert", form, trace)

    async def merge(self, documents: list[bytes], *, trace: str) -> bytes:
        form = aiohttp.FormData()
        for index, data in enumerate(documents, start=1):
            form.add_field(
                "files",
                data,
                filename=f"{index:06d}.pdf",
                content_type=PDF_CONTENT_TYPE,
            )
        return await self._post("/forms/pdfengines/merge", form, trace)

    async def split(
        self,
        data: bytes,
        *,
        start: int,
        end: int,
        trace: str,
    ) -> bytes:
        form = aiohttp.FormData()
        form.add_field(
            "files", data, filename="input.pdf", content_type=PDF_CONTENT_TYPE
        )
        form.add_field("splitMode", "pages")
        form.add_field("splitSpan", str(start) if start == end else f"{start}-{end}")
        form.add_field("splitUnify", "true")
        return await self._post("/forms/pdfengines/split", form, trace)

    async def _post(self, route: str, form: aiohttp.FormData, trace: str) -> bytes:
        try:
            async with self.session.post(
                f"{self.endpoint}{route}",
                data=form,
                headers={"Gotenberg-Trace": trace},
                timeout=self.timeout,
            ) as response:
                if response.status == 429 or response.status >= 500:
                    raise TemporaryError(
                        "Document transform engine is temporarily unavailable",
                        delay=1,
                        classification="transform_provider_unavailable",
                        error_code="transform_provider_temporary_failure",
                    )
                if response.status < 200 or response.status >= 300:
                    raise PermanentError(
                        "Document transform request was rejected",
                        classification="transform_request_rejected",
                        error_code="transform_request_rejected",
                    )
                content_type = (
                    response.headers.get("Content-Type", "")
                    .split(";", 1)[0]
                    .strip()
                    .lower()
                )
                if content_type != PDF_CONTENT_TYPE:
                    raise PermanentError(
                        "Document transform engine returned an unsupported representation",
                        classification="transform_provider_contract",
                        error_code="transform_provider_contract_failure",
                    )
                try:
                    body = await response.content.readexactly(self.max_bytes + 1)
                except asyncio.IncompleteReadError as ex:
                    body = ex.partial
                else:
                    raise PermanentError(
                        "Document transform output exceeded its size limit",
                        classification="transform_output_too_large",
                        error_code="transform_output_too_large",
                    )
                return body
        except (aiohttp.ClientError, TimeoutError) as ex:
            raise TemporaryError(
                "Document transform engine is temporarily unavailable",
                delay=1,
                classification="transform_provider_unavailable",
                error_code="transform_provider_transport_failure",
            ) from ex
