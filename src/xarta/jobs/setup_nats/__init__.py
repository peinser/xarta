from __future__ import annotations

from xarta.services.base import Service

from .base import bp

service = Service(
    name="setup-nats",
    v1=bp,
    latest=bp.copy(
        "setup-nats-latest",
        url_prefix="/job/setup-nats",
    ),
)
