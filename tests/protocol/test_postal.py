from __future__ import annotations

import dataclasses

from uuid import UUID

import pytest

from xarta.flow_profiles.compiler import validate_profile_template
from xarta.protocol.dag import PostalNode
from xarta.protocol.dag import PostalOutcome
from xarta.protocol.dag import parse
from xarta.protocol.dag.postal import DocumentBoundaryPolicy
from xarta.protocol.dag.postal import LetterV1
from xarta.protocol.dag.postal import OrdinaryLetterServiceV1
from xarta.protocol.dag.postal import PostalRequest
from xarta.protocol.dag.postal import PrintColorMode
from xarta.protocol.dag.postal import PrintSides
from xarta.protocol.dag.postal import RegisteredLetterServiceV1
from xarta.protocol.document.source import ArchiveDocumentSource
from xarta.protocol.document.source import GenerateDocumentSource
from xarta.protocol.document.source import RenderDocumentSource

DOCUMENT_ID = "61ea2613-87c6-4546-b861-af7eb3897051"
ARCHIVE_ID = "0ce71682-841d-4f84-91ba-3bed69bda212"
ARCHIVE_VERSION = "2e90fe66-71cb-48fc-b57d-62cdad7c061c"


def specification(*, registered: bool = False, printing: bool = True) -> dict:
    service = (
        {"type": "registered", "version": 1, "acknowledgement": "none"}
        if registered
        else {"type": "ordinary", "version": 1, "speed": "non_priority"}
    )
    mailpiece = {
        "type": "letter",
        "version": 1,
        "content": {
            "documents": [
                {
                    "source": "archive",
                    "id": ARCHIVE_ID,
                    "version": ARCHIVE_VERSION,
                    "role": "cover-letter",
                },
                {"source": "generate", "id": DOCUMENT_ID, "role": "terms"},
            ]
        },
        "recipient": {
            "name": "Jan Janssens",
            "company_name": "Example NV",
            "department": "Legal",
            "address": {
                "format": "structured",
                "street": "Wetstraat",
                "house_number": "16",
                "box_number": "4",
                "postal_code": "1000",
                "city": "Brussel",
                "region": None,
                "country_code": "BE",
            },
        },
        "sender": {"type": "profile", "id": "peinser-default"},
        "service": service,
        "references": {"case_id": "CASE-2026-0001"},
        "extensions": {"example:batch": {"sequence": 2}},
    }
    if printing:
        mailpiece["print"] = {
            "version": 1,
            "color_mode": "monochrome",
            "sides": "duplex_long_edge",
            "document_boundary": "continuous",
        }
    return {"kind": "postal", "destination": "local-brussels", "mailpiece": mailpiece}


@pytest.mark.parametrize("registered", [False, True])
def test_letter_v1_round_trip_and_pure_interpretation(registered: bool) -> None:
    raw = specification(registered=registered)

    node = parse(raw)
    first = node.interpret()
    second = node.interpret()

    assert isinstance(node, PostalNode)
    assert isinstance(first, PostalRequest)
    assert first == second
    assert first.destination == "local-brussels"
    assert isinstance(first.mailpiece, LetterV1)
    service_type = RegisteredLetterServiceV1 if registered else OrdinaryLetterServiceV1
    assert isinstance(first.mailpiece.service, service_type)
    assert [document.role for document in first.mailpiece.content.documents] == [
        "cover-letter",
        "terms",
    ]
    assert isinstance(
        first.mailpiece.content.documents[0].source, ArchiveDocumentSource
    )
    assert isinstance(
        first.mailpiece.content.documents[1].source, GenerateDocumentSource
    )
    assert parse(node.dict()).dict() == node.dict()


def test_postal_parser_supports_nested_successors_and_all_outcomes() -> None:
    raw = specification()
    raw["on"] = {
        "prepared": [{"kind": "debug"}],
        "delivered": [{"kind": "postal", "mailpiece": raw["mailpiece"]}],
    }

    node = parse(raw)

    assert node.on["prepared"][0].parent is node
    assert isinstance(node.on["delivered"][0], PostalNode)
    assert node.on["delivered"][0].parent is node
    assert {
        "prepared",
        "handed_over",
        "carrier_accepted",
        "out_for_delivery",
        "available_for_pickup",
        "delivery_exception",
        "delivered",
        "returned",
        "deposit_evidence_available",
        "delivery_evidence_available",
        "invalid_address",
        "unsupported_service",
        "capacity_rejected",
        "production_failed",
        "evidence_unavailable",
        "cancelled",
    } == node.OUTCOMES
    assert frozenset(outcome.value for outcome in PostalOutcome) == node.OUTCOMES


def test_postal_is_registered_for_flow_profiles() -> None:
    raw = specification()
    raw["node_key"] = "send-letter"

    validate_profile_template(raw, frozenset(), frozenset())


def test_print_settings_are_optional() -> None:
    node = parse(specification(printing=False))

    assert node.mailpiece.print is None
    assert "print" not in node.dict()["mailpiece"]


@pytest.mark.parametrize("color", list(PrintColorMode))
@pytest.mark.parametrize("sides", list(PrintSides))
@pytest.mark.parametrize("boundary", list(DocumentBoundaryPolicy))
def test_all_print_variants_are_supported(color, sides, boundary) -> None:
    raw = specification()
    raw["mailpiece"]["print"].update(
        {
            "color_mode": color.value,
            "sides": sides.value,
            "document_boundary": boundary.value,
        }
    )

    settings = parse(raw).mailpiece.print

    assert settings.color_mode is color
    assert settings.sides is sides
    assert settings.document_boundary is boundary


def test_render_source_uses_existing_document_source_contract() -> None:
    raw = specification()
    raw["mailpiece"]["content"]["documents"] = [
        {
            "source": "render",
            "id": DOCUMENT_ID,
            "document_type": "letter",
            "payload": {"content_type": "application/json", "data": {"name": "Jan"}},
            "content_type": "application/pdf",
        }
    ]

    reference = parse(raw).mailpiece.content.documents[0]

    assert isinstance(reference.source, RenderDocumentSource)
    assert reference.source.id == UUID(DOCUMENT_ID)


@pytest.mark.parametrize(
    ("path", "field"),
    [
        ((), "provider"),
        (("mailpiece",), "franking"),
        (("mailpiece", "content"), "sort"),
        (("mailpiece", "content", "documents", 0), "filename"),
        (("mailpiece", "recipient"), "email"),
        (("mailpiece", "recipient", "address"), "line1"),
        (("mailpiece", "sender"), "revision"),
        (("mailpiece", "service"), "tracking"),
        (("mailpiece", "print"), "copies"),
    ],
)
def test_unknown_fields_are_rejected_at_every_schema_level(path, field) -> None:
    raw = specification()
    target = raw
    for component in path:
        target = target[component]
    target[field] = "unsupported"

    with pytest.raises(ValueError, match="unsupported fields"):
        parse(raw)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda value: value["mailpiece"].update(type="parcel"), "letter version 1"),
        (
            lambda value: value["mailpiece"]["recipient"].update(
                name=None, company_name=None
            ),
            "name or company_name",
        ),
        (
            lambda value: value["mailpiece"]["recipient"]["address"].update(
                country_code="NL"
            ),
            "must be BE",
        ),
        (
            lambda value: value["mailpiece"]["recipient"]["address"].update(
                postal_code="0123"
            ),
            "Belgian postcode",
        ),
        (
            lambda value: value["mailpiece"]["service"].update(speed="economy"),
            "speed is unsupported",
        ),
        (
            lambda value: value["mailpiece"]["sender"].update(type="inline"),
            "type must be profile",
        ),
    ],
)
def test_letter_semantics_are_strict(change, message: str) -> None:
    raw = specification()
    change(raw)

    with pytest.raises(ValueError, match=message):
        parse(raw)


@pytest.mark.parametrize(
    "path",
    [
        ("mailpiece",),
        ("mailpiece", "service"),
        ("mailpiece", "print"),
    ],
)
@pytest.mark.parametrize("version", [True, 1.0, "1"])
def test_versions_require_the_integer_one(path, version) -> None:
    raw = specification()
    target = raw
    for component in path:
        target = target[component]
    target["version"] = version

    with pytest.raises(ValueError, match="version"):
        parse(raw)


def test_document_ids_require_uuids() -> None:
    raw = specification()
    raw["mailpiece"]["content"]["documents"][0]["id"] = "not-a-uuid"

    with pytest.raises(ValueError, match="id must be a UUID"):
        parse(raw)


def test_document_order_is_preserved() -> None:
    node = parse(specification())

    assert [str(item.source.id) for item in node.mailpiece.content.documents] == [
        ARCHIVE_ID,
        DOCUMENT_ID,
    ]


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("references", {f"key-{index}": "value" for index in range(33)}, "too many"),
        ("references", {"key": "x" * 257}, "length limit"),
        ("extensions", {"not_namespaced": True}, "namespaced"),
        (
            "extensions",
            {f"example:key-{index}": index for index in range(33)},
            "too many",
        ),
        ("extensions", {"example:data": "x" * (16 * 1024)}, "size limit"),
    ],
)
def test_references_and_extensions_are_bounded(field, value, message: str) -> None:
    raw = specification()
    raw["mailpiece"][field] = value

    with pytest.raises(ValueError, match=message):
        parse(raw)


def test_interpreted_models_are_immutable_and_detached() -> None:
    raw = specification()
    node = parse(raw)
    raw["mailpiece"]["references"]["case_id"] = "changed"
    raw["mailpiece"]["extensions"]["example:batch"]["sequence"] = 99

    assert node.mailpiece.references["case_id"] == "CASE-2026-0001"
    assert node.mailpiece.extensions["example:batch"]["sequence"] == 2
    with pytest.raises(dataclasses.FrozenInstanceError):
        node.mailpiece.recipient.name = "Changed"
    with pytest.raises(TypeError):
        node.mailpiece.references["other"] = "value"
    with pytest.raises(TypeError):
        node.mailpiece.extensions["example:batch"]["sequence"] = 3


def test_document_count_is_bounded() -> None:
    raw = specification()
    document = {"source": "generate", "id": DOCUMENT_ID}
    raw["mailpiece"]["content"]["documents"] = [document] * 26

    with pytest.raises(ValueError, match="too many documents"):
        parse(raw)
