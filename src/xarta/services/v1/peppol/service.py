from __future__ import annotations

import hashlib

from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING
from typing import Any

from xarta.exceptions.protocol import TemporaryError
from xarta.execution import ExecutionMode
from xarta.logging import log_capability_adapter_selected
from xarta.pricing import PricingBinding
from xarta.protocol.dag import CapabilityResult
from xarta.protocol.dag import CommonOutcome
from xarta.protocol.dag import NodeTask
from xarta.protocol.dag import OutcomeEmission
from xarta.services.v1.peppol.details import failure_details
from xarta.services.v1.peppol.details import sanitize_provider_message
from xarta.services.v1.peppol.details import validation_details
from xarta.services.v1.peppol.inspection import PeppolDocumentError
from xarta.services.v1.peppol.inspection import PeppolDocumentInspector
from xarta.services.v1.peppol.models import PeppolAdapter
from xarta.services.v1.peppol.models import PeppolAmbiguousError
from xarta.services.v1.peppol.models import PeppolDeliveryState
from xarta.services.v1.peppol.models import PeppolFailure
from xarta.services.v1.peppol.models import PeppolFailureCategory
from xarta.services.v1.peppol.models import PeppolFailureStage
from xarta.services.v1.peppol.models import PeppolParticipant
from xarta.services.v1.peppol.models import PeppolProviderDocument
from xarta.services.v1.peppol.models import PeppolProviderError
from xarta.services.v1.peppol.models import PeppolSenderNotConfiguredError
from xarta.services.v1.peppol.models import PeppolSubmissionMode
from xarta.services.v1.peppol.models import PeppolUpdate
from xarta.services.v1.peppol.models import is_retryable_read_failure
from xarta.services.v1.peppol.profiles import require_supported_profile
from xarta.services.v1.peppol.reducer import reduce_peppol
from xarta.tracking import DestinationRegistry
from xarta.tracking import Reduction
from xarta.tracking import TrackedCapabilityService
from xarta.tracking import TrackedOperationLifecycle

if TYPE_CHECKING:
    from collections.abc import Mapping

    from xarta.adapters import AdapterRegistry
    from xarta.protocol.dag.peppol import PeppolNode
    from xarta.protocol.document.source import DocumentSourceResult


@dataclass(frozen=True)
class PeppolComponents:
    destinations: DestinationRegistry
    adapters: AdapterRegistry[PeppolAdapter]
    adapters_by_binding: Mapping
    callback_accounts: Mapping
    execution_mode: ExecutionMode

    def current(self):
        return self.destinations.resolve("peppol", None)

    def current_pricing_binding(self) -> PricingBinding:
        binding = self.current().binding
        return PricingBinding(
            capability="peppol",
            destination=None,
            adapter=binding.adapter,
            adapter_configuration_revision=binding.configuration_revision,
        )

    def adapter_for(self, binding):
        return self.adapters_by_binding[binding]

    def bind(self, binding, sender, provider_account_reference=None):
        adapter = self.adapter_for(binding)
        if provider_account_reference is not None:
            return adapter.bind_account(provider_account_reference).bind_sender(sender)
        return adapter.bind_sender(sender)

    def callback_candidates(self, adapter: str, account_reference: str):
        return self.callback_accounts.get((adapter, account_reference), ())


def build_peppol_components(
    configuration: Mapping[str, Any],
    adapters: AdapterRegistry[PeppolAdapter],
) -> PeppolComponents:
    destinations = DestinationRegistry(
        {"server-configuration": {"kind": "peppol", **dict(configuration)}},
        defaults={"peppol": "server-configuration"},
        require_explicit_revisions=True,
    )
    adapters_by_binding = {}
    execution_mode = None
    callback_accounts: dict[tuple[str, str], list[tuple[Any, Any]]] = {}
    for resolved in destinations.iter_resolved():
        if resolved.binding.capability != "peppol":
            raise ValueError(
                f"Destination {resolved.binding.destination} is not valid for peppol"
            )
        adapters.validate(resolved.binding.adapter, resolved.configuration)
        adapter = adapters.create(resolved.binding.adapter, resolved.configuration)
        mode = adapter.execution_mode
        if not isinstance(mode, ExecutionMode):
            raise TypeError("Peppol adapter returned an invalid execution mode")
        if execution_mode is None:
            execution_mode = mode
        elif execution_mode is not mode:
            raise ValueError("Peppol destination changes execution mode")
        adapters_by_binding[resolved.binding] = adapter
        for account_reference in adapter.account_references:
            callback_accounts.setdefault(
                (resolved.binding.adapter, account_reference), []
            ).append((resolved.binding, adapter))
    if execution_mode is None:
        raise ValueError("Peppol requires a configured adapter")
    return PeppolComponents(
        destinations,
        adapters,
        MappingProxyType(adapters_by_binding),
        MappingProxyType(
            {
                account: tuple(candidates)
                for account, candidates in callback_accounts.items()
            }
        ),
        execution_mode,
    )


class PeppolService:
    def __init__(self, store, components: PeppolComponents, publish=None) -> None:
        self.tracking = TrackedCapabilityService(
            store, components.destinations, publish
        )
        self.components = components
        self.inspector = PeppolDocumentInspector()

    async def submit(
        self,
        task: NodeTask,
        node: PeppolNode,
        document: DocumentSourceResult,
        log=None,
    ) -> CapabilityResult:
        try:
            descriptor = self.inspector.inspect(document)
        except PeppolDocumentError as ex:
            return CapabilityResult(
                outcomes=(
                    OutcomeEmission(
                        "invalid_document",
                        details={"issues": [{"severity": "fatal", "message": str(ex)}]},
                    ),
                )
            )
        try:
            require_supported_profile(
                descriptor.customization_id, descriptor.profile_id
            )
        except ValueError:
            return CapabilityResult(
                outcomes=(
                    OutcomeEmission(
                        "unsupported_profile",
                        details={"profile_id": descriptor.profile_id},
                    ),
                )
            )

        existing_operation = await self.tracking.store.operation_for_execution(
            task.node_execution_id
        )
        resolved = (
            self.components.destinations.resolve_binding(existing_operation.binding)
            if existing_operation is not None
            else self.components.current()
        )
        if log is not None:
            await log_capability_adapter_selected(
                log, resolved.binding, self.components.execution_mode.value
            )
        try:
            adapter = self.components.bind(
                resolved.binding,
                descriptor.sender,
                (
                    existing_operation.provider_account_reference
                    if existing_operation is not None
                    else None
                ),
            )
        except PeppolSenderNotConfiguredError:
            provider = self.components.adapter_for(resolved.binding).provider
            return CapabilityResult(
                outcomes=(
                    OutcomeEmission(
                        CommonOutcome.EXECUTION_FAILED,
                        details={
                            "provider": provider,
                            "stage": "validation",
                            "category": "configuration_error",
                            "message": "No provider account is configured for the UBL sender",
                            "sender": (
                                f"{descriptor.sender.scheme}:{descriptor.sender.identifier}"
                            ),
                        },
                    ),
                )
            )

        if existing_operation is None:
            try:
                await adapter.ensure_account_identity()
                validation = await adapter.validate_ubl(document.data)
                if not validation.valid:
                    return CapabilityResult(
                        outcomes=(
                            OutcomeEmission(
                                "invalid_document",
                                details=validation_details(validation.issues),
                            ),
                        )
                    )
                lookup = await adapter.lookup_participant(
                    descriptor.receiver, descriptor
                )
                if not lookup.registered:
                    return CapabilityResult(
                        outcomes=(
                            OutcomeEmission(
                                "recipient_not_registered",
                                details={
                                    "provider": adapter.provider,
                                    "message": sanitize_provider_message(
                                        lookup.message
                                    ),
                                },
                            ),
                        )
                    )
            except PeppolProviderError as ex:
                if is_retryable_read_failure(ex.failure):
                    raise TemporaryError(
                        "Temporary Peppol provider read failure"
                    ) from ex
                return CapabilityResult(
                    outcomes=(
                        OutcomeEmission(
                            CommonOutcome.EXECUTION_FAILED,
                            details=failure_details(ex.failure),
                        ),
                    )
                )

        digest = hashlib.sha256(document.data).hexdigest()
        initial_state = {
            "phase": "prepared",
            "source_document_id": str(document.id),
            "source_document_version": node.document.get("version"),
            "sha256": digest,
            "business_document_id": descriptor.business_document_id,
            "issue_date": descriptor.issue_date.isoformat(),
            "customization_id": descriptor.customization_id,
            "profile_id": descriptor.profile_id,
            "document_kind": descriptor.document_kind,
            "sender": descriptor.sender.dict(),
            "receiver": descriptor.receiver.dict(),
            "provider_document_id": None,
            "provider_state": "prepared",
        }
        operation, _ = await self.tracking.prepare(
            task,
            "peppol",
            None,
            initial_state,
            adapter.provider_account_reference,
        )
        adapter = self.components.bind(
            operation.binding,
            PeppolParticipant(**operation.state["sender"]),
            operation.provider_account_reference,
        )
        if operation.state["sha256"] != digest:
            return await self._terminal_failure(
                operation,
                CommonOutcome.EXECUTION_FAILED,
                PeppolFailure(
                    PeppolFailureStage.VALIDATION,
                    PeppolFailureCategory.CONFIGURATION_ERROR,
                    provider_message="Retrieved UBL bytes differ from the pinned document digest",
                    provider=adapter.provider,
                ),
            )

        if adapter.submission_mode is PeppolSubmissionMode.DIRECT:
            return await self._submit_direct(
                operation, adapter, document.data, descriptor
            )
        return await self._submit_staged(operation, adapter, document.data)

    async def _submit_staged(
        self, operation, adapter, document: bytes
    ) -> CapabilityResult:
        phase = str(operation.state["phase"])
        provider_document_id = operation.provider_reference
        if phase == "creating" and provider_document_id is None:
            return await self._resolve_irreconcilable_uncertainty(
                operation,
                PeppolFailure(
                    PeppolFailureStage.DOCUMENT_CREATION,
                    PeppolFailureCategory.AMBIGUOUS_RESULT,
                    provider_message="The provider may have created a DRAFT whose identity was not durably received",
                    provider=adapter.provider,
                ),
            )

        if provider_document_id is None:
            operation = await self.tracking.checkpoint_operation(
                operation, {**operation.state, "phase": "creating"}
            )
            try:
                provider_document = await adapter.create_document(document)
            except PeppolAmbiguousError as ex:
                return await self._resolve_irreconcilable_uncertainty(
                    operation, ex.failure
                )
            except PeppolProviderError as ex:
                if is_retryable_read_failure(ex.failure):
                    raise TemporaryError(
                        "Temporary Peppol reconciliation failure"
                    ) from ex
                return await self._terminal_failure(
                    operation, CommonOutcome.EXECUTION_FAILED, ex.failure
                )
            if provider_document.delivery_state is not PeppolDeliveryState.STAGED:
                return await self._terminal_failure(
                    operation,
                    CommonOutcome.EXECUTION_FAILED,
                    PeppolFailure(
                        PeppolFailureStage.DOCUMENT_CREATION,
                        PeppolFailureCategory.PROVIDER_ERROR,
                        provider_message="Provider document was not created in DRAFT state",
                        provider=adapter.provider,
                    ),
                )
            # Persist the provider document ID before /send. A retry must continue
            # with this DRAFT rather than creating another provider document.
            operation = await self.tracking.checkpoint_operation(
                operation,
                {
                    **operation.state,
                    "phase": "provider_created",
                    "provider_document_id": provider_document.id,
                    "provider_state": provider_document.raw_state,
                },
                provider_document.id,
            )
            provider_document_id = provider_document.id
        else:
            try:
                provider_document = await adapter.get_document(provider_document_id)
            except PeppolProviderError as ex:
                if is_retryable_read_failure(ex.failure):
                    raise TemporaryError(
                        "Temporary Peppol reconciliation failure"
                    ) from ex
                return await self._terminal_failure(
                    operation, CommonOutcome.EXECUTION_FAILED, ex.failure
                )
            if provider_document.delivery_state is not PeppolDeliveryState.STAGED:
                return await self._apply_observation(
                    operation, provider_document, "reconciliation"
                )
            if phase == "sending":
                return await self._mark_reconcilable_uncertainty(
                    operation,
                    PeppolFailure(
                        PeppolFailureStage.SUBMISSION,
                        PeppolFailureCategory.AMBIGUOUS_RESULT,
                        provider_message="The send request outcome could not be established; provider still reports DRAFT",
                        provider=adapter.provider,
                    ),
                )

        operation = await self.tracking.checkpoint_operation(
            operation, {**operation.state, "phase": "sending"}
        )
        sender = PeppolParticipant(**operation.state["sender"])
        receiver = PeppolParticipant(**operation.state["receiver"])
        try:
            provider_document = await adapter.send_document(
                provider_document_id, sender, receiver
            )
        except PeppolAmbiguousError as ex:
            try:
                provider_document = await adapter.get_document(provider_document_id)
            except PeppolProviderError:
                return await self._mark_reconcilable_uncertainty(operation, ex.failure)
            if provider_document.delivery_state is PeppolDeliveryState.STAGED:
                return await self._mark_reconcilable_uncertainty(operation, ex.failure)
        except PeppolProviderError as ex:
            return await self._terminal_failure(
                operation, CommonOutcome.EXECUTION_FAILED, ex.failure
            )
        return await self._apply_observation(operation, provider_document, "submission")

    async def _submit_direct(
        self, operation, adapter, document: bytes, descriptor
    ) -> CapabilityResult:
        if operation.provider_reference is not None:
            provider_document = PeppolProviderDocument(
                adapter.provider,
                operation.provider_reference,
                operation.state["provider_state"],
                PeppolDeliveryState(operation.state["provider_delivery_state"]),
            )
            return await self._apply_direct_observation(
                operation, adapter, provider_document, "submission"
            )
        if operation.state["phase"] == "submitting":
            return await self._resolve_irreconcilable_uncertainty(
                operation,
                PeppolFailure(
                    PeppolFailureStage.SUBMISSION,
                    PeppolFailureCategory.AMBIGUOUS_RESULT,
                    provider_message=(
                        "The direct send may have succeeded but no provider document "
                        "identity was durably received"
                    ),
                    provider=adapter.provider,
                ),
            )
        operation = await self.tracking.checkpoint_operation(
            operation, {**operation.state, "phase": "submitting"}
        )
        try:
            provider_document = await adapter.submit_document(document, descriptor)
        except PeppolAmbiguousError as ex:
            return await self._resolve_irreconcilable_uncertainty(operation, ex.failure)
        except PeppolProviderError as ex:
            outcome = (
                "invalid_document"
                if ex.failure.category is PeppolFailureCategory.INVALID_DOCUMENT
                else CommonOutcome.EXECUTION_FAILED
            )
            return await self._terminal_failure(operation, outcome, ex.failure)
        operation = await self.tracking.checkpoint_operation(
            operation,
            {
                **operation.state,
                "phase": "provider_responded",
                "provider_document_id": provider_document.id,
                "provider_state": provider_document.raw_state,
                "provider_delivery_state": provider_document.delivery_state.value,
            },
            provider_document.id,
        )
        return await self._apply_direct_observation(
            operation, adapter, provider_document, "submission"
        )

    async def reconcile(self, operation_id) -> CapabilityResult:
        operation = await self.tracking.store.get_operation(operation_id)
        adapter = self.components.bind(
            operation.binding,
            PeppolParticipant(**operation.state["sender"]),
            operation.provider_account_reference,
        )
        if operation.provider_reference is None:
            return CapabilityResult(waiting_feedback=True)
        if adapter.submission_mode is PeppolSubmissionMode.DIRECT:
            provider_document = PeppolProviderDocument(
                adapter.provider,
                operation.provider_reference,
                operation.state["provider_state"],
                PeppolDeliveryState(operation.state["provider_delivery_state"]),
            )
            return await self._apply_direct_observation(
                operation, adapter, provider_document, "reconciliation"
            )
        try:
            provider_document = await adapter.get_document(operation.provider_reference)
        except PeppolProviderError as ex:
            if is_retryable_read_failure(ex.failure):
                raise TemporaryError("Temporary Peppol reconciliation failure") from ex
            raise
        return await self._apply_observation(
            operation, provider_document, "reconciliation"
        )

    async def _apply_direct_observation(
        self, operation, adapter, provider_document, source: str
    ) -> CapabilityResult:
        if provider_document.delivery_state is PeppolDeliveryState.UNKNOWN:
            return await self._terminal_failure(
                operation,
                CommonOutcome.EXECUTION_FAILED,
                PeppolFailure(
                    PeppolFailureStage.SUBMISSION,
                    PeppolFailureCategory.PROVIDER_REJECTED,
                    provider_message="The provider did not complete Peppol delivery",
                    provider=adapter.provider,
                ),
            )
        await self.tracking.feedback(
            operation.id,
            operation.binding.adapter,
            f"{source}:{provider_document.id}:{provider_document.raw_state}",
            PeppolUpdate(provider_document),
            reduce_peppol,
            source,
        )
        return CapabilityResult(outcomes_persisted=True)

    async def _apply_observation(
        self, operation, provider_document: PeppolProviderDocument, source: str
    ) -> CapabilityResult:
        state = {
            **operation.state,
            "phase": "waiting_feedback",
            "provider_state": provider_document.raw_state,
        }
        preserve_uncertainty = (
            operation.lifecycle is TrackedOperationLifecycle.UNCERTAIN
            and provider_document.delivery_state is PeppolDeliveryState.STAGED
        )
        if not preserve_uncertainty:
            await self.tracking.mark_waiting(
                operation.id,
                provider_document.id,
                state,
                source=source,
            )
        await self.tracking.feedback(
            operation.id,
            operation.binding.adapter,
            f"{source}:{provider_document.id}:{provider_document.raw_state}",
            PeppolUpdate(provider_document),
            reduce_peppol,
            source,
        )
        return CapabilityResult(outcomes_persisted=True)

    async def _terminal_failure(
        self,
        operation,
        outcome: str,
        failure: PeppolFailure,
    ) -> CapabilityResult:
        await self.tracking.feedback(
            operation.id,
            operation.binding.adapter,
            f"terminal:{outcome}:{failure.stage.value}",
            failure,
            lambda state, update: Reduction(
                state={**state, "last_failure": failure_details(update)},
                outcomes=(OutcomeEmission(outcome, details=failure_details(update)),),
                operation_resolved=True,
            ),
            "submission",
        )
        return CapabilityResult(outcomes_persisted=True)

    async def _resolve_irreconcilable_uncertainty(
        self, operation, failure: PeppolFailure
    ) -> CapabilityResult:
        return await self._terminal_failure(
            operation, CommonOutcome.OUTCOME_UNCERTAIN, failure
        )

    async def _mark_reconcilable_uncertainty(
        self, operation, failure: PeppolFailure
    ) -> CapabilityResult:
        operation = await self.tracking.checkpoint_operation(
            operation,
            {
                **operation.state,
                "phase": "sending",
                "last_failure": failure_details(failure),
            },
        )
        await self.tracking.mark_uncertain(operation.id, source="submission")
        return CapabilityResult(waiting_feedback=True)
