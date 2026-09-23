from __future__ import annotations

from xarta.services.base import Service
from xarta.services.v1.agents.base import bp

service = Service(name="agents", latest=bp)
