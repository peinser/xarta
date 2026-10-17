from __future__ import annotations

import base64

import orjson
import pytest

from xarta.http.pagination import Page
from xarta.http.pagination import PaginationError
from xarta.http.pagination import decode_cursor
from xarta.http.pagination import encode_cursor
from xarta.http.pagination import parse_limit


def test_page_serializes_items_and_cursor() -> None:
    assert Page((1, 2), "next", limit=50).dict(str) == {
        "items": ["1", "2"],
        "pagination": {"limit": 50, "next": "next"},
    }
    assert Page((), None, limit=50).dict(str) == {
        "items": [],
        "pagination": {"limit": 50, "next": None},
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


@pytest.mark.parametrize(
    "value", [True, False, 1.0, 1.5, [], {}, "1.5", "", "0", "101", -1]
)
def test_limit_rejects_nonintegers_and_out_of_bounds_values(value):
    with pytest.raises(PaginationError):
        parse_limit(value, default=50, maximum=100)


@pytest.mark.parametrize("value, expected", [(None, 50), (1, 1), (100, 100), ("7", 7)])
def test_json_and_query_limits_have_the_same_bounds(value, expected):
    assert parse_limit(value, default=50, maximum=100) == expected


@pytest.mark.parametrize(
    "items, limit, next_cursor",
    [
        ((), 50, None),
        ((1,), 50, None),
        ((1, 2), 2, "opaque-continuation"),
    ],
)
def test_collection_envelope_reports_effective_limit(items, limit, next_cursor):
    result = Page(items, next_cursor, limit).dict(lambda value: {"id": value})
    assert result == {
        "items": [{"id": value} for value in items],
        "pagination": {"limit": limit, "next": next_cursor},
    }


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        1,
        "cursor",
        {},
        {"version": True, "scope": "collection", "values": {"id": "42"}},
        {"version": 1.0, "scope": "collection", "values": {"id": "42"}},
        {"version": 2, "scope": "collection", "values": {"id": "42"}},
        {"version": 1, "scope": "other", "values": {"id": "42"}},
        {"version": 1, "scope": "collection", "values": None},
        {"version": 1, "scope": "collection", "values": {"id": 42}},
        {"version": 1, "scope": "collection", "values": {"id": "42", "extra": "value"}},
        {"version": 1, "scope": "collection", "values": {}},
        {"version": 1, "scope": "collection", "values": {"id": "42"}, "extra": True},
    ],
)
def test_cursor_rejects_invalid_envelopes_with_domain_errors(payload):
    cursor = base64.urlsafe_b64encode(orjson.dumps(payload)).decode()
    with pytest.raises(PaginationError):
        decode_cursor(cursor, scope="collection", keys=frozenset({"id"}))


@pytest.mark.parametrize(
    "cursor", [None, 1, True, [], {}, "", "é", "%%%", "e30", "x" * 2049]
)
def test_cursor_rejects_invalid_input_types_and_encoding(cursor):
    with pytest.raises(PaginationError):
        decode_cursor(cursor, scope="collection", keys=frozenset({"id"}))


def test_cursor_rejects_junk_in_an_otherwise_valid_token():
    cursor = encode_cursor("collection", {"id": "42"})
    for invalid in ("!" + cursor, cursor + "\n", base64.b64encode(b"\xff").decode()):
        with pytest.raises(PaginationError):
            decode_cursor(invalid, scope="collection", keys=frozenset({"id"}))


def test_cursor_round_trip_preserves_values_without_leaking_mutable_state():
    values = {"name": "Facture été", "id": "42"}
    cursor = encode_cursor("collection:invoices", values)
    values["id"] = "99"
    assert decode_cursor(
        cursor, scope="collection:invoices", keys=frozenset(values)
    ) == {
        "name": "Facture été",
        "id": "42",
    }
