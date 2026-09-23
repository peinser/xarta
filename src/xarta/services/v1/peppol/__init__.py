from __future__ import annotations

from xarta.services.base import Service

from . import reconciler
from .base import bp

service = Service(
    name="peppol",
    v1=bp,
    latest=bp.copy("peppol-latest", url_prefix="/api/peppol"),
)
