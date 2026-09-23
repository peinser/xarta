"""Perform one explicitly authorized real x402 settlement on Base Sepolia."""

from __future__ import annotations

import asyncio
import json
import os
import time

from itertools import count
from pathlib import Path
from typing import Any

import httpx

from eth_account import Account
from x402 import x402Client
from x402.extensions.payment_identifier import PAYMENT_IDENTIFIER
from x402.extensions.payment_identifier import append_payment_identifier_to_extensions
from x402.extensions.payment_identifier import generate_payment_id
from x402.http import decode_payment_required_header
from x402.http import x402HTTPClient
from x402.http.clients import x402HttpxClient
from x402.mechanisms.evm import EthAccountSigner
from x402.mechanisms.evm.exact import register_exact_evm_client

from tests.scenarios.common import BASE_URL
from tests.scenarios.common import ROOT
from tests.scenarios.common import SCENARIO_ARTIFACTS
from tests.scenarios.common import standalone_application
from tests.scenarios.x402_paid_intake import flow
from tests.scenarios.x402_paid_intake import identities

NETWORK = "eip155:84532"
CHAIN_ID = 84532
USDC = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
RESOURCE_URL = "https://api.example.com/api/v1/intake/"
DEFAULT_OUTPUT = SCENARIO_ARTIFACTS / "x402-base-sepolia/results.json"
DEFAULT_APPLICATION_LOG = SCENARIO_ARTIFACTS / "x402-base-sepolia/application.log"
_RPC_IDS = count(1)


def required_environment(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


async def rpc(
    client: httpx.AsyncClient, url: str, method: str, params: list[Any]
) -> Any:
    response = await client.post(
        url,
        json={
            "jsonrpc": "2.0",
            "id": next(_RPC_IDS),
            "method": method,
            "params": params,
        },
    )
    response.raise_for_status()
    body = response.json()
    if "error" in body:
        raise RuntimeError(f"JSON-RPC {method} failed: {body['error']}")
    if "result" not in body:
        raise RuntimeError(f"JSON-RPC {method} omitted result")
    return body["result"]


async def balance_of(client: httpx.AsyncClient, rpc_url: str, address: str) -> int:
    account = address.removeprefix("0x")
    if len(account) != 40:
        raise ValueError("USDC balance address is invalid")
    calldata = "0x70a08231" + account.lower().rjust(64, "0")
    result = await rpc(
        client,
        rpc_url,
        "eth_call",
        [{"to": USDC, "data": calldata}, "latest"],
    )
    return int(result, 16)


async def wait_for_receipt(
    client: httpx.AsyncClient, rpc_url: str, transaction: str
) -> dict[str, Any]:
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        receipt = await rpc(client, rpc_url, "eth_getTransactionReceipt", [transaction])
        if receipt is not None:
            if int(receipt["status"], 16) != 1:
                raise RuntimeError(f"Settlement transaction reverted: {transaction}")
            return receipt
        await asyncio.sleep(1)
    raise TimeoutError(f"Settlement receipt was not available: {transaction}")


async def verify_archive(ids: dict[str, str], expected: bytes) -> None:
    endpoint = (
        f"{BASE_URL}/api/v1/archive/documents/{ids['document']}"
        f"/versions/{ids['version']}"
    )
    deadline = time.monotonic() + 30
    async with httpx.AsyncClient(timeout=10) as client:
        while time.monotonic() < deadline:
            response = await client.get(endpoint)
            if response.status_code == 200:
                if response.content != expected:
                    raise RuntimeError("Base Sepolia paid archive bytes are incorrect")
                return
            if response.status_code != 404:
                raise RuntimeError(
                    f"Base Sepolia paid archive returned HTTP {response.status_code}"
                )
            await asyncio.sleep(0.1)
    raise TimeoutError("Base Sepolia paid archive did not complete")


def multipart(ids: dict[str, str], definition: dict[str, Any], data: bytes):
    return {
        "flow": ("flow.json", json.dumps(definition), "application/json"),
        ids["document"]: ("base-sepolia-contract.txt", data, "text/plain"),
    }


def settlement_log(log_path: Path, transaction: str) -> dict[str, Any]:
    matches = []
    for line in log_path.read_text().splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("event") == "x402_settlement_succeeded":
            matches.append(event)
    if len(matches) != 1 or matches[0].get("transaction") != transaction:
        raise RuntimeError("Xarta did not log the Base Sepolia settlement transaction")
    return matches[0]


async def run(
    output_path: Path = DEFAULT_OUTPUT,
    application_log: Path = DEFAULT_APPLICATION_LOG,
) -> None:
    if os.environ.get("X402_BASE_SEPOLIA_CONTRACT_TEST") != "true":
        raise RuntimeError(
            "Set X402_BASE_SEPOLIA_CONTRACT_TEST=true to authorize this test"
        )

    private_key = required_environment("EVM_PRIVATE_KEY")
    rpc_url = required_environment("BASE_SEPOLIA_RPC_URL")
    pay_to = required_environment("X402_PAY_TO")
    facilitator_url = os.environ.get(
        "X402_FACILITATOR_URL", "https://x402.org/facilitator"
    )
    buyer = Account.from_key(private_key)
    seller = os.environ.get("X402_SELLER_ADDRESS", pay_to)
    if seller.lower() != pay_to.lower():
        raise RuntimeError("X402_SELLER_ADDRESS must equal X402_PAY_TO")

    ids = identities()
    definition = flow(ids, 1)
    data = b"xarta Base Sepolia x402 contract scenario\n"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    environment = {
        "X402_ENABLED": "true",
        "X402_INTAKE_ENABLED": "true",
        "X402_PREVIEW_ENABLED": "false",
        "X402_FACILITATOR_URL": facilitator_url,
        "X402_NETWORK": NETWORK,
        "X402_PAY_TO": pay_to,
        "X402_PAYMENT_TIMEOUT_SECONDS": "60",
        "X402_INTAKE_RESOURCE_URL": RESOURCE_URL,
        "X402_CHALLENGE_SIGNING_KEY": "base-sepolia-scenario-challenge-key",
        "PRICING_CONFIG_PATH": str(ROOT / ".dev/conf/pricing.json"),
    }
    if "EVM_PRIVATE_KEY" in environment or "X402_SELLER_PRIVATE_KEY" in environment:
        raise RuntimeError("Wallet private keys must not enter the Xarta environment")

    async with httpx.AsyncClient(timeout=60) as rpc_client:
        actual_chain = int(await rpc(rpc_client, rpc_url, "eth_chainId", []), 16)
        if actual_chain != CHAIN_ID:
            raise RuntimeError(f"RPC endpoint uses unexpected chain {actual_chain}")
        buyer_before = await balance_of(rpc_client, rpc_url, buyer.address)
        seller_before = await balance_of(rpc_client, rpc_url, seller)

        async with standalone_application(application_log, {"archive"}, environment):
            async with httpx.AsyncClient(timeout=60) as client:
                challenge_response = await client.post(
                    f"{BASE_URL}/api/v1/intake/",
                    files=multipart(ids, definition, data),
                )
                if challenge_response.status_code != 402:
                    raise RuntimeError(
                        f"Unpaid request returned HTTP {challenge_response.status_code}"
                    )
                encoded = challenge_response.headers.get("PAYMENT-REQUIRED")
                if encoded is None:
                    raise RuntimeError("Unpaid request omitted PAYMENT-REQUIRED")
                payment_required = decode_payment_required_header(encoded)
                requirements = payment_required.accepts[0]
                if (
                    requirements.network != NETWORK
                    or requirements.asset.lower() != USDC.lower()
                    or requirements.pay_to.lower() != seller.lower()
                    or int(requirements.amount) <= 0
                ):
                    raise RuntimeError(
                        "Base Sepolia payment requirements are incorrect"
                    )
                amount = int(requirements.amount)
                if buyer_before < amount:
                    raise RuntimeError("Buyer has insufficient Base Sepolia USDC")

            protocol_client = x402Client()
            register_exact_evm_client(
                protocol_client, EthAccountSigner(buyer), networks=NETWORK
            )
            payment_id = generate_payment_id("xarta_base_sepolia_")

            def attach_payment_identifier(context) -> None:
                extensions = context.payment_required.extensions
                if extensions is None or PAYMENT_IDENTIFIER not in extensions:
                    raise RuntimeError("Xarta did not declare payment-identifier")
                append_payment_identifier_to_extensions(extensions, payment_id)

            protocol_client.on_before_payment_creation(attach_payment_identifier)
            async with x402HttpxClient(protocol_client, timeout=90) as paid_client:
                paid_response = await paid_client.post(
                    f"{BASE_URL}/api/v1/intake/",
                    files=multipart(ids, definition, data),
                )
                await paid_response.aread()
                if paid_response.status_code != 200:
                    raise RuntimeError(
                        f"Paid request returned HTTP {paid_response.status_code}: "
                        f"{paid_response.text}"
                    )
                settlement = x402HTTPClient(
                    protocol_client
                ).get_payment_settle_response(paid_response.headers.get)
                if settlement is None or not settlement.success:
                    raise RuntimeError(
                        "Paid response omitted successful settlement evidence"
                    )
                transaction = settlement.transaction
                if len(transaction) != 66 or not transaction.startswith("0x"):
                    raise RuntimeError(
                        "Paid response omitted settlement transaction hash"
                    )
                receipt_body = paid_response.json()
                if receipt_body.get("payment", {}).get("transaction") != transaction:
                    raise RuntimeError("Xarta JSON receipt omitted settlement evidence")

            await verify_archive(ids, data)

            receipt = await wait_for_receipt(rpc_client, rpc_url, transaction)
            buyer_after = await balance_of(rpc_client, rpc_url, buyer.address)
            seller_after = await balance_of(rpc_client, rpc_url, seller)

    seller_delta = seller_after - seller_before
    if seller_delta != amount:
        raise RuntimeError(
            f"Seller received {seller_delta} atomic USDC instead of {amount}"
        )
    if buyer_before - buyer_after != amount:
        raise RuntimeError("Buyer USDC decrease does not equal the payment amount")
    event = settlement_log(application_log, transaction)
    if (
        event.get("network") != NETWORK
        or event.get("amount") != str(amount)
        or event.get("pay_to", "").lower() != seller.lower()
        or receipt.get("transactionHash", "").lower() != transaction.lower()
    ):
        raise RuntimeError("Logged, protocol, and on-chain settlement evidence differ")

    report = {
        "network": NETWORK,
        "asset": USDC,
        "buyer_address": buyer.address,
        "seller_address": seller,
        "flow_id": ids["flow"],
        "amount_atomic": str(amount),
        "transaction": transaction,
        "transaction_status": "success",
        "buyer_balance_before": str(buyer_before),
        "buyer_balance_after": str(buyer_after),
        "seller_balance_before": str(seller_before),
        "seller_balance_after": str(seller_after),
        "seller_balance_delta": str(seller_delta),
        "xarta_execution_verified": True,
        "settlement_log_verified": True,
    }
    output_path.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    print(f"report={output_path}")
    print(f"application_log={application_log}")


if __name__ == "__main__":
    asyncio.run(run())
