r"""
Generic utilities for the QR service.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import qrcode
import qrcode.image.svg

from sanic import response

if TYPE_CHECKING:
    from sanic import HTTPResponse


def qr(data: str) -> HTTPResponse:
    r"""
    Utility method that conveniently generates the necessary HTTPResponse
    for the QR data that needs to be encoded.
    """
    # Prepare the QR code and its configuration.
    qr = qrcode.QRCode(
        border=1,
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        image_factory=qrcode.image.svg.SvgPathImage,
    )

    qr.add_data(data)
    qr.make(fit=True)

    return response.text(
        body=qr.make_image().to_string(encoding="unicode"),
        content_type="image/svg+xml",
    )
