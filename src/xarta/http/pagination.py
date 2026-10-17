"""Small building blocks for stateless, keyset-based collection traversal.

A cursor is a bookmark, not a PostgreSQL cursor or an offset. The collection
handler chooses the ordering, fetches ``limit + 1`` matching rows, returns at most
``limit`` rows, and encodes the last *returned* row's position when more exist.
The next request uses a seek predicate such as ``document_id > last_id``.

This module only packages results and encodes/validates bookmarks. The caller
must bind the scope to its collection/filter/order, validate resource-specific
position types, and apply those predicates on every request. Tokens are base64
JSON containing the position itself, not keys into a Redis/result cache. These
functions neither store nor retrieve a query session. Tokens are not encrypted
or signed credentials. See docs/archive-metadata-search.md
for a worked example and the consequences of changes between requests.
"""

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
    """Serialize a batch that its caller has already bounded.

    ``limit`` is the effective requested maximum, not ``len(items)``. The caller
    supplies an encoded ``next_cursor`` or None when traversal is complete; this
    class neither slices rows nor decides whether another batch exists.
    """

    items: tuple[T, ...]
    next_cursor: str | None
    limit: int

    def dict(self, serialize: Callable[[T], Any]) -> dict[str, Any]:
        return {
            "items": [serialize(item) for item in self.items],
            "pagination": {"limit": self.limit, "next": self.next_cursor},
        }


def parse_limit(
    value: str | int | None, *, default: int, maximum: int, minimum: int = 1
) -> int:
    """Accept integer query text or an integer value within the configured bounds.

    None selects the caller's default. Booleans/floats are not integers here:
    accepting them via int() would silently turn true into 1 or truncate a value.
    JSON endpoints can additionally reject text/null when explicitly supplied.
    """
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise PaginationError("limit must be an integer")
    try:
        limit = int(value)
    except ValueError as ex:
        raise PaginationError("limit must be an integer") from ex
    if not minimum <= limit <= maximum:
        raise PaginationError(f"limit must be between {minimum} and {maximum}")
    return limit


def encode_cursor(scope: str, values: Mapping[str, str]) -> str:
    """Encode a caller-supplied query scope and keyset position as a bookmark.

    The envelope version describes this encoding, not a document version. Scope
    identifies the query for comparison, not cache retrieval; for archive search
    it includes a digest of the archive,
    normalized filter, HEAD selection and ordering. Values contain only the seek
    position, e.g. the last returned document UUID, never a row count or offset.
    """
    payload = orjson.dumps(
        {"version": 1, "scope": scope, "values": dict(values)},
        option=orjson.OPT_SORT_KEYS,
    )
    return base64.urlsafe_b64encode(payload).decode()


def decode_cursor(value: str, *, scope: str, keys: frozenset[str]) -> Mapping[str, str]:
    """Check encoding, envelope version, expected scope and position-field types.

    Scope comparison prevents accidentally continuing a different query. It is
    not authentication: a client can decode or construct base64 JSON. The caller
    must still apply the requested collection/filter and its access policy, and
    parse values into the expected domain types (UUIDs, timestamps, etc.).
    """
    if not isinstance(value, str) or not value or len(value) > MAX_CURSOR_LENGTH:
        raise PaginationError("cursor is invalid")
    try:
        decoded = base64.b64decode(value, altchars=b"-_", validate=True)
        payload = orjson.loads(decoded)
        if (
            not isinstance(payload, dict)
            or set(payload) != {"version", "scope", "values"}
            or type(payload["version"]) is not int
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
