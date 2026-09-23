from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from xarta.execution import ExecutionMode

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import date
    from typing import Any


class PeppolDocumentKind(StrEnum):
    INVOICE = "invoice"
    CREDIT_NOTE = "credit_note"


@dataclass(frozen=True)
class PeppolParticipant:
    scheme: str
    identifier: str

    def dict(self) -> dict[str, str]:
        return {"scheme": self.scheme, "identifier": self.identifier}


@dataclass(frozen=True)
class PeppolDocumentDescriptor:
    document_kind: str
    customization_id: str
    profile_id: str
    business_document_id: str
    issue_date: date
    sender: PeppolParticipant
    receiver: PeppolParticipant


@dataclass(frozen=True)
class PeppolValidationIssue:
    rule: str | None
    severity: str
    message: str


@dataclass(frozen=True)
class PeppolValidationResult:
    valid: bool
    issues: tuple[PeppolValidationIssue, ...] = ()


@dataclass(frozen=True)
class PeppolParticipantLookup:
    registered: bool
    message: str | None = None


class PeppolSubmissionMode(StrEnum):
    STAGED = "staged"
    DIRECT = "direct"


class PeppolDeliveryState(StrEnum):
    STAGED = "staged"
    SUBMITTED = "submitted"
    DELIVERY_CONFIRMED = "delivery_confirmed"
    DELIVERY_FAILED = "delivery_failed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class PeppolProviderDocument:
    provider: str
    id: str
    raw_state: str
    delivery_state: PeppolDeliveryState
    provider_code: str | None = None
    provider_message: str | None = None


@dataclass(frozen=True)
class EInvoiceBeWebhook:
    event_id: str
    event_type: str
    tenant_id: str
    provider_document_id: str
    occurred_at: int | None = None


class PeppolFailureStage(StrEnum):
    VALIDATION = "validation"
    PARTICIPANT_LOOKUP = "participant_lookup"
    DOCUMENT_CREATION = "document_creation"
    SUBMISSION = "submission"
    DELIVERY = "delivery"
    RECONCILIATION = "reconciliation"


class PeppolFailureCategory(StrEnum):
    INVALID_DOCUMENT = "invalid_document"
    RECIPIENT_NOT_REGISTERED = "recipient_not_registered"
    PROVIDER_REJECTED = "provider_rejected"
    DELIVERY_FAILED = "delivery_failed"
    AUTHENTICATION_FAILED = "authentication_failed"
    CONFIGURATION_ERROR = "configuration_error"
    RATE_LIMITED = "rate_limited"
    TRANSPORT_ERROR = "transport_error"
    AMBIGUOUS_RESULT = "ambiguous_result"
    PROVIDER_ERROR = "provider_error"


@dataclass(frozen=True)
class PeppolFailure:
    stage: PeppolFailureStage
    category: PeppolFailureCategory
    provider_code: str | None = None
    provider_message: str | None = None
    http_status: int | None = None
    provider: str = "peppol"


def is_retryable_read_failure(failure: PeppolFailure) -> bool:
    return failure.category in {
        PeppolFailureCategory.TRANSPORT_ERROR,
        PeppolFailureCategory.RATE_LIMITED,
    } or (
        failure.category is PeppolFailureCategory.PROVIDER_ERROR
        and (failure.http_status is None or failure.http_status >= 500)
    )


def is_ambiguous_write_failure(failure: PeppolFailure) -> bool:
    return failure.category is PeppolFailureCategory.TRANSPORT_ERROR or (
        failure.category is PeppolFailureCategory.PROVIDER_ERROR
        and (failure.http_status is None or failure.http_status >= 500)
    )


@dataclass(frozen=True)
class PeppolUpdate:
    provider_document: PeppolProviderDocument
    failure: PeppolFailure | None = None


class PeppolProviderError(Exception):
    def __init__(self, failure: PeppolFailure) -> None:
        super().__init__(failure.category.value)
        self.failure = failure


class PeppolAmbiguousError(PeppolProviderError):
    pass


class PeppolSenderNotConfiguredError(ValueError):
    def __init__(self, sender: PeppolParticipant) -> None:
        super().__init__(
            f"No tenant API key is configured for {sender.scheme}:{sender.identifier}"
        )
        self.sender = sender


class PeppolAdapter:
    @property
    def provider(self) -> str:
        raise NotImplementedError

    @property
    def execution_mode(self) -> ExecutionMode:
        raise NotImplementedError

    @property
    def submission_mode(self) -> PeppolSubmissionMode:
        raise NotImplementedError

    @property
    def account_references(self) -> tuple[str, ...]:
        raise NotImplementedError

    @property
    def provider_account_reference(self) -> str:
        raise NotImplementedError

    def bind_sender(self, sender: PeppolParticipant) -> PeppolAdapter:
        raise NotImplementedError

    def bind_account(self, account_reference: str) -> PeppolAdapter:
        raise NotImplementedError

    async def ensure_account_identity(self) -> None:
        raise NotImplementedError

    async def validate_ubl(self, document: bytes) -> PeppolValidationResult:
        raise NotImplementedError

    async def lookup_participant(
        self,
        participant: PeppolParticipant,
        descriptor: PeppolDocumentDescriptor,
    ) -> PeppolParticipantLookup:
        raise NotImplementedError

    async def create_document(self, document: bytes) -> PeppolProviderDocument:
        raise NotImplementedError

    async def send_document(
        self,
        provider_document_id: str,
        sender: PeppolParticipant,
        receiver: PeppolParticipant,
    ) -> PeppolProviderDocument:
        raise NotImplementedError

    async def submit_document(
        self, document: bytes, descriptor: PeppolDocumentDescriptor
    ) -> PeppolProviderDocument:
        raise NotImplementedError

    async def get_document(self, provider_document_id: str) -> PeppolProviderDocument:
        raise NotImplementedError
