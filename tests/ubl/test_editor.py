from __future__ import annotations

import base64

import pytest

from lxml import etree

from xarta.ubl.editor import CAC
from xarta.ubl.editor import CBC
from xarta.ubl.editor import UBLAttachment
from xarta.ubl.editor import UBLEditor
from xarta.ubl.editor import UBLError


def attachment(identifier="support", **kwargs):
    return UBLAttachment(
        identifier, "support.pdf", "application/pdf", b"%PDF-attachment", **kwargs
    )


@pytest.mark.parametrize("name", ["Invoice", "CreditNote"])
def test_add_attachments_preserves_original_and_schema_order(editor, invoice, name):
    original = invoice(
        name,
        references="<cac:AdditionalDocumentReference><cbc:ID>existing</cbc:ID></cac:AdditionalDocumentReference>",
    )
    result = editor.edit(
        original, (attachment(description="Evidence & notes"), attachment("second"))
    )
    root = etree.fromstring(result)
    references = root.findall(f"{{{CAC}}}AdditionalDocumentReference")
    assert [item.findtext(f"{{{CBC}}}ID") for item in references] == [
        "existing",
        "support",
        "second",
    ]
    binary = references[1].find(
        f"{{{CAC}}}Attachment/{{{CBC}}}EmbeddedDocumentBinaryObject"
    )
    assert binary.get("mimeCode") == "application/pdf"
    assert binary.get("filename") == "support.pdf"
    assert base64.b64decode(binary.text, validate=True) == b"%PDF-attachment"
    assert references[1].findtext(f"{{{CBC}}}DocumentDescription") == "Evidence & notes"
    assert root.findtext(f"{{{CBC}}}ID") == "INV-001"
    assert b"support.pdf" not in original
    assert editor.schemas[name].validate(root)
    assert (
        editor.edit(
            original, (attachment(description="Evidence & notes"), attachment("second"))
        )
        == result
    )


def test_insert_before_project_and_preserve_unknown_extension(editor, invoice):
    original = invoice(
        project="<cac:ProjectReference><cbc:ID>project</cbc:ID></cac:ProjectReference>",
        extension="""
      <ext:UBLExtensions xmlns:ext="urn:oasis:names:specification:ubl:schema:xsd:CommonExtensionComponents-2">
        <ext:UBLExtension><ext:ExtensionContent><custom:value xmlns:custom="urn:example:custom">keep me</custom:value>
        </ext:ExtensionContent></ext:UBLExtension></ext:UBLExtensions>""",
    )
    root = etree.fromstring(editor.edit(original, (attachment(),)))
    assert root.find(".//{urn:example:custom}value").text == "keep me"
    assert root.index(root.find(f"{{{CAC}}}AdditionalDocumentReference")) < root.index(
        root.find(f"{{{CAC}}}ProjectReference")
    )


def test_credit_note_attachments_precede_statement_and_originator(editor, invoice):
    # CreditNote orders these references after AdditionalDocumentReference.
    original = invoice(
        "CreditNote",
        references=(
            "<cac:StatementDocumentReference><cbc:ID>statement</cbc:ID></cac:StatementDocumentReference>"
            "<cac:OriginatorDocumentReference><cbc:ID>tender</cbc:ID></cac:OriginatorDocumentReference>"
        ),
    )
    root = etree.fromstring(editor.edit(original, (attachment(),)))
    names = [etree.QName(item).localname for item in root]
    assert names.index("AdditionalDocumentReference") < names.index(
        "StatementDocumentReference"
    )


@pytest.mark.parametrize(
    "document",
    [
        b"",
        b"<broken",
        b"<Invoice/>",
        b'<!DOCTYPE Invoice [<!ENTITY x "value">]><Invoice>&x;</Invoice>',
        ('<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE Invoice><Invoice/>').encode(
            "utf-16"
        ),
        b" " * 5000 + b"<!DOCTYPE Invoice><Invoice/>",
    ],
)
def test_reject_invalid_or_unsafe_xml(editor, document):
    with pytest.raises(UBLError):
        editor.edit(document, (attachment(),))


@pytest.mark.parametrize(
    "signature",
    [
        '<ds:Signature xmlns:ds="http://www.w3.org/2000/09/xmldsig#"/>',
        "<cac:Signature><cbc:ID>signature</cbc:ID></cac:Signature>",
    ],
)
def test_reject_signed_documents_before_modifying(editor, invoice, signature):
    with pytest.raises(UBLError, match="Signed"):
        editor.edit(invoice(references=signature), (attachment(),))


def test_reject_schema_invalid_input(editor, invoice):
    with pytest.raises(UBLError, match="schema"):
        editor.edit(invoice().replace(b"2026-01-01", b"bad-date"), (attachment(),))


def test_reject_existing_or_repeated_attachment_ids(editor, invoice):
    original = invoice(
        references="<cac:AdditionalDocumentReference><cbc:ID> support </cbc:ID></cac:AdditionalDocumentReference>"
    )
    with pytest.raises(UBLError, match="already exists"):
        editor.edit(original, (attachment(),))
    with pytest.raises(UBLError, match="already exists"):
        editor.edit(invoice(), (attachment(), attachment()))


@pytest.mark.parametrize(
    ("mime", "data"), [("not-a-mime-type", b"x"), ("application/pdf", b"")]
)
def test_reject_unsupported_or_empty_attachment(editor, invoice, mime, data):
    with pytest.raises(UBLError):
        editor.edit(invoice(), (UBLAttachment("id", "file", mime, data),))


def test_enforce_encoded_output_size(invoice):
    original = invoice()
    data = b"x" * 2000
    editor = UBLEditor(max_bytes=len(original) + len(data))
    with pytest.raises(UBLError, match="size limit"):
        editor.edit(
            original, (UBLAttachment("id", "file.pdf", "application/pdf", data),)
        )
