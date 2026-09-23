from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Any

import aiofiles  # type: ignore[import-untyped]
import orjson

if TYPE_CHECKING:
    from collections.abc import Mapping


async def load_archive_destinations(path: str) -> Mapping[str, Mapping[str, Any]]:
    async with aiofiles.open(path, "rb") as configuration:
        value = orjson.loads(await configuration.read())
    if not isinstance(value, dict):
        raise TypeError("Archive destination configuration must be an object")
    return value


async def load_archive_storage(path: str) -> Mapping[str, Any]:
    async with aiofiles.open(path, "rb") as configuration:
        value = orjson.loads(await configuration.read())
    if not isinstance(value, dict):
        raise TypeError("Archive storage configuration must be an object")
    return value
