from __future__ import annotations

import aiosmtplib

from xarta.protocol.dag.email import EmailOutcome
from xarta.services.v1.email.nats import _classify_smtp_error


def test_email_maps_recipient_provider_details_to_semantic_outcome() -> None:
    refusal = aiosmtplib.errors.SMTPRecipientRefused(
        550, "5.1.1 Recipient does not exist", "missing@example.com"
    )
    result, retryable = _classify_smtp_error(
        aiosmtplib.errors.SMTPRecipientsRefused([refusal])
    )

    assert result is not None
    assert result.outcome == EmailOutcome.RECIPIENT_UNKNOWN
    assert result.details is not None
    assert result.details["smtp_code"] == 550
    assert retryable is False


def test_email_temporary_mailbox_full_carries_terminal_semantics() -> None:
    error = aiosmtplib.errors.SMTPRecipientRefused(
        452, "4.2.2 Mailbox temporarily full", "full@example.com"
    )

    result, retryable = _classify_smtp_error(error)

    assert result is not None
    assert result.outcome == EmailOutcome.MAILBOX_FULL
    assert retryable is True


def test_email_bare_recipient_codes_are_only_message_rejections() -> None:
    for code in (452, 550):
        error = aiosmtplib.errors.SMTPRecipientRefused(
            code, "Recipient rejected", "recipient@example.com"
        )

        result, retryable = _classify_smtp_error(error)

        assert result is not None
        assert result.outcome == EmailOutcome.MESSAGE_REJECTED
        assert retryable is (code == 452)


def test_email_parses_extended_enhanced_status_components_without_prefix_matches() -> (
    None
):
    extended = aiosmtplib.errors.SMTPRecipientRefused(
        550, "5.101.123 Recipient rejected", "recipient@example.com"
    )
    near_unknown = aiosmtplib.errors.SMTPRecipientRefused(
        550, "5.1.10 Recipient rejected", "recipient@example.com"
    )
    near_full = aiosmtplib.errors.SMTPRecipientRefused(
        550, "5.2.20 Recipient rejected", "recipient@example.com"
    )

    result, _ = _classify_smtp_error(extended)
    assert result is not None
    assert result.details is not None
    assert result.details["enhanced_status"] == "5.101.123"
    assert result.outcome == EmailOutcome.MESSAGE_REJECTED

    for error in (near_unknown, near_full):
        result, _ = _classify_smtp_error(error)
        assert result is not None
        assert result.outcome == EmailOutcome.MESSAGE_REJECTED


def test_email_transport_and_authentication_failures_are_not_domain_outcomes() -> None:
    disconnected = aiosmtplib.errors.SMTPServerDisconnected("Connection lost")
    auth = aiosmtplib.errors.SMTPAuthenticationError(535, "Authentication failed")

    assert _classify_smtp_error(disconnected) == (None, True)
    assert _classify_smtp_error(auth) == (None, False)
