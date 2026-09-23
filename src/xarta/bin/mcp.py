from __future__ import annotations

import argparse

from mcp.server.transport_security import TransportSecuritySettings
from uvicorn import Config
from uvicorn import Server

from xarta import env
from xarta import telemetry
from xarta.mcp import MCPConfiguration
from xarta.mcp import create_mcp_server
from xarta.mcp.configuration import comma_separated_values

parser = argparse.ArgumentParser("Xarta MCP service")
parser.add_argument("--host", default="127.0.0.1")
parser.add_argument("--port", default=8000, type=int)
parser.add_argument("--workers", default=1, type=int)
arguments, _ = parser.parse_known_args()

if arguments.workers != 1:
    raise ValueError("The stateless MCP service uses one worker per container")

configuration = MCPConfiguration(
    intake_base_url=env.extract("MCP_INTAKE_BASE_URL", optional=False, dtype=str),
    document_type_base_url=env.extract(
        "MCP_DOCUMENT_TYPE_BASE_URL", optional=False, dtype=str
    ),
    archive_base_url=env.extract("MCP_ARCHIVE_BASE_URL", optional=False, dtype=str),
    request_timeout_seconds=env.extract(
        "MCP_REQUEST_TIMEOUT_SECONDS", optional=False, dtype=float
    ),
    archive_max_resource_bytes=env.extract(
        "MCP_ARCHIVE_MAX_RESOURCE_BYTES", optional=False, dtype=int
    ),
    allowed_hosts=comma_separated_values(
        env.extract("MCP_ALLOWED_HOSTS", optional=False, dtype=str)
    ),
    allowed_origins=comma_separated_values(
        env.extract("MCP_ALLOWED_ORIGINS", default="", dtype=str)
    ),
)
server = create_mcp_server(configuration)


if __name__ == "__main__":
    transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=list(configuration.allowed_hosts),
        allowed_origins=list(configuration.allowed_origins),
    )
    telemetry.initialize_process("MCP")
    app = server.streamable_http_app(
        host=arguments.host,
        streamable_http_path="/api/mcp",
        json_response=True,
        stateless_http=True,
        transport_security=transport_security,
    )
    try:
        Server(
            Config(
                telemetry.instrument_asgi(app),
                host=arguments.host,
                port=arguments.port,
                log_level=server.settings.log_level.lower(),
            )
        ).run()
    finally:
        telemetry.shutdown()
