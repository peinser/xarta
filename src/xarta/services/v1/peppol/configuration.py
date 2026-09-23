from __future__ import annotations

import aiofiles  # type: ignore[import-untyped]
import orjson


async def load_peppol_destinations(path: str) -> dict:
    async with aiofiles.open(path, "rb") as configuration:
        value = orjson.loads(await configuration.read())
    if not isinstance(value, dict):
        raise TypeError("Peppol configuration must be an object")
    return value
