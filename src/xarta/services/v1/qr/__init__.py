from __future__ import annotations

from xarta.services.base import Service

from . import archive
from . import payments
from .base import bp

service = Service(
    name="qr",
    v1=bp,
    latest=bp.copy("qr-latest", url_prefix="/api/qr"),
)
