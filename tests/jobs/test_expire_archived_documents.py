from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import Mock

import pytest

from xarta.jobs.expire_archived_documents.base import DB
from xarta.jobs.expire_archived_documents.base import _launch
from xarta.jobs.expire_archived_documents.db import ArchivePostgresModel


class AcceptedResponse:
    status = 202

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


@pytest.mark.asyncio
async def test_expiry_job_accepts_deletion_enqueue_response(monkeypatch) -> None:
    monkeypatch.setattr(
        DB,
        "expired",
        AsyncMock(return_value=[("payroll", "00000000-0000-0000-0000-000000000001")]),
    )
    session = SimpleNamespace(delete=lambda *_args, **_kwargs: AcceptedResponse())
    app = SimpleNamespace(
        ctx=SimpleNamespace(http_client_session=session),
        stop=Mock(),
    )

    await _launch(app)

    app.stop.assert_called_once_with()


def test_expiry_query_joins_current_version_by_internal_id() -> None:
    query = ArchivePostgresModel.QUERY_FIND_EXPIRED
    assert "version._id = aggregate.head_version_id" in query
    assert "version.archive" not in query
    assert "version.document_id" not in query
