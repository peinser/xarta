r"""
Eventing model and utilities for the generate service.
"""

from __future__ import annotations

import asyncio

from typing import TYPE_CHECKING

from xarta.nats.sanic import SanicNATSSynchronousRequestsConsumerModel
from xarta.protocol.dag import CapabilityResult
from xarta.protocol.dag import OutcomeEmission
from xarta.protocol.dag.generate import GenerateNode

if TYPE_CHECKING:
    from sanic import Sanic


async def _worker(node: GenerateNode, **kwargs) -> CapabilityResult:
    assert isinstance(node, GenerateNode)

    # Interpret the contents of the node.
    documents = node.interpret()

    # Retrieve the specified documents.
    source_results = await asyncio.gather(
        *[document_source.retrieve() for document_source in documents]
    )

    # Store the retrieved documents in the temp working directory.
    await asyncio.gather(*[result.persist() for result in source_results])

    return CapabilityResult((OutcomeEmission(outcome="success"),))


class GenerateNATSModel(SanicNATSSynchronousRequestsConsumerModel):
    @classmethod
    async def register(cls, app: Sanic, **kwargs) -> None:
        await super().register(
            app=app,
            fn=_worker,
            name="generate",
            **kwargs,
        )
