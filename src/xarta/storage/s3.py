from __future__ import annotations

import asyncio

from typing import Any


class S3ClientManager:
    """Own one aiobotocore client and its connection pool for a process runtime."""

    def __init__(
        self, client_options: dict[str, Any], *, max_pool_connections: int = 10
    ) -> None:
        self.client_options = dict(client_options)
        self.max_pool_connections = max_pool_connections
        self._lock = asyncio.Lock()
        self._context = None
        self._client = None

    async def get(self):
        if self._client is not None:
            return self._client
        async with self._lock:
            if self._client is None:
                from aiobotocore.config import AioConfig  # type: ignore[import-untyped]
                from aiobotocore.session import get_session  # type: ignore[import-untyped]

                context = get_session().create_client(
                    "s3",
                    config=AioConfig(max_pool_connections=self.max_pool_connections),
                    **self.client_options,
                )
                client = await context.__aenter__()
                self._context = context
                self._client = client
        return self._client

    async def close(self) -> None:
        """Close the persistent client and its HTTP connection pool."""
        async with self._lock:
            context = self._context
            self._context = None
            self._client = None
            if context is not None:
                await context.__aexit__(None, None, None)
