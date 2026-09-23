"""Exercise local postal intake, station production, and tracked handover."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import sys
import time
import uuid

from datetime import UTC
from datetime import datetime
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp
import asyncpg

from pyhanko.pdf_utils import generic
from pyhanko.pdf_utils.reader import PdfFileReader

from tests.fixtures.jinja_selftest import SELFTEST_TEMPLATE_ENGINE
from tests.fixtures.jinja_selftest import jinja_selftest_template_engine_options
from tests.scenarios.common import BASE_URL
from tests.scenarios.common import ROOT
from tests.scenarios.common import SCENARIO_ARTIFACTS
from tests.scenarios.common import connect_archive_postgres
from tests.scenarios.common import connect_postgres
from tests.scenarios.common import identifier
from tests.scenarios.common import standalone_application

sys.path.insert(0, str(ROOT / "apps/postal-station/src"))

from postal_station.api_client import DownloadedPackage
from postal_station.application import PostalStation
from postal_station.configuration import StationConfig
from postal_station.models import PackageReceipt
from postal_station.models import PrintAttempt
from postal_station.packages import PackageVerifier
from postal_station.printing.cups import CupsPrinter
from postal_station.printing.fake import FakePrinter
from postal_station.rendering import Renderer

DEFAULT_DIRECTORY = SCENARIO_ARTIFACTS / "postal-local"
DEFAULT_OUTPUT = DEFAULT_DIRECTORY / "results.json"
DEFAULT_LOG = DEFAULT_DIRECTORY / "application.log"
STATION_ID = "development-station"
STATION_TOKEN = "development-postal-station-token"
RUN_TOKEN = "postal-local-scenario-run-token"
DOCUMENT_TYPE = "scenario-jinja-selftest"
RUN_COUNT = 3
STAMP_POLICY_REVISION = "development-v1"
TARIFF_REVISION = "development-v1"
SUPPLY_INSTRUCTIONS = [
    {
        "reference": "prior-1",
        "description": "Synthetic Bpost Prior unit stamp",
        "quantity": 1,
    }
]
PRICE_LINES = [
    {
        "reference": "prior-1",
        "quantity": 1,
        "unit_price": "1.50",
        "amount": "1.50",
    }
]


async def ensure_document_type(connection: asyncpg.Connection) -> None:
    await connection.execute(
        "INSERT INTO document_types "
        "(identifier, default_template_engine, default_content_type) "
        "VALUES ($1, $2, $3) ON CONFLICT (identifier) DO NOTHING",
        DOCUMENT_TYPE,
        SELFTEST_TEMPLATE_ENGINE,
        "application/pdf",
    )


def postal_flow() -> tuple[dict[str, Any], dict[str, str]]:
    ids = {
        "flow": identifier(),
        "generate_node": identifier(),
        "generated_1": identifier(),
        "generated_2": identifier(),
        "archive_node": identifier(),
        "archive_document_1": identifier(),
        "archive_document_2": identifier(),
        "archive_version_1": identifier(),
        "archive_version_2": identifier(),
        "postal_node": identifier(),
    }
    documents = [
        {
            "source": "render",
            "id": ids[f"generated_{ordinal}"],
            "document_type": DOCUMENT_TYPE,
            "content_type": "application/pdf",
            "template_engine": SELFTEST_TEMPLATE_ENGINE,
            "template_engines_options": jinja_selftest_template_engine_options(),
            "payload": {
                "content_type": "application/json",
                "data": {"scenario": "postal-local", "ordinal": ordinal},
            },
        }
        for ordinal in (1, 2)
    ]
    archived_documents = [
        {
            "archive": "default",
            "document_id": ids[f"archive_document_{ordinal}"],
            "version_id": ids[f"archive_version_{ordinal}"],
            "document_type": DOCUMENT_TYPE,
            "metadata": {},
            "representations": [
                {
                    "source": {
                        "source": "generate",
                        "id": ids[f"generated_{ordinal}"],
                    }
                }
            ],
        }
        for ordinal in (1, 2)
    ]
    postal_documents = [
        {
            "source": "archive",
            "archive": "default",
            "id": ids[f"archive_document_{ordinal}"],
            "version": ids[f"archive_version_{ordinal}"],
            "role": f"document-{ordinal:03d}",
        }
        for ordinal in (1, 2)
    ]
    definition = {
        "id": ids["flow"],
        "correlation_id": ids["flow"],
        "dag": {
            "id": ids["generate_node"],
            "kind": "generate",
            "documents": documents,
            "on": {
                "success": [
                    {
                        "id": ids["archive_node"],
                        "kind": "archive",
                        "destination": "default-archive",
                        "documents": archived_documents,
                        "on": {
                            "success": [
                                {
                                    "id": ids["postal_node"],
                                    "kind": "postal",
                                    "destination": "local-brussels",
                                    "mailpiece": {
                                        "type": "letter",
                                        "version": 1,
                                        "content": {"documents": postal_documents},
                                        "recipient": {
                                            "name": "Postal Scenario Ordinary",
                                            "address": {
                                                "format": "structured",
                                                "street": "Wetstraat",
                                                "house_number": "16",
                                                "postal_code": "1000",
                                                "city": "Brussel",
                                                "country_code": "BE",
                                            },
                                        },
                                        "sender": {
                                            "type": "profile",
                                            "id": "scenario-sender",
                                        },
                                        "service": {
                                            "type": "ordinary",
                                            "version": 1,
                                            "speed": "priority",
                                        },
                                        "print": {
                                            "version": 1,
                                            "color_mode": "monochrome",
                                            "sides": "duplex_long_edge",
                                            "document_boundary": "continuous",
                                        },
                                        "references": {"scenario": "postal-local"},
                                        "extensions": {},
                                    },
                                }
                            ]
                        },
                    }
                ]
            },
        },
    }
    return definition, ids


def station_headers(*, idempotency_key: str | None = None) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {STATION_TOKEN}",
        "X-Postal-Run-Token": RUN_TOKEN,
    }
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    return headers


async def json_request(
    session: aiohttp.ClientSession,
    method: str,
    path: str,
    *,
    expected: int = 200,
    body: object | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    async with session.request(
        method, f"{BASE_URL}{path}", json=body, headers=headers
    ) as response:
        content = await response.read()
        if response.status != expected:
            raise RuntimeError(
                f"{method} {path} returned HTTP {response.status}: {content!r}"
            )
    value = json.loads(content)
    if not isinstance(value, dict):
        raise RuntimeError(f"{method} {path} returned a non-object response")
    return value


async def wait_for_admission(
    connection: asyncpg.Connection, flow_ids: list[uuid.UUID]
) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        count = await connection.fetchval(
            "SELECT count(*) FROM postal_local.operations local "
            "JOIN tracked_operations tracked ON tracked._id = local.tracked_operation_id "
            "JOIN node_executions execution ON execution._id = tracked.node_execution_id "
            "WHERE execution.flow_id = ANY($1::uuid[])",
            flow_ids,
        )
        if count == len(flow_ids):
            await connection.execute(
                "UPDATE postal_local.production_tasks task SET priority = 2147483647 "
                "FROM postal_local.operations local "
                "JOIN tracked_operations tracked ON tracked._id = local.tracked_operation_id "
                "JOIN node_executions execution ON execution._id = tracked.node_execution_id "
                "WHERE task.operation_id = local._id "
                "AND execution.flow_id = ANY($1::uuid[])",
                flow_ids,
            )
            return
        await asyncio.sleep(0.02)
    raise TimeoutError("Postal flows were not admitted for local production")


async def submit_flows(
    session: aiohttp.ClientSession, runs: list[dict[str, Any]]
) -> None:
    for run in runs:
        async with session.post(
            f"{BASE_URL}/api/v1/intake/",
            json=run["definition"],
        ) as response:
            content = await response.read()
            if response.status != 202:
                raise RuntimeError(
                    f"Postal intake returned HTTP {response.status}: {content!r}"
                )
            if json.loads(content) != {"id": run["ids"]["flow"]}:
                raise RuntimeError("Postal intake returned an unexpected flow identity")


async def claim_and_verify_package(
    session: aiohttp.ClientSession,
    extracted_directory: Path,
    package_path: Path,
    claim_key: str,
    expected_tasks: int,
) -> tuple[dict[str, Any], Any, bytes]:
    claim_path = "/api/v1/postal/local/production-runs/claims"
    headers = station_headers(idempotency_key=claim_key)
    claim = await json_request(
        session,
        "POST",
        claim_path,
        expected=201,
        body={"limits": {"items": expected_tasks}},
        headers=headers,
    )
    if (
        claim["station_id"] != STATION_ID
        or claim["claim_token"] != RUN_TOKEN
        or len(claim["task_ids"]) != expected_tasks
    ):
        raise RuntimeError(f"Atomic station claim did not select every task: {claim!r}")
    replay = await json_request(
        session,
        "POST",
        claim_path,
        body={"limits": {"items": expected_tasks}},
        headers=headers,
    )
    if (
        replay["id"] != claim["id"]
        or replay["task_ids"] != claim["task_ids"]
        or not replay["duplicate"]
    ):
        raise RuntimeError(
            "Station claim replay did not return the original atomic claim"
        )

    download_path = f"/api/v1/postal/local/production-runs/{claim['id']}/package"
    async with session.get(
        f"{BASE_URL}{download_path}", headers=station_headers()
    ) as response:
        package_bytes = await response.read()
        if response.status != 200:
            raise RuntimeError(
                f"Production package returned HTTP {response.status}: {package_bytes!r}"
            )
        receipt = PackageReceipt(
            response.headers["X-Postal-Package-SHA256"],
            response.headers["X-Postal-Manifest-SHA256"],
            int(response.headers["X-Postal-Package-Bytes"]),
        )
    package_path.write_bytes(package_bytes)
    if extracted_directory.exists():
        shutil.rmtree(extracted_directory)
    verified = PackageVerifier().verify(package_bytes, receipt, extracted_directory)
    if (
        verified.manifest.run_id != claim["id"]
        or len(verified.manifest.jobs) != expected_tasks
    ):
        raise RuntimeError("Verified production package has unexpected work")
    forbidden = {"letter.pdf", "traveller.pdf", "content.pdf"}
    if any(path.name in forbidden for path in extracted_directory.rglob("*.pdf")):
        raise RuntimeError("Server package contains a station-rendered PDF")

    acknowledgement_path = f"{download_path}-acknowledgements"
    acknowledgement = {
        "package_sha256": receipt.package_sha256,
        "manifest_sha256": receipt.manifest_sha256,
        "byte_count": receipt.byte_count,
    }
    acknowledgement_headers = station_headers(
        idempotency_key=f"postal-local-package-receipt:{claim['id']}"
    )
    first = await json_request(
        session,
        "POST",
        acknowledgement_path,
        body=acknowledgement,
        headers=acknowledgement_headers,
    )
    duplicate = await json_request(
        session,
        "POST",
        acknowledgement_path,
        body=acknowledgement,
        headers=acknowledgement_headers,
    )
    if first != {"duplicate": False} or duplicate != {"duplicate": True}:
        raise RuntimeError("Package receipt was not idempotent")
    return claim, verified, package_bytes, receipt


class ScenarioStationApi:
    def __init__(self, session: aiohttp.ClientSession, downloaded: DownloadedPackage):
        self.session = session
        self.downloaded = downloaded

    async def download_package(self, run_id: str) -> DownloadedPackage:
        return self.downloaded

    async def active_runs(self, station_id):
        raise AssertionError("unused")

    async def acknowledge_package(self, run_id, receipt):
        raise AssertionError("unused")

    async def claim_run(self, idempotency_key, limits=None):
        raise AssertionError("unused")

    async def submit_scan(self, station_id, barcode, mode, *, handover_batch_id=None):
        raise AssertionError("unused")

    async def create_print_attempt(
        self, run_id, job_id, generation, rendered_sha256, rendered_byte_count
    ) -> PrintAttempt:
        value = await json_request(
            self.session,
            "POST",
            f"/api/v1/postal/local/print-jobs/{job_id}/attempts",
            expected=201,
            body={
                "generation": generation,
                "rendered_sha256": rendered_sha256,
                "rendered_byte_count": rendered_byte_count,
            },
            headers=station_headers(idempotency_key=f"postal-local-print:{job_id}"),
        )
        return PrintAttempt.from_dict(value)

    async def report_print_event(
        self, attempt_id, event, *, printer_job_id=None, detail=None
    ) -> None:
        body: dict[str, Any] = {"event": event.value}
        if printer_job_id is not None:
            body["printer_job_id"] = printer_job_id
        if detail is not None:
            body["detail"] = detail
        await json_request(
            self.session,
            "POST",
            f"/api/v1/postal/local/print-attempts/{attempt_id}/events",
            body=body,
            headers=station_headers(
                idempotency_key=f"postal-local-print-complete:{attempt_id}"
            ),
        )


async def print_sequentially(
    session: aiohttp.ClientSession,
    run_id: str,
    package_bytes: bytes,
    receipt: PackageReceipt,
    cache_directory: Path,
    printer: Any,
) -> Any:
    api = ScenarioStationApi(session, DownloadedPackage(package_bytes, receipt))
    config = StationConfig(BASE_URL, STATION_ID, "scenario-printer", cache_directory)
    station = PostalStation(config, api, printer, PackageVerifier())
    await station.process_run(run_id, acknowledge=False)
    return printer


async def scan(
    session: aiohttp.ClientSession,
    task_id: str,
    mode: str,
    handover_batch_id: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "station_id": STATION_ID,
        "barcode": task_id,
        "mode": mode,
        "occurred_at": datetime.now(UTC).isoformat(),
    }
    if handover_batch_id is not None:
        body["handover_batch_id"] = handover_batch_id
    return await json_request(
        session,
        "POST",
        "/api/v1/postal/local/production-scans",
        body=body,
        headers=station_headers(idempotency_key=f"postal-local-scan:{task_id}:{mode}"),
    )


async def complete_physical_workflow(
    session: aiohttp.ClientSession, task_ids: list[str]
) -> tuple[str, int]:
    duplicate_scans = 0
    for mode in ("start_production", "ready_for_handover"):
        for task_id in task_ids:
            first = await scan(session, task_id, mode)
            duplicate = await scan(session, task_id, mode)
            if first["duplicate"] or not duplicate["duplicate"]:
                raise RuntimeError(f"{mode} scan was not durably deduplicated")
            duplicate_scans += 1
    batch_request = {
        "service_date": datetime.now(ZoneInfo("Europe/Brussels")).date().isoformat(),
        "task_ids": task_ids,
        "operator_reference": "postal-local-scenario",
    }
    batch = await json_request(
        session,
        "POST",
        "/api/v1/postal/local/handover-batches",
        expected=201,
        body=batch_request,
        headers=station_headers(idempotency_key="postal-local-handover-batch"),
    )
    replayed_batch = await json_request(
        session,
        "POST",
        "/api/v1/postal/local/handover-batches",
        expected=200,
        body=batch_request,
        headers=station_headers(idempotency_key="postal-local-handover-batch"),
    )
    if replayed_batch != {"id": batch["id"], "duplicate": True}:
        raise RuntimeError("Handover batch was not durably deduplicated")
    for task_id in task_ids:
        first = await scan(session, task_id, "confirm_handover", batch["id"])
        duplicate = await scan(session, task_id, "confirm_handover", batch["id"])
        if first["duplicate"] or not duplicate["duplicate"]:
            raise RuntimeError("confirm_handover scan was not durably deduplicated")
        duplicate_scans += 1
    return batch["id"], duplicate_scans


async def download_archived_sources(
    session: aiohttp.ClientSession,
    archive_connection: asyncpg.Connection,
    runs: list[dict[str, Any]],
    download_directory: Path,
) -> tuple[dict[str, list[dict[str, Any]]], dict[tuple[str, int], bytes]]:
    if download_directory.exists():
        shutil.rmtree(download_directory)
    download_directory.mkdir(parents=True, exist_ok=True)
    results: dict[str, list[dict[str, Any]]] = {}
    content: dict[tuple[str, int], bytes] = {}
    for run in runs:
        ids = run["ids"]
        flow_results = []
        for ordinal in (1, 2):
            document_id = ids[f"archive_document_{ordinal}"]
            version_id = ids[f"archive_version_{ordinal}"]
            endpoint = f"/api/v1/archive/documents/{document_id}/versions/{version_id}"
            async with session.get(f"{BASE_URL}{endpoint}") as response:
                data = await response.read()
                if response.status != 200 or response.content_type != "application/pdf":
                    raise RuntimeError(
                        f"Archive version download {endpoint} returned HTTP "
                        f"{response.status} and {response.content_type!r}: {data!r}"
                    )
            if not data.startswith(b"%PDF-"):
                raise RuntimeError(f"Archive version {version_id} is not a PDF")
            available = await archive_connection.fetchval(
                "SELECT EXISTS(SELECT 1 FROM archive_document_versions WHERE version_id = $1 AND state = 'available')",
                uuid.UUID(version_id),
            )
            if not available:
                raise RuntimeError(f"Archive version {version_id} is not available")
            path = download_directory / f"{ordinal:03d}-{version_id}.pdf"
            path.write_bytes(data)
            digest = hashlib.sha256(data).hexdigest()
            content[(ids["flow"], ordinal)] = data
            flow_results.append(
                {
                    "ordinal": ordinal,
                    "document_id": document_id,
                    "version_id": version_id,
                    "path": str(path),
                    "sha256": digest,
                    "bytes": len(data),
                }
            )
        results[ids["flow"]] = flow_results
    return results, content


def _validate_letter_checksums(directory: Path) -> list[str]:
    checksum_paths = sorted(directory.glob("*checksum*"))
    for checksum_path in checksum_paths:
        for line in checksum_path.read_text(encoding="ascii").splitlines():
            digest, separator, name = line.partition("  ")
            target = directory.joinpath(*name.split("/"))
            if (
                not separator
                or not target.is_file()
                or hashlib.sha256(target.read_bytes()).hexdigest() != digest
            ):
                raise RuntimeError(
                    f"Invalid per-letter checksum entry in {checksum_path}: {line!r}"
                )
    return [str(path) for path in checksum_paths]


def _pdf_page_streams(document: bytes) -> list[bytes]:
    reader = PdfFileReader(BytesIO(document), strict=True)
    pages = []
    for page_index in range(int(reader.root["/Pages"]["/Count"])):
        page_ref, _ = reader.find_page_for_modification(page_index)
        contents = page_ref.get_object().get("/Contents")
        if contents is None:
            pages.append(b"")
            continue
        contents = contents.get_object()
        streams = contents if isinstance(contents, generic.ArrayObject) else (contents,)
        pages.append(b"\n".join(stream.get_object().data for stream in streams))
    return pages


async def evaluate_package(
    connection: asyncpg.Connection,
    package: Any,
    runs: list[dict[str, Any]],
    archived_content: dict[tuple[str, int], bytes],
    rendered_directory: Path,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    flow_ids = [uuid.UUID(run["ids"]["flow"]) for run in runs]
    rows = await connection.fetch(
        "SELECT execution.flow_id, local.id operation_id, task.id task_id, plan.plan "
        "FROM node_executions execution "
        "JOIN tracked_operations tracked ON tracked.node_execution_id = execution._id "
        "JOIN postal_local.operations local ON local.tracked_operation_id = tracked._id "
        "JOIN postal_local.production_plans plan ON plan.operation_id = local._id "
        "JOIN postal_local.production_tasks task ON task.operation_id = local._id "
        "WHERE execution.flow_id = ANY($1::uuid[])",
        flow_ids,
    )
    if len(rows) != len(runs):
        raise RuntimeError("Production plans do not map one-to-one to submitted flows")
    by_operation = {str(row["operation_id"]): row for row in rows}
    run_by_flow = {run["ids"]["flow"]: run for run in runs}
    letters = package.manifest.letters
    if len(letters) != len(runs):
        raise RuntimeError("Production package has an unexpected letter count")

    evaluations: dict[str, dict[str, Any]] = {}
    rendered_outputs: dict[str, Any] = {}
    renderer = Renderer()
    for letter in letters:
        operation_id = letter.operation_id
        row = by_operation.get(operation_id)
        if row is None:
            raise RuntimeError(f"Package contains unknown operation {operation_id}")
        flow_id = str(row["flow_id"])
        run = run_by_flow[flow_id]
        ids = run["ids"]
        plan = row["plan"]
        if isinstance(plan, str):
            plan = json.loads(plan)

        expected_references = [
            {
                "source": "archive",
                "archive": "default",
                "id": ids[f"archive_document_{ordinal}"],
                "version": ids[f"archive_version_{ordinal}"],
                "role": f"document-{ordinal:03d}",
            }
            for ordinal in (1, 2)
        ]
        franking = plan["franking"]
        pricing = franking["pricing"]
        estimated_weight = plan["quantities"]["estimated_weight_g"]
        if (
            plan["mailpiece"]["content"]["documents"] != expected_references
            or plan["traveller"]["template_revision"] != "development-v1"
            or franking["method"] != "stamps"
            or franking["provider"]
            != {
                "id": "bpost",
                "profile": {
                    "id": "development-stamps-v1",
                    "revision": "development-v1",
                },
            }
            or franking["supplies"] != SUPPLY_INSTRUCTIONS
            or franking["policy_revision"] != STAMP_POLICY_REVISION
            or pricing
            != {
                "tariff_revision": TARIFF_REVISION,
                "currency": "EUR",
                "lines": PRICE_LINES,
                "total_postage": "1.50",
            }
            or not estimated_weight
            or Decimal(estimated_weight) <= 0
        ):
            raise RuntimeError(f"Production plan is incorrect for flow {flow_id}")

        source_entries = letter.sources
        if len(source_entries) != 2:
            raise RuntimeError(f"Letter {operation_id} does not contain two sources")
        source_results = []
        expected_content_pages = []
        for ordinal, source in enumerate(source_entries, 1):
            if (
                source.ordinal != ordinal
                or source.path
                != f"letters/{letter.sequence:06d}/sources/{ordinal:03d}.pdf"
            ):
                raise RuntimeError(f"Letter source order is incorrect: {source!r}")
            source_path = package.path_for(source)
            extracted = source_path.read_bytes()
            archived = archived_content[(flow_id, ordinal)]
            if (
                extracted != archived
                or source.sha256 != hashlib.sha256(archived).hexdigest()
            ):
                raise RuntimeError(
                    f"Postal source {ordinal} differs from archive version for {flow_id}"
                )
            source_results.append(
                {
                    "ordinal": ordinal,
                    "path": str(source_path),
                    "sha256": source.sha256,
                    "archive_bytes_equal": True,
                }
            )
            expected_content_pages.extend(_pdf_page_streams(archived))

        output = renderer.render(package, letter, rendered_directory)
        rendered_outputs[letter.job.id] = output
        letter_path = output.path
        pages = _pdf_page_streams(letter_path.read_bytes())
        if len(pages) != len(expected_content_pages) + 2:
            raise RuntimeError(f"Letter PDF has an unexpected page count for {flow_id}")
        if pages[1].strip() or pages[2:] != expected_content_pages:
            raise RuntimeError(
                f"Letter PDF does not place a blank page before ordered content for {flow_id}"
            )
        traveller = pages[0]
        label_marker = b"/XartaLabel BMC"
        marker_offset = traveller.find(label_marker)
        barcode = str(row["task_id"]).encode()
        required_tags = (
            b"/XartaRetained BMC",
            b"/XartaBarcode BMC",
            b"/XartaSection1 BMC",
            b"/XartaSection2 BMC",
            b"/XartaSection3 BMC",
            b"/XartaSection4 BMC",
            b"/XartaCutLine BMC",
            b"/XartaLabel BMC",
        )
        if (
            marker_offset < 0
            or barcode not in traveller[:marker_offset]
            or barcode in traveller[marker_offset:]
            or b"postal:" in traveller
            or traveller[:marker_offset].count(b" re f") < 50
            or b" re f" in traveller[marker_offset:]
            or any(tag not in traveller for tag in required_tags)
            or b"[5 4] 0 d" not in traveller
            or traveller.count(b" c") < 16
        ):
            raise RuntimeError(f"Traveller barcode placement is unsafe for {flow_id}")

        evaluations[flow_id] = {
            "operation_id": operation_id,
            "task_id": str(row["task_id"]),
            "rendered_directory": str(rendered_directory),
            "required_files": [str(letter_path)],
            "sources": source_results,
            "production_instructions": {
                "provider": franking["provider"],
                "method": franking["method"],
                "supplies": franking["supplies"],
                "policy_revision": franking["policy_revision"],
                "pricing": pricing,
                "estimated_weight_g": estimated_weight,
            },
            "letter_pdf": {
                "path": str(letter_path),
                "sha256": output.sha256,
                "byte_count": output.byte_count,
                "pages": len(pages),
                "traveller_page": 1,
                "intentional_blank_page": 2,
                "content_first_page": 3,
                "internal_barcode": barcode.decode(),
                "code128_graphics_present": True,
                "retained_content_contains_barcode": True,
                "detachable_label_contains_barcode": False,
            },
        }
    if set(evaluations) != set(run_by_flow):
        raise RuntimeError("Package evaluations do not cover every flow")
    return evaluations, rendered_outputs


async def database_invariants(
    connection: asyncpg.Connection,
    flow_ids: list[uuid.UUID],
    task_ids: list[uuid.UUID],
    run_id: uuid.UUID,
) -> dict[str, Any]:
    operations = await connection.fetch(
        "SELECT execution.flow_id, execution.state::text execution_state, "
        "tracked.lifecycle::text lifecycle, local.service, local.state::text local_state, "
        "task.assignment_state::text assignment_state, task.print_state::text print_state, "
        "task.physical_state::text physical_state, run.state::text run_state, "
        "batch.state::text handover_batch_state "
        "FROM node_executions execution "
        "JOIN tracked_operations tracked ON tracked.node_execution_id = execution._id "
        "JOIN postal_local.operations local ON local.tracked_operation_id = tracked._id "
        "JOIN postal_local.production_tasks task ON task.operation_id = local._id "
        "JOIN postal_local.production_run_items run_item ON run_item.production_task_id = task._id "
        "JOIN postal_local.production_runs run ON run._id = run_item.production_run_id "
        "JOIN postal_local.handover_batch_items batch_item ON batch_item.production_task_id = task._id "
        "JOIN postal_local.handover_batches batch ON batch._id = batch_item.handover_batch_id "
        "WHERE execution.flow_id = ANY($1::uuid[]) ORDER BY execution.flow_id",
        flow_ids,
    )
    if len(operations) != len(flow_ids):
        raise RuntimeError(
            f"Expected {len(flow_ids)} tracked postal operations, found {len(operations)}"
        )
    for row in operations:
        if row["service"] != "ordinary" or (
            row["execution_state"],
            row["lifecycle"],
        ) != ("resolved", "resolved"):
            raise RuntimeError(f"Postal operation resolved incorrectly: {dict(row)!r}")
        if (
            row["local_state"] != "handed_over"
            or row["assignment_state"] != "completed"
            or row["print_state"] != "completed"
            or row["physical_state"] != "handed_over"
            or row["run_state"] != "completed"
            or row["handover_batch_state"] != "completed"
        ):
            raise RuntimeError(f"Postal local projection is incomplete: {dict(row)!r}")

    outcomes = await connection.fetch(
        "SELECT execution.flow_id, event.outcome "
        "FROM outcome_events event "
        "JOIN node_executions execution ON execution._id = event.node_execution_id "
        "JOIN tracked_operations tracked ON tracked._id = event.tracked_operation_id "
        "JOIN postal_local.operations local ON local.tracked_operation_id = tracked._id "
        "WHERE execution.flow_id = ANY($1::uuid[]) "
        "ORDER BY execution.flow_id, event.received_at, event._id",
        flow_ids,
    )
    outcomes_by_flow = {
        str(flow_id): [row["outcome"] for row in outcomes if row["flow_id"] == flow_id]
        for flow_id in flow_ids
    }
    if any(value != ["prepared", "handed_over"] for value in outcomes_by_flow.values()):
        raise RuntimeError(f"Postal outcomes are incorrect: {outcomes_by_flow!r}")
    duplicate_outcomes = await connection.fetchval(
        "SELECT count(*) FROM (SELECT event.node_execution_id, event.outcome "
        "FROM outcome_events event JOIN node_executions execution "
        "ON execution._id = event.node_execution_id "
        "WHERE execution.flow_id = ANY($1::uuid[]) "
        "GROUP BY event.node_execution_id, event.outcome HAVING count(*) > 1) duplicates",
        flow_ids,
    )
    event_counts = await connection.fetch(
        "SELECT task.id, count(event._id) event_count, count(DISTINCT event.event_type) mode_count "
        "FROM postal_local.production_tasks task "
        "LEFT JOIN postal_local.production_events event ON event.production_task_id = task._id "
        "WHERE task.id = ANY($1::uuid[]) GROUP BY task.id",
        task_ids,
    )
    if (
        len(event_counts) != len(flow_ids)
        or duplicate_outcomes
        or any(
            row["event_count"] != 3 or row["mode_count"] != 3 for row in event_counts
        )
    ):
        raise RuntimeError("Duplicate scans created duplicate local events or outcomes")
    print_counts = await connection.fetchrow(
        "SELECT count(DISTINCT job._id) jobs, count(DISTINCT attempt._id) attempts, "
        "count(*) FILTER (WHERE job.state = 'completed' AND attempt.state = 'completed') completed, "
        "bool_and(job.kind = 'letter') letter_jobs, "
        "bool_and(attempt.rendered_sha256 ~ '^[0-9a-f]{64}$' AND attempt.rendered_byte_count > 0) rendered_metadata, "
        "count(DISTINCT job.sequence) distinct_sequences "
        "FROM postal_local.production_runs run "
        "JOIN postal_local.production_run_items item ON item.production_run_id = run._id "
        "JOIN postal_local.print_jobs job ON job.production_run_item_id = item._id "
        "JOIN postal_local.print_attempts attempt ON attempt.print_job_id = job._id "
        "WHERE run.id = $1",
        run_id,
    )
    inbox_count = await connection.fetchval(
        "SELECT count(*) FROM provider_event_inbox inbox "
        "JOIN tracked_operations tracked ON tracked._id = inbox.tracked_operation_id "
        "JOIN node_executions execution ON execution._id = tracked.node_execution_id "
        "WHERE execution.flow_id = ANY($1::uuid[])",
        flow_ids,
    )
    carrier_observations = await connection.fetchval(
        "SELECT count(*) FROM postal_local.carrier_observations observation "
        "JOIN postal_local.production_tasks task ON task._id = observation.production_task_id "
        "WHERE task.id = ANY($1::uuid[])",
        task_ids,
    )
    server_rendered_artifacts = await connection.fetchval(
        "SELECT count(*) FROM postal_local.artifacts artifact "
        "JOIN postal_local.operations operation ON operation._id = artifact.operation_id "
        "JOIN tracked_operations tracked ON tracked._id = operation.tracked_operation_id "
        "JOIN node_executions execution ON execution._id = tracked.node_execution_id "
        "WHERE execution.flow_id = ANY($1::uuid[]) "
        "AND (artifact.kind = 'letter' OR artifact.kind LIKE 'production-package:%')",
        flow_ids,
    )
    expected_print_jobs = len(flow_ids)
    if dict(print_counts or {}) != {
        "jobs": expected_print_jobs,
        "attempts": expected_print_jobs,
        "completed": expected_print_jobs,
        "letter_jobs": True,
        "rendered_metadata": True,
        "distinct_sequences": expected_print_jobs,
    }:
        raise RuntimeError(
            f"Print persistence is incorrect: {dict(print_counts or {})!r}"
        )
    if (
        inbox_count != len(flow_ids) * 3
        or carrier_observations != 0
        or server_rendered_artifacts != 0
    ):
        raise RuntimeError("Generic event inbox or disabled carrier state is incorrect")
    return {
        "operations": [
            {
                "flow_id": str(row["flow_id"]),
                "service": row["service"],
                "execution_state": row["execution_state"],
                "lifecycle": row["lifecycle"],
                "local_state": row["local_state"],
                "assignment_state": row["assignment_state"],
                "print_state": row["print_state"],
                "physical_state": row["physical_state"],
                "run_state": row["run_state"],
                "handover_batch_state": row["handover_batch_state"],
            }
            for row in operations
        ],
        "outcomes_by_flow": outcomes_by_flow,
        "production_events": sum(row["event_count"] for row in event_counts),
        "duplicate_outcomes": duplicate_outcomes,
        "print_jobs": dict(print_counts or {}),
        "server_letter_or_package_artifacts": server_rendered_artifacts,
        "provider_event_inbox": inbox_count,
        "carrier_observations": carrier_observations,
    }


def application_errors(log_path: Path) -> list[str]:
    errors = []
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


async def main(
    output_path: Path, application_log: Path, physical_printer: str | None = None
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    package_path = output_path.parent / "production-package.zip"
    extracted_directory = output_path.parent / "extracted-package"
    archive_download_directory = output_path.parent / "archive-downloads"
    station_cache_directory = output_path.parent / "station-cache"
    run_count = 1 if physical_printer else RUN_COUNT
    definitions = [postal_flow() for _ in range(run_count)]
    runs = [{"definition": definition, "ids": ids} for definition, ids in definitions]
    all_ids = [value for run in runs for value in run["ids"].values()]
    if len(all_ids) != len(set(all_ids)):
        raise RuntimeError("Scenario flow instances do not use distinct UUIDs")
    flow_ids = [uuid.UUID(run["ids"]["flow"]) for run in runs]
    connection = await connect_postgres()
    archive_connection = await connect_archive_postgres()
    try:
        await ensure_document_type(connection)
        async with standalone_application(
            application_log,
            {"archive", "generate", "postal"},
            {"SCENARIO_DECODE_POSTGRES_JSON": "true"},
        ):
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=35)
            ) as session:
                await submit_flows(session, runs)
                await wait_for_admission(connection, flow_ids)
                claim, package, package_bytes, receipt = await claim_and_verify_package(
                    session,
                    extracted_directory,
                    package_path,
                    f"postal-local-scenario-claim:{runs[0]['ids']['flow']}",
                    len(runs),
                )
                archive_results, archived_content = await download_archived_sources(
                    session,
                    archive_connection,
                    runs,
                    archive_download_directory,
                )
                printer = (
                    CupsPrinter(physical_printer) if physical_printer else FakePrinter()
                )
                printer = await print_sequentially(
                    session,
                    claim["id"],
                    package_bytes,
                    receipt,
                    station_cache_directory,
                    printer,
                )
                package_evaluations, _rendered_outputs = await evaluate_package(
                    connection,
                    package,
                    runs,
                    archived_content,
                    station_cache_directory / claim["id"] / "rendered",
                )
                batch_id, duplicate_scans = await complete_physical_workflow(
                    session, claim["task_ids"]
                )
                invariants = await database_invariants(
                    connection,
                    flow_ids,
                    [uuid.UUID(value) for value in claim["task_ids"]],
                    uuid.UUID(claim["id"]),
                )
    finally:
        await archive_connection.close()
        await connection.close()

    errors = application_errors(application_log)
    if errors:
        raise RuntimeError(
            f"Application log contains errors; inspect {application_log}"
        )
    report = {
        "functional": {
            "flows": [
                {
                    "flow_id": run["ids"]["flow"],
                    "generated_ids": [
                        run["ids"]["generated_1"],
                        run["ids"]["generated_2"],
                    ],
                    "archive_versions": archive_results[run["ids"]["flow"]],
                }
                for run in runs
            ],
            "run_id": claim["id"],
            "claimed_tasks": claim["task_ids"],
            "package_bytes": len(package_bytes),
            "package_path": str(package_path),
            "extracted_package_path": str(extracted_directory),
            "package_manifest_path": str(extracted_directory / "manifest.json"),
            "package_checksums_path": str(extracted_directory / "checksums.sha256"),
            "package_jobs_verified": len(package.manifest.jobs),
            "printer": physical_printer or "fake",
            "printer_submissions": (
                len(printer.submissions) if isinstance(printer, FakePrinter) else 1
            ),
            "scans_after_cups_acceptance_are_simulated": bool(physical_printer),
            "handover_batch_id": batch_id,
            "duplicate_scans_verified": duplicate_scans,
        },
        "package_evaluations_by_flow": package_evaluations,
        "database": invariants,
        "application_errors": errors,
    }
    output_path.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    print(f"report={output_path}")
    print(f"application_log={application_log}")


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--application-log", type=Path, default=DEFAULT_LOG)
    parser.add_argument("--physical-printer")
    arguments = parser.parse_args()
    if arguments.physical_printer and os.environ.get("CONFIRM_PHYSICAL_PRINT") != "yes":
        parser.error("--physical-printer requires CONFIRM_PHYSICAL_PRINT=yes")
    if arguments.physical_printer and arguments.output == DEFAULT_OUTPUT:
        directory = SCENARIO_ARTIFACTS / "postal-local-printer"
        arguments.output = directory / "results.json"
        if arguments.application_log == DEFAULT_LOG:
            arguments.application_log = directory / "application.log"
    return arguments


if __name__ == "__main__":
    arguments = parse_arguments()
    asyncio.run(
        main(arguments.output, arguments.application_log, arguments.physical_printer)
    )
