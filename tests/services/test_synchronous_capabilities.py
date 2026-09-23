from __future__ import annotations

from types import SimpleNamespace

import pytest

from xarta.nats.sanic import SanicNATSSynchronousRequestsConsumerModel
from xarta.protocol.dag.generate import GenerateNode
from xarta.protocol.dag.signature import SignatureNode
from xarta.protocol.dag.webhook import WebhookNode
from xarta.services.v1.archive.nats import ArchiveNATSModel
from xarta.services.v1.archive.nats import WaitForArchiveNATSModel
from xarta.services.v1.bundle.nats import BundleDAGNATSModel
from xarta.services.v1.doccle.nats import DoccleNATSModel
from xarta.services.v1.generate.nats import GenerateNATSModel
from xarta.services.v1.generate.nats import _worker as generate
from xarta.services.v1.search.nats import SearchNATSModel
from xarta.services.v1.sftp.nats import SFTPNATSModel
from xarta.services.v1.signature.nats import SignatureNATSModel
from xarta.services.v1.signature.nats import _worker as sign
from xarta.services.v1.transform.nats import TransformNATSModel
from xarta.services.v1.webhook.nats import WebhookNATSModel
from xarta.services.v1.webhook.nats import _worker as webhook


@pytest.mark.parametrize(
    "model",
    [
        ArchiveNATSModel,
        BundleDAGNATSModel,
        DoccleNATSModel,
        GenerateNATSModel,
        SearchNATSModel,
        SFTPNATSModel,
        SignatureNATSModel,
        TransformNATSModel,
        WaitForArchiveNATSModel,
        WebhookNATSModel,
    ],
)
def test_synchronous_services_use_jetstream_only_consumer(model) -> None:
    assert issubclass(model, SanicNATSSynchronousRequestsConsumerModel)


@pytest.mark.asyncio
async def test_generate_emits_success_without_selecting_an_edge() -> None:
    result = await generate(GenerateNode(documents=[]))

    assert [outcome.outcome for outcome in result.outcomes] == ["success"]


@pytest.mark.asyncio
async def test_signature_emits_success_without_selecting_an_edge() -> None:
    result = await sign(SignatureNode(documents=[]))

    assert [outcome.outcome for outcome in result.outcomes] == ["success"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("ok", "expected"), [(True, "success"), (False, "failure")])
async def test_webhook_maps_response_category_to_outcome(
    monkeypatch, ok: bool, expected: str
) -> None:
    node = WebhookNode(url="https://example.test")

    class ResponseContext:
        async def __aenter__(self):
            return SimpleNamespace(ok=ok)

        async def __aexit__(self, *_args):
            return None

    async def interpret(*_args, **_kwargs):
        return ResponseContext()

    monkeypatch.setattr(node, "interpret", interpret)

    result = await webhook(node=node, http_session=SimpleNamespace())

    assert [outcome.outcome for outcome in result.outcomes] == [expected]
