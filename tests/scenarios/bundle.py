"""Run and measure the synchronous archive/bundle/archive workflow scenario."""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import time
import uuid
import zipfile

from pathlib import Path
from typing import Any

import aiohttp
import asyncpg

from common import BASE_URL
from common import SCENARIO_ARTIFACTS
from common import connect_archive_postgres
from common import connect_postgres
from common import elapsed
from common import identifier
from common import standalone_application
from common import statistics_for
from common import timestamp

INPUTS = {
    "input-one.bin": b"first deterministic bundle scenario input\n",
    "input-two.bin": b"second deterministic bundle scenario input\n",
}
DEFAULT_OUTPUT = SCENARIO_ARTIFACTS / "bundle/results.json"
DEFAULT_LOG = SCENARIO_ARTIFACTS / "bundle/application.log"


def flow() -> tuple[dict[str, Any], dict[str, str]]:
    ids = {
        "flow": identifier(),
        "document_one": identifier(),
        "document_two": identifier(),
        "bundle": identifier(),
        "version_one": identifier(),
        "version_two": identifier(),
        "bundle_version": identifier(),
        "archive_node": identifier(),
        "bundle_node": identifier(),
        "bundle_archive_node": identifier(),
    }
    definition = {
        "id": ids["flow"],
        "correlation_id": ids["flow"],
        "files": {
            ids["document_one"]: {"metadata": {"original_filename": "input-one.bin"}},
            ids["document_two"]: {"metadata": {"original_filename": "input-two.bin"}},
        },
        "dag": {
            "id": ids["archive_node"],
            "kind": "archive",
            "destination": "default-archive",
            "documents": [
                {
                    "archive": "default",
                    "document_id": ids["document_one"],
                    "version_id": ids["version_one"],
                    "document_type": None,
                    "metadata": {"original_filename": "input-one.bin"},
                    "representations": [
                        {"source": {"source": "generate", "id": ids["document_one"]}}
                    ],
                },
                {
                    "archive": "default",
                    "document_id": ids["document_two"],
                    "version_id": ids["version_two"],
                    "document_type": None,
                    "metadata": {"original_filename": "input-two.bin"},
                    "representations": [
                        {"source": {"source": "generate", "id": ids["document_two"]}}
                    ],
                },
            ],
            "on": {
                "success": [
                    {
                        "id": ids["bundle_node"],
                        "kind": "bundle",
                        "documents": [
                            {
                                "source": "archive",
                                "id": ids["document_one"],
                                "archive": "default",
                                "version": ids["version_one"],
                                "filename": "input-one.bin",
                            },
                            {
                                "source": "archive",
                                "id": ids["document_two"],
                                "archive": "default",
                                "version": ids["version_two"],
                                "filename": "input-two.bin",
                            },
                        ],
                        "out": ids["bundle"],
                        "compression": {"method": "deflated", "level": 9},
                        "on": {
                            "success": [
                                {
                                    "id": ids["bundle_archive_node"],
                                    "kind": "archive",
                                    "destination": "default-archive",
                                    "documents": [
                                        {
                                            "archive": "default",
                                            "document_id": ids["bundle"],
                                            "version_id": ids["bundle_version"],
                                            "document_type": None,
                                            "metadata": {},
                                            "representations": [
                                                {
                                                    "source": {
                                                        "source": "generate",
                                                        "id": ids["bundle"],
                                                    }
                                                }
                                            ],
                                        }
                                    ],
                                }
                            ]
                        },
                    }
                ]
            },
        },
    }
    return definition, ids


async def wait_for_version(
    archive_connection: asyncpg.Connection, version_id: str
) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if await archive_connection.fetchval(
            "SELECT EXISTS(SELECT 1 FROM archive_document_versions WHERE version_id = $1)",
            uuid.UUID(version_id),
        ):
            return
        await asyncio.sleep(0.005)
    raise TimeoutError(f"Bundle version {version_id} did not become available")


async def validate_output(
    session: aiohttp.ClientSession,
    archive_connection: asyncpg.Connection,
    ids: dict[str, str],
) -> None:
    endpoint = f"{BASE_URL}/api/v1/archive/documents/{ids['bundle']}/versions/{ids['bundle_version']}"
    async with session.get(endpoint) as response:
        if response.status != 200:
            body = await response.read()
            raise RuntimeError(
                f"Output validation returned HTTP {response.status}: {body!r}"
            )
        if response.content_type != "application/zip":
            raise RuntimeError(
                f"Unexpected output content type: {response.content_type}"
            )
        data = await response.read()

    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        if archive.namelist() != sorted(INPUTS):
            raise RuntimeError("Bundle members are incorrect")
        if any(archive.read(name) != content for name, content in INPUTS.items()):
            raise RuntimeError("Bundle member content is incorrect")
        if {entry.date_time for entry in archive.infolist()} != {(1980, 1, 1, 0, 0, 0)}:
            raise RuntimeError("Bundle metadata is not deterministic")

    metadata = await archive_connection.fetchrow(
        "SELECT representation.content_type, representation.state::text AS state "
        "FROM archive_document_versions version "
        "JOIN archive_document_representations representation "
        "ON representation._id = version.default_representation_id "
        "WHERE version.version_id = $1",
        uuid.UUID(ids["bundle_version"]),
    )
    if dict(metadata or {}) != {
        "content_type": "application/zip",
        "state": "available",
    }:
        raise RuntimeError(f"Archived bundle metadata is incorrect: {metadata!r}")


async def assert_no_tracked_state(
    connection: asyncpg.Connection, flow_ids: list[uuid.UUID]
) -> dict[str, int]:
    counts = {
        "node_executions": await connection.fetchval(
            "SELECT count(*) FROM node_executions WHERE flow_id = ANY($1::uuid[])",
            flow_ids,
        ),
        "execution_attempts": await connection.fetchval(
            "SELECT count(*) FROM execution_attempts attempt "
            "JOIN node_executions execution "
            "ON execution._id = attempt.node_execution_id "
            "WHERE execution.flow_id = ANY($1::uuid[])",
            flow_ids,
        ),
        "tracked_operations": await connection.fetchval(
            "SELECT count(*) FROM tracked_operations operation "
            "JOIN node_executions execution "
            "ON execution._id = operation.node_execution_id "
            "WHERE execution.flow_id = ANY($1::uuid[])",
            flow_ids,
        ),
        "outcome_events": await connection.fetchval(
            "SELECT count(*) FROM outcome_events WHERE flow_id = ANY($1::uuid[])",
            flow_ids,
        ),
    }
    if any(counts.values()):
        raise RuntimeError(f"Synchronous flows created tracked state: {counts}")
    return counts


def stage_statistics(log_path: Path, runs: list[dict[str, Any]]) -> dict[str, Any]:
    events: dict[str, list[dict[str, Any]]] = {run["ids"]["flow"]: [] for run in runs}
    errors: list[str] = []
    for line in log_path.read_text().splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            if " ERROR: " in line:
                errors.append(line)
            continue
        flow_id = event.get("flow_id")
        if flow_id in events:
            events[flow_id].append(event)
            if event.get("level") == "error":
                errors.append(line)

    stages: list[dict[str, float]] = []
    for run in runs:
        flow_events = events[run["ids"]["flow"]]
        starts = [event for event in flow_events if event.get("start")]
        completes = [event for event in flow_events if event.get("complete")]
        archive_starts = sorted(
            (event for event in starts if event["kind"] == "archive"), key=timestamp
        )
        archive_completes = sorted(
            (event for event in completes if event["kind"] == "archive"),
            key=timestamp,
        )
        bundle_start = next(event for event in starts if event["kind"] == "bundle")
        bundle_complete = next(
            event for event in completes if event["kind"] == "bundle"
        )
        stages.append(
            {
                "processing_ms": elapsed(archive_starts[0], archive_completes[-1]),
                "initial_archive_ms": elapsed(archive_starts[0], archive_completes[0]),
                "bundle_ms": elapsed(bundle_start, bundle_complete),
                "final_archive_ms": elapsed(archive_starts[-1], archive_completes[-1]),
            }
        )

    if len(stages) != len(runs):
        raise RuntimeError("Application log does not contain every scenario flow")
    return {
        "errors": errors,
        "stages": {
            name: statistics_for([stage[name] for stage in stages])
            for name in stages[0]
        },
    }


async def execute(count: int, log_path: Path) -> dict[str, Any]:
    connection = await connect_postgres()
    archive_connection = await connect_archive_postgres()
    runs: list[dict[str, Any]] = []
    timeout = aiohttp.ClientTimeout(total=35)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for index in range(count):
                definition, ids = flow()
                form = aiohttp.FormData()
                form.add_field(
                    "flow",
                    json.dumps(definition).encode(),
                    filename="flow.json",
                    content_type="application/json",
                )
                for document_id, (filename, content) in zip(
                    (ids["document_one"], ids["document_two"]),
                    INPUTS.items(),
                    strict=True,
                ):
                    form.add_field(
                        document_id,
                        content,
                        filename=filename,
                        content_type="application/octet-stream",
                    )

                started = time.perf_counter_ns()
                async with session.post(
                    f"{BASE_URL}/api/v1/intake/", data=form
                ) as response:
                    body = await response.read()
                    if response.status != 202:
                        raise RuntimeError(
                            f"Flow {index + 1} returned HTTP {response.status}: {body!r}"
                        )
                await wait_for_version(archive_connection, ids["bundle_version"])
                duration_ms = (time.perf_counter_ns() - started) / 1_000_000
                await validate_output(session, archive_connection, ids)
                runs.append(
                    {"index": index + 1, "duration_ms": duration_ms, "ids": ids}
                )
                if (index + 1) % 10 == 0 or index + 1 == count:
                    print(
                        f"completed={index + 1} latest_ms={duration_ms:.3f}",
                        flush=True,
                    )

        tracked_state = await assert_no_tracked_state(
            connection, [uuid.UUID(run["ids"]["flow"]) for run in runs]
        )
    finally:
        await archive_connection.close()
        await connection.close()

    durations = [run["duration_ms"] for run in runs]
    return {
        "functional": {
            "flows_completed": len(runs),
            "outputs_validated": len(runs),
            "tracked_state": tracked_state,
        },
        "performance": {
            "all": statistics_for(durations),
            "first_ms": durations[0],
            "excluding_first": statistics_for(durations[1:] or durations),
            **stage_statistics(log_path, runs),
        },
        "runs": runs,
    }


async def main(count: int, output_path: Path, log_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    capabilities = {
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
    }
    async with standalone_application(log_path, capabilities):
        report = await execute(count, log_path)

    output_path.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({**report["functional"], **report["performance"]}, indent=2))
    print(f"report={output_path}")
    print(f"application_log={log_path}")


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=50)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG)
    arguments = parser.parse_args()
    if arguments.count < 1:
        parser.error("--count must be at least 1")
    return arguments


if __name__ == "__main__":
    arguments = parse_arguments()
    asyncio.run(main(arguments.count, arguments.output, arguments.log))
