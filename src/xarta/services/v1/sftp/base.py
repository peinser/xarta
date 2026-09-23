from __future__ import annotations

from typing import TYPE_CHECKING

from sanic import Blueprint
from sanic import response

from xarta import env

from . import utils

if TYPE_CHECKING:
    from sanic import HTTPResponse
    from sanic import Request
    from sanic import Sanic


bp = Blueprint(name="sftp-v1", url_prefix="/api/v1/sftp")

env.verify(
    blueprint=bp,
    required={
        "NATS_SERVERS",
        "SFTP_CONFIGURATIONS_CONFIG_PATH",
        "TMP_STORAGE",
        "ARCHIVE_SERVICE_ENDPOINT",
        "ARCHIVE_SERVICE_TIMEOUT",
        "BUNDLE_SERVICE_ENDPOINT",
        "BUNDLE_SERVICE_TIMEOUT",
        "RENDER_SERVICE_ENDPOINT",
        "RENDER_SERVICE_TIMEOUT",
    },
)


@bp.listener("before_server_start")
async def _setup_nats(app: Sanic) -> None:
    from .nats import SFTPNATSModel
    from .nats import build_sftp_components

    if not hasattr(app.ctx, "sftp_components"):
        try:
            configurations = await utils.load_sftp_configurations()
            app.ctx.sftp_components = build_sftp_components(configurations)
        except (TypeError, ValueError) as ex:
            raise env.ConfigurationError(str(ex)) from ex
    await SFTPNATSModel.register(app=app, components=app.ctx.sftp_components)


@bp.route("/", methods=["GET"])
async def sftp(_: Request) -> HTTPResponse:
    return response.empty()
