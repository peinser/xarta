from __future__ import annotations

import asyncio

from collections.abc import Awaitable
from collections.abc import Callable
from collections.abc import Iterable
from typing import TypeVar
from typing import cast

Item = TypeVar("Item")
Result = TypeVar("Result")


async def bounded_map(
    items: Iterable[Item],
    limit: int,
    operation: Callable[[Item], Awaitable[Result]],
) -> list[Result]:
    """Run operations concurrently, preserving order and settling every started task."""
    if limit < 1:
        raise ValueError("Concurrency limit must be positive")

    semaphore = asyncio.Semaphore(limit)

    async def apply(item: Item) -> Result:
        async with semaphore:
            return await operation(item)

    tasks = [asyncio.create_task(apply(item)) for item in items]
    try:
        results = await asyncio.gather(*tasks, return_exceptions=True)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise

    for result in results:
        if isinstance(result, BaseException):
            raise result
    return cast(list[Result], results)
