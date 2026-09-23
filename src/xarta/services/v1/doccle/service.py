from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Any

from xarta.services.v1.doccle.adapters import DoccleAmbiguousTransportError
from xarta.services.v1.doccle.adapters import DoccleSafePreTransmissionError
from xarta.services.v1.doccle.models import DoccleAdapter
from xarta.services.v1.doccle.models import DoccleReceiver
from xarta.services.v1.doccle.models import DoccleReceiverProfile
from xarta.services.v1.doccle.models import DoccleResultCategory
from xarta.services.v1.doccle.models import ReceiverState
from xarta.tracking import DestinationRegistry

if TYPE_CHECKING:
    from collections.abc import Mapping

    from xarta.adapters import AdapterRegistry
    from xarta.services.v1.doccle.repositories import ReceiverRepository


@dataclass(frozen=True, slots=True)
class DoccleComponents:
    destinations: DestinationRegistry
    adapters: AdapterRegistry[DoccleAdapter]
    sender: str

    def resolve_sender(self):
        return self.destinations.resolve("doccle", self.sender)


def build_doccle_components(
    configuration: Mapping[str, Any],
    adapters: AdapterRegistry[DoccleAdapter],
) -> DoccleComponents:
    current_sender = configuration["current_sender"]
    normalized = {
        sender: {"kind": "doccle", **dict(sender_configuration)}
        for sender, sender_configuration in configuration["senders"].items()
    }
    destinations = DestinationRegistry(normalized, require_explicit_revisions=True)
    for resolved in destinations.iter_resolved():
        if resolved.binding.capability != "doccle":
            raise ValueError(
                f"Destination {resolved.binding.destination} is not valid for doccle"
            )
        adapters.validate(resolved.binding.adapter, resolved.configuration)
    destinations.resolve("doccle", current_sender)
    return DoccleComponents(destinations, adapters, current_sender)


class ReceiverService:
    def __init__(
        self, repository: ReceiverRepository, components: DoccleComponents
    ) -> None:
        self.repository = repository
        self.components = components

    async def resolve(self, subject: Mapping[str, Any]) -> DoccleReceiver | None:
        return await self.repository.load_by_subject(
            self.components.sender, dict(subject)
        )

    async def ensure(
        self,
        subject: Mapping[str, Any],
        profile: DoccleReceiverProfile,
    ) -> DoccleReceiver:
        resolved = self.components.resolve_sender()
        destination = resolved.binding.destination
        receiver = await self.repository.insert_or_load(destination, dict(subject))
        if receiver.state is ReceiverState.PROVISIONED:
            return receiver

        # The lease exceeds the bounded provider timeout. A process may die while
        # the PUT is in flight, but a later caller can reclaim the same Receiver ID.
        lease_seconds = max(float(resolved.configuration.get("timeout", 15)) * 2, 30)
        claimed = await self.repository.claim_provisioning(
            receiver.id, lease_seconds=lease_seconds
        )
        if claimed is None:
            current = await self.repository.load(receiver.id)
            if current is None:
                raise RuntimeError("Claimed Doccle receiver disappeared")
            return current
        if claimed.provisioning_lease_token is None:
            raise RuntimeError("Doccle provisioning claim has no lease token")

        adapter = self.components.adapters.create(
            resolved.binding.adapter, resolved.configuration
        )
        try:
            result = await adapter.create_or_update_receiver(
                receiver_id=claimed.external_receiver_id, profile=profile
            )
        except DoccleAmbiguousTransportError as ex:
            updated = await self.repository.complete_provisioning(
                claimed.id,
                claimed.provisioning_lease_token,
                ReceiverState.UNCERTAIN,
                error={"category": "ambiguous", "message": str(ex)},
            )
        except DoccleSafePreTransmissionError as ex:
            updated = await self.repository.complete_provisioning(
                claimed.id,
                claimed.provisioning_lease_token,
                ReceiverState.FAILED,
                error={"category": "safe_pretransmission", "message": str(ex)},
            )
        else:
            provisioned = result.category is DoccleResultCategory.PROVISIONED
            updated = await self.repository.complete_provisioning(
                claimed.id,
                claimed.provisioning_lease_token,
                ReceiverState.PROVISIONED if provisioned else ReceiverState.FAILED,
                error=(
                    None
                    if provisioned
                    else {
                        "category": result.category.value,
                        "provider_status": result.provider_status,
                    }
                ),
            )
        if updated is None:
            raise RuntimeError(
                "Doccle receiver provisioning state changed unexpectedly"
            )
        return updated
