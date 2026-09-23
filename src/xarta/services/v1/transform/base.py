from __future__ import annotations

from typing import TYPE_CHECKING

from sanic import Blueprint
from sanic import response

from xarta import env
from xarta.http.sessions import HTTPRequestManager
from xarta.storage.configuration import initialize_temporary_storage

from .gotenberg import GotenbergTransformClient
from .nats import TransformComponents
from .nats import TransformNATSModel

if TYPE_CHECKING:
    from sanic import HTTPResponse
    from sanic import Request
    from sanic import Sanic


bp = Blueprint(name="transform-v1", url_prefix="/api/v1/transform")

env.verify(
    blueprint=bp,
    required={
        "NATS_SERVERS",
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
async def _setup_transform(app: Sanic) -> None:
    await initialize_temporary_storage()
    session = HTTPRequestManager.__session__
    if session is None:
        raise env.ConfigurationError("HTTP session is not available")
    try:
        endpoint = env.extract(
            "TRANSFORM_GOTENBERG_ENDPOINT",
            default="http://template-engine-gotenberg:3000",
            dtype=str,
        )
        timeout = env.extract("TRANSFORM_GOTENBERG_TIMEOUT", default=30.0, dtype=float)
        max_bytes = env.extract(
            "TRANSFORM_MAX_BYTES",
            default=50 * 1024 * 1024,
            dtype=int,
        )
        if not endpoint:
            raise ValueError("Transform Gotenberg endpoint must be non-empty")
        app.ctx.transform_components = TransformComponents(
            client=GotenbergTransformClient(session, endpoint, timeout, max_bytes),
            max_bytes=max_bytes,
        )
    except (TypeError, ValueError) as ex:
        raise env.ConfigurationError(str(ex)) from ex
    await TransformNATSModel.register(app=app, components=app.ctx.transform_components)


@bp.route("/", methods=["GET"])
async def transform(_: Request) -> HTTPResponse:
    return response.empty()
