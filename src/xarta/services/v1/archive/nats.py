r"""
Eventing model and utilities for the archive service.
"""

from __future__ import annotations

import asyncio
import datetime

from typing import TYPE_CHECKING
from uuid import UUID
from uuid import uuid5

import orjson

from xarta.exceptions.protocol import TemporaryError
from xarta.logging import log_capability_adapter_selected
from xarta.nats.sanic import SanicNATSSynchronousRequestsConsumerModel
from xarta.protocol.dag import CapabilityResult
from xarta.protocol.dag import NodeTask
from xarta.protocol.dag import OutcomeEmission
from xarta.protocol.dag import OutcomeSubject
from xarta.protocol.dag.archive import ArchiveNode
from xarta.protocol.dag.waitfor import WaitForNode
from xarta.telemetry import nats_producer_span

from .adapters import ArchiveRepresentationSubmission
from .adapters import ArchiveTransportError
from .adapters import ArchiveVersionSubmission
from .db import ArchivePostgresModel as DB


async def publish_deletion_command(jetstream, command) -> None:
    subject = "archive.commands.delete"
    headers = {"Nats-Msg-Id": str(command.message_id)}
    with nats_producer_span(subject, headers):
        await jetstream.publish(
            subject,
            orjson.dumps(command.dict()),
            headers=headers,
        )


if TYPE_CHECKING:
    from typing import Final

    from nats.aio.msg import Msg
    from sanic import Sanic

    from .base import ArchiveComponents


def _submission_result(state) -> CapabilityResult:
    results = state.get("results", ())
    if results:
        outcomes = tuple(
            OutcomeEmission(
                outcome=result["outcome"],
                subject=OutcomeSubject(kind="document", id=result["document_id"]),
                details={"version_id": result["version_id"]},
            )
            for result in results
        )
    else:
        outcomes = tuple(
            OutcomeEmission(outcome=outcome) for outcome in state.get("outcomes", ())
        )
    aggregate = "failure" if state.get("errors") else "success"
    outcomes = (*outcomes, OutcomeEmission(outcome=aggregate))
    return CapabilityResult(outcomes=outcomes)


async def _worker(
    node: ArchiveNode,
    task: NodeTask,
    components: ArchiveComponents,
    **kwargs,
) -> CapabilityResult:
    assert isinstance(node, ArchiveNode)
    resolved = components.destinations.resolve("archive", node.destination)
    adapter = components.adapters.create(
        resolved.binding.adapter, resolved.configuration
    )
    log = kwargs.get("logger")
    if log is not None:
        await log_capability_adapter_selected(log, resolved.binding, "synchronous")
    versions = []
    for document_index, specification in enumerate(node.documents):
        source_results = await asyncio.gather(
            *(item.source.retrieve() for item in specification.representations)
        )
        document_id = specification.document_id or uuid5(
            task.node_execution_id, f"archive-document:{document_index}"
        )
        version_id = specification.version_id or uuid5(
            task.node_execution_id,
            f"archive-version:{document_index}:{document_id}",
        )
        representations = tuple(
            ArchiveRepresentationSubmission(
                representation_id=(
                    representation.representation_id
                    or uuid5(
                        task.node_execution_id,
                        f"archive-representation:{document_index}:{representation_index}:{document_id}",
                    )
                ),
                content_type=source.content_type,
                data=source.data,
                name=representation.name,
                metadata=representation.metadata,
            )
            for representation_index, (representation, source) in enumerate(
                zip(specification.representations, source_results, strict=True)
            )
        )
        default_representation_id = (
            specification.default_representation_id
            or representations[0].representation_id
        )
        versions.append(
            ArchiveVersionSubmission(
                archive=specification.archive
                or str(resolved.configuration.get("archive", "default")),
                document_id=document_id,
                version_id=version_id,
                parent_version_id=specification.parent_version_id,
                created=specification.created or task.created_at,
                expires=specification.expires,
                document_type=specification.document_type,
                metadata=specification.metadata,
                default_representation_id=default_representation_id,
                representations=representations,
            )
        )
    try:
        submission = await adapter.submit(
            idempotency_key=str(task.node_execution_id), versions=versions
        )
    except ArchiveTransportError as ex:
        raise TemporaryError(
            "Archive destination is temporarily unavailable",
            delay=5,
            auto_replay=True,
            backoff_multiplier=2,
        ) from ex
    return _submission_result(submission.state)


class ArchiveNATSModel(SanicNATSSynchronousRequestsConsumerModel):
    @classmethod
    async def register(  # type: ignore[override]
        cls, app: Sanic, components: ArchiveComponents, **kwargs
    ) -> None:
        async def worker(node: ArchiveNode, task: NodeTask, **worker_kwargs):
            return await _worker(node, task, components, **worker_kwargs)

        await super().register(
            app=app,
            fn=worker,
            name=ArchiveNode.KIND,
            **kwargs,
        )


class WaitForArchiveNATSModel(SanicNATSSynchronousRequestsConsumerModel):
    BACKOFFS: Final[list[float]] = [
        1.0,
        2.0,
        4.0,
        8.0,
        16.0,
        32.0,
        64.0,
        128.0,
        256.0,
        512.0,
    ]

    @classmethod
    async def register(cls, app: Sanic, **kwargs) -> None:  # type: ignore[override]
        async def _worker(node: WaitForNode, msg: Msg, **kwargs):
            assert isinstance(node, WaitForNode)

            # Interpret the semantics of the node.
            document_sources = node.interpret()
            identifiers = {source.id for source in document_sources}

            # Check if all documents exist in the archive.
            if not await DB.exists(identifiers=identifiers):
                # Determine which backoff set needs to be utilized.
                backoffs = (
                    node.backoffs if node.backoffs else WaitForArchiveNATSModel.BACKOFFS
                )

                # Check if we've surpassed the maximum allowable deliveries.
                if msg.metadata.num_delivered > len(backoffs):
                    return CapabilityResult((OutcomeEmission(outcome="failure"),))

                raise TemporaryError(
                    delay=backoffs[msg.metadata.num_delivered - 1],
                    backoff_multiplier=1,
                )

            return CapabilityResult((OutcomeEmission(outcome="success"),))

        await super().register(
            app=app,
            fn=_worker,
            name=WaitForNode.KIND,
            **kwargs,
        )
