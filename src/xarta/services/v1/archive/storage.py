from __future__ import annotations

import asyncio
import contextlib
import os
import uuid

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from typing import Protocol
from zlib import crc32

from xarta.protocol.document.archive import validate_archive_name
from xarta.storage.s3 import S3ClientManager


class StorageBackend(Protocol):
    def representation_key_prefix(
        self, archive: str, document_id: uuid.UUID, version_id: uuid.UUID
    ) -> str:
        """Return the prefix for a representation UUID and private allocation suffix.

        Called once when preparing a snapshot. Persist completed keys; reads and
        deletion use those exact keys rather than regenerating them.
        """
        ...

    async def put(self, key: str, data: bytes, content_type: str) -> bool:
        """Store immutable bytes and return whether this call created them.

        Raise FileExistsError when the key already holds different bytes.
        """
        ...

    async def get(self, key: str) -> bytes: ...

    async def delete(self, key: str) -> None: ...


def _fsync_directory(directory: str) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _make_durable_directories(directory: str) -> None:
    """Create missing directories and sync each new entry into its parent."""
    missing = []
    current = directory
    while not os.path.isdir(current):
        missing.append(current)
        current = os.path.dirname(current)
    os.makedirs(directory, exist_ok=True)
    for created in reversed(missing):
        _fsync_directory(os.path.dirname(created))


def _read(path: str) -> bytes:
    with open(path, "rb") as stream:
        return stream.read()


def _write_durably(path: str, data: bytes) -> bool:
    """Store immutable bytes at path and return whether this call created them.

    The database commits a reference to the path after this returns, so the bytes
    and the directory entries leading to them must be durable first.
    """
    directory = os.path.dirname(path)
    _make_durable_directories(directory)
    temporary = f"{path}.{uuid.uuid4()}.tmp"
    try:
        with open(temporary, "xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
            created = True
        except FileExistsError as ex:
            if _read(path) != data:
                raise FileExistsError(
                    f"Immutable storage file already exists: {path}"
                ) from ex
            created = False
        _fsync_directory(directory)
        return created
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.remove(temporary)


class FilesystemStorageBackend:
    def __init__(self, root: str) -> None:
        self.root = os.path.abspath(root)

    @staticmethod
    def representation_key_prefix(
        archive: str, document_id: uuid.UUID, version_id: uuid.UUID
    ) -> str:
        # CRC32 of the 16 UUID bytes mixes time-ordered prefixes cheaply. The low
        # 24 bits select directories only; filenames retain the logical identities.
        # Ponytail: a document's versions share a leaf. Shard by representation
        # if unusually large per-document histories make that leaf too large.
        shard = crc32(document_id.bytes) & 0xFFFFFF
        return (
            f"{archive}/{shard >> 16:02x}/{(shard >> 8) & 0xFF:02x}/"
            f"{shard & 0xFF:02x}/{document_id}-{version_id}-"
        )

    def _path(self, key: str) -> str:
        path = os.path.abspath(os.path.join(self.root, key))
        if os.path.commonpath((self.root, path)) != self.root:
            raise ValueError("Storage key escapes the configured root")
        return path

    async def put(self, key: str, data: bytes, content_type: str) -> bool:
        del content_type
        # One thread hop per file: each hop wakes a worker thread and the event
        # loop, and per-step hops made those wake-ups the per-process bottleneck.
        return await asyncio.to_thread(_write_durably, self._path(key), data)

    async def get(self, key: str) -> bytes:
        return await asyncio.to_thread(_read, self._path(key))

    async def delete(self, key: str) -> None:
        with contextlib.suppress(FileNotFoundError):
            await asyncio.to_thread(os.remove, self._path(key))


class LegacyFilesystemStorageBackend(FilesystemStorageBackend):
    def __init__(self) -> None:
        pass

    def _path(self, key: str) -> str:
        return os.path.abspath(key)


class ArchiveStorageSessions:
    """Process-lifetime S3 clients shared by configured archive backends."""

    def __init__(self) -> None:
        self._s3: dict[tuple[tuple[str, Any], ...], S3ClientManager] = {}

    def s3(self, client_options: Mapping[str, Any]) -> S3ClientManager:
        key = tuple(sorted(client_options.items()))
        if key not in self._s3:
            # Uploads hold no database connection, so concurrent requests can all be
            # uploading at once (two objects each for a typical snapshot). With the
            # client default of 10, uploads queue from about 16 concurrent writes.
            self._s3[key] = S3ClientManager(
                dict(client_options), max_pool_connections=64
            )
        return self._s3[key]

    async def close(self) -> None:
        """Close all archive S3 clients and their HTTP connection pools."""
        await asyncio.gather(*(client.close() for client in self._s3.values()))


class S3StorageBackend:
    @staticmethod
    def representation_key_prefix(
        archive: str, document_id: uuid.UUID, version_id: uuid.UUID
    ) -> str:
        # Object-key prefixes are not filesystem directories; no shard is needed.
        return f"{archive}/{document_id}/{version_id}/"

    def __init__(
        self,
        configuration: Mapping[str, Any],
        sessions: ArchiveStorageSessions | None = None,
    ) -> None:
        self.bucket = str(configuration["bucket"])
        self.prefix = str(configuration.get("prefix", "")).strip("/")
        self.client_options = {
            key: configuration[key]
            for key in (
                "endpoint_url",
                "aws_access_key_id",
                "aws_secret_access_key",
                "aws_session_token",
                "region_name",
                "use_ssl",
                "verify",
            )
            if key in configuration
        }
        self._client_manager = (sessions or ArchiveStorageSessions()).s3(
            self.client_options
        )

    def _key(self, key: str) -> str:
        return f"{self.prefix}/{key}" if self.prefix else key

    async def put(self, key: str, data: bytes, content_type: str) -> bool:
        from botocore.exceptions import ClientError  # type: ignore[import-untyped]

        try:
            client = await self._client_manager.get()
            await client.put_object(
                Bucket=self.bucket,
                Key=self._key(key),
                Body=data,
                ContentType=content_type,
                IfNoneMatch="*",
            )
            return True
        except ClientError as ex:
            status = ex.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if status != 412:
                raise
            if await self.get(key) != data:
                raise FileExistsError(
                    f"Immutable storage key already exists: {key}"
                ) from ex
            return False

    async def get(self, key: str) -> bytes:
        from botocore.exceptions import ClientError

        try:
            client = await self._client_manager.get()
            result = await client.get_object(Bucket=self.bucket, Key=self._key(key))
            async with result["Body"] as body:
                return bytes(await body.read())
        except ClientError as ex:
            status = ex.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            code = ex.response.get("Error", {}).get("Code")
            if status == 404 or code in {"NoSuchKey", "NoSuchBucket", "NotFound"}:
                raise FileNotFoundError(key) from ex
            raise

    async def delete(self, key: str) -> None:
        client = await self._client_manager.get()
        await client.delete_object(Bucket=self.bucket, Key=self._key(key))


@dataclass(frozen=True)
class ArchivePolicy:
    write: str = "versioned"
    require_current_predecessor: bool = False
    duplicate_unchanged: bool = True

    def __post_init__(self) -> None:
        if self.write not in {"create-only", "versioned", "replace-current"}:
            raise ValueError(f"Unknown archive write policy: {self.write}")


@dataclass(frozen=True)
class ArchiveTarget:
    name: str
    backend_name: str
    backend_revision: str
    backend: StorageBackend
    policy: ArchivePolicy


class ArchiveStorageRegistry:
    def __init__(
        self,
        configuration: Mapping[str, Any],
        default_root: str,
        sessions: ArchiveStorageSessions | None = None,
    ) -> None:
        sessions = sessions or ArchiveStorageSessions()
        backend_configurations = configuration.get("backends", {})
        archive_configurations = configuration.get("archives", {})
        if not backend_configurations:
            backend_configurations = {
                "filesystem": {
                    "kind": "filesystem",
                    "root": default_root,
                    "revision": "1",
                }
            }
        if not archive_configurations:
            archive_configurations = {"default": {"backend": "filesystem"}}

        self._targets: dict[str, ArchiveTarget] = {}
        self._backends: dict[tuple[str, str], StorageBackend] = {
            ("legacy-filesystem", "00008"): LegacyFilesystemStorageBackend()
        }
        backend_revisions: dict[str, str] = {}
        for backend_name, backend_configuration in backend_configurations.items():
            current_revision = backend_configuration.get("current_revision")
            revisions = backend_configuration.get("revisions")
            if current_revision is not None or revisions is not None:
                if not isinstance(current_revision, str) or not current_revision:
                    raise ValueError(
                        f"Archive backend {backend_name} requires current_revision"
                    )
                if (
                    not isinstance(revisions, Mapping)
                    or current_revision not in revisions
                ):
                    raise ValueError(
                        f"Archive backend {backend_name} current revision is unavailable"
                    )
                common = {
                    key: value
                    for key, value in backend_configuration.items()
                    if key not in {"current_revision", "revisions"}
                }
                configured_revisions = {
                    str(revision): {**common, **dict(configuration)}
                    for revision, configuration in revisions.items()
                }
            else:
                current_revision = str(backend_configuration.get("revision", "1"))
                configured_revisions = {current_revision: backend_configuration}

            backend_revisions[str(backend_name)] = current_revision
            for revision, configuration in configured_revisions.items():
                kind = configuration.get("kind")
                if kind == "filesystem":
                    backend: StorageBackend = FilesystemStorageBackend(
                        str(configuration.get("root", default_root))
                    )
                elif kind == "s3":
                    if not configuration.get("bucket"):
                        raise ValueError(f"S3 backend {backend_name} requires bucket")
                    backend = S3StorageBackend(configuration, sessions)
                else:
                    raise ValueError(f"Unknown archive storage backend kind: {kind}")
                self._backends[(str(backend_name), revision)] = backend

        for archive, archive_configuration in archive_configurations.items():
            archive = validate_archive_name(archive)
            backend_name = str(archive_configuration.get("backend"))
            if backend_name not in backend_revisions:
                raise ValueError(
                    f"Archive {archive} references unknown backend {backend_name}"
                )
            backend_revision = backend_revisions[backend_name]
            backend = self._backends[(backend_name, backend_revision)]
            policy_configuration = archive_configuration.get("policy", {})
            if "history" in policy_configuration:
                raise ValueError(
                    "Archive history policy was removed; only whole documents can be deleted"
                )
            for flag in ("require_current_predecessor", "duplicate_unchanged"):
                if flag in policy_configuration and not isinstance(
                    policy_configuration[flag], bool
                ):
                    raise ValueError(f"Archive policy {flag} must be a boolean")
            self._targets[str(archive)] = ArchiveTarget(
                name=str(archive),
                backend_name=backend_name,
                backend_revision=backend_revision,
                backend=backend,
                policy=ArchivePolicy(
                    write=policy_configuration.get("write", "versioned"),
                    require_current_predecessor=policy_configuration.get(
                        "require_current_predecessor", False
                    ),
                    duplicate_unchanged=policy_configuration.get(
                        "duplicate_unchanged", True
                    ),
                ),
            )

    def resolve(self, archive: str) -> ArchiveTarget:
        try:
            return self._targets[archive]
        except KeyError as ex:
            raise KeyError(f"Unknown archive: {archive}") from ex

    def backend(self, name: str, revision: str) -> StorageBackend:
        try:
            return self._backends[(name, revision)]
        except KeyError as ex:
            raise KeyError(
                f"Storage backend revision is not configured: {name}@{revision}"
            ) from ex
