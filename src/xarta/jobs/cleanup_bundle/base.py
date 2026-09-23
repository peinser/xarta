r"""
Blueprint definition of the cleanup bundle storage job. This jobs primary
responsibility is for cleaning up temporary bundles.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sanic import Blueprint

from xarta import env
from xarta.jobs.cleanup import remove_expired_files

if TYPE_CHECKING:
    from typing import Final

    from sanic import Sanic


bp = Blueprint(
    name="cleanup-bundle-v1",
    url_prefix="/job/v1/cleanup-bundle",
)


env.verify(
    blueprint=bp,
    required={
        "BUNDLE_STORAGE",
    },
)


BUNDLE_STORAGE: Final[str] = env.extract(
    key="BUNDLE_STORAGE",
    optional=False,
    dtype=str,
)


@bp.listener("after_server_start")
async def _launch(app: Sanic) -> None:
    await remove_expired_files(BUNDLE_STORAGE, older_than_days=2)

    app.stop()
