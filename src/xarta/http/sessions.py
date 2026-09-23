r"""
HTTP session management.
"""

from __future__ import annotations

import asyncio

from typing import TYPE_CHECKING

import aiohttp

from opentelemetry.instrumentation.aiohttp_client import create_trace_config

from xarta.telemetry import is_enabled

if TYPE_CHECKING:
    from asyncio import AbstractEventLoop

    from sanic import Sanic


class HTTPRequestManager:
    r"""
    Global HTTP request manager. Sets up a global AIOHTTP
    ClientSession for Sanic on server start and
    cleans the session up before the server stops.
    """

    __app__: Sanic | None = None
    __session__: aiohttp.ClientSession | None = None
    __lock__: asyncio.Lock = asyncio.Lock()

    @classmethod
    async def open(cls, loop: AbstractEventLoop, **kwargs) -> aiohttp.ClientSession:
        connector = aiohttp.TCPConnector(limit=512)
        timeout = kwargs.pop("timeout", aiohttp.ClientTimeout(total=30, connect=5))
        if is_enabled():
            kwargs.setdefault(
                "trace_configs", [create_trace_config(url_filter=_trace_url)]
            )
        return aiohttp.ClientSession(connector=connector, timeout=timeout, **kwargs)

    @classmethod
    async def _cleanup(cls, app: Sanic) -> None:
        async with cls.__lock__:
            if hasattr(app.ctx, "http_client_session"):
                del app.ctx.http_client_session
                await cls.__session__.close()

    @classmethod
    async def register(cls, app: Sanic, **kwargs) -> None:
        r"""
        This method will serve as entrypoint to setup the necessary
        aiohttp connection client session.
        """
        async with cls.__lock__:
            if not hasattr(app.ctx, "http_client_session"):
                app.ctx.http_client_session = None

            if not app.ctx.http_client_session:
                session = await cls.open(loop=app.loop, **kwargs)
                app.ctx.http_client_session = cls.__session__ = session

        app.register_listener(cls._cleanup, "after_server_stop")


def initialize(app: Sanic):
    app.before_server_start(HTTPRequestManager.register)


def _trace_url(url) -> str:
    """Keep query parameters out of exported client URL attributes."""
    return str(url.with_query(None).with_fragment(None))
