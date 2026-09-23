from __future__ import annotations

from xarta.services.base import Service

from .base import bp

service = Service(
    name="search",
    v1=bp,
    latest=bp.copy("search-latest", url_prefix="/api/search"),
)
