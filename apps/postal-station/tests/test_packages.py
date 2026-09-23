from __future__ import annotations

import io
import zipfile

import pytest

from package_factory import digest
from package_factory import package_bytes
from postal_station.models import PackageReceipt
from postal_station.packages import PackageVerificationError
from postal_station.packages import PackageVerifier


def test_verifies_every_layer_and_extracts_jobs(tmp_path):
    package, receipt = package_bytes()

    verified = PackageVerifier().verify(package, receipt, tmp_path / "run")

    assert [job.kind for job in verified.manifest.jobs] == ["letter"]
    source = verified.manifest.letters[0].sources[0]
    assert verified.path_for(source).read_bytes().startswith(b"%PDF")


@pytest.mark.parametrize("field", ["package_sha256", "manifest_sha256", "byte_count"])
def test_rejects_receipt_mismatch(tmp_path, field):
    package, receipt = package_bytes()
    values = {
        "package_sha256": receipt.package_sha256,
        "manifest_sha256": receipt.manifest_sha256,
        "byte_count": receipt.byte_count,
    }
    values[field] = 0 if field == "byte_count" else "0" * 64

    with pytest.raises(PackageVerificationError):
        PackageVerifier().verify(package, PackageReceipt(**values), tmp_path / "run")


def test_rejects_unknown_manifest_field(tmp_path):
    package, receipt = package_bytes(manifest_changes={"credentials": "forbidden"})

    with pytest.raises(PackageVerificationError, match="unknown=.*credentials"):
        PackageVerifier().verify(package, receipt, tmp_path / "run")


def test_rejects_unsafe_member_even_when_checksummed(tmp_path):
    package, receipt = package_bytes(extra={"../outside.pdf": b"bad"})

    with pytest.raises(PackageVerificationError, match="unsafe ZIP member"):
        PackageVerifier().verify(package, receipt, tmp_path / "run")


def test_rejects_member_checksum_mismatch(tmp_path):
    package, receipt = package_bytes()
    source = zipfile.ZipFile(io.BytesIO(package))
    output = io.BytesIO()
    with source, zipfile.ZipFile(output, "w") as changed:
        for info in source.infolist():
            value = source.read(info.filename)
            if info.filename == "letters/000001/sources/001.pdf":
                value = b"tampered"
            changed.writestr(info, value)
    changed_package = output.getvalue()
    changed_receipt = PackageReceipt(
        digest(changed_package), receipt.manifest_sha256, len(changed_package)
    )

    with pytest.raises(PackageVerificationError, match="member checksum mismatch"):
        PackageVerifier().verify(changed_package, changed_receipt, tmp_path / "run")


def test_rejects_checksum_file_with_incomplete_coverage(tmp_path):
    package, receipt = package_bytes()
    source = zipfile.ZipFile(io.BytesIO(package))
    output = io.BytesIO()
    with source, zipfile.ZipFile(output, "w") as changed:
        for info in source.infolist():
            value = source.read(info.filename)
            if info.filename == "checksums.sha256":
                value = value.splitlines(keepends=True)[0]
            changed.writestr(info, value)
    changed_package = output.getvalue()
    changed_receipt = PackageReceipt(
        digest(changed_package), receipt.manifest_sha256, len(changed_package)
    )

    with pytest.raises(PackageVerificationError, match="cover every package file"):
        PackageVerifier().verify(changed_package, changed_receipt, tmp_path / "run")


def test_rejects_package_over_uncompressed_size_limit(tmp_path):
    package, receipt = package_bytes()

    with pytest.raises(PackageVerificationError, match="uncompressed size"):
        PackageVerifier(max_uncompressed_bytes=10).verify(
            package, receipt, tmp_path / "run"
        )


def test_rejects_file_used_as_parent_directory(tmp_path):
    package, receipt = package_bytes(extra={"letters": b"not a directory"})

    with pytest.raises(PackageVerificationError, match="file as its parent"):
        PackageVerifier().verify(package, receipt, tmp_path / "run")


def test_rejects_v2_shape(tmp_path):
    package, receipt = package_bytes(
        manifest_changes={
            "jobs": [
                {
                    "id": "job-t",
                    "sequence": 1,
                    "kind": "traveller",
                    "generation": 1,
                    "path": "letters/000001/letter.pdf",
                    "sha256": digest(b"%PDF letter"),
                    "byte_count": len(b"%PDF letter"),
                    "color_mode": "monochrome",
                    "sides": "simplex",
                }
            ]
        }
    )

    with pytest.raises(PackageVerificationError, match="unknown=.*jobs"):
        PackageVerifier().verify(package, receipt, tmp_path / "run")
