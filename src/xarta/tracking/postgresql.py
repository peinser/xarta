from __future__ import annotations

from xarta.db.postgresql.sanic import SanicPostgresModel


class TrackingPostgresModel(SanicPostgresModel):
    ENV_PREFIX = "TRACKING"
