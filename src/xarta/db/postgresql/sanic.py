r"""
PostgreSQL models for supporting the integration with Sanic.
"""

from __future__ import annotations

import asyncio

from typing import TYPE_CHECKING

import asyncpg

from .base import BasePostgresModel

if TYPE_CHECKING:
    from sanic import Sanic


class SanicPostgresModel(BasePostgresModel):
    r"""
    A PostgreSQL model for integrating with Sanic applications. The
    class is responsible for maintaining the pool in the Sanic application.
    """

    __app__: Sanic | None = None
    __lock__: asyncio.Lock = asyncio.Lock()
    __pool__: asyncpg.Pool | None = None

    @classmethod
    def pool(cls) -> asyncpg.Pool:
        if cls.__pool__ is None:
            raise RuntimeError(f"{cls.__name__} is not registered")
        return cls.__pool__

    @classmethod
    async def _cleanup(cls, app: Sanic) -> None:
        async with cls.__lock__:
            if (
                hasattr(app.ctx, "postgres_pools")
                and cls.__name__ in app.ctx.postgres_pools
            ):
                pool = app.ctx.postgres_pools[cls.__name__]
                del app.ctx.postgres_pools[cls.__name__]
                await pool.close()
                cls.__pool__ = None

    @classmethod
    async def register(cls, app: Sanic, **kwargs) -> None:
        r"""
        This method will serve as entrypoint to setup the necessary
        pool and other connection details such as the database
        and name based on the specified environment variables.
        The model will be attached to a specific Sanic application.ln
        """
        async with cls.__lock__:
            # Create the PostgreSQL context for the Sanic application.
            if not hasattr(app.ctx, "postgres_pools"):
                app.ctx.postgres_pools = {}

            if cls.__name__ not in app.ctx.postgres_pools:
                pool = await cls.open(loop=app.loop, **kwargs)
                app.ctx.postgres_pools[cls.__name__] = pool
                cls.__pool__ = pool

        app.register_listener(cls._cleanup, "before_server_stop")
