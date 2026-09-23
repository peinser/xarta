from __future__ import annotations

# ruff: noqa: I001

from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from typing import cast
from typing import Literal

import jwt

from eth_utils import is_address
from x402 import ResourceConfig
from x402 import ResourceInfo
from x402 import SettleResponse
from x402.extensions.payment_identifier import PAYMENT_IDENTIFIER
from x402.extensions.payment_identifier import declare_payment_identifier_extension
from x402.extensions.payment_identifier import extract_and_validate_payment_identifier
from x402.extensions.payment_identifier import (
    payment_identifier_resource_server_extension,
)
from x402.http import FacilitatorConfig
from x402.http import HTTPFacilitatorClient
from x402.mechanisms.evm.exact import ExactEvmServerScheme
from x402.schemas import PaymentPayload
from x402.schemas import PaymentRequired
from x402.schemas import PaymentRequirements
from x402.server import x402ResourceServer

from xarta.payments.x402.configuration import X402Configuration
from xarta.pricing.models import PriceQuote


class MissingPaymentIdentifierError(ValueError):
    pass


class PaymentRequirementsMismatchError(ValueError):
    pass


class InvalidPaymentChallengeError(ValueError):
    pass


class InvalidSettlementEvidenceError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PaymentSettlement:
    network: str
    transaction: str
    payer: str
    pay_to: str
    asset: str
    amount: str

    @classmethod
    def from_response(
        cls, response: SettleResponse, requirements: PaymentRequirements
    ) -> PaymentSettlement:
        transaction = response.transaction
        if (
            not response.success
            or len(transaction) != 66
            or not transaction.startswith("0x")
            or any(
                character not in "0123456789abcdefABCDEF"
                for character in transaction[2:]
            )
        ):
            raise InvalidSettlementEvidenceError(
                "Successful EVM settlement requires a transaction hash"
            )
        if response.network != requirements.network:
            raise InvalidSettlementEvidenceError(
                "Settlement network does not match payment requirements"
            )
        if response.amount is not None and response.amount != requirements.amount:
            raise InvalidSettlementEvidenceError(
                "Settlement amount does not match payment requirements"
            )
        if response.payer is None or not is_address(response.payer):
            raise InvalidSettlementEvidenceError(
                "Successful EVM settlement requires a payer address"
            )
        return cls(
            network=response.network,
            transaction=transaction,
            payer=response.payer,
            pay_to=requirements.pay_to,
            asset=requirements.asset,
            amount=requirements.amount,
        )

    def dict(self) -> dict[str, str]:
        return {
            "network": self.network,
            "transaction": self.transaction,
            "payer": self.payer,
            "pay_to": self.pay_to,
            "asset": self.asset,
            "amount": self.amount,
        }


class X402PaymentServer:
    _CHALLENGE_AUDIENCE = "xarta-x402"
    _CHALLENGE_FIELD = "xartaChallenge"

    def __init__(
        self,
        configuration: X402Configuration,
        *,
        facilitator_client=None,
    ) -> None:
        self.configuration = configuration
        if not configuration.enabled:
            raise ValueError(
                "Cannot create an x402 payment server while x402 is disabled"
            )
        if (
            not configuration.facilitator_url
            or not configuration.network
            or not configuration.challenge_signing_key
        ):
            raise ValueError(
                "Enabled x402 requires facilitator and network configuration"
            )
        self.challenge_signing_key = configuration.challenge_signing_key
        facilitator = facilitator_client or HTTPFacilitatorClient(
            FacilitatorConfig(url=configuration.facilitator_url)
        )
        self.facilitator = facilitator
        self.resource_server = x402ResourceServer(facilitator)
        self.resource_server.register(
            configuration.network, cast(Any, ExactEvmServerScheme())
        ).register_extension(cast(Any, payment_identifier_resource_server_extension))

    def initialize(self) -> None:
        self.resource_server.initialize()

    async def aclose(self) -> None:
        close = getattr(self.facilitator, "aclose", None)
        if close is not None:
            await close()

    def issue_payment_required(
        self,
        quote: PriceQuote,
        *,
        description: str,
        mime_type: str,
        payment_flow: Literal["authorization", "upfront"] = "authorization",
    ) -> tuple[PaymentRequired, PaymentRequirements]:
        if quote.selling_price.currency != "USD":
            raise ValueError(
                "Xarta's initial x402 pricing policy supports USD-denominated quotes only"
            )
        if quote.selling_price.amount <= Decimal(0):
            raise ValueError("Free quotes do not create x402 requirements")
        network = self.configuration.network
        pay_to = self.configuration.pay_to
        timeout = self.configuration.payment_timeout_seconds
        if network is None or pay_to is None or timeout is None:
            raise ValueError("Enabled x402 payment configuration is incomplete")
        requirements = self.resource_server.build_payment_requirements(
            ResourceConfig(
                scheme="exact",
                network=network,
                pay_to=pay_to,
                price=f"${quote.selling_price.amount:f}",
                max_timeout_seconds=timeout,
                extra={"paymentFlow": payment_flow},
            ),
            extensions=[PAYMENT_IDENTIFIER],
        )[0]
        if int(requirements.amount) <= 0:
            raise ValueError("A positive selling price must not round down to zero")
        token = jwt.encode(
            {
                "aud": self._CHALLENGE_AUDIENCE,
                "iat": int(quote.created_at.timestamp()),
                "exp": int(quote.expires_at.timestamp()),
                "resource": quote.resource,
                "request_fingerprint": quote.request_fingerprint,
                "pricing_revision": quote.pricing_revision,
                "requirements": requirements.model_dump(
                    by_alias=True, exclude_none=True
                ),
            },
            self.challenge_signing_key,
            algorithm="HS256",
        )
        requirements = requirements.model_copy(
            update={"extra": {**requirements.extra, self._CHALLENGE_FIELD: token}}
        )
        payment_required = PaymentRequired(
            resource=ResourceInfo(
                url=quote.resource,
                description=description,
                mime_type=mime_type,
            ),
            accepts=[requirements],
            extensions=self.payment_extensions(),
        )
        return payment_required, requirements

    @staticmethod
    def payment_extensions() -> dict[str, Any]:
        return {PAYMENT_IDENTIFIER: declare_payment_identifier_extension(required=True)}

    def require_authenticated_requirements(
        self,
        payment_payload: PaymentPayload,
        *,
        resource: str,
        request_fingerprint: str,
    ) -> PaymentRequirements:
        extra = payment_payload.accepted.extra or {}
        token = extra.get(self._CHALLENGE_FIELD)
        if not isinstance(token, str):
            raise InvalidPaymentChallengeError(
                "Payment requirements do not contain Xarta's signed challenge"
            )
        try:
            claims = jwt.decode(
                token,
                self.challenge_signing_key,
                algorithms=["HS256"],
                audience=self._CHALLENGE_AUDIENCE,
                options={
                    "require": [
                        "aud",
                        "iat",
                        "exp",
                        "resource",
                        "request_fingerprint",
                        "pricing_revision",
                        "requirements",
                    ]
                },
            )
            server_requirements = PaymentRequirements.model_validate(
                claims["requirements"]
            )
        except (jwt.PyJWTError, KeyError, TypeError, ValueError) as ex:
            raise InvalidPaymentChallengeError(
                "Payment requirements contain an invalid or expired Xarta challenge"
            ) from ex
        if claims["resource"] != resource:
            raise InvalidPaymentChallengeError(
                "Payment challenge does not apply to this resource"
            )
        if claims["request_fingerprint"] != request_fingerprint:
            raise InvalidPaymentChallengeError(
                "Payment challenge does not match the prepared request"
            )
        accepted_without_token = payment_payload.accepted.model_copy(
            update={
                "extra": {
                    key: value
                    for key, value in extra.items()
                    if key != self._CHALLENGE_FIELD
                }
            }
        )
        if accepted_without_token != server_requirements:
            raise PaymentRequirementsMismatchError(
                "PaymentPayload.accepted does not match signed server requirements"
            )
        return payment_payload.accepted

    def issue_intake_payment_required(
        self, quote: PriceQuote, *, description: str, mime_type: str
    ) -> tuple[PaymentRequired, PaymentRequirements]:
        return self.issue_payment_required(
            quote,
            description=description,
            mime_type=mime_type,
            payment_flow="upfront",
        )

    def issue_preview_payment_required(
        self, quote: PriceQuote, *, description: str, mime_type: str
    ) -> tuple[PaymentRequired, PaymentRequirements]:
        return self.issue_payment_required(
            quote,
            description=description,
            mime_type=mime_type,
            payment_flow="authorization",
        )

    @staticmethod
    def require_payment_identifier(payment_payload: PaymentPayload) -> str:
        payment_id, validation = extract_and_validate_payment_identifier(
            payment_payload
        )
        if payment_id is None or not validation.valid:
            raise MissingPaymentIdentifierError(
                "The required x402 payment-identifier extension is missing or invalid"
            )
        return payment_id

    def require_server_requirements(
        self,
        payment_payload: PaymentPayload,
        server_requirements: PaymentRequirements,
    ) -> None:
        matched = self.resource_server.find_matching_requirements(
            [server_requirements], payment_payload
        )
        if matched is None:
            raise PaymentRequirementsMismatchError(
                "PaymentPayload.accepted does not match server-issued requirements"
            )

    async def verify(
        self,
        payment_payload: PaymentPayload,
        server_requirements: PaymentRequirements,
        *,
        declared_extensions: dict,
    ):
        self.require_server_requirements(payment_payload, server_requirements)
        return await self.resource_server.verify_payment(
            payment_payload,
            server_requirements,
            declared_extensions=declared_extensions,
        )

    async def settle(
        self,
        payment_payload: PaymentPayload,
        server_requirements: PaymentRequirements,
        *,
        declared_extensions: dict,
    ) -> SettleResponse:
        self.require_server_requirements(payment_payload, server_requirements)
        return await self.resource_server.settle_payment(
            payment_payload,
            server_requirements,
            declared_extensions=declared_extensions,
        )
