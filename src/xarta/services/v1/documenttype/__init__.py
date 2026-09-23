from __future__ import annotations

from xarta.services.base import Service

from . import read
from .base import bp

service = Service(
    name="document-type",
    v1=bp,
    latest=bp.copy("document-type-latest", url_prefix="/api/document-type"),
)
