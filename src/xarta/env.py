r"""
Utilities related to managing environment variables.
"""

from __future__ import annotations

import os

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Any

    from sanic import Blueprint
    from sanic import Sanic


class ConfigurationError(ValueError):
    """Raised when required application configuration is invalid."""


def extract(
    key: str,
    optional: bool = True,
    default: str | None = None,
    verify: Callable | None = None,
    dtype: type = str,
) -> Any:
    r"""
    Utility method to extract environment variables with ease.
    This method adds the ability to extract a specific key and
    an optional default value. Whenever the specified key is not
    specified in the program environment, the method raises a
    descriptive configuration error.
    """
    if default is None and not optional:
        if key not in os.environ:
            raise ConfigurationError(f"Required environment variable {key} is missing")

    value = os.getenv(key=key, default=default)

    if not optional:
        if not value:
            raise ConfigurationError(f"Required environment variable {key} is empty")

    # An optional verification function can be specified to check
    # the integrity of the provided value.
    if value and verify is not None:
        if not verify(value):
            raise ConfigurationError(f"Environment variable {key} is invalid")

    if value is None:
        return None

    try:
        return dtype(value)
    except (TypeError, ValueError) as ex:
        raise ConfigurationError(
            f"Environment variable {key} must be a valid {dtype.__name__}"
        ) from ex


def verify(blueprint: Blueprint, required: set[str]) -> None:
    r"""
    A method which verifies the scope of the program environment.
    """

    @blueprint.listener("before_server_start")
    async def _verify(_: Sanic) -> None:
        missing = required - os.environ.keys()
        if missing:
            names = ", ".join(sorted(missing))
            raise ConfigurationError(
                f"Required environment variables are missing: {names}"
            )


def verify_postgresql(blueprint: Blueprint, owner: str) -> None:
    """Require one complete owner-specific or legacy PostgreSQL configuration."""

    @blueprint.listener("before_server_start")
    async def _verify_postgresql(_: Sanic) -> None:
        names = ("USER", "PASSWORD", "DATABASE", "HOST")
        owner_complete = all(
            os.environ.get(f"{owner}_POSTGRESQL_{name}") for name in names
        )
        legacy_complete = all(os.environ.get(f"POSTGRESQL_{name}") for name in names)
        if not owner_complete and not legacy_complete:
            raise ConfigurationError(
                f"{owner} requires complete {owner}_POSTGRESQL_* configuration"
            )
