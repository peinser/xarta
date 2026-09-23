from __future__ import annotations

import aiobotocore.session  # type: ignore[import-untyped]
import pytest

from xarta.storage.s3 import S3ClientManager


@pytest.mark.asyncio
async def test_s3_client_manager_reuses_and_closes_one_client(monkeypatch) -> None:
    client = object()
    entered = 0
    exited = 0

    class Context:
        async def __aenter__(self):
            nonlocal entered
            entered += 1
            return client

        async def __aexit__(self, *_args):
            nonlocal exited
            exited += 1

    class Session:
        def create_client(self, service: str, **options):
            assert service == "s3"
            assert options["region_name"] == "us-east-1"
            assert options["config"].max_pool_connections == 32
            return Context()

    monkeypatch.setattr(aiobotocore.session, "get_session", Session)
    manager = S3ClientManager({"region_name": "us-east-1"}, max_pool_connections=32)

    assert await manager.get() is client
    assert await manager.get() is client
    assert entered == 1

    await manager.close()
    assert exited == 1

    assert await manager.get() is client
    assert entered == 2
    await manager.close()
