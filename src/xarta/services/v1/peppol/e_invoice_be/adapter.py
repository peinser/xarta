from __future__ import annotations

import asyncio
import hashlib
import hmac
import json

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import aiohttp
import orjson

from xarta.execution import ExecutionMode
from xarta.services.v1.peppol.models import EInvoiceBeWebhook
from xarta.services.v1.peppol.models import PeppolAmbiguousError
from xarta.services.v1.peppol.models import PeppolDeliveryState
from xarta.services.v1.peppol.models import PeppolFailure
from xarta.services.v1.peppol.models import PeppolFailureCategory
from xarta.services.v1.peppol.models import PeppolFailureStage
from xarta.services.v1.peppol.models import PeppolParticipant
from xarta.services.v1.peppol.models import PeppolParticipantLookup
from xarta.services.v1.peppol.models import PeppolProviderDocument
from xarta.services.v1.peppol.models import PeppolProviderError
from xarta.services.v1.peppol.models import PeppolSenderNotConfiguredError
from xarta.services.v1.peppol.models import PeppolSubmissionMode
from xarta.services.v1.peppol.models import PeppolValidationIssue
from xarta.services.v1.peppol.models import PeppolValidationResult
from xarta.services.v1.peppol.models import is_ambiguous_write_failure

from .configuration import EInvoiceBeConfiguration
from .configuration import EInvoiceBeTenantConfiguration

MAX_PROVIDER_RESPONSE_BYTES = 256 * 1024
MAX_WEBHOOK_BODY_BYTES = 64 * 1024


class EInvoiceBeDocumentState(StrEnum):
    DRAFT = "draft"
    TRANSIT = "transit"
    SENT = "sent"
    FAILED = "failed"
    UNKNOWN = "unknown"


def validate_e_invoice_be_configuration(configuration: Mapping[str, Any]) -> None:
    EInvoiceBeConfiguration.parse(configuration)


def parse_untrusted_e_invoice_be_webhook(
    raw_body: bytes,
) -> tuple[Mapping[str, Any], str, str]:
    """Extract routing hints only; callers must authenticate before using them."""
    if not raw_body or len(raw_body) > MAX_WEBHOOK_BODY_BYTES:
        raise ValueError("Webhook body is empty or exceeds its size limit")
    try:
        payload = json.loads(raw_body)
    except (UnicodeDecodeError, json.JSONDecodeError) as ex:
        raise ValueError("Webhook body is not valid JSON") from ex
    if not isinstance(payload, Mapping):
        raise ValueError("Webhook body must be an object")
    data = payload.get("data")
    tenant_id = payload.get("tenant_id")
    document_id = data.get("document_id") if isinstance(data, Mapping) else None
    if not isinstance(tenant_id, str) or not tenant_id:
        raise ValueError("Webhook requires tenant_id")
    if not isinstance(document_id, str) or not document_id:
        raise ValueError("Webhook requires data.document_id")
    return payload, tenant_id, document_id


def canonical_e_invoice_be_webhook(payload: Mapping[str, Any]) -> bytes:
    # This matches the provider's published Python fixture, including json.dumps'
    # default separator whitespace and ASCII escaping.
    return json.dumps(payload, sort_keys=True).encode("utf-8")


class EInvoiceBePeppolAdapter:
    provider = "e-invoice.be"
    submission_mode = PeppolSubmissionMode.STAGED

    @property
    def execution_mode(self) -> ExecutionMode:
        return ExecutionMode.TRACKED

    def __init__(
        self,
        configuration: EInvoiceBeConfiguration,
        session: aiohttp.ClientSession,
    ) -> None:
        self.configuration = configuration
        self.session = session
        self._validated_tenants: set[str] = set()
        self._validation_locks: dict[str, asyncio.Lock] = {}

    @property
    def account_references(self) -> tuple[str, ...]:
        return tuple(self.configuration.tenants_by_id)

    def bind_sender(self, sender: PeppolParticipant) -> EInvoiceBeTenantClient:
        tenant = self.configuration.tenants_by_participant.get(sender)
        if tenant is None:
            raise PeppolSenderNotConfiguredError(sender)
        return EInvoiceBeTenantClient(self, tenant)

    def bind_tenant(self, tenant_id: str) -> EInvoiceBeTenantClient:
        try:
            tenant = self.configuration.tenants_by_id[tenant_id]
        except KeyError as ex:
            raise KeyError(f"Unknown e-invoice.be tenant: {tenant_id}") from ex
        return EInvoiceBeTenantClient(self, tenant)

    def bind_account(self, account_reference: str) -> EInvoiceBeTenantClient:
        return self.bind_tenant(account_reference)

    async def ensure_account_identity(
        self, tenant: EInvoiceBeTenantConfiguration
    ) -> None:
        if tenant.tenant_id in self._validated_tenants:
            return
        lock = self._validation_locks.setdefault(tenant.tenant_id, asyncio.Lock())
        async with lock:
            if tenant.tenant_id in self._validated_tenants:
                return
            await EInvoiceBeTenantClient(self, tenant).validate_account_identity()
            self._validated_tenants.add(tenant.tenant_id)

    def authenticate_webhook(
        self, payload: Mapping[str, Any], supplied_signature: str | None
    ) -> EInvoiceBeWebhook:
        tenant_id = payload.get("tenant_id")
        if not isinstance(tenant_id, str):
            raise ValueError("Webhook requires tenant_id")
        tenant = self.configuration.tenants_by_id.get(tenant_id)
        if tenant is None:
            raise LookupError(f"Unknown e-invoice.be tenant: {tenant_id}")
        expected = (
            "sha256="
            + hmac.new(
                tenant.webhook_secret.encode(),
                canonical_e_invoice_be_webhook(payload),
                hashlib.sha256,
            ).hexdigest()
        )
        if not supplied_signature or not hmac.compare_digest(
            supplied_signature, expected
        ):
            raise PermissionError("Invalid e-invoice.be webhook signature")
        data = payload.get("data")
        if not isinstance(data, Mapping):
            raise ValueError("Webhook data must be an object")
        event_id = payload.get("id")
        event_type = payload.get("type")
        document_id = data.get("document_id")
        if not all(
            isinstance(value, str) and value
            for value in (event_id, event_type, document_id)
        ):
            raise ValueError("Webhook requires event id, type, and document id")
        if event_type not in {"document.sent", "document.sent.failed"}:
            raise ValueError("Unsupported e-invoice.be webhook event type")
        occurred_at = payload.get("created_at")
        return EInvoiceBeWebhook(
            str(event_id),
            str(event_type),
            tenant_id,
            str(document_id),
            occurred_at if isinstance(occurred_at, int) else None,
        )


@dataclass(frozen=True)
class EInvoiceBeTenantClient:
    adapter: EInvoiceBePeppolAdapter
    tenant: EInvoiceBeTenantConfiguration

    @property
    def configuration(self) -> EInvoiceBeConfiguration:
        return self.adapter.configuration

    @property
    def session(self) -> aiohttp.ClientSession:
        return self.adapter.session

    @property
    def provider(self) -> str:
        return self.adapter.provider

    @property
    def submission_mode(self) -> PeppolSubmissionMode:
        return self.adapter.submission_mode

    @property
    def provider_account_reference(self) -> str:
        return self.tenant.tenant_id

    @property
    def base_url(self) -> str:
        return self.configuration.base_url

    @property
    def timeout(self) -> float:
        return self.configuration.timeout

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.tenant.api_key}"}

    def bind_sender(self, sender: PeppolParticipant) -> EInvoiceBeTenantClient:
        if sender not in self.tenant.peppol_ids:
            raise PeppolSenderNotConfiguredError(sender)
        return self

    async def ensure_account_identity(self) -> None:
        await self.adapter.ensure_account_identity(self.tenant)

    async def get_account_identity(self) -> Mapping[str, Any]:
        """Read account identity for readiness checks and developer smoke tests."""
        return await self._safe_request_json(
            "GET", "/api/me/", PeppolFailureStage.VALIDATION
        )

    async def validate_account_identity(self) -> Mapping[str, Any]:
        identity = await self.get_account_identity()
        identity_tenant_id = identity.get("tenant_id") or identity.get("id")
        if (
            identity_tenant_id is not None
            and identity_tenant_id != self.tenant.tenant_id
        ):
            raise PeppolProviderError(
                PeppolFailure(
                    PeppolFailureStage.VALIDATION,
                    PeppolFailureCategory.CONFIGURATION_ERROR,
                    provider_message="API key belongs to another provider tenant",
                    provider="e-invoice.be",
                )
            )
        provider_peppol_ids = set(identity.get("peppol_ids") or [])
        if any(
            f"{participant.scheme}:{participant.identifier}" not in provider_peppol_ids
            for participant in self.tenant.peppol_ids
        ):
            raise PeppolProviderError(
                PeppolFailure(
                    PeppolFailureStage.VALIDATION,
                    PeppolFailureCategory.CONFIGURATION_ERROR,
                    provider_message="Configured Peppol participant is not present in the provider account",
                    provider="e-invoice.be",
                )
            )
        return identity

    async def validate_ubl(self, document: bytes) -> PeppolValidationResult:
        try:
            payload = await self._multipart_request(
                "/api/validate/ubl", document, PeppolFailureStage.VALIDATION
            )
        except (TimeoutError, aiohttp.ClientConnectionError) as ex:
            raise self._transport_error(PeppolFailureStage.VALIDATION) from ex
        issues = tuple(
            PeppolValidationIssue(
                rule=value.get("rule_id"),
                severity=str(value.get("type", "error")),
                message=str(value.get("message", "Validation failed")),
            )
            for value in payload.get("issues", [])
            if isinstance(value, Mapping)
        )
        return PeppolValidationResult(bool(payload.get("is_valid")), issues)

    async def lookup_participant(
        self, participant: PeppolParticipant, descriptor=None
    ) -> PeppolParticipantLookup:
        payload = await self._safe_request_json(
            "GET",
            "/api/validate/peppol-id",
            PeppolFailureStage.PARTICIPANT_LOOKUP,
            params={"peppol_id": f"{participant.scheme}:{participant.identifier}"},
        )
        registered = bool(payload.get("is_valid"))
        return PeppolParticipantLookup(
            registered,
            (
                None
                if registered
                else "Recipient is not registered for the requested Peppol service"
            ),
        )

    async def create_document(self, document: bytes) -> PeppolProviderDocument:
        try:
            payload = await self._multipart_request(
                "/api/documents/ubl",
                document,
                PeppolFailureStage.DOCUMENT_CREATION,
                expected_status=201,
            )
            return self._document(payload, PeppolFailureStage.DOCUMENT_CREATION)
        except (TimeoutError, aiohttp.ClientConnectionError) as ex:
            raise PeppolAmbiguousError(
                PeppolFailure(
                    PeppolFailureStage.DOCUMENT_CREATION,
                    PeppolFailureCategory.AMBIGUOUS_RESULT,
                    provider_message="The provider request may have created a document but no definitive response was received",
                    provider="e-invoice.be",
                )
            ) from ex
        except PeppolProviderError as ex:
            if not is_ambiguous_write_failure(ex.failure):
                raise
            raise PeppolAmbiguousError(ex.failure) from ex

    async def send_document(
        self,
        provider_document_id: str,
        sender: PeppolParticipant,
        receiver: PeppolParticipant,
    ) -> PeppolProviderDocument:
        # e-invoice.be does not treat UBL EndpointID as authoritative routing by
        # default. Always pass identities pinned from the inspected UBL.
        try:
            payload = await self._request_json(
                "POST",
                f"/api/documents/{provider_document_id}/send",
                PeppolFailureStage.SUBMISSION,
                params={
                    "sender_peppol_scheme": sender.scheme,
                    "sender_peppol_id": sender.identifier,
                    "receiver_peppol_scheme": receiver.scheme,
                    "receiver_peppol_id": receiver.identifier,
                },
            )
            return self._document(payload, PeppolFailureStage.SUBMISSION)
        except (TimeoutError, aiohttp.ClientConnectionError) as ex:
            raise PeppolAmbiguousError(
                PeppolFailure(
                    PeppolFailureStage.SUBMISSION,
                    PeppolFailureCategory.AMBIGUOUS_RESULT,
                    provider_message="The send request outcome could not be established",
                    provider="e-invoice.be",
                )
            ) from ex
        except PeppolProviderError as ex:
            if not is_ambiguous_write_failure(ex.failure):
                raise
            raise PeppolAmbiguousError(ex.failure) from ex

    async def get_document(self, provider_document_id: str) -> PeppolProviderDocument:
        payload = await self._safe_request_json(
            "GET",
            f"/api/documents/{provider_document_id}",
            PeppolFailureStage.RECONCILIATION,
        )
        return self._document(payload, PeppolFailureStage.RECONCILIATION)

    async def _safe_request_json(
        self, method: str, path: str, stage: PeppolFailureStage, **kwargs
    ) -> Mapping[str, Any]:
        try:
            return await self._request_json(method, path, stage, **kwargs)
        except (TimeoutError, aiohttp.ClientConnectionError) as ex:
            raise self._transport_error(stage) from ex

    @staticmethod
    def _transport_error(stage: PeppolFailureStage) -> PeppolProviderError:
        return PeppolProviderError(
            PeppolFailure(
                stage,
                PeppolFailureCategory.TRANSPORT_ERROR,
                provider_message="The provider could not be reached",
                provider="e-invoice.be",
            )
        )

    async def _multipart_request(
        self,
        path: str,
        document: bytes,
        stage: PeppolFailureStage,
        *,
        expected_status: int | None = None,
    ) -> Mapping[str, Any]:
        form = aiohttp.FormData()
        form.add_field(
            "file", document, filename="document.xml", content_type="application/xml"
        )
        return await self._request_json(
            "POST", path, stage, data=form, expected_status=expected_status
        )

    async def _request_json(
        self,
        method: str,
        path: str,
        stage: PeppolFailureStage,
        *,
        expected_status: int | None = None,
        **kwargs,
    ) -> Mapping[str, Any]:
        async with asyncio.timeout(self.timeout):
            async with self.session.request(
                method, f"{self.base_url}{path}", headers=self.headers, **kwargs
            ) as response:
                body = await response.content.read(MAX_PROVIDER_RESPONSE_BYTES + 1)
                if len(body) > MAX_PROVIDER_RESPONSE_BYTES:
                    raise PeppolProviderError(
                        PeppolFailure(
                            stage,
                            PeppolFailureCategory.PROVIDER_ERROR,
                            provider="e-invoice.be",
                        )
                    )
                try:
                    payload = orjson.loads(body) if body else {}
                except orjson.JSONDecodeError:
                    payload = {}
                if not isinstance(payload, Mapping):
                    payload = {}
                successful = (
                    response.status == expected_status
                    if expected_status
                    else 200 <= response.status < 300
                )
                if not successful:
                    category = PeppolFailureCategory.PROVIDER_ERROR
                    if response.status in {401, 403}:
                        category = PeppolFailureCategory.AUTHENTICATION_FAILED
                    elif 400 <= response.status < 500 and response.status != 429:
                        category = PeppolFailureCategory.PROVIDER_REJECTED
                    elif response.status == 429:
                        category = PeppolFailureCategory.RATE_LIMITED
                    raise PeppolProviderError(
                        PeppolFailure(
                            stage,
                            category,
                            provider_code=(
                                str(payload.get("code"))
                                if payload.get("code")
                                else None
                            ),
                            provider_message=(
                                str(payload.get("detail"))
                                if payload.get("detail")
                                else None
                            ),
                            http_status=response.status,
                            provider="e-invoice.be",
                        )
                    )
                return payload

    @staticmethod
    def _document(
        payload: Mapping[str, Any], stage: PeppolFailureStage
    ) -> PeppolProviderDocument:
        identifier = payload.get("id")
        if not isinstance(identifier, str) or not identifier:
            raise PeppolProviderError(
                PeppolFailure(
                    stage,
                    PeppolFailureCategory.PROVIDER_ERROR,
                    provider="e-invoice.be",
                )
            )
        raw_state = str(payload.get("state", "UNKNOWN")).lower()
        try:
            state = EInvoiceBeDocumentState(raw_state)
        except ValueError:
            state = EInvoiceBeDocumentState.UNKNOWN
        error = payload.get("error")
        error = error if isinstance(error, Mapping) else {}
        delivery_states = {
            EInvoiceBeDocumentState.DRAFT: PeppolDeliveryState.STAGED,
            EInvoiceBeDocumentState.TRANSIT: PeppolDeliveryState.SUBMITTED,
            EInvoiceBeDocumentState.SENT: PeppolDeliveryState.DELIVERY_CONFIRMED,
            EInvoiceBeDocumentState.FAILED: PeppolDeliveryState.DELIVERY_FAILED,
            EInvoiceBeDocumentState.UNKNOWN: PeppolDeliveryState.UNKNOWN,
        }
        return PeppolProviderDocument(
            "e-invoice.be",
            identifier,
            state.value.upper(),
            delivery_states[state],
            str(error.get("code")) if error.get("code") else None,
            str(error.get("message")) if error.get("message") else None,
        )


class EInvoiceBePeppolAdapterFactory:
    def __init__(self, session: aiohttp.ClientSession) -> None:
        self.session = session

    def validate(self, configuration: Mapping[str, Any]) -> None:
        validate_e_invoice_be_configuration(configuration)

    def create(self, configuration: Mapping[str, Any]) -> EInvoiceBePeppolAdapter:
        return EInvoiceBePeppolAdapter(
            EInvoiceBeConfiguration.parse(configuration), self.session
        )
