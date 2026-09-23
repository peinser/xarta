from __future__ import annotations

from dataclasses import dataclass

from x402 import SettleResponse
from x402.http import decode_payment_signature_header
from x402.http import encode_payment_required_header
from x402.http import encode_payment_response_header
from x402.schemas import PaymentPayload
from x402.schemas import PaymentRequired

PAYMENT_REQUIRED_HEADER = "PAYMENT-REQUIRED"
PAYMENT_SIGNATURE_HEADER = "PAYMENT-SIGNATURE"
PAYMENT_RESPONSE_HEADER = "PAYMENT-RESPONSE"


class MalformedPaymentHeaderError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class X402HTTPTransport:
    def read_payment_payload(self, headers) -> PaymentPayload | None:
        encoded = headers.get(PAYMENT_SIGNATURE_HEADER)
        if encoded is None:
            return None
        try:
            payload = decode_payment_signature_header(encoded)
        except Exception as ex:
            raise MalformedPaymentHeaderError(
                "PAYMENT-SIGNATURE is not a valid x402 v2 payload"
            ) from ex
        if not isinstance(payload, PaymentPayload) or payload.x402_version != 2:
            raise MalformedPaymentHeaderError("Only x402 v2 PaymentPayload is accepted")
        return payload

    def payment_required_header(self, payment_required: PaymentRequired) -> str:
        return encode_payment_required_header(payment_required)

    def payment_response_header(self, settlement: SettleResponse) -> str:
        return encode_payment_response_header(settlement)
