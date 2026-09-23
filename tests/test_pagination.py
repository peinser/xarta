from __future__ import annotations

import pytest

from xarta.http.pagination import Page
from xarta.http.pagination import PaginationError
from xarta.http.pagination import decode_cursor
from xarta.http.pagination import encode_cursor
from xarta.http.pagination import parse_limit


def test_page_serializes_items_and_cursor() -> None:
    assert Page((1, 2), "next").dict(str) == {
        "items": ["1", "2"],
        "next_cursor": "next",
    }


def test_limit_is_bounded() -> None:
    assert parse_limit(None, default=50, maximum=100) == 50
    assert parse_limit("1", default=50, maximum=100) == 1
    assert parse_limit("100", default=50, maximum=100) == 100

    for value in ("0", "101", "invalid"):
        with pytest.raises(PaginationError):
            parse_limit(value, default=50, maximum=100)


def test_cursor_is_opaque_scoped_and_schema_checked() -> None:
    cursor = encode_cursor(
        "archive:document:versions", {"created": "2030-01-01", "id": "42"}
    )

    assert decode_cursor(
        cursor,
        scope="archive:document:versions",
        keys=frozenset({"created", "id"}),
    ) == {
        "created": "2030-01-01",
        "id": "42",
    }
    with pytest.raises(PaginationError):
        decode_cursor(
            cursor,
            scope="another-resource",
            keys=frozenset({"created", "id"}),
        )
    with pytest.raises(PaginationError):
        decode_cursor(
            cursor,
            scope="archive:document:versions",
            keys=frozenset({"id"}),
        )
    with pytest.raises(PaginationError):
        decode_cursor("not-base64!", scope="resource", keys=frozenset({"id"}))
    with pytest.raises(PaginationError):
        decode_cursor(
            "x" * 2049,
            scope="archive:document:versions",
            keys=frozenset({"created", "id"}),
        )
