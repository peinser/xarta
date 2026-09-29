from __future__ import annotations

from xarta.services.base import Service

from .base import bp

service = Service(
    name="ubl", v1=bp, latest=bp.copy("ubl-latest", url_prefix="/api/ubl")
)
