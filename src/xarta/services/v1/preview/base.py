r"""
Base blueprint definition of the v1 preview API.
"""

from __future__ import annotations

import uuid

from typing import TYPE_CHECKING

import orjson
import xmltodict

from sanic import Blueprint
from sanic import response

from xarta.exceptions.http import BadRequestError
from xarta.http.sessions import HTTPRequestManager
from xarta.protocol.document.request.render import RENDER_SERVICE_ENDPOINT
from xarta.protocol.document.request.render import RENDER_SERVICE_TIMEOUT
from xarta.protocol.document.request.render import DocumentRenderRequest

if TYPE_CHECKING:
    from sanic import HTTPResponse
    from sanic import Request


bp: Blueprint = Blueprint(
    name="preview-v1",
    url_prefix="/api/v1/preview",
)


@bp.route("/", methods=["POST"])
async def preview(request: Request) -> HTTPResponse:
    r"""
    The preview service simply passes the request to the rendering service, which
    is responsible for handling the document rendering. Any error codes or payloads
    will be directly returned to the client.
    """
    match request.headers.get("content-type", None):
        case "application/json":
            return await _json(request)
        case "application/xml":
            return await _xml(request)
        case "text/xml":
            return await _xml(request)

    raise BadRequestError


async def _json(request: Request) -> HTTPResponse:
    if not request.json:
        raise BadRequestError

    # Extract the user-provided preview request.
    preview_request = request.json

    # Enrich the payload content-type if no such element has been specified.
    payload = preview_request.get("payload", {})
    if not payload.get("content_type", None):
        payload["content_type"] = "application/json"

    try:
        DocumentRenderRequest.validate(preview_request)
    except Exception as ex:
        raise BadRequestError from ex

    headers = {
        "correlation-id": request.headers.get("correlation-id", str(uuid.uuid4())),
        "content-type": "application/json",
    }

    http_session = HTTPRequestManager.__session__
    async with http_session.post(
        RENDER_SERVICE_ENDPOINT,
        data=orjson.dumps(preview_request),
        headers=headers,
        timeout=RENDER_SERVICE_TIMEOUT,
    ) as r:
        if r.status == 200:
            content_type = r.headers.get("content-type", None)
            assert (
                content_type
            ), r"Content-Type must be provided by the rendering service."
            headers = {"content-type": content_type}

        return response.raw(
            headers=headers,
            status=r.status,
            body=await r.read(),
        )


async def _xml(request: Request) -> HTTPResponse:
    if not request.body:
        raise BadRequestError

    # TODO Optimize, just pass the XML payload along with application/xml as payload type.

    preview_request = xmltodict.parse(request.body)["preview"]
    payload = preview_request.get("payload", {})
    if not payload.get("content_type", None):
        payload["content_type"] = "application/json+xml"

    request.parsed_json = preview_request

    return await _json(request)
