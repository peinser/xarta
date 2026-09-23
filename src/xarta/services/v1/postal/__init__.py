from __future__ import annotations

from xarta.services.base import Service
from xarta.services.v1.postal.base import bp

service = Service(
    name="postal",
    v1=bp,
    latest=bp.copy("postal-latest", url_prefix="/api/postal"),
)
