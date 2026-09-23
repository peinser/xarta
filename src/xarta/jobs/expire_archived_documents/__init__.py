from __future__ import annotations

from xarta.services.base import Service

from .base import bp

service = Service(
    name="expire-archived-documents",
    v1=bp,
    latest=bp.copy(
        "expire-archived-documents-latest",
        url_prefix="/job/expire-archived-documents",
    ),
)
