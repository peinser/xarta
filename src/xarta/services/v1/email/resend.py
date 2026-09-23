from __future__ import annotations

import asyncio
import base64
import binascii
import datetime
import hashlib
import hmac

from collections.abc import Mapping
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import getaddresses
from math import isfinite
from typing import Any

import aiohttp
import orjson

from xarta.exceptions.protocol import TemporaryError
from xarta.execution import ExecutionMode
from xarta.protocol.dag import sanitize_outcome_message
from xarta.services.v1.email.adapters import BeforeSubmit
from xarta.services.v1.email.adapters import EmailCompletionMode
from xarta.services.v1.email.adapters import EmailSideEffectBoundary
from xarta.services.v1.email.adapters import EmailSubmission
from xarta.services.v1.email.adapters import EmailSubmissionPlan
from xarta.services.v1.email.tracking import initial_async_email_state
from xarta.tracking import AmbiguousSubmissionError

MAX_RESEND_RESPONSE_BYTES = 256 * 1024
MAX_RESEND_WEBHOOK_BYTES = 256 * 1024
MAX_RESEND_REQUEST_BYTES = 40 * 1024 * 1024
RESEND_SIGNATURE_TOLERANCE_SECONDS = 5 * 60


@dataclass(frozen=True, slots=True)
class ResendConfiguration:
    base_url: str
    api_key: str
    webhook_secret: str
    account_id: str
    timeout: float
    feedback_retention_seconds: int

    @classmethod
    def parse(cls, value: Mapping[str, Any]) -> ResendConfiguration:
        base_url = value.get("base_url", "https://api.resend.com")
        if not isinstance(base_url, str) or not base_url.startswith("https://"):
            raise ValueError("Resend base_url must be an HTTPS URL")
        api_key = value.get("api_key")
        webhook_secret = value.get("webhook_secret")
        account_id = value.get("account_id")
        for name, configured in (
            ("api_key", api_key),
            ("webhook_secret", webhook_secret),
            ("account_id", account_id),
        ):
            if not isinstance(configured, str) or not configured:
                raise ValueError(f"Resend configuration requires {name}")
        if not webhook_secret.startswith("whsec_"):
            raise ValueError("Resend webhook_secret must use the whsec_ format")
        try:
            decoded_secret = base64.b64decode(
                webhook_secret.removeprefix("whsec_"), validate=True
            )
        except (binascii.Error, ValueError) as ex:
            raise ValueError("Resend webhook_secret is not valid base64") from ex
        if len(decoded_secret) < 16:
            raise ValueError("Resend webhook_secret is too short")
        timeout = value.get("timeout", 30)
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, int | float)
            or not isfinite(float(timeout))
            or timeout <= 0
        ):
            raise ValueError("Resend timeout must be a positive number")
        retention = value.get("feedback_retention_seconds", 86_400)
        if (
            isinstance(retention, bool)
            or not isinstance(retention, int)
            or retention <= 0
        ):
            raise ValueError(
                "Resend feedback_retention_seconds must be a positive integer"
            )
        return cls(
            base_url.rstrip("/"),
            api_key,
            webhook_secret,
            account_id,
            float(timeout),
            retention,
        )


@dataclass(frozen=True, slots=True)
class ResendWebhookEvent:
    event_id: str
    event_type: str
    email_id: str
    recipients: tuple[str, ...]
    occurred_at: datetime.datetime
    data: Mapping[str, Any]


class ResendEmailAdapter:
    @property
    def execution_mode(self) -> ExecutionMode:
        return ExecutionMode.TRACKED

    def __init__(
        self, configuration: ResendConfiguration, session: aiohttp.ClientSession
    ) -> None:
        self.configuration = configuration
        self.session = session

    @property
    def provider_account_reference(self) -> str:
        return self.configuration.account_id

    def initial_state(self, recipients: list[str]) -> Mapping[str, Any]:
        return initial_async_email_state(recipients)

    def plan_submission(
        self,
        current_state: Mapping[str, Any],
        recipients: list[str],
        message: EmailMessage,
        now: datetime.datetime,
    ) -> EmailSubmissionPlan:
        state = (
            dict(current_state)
            if current_state.get("feedback_mode") == "asynchronous"
            else dict(self.initial_state(recipients))
        )
        fingerprint = self.submission_fingerprint(message)
        pinned_fingerprint = state.get("submission_fingerprint")
        if pinned_fingerprint is not None and pinned_fingerprint != fingerprint:
            raise AmbiguousSubmissionError(
                "Email content changed after idempotent submission began"
            )
        raw_started_at = state.get("submission_started_at")
        started_at = (
            datetime.datetime.fromisoformat(raw_started_at)
            if isinstance(raw_started_at, str)
            else now
        )
        valid_until = started_at + datetime.timedelta(hours=24)
        state.update(
            {
                "submission_started_at": started_at.isoformat(),
                "idempotency_valid_until": valid_until.isoformat(),
                "submission_fingerprint": fingerprint,
            }
        )
        return EmailSubmissionPlan(
            state,
            EmailSideEffectBoundary.CHECKPOINTED_IDEMPOTENCY,
            valid_until,
        )

    async def submit(
        self,
        *,
        idempotency_key: str,
        idempotency_valid_until: datetime.datetime | None,
        sender: str,
        recipients: list[str],
        message: EmailMessage,
        before_submit: BeforeSubmit,
    ) -> EmailSubmission:
        if idempotency_valid_until is None:
            raise ValueError("Resend submission requires an idempotency expiry")
        retry_deadline = idempotency_valid_until - datetime.timedelta(
            seconds=self.configuration.timeout + 60
        )
        if datetime.datetime.now(datetime.UTC) >= retry_deadline:
            raise AmbiguousSubmissionError(
                "Resend idempotency safety window expired before submission was reconciled"
            )
        payload, serialized_payload = self._serialize_payload(message)
        payload_recipients = {
            recipient
            for field in ("to", "cc", "bcc")
            for recipient in payload.get(field, [])
        }
        if {value.casefold() for value in payload_recipients} != set(recipients):
            raise ValueError(
                "Resend envelope recipients do not match tracked recipients"
            )
        await before_submit()
        try:
            async with asyncio.timeout(self.configuration.timeout):
                async with self.session.post(
                    f"{self.configuration.base_url}/emails",
                    data=serialized_payload,
                    headers={
                        "Authorization": f"Bearer {self.configuration.api_key}",
                        "Content-Type": "application/json",
                        "Idempotency-Key": idempotency_key,
                        "User-Agent": "xarta-email/1",
                    },
                ) as response:
                    body = await response.content.read(MAX_RESEND_RESPONSE_BYTES + 1)
                    if len(body) > MAX_RESEND_RESPONSE_BYTES:
                        raise TemporaryError("Resend response exceeded its size limit")
                    try:
                        parsed = self._json_object(body)
                    except ValueError:
                        if response.status == 429 or response.status >= 500:
                            raise TemporaryError(
                                "Temporary Resend failure returned malformed JSON",
                                delay=5,
                            )
                        if 200 <= response.status < 300:
                            raise TemporaryError(
                                "Resend accepted the request but omitted a usable response",
                                delay=5,
                            )
                        raise
                    if (
                        response.status == 409
                        and parsed.get("name") == "concurrent_idempotent_requests"
                    ):
                        raise TemporaryError(
                            "Concurrent Resend idempotent request", delay=5
                        )
                    if response.status == 429 or response.status >= 500:
                        raise TemporaryError(
                            "Temporary Resend submission failure", delay=5
                        )
                    if not 200 <= response.status < 300:
                        reason = sanitize_outcome_message(
                            str(parsed.get("message") or "Resend rejected the email")
                        )
                        raise ValueError(f"Resend submission rejected: {reason}")
        except (TimeoutError, aiohttp.ClientConnectionError) as ex:
            if datetime.datetime.now(datetime.UTC) < retry_deadline:
                raise TemporaryError(
                    "Resend submission result was not received; retrying the idempotency key",
                    delay=5,
                ) from ex
            raise AmbiguousSubmissionError(
                "Resend submission became uncertain after idempotency expiry"
            ) from ex
        provider_reference = parsed.get("id")
        if not isinstance(provider_reference, str) or not provider_reference:
            raise TemporaryError(
                "Resend success response omitted the email ID", delay=5
            )
        return EmailSubmission(
            provider_reference,
            {
                "feedback_mode": "asynchronous",
                "feedback_retention_seconds": self.configuration.feedback_retention_seconds,
            },
            EmailCompletionMode.FEEDBACK,
        )

    async def retrieve(self, provider_reference: str) -> Mapping[str, Any]:
        try:
            async with asyncio.timeout(self.configuration.timeout):
                async with self.session.get(
                    f"{self.configuration.base_url}/emails/{provider_reference}",
                    headers={
                        "Authorization": f"Bearer {self.configuration.api_key}",
                        "User-Agent": "xarta-email/1",
                    },
                ) as response:
                    body = await response.content.read(MAX_RESEND_RESPONSE_BYTES + 1)
                    if len(body) > MAX_RESEND_RESPONSE_BYTES:
                        raise TemporaryError("Resend response exceeded its size limit")
                    parsed = self._json_object(body)
                    if response.status == 429 or response.status >= 500:
                        raise TemporaryError("Temporary Resend reconciliation failure")
                    if not 200 <= response.status < 300:
                        raise ValueError("Resend email could not be retrieved")
                    return parsed
        except (TimeoutError, aiohttp.ClientConnectionError) as ex:
            raise TemporaryError("Temporary Resend reconciliation failure") from ex

    def authenticate_webhook(
        self,
        raw_body: bytes,
        headers: Mapping[str, str],
        *,
        now: datetime.datetime | None = None,
    ) -> ResendWebhookEvent:
        if not raw_body or len(raw_body) > MAX_RESEND_WEBHOOK_BYTES:
            raise ValueError("Resend webhook body is empty or exceeds its size limit")
        event_id = headers.get("svix-id") or headers.get("Svix-Id")
        raw_timestamp = headers.get("svix-timestamp") or headers.get("Svix-Timestamp")
        signatures = headers.get("svix-signature") or headers.get("Svix-Signature")
        if not event_id or not raw_timestamp or not signatures:
            raise PermissionError("Resend webhook signature headers are missing")
        try:
            timestamp = int(raw_timestamp)
        except ValueError as ex:
            raise PermissionError("Resend webhook timestamp is invalid") from ex
        observed_at = now or datetime.datetime.now(datetime.UTC)
        if (
            abs(observed_at.timestamp() - timestamp)
            > RESEND_SIGNATURE_TOLERANCE_SECONDS
        ):
            raise PermissionError("Resend webhook timestamp is outside tolerance")
        secret = base64.b64decode(
            self.configuration.webhook_secret.removeprefix("whsec_"), validate=True
        )
        signed = f"{event_id}.{raw_timestamp}.".encode() + raw_body
        expected = base64.b64encode(
            hmac.new(secret, signed, hashlib.sha256).digest()
        ).decode()
        valid = any(
            version == "v1" and hmac.compare_digest(signature, expected)
            for candidate in signatures.split()
            if "," in candidate
            for version, signature in (candidate.split(",", 1),)
        )
        if not valid:
            raise PermissionError("Invalid Resend webhook signature")
        payload = self._json_object(raw_body)
        event_type = payload.get("type")
        raw_occurred_at = payload.get("created_at")
        data = payload.get("data")
        if not isinstance(event_type, str) or not isinstance(data, Mapping):
            raise ValueError("Resend webhook envelope is invalid")
        email_id = data.get("email_id")
        recipients = data.get("to")
        if not isinstance(email_id, str) or not email_id:
            raise ValueError("Resend webhook requires data.email_id")
        if not isinstance(recipients, list) or any(
            not isinstance(recipient, str) or not recipient for recipient in recipients
        ):
            raise ValueError("Resend webhook requires recipient addresses")
        try:
            occurred_at = datetime.datetime.fromisoformat(str(raw_occurred_at))
        except ValueError as ex:
            raise ValueError("Resend webhook created_at is invalid") from ex
        if occurred_at.tzinfo is None:
            raise ValueError("Resend webhook created_at requires a timezone")
        return ResendWebhookEvent(
            event_id,
            event_type,
            email_id,
            tuple(dict.fromkeys(recipient.casefold() for recipient in recipients)),
            occurred_at,
            data,
        )

    @staticmethod
    def _payload(message: EmailMessage) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "from": str(message["From"]),
            "to": _addresses(message.get_all("To", [])),
            "subject": str(message["Subject"] or ""),
        }
        for header, field in (("Cc", "cc"), ("Bcc", "bcc"), ("Reply-To", "reply_to")):
            addresses = _addresses(message.get_all(header, []))
            if addresses:
                payload[field] = addresses
        plain = message.get_body(preferencelist=("plain",))
        html = message.get_body(preferencelist=("html",))
        if plain is not None:
            payload["text"] = plain.get_content()
        if html is not None:
            payload["html"] = html.get_content()
        attachments = []
        for attachment in message.iter_attachments():
            content = attachment.get_payload(decode=True) or b""
            encoded = base64.b64encode(content).decode()
            attachments.append(
                {
                    "filename": attachment.get_filename() or "attachment",
                    "content": encoded,
                    "content_type": attachment.get_content_type(),
                }
            )
        if attachments:
            payload["attachments"] = attachments
        if not payload.get("text") and not payload.get("html"):
            raise ValueError("Resend email requires text or HTML content")
        return payload

    @classmethod
    def submission_fingerprint(cls, message: EmailMessage) -> str:
        _, canonical = cls._serialize_payload(message)
        return hashlib.sha256(canonical).hexdigest()

    @classmethod
    def _serialize_payload(
        cls,
        message: EmailMessage,
        *,
        max_request_bytes: int = MAX_RESEND_REQUEST_BYTES,
    ) -> tuple[dict[str, Any], bytes]:
        payload = cls._payload(message)
        serialized = orjson.dumps(payload, option=orjson.OPT_SORT_KEYS)
        estimated_size = len(serialized)
        if estimated_size > max_request_bytes:
            raise ValueError(
                "Resend encoded JSON request exceeds its size limit: "
                f"estimated={estimated_size} bytes, limit={max_request_bytes} bytes; "
                "the estimate includes base64 attachments, text, HTML, addressing fields, and JSON encoding"
            )
        return payload, serialized

    @staticmethod
    def _json_object(body: bytes) -> Mapping[str, Any]:
        try:
            value = orjson.loads(body) if body else {}
        except orjson.JSONDecodeError as ex:
            raise ValueError("Resend response is not valid JSON") from ex
        if not isinstance(value, Mapping):
            raise ValueError("Resend JSON payload must be an object")
        return value


class ResendEmailAdapterFactory:
    def __init__(self, session: aiohttp.ClientSession) -> None:
        self.session = session

    def validate(self, configuration: Mapping[str, Any]) -> None:
        ResendConfiguration.parse(configuration)

    def create(self, configuration: Mapping[str, Any]) -> ResendEmailAdapter:
        return ResendEmailAdapter(
            ResendConfiguration.parse(configuration), self.session
        )


def _addresses(values: list[str]) -> list[str]:
    return [address for _, address in getaddresses(values) if address]
