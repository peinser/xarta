from __future__ import annotations

from xarta.db.postgresql.sanic import SanicPostgresModel


class DocclePostgresModel(SanicPostgresModel):
    ENV_PREFIX = "DOCCLE"
