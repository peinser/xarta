from __future__ import annotations

import io
import re

from types import SimpleNamespace
from uuid import uuid4

import pytest

from postal_station.rendering.traveller import render_traveller
from pyhanko.pdf_utils.reader import PdfFileReader


def _item(*, supplies: list[dict] | None = None) -> SimpleNamespace:
    supplies = supplies or [
        {
            "reference": "prior-1",
            "description": "Synthetic Bpost Prior unit stamp",
            "quantity": 1,
        }
    ]
    lines = [
        {
            "reference": supply["reference"],
            "quantity": supply["quantity"],
            "unit_price": "1.50",
            "amount": str(1.5 * supply["quantity"]),
        }
        for supply in supplies
    ]
    return SimpleNamespace(
        task_id=uuid4(),
        sources=((1, "one", "content"), (2, "two", "content")),
        plan={
            "mailpiece": {
                "service": {"type": "ordinary", "speed": "priority"},
                "recipient": {
                    "name": "Renée Example",
                    "address": {
                        "street": "Rue de l'Étude",
                        "house_number": "12",
                        "postal_code": "1000",
                        "city": "Bruxelles",
                    },
                },
            },
            "quantities": {"estimated_weight_g": "14.98"},
            "franking": {
                "provider": {
                    "id": "bpost",
                    "profile": {
                        "id": "development-stamps-v1",
                        "revision": "development-v1",
                    },
                },
                "method": "stamps",
                "policy_revision": "policy-development-v1",
                "supplies": supplies,
                "pricing": {
                    "tariff_revision": "tariff-development-v1",
                    "currency": "EUR",
                    "lines": lines,
                    "total_postage": "1.50",
                },
            },
        },
    )


def _page(document: bytes) -> tuple[list[float], bytes]:
    reader = PdfFileReader(io.BytesIO(document), strict=True)
    page_ref, _ = reader.find_page_for_modification(0)
    page = page_ref.get_object()
    media_box = [float(value) for value in page["/MediaBox"]]
    return media_box, page["/Contents"].get_object().data


def _render(item: SimpleNamespace) -> bytes:
    instructions = SimpleNamespace(
        service=item.plan["mailpiece"]["service"],
        recipient=item.plan["mailpiece"]["recipient"],
        quantities=item.plan["quantities"],
        franking=item.plan["franking"],
    )
    return render_traveller(str(item.task_id), len(item.sources), instructions)


def test_traveller_has_exact_a4_bounded_marked_regions_and_isolated_label() -> None:
    item = _item()
    media_box, stream = _page(_render(item))
    assert media_box == pytest.approx([0, 0, 595.276, 841.890], abs=0.001)

    tags = [
        b"/XartaRetained BMC",
        b"/XartaBarcode BMC",
        b"/XartaSection1 BMC",
        b"/XartaSection2 BMC",
        b"/XartaSection3 BMC",
        b"/XartaSection4 BMC",
        b"/XartaCutLine BMC",
        b"/XartaLabel BMC",
    ]
    assert all(tag in stream for tag in tags)
    regions = [
        tuple(map(float, match.groups()))
        for match in re.finditer(
            rb"% XartaRegion ([0-9.]+) ([0-9.]+) ([0-9.]+) ([0-9.]+)", stream
        )
    ]
    assert len(regions) == 8
    retained, barcode, section1, section2, section3, section4, cut, label = regions
    assert retained[0] + retained[2] < barcode[0]
    assert barcode[1] > cut[1] + cut[3]
    for upper, lower in zip(
        (retained, section1, section2, section3, section4, cut),
        (section1, section2, section3, section4, cut, label),
        strict=True,
    ):
        assert lower[1] + lower[3] < upper[1]

    barcode_start = stream.index(b"/XartaBarcode BMC")
    barcode_end = stream.index(b"EMC", barcode_start)
    label_start = stream.index(b"/XartaLabel BMC")
    barcode_stream = stream[barcode_start:barcode_end]
    label_stream = stream[label_start:]
    task_uuid = str(item.task_id).encode()
    assert barcode_stream.count(b" re f") > 50
    assert b" re f" not in label_stream
    assert task_uuid in stream[:label_start]
    assert task_uuid not in label_stream
    assert b"operation" not in label_stream.lower()
    assert b"scan" not in label_stream.lower()


def test_traveller_emits_operational_sections_cut_graphics_and_price_snapshot() -> None:
    _, stream = _page(_render(_item()))
    for value in (
        b"START AND VERIFY",
        b"PREPARE MAILPIECE",
        b"APPLY POSTAGE",
        b"FINISH AND HAND OVER",
        b"CUT HERE - DETACH MAILING LABEL",
        b"Synthetic Bpost Prior unit stamp",
        b"tariff-development-v1",
        b"1.50 EUR",
    ):
        assert value in stream
    cut_start = stream.index(b"/XartaCutLine BMC")
    cut_end = stream.index(b"EMC", cut_start)
    cut = stream[cut_start:cut_end]
    assert b"[5 4] 0 d" in cut
    assert cut.count(b" c") == 16
    assert cut.count(b" l S") == 5  # Four blades plus the dashed cut line.


def test_traveller_rejects_content_that_cannot_fit_a_bounded_region() -> None:
    supplies = [
        {
            "reference": f"stamp-{index}",
            "description": "Synthetic postage supply",
            "quantity": 1,
        }
        for index in range(20)
    ]
    with pytest.raises(ValueError, match="APPLY POSTAGE region"):
        _render(_item(supplies=supplies))
