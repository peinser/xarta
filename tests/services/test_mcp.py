from __future__ import annotations

import base64
import hashlib

from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import orjson
import pytest

from jsonschema import Draft202012Validator
from jsonschema import FormatChecker
from mcp import Client
from mcp.shared.exceptions import MCPError

from xarta.mcp import MCPConfiguration
from xarta.mcp import create_mcp_server
from xarta.mcp.intake import IntakeClient
from xarta.protocol.dag import parse
from xarta.protocol.dag import supported_node_kinds
from xarta.services.v1.intake.base import intake_capabilities
from xarta.services.v1.intake.base import prepare_flow
from xarta.services.v1.intake.capabilities import CapabilityManifest


class FakeResponse:
    def __init__(
        self, status: int, body: Any, headers: dict[str, str] | None = None
    ) -> None:
        self.status = status
        self.body = body if isinstance(body, bytes) else orjson.dumps(body)
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def read(self) -> bytes:
        return self.body


class FakeSession:
    def __init__(self, *_args, **_kwargs) -> None:
        self.requests: list[dict[str, Any]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    def request(self, method: str, url: str, **kwargs) -> FakeResponse:
        self.requests.append({"method": method, "url": url, **kwargs})
        if url == "http://document-types/api/v1/document-type/":
            return FakeResponse(
                200,
                {"items": [{"identifier": "invoice"}], "next_cursor": None},
            )
        if url.endswith("/document-type/invoice/check"):
            return FakeResponse(
                200,
                {
                    "identifier": "invoice",
                    "validation_scope": "render-request",
                    "valid": True,
                    "normalized_request": kwargs["json"] | {"document_type": "invoice"},
                },
            )
        if url.endswith("/document-type/invoice"):
            return FakeResponse(200, {"identifier": "invoice", "defaults": {}})
        if url.endswith("/versions"):
            return FakeResponse(
                200,
                {
                    "archive": "default",
                    "document_id": "10000000-0000-0000-0000-000000000001",
                    "head_version_id": "20000000-0000-0000-0000-000000000002",
                    "items": [],
                    "next_cursor": None,
                },
            )
        if url.endswith("/details"):
            content = b"test"
            return FakeResponse(
                200,
                {
                    "id": "10000000-0000-0000-0000-000000000001",
                    "archive": "default",
                    "version_id": "20000000-0000-0000-0000-000000000002",
                    "state": "available",
                    "default_representation_id": "30000000-0000-0000-0000-000000000003",
                    "representations": [
                        {
                            "representation_id": "30000000-0000-0000-0000-000000000003",
                            "name": "test.txt",
                            "content_type": "text/plain",
                            "metadata": {},
                            "checksum": {"sha512": hashlib.sha512(content).hexdigest()},
                            "size": len(content),
                            "state": "available",
                        }
                    ],
                },
            )
        if "/archive/archives/default/documents/" in url:
            return FakeResponse(200, b"test", {"content-type": "text/plain"})
        if url.endswith("/capabilities"):
            return FakeResponse(
                200,
                {
                    "capabilities": [{"kind": "archive"}],
                    "pricing": {"enabled": True},
                },
            )
        if kwargs.get("headers"):
            return FakeResponse(
                200,
                {"id": "flow", "payment": {"transaction": "0x123"}},
                {"PAYMENT-RESPONSE": "settled"},
            )
        return FakeResponse(
            402,
            {"error": "Payment is required for this intake."},
            {"PAYMENT-REQUIRED": "requirements"},
        )

    def get(self, url: str, **kwargs) -> FakeResponse:
        return self.request("GET", url, **kwargs)


class CorruptArchiveSession(FakeSession):
    def request(self, method: str, url: str, **kwargs) -> FakeResponse:
        if "/archive/archives/default/documents/" in url and not url.endswith(
            "/details"
        ):
            self.requests.append({"method": method, "url": url, **kwargs})
            return FakeResponse(200, b"fail", {"content-type": "text/plain"})
        return super().request(method, url, **kwargs)


class MetadataOnlyArchiveSession(FakeSession):
    def request(self, method: str, url: str, **kwargs) -> FakeResponse:
        response = super().request(method, url, **kwargs)
        if url.endswith("/details"):
            payload = orjson.loads(response.body)
            payload["state"] = "metadata-only"
            payload["representations"][0]["state"] = "metadata-only"
            return FakeResponse(200, payload)
        return response


def configuration() -> MCPConfiguration:
    return MCPConfiguration(
        intake_base_url="http://intake/api/v1/intake",
        document_type_base_url="http://document-types/api/v1/document-type",
        archive_base_url="http://archive/api/v1/archive",
        request_timeout_seconds=30,
        archive_max_resource_bytes=4096,
        allowed_hosts=("xarta.example.test",),
    )


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"intake_base_url": "ftp://intake"}, "HTTP or HTTPS"),
        ({"archive_base_url": "http://user:pass@archive"}, "credentials"),
        ({"document_type_base_url": "http://types?query=1"}, "query or fragment"),
        ({"request_timeout_seconds": 0}, "timeout must be positive"),
        ({"archive_max_resource_bytes": 0}, "resource limit"),
        ({"allowed_hosts": ()}, "at least one host"),
    ],
)
def test_mcp_configuration_rejects_unsafe_values(overrides, message: str) -> None:
    values = {
        "intake_base_url": "http://intake",
        "document_type_base_url": "http://document-types",
        "archive_base_url": "http://archive",
        "request_timeout_seconds": 30,
        "archive_max_resource_bytes": 4096,
        "allowed_hosts": ("host",),
    }
    values.update(overrides)
    with pytest.raises(ValueError, match=message):
        MCPConfiguration(**values)


@pytest.mark.asyncio
async def test_intake_client_preserves_x402_envelope() -> None:
    session = FakeSession()
    client = IntakeClient(session, "http://intake/api/v1/intake", 30)  # type: ignore[arg-type]

    challenge = await client.submit({"id": "flow", "dag": {}}, None)
    settled = await client.submit({"id": "flow", "dag": {}}, "signed-authorization")

    assert challenge == {
        "status": 402,
        "body": {"error": "Payment is required for this intake."},
        "payment_required": "requirements",
    }
    assert settled["status"] == 200
    assert settled["payment_response"] == "settled"
    assert session.requests[0]["headers"] is None
    assert session.requests[1]["headers"] == {
        "PAYMENT-SIGNATURE": "signed-authorization"
    }


@pytest.mark.asyncio
async def test_mcp_protocol_exposes_capabilities_and_paid_submission(
    monkeypatch,
) -> None:
    session = FakeSession()
    monkeypatch.setattr("xarta.mcp.server.aiohttp.ClientSession", lambda **_: session)
    server = create_mcp_server(configuration())

    async with Client(server, raise_exceptions=True) as client:
        capabilities = await client.call_tool("get_capabilities", {})
        challenge = await client.call_tool(
            "submit_flow", {"flow": {"id": "flow", "dag": {}}}
        )
        settled = await client.call_tool(
            "submit_flow",
            {
                "flow": {"id": "flow", "dag": {}},
                "payment_signature": "signed-authorization",
            },
        )

    assert capabilities.structured_content["body"]["pricing"] == {"enabled": True}
    assert challenge.structured_content["payment_required"] == "requirements"
    assert settled.structured_content["payment_response"] == "settled"


@pytest.mark.asyncio
async def test_mcp_submission_tools_are_marked_irreversible() -> None:
    tools = {
        tool.name: tool
        for tool in await create_mcp_server(configuration()).list_tools()
    }

    assert tools["get_capabilities"].annotations.read_only_hint is True
    assert tools["list_document_types"].annotations.read_only_hint is True
    assert tools["get_archived_document"].annotations.read_only_hint is True
    assert tools["prepare_flow"].annotations.read_only_hint is True
    assert tools["submit_flow"].annotations.destructive_hint is True
    assert tools["submit_flow"].annotations.idempotent_hint is False
    assert tools["submit_profile"].annotations.destructive_hint is True


@pytest.mark.asyncio
async def test_mcp_exposes_document_types_and_bounded_archive_resources(
    monkeypatch,
) -> None:
    session = FakeSession()
    monkeypatch.setattr("xarta.mcp.server.aiohttp.ClientSession", lambda **_: session)
    server = create_mcp_server(configuration())
    document_id = "10000000-0000-0000-0000-000000000001"
    version_id = "20000000-0000-0000-0000-000000000002"
    representation_id = "30000000-0000-0000-0000-000000000003"
    uri = f"xarta-archive://default/documents/{document_id}/versions/{version_id}/representations/{representation_id}"

    async with Client(server, raise_exceptions=True) as client:
        listed = await client.call_tool("list_document_types", {})
        retrieved = await client.call_tool(
            "get_document_type", {"identifier": "invoice"}
        )
        checked = await client.call_tool(
            "check_document_type",
            {
                "identifier": "invoice",
                "payload": {
                    "content_type": "application/json",
                    "data": {"invoice_number": "INV-42"},
                },
            },
        )
        archived = await client.call_tool(
            "get_archived_document", {"document_id": document_id}
        )
        versions = await client.call_tool(
            "list_archived_document_versions", {"document_id": document_id}
        )
        templates = await client.list_resource_templates()
        content = await client.read_resource(uri)

    assert listed.structured_content["body"]["items"][0]["identifier"] == "invoice"
    assert retrieved.structured_content["body"]["identifier"] == "invoice"
    assert checked.structured_content["body"]["valid"] is True
    assert archived.structured_content["body"]["resource"]["uri"] == uri
    assert archived.structured_content["body"]["resource"]["mime_type"] == (
        "text/plain"
    )
    assert archived.content[0].mime_type == "text/plain"
    assert archived.content[0].uri == uri
    assert versions.structured_content["body"]["head_version_id"] == version_id
    assert templates.resource_templates[0].uri_template.startswith("xarta-archive://")
    assert base64.b64decode(content.contents[0].blob) == b"test"


@pytest.mark.asyncio
async def test_mcp_rejects_oversized_archive_resource_before_content_fetch(
    monkeypatch,
) -> None:
    session = FakeSession()
    monkeypatch.setattr("xarta.mcp.server.aiohttp.ClientSession", lambda **_: session)
    limited = configuration()
    limited = MCPConfiguration(
        intake_base_url=limited.intake_base_url,
        document_type_base_url=limited.document_type_base_url,
        archive_base_url=limited.archive_base_url,
        request_timeout_seconds=limited.request_timeout_seconds,
        archive_max_resource_bytes=3,
        allowed_hosts=limited.allowed_hosts,
    )

    async with Client(create_mcp_server(limited), raise_exceptions=True) as client:
        archived = await client.call_tool(
            "get_archived_document",
            {"document_id": "10000000-0000-0000-0000-000000000001"},
        )

    assert archived.structured_content["body"]["resource"] == {
        "available": False,
        "reason": "resource_too_large",
        "maximum_bytes": 3,
    }
    assert not any(
        request["url"].endswith("/versions/20000000-0000-0000-0000-000000000002")
        for request in session.requests
    )


@pytest.mark.asyncio
async def test_mcp_rejects_archive_content_with_wrong_checksum(monkeypatch) -> None:
    session = CorruptArchiveSession()
    monkeypatch.setattr("xarta.mcp.server.aiohttp.ClientSession", lambda **_: session)
    uri = (
        "xarta-archive://default/documents/"
        "10000000-0000-0000-0000-000000000001/versions/"
        "20000000-0000-0000-0000-000000000002/representations/"
        "30000000-0000-0000-0000-000000000003"
    )

    async with Client(
        create_mcp_server(configuration()), raise_exceptions=True
    ) as client:
        with pytest.raises(MCPError, match="checksum does not match"):
            await client.read_resource(uri)


@pytest.mark.asyncio
async def test_mcp_does_not_link_metadata_only_archive_content(monkeypatch) -> None:
    session = MetadataOnlyArchiveSession()
    monkeypatch.setattr("xarta.mcp.server.aiohttp.ClientSession", lambda **_: session)

    async with Client(
        create_mcp_server(configuration()), raise_exceptions=True
    ) as client:
        archived = await client.call_tool(
            "get_archived_document",
            {"document_id": "10000000-0000-0000-0000-000000000001"},
        )

    assert archived.structured_content["body"]["resource"] == {
        "available": False,
        "reason": "content_unavailable",
    }


@pytest.mark.asyncio
async def test_intake_advertises_only_deployed_capability_contracts() -> None:
    request = SimpleNamespace(
        app=SimpleNamespace(
            ctx=SimpleNamespace(
                intake_capabilities=CapabilityManifest(frozenset({"archive", "email"})),
                x402_configuration=SimpleNamespace(
                    protects=lambda name: name == "intake"
                ),
            )
        )
    )

    result = await intake_capabilities(request)
    payload = orjson.loads(result.body)

    assert [item["kind"] for item in payload["capabilities"]] == ["archive", "email"]
    assert payload["pricing"] == {"enabled": True}
    assert "created" in payload["capabilities"][0]["outcomes"]
    assert payload["capabilities"][0]["description"]
    assert payload["capabilities"][0]["example"]["kind"] == "archive"
    assert payload["capabilities"][1]["fields"]["to"] == {
        "required": True,
        "type": "str",
    }
    assert payload["flow_schema"]["$defs"]["node"]["oneOf"] == [
        {"$ref": "#/$defs/node_archive"},
        {"$ref": "#/$defs/node_email"},
    ]


def test_every_advertised_example_matches_schema_and_real_parser() -> None:
    manifest = CapabilityManifest(supported_node_kinds())
    schema = manifest.schema()
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())

    for contract in manifest.contracts():
        flow = {
            "id": "10000000-0000-0000-0000-000000000001",
            "dag": contract["example"],
        }
        assert list(validator.iter_errors(flow)) == [], contract["kind"]
        node = parse(contract["example"])
        assert node.kind == contract["kind"]
        if node.kind not in {"debug", "webhook"}:
            node.interpret()


def test_schema_accepts_an_arbitrary_multicapability_flow() -> None:
    dag = {
        "kind": "generate",
        "documents": [],
        "on": {
            "success": [
                {
                    "kind": "signature",
                    "documents": [],
                    "on": {
                        "success": [
                            {
                                "kind": "archive",
                                "documents": [],
                                "on": {
                                    "created": [
                                        {
                                            "kind": "email",
                                            "to": "recipient@example.test",
                                            "sender": "sender@example.test",
                                            "body": {"plain": "Completed"},
                                        }
                                    ]
                                },
                            }
                        ]
                    },
                }
            ]
        },
    }
    schema = CapabilityManifest(
        frozenset({"generate", "signature", "archive", "email"})
    ).schema()
    flow = {"id": "10000000-0000-0000-0000-000000000001", "dag": dag}

    Draft202012Validator(schema, format_checker=FormatChecker()).validate(flow)
    assert parse(dag).on["success"][0].kind == "signature"


def test_schema_rejects_nodes_not_deployed_to_intake() -> None:
    schema = CapabilityManifest(frozenset({"generate"})).schema()
    flow = {
        "dag": {
            "kind": "generate",
            "documents": [],
            "on": {
                "success": [
                    {
                        "kind": "email",
                        "to": "recipient@example.test",
                        "sender": "sender@example.test",
                        "body": {"plain": "Unexpected"},
                    }
                ]
            },
        }
    }

    assert list(Draft202012Validator(schema).iter_errors(flow))


@pytest.mark.asyncio
async def test_prepare_flow_validates_and_prices_without_committing() -> None:
    quote = SimpleNamespace(
        pricing_revision="pricing-v1",
        selling_price=SimpleNamespace(amount="1.25", currency="USD"),
        expires_at=SimpleNamespace(isoformat=lambda: "2030-01-01T00:00:00+00:00"),
    )
    pricing_engine = SimpleNamespace(quote_flow=Mock(return_value=quote))
    request = SimpleNamespace(
        files={},
        json={
            "id": "10000000-0000-0000-0000-000000000001",
            "dag": {"kind": "generate", "documents": []},
        },
        app=SimpleNamespace(
            ctx=SimpleNamespace(
                intake_capabilities=CapabilityManifest(frozenset({"generate"})),
                x402_configuration=SimpleNamespace(
                    protects=lambda name: name == "intake",
                    intake_resource_url="https://xarta.example.test/api/v1/intake/",
                ),
                pricing_engine=pricing_engine,
            )
        ),
    )

    result = await prepare_flow(request)
    payload = orjson.loads(result.body)

    assert result.status == 200
    assert payload["flow"]["id"] == request.json["id"]
    assert payload["pricing"] == {
        "enabled": True,
        "pricing_revision": "pricing-v1",
        "selling_price": {"amount": "1.25", "currency": "USD"},
        "expires_at": "2030-01-01T00:00:00+00:00",
    }
    pricing_engine.quote_flow.assert_called_once()
