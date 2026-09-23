r"""
Email service generic utilities.
"""

from __future__ import annotations

import aiofiles  # type: ignore[import-untyped]

from xarta import json


async def load_email_configurations(path: str) -> dict:
    async with aiofiles.open(path) as f:
        configurations = json.loads(await f.read())
    if not isinstance(configurations, dict):
        raise ValueError("Email configurations must be an object")
    return configurations
