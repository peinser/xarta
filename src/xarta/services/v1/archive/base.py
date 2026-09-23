r"""
Base blueprint definition of the v1 archive API.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sanic import Blueprint

from xarta import cache
from xarta import env
from xarta.adapters import AdapterRegistry
from xarta.protocol.dag.archive.constants import ARCHIVE_SERVICE_ENDPOINT
from xarta.protocol.dag.archive.constants import ARCHIVE_SERVICE_TIMEOUT
from xarta.tracking import DestinationRegistry

from .adapters import ArchiveAdapter
from .adapters import XartaHTTPArchiveAdapterFactory
from .configuration import load_archive_destinations
from .configuration import load_archive_storage
from .storage import ArchiveStorageRegistry
from .storage import ArchiveStorageSessions

if TYPE_CHECKING:
    from typing import Final

    from sanic import Sanic


bp = Blueprint(
    name="archive-v1",
    url_prefix="/api/v1/archive",
)


env.verify(
    blueprint=bp,
    required={
        "ARCHIVE_STORAGE",
        "DOCUMENT_TYPE_SERVICE_ENDPOINT",
        "DOCUMENT_TYPE_SERVICE_TIMEOUT",
        "CACHE_CONFIG_PATH",
        "TMP_STORAGE",
        "NATS_SERVERS",
        "ARCHIVE_POSTGRESQL_USER",
        "ARCHIVE_POSTGRESQL_PASSWORD",
        "ARCHIVE_POSTGRESQL_DATABASE",
        "ARCHIVE_POSTGRESQL_HOST",
    },
)


ARCHIVE_STORAGE: Final[str] = env.extract(
    key="ARCHIVE_STORAGE",
    default="archive",
    dtype=str,
)

ARCHIVE_CONFIGURATIONS_CONFIG_PATH: Final[str | None] = env.extract(
    key="ARCHIVE_CONFIGURATIONS_CONFIG_PATH",
)

ARCHIVE_STORAGE_CONFIG_PATH: Final[str | None] = env.extract(
    key="ARCHIVE_STORAGE_CONFIG_PATH",
)


@dataclass(frozen=True)
class ArchiveComponents:
    destinations: DestinationRegistry
    adapters: AdapterRegistry[ArchiveAdapter]


def create_archive_components(
    configurations: dict | None = None,
) -> ArchiveComponents:
    if configurations is None:
        configurations = {
            "default-archive": {
                "kind": "archive",
                "adapter": "xarta-http-archive",
                "current_revision": "default",
                "revisions": {
                    "default": {
                        "endpoint": ARCHIVE_SERVICE_ENDPOINT,
                        "timeout": ARCHIVE_SERVICE_TIMEOUT,
                        "concurrency": 10,
                    }
                },
            }
        }
    defaults = (
        {"archive": "default-archive"} if "default-archive" in configurations else {}
    )
    destinations = DestinationRegistry(
        configurations, defaults, require_explicit_revisions=True
    )
    adapters = AdapterRegistry[ArchiveAdapter](
        {"xarta-http-archive": XartaHTTPArchiveAdapterFactory()}
    )
    for resolved in destinations.iter_resolved():
        if resolved.binding.capability != "archive":
            raise ValueError(
                f"Archive destination {resolved.binding.destination} has non-archive kind"
            )
        adapters.validate(resolved.binding.adapter, resolved.configuration)
    return ArchiveComponents(destinations=destinations, adapters=adapters)


@bp.listener("before_server_start")
async def _setup_database(app: Sanic) -> None:
    from .db import ArchivePostgresModel as DB

    await DB.register(app)


@bp.listener("before_server_start")
async def _load_components(app: Sanic) -> None:
    try:
        if not hasattr(app.ctx, "archive_components"):
            configurations = (
                dict(
                    await load_archive_destinations(ARCHIVE_CONFIGURATIONS_CONFIG_PATH)
                )
                if ARCHIVE_CONFIGURATIONS_CONFIG_PATH
                else None
            )
            app.ctx.archive_components = create_archive_components(configurations)
        if not hasattr(app.ctx, "archive_storage"):
            if not hasattr(app.ctx, "archive_sessions"):
                app.ctx.archive_sessions = ArchiveStorageSessions()
            storage_configuration = (
                await load_archive_storage(ARCHIVE_STORAGE_CONFIG_PATH)
                if ARCHIVE_STORAGE_CONFIG_PATH
                else {}
            )
            app.ctx.archive_storage = ArchiveStorageRegistry(
                storage_configuration,
                ARCHIVE_STORAGE,
                app.ctx.archive_sessions,
            )
    except (TypeError, ValueError) as ex:
        raise env.ConfigurationError(str(ex)) from ex


@bp.listener("before_server_start")
async def _setup_nats(app: Sanic) -> None:
    from .nats import ArchiveNATSModel
    from .nats import WaitForArchiveNATSModel

    if not hasattr(app.ctx, "archive_components"):
        raise RuntimeError("Archive components have not been loaded")
    await ArchiveNATSModel.register(app=app, components=app.ctx.archive_components)
    await WaitForArchiveNATSModel.register(app=app)


@bp.listener("before_server_start")
async def _setup_cache(app: Sanic) -> None:
    await cache.register()


@bp.listener("after_server_stop")
async def _close_archive_storage(app: Sanic) -> None:
    """Close process-lifetime archive clients after request handling has stopped."""
    sessions = getattr(app.ctx, "archive_sessions", None)
    if sessions is not None:
        await sessions.close()
