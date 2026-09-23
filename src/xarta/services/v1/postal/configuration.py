from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import orjson


async def load_json_object(path: str) -> dict[str, Any]:
    value = orjson.loads(Path(path).read_bytes())
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"Configuration {path} must contain an object")
    return value


def configuration_section(value: Mapping[str, Any], name: str) -> dict[str, Any]:
    section = value.get(name)
    if not isinstance(section, Mapping):
        raise ValueError(f"Postal configuration requires a {name} object")
    return dict(section)
