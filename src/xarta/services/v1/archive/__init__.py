from __future__ import annotations

from xarta.services.base import Service

from . import delete
from . import details
from . import download
from . import reconciler
from . import store
from .base import bp

service = Service(
    name="archive",
    v1=bp,
    latest=bp.copy("archive-latest", url_prefix="/api/archive"),
)
