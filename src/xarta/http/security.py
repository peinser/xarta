r"""
HTTP Security Module, includes utilities for verifying JWT's.
"""

from __future__ import annotations

import asyncio
import os

from typing import TYPE_CHECKING

import jwt

from xarta import crypto
from xarta import exceptions
from xarta.logging import logger

from .sessions import HTTPRequestManager

if TYPE_CHECKING:

    from sanic import HTTPResponse
    from sanic import Request
    from sanic import Sanic


class JWKSManager:
    r"""
    Global JWKS manager for managing JWKS's found
    at the configured JWKS endpoint.
    """

    __app__: Sanic
    __lock__: asyncio.Lock = asyncio.Lock()

    def __init__(self) -> None:
        self._origin = os.environ.get("OIDC_ORIGIN", None)
        self._jwks = crypto.JWKSet()
        self._public_keys = {}

        assert self._origin, "No OIDC origin has been specified."

    @property
    def jwks(self) -> crypto.JWKSet:
        return self._jwks

    def get(self, kid: str) -> tuple[crypto.PublicKeyTypes, str]:
        return self._public_keys[kid]

    async def refresh(self) -> None:
        # First, find the JWKS uri.
        async with HTTPRequestManager.__session__.get(
            f"{self._origin}/.well-known/openid-configuration"
        ) as response:
            configuration = await response.json()

        # Next, load the JWKS at the specified uri.
        async with HTTPRequestManager.__session__.get(
            configuration["jwks_uri"]
        ) as response:
            self._jwks = crypto.JWKSet.from_json(keyset=await response.read())

        # Populate the public keys based on the provided JWKS.
        for jwk in self._jwks:
            self._public_keys[jwk["kid"]] = (jwk._get_public_key(), jwk["alg"])

    @classmethod
    async def register(cls, app: Sanic, refresh_interval: float = 900.0) -> None:
        r"""
        Registers the manager with the Sanic application and
        sets up a background task to periodically refresh the JWKS.
        """
        # Check if the JWKS manager has been allocated.
        async with cls.__lock__:
            if hasattr(app.ctx, "jwks_manager"):
                return

            cls.__app__ = app

            manager = JWKSManager()
            app.ctx.jwks_manager = manager

            # Setup the background task.
            async def _update(app: Sanic) -> None:
                await asyncio.sleep(5.0)  # Initial delay.
                while True:
                    try:
                        await app.ctx.jwks_manager.refresh()
                    except Exception as ex:
                        logger.exception(ex)
                    await asyncio.sleep(delay=refresh_interval)

            app.add_task(_update)


def protected(f: callable) -> callable:

    def _wrapped(request: Request, **kwargs) -> HTTPResponse:
        token = request.token

        if not token:
            raise exceptions.http.UnauthorizedError

        try:
            headers = jwt.get_unverified_header(token)
            kid = headers.get("kid", None)
            manager = request.app.ctx.jwks_manager
            public_key, signing_algorithm = manager.get(kid)

            claims = jwt.decode(
                token,
                key=public_key,
                algorithms=[signing_algorithm],
                options={"verify_aud": False},
            )

        except:
            raise exceptions.http.UnauthorizedError

        request.ctx.claims = claims

        return f(request, **kwargs)

    return _wrapped
