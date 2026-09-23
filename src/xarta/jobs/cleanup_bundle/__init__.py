from __future__ import annotations

from xarta.services.base import Service

from .base import bp

service = Service(
    name="cleanup-bundle",
    v1=bp,
    latest=bp.copy(
        "cleanup-bundle-latest",
        url_prefix="/job/cleanup-bundle",
    ),
)
