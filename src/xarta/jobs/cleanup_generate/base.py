r"""
Blueprint definition of the cleanup generate storage storage job. This jobs primary
responsibility is for cleaning up temporary files.
"""

from __future__ import annotations

import datetime

from typing import TYPE_CHECKING

from sanic import Blueprint

from xarta.storage import get_temporary_storage

if TYPE_CHECKING:
    from sanic import Sanic


bp = Blueprint(
    name="cleanup-generate-v1",
    url_prefix="/job/v1/cleanup-generate",
)


@bp.listener("after_server_start")
async def _launch(app: Sanic) -> None:
    cutoff = datetime.datetime.now(tz=datetime.UTC) - datetime.timedelta(days=5)
    await get_temporary_storage().cleanup_older_than(cutoff)

    app.stop()
