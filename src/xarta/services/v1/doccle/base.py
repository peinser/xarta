from __future__ import annotations

from typing import TYPE_CHECKING

from sanic import Blueprint
from sanic import response

from xarta import env
from xarta.adapters import AdapterRegistry
from xarta.services.v1.doccle.adapters import DoccleSenderRESTAdapterFactory
from xarta.services.v1.doccle.api import ensure_receiver
from xarta.services.v1.doccle.api import get_receiver
from xarta.services.v1.doccle.api import receiver_callback
from xarta.services.v1.doccle.api import resolve_receiver
from xarta.services.v1.doccle.callbacks import parse_certificate_fingerprints
from xarta.services.v1.doccle.configuration import load_doccle_destinations
from xarta.services.v1.doccle.postgresql import DocclePostgresModel
from xarta.services.v1.doccle.repositories import ReceiverRepository
from xarta.services.v1.doccle.service import ReceiverService
from xarta.services.v1.doccle.service import build_doccle_components
from xarta.tracking import TrackingPostgresModel

if TYPE_CHECKING:
    from sanic import Request
    from sanic import Sanic

    from xarta.services.v1.doccle.models import DoccleAdapter


bp = Blueprint(name="doccle-v1", url_prefix="/api/v1/doccle")

env.verify(
    blueprint=bp,
    required={
        "NATS_SERVERS",
        "TMP_STORAGE",
        "DOCCLE_CONFIGURATIONS_CONFIG_PATH",
        "DOCCLE_CONTROL_PLANE_TOKEN",
    },
)
env.verify_postgresql(bp, "TRACKING")
env.verify_postgresql(bp, "DOCCLE")


@bp.listener("before_server_start")
async def _setup_doccle(app: Sanic) -> None:
    from xarta.services.v1.doccle.nats import DoccleNATSModel

    await TrackingPostgresModel.register(app)
    await DocclePostgresModel.register(app)
    if not hasattr(app.ctx, "doccle_components"):
        try:
            path = env.extract("DOCCLE_CONFIGURATIONS_CONFIG_PATH", optional=False)
            configurations = await load_doccle_destinations(path)
            adapters: AdapterRegistry[DoccleAdapter] = AdapterRegistry(
                {
                    "doccle-sender-rest": DoccleSenderRESTAdapterFactory(
                        app.ctx.http_client_session
                    )
                }
            )
            app.ctx.doccle_components = build_doccle_components(
                configurations,
                adapters,
            )
            app.ctx.doccle_control_plane_token = env.extract(
                "DOCCLE_CONTROL_PLANE_TOKEN", optional=False
            )
            callbacks_enabled = env.extract("DOCCLE_CALLBACKS_ENABLED", default="false")
            if callbacks_enabled.lower() not in {"true", "false"}:
                raise ValueError("DOCCLE_CALLBACKS_ENABLED must be true or false")
            app.ctx.doccle_callbacks_enabled = callbacks_enabled.lower() == "true"
            app.ctx.doccle_callback_certificate_fingerprints = (
                parse_certificate_fingerprints(
                    env.extract(
                        "DOCCLE_CALLBACK_CERTIFICATE_FINGERPRINTS", optional=False
                    )
                )
                if app.ctx.doccle_callbacks_enabled
                else frozenset()
            )
        except (TypeError, ValueError) as ex:
            raise env.ConfigurationError(str(ex)) from ex

    pool = DocclePostgresModel.pool()
    app.ctx.doccle_receiver_repository = ReceiverRepository(pool)
    app.ctx.doccle_receiver_service = ReceiverService(
        app.ctx.doccle_receiver_repository, app.ctx.doccle_components
    )
    await DoccleNATSModel.register(
        app=app,
        components=app.ctx.doccle_components,
        receiver_repository=app.ctx.doccle_receiver_repository,
    )


@bp.get("/")
async def health(_: Request):
    return response.empty()


bp.add_route(resolve_receiver, "/receivers/resolve", methods=["POST"])
bp.add_route(ensure_receiver, "/receivers/ensure", methods=["PUT"])
bp.add_route(get_receiver, "/receivers/<receiver_id:str>", methods=["GET"])
bp.add_route(receiver_callback, "/callbacks", methods=["POST"])
