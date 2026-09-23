r"""
Base definitions of the webhook DAG node and its subsidiaries.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket

from dataclasses import asdict
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlsplit
from uuid import UUID

import aiohttp
import orjson

import xarta.protocol.dag

from xarta.protocol.dag import Node
from xarta.protocol.document.source import parse as parse_source

if TYPE_CHECKING:
    from typing import Any
    from typing import Final


_CONTENT_TYPE_JSON: Final[str] = "application/json"


@dataclass
class MultipartEntry:
    name: str
    value: Any = None
    content_type: str | None = None
    filename: str | None = None

    async def prepare(self) -> None: ...


class SourceMultipartEntry(MultipartEntry):
    KIND: Final[str] = "source"

    def __init__(self, name: str, source: dict, **kwargs):
        super().__init__(name=name)
        self._source = parse_source(**source)

    async def prepare(self) -> None:
        retrieved = await self._source.retrieve()
        self.content_type = retrieved.content_type
        self.value = retrieved.data


class JSONMultipartEntry(MultipartEntry):
    def __init__(self, name: str, value: dict, **kwargs):
        value = orjson.dumps(value)
        super().__init__(
            name=name, value=value, content_type=_CONTENT_TYPE_JSON, **kwargs
        )


def _parse_multipart(payload: dict) -> MultipartEntry:
    payload = dict(payload)
    kind = payload.pop("kind", None)

    match kind:
        case SourceMultipartEntry.KIND:
            return SourceMultipartEntry(**payload)

        case None:
            if payload.get("content_type") == _CONTENT_TYPE_JSON:
                return JSONMultipartEntry(**payload)

            return MultipartEntry(**payload)

        case _:
            raise NotImplementedError


class WebhookNode(Node):
    KIND: Final[str] = "webhook"
    OUTCOMES = frozenset({"success", "failure"})

    def __init__(
        self,
        url: str,
        method: str | None = "POST",
        data: str | dict | None = None,
        auth: dict | None = None,
        headers: dict | None = None,
        multipart: list[dict] | None = None,
        id: UUID | None = None,
        on: dict[str, list[Node]] | None = None,
        parent: Node | None = None,
    ):
        super().__init__(
            kind=WebhookNode.KIND,
            id=id,
            on=on,
            parent=parent,
        )

        self._method = method
        self._auth = dict(auth or {})
        self._url = url
        self._data = data
        self._headers = dict(headers or {})
        self._multipart = list(multipart or [])

    async def _validate_destination(self) -> tuple[str, str]:
        parsed = urlsplit(self._url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Webhook URL must use HTTP or HTTPS")
        if parsed.username or parsed.password:
            raise ValueError("Webhook URL must not contain credentials")

        try:
            addresses = {ipaddress.ip_address(parsed.hostname)}
        except ValueError:
            loop = asyncio.get_running_loop()
            records = await loop.getaddrinfo(
                parsed.hostname,
                parsed.port or (443 if parsed.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            )
            addresses = {ipaddress.ip_address(record[4][0]) for record in records}

        if not addresses or any(not address.is_global for address in addresses):
            raise ValueError(
                "Webhook destination must resolve only to public addresses"
            )

        # Connect to the address that was validated instead of resolving the hostname
        # a second time in aiohttp, which would permit DNS rebinding.
        address = min(addresses, key=lambda item: (item.version, int(item)))
        address_host = f"[{address}]" if address.version == 6 else str(address)
        netloc = address_host
        if parsed.port:
            netloc = f"{netloc}:{parsed.port}"
        return parsed._replace(netloc=netloc).geturl(), parsed.hostname

    def _prepare_auth(self):
        if not self._auth:
            return None

        basic = self._auth.get("basic")
        if basic:
            return aiohttp.BasicAuth(basic.get("username"), basic.get("password"))

        raise NotImplementedError

    async def _prepare_multipart(self) -> aiohttp.FormData:
        entries = [_parse_multipart(entry) for entry in self._multipart]
        await asyncio.gather(*[entry.prepare() for entry in entries])

        data = aiohttp.FormData()
        for entry in entries:
            data.add_field(**asdict(entry))

        return data

    async def _prepare_data(self) -> bytes | aiohttp.FormData:
        if self._multipart:
            return await self._prepare_multipart()

        elif (
            self._headers.get("Content-Type", "").split(";", 1)[0] == _CONTENT_TYPE_JSON
        ):
            return orjson.dumps(self._data)

        return self._data

    async def interpret(self, session: aiohttp.ClientSession):
        destination, server_hostname = await self._validate_destination()
        auth = self._prepare_auth()
        data = await self._prepare_data()
        headers = dict(self._headers)
        headers["Host"] = urlsplit(self._url).netloc

        request_method = getattr(session, self._method.lower(), None)
        if not request_method:
            raise ValueError(f"Unsupported HTTP method: {self._method}")

        return request_method(
            auth=auth,
            data=data,
            url=destination,
            headers=headers,
            allow_redirects=False,
            server_hostname=server_hostname,
        )

    def dict(self) -> dict:
        specification = super().dict()
        specification.update(
            {
                "auth": self._auth,
                "url": self._url,
                "method": self._method,
                "data": self._data,
                "headers": self._headers,
                "multipart": self._multipart,
            }
        )

        return specification

    @staticmethod
    def fromdict(
        _specification: dict,
        url: str,
        auth: dict | None = None,
        data: dict | None = None,
        headers: dict | None = None,
        multipart: list[dict] | None = None,
        method: str | None = "POST",
        parent: Node | None = None,
        **kwargs,
    ) -> WebhookNode:
        return WebhookNode(
            **xarta.protocol.dag.parse_common_fields(**_specification),
            url=url,
            auth=auth,
            headers=headers,
            multipart=multipart,
            method=method,
            data=data,
        )
