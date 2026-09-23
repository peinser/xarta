from __future__ import annotations

import base64
import binascii

from collections.abc import Callable
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from typing import Generic
from typing import TypeVar
from typing import cast

import orjson

T = TypeVar("T")
MAX_CURSOR_LENGTH = 2048


class PaginationError(ValueError):
    """Raised when pagination parameters are malformed or out of bounds."""


@dataclass(frozen=True)
class Page(Generic[T]):
    """The stable public representation of one cursor-paginated page."""

    items: tuple[T, ...]
    next_cursor: str | None

    def dict(self, serialize: Callable[[T], Any]) -> dict[str, Any]:
        return {
            "items": [serialize(item) for item in self.items],
            "next_cursor": self.next_cursor,
        }


def parse_limit(
    value: str | None, *, default: int, maximum: int, minimum: int = 1
) -> int:
    if value is None:
        return default
    try:
        limit = int(value)
    except ValueError as ex:
        raise PaginationError("limit must be an integer") from ex
    if not minimum <= limit <= maximum:
        raise PaginationError(f"limit must be between {minimum} and {maximum}")
    return limit


def encode_cursor(scope: str, values: Mapping[str, str]) -> str:
    payload = orjson.dumps(
        {"version": 1, "scope": scope, "values": dict(values)},
        option=orjson.OPT_SORT_KEYS,
    )
    return base64.urlsafe_b64encode(payload).decode()


def decode_cursor(value: str, *, scope: str, keys: frozenset[str]) -> Mapping[str, str]:
    if not value or len(value) > MAX_CURSOR_LENGTH:
        raise PaginationError("cursor is invalid")
    try:
        decoded = base64.urlsafe_b64decode(value)
        payload = orjson.loads(decoded)
        if (
            not isinstance(payload, dict)
            or payload.get("version") != 1
            or payload.get("scope") != scope
            or not isinstance(payload.get("values"), dict)
            or set(payload["values"]) != keys
            or not all(isinstance(item, str) for item in payload["values"].values())
        ):
            raise ValueError("cursor payload does not match the requested resource")
        return cast("dict[str, str]", payload["values"])
    except (binascii.Error, TypeError, ValueError, orjson.JSONDecodeError) as ex:
        raise PaginationError("cursor is invalid") from ex
