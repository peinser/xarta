from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING
from typing import Any
from typing import Protocol

if TYPE_CHECKING:
    import datetime

    from collections.abc import Mapping
    from uuid import UUID


class ReceiverState(StrEnum):
    PENDING = "pending"
    PROVISIONING = "provisioning"
    PROVISIONED = "provisioned"
    UNCERTAIN = "uncertain"
    FAILED = "failed"


class DoccleResultCategory(StrEnum):
    STORED = "stored"
    PROVISIONED = "provisioned"
    RECEIVER_NOT_FOUND = "receiver_not_found"
    BUSINESS_REJECTION = "business_rejection"
    AUTHENTICATION_FAILURE = "authentication_failure"
    CONFIGURATION_ERROR = "configuration_error"
    PROTOCOL_ERROR = "protocol_error"


class DoccleTransportCategory(StrEnum):
    SAFE_PRETRANSMISSION = "safe_pretransmission"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True, slots=True)
class DoccleReceiverProfile:
    label: str
    first_name: str | None = None
    last_name: str | None = None
    email: str | None = None
    language: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.label, str) or not self.label.strip():
            raise ValueError("Doccle receiver label must be a non-empty string")
        for name in ("first_name", "last_name", "email", "language"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(
                    f"Doccle receiver {name} must be non-empty when provided"
                )


@dataclass(frozen=True, slots=True)
class DoccleDocument:
    document_id: str
    document_type: str
    filename: str
    content_type: str
    content: bytes
    names: Mapping[str, str] | None = None
    published_at: datetime.datetime | None = None


@dataclass(frozen=True, slots=True)
class DoccleResult:
    category: DoccleResultCategory
    provider_reference: str | None = None
    provider_status: str | None = None


class DoccleAdapter(Protocol):
    async def create_or_update_receiver(
        self, *, receiver_id: str, profile: DoccleReceiverProfile
    ) -> DoccleResult: ...

    async def put_document(
        self, *, receiver_id: str, document: DoccleDocument
    ) -> DoccleResult: ...


@dataclass(frozen=True, slots=True)
class DoccleReceiver:
    id: UUID
    destination: str
    subject: dict[str, Any]
    external_receiver_id: str
    state: ReceiverState
    linked: bool | None
    error: Any | None
    created_at: datetime.datetime
    updated_at: datetime.datetime
    provisioning_lease_token: UUID | None = None
    provisioning_lease_until: datetime.datetime | None = None
    linked_receipt_order: int | None = None


@dataclass(frozen=True, slots=True)
class ReceiverCallback:
    id: UUID
    destination: str
    callback_identity: str
    receiver_id: UUID
    external_receiver_id: str
    linked: bool
    payload: Any
    applied: bool
    received_at: datetime.datetime
    receipt_order: int = 0


@dataclass(frozen=True, slots=True)
class CallbackResult:
    receiver: DoccleReceiver
    callback: ReceiverCallback
    duplicate: bool
