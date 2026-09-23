r"""
Base Xarta caching module.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import aiofiles
import orjson

from aiocache import caches

from xarta import env

if TYPE_CHECKING:
    from typing import Any
    from typing import Final

    from aiocache import Cache


CACHE_CONFIG_PATH: Final[str] = env.extract(
    key="CACHE_CONFIG_PATH",
    default="config/cache.json",
    dtype=str,
)


_default: Cache | None = None
_redis: Cache | None = None
_in_memory: Cache | None = None


async def register() -> None:
    r"""
    Initializes the caching registry according to the specification
    outlined in the cache configuration file.
    """
    global _default, _redis, _in_memory

    async with aiofiles.open(CACHE_CONFIG_PATH) as f:
        caches.set_config(orjson.loads(await f.read()))

    _default = caches.get("default")
    _redis = caches.get("redis")
    _in_memory = caches.get("in_memory")


def _dumps(input: dict | bytes) -> bytes:
    return input if isinstance(input, bytes) else orjson.dumps(input)


def _loads(input: Any | None) -> bytes | None:
    return orjson.loads(input) if input else None


class CacheManager:

    @property
    def default(self) -> Cache:
        return _default

    @property
    def redis(self) -> Cache:
        return _redis

    @property
    def in_memory(self) -> Cache:
        return _in_memory

    async def set(
        self, key, value, ttl: float = -1, namespace: str | None = None
    ) -> None:
        await self.default.set(
            key, value, ttl=ttl, namespace=namespace, dumps_fn=_dumps
        )

    async def get(self, key, namespace: str | None = None):
        return await self.default.get(key, namespace=namespace, loads_fn=_loads)
