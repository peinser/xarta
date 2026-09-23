"""Run and measure the unpaid Jinja selftest preview scenario."""

from __future__ import annotations

import argparse
import asyncio
import json
import time

from pathlib import Path
from typing import Any

import aiohttp
import asyncpg

from tests.fixtures.jinja_selftest import SELFTEST_DEFAULT_MARKERS
from tests.fixtures.jinja_selftest import SELFTEST_TEMPLATE_ENGINE
from tests.fixtures.jinja_selftest import jinja_selftest_template_engine_options
from tests.scenarios.common import BASE_URL
from tests.scenarios.common import SCENARIO_ARTIFACTS
from tests.scenarios.common import connect_postgres
from tests.scenarios.common import identifier
from tests.scenarios.common import standalone_application
from tests.scenarios.common import statistics_for

DEFAULT_OUTPUT = SCENARIO_ARTIFACTS / "preview/results.json"
DEFAULT_LOG = SCENARIO_ARTIFACTS / "preview/application.log"
PAYMENT_RESPONSE_HEADERS = ("PAYMENT-REQUIRED", "PAYMENT-RESPONSE")


async def create_document_type(connection: asyncpg.Connection) -> str:
    document_type = f"scenario-preview-{identifier()}"
    await connection.execute(
        "INSERT INTO document_types "
        "(identifier, default_template_engine, default_content_type) "
        "VALUES ($1, $2, $3)",
        document_type,
        SELFTEST_TEMPLATE_ENGINE,
        "text/html",
    )
    return document_type


async def execute(count: int, document_type: str) -> dict[str, Any]:
    request = {
        "document_type": document_type,
        "content_type": "text/html",
        "template_engine": SELFTEST_TEMPLATE_ENGINE,
        "template_engine_options": jinja_selftest_template_engine_options(),
        "payload": {"content_type": "application/json", "data": {}},
    }
    durations: list[float] = []
    timeout = aiohttp.ClientTimeout(total=35)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for index in range(count):
            started = time.perf_counter_ns()
            async with session.post(
                f"{BASE_URL}/api/v1/preview/",
                json=request,
                headers={"correlation-id": identifier()},
            ) as response:
                body = await response.text()
                duration_ms = (time.perf_counter_ns() - started) / 1_000_000
                if response.status == 402:
                    raise RuntimeError("Preview unexpectedly required an x402 payment")
                if response.status != 200:
                    raise RuntimeError(
                        f"Preview {index + 1} returned HTTP {response.status}: {body!r}"
                    )
                content_type = response.headers.get("content-type", "").split(";", 1)[0]
                if content_type != "text/html":
                    raise RuntimeError(
                        f"Preview {index + 1} returned {content_type!r}, not text/html"
                    )
                payment_headers = [
                    name
                    for name in PAYMENT_RESPONSE_HEADERS
                    if name in response.headers
                ]
                if payment_headers:
                    raise RuntimeError(
                        "Preview unexpectedly returned x402 headers: "
                        f"{', '.join(payment_headers)}"
                    )
                missing = [
                    marker for marker in SELFTEST_DEFAULT_MARKERS if marker not in body
                ]
                if missing:
                    raise RuntimeError(
                        f"Preview {index + 1} omitted selftest markers: {missing}"
                    )

            durations.append(duration_ms)
            print(f"completed={index + 1} latest_ms={duration_ms:.3f}", flush=True)

    return {
        "functional": {
            "previews_completed": len(durations),
            "outputs_validated": len(durations),
            "x402_inactive": True,
        },
        "performance": statistics_for(durations),
    }


def application_errors(log_path: Path) -> list[str]:
    errors: list[str] = []
    for line in log_path.read_text().splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            if " ERROR: " in line:
                errors.append(line)
            continue
        if event.get("level") == "error":
            errors.append(line)
    return errors


async def main(count: int, output_path: Path, log_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    connection = await connect_postgres()
    document_type = await create_document_type(connection)
    try:
        async with standalone_application(
            log_path,
            {
                "archive",
                "bundle",
                "doccle",
                "email",
                "generate",
                "search-index",
                "sftp",
                "signature",
                "wait-for",
                "webhook",
            },
            {
                "X402_ENABLED": "false",
                "X402_PREVIEW_ENABLED": "false",
            },
        ):
            report = await execute(count, document_type)
    finally:
        await connection.execute(
            "DELETE FROM document_types WHERE identifier = $1", document_type
        )
        await connection.close()

    report["performance"]["errors"] = application_errors(log_path)
    if report["performance"]["errors"]:
        raise RuntimeError("Application log contains errors")
    output_path.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({**report["functional"], **report["performance"]}, indent=2))
    print(f"report={output_path}")
    print(f"application_log={log_path}")


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG)
    arguments = parser.parse_args()
    if arguments.count < 1:
        parser.error("--count must be at least 1")
    return arguments


if __name__ == "__main__":
    arguments = parse_arguments()
    asyncio.run(main(arguments.count, arguments.output, arguments.log))
