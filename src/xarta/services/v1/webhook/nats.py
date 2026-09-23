r"""Eventing model and utilities for webhook delivery."""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

from xarta.http.sessions import HTTPRequestManager
from xarta.nats.sanic import SanicNATSSynchronousRequestsConsumerModel
from xarta.protocol.dag import CapabilityResult
from xarta.protocol.dag import OutcomeEmission
from xarta.protocol.dag.webhook import WebhookNode

if TYPE_CHECKING:
    from aiohttp import ClientSession
    from sanic import Sanic


class WebhookNATSModel(SanicNATSSynchronousRequestsConsumerModel):
    @classmethod
    async def register(cls, app: Sanic, **kwargs) -> None:
        http_session: ClientSession = HTTPRequestManager.__session__

        await super().register(
            app=app,
            fn=partial(_worker, http_session=http_session),
            name="webhook",
            **kwargs,
        )


async def _worker(
    node: WebhookNode, http_session: ClientSession, **kwargs
) -> CapabilityResult:
    assert isinstance(node, WebhookNode)

    async with await node.interpret(session=http_session) as response:
        outcome = "success" if response.ok else "failure"
        return CapabilityResult((OutcomeEmission(outcome=outcome),))
