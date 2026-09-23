from __future__ import annotations

from xarta.services.base import Service

from .base import bp

service = Service(
    name="render",
    v1=bp,
    latest=bp.copy("render-latest", url_prefix="/api/render"),
)
