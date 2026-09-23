r"""
Module for implementing rate-limiting distributions.
"""

from __future__ import annotations

import time

from functools import wraps
from typing import TYPE_CHECKING

from xarta import cache
from xarta.exceptions.http import RateLimitedError
from xarta.utils import networking

if TYPE_CHECKING:
    from typing import Final


TOKEN_BUCKET_NAMESPACE: Final[str] = "rate-limit:tb"


async def token_bucket(
    key: str,
    max_bucket_size: int = 10,
    refill_rate: float = 0.5,
    name: str = "default",
    ttl: float = 60.0,
):
    """
    Implements Token Bucket rate limiting.

    :param key: String representation of an identifying of a user / application.
    :param bucket_size: Max tokens in the bucket
    :param refill_rate: Tokens added per second
    :param name: Name of the rate limiter, defaults to `default`.
    :param ttl: TTL before the key expires in the cache. Defaults to 60.
    :return: True if the request is allowed, False otherwise
    """
    namespace = f"{TOKEN_BUCKET_NAMESPACE}:{name}"

    t = time.time()  # Capture the time of the request.

    # Get bucket data from Redis
    bucket_data = await cache.manager.get(key, namespace=namespace)
    if bucket_data:
        tokens_available, t_0 = bucket_data.split(",")
        tokens_available = int(tokens_available)
        t_0 = float(t_0)
    else:
        tokens_available, t_0 = max_bucket_size, t

    # Refill tokens based on time elapsed
    elapsed = t - t_0
    tokens_available = min(
        max_bucket_size, tokens_available + int(elapsed * refill_rate)
    )

    # Check if sufficient tokens are available.
    if tokens_available <= 0:
        return False

    # Deduct token and update Redis
    tokens_available -= 1

    await cache.manager.set(
        key, f"{tokens_available},{t}", ttl=ttl, namespace=namespace
    )

    return True


def tb(
    name: str = "default",
    max_bucket_size: int = 25,
    refill_rate: float = 5,
    ttl: float = 60.0,
    ignore_bogon: bool = True,
):
    """
    Sanic decorator to apply rate limiting using the token bucket algorithm.

    :param name: Name of the rate limiter (used for namespacing in Redis).
    """

    def decorator(handler):
        @wraps(handler)
        async def wrapper(request, *args, **kwargs):
            ip = request.remote_addr or request.ip
            if not ignore_bogon or not networking.is_bogon(ip):
                allowed = await token_bucket(
                    key=ip,
                    max_bucket_size=max_bucket_size,
                    refill_rate=refill_rate,
                    name=name,
                    ttl=ttl,
                )
                if not allowed:
                    raise RateLimitedError
            return await handler(request, *args, **kwargs)

        return wrapper

    return decorator
