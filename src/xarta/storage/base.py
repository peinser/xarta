from __future__ import annotations

import asyncio
import contextlib
import os
import uuid

from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING
from typing import Any
from typing import Protocol

import aiofiles  # type: ignore[import-untyped]
import aiofiles.os  # type: ignore[import-untyped]

from xarta.storage.s3 import S3ClientManager

if TYPE_CHECKING:
    import datetime

    from collections.abc import Mapping


class TemporaryStorage(Protocol):
    async def put(self, key: str, data: bytes, content_type: str) -> bool:
        """Store immutable bytes and return whether this call created them."""
        ...

    async def get(self, key: str) -> bytes: ...

    async def delete(self, key: str) -> None: ...

    async def cleanup_older_than(
        self, cutoff: datetime.datetime, *, limit: int = 10_000
    ) -> int: ...


def _validate_key(key: str) -> str:
    if not key or key.startswith(("/", "\\")) or "\\" in key:
        raise ValueError("Storage key must be a non-empty relative POSIX path")
    parts = key.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("Storage key contains an invalid path segment")
    return key


@dataclass(frozen=True, slots=True)
class FilesystemTemporaryStorage:
    root: str

    def __post_init__(self) -> None:
        root = os.path.abspath(os.path.expanduser(self.root))
        if root == os.path.abspath(os.sep):
            raise ValueError("Temporary storage cannot use the filesystem root")
        object.__setattr__(self, "root", root)

    def _path(self, key: str) -> str:
        return os.path.join(self.root, *_validate_key(key).split("/"))

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
                        f"Immutable temporary storage key already exists: {key}"
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

    async def cleanup_older_than(
        self, cutoff: datetime.datetime, *, limit: int = 10_000
    ) -> int:
        _validate_cleanup(cutoff, limit)
        return await asyncio.to_thread(self._cleanup, cutoff.timestamp(), limit)

    def _cleanup(self, cutoff: float, limit: int) -> int:
        root = Path(self.root)
        if not root.exists():
            return 0
        removed = 0
        for directory, _, files in os.walk(root, followlinks=False):
            for name in files:
                if removed >= limit:
                    return removed
                path = Path(directory, name)
                if not path.is_symlink() and path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
        return removed


@dataclass(frozen=True, slots=True)
class S3TemporaryStorage:
    bucket: str
    prefix: str = ""
    max_pool_connections: int = 10
    client_options: Mapping[str, Any] = field(
        default_factory=lambda: MappingProxyType({}), repr=False
    )
    _client_manager: S3ClientManager = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "prefix", self.prefix.strip("/"))
        object.__setattr__(
            self, "client_options", MappingProxyType(dict(self.client_options))
        )
        object.__setattr__(
            self,
            "_client_manager",
            S3ClientManager(
                dict(self.client_options),
                max_pool_connections=self.max_pool_connections,
            ),
        )

    def _key(self, key: str) -> str:
        key = _validate_key(key)
        return f"{self.prefix}/{key}" if self.prefix else key

    async def _client(self):
        return await self._client_manager.get()

    async def initialize(self) -> None:
        """Open the process-wide S3 client before serving storage requests."""
        await self._client()

    async def put(self, key: str, data: bytes, content_type: str) -> bool:
        from botocore.exceptions import ClientError  # type: ignore[import-untyped]

        try:
            client = await self._client()
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
            code = ex.response.get("Error", {}).get("Code")
            if (
                status != 412 and code not in {"PreconditionFailed", "412"}
            ) or await self.get(key) != data:
                raise
            return False

    async def get(self, key: str) -> bytes:
        from botocore.exceptions import ClientError

        try:
            client = await self._client()
            result = await client.get_object(Bucket=self.bucket, Key=self._key(key))
            async with result["Body"] as body:
                return bytes(await body.read())
        except ClientError as ex:
            status = ex.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            code = ex.response.get("Error", {}).get("Code")
            if status == 404 or code in {
                "NoSuchKey",
                "NoSuchBucket",
                "NotFound",
                "404",
            }:
                raise FileNotFoundError(key) from ex
            raise

    async def delete(self, key: str) -> None:
        client = await self._client()
        await client.delete_object(Bucket=self.bucket, Key=self._key(key))

    async def cleanup_older_than(
        self, cutoff: datetime.datetime, *, limit: int = 10_000
    ) -> int:
        _validate_cleanup(cutoff, limit)
        prefix = f"{self.prefix}/" if self.prefix else ""
        continuation_token: str | None = None
        inspected = 0
        expired: list[dict[str, str]] = []
        client = await self._client()
        while inspected < limit:
            request: dict[str, Any] = {
                "Bucket": self.bucket,
                "Prefix": prefix,
                "MaxKeys": min(1000, limit - inspected),
            }
            if continuation_token:
                request["ContinuationToken"] = continuation_token
            response = await client.list_objects_v2(**request)
            contents = response.get("Contents", ())
            inspected += len(contents)
            expired.extend(
                {"Key": item["Key"]}
                for item in contents
                if item["LastModified"] < cutoff
            )
            continuation_token = response.get("NextContinuationToken")
            if not response.get("IsTruncated") or not continuation_token:
                break
        for offset in range(0, len(expired), 1000):
            await client.delete_objects(
                Bucket=self.bucket,
                Delete={
                    "Objects": expired[offset : offset + 1000],
                    "Quiet": True,
                },
            )
        return len(expired)

    async def close(self) -> None:
        await self._client_manager.close()


def _validate_cleanup(cutoff: datetime.datetime, limit: int) -> None:
    if cutoff.tzinfo is None:
        raise ValueError("Cleanup cutoff must be timezone-aware")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("Cleanup limit must be a positive integer")
