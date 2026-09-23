r"""
A command line utility for managing and booting services.
"""

from __future__ import annotations

import argparse
import asyncio

from typing import TYPE_CHECKING

from sanic import response

from xarta.services.versions import load

from .utils import create_app

if TYPE_CHECKING:
    from sanic import HTTPResponse
    from sanic import Request


parser = argparse.ArgumentParser("Service Bootstrap Manager")

parser.add_argument(
    "--run", type=str, default=[], action="append", help="Lists the service to execute."
)
parser.add_argument(
    "--host",
    type=str,
    default=None,
    help="IP address to run the host on (default: none).",
)
parser.add_argument(
    "--port",
    type=int,
    default=8000,
    help="The port to run the Sanic service on (default: 8000).",
)
parser.add_argument(
    "--debug",
    action="store_true",
    default=False,
    help="Run in debug mode (default: false).",
)
parser.add_argument(
    "--fast",
    action="store_true",
    default=False,
    help="Fast-mode, disables logging (default: false).",
)
parser.add_argument(
    "--reload",
    action="store_true",
    default=False,
    help="Enable hot-reloading (default: false).",
)
parser.add_argument(
    "--workers",
    type=int,
    default=1,
    help="Workers to process incoming requests (default: 1).",
)
parser.add_argument(
    "--access-logs", action="store_true", help="Enable access logs (default: false)."
)

arguments, _ = parser.parse_known_args()


if not len(arguments.run):
    raise ValueError(
        "No services have been specified! Add arguments to the `--run` flag."
    )


app = create_app("Service")

_services = []
for proposal in arguments.run:
    service, version = proposal.split("=")
    group = load(service)

    app.blueprint(group.blueprint(version=version))
    _services.append(
        {
            "service": service,
            "version": version,
        }
    )


if not _services:
    raise ValueError(
        "No valid service has been added to the runtime! Check the `--run` argument."
    )


@app.route("/.info/healthz", methods=["GET"])
async def healthz(_: Request) -> HTTPResponse:
    return response.empty()


@app.route("/.info/readyz", methods=["GET"])
async def readyz(request: Request) -> HTTPResponse:
    runtimes = getattr(request.app.ctx, "nats_runtimes", {})
    if any(not runtime.ready for runtime in runtimes.values()):
        return response.json({"status": "not-ready", "dependency": "nats"}, status=503)
    pools = getattr(request.app.ctx, "postgres_pools", {})
    try:
        async with asyncio.timeout(2):
            await asyncio.gather(
                *(pool.fetchval("SELECT 1") for pool in pools.values())
            )
    except Exception:
        return response.json(
            {"status": "not-ready", "dependency": "postgresql"}, status=503
        )
    return response.json({"status": "ready"})


if __name__ == "__main__":
    app.run(
        workers=arguments.workers,
        fast=arguments.fast,
        host=arguments.host,
        port=arguments.port,
        debug=arguments.debug,
        auto_reload=arguments.reload,
        access_log=arguments.access_logs,
        single_process=arguments.workers == 1,
    )
