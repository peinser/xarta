"""Verification and safe extraction of postal production packages."""

from __future__ import annotations

import hashlib
import json
import shutil
import stat
import zipfile

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from pathlib import PurePosixPath

from postal_station.models import PackageManifest
from postal_station.models import PackageReceipt
from postal_station.models import VerifiedPackage
from postal_station.rendering.composition import page_count


class PackageVerificationError(ValueError):
    """The downloaded package does not match its authenticated metadata."""


@dataclass(frozen=True, slots=True)
class PackageVerifier:
    max_members: int = 10_000
    max_uncompressed_bytes: int = 2_147_483_648

    def verify(
        self, package: bytes, receipt: PackageReceipt, destination: Path
    ) -> VerifiedPackage:
        if len(package) != receipt.byte_count:
            raise PackageVerificationError("package byte count does not match receipt")
        if _sha256(package) != receipt.package_sha256:
            raise PackageVerificationError("package checksum does not match receipt")

        try:
            with zipfile.ZipFile(BytesIO(package)) as archive:
                members = self._validated_members(archive)
                manifest_bytes = archive.read("manifest.json")
                if _sha256(manifest_bytes) != receipt.manifest_sha256:
                    raise PackageVerificationError(
                        "manifest checksum does not match receipt"
                    )
                checksums = _parse_checksums(archive.read("checksums.sha256"))
                file_names = {
                    name for name, info in members.items() if not info.is_dir()
                }
                expected_checksum_names = file_names - {"checksums.sha256"}
                if set(checksums) != expected_checksum_names:
                    raise PackageVerificationError(
                        "checksum manifest must cover every package file exactly once"
                    )
                for name, expected_digest in checksums.items():
                    if _sha256(archive.read(name)) != expected_digest:
                        raise PackageVerificationError(
                            f"member checksum mismatch: {name}"
                        )

                manifest = _parse_manifest(manifest_bytes)
                source_paths = {
                    source.path
                    for letter in manifest.letters
                    for source in letter.sources
                }
                if file_names != {"manifest.json", "checksums.sha256", *source_paths}:
                    raise PackageVerificationError(
                        "package may contain only manifest, checksums, and declared sources"
                    )
                for letter in manifest.letters:
                    for source in letter.sources:
                        if source.path not in file_names:
                            raise PackageVerificationError(
                                f"source member is missing: {source.path}"
                            )
                        value = archive.read(source.path)
                        info = members[source.path]
                        if (
                            info.file_size != source.byte_count
                            or checksums[source.path] != source.sha256
                        ):
                            raise PackageVerificationError(
                                f"source metadata does not match member: {source.path}"
                            )
                        try:
                            pages = page_count(value)
                        except Exception as error:
                            raise PackageVerificationError(
                                f"source is not a strict PDF: {source.path}"
                            ) from error
                        if pages != source.page_count:
                            raise PackageVerificationError(
                                f"source page count does not match member: {source.path}"
                            )
                self._extract(archive, members, destination)
        except (KeyError, zipfile.BadZipFile, UnicodeDecodeError) as error:
            raise PackageVerificationError("invalid production package") from error
        return VerifiedPackage(manifest, destination)

    def _validated_members(
        self, archive: zipfile.ZipFile
    ) -> dict[str, zipfile.ZipInfo]:
        if len(archive.infolist()) > self.max_members:
            raise PackageVerificationError("ZIP contains too many members")
        members: dict[str, zipfile.ZipInfo] = {}
        uncompressed_bytes = 0
        for info in archive.infolist():
            name = info.filename
            if name in members:
                raise PackageVerificationError(f"duplicate ZIP member: {name}")
            _validate_member_path(name)
            if info.flag_bits & 0x1:
                raise PackageVerificationError(f"encrypted ZIP member: {name}")
            mode = info.external_attr >> 16
            file_type = stat.S_IFMT(mode)
            if file_type and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                raise PackageVerificationError(f"non-regular ZIP member: {name}")
            uncompressed_bytes += info.file_size
            if uncompressed_bytes > self.max_uncompressed_bytes:
                raise PackageVerificationError(
                    "ZIP uncompressed size exceeds the configured limit"
                )
            members[name] = info
        if "manifest.json" not in members or "checksums.sha256" not in members:
            raise PackageVerificationError(
                "package must contain manifest.json and checksums.sha256"
            )
        _validate_member_tree(members)
        return members

    @staticmethod
    def _extract(
        archive: zipfile.ZipFile, members: dict[str, zipfile.ZipInfo], destination: Path
    ) -> None:
        try:
            destination.mkdir(mode=0o700, parents=True, exist_ok=False)
            for name, info in members.items():
                target = destination.joinpath(*name.split("/"))
                if info.is_dir():
                    target.mkdir(mode=0o700, parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                target.write_bytes(archive.read(name))
                target.chmod(0o600)
        except Exception:
            shutil.rmtree(destination, ignore_errors=True)
            raise


def _validate_member_path(name: str) -> None:
    path = PurePosixPath(name)
    if (
        not name
        or "\\" in name
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise PackageVerificationError(f"unsafe ZIP member path: {name}")


def _validate_member_tree(members: dict[str, zipfile.ZipInfo]) -> None:
    file_names = {
        name.rstrip("/") for name, info in members.items() if not info.is_dir()
    }
    directory_names = {
        name.rstrip("/") for name, info in members.items() if info.is_dir()
    }
    if file_names & directory_names:
        raise PackageVerificationError("ZIP path is both a file and directory")
    for name in members:
        parts = PurePosixPath(name).parts
        for index in range(1, len(parts)):
            if "/".join(parts[:index]) in file_names:
                raise PackageVerificationError("ZIP member has a file as its parent")


def _parse_checksums(raw: bytes) -> dict[str, str]:
    checksums: dict[str, str] = {}
    try:
        lines = raw.decode("ascii").splitlines()
    except UnicodeDecodeError as error:
        raise PackageVerificationError("checksums.sha256 must be ASCII") from error
    if not lines:
        raise PackageVerificationError("checksums.sha256 must not be empty")
    for line in lines:
        if len(line) < 67 or line[64:66] != "  ":
            raise PackageVerificationError("invalid checksums.sha256 line")
        digest, name = line[:64], line[66:]
        if len(digest) != 64 or any(
            character not in "0123456789abcdef" for character in digest
        ):
            raise PackageVerificationError("invalid member SHA-256 digest")
        _validate_member_path(name)
        if name in checksums:
            raise PackageVerificationError(f"duplicate checksum entry: {name}")
        checksums[name] = digest
    return checksums


def _parse_manifest(raw: bytes) -> PackageManifest:
    try:
        value = json.loads(raw)
        return PackageManifest.from_dict(value)
    except (json.JSONDecodeError, ValueError) as error:
        raise PackageVerificationError(f"invalid package manifest: {error}") from error


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()
