"""Run and measure the development-standard flow profile archive scenario."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import time
import uuid

from pathlib import Path
from typing import Any

import aiohttp
import asyncpg

from common import BASE_URL
from common import ROOT
from common import SCENARIO_ARTIFACTS
from common import connect_archive_postgres
from common import connect_postgres
from common import elapsed
from common import standalone_application
from common import statistics_for

import xarta.protocol.dag

from xarta.protocol.document.source import DocumentSourceResult
from xarta.storage.configuration import close_temporary_storage

PROFILE_NAME = "development-standard"
PROFILE_VERSION = 1
PROFILE_REFERENCE = f"{PROFILE_NAME}@{PROFILE_VERSION}"
PROFILE_FINGERPRINT = (
    "sha256:3db653ad661e010cd6c13b73820e3c0ea34b1d6ec311756c961ffc69ceb003f3"
)
EXPECTED_CONTRACT = {
    "delivery_profile": PROFILE_REFERENCE,
    "name": PROFILE_NAME,
    "version": PROFILE_VERSION,
    "current": True,
    "description": "Development-only example profile",
    "fingerprint": PROFILE_FINGERPRINT,
    "inputs": {
        "document": {
            "type": "document-source",
            "required": True,
            "allowed_sources": ["generate"],
        }
    },
    "generated_values": {},
}
DEFAULT_OUTPUT = SCENARIO_ARTIFACTS / "flow-profile/results.json"
DEFAULT_LOG = SCENARIO_ARTIFACTS / "flow-profile/application.log"


def source_bytes(index: int) -> bytes:
    return f"xarta flow profile scenario document\nsequence={index:04d}\n".encode()


def identities(flow_id: uuid.UUID) -> dict[str, str]:
    document_id = uuid.uuid5(flow_id, "flow-profile-scenario:document")
    node_id = uuid.uuid5(
        flow_id,
        f"xarta:flow-profile:{PROFILE_REFERENCE}:{PROFILE_FINGERPRINT}:node:archive-document",
    )
    execution_id = uuid.uuid5(flow_id, f"root:{node_id}")
    version_id = uuid.uuid5(execution_id, f"{document_id}:0")
    return {
        "flow": str(flow_id),
        "document": str(document_id),
        "archive_node": str(node_id),
        "archive_execution": str(execution_id),
        "version": str(version_id),
    }


async def validate_contract(session: aiohttp.ClientSession) -> dict[str, Any]:
    endpoint = f"{BASE_URL}/api/v1/intake/profiles/{PROFILE_NAME}/{PROFILE_VERSION}"
    async with session.get(endpoint) as response:
        body = await response.read()
        if response.status != 200:
            raise RuntimeError(
                f"Profile contract returned HTTP {response.status}: {body!r}"
            )
        contract = json.loads(body)
    if contract != EXPECTED_CONTRACT:
        raise RuntimeError(f"Deployed profile contract is incorrect: {contract!r}")

    async with session.get(f"{BASE_URL}/api/v1/intake/profiles") as response:
        body = await response.read()
        if response.status != 200:
            raise RuntimeError(
                f"Profile index returned HTTP {response.status}: {body!r}"
            )
        profiles = json.loads(body).get("profiles")
    if not isinstance(profiles, list) or profiles.count(contract) != 1:
        raise RuntimeError("Profile index does not contain the exact deployed contract")
    return contract


async def wait_for_version(
    archive_connection: asyncpg.Connection,
    document_id: uuid.UUID,
    version_id: uuid.UUID,
) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if await archive_connection.fetchval(
            "SELECT EXISTS("
            "SELECT 1 FROM archive_documents document "
            "JOIN archive_document_versions version "
            "ON version.aggregate_id = document._id "
            "WHERE document.archive = 'default' AND document.document_id = $1 "
            "AND version.version_id = $2)",
            document_id,
            version_id,
        ):
            return
        await asyncio.sleep(0.01)
    raise TimeoutError(f"Archive version {version_id} did not become available")


async def validate_archive(
    session: aiohttp.ClientSession,
    archive_connection: asyncpg.Connection,
    ids: dict[str, str],
    expected_data: bytes,
) -> float:
    endpoint = f"{BASE_URL}/api/v1/archive/documents/{ids['document']}/versions/{ids['version']}"
    started = time.perf_counter_ns()
    async with session.get(endpoint) as response:
        archived = await response.read()
        if response.status != 200:
            raise RuntimeError(
                f"Archive download returned HTTP {response.status}: {archived!r}"
            )
        if response.content_type != "text/plain":
            raise RuntimeError(f"Archive download returned {response.content_type!r}")
    download_ms = (time.perf_counter_ns() - started) / 1_000_000
    if archived != expected_data:
        raise RuntimeError("Archived document bytes differ from the generated source")

    async with session.get(f"{endpoint}/details") as response:
        body = await response.read()
        if response.status != 200:
            raise RuntimeError(
                f"Archive details returned HTTP {response.status}: {body!r}"
            )
        details = json.loads(body)
    expected_details = {
        "id": ids["document"],
        "archive": "default",
        "version_id": ids["version"],
        "parent_version_id": None,
        "document_type": None,
        "metadata": {},
        "expires": None,
        "state": "available",
    }
    comparable_details = {key: details.get(key) for key in expected_details}
    if comparable_details != expected_details:
        raise RuntimeError(f"Archived document metadata is incorrect: {details!r}")

    row = await archive_connection.fetchrow(
        "SELECT version.metadata, representation.content_type, "
        "representation.state::text AS state, representation.size, "
        "encode(representation.checksum, 'hex') AS checksum "
        "FROM archive_documents document JOIN archive_document_versions version "
        "ON version.aggregate_id = document._id "
        "JOIN archive_document_representations representation "
        "ON representation._id = version.default_representation_id "
        "WHERE document.archive = 'default' AND document.document_id = $1 "
        "AND version.version_id = $2",
        uuid.UUID(ids["document"]),
        uuid.UUID(ids["version"]),
    )
    expected_row = {
        "metadata": {},
        "content_type": "text/plain",
        "state": "available",
        "size": len(expected_data),
        "checksum": hashlib.sha512(expected_data).hexdigest(),
    }
    actual_row = dict(row or {})
    if isinstance(actual_row.get("metadata"), str):
        actual_row["metadata"] = json.loads(actual_row["metadata"])
    if actual_row != expected_row:
        raise RuntimeError(f"Archived PostgreSQL metadata is incorrect: {row!r}")
    return download_ms


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
        raise RuntimeError(f"Synchronous profile flows created tracked state: {counts}")
    return counts


async def replay_first(
    session: aiohttp.ClientSession,
    archive_connection: asyncpg.Connection,
    run: dict[str, Any],
) -> dict[str, Any]:
    started = time.perf_counter_ns()
    async with session.post(
        f"{BASE_URL}/api/v1/intake/profiles/{PROFILE_NAME}/{PROFILE_VERSION}",
        json=run["submission"],
    ) as response:
        body = await response.read()
        if response.status != 202:
            raise RuntimeError(
                f"Profile replay returned HTTP {response.status}: {body!r}"
            )
        replay = json.loads(body)
    replay_ms = (time.perf_counter_ns() - started) / 1_000_000
    expected_response = {
        "id": run["ids"]["flow"],
        "delivery_profile": PROFILE_REFERENCE,
        "semantic_fingerprint": run["semantic_fingerprint"],
    }
    if replay != expected_response:
        raise RuntimeError(f"Profile replay response changed: {replay!r}")
    await asyncio.sleep(0.25)
    version_count = await archive_connection.fetchval(
        "SELECT count(*) FROM archive_documents document "
        "JOIN archive_document_versions version "
        "ON version.aggregate_id = document._id "
        "WHERE document.archive = 'default' AND document.document_id = $1",
        uuid.UUID(run["ids"]["document"]),
    )
    if version_count != 1:
        raise RuntimeError(f"Profile replay created {version_count} archive versions")
    return {
        "accepted": True,
        "response_stable": True,
        "archive_version_count": version_count,
        "duration_ms": replay_ms,
    }


async def execute(
    connection: asyncpg.Connection,
    archive_connection: asyncpg.Connection,
    count: int,
) -> dict[str, Any]:
    runs: list[dict[str, Any]] = []
    timeout = aiohttp.ClientTimeout(total=35)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        contract = await validate_contract(session)
        for index in range(1, count + 1):
            ids = identities(uuid.uuid4())
            data = source_bytes(index)
            metadata = {
                "original_filename": f"flow-profile-{index:04d}.txt",
                "scenario": "flow-profile",
                "sequence": index,
            }
            await DocumentSourceResult.parse(
                id=ids["document"],
                content_type="text/plain",
                data=data,
                metadata=metadata,
            ).persist()
            submission = {
                "id": ids["flow"],
                "correlation_id": ids["flow"],
                "inputs": {"document": {"source": "generate", "id": ids["document"]}},
            }

            started = time.perf_counter_ns()
            async with session.post(
                f"{BASE_URL}/api/v1/intake/profiles/{PROFILE_NAME}/{PROFILE_VERSION}",
                json=submission,
            ) as response:
                body = await response.read()
                if response.status != 202:
                    raise RuntimeError(
                        f"Profile execution {index} returned HTTP {response.status}: {body!r}"
                    )
                intake_response = json.loads(body)
            expected_response = {
                "id": ids["flow"],
                "delivery_profile": PROFILE_REFERENCE,
            }
            if {key: intake_response.get(key) for key in expected_response} != (
                expected_response
            ):
                raise RuntimeError(
                    f"Profile execution {index} response is incorrect: {intake_response!r}"
                )
            fingerprint = intake_response.get("semantic_fingerprint")
            if not isinstance(fingerprint, str) or not fingerprint.startswith(
                "sha256:"
            ):
                raise RuntimeError("Profile intake omitted its semantic fingerprint")

            await wait_for_version(
                archive_connection,
                uuid.UUID(ids["document"]),
                uuid.UUID(ids["version"]),
            )
            processing_ms = (time.perf_counter_ns() - started) / 1_000_000
            download_ms = await validate_archive(session, archive_connection, ids, data)
            total_ms = (time.perf_counter_ns() - started) / 1_000_000
            runs.append(
                {
                    "index": index,
                    "processing_ms": processing_ms,
                    "download_ms": download_ms,
                    "total_ms": total_ms,
                    "semantic_fingerprint": fingerprint,
                    "ids": ids,
                    "submission": submission,
                }
            )
            if index % 10 == 0 or index == count:
                print(
                    f"completed={index} processing_ms={processing_ms:.3f} total_ms={total_ms:.3f}",
                    flush=True,
                )

        replay = await replay_first(session, archive_connection, runs[0])
        tracked_state = await assert_no_tracked_state(
            connection, [uuid.UUID(run["ids"]["flow"]) for run in runs]
        )

    durations = [run["processing_ms"] for run in runs]
    return {
        "functional": {
            "profile_contract": contract,
            "flows_completed": len(runs),
            "archives_validated": len(runs),
            "deterministic_identities_validated": len(runs),
            "replay": replay,
            "tracked_state": tracked_state,
        },
        "performance": {
            "processing": statistics_for(durations),
            "cold_processing_ms": durations[0],
            "steady_processing": statistics_for(durations[1:] or durations),
            "download": statistics_for([run["download_ms"] for run in runs]),
            "end_to_end": statistics_for([run["total_ms"] for run in runs]),
        },
        "runs": runs,
    }


def log_statistics(log_path: Path, runs: list[dict[str, Any]]) -> dict[str, Any]:
    flow_ids = [run["ids"]["flow"] for run in runs]
    events: dict[str, list[dict[str, Any]]] = {flow_id: [] for flow_id in flow_ids}
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
        if flow_id in events and event.get("kind") == "archive":
            events[flow_id].append(event)

    archive_ms = []
    for flow_id in flow_ids:
        flow_events = events[flow_id]
        starts = [event for event in flow_events if event.get("start")]
        completes = [event for event in flow_events if event.get("complete")]
        if len(starts) != 1 or len(completes) != 1:
            raise RuntimeError(
                f"Application log has incomplete or duplicate archive stages for {flow_id}"
            )
        archive_ms.append(elapsed(starts[0], completes[0]))
    return {
        "archive_stage": {
            "all": statistics_for(archive_ms),
            "cold_ms": archive_ms[0],
            "steady": statistics_for(archive_ms[1:] or archive_ms),
        },
        "errors": errors,
    }


async def main(count: int, output_path: Path, log_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    connection = await connect_postgres()
    archive_connection = await connect_archive_postgres()
    try:
        async with standalone_application(
            log_path,
            {"archive"},
            {"FLOW_PROFILES_CONFIG_PATH": str(ROOT / ".dev/conf/flow-profiles.json")},
        ):
            report = await execute(connection, archive_connection, count)
    finally:
        await archive_connection.close()
        await connection.close()
        await close_temporary_storage()

    log_report = log_statistics(log_path, report["runs"])
    report["performance"]["app_log"] = log_report
    output_path.write_text(json.dumps(report, indent=2) + "\n")
    if log_report["errors"]:
        raise RuntimeError("Application log contains errors")
    print(json.dumps({**report["functional"], **report["performance"]}, indent=2))
    print(f"report={output_path}")
    print(f"application_log={log_path}")


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=20)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG)
    arguments = parser.parse_args()
    if arguments.count < 1:
        parser.error("--count must be at least 1")
    return arguments


if __name__ == "__main__":
    arguments = parse_arguments()
    asyncio.run(main(arguments.count, arguments.output, arguments.log))
