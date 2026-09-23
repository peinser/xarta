from __future__ import annotations

import hashlib
import json

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from xarta.protocol.document.request import bundle as bundle_protocol
from xarta.services.v1.bundle.builder import build_zip


@dataclass(frozen=True, slots=True)
class PackageSource:
    ordinal: int
    path: str
    content: bytes
    sha256: str
    byte_count: int
    page_count: int
    role: str


@dataclass(frozen=True, slots=True)
class PackageLetter:
    operation_id: str
    task_id: str
    plan_sha256: str
    sequence: int
    generation: int
    job_id: str
    sources: tuple[PackageSource, ...]
    print_instructions: dict[str, Any]
    traveller: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ProductionPackage:
    filename: str
    content: bytes
    checksum: str
    manifest_checksum: str
    byte_count: int


def build_production_package(
    run_id: str, letters: tuple[PackageLetter, ...]
) -> ProductionPackage:
    if not run_id or not letters:
        raise ValueError("Production package requires a run ID and at least one letter")
    members: list[tuple[str, bytes]] = []
    manifest_letters: list[dict[str, Any]] = []
    if tuple(letter.sequence for letter in letters) != tuple(
        range(1, len(letters) + 1)
    ):
        raise ValueError("Package letters must have contiguous sequence order")
    for letter in letters:
        source_entries = []
        for source in letter.sources:
            _safe_path(source.path)
            if (
                hashlib.sha256(source.content).hexdigest() != source.sha256
                or len(source.content) != source.byte_count
            ):
                raise ValueError(
                    "Package source bytes do not match persisted production plan"
                )
            members.append((source.path, source.content))
            source_entries.append(
                {
                    "ordinal": source.ordinal,
                    "path": source.path,
                    "sha256": source.sha256,
                    "byte_count": source.byte_count,
                    "page_count": source.page_count,
                    "role": source.role,
                }
            )
        manifest_letters.append(
            {
                "operation_id": letter.operation_id,
                "task_id": letter.task_id,
                "plan_sha256": letter.plan_sha256,
                "sequence": letter.sequence,
                "generation": letter.generation,
                "job": {
                    "id": letter.job_id,
                    "sequence": letter.sequence,
                    "kind": "letter",
                },
                "sources": source_entries,
                "print": letter.print_instructions,
                "traveller": letter.traveller,
            }
        )
    manifest = _json_bytes(
        {
            "schema": "xarta.postal.production-package/v3",
            "run_id": run_id,
            "letters": manifest_letters,
        }
    )
    members.append(("manifest.json", manifest))
    checksums = "".join(
        f"{hashlib.sha256(content).hexdigest()}  {name}\n"
        for name, content in sorted(members)
    ).encode("ascii")
    members.append(("checksums.sha256", checksums))
    content = build_zip(
        members,
        bundle_protocol.DocumentBundleRequestCompressionOptions(
            bundle_protocol.DocumentBundleRequestCompressionMethod.DEFLATED, 9
        ),
    )
    return ProductionPackage(
        f"postal-run-{run_id}.zip",
        content,
        hashlib.sha256(content).hexdigest(),
        hashlib.sha256(manifest).hexdigest(),
        len(content),
    )


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _safe_path(value: str) -> None:
    path = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError("Package source path is unsafe")
