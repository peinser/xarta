from __future__ import annotations

import aiofiles  # type: ignore[import-untyped]
import orjson

from xarta import env

SFTP_CONFIGURATIONS_CONFIG_PATH = env.extract(
    "SFTP_CONFIGURATIONS_CONFIG_PATH",
    default="config/sftp.json",
)


async def load_sftp_configurations() -> dict:
    async with aiofiles.open(SFTP_CONFIGURATIONS_CONFIG_PATH, "rb") as configuration:
        value = orjson.loads(await configuration.read())
    if not isinstance(value, dict):
        raise TypeError("SFTP configuration must be an object")
    return value
