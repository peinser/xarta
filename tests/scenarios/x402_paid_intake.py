"""Run 50 stateless x402 settlements through synchronous archive execution."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import ssl
import time
import uuid

from pathlib import Path
from typing import Any

import aiohttp
import asyncpg

from x402.http import decode_payment_required_header
from x402.http import decode_payment_response_header
from x402.http import encode_payment_signature_header

from tests.scenarios.common import BASE_URL
from tests.scenarios.common import ROOT
from tests.scenarios.common import SCENARIO_ARTIFACTS
from tests.scenarios.common import connect_archive_postgres
from tests.scenarios.common import connect_postgres
from tests.scenarios.common import standalone_application
from tests.scenarios.common import statistics_for
from tests.scenarios.x402_facilitator import FACILITATOR_URL
from tests.scenarios.x402_facilitator import NETWORK
from tests.scenarios.x402_facilitator import PAY_TO
from tests.scenarios.x402_facilitator import local_facilitator
from tests.scenarios.x402_facilitator import payload

RESOURCE_URL = "https://api.example.com/api/v1/intake/"
DEFAULT_OUTPUT = SCENARIO_ARTIFACTS / "x402-paid-intake/results.json"
DEFAULT_APPLICATION_LOG = SCENARIO_ARTIFACTS / "x402-paid-intake/application.log"
DEFAULT_FACILITATOR_LOG = SCENARIO_ARTIFACTS / "x402-paid-intake/facilitator.log"


def identities() -> dict[str, str]:
    flow_id = uuid.uuid4()
    document_id = uuid.uuid5(flow_id, "paid-intake-document")
    return {
        "flow": str(flow_id),
        "document": str(document_id),
        "version": str(uuid.uuid5(flow_id, "paid-intake-version")),
        "archive_node": str(uuid.uuid5(flow_id, "paid-intake-archive")),
    }


def source_bytes(index: int) -> bytes:
    return f"xarta paid intake scenario\nsequence={index:04d}\n".encode()


def flow(ids: dict[str, str], index: int) -> dict[str, Any]:
    return {
        "id": ids["flow"],
        "correlation_id": ids["flow"],
        "files": {
            ids["document"]: {
                "metadata": {"scenario": "x402-paid-intake", "sequence": index}
            }
        },
        "dag": {
            "id": ids["archive_node"],
            "kind": "archive",
            "destination": "default-archive",
            "documents": [
                {
                    "archive": "default",
                    "document_id": ids["document"],
                    "version_id": ids["version"],
                    "document_type": None,
                    "metadata": {"scenario": "x402-paid-intake", "sequence": index},
                    "representations": [
                        {"source": {"source": "generate", "id": ids["document"]}}
                    ],
                }
            ],
        },
    }


def form(
    definition: dict[str, Any], ids: dict[str, str], data: bytes
) -> aiohttp.FormData:
    value = aiohttp.FormData()
    value.add_field(
        "flow",
        json.dumps(definition).encode(),
        filename="flow.json",
        content_type="application/json",
    )
    value.add_field(
        ids["document"],
        data,
        filename=f"paid-intake-{ids['document']}.txt",
        content_type="text/plain",
    )
    return value


async def wait_for_version(
    archive_connection: asyncpg.Connection, version_id: uuid.UUID
) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if await archive_connection.fetchval(
            "SELECT EXISTS(SELECT 1 FROM archive_document_versions WHERE version_id = $1 AND state = 'available')",
            version_id,
        ):
            return
        await asyncio.sleep(0.01)
    raise TimeoutError(f"Archive version {version_id} did not become available")


async def validate_archive(
    session: aiohttp.ClientSession,
    archive_connection: asyncpg.Connection,
    ids: dict[str, str],
    data: bytes,
    index: int,
) -> None:
    endpoint = f"{BASE_URL}/api/v1/archive/documents/{ids['document']}/versions/{ids['version']}"
    async with session.get(endpoint) as response:
        body = await response.read()
        if response.status != 200 or body != data:
            raise RuntimeError(
                f"Paid archive returned HTTP {response.status} or incorrect bytes"
            )
        if response.content_type != "text/plain":
            raise RuntimeError("Paid archive returned an incorrect content type")
    row = await archive_connection.fetchrow(
        "SELECT version.metadata, representation.size, "
        "encode(representation.checksum, 'hex') AS checksum "
        "FROM archive_documents document JOIN archive_document_versions version "
        "ON version.aggregate_id = document._id "
        "JOIN archive_document_representations representation "
        "ON representation._id = version.default_representation_id "
        "WHERE document.document_id = $1 AND version.version_id = $2",
        uuid.UUID(ids["document"]),
        uuid.UUID(ids["version"]),
    )
    actual = dict(row or {})
    if isinstance(actual.get("metadata"), str):
        actual["metadata"] = json.loads(actual["metadata"])
    expected = {
        "metadata": {"scenario": "x402-paid-intake", "sequence": index},
        "size": len(data),
        "checksum": hashlib.sha512(data).hexdigest(),
    }
    if actual != expected:
        raise RuntimeError(f"Paid archive metadata is incorrect: {actual!r}")


async def tracking_invariants(
    connection: asyncpg.Connection, runs: list[dict[str, Any]]
) -> dict[str, Any]:
    flow_ids = [uuid.UUID(run["ids"]["flow"]) for run in runs]
    tracked = {
        "node_executions": await connection.fetchval(
            "SELECT count(*) FROM node_executions WHERE flow_id = ANY($1::uuid[])",
            flow_ids,
        ),
        "outcome_events": await connection.fetchval(
            "SELECT count(*) FROM outcome_events WHERE flow_id = ANY($1::uuid[])",
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
    }
    if any(tracked.values()):
        raise RuntimeError(f"Synchronous paid intake created tracked state: {tracked}")
    obsolete = await connection.fetchval(
        "SELECT count(*) FROM unnest(ARRAY["
        "'payment_price_quotes', 'payment_requirements', 'payment_purchases', "
        "'paid_intake_admissions', 'paid_preview_results', 'payment_outbox'"
        "]) name WHERE to_regclass('public.' || name) IS NOT NULL"
    )
    if obsolete:
        raise RuntimeError("Obsolete x402 payment tables still exist")
    return {"tracked_state": tracked, "payment_tables": 0}


async def facilitator_invariants(
    certificate_path: Path, payment_ids: list[str]
) -> dict[str, Any]:
    context = ssl.create_default_context(cafile=str(certificate_path))
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{FACILITATOR_URL}/statistics", ssl=context
        ) as response:
            statistics = await response.json()
    if statistics["verified"]:
        raise RuntimeError("Upfront intake unexpectedly called facilitator verify")
    if statistics["settled"] != sorted(payment_ids):
        raise RuntimeError("Facilitator settlements do not match paid intake")
    if [event["operation"] for event in statistics["events"]] != ["settle"] * len(
        payment_ids
    ):
        raise RuntimeError("Paid intake facilitator choreography is incorrect")
    return {"verify_calls": 0, "settle_calls": len(statistics["settled"])}


async def execute(
    connection: asyncpg.Connection,
    archive_connection: asyncpg.Connection,
    count: int,
    certificate_path: Path,
) -> dict[str, Any]:
    runs: list[dict[str, Any]] = []
    payment_ids: list[str] = []
    timeout = aiohttp.ClientTimeout(total=35)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for index in range(1, count + 1):
            ids = identities()
            definition = flow(ids, index)
            data = source_bytes(index)
            started = time.perf_counter_ns()
            challenge_started = time.perf_counter_ns()
            async with session.post(
                f"{BASE_URL}/api/v1/intake/", data=form(definition, ids, data)
            ) as response:
                body = await response.read()
                if response.status != 402:
                    raise RuntimeError(
                        f"Unpaid intake returned HTTP {response.status}: {body!r}"
                    )
                encoded_required = response.headers.get("PAYMENT-REQUIRED")
            challenge_ms = (time.perf_counter_ns() - challenge_started) / 1_000_000
            if encoded_required is None:
                raise RuntimeError("Unpaid intake omitted PAYMENT-REQUIRED")
            payment_required = decode_payment_required_header(encoded_required)
            if len(payment_required.accepts) != 1:
                raise RuntimeError("Paid intake did not issue one payment requirement")
            requirements = payment_required.accepts[0]
            if (
                requirements.amount != "1250"
                or requirements.extra.get("paymentFlow") != "upfront"
                or not requirements.extra.get("xartaChallenge")
            ):
                raise RuntimeError(
                    "Paid intake challenge does not match pricing policy"
                )

            payment_id = f"pay_intake_{index:04d}_{hashlib.sha256(ids['flow'].encode()).hexdigest()[:16]}"
            payment_payload = payload(requirements, payment_id)
            paid_started = time.perf_counter_ns()
            async with session.post(
                f"{BASE_URL}/api/v1/intake/",
                data=form(definition, ids, data),
                headers={
                    "PAYMENT-SIGNATURE": encode_payment_signature_header(
                        payment_payload
                    )
                },
            ) as response:
                body = await response.read()
                if response.status != 200:
                    raise RuntimeError(
                        f"Paid intake returned HTTP {response.status}: {body!r}"
                    )
                encoded_settlement = response.headers.get("PAYMENT-RESPONSE")
            paid_ms = (time.perf_counter_ns() - paid_started) / 1_000_000
            response_body = json.loads(body)
            if response_body.get("id") != ids["flow"] or encoded_settlement is None:
                raise RuntimeError("Paid intake response is incomplete")
            settlement = decode_payment_response_header(encoded_settlement)
            if not settlement.success or settlement.amount != "1250":
                raise RuntimeError("Paid intake settlement response is invalid")
            expected_transaction = (
                "0x" + hashlib.sha256(payment_id.encode()).hexdigest()
            )
            if (
                settlement.transaction != expected_transaction
                or response_body.get("payment", {}).get("transaction")
                != expected_transaction
            ):
                raise RuntimeError("Paid intake omitted transaction evidence")

            await wait_for_version(archive_connection, uuid.UUID(ids["version"]))
            processing_ms = (time.perf_counter_ns() - paid_started) / 1_000_000
            await validate_archive(session, archive_connection, ids, data, index)
            total_ms = (time.perf_counter_ns() - started) / 1_000_000
            runs.append(
                {
                    "index": index,
                    "ids": ids,
                    "challenge_ms": challenge_ms,
                    "paid_acceptance_ms": paid_ms,
                    "processing_ms": processing_ms,
                    "total_ms": total_ms,
                    "payment_id": payment_id,
                    "transaction": expected_transaction,
                    "amount": requirements.amount,
                }
            )
            payment_ids.append(payment_id)
            if index % 10 == 0 or index == count:
                print(
                    f"completed={index} paid_ms={paid_ms:.3f} processing_ms={processing_ms:.3f}",
                    flush=True,
                )

    tracking = await tracking_invariants(connection, runs)
    facilitator = await facilitator_invariants(certificate_path, payment_ids)
    return {
        "functional": {
            "flows_completed": len(runs),
            "archives_validated": len(runs),
            "pricing_quotes_validated": len(runs),
            "payments_settled": len(payment_ids),
            "payment_ledger_present": False,
            "tracking": tracking,
            "facilitator": facilitator,
        },
        "performance": {
            "challenge": statistics_for([run["challenge_ms"] for run in runs]),
            "paid_acceptance": statistics_for(
                [run["paid_acceptance_ms"] for run in runs]
            ),
            "paid_acceptance_excluding_first": statistics_for(
                [run["paid_acceptance_ms"] for run in runs[1:]]
                or [runs[0]["paid_acceptance_ms"]]
            ),
            "processing": statistics_for([run["processing_ms"] for run in runs]),
            "processing_excluding_first": statistics_for(
                [run["processing_ms"] for run in runs[1:]] or [runs[0]["processing_ms"]]
            ),
            "end_to_end": statistics_for([run["total_ms"] for run in runs]),
        },
        "runs": runs,
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


def settlement_log_invariants(
    log_path: Path, runs: list[dict[str, Any]]
) -> dict[str, Any]:
    events = []
    for line in log_path.read_text().splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("event") == "x402_settlement_succeeded":
            events.append(event)
    if len(events) != len(runs):
        raise RuntimeError(
            f"Expected {len(runs)} settlement log events, found {len(events)}"
        )
    by_flow = {event["flow_id"]: event for event in events}
    for run in runs:
        event = by_flow.get(run["ids"]["flow"])
        if event is None or {
            "transaction": event.get("transaction"),
            "network": event.get("network"),
            "amount": event.get("amount"),
            "pay_to": event.get("pay_to"),
        } != {
            "transaction": run["transaction"],
            "network": NETWORK,
            "amount": run["amount"],
            "pay_to": PAY_TO,
        }:
            raise RuntimeError("Structured settlement log evidence is incorrect")
    return {"events": len(events), "transactions_are_synthetic": True}


async def main(
    count: int, output_path: Path, application_log: Path, facilitator_log: Path
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    connection = await connect_postgres()
    archive_connection = await connect_archive_postgres()
    try:
        async with local_facilitator(facilitator_log) as certificate_path:
            environment = {
                "SSL_CERT_FILE": str(certificate_path),
                "X402_ENABLED": "true",
                "X402_INTAKE_ENABLED": "true",
                "X402_PREVIEW_ENABLED": "false",
                "X402_FACILITATOR_URL": FACILITATOR_URL,
                "X402_NETWORK": NETWORK,
                "X402_PAY_TO": PAY_TO,
                "X402_PAYMENT_TIMEOUT_SECONDS": "60",
                "X402_INTAKE_RESOURCE_URL": RESOURCE_URL,
                "X402_CHALLENGE_SIGNING_KEY": "local-scenario-challenge-signing-key",
                "PRICING_CONFIG_PATH": str(ROOT / ".dev/conf/pricing.json"),
            }
            async with standalone_application(
                application_log, {"archive"}, environment
            ):
                report = await execute(
                    connection, archive_connection, count, certificate_path
                )
    finally:
        await archive_connection.close()
        await connection.close()

    errors = application_errors(application_log)
    report["functional"]["settlement_logs"] = settlement_log_invariants(
        application_log, report["runs"]
    )
    report["performance"]["application_errors"] = errors
    if errors:
        raise RuntimeError("Application log contains errors")
    output_path.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({**report["functional"], **report["performance"]}, indent=2))
    print(f"report={output_path}")
    print(f"application_log={application_log}")
    print(f"facilitator_log={facilitator_log}")


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=50)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--application-log", type=Path, default=DEFAULT_APPLICATION_LOG)
    parser.add_argument("--facilitator-log", type=Path, default=DEFAULT_FACILITATOR_LOG)
    arguments = parser.parse_args()
    if arguments.count < 1:
        parser.error("--count must be at least 1")
    return arguments


if __name__ == "__main__":
    arguments = parse_arguments()
    asyncio.run(
        main(
            arguments.count,
            arguments.output,
            arguments.application_log,
            arguments.facilitator_log,
        )
    )
