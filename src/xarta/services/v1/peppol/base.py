from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Any
from typing import cast

from sanic import Blueprint
from sanic import response

from xarta import env
from xarta.adapters import AdapterRegistry
from xarta.services.v1.peppol.api import e_invoice_be_callback
from xarta.services.v1.peppol.api import participant_registration
from xarta.services.v1.peppol.api import peppol_operation_status
from xarta.services.v1.peppol.api import recommand_callback
from xarta.services.v1.peppol.configuration import load_peppol_destinations
from xarta.services.v1.peppol.discovery import PeppolParticipantDiscovery
from xarta.services.v1.peppol.e_invoice_be import EInvoiceBePeppolAdapterFactory
from xarta.services.v1.peppol.recommand import RecommandPeppolAdapterFactory
from xarta.services.v1.peppol.service import build_peppol_components
from xarta.tracking import TrackingPostgresModel

if TYPE_CHECKING:
    from xarta.services.v1.peppol.models import PeppolAdapter


bp = Blueprint(name="peppol-v1", url_prefix="/api/v1/peppol")

env.verify(
    blueprint=bp,
    required={
        "NATS_SERVERS",
        "TMP_STORAGE",
        "PEPPOL_CONFIGURATIONS_CONFIG_PATH",
        "ARCHIVE_SERVICE_ENDPOINT",
        "ARCHIVE_SERVICE_TIMEOUT",
        "BUNDLE_SERVICE_ENDPOINT",
        "BUNDLE_SERVICE_TIMEOUT",
        "RENDER_SERVICE_ENDPOINT",
        "RENDER_SERVICE_TIMEOUT",
    },
)
env.verify_postgresql(bp, "TRACKING")


@bp.listener("before_server_start")
async def _setup_peppol(app) -> None:
    from xarta.services.v1.peppol.nats import PeppolNATSModel
    from xarta.services.v1.peppol.nats import RecommandCallbackNATSModel

    await TrackingPostgresModel.register(app)
    if not hasattr(app.ctx, "peppol_discovery"):
        try:
            app.ctx.peppol_discovery = await PeppolParticipantDiscovery.open(
                environment=env.extract(
                    "PEPPOL_DISCOVERY_ENVIRONMENT", default="production", dtype=str
                ),
                dns_timeout=env.extract(
                    "PEPPOL_DISCOVERY_DNS_TIMEOUT", default="5", dtype=float
                ),
                http_timeout=env.extract(
                    "PEPPOL_DISCOVERY_HTTP_TIMEOUT", default="10", dtype=float
                ),
                total_timeout=env.extract(
                    "PEPPOL_DISCOVERY_TOTAL_TIMEOUT", default="20", dtype=float
                ),
                concurrency=env.extract(
                    "PEPPOL_DISCOVERY_CONCURRENCY", default="10", dtype=int
                ),
            )
        except ValueError as ex:
            raise env.ConfigurationError(str(ex)) from ex
    if not hasattr(app.ctx, "peppol_components"):
        path = env.extract("PEPPOL_CONFIGURATIONS_CONFIG_PATH", optional=False)
        configurations = await load_peppol_destinations(path)
        adapters: AdapterRegistry[PeppolAdapter] = AdapterRegistry(
            {
                "e-invoice-be-rest": cast(
                    "Any",
                    EInvoiceBePeppolAdapterFactory(app.ctx.http_client_session),
                ),
                "recommand-rest": cast(
                    "Any",
                    RecommandPeppolAdapterFactory(app.ctx.http_client_session),
                ),
            }
        )
        app.ctx.peppol_components = build_peppol_components(configurations, adapters)
    await PeppolNATSModel.register(app=app, components=app.ctx.peppol_components)
    await RecommandCallbackNATSModel.register(
        app=app, components=app.ctx.peppol_components
    )


@bp.get("/")
async def health(_):
    return response.empty()


@bp.listener("after_server_stop")
async def _close_discovery(app) -> None:
    discovery = getattr(app.ctx, "peppol_discovery", None)
    if discovery is not None:
        await discovery.close()
        del app.ctx.peppol_discovery


bp.add_route(e_invoice_be_callback, "/callbacks/e-invoice-be", methods=["POST"])
bp.add_route(recommand_callback, "/callbacks/recommand", methods=["POST"])
bp.add_route(
    participant_registration,
    "/participants/<scheme:str>/<identifier:str>/registration",
    methods=["GET"],
)
bp.add_route(
    peppol_operation_status,
    "/operations/<operation_id:str>",
    methods=["GET"],
)
