r"""
Common HTTP middleware for Sanic.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from xarta.exceptions.http import BadRequestError

if TYPE_CHECKING:
    from sanic import HTTPResponse
    from sanic import Request


def ensure_json(f: callable) -> callable:

    def _wrapped(request: Request, **kwargs) -> HTTPResponse:
        if not request.json:
            raise BadRequestError

        return f(request, **kwargs)

    return _wrapped
