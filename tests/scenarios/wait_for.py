"""Exercise and measure local wait-for retries and failure routing."""

from __future__ import annotations

import argparse
import asyncio
import json
import time
import uuid

from itertools import pairwise
from pathlib import Path
from typing import Any

import aiohttp
import asyncpg

from tests.scenarios.common import BASE_URL
from tests.scenarios.common import SCENARIO_ARTIFACTS
from tests.scenarios.common import connect_archive_postgres
from tests.scenarios.common import connect_postgres
from tests.scenarios.common import elapsed
from tests.scenarios.common import identifier
from tests.scenarios.common import standalone_application
from tests.scenarios.common import statistics_for
from tests.scenarios.common import timestamp

DEFAULT_OUTPUT = SCENARIO_ARTIFACTS / "wait-for/results.json"
DEFAULT_LOG = SCENARIO_ARTIFACTS / "wait-for/application.log"
SUCCESS_BACKOFFS = [0.25, 0.25]
FAILURE_BACKOFFS = [0.04, 0.06]
MARKER_CONTENT_TYPE = "application/x-xarta-scenario-marker"


def flow(
    subcase: str,
) -> tuple[dict[str, Any], dict[str, str], bytes, bytes]:
    ids = {
        "flow": identifier(),
        "wait_node": identifier(),
        "awaited_document": identifier(),
        "awaited_version": identifier(),
        "selected_input": identifier(),
        "selected_document": identifier(),
        "selected_version": identifier(),
        "selected_node": identifier(),
        "trap_input": identifier(),
        "trap_document": identifier(),
        "trap_version": identifier(),
        "trap_node": identifier(),
    }
    selected_outcome = "success" if subcase == "delayed_success" else "failure"
    trap_outcome = "failure" if selected_outcome == "success" else "success"
    backoffs = SUCCESS_BACKOFFS if subcase == "delayed_success" else FAILURE_BACKOFFS
    marker = f"wait-for:{subcase}:{ids['flow']}\n".encode()
    trap = f"wait-for:trap:{subcase}:{ids['flow']}\n".encode()

    def marker_node(prefix: str) -> dict[str, Any]:
        return {
            "id": ids[f"{prefix}_node"],
            "kind": "archive",
            "destination": "default-archive",
            "documents": [
                {
                    "archive": "default",
                    "document_id": ids[f"{prefix}_document"],
                    "version_id": ids[f"{prefix}_version"],
                    "document_type": None,
                    "metadata": {"scenario": "wait-for", "marker": prefix},
                    "representations": [
                        {
                            "source": {
                                "source": "generate",
                                "id": ids[f"{prefix}_input"],
                            }
                        }
                    ],
                }
            ],
        }

    definition = {
        "id": ids["flow"],
        "correlation_id": ids["flow"],
        "files": {
            ids["selected_input"]: {
                "metadata": {"original_filename": "selected.marker"}
            },
            ids["trap_input"]: {"metadata": {"original_filename": "trap.marker"}},
        },
        "dag": {
            "id": ids["wait_node"],
            "kind": "wait-for",
            "backoffs": backoffs,
            "documents": [
                {
                    "id": ids["awaited_document"],
                    "archive": "default",
                    "version": ids["awaited_version"],
                }
            ],
            "on": {
                selected_outcome: [marker_node("selected")],
                trap_outcome: [marker_node("trap")],
            },
        },
    }
    ids["subcase"] = subcase
    return definition, ids, marker, trap


def retry_warning_count(log_path: Path) -> int:
    if not log_path.exists():
        return 0
    return log_path.read_text().count("NATS handler failed temporarily")


async def wait_for_retry(log_path: Path, previous_count: int) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if retry_warning_count(log_path) > previous_count:
            return
        await asyncio.sleep(0.005)
    raise TimeoutError("Wait-for did not produce its first JetStream retry")


async def create_awaited_version(
    session: aiohttp.ClientSession, ids: dict[str, str]
) -> None:
    form = aiohttp.FormData()
    form.add_field("content-type", "application/octet-stream")
    form.add_field("document-id", ids["awaited_document"])
    form.add_field("version-id", ids["awaited_version"])
    form.add_field(
        "file",
        b"awaited archive version\n",
        filename="awaited.bin",
        content_type="application/octet-stream",
    )
    async with session.put(
        f"{BASE_URL}/api/v1/archive/documents", data=form
    ) as response:
        body = await response.read()
        if response.status != 201:
            raise RuntimeError(
                f"Awaited archive version returned HTTP {response.status}: {body!r}"
            )


async def marker_response(
    session: aiohttp.ClientSession, document_id: str, version_id: str
) -> tuple[int, bytes]:
    endpoint = (
        f"{BASE_URL}/api/v1/archive/documents/{document_id}/versions/{version_id}"
    )
    async with session.get(endpoint) as response:
        return response.status, await response.read()


async def wait_for_selected_marker(
    session: aiohttp.ClientSession,
    archive_connection: asyncpg.Connection,
    ids: dict[str, str],
    expected: bytes,
) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        exists = await archive_connection.fetchval(
            "SELECT EXISTS(SELECT 1 FROM archive_document_versions WHERE version_id = $1 AND state = 'available')",
            uuid.UUID(ids["selected_version"]),
        )
        if exists:
            status, body = await marker_response(
                session, ids["selected_document"], ids["selected_version"]
            )
            if status != 200 or body != expected:
                raise RuntimeError(
                    f"Selected branch marker returned HTTP {status}: {body!r}"
                )
            return
        await asyncio.sleep(0.005)
    raise TimeoutError(f"Selected {ids['subcase']} branch marker was not archived")


async def assert_trap_absent(
    archive_connection: asyncpg.Connection, ids: dict[str, str]
) -> None:
    exists = await archive_connection.fetchval(
        "SELECT EXISTS(SELECT 1 FROM archive_document_versions WHERE version_id = $1)",
        uuid.UUID(ids["trap_version"]),
    )
    if exists:
        raise RuntimeError("Trap branch marker was archived")


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
        raise RuntimeError(
            f"Synchronous wait-for flows created tracked state: {counts}"
        )
    return counts


def application_log_report(
    log_path: Path, runs: list[dict[str, Any]]
) -> dict[str, Any]:
    events: dict[str, list[dict[str, Any]]] = {run["ids"]["flow"]: [] for run in runs}
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
        if event.get("flow_id") in events:
            events[event["flow_id"]].append(event)

    timings: dict[str, list[dict[str, float]]] = {
        "delayed_success": [],
        "retry_exhaustion": [],
    }
    delivery_counts: dict[str, list[int]] = {
        "delayed_success": [],
        "retry_exhaustion": [],
    }
    expected_counts = {
        "delayed_success": 2,
        "retry_exhaustion": len(FAILURE_BACKOFFS) + 1,
    }
    for run in runs:
        flow_events = events[run["ids"]["flow"]]
        wait_starts = sorted(
            (
                event
                for event in flow_events
                if event.get("kind") == "wait-for" and event.get("start")
            ),
            key=timestamp,
        )
        wait_complete = next(
            (
                event
                for event in flow_events
                if event.get("kind") == "wait-for" and event.get("complete")
            ),
            None,
        )
        archive_complete = next(
            (
                event
                for event in flow_events
                if event.get("kind") == "archive" and event.get("complete")
            ),
            None,
        )
        subcase = run["ids"]["subcase"]
        delivery_counts[subcase].append(len(wait_starts))
        if len(wait_starts) != expected_counts[subcase]:
            raise RuntimeError(
                f"Flow {run['ids']['flow']} had {len(wait_starts)} wait-for "
                f"deliveries, expected {expected_counts[subcase]}"
            )
        if wait_complete is None or archive_complete is None:
            raise RuntimeError(
                f"Application log is missing terminal stages for {run['ids']['flow']}"
            )
        retry_intervals = [
            elapsed(first, second) for first, second in pairwise(wait_starts)
        ]
        timings[subcase].append(
            {
                "wait_first_start_to_terminal_ms": elapsed(
                    wait_starts[0], wait_complete
                ),
                "terminal_branch_ms": elapsed(wait_complete, archive_complete),
                "application_total_ms": elapsed(wait_starts[0], archive_complete),
                "mean_retry_interval_ms": sum(retry_intervals) / len(retry_intervals),
            }
        )

    if errors:
        raise RuntimeError("Application log contains errors")
    return {
        "errors": errors,
        "wait_for_delivery_counts": {
            subcase: {
                "expected_per_run": expected_counts[subcase],
                "observed_per_run": counts,
            }
            for subcase, counts in delivery_counts.items()
        },
        "stage_and_retry_timings": {
            subcase: {
                name: statistics_for([timing[name] for timing in subcase_timings])
                for name in subcase_timings[0]
            }
            for subcase, subcase_timings in timings.items()
        },
    }


async def submit_flow(
    session: aiohttp.ClientSession,
    definition: dict[str, Any],
    ids: dict[str, str],
    selected_marker: bytes,
    trap_marker: bytes,
) -> None:
    form = aiohttp.FormData()
    form.add_field(
        "flow",
        json.dumps(definition).encode(),
        filename="flow.json",
        content_type="application/json",
    )
    form.add_field(
        ids["selected_input"],
        selected_marker,
        filename="selected.marker",
        content_type=MARKER_CONTENT_TYPE,
    )
    form.add_field(
        ids["trap_input"],
        trap_marker,
        filename="trap.marker",
        content_type=MARKER_CONTENT_TYPE,
    )
    async with session.post(f"{BASE_URL}/api/v1/intake/", data=form) as response:
        body = await response.read()
        if response.status != 202:
            raise RuntimeError(
                f"{ids['subcase']} intake returned HTTP {response.status}: {body!r}"
            )


async def execute(count: int, log_path: Path) -> dict[str, Any]:
    connection = await connect_postgres()
    archive_connection = await connect_archive_postgres()
    runs: list[dict[str, Any]] = []
    timeout = aiohttp.ClientTimeout(total=15)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for subcase in ("delayed_success", "retry_exhaustion"):
                for index in range(count):
                    definition, ids, selected_marker, trap_marker = flow(subcase)
                    warnings_before = retry_warning_count(log_path)
                    started = time.perf_counter_ns()
                    await submit_flow(
                        session, definition, ids, selected_marker, trap_marker
                    )
                    if subcase == "delayed_success":
                        await wait_for_retry(log_path, warnings_before)
                        await create_awaited_version(session, ids)
                    await wait_for_selected_marker(
                        session, archive_connection, ids, selected_marker
                    )
                    latency_ms = (time.perf_counter_ns() - started) / 1_000_000
                    await assert_trap_absent(archive_connection, ids)
                    runs.append(
                        {
                            "subcase_index": index + 1,
                            "intake_to_terminal_ms": latency_ms,
                            "ids": ids,
                        }
                    )
                    print(
                        f"subcase={subcase} completed={index + 1} latest_ms={latency_ms:.3f}",
                        flush=True,
                    )

        tracked_state = await assert_no_tracked_state(
            connection, [uuid.UUID(run["ids"]["flow"]) for run in runs]
        )
    finally:
        await archive_connection.close()
        await connection.close()

    return {
        "functional": {
            "runs_per_subcase": count,
            "flows_completed": len(runs),
            "selected_markers_validated": len(runs),
            "trap_markers_absent": len(runs),
            "tracked_state": tracked_state,
        },
        "performance": {
            "intake_to_terminal": {
                subcase: statistics_for(
                    [
                        run["intake_to_terminal_ms"]
                        for run in runs
                        if run["ids"]["subcase"] == subcase
                    ]
                )
                for subcase in ("delayed_success", "retry_exhaustion")
            }
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

    report["performance"].update(application_log_report(log_path, report["runs"]))
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
