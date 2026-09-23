r"""
Base blueprint definition of the v1 Webhook API.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sanic import Blueprint
from sanic import response

from xarta import env

from .nats import WebhookNATSModel as NATS

if TYPE_CHECKING:
    from sanic import HTTPResponse
    from sanic import Request
    from sanic import Sanic


bp: Blueprint = Blueprint(
    name="webhook-v1",
    url_prefix="/api/v1/webhook",
)


env.verify(
    blueprint=bp,
    required={
        "NATS_SERVERS",
        "TMP_STORAGE",
        "RENDER_SERVICE_ENDPOINT",
        "RENDER_SERVICE_TIMEOUT",
        "ARCHIVE_SERVICE_ENDPOINT",
        "ARCHIVE_SERVICE_TIMEOUT",
        "BUNDLE_SERVICE_ENDPOINT",
        "BUNDLE_SERVICE_TIMEOUT",
    },
)


@bp.listener("before_server_start")
async def _setup_nats(app: Sanic) -> None:
    r"""
    A method which configures the connection with NATS and the Jetstream component.

    Note: additions to @bp.listener will be executed sequently (unless an await is present).
    """
    await NATS.register(app=app)


@bp.route("/", methods=["GET", "POST"])
async def reflect(request: Request) -> HTTPResponse:
    return response.empty()
