from __future__ import annotations

import hashlib

from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING

from xarta.adapters import AdapterRegistry
from xarta.exceptions.protocol import TemporaryError
from xarta.logging import log_capability_adapter_selected
from xarta.nats.sanic import SanicNATSSynchronousRequestsConsumerModel
from xarta.protocol.dag import CapabilityResult
from xarta.protocol.dag import CommonOutcome
from xarta.protocol.dag import NodeTask
from xarta.protocol.dag import OutcomeEmission
from xarta.protocol.dag import OutcomeSubject
from xarta.protocol.dag.sftp import SFTPNode
from xarta.services.v1.sftp.adapters import AsyncSSHSFTPAdapterFactory
from xarta.services.v1.sftp.adapters import SFTPAdapter
from xarta.services.v1.sftp.adapters import SFTPCommitUncertainError
from xarta.services.v1.sftp.adapters import SFTPTransportError
from xarta.tracking import DestinationRegistry

if TYPE_CHECKING:
    from collections.abc import Mapping
    from typing import Any


@dataclass(frozen=True)
class SFTPComponents:
    destinations: DestinationRegistry
    adapters: AdapterRegistry[SFTPAdapter]


def build_sftp_components(
    configurations: Mapping[str, Mapping[str, Any]],
    adapters: AdapterRegistry[SFTPAdapter] | None = None,
) -> SFTPComponents:
    adapter_registry = adapters or AdapterRegistry(
        {"asyncssh-sftp": AsyncSSHSFTPAdapterFactory()}
    )
    destinations = DestinationRegistry(configurations, require_explicit_revisions=True)
    for resolved in destinations.iter_resolved():
        if resolved.binding.capability != "sftp":
            raise ValueError(
                f"Destination {resolved.binding.destination} is not valid for sftp"
            )
        adapter_registry.validate(
            resolved.binding.adapter,
            resolved.configuration,
        )
    return SFTPComponents(destinations=destinations, adapters=adapter_registry)


async def _worker(
    node: SFTPNode,
    task: NodeTask,
    *,
    components: SFTPComponents,
    **kwargs,
) -> CapabilityResult:
    document_source, relative_path = node.interpret()
    document = await document_source.retrieve()
    checksum = hashlib.sha256(document.data).hexdigest()
    destination = node.destination or "default-sftp"
    resolved = components.destinations.resolve("sftp", destination)
    adapter = components.adapters.create(
        resolved.binding.adapter, resolved.configuration
    )
    log = kwargs.get("logger")
    if log is not None:
        await log_capability_adapter_selected(log, resolved.binding, "synchronous")

    async def before_commit() -> None:
        return None

    try:
        submission = await adapter.upload(
            idempotency_key=str(task.node_execution_id),
            document=document,
            relative_path=relative_path,
            before_commit=before_commit,
        )
    except SFTPCommitUncertainError:
        outcome = CommonOutcome.OUTCOME_UNCERTAIN
    except SFTPTransportError as ex:
        raise TemporaryError("SFTP transport failed", delay=5) from ex
    else:
        outcome = submission.outcome
    return CapabilityResult(
        (
            OutcomeEmission(
                outcome=outcome,
                subject=OutcomeSubject(kind="document", id=str(document.id)),
                details={
                    "path": relative_path,
                    "size": len(document.data),
                    "sha256": checksum,
                },
            ),
        )
    )


class SFTPNATSModel(SanicNATSSynchronousRequestsConsumerModel):
    @classmethod
    async def register(  # type: ignore[override]
        cls, app, components: SFTPComponents, **kwargs
    ) -> None:
        await super().register(
            app=app,
            fn=partial(_worker, components=components),
            name=SFTPNode.KIND,
            **kwargs,
        )
