from __future__ import annotations

import asyncio

from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock
from uuid import UUID
from uuid import uuid4

import asyncssh
import orjson
import pytest

from xarta import env
from xarta.adapters import AdapterRegistry
from xarta.protocol.dag import Node
from xarta.protocol.dag import NodeTask
from xarta.protocol.dag.sftp import SFTPNode
from xarta.protocol.document.source import DocumentSourceResult
from xarta.services.v1.sftp.adapters import AsyncSSHSFTPAdapter
from xarta.services.v1.sftp.adapters import AsyncSSHSFTPAdapterFactory
from xarta.services.v1.sftp.adapters import SFTPCommitUncertainError
from xarta.services.v1.sftp.adapters import SFTPSubmission
from xarta.services.v1.sftp.adapters import SFTPTransportError
from xarta.services.v1.sftp.base import _setup_nats
from xarta.services.v1.sftp.nats import SFTPNATSModel
from xarta.services.v1.sftp.nats import _worker
from xarta.services.v1.sftp.nats import build_sftp_components

EXAMPLES = Path(__file__).parents[2] / "examples" / "protocol"


def configuration() -> dict:
    return {
        "kind": "sftp",
        "adapter": "asyncssh-sftp",
        "revision": "test-v1",
        "host": "sftp.example.test",
        "username": "xarta",
        "password": "test-password",
        "known_hosts": "/tmp/test-known-hosts",
        "base_path": "/incoming",
    }


class FakeAdapterFactory:
    def __init__(self, outcome: str = "uploaded") -> None:
        self.outcome = outcome

    def validate(self, configuration) -> None:
        if not configuration.get("token"):
            raise ValueError("fake-sftp requires token")

    def create(self, configuration):
        outcome = self.outcome

        class FakeAdapter:
            async def upload(self, *, before_commit, relative_path, **kwargs):
                if outcome in {"uploaded", "transport_after_commit"}:
                    await before_commit()
                if outcome == "transport_after_commit":
                    raise SFTPCommitUncertainError
                return SFTPSubmission(outcome, f"/incoming/{relative_path}")

        return FakeAdapter()


def fake_components(outcome: str = "uploaded"):
    configured = {**configuration(), "adapter": "fake-sftp", "token": "test"}
    registry = AdapterRegistry({"fake-sftp": FakeAdapterFactory(outcome)})
    return build_sftp_components(
        {
            "partner-sftp": {
                "current_revision": "v1",
                "revisions": {"v1": configured},
            }
        },
        registry,
    )


def test_sftp_adapter_requires_host_verification_and_one_authentication_method() -> (
    None
):
    missing_hosts = configuration()
    del missing_hosts["known_hosts"]
    with pytest.raises(ValueError, match="known_hosts"):
        AsyncSSHSFTPAdapter(missing_hosts)

    conflicting_auth = configuration()
    conflicting_auth["client_keys"] = ["id_ed25519"]
    with pytest.raises(ValueError, match="exactly one"):
        AsyncSSHSFTPAdapter(conflicting_auth)


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan"), True])
def test_sftp_adapter_requires_positive_finite_timeouts(timeout) -> None:
    configured = configuration()
    configured["operation_timeout"] = timeout

    with pytest.raises(ValueError, match="positive finite"):
        AsyncSSHSFTPAdapterFactory().validate(configured)


def test_sftp_components_validate_retained_revisions() -> None:
    configured = configuration()
    historical = {**configured, "adapter": "removed-sftp"}
    destinations = {
        "partner-sftp": {
            "kind": "sftp",
            "adapter": "asyncssh-sftp",
            "current_revision": "v2",
            "revisions": {"v1": historical, "v2": configured},
        }
    }

    with pytest.raises(ValueError, match="Unknown adapter 'removed-sftp'"):
        build_sftp_components(destinations)


def test_sftp_components_validate_malformed_historical_revision() -> None:
    configured = configuration()
    historical = dict(configured)
    del historical["known_hosts"]
    destinations = {
        "partner-sftp": {
            "kind": "sftp",
            "adapter": "asyncssh-sftp",
            "current_revision": "v2",
            "revisions": {"v1": historical, "v2": configured},
        }
    }

    with pytest.raises(ValueError, match="known_hosts"):
        build_sftp_components(destinations)


def test_sftp_components_require_explicit_revisions() -> None:
    with pytest.raises(ValueError, match="explicit current_revision and revisions"):
        build_sftp_components({"partner-sftp": configuration()})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "historical",
    [
        {**configuration(), "adapter": "missing"},
        {key: value for key, value in configuration().items() if key != "known_hosts"},
    ],
    ids=["unknown-adapter", "malformed-configuration"],
)
async def test_sftp_startup_translates_invalid_components_before_nats(
    monkeypatch,
    historical,
) -> None:
    invalid = {
        "partner-sftp": {
            "kind": "sftp",
            "adapter": "asyncssh-sftp",
            "current_revision": "v2",
            "revisions": {"v1": historical, "v2": configuration()},
        }
    }
    register = AsyncMock()
    monkeypatch.setattr(
        "xarta.services.v1.sftp.base.utils.load_sftp_configurations",
        AsyncMock(return_value=invalid),
    )
    monkeypatch.setattr(SFTPNATSModel, "register", register)

    with pytest.raises(env.ConfigurationError, match="Unknown adapter|known_hosts"):
        await _setup_nats(SimpleNamespace(ctx=SimpleNamespace()))

    register.assert_not_awaited()


@pytest.mark.asyncio
async def test_sftp_adapter_stages_verifies_and_renames(monkeypatch) -> None:
    written = bytearray()
    renamed = []

    class Target:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def write(self, value):
            written.extend(value)

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def stat(self, path):
            if ".xarta-" not in path:
                import asyncssh

                raise asyncssh.SFTPNoSuchFile("missing")
            return SimpleNamespace(size=len(written))

        def open(self, *_args):
            return Target()

        async def rename(self, source, destination):
            renamed.append((source, destination))

    class Connection:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def start_sftp_client(self):
            return Client()

    monkeypatch.setattr("asyncssh.connect", lambda *args, **kwargs: Connection())
    before_commit = AsyncMock()
    adapter = AsyncSSHSFTPAdapter(configuration())
    document = SimpleNamespace(data=b"invoice")

    submission = await adapter.upload(
        idempotency_key="operation-1",
        document=cast("DocumentSourceResult", document),
        relative_path="invoices/invoice.pdf",
        before_commit=before_commit,
    )

    assert written == b"invoice"
    before_commit.assert_awaited_once_with()
    assert renamed == [
        (
            "/incoming/invoices/.invoice.pdf.xarta-operation-1",
            "/incoming/invoices/invoice.pdf",
        )
    ]
    assert submission == SFTPSubmission("uploaded", "/incoming/invoices/invoice.pdf")


@pytest.mark.asyncio
async def test_sftp_adapter_maps_operation_timeout_to_transport_error(
    monkeypatch,
) -> None:
    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def stat(self, _path):
            import asyncio

            await asyncio.sleep(1)

    class Connection:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def start_sftp_client(self):
            return Client()

    monkeypatch.setattr("asyncssh.connect", lambda *args, **kwargs: Connection())
    adapter = AsyncSSHSFTPAdapter({**configuration(), "operation_timeout": 0.01})

    with pytest.raises(SFTPTransportError):
        await adapter.upload(
            idempotency_key="operation-1",
            document=cast("DocumentSourceResult", SimpleNamespace(data=b"invoice")),
            relative_path="invoice.pdf",
            before_commit=AsyncMock(),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        TimeoutError("rename timed out"),
        asyncio.CancelledError(),
        asyncssh.ConnectionLost("connection lost"),
    ],
    ids=["timeout", "cancellation", "connection-loss"],
)
async def test_sftp_adapter_maps_post_boundary_failures_to_uncertain(
    monkeypatch, failure
) -> None:
    class Target:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def write(self, _value):
            pass

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def stat(self, path):
            if ".xarta-" not in path:
                raise asyncssh.SFTPNoSuchFile("missing")
            return SimpleNamespace(size=7)

        def open(self, *_args):
            return Target()

        async def rename(self, *_args):
            raise failure

    class Connection:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def start_sftp_client(self):
            return Client()

    monkeypatch.setattr("asyncssh.connect", lambda *args, **kwargs: Connection())

    with pytest.raises(SFTPCommitUncertainError):
        await AsyncSSHSFTPAdapter(configuration()).upload(
            idempotency_key="operation-1",
            document=cast("DocumentSourceResult", SimpleNamespace(data=b"invoice")),
            relative_path="invoice.pdf",
            before_commit=AsyncMock(),
        )


@pytest.mark.asyncio
async def test_sftp_worker_returns_uploaded_outcome(monkeypatch) -> None:
    expected = orjson.loads((EXAMPLES / "sftp-upload-events.json").read_bytes())[
        "steps"
    ][0]
    document_id = UUID("93000000-0000-0000-0000-000000000001")
    successor = Node(kind="debug", id=UUID(expected["expected_scheduled_node_ids"][0]))
    node = SFTPNode(
        document={"source": "generate", "id": str(document_id)},
        path="invoices/invoice.pdf",
        destination="partner-sftp",
        on={"uploaded": [successor]},
    )
    task = NodeTask(flow_id=uuid4(), node=node)
    document = SimpleNamespace(id=document_id, data=b"invoice")
    source = SimpleNamespace(retrieve=AsyncMock(return_value=document))
    monkeypatch.setattr(SFTPNode, "interpret", lambda self: (source, self.path))

    result = await _worker(node, task, components=fake_components())

    assert result.outcomes_persisted is False
    assert [outcome.outcome for outcome in result.outcomes] == [
        value["outcome"] for value in expected["expected_outcomes"]
    ]
    assert result.outcomes[0].subject is not None
    assert (
        result.outcomes[0].subject.dict() == expected["expected_outcomes"][0]["subject"]
    )


@pytest.mark.asyncio
async def test_sftp_worker_routes_path_conflict(monkeypatch) -> None:
    document_id = uuid4()
    node = SFTPNode(
        document={"source": "generate", "id": str(document_id)},
        path="invoice.pdf",
        destination="partner-sftp",
    )
    task = NodeTask(flow_id=uuid4(), node=node)
    source = SimpleNamespace(
        retrieve=AsyncMock(return_value=SimpleNamespace(id=document_id, data=b"data"))
    )
    monkeypatch.setattr(SFTPNode, "interpret", lambda self: (source, self.path))

    result = await _worker(node, task, components=fake_components("path_conflict"))

    assert [outcome.outcome for outcome in result.outcomes] == ["path_conflict"]


@pytest.mark.asyncio
async def test_sftp_worker_returns_uncertain_after_commit_boundary(
    monkeypatch,
) -> None:
    document_id = uuid4()
    node = SFTPNode(
        document={"source": "generate", "id": str(document_id)},
        path="invoice.pdf",
        destination="partner-sftp",
    )
    task = NodeTask(flow_id=uuid4(), node=node)
    source = SimpleNamespace(
        retrieve=AsyncMock(return_value=SimpleNamespace(id=document_id, data=b"data"))
    )
    monkeypatch.setattr(SFTPNode, "interpret", lambda self: (source, self.path))
    result = await _worker(
        node, task, components=fake_components("transport_after_commit")
    )

    assert [outcome.outcome for outcome in result.outcomes] == ["outcome_uncertain"]
