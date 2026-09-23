from __future__ import annotations

from xarta.services.base import Service

from .base import bp

service = Service(
    name="generate",
    v1=bp,
    latest=bp.copy("generate-latest", url_prefix="/api/generate"),
)
