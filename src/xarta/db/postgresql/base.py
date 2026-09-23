r"""
Base PostgreSQL integration and utilities.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import asyncpg

import xarta.env

if TYPE_CHECKING:
    from asyncio import AbstractEventLoop


class BasePostgresModel:
    ENV_PREFIX: str | None = None
    r"""
    An abstract represention of a PostgreSQL model that
    provides all utility methods for integrating with
    a PostgreSQL database. This method
    will serve as an entry to setup the necessary
    pool properties and other connection details
    such as database host amongst others.
    """

    @classmethod
    async def open(
        cls,
        loop: AbstractEventLoop,
        user: str | None = None,
        password: str | None = None,
        database: str | None = None,
        host: str | None = None,
        command_timeout: int = 60,
        min_pool_size: int = 1,
        max_pool_size: int = 5,
    ) -> asyncpg.pool.Pool:
        r"""
        Method that allocates the database pool and the associated resources.

        Subclasses of the base model have to call the parent method and potentially
        intercept the return value of this method to collect the reference
        of the desired collection.

        TODO: Check remaining open connections when allocating a pool.
        """

        def setting(name: str, default):
            fallback = xarta.env.extract(f"POSTGRESQL_{name}", default=default)
            if cls.ENV_PREFIX is None:
                return fallback
            return xarta.env.extract(
                f"{cls.ENV_PREFIX}_POSTGRESQL_{name}", default=fallback
            )

        pool = await asyncpg.create_pool(
            user=setting("USER", user),
            password=setting("PASSWORD", password),
            database=setting("DATABASE", database),
            host=setting("HOST", host),
            loop=loop,
            command_timeout=int(setting("COMMAND_TIMEOUT", command_timeout)),
            min_size=int(setting("MIN_POOL_SIZE", min_pool_size)),
            max_size=int(setting("MAX_POOL_SIZE", max_pool_size)),
        )

        return pool
