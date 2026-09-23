from __future__ import annotations

from typing import Any

import aiofiles  # type: ignore[import-untyped]
import orjson


async def load_search_destinations(path: str) -> dict[str, dict[str, Any]]:
    async with aiofiles.open(path, "rb") as configuration:
        value = orjson.loads(await configuration.read())
    if not isinstance(value, dict):
        raise TypeError("Search destination configuration must be an object")
    if any(not isinstance(item, dict) for item in value.values()):
        raise TypeError("Each search destination must be an object")
    return value
