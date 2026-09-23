from __future__ import annotations

import asyncio
import posixpath

from dataclasses import dataclass
from math import isfinite
from pathlib import PurePosixPath
from typing import TYPE_CHECKING
from typing import Protocol

import asyncssh

from asyncssh import sftp as sftp_status

if TYPE_CHECKING:
    from collections.abc import Awaitable
    from collections.abc import Callable
    from collections.abc import Mapping
    from typing import Any

    from xarta.protocol.document.source import DocumentSourceResult


@dataclass(frozen=True)
class SFTPSubmission:
    outcome: str
    provider_reference: str | None


class SFTPTransportError(Exception):
    pass


class SFTPCommitUncertainError(Exception):
    pass


class SFTPAdapter(Protocol):
    async def upload(
        self,
        *,
        idempotency_key: str,
        document: DocumentSourceResult,
        relative_path: str,
        before_commit: Callable[[], Awaitable[None]],
    ) -> SFTPSubmission: ...


def _positive_finite_timeout(
    configuration: Mapping[str, Any], name: str, default: float
) -> float:
    value = configuration.get(name, default)
    if isinstance(value, bool):
        raise ValueError(f"SFTP destination {name} must be a positive finite number")
    try:
        timeout = float(value)
    except (TypeError, ValueError) as ex:
        raise ValueError(
            f"SFTP destination {name} must be a positive finite number"
        ) from ex
    if not isfinite(timeout) or timeout <= 0:
        raise ValueError(f"SFTP destination {name} must be a positive finite number")
    return timeout


def _validate_configuration(configuration: Mapping[str, Any]) -> None:
    for name in ("host", "username", "known_hosts"):
        if not isinstance(configuration.get(name), str) or not configuration[name]:
            raise ValueError(f"SFTP destination requires a non-empty {name}")

    password = configuration.get("password")
    client_keys = configuration.get("client_keys")
    has_password = isinstance(password, str) and bool(password)
    has_client_keys = (isinstance(client_keys, str) and bool(client_keys)) or (
        isinstance(client_keys, list | tuple)
        and bool(client_keys)
        and all(isinstance(key, str) and key for key in client_keys)
    )
    if bool(password) and not has_password:
        raise ValueError("SFTP destination password must be a non-empty string")
    if bool(client_keys) and not has_client_keys:
        raise ValueError(
            "SFTP destination client_keys must contain non-empty key paths"
        )
    if has_password == has_client_keys:
        raise ValueError("SFTP destination requires exactly one authentication method")

    port = configuration.get("port", 22)
    if isinstance(port, bool):
        raise ValueError("SFTP destination port must be an integer between 1 and 65535")
    try:
        parsed_port = int(port)
    except (TypeError, ValueError) as ex:
        raise ValueError(
            "SFTP destination port must be an integer between 1 and 65535"
        ) from ex
    if str(parsed_port) != str(port) or not 1 <= parsed_port <= 65535:
        raise ValueError("SFTP destination port must be an integer between 1 and 65535")

    base_path = configuration.get("base_path", "/")
    if not isinstance(base_path, str) or not PurePosixPath(base_path).is_absolute():
        raise ValueError("SFTP destination base_path must be absolute")
    _positive_finite_timeout(configuration, "connect_timeout", 10)
    _positive_finite_timeout(configuration, "operation_timeout", 60)

    for name in ("create_directories", "overwrite"):
        value = configuration.get(name, False)
        if not isinstance(value, bool):
            raise ValueError(f"SFTP destination {name} must be a boolean")


class AsyncSSHSFTPAdapter:
    def __init__(self, configuration: Mapping[str, Any]) -> None:
        _validate_configuration(configuration)
        self.host = configuration["host"]
        self.port = int(configuration.get("port", 22))
        self.username = configuration["username"]
        self.known_hosts = configuration["known_hosts"]
        self.password = configuration.get("password")
        self.client_keys = configuration.get("client_keys")
        self.base_path = configuration.get("base_path", "/")
        self.connect_timeout = float(configuration.get("connect_timeout", 10))
        self.operation_timeout = float(configuration.get("operation_timeout", 60))
        self.create_directories = configuration.get("create_directories", False)
        self.overwrite = configuration.get("overwrite", False)

    async def upload(
        self,
        *,
        idempotency_key: str,
        document: DocumentSourceResult,
        relative_path: str,
        before_commit: Callable[[], Awaitable[None]],
    ) -> SFTPSubmission:
        final_path = posixpath.join(self.base_path, relative_path)
        parent = posixpath.dirname(final_path)
        filename = posixpath.basename(final_path)
        staging_path = posixpath.join(parent, f".{filename}.xarta-{idempotency_key}")
        commit_started = False

        try:
            async with asyncssh.connect(
                self.host,
                port=self.port,
                username=self.username,
                password=self.password,
                client_keys=self.client_keys,
                known_hosts=self.known_hosts,
                connect_timeout=self.connect_timeout,
            ) as connection:
                async with asyncio.timeout(self.operation_timeout):
                    async with connection.start_sftp_client() as client:
                        if (
                            await self._exists(client, final_path)
                            and not self.overwrite
                        ):
                            return SFTPSubmission("path_conflict", final_path)
                        if self.create_directories:
                            await client.makedirs(parent, exist_ok=True)
                        async with client.open(staging_path, "wb") as target:
                            await target.write(document.data)
                        attributes = await client.stat(staging_path)
                        if attributes.size != len(document.data):
                            raise SFTPTransportError("SFTP staging size mismatch")
                        await before_commit()
                        commit_started = True
                        try:
                            if self.overwrite:
                                await client.posix_rename(staging_path, final_path)
                            else:
                                await client.rename(staging_path, final_path)
                        except (
                            asyncio.CancelledError,
                            TimeoutError,
                            asyncssh.ConnectionLost,
                            asyncssh.DisconnectError,
                            asyncssh.SFTPConnectionLost,
                            asyncssh.SFTPNoConnection,
                        ) as ex:
                            raise SFTPCommitUncertainError from ex
                        return SFTPSubmission("uploaded", final_path)
        except (SFTPCommitUncertainError, SFTPTransportError):
            raise
        except asyncio.CancelledError as ex:
            if commit_started:
                raise SFTPCommitUncertainError from ex
            raise
        except asyncssh.SFTPFileAlreadyExists:
            return SFTPSubmission("path_conflict", final_path)
        except asyncssh.SFTPError as ex:
            if ex.code in {
                sftp_status.FX_PERMISSION_DENIED,
                sftp_status.FX_WRITE_PROTECT,
                sftp_status.FX_INVALID_FILENAME,
                sftp_status.FX_NOT_A_DIRECTORY,
                sftp_status.FX_NO_SUCH_PATH,
                sftp_status.FX_OP_UNSUPPORTED,
            }:
                return SFTPSubmission("upload_rejected", None)
            raise SFTPTransportError from ex
        except (
            TimeoutError,
            OSError,
            asyncssh.ConnectionLost,
            asyncssh.DisconnectError,
        ) as ex:
            if commit_started:
                raise SFTPCommitUncertainError from ex
            raise SFTPTransportError from ex

    @staticmethod
    async def _exists(client, path: str) -> bool:
        try:
            await client.stat(path)
        except (asyncssh.SFTPNoSuchFile, asyncssh.SFTPNoSuchPath):
            return False
        return True


class AsyncSSHSFTPAdapterFactory:
    def validate(self, configuration: Mapping[str, Any]) -> None:
        _validate_configuration(configuration)

    def create(self, configuration: Mapping[str, Any]) -> SFTPAdapter:
        return AsyncSSHSFTPAdapter(configuration)
