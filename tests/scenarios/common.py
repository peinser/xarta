"""Shared process, database, timing, and reporting utilities for scenarios."""

from __future__ import annotations

import asyncio
import datetime
import json
import math
import os
import pkgutil
import signal
import socket
import statistics
import sys
import time
import uuid

from collections.abc import AsyncIterator
from collections.abc import Collection
from collections.abc import Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiohttp
import asyncpg

ROOT = Path(__file__).parents[2]
HOST = "127.0.0.1"
PORT = 8000
BASE_URL = f"http://{HOST}:{PORT}"
INTAKE_BASE_URL = os.environ.get("SCENARIO_INTAKE_BASE_URL", BASE_URL).rstrip("/")
ARCHIVE_BASE_URL = os.environ.get("SCENARIO_ARCHIVE_BASE_URL", BASE_URL).rstrip("/")
SCENARIO_ARTIFACTS = ROOT / ".dev/runtime/scenarios"


def scenario_standalone_application():
    """Load the standalone app without treating postal internals as a service."""
    original_iter_modules = pkgutil.iter_modules

    def service_modules(path=None, prefix=""):
        for module in original_iter_modules(path, prefix):
            if module.name != "postal_local":
                yield module

    pkgutil.iter_modules = service_modules
    try:
        from xarta.bin.standalone import app
    finally:
        pkgutil.iter_modules = original_iter_modules
    if os.environ.get("SCENARIO_DECODE_POSTGRES_JSON") == "true":

        @app.listener("after_server_start")
        async def decode_postgres_json(application) -> None:
            pool = application.ctx.postal_repository.pool
            connections = []
            try:
                for _ in range(pool.get_max_size()):
                    connections.append(await pool.acquire())
                for connection in connections:
                    await connection.set_type_codec(
                        "jsonb",
                        schema="pg_catalog",
                        encoder=lambda value: (
                            value if isinstance(value, str) else json.dumps(value)
                        ),
                        decoder=json.loads,
                        format="text",
                    )
            finally:
                for connection in connections:
                    await pool.release(connection)

    return app


def identifier() -> str:
    return str(uuid.uuid4())


def percentile(values: list[float], percent: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * percent
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def statistics_for(values: list[float]) -> dict[str, float | int]:
    return {
        "count": len(values),
        "mean_ms": statistics.fmean(values),
        "median_ms": statistics.median(values),
        "stdev_ms": statistics.stdev(values) if len(values) > 1 else 0.0,
        "min_ms": min(values),
        "p90_ms": percentile(values, 0.90),
        "p95_ms": percentile(values, 0.95),
        "p99_ms": percentile(values, 0.99),
        "max_ms": max(values),
    }


def timestamp(event: Mapping[str, Any]) -> datetime.datetime:
    return datetime.datetime.fromisoformat(event["timestamp"])


def elapsed(start: Mapping[str, Any], end: Mapping[str, Any]) -> float:
    return (timestamp(end) - timestamp(start)).total_seconds() * 1000


def application_environment(
    capabilities: Collection[str], extra: Mapping[str, str] | None = None
) -> dict[str, str]:
    environment = os.environ.copy()
    environment.pop("EVM_PRIVATE_KEY", None)
    environment.pop("X402_SELLER_PRIVATE_KEY", None)
    environment.update(
        {
            "ARCHIVE_POSTGRESQL_DATABASE": os.environ.get(
                "ARCHIVE_POSTGRESQL_DATABASE", "archive"
            ),
            "ARCHIVE_POSTGRESQL_HOST": os.environ.get(
                "ARCHIVE_POSTGRESQL_HOST", "postgres"
            ),
            "ARCHIVE_POSTGRESQL_PASSWORD": os.environ.get(
                "ARCHIVE_POSTGRESQL_PASSWORD", "dev"
            ),
            "ARCHIVE_POSTGRESQL_USER": os.environ.get("ARCHIVE_POSTGRESQL_USER", "dev"),
            "INTAKE_CAPABILITIES": json.dumps(sorted(capabilities)),
            "DOCUMENT_SIGN_PUBLIC_PEM": str(
                ROOT / ".dev/runtime/certificates/public.pem"
            ),
            "DOCUMENT_SIGN_PRIVATE_PEM": str(
                ROOT / ".dev/runtime/certificates/private.pem"
            ),
            "DOCUMENT_SIGN_CHAIN_PEM": str(
                ROOT / ".dev/runtime/certificates/chain.pem"
            ),
            "SIGNATURE_CONFIG_PATH": str(ROOT / ".dev/conf/signature.json"),
            "TIMESTAMP_PROVIDERS_CONFIG_PATH": str(
                ROOT / ".dev/conf/timestamp-providers.json"
            ),
            "POSTAL_CONFIGURATIONS_CONFIG_PATH": str(
                ROOT / ".dev/conf/postal-destinations.json"
            ),
            "POSTAL_LOCAL_PROFILES_CONFIG_PATH": str(
                ROOT / ".dev/conf/postal-local-profiles.json"
            ),
            "POSTAL_BPOST_PROFILES_CONFIG_PATH": str(
                ROOT / ".dev/conf/postal-bpost-profiles.json"
            ),
            "POSTAL_LOCAL_CAPACITY_CONFIG_PATH": str(
                ROOT / ".dev/conf/postal-local-capacity.json"
            ),
            "POSTAL_LOCAL_STATIONS_CONFIG_PATH": str(
                ROOT / ".dev/conf/postal-local-stations.json"
            ),
            "POSTAL_LOCAL_STORAGE_CONFIG_PATH": str(
                ROOT / ".dev/conf/postal-local-storage.json"
            ),
            "SIGNATURE_ALLOW_DEVELOPMENT_POLICIES": "true",
        }
    )
    if extra:
        environment.update(extra)
    return environment


def require_available_port() -> None:
    with socket.socket() as connection:
        connection.settimeout(0.2)
        if connection.connect_ex((HOST, PORT)) == 0:
            raise RuntimeError(
                f"Port {PORT} is in use. Stop the local Xarta app before running "
                "a scenario; the scenario owns its application process."
            )


async def wait_until_ready(process: asyncio.subprocess.Process, log_path: Path) -> None:
    timeout = time.monotonic() + 30
    async with aiohttp.ClientSession() as session:
        while time.monotonic() < timeout:
            if process.returncode is not None:
                raise RuntimeError(
                    f"Scenario app exited during startup; inspect {log_path}"
                )
            try:
                async with session.get(f"{BASE_URL}/.info/healthz") as response:
                    if response.status == 204:
                        await asyncio.sleep(0.5)
                        return
            except aiohttp.ClientError:
                pass
            await asyncio.sleep(0.1)
    raise TimeoutError(f"Scenario app did not become ready; inspect {log_path}")


async def stop_application(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    process.send_signal(signal.SIGTERM)
    try:
        await asyncio.wait_for(process.wait(), timeout=15)
    except TimeoutError:
        process.kill()
        await process.wait()


@asynccontextmanager
async def standalone_application(
    log_path: Path,
    capabilities: Collection[str],
    extra_environment: Mapping[str, str] | None = None,
) -> AsyncIterator[None]:
    require_available_port()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("wb") as log:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "sanic",
            "--single-process",
            "--host",
            HOST,
            "--port",
            str(PORT),
            "--factory",
            "tests.scenarios.common:scenario_standalone_application",
            cwd=ROOT,
            env=application_environment(capabilities, extra_environment),
            stdout=log,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            await wait_until_ready(process, log_path)
            yield
        finally:
            await stop_application(process)


@asynccontextmanager
async def compose_application(
    log_path: Path,
    compose_path: Path,
    services: Collection[str] | None = None,
) -> AsyncIterator[None]:
    """Run and capture a dedicated split-service Compose application."""
    require_available_port()
    compose_path = compose_path.resolve()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    command = ("docker", "compose", "-f", str(compose_path))

    async def run(*arguments: str) -> None:
        process = await asyncio.create_subprocess_exec(
            *command,
            *arguments,
            cwd=ROOT,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        output, _ = await process.communicate()
        if process.returncode:
            raise RuntimeError(
                f"Compose command failed ({' '.join(arguments)}):\n"
                f"{output.decode(errors='replace')}"
            )

    async def collect_logs() -> None:
        with log_path.open("wb") as log:
            process = await asyncio.create_subprocess_exec(
                *command,
                "logs",
                "--no-color",
                "--no-log-prefix",
                cwd=ROOT,
                stdout=log,
                stderr=asyncio.subprocess.STDOUT,
            )
            if await process.wait():
                raise RuntimeError(f"Could not collect Compose logs in {log_path}")

    try:
        await run(
            "up",
            "--detach",
            "--force-recreate",
            "--remove-orphans",
            "--wait",
            "--wait-timeout",
            "90",
            *(services or ()),
        )
        yield
    finally:
        active_exception = sys.exception()
        cleanup_error: Exception | None = None
        try:
            await collect_logs()
        except Exception as error:
            cleanup_error = error
        try:
            await run("down", "--remove-orphans", "--timeout", "15")
        except Exception as error:
            cleanup_error = cleanup_error or error
        if active_exception is None and cleanup_error is not None:
            raise cleanup_error


def _span_tags(span: Mapping[str, Any]) -> dict[str, Any]:
    return {tag["key"]: tag.get("value") for tag in span.get("tags", ())}


def _validate_split_trace(
    trace_data: Mapping[str, Any],
    flow_id: str,
    expected_services: Collection[str],
) -> None:
    spans = trace_data.get("spans", ())
    processes = trace_data.get("processes", {})
    services = {
        processes[span["processID"]]["serviceName"]
        for span in spans
        if span.get("processID") in processes
    }
    expected = set(expected_services)
    if services != expected:
        raise RuntimeError(
            f"Trace services differ: expected {sorted(expected)}, found {sorted(services)}"
        )

    tagged = {span["spanID"]: (span, _span_tags(span)) for span in spans}
    errors = [
        span["operationName"]
        for span, tags in tagged.values()
        if tags.get("error") in {True, "true"}
        or str(tags.get("otel.status_code", "")).upper() == "ERROR"
    ]
    if errors:
        raise RuntimeError(f"Trace contains error spans: {errors}")

    producers = {
        span_id: (span, tags)
        for span_id, (span, tags) in tagged.items()
        if tags.get("xarta.flow.id") == flow_id
        and tags.get("messaging.operation.type") == "publish"
    }
    consumers = [
        (span, tags)
        for span, tags in tagged.values()
        if tags.get("xarta.flow.id") == flow_id
        and tags.get("messaging.operation.type") == "process"
    ]
    if not producers or not consumers:
        raise RuntimeError("Trace has no flow-scoped NATS producer/consumer spans")

    consumed_producers = set()
    trace_id = trace_data.get("traceID")
    for consumer, consumer_tags in consumers:
        parent_ids = {
            reference["spanID"]
            for reference in consumer.get("references", ())
            if reference.get("refType") == "CHILD_OF"
            and reference.get("traceID") == trace_id
        }
        parent_ids &= producers.keys()
        if len(parent_ids) != 1:
            raise RuntimeError(
                f"Consumer {consumer['operationName']!r} has no unique producer parent"
            )
        producer_id = parent_ids.pop()
        producer_tags = producers[producer_id][1]
        for key in (
            "messaging.destination.name",
            "messaging.message.id",
            "xarta.flow.id",
        ):
            if producer_tags.get(key) != consumer_tags.get(key):
                raise RuntimeError(
                    f"Consumer {consumer['operationName']!r} differs from its producer on {key}"
                )
        consumed_producers.add(producer_id)

    missing_consumers = producers.keys() - consumed_producers
    if missing_consumers:
        names = [
            producers[span_id][0]["operationName"] for span_id in missing_consumers
        ]
        raise RuntimeError(f"NATS producer spans have no consumer: {names}")


async def assert_split_trace(
    flow_id: str,
    expected_services: Collection[str],
    *,
    timeout: float = 30,
    settle: float = 1,
) -> str:
    """Validate one exported split-service trace through the Jaeger API."""
    endpoint = os.environ.get("SCENARIO_JAEGER_QUERY_URL", "").rstrip("/")
    if not endpoint:
        raise ValueError("SCENARIO_JAEGER_QUERY_URL is required for trace validation")
    if not expected_services:
        raise ValueError("Expected trace services cannot be empty")

    deadline = time.monotonic() + timeout
    last_error = "trace not found"
    stable_since: float | None = None
    fingerprint: frozenset[str] | None = None
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session:
        while time.monotonic() < deadline:
            try:
                trace_ids = set()
                for service in expected_services:
                    async with session.get(
                        f"{endpoint}/api/traces",
                        params={
                            "service": service,
                            "tags": json.dumps(
                                {"xarta.flow.id": flow_id}, separators=(",", ":")
                            ),
                            "lookback": "1h",
                            "limit": "20",
                        },
                    ) as response:
                        response.raise_for_status()
                        result = await response.json()
                    trace_ids.update(
                        trace["traceID"] for trace in result.get("data", ())
                    )
                if len(trace_ids) != 1:
                    raise RuntimeError(
                        f"Expected one shared trace, found {sorted(trace_ids)}"
                    )

                trace_id = trace_ids.pop()
                async with session.get(f"{endpoint}/api/traces/{trace_id}") as response:
                    response.raise_for_status()
                    result = await response.json()
                trace_data = result["data"][0]
                _validate_split_trace(trace_data, flow_id, expected_services)
                current = frozenset(span["spanID"] for span in trace_data["spans"])
                now = time.monotonic()
                if current != fingerprint:
                    fingerprint = current
                    stable_since = now
                elif stable_since is None:
                    stable_since = now
                elif stable_since is not None and now - stable_since >= settle:
                    return trace_id
            except (aiohttp.ClientError, KeyError, RuntimeError) as ex:
                last_error = str(ex)
                stable_since = None
            await asyncio.sleep(0.2)

    raise TimeoutError(f"Trace validation timed out for flow {flow_id}: {last_error}")


async def connect_postgres() -> asyncpg.Connection:
    return await asyncpg.connect(
        user=os.environ.get("POSTGRESQL_USER", "dev"),
        password=os.environ.get("POSTGRESQL_PASSWORD", "dev"),
        database=os.environ.get("POSTGRESQL_DATABASE", "dev"),
        host=os.environ.get("POSTGRESQL_HOST", "postgres"),
    )


async def connect_archive_postgres() -> asyncpg.Connection:
    return await asyncpg.connect(
        user=os.environ.get("ARCHIVE_POSTGRESQL_USER", "dev"),
        password=os.environ.get("ARCHIVE_POSTGRESQL_PASSWORD", "dev"),
        database=os.environ.get("ARCHIVE_POSTGRESQL_DATABASE", "archive"),
        host=os.environ.get("ARCHIVE_POSTGRESQL_HOST", "postgres"),
    )
