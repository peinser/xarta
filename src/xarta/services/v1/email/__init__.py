from __future__ import annotations

from xarta.services.base import Service
from xarta.services.v1.email import reconciler
from xarta.services.v1.email.base import bp

service = Service(
    name="email",
    v1=bp,
    latest=bp.copy("email-latest", url_prefix="/api/email"),
)
