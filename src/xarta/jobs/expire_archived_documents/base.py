r"""
Blueprint definition of the expire archived documents job. This jobs primary
responsibility is for cleaning up archived documents that are beyond their
expiry date.
"""

from __future__ import annotations

import asyncio

from typing import TYPE_CHECKING
from urllib.parse import quote

from sanic import Blueprint

from xarta import env

from .db import ArchivePostgresModel as DB

if TYPE_CHECKING:
    from typing import Final

    from aiohttp import ClientSession
    from sanic import Sanic


bp = Blueprint(
    name="expire-archived-documents-v1",
    url_prefix="/job/v1/expire-archived-documents",
)


env.verify(
    blueprint=bp,
    required={
        "ARCHIVE_SERVICE_ENDPOINT",
        "ARCHIVE_POSTGRESQL_USER",
        "ARCHIVE_POSTGRESQL_PASSWORD",
        "ARCHIVE_POSTGRESQL_DATABASE",
        "ARCHIVE_POSTGRESQL_HOST",
    },
)


ARCHIVE_SERVICE_ENDPOINT: Final[str] = env.extract(
    key="ARCHIVE_SERVICE_ENDPOINT",
    dtype=str,
)

CLEANUP_BATCHSIZE: Final[int] = env.extract(
    key="CLEANUP_BATCHSIZE",
    dtype=int,
    default="10000",
)


@bp.listener("before_server_start")
async def _setup_database(app: Sanic) -> None:
    await DB.register(app)


@bp.listener("after_server_start")
async def _launch(app: Sanic) -> None:
    http_session: ClientSession = app.ctx.http_client_session
    expired_documents = await DB.expired(limit=CLEANUP_BATCHSIZE)
    semaphore = asyncio.Semaphore(32)

    async def _delete(archive: str, document_id: str) -> None:
        async with (
            semaphore,
            http_session.delete(
                f"{ARCHIVE_SERVICE_ENDPOINT}/archives/{quote(archive, safe='')}/documents/{document_id}"
            ) as response,
        ):
            if response.status not in {200, 202}:
                raise RuntimeError(
                    f"Archive deletion failed for {archive}/{document_id}: {response.status}"
                )

    await asyncio.gather(*(_delete(*document) for document in expired_documents))

    app.stop()
