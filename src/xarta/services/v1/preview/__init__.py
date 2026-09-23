from __future__ import annotations

from xarta.services.base import Service

from .base import bp

service = Service(
    name="preview",
    v1=bp,
    latest=bp.copy("preview-latest", url_prefix="/api/preview"),
)
