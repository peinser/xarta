from __future__ import annotations

import asyncio
import hashlib

from io import BytesIO
from uuid import uuid5

from pyhanko.pdf_utils.reader import PdfFileReader

from xarta.services.v1.postal.adapters.local.packages import PackageLetter
from xarta.services.v1.postal.adapters.local.packages import PackageSource
from xarta.services.v1.postal.adapters.local.packages import build_production_package


def _page_count(document: bytes) -> int:
    reader = PdfFileReader(BytesIO(document), strict=True)
    count = int(reader.root["/Pages"]["/Count"])
    if count < 1:
        raise ValueError("Postal source PDF must contain at least one page")
    for index in range(count):
        reader.find_page_for_modification(index)
    return count


async def build_and_publish_run(
    repository, artifacts, run, *, station_id: str, site_id: str
) -> None:
    items = await repository.run_package_items(run.id, station_id, site_id)
    if not items:
        raise ValueError("No eligible postal tasks were claimed")
    letters = []
    for item in items:
        plan_documents = {
            document["ordinal"]: document for document in item.plan["documents"]
        }
        sources = []
        for (
            ordinal,
            reference,
            role,
            persisted_sha256,
            persisted_size,
            persisted_pages,
        ) in item.sources:
            document = plan_documents.get(ordinal)
            if document is None:
                raise ValueError(
                    "Persisted source is absent from immutable production plan"
                )
            expected = (
                document["sha256"],
                document["size"],
                document["page_count"],
                document["role"],
            )
            if expected != (persisted_sha256, persisted_size, persisted_pages, role):
                raise ValueError(
                    "Persisted source metadata differs from immutable production plan"
                )
            content = await artifacts.get(reference)
            if (
                hashlib.sha256(content).hexdigest() != persisted_sha256
                or len(content) != persisted_size
                or await asyncio.to_thread(_page_count, content) != persisted_pages
            ):
                raise ValueError(
                    "Stored source bytes differ from immutable production plan"
                )
            path = f"letters/{item.sequence:06d}/sources/{ordinal:03d}.pdf"
            sources.append(
                PackageSource(
                    ordinal,
                    path,
                    content,
                    persisted_sha256,
                    persisted_size,
                    persisted_pages,
                    role,
                )
            )
        mailpiece = item.plan["mailpiece"]
        recipient = mailpiece["recipient"]
        address = recipient["address"]
        traveller = {
            "schema": "xarta.postal.traveller/v1",
            "template_revision": item.plan["traveller"]["template_revision"],
            "service": {
                "type": mailpiece["service"]["type"],
                "speed": mailpiece["service"].get("speed"),
            },
            "recipient": {
                "name": recipient.get("name"),
                "company_name": recipient.get("company_name"),
                "address": {
                    name: address[name]
                    for name in ("street", "house_number", "postal_code", "city")
                },
            },
            "quantities": item.plan["quantities"],
            "franking": item.plan["franking"],
        }
        letters.append(
            PackageLetter(
                str(item.operation_id),
                str(item.task_id),
                item.plan_digest,
                item.sequence,
                item.generation,
                str(uuid5(item.task_id, f"print-job:{item.generation}:letter")),
                tuple(sources),
                {
                    name: item.plan["print"][name]
                    for name in ("color_mode", "sides", "document_boundary")
                },
                traveller,
            )
        )
    package = await asyncio.to_thread(
        build_production_package, str(run.id), tuple(letters)
    )
    package_reference = f"postal-local/runs/{run.id}/{package.checksum}.zip"
    await artifacts.storage.put(package_reference, package.content, "application/zip")
    await repository.publish_run_package(
        run_id=run.id,
        package_storage_reference=package_reference,
        package_checksum=package.checksum,
        manifest_checksum=package.manifest_checksum,
        package_byte_count=package.byte_count,
        items=items,
    )
