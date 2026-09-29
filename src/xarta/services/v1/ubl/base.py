from __future__ import annotations

from typing import TYPE_CHECKING

from lxml import etree  # type: ignore[import-untyped]
from sanic import Blueprint
from sanic import response

from xarta import env
from xarta.services.v1.ubl.api import validate_business_rules
from xarta.services.v1.ubl.api import validate_schema
from xarta.services.v1.ubl.api import validation_profiles
from xarta.storage.configuration import initialize_temporary_storage
from xarta.ubl.editor import UBLEditor
from xarta.ubl.validation import UBLValidator

from .nats import UBLNATSModel

if TYPE_CHECKING:
    from sanic import HTTPResponse
    from sanic import Request
    from sanic import Sanic

bp = Blueprint(name="ubl-v1", url_prefix="/api/v1/ubl")
env.verify(
    blueprint=bp,
    required={
        "NATS_SERVERS",
        "TMP_STORAGE",
        "ARCHIVE_SERVICE_ENDPOINT",
        "ARCHIVE_SERVICE_TIMEOUT",
        "BUNDLE_SERVICE_ENDPOINT",
        "BUNDLE_SERVICE_TIMEOUT",
        "RENDER_SERVICE_ENDPOINT",
        "RENDER_SERVICE_TIMEOUT",
    },
)


@bp.listener("before_server_start")
async def _setup_ubl(app: Sanic) -> None:
    await initialize_temporary_storage()
    try:
        editor = UBLEditor(
            max_bytes=env.extract("UBL_MAX_BYTES", default="10485760", dtype=int)
        )
        app.ctx.ubl_validator = UBLValidator(
            editor,
            timeout=env.extract("UBL_VALIDATION_TIMEOUT", default="30", dtype=float),
            concurrency=env.extract(
                "UBL_VALIDATION_CONCURRENCY", default="2", dtype=int
            ),
        )
    except (OSError, TypeError, ValueError, etree.LxmlError) as ex:
        raise env.ConfigurationError(
            "UBL requires positive limits and intact bundled schemas and rules"
        ) from ex
    await UBLNATSModel.register(app=app, editor=editor)


@bp.get("/")
async def ubl(_: Request) -> HTTPResponse:
    return response.empty()


bp.add_route(validate_schema, "/validate/schema", methods=["POST"])
bp.add_route(validate_business_rules, "/validate/business-rules", methods=["POST"])
bp.add_route(validation_profiles, "/validation/profiles", methods=["GET"])
