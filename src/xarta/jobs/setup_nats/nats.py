r"""
Eventing model and utilities for the NATS setup job.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import nats

from xarta.nats.sanic import SanicNATSModel

if TYPE_CHECKING:
    from sanic import Sanic


class SetupNATSModel(SanicNATSModel):
    r"""
    An  model curated for the NATS setup job.
    """

    @classmethod
    def js(cls) -> nats.js.JetStreamContext:
        return cls.jetstream()

    @classmethod
    async def register(cls, app: Sanic, **kwargs) -> None:
        r"""
        This method will serve as entrypoint to setup the necessary
        NATS and other connection details such as the database
        and name based on the specified environment variables.
        The model will be attached to a specific Sanic application.
        """
        await super().register(app=app, **kwargs)
