from __future__ import annotations

from xarta.services.base import Service

from .base import bp

service = Service(
    name="cleanup-generate",
    v1=bp,
    latest=bp.copy(
        "cleanup-generate-latest",
        url_prefix="/job/cleanup-generate",
    ),
)
