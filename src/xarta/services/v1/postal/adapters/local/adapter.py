from __future__ import annotations

import hashlib

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from decimal import InvalidOperation
from typing import TYPE_CHECKING
from typing import Any
from zoneinfo import ZoneInfo

import orjson

from xarta.execution import ExecutionMode
from xarta.protocol.dag import CapabilityResult
from xarta.protocol.dag import OutcomeEmission
from xarta.protocol.dag.postal import LetterPrintSettingsV1
from xarta.services.v1.postal.adapters.local.capacity import CapacityDecision
from xarta.services.v1.postal.adapters.local.capacity import CapacityOverflow
from xarta.services.v1.postal.adapters.local.capacity import CapacityPool
from xarta.services.v1.postal.adapters.local.instructions import ProductionInstructionResolver  # fmt: skip
from xarta.tracking import Reduction

if TYPE_CHECKING:
    from xarta.protocol.dag import NodeTask
    from xarta.protocol.dag.postal import PostalRequest
    from xarta.services.v1.postal.adapters.local.artifacts import PostalArtifactStore
    from xarta.services.v1.postal.adapters.local.repository import LocalPostalRepository
    from xarta.tracking import TrackedCapabilityService


@dataclass(frozen=True, slots=True)
class LocalProfile:
    id: str
    revision: str
    site: str
    default_print: Mapping[str, str]
    provider_adapter: str
    provider_profile: str
    traveller_template_revision: str
    sheet_weight_g: Decimal = Decimal(0)
    envelope_weight_g: Decimal = Decimal(0)


class LocalPostalAdapter:
    """Tracked local production adapter.

    Source retrieval is retry-safe only after the immutable snapshot has been
    persisted. Physical printing and handover are explicitly station-driven;
    ambiguous print submission is recorded as uncertain and never auto-retried.
    """

    execution_mode = ExecutionMode.TRACKED

    def __init__(
        self,
        *,
        configuration: Mapping[str, Any],
        profile: LocalProfile,
        capacity_pool: CapacityPool,
        repository: LocalPostalRepository,
        artifacts: PostalArtifactStore,
        tracking: TrackedCapabilityService,
        instruction_resolver: ProductionInstructionResolver,
    ) -> None:
        self.configuration = dict(configuration)
        self.profile = profile
        self.capacity_pool = capacity_pool
        self.repository = repository
        self.artifacts = artifacts
        self.tracking = tracking
        self.instruction_resolver = instruction_resolver

    async def submit(
        self, *, task: NodeTask, request: PostalRequest, logger: Any
    ) -> CapabilityResult:
        operation, _ = await self.tracking.prepare(
            task,
            "postal",
            request.destination,
            {"phase": "snapshotting", "adapter": "local"},
        )
        log = logger.bind(operation_id=str(operation.id))
        await log.ainfo(
            "postal_operation_prepared",
            duplicate=operation.provider_reference is not None,
        )
        if operation.provider_reference is not None:
            await log.ainfo("postal_operation_resumed")
            return CapabilityResult(outcomes_persisted=True, waiting_feedback=True)
        documents = await self.artifacts.snapshot_documents(
            operation.id, request.mailpiece.content.documents
        )
        await log.ainfo(
            "postal_documents_snapshotted",
            document_count=len(documents),
            page_count=sum(document.page_count for document in documents),
            byte_count=sum(document.size for document in documents),
        )
        explicit_print = request.mailpiece.print
        print_settings = (
            explicit_print.dict()
            if explicit_print is not None
            else dict(self.profile.default_print)
        )
        if explicit_print is None:
            # Validate configured defaults through the protocol's strict parser.
            print_settings = LetterPrintSettingsV1.fromdict(
                {"version": 1, **print_settings}
            ).dict()
        sides = print_settings["sides"]
        pages = sum(document.page_count for document in documents)
        duplex = sides != "simplex"
        blank_pages = 0
        if duplex and print_settings["document_boundary"] != "continuous":
            blank_pages = sum(document.page_count % 2 for document in documents[:-1])
        sheets = pages if not duplex else (pages + blank_pages + 1) // 2
        estimated_weight_g = (
            self.profile.sheet_weight_g * sheets + self.profile.envelope_weight_g
        )
        speed = getattr(request.mailpiece.service, "speed", None)
        speed_value = speed.value if speed is not None else None
        instructions = self.instruction_resolver.resolve(
            self.profile.provider_profile,
            service_type=request.mailpiece.service.type,
            speed=speed_value,
            estimated_weight_g=estimated_weight_g,
        )
        plan = {
            "schema": "xarta.postal.local-production-plan/v1",
            "profile": {"id": self.profile.id, "revision": self.profile.revision},
            "traveller": {
                "template_revision": self.profile.traveller_template_revision
            },
            "mailpiece": request.mailpiece.dict(),
            "print": print_settings,
            "quantities": {
                "content_pages": pages,
                "blank_pages": blank_pages,
                "sheets": sheets,
                "estimated_weight_g": str(estimated_weight_g),
            },
            "documents": [
                {
                    "id": str(document.id),
                    "ordinal": document.ordinal,
                    "role": document.role,
                    "sha256": document.sha256,
                    "size": document.size,
                    "page_count": document.page_count,
                    "storage_reference": document.storage_reference,
                }
                for document in documents
            ],
            "franking": {
                "provider": {
                    "id": instructions.provider_id,
                    "profile": {
                        "id": instructions.provider_profile_id,
                        "revision": instructions.provider_profile_revision,
                    },
                },
                "method": instructions.franking_method,
                "policy_revision": instructions.policy_revision,
                "supplies": [
                    {
                        "reference": supply.reference,
                        "description": supply.description,
                        "quantity": supply.quantity,
                    }
                    for supply in instructions.supplies
                ],
                "pricing": {
                    "tariff_revision": instructions.tariff_revision,
                    "currency": instructions.currency,
                    "lines": [
                        {
                            "reference": line.reference,
                            "quantity": line.quantity,
                            "unit_price": str(line.unit_price),
                            "amount": str(line.amount),
                        }
                        for line in instructions.pricing
                    ],
                    "total_postage": str(instructions.total_postage),
                },
            },
        }
        encoded = orjson.dumps(plan, option=orjson.OPT_SORT_KEYS)
        digest = hashlib.sha256(
            b"xarta-postal-local-production-plan-v1\0" + encoded
        ).hexdigest()
        service = request.mailpiece.service.type
        service_date = datetime.now(ZoneInfo(self.capacity_pool.timezone)).date()
        local_operation_id, capacity_decision = await self.repository.create_submission(
            tracked_operation_id=operation.id,
            service=service,
            site_id=self.profile.site,
            service_date=service_date,
            plan=plan,
            plan_digest=digest,
            documents=documents,
            capacity_pool=self.capacity_pool,
        )
        await self.tracking.mark_waiting(
            operation.id,
            str(local_operation_id),
            {
                "phase": "production",
                "adapter": "local",
                "local_operation_id": str(local_operation_id),
                "service": service,
                "plan_digest": digest,
            },
            source="submission",
        )
        log = log.bind(
            local_operation_id=str(local_operation_id),
            service=service,
            site_id=self.profile.site,
        )
        await log.ainfo(
            "postal_local_submission_committed",
            capacity_decision=capacity_decision.value,
        )
        if capacity_decision is CapacityDecision.REJECT:

            async def reject_capacity(connection: Any, context: Any) -> None:
                del context
                await self.repository.reject_capacity(connection, operation.id)

            await self.tracking.feedback(
                operation.id,
                operation.binding.adapter,
                f"capacity-rejected:{local_operation_id}",
                None,
                lambda state, update: Reduction(
                    state={**state, "phase": "capacity_rejected"},
                    outcomes=(OutcomeEmission("capacity_rejected"),),
                    operation_resolved=True,
                ),
                "postal-local-capacity",
                transaction_mutation=reject_capacity,
            )
            await log.awarning("postal_capacity_rejected", outcome="capacity_rejected")
            return CapabilityResult(outcomes_persisted=True)
        return CapabilityResult(outcomes_persisted=True, waiting_feedback=True)


class LocalPostalAdapterFactory:
    def __init__(
        self,
        *,
        profiles: Mapping[str, Mapping[str, Any]],
        capacity_pools: Mapping[str, Mapping[str, Any]],
        repository: LocalPostalRepository,
        artifacts: PostalArtifactStore,
        tracking: TrackedCapabilityService,
        instruction_resolvers: Mapping[str, ProductionInstructionResolver],
    ) -> None:
        self.profiles = profiles
        self.capacity_pools = capacity_pools
        self.repository = repository
        self.artifacts = artifacts
        self.tracking = tracking
        self.instruction_resolvers = instruction_resolvers

    def validate(self, configuration: Mapping[str, Any]) -> None:
        profile_id = configuration.get("local_profile")
        pool_id = configuration.get("capacity_pool")
        if not isinstance(profile_id, str) or profile_id not in self.profiles:
            raise ValueError("Local postal destination references an unknown profile")
        if not isinstance(pool_id, str) or pool_id not in self.capacity_pools:
            raise ValueError(
                "Local postal destination references an unknown capacity pool"
            )
        if configuration.get("execution_mode", "tracked") != "tracked":
            raise ValueError("Local postal adapter execution mode must be tracked")
        profile = self._profile(profile_id)
        if profile.provider_adapter not in self.instruction_resolvers:
            raise ValueError(
                "Local postal profile references an unknown provider adapter"
            )
        self.instruction_resolvers[profile.provider_adapter].validate_profile(
            profile.provider_profile
        )
        self._pool(pool_id)

    def create(self, configuration: Mapping[str, Any]) -> LocalPostalAdapter:
        self.validate(configuration)
        profile = self._profile(configuration["local_profile"])
        return LocalPostalAdapter(
            configuration=configuration,
            profile=profile,
            capacity_pool=self._pool(configuration["capacity_pool"]),
            repository=self.repository,
            artifacts=self.artifacts,
            tracking=self.tracking,
            instruction_resolver=self.instruction_resolvers[profile.provider_adapter],
        )

    def _profile(self, identifier: str) -> LocalProfile:
        value = self.profiles[identifier]
        revision = value.get("revision")
        site = value.get("site")
        default_print = value.get("default_print")
        provider = value.get("provider")
        physical = value.get("physical")
        traveller_revision = value.get("traveller_template_revision")
        if (
            not isinstance(revision, str)
            or not revision
            or not isinstance(site, str)
            or not site
            or not isinstance(default_print, Mapping)
            or not isinstance(provider, Mapping)
            or not isinstance(physical, Mapping)
            or not isinstance(traveller_revision, str)
            or not traveller_revision
        ):
            raise ValueError(f"Local postal profile {identifier} is invalid")
        provider_adapter = provider.get("adapter")
        provider_profile = provider.get("profile")
        if (
            not isinstance(provider_adapter, str)
            or not provider_adapter
            or not isinstance(provider_profile, str)
            or not provider_profile
        ):
            raise ValueError(
                "Local postal profile requires provider adapter and profile references"
            )
        try:
            sheet_weight_g = Decimal(str(physical["sheet_weight_g"]))
            envelope_weight_g = Decimal(str(physical["envelope_weight_g"]))
        except (InvalidOperation, KeyError, ValueError) as ex:
            raise ValueError(
                "Postal physical profile requires Decimal weight values"
            ) from ex
        if sheet_weight_g <= 0 or envelope_weight_g <= 0:
            raise ValueError("Postal physical weights must be positive")
        return LocalProfile(
            identifier,
            revision,
            site,
            dict(default_print),
            provider_adapter,
            provider_profile,
            traveller_revision,
            sheet_weight_g=sheet_weight_g,
            envelope_weight_g=envelope_weight_g,
        )

    def _pool(self, identifier: str) -> CapacityPool:
        value = self.capacity_pools[identifier]
        try:
            return CapacityPool(
                identifier,
                str(value["timezone"]),
                int(value["daily_admission_limit"]),
                CapacityOverflow(value.get("overflow", "queue")),
            )
        except (KeyError, TypeError, ValueError) as ex:
            raise ValueError(
                f"Local postal capacity pool {identifier} is invalid"
            ) from ex
