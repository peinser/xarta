r"""
Endpoints for generating QR codes tied to Payments. In particular
this module will generate codes using the EPC QR Code Data Format
(SEPA Credit Transfer).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from xarta.exceptions.http import BadRequestError
from xarta.protocol.payments.sepa import SEPAQRData

from .base import bp
from .utils import qr

if TYPE_CHECKING:
    from sanic import HTTPResponse
    from sanic import Request


@bp.route("/payments/sepa", methods=["GET"])
async def sepa_parameters(request: Request) -> HTTPResponse:
    mapping = {k: v[0] for k, v in request.args.items()}
    data = SEPAQRData(**mapping)

    return qr(data.payload())


@bp.route("/payments/sepa", methods=["POST"])
async def sepa_body(request: Request) -> HTTPResponse:
    if not request.json:
        raise BadRequestError

    data = SEPAQRData(**request.json)

    return qr(data.payload())
