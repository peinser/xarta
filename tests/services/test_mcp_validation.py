from __future__ import annotations

from dataclasses import replace

import pytest

from mcp import Client

from tests.services.test_mcp import FakeResponse
from tests.services.test_mcp import FakeSession
from tests.services.test_mcp import configuration
from xarta.mcp import create_mcp_server
from xarta.mcp.peppol import PeppolClient
from xarta.mcp.ubl import UBLClient

UBL_TOOLS = {
    "list_ubl_validation_profiles",
    "validate_ubl_schema",
    "validate_ubl_business_rules",
}
PEPPOL_TOOL = "check_peppol_participant_registration"


def enabled():
    return replace(
        configuration(),
        ubl_base_url="http://ubl/api/v1/ubl",
        peppol_base_url="http://peppol/api/v1/peppol",
    )


class ValidationSession(FakeSession):
    def __init__(self, status=200, body=None):
        super().__init__()
        self.status = status
        self.body = body

    def request(self, method, url, **kwargs):
        self.requests.append({"method": method, "url": url, **kwargs})
        if self.body is not None:
            return FakeResponse(self.status, self.body)
        if url.endswith("/validation/profiles"):
            return FakeResponse(
                200,
                {"profiles": [{"id": "peppol-bis-billing-3", "release": "2026.05"}]},
            )
        if "/validate/" in url:
            return FakeResponse(
                200,
                {
                    "valid": False,
                    "issues": [{"rule": "PEPPOL-COMMON-R043", "severity": "fatal"}],
                },
            )
        return FakeResponse(
            200,
            {
                "registered": None,
                "status": "indeterminate",
                "definitive": False,
                "retryable": True,
                "evidence": [{"stage": "smp", "code": "smp_timeout"}],
            },
        )


@pytest.mark.parametrize(
    ("ubl", "peppol"), [(False, False), (True, False), (False, True), (True, True)]
)
async def test_optional_tools_are_advertised_only_for_configured_services(ubl, peppol):
    settings = replace(
        configuration(),
        ubl_base_url=enabled().ubl_base_url if ubl else None,
        peppol_base_url=enabled().peppol_base_url if peppol else None,
    )
    tools = {tool.name: tool for tool in await create_mcp_server(settings).list_tools()}
    assert (UBL_TOOLS & tools.keys()) == (UBL_TOOLS if ubl else set())
    assert (PEPPOL_TOOL in tools) is peppol
    for name in UBL_TOOLS | {PEPPOL_TOOL}:
        if name in tools:
            assert tools[name].annotations.read_only_hint
            assert tools[name].annotations.idempotent_hint
            assert not tools[name].annotations.destructive_hint
            assert tools[name].annotations.open_world_hint is (name == PEPPOL_TOOL)


async def test_mcp_tools_forward_xml_profile_and_registration_evidence(monkeypatch):
    session = ValidationSession()
    monkeypatch.setattr("xarta.mcp.server.aiohttp.ClientSession", lambda **_: session)
    async with Client(create_mcp_server(enabled()), raise_exceptions=True) as client:
        profiles = await client.call_tool("list_ubl_validation_profiles", {})
        xml = '<?xml version="1.0" encoding="UTF-8"?><Invoice>évidence</Invoice>'
        schema = await client.call_tool("validate_ubl_schema", {"xml": xml})
        rules = await client.call_tool("validate_ubl_business_rules", {"xml": xml})
        participant = await client.call_tool(
            PEPPOL_TOOL, {"scheme": "0208", "identifier": "0308357159"}
        )
    assert profiles.structured_content["body"]["profiles"][0]["release"] == "2026.05"
    assert schema.structured_content["body"]["valid"] is False
    assert rules.structured_content["body"]["issues"][0]["rule"] == "PEPPOL-COMMON-R043"
    body = participant.structured_content["body"]
    assert body["registered"] is None and not body["definitive"]
    assert body["evidence"] == [{"stage": "smp", "code": "smp_timeout"}]
    assert session.requests[1]["data"] == xml.encode("utf-8")
    assert session.requests[1]["json"] is None
    assert (
        session.requests[1]["headers"]["Content-Type"]
        == "application/xml; charset=utf-8"
    )
    assert session.requests[1]["allow_redirects"] is False
    assert session.requests[2]["params"] == {"profile": "peppol-bis-billing-3"}
    assert session.requests[2]["timeout"].total == 35
    assert (
        session.requests[3]["url"]
        == "http://peppol/api/v1/peppol/participants/0208/0308357159/registration"
    )


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (
            503,
            {"valid": None, "status": "indeterminate", "error": "Engine unavailable"},
        ),
        (400, {"error": "Unsupported profile"}),
        (413, {"error": "Too large"}),
    ],
)
async def test_mcp_preserves_validation_error_envelope(monkeypatch, status, body):
    session = ValidationSession(status, body)
    monkeypatch.setattr("xarta.mcp.server.aiohttp.ClientSession", lambda **_: session)
    async with Client(create_mcp_server(enabled()), raise_exceptions=True) as client:
        result = await client.call_tool(
            "validate_ubl_business_rules", {"xml": "<Invoice/>"}
        )
    assert result.structured_content == {"status": status, "body": body}


@pytest.mark.parametrize("registered", [True, False, None])
async def test_registration_tristate_is_preserved(monkeypatch, registered):
    body = {
        "registered": registered,
        "definitive": registered is not None,
        "evidence": [],
    }
    session = ValidationSession(200, body)
    monkeypatch.setattr("xarta.mcp.server.aiohttp.ClientSession", lambda **_: session)
    async with Client(create_mcp_server(enabled()), raise_exceptions=True) as client:
        result = await client.call_tool(
            PEPPOL_TOOL, {"scheme": "0208", "identifier": "0308357159"}
        )
    assert result.structured_content["body"] == body


async def test_transport_timeout_is_a_tool_error_not_a_negative_result(monkeypatch):
    class Unavailable(FakeSession):
        def request(self, *args, **kwargs):
            raise TimeoutError("Connection failed")

    monkeypatch.setattr(
        "xarta.mcp.server.aiohttp.ClientSession", lambda **_: Unavailable()
    )
    async with Client(create_mcp_server(enabled()), raise_exceptions=True) as client:
        result = await client.call_tool(
            PEPPOL_TOOL, {"scheme": "0208", "identifier": "0308357159"}
        )
    assert result.is_error is True
    assert result.structured_content is None


async def test_ubl_input_bound_counts_utf8_bytes_and_rejects_incompatible_encoding():
    session = ValidationSession()
    client = UBLClient(session, "http://ubl", 35, 3)
    with pytest.raises(ValueError, match="byte limit"):
        await client.validate_schema("éé")
    with pytest.raises(ValueError, match="declare UTF-8"):
        await client.validate_schema(
            '<?xml version="1.0" encoding="ISO-8859-1"?><Invoice/>'
        )
    assert not session.requests


async def test_participant_path_segments_cannot_escape_the_lookup_route():
    session = ValidationSession()
    client = PeppolClient(session, "http://peppol", 30)
    await client.participant_registration("0208", "value/?#")
    assert session.requests[0]["url"].endswith("/0208/value%2F%3F%23/registration")
    with pytest.raises(ValueError):
        await client.participant_registration("0208", "..")
