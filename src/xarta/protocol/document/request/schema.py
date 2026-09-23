from __future__ import annotations

from importlib.resources import files
from typing import Any

import orjson

SCHEMA_DOCUMENT_RENDER_REQUEST: dict[str, Any] = orjson.loads(
    files("xarta.protocol.document.request")
    .joinpath("_schemas/document_render_request.json")
    .read_bytes()
)
