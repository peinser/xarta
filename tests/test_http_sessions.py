from __future__ import annotations

import asyncio

from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp

from xarta.http.sessions import USER_AGENT
from xarta.http.sessions import HTTPRequestManager


async def test_default_user_agent_and_explicit_header_override():
    async with await HTTPRequestManager.open(
        loop=asyncio.get_running_loop()
    ) as session:
        assert session.headers["User-Agent"] == USER_AGENT
    async with await HTTPRequestManager.open(
        loop=asyncio.get_running_loop(), headers={"user-agent": "custom-client"}
    ) as session:
        assert session.headers.getall("User-Agent") == ["custom-client"]


async def test_factory_reuses_supplied_connector():
    connector = aiohttp.TCPConnector(limit=3)
    session = await HTTPRequestManager.open(
        loop=asyncio.get_running_loop(), connector=connector
    )
    assert session.connector is connector
    await session.close()
    assert connector.closed


async def test_cleanup_closes_the_app_session_not_another_apps_global_alias(
    monkeypatch,
):
    first, second = AsyncMock(), AsyncMock()
    app = SimpleNamespace(ctx=SimpleNamespace(http_client_session=first))
    monkeypatch.setattr(HTTPRequestManager, "__session__", second)
    await HTTPRequestManager._cleanup(app)
    first.close.assert_awaited_once()
    second.close.assert_not_awaited()
    assert HTTPRequestManager.__session__ is second
