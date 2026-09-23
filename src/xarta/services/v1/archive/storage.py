from __future__ import annotations

import asyncio
import contextlib
import os
import re
import uuid

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from typing import Protocol

import aiofiles  # type: ignore[import-untyped]
import aiofiles.os  # type: ignore[import-untyped]

from xarta.storage.s3 import S3ClientManager


class StorageBackend(Protocol):
    async def put(self, key: str, data: bytes, content_type: str) -> bool:
        """Store immutable bytes and return whether this call created them."""
        ...

    async def get(self, key: str) -> bytes: ...

    async def delete(self, key: str) -> None: ...


class FilesystemStorageBackend:
    def __init__(self, root: str) -> None:
        self.root = os.path.abspath(root)

    def _path(self, key: str) -> str:
        path = os.path.abspath(os.path.join(self.root, key))
        if os.path.commonpath((self.root, path)) != self.root:
            raise ValueError("Storage key escapes the configured root")
        return path

    async def put(self, key: str, data: bytes, content_type: str) -> bool:
        del content_type
        path = self._path(key)
        await aiofiles.os.makedirs(os.path.dirname(path), exist_ok=True)
        temporary = f"{path}.{uuid.uuid4()}.tmp"
        try:
            async with aiofiles.open(temporary, "xb") as stream:
                await stream.write(data)
                await stream.flush()
            try:
                await asyncio.to_thread(os.link, temporary, path)
                return True
            except FileExistsError as ex:
                if await self.get(key) != data:
                    raise FileExistsError(
                        f"Immutable storage key already exists: {key}"
                    ) from ex
                return False
        finally:
            with contextlib.suppress(FileNotFoundError):
                await aiofiles.os.remove(temporary)

    async def get(self, key: str) -> bytes:
        async with aiofiles.open(self._path(key), "rb") as stream:
            return bytes(await stream.read())

    async def delete(self, key: str) -> None:
        with contextlib.suppress(FileNotFoundError):
            await aiofiles.os.remove(self._path(key))


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
            self._s3[key] = S3ClientManager(dict(client_options))
        return self._s3[key]

    async def close(self) -> None:
        """Close all archive S3 clients and their HTTP connection pools."""
        await asyncio.gather(*(client.close() for client in self._s3.values()))


class S3StorageBackend:
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
            if status != 412 or await self.get(key) != data:
                raise
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
    history: str = "retain-all"
    require_current_predecessor: bool = False
    duplicate_unchanged: bool = True

    def __post_init__(self) -> None:
        if self.write not in {"create-only", "versioned", "replace-current"}:
            raise ValueError(f"Unknown archive write policy: {self.write}")
        if self.history not in {"retain-all", "retain-metadata", "latest-only"}:
            raise ValueError(f"Unknown archive history policy: {self.history}")


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
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", str(archive)):
                raise ValueError(f"Invalid archive name: {archive}")
            backend_name = str(archive_configuration.get("backend"))
            if backend_name not in backend_revisions:
                raise ValueError(
                    f"Archive {archive} references unknown backend {backend_name}"
                )
            backend_revision = backend_revisions[backend_name]
            backend = self._backends[(backend_name, backend_revision)]
            policy_configuration = archive_configuration.get("policy", {})
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
                    history=policy_configuration.get("history", "retain-all"),
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
