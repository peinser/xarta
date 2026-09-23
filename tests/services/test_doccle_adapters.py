from __future__ import annotations

import datetime
import json

from typing import Any

import pytest

from lxml import etree  # type: ignore[import-untyped]

from xarta.services.v1.doccle.adapters import DOCUMENT_NAMESPACE
from xarta.services.v1.doccle.adapters import RECEIVER_NAMESPACE
from xarta.services.v1.doccle.adapters import REST_NAMESPACE
from xarta.services.v1.doccle.adapters import DoccleResponseError
from xarta.services.v1.doccle.adapters import DoccleSenderRESTAdapter
from xarta.services.v1.doccle.adapters import DoccleTransportError
from xarta.services.v1.doccle.adapters import build_doccle_document_request
from xarta.services.v1.doccle.adapters import build_doccle_receiver_request
from xarta.services.v1.doccle.adapters import classify_doccle_response
from xarta.services.v1.doccle.adapters import parse_document_response
from xarta.services.v1.doccle.configuration import validate_doccle_destinations
from xarta.services.v1.doccle.models import DoccleDocument
from xarta.services.v1.doccle.models import DoccleReceiverProfile
from xarta.services.v1.doccle.models import DoccleResultCategory
from xarta.services.v1.doccle.models import DoccleTransportCategory


def configuration(**overrides) -> dict:
    value = {
        "adapter": "doccle-sender-rest",
        "sender_name": "sender/name",
        "endpoint": "https://webservice.doccle.be/mci-rest-app/rest/mci/external",
        "timeout": 5,
        "credentials": {
            "token_url": "https://id.example.test/oauth/token",
            "client_id": "client-id",
            "client_secret": "client-secret",
        },
        "document_types": {"invoice": "INVOICE"},
        "max_response_bytes": 1024,
    }
    value.update(overrides)
    return value


def profile() -> DoccleReceiverProfile:
    return DoccleReceiverProfile(
        label="Customer <&>",
        first_name="Ada",
        last_name="Lovelace",
        email="a&b@test",
        language="EN",
    )


def document(document_type: str = "invoice") -> DoccleDocument:
    return DoccleDocument(
        document_id="document/1",
        document_type=document_type,
        filename="invoice <final>.pdf",
        content_type="application/pdf",
        content=b"\x00PDF&",
        names={"fr": "Facture", "en": "A & B"},
        published_at=datetime.datetime(2026, 8, 28, 10, tzinfo=datetime.UTC),
    )


class Content:
    def __init__(self, body: bytes) -> None:
        self.body = body

    async def read(self, amount: int) -> bytes:
        result, self.body = self.body[:amount], self.body[amount:]
        return result


class Response:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self.content = Content(body)
        self.content_length = len(body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


class Session:
    def __init__(self, result: Response | BaseException) -> None:
        self.result = result
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def _request(self, method, *args, **kwargs):
        if args[0] == "https://id.example.test/oauth/token":
            return Response(
                200,
                json.dumps(
                    {"access_token": "secret-token", "expires_in": 300}
                ).encode(),
            )
        self.calls.append((method, args, kwargs))
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result

    def put(self, *args, **kwargs):
        return self._request("PUT", *args, **kwargs)

    def post(self, *args, **kwargs):
        return self._request("POST", *args, **kwargs)


def test_receiver_request_matches_official_create_update_receiver_schema() -> None:
    payload = build_doccle_receiver_request("receiver-1", profile())
    root = etree.fromstring(payload)

    assert root.tag == f"{{{REST_NAMESPACE}}}createUpdateReceiver"
    receiver = root.find(f"{{{REST_NAMESPACE}}}receiver")
    assert receiver is not None
    assert receiver.findtext(f"{{{RECEIVER_NAMESPACE}}}id") == "receiver-1"
    assert receiver.findtext(f"{{{RECEIVER_NAMESPACE}}}action") == "Store"
    assert (
        receiver.findtext(
            f"{{{RECEIVER_NAMESPACE}}}labels/{{{RECEIVER_NAMESPACE}}}label-default"
        )
        == "Customer <&>"
    )
    assert b"a&amp;b@test" in payload


def test_put_document_request_matches_official_schema_and_base64() -> None:
    payload = build_doccle_document_request("receiver-1", document(), "INVOICE")
    root = etree.fromstring(payload)

    assert root.tag == f"{{{REST_NAMESPACE}}}putDocument"
    provider_document = root.find(f"{{{REST_NAMESPACE}}}document")
    assert provider_document is not None
    assert provider_document.findtext(f"{{{DOCUMENT_NAMESPACE}}}id") == "document/1"
    assert (
        provider_document.findtext(
            f"{{{DOCUMENT_NAMESPACE}}}receiver/{{{DOCUMENT_NAMESPACE}}}receiver-id"
        )
        == "receiver-1"
    )
    assert (
        provider_document.findtext(f"{{{DOCUMENT_NAMESPACE}}}sender-document-type")
        == "INVOICE"
    )
    assert root.findtext(f"{{{REST_NAMESPACE}}}newDocumentVersion") == "AFBERiY="
    entries = provider_document.findall(
        f"{{{DOCUMENT_NAMESPACE}}}document-display/"
        f"{{{DOCUMENT_NAMESPACE}}}name/{{{DOCUMENT_NAMESPACE}}}entry"
    )
    assert [(entry.get("lang"), entry.text) for entry in entries] == [
        ("en", "A & B"),
        ("fr", "Facture"),
    ]


@pytest.mark.parametrize(
    "declaration",
    [
        b'<!DOCTYPE documentUri SYSTEM "https://example.test/value.dtd">',
        b'<!DOCTYPE documentUri [<!ENTITY value "accepted">]>',
    ],
)
def test_response_parser_rejects_dtds_and_entities(declaration: bytes) -> None:
    with pytest.raises(DoccleResponseError, match="DTDs or entities"):
        parse_document_response(
            declaration + b"<documentUri><uri>safe://value</uri></documentUri>",
            max_response_bytes=1024,
        )


@pytest.mark.parametrize(
    ("http_status", "operation", "body", "expected"),
    [
        (201, "receiver", b"", DoccleResultCategory.PROVISIONED),
        (
            200,
            "document",
            b"<documentUri><uri>safe://receiver@sender/document_1</uri></documentUri>",
            DoccleResultCategory.STORED,
        ),
        (403, "document", b"", DoccleResultCategory.AUTHENTICATION_FAILURE),
        (404, "document", b"", DoccleResultCategory.RECEIVER_NOT_FOUND),
        (409, "receiver", b"", DoccleResultCategory.BUSINESS_REJECTION),
        (
            500,
            "document",
            b"<error><code>exc000</code></error>",
            DoccleResultCategory.PROTOCOL_ERROR,
        ),
    ],
)
def test_documented_response_classification(
    http_status: int, operation: str, body: bytes, expected: DoccleResultCategory
) -> None:
    result = classify_doccle_response(
        http_status, body, max_response_bytes=1024, operation=operation
    )
    assert result.category is expected


def test_revisioned_configuration_validates_every_revision() -> None:
    destinations: dict[str, Any] = {
        "current_sender": "billing",
        "senders": {
            "billing": {
                "current_revision": "v2",
                "revisions": {
                    "v1": configuration(timeout=2),
                    "v2": configuration(timeout=3),
                },
            }
        },
    }
    assert validate_doccle_destinations(destinations) is destinations
    destinations["senders"]["billing"]["revisions"]["v1"][
        "endpoint"
    ] = "http://insecure"
    with pytest.raises(ValueError, match="HTTPS"):
        validate_doccle_destinations(destinations)


def test_document_path_requires_stable_document_identity() -> None:
    with pytest.raises(ValueError, match="document_id"):
        validate_doccle_destinations(
            {
                "current_sender": "billing",
                "senders": {
                    "billing": {
                        "current_revision": "v1",
                        "revisions": {
                            "v1": configuration(
                                document_path="/senders/{sender_name}/receivers/{receiver_id}/documents"
                            )
                        },
                    }
                },
            }
        )


@pytest.mark.asyncio
async def test_adapter_uses_actual_paths_bearer_auth_and_independent_escaping() -> None:
    session = Session(Response(201, b""))
    adapter = DoccleSenderRESTAdapter(configuration(), session)  # type: ignore[arg-type]

    provisioned = await adapter.create_or_update_receiver(
        receiver_id="receiver/id", profile=profile()
    )
    session.result = Response(
        200,
        b"<documentUri><uri>safe://receiver@sender/document_1</uri></documentUri>",
    )
    stored = await adapter.put_document(receiver_id="receiver/id", document=document())

    assert provisioned.category is DoccleResultCategory.PROVISIONED
    assert stored.category is DoccleResultCategory.STORED
    assert [call[0] for call in session.calls] == ["PUT", "POST"]
    assert session.calls[0][1][0].endswith(
        "/senders/sender%2Fname/receivers/receiver%2Fid"
    )
    assert session.calls[1][1][0].endswith(
        "/senders/sender%2Fname/receivers/receiver%2Fid/documents/document%2F1"
    )
    options = session.calls[0][2]
    assert options["headers"]["Authorization"] == "Bearer secret-token"
    assert options["ssl"] is True
    assert options["allow_redirects"] is False


@pytest.mark.asyncio
async def test_unknown_mapping_fails_before_post() -> None:
    session = Session(Response(200, b""))
    adapter = DoccleSenderRESTAdapter(configuration(), session)  # type: ignore[arg-type]
    with pytest.raises(DoccleTransportError) as caught:
        await adapter.put_document(receiver_id="receiver", document=document("unknown"))
    assert caught.value.category is DoccleTransportCategory.SAFE_PRETRANSMISSION
    assert session.calls == []


@pytest.mark.asyncio
async def test_timeout_is_ambiguous() -> None:
    adapter = DoccleSenderRESTAdapter(
        configuration(), Session(TimeoutError())  # type: ignore[arg-type]
    )
    with pytest.raises(DoccleTransportError) as caught:
        await adapter.create_or_update_receiver(
            receiver_id="receiver", profile=profile()
        )
    assert caught.value.category is DoccleTransportCategory.AMBIGUOUS
