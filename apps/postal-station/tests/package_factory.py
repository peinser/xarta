from __future__ import annotations

import hashlib
import io
import json
import zipfile

from postal_station.models import PackageReceipt
from pyhanko.pdf_utils import generic
from pyhanko.pdf_utils.writer import PageObject
from pyhanko.pdf_utils.writer import PdfFileWriter

RUN_ID = "11111111-1111-4111-8111-111111111111"
OPERATION_ID = "22222222-2222-4222-8222-222222222222"
TASK_ID = "33333333-3333-4333-8333-333333333333"
JOB_ID = "44444444-4444-4444-8444-444444444444"


def _pdf() -> bytes:
    writer = PdfFileWriter()
    stream = writer.add_object(generic.StreamObject(stream_data=b"BT ET"))
    writer.insert_page(PageObject(stream, (0, 0, 595.276, 841.890)))
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def package_bytes(
    *,
    manifest_changes: dict[str, object] | None = None,
    extra: dict[str, bytes] | None = None,
):
    source = _pdf()
    source_path = "letters/000001/sources/001.pdf"
    manifest: dict[str, object] = {
        "schema": "xarta.postal.production-package/v3",
        "run_id": RUN_ID,
        "letters": [
            {
                "operation_id": OPERATION_ID,
                "task_id": TASK_ID,
                "plan_sha256": "a" * 64,
                "sequence": 1,
                "generation": 1,
                "job": {"id": JOB_ID, "sequence": 1, "kind": "letter"},
                "sources": [
                    {
                        "ordinal": 1,
                        "path": source_path,
                        "sha256": digest(source),
                        "byte_count": len(source),
                        "page_count": 1,
                        "role": "document",
                    }
                ],
                "print": {
                    "color_mode": "color",
                    "sides": "duplex_long_edge",
                    "document_boundary": "start_on_recto",
                },
                "traveller": {
                    "schema": "xarta.postal.traveller/v1",
                    "template_revision": "development-v1",
                    "service": {"type": "ordinary", "speed": "priority"},
                    "recipient": {
                        "name": "Recipient",
                        "company_name": None,
                        "address": {
                            "street": "Street",
                            "house_number": "1",
                            "postal_code": "1000",
                            "city": "Brussels",
                        },
                    },
                    "quantities": {
                        "content_pages": 1,
                        "blank_pages": 0,
                        "sheets": 1,
                        "estimated_weight_g": "10",
                    },
                    "franking": {
                        "provider": {
                            "id": "bpost",
                            "profile": {"id": "dev", "revision": "1"},
                        },
                        "method": "stamps",
                        "policy_revision": "1",
                        "supplies": [
                            {
                                "reference": "unit",
                                "description": "Unit stamp",
                                "quantity": 1,
                            }
                        ],
                        "pricing": {
                            "tariff_revision": "1",
                            "currency": "EUR",
                            "lines": [
                                {
                                    "reference": "unit",
                                    "quantity": 1,
                                    "unit_price": "1.50",
                                    "amount": "1.50",
                                }
                            ],
                            "total_postage": "1.50",
                        },
                    },
                },
            }
        ],
    }
    if manifest_changes:
        manifest.update(manifest_changes)
    files = {
        "manifest.json": json.dumps(manifest, separators=(",", ":")).encode(),
        source_path: source,
    }
    if extra:
        files.update(extra)
    checksums = "".join(
        f"{digest(value)}  {name}\n" for name, value in files.items()
    ).encode()
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, value in files.items():
            archive.writestr(name, value)
        archive.writestr("checksums.sha256", checksums)
    package = output.getvalue()
    return package, PackageReceipt(
        digest(package), digest(files["manifest.json"]), len(package)
    )


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()
