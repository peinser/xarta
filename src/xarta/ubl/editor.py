from __future__ import annotations

import base64
import re
import threading

from dataclasses import dataclass
from pathlib import Path

from lxml import etree  # type: ignore[import-untyped]

CBC = "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2"
CAC = "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
ROOTS = {
    f"{{urn:oasis:names:specification:ubl:schema:xsd:{name}-2}}{name}": name
    for name in ("Invoice", "CreditNote")
}
# Elements that follow AdditionalDocumentReference in each UBL 2.1 root sequence.
ATTACHMENT_SUCCESSORS = {
    "Invoice": "cac:ProjectReference | cac:Signature | cac:AccountingSupplierParty",
    "CreditNote": (
        "cac:StatementDocumentReference | cac:OriginatorDocumentReference"
        " | cac:Signature | cac:AccountingSupplierParty"
    ),
}
BUNDLED_SCHEMA_DIRECTORY = Path(__file__).parent / "resources" / "xsd"
MIME_TYPE = re.compile(r"[a-z0-9!#$&^_.+\-]+/[a-z0-9!#$&^_.+\-]+", re.ASCII)


class UBLError(ValueError):
    def __init__(
        self,
        message: str,
        code: str = "ubl_invalid_document",
        schema_errors: tuple = (),
    ) -> None:
        super().__init__(message)
        self.code = code
        # lxml XSD log entries. They can contain invoice data, so only the
        # validation API returns them; they never enter the message.
        self.schema_errors = schema_errors


@dataclass(frozen=True)
class UBLAttachment:
    id: str
    filename: str
    content_type: str
    data: bytes
    description: str | None = None


def _parser():
    return etree.XMLParser(
        resolve_entities=False,
        load_dtd=False,
        no_network=True,
        recover=False,
        huge_tree=False,
        remove_blank_text=False,
    )


def parse_ubl_document(document: bytes, max_bytes: int):
    if len(document) > max_bytes:
        raise UBLError("UBL document exceeds the size limit", "ubl_size_limit")
    if not document:
        raise UBLError("UBL document is empty")
    try:
        root = etree.fromstring(document, parser=_parser())
    except (etree.XMLSyntaxError, ValueError) as ex:
        raise UBLError("UBL document is malformed XML") from ex
    if root.getroottree().docinfo.doctype or any(
        isinstance(element, etree._Entity) for element in root.iter()
    ):
        raise UBLError("UBL documents must not contain DTDs or entities")
    return root


class UBLEditor:
    """Validate and edit unsigned UBL 2.1 invoices and credit notes.

    Schemas are the bundled, checksummed OASIS UBL 2.1 XSDs.
    XML is edited in place in a private tree, preserving unknown extension nodes.
    """

    def __init__(self, *, max_bytes: int = 10 * 1024 * 1024) -> None:
        if isinstance(max_bytes, bool) or max_bytes < 1:
            raise ValueError("UBL_MAX_BYTES must be positive")
        self.max_bytes = max_bytes
        self.schemas = {
            name: etree.XMLSchema(
                etree.parse(
                    str(BUNDLED_SCHEMA_DIRECTORY / "maindoc" / f"UBL-{name}-2.1.xsd"),
                    parser=_parser(),
                )
            )
            for name in ROOTS.values()
        }
        # lxml stores each schema's error log on the shared schema object.
        self._lock = threading.Lock()

    def require_size(self, size: int) -> None:
        if size > self.max_bytes:
            raise UBLError(
                "UBL document or cumulative inputs exceed the size limit",
                "ubl_size_limit",
            )

    def validate(self, root) -> None:
        """Raise UBLError unless root is a schema-valid UBL 2.1 Invoice or CreditNote."""
        name = ROOTS.get(root.tag)
        if name is None:
            raise UBLError(
                "Only UBL 2.1 Invoice and CreditNote are supported",
                "ubl_unsupported_document",
            )
        version = root.find(f"{{{CBC}}}UBLVersionID")
        if version is not None and (version.text or "").strip() != "2.1":
            raise UBLError(
                "Only UBL version 2.1 is supported", "ubl_unsupported_document"
            )
        schema = self.schemas[name]
        # Validation threads and the NATS worker share this editor. Hold the lock
        # until the error log is copied, so no caller reads another caller's errors.
        with self._lock:
            if schema.validate(root):
                return
            errors = tuple(schema.error_log)
        raise UBLError(
            "Document does not validate against the UBL 2.1 schema",
            schema_errors=errors,
        )

    def edit(self, document: bytes, attachments: tuple[UBLAttachment, ...]) -> bytes:
        self.require_size(len(document) + sum(len(item.data) for item in attachments))
        root = parse_ubl_document(document, self.max_bytes)
        if root.xpath(
            ".//ds:Signature | .//cac:Signature | .//sig:UBLDocumentSignatures",
            namespaces={
                "ds": "http://www.w3.org/2000/09/xmldsig#",
                "cac": CAC,
                "sig": "urn:oasis:names:specification:ubl:schema:xsd:CommonSignatureComponents-2",
            },
        ):
            raise UBLError(
                "Signed UBL documents cannot be edited", "ubl_signed_document"
            )
        self.validate(root)
        references = root.findall(f"{{{CAC}}}AdditionalDocumentReference")
        seen = {(item.findtext(f"{{{CBC}}}ID") or "").strip() for item in references}
        # Insert before the first successor instead of appending after invoice lines.
        # AccountingSupplierParty is mandatory and the input validated, so one exists.
        successor = root.xpath(
            ATTACHMENT_SUCCESSORS[ROOTS[root.tag]], namespaces={"cac": CAC}
        )[0]
        for attachment in attachments:
            identifier = attachment.id.strip()
            if identifier in seen:
                raise UBLError(
                    "Attachment reference ID already exists", "ubl_attachment_conflict"
                )
            seen.add(identifier)
            mime = attachment.content_type.split(";", 1)[0].strip().lower()
            if not MIME_TYPE.fullmatch(mime) or len(mime) > 255:
                raise UBLError(
                    "Attachment MIME type is invalid", "ubl_unsupported_attachment"
                )
            if not attachment.data:
                raise UBLError("Attachment is empty", "ubl_unsupported_attachment")
            reference = etree.Element(f"{{{CAC}}}AdditionalDocumentReference")
            etree.SubElement(reference, f"{{{CBC}}}ID").text = attachment.id
            if attachment.description is not None:
                etree.SubElement(reference, f"{{{CBC}}}DocumentDescription").text = (
                    attachment.description
                )
            container = etree.SubElement(reference, f"{{{CAC}}}Attachment")
            binary = etree.SubElement(
                container, f"{{{CBC}}}EmbeddedDocumentBinaryObject"
            )
            binary.set("mimeCode", mime)
            binary.set("filename", attachment.filename)
            binary.text = base64.b64encode(attachment.data).decode("ascii")
            successor.addprevious(reference)
        self.validate(root)
        result: bytes = etree.tostring(
            root.getroottree(), encoding="UTF-8", xml_declaration=True
        )
        self.require_size(len(result))
        return result
