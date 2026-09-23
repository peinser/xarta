"""Run and measure the synchronous archive/search/search/archive scenario."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import time
import uuid

from pathlib import Path
from typing import Any

import aiohttp
import asyncpg

from common import ARCHIVE_BASE_URL
from common import INTAKE_BASE_URL
from common import ROOT
from common import SCENARIO_ARTIFACTS
from common import compose_application
from common import connect_archive_postgres
from common import connect_postgres
from common import elapsed
from common import identifier
from common import standalone_application
from common import statistics_for
from common import timestamp

DEFAULT_OUTPUT = SCENARIO_ARTIFACTS / "search/results.json"
DEFAULT_LOG = SCENARIO_ARTIFACTS / "search/application.log"
DEFAULT_SEARCH_CONFIG = ROOT / ".dev/conf/search.json"
DOCUMENT_TYPE = "scenario-search-generated-pdf"
SELFTEST_TEMPLATE_ENGINE = "jinja"
WORKLOADS = ("generated-pdf",)
TRAP_OUTCOMES = (
    "first_unchanged",
    "first_unsupported_content",
    "first_index_rejected",
    "second_indexed",
    "second_unsupported_content",
    "second_index_rejected",
)


def jinja_selftest_template_engine_options() -> dict[str, object]:
    return {
        SELFTEST_TEMPLATE_ENGINE: {
            "kind": SELFTEST_TEMPLATE_ENGINE,
            "template": {"path": "engine/selftest.html"},
        }
    }


def archive_node(
    node_id: str, source_id: str, document_id: str, version_id: str
) -> dict[str, Any]:
    return {
        "id": node_id,
        "kind": "archive",
        "destination": "default-archive",
        "documents": [
            {
                "archive": "default",
                "document_id": document_id,
                "version_id": version_id,
                "document_type": DOCUMENT_TYPE,
                "metadata": {},
                "representations": [
                    {"source": {"source": "generate", "id": source_id}}
                ],
            }
        ],
    }


def workload(index: int) -> str:
    return WORKLOADS[(index - 1) % len(WORKLOADS)]


def flow(index: int) -> tuple[dict[str, Any], dict[str, str]]:
    ids = {
        "flow": identifier(),
        "source": identifier(),
        "source_version": identifier(),
        "terminal": identifier(),
        "terminal_version": identifier(),
        "initial_archive_node": identifier(),
        "generate_node": identifier(),
        "first_search_node": identifier(),
        "second_search_node": identifier(),
        "terminal_archive_node": identifier(),
        "workload": workload(index),
    }
    for outcome in TRAP_OUTCOMES:
        ids[f"trap_{outcome}"] = identifier()
        ids[f"trap_{outcome}_version"] = identifier()
        ids[f"trap_{outcome}_node"] = identifier()

    terminal_archive = archive_node(
        ids["terminal_archive_node"],
        ids["source"],
        ids["terminal"],
        ids["terminal_version"],
    )
    second_search = {
        "id": ids["second_search_node"],
        "kind": "search-index",
        "document": {
            "source": "archive",
            "archive": "default",
            "id": ids["source"],
            "version": ids["source_version"],
        },
        "destination": "default-search",
        "on": {
            "unchanged": [terminal_archive],
            "indexed": [
                archive_node(
                    ids["trap_second_indexed_node"],
                    ids["source"],
                    ids["trap_second_indexed"],
                    ids["trap_second_indexed_version"],
                )
            ],
            "unsupported_content": [
                archive_node(
                    ids["trap_second_unsupported_content_node"],
                    ids["source"],
                    ids["trap_second_unsupported_content"],
                    ids["trap_second_unsupported_content_version"],
                )
            ],
            "index_rejected": [
                archive_node(
                    ids["trap_second_index_rejected_node"],
                    ids["source"],
                    ids["trap_second_index_rejected"],
                    ids["trap_second_index_rejected_version"],
                )
            ],
        },
    }
    first_search = {
        "id": ids["first_search_node"],
        "kind": "search-index",
        "document": {
            "source": "archive",
            "archive": "default",
            "id": ids["source"],
            "version": ids["source_version"],
        },
        "destination": "default-search",
        "on": {
            "indexed": [second_search],
            "unchanged": [
                archive_node(
                    ids["trap_first_unchanged_node"],
                    ids["source"],
                    ids["trap_first_unchanged"],
                    ids["trap_first_unchanged_version"],
                )
            ],
            "unsupported_content": [
                archive_node(
                    ids["trap_first_unsupported_content_node"],
                    ids["source"],
                    ids["trap_first_unsupported_content"],
                    ids["trap_first_unsupported_content_version"],
                )
            ],
            "index_rejected": [
                archive_node(
                    ids["trap_first_index_rejected_node"],
                    ids["source"],
                    ids["trap_first_index_rejected"],
                    ids["trap_first_index_rejected_version"],
                )
            ],
        },
    }
    initial_archive = archive_node(
        ids["initial_archive_node"],
        ids["source"],
        ids["source"],
        ids["source_version"],
    )
    initial_archive["on"] = {"success": [first_search]}

    return {
        "id": ids["flow"],
        "correlation_id": ids["flow"],
        "dag": {
            "id": ids["generate_node"],
            "kind": "generate",
            "documents": [
                {
                    "source": "render",
                    "id": ids["source"],
                    "document_type": DOCUMENT_TYPE,
                    "content_type": "application/pdf",
                    "template_engine": SELFTEST_TEMPLATE_ENGINE,
                    "template_engines_options": jinja_selftest_template_engine_options(),
                    "payload": {"content_type": "application/json", "data": {}},
                }
            ],
            "on": {"success": [initial_archive]},
        },
    }, ids


async def ensure_document_type(connection: asyncpg.Connection) -> None:
    await connection.execute(
        "INSERT INTO document_types "
        "(identifier, default_template_engine, default_content_type) "
        "VALUES ($1, $2, $3) ON CONFLICT (identifier) DO NOTHING",
        DOCUMENT_TYPE,
        SELFTEST_TEMPLATE_ENGINE,
        "application/pdf",
    )


def load_search_configuration(path: Path) -> dict[str, Any]:
    configurations = json.loads(path.read_text())
    destination = configurations.get("default-search", {})
    revision = destination.get("current_revision")
    configuration = destination.get("revisions", {}).get(revision, {})
    if destination.get("kind") != "search-index" or not all(
        isinstance(configuration.get(name), str) and configuration[name].strip()
        for name in ("endpoint", "index")
    ):
        raise RuntimeError(f"Invalid default-search configuration in {path}")
    return {
        **configuration,
        "revision": revision,
        "config_path": str(path),
    }


async def wait_for_version(
    archive_connection: asyncpg.Connection,
    version_id: str,
    description: str,
    lock: asyncio.Lock | None = None,
) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if lock is None:
            available = await archive_connection.fetchval(
                "SELECT EXISTS(SELECT 1 FROM archive_document_versions WHERE version_id = $1 AND state = 'available')",
                uuid.UUID(version_id),
            )
        else:
            async with lock:
                available = await archive_connection.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM archive_document_versions "
                    "WHERE version_id = $1 AND state = 'available')",
                    uuid.UUID(version_id),
                )
        if available:
            return
        await asyncio.sleep(0.005)
    raise TimeoutError(f"{description} version {version_id} did not become available")


async def validate_archive_outputs(
    session: aiohttp.ClientSession,
    archive_connection: asyncpg.Connection,
    ids: dict[str, str],
    lock: asyncio.Lock | None = None,
) -> float:
    source_endpoint = f"{ARCHIVE_BASE_URL}/api/v1/archive/archives/default/documents/{ids['source']}/versions/{ids['source_version']}"
    started = time.perf_counter_ns()
    async with session.get(source_endpoint) as response:
        data = await response.read()
        if response.status != 200:
            raise RuntimeError(
                f"Source archive returned HTTP {response.status}: {data!r}"
            )
        if response.content_type != "application/pdf" or not (
            data.startswith(b"%PDF-") and b"%%EOF" in data[-1024:]
        ):
            raise RuntimeError("Source archive content is incorrect")
        source_data = data
    source_download_ms = (time.perf_counter_ns() - started) / 1_000_000

    endpoint = f"{ARCHIVE_BASE_URL}/api/v1/archive/documents/{ids['terminal']}/versions/{ids['terminal_version']}"
    async with session.get(endpoint) as response:
        data = await response.read()
        if response.status != 200:
            raise RuntimeError(
                f"Terminal archive returned HTTP {response.status}: {data!r}"
            )
        if response.content_type != "application/pdf" or data != source_data:
            raise RuntimeError("Terminal archive content is incorrect")

    trap_versions = [
        uuid.UUID(ids[f"trap_{outcome}_version"]) for outcome in TRAP_OUTCOMES
    ]
    if lock is None:
        archived_traps = await archive_connection.fetch(
            "SELECT version_id FROM archive_document_versions WHERE version_id = ANY($1::uuid[])",
            trap_versions,
        )
    else:
        async with lock:
            archived_traps = await archive_connection.fetch(
                "SELECT version_id FROM archive_document_versions WHERE version_id = ANY($1::uuid[])",
                trap_versions,
            )
    if archived_traps:
        raise RuntimeError(
            f"Unexpected search outcome reached traps: {archived_traps!r}"
        )
    return source_download_ms


async def validate_search_document(
    session: aiohttp.ClientSession,
    configuration: dict[str, Any],
    ids: dict[str, str],
    *,
    refresh: bool = True,
) -> dict[str, Any]:
    identity = "\0".join(
        (
            configuration["revision"],
            "archive",
            "default",
            ids["source"],
            ids["source_version"],
        )
    )
    document_id = hashlib.sha256(identity.encode()).hexdigest()
    headers: dict[str, str] = {}
    if configuration.get("api_key"):
        headers["authorization"] = f"ApiKey {configuration['api_key']}"
    auth = (
        aiohttp.BasicAuth(configuration["username"], configuration["password"])
        if configuration.get("username")
        else None
    )
    endpoint = f"{configuration['endpoint'].rstrip('/')}/{configuration['index']}/_doc/{document_id}"
    lookup_started = time.perf_counter_ns()
    async with session.get(endpoint, headers=headers, auth=auth) as response:
        body = await response.read()
        if response.status != 200:
            raise RuntimeError(
                f"OpenSearch document returned HTTP {response.status}: {body!r}"
            )
        payload = json.loads(body)
    lookup_ms = (time.perf_counter_ns() - lookup_started) / 1_000_000

    expected = {
        "projection_revision": configuration["revision"],
        "source_kind": "archive",
        "source_archive": "default",
        "source_document_id": ids["source"],
        "source_version_id": ids["source_version"],
        "content_type": None,
        "document_type": DOCUMENT_TYPE,
        "metadata": {},
    }
    source = payload.get("_source", {})
    actual = {name: source.get(name) for name in expected}
    representations = source.get("representations")
    representation = representations[0] if isinstance(representations, list) else {}
    if (
        payload.get("_id") != document_id
        or actual != expected
        or "digest" in source
        or len(representations or []) != 1
        or representation.get("representation_id")
        != source.get("default_representation_id")
        or representation.get("content_type") != "application/pdf"
        or representation.get("metadata") != {}
    ):
        raise RuntimeError(
            f"OpenSearch document identity or content is incorrect: {payload!r}"
        )
    index_endpoint = f"{configuration['endpoint'].rstrip('/')}/{configuration['index']}"
    refresh_ms = await refresh_search_index(session, configuration) if refresh else 0.0

    query_started = time.perf_counter_ns()
    async with session.post(
        f"{index_endpoint}/_search",
        json={
            "query": {
                "bool": {
                    "filter": [
                        {"term": {"source_archive.keyword": "default"}},
                        {"term": {"source_document_id.keyword": ids["source"]}},
                        {"term": {"source_version_id.keyword": ids["source_version"]}},
                    ]
                }
            }
        },
        headers=headers,
        auth=auth,
    ) as response:
        body = await response.read()
        if response.status != 200:
            raise RuntimeError(
                f"OpenSearch query returned HTTP {response.status}: {body!r}"
            )
        result = json.loads(body)
    query_ms = (time.perf_counter_ns() - query_started) / 1_000_000
    hits = result.get("hits", {}).get("hits", [])
    if document_id not in {hit.get("_id") for hit in hits}:
        raise RuntimeError(f"Indexed source was not discoverable: {result!r}")

    return {
        "endpoint": endpoint,
        "index": configuration["index"],
        "document_id": document_id,
        "source_identity": {
            "kind": "archive",
            "archive": "default",
            "document_id": ids["source"],
            "version_id": ids["source_version"],
            "projection_revision": configuration["revision"],
        },
        "lookup_ms": lookup_ms,
        "refresh_ms": refresh_ms,
        "query_ms": query_ms,
    }


async def refresh_search_index(
    session: aiohttp.ClientSession, configuration: dict[str, Any]
) -> float:
    headers: dict[str, str] = {}
    if configuration.get("api_key"):
        headers["authorization"] = f"ApiKey {configuration['api_key']}"
    auth = (
        aiohttp.BasicAuth(configuration["username"], configuration["password"])
        if configuration.get("username")
        else None
    )
    endpoint = f"{configuration['endpoint'].rstrip('/')}/{configuration['index']}"
    started = time.perf_counter_ns()
    async with session.post(
        f"{endpoint}/_refresh", headers=headers, auth=auth
    ) as response:
        body = await response.read()
        if response.status != 200:
            raise RuntimeError(
                f"OpenSearch refresh returned HTTP {response.status}: {body!r}"
            )
    return (time.perf_counter_ns() - started) / 1_000_000


async def delete_and_validate_projection(
    session: aiohttp.ClientSession,
    archive_connection: asyncpg.Connection,
    archive_connection_lock: asyncio.Lock,
    configuration: dict[str, Any],
    ids: dict[str, str],
    search_document: dict[str, Any],
) -> float:
    headers: dict[str, str] = {}
    if configuration.get("api_key"):
        headers["authorization"] = f"ApiKey {configuration['api_key']}"
    auth = (
        aiohttp.BasicAuth(configuration["username"], configuration["password"])
        if configuration.get("username")
        else None
    )
    started = time.perf_counter_ns()
    for document_id in (ids["source"], ids["terminal"]):
        delete_endpoint = f"{ARCHIVE_BASE_URL}/api/v1/archive/archives/default/documents/{document_id}?safe=false"
        async with session.delete(delete_endpoint) as response:
            body = await response.read()
            if response.status != 202:
                raise RuntimeError(
                    f"Archive deletion returned HTTP {response.status}: {body!r}"
                )
    document_ids = [uuid.UUID(ids["source"]), uuid.UUID(ids["terminal"])]
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        async with archive_connection_lock:
            deleted_archives = await archive_connection.fetchval(
                "SELECT count(*) FROM archive_documents "
                "WHERE archive = 'default' AND document_id = ANY($1::uuid[]) "
                "AND lifecycle = 'deleted'",
                document_ids,
            )
        async with session.get(
            search_document["endpoint"], headers=headers, auth=auth
        ) as search_response:
            if search_response.status == 404 and deleted_archives == len(document_ids):
                break
        await asyncio.sleep(0.01)
    else:
        raise TimeoutError("Deleted archive or search projection remained available")
    return (time.perf_counter_ns() - started) / 1_000_000


async def validate_lifecycle(
    session: aiohttp.ClientSession,
    archive_connection: asyncpg.Connection,
    archive_connection_lock: asyncio.Lock,
    configuration: dict[str, Any],
    ids: dict[str, str],
    search_document: dict[str, Any],
) -> float:
    return await delete_and_validate_projection(
        session,
        archive_connection,
        archive_connection_lock,
        configuration,
        ids,
        search_document,
    )


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
            "JOIN node_executions execution ON execution._id = attempt.node_execution_id "
            "WHERE execution.flow_id = ANY($1::uuid[])",
            flow_ids,
        ),
        "tracked_operations": await connection.fetchval(
            "SELECT count(*) FROM tracked_operations operation "
            "JOIN node_executions execution ON execution._id = operation.node_execution_id "
            "WHERE execution.flow_id = ANY($1::uuid[])",
            flow_ids,
        ),
        "outcome_events": await connection.fetchval(
            "SELECT count(*) FROM outcome_events WHERE flow_id = ANY($1::uuid[])",
            flow_ids,
        ),
        "provider_event_inbox": await connection.fetchval(
            "SELECT count(*) FROM provider_event_inbox inbox "
            "JOIN tracked_operations operation ON operation._id = inbox.tracked_operation_id "
            "JOIN node_executions execution ON execution._id = operation.node_execution_id "
            "WHERE execution.flow_id = ANY($1::uuid[])",
            flow_ids,
        ),
    }
    if any(counts.values()):
        raise RuntimeError(f"Synchronous flows created tracked state: {counts}")
    return counts


def log_statistics(log_path: Path, runs: list[dict[str, Any]]) -> dict[str, Any]:
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
        flow_id = event.get("flow_id")
        if flow_id in events:
            events[flow_id].append(event)

    stages: list[dict[str, float]] = []
    for run in runs:
        flow_events = events[run["ids"]["flow"]]
        starts = [event for event in flow_events if event.get("start")]
        completes = [event for event in flow_events if event.get("complete")]
        archive_starts = sorted(
            (event for event in starts if event.get("kind") == "archive"),
            key=timestamp,
        )
        archive_completes = sorted(
            (event for event in completes if event.get("kind") == "archive"),
            key=timestamp,
        )
        search_starts = sorted(
            (event for event in starts if event.get("kind") == "search-index"),
            key=timestamp,
        )
        search_completes = sorted(
            (event for event in completes if event.get("kind") == "search-index"),
            key=timestamp,
        )
        if not all(
            len(stage_events) == 2
            for stage_events in (
                archive_starts,
                archive_completes,
                search_starts,
                search_completes,
            )
        ):
            raise RuntimeError(
                "Application log does not contain exactly two archive and two search "
                f"stages for flow {run['ids']['flow']}"
            )
        stages.append(
            {
                "processing_ms": elapsed(archive_starts[0], archive_completes[1]),
                "initial_archive_ms": elapsed(archive_starts[0], archive_completes[0]),
                "first_search_ms": elapsed(search_starts[0], search_completes[0]),
                "second_search_ms": elapsed(search_starts[1], search_completes[1]),
                "terminal_archive_ms": elapsed(archive_starts[1], archive_completes[1]),
            }
        )

    archive_durations = [
        duration
        for stage in stages
        for duration in (stage["initial_archive_ms"], stage["terminal_archive_ms"])
    ]
    search_durations = [
        duration
        for stage in stages
        for duration in (stage["first_search_ms"], stage["second_search_ms"])
    ]
    return {
        "errors": errors,
        "stages": {
            name: statistics_for([stage[name] for stage in stages])
            for name in stages[0]
        },
        "archive_stages": statistics_for(archive_durations),
        "search_stages": statistics_for(search_durations),
        "workload_stages": {
            name: {
                stage_name: statistics_for(
                    [
                        stage[stage_name]
                        for run, stage in zip(runs, stages, strict=True)
                        if run["workload"] == name
                    ]
                )
                for stage_name in stages[0]
            }
            for name in WORKLOADS
            if any(run["workload"] == name for run in runs)
        },
    }


async def execute(
    count: int, configuration: dict[str, Any], concurrency: int
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    connection = await connect_postgres()
    archive_connection = await connect_archive_postgres()
    runs: list[dict[str, Any]] = []
    search_documents: list[dict[str, Any]] = []
    timeout = aiohttp.ClientTimeout(total=35)
    archive_connection_lock = asyncio.Lock()
    semaphore = asyncio.Semaphore(concurrency)
    completed = 0
    execution_started = time.perf_counter_ns()
    try:
        await ensure_document_type(connection)
        async with aiohttp.ClientSession(timeout=timeout) as session:

            async def execute_one(index: int) -> None:
                nonlocal completed
                async with semaphore:
                    definition, ids = flow(index + 1)
                    form = aiohttp.FormData()
                    form.add_field(
                        "flow",
                        json.dumps(definition).encode(),
                        filename="flow.json",
                        content_type="application/json",
                    )
                    started = time.perf_counter_ns()
                    intake_started = time.perf_counter_ns()
                    async with session.post(
                        f"{INTAKE_BASE_URL}/api/v1/intake/", data=form
                    ) as response:
                        body = await response.read()
                        if response.status != 202:
                            raise RuntimeError(
                                f"Flow {index + 1} returned HTTP {response.status}: {body!r}"
                            )
                    intake_submission_ms = (
                        time.perf_counter_ns() - intake_started
                    ) / 1_000_000
                    await wait_for_version(
                        archive_connection,
                        ids["terminal_version"],
                        "Terminal marker",
                        archive_connection_lock,
                    )
                    duration_ms = (time.perf_counter_ns() - started) / 1_000_000
                    source_download_ms = await validate_archive_outputs(
                        session,
                        archive_connection,
                        ids,
                        archive_connection_lock,
                    )
                    search_document = await validate_search_document(
                        session, configuration, ids
                    )
                    search_documents.append(search_document)
                    deletion_cleanup_ms = await validate_lifecycle(
                        session,
                        archive_connection,
                        archive_connection_lock,
                        configuration,
                        ids,
                        search_document,
                    )
                    runs.append(
                        {
                            "index": index + 1,
                            "duration_ms": duration_ms,
                            "intake_submission_ms": intake_submission_ms,
                            "source_download_ms": source_download_ms,
                            "search_lookup_ms": search_document["lookup_ms"],
                            "search_refresh_ms": search_document["refresh_ms"],
                            "search_query_ms": search_document["query_ms"],
                            "deletion_cleanup_ms": deletion_cleanup_ms,
                            "workload": ids["workload"],
                            "ids": ids,
                        }
                    )
                    completed += 1
                    if completed % 10 == 0 or completed == count:
                        print(
                            f"completed={completed} latest_ms={duration_ms:.3f}",
                            flush=True,
                        )

            await asyncio.gather(*(execute_one(index) for index in range(count)))

        tracked_state = await assert_no_tracked_state(
            connection, [uuid.UUID(run["ids"]["flow"]) for run in runs]
        )
    finally:
        await archive_connection.close()
        await connection.close()

    durations = [run["duration_ms"] for run in runs]
    wall_seconds = (time.perf_counter_ns() - execution_started) / 1_000_000_000
    workloads = {
        name: {
            "count": len(selected),
            "flow": statistics_for([run["duration_ms"] for run in selected]),
            "source_download": statistics_for(
                [run["source_download_ms"] for run in selected]
            ),
            "search_query": statistics_for(
                [run["search_query_ms"] for run in selected]
            ),
            "deletion_cleanup": statistics_for(
                [run["deletion_cleanup_ms"] for run in selected]
            ),
        }
        for name in WORKLOADS
        if (selected := [run for run in runs if run["workload"] == name])
    }
    return (
        {
            "functional": {
                "flows_completed": len(runs),
                "terminal_archives_validated": len(runs),
                "source_downloads_validated": len(runs),
                "search_documents_validated": len(search_documents),
                "search_queries_validated": len(search_documents),
                "deletion_cleanups_validated": len(search_documents),
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
                "all": statistics_for(durations),
                "concurrency": concurrency,
                "wall_seconds": wall_seconds,
                "flows_per_second": count / wall_seconds,
                "intake_submission": statistics_for(
                    [run["intake_submission_ms"] for run in runs]
                ),
                "cold_first_ms": durations[0],
                "excluding_first": statistics_for(durations[1:] or durations),
                "source_download": statistics_for(
                    [run["source_download_ms"] for run in runs]
                ),
                "search_lookup": statistics_for(
                    [run["search_lookup_ms"] for run in runs]
                ),
                "search_refresh": statistics_for(
                    [run["search_refresh_ms"] for run in runs]
                ),
                "search_query": statistics_for(
                    [run["search_query_ms"] for run in runs]
                ),
                "deletion_cleanup": statistics_for(
                    [run["deletion_cleanup_ms"] for run in runs]
                ),
                "workloads": workloads,
            },
            "search_documents": search_documents,
            "runs": runs,
        },
        runs,
    )


async def main(
    count: int,
    concurrency: int,
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
        report, runs = await execute(count, configuration, concurrency)

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
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG)
    parser.add_argument("--compose-file", type=Path)
    arguments = parser.parse_args()
    if arguments.count < 1:
        parser.error("--count must be at least 1")
    if arguments.concurrency < 1 or arguments.concurrency > 100:
        parser.error("--concurrency must be between 1 and 100")
    return arguments


if __name__ == "__main__":
    arguments = parse_arguments()
    asyncio.run(
        main(
            arguments.count,
            arguments.concurrency,
            arguments.output,
            arguments.log,
            arguments.compose_file,
        )
    )
