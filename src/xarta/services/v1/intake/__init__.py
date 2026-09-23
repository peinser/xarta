from __future__ import annotations

from xarta.services.base import Service

from .base import bp

service = Service(
    name="intake",
    v1=bp,
    latest=bp.copy("intake-latest", url_prefix="/api/intake"),
)
