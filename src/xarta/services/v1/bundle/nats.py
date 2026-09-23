r"""
Eventing model and utilities for the archive service.
"""

# ruff: noqa: I001

from __future__ import annotations

import asyncio
import mimetypes
import os
import shutil

from typing import TYPE_CHECKING

import aiofiles
import orjson

from nats.aio.msg import Msg

from xarta import cache
from xarta.http.sessions import HTTPRequestManager
from xarta.nats.sanic import SanicNATSJobsConsumerModel
from xarta.nats.sanic import SanicNATSSynchronousRequestsConsumerModel
from xarta.protocol.dag import CapabilityResult
from xarta.protocol.dag import OutcomeEmission
from xarta.protocol.dag.archive.constants import ARCHIVE_SERVICE_ENDPOINT
from xarta.protocol.dag.archive.constants import ARCHIVE_SERVICE_TIMEOUT
from xarta.protocol.dag.bundle import BundleNode
from xarta.protocol.document.source import DocumentSourceResult
from xarta.protocol.document.type import DocumentTypeIdentifier
from xarta.protocol.document.request.bundle import BUNDLE_STORAGE
from xarta.protocol.document.request.bundle import CACHE_BUNDLE_NAMESPACE
from xarta.protocol.document.request.bundle import DocumentBundleRequest
from xarta.telemetry import nats_producer_span

from .builder import build_zip

if TYPE_CHECKING:
    from uuid import UUID

    from aiohttp import ClientSession
    from sanic import Sanic


async def _download_document(id: UUID | str, filename: str | None = None):
    http_session = HTTPRequestManager.__session__
    if http_session is None:
        raise RuntimeError("HTTP session is not available")

    # Check if a custom filename is specified.
    if not filename:
        filename = id

    async with http_session.get(
        f"{ARCHIVE_SERVICE_ENDPOINT}/documents/{id}",
        timeout=ARCHIVE_SERVICE_TIMEOUT,
    ) as response:
        if response.status == 200:
            mime_type = response.headers.get("Content-Type", "").split(";", 1)[0]
            extension = mimetypes.guess_extension(mime_type) or ""
            return await response.read(), filename + extension

    return None


async def _bundler(msg: Msg, **kwargs):
    # Extract the job identifier from the event.
    job_id = orjson.loads(msg.data).get("id", None)

    # Extract the raw document bundle request from the cache.
    payload = await cache.manager.get(job_id, namespace=CACHE_BUNDLE_NAMESPACE)

    # Parse the document bundle request.
    request = DocumentBundleRequest.fromdict(payload)

    # Update the document bundle request to be in progress.
    await request.start()

    # Make the actual bundle.
    async with aiofiles.tempfile.TemporaryDirectory() as dir:
        # Download the individual files before bundling.
        files = await asyncio.gather(
            *(
                _download_document(id=document.id, filename=document.filename)
                for document in request.documents
            )
        )

        # Verify whether all files could be downloaded.
        missing = []
        for index, document in enumerate(request.documents):
            download_result = files[index]
            if not download_result:
                missing.append(document.id)

        if missing:
            await request.fail(missing=missing)
            raise ValueError("Cannot bundle documents:", missing)

        zip_filepath = f"{dir}/wip.zip"
        members = [
            (filename, data)
            for result in files
            if result is not None
            for data, filename in (result,)
        ]
        binary = await asyncio.to_thread(
            build_zip, members, request.options.compression
        )
        async with aiofiles.open(zip_filepath, "wb") as archive:
            await archive.write(binary)

        # Move the zipfile to the permanent storage, and ensure the hierchical structure exists.
        sub_directories = job_id[:2], job_id[2:4], job_id[4:6], job_id[6:8]
        base = os.path.join(BUNDLE_STORAGE, *sub_directories)
        await aiofiles.os.makedirs(base, exist_ok=True)
        await asyncio.to_thread(shutil.copy, zip_filepath, f"{base}/{job_id}")

    # Update the status of the bundle request.
    await request.complete()


async def _dag_worker(node: BundleNode, **kwargs) -> CapabilityResult:
    assert isinstance(node, BundleNode)
    documents = node.interpret()
    sources = await asyncio.gather(
        *(document.source.retrieve() for document in documents)
    )
    binary = await asyncio.to_thread(
        build_zip,
        [
            (document.filename, result.data)
            for document, result in zip(documents, sources, strict=True)
        ],
        node.compression,
    )
    await DocumentSourceResult(
        id=node.output_id,
        document_type=DocumentTypeIdentifier(value=None),  # type: ignore[arg-type]
        content_type="application/zip",
        data=binary,
    ).persist()
    return CapabilityResult((OutcomeEmission(outcome="success"),))


class BundleNATSModel(SanicNATSJobsConsumerModel):

    @classmethod
    async def bundle(cls, job_id: UUID) -> None:
        subject = f"jobs.bundle.{job_id}"
        headers = {"Nats-Msg-Id": str(job_id)}
        with nats_producer_span(subject, headers):
            await cls.jetstream().publish(
                subject,
                orjson.dumps({"id": job_id}, default=str),
                headers=headers,
            )

    @classmethod
    async def register(cls, app: Sanic, **kwargs) -> None:  # type: ignore[override]
        await super().register(
            app=app,
            fn=_bundler,
            name="bundle",
            **kwargs,
        )


class BundleDAGNATSModel(SanicNATSSynchronousRequestsConsumerModel):
    @classmethod
    async def register(cls, app: Sanic, **kwargs) -> None:  # type: ignore[override]
        await super().register(
            app=app,
            fn=_dag_worker,
            name="bundle",
            **kwargs,
        )
