from __future__ import annotations

from xarta.services.base import Service

from .base import bp

service = Service(
    name="signature",
    v1=bp,
    latest=bp.copy("signature-latest", url_prefix="/api/signature"),
)
