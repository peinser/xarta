"""Verify and measure x402 v2 against a deterministic local facilitator."""

from __future__ import annotations

import argparse
import asyncio
import datetime
import hashlib
import ipaddress
import json
import os
import socket
import ssl
import sys
import tempfile
import time

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any

import aiohttp
import httpx

from aiohttp import web
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from x402.extensions.payment_identifier import PAYMENT_IDENTIFIER
from x402.extensions.payment_identifier import append_payment_identifier_to_extensions
from x402.extensions.payment_identifier import declare_payment_identifier_extension
from x402.http import FacilitatorConfig
from x402.http import HTTPFacilitatorClient
from x402.http import decode_payment_required_header
from x402.http import decode_payment_response_header
from x402.schemas import PaymentPayload

from tests.scenarios.common import SCENARIO_ARTIFACTS
from tests.scenarios.common import statistics_for
from xarta.payments.x402 import PaymentRequirementsMismatchError
from xarta.payments.x402 import X402HTTPTransport
from xarta.payments.x402 import X402PaymentServer
from xarta.payments.x402 import parse_x402_configuration
from xarta.pricing import Money
from xarta.pricing import PriceQuote

HOST = "127.0.0.1"
FACILITATOR_PORT = 8402
FACILITATOR_URL = f"https://localhost:{FACILITATOR_PORT}"
NETWORK = "eip155:84532"
PAY_TO = "0x0000000000000000000000000000000000000001"
PAYER = "0x0000000000000000000000000000000000000002"
CHALLENGE_SIGNING_KEY = "local-scenario-challenge-signing-key"
RESOURCE_URL = "https://api.example.com/api/v1/preview/"
DEFAULT_OUTPUT = SCENARIO_ARTIFACTS / "x402-facilitator/results.json"
DEFAULT_LOG = SCENARIO_ARTIFACTS / "x402-facilitator/facilitator.log"


class FacilitatorState:
    def __init__(self, log_path: Path) -> None:
        self.log_path = log_path
        self.verified: set[str] = set()
        self.settled: set[str] = set()
        self.events: list[dict[str, str]] = []

    def record(self, operation: str, payment_id: str) -> None:
        event = {
            "operation": operation,
            "payment_identifier": payment_id,
            "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
        }
        self.events.append(event)
        with self.log_path.open("a") as log:
            log.write(json.dumps(event) + "\n")


def payment_identifier(body: dict[str, Any]) -> str:
    try:
        payment_payload = body["paymentPayload"]
        requirements = body["paymentRequirements"]
        if body["x402Version"] != 2 or payment_payload["x402Version"] != 2:
            raise ValueError("only x402 v2 is supported")
        if payment_payload["accepted"] != requirements:
            raise ValueError("accepted requirements do not match server requirements")
        if requirements["scheme"] != "exact" or requirements["network"] != NETWORK:
            raise ValueError("unsupported payment kind")
        extension = payment_payload["extensions"][PAYMENT_IDENTIFIER]
        value = extension["info"]["id"]
    except (KeyError, TypeError) as ex:
        raise ValueError("malformed facilitator request") from ex
    if not isinstance(value, str) or not value:
        raise ValueError("invalid payment identifier")
    return value


async def supported(_: web.Request) -> web.Response:
    return web.json_response(
        {
            "kinds": [{"x402Version": 2, "scheme": "exact", "network": NETWORK}],
            "extensions": [PAYMENT_IDENTIFIER],
            "signers": {},
        }
    )


async def verify(request: web.Request) -> web.Response:
    state: FacilitatorState = request.app["state"]
    try:
        identifier = payment_identifier(await request.json())
    except (json.JSONDecodeError, ValueError) as ex:
        return web.json_response({"error": str(ex)}, status=400)
    state.verified.add(identifier)
    state.record("verify", identifier)
    return web.json_response({"isValid": True, "payer": PAYER})


async def settle(request: web.Request) -> web.Response:
    state: FacilitatorState = request.app["state"]
    try:
        body = await request.json()
        identifier = payment_identifier(body)
    except (json.JSONDecodeError, ValueError) as ex:
        return web.json_response({"error": str(ex)}, status=400)
    requirements = body["paymentRequirements"]
    payment_flow = requirements.get("extra", {}).get("paymentFlow")
    if payment_flow == "authorization" and identifier not in state.verified:
        return web.json_response(
            {
                "success": False,
                "errorReason": "payment_not_verified",
                "payer": PAYER,
                "transaction": "",
                "network": NETWORK,
                "amount": requirements["amount"],
            }
        )
    if identifier in state.settled:
        return web.json_response(
            {
                "success": False,
                "errorReason": "duplicate_settlement",
                "payer": PAYER,
                "transaction": "",
                "network": NETWORK,
                "amount": requirements["amount"],
            }
        )
    state.settled.add(identifier)
    state.record("settle", identifier)
    transaction = "0x" + hashlib.sha256(identifier.encode()).hexdigest()
    return web.json_response(
        {
            "success": True,
            "payer": PAYER,
            "transaction": transaction,
            "network": NETWORK,
            "amount": requirements["amount"],
        }
    )


@asynccontextmanager
async def local_facilitator(
    log_path: Path,
) -> AsyncIterator[Path]:
    require_available_port()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("")
    with tempfile.TemporaryDirectory(prefix="xarta-x402-facilitator-") as temporary:
        certificate_path, key_path = create_certificate(Path(temporary))
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "tests.scenarios.x402_facilitator",
            "--serve",
            "--certificate",
            str(certificate_path),
            "--key",
            str(key_path),
            "--log",
            str(log_path),
        )
        try:
            await wait_until_ready(process, certificate_path)
            yield certificate_path
        finally:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except TimeoutError:
                    process.kill()
                    await process.wait()


async def statistics(request: web.Request) -> web.Response:
    state: FacilitatorState = request.app["state"]
    return web.json_response(
        {
            "verified": sorted(state.verified),
            "settled": sorted(state.settled),
            "events": state.events,
        }
    )


def facilitator_app(log_path: Path) -> web.Application:
    app = web.Application()
    app["state"] = FacilitatorState(log_path)
    app.router.add_get("/supported", supported)
    app.router.add_post("/verify", verify)
    app.router.add_post("/settle", settle)
    app.router.add_get("/statistics", statistics)
    return app


def create_certificate(directory: Path) -> tuple[Path, Path]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(hours=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("localhost"),
                    x509.IPAddress(ipaddress.ip_address(HOST)),
                ]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    certificate_path = directory / "certificate.pem"
    key_path = directory / "private-key.pem"
    certificate_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    key_path.chmod(0o600)
    return certificate_path, key_path


def require_available_port() -> None:
    with socket.socket() as connection:
        connection.settimeout(0.2)
        if connection.connect_ex((HOST, FACILITATOR_PORT)) == 0:
            raise RuntimeError(
                f"Port {FACILITATOR_PORT} is in use; the x402 scenario owns it"
            )


async def wait_until_ready(
    process: asyncio.subprocess.Process, certificate_path: Path
) -> None:
    timeout = time.monotonic() + 10
    context = ssl.create_default_context(cafile=str(certificate_path))
    async with aiohttp.ClientSession() as session:
        while time.monotonic() < timeout:
            if process.returncode is not None:
                raise RuntimeError("Local x402 facilitator exited during startup")
            try:
                async with session.get(
                    f"{FACILITATOR_URL}/supported", ssl=context
                ) as response:
                    if response.status == 200:
                        return
            except aiohttp.ClientError:
                pass
            await asyncio.sleep(0.05)
    raise TimeoutError("Local x402 facilitator did not become ready")


def configuration() -> dict[str, Any]:
    return {
        "enabled": True,
        "intake_enabled": False,
        "preview_enabled": True,
        "facilitator_url": FACILITATOR_URL,
        "network": NETWORK,
        "pay_to": PAY_TO,
        "payment_timeout_seconds": 60,
        "preview_resource_url": RESOURCE_URL,
        "challenge_signing_key": CHALLENGE_SIGNING_KEY,
    }


def quote(index: int) -> PriceQuote:
    now = datetime.datetime.now(datetime.UTC)
    return PriceQuote(
        resource=RESOURCE_URL,
        request_fingerprint=f"scenario-request-{index}",
        pricing_revision="scenario-v1",
        estimated_internal_cost=Money(Decimal("0.001"), "USD"),
        selling_price=Money(Decimal("0.001"), "USD"),
        created_at=now,
        expires_at=now + datetime.timedelta(minutes=5),
        operations=(),
    )


def payload(requirements, identifier: str) -> PaymentPayload:
    extensions = {
        PAYMENT_IDENTIFIER: declare_payment_identifier_extension(required=True)
    }
    append_payment_identifier_to_extensions(extensions, identifier)
    return PaymentPayload(
        accepted=requirements,
        payload={
            "signature": "0xscenario-signature",
            "authorization": {"nonce": identifier},
        },
        extensions=extensions,
    )


async def execute(count: int, certificate_path: Path) -> dict[str, Any]:
    async_client = httpx.AsyncClient(verify=str(certificate_path))
    facilitator = HTTPFacilitatorClient(
        FacilitatorConfig(url=FACILITATOR_URL, http_client=async_client)
    )
    previous_ca = os.environ.get("SSL_CERT_FILE")
    os.environ["SSL_CERT_FILE"] = str(certificate_path)
    try:
        payment_server = X402PaymentServer(
            parse_x402_configuration(configuration()),
            facilitator_client=facilitator,
        )
        payment_server.initialize()
    finally:
        if previous_ca is None:
            os.environ.pop("SSL_CERT_FILE", None)
        else:
            os.environ["SSL_CERT_FILE"] = previous_ca

    transport = X402HTTPTransport()
    challenge_durations: list[float] = []
    verify_durations: list[float] = []
    settle_durations: list[float] = []
    total_durations: list[float] = []
    identifiers: list[str] = []
    transactions: list[str] = []
    first_payload: PaymentPayload | None = None
    first_requirements = None
    first_extensions: dict[str, Any] | None = None

    try:
        for index in range(count):
            started = time.perf_counter_ns()
            challenge_started = time.perf_counter_ns()
            payment_required, requirements = (
                payment_server.issue_preview_payment_required(
                    quote(index),
                    description="x402 facilitator scenario",
                    mime_type="application/json",
                )
            )
            restored = decode_payment_required_header(
                transport.payment_required_header(payment_required)
            )
            challenge_durations.append(
                (time.perf_counter_ns() - challenge_started) / 1_000_000
            )
            if restored != payment_required or restored.x402_version != 2:
                raise RuntimeError("x402 challenge did not survive the HTTP round trip")
            if requirements.extra.get("paymentFlow") != "authorization":
                raise RuntimeError(
                    "Preview did not advertise authorization payment flow"
                )

            identifier = f"pay_scenario_{index:04d}_{hashlib.sha256(str(index).encode()).hexdigest()[:16]}"
            payment_payload = payload(requirements, identifier)
            payment_server.require_payment_identifier(payment_payload)

            verify_started = time.perf_counter_ns()
            verification = await payment_server.verify(
                payment_payload,
                requirements,
                declared_extensions=payment_required.extensions,
            )
            verify_durations.append(
                (time.perf_counter_ns() - verify_started) / 1_000_000
            )
            if not verification.is_valid or verification.payer != PAYER:
                raise RuntimeError(f"Facilitator rejected payment {identifier}")

            settle_started = time.perf_counter_ns()
            settlement = await payment_server.settle(
                payment_payload,
                requirements,
                declared_extensions=payment_required.extensions,
            )
            settle_durations.append(
                (time.perf_counter_ns() - settle_started) / 1_000_000
            )
            restored_settlement = decode_payment_response_header(
                transport.payment_response_header(settlement)
            )
            if restored_settlement != settlement or not settlement.success:
                raise RuntimeError(f"Settlement response failed for {identifier}")
            expected_transaction = (
                "0x" + hashlib.sha256(identifier.encode()).hexdigest()
            )
            if settlement.transaction != expected_transaction:
                raise RuntimeError(f"Unexpected transaction for {identifier}")

            total_ms = (time.perf_counter_ns() - started) / 1_000_000
            total_durations.append(total_ms)
            identifiers.append(identifier)
            transactions.append(settlement.transaction)
            if first_payload is None:
                first_payload = payment_payload
                first_requirements = requirements
                first_extensions = payment_required.extensions
            print(f"completed={index + 1} latest_ms={total_ms:.3f}", flush=True)

        assert first_payload is not None
        assert first_requirements is not None
        assert first_extensions is not None
        duplicate = await payment_server.settle(
            first_payload,
            first_requirements,
            declared_extensions=first_extensions,
        )
        if duplicate.success or duplicate.error_reason != "duplicate_settlement":
            raise RuntimeError("Facilitator did not reject duplicate settlement")

        mismatched = first_requirements.model_copy(update={"amount": "1"})
        try:
            payment_server.require_server_requirements(
                payload(mismatched, "pay_mismatch_1234567890"), first_requirements
            )
        except PaymentRequirementsMismatchError:
            mismatch_rejected = True
        else:
            mismatch_rejected = False
            raise RuntimeError("Resource server accepted client-modified requirements")

        context = ssl.create_default_context(cafile=str(certificate_path))
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{FACILITATOR_URL}/statistics", ssl=context
            ) as response:
                facilitator_statistics = await response.json()
        if facilitator_statistics["verified"] != sorted(identifiers):
            raise RuntimeError("Facilitator verification ledger does not match the run")
        if facilitator_statistics["settled"] != sorted(identifiers):
            raise RuntimeError("Facilitator settlement ledger does not match the run")
        expected_events = [
            event
            for identifier in identifiers
            for event in (
                {"operation": "verify", "payment_identifier": identifier},
                {"operation": "settle", "payment_identifier": identifier},
            )
        ]
        actual_events = [
            {
                "operation": event["operation"],
                "payment_identifier": event["payment_identifier"],
            }
            for event in facilitator_statistics["events"]
        ]
        if actual_events != expected_events:
            raise RuntimeError(
                "Facilitator calls did not follow verify/settle ordering"
            )
    finally:
        await facilitator.aclose()
        await async_client.aclose()

    return {
        "functional": {
            "protocol": "x402-v2",
            "network": NETWORK,
            "payments_verified": len(identifiers),
            "payments_settled": len(transactions),
            "unique_payment_identifiers": len(set(identifiers)),
            "unique_transactions": len(set(transactions)),
            "duplicate_settlement_rejected": True,
            "requirements_mismatch_rejected": mismatch_rejected,
            "facilitator_is_local": True,
            "cryptographic_signatures_validated": False,
            "transaction_hashes_are_synthetic": True,
        },
        "performance": {
            "challenge": statistics_for(challenge_durations),
            "verify": statistics_for(verify_durations),
            "settle": statistics_for(settle_durations),
            "total": statistics_for(total_durations),
            "total_excluding_first": (
                statistics_for(total_durations[1:]) if count > 1 else None
            ),
        },
    }


async def run(count: int, output_path: Path, log_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    async with local_facilitator(log_path) as certificate_path:
        report = await execute(count, certificate_path)

    output_path.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({**report["functional"], **report["performance"]}, indent=2))
    print(f"report={output_path}")
    print(f"facilitator_log={log_path}")


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=50)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG)
    parser.add_argument("--serve", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--certificate", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--key", type=Path, help=argparse.SUPPRESS)
    arguments = parser.parse_args()
    if arguments.count < 1:
        parser.error("--count must be at least 1")
    if arguments.serve and (arguments.certificate is None or arguments.key is None):
        parser.error("--serve requires --certificate and --key")
    return arguments


if __name__ == "__main__":
    arguments = parse_arguments()
    if arguments.serve:
        tls = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        tls.load_cert_chain(arguments.certificate, arguments.key)
        web.run_app(
            facilitator_app(arguments.log),
            host=HOST,
            port=FACILITATOR_PORT,
            ssl_context=tls,
            print=None,
        )
    else:
        asyncio.run(run(arguments.count, arguments.output, arguments.log))
