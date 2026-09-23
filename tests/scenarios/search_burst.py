"""Burst-submit archive/search flows, then validate each lifecycle phase."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
import uuid

from collections.abc import Awaitable
from collections.abc import Callable
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from typing import TypeVar

import aiohttp
import asyncpg

from common import INTAKE_BASE_URL
from common import ROOT
from common import SCENARIO_ARTIFACTS
from common import compose_application
from common import connect_archive_postgres
from common import connect_postgres
from common import standalone_application
from common import statistics_for
from search import DEFAULT_SEARCH_CONFIG
from search import assert_no_tracked_state
from search import delete_and_validate_projection
from search import ensure_document_type
from search import flow
from search import load_search_configuration
from search import log_statistics
from search import refresh_search_index
from search import validate_archive_outputs
from search import validate_search_document

DEFAULT_OUTPUT = SCENARIO_ARTIFACTS / "search/burst-results.json"
DEFAULT_LOG = SCENARIO_ARTIFACTS / "search/burst-application.log"

Item = TypeVar("Item")
Result = TypeVar("Result")


async def bounded_map(
    items: Sequence[Item],
    concurrency: int,
    operation: Callable[[Item], Awaitable[Result]],
    label: str,
) -> list[Result]:
    semaphore = asyncio.Semaphore(concurrency)
    completed = 0

    async def execute(item: Item) -> Result:
        nonlocal completed
        async with semaphore:
            result = await operation(item)
            completed += 1
            if completed % 100 == 0 or completed == len(items):
                print(f"phase={label} completed={completed}", flush=True)
            return result

    return list(await asyncio.gather(*(execute(item) for item in items)))


def intake_form(definition: dict[str, Any]) -> aiohttp.FormData:
    form = aiohttp.FormData()
    form.add_field(
        "flow",
        json.dumps(definition).encode(),
        filename="flow.json",
        content_type="application/json",
    )
    return form


async def wait_for_terminal_archives(
    connection: asyncpg.Connection, runs: list[dict[str, Any]]
) -> None:
    version_ids = [uuid.UUID(run["ids"]["terminal_version"]) for run in runs]
    deadline = time.monotonic() + 60
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
        f"Only {available} of {len(version_ids)} terminal archives became available"
    )


def phase_performance(
    wall_seconds: float, durations: Sequence[float], count: int
) -> dict[str, Any]:
    return {
        "wall_seconds": wall_seconds,
        "flows_per_second": count / wall_seconds,
        "operation": statistics_for(list(durations)),
    }


async def execute(
    count: int,
    configuration: dict[str, Any],
    submission_concurrency: int,
    validation_concurrency: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    connection = await connect_postgres()
    archive_connection = await connect_archive_postgres()
    archive_connection_lock = asyncio.Lock()
    await ensure_document_type(connection)
    definitions = [flow(index + 1) for index in range(count)]
    runs = [
        {
            "index": index + 1,
            "workload": ids["workload"],
            "ids": ids,
        }
        for index, (_, ids) in enumerate(definitions)
    ]
    timeout = aiohttp.ClientTimeout(total=60)
    total_started = time.perf_counter_ns()
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:

            async def submit(item: tuple[dict[str, Any], dict[str, str]]) -> float:
                definition, ids = item
                started = time.perf_counter_ns()
                async with session.post(
                    f"{INTAKE_BASE_URL}/api/v1/intake/",
                    data=intake_form(definition),
                ) as response:
                    body = await response.read()
                    if response.status != 202:
                        raise RuntimeError(
                            f"Flow {ids['flow']} returned HTTP {response.status}: {body!r}"
                        )
                return (time.perf_counter_ns() - started) / 1_000_000

            started = time.perf_counter_ns()
            submission_durations = await bounded_map(
                definitions, submission_concurrency, submit, "submission"
            )
            submission_wall = (time.perf_counter_ns() - started) / 1_000_000_000

            started = time.perf_counter_ns()
            await wait_for_terminal_archives(archive_connection, runs)
            drain_wall = (time.perf_counter_ns() - started) / 1_000_000_000

            async def validate_download(run: dict[str, Any]) -> float:
                return await validate_archive_outputs(
                    session,
                    archive_connection,
                    run["ids"],
                    archive_connection_lock,
                )

            started = time.perf_counter_ns()
            download_durations = await bounded_map(
                runs, validation_concurrency, validate_download, "download"
            )
            download_wall = (time.perf_counter_ns() - started) / 1_000_000_000

            index_refresh_ms = await refresh_search_index(session, configuration)

            async def validate_search(run: dict[str, Any]) -> dict[str, Any]:
                return await validate_search_document(
                    session, configuration, run["ids"], refresh=False
                )

            started = time.perf_counter_ns()
            search_documents = await bounded_map(
                runs, validation_concurrency, validate_search, "search"
            )
            search_wall = (time.perf_counter_ns() - started) / 1_000_000_000

            async def delete_projection(
                item: tuple[dict[str, Any], dict[str, Any]],
            ) -> float:
                run, search_document = item
                return await delete_and_validate_projection(
                    session,
                    archive_connection,
                    archive_connection_lock,
                    configuration,
                    run["ids"],
                    search_document,
                )

            started = time.perf_counter_ns()
            deletion_durations = await bounded_map(
                list(zip(runs, search_documents, strict=True)),
                validation_concurrency,
                delete_projection,
                "deletion",
            )
            deletion_wall = (time.perf_counter_ns() - started) / 1_000_000_000

        tracked_state = await assert_no_tracked_state(
            connection, [uuid.UUID(run["ids"]["flow"]) for run in runs]
        )
    finally:
        await archive_connection.close()
        await connection.close()

    total_wall = (time.perf_counter_ns() - total_started) / 1_000_000_000
    for run, submission, download, search_document, deletion in zip(
        runs,
        submission_durations,
        download_durations,
        search_documents,
        deletion_durations,
        strict=True,
    ):
        run.update(
            {
                "intake_submission_ms": submission,
                "source_download_ms": download,
                "search_lookup_ms": search_document["lookup_ms"],
                "search_query_ms": search_document["query_ms"],
                "deletion_cleanup_ms": deletion,
            }
        )

    return (
        {
            "functional": {
                "flows_submitted": count,
                "flows_completed": count,
                "terminal_archives_validated": count,
                "source_downloads_validated": count,
                "search_documents_validated": count,
                "search_queries_validated": count,
                "deletion_cleanups_validated": count,
                "trap_archives_found": 0,
                "tracked_state": tracked_state,
                "search_configuration": {
                    "path": configuration["config_path"],
                    "destination": "default-search",
                    "revision": configuration["revision"],
                    "endpoint": configuration["endpoint"],
                    "index": configuration["index"],
                },
            },
            "performance": {
                "nats_workers_per_consumer": int(
                    os.environ.get(
                        "SEARCH_SCENARIO_NATS_WORKERS",
                        os.environ.get("NATS_WORKERS", "1"),
                    )
                ),
                "submission_concurrency": submission_concurrency,
                "validation_concurrency": validation_concurrency,
                "total_wall_seconds": total_wall,
                "submission": phase_performance(
                    submission_wall, submission_durations, count
                ),
                "dag_drain": {
                    "wall_seconds": drain_wall,
                    "flows_per_second": count / (submission_wall + drain_wall),
                    "from_submission_start_seconds": submission_wall + drain_wall,
                },
                "download": phase_performance(download_wall, download_durations, count),
                "search_index_refresh_ms": index_refresh_ms,
                "search": phase_performance(
                    search_wall,
                    [document["query_ms"] for document in search_documents],
                    count,
                ),
                "deletion": phase_performance(deletion_wall, deletion_durations, count),
            },
            "search_documents": search_documents,
            "runs": runs,
        },
        runs,
    )


async def main(
    count: int,
    submission_concurrency: int,
    validation_concurrency: int,
    output_path: Path,
    log_path: Path,
    compose_path: Path | None,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    config_path = Path(
        os.environ.get("SEARCH_CONFIGURATIONS_CONFIG_PATH", DEFAULT_SEARCH_CONFIG)
    )
    if not config_path.is_absolute():
        config_path = ROOT / config_path
    configuration = load_search_configuration(config_path)

    application = (
        compose_application(
            log_path, compose_path, ("intake", "archive", "generation", "search")
        )
        if compose_path
        else standalone_application(
            log_path, {"archive", "generate", "render", "search-index"}
        )
    )
    async with application:
        report, runs = await execute(
            count,
            configuration,
            submission_concurrency,
            validation_concurrency,
        )

    report["performance"].update(log_statistics(log_path, runs))
    if report["performance"]["errors"]:
        raise RuntimeError("Application log contains errors")
    output_path.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({**report["functional"], **report["performance"]}, indent=2))
    print(f"report={output_path}")
    print(f"application_log={log_path}")


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=20)
    parser.add_argument("--submission-concurrency", type=int, default=100)
    parser.add_argument("--validation-concurrency", type=int, default=50)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG)
    parser.add_argument("--compose-file", type=Path)
    arguments = parser.parse_args()
    if arguments.count < 1:
        parser.error("--count must be at least 1")
    if not 1 <= arguments.submission_concurrency <= 1000:
        parser.error("--submission-concurrency must be between 1 and 1000")
    if not 1 <= arguments.validation_concurrency <= 100:
        parser.error("--validation-concurrency must be between 1 and 100")
    return arguments


if __name__ == "__main__":
    arguments = parse_arguments()
    asyncio.run(
        main(
            arguments.count,
            arguments.submission_concurrency,
            arguments.validation_concurrency,
            arguments.output,
            arguments.log,
            arguments.compose_file,
        )
    )
