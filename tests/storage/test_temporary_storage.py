from __future__ import annotations

import datetime
import json
import os

from typing import TYPE_CHECKING
from typing import Any

import pytest

from botocore.exceptions import ClientError  # type: ignore[import-untyped]

from xarta.storage import FilesystemTemporaryStorage
from xarta.storage import S3TemporaryStorage
from xarta.storage import configure_temporary_storage
from xarta.storage.configuration import _reset_temporary_storage_for_tests
from xarta.storage.configuration import get_temporary_storage
from xarta.storage.configuration import initialize_temporary_storage

if TYPE_CHECKING:
    from pathlib import Path


async def test_filesystem_operations_are_immutable(tmp_path: Path) -> None:
    storage = FilesystemTemporaryStorage(str(tmp_path))

    assert await storage.put("aa/value", b"one", "text/plain") is True
    assert await storage.put("aa/value", b"one", "text/plain") is False
    assert await storage.get("aa/value") == b"one"
    with pytest.raises(FileExistsError):
        await storage.put("aa/value", b"two", "text/plain")

    await storage.delete("aa/value")
    await storage.delete("aa/value")
    with pytest.raises(FileNotFoundError):
        await storage.get("aa/value")


async def test_filesystem_cleanup_is_bounded(tmp_path: Path) -> None:
    storage = FilesystemTemporaryStorage(str(tmp_path))
    await storage.put("old/one", b"1", "text/plain")
    await storage.put("old/two", b"2", "text/plain")
    await storage.put("new/three", b"3", "text/plain")
    old = (
        datetime.datetime.now(tz=datetime.UTC) - datetime.timedelta(days=2)
    ).timestamp()
    os.utime(tmp_path / "old" / "one", (old, old))
    os.utime(tmp_path / "old" / "two", (old, old))

    cutoff = datetime.datetime.now(tz=datetime.UTC) - datetime.timedelta(days=1)
    assert await storage.cleanup_older_than(cutoff, limit=1) == 1
    assert await storage.cleanup_older_than(cutoff, limit=10) == 1
    assert await storage.get("new/three") == b"3"


def test_configuration_is_strict_and_does_not_initialize_s3() -> None:
    storage = configure_temporary_storage(
        {
            "kind": "s3",
            "bucket": "tmp",
            "endpoint_url": "http://objects:9000",
            "use_ssl": False,
            "max_pool_connections": 32,
        }
    )
    assert isinstance(storage, S3TemporaryStorage)
    assert storage.bucket == "tmp"
    assert storage.max_pool_connections == 32
    assert "endpoint_url" not in repr(storage)

    with pytest.raises(ValueError, match="Unknown temporary storage"):
        configure_temporary_storage({"kind": "filesystem", "root": "/tmp/x", "typo": 1})
    with pytest.raises(TypeError, match="use_ssl"):
        configure_temporary_storage({"kind": "s3", "bucket": "tmp", "use_ssl": "false"})
    with pytest.raises(ValueError, match="both access key"):
        configure_temporary_storage(
            {"kind": "s3", "bucket": "tmp", "aws_access_key_id": "key"}
        )
    with pytest.raises(TypeError, match="max_pool_connections"):
        configure_temporary_storage(
            {"kind": "s3", "bucket": "tmp", "max_pool_connections": True}
        )
    with pytest.raises(ValueError, match="max_pool_connections"):
        configure_temporary_storage(
            {"kind": "s3", "bucket": "tmp", "max_pool_connections": 257}
        )


def test_environment_configuration_is_cached(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _reset_temporary_storage_for_tests()
    monkeypatch.delenv("TMP_STORAGE_CONFIG_PATH", raising=False)
    monkeypatch.setenv("TMP_STORAGE", str(tmp_path / "one"))
    first = get_temporary_storage()
    monkeypatch.setenv("TMP_STORAGE", str(tmp_path / "two"))

    assert get_temporary_storage() is first
    assert isinstance(first, FilesystemTemporaryStorage)
    assert first.root == str(tmp_path / "one")
    _reset_temporary_storage_for_tests()


def test_json_configuration_path_selects_s3(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    configuration = tmp_path / "temporary-storage.json"
    configuration.write_text(
        json.dumps({"kind": "s3", "bucket": "tmp", "prefix": "documents"})
    )
    _reset_temporary_storage_for_tests()
    monkeypatch.setenv("TMP_STORAGE_CONFIG_PATH", str(configuration))

    storage = get_temporary_storage()

    assert isinstance(storage, S3TemporaryStorage)
    assert storage.prefix == "documents"
    _reset_temporary_storage_for_tests()


async def test_initialize_opens_configured_s3_client(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    configuration = tmp_path / "temporary-storage.json"
    configuration.write_text(json.dumps({"kind": "s3", "bucket": "tmp"}))
    monkeypatch.setenv("TMP_STORAGE_CONFIG_PATH", str(configuration))
    initialized = 0

    async def initialize(_self: S3TemporaryStorage) -> None:
        nonlocal initialized
        initialized += 1

    _reset_temporary_storage_for_tests()
    monkeypatch.setattr(S3TemporaryStorage, "initialize", initialize)

    await initialize_temporary_storage()

    assert initialized == 1
    _reset_temporary_storage_for_tests()


class _Body:
    def __init__(self, data: bytes) -> None:
        self.data = data

    async def __aenter__(self) -> _Body:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def read(self) -> bytes:
        return self.data


class _FakeS3:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, datetime.datetime]] = {}
        self.list_calls: list[dict[str, Any]] = []

    async def put_object(self, **request: Any) -> None:
        key = request["Key"]
        if key in self.objects:
            raise ClientError(
                {
                    "Error": {"Code": "PreconditionFailed"},
                    "ResponseMetadata": {"HTTPStatusCode": 412},
                },
                "PutObject",
            )
        self.objects[key] = (request["Body"], datetime.datetime.now(tz=datetime.UTC))

    async def get_object(self, **request: Any) -> dict[str, Any]:
        try:
            data = self.objects[request["Key"]][0]
        except KeyError as ex:
            raise ClientError(
                {
                    "Error": {"Code": "NoSuchKey"},
                    "ResponseMetadata": {"HTTPStatusCode": 404},
                },
                "GetObject",
            ) from ex
        return {"Body": _Body(data)}

    async def delete_object(self, **request: Any) -> None:
        self.objects.pop(request["Key"], None)

    async def list_objects_v2(self, **request: Any) -> dict[str, Any]:
        self.list_calls.append(request)
        keys = sorted(key for key in self.objects if key.startswith(request["Prefix"]))
        start = int(request.get("ContinuationToken", "0"))
        selected = keys[start : start + 1]
        next_offset = start + len(selected)
        return {
            "Contents": [
                {"Key": key, "LastModified": self.objects[key][1]} for key in selected
            ],
            "IsTruncated": next_offset < len(keys),
            "NextContinuationToken": (
                str(next_offset) if next_offset < len(keys) else None
            ),
        }

    async def delete_objects(self, **request: Any) -> None:
        for item in request["Delete"]["Objects"]:
            self.objects.pop(item["Key"], None)


async def test_s3_operations_missing_normalization_and_paginated_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeS3()
    storage = S3TemporaryStorage(bucket="tmp", prefix="scratch")

    async def client(_self):
        return fake

    monkeypatch.setattr(S3TemporaryStorage, "_client", client)
    assert await storage.put("aa/one", b"1", "text/plain") is True
    assert await storage.put("aa/one", b"1", "text/plain") is False
    assert await storage.get("aa/one") == b"1"
    await storage.delete("aa/one")
    with pytest.raises(FileNotFoundError):
        await storage.get("aa/one")
    with pytest.raises(FileNotFoundError):
        await storage.get("missing")

    old = datetime.datetime.now(tz=datetime.UTC) - datetime.timedelta(days=2)
    fake.objects["scratch/aa/one"] = (b"1", old)
    fake.objects["scratch/bb/two"] = (b"2", old)
    assert (
        await storage.cleanup_older_than(
            datetime.datetime.now(tz=datetime.UTC) - datetime.timedelta(days=1), limit=2
        )
        == 2
    )
    assert len(fake.list_calls) == 2
    assert all(call["Prefix"] == "scratch/" for call in fake.list_calls)
