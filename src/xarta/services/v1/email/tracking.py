from __future__ import annotations

import datetime

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING
from typing import Any

from xarta.protocol.dag import OutcomeEmission
from xarta.protocol.dag import OutcomeSubject
from xarta.protocol.dag.email import EmailOutcome
from xarta.tracking import Reduction

if TYPE_CHECKING:
    from collections.abc import Mapping


class EmailRecipientStatus(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    DELIVERED = "delivered"
    MAILBOX_FULL = "mailbox_full"
    RECIPIENT_UNKNOWN = "recipient_unknown"
    MESSAGE_REJECTED = "message_rejected"
    DELIVERY_DELAYED = "delivery_delayed"
    COMPLAINED = "complained"


class AsyncEmailEventType(StrEnum):
    ACCEPTED = "accepted"
    DELIVERY_DELAYED = "delivery_delayed"
    DELIVERED = "delivered"
    DELIVERY_FAILED = "delivery_failed"
    COMPLAINED = "complained"
    CLOSE = "close"


FINAL_RECIPIENT_STATUSES = frozenset(
    {
        EmailRecipientStatus.DELIVERED,
        EmailRecipientStatus.MAILBOX_FULL,
        EmailRecipientStatus.RECIPIENT_UNKNOWN,
        EmailRecipientStatus.MESSAGE_REJECTED,
    }
)


@dataclass(frozen=True)
class EmailUpdate:
    recipient: str
    status: EmailRecipientStatus
    details: Mapping[str, Any] | None = None
    occurred_at: datetime.datetime | None = None
    provider_sequence: int | None = None


@dataclass(frozen=True)
class AsyncEmailUpdate:
    event_type: AsyncEmailEventType
    recipients: tuple[str, ...] = ()
    failure_status: EmailRecipientStatus | None = None
    details: Mapping[str, Any] | None = None
    occurred_at: datetime.datetime | None = None
    provider_message_id: str | None = None


def initial_email_state(recipients: list[str]) -> dict:
    return {
        "recipients": dict.fromkeys(
            dict.fromkeys(recipients), EmailRecipientStatus.PENDING.value
        ),
        "provider_sequences": {},
        "aggregate_emitted": None,
    }


def initial_async_email_state(recipients: list[str]) -> dict:
    return {
        "feedback_mode": "asynchronous",
        "recipients": {
            recipient: {
                "accepted": False,
                "delivery": "pending",
                "delayed": False,
                "complained": False,
                "emitted": [],
            }
            for recipient in dict.fromkeys(recipients)
        },
        "delivery_aggregate_emitted": None,
        "acceptance_aggregate_emitted": None,
        "contradictions": [],
        "closed": False,
    }


def reduce_async_email(
    current_state: Mapping[str, Any], update: AsyncEmailUpdate
) -> Reduction:
    state = dict(current_state)
    if update.provider_message_id:
        existing_message_id = state.get("smtp_message_id")
        if existing_message_id is None:
            state["smtp_message_id"] = update.provider_message_id
        elif existing_message_id != update.provider_message_id:
            contradictions = list(state.get("contradictions", []))
            contradictions.append(
                {
                    "observed": "provider_message_id_changed",
                    "expected": existing_message_id,
                    "received": update.provider_message_id,
                }
            )
            state["contradictions"] = contradictions[-20:]
    recipients = {
        recipient: dict(value)
        for recipient, value in current_state["recipients"].items()
    }
    state["recipients"] = recipients
    if update.event_type is AsyncEmailEventType.CLOSE:
        if not state.get("delivery_aggregate_emitted"):
            return Reduction(state=state, outcomes=(), operation_resolved=False)
        state["closed"] = True
        return Reduction(state=state, outcomes=(), operation_resolved=True)

    unknown = set(update.recipients) - set(recipients)
    if unknown:
        raise ValueError(f"Unknown email recipients: {', '.join(sorted(unknown))}")
    emissions = []
    contradictions = list(state.get("contradictions", []))
    for recipient in update.recipients:
        recipient_state = recipients[recipient]
        emitted = set(recipient_state.get("emitted", []))

        if (
            update.event_type is not AsyncEmailEventType.ACCEPTED
            and not recipient_state["accepted"]
        ):
            recipient_state["accepted"] = True
            _emit_async_once(emissions, emitted, recipient, "accepted", update)

        if update.event_type is AsyncEmailEventType.ACCEPTED:
            recipient_state["accepted"] = True
            _emit_async_once(emissions, emitted, recipient, "accepted", update)
        elif update.event_type is AsyncEmailEventType.DELIVERY_DELAYED:
            if recipient_state["delivery"] not in {"delivered", "failed"}:
                recipient_state["delivery"] = "delayed"
                recipient_state["delayed"] = True
                _emit_async_once(
                    emissions, emitted, recipient, "delivery_delayed", update
                )
        elif update.event_type is AsyncEmailEventType.DELIVERED:
            if recipient_state["delivery"] == "failed":
                contradictions.append(
                    {"recipient": recipient, "observed": "delivered_after_failure"}
                )
            else:
                recipient_state["delivery"] = "delivered"
                _emit_async_once(emissions, emitted, recipient, "delivered", update)
        elif update.event_type is AsyncEmailEventType.DELIVERY_FAILED:
            failure = update.failure_status or EmailRecipientStatus.MESSAGE_REJECTED
            if failure not in FINAL_RECIPIENT_STATUSES - {
                EmailRecipientStatus.DELIVERED
            }:
                raise ValueError("Invalid asynchronous email failure status")
            if recipient_state["delivery"] == "delivered":
                contradictions.append(
                    {"recipient": recipient, "observed": "failure_after_delivered"}
                )
            else:
                recipient_state["delivery"] = "failed"
                recipient_state["failure_status"] = failure.value
                _emit_async_once(emissions, emitted, recipient, failure.value, update)
        elif update.event_type is AsyncEmailEventType.COMPLAINED:
            if recipient_state["delivery"] == "failed":
                contradictions.append(
                    {"recipient": recipient, "observed": "complaint_after_failure"}
                )
            else:
                recipient_state["delivery"] = "delivered"
                recipient_state["complained"] = True
                _emit_async_once(emissions, emitted, recipient, "delivered", update)
                _emit_async_once(emissions, emitted, recipient, "complained", update)
        recipient_state["emitted"] = sorted(emitted)

    state["contradictions"] = contradictions[-20:]
    if not state.get("acceptance_aggregate_emitted") and all(
        recipient["accepted"] for recipient in recipients.values()
    ):
        state["acceptance_aggregate_emitted"] = EmailOutcome.ALL_ACCEPTED.value
        emissions.append(OutcomeEmission(EmailOutcome.ALL_ACCEPTED.value))
    if not state.get("delivery_aggregate_emitted") and _async_delivery_settled(
        recipients
    ):
        aggregate = _async_aggregate(recipients)
        state["delivery_aggregate_emitted"] = aggregate
        retention = int(state.get("feedback_retention_seconds", 86_400))
        state["feedback_closes_at"] = (
            datetime.datetime.now(datetime.UTC) + datetime.timedelta(seconds=retention)
        ).isoformat()
        emissions.append(OutcomeEmission(aggregate))
    return Reduction(state=state, outcomes=tuple(emissions), operation_resolved=False)


def reduce_email(current_state: Mapping[str, Any], update: EmailUpdate) -> Reduction:
    recipients = dict(current_state["recipients"])
    sequences = dict(current_state.get("provider_sequences", {}))
    if update.recipient not in recipients:
        raise ValueError(f"Unknown email recipient: {update.recipient}")
    if update.status is EmailRecipientStatus.PENDING:
        raise ValueError("pending is internal state, not provider feedback")

    previous_sequence = sequences.get(update.recipient)
    if (
        update.provider_sequence is not None
        and previous_sequence is not None
        and update.provider_sequence <= previous_sequence
    ):
        return Reduction(
            state=dict(current_state),
            outcomes=(),
            operation_resolved=_email_resolved(recipients),
        )

    previous = EmailRecipientStatus(recipients[update.recipient])
    if previous in FINAL_RECIPIENT_STATUSES and previous != update.status:
        return Reduction(
            state=dict(current_state),
            outcomes=(),
            operation_resolved=_email_resolved(recipients),
        )
    if previous == update.status:
        if update.provider_sequence is not None:
            sequences[update.recipient] = update.provider_sequence
            state = dict(current_state)
            state["provider_sequences"] = sequences
        else:
            state = dict(current_state)
        return Reduction(
            state=state,
            outcomes=(),
            operation_resolved=_email_resolved(recipients),
        )

    recipients[update.recipient] = update.status.value
    if update.provider_sequence is not None:
        sequences[update.recipient] = update.provider_sequence

    emissions = [
        OutcomeEmission(
            outcome=update.status.value,
            subject=OutcomeSubject(kind="recipient", id=update.recipient),
            details=update.details,
            occurred_at=update.occurred_at,
        )
    ]
    aggregate_emitted = current_state.get("aggregate_emitted")
    operation_resolved = _email_resolved(recipients)
    if operation_resolved and aggregate_emitted is None:
        aggregate_emitted = _aggregate_outcome(recipients)
        emissions.append(OutcomeEmission(outcome=aggregate_emitted))

    return Reduction(
        state={
            "recipients": recipients,
            "provider_sequences": sequences,
            "aggregate_emitted": aggregate_emitted,
        },
        outcomes=tuple(emissions),
        operation_resolved=operation_resolved,
    )


def reduce_smtp_submission(
    current_state: Mapping[str, Any], emissions: tuple[OutcomeEmission, ...]
) -> Reduction:
    recipients = dict(current_state["recipients"])
    for emission in emissions:
        if emission.subject is None:
            for recipient in recipients:
                recipients[recipient] = emission.outcome
            continue
        recipients[emission.subject.id] = emission.outcome

    accepted = sum(
        status == EmailRecipientStatus.ACCEPTED.value for status in recipients.values()
    )
    if accepted == len(recipients):
        aggregate = EmailOutcome.ALL_ACCEPTED.value
    elif accepted == 0:
        aggregate = EmailOutcome.ALL_FAILED.value
    else:
        aggregate = EmailOutcome.PARTIALLY_ACCEPTED.value

    return Reduction(
        state={
            **current_state,
            "recipients": recipients,
            "aggregate_emitted": aggregate,
        },
        outcomes=(*emissions, OutcomeEmission(outcome=aggregate)),
        operation_resolved=True,
    )


def _email_resolved(recipients: Mapping[str, str]) -> bool:
    return bool(recipients) and all(
        EmailRecipientStatus(status) in FINAL_RECIPIENT_STATUSES
        for status in recipients.values()
    )


def _aggregate_outcome(recipients: Mapping[str, str]) -> str:
    delivered = sum(
        status == EmailRecipientStatus.DELIVERED.value for status in recipients.values()
    )
    if delivered == len(recipients):
        return EmailOutcome.ALL_DELIVERED.value
    if delivered == 0:
        return EmailOutcome.ALL_FAILED.value
    return EmailOutcome.PARTIALLY_DELIVERED.value


def _async_delivery_settled(recipients: Mapping[str, Mapping[str, Any]]) -> bool:
    return bool(recipients) and all(
        state["delivery"] in {"delivered", "failed"} for state in recipients.values()
    )


def _async_aggregate(recipients: Mapping[str, Mapping[str, Any]]) -> str:
    delivered = sum(state["delivery"] == "delivered" for state in recipients.values())
    if delivered == len(recipients):
        return EmailOutcome.ALL_DELIVERED.value
    if delivered == 0:
        return EmailOutcome.ALL_FAILED.value
    return EmailOutcome.PARTIALLY_DELIVERED.value


def _emit_async_once(
    emissions: list[OutcomeEmission],
    emitted: set[str],
    recipient: str,
    outcome: str,
    update: AsyncEmailUpdate,
) -> None:
    if outcome in emitted:
        return
    emissions.append(
        OutcomeEmission(
            outcome=outcome,
            subject=OutcomeSubject("recipient", recipient),
            details=update.details,
            occurred_at=update.occurred_at,
        )
    )
    emitted.add(outcome)
