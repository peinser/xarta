r"""
Base blueprint definition of the v1 Bundle API.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sanic import Blueprint

from xarta import cache
from xarta import env

from .nats import BundleDAGNATSModel
from .nats import BundleNATSModel

if TYPE_CHECKING:
    from sanic import Sanic


bp: Blueprint = Blueprint(
    name="bundle-v1",
    url_prefix="/api/v1/bundle",
)


env.verify(
    blueprint=bp,
    required={
        "ARCHIVE_SERVICE_ENDPOINT",
        "ARCHIVE_SERVICE_TIMEOUT",
        "BUNDLE_STORAGE",
        "CACHE_CONFIG_PATH",
        "NATS_SERVERS",
        "TMP_STORAGE",
    },
)


@bp.listener("before_server_start")
async def _setup_nats(app: Sanic) -> None:
    await BundleNATSModel.register(app=app)
    await BundleDAGNATSModel.register(app=app)


@bp.listener("before_server_start")
async def _setup_cache(app: Sanic) -> None:
    await cache.register()
