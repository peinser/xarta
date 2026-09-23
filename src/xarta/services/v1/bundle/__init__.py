from __future__ import annotations

from xarta.services.base import Service

from . import bundle
from .base import bp

service = Service(
    name="bundle",
    v1=bp,
    latest=bp.copy("bundle-latest", url_prefix="/api/bundle"),
)
