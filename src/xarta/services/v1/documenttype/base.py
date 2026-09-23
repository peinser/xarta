r"""
Base blueprint definition of the v1 document type API.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sanic import Blueprint

from xarta import cache
from xarta import env

from .db import DocumentTypePostgresModel as DB

if TYPE_CHECKING:
    from sanic import Sanic


bp = Blueprint(
    name="document-type-v1",
    url_prefix="/api/v1/document-type",
)


env.verify(
    blueprint=bp,
    required={
        "POSTGRESQL_USER",
        "POSTGRESQL_PASSWORD",
        "POSTGRESQL_DATABASE",
        "POSTGRESQL_HOST",
    },
)


@bp.listener("before_server_start")
async def _setup_database(app: Sanic) -> None:
    await DB.register(app)


@bp.listener("before_server_start")
async def _setup_cache(app: Sanic) -> None:
    await cache.register()
