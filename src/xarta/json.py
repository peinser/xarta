r"""
JSON-specific utilities for Xarta. Includes a custom serializer and deserialized
based on `orjson`.
"""

from __future__ import annotations

import datetime

from typing import TYPE_CHECKING

import orjson

from isodate import duration_isoformat

if TYPE_CHECKING:
    from typing import Any


def _default_serializer(obj: object) -> bytes:
    r"""
    Our custom JSON serializer for objects which are not serializeble by default.
    """
    if isinstance(obj, dict):
        return dumps(obj)

    match type(obj):
        case datetime.datetime:
            return obj.isoformat()
        case datetime.timedelta:
            return duration_isoformat(obj)

    raise TypeError


def dumps(obj: Any, **kwargs) -> bytes:
    return orjson.dumps(obj, default=_default_serializer, **kwargs)


def loads(value: str | bytes | bytearray | memoryview) -> Any:
    return orjson.loads(value)
