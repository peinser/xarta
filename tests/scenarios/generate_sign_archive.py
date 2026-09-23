"""Generate, sign, archive, and validate a Jinja selftest PDF."""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import time
import uuid

from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp
import asyncpg

from asn1crypto import pem
from asn1crypto import x509
from pyhanko.pdf_utils.reader import PdfFileReader
from pyhanko.sign.fields import SigSeedSubFilter
from pyhanko.sign.validation import async_validate_pdf_signature
from pyhanko_certvalidator import ValidationContext

from tests.fixtures.jinja_selftest import SELFTEST_TEMPLATE_ENGINE
from tests.fixtures.jinja_selftest import jinja_selftest_template_engine_options
from tests.scenarios.common import ARCHIVE_BASE_URL
from tests.scenarios.common import INTAKE_BASE_URL
from tests.scenarios.common import ROOT
from tests.scenarios.common import SCENARIO_ARTIFACTS
from tests.scenarios.common import assert_split_trace
from tests.scenarios.common import compose_application
from tests.scenarios.common import connect_archive_postgres
from tests.scenarios.common import connect_postgres
from tests.scenarios.common import elapsed
from tests.scenarios.common import identifier
from tests.scenarios.common import standalone_application
from tests.scenarios.common import statistics_for
from tests.scenarios.common import timestamp
from tests.scenarios.rfc3161_tsa import local_tsa

DOCUMENT_TYPE = "scenario-jinja-selftest"


@dataclass(frozen=True)
class SignatureScenarioPolicy:
    id: str
    profile: str
    subfilter: SigSeedSubFilter
    digest_algorithm: str | None
    timestamp_expected: bool = False

    @property
    def artifact_directory(self) -> Path:
        return SCENARIO_ARTIFACTS / f"signature/{self.id}"


ELECTRONIC_SEAL_POLICY = SignatureScenarioPolicy(
    id="xarta-dev-seal-v1",
    profile="legacy-pdf-cms",
    subfilter=SigSeedSubFilter.ADOBE_PKCS7_DETACHED,
    digest_algorithm=None,
)
PADES_B_B_POLICY = SignatureScenarioPolicy(
    id="pades-b-b-v1",
    profile="pades-b-b",
    subfilter=SigSeedSubFilter.PADES,
    digest_algorithm="sha256",
)
PADES_B_T_POLICY = SignatureScenarioPolicy(
    id="pades-b-t-v1",
    profile="pades-b-t",
    subfilter=SigSeedSubFilter.PADES,
    digest_algorithm="sha256",
    timestamp_expected=True,
)


async def ensure_document_type(connection: asyncpg.Connection) -> None:
    await connection.execute(
        "INSERT INTO document_types "
        "(identifier, default_template_engine, default_content_type) "
        "VALUES ($1, $2, $3) ON CONFLICT (identifier) DO NOTHING",
        DOCUMENT_TYPE,
        SELFTEST_TEMPLATE_ENGINE,
        "application/pdf",
    )


def flow(policy: SignatureScenarioPolicy) -> tuple[dict[str, Any], dict[str, str]]:
    ids = {
        "flow": identifier(),
        "generated": identifier(),
        "signed": identifier(),
        "version": identifier(),
        "generate_node": identifier(),
        "signature_node": identifier(),
        "archive_node": identifier(),
    }
    definition = {
        "id": ids["flow"],
        "correlation_id": ids["flow"],
        "dag": {
            "id": ids["generate_node"],
            "kind": "generate",
            "documents": [
                {
                    "source": "render",
                    "id": ids["generated"],
                    "document_type": DOCUMENT_TYPE,
                    "content_type": "application/pdf",
                    "template_engine": SELFTEST_TEMPLATE_ENGINE,
                    "template_engines_options": jinja_selftest_template_engine_options(),
                    "payload": {"content_type": "application/json", "data": {}},
                }
            ],
            "on": {
                "success": [
                    {
                        "id": ids["signature_node"],
                        "kind": "signature",
                        "policy": policy.id,
                        "documents": [{"in": ids["generated"], "out": ids["signed"]}],
                        "on": {
                            "success": [
                                {
                                    "id": ids["archive_node"],
                                    "kind": "archive",
                                    "destination": "default-archive",
                                    "documents": [
                                        {
                                            "archive": "default",
                                            "document_id": ids["signed"],
                                            "version_id": ids["version"],
                                            "document_type": DOCUMENT_TYPE,
                                            "metadata": {},
                                            "representations": [
                                                {
                                                    "source": {
                                                        "source": "generate",
                                                        "id": ids["signed"],
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
        await asyncio.sleep(0.01)
    raise TimeoutError(f"Signed PDF version {version_id} did not become available")


async def validate_pdf_signature(
    data: bytes, policy: SignatureScenarioPolicy
) -> dict[str, Any]:
    reader = PdfFileReader(io.BytesIO(data))
    if len(reader.embedded_signatures) != 1:
        raise RuntimeError(
            f"Expected one embedded PDF signature, found {len(reader.embedded_signatures)}"
        )
    certificate_directory = ROOT / ".dev/runtime/certificates"
    certificate_path = certificate_directory / "public.pem"
    _, _, certificate_der = pem.unarmor(certificate_path.read_bytes())
    expected_certificate = x509.Certificate.load(certificate_der)
    _, _, root_der = pem.unarmor((certificate_directory / "root.pem").read_bytes())
    root_certificate = x509.Certificate.load(root_der)
    _, _, intermediate_der = pem.unarmor(
        (certificate_directory / "chain.pem").read_bytes()
    )
    intermediate_certificate = x509.Certificate.load(intermediate_der)
    _, _, tsa_der = pem.unarmor((certificate_directory / "tsa-public.pem").read_bytes())
    tsa_certificate = x509.Certificate.load(tsa_der)
    embedded_signature = reader.embedded_signatures[0]
    if embedded_signature.sig_object["/SubFilter"] != policy.subfilter.value:
        raise RuntimeError(f"PDF signature does not use the {policy.profile} subfilter")
    digest_algorithm = embedded_signature.signer_info["digest_algorithm"][
        "algorithm"
    ].native
    if (
        policy.digest_algorithm is not None
        and digest_algorithm != policy.digest_algorithm
    ):
        raise RuntimeError(
            f"PDF signature uses digest {digest_algorithm!r}, expected {policy.digest_algorithm!r}"
        )
    signed_attribute_types = {
        attribute["type"].native
        for attribute in embedded_signature.signer_info["signed_attrs"]
    }
    if (
        policy.subfilter is SigSeedSubFilter.PADES
        and "signing_certificate_v2" not in signed_attribute_types
    ):
        raise RuntimeError("PAdES signature is missing signing_certificate_v2")
    unsigned_attributes = embedded_signature.signer_info["unsigned_attrs"]
    unsigned_attribute_types = (
        {attribute["type"].native for attribute in unsigned_attributes}
        if unsigned_attributes.native is not None
        else set()
    )
    timestamp_present = "signature_time_stamp_token" in unsigned_attribute_types
    if timestamp_present != policy.timestamp_expected:
        raise RuntimeError(
            f"Policy {policy.id} timestamp presence is {timestamp_present}, expected {policy.timestamp_expected}"
        )
    if "/DSS" in reader.root:
        raise RuntimeError(f"Policy {policy.id} unexpectedly embedded a PDF DSS")
    status = await async_validate_pdf_signature(
        embedded_signature,
        signer_validation_context=ValidationContext(
            trust_roots=[root_certificate], allow_fetching=False
        ),
        ts_validation_context=ValidationContext(
            trust_roots=[root_certificate], allow_fetching=False
        ),
    )
    if not status.intact or not status.valid or not status.trusted:
        raise RuntimeError(f"PDF signature validation failed: {status.summary()}")
    if status.signing_cert.dump() != expected_certificate.dump():
        raise RuntimeError("PDF was not signed by the configured document certificate")
    embedded_certificates = {
        certificate.dump() for certificate in embedded_signature.other_embedded_certs
    }
    if intermediate_certificate.dump() not in embedded_certificates:
        raise RuntimeError("PDF signature does not embed the intermediate certificate")
    if root_certificate.dump() in embedded_certificates:
        raise RuntimeError("PDF signature unexpectedly embeds the trust root")
    timestamp_status = status.timestamp_validity
    if policy.timestamp_expected:
        if timestamp_status is None:
            raise RuntimeError("PAdES B-T signature has no validated timestamp")
        if (
            not timestamp_status.intact
            or not timestamp_status.valid
            or not timestamp_status.trusted
        ):
            raise RuntimeError(
                f"RFC 3161 timestamp validation failed: {timestamp_status.summary()}"
            )
        if timestamp_status.signing_cert.dump() != tsa_certificate.dump():
            raise RuntimeError("RFC 3161 timestamp used an unexpected TSA certificate")
    elif timestamp_status is not None:
        raise RuntimeError(f"Policy {policy.id} unexpectedly validated a timestamp")

    return {
        "field_name": embedded_signature.field_name,
        "policy": policy.id,
        "profile": policy.profile,
        "subfilter": policy.subfilter.value,
        "digest_algorithm": digest_algorithm,
        "timestamp_present": timestamp_present,
        "timestamp_valid": timestamp_status.valid if timestamp_status else None,
        "timestamp_trusted": timestamp_status.trusted if timestamp_status else None,
        "dss_present": False,
        "intermediate_embedded": True,
        "root_embedded": False,
        "intact": status.intact,
        "valid": status.valid,
        "trusted": status.trusted,
        "signer_matches_configuration": True,
    }


async def validate_output(
    session: aiohttp.ClientSession,
    archive_connection: asyncpg.Connection,
    ids: dict[str, str],
    policy: SignatureScenarioPolicy,
) -> tuple[dict[str, Any], dict[str, float]]:
    endpoint = f"{ARCHIVE_BASE_URL}/api/v1/archive/documents/{ids['signed']}/versions/{ids['version']}"
    started = time.perf_counter_ns()
    async with session.get(endpoint) as response:
        data = await response.read()
        if response.status != 200:
            raise RuntimeError(
                f"Signed PDF download returned HTTP {response.status}: {data!r}"
            )
        if response.content_type != "application/pdf":
            raise RuntimeError(
                f"Signed PDF download returned {response.content_type!r}"
            )
    download_ms = (time.perf_counter_ns() - started) / 1_000_000

    if not data.startswith(b"%PDF-"):
        raise RuntimeError("Archived output is not a PDF")
    started = time.perf_counter_ns()
    signature = await validate_pdf_signature(data, policy)
    validation_ms = (time.perf_counter_ns() - started) / 1_000_000

    started = time.perf_counter_ns()
    metadata = await archive_connection.fetchrow(
        "SELECT representation.content_type, version.state::text AS state "
        "FROM archive_document_versions version "
        "JOIN archive_document_representations representation "
        "ON representation.version_internal_id = version._id "
        "AND representation.representation_id = version.default_representation_id "
        "WHERE version.version_id = $1",
        uuid.UUID(ids["version"]),
    )
    metadata_ms = (time.perf_counter_ns() - started) / 1_000_000
    if dict(metadata or {}) != {
        "content_type": "application/pdf",
        "state": "available",
    }:
        raise RuntimeError(f"Archived PDF metadata is incorrect: {metadata!r}")

    return signature, {
        "download_ms": download_ms,
        "validation_ms": validation_ms,
        "metadata_ms": metadata_ms,
    }


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
        starts = {event["kind"]: event for event in flow_events if event.get("start")}
        completes = {
            event["kind"]: event for event in flow_events if event.get("complete")
        }
        if starts.keys() != {"generate", "signature", "archive"} or starts.keys() != (
            completes.keys()
        ):
            raise RuntimeError(
                f"Application log does not contain every stage for flow {run['ids']['flow']}"
            )
        stages.append(
            {
                "processing_ms": elapsed(starts["generate"], completes["archive"]),
                "generate_ms": elapsed(starts["generate"], completes["generate"]),
                "signature_ms": elapsed(starts["signature"], completes["signature"]),
                "archive_ms": elapsed(starts["archive"], completes["archive"]),
            }
        )

    return {
        "errors": errors,
        "application_stages": {
            name: statistics_for([stage[name] for stage in stages])
            for name in stages[0]
        },
    }


async def execute(
    archive_connection: asyncpg.Connection,
    count: int,
    log_path: Path,
    policy: SignatureScenarioPolicy,
) -> dict[str, Any]:
    runs: list[dict[str, Any]] = []
    signatures: list[dict[str, Any]] = []
    timeout = aiohttp.ClientTimeout(total=35)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for index in range(count):
            definition, ids = flow(policy)
            started = time.perf_counter_ns()
            async with session.post(
                f"{INTAKE_BASE_URL}/api/v1/intake/", json=definition
            ) as response:
                body = await response.read()
                if response.status != 202:
                    raise RuntimeError(
                        f"Flow {index + 1} returned HTTP {response.status}: {body!r}"
                    )
            await wait_for_version(archive_connection, ids["version"])
            processing_ms = (time.perf_counter_ns() - started) / 1_000_000
            signature, validation_timings = await validate_output(
                session, archive_connection, ids, policy
            )
            total_ms = (time.perf_counter_ns() - started) / 1_000_000
            signatures.append(signature)
            runs.append(
                {
                    "index": index + 1,
                    "processing_ms": processing_ms,
                    "total_ms": total_ms,
                    **validation_timings,
                    "ids": ids,
                }
            )
            if (index + 1) % 10 == 0 or index + 1 == count:
                print(
                    f"completed={index + 1} processing_ms={processing_ms:.3f} total_ms={total_ms:.3f}",
                    flush=True,
                )

    processing_durations = [run["processing_ms"] for run in runs]

    return {
        "functional": {
            "flows_completed": len(runs),
            "outputs_downloaded": len(runs),
            "signatures_validated": len(signatures),
            "pdf_signature": signatures[0],
        },
        "performance": {
            "processing": statistics_for(processing_durations),
            "first_processing_ms": processing_durations[0],
            "processing_excluding_first": statistics_for(
                processing_durations[1:] or processing_durations
            ),
            "end_to_end": statistics_for([run["total_ms"] for run in runs]),
            "download": statistics_for([run["download_ms"] for run in runs]),
            "signature_validation": statistics_for(
                [run["validation_ms"] for run in runs]
            ),
            "metadata": statistics_for([run["metadata_ms"] for run in runs]),
        },
        "runs": runs,
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


async def main(
    policy: SignatureScenarioPolicy,
    count: int,
    output_path: Path,
    log_path: Path,
    compose_path: Path | None,
    validate_trace: bool,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    connection = await connect_postgres()
    archive_connection = await connect_archive_postgres()
    try:
        await ensure_document_type(connection)
        async with _timestamp_service(policy):
            application = (
                compose_application(
                    log_path,
                    compose_path,
                    ("intake", "archive", "generation", "signature"),
                )
                if compose_path
                else standalone_application(
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
                )
            )
            async with application:
                report = await execute(archive_connection, count, log_path, policy)
                if validate_trace:
                    report["deployment"] = {
                        "trace_id": await assert_split_trace(
                            report["runs"][0]["ids"]["flow"],
                            {
                                "xarta-intake",
                                "xarta-generation",
                                "xarta-signature",
                                "xarta-archive",
                            },
                        )
                    }
    finally:
        await archive_connection.close()
        await connection.close()

    report["performance"].update(stage_statistics(log_path, report["runs"]))
    report["performance"]["errors"] = application_errors(log_path)
    if report["performance"]["errors"]:
        raise RuntimeError("Application log contains errors")
    output_path.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({**report["functional"], **report["performance"]}, indent=2))
    print(f"report={output_path}")
    print(f"application_log={log_path}")


def parse_arguments(policy: SignatureScenarioPolicy) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=f"Generate, sign with {policy.id}, archive, and validate a PDF."
    )
    parser.add_argument("--count", type=int, default=50)
    parser.add_argument(
        "--output", type=Path, default=policy.artifact_directory / "results.json"
    )
    parser.add_argument(
        "--log", type=Path, default=policy.artifact_directory / "application.log"
    )
    parser.add_argument("--compose-file", type=Path)
    parser.add_argument("--validate-trace", action="store_true")
    arguments = parser.parse_args()
    if arguments.count < 1:
        parser.error("--count must be at least 1")
    return arguments


def run(policy: SignatureScenarioPolicy) -> None:
    arguments = parse_arguments(policy)
    asyncio.run(
        main(
            policy,
            arguments.count,
            arguments.output,
            arguments.log,
            arguments.compose_file,
            arguments.validate_trace,
        )
    )


@asynccontextmanager
async def _timestamp_service(policy: SignatureScenarioPolicy):
    if policy.timestamp_expected:
        async with local_tsa():
            yield
        return
    yield


if __name__ == "__main__":
    run(PADES_B_B_POLICY)
