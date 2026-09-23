from __future__ import annotations

import asyncio
import hashlib
import hmac

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import aiohttp
import orjson

from xarta.execution import ExecutionMode
from xarta.services.v1.peppol.inspection import MAX_PEPPOL_DOCUMENT_BYTES
from xarta.services.v1.peppol.models import PeppolAmbiguousError
from xarta.services.v1.peppol.models import PeppolDeliveryState
from xarta.services.v1.peppol.models import PeppolDocumentDescriptor
from xarta.services.v1.peppol.models import PeppolFailure
from xarta.services.v1.peppol.models import PeppolFailureCategory
from xarta.services.v1.peppol.models import PeppolFailureStage
from xarta.services.v1.peppol.models import PeppolParticipant
from xarta.services.v1.peppol.models import PeppolParticipantLookup
from xarta.services.v1.peppol.models import PeppolProviderDocument
from xarta.services.v1.peppol.models import PeppolProviderError
from xarta.services.v1.peppol.models import PeppolSenderNotConfiguredError
from xarta.services.v1.peppol.models import PeppolSubmissionMode
from xarta.services.v1.peppol.models import PeppolValidationResult
from xarta.services.v1.peppol.models import is_ambiguous_write_failure
from xarta.services.v1.peppol.profiles import document_type_identifier

from .configuration import RecommandCompanyConfiguration
from .configuration import RecommandConfiguration

MAX_PROVIDER_RESPONSE_BYTES = MAX_PEPPOL_DOCUMENT_BYTES + 256 * 1024
MAX_WEBHOOK_BODY_BYTES = 64 * 1024


@dataclass(frozen=True)
class RecommandWebhook:
    event_id: str
    company_id: str
    provider_document_id: str


def parse_untrusted_recommand_webhook(raw_body: bytes) -> tuple[Mapping[str, Any], str]:
    if not raw_body or len(raw_body) > MAX_WEBHOOK_BODY_BYTES:
        raise ValueError("Webhook body is empty or exceeds its size limit")
    try:
        payload = orjson.loads(raw_body)
    except orjson.JSONDecodeError as ex:
        raise ValueError("Webhook body is not valid JSON") from ex
    if not isinstance(payload, Mapping):
        raise ValueError("Webhook body must be an object")
    company_id = payload.get("companyId")
    if not isinstance(company_id, str) or not company_id:
        raise ValueError("Webhook requires companyId")
    return payload, company_id


class RecommandPeppolAdapter:
    provider = "recommand"
    execution_mode = ExecutionMode.TRACKED
    submission_mode = PeppolSubmissionMode.DIRECT

    def __init__(
        self, configuration: RecommandConfiguration, session: aiohttp.ClientSession
    ) -> None:
        self.configuration = configuration
        self.session = session

    @property
    def account_references(self) -> tuple[str, ...]:
        return tuple(self.configuration.companies_by_id)

    def bind_sender(self, sender: PeppolParticipant) -> RecommandCompanyClient:
        company = self.configuration.companies_by_participant.get(sender)
        if company is None:
            raise PeppolSenderNotConfiguredError(sender)
        return RecommandCompanyClient(self, company)

    def bind_account(self, account_reference: str) -> RecommandCompanyClient:
        try:
            company = self.configuration.companies_by_id[account_reference]
        except KeyError as ex:
            raise KeyError(f"Unknown Recommand company: {account_reference}") from ex
        return RecommandCompanyClient(self, company)

    def authenticate_webhook(
        self,
        raw_body: bytes,
        payload: Mapping[str, Any],
        supplied_signature: str | None,
        event_id: str | None,
    ) -> RecommandWebhook:
        expected = (
            "sha256="
            + hmac.new(
                self.configuration.webhook_secret.encode(), raw_body, hashlib.sha256
            ).hexdigest()
        )
        if not supplied_signature or not hmac.compare_digest(
            supplied_signature, expected
        ):
            raise PermissionError("Invalid Recommand webhook signature")
        if not event_id:
            raise ValueError("Recommand webhook requires X-Idempotency-Key")
        company_id = payload.get("companyId")
        document_id = payload.get("documentId")
        if not isinstance(company_id, str) or not company_id:
            raise ValueError("Recommand webhook requires companyId")
        if not isinstance(document_id, str) or not document_id:
            raise ValueError("Recommand webhook requires documentId")
        return RecommandWebhook(event_id, company_id, document_id)


@dataclass(frozen=True)
class RecommandCompanyClient:
    adapter: RecommandPeppolAdapter
    company: RecommandCompanyConfiguration

    @property
    def provider(self) -> str:
        return self.adapter.provider

    @property
    def execution_mode(self) -> ExecutionMode:
        return self.adapter.execution_mode

    @property
    def submission_mode(self) -> PeppolSubmissionMode:
        return self.adapter.submission_mode

    @property
    def provider_account_reference(self) -> str:
        return self.company.company_id

    def bind_sender(self, sender: PeppolParticipant) -> RecommandCompanyClient:
        if sender not in self.company.peppol_ids:
            raise PeppolSenderNotConfiguredError(sender)
        return self

    async def ensure_account_identity(self) -> None:
        # Recommand validates the configured company during the actual operation.
        return None

    async def validate_ubl(self, document: bytes) -> PeppolValidationResult:
        return PeppolValidationResult(True)

    async def lookup_participant(
        self,
        participant: PeppolParticipant,
        descriptor: PeppolDocumentDescriptor,
    ) -> PeppolParticipantLookup:
        payload = await self._safe_request_json(
            "POST",
            "/verify-document-support",
            PeppolFailureStage.PARTICIPANT_LOOKUP,
            json={
                "peppolAddress": f"{participant.scheme}:{participant.identifier}",
                "documentType": document_type_identifier(
                    descriptor.document_kind, descriptor.customization_id
                ),
                "processId": descriptor.profile_id,
            },
        )
        is_valid = payload.get("isValid")
        if payload.get("success") is not True or not isinstance(is_valid, bool):
            raise self._error(
                PeppolFailureStage.PARTICIPANT_LOOKUP,
                PeppolFailureCategory.PROVIDER_ERROR,
            )
        registered = is_valid
        return PeppolParticipantLookup(
            registered,
            (
                None
                if registered
                else "Recipient does not support the requested Peppol document"
            ),
        )

    async def submit_document(
        self, document: bytes, descriptor: PeppolDocumentDescriptor
    ) -> PeppolProviderDocument:
        try:
            xml = document.decode("utf-8")
        except UnicodeDecodeError as ex:
            raise self._error(
                PeppolFailureStage.SUBMISSION,
                PeppolFailureCategory.INVALID_DOCUMENT,
                "UBL document must use UTF-8 for Recommand raw XML submission",
            ) from ex
        try:
            payload = await self._request_json(
                "POST",
                f"/{self.company.company_id}/send",
                PeppolFailureStage.SUBMISSION,
                json={
                    "recipient": (
                        f"{descriptor.receiver.scheme}:{descriptor.receiver.identifier}"
                    ),
                    "documentType": "xml",
                    "document": xml,
                    "doctypeId": document_type_identifier(
                        descriptor.document_kind, descriptor.customization_id
                    ),
                    "processId": descriptor.profile_id,
                },
            )
        except (TimeoutError, aiohttp.ClientConnectionError) as ex:
            raise PeppolAmbiguousError(
                PeppolFailure(
                    PeppolFailureStage.SUBMISSION,
                    PeppolFailureCategory.AMBIGUOUS_RESULT,
                    provider_message="The Recommand send outcome could not be established",
                    provider=self.provider,
                )
            ) from ex
        except PeppolProviderError as ex:
            if not is_ambiguous_write_failure(ex.failure):
                raise
            raise PeppolAmbiguousError(ex.failure) from ex
        return self._send_response(payload)

    @property
    def _authorization(self) -> str:
        return aiohttp.encode_basic_auth(
            self.adapter.configuration.api_key,
            self.adapter.configuration.api_secret,
        )

    async def _safe_request_json(
        self, method: str, path: str, stage: PeppolFailureStage, **kwargs
    ) -> Mapping[str, Any]:
        try:
            return await self._request_json(method, path, stage, **kwargs)
        except (TimeoutError, aiohttp.ClientConnectionError) as ex:
            raise self._error(
                stage,
                PeppolFailureCategory.TRANSPORT_ERROR,
                "The provider could not be reached",
            ) from ex

    async def _request_json(
        self, method: str, path: str, stage: PeppolFailureStage, **kwargs
    ) -> Mapping[str, Any]:
        async with asyncio.timeout(self.adapter.configuration.timeout):
            async with self.adapter.session.request(
                method,
                f"{self.adapter.configuration.base_url}{path}",
                headers={"Authorization": self._authorization},
                **kwargs,
            ) as response:
                body = await response.content.read(MAX_PROVIDER_RESPONSE_BYTES + 1)
                if len(body) > MAX_PROVIDER_RESPONSE_BYTES:
                    raise self._error(stage, PeppolFailureCategory.PROVIDER_ERROR)
                try:
                    payload = orjson.loads(body) if body else {}
                except orjson.JSONDecodeError:
                    payload = {}
                if not isinstance(payload, Mapping):
                    payload = {}
                if 200 <= response.status < 300:
                    return payload
                if response.status in {401, 403}:
                    category = PeppolFailureCategory.AUTHENTICATION_FAILED
                elif response.status == 429:
                    category = PeppolFailureCategory.RATE_LIMITED
                elif (
                    stage is PeppolFailureStage.SUBMISSION
                    and response.status in {400, 422}
                    and payload.get("success") is False
                    and isinstance(payload.get("errors"), Mapping)
                ):
                    category = PeppolFailureCategory.INVALID_DOCUMENT
                elif 400 <= response.status < 500:
                    category = PeppolFailureCategory.PROVIDER_REJECTED
                else:
                    category = PeppolFailureCategory.PROVIDER_ERROR
                raise PeppolProviderError(
                    PeppolFailure(
                        stage,
                        category,
                        http_status=response.status,
                        provider=self.provider,
                    )
                )

    def _send_response(self, payload: Mapping[str, Any]) -> PeppolProviderDocument:
        identifier = payload.get("id")
        sent = payload.get("sentOverPeppol")
        if (
            payload.get("success") is not True
            or not isinstance(identifier, str)
            or not identifier
            or payload.get("companyId") != self.company.company_id
            or not isinstance(sent, bool)
        ):
            raise PeppolAmbiguousError(
                PeppolFailure(
                    PeppolFailureStage.SUBMISSION,
                    PeppolFailureCategory.AMBIGUOUS_RESULT,
                    provider_message="The Recommand send response did not establish its outcome",
                    provider=self.provider,
                )
            )
        return PeppolProviderDocument(
            self.provider,
            identifier,
            "sent_over_peppol" if sent else "not_sent_over_peppol",
            (
                PeppolDeliveryState.DELIVERY_CONFIRMED
                if sent
                else PeppolDeliveryState.UNKNOWN
            ),
        )

    def _error(
        self,
        stage: PeppolFailureStage,
        category: PeppolFailureCategory,
        message: str | None = None,
    ) -> PeppolProviderError:
        return PeppolProviderError(
            PeppolFailure(
                stage,
                category,
                provider_message=message,
                provider=self.provider,
            )
        )


class RecommandPeppolAdapterFactory:
    def __init__(self, session: aiohttp.ClientSession) -> None:
        self.session = session

    def validate(self, configuration: Mapping[str, Any]) -> None:
        RecommandConfiguration.parse(configuration)

    def create(self, configuration: Mapping[str, Any]) -> RecommandPeppolAdapter:
        return RecommandPeppolAdapter(
            RecommandConfiguration.parse(configuration), self.session
        )
