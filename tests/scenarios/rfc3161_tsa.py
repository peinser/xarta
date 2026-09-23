"""Local RFC 3161 TSA used by deterministic signature tests and scenarios."""

from __future__ import annotations

import argparse
import asyncio
import socket
import sys
import time

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import aiohttp

from aiohttp import web
from asn1crypto import keys
from asn1crypto import tsp
from pyhanko.keys import load_cert_from_pemder
from pyhanko.keys import load_private_key_from_pemder
from pyhanko.sign.timestamps import DummyTimeStamper

from tests.scenarios.common import ROOT

HOST = "127.0.0.1"
PORT = 8403
URL = f"http://{HOST}:{PORT}/"
CERTIFICATE = ROOT / ".dev/runtime/certificates/tsa-public.pem"
PRIVATE_KEY = ROOT / ".dev/runtime/certificates/tsa-private.pem"
STATE = web.AppKey("state", dict)


async def timestamp(request: web.Request) -> web.Response:
    if request.content_type != "application/timestamp-query":
        return web.Response(status=415)
    try:
        body = await request.read()
        timestamp_request = tsp.TimeStampReq.load(body)
        timestamp_response = request.app[STATE]["timestamper"].request_tsa_response(
            timestamp_request
        )
    except (TypeError, ValueError):
        return web.Response(status=400)
    request.app[STATE]["requests"] += 1
    return web.Response(
        body=timestamp_response.dump(),
        content_type="application/timestamp-reply",
    )


async def statistics(request: web.Request) -> web.Response:
    return web.json_response({"requests": request.app[STATE]["requests"]})


def tsa_app(certificate_path: Path, private_key_path: Path) -> web.Application:
    certificate = load_cert_from_pemder(str(certificate_path))
    private_key: keys.PrivateKeyInfo = load_private_key_from_pemder(
        str(private_key_path), passphrase=None
    )
    app = web.Application(client_max_size=64 * 1024)
    app[STATE] = {
        "timestamper": DummyTimeStamper(certificate, private_key),
        "requests": 0,
    }
    app.router.add_post("/", timestamp)
    app.router.add_get("/statistics", statistics)
    return app


def require_available_port() -> None:
    with socket.socket() as connection:
        connection.settimeout(0.2)
        if connection.connect_ex((HOST, PORT)) == 0:
            raise RuntimeError(f"Port {PORT} is in use; the TSA scenario owns it")


async def wait_until_ready(process: asyncio.subprocess.Process) -> None:
    deadline = time.monotonic() + 10
    async with aiohttp.ClientSession() as session:
        while time.monotonic() < deadline:
            if process.returncode is not None:
                raise RuntimeError("Local RFC 3161 TSA exited during startup")
            try:
                async with session.get(f"{URL}statistics") as response:
                    if response.status == 200:
                        return
            except aiohttp.ClientError:
                pass
            await asyncio.sleep(0.05)
    raise TimeoutError("Local RFC 3161 TSA did not become ready")


@asynccontextmanager
async def local_tsa() -> AsyncIterator[None]:
    require_available_port()
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "tests.scenarios.rfc3161_tsa",
        "--serve",
    )
    try:
        await wait_until_ready(process)
        yield
    finally:
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except TimeoutError:
                process.kill()
                await process.wait()


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--certificate", type=Path, default=CERTIFICATE)
    parser.add_argument("--private-key", type=Path, default=PRIVATE_KEY)
    return parser.parse_args()


if __name__ == "__main__":
    arguments = parse_arguments()
    if not arguments.serve:
        raise SystemExit("Use --serve to run the local RFC 3161 TSA")
    web.run_app(
        tsa_app(arguments.certificate, arguments.private_key),
        host=HOST,
        port=PORT,
        print=None,
    )
