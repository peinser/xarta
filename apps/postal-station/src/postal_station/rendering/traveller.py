from __future__ import annotations

import textwrap

from dataclasses import dataclass
from io import BytesIO
from typing import Any
from typing import cast

import barcode  # type: ignore[import-untyped]

from barcode.writer import BaseWriter  # type: ignore[import-untyped]
from pyhanko.pdf_utils import generic
from pyhanko.pdf_utils.writer import PageObject
from pyhanko.pdf_utils.writer import PdfFileWriter

_MM_TO_POINTS = 72 / 25.4
_PAGE_WIDTH = 595.276
_PAGE_HEIGHT = 841.890


@dataclass(frozen=True, slots=True)
class PdfBarcode:
    commands: bytes
    width: float
    height: float


class PdfBarcodeWriter(BaseWriter):
    def __init__(self) -> None:
        self._commands: list[str] = []
        self.width = 0.0
        self.height = 0.0
        super().__init__(self._initialize, self._paint_module, None, self._finish)

    def _initialize(self, code: list[str]) -> None:
        width_mm, height_mm = self.calculate_size(len(code[0]), len(code))
        self.width = width_mm * _MM_TO_POINTS
        self.height = height_mm * _MM_TO_POINTS
        self._commands = ["q", "0 0 0 rg"]

    def _paint_module(
        self, xpos: float, ypos: float, width: float, color: str | int
    ) -> None:
        if color != self.foreground:
            return
        x = xpos * _MM_TO_POINTS
        y = (self.height / _MM_TO_POINTS - ypos - self.module_height) * _MM_TO_POINTS
        self._commands.append(
            f"{x:.4f} {y:.4f} {width * _MM_TO_POINTS:.4f} "
            f"{self.module_height * _MM_TO_POINTS:.4f} re f"
        )

    def _finish(self) -> bytes:
        self._commands.append("Q")
        return "\n".join(self._commands).encode("ascii")


def render_code128(payload: str) -> PdfBarcode:
    writer = PdfBarcodeWriter()
    code = barcode.get("code128", payload, writer=writer)
    commands = cast(Any, code).render(
        {
            "module_width": 0.2,
            "module_height": 15.0,
            "quiet_zone": 4.0,
            "write_text": False,
            "margin_top": 1.0,
            "margin_bottom": 1.0,
        }
    )
    return PdfBarcode(commands, writer.width, writer.height)


def _pdf_text(value: str) -> str:
    try:
        encoded = value.encode("cp1252")
    except UnicodeEncodeError as ex:
        raise ValueError(
            "Traveller text contains characters unsupported by Helvetica"
        ) from ex
    escaped = []
    for byte in encoded:
        if byte in (ord("("), ord(")"), ord("\\")):
            escaped.append("\\" + chr(byte))
        elif 32 <= byte <= 126:
            escaped.append(chr(byte))
        else:
            escaped.append(f"\\{byte:03o}")
    return "".join(escaped)


def _wrapped(values: list[str], *, width: int) -> list[str]:
    lines: list[str] = []
    for value in values:
        wrapped = textwrap.wrap(value, width=width, break_long_words=False)
        if not wrapped and value:
            raise ValueError("Traveller contains an unbreakable line that is too long")
        if any(len(line) > width for line in wrapped):
            raise ValueError("Traveller contains an unbreakable line that is too long")
        lines.extend(wrapped or [""])
    return lines


def _region(
    tag: str,
    title: str,
    values: list[str],
    *,
    x: float,
    y: float,
    width: float,
    height: float,
) -> list[str]:
    lines = _wrapped(values, width=max(1, int((width - 20) / 4.6)))
    if len(lines) > int((height - 35) / 11):
        raise ValueError(f"Traveller content exceeds {title} region")
    commands = [
        f"/{tag} BMC",
        f"% XartaRegion {x:.3f} {y:.3f} {width:.3f} {height:.3f}",
        "0.7 w",
        f"{x:.3f} {y:.3f} {width:.3f} {height:.3f} re S",
        f"BT /F1 11 Tf {x + 10:.3f} {y + height - 18:.3f} Td ({_pdf_text(title)}) Tj ET",
    ]
    text_y = y + height - 34
    commands.append(f"BT /F1 8 Tf {x + 10:.3f} {text_y:.3f} Td 11 TL")
    for line in lines:
        commands.append(f"({_pdf_text(line)}) Tj T*")
    commands.extend(("ET", "EMC"))
    return commands


def _scissors(x: float, y: float) -> list[str]:
    # Four cubic Bezier segments draw each handle without relying on a text glyph.
    k = 1.657
    circles: list[str] = []
    for cy in (y + 5, y - 5):
        circles.extend(
            (
                f"{x - 5:.3f} {cy:.3f} m",
                f"{x - 5:.3f} {cy + k:.3f} {x - 3.657:.3f} {cy + 3:.3f} {x - 2:.3f} {cy + 3:.3f} c",
                f"{x - 0.343:.3f} {cy + 3:.3f} {x + 1:.3f} {cy + k:.3f} {x + 1:.3f} {cy:.3f} c",
                f"{x + 1:.3f} {cy - k:.3f} {x - 0.343:.3f} {cy - 3:.3f} {x - 2:.3f} {cy - 3:.3f} c",
                f"{x - 3.657:.3f} {cy - 3:.3f} {x - 5:.3f} {cy - k:.3f} {x - 5:.3f} {cy:.3f} c S",
            )
        )
    return [
        *circles,
        f"{x - 5:.3f} {y + 3:.3f} m {x + 8:.3f} {y - 6:.3f} l S",
        f"{x - 5:.3f} {y - 3:.3f} m {x + 8:.3f} {y + 6:.3f} l S",
    ]


def render_traveller(task_id: str, source_count: int, instructions: Any) -> bytes:
    service = instructions.service
    recipient = instructions.recipient
    address = recipient["address"]
    franking = instructions.franking
    pricing = franking["pricing"]
    provider = franking["provider"]
    profile = provider["profile"]
    task_uuid = task_id

    label_lines = [
        str(recipient.get("name") or recipient.get("company_name") or ""),
        f"{address['street']} {address['house_number']}",
        f"{address['postal_code']} {address['city']}",
        "BE",
    ]
    if any(task_uuid in line for line in label_lines):
        raise ValueError("Detachable label must not contain the task UUID")

    supply_lines = [
        f"Apply {supply['quantity']} x {supply['description']} [{supply['reference']}] exactly as listed."
        for supply in franking["supplies"]
    ]
    price_by_reference = {line["reference"]: line for line in pricing["lines"]}
    price_lines = [
        f"{supply['reference']}: {price_by_reference[supply['reference']]['quantity']} x "
        f"{price_by_reference[supply['reference']]['unit_price']} {pricing['currency']} = "
        f"{price_by_reference[supply['reference']]['amount']} {pricing['currency']}"
        for supply in franking["supplies"]
    ]
    speed = service.get("speed") or "standard"
    commands = [
        "/XartaRetained BMC",
        "% XartaRegion 30.000 752.000 290.000 69.890",
        "BT /F1 13 Tf 30 811 Td (XARTA POSTAL TRAVELLER) Tj ET",
        "BT /F1 9 Tf 30 793 Td (RETAIN OUTSIDE ENVELOPE - REMOVE BEFORE HANDOVER) Tj ET",
        f"BT /F1 8 Tf 30 777 Td (Task: {_pdf_text(task_uuid)}) Tj ET",
        f"BT /F1 8 Tf 30 764 Td (Service: {_pdf_text(service['type'])} / {_pdf_text(speed)}) Tj ET",
        "EMC",
    ]
    rendered_barcode = render_code128(task_uuid)
    barcode_x, barcode_y, barcode_width, barcode_height = 340.0, 752.0, 225.276, 69.890
    scale = min(
        (barcode_width - 12) / rendered_barcode.width, 42 / rendered_barcode.height
    )
    commands.extend(
        (
            "/XartaBarcode BMC",
            f"% XartaRegion {barcode_x:.3f} {barcode_y:.3f} {barcode_width:.3f} {barcode_height:.3f}",
            f"{barcode_x:.3f} {barcode_y:.3f} {barcode_width:.3f} {barcode_height:.3f} re S",
            f"q {scale:.6f} 0 0 {scale:.6f} {barcode_x + 6:.3f} {barcode_y + 22:.3f} cm",
            rendered_barcode.commands.decode("ascii"),
            "Q",
            f"BT /F1 7 Tf {barcode_x + 17:.3f} {barcode_y + 9:.3f} Td ({task_uuid}) Tj ET",
            "EMC",
        )
    )
    commands.extend(
        _region(
            "XartaSection1",
            "1  START AND VERIFY",
            [
                "Scan the bare task barcode as start_production.",
                f"Verify task, {service['type']} service, and {source_count} source documents before continuing.",
            ],
            x=30,
            y=642,
            width=535.276,
            height=96,
        )
    )
    commands.extend(
        _region(
            "XartaSection2",
            "2  PREPARE MAILPIECE",
            [
                "Detach the mailing label below and attach it securely to the envelope.",
                "Keep source documents in their supplied order. Do not place the retained traveller in the envelope.",
            ],
            x=30,
            y=540,
            width=535.276,
            height=92,
        )
    )
    commands.extend(
        _region(
            "XartaSection3",
            "3  APPLY POSTAGE",
            [
                *supply_lines,
                f"Estimated weight: {instructions.quantities['estimated_weight_g']} g. Franking: {franking['method']}.",
                f"Provider: {provider['id']}; profile: {profile['id']} rev {profile['revision']}; policy: {franking['policy_revision']}.",
                f"Tariff: {pricing['tariff_revision']}; snapshot total: {pricing['total_postage']} {pricing['currency']}.",
                *price_lines,
            ],
            x=30,
            y=382,
            width=535.276,
            height=148,
        )
    )
    commands.extend(
        _region(
            "XartaSection4",
            "4  FINISH AND HAND OVER",
            [
                "Fold and insert all content in source order, then seal the envelope.",
                "Scan ready_for_handover. Keep this traveller outside the envelope.",
                "At physical handover, remove and retain the traveller, then scan confirm_handover.",
            ],
            x=30,
            y=264,
            width=535.276,
            height=108,
        )
    )
    commands.extend(
        (
            "/XartaCutLine BMC",
            "% XartaRegion 20.000 232.000 555.276 20.000",
            "[5 4] 0 d 30 241 m 565.276 241 l S [] 0 d",
            "BT /F1 8 Tf 194 247 Td (CUT HERE - DETACH MAILING LABEL) Tj ET",
            *_scissors(24, 241),
            *_scissors(582, 241),
            "EMC",
        )
    )
    commands.extend(
        _region(
            "XartaLabel",
            "DETACHABLE MAILING LABEL",
            label_lines,
            x=50,
            y=35,
            width=495.276,
            height=180,
        )
    )
    return _single_page_pdf_from_commands(commands)


def _single_page_pdf(text: str) -> bytes:
    lines = _wrapped(text.splitlines(), width=90)
    if len(lines) > 55:
        raise ValueError("PDF text exceeds one page")
    commands = ["BT /F1 10 Tf 40 800 Td 13 TL"]
    commands.extend(f"({_pdf_text(line)}) Tj T*" for line in lines)
    commands.append("ET")
    return _single_page_pdf_from_commands(commands)


def _single_page_pdf_from_commands(commands: list[str]) -> bytes:
    writer = PdfFileWriter()
    stream = writer.add_object(
        generic.StreamObject(stream_data="\n".join(commands).encode("ascii"))
    )
    font = writer.add_object(
        generic.DictionaryObject(
            {
                generic.pdf_name("/Type"): generic.pdf_name("/Font"),
                generic.pdf_name("/Subtype"): generic.pdf_name("/Type1"),
                generic.pdf_name("/BaseFont"): generic.pdf_name("/Helvetica"),
                generic.pdf_name("/Encoding"): generic.pdf_name("/WinAnsiEncoding"),
            }
        )
    )
    page = PageObject(
        stream,
        (0, 0, _PAGE_WIDTH, _PAGE_HEIGHT),
        generic.DictionaryObject(
            {
                generic.pdf_name("/Font"): generic.DictionaryObject(
                    {generic.pdf_name("/F1"): font}
                )
            }
        ),
    )
    writer.insert_page(page)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()
