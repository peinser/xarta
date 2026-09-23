from __future__ import annotations

import datetime
import re
import uuid

from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field
from enum import StrEnum
from typing import Any
from uuid import UUID

import orjson


class CommonOutcome(StrEnum):
    EXECUTION_FAILED = "execution_failed"
    RETRY_EXHAUSTED = "retry_exhausted"
    OUTCOME_UNCERTAIN = "outcome_uncertain"


COMMON_OUTCOMES = frozenset(outcome.value for outcome in CommonOutcome)
MAX_OUTCOME_DETAIL_MESSAGE_LENGTH = 4196
MAX_OUTCOME_DETAILS_BYTES = 64 * 1024
_OUTCOME_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def sanitize_outcome_message(message: str | None) -> str | None:
    if message is None:
        return None
    return _OUTCOME_CONTROL_CHARACTERS.sub("", message).strip()[
        :MAX_OUTCOME_DETAIL_MESSAGE_LENGTH
    ]


def validate_outcome_details(details: Mapping[str, Any] | None) -> None:
    if details is None:
        return
    if not isinstance(details, Mapping):
        raise TypeError("Outcome details must be a JSON object")
    try:
        encoded = orjson.dumps(details)
    except (TypeError, ValueError) as ex:
        raise ValueError("Outcome details must be JSON serializable") from ex
    if len(encoded) > MAX_OUTCOME_DETAILS_BYTES:
        raise ValueError("Outcome details exceed the size limit")


@dataclass(frozen=True)
class OutcomeSubject:
    kind: str
    id: str

    def dict(self) -> dict:
        return {"kind": self.kind, "id": self.id}

    @staticmethod
    def fromdict(data: Mapping[str, Any]) -> OutcomeSubject:
        return OutcomeSubject(kind=data["kind"], id=data["id"])


@dataclass(frozen=True)
class OutcomeEmission:
    outcome: str
    subject: OutcomeSubject | None = None
    details: Mapping[str, Any] | None = None
    occurred_at: datetime.datetime | None = None

    def __post_init__(self) -> None:
        validate_outcome_details(self.details)


@dataclass(frozen=True)
class CapabilityResult:
    outcomes: tuple[OutcomeEmission, ...] = ()
    waiting_feedback: bool = False
    outcomes_persisted: bool = False


@dataclass(frozen=True)
class OutcomeEvent:
    flow_id: UUID
    node_execution_id: UUID
    outcome: str
    source: str
    id: UUID = field(default_factory=uuid.uuid4)
    tracked_operation_id: UUID | None = None
    subject: OutcomeSubject | None = None
    details: Mapping[str, Any] | None = None
    external_event_id: str | None = None
    occurred_at: datetime.datetime | None = None
    received_at: datetime.datetime = field(
        default_factory=lambda: datetime.datetime.now(datetime.UTC)
    )

    def __post_init__(self) -> None:
        validate_outcome_details(self.details)
