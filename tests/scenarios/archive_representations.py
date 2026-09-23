"""Burst-submit multi-representation archive flows and measure completion."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
import uuid

from pathlib import Path
from typing import Any

import aiohttp
import asyncpg

from tests.scenarios.common import INTAKE_BASE_URL
from tests.scenarios.common import SCENARIO_ARTIFACTS
from tests.scenarios.common import compose_application
from tests.scenarios.common import connect_archive_postgres
from tests.scenarios.common import standalone_application
from tests.scenarios.common import statistics_for
from xarta.concurrency import bounded_map

DEFAULT_OUTPUT = SCENARIO_ARTIFACTS / "archive/representations-results.json"
DEFAULT_LOG = SCENARIO_ARTIFACTS / "archive/representations-application.log"


def representation_bytes(flow_index: int, representation_index: int) -> bytes:
    return (
        "xarta archive representation scenario\n"
        f"flow={flow_index:04d}\nrepresentation={representation_index:02d}\n"
    ).encode()


def flow(
    index: int, representation_count: int
) -> tuple[dict[str, Any], dict[str, Any]]:
    flow_id = uuid.uuid4()
    document_id = uuid.uuid5(flow_id, "document")
    version_id = uuid.uuid5(flow_id, "version")
    representations = [
        {
            "source": str(uuid.uuid5(flow_id, f"source:{item}")),
            "representation": str(uuid.uuid5(flow_id, f"representation:{item}")),
        }
        for item in range(representation_count)
    ]
    default_representation = representations[0]["representation"]
    definition = {
        "id": str(flow_id),
        "correlation_id": str(flow_id),
        "files": {
            item["source"]: {
                "metadata": {
                    "scenario": "archive-representations",
                    "representation": position,
                }
            }
            for position, item in enumerate(representations)
        },
        "dag": {
            "id": str(uuid.uuid5(flow_id, "archive-node")),
            "kind": "archive",
            "destination": "default-archive",
            "documents": [
                {
                    "archive": "default",
                    "document_id": str(document_id),
                    "version_id": str(version_id),
                    "metadata": {
                        "scenario": "archive-representations",
                        "sequence": index,
                    },
                    "default_representation_id": default_representation,
                    "representations": [
                        {
                            "source": {"source": "generate", "id": item["source"]},
                            "representation_id": item["representation"],
                            "name": f"representation-{position}.txt",
                            "metadata": {"sequence": position},
                        }
                        for position, item in enumerate(representations)
                    ],
                }
            ],
        },
    }
    return definition, {
        "flow": str(flow_id),
        "document": str(document_id),
        "version": str(version_id),
        "default_representation": default_representation,
        "representations": representations,
    }


def intake_form(
    definition: dict[str, Any], ids: dict[str, Any], index: int
) -> aiohttp.FormData:
    form = aiohttp.FormData()
    form.add_field(
        "flow",
        json.dumps(definition).encode(),
        filename="flow.json",
        content_type="application/json",
    )
    for position, representation in enumerate(ids["representations"]):
        form.add_field(
            representation["source"],
            representation_bytes(index, position),
            filename=f"representation-{position}.txt",
            content_type="text/plain",
        )
    return form


async def wait_for_versions(
    connection: asyncpg.Connection, version_ids: list[uuid.UUID]
) -> None:
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        available = await connection.fetchval(
            "SELECT count(*) FROM archive_document_versions "
            "WHERE version_id = ANY($1::uuid[]) AND state = 'available'",
            version_ids,
        )
        if available == len(version_ids):
            return
        await asyncio.sleep(0.01)
    raise TimeoutError(
        f"Only {available} of {len(version_ids)} archive versions became available"
    )


async def validate_representations(
    connection: asyncpg.Connection, runs: list[dict[str, Any]]
) -> None:
    expected = {
        uuid.UUID(run["version"]): (
            uuid.UUID(run["default_representation"]),
            {uuid.UUID(item["representation"]) for item in run["representations"]},
        )
        for run in runs
    }
    rows = await connection.fetch(
        "SELECT version.version_id, version.default_representation_id, "
        "array_agg(representation.representation_id) AS representation_ids "
        "FROM archive_document_versions version "
        "JOIN archive_document_representations representation "
        "ON representation.version_internal_id = version._id "
        "WHERE version.version_id = ANY($1::uuid[]) AND version.state = 'available' "
        "GROUP BY version._id",
        list(expected),
    )
    actual = {
        row["version_id"]: (
            row["default_representation_id"],
            set(row["representation_ids"]),
        )
        for row in rows
    }
    if actual != expected:
        raise RuntimeError(
            "Archived representations do not match the submitted workload"
        )


async def execute(
    count: int, representation_count: int, submission_concurrency: int
) -> dict[str, Any]:
    definitions = [flow(index + 1, representation_count) for index in range(count)]
    runs = [ids for _, ids in definitions]
    connection = await connect_archive_postgres()
    timeout = aiohttp.ClientTimeout(total=60)
    completed = 0
    started = time.perf_counter_ns()
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:

            async def submit(
                item: tuple[int, tuple[dict[str, Any], dict[str, Any]]],
            ) -> float:
                nonlocal completed
                index, (definition, ids) = item
                request_started = time.perf_counter_ns()
                async with session.post(
                    f"{INTAKE_BASE_URL}/api/v1/intake/",
                    data=intake_form(definition, ids, index + 1),
                ) as response:
                    body = await response.read()
                    if response.status != 202:
                        raise RuntimeError(
                            f"Flow {ids['flow']} returned HTTP {response.status}: {body!r}"
                        )
                completed += 1
                if completed % 100 == 0 or completed == count:
                    print(f"phase=submission completed={completed}", flush=True)
                return (time.perf_counter_ns() - request_started) / 1_000_000

            submission_durations = await bounded_map(
                enumerate(definitions), submission_concurrency, submit
            )
            submission_finished = time.perf_counter_ns()
            await wait_for_versions(
                connection, [uuid.UUID(run["version"]) for run in runs]
            )
            completion_finished = time.perf_counter_ns()

        await validate_representations(connection, runs)
    finally:
        await connection.close()

    total_wall = (completion_finished - started) / 1_000_000_000
    submission_wall = (submission_finished - started) / 1_000_000_000
    return {
        "functional": {
            "flows_submitted": count,
            "flows_completed": count,
            "versions_validated": count,
            "representations_validated": count * representation_count,
        },
        "performance": {
            "nats_workers_per_consumer": int(
                os.environ.get(
                    "ARCHIVE_REPRESENTATIONS_NATS_WORKERS",
                    os.environ.get("NATS_WORKERS", "1"),
                )
            ),
            "representation_write_concurrency": int(
                os.environ.get("ARCHIVE_REPRESENTATION_WRITE_CONCURRENCY", "10")
            ),
            "representations_per_version": representation_count,
            "submission_concurrency": submission_concurrency,
            "flow_completion": {
                "wall_seconds": total_wall,
                "flows_per_second": count / total_wall,
                "representations_per_second": count * representation_count / total_wall,
            },
            "submission": {
                "wall_seconds": submission_wall,
                "flows_per_second": count / submission_wall,
                "operation": statistics_for(submission_durations),
            },
            "drain_wall_seconds": (completion_finished - submission_finished)
            / 1_000_000_000,
        },
    }


async def main(
    count: int,
    representation_count: int,
    submission_concurrency: int,
    output_path: Path,
    log_path: Path,
    compose_path: Path | None,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    application = (
        compose_application(log_path, compose_path, ("intake", "archive"))
        if compose_path
        else standalone_application(log_path, {"archive"})
    )
    async with application:
        report = await execute(count, representation_count, submission_concurrency)

    output_path.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({**report["functional"], **report["performance"]}, indent=2))
    print(f"report={output_path}")
    print(f"application_log={log_path}")


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=100)
    parser.add_argument("--representations", type=int, default=10)
    parser.add_argument("--submission-concurrency", type=int, default=100)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG)
    parser.add_argument("--compose-file", type=Path)
    arguments = parser.parse_args()
    if arguments.count < 1:
        parser.error("--count must be at least 1")
    if not 1 <= arguments.representations <= 100:
        parser.error("--representations must be between 1 and 100")
    if not 1 <= arguments.submission_concurrency <= 1000:
        parser.error("--submission-concurrency must be between 1 and 1000")
    return arguments


if __name__ == "__main__":
    arguments = parse_arguments()
    asyncio.run(
        main(
            arguments.count,
            arguments.representations,
            arguments.submission_concurrency,
            arguments.output,
            arguments.log,
            arguments.compose_file,
        )
    )
