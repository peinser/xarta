from __future__ import annotations

import datetime

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import asyncpg
import orjson
import pytest

from xarta.exceptions.http import BadRequestError
from xarta.exceptions.http import ServiceUnavailable
from xarta.http.pagination import decode_cursor
from xarta.http.pagination import encode_cursor
from xarta.services.v1.archive.db import ArchivePostgresModel
from xarta.services.v1.archive.search import MAX_SEARCH_BYTES
from xarta.services.v1.archive.search import parse_filter
from xarta.services.v1.archive.search import search_cursor_scope
from xarta.services.v1.archive.search import search_default
from xarta.services.v1.archive.search import search_scoped


def request(payload):
    return SimpleNamespace(
        content_type="application/json", body=orjson.dumps(payload), args={}
    )


def match(number):
    return {
        "id": str(UUID(int=number)),
        "version": {"id": str(UUID(int=number + 100)), "parent": None},
        "document_type": "invoice",
        "metadata": {"customer_id": "CUST-0042"},
        "created_at": "2026-10-16T09:00:00+00:00",
        "expires_at": None,
    }


@pytest.mark.parametrize(
    "filters",
    [
        None,
        [],
        {"representation_metadata": {"language": "nl"}},
        {"document_type": 42},
        {"document_type": ""},
        {"document_type": "a\0b"},
        {"metadata": None},
        {"metadata": []},
        {"metadata": {"tags": ["invoice"]}},
        {"metadata": {"customer": {}}},
        {"metadata": {"name": "a\0b"}},
        {"metadata": {"a\0b": "customer"}},
        {"metadata": {"amount": float("inf")}},
    ],
)
def test_filter_validation_raises_domain_errors(filters):
    with pytest.raises(ValueError):
        parse_filter(filters)


def test_filter_types_and_scope_are_explicit():
    filters = {
        "document_type": None,
        "metadata": {
            "customer": {"country": "BE"},
            "year": 2026,
            "approved": False,
            "reference": None,
        },
    }
    assert parse_filter(filters) == filters
    assert parse_filter({"metadata": {}}) == {}
    assert search_cursor_scope("invoices", {}) == search_cursor_scope(
        "invoices", parse_filter({"metadata": {}})
    )
    assert search_cursor_scope("invoices", filters) == search_cursor_scope(
        "invoices", {"metadata": filters["metadata"], "document_type": None}
    )
    for changed in (
        {},
        {"metadata": filters["metadata"]},
        {"metadata": {"year": "2026"}},
    ):
        assert search_cursor_scope("invoices", filters) != search_cursor_scope(
            "invoices", changed
        )
    assert search_cursor_scope("invoices", filters) != search_cursor_scope(
        "payroll", filters
    )


def test_metadata_depth_is_bounded():
    metadata = {"customer_id": "CUST-0042"}
    for _ in range(9):
        metadata = {"nested": metadata}
    with pytest.raises(ValueError, match="nested object levels"):
        parse_filter({"metadata": metadata})


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {"offset": 1},
        {"history": True},
        {"limit": True},
        {"limit": 1.5},
        {"limit": "1"},
        {"limit": None},
        {"limit": 0},
        {"limit": 101},
        {"cursor": None},
    ],
)
async def test_invalid_requests_never_query_the_database(monkeypatch, payload):
    query = AsyncMock()
    monkeypatch.setattr(ArchivePostgresModel, "search_heads", query)
    with pytest.raises(BadRequestError):
        await search_default(request(payload))
    query.assert_not_awaited()


async def test_body_type_size_and_invalid_json(monkeypatch):
    query = AsyncMock()
    monkeypatch.setattr(ArchivePostgresModel, "search_heads", query)
    for body, content_type, status in (
        (b"{}", "text/plain", 415),
        (b" " * (MAX_SEARCH_BYTES + 1), "application/json", 413),
        (b"{", "application/json", 400),
    ):
        with pytest.raises(Exception) as raised:
            await search_default(
                SimpleNamespace(body=body, content_type=content_type, args={})
            )
        assert raised.value.status_code == status
    query.assert_not_awaited()


async def test_search_rejects_query_string_parameters_instead_of_restarting(
    monkeypatch,
):
    query = AsyncMock()
    monkeypatch.setattr(ArchivePostgresModel, "search_heads", query)
    candidate = request({})
    candidate.args = {"cursor": "continuation-belongs-in-body"}
    with pytest.raises(BadRequestError, match="JSON body"):
        await search_default(candidate)
    query.assert_not_awaited()


async def test_search_cursors_bind_filter_archive_and_order_but_not_batch_size(
    monkeypatch,
):
    filters = {"metadata": {"customer_id": "CUST-0042"}}
    query = AsyncMock(return_value=(match(1), match(2)))
    monkeypatch.setattr(ArchivePostgresModel, "search_heads", query)
    first = orjson.loads(
        (await search_default(request({"filter": filters, "limit": 1}))).body
    )
    assert first["items"] == [match(1)]
    assert first["pagination"]["limit"] == 1
    cursor = first["pagination"]["next"]
    assert decode_cursor(
        cursor,
        scope=search_cursor_scope("default", filters),
        keys=frozenset({"document_id"}),
    ) == {"document_id": match(1)["id"]}
    query.return_value = (match(2),)
    second = orjson.loads(
        (
            await search_default(
                request({"filter": filters, "limit": 100, "cursor": cursor})
            )
        ).body
    )
    assert second["items"] == [match(2)]
    assert second["pagination"] == {"limit": 100, "next": None}
    query.assert_awaited_with("default", filters, limit=100, after=UUID(int=1))
    for archive, changed in (("different", filters), ("default", {})):
        query.reset_mock()
        with pytest.raises(BadRequestError):
            await search_scoped(request({"filter": changed, "cursor": cursor}), archive)
        query.assert_not_awaited()
    invalid = encode_cursor(
        search_cursor_scope("default", filters), {"document_id": "not-a-uuid"}
    )
    with pytest.raises(BadRequestError):
        await search_default(request({"filter": filters, "cursor": invalid}))


async def test_search_empty_result_and_unknown_namespace(monkeypatch):
    query = AsyncMock(return_value=())
    monkeypatch.setattr(ArchivePostgresModel, "search_heads", query)
    result = await search_scoped(request({}), "no-documents")
    assert orjson.loads(result.body) == {
        "archive": "no-documents",
        "items": [],
        "pagination": {"limit": 50, "next": None},
    }


@pytest.mark.parametrize(
    "error",
    [TimeoutError(), asyncpg.QueryCanceledError(), asyncpg.PostgresConnectionError()],
)
async def test_query_failures_are_not_empty_successes(monkeypatch, error):
    monkeypatch.setattr(
        ArchivePostgresModel, "search_heads", AsyncMock(side_effect=error)
    )
    with pytest.raises(ServiceUnavailable):
        await search_default(request({}))


async def test_sql_binds_inputs_and_returns_lightweight_metadata(monkeypatch):
    class Acquire:
        async def __aenter__(self):
            return connection

        async def __aexit__(self, *_):
            return None

    connection = SimpleNamespace(
        fetch=AsyncMock(
            return_value=[
                {
                    "document_id": UUID(int=1),
                    "version_id": UUID(int=2),
                    "parent_version_id": None,
                    "document_type": "invoice",
                    "metadata": '{"customer_id":"CUST-0042"}',
                    "created": datetime.datetime(2026, 10, 16, tzinfo=datetime.UTC),
                    "expires": None,
                }
            ]
        )
    )
    monkeypatch.setattr(
        ArchivePostgresModel,
        "pool",
        classmethod(lambda cls: SimpleNamespace(acquire=Acquire)),
    )
    filters = {
        "document_type": "invoice' OR TRUE --",
        "metadata": {"customer_id": "CUST-0042"},
    }
    rows = await ArchivePostgresModel.search_heads(
        "invoices", filters, limit=10, after=UUID(int=0)
    )
    sql, *parameters = connection.fetch.await_args.args
    assert parameters == [
        "invoices",
        filters["document_type"],
        orjson.dumps(filters["metadata"]).decode(),
        UUID(int=0),
        11,
    ]
    assert filters["document_type"] not in sql
    assert "version._id = aggregate.head_version_id" in sql
    assert "aggregate.lifecycle = 'active'" in sql
    assert "aggregate.document_id > $4::uuid" in sql
    assert "metadata @> $3::jsonb" in sql
    assert "ORDER BY aggregate.document_id ASC LIMIT $5" in sql
    assert "OFFSET" not in sql and "COUNT(" not in sql
    assert "archive_document_representations" not in sql
    assert rows[0]["version"] == {"id": str(UUID(int=2)), "parent": None}
    assert rows[0]["metadata"] == {"customer_id": "CUST-0042"}
    await ArchivePostgresModel.search_heads(
        "invoices", {"document_type": None}, limit=1
    )
    sql, *parameters = connection.fetch.await_args.args
    assert "version.document_type IS NULL" in sql
    assert "metadata @>" not in sql
    assert parameters == ["invoices", 2]
