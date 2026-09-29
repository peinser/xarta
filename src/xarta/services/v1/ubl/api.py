from __future__ import annotations

import asyncio

from sanic import response

from xarta.ubl.validation import RULESET_ID
from xarta.ubl.validation import UBLValidationUnavailable


async def validation_profiles(request):
    return response.json({"profiles": request.app.ctx.ubl_validator.profiles()})


def _input_error(request):
    if request.content_type not in {"application/xml", "text/xml"}:
        return response.json(
            {"error": "Send the UBL XML as application/xml or text/xml"}, status=415
        )
    if len(request.body) > request.app.ctx.ubl_validator.editor.max_bytes:
        return response.json(
            {"error": "UBL document exceeds the size limit"}, status=413
        )
    return None


async def validate_schema(request):
    error = _input_error(request)
    if error is not None:
        return error
    result = await asyncio.to_thread(
        request.app.ctx.ubl_validator.validate_schema, request.body
    )
    return response.json(result.dict())


async def validate_business_rules(request):
    error = _input_error(request)
    if error is not None:
        return error
    try:
        result = await request.app.ctx.ubl_validator.validate_business_rules(
            request.body,
            request.args.get("profile", RULESET_ID),
        )
    except ValueError as ex:
        return response.json({"error": str(ex)}, status=400)
    except UBLValidationUnavailable as ex:
        return response.json(
            {"valid": None, "status": "indeterminate", "error": str(ex)}, status=503
        )
    return response.json(result.dict())
