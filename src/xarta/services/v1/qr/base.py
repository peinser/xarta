r"""
Blueprint definition for the QRCodes v1 service and setup of
remaining necessary dependencies.
"""

from __future__ import annotations

from sanic import Blueprint

from xarta import env

bp: Blueprint = Blueprint(
    name="qr-v1",
    url_prefix="/api/v1/qr",
)


env.verify(
    blueprint=bp,
    required={
        "ARCHIVE_SERVICE_ENDPOINT",
    },
)
