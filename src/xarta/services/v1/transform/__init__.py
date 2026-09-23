from __future__ import annotations

from xarta.services.base import Service

from .base import bp

service = Service(
    name="transform",
    v1=bp,
    latest=bp.copy("transform-latest", url_prefix="/api/transform"),
)
