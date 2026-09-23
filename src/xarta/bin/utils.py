r"""
Generic utilities for bootstrapping applications.
"""

from __future__ import annotations

from sanic import Sanic

from xarta import http
from xarta import json
from xarta import logging
from xarta import telemetry
from xarta.storage.configuration import close_temporary_storage


def create_app(name: str) -> Sanic:
    r"""
    Factory method to create a Sanic application with our custom defaults.
    """
    app = Sanic(name, dumps=json.dumps, loads=json.loads)
    app.config.FALLBACK_ERROR_FORMAT = "json"
    app.ctx.logger = logging.logger
    telemetry.initialize(app)
    http.initialize_http_sessions(app)

    async def close_temporary_storage_backend(_: Sanic) -> None:
        """Release the lazily opened temporary-storage client during shutdown."""
        await close_temporary_storage()

    app.register_listener(close_temporary_storage_backend, "after_server_stop")

    return app
