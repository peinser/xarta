r"""
Endpoints related to bundling documents.
"""

from __future__ import annotations

import os

from typing import TYPE_CHECKING

from sanic import response

from xarta import cache
from xarta import ratelimiting
from xarta.exceptions.http import BadRequestError
from xarta.protocol.document.request.bundle import BUNDLE_STORAGE
from xarta.protocol.document.request.bundle import BUNDLE_TIMEOUT
from xarta.protocol.document.request.bundle import CACHE_BUNDLE_NAMESPACE
from xarta.protocol.document.request.bundle import DocumentBundleRequest
from xarta.protocol.document.request.bundle import DocumentBundleRequestState

from .base import bp
from .nats import BundleNATSModel as NATS

if TYPE_CHECKING:
    from uuid import UUID

    from sanic import HTTPResponse
    from sanic import Request


@bp.route("/", methods=["POST"])
@ratelimiting.tb(name="bundle", max_bucket_size=5, refill_rate=0.5, ignore_bogon=True)
async def start_zip_download(request: Request) -> HTTPResponse:
    payload = dict(request.json or {})

    # Delete the `id` and `state` keys from the provided payload. They cannot be set by the end-user.
    payload.pop("id", None)
    payload.pop("status", None)

    try:
        bundle_request = DocumentBundleRequest.fromdict(payload)
    except ValueError as ex:
        raise BadRequestError(str(ex)) from ex
    payload = bundle_request.dict()  # Get a clean serialized request

    await cache.manager.set(
        bundle_request.id, payload, ttl=BUNDLE_TIMEOUT, namespace=CACHE_BUNDLE_NAMESPACE
    )
    await NATS.bundle(bundle_request.id)

    return response.json(payload, status=202)  # Accepted for asynchronous processing.


@bp.route("/<job_id:uuid>", methods=["GET"])
@ratelimiting.tb(
    name="bundle-status", max_bucket_size=10, refill_rate=0.5, ignore_bogon=True
)
async def job_status(_: Request, job_id: UUID) -> HTTPResponse:
    payload = await cache.manager.get(job_id, namespace=CACHE_BUNDLE_NAMESPACE)
    if not payload:
        return response.empty(status=404)

    request = DocumentBundleRequest.fromdict(payload)

    # Check if the job status is completed.
    if request.status.state == DocumentBundleRequestState.COMPLETED:
        # Check if a custom filename has been specified.
        if request.options.filename:
            headers = {
                "Content-Disposition": f'Attachment; filename="{request.options.filename}"',
            }
        else:
            headers = None

        # Prepare the base path in the hierarchical structure.
        iden = str(request.id)
        sub_directories = iden[:2], iden[2:4], iden[4:6], iden[6:8]
        base = os.path.join(BUNDLE_STORAGE, *sub_directories)

        return await response.file_stream(
            location=f"{base}/{request.id}",
            chunk_size=16384,
            headers=headers,
        )

    return response.json(request.dict())
