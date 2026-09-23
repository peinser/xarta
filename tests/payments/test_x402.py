from __future__ import annotations

import datetime
import json

from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from sanic.exceptions import ServiceUnavailable
from x402 import SettleResponse
from x402 import SupportedKind
from x402 import SupportedResponse
from x402 import VerifyResponse
from x402.extensions.payment_identifier import PAYMENT_IDENTIFIER
from x402.extensions.payment_identifier import append_payment_identifier_to_extensions
from x402.extensions.payment_identifier import declare_payment_identifier_extension
from x402.http import decode_payment_required_header
from x402.http import decode_payment_response_header
from x402.http import encode_payment_signature_header
from x402.schemas import PaymentPayload
from x402.schemas import PaymentRequirements

from xarta.payments.x402 import InvalidPaymentChallengeError
from xarta.payments.x402 import InvalidSettlementEvidenceError
from xarta.payments.x402 import MalformedPaymentHeaderError
from xarta.payments.x402 import PaymentRequirementsMismatchError
from xarta.payments.x402 import PaymentSettlement
from xarta.payments.x402 import X402HTTPTransport
from xarta.payments.x402 import X402PaymentServer
from xarta.payments.x402 import parse_x402_configuration
from xarta.pricing import Money
from xarta.pricing import PriceQuote
from xarta.protocol.dag import Node
from xarta.protocol.document.request.flow import DocumentFlowRequest
from xarta.services.v1.intake import base as intake_base

NETWORK = "eip155:84532"
PAY_TO = "0x0000000000000000000000000000000000000001"
PAYER = "0x0000000000000000000000000000000000000002"
TRANSACTION = "0x" + "a" * 64
SIGNING_KEY = "scenario-challenge-signing-key-32-bytes"
INTAKE_RESOURCE = "https://api.example.com/api/v1/intake/"
PREVIEW_RESOURCE = "https://api.example.com/api/v1/preview/"


class Facilitator:
    def __init__(self, settlement: SettleResponse | Exception | None = None) -> None:
        self.settlement = settlement

    def get_supported(self):
        return SupportedResponse(
            kinds=[SupportedKind(x402Version=2, scheme="exact", network=NETWORK)],
            extensions=[PAYMENT_IDENTIFIER],
        )

    async def verify(self, payload, requirements):
        return VerifyResponse(isValid=True, payer=PAYER)

    async def settle(self, payload, requirements):
        if isinstance(self.settlement, Exception):
            raise self.settlement
        return self.settlement or SettleResponse(
            success=True,
            payer=PAYER,
            transaction=TRANSACTION,
            network=requirements.network,
            amount=requirements.amount,
        )


def configuration(**overrides):
    value = {
        "enabled": True,
        "intake_enabled": True,
        "preview_enabled": True,
        "facilitator_url": "https://x402.org/facilitator",
        "network": NETWORK,
        "pay_to": PAY_TO,
        "payment_timeout_seconds": 60,
        "intake_resource_url": INTAKE_RESOURCE,
        "preview_resource_url": PREVIEW_RESOURCE,
        "challenge_signing_key": SIGNING_KEY,
    }
    value.update(overrides)
    return parse_x402_configuration(value)


def quote(amount: str = "0.001", *, expired: bool = False) -> PriceQuote:
    now = datetime.datetime.now(datetime.UTC)
    if expired:
        now -= datetime.timedelta(minutes=10)
    return PriceQuote(
        resource=INTAKE_RESOURCE,
        request_fingerprint="request",
        pricing_revision="v1",
        estimated_internal_cost=Money(Decimal(amount), "USD"),
        selling_price=Money(Decimal(amount), "USD"),
        created_at=now,
        expires_at=now + datetime.timedelta(minutes=5),
        operations=(),
    )


def server(
    facilitator: Facilitator | None = None, **configuration_overrides
) -> X402PaymentServer:
    result = X402PaymentServer(
        configuration(**configuration_overrides),
        facilitator_client=facilitator or Facilitator(),
    )
    result.initialize()
    return result


def payload(
    requirements: PaymentRequirements, payment_id: str = "pay_1234567890123456"
) -> PaymentPayload:
    extensions = {
        PAYMENT_IDENTIFIER: declare_payment_identifier_extension(required=True)
    }
    append_payment_identifier_to_extensions(extensions, payment_id)
    return PaymentPayload(
        accepted=requirements,
        payload={"authorization": {}, "signature": "0xsignature"},
        extensions=extensions,
    )


def issue(payment_server: X402PaymentServer, amount: str = "0.001"):
    return payment_server.issue_intake_payment_required(
        quote(amount), description="Paid intake", mime_type="application/json"
    )


def test_configuration_supports_disabled_sepolia_and_mainnet() -> None:
    disabled = parse_x402_configuration({"enabled": False})
    sepolia = configuration()
    mainnet = configuration(
        network="eip155:8453", facilitator_url="https://facilitator.example.com"
    )

    assert disabled.enabled is False
    assert disabled.facilitator_url is None
    assert sepolia.network == "eip155:84532"
    assert mainnet.network == "eip155:8453"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"network": "eip155:1"}, "network"),
        ({"pay_to": "not-an-address"}, "EVM address"),
        ({"facilitator_url": "http://example.com"}, "HTTPS URL"),
        ({"facilitator_url": "https://user:pass@example.com"}, "credentials"),
        ({"payment_timeout_seconds": 0}, "positive integer"),
        ({"intake_resource_url": None}, "HTTPS URL"),
        ({"challenge_signing_key": "short"}, "at least 32 bytes"),
        (
            {"network": "eip155:8453"},
            "development facilitator",
        ),
    ],
)
def test_invalid_configuration_is_rejected(overrides, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        configuration(**overrides)


@pytest.mark.parametrize(
    ("usd", "atomic"),
    [("0.001", "1000"), ("0.00125", "1250"), ("1.00", "1000000")],
)
def test_official_v2_requirements_and_usdc_atomic_amount(usd: str, atomic: str) -> None:
    payment_required, requirements = issue(server(), usd)
    restored = decode_payment_required_header(
        X402HTTPTransport().payment_required_header(payment_required)
    )

    assert restored == payment_required
    assert payment_required.x402_version == 2
    assert requirements.scheme == "exact"
    assert requirements.network == NETWORK
    assert requirements.asset == "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
    assert requirements.amount == atomic
    assert requirements.pay_to == PAY_TO
    assert requirements.max_timeout_seconds == 60
    assert requirements.extra["paymentFlow"] == "upfront"
    assert requirements.extra["name"] == "USDC"
    assert requirements.extra["version"] == "2"
    assert isinstance(requirements.extra["xartaChallenge"], str)
    assert payment_required.extensions[PAYMENT_IDENTIFIER]["info"]["required"] is True


def test_positive_price_below_one_atomic_unit_is_rejected() -> None:
    with pytest.raises(ValueError, match="round down to zero"):
        issue(server(), "0.0000001")


def test_signed_challenge_authenticates_request_and_all_requirements() -> None:
    payment_server = server()
    _, requirements = issue(payment_server)

    authenticated = payment_server.require_authenticated_requirements(
        payload(requirements), resource=INTAKE_RESOURCE, request_fingerprint="request"
    )

    assert authenticated == requirements
    cheaper = requirements.model_copy(update={"amount": "1"})
    with pytest.raises(PaymentRequirementsMismatchError):
        payment_server.require_authenticated_requirements(
            payload(cheaper), resource=INTAKE_RESOURCE, request_fingerprint="request"
        )
    with pytest.raises(InvalidPaymentChallengeError, match="resource"):
        payment_server.require_authenticated_requirements(
            payload(requirements),
            resource="https://api.example.com/other",
            request_fingerprint="request",
        )
    with pytest.raises(InvalidPaymentChallengeError, match="prepared request"):
        payment_server.require_authenticated_requirements(
            payload(requirements),
            resource=INTAKE_RESOURCE,
            request_fingerprint="different",
        )


def test_expired_and_foreign_signed_challenges_are_rejected() -> None:
    payment_server = server()
    _, expired_requirements = payment_server.issue_intake_payment_required(
        quote(expired=True), description="Paid intake", mime_type="application/json"
    )
    with pytest.raises(InvalidPaymentChallengeError, match="expired"):
        payment_server.require_authenticated_requirements(
            payload(expired_requirements),
            resource=INTAKE_RESOURCE,
            request_fingerprint="request",
        )

    _, requirements = issue(payment_server)
    with pytest.raises(InvalidPaymentChallengeError):
        server(
            challenge_signing_key="another-challenge-signing-key-32-bytes"
        ).require_authenticated_requirements(
            payload(requirements),
            resource=INTAKE_RESOURCE,
            request_fingerprint="request",
        )


def test_payment_identifier_is_supported_only_as_protocol_extension() -> None:
    payment_server = server()
    _, requirements = issue(payment_server)

    assert payment_server.require_payment_identifier(payload(requirements)).startswith(
        "pay_"
    )
    missing = PaymentPayload(
        accepted=requirements,
        payload={"authorization": {}, "signature": "0xsignature"},
    )
    with pytest.raises(ValueError, match="payment-identifier"):
        payment_server.require_payment_identifier(missing)


def test_protocol_payload_and_settlement_headers_round_trip() -> None:
    _, requirements = issue(server())
    payment_payload = payload(requirements)
    transport = X402HTTPTransport()

    restored_payload = transport.read_payment_payload(
        {"PAYMENT-SIGNATURE": encode_payment_signature_header(payment_payload)}
    )
    settlement = SettleResponse(
        success=True,
        payer=PAYER,
        transaction=TRANSACTION,
        network=NETWORK,
        amount="1000",
    )
    restored_settlement = decode_payment_response_header(
        transport.payment_response_header(settlement)
    )

    assert restored_payload == payment_payload
    assert restored_settlement == settlement
    with pytest.raises(MalformedPaymentHeaderError):
        transport.read_payment_payload({"PAYMENT-SIGNATURE": "not-base64"})


@pytest.mark.asyncio
async def test_official_server_verifies_and_settles_against_signed_terms() -> None:
    payment_server = server()
    payment_required, requirements = issue(payment_server)
    payment_payload = payload(requirements)

    verification = await payment_server.verify(
        payment_payload,
        requirements,
        declared_extensions=payment_required.extensions,
    )
    settlement = await payment_server.settle(
        payment_payload,
        requirements,
        declared_extensions=payment_required.extensions,
    )

    assert verification.is_valid is True
    assert settlement.transaction == TRANSACTION


def test_successful_settlement_requires_complete_transaction_evidence() -> None:
    _, requirements = issue(server(), "0.00125")
    response = SettleResponse(
        success=True,
        payer=PAYER,
        transaction=TRANSACTION,
        network=NETWORK,
        amount="1250",
    )

    assert PaymentSettlement.from_response(response, requirements).dict() == {
        "network": NETWORK,
        "transaction": TRANSACTION,
        "payer": PAYER,
        "pay_to": PAY_TO,
        "asset": requirements.asset,
        "amount": "1250",
    }
    for invalid in (
        response.model_copy(update={"transaction": ""}),
        response.model_copy(update={"network": "eip155:8453"}),
        response.model_copy(update={"amount": "1"}),
        response.model_copy(update={"payer": None}),
    ):
        with pytest.raises(InvalidSettlementEvidenceError):
            PaymentSettlement.from_response(invalid, requirements)


def paid_request(payment_server: X402PaymentServer, requirements, events):
    class Logger:
        def info(self, event, **values):
            events.append(("log", event, values))

    return SimpleNamespace(
        headers={
            "PAYMENT-SIGNATURE": encode_payment_signature_header(payload(requirements))
        },
        app=SimpleNamespace(
            ctx=SimpleNamespace(
                x402_configuration=configuration(),
                x402_payment_server=payment_server,
                x402_transport=X402HTTPTransport(),
                logger=Logger(),
            )
        ),
    )


def prepared_intake():
    flow = DocumentFlowRequest(id=uuid4(), dag=Node(kind="archive"))
    return SimpleNamespace(
        flow=flow,
        sources=(),
        flow_id_supplied=True,
        semantic_fingerprint="request",
    )


@pytest.mark.asyncio
async def test_paid_intake_orders_settlement_log_execution_and_response(
    monkeypatch,
) -> None:
    events = []
    payment_server = server()
    _, requirements = issue(payment_server)
    original_settle = payment_server.settle

    async def settle(*args, **kwargs):
        events.append(("settle",))
        return await original_settle(*args, **kwargs)

    async def schedule(flow, admission=None):
        events.append(("execute", admission))

    payment_server.settle = settle
    monkeypatch.setattr(intake_base.NATS, "schedule", schedule)
    prepared = prepared_intake()

    result = await intake_base._accept_intake(
        paid_request(payment_server, requirements, events),
        prepared,
        {"id": str(prepared.flow.id)},
    )

    body = json.loads(result.body)
    assert [event[0] for event in events] == ["settle", "log", "execute"]
    assert events[1][1] == "x402_settlement_succeeded"
    assert events[1][2]["transaction"] == TRANSACTION
    assert events[1][2]["flow_id"] == str(prepared.flow.id)
    assert events[2][1]["transaction"] == TRANSACTION
    assert body["payment"]["transaction"] == TRANSACTION
    assert (
        decode_payment_response_header(result.headers["payment-response"]).transaction
        == TRANSACTION
    )


@pytest.mark.asyncio
async def test_rejected_settlement_does_not_log_or_execute(monkeypatch) -> None:
    events = []
    rejection = SettleResponse(
        success=False,
        errorReason="invalid_payment",
        payer=PAYER,
        transaction="",
        network=NETWORK,
        amount="1000",
    )
    payment_server = server(Facilitator(rejection))
    _, requirements = issue(payment_server)

    async def schedule(flow, admission=None):
        events.append(("execute",))

    monkeypatch.setattr(intake_base.NATS, "schedule", schedule)
    result = await intake_base._accept_intake(
        paid_request(payment_server, requirements, events),
        prepared_intake(),
        {"id": "unused"},
    )

    assert result.status == 402
    assert events == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        TimeoutError(),
        ConnectionError(),
        RuntimeError("HTTP 500"),
        ValueError("malformed"),
    ],
)
async def test_ambiguous_settlement_exception_does_not_execute(
    monkeypatch, error: Exception
) -> None:
    events = []
    payment_server = server(Facilitator(error))
    _, requirements = issue(payment_server)

    async def schedule(flow, admission=None):
        events.append(("execute",))

    monkeypatch.setattr(intake_base.NATS, "schedule", schedule)
    with pytest.raises(ServiceUnavailable, match="uncertain"):
        await intake_base._accept_intake(
            paid_request(payment_server, requirements, events),
            prepared_intake(),
            {"id": "unused"},
        )
    assert events == []
