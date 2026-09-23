r"""
Base blueprint definition of the v1 render API.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiocache import cached
from jsonschema import ValidationError
from sanic import Blueprint

from xarta import env
from xarta.exceptions.http import BadRequestError
from xarta.exceptions.http import ServiceUnavailable
from xarta.exceptions.protocol import InvalidDocumentType
from xarta.exceptions.protocol import InvalidTemplateEngine
from xarta.http.sessions import HTTPRequestManager
from xarta.logging import logger
from xarta.protocol.document.request.render import DocumentRenderRequest
from xarta.protocol.document.type import DocumentType
from xarta.protocol.document.type.constants import DOCUMENT_TYPE_SERVICE_ENDPOINT
from xarta.protocol.document.type.constants import DOCUMENT_TYPE_SERVICE_TIMEOUT
from xarta.protocol.template.engine import TemplateEngine
from xarta.protocol.template.engine import TemplateEngineIdentifier

from . import utils

if TYPE_CHECKING:
    from aiohttp import ClientSession
    from sanic import HTTPResponse
    from sanic import Request
    from sanic import Sanic


TEMPLATE_ENGINES: dict[TemplateEngineIdentifier, TemplateEngine] = None


bp = Blueprint(
    name="render-v1",
    url_prefix="/api/v1/render",
)


env.verify(
    blueprint=bp,
    required={
        "DOCUMENT_TYPE_SERVICE_ENDPOINT",
        "DOCUMENT_TYPE_SERVICE_TIMEOUT",
    },
)


@bp.listener("before_server_start")
async def _load_template_engines(_: Sanic) -> None:
    global TEMPLATE_ENGINES

    TEMPLATE_ENGINES = await utils.load_template_engines()


@cached(ttl=600, key_builder=lambda ns, identifier, **kw: f"{ns}:{identifier}")
async def _fetch_document_type(session: ClientSession, identifier: str) -> DocumentType:
    async with session.get(
        f"{DOCUMENT_TYPE_SERVICE_ENDPOINT}/{identifier}",
        timeout=DOCUMENT_TYPE_SERVICE_TIMEOUT,
    ) as response:
        if response.status != 200:
            raise BadRequestError

        return DocumentType.fromdict(await response.json())


@bp.route("/", methods=["POST"])
async def render(request: Request) -> HTTPResponse:
    try:
        # Parse the specified render request, and validate the structure.
        render_request = DocumentRenderRequest.fromdict(request.json)

    except ValidationError as ex:
        raise BadRequestError from ex

    # Extract the shared HTTP session manager.
    http_session = HTTPRequestManager.__session__

    try:
        # Retrieve additional configuration options based on the document type.
        document_type = await _fetch_document_type(
            session=http_session,
            identifier=render_request.document_type.value,
        )

    except BadRequestError as ex:
        raise InvalidDocumentType from ex

    # Enrich the rendering options with the optional configuration.
    render_request.enrich(document_type=document_type)

    try:
        template_engine = TEMPLATE_ENGINES[render_request.template_engine]

    except KeyError as ex:
        raise InvalidTemplateEngine from ex

    try:
        operation = template_engine.prepare(render_request)

    except ValueError as ex:
        raise BadRequestError(message=str(ex)) from ex

    try:
        return await operation.execute(session=http_session)
    except Exception:
        message = f"The rendering engine at {template_engine.endpoint} is unavailable."
        await logger.aerror(
            message,
            correlation_id=str(render_request.correlation_id),
            document_type=render_request.document_type.value,
        )

    raise ServiceUnavailable(message=message)
