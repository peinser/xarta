from __future__ import annotations

import asyncio
import hashlib

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote
from uuid import UUID

import aiohttp

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ResourceError
from mcp.server.mcpserver.exceptions import ResourceNotFoundError
from mcp.types import CallToolResult
from mcp.types import ResourceLink
from mcp.types import ToolAnnotations
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.responses import Response

from xarta.http.sessions import HTTPRequestManager
from xarta.mcp.archive import ArchiveClient
from xarta.mcp.configuration import MCPConfiguration
from xarta.mcp.documents import DocumentTypeClient
from xarta.mcp.intake import IntakeClient


@dataclass(frozen=True, slots=True)
class MCPDependencies:
    intake: IntakeClient
    documents: DocumentTypeClient
    archive: ArchiveClient


def create_mcp_server(configuration: MCPConfiguration) -> MCPServer[MCPDependencies]:
    active_dependencies: MCPDependencies | None = None

    @asynccontextmanager
    async def lifespan(_: MCPServer) -> AsyncIterator[MCPDependencies]:
        nonlocal active_dependencies
        timeout = aiohttp.ClientTimeout(
            total=configuration.request_timeout_seconds,
            connect=min(configuration.request_timeout_seconds, 5),
        )
        async with await HTTPRequestManager.open(
            asyncio.get_running_loop(), timeout=timeout
        ) as session:
            active_dependencies = MCPDependencies(
                intake=IntakeClient(
                    session,
                    configuration.intake_base_url,
                    configuration.request_timeout_seconds,
                ),
                documents=DocumentTypeClient(
                    session,
                    configuration.document_type_base_url,
                    configuration.request_timeout_seconds,
                ),
                archive=ArchiveClient(
                    session,
                    configuration.archive_base_url,
                    configuration.request_timeout_seconds,
                ),
            )
            try:
                yield active_dependencies
            finally:
                active_dependencies = None

    server = MCPServer(
        "xarta",
        title="Xarta document workflows",
        description=(
            "Discover document types, construct workflows and retrieve archived documents."
        ),
        instructions=(
            "Consult document types before constructing rendered documents. Call "
            "get_capabilities before constructing a custom DAG. Prefer an exact "
            "flow profile when one matches the request. Otherwise compose deployed "
            "capabilities through outcome edges, then call prepare_flow to validate and "
            "quote before submit_flow. A 402 result includes payment_required; authorize "
            "those x402 v2 terms and retry the identical submission with payment_signature."
        ),
        version="1.1.0",
        lifespan=lifespan,
    )

    def intake(ctx: Context[MCPDependencies]) -> IntakeClient:
        return ctx.request_context.lifespan_context.intake

    def documents(ctx: Context[MCPDependencies]) -> DocumentTypeClient:
        return ctx.request_context.lifespan_context.documents

    def archive_client(ctx: Context[MCPDependencies]) -> ArchiveClient:
        return ctx.request_context.lifespan_context.archive

    @server.tool(
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        )
    )
    async def list_document_types(
        ctx: Context[MCPDependencies], limit: int = 50, cursor: str | None = None
    ) -> dict[str, Any]:
        """List deployed document types and their public rendering defaults."""
        return await documents(ctx).list(limit=limit, cursor=cursor)

    @server.tool(
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        )
    )
    async def get_document_type(
        identifier: str, ctx: Context[MCPDependencies]
    ) -> dict[str, Any]:
        """Consult one document type, including its current template and defaults."""
        return await documents(ctx).retrieve(identifier)

    @server.tool(
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        )
    )
    async def check_document_type(
        identifier: str,
        payload: dict[str, Any],
        ctx: Context[MCPDependencies],
        content_type: str | None = None,
        template_engine: str | None = None,
        template_engine_options: dict[str, Any] | None = None,
        parameters: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Check generic render-request structure and resolve document-type defaults without rendering."""
        request: dict[str, Any] = {"payload": payload}
        optional = {
            "content_type": content_type,
            "template_engine": template_engine,
            "template_engine_options": template_engine_options,
            "parameters": parameters,
            "metadata": metadata,
        }
        request.update(
            {key: value for key, value in optional.items() if value is not None}
        )
        return await documents(ctx).check(identifier, request)

    @server.tool(
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        )
    )
    async def get_capabilities(ctx: Context[MCPDependencies]) -> dict[str, Any]:
        """Return the complete recursive flow schema, deployed node explanations/examples, outcomes and pricing state."""
        return await intake(ctx).get("/capabilities")

    @server.tool(
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        )
    )
    async def list_flow_profiles(ctx: Context[MCPDependencies]) -> dict[str, Any]:
        """List exact server-owned flow profiles and their typed business inputs."""
        return await intake(ctx).list_profiles()

    @server.tool(
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        )
    )
    async def get_flow_profile(
        name: str, version: int, ctx: Context[MCPDependencies]
    ) -> dict[str, Any]:
        """Get one exact flow profile contract. Versions must never be guessed or aliased."""
        return await intake(ctx).get_profile(name, version)

    @server.tool(
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        )
    )
    async def prepare_flow(
        flow: dict[str, Any], ctx: Context[MCPDependencies]
    ) -> dict[str, Any]:
        """Validate and normalize a caller-authored DAG without executing it; quote it when pricing is enabled."""
        return await intake(ctx).prepare(flow)

    @server.tool(
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        )
    )
    async def prepare_profile(
        name: str,
        version: int,
        inputs: dict[str, Any],
        flow_id: str,
        ctx: Context[MCPDependencies],
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        """Validate, compile and quote an exact profile submission without executing it."""
        flow = {
            "id": flow_id,
            "delivery_profile": f"{name}@{version}",
            "delivery_values": inputs,
        }
        if correlation_id is not None:
            flow["correlation_id"] = correlation_id
        return await intake(ctx).prepare(flow)

    @server.tool(
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=True,
            idempotent_hint=False,
            open_world_hint=True,
        )
    )
    async def submit_flow(
        flow: dict[str, Any],
        ctx: Context[MCPDependencies],
        payment_signature: str | None = None,
    ) -> dict[str, Any]:
        """Submit a caller-authored DAG. This may cause irreversible delivery after x402 settlement."""
        return await intake(ctx).submit(flow, payment_signature)

    @server.tool(
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=True,
            idempotent_hint=False,
            open_world_hint=True,
        )
    )
    async def submit_profile(
        name: str,
        version: int,
        inputs: dict[str, Any],
        flow_id: str,
        ctx: Context[MCPDependencies],
        correlation_id: str | None = None,
        payment_signature: str | None = None,
    ) -> dict[str, Any]:
        """Submit an exact profile. This may cause irreversible delivery after x402 settlement."""
        payload: dict[str, Any] = {"id": flow_id, "inputs": inputs}
        if correlation_id is not None:
            payload["correlation_id"] = correlation_id
        return await intake(ctx).submit_profile(
            name, version, payload, payment_signature
        )

    @server.tool(
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        )
    )
    async def get_archived_document(
        document_id: UUID,
        ctx: Context[MCPDependencies],
        archive: str = "default",
        version_id: UUID | None = None,
    ) -> CallToolResult:
        """Get archive metadata and a bounded resource link pinned to one immutable version."""
        result = await archive_client(ctx).details(archive, document_id, version_id)
        if result["status"] != 200 or not isinstance(result["body"], dict):
            return CallToolResult(content=[], structured_content=result)

        details = dict(result["body"])
        resolved_version = details.get("version_id")
        default_representation_id = details.get("default_representation_id")
        representations = details.get("representations", [])
        default_representation = next(
            (
                item
                for item in representations
                if item.get("representation_id") == default_representation_id
            ),
            None,
        )
        size = (
            default_representation.get("size")
            if isinstance(default_representation, dict)
            else None
        )
        if (
            isinstance(default_representation, dict)
            and default_representation.get("state") == "available"
            and isinstance(size, int)
            and resolved_version
        ):
            if size <= configuration.archive_max_resource_bytes:
                uri = (
                    f"xarta-archive://{quote(archive, safe='')}/documents/"
                    f"{document_id}/versions/{resolved_version}/representations/"
                    f"{default_representation_id}"
                )
                details["resource"] = {
                    "uri": uri,
                    "mime_type": default_representation["content_type"],
                    "size": size,
                }
                result["body"] = details
                return CallToolResult(
                    content=[
                        ResourceLink(
                            name=f"{archive}/{document_id}@{resolved_version}",
                            uri=uri,
                            description=(
                                "Immutable archived document content; use the tool metadata "
                                "for its verified content type"
                            ),
                            mime_type=default_representation["content_type"],
                            size=size,
                        )
                    ],
                    structured_content=result,
                )
            details["resource"] = {
                "available": False,
                "reason": "resource_too_large",
                "maximum_bytes": configuration.archive_max_resource_bytes,
            }
        else:
            details["resource"] = {
                "available": False,
                "reason": "content_unavailable",
            }
        result["body"] = details
        return CallToolResult(content=[], structured_content=result)

    @server.tool(
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        )
    )
    async def list_archived_document_versions(
        document_id: UUID,
        ctx: Context[MCPDependencies],
        archive: str = "default",
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """List immutable versions of one archived document using an opaque cursor."""
        return await archive_client(ctx).versions(
            archive, document_id, limit=limit, cursor=cursor
        )

    @server.resource(
        "xarta-archive://{archive}/documents/{document_id}/versions/{version_id}/representations/{representation_id}",
        name="archived-document-content",
        title="Archived document content",
        description="Read one size-bounded immutable archived document version.",
        mime_type="application/octet-stream",
    )
    async def archived_document_content(
        archive: str,
        document_id: str,
        version_id: str,
        representation_id: str,
    ) -> bytes:
        try:
            parsed_document_id = UUID(document_id)
            parsed_version_id = UUID(version_id)
            parsed_representation_id = UUID(representation_id)
        except ValueError as ex:
            raise ResourceNotFoundError(
                "Archive resource identifiers are invalid"
            ) from ex

        if active_dependencies is None:
            raise ResourceError("The archive service is unavailable")
        archive_client = active_dependencies.archive
        details_response = await archive_client.details(
            archive, parsed_document_id, parsed_version_id
        )
        if details_response["status"] == 404:
            raise ResourceNotFoundError("Archived document version was not found")
        if details_response["status"] != 200 or not isinstance(
            details_response["body"], dict
        ):
            raise ResourceError("Archived document metadata is unavailable")
        details = details_response["body"]
        representation = next(
            (
                item
                for item in details.get("representations", [])
                if item.get("representation_id") == str(parsed_representation_id)
            ),
            None,
        )
        if not isinstance(representation, dict):
            raise ResourceNotFoundError("Archived representation was not found")
        if representation.get("state") != "available":
            raise ResourceError("Archived document content is unavailable")
        size = representation.get("size")
        if not isinstance(size, int) or size > configuration.archive_max_resource_bytes:
            raise ResourceError("Archived document exceeds the MCP resource size limit")

        status, content, content_type = await archive_client.content(
            archive,
            parsed_document_id,
            parsed_version_id,
            parsed_representation_id,
        )
        if status == 404:
            raise ResourceNotFoundError("Archived document content was not found")
        if status != 200:
            raise ResourceError("Archived document content is unavailable")
        if len(content) != size:
            raise ResourceError("Archived document size does not match its metadata")
        checksum = (representation.get("checksum") or {}).get("sha512")
        if (
            not isinstance(checksum, str)
            or hashlib.sha512(content).hexdigest() != checksum
        ):
            raise ResourceError(
                "Archived document checksum does not match its metadata"
            )
        declared_content_type = representation.get("content_type")
        if (
            isinstance(declared_content_type, str)
            and content_type
            and content_type.partition(";")[0] != declared_content_type
        ):
            raise ResourceError(
                "Archived document content type does not match its metadata"
            )
        return content

    @server.prompt(name="construct_document_flow")
    def construct_document_flow(request: str) -> str:
        """Guide an agent in selecting a profile or constructing a custom Xarta DAG."""
        return (
            f"Construct a Xarta document workflow for this request: {request}\n\n"
            "First call list_document_types when rendering a document, then call "
            "get_capabilities and list_flow_profiles. Use an exact profile if "
            "its declared inputs satisfy the request. Otherwise build a custom DAG using "
            "only deployed kinds and accepted outcome edges. Give the flow an explicit "
            "UUID, call prepare_flow, correct validation errors, and show the quote before "
            "calling submit_flow. If submission returns status 402, authorize only the "
            "returned payment_required terms and retry the identical flow with the resulting "
            "payment_signature."
        )

    @server.custom_route("/.info/healthz", methods=["GET"])
    async def healthz(_: Request) -> Response:
        return Response(status_code=204)

    @server.custom_route("/.info/readyz", methods=["GET"])
    async def readyz(_: Request) -> Response:
        return JSONResponse({"status": "ready"})

    return server
