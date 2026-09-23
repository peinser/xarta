"""Strict ordered PDF merge and atomic postal letter composition."""

from __future__ import annotations

from io import BytesIO

from pyhanko.pdf_utils import generic
from pyhanko.pdf_utils.reader import PdfFileReader
from pyhanko.pdf_utils.writer import PageObject
from pyhanko.pdf_utils.writer import PdfFileWriter
from pyhanko.pdf_utils.writer import copy_into_new_writer

from postal_station.models import DocumentBoundary
from postal_station.models import PrintSides


def page_count(document: bytes) -> int:
    reader = PdfFileReader(BytesIO(document), strict=True)
    count = int(reader.root["/Pages"]["/Count"])
    if count < 1:
        raise ValueError("PDF must contain at least one page")
    for index in range(count):
        reader.find_page_for_modification(index)
    return count


def merge_pdf_documents(
    documents: tuple[bytes, ...], *, sides: PrintSides, boundary: DocumentBoundary
) -> bytes:
    if not documents:
        raise ValueError("Postal content requires at least one PDF")
    readers = [PdfFileReader(BytesIO(document), strict=True) for document in documents]
    counts = [page_count(document) for document in documents]
    writer = copy_into_new_writer(readers[0])
    align_documents = (
        sides is not PrintSides.SIMPLEX and boundary is not DocumentBoundary.CONTINUOUS
    )
    for previous_count, reader in zip(counts, readers[1:], strict=False):
        if align_documents and previous_count % 2:
            _insert_blank_page(writer)
        for page_index in range(int(reader.root["/Pages"]["/Count"])):
            page_ref, _ = reader.find_page_for_modification(page_index)
            page = writer.import_object(page_ref).get_object()
            page.pop("/Parent", None)
            writer.insert_page(page)
    return _write(writer)


def compose_letter_pdf(traveller: bytes, content: bytes, *, sides: PrintSides) -> bytes:
    traveller_reader = PdfFileReader(BytesIO(traveller), strict=True)
    if page_count(traveller) != 1:
        raise ValueError("Postal traveller must contain exactly one page")
    content_reader = PdfFileReader(BytesIO(content), strict=True)
    page_count(content)
    writer = copy_into_new_writer(traveller_reader)
    if sides is not PrintSides.SIMPLEX:
        _insert_blank_page(writer)
    for page_index in range(int(content_reader.root["/Pages"]["/Count"])):
        page_ref, _ = content_reader.find_page_for_modification(page_index)
        page = writer.import_object(page_ref).get_object()
        page.pop("/Parent", None)
        writer.insert_page(page)
    return _write(writer)


def _insert_blank_page(writer: PdfFileWriter) -> None:
    stream = writer.add_object(generic.StreamObject(stream_data=b""))
    writer.insert_page(PageObject(stream, (0, 0, 595.276, 841.890)))


def _write(writer: PdfFileWriter) -> bytes:
    output = BytesIO()
    writer.write(output)
    return output.getvalue()
