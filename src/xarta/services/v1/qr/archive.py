r"""
Endpoints for generating QR codes tied to the Xarta Archive.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from xarta.protocol.dag.archive.constants import ARCHIVE_SERVICE_ENDPOINT

from .base import bp
from .utils import qr

if TYPE_CHECKING:
    from sanic import HTTPResponse
    from sanic import Request


@bp.route("/archive/<identifier:uuid>", methods=["GET"])
async def archive_download(_: Request, identifier: UUID) -> HTTPResponse:
    r"""
    A simple and dumb service that is responsible for generating
    a QR code to the archived document.
    """
    return qr(f"{ARCHIVE_SERVICE_ENDPOINT}/documents/{identifier}")


@bp.route("/archive/<identifier:uuid>/details", methods=["GET"])
async def archive_details(_: Request, identifier: UUID) -> HTTPResponse:
    r"""
    A simple and dumb service that is responsible for generating
    a QR code to the details (e.g., metadata) of the archived document.
    """
    return qr(f"{ARCHIVE_SERVICE_ENDPOINT}/documents/{identifier}/details")
