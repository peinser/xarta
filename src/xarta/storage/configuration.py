from __future__ import annotations

import json
import os
import threading

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from xarta.storage.base import FilesystemTemporaryStorage
from xarta.storage.base import S3TemporaryStorage
from xarta.storage.base import TemporaryStorage

_FILESYSTEM_FIELDS = {"kind", "root"}
_S3_FIELDS = {
    "kind",
    "bucket",
    "prefix",
    "endpoint_url",
    "aws_access_key_id",
    "aws_secret_access_key",
    "aws_session_token",
    "region_name",
    "use_ssl",
    "verify",
    "max_pool_connections",
}
_STRING_S3_FIELDS = {
    "endpoint_url",
    "aws_access_key_id",
    "aws_secret_access_key",
    "aws_session_token",
    "region_name",
}
_backend: TemporaryStorage | None = None
_lock = threading.Lock()


def configure_temporary_storage(configuration: Mapping[str, Any]) -> TemporaryStorage:
    if not isinstance(configuration, Mapping):
        raise TypeError("Temporary storage configuration must be an object")
    kind = configuration.get("kind")
    if kind == "filesystem":
        _reject_unknown(configuration, _FILESYSTEM_FIELDS)
        root = configuration.get("root")
        if not isinstance(root, str) or not root.strip():
            raise ValueError("Filesystem temporary storage requires a non-empty root")
        return FilesystemTemporaryStorage(root)
    if kind == "s3":
        _reject_unknown(configuration, _S3_FIELDS)
        bucket = configuration.get("bucket")
        if not isinstance(bucket, str) or not bucket.strip():
            raise ValueError("S3 temporary storage requires a non-empty bucket")
        if "/" in bucket or any(character.isspace() for character in bucket):
            raise ValueError("S3 temporary storage bucket contains invalid characters")
        for field in _STRING_S3_FIELDS:
            if field in configuration and (
                not isinstance(configuration[field], str) or not configuration[field]
            ):
                raise TypeError(
                    f"S3 temporary storage {field} must be a non-empty string"
                )
        prefix = configuration.get("prefix", "")
        if not isinstance(prefix, str):
            raise TypeError("S3 temporary storage prefix must be a string")
        if "\\" in prefix or any(part in {".", ".."} for part in prefix.split("/")):
            raise ValueError("S3 temporary storage prefix contains an invalid segment")
        endpoint = configuration.get("endpoint_url")
        if endpoint:
            parsed_endpoint = urlsplit(endpoint)
            if (
                parsed_endpoint.scheme not in {"http", "https"}
                or not parsed_endpoint.netloc
            ):
                raise ValueError(
                    "S3 temporary storage endpoint_url must be an HTTP(S) URL"
                )
        if bool(configuration.get("aws_access_key_id")) != bool(
            configuration.get("aws_secret_access_key")
        ):
            raise ValueError(
                "S3 temporary storage requires both access key and secret access key"
            )
        for field in ("use_ssl",):
            if field in configuration and not isinstance(configuration[field], bool):
                raise TypeError(f"S3 temporary storage {field} must be a boolean")
        verify = configuration.get("verify", True)
        if not isinstance(verify, bool | str) or verify == "":
            raise TypeError(
                "S3 temporary storage verify must be a boolean or CA bundle path"
            )
        max_pool_connections = configuration.get("max_pool_connections", 10)
        if isinstance(max_pool_connections, bool) or not isinstance(
            max_pool_connections, int
        ):
            raise TypeError(
                "S3 temporary storage max_pool_connections must be an integer"
            )
        if not 1 <= max_pool_connections <= 256:
            raise ValueError(
                "S3 temporary storage max_pool_connections must be between 1 and 256"
            )
        options = {
            key: value
            for key, value in configuration.items()
            if key not in {"kind", "bucket", "prefix", "max_pool_connections"}
        }
        return S3TemporaryStorage(
            bucket=bucket,
            prefix=prefix,
            client_options=options,
            max_pool_connections=max_pool_connections,
        )
    raise ValueError(f"Unknown temporary storage backend kind: {kind!r}")


def get_temporary_storage() -> TemporaryStorage:
    global _backend
    if _backend is None:
        with _lock:
            if _backend is None:
                path = os.environ.get("TMP_STORAGE_CONFIG_PATH")
                if path:
                    with open(path, encoding="utf-8") as stream:
                        configuration = json.load(stream)
                else:
                    root = os.environ.get("TMP_STORAGE", "{TMP_STORAGE}")
                    configuration = {"kind": "filesystem", "root": root}
                _backend = configure_temporary_storage(configuration)
    return _backend


async def initialize_temporary_storage() -> None:
    """Initialize a backend that has an asynchronous startup step."""
    backend = get_temporary_storage()
    initialize = getattr(backend, "initialize", None)
    if initialize is not None:
        await initialize()


async def close_temporary_storage() -> None:
    """Close and clear the process-wide temporary-storage backend at shutdown."""
    global _backend
    backend = _backend
    _backend = None
    close = getattr(backend, "close", None)
    if close is not None:
        await close()


def _reject_unknown(configuration: Mapping[str, Any], allowed: set[str]) -> None:
    unknown = set(configuration) - allowed
    if unknown:
        raise ValueError(
            "Unknown temporary storage configuration field(s): "
            + ", ".join(sorted(unknown))
        )


def _reset_temporary_storage_for_tests() -> None:
    global _backend
    with _lock:
        _backend = None
