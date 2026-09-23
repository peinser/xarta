from __future__ import annotations

from typing import TYPE_CHECKING

from sanic import Blueprint
from sanic import response

from xarta import env
from xarta.adapters import AdapterRegistry
from xarta.services.v1.search.adapters import ElasticsearchSearchAdapterFactory
from xarta.services.v1.search.adapters import PostgreSQLSearchAdapterFactory
from xarta.services.v1.search.configuration import load_search_destinations
from xarta.services.v1.search.db import SearchPostgresModel
from xarta.services.v1.search.nats import build_search_components

if TYPE_CHECKING:
    from sanic import HTTPResponse
    from sanic import Request
    from sanic import Sanic

    from xarta.services.v1.search.adapters import SearchAdapter


bp = Blueprint(name="search-v1", url_prefix="/api/v1/search")

env.verify(
    blueprint=bp,
    required={
        "NATS_SERVERS",
        "SEARCH_CONFIGURATIONS_CONFIG_PATH",
        "POSTGRESQL_USER",
        "POSTGRESQL_PASSWORD",
        "POSTGRESQL_DATABASE",
        "POSTGRESQL_HOST",
        "TMP_STORAGE",
    },
)


@bp.listener("before_server_start")
async def _setup_search(app: Sanic) -> None:
    from xarta.services.v1.search.nats import SearchArchiveLifecycleNATSModel
    from xarta.services.v1.search.nats import SearchNATSModel

    await SearchPostgresModel.register(app)
    if not hasattr(app.ctx, "search_components"):
        try:
            path = env.extract("SEARCH_CONFIGURATIONS_CONFIG_PATH", optional=False)
            configurations = await load_search_destinations(path)
            adapters: AdapterRegistry[SearchAdapter] = AdapterRegistry(
                {
                    "postgresql-search": PostgreSQLSearchAdapterFactory(
                        SearchPostgresModel.pool()
                    ),
                    "elasticsearch": ElasticsearchSearchAdapterFactory(
                        app.ctx.http_client_session
                    ),
                }
            )
            app.ctx.search_components = build_search_components(
                configurations, adapters
            )
        except (TypeError, ValueError) as ex:
            raise env.ConfigurationError(str(ex)) from ex
    await SearchNATSModel.register(app=app, components=app.ctx.search_components)
    await SearchArchiveLifecycleNATSModel.register(
        app=app, components=app.ctx.search_components
    )


@bp.route("/", methods=["GET"])
async def search(_: Request) -> HTTPResponse:
    """Health-only endpoint; querying the index is intentionally out of scope."""

    return response.empty()
