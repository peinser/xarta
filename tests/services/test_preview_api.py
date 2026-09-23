from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import orjson
import pytest

import xarta.services.v1.preview.base as preview_module

from tests.fixtures.jinja_selftest import SELFTEST_TEMPLATE_ENGINE
from tests.fixtures.jinja_selftest import jinja_selftest_template_engine_options


class ResponseContext:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, *_):
        return None


class Session:
    def __init__(self, response):
        self.response = response
        self.kwargs = None

    def post(self, _url, **kwargs):
        self.kwargs = kwargs
        return ResponseContext(self.response)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "parameters",
    [
        None,
        {
            "title": "Xarta Preview self-test",
            "locale": {"language": "en", "country": "GB"},
        },
    ],
)
async def test_preview_forwards_jinja_selftest_request(monkeypatch, parameters) -> None:
    response = SimpleNamespace(
        status=200,
        headers={"content-type": "text/html"},
        read=AsyncMock(return_value=b"<html>Engine self-test</html>"),
    )
    session = Session(response)
    monkeypatch.setattr(preview_module.HTTPRequestManager, "__session__", session)
    request_data = {
        "document_type": "test",
        "content_type": "text/html",
        "template_engine": SELFTEST_TEMPLATE_ENGINE,
        "template_engine_options": jinja_selftest_template_engine_options(),
        "payload": {"content_type": "application/json", "data": {}},
    }
    if parameters is not None:
        request_data["parameters"] = parameters
    request = SimpleNamespace(
        json=request_data,
        headers={"content-type": "application/json"},
    )

    result = await preview_module._json(request)

    assert result.status == 200
    assert result.headers["content-type"] == "text/html"
    assert result.body == b"<html>Engine self-test</html>"
    assert orjson.loads(session.kwargs["data"]) == request_data
