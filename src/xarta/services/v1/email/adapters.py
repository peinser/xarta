from __future__ import annotations

import inspect
import re

from collections.abc import Awaitable
from collections.abc import Callable
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from math import isfinite
from types import MappingProxyType
from typing import TYPE_CHECKING
from typing import Any
from typing import Protocol

import aiosmtplib

from xarta.exceptions.protocol import TemporaryError
from xarta.protocol.dag import OutcomeEmission
from xarta.protocol.dag import OutcomeSubject
from xarta.protocol.dag import sanitize_outcome_message
from xarta.protocol.dag.email import EmailOutcome
from xarta.tracking import AmbiguousSubmissionError

if TYPE_CHECKING:
    import datetime

    from email.message import EmailMessage

    from xarta.services.v1.email.tracking import EmailUpdate


BeforeSubmit = Callable[[], Awaitable[None]]
MAX_RECORDED_SMTP_RECIPIENT_ERRORS = 20


class EmailCompletionMode(StrEnum):
    IMMEDIATE = "immediate"
    FEEDBACK = "feedback"


class EmailSideEffectBoundary(StrEnum):
    CHECKPOINTED_IDEMPOTENCY = "checkpointed_idempotency"
    MARK_UNCERTAIN = "mark_uncertain"


@dataclass(frozen=True)
class EmailSubmissionPlan:
    state: Mapping[str, Any]
    side_effect_boundary: EmailSideEffectBoundary
    idempotency_valid_until: datetime.datetime | None = None


@dataclass(frozen=True)
class EmailSubmission:
    provider_reference: str | None
    state: Mapping[str, Any]
    completion: EmailCompletionMode = EmailCompletionMode.IMMEDIATE


class EmailAdapter(Protocol):
    """Mandatory interface for adapters which submit email."""

    @property
    def execution_mode(self) -> ExecutionMode: ...

    @property
    def provider_account_reference(self) -> str | None: ...

    def initial_state(self, recipients: list[str]) -> Mapping[str, Any]: ...

    def plan_submission(
        self,
        current_state: Mapping[str, Any],
        recipients: list[str],
        message: EmailMessage,
        now: datetime.datetime,
    ) -> EmailSubmissionPlan: ...

    async def submit(
        self,
        *,
        idempotency_key: str,
        idempotency_valid_until: datetime.datetime | None,
        sender: str,
        recipients: list[str],
        message: EmailMessage,
        before_submit: BeforeSubmit,
    ) -> EmailSubmission: ...


class EmailFeedbackAdapter(EmailAdapter, Protocol):
    """Optional interface for adapters which consume provider feedback."""

    def parse_feedback(
        self, payload: bytes, headers: Mapping[str, str]
    ) -> EmailUpdate: ...


class EmailReconcileAdapter(EmailAdapter, Protocol):
    """Optional interface for adapters which reconcile provider state."""

    async def reconcile(self, provider_reference: str) -> list[EmailUpdate]: ...


def smtp_responses(error: aiosmtplib.errors.SMTPException) -> list[dict]:
    failures = getattr(error, "recipients", None) or [error]
    responses = []
    for failure in failures:
        response = getattr(failure, "message", None)
        if isinstance(response, bytes):
            response = response.decode(errors="replace")
        response = str(response or failure)
        enhanced_match = re.search(
            r"(?<![\d.])([245]\.\d{1,3}\.\d{1,3})(?![\d.])", response
        )
        responses.append(
            {
                "recipient": getattr(failure, "recipient", None),
                "smtp_code": getattr(failure, "code", None),
                "enhanced_status": (
                    enhanced_match.group(1) if enhanced_match else None
                ),
                "provider_message": sanitize_outcome_message(response),
            }
        )
    return responses


def classify_smtp_error(
    error: aiosmtplib.errors.SMTPException,
) -> tuple[OutcomeEmission | None, bool]:
    responses = smtp_responses(error)
    code = responses[0]["smtp_code"]

    recipient_failure = isinstance(
        error,
        aiosmtplib.errors.SMTPRecipientRefused
        | aiosmtplib.errors.SMTPRecipientsRefused,
    )
    message_failure = isinstance(error, aiosmtplib.errors.SMTPDataError)
    transport_failure = isinstance(
        error,
        aiosmtplib.errors.SMTPConnectError
        | aiosmtplib.errors.SMTPServerDisconnected
        | aiosmtplib.errors.SMTPTimeoutError,
    )

    if not recipient_failure and not message_failure:
        return None, transport_failure or (code is not None and 400 <= code < 500)

    outcomes = set()
    for response in responses:
        enhanced_status = response["enhanced_status"]
        components = enhanced_status.split(".") if enhanced_status else []
        if recipient_failure and components in (["4", "2", "2"], ["5", "2", "2"]):
            outcomes.add(EmailOutcome.MAILBOX_FULL)
        elif recipient_failure and components == ["5", "1", "1"]:
            outcomes.add(EmailOutcome.RECIPIENT_UNKNOWN)
        else:
            outcomes.add(EmailOutcome.MESSAGE_REJECTED)

    outcome = outcomes.pop() if len(outcomes) == 1 else EmailOutcome.MESSAGE_REJECTED
    recorded_responses = responses[:MAX_RECORDED_SMTP_RECIPIENT_ERRORS]
    details = {
        "smtp_code": responses[0]["smtp_code"],
        "enhanced_status": responses[0]["enhanced_status"],
        "provider_message": responses[0]["provider_message"],
        "recipient_errors": recorded_responses,
        "total_recipient_error_count": len(responses),
        "recorded_recipient_error_count": len(recorded_responses),
        "truncated": len(recorded_responses) != len(responses),
    }
    result = OutcomeEmission(
        outcome=outcome.value,
        subject=(
            OutcomeSubject(kind="recipient", id=responses[0]["recipient"])
            if len(responses) == 1 and responses[0]["recipient"]
            else None
        ),
        details=details,
    )
    retryable = any(
        response["smtp_code"] is not None and 400 <= response["smtp_code"] < 500
        for response in responses
    )
    return result, retryable


class SMTPEmailAdapter:
    @property
    def execution_mode(self) -> ExecutionMode:
        return ExecutionMode.SYNCHRONOUS

    @property
    def provider_account_reference(self) -> None:
        return None

    def initial_state(self, recipients: list[str]) -> Mapping[str, Any]:
        from xarta.services.v1.email.tracking import initial_email_state

        return initial_email_state(recipients)

    def plan_submission(
        self,
        current_state: Mapping[str, Any],
        recipients: list[str],
        message: EmailMessage,
        now: datetime.datetime,
    ) -> EmailSubmissionPlan:
        return EmailSubmissionPlan(
            self.initial_state(recipients), EmailSideEffectBoundary.MARK_UNCERTAIN
        )

    def __init__(self, configuration: Mapping[str, Any]) -> None:
        SMTPEmailAdapterFactory().validate(configuration)
        self._server = MappingProxyType(dict(configuration["server"]))
        self._password = configuration["password"]

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
        outcomes = await self._send(sender, recipients, message, before_submit)
        return EmailSubmission(
            provider_reference=idempotency_key,
            state={
                "submission_outcomes": [
                    {
                        "outcome": outcome.outcome,
                        "subject": outcome.subject.dict() if outcome.subject else None,
                        "details": outcome.details,
                    }
                    for outcome in outcomes
                ]
            },
            completion=EmailCompletionMode.IMMEDIATE,
        )

    async def _send(
        self,
        sender: str,
        recipients: list[str],
        message: EmailMessage,
        before_submit: BeforeSubmit,
    ) -> tuple[OutcomeEmission, ...]:
        client = aiosmtplib.SMTP(**self._server)
        recipient_errors: dict[str, Any] = {}
        send_completed = False

        try:
            async with client:
                await client.login(sender, self._password)
                await before_submit()
                try:
                    recipient_errors, _ = await client.send_message(
                        message, recipients=recipients
                    )
                    send_completed = True
                except (
                    aiosmtplib.errors.SMTPServerDisconnected,
                    aiosmtplib.errors.SMTPTimeoutError,
                ) as ex:
                    raise AmbiguousSubmissionError from ex
        except aiosmtplib.errors.SMTPException as ex:
            if send_completed:
                pass
            elif isinstance(ex, aiosmtplib.errors.SMTPRecipientsRefused):
                outcomes = []
                retryable_outcome = None
                for refusal in ex.recipients:
                    outcome, retryable = classify_smtp_error(refusal)
                    if outcome is not None:
                        outcomes.append(outcome)
                        if retryable:
                            retryable_outcome = outcome
                if retryable_outcome is not None:
                    raise TemporaryError(outcome=retryable_outcome) from ex
                return tuple(outcomes)
            else:
                result, retryable = classify_smtp_error(ex)
                if retryable:
                    raise TemporaryError(outcome=result) from ex
                if result is not None:
                    return (result,)
                raise

        if recipient_errors:
            outcomes = []
            for recipient, response in recipient_errors.items():
                outcome, _ = classify_smtp_error(
                    aiosmtplib.errors.SMTPRecipientRefused(
                        response.code, response.message, recipient
                    )
                )
                if outcome is not None:
                    outcomes.append(outcome)
            rejected = set(recipient_errors)
            outcomes.extend(
                OutcomeEmission(
                    outcome=EmailOutcome.ACCEPTED.value,
                    subject=OutcomeSubject(kind="recipient", id=recipient),
                )
                for recipient in recipients
                if recipient not in rejected
            )
            return tuple(outcomes)

        return tuple(
            OutcomeEmission(
                outcome=EmailOutcome.ACCEPTED.value,
                subject=OutcomeSubject(kind="recipient", id=recipient),
            )
            for recipient in recipients
        )


class SMTPEmailAdapterFactory:
    """Validates SMTP configuration without opening a connection."""

    def validate(self, configuration: Mapping[str, Any]) -> None:
        server = configuration.get("server")
        if not isinstance(server, Mapping):
            raise ValueError("SMTP destination requires a server object")
        hostname = server.get("hostname")
        if not isinstance(hostname, str) or not hostname:
            raise ValueError("SMTP destination requires a non-empty hostname")
        password = configuration.get("password")
        if not isinstance(password, str) or not password:
            raise ValueError("SMTP destination requires a non-empty password")
        port = server.get("port", 25)
        if (
            isinstance(port, bool)
            or not isinstance(port, int)
            or not 1 <= port <= 65535
        ):
            raise ValueError("SMTP destination port must be between 1 and 65535")
        for name in ("start_tls", "use_tls"):
            if name in server and not isinstance(server[name], bool):
                raise ValueError(f"SMTP destination {name} must be a boolean")
        if server.get("start_tls") and server.get("use_tls"):
            raise ValueError(
                "SMTP destination cannot enable start_tls and use_tls together"
            )
        timeout = server.get("timeout")
        if timeout is not None:
            if isinstance(timeout, bool) or not isinstance(timeout, int | float):
                raise ValueError("SMTP destination timeout must be a positive number")
            if not isfinite(float(timeout)) or timeout <= 0:
                raise ValueError("SMTP destination timeout must be a positive number")
        try:
            inspect.signature(aiosmtplib.SMTP).bind(**dict(server))
        except TypeError as ex:
            raise ValueError(f"Invalid SMTP server configuration: {ex}") from ex

    def create(self, configuration: Mapping[str, Any]) -> SMTPEmailAdapter:
        return SMTPEmailAdapter(configuration)


from xarta.execution import ExecutionMode
