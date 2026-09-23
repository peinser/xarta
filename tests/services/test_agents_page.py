from __future__ import annotations

import pytest

from xarta.services.v1.agents.base import agents
from xarta.services.v1.agents.base import agents_markdown
from xarta.services.v1.agents.base import bp


def test_agents_page_has_public_route() -> None:
    assert {route.uri for route in bp._future_routes} == {"/agents", "/agents.md"}


@pytest.mark.asyncio
async def test_agents_page_documents_x402_intake() -> None:
    result = await agents(None)  # type: ignore[arg-type]
    body = result.body.decode()

    assert result.status == 200
    assert result.content_type == "text/html; charset=utf-8"
    assert "Xarta for AI agents" in body
    assert "GET /api/v1/intake/profiles" in body
    assert "PAYMENT-REQUIRED" in body
    assert "PAYMENT-SIGNATURE" in body
    assert "PAYMENT-RESPONSE" in body
    assert "when x402 is enabled" in body
    assert result.headers["cache-control"] == "public, max-age=300"
    assert result.headers["x-content-type-options"] == "nosniff"


@pytest.mark.asyncio
async def test_agents_markdown_is_compact_machine_documentation() -> None:
    result = await agents_markdown(None)  # type: ignore[arg-type]
    body = result.body.decode()

    assert result.content_type == "text/markdown; charset=utf-8"
    assert "`/api/mcp`" in body
    assert "Profiles are optional" in body
    assert "`payment_required`" in body
    assert "<html" not in body
