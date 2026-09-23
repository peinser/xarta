from __future__ import annotations

import datetime

from pathlib import Path

import orjson
import pytest

from xarta.protocol.dag import DoccleNode
from xarta.protocol.dag import DoccleOutcome
from xarta.protocol.dag import DoccleReceiverSelector
from xarta.protocol.document.request.flow import DocumentFlowRequest

EXAMPLES = Path(__file__).parents[2] / "examples" / "protocol"


@pytest.mark.parametrize("fixture", ["doccle-by-subject.json", "doccle-by-id.json"])
def test_doccle_fixture_loads_through_production_parser(fixture: str) -> None:
    data = orjson.loads((EXAMPLES / fixture).read_bytes())

    request = DocumentFlowRequest.fromdict(data)

    assert isinstance(request.dag, DoccleNode)
    assert request.dag.dict()["receiver"] == data["dag"]["receiver"]
    assert "destination" not in request.dag.dict()
    assert DocumentFlowRequest.fromdict(request.dict()).dag.dict() == request.dag.dict()


@pytest.mark.parametrize("selector", [{}, {"id": "receiver-1", "subject": {"id": 1}}])
def test_receiver_selector_requires_exactly_one_selector(selector: dict) -> None:
    with pytest.raises(ValueError, match="exactly one"):
        DoccleReceiverSelector.fromdict(selector)


def test_receiver_selector_rejects_unknown_fields() -> None:
    with pytest.raises(ValueError, match="unsupported"):
        DoccleReceiverSelector.fromdict({"id": "receiver-1", "alias": "customer"})


def test_receiver_subject_is_opaque() -> None:
    subject = {
        "customer": "  Tenant/A:customer#0042?value=%2F  ",
        "provider_specific": {"number": 42, "active": True, "missing": None},
        "ordered": [2, 1],
    }

    selector = DoccleReceiverSelector(subject=subject)

    assert selector.subject == subject
    assert selector.dict() == {"subject": subject}


@pytest.mark.parametrize("value", ["not-a-date", "2026-08-28T10:15:00"])
def test_doccle_requires_valid_timezone_aware_published_at(value: str) -> None:
    with pytest.raises(ValueError, match="published_at"):
        DoccleNode(
            document={"source": "generate", "id": "document-1"},
            receiver={"subject": {"customer": "customer-1"}},
            document_type="invoice",
            published_at=value,
        )


def test_doccle_exports_outcomes_and_optional_fields() -> None:
    node = DoccleNode(
        document={"source": "generate", "id": "document-1"},
        receiver=DoccleReceiverSelector(id="receiver-1"),
        document_type="invoice",
        name={"en": "Invoice"},
        published_at=datetime.datetime(2026, 8, 28, tzinfo=datetime.UTC),
    )

    assert frozenset(outcome.value for outcome in DoccleOutcome) == node.OUTCOMES
    assert node.dict()["published_at"] == "2026-08-28T00:00:00+00:00"
    assert node.dict()["name"] == {"en": "Invoice"}
