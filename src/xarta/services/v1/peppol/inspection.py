from __future__ import annotations

import datetime

from typing import TYPE_CHECKING

from lxml import etree  # type: ignore[import-untyped]

from xarta.services.v1.peppol.models import PeppolDocumentDescriptor
from xarta.services.v1.peppol.models import PeppolDocumentKind
from xarta.services.v1.peppol.models import PeppolParticipant

if TYPE_CHECKING:
    from xarta.protocol.document.source import DocumentSourceResult


MAX_PEPPOL_DOCUMENT_BYTES = 10 * 1024 * 1024
UBL_INVOICE_NAMESPACE = "urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
UBL_CREDIT_NOTE_NAMESPACE = "urn:oasis:names:specification:ubl:schema:xsd:CreditNote-2"
CBC_NAMESPACE = "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2"
CAC_NAMESPACE = (
    "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
)
NAMESPACES = {"cbc": CBC_NAMESPACE, "cac": CAC_NAMESPACE}


class PeppolDocumentError(ValueError):
    pass


class PeppolDocumentInspector:
    def inspect(self, document: DocumentSourceResult) -> PeppolDocumentDescriptor:
        if not document.data or len(document.data) > MAX_PEPPOL_DOCUMENT_BYTES:
            raise PeppolDocumentError("UBL document is empty or exceeds the size limit")
        lowered = document.data[:4096].lower()
        if b"<!doctype" in lowered or b"<!entity" in lowered:
            raise PeppolDocumentError("UBL document must not contain DTDs or entities")
        parser = etree.XMLParser(
            resolve_entities=False,
            load_dtd=False,
            no_network=True,
            recover=False,
            huge_tree=False,
        )
        try:
            root = etree.fromstring(document.data, parser=parser)
        except (etree.XMLSyntaxError, ValueError) as ex:
            raise PeppolDocumentError("UBL document is malformed XML") from ex

        qualified_name = etree.QName(root)
        supported_roots = {
            (UBL_INVOICE_NAMESPACE, "Invoice"): PeppolDocumentKind.INVOICE,
            (UBL_CREDIT_NOTE_NAMESPACE, "CreditNote"): PeppolDocumentKind.CREDIT_NOTE,
        }
        try:
            document_kind = supported_roots[
                (qualified_name.namespace, qualified_name.localname)
            ]
        except KeyError as ex:
            raise PeppolDocumentError(
                "Only UBL 2.1 Invoice and CreditNote are supported"
            ) from ex

        customization_id = self._required_text(root, "cbc:CustomizationID")
        profile_id = self._required_text(root, "cbc:ProfileID")
        business_document_id = self._required_text(root, "cbc:ID")
        issue_date_text = self._required_text(root, "cbc:IssueDate")
        try:
            issue_date = datetime.date.fromisoformat(issue_date_text)
        except ValueError as ex:
            raise PeppolDocumentError("UBL IssueDate must be an ISO date") from ex

        sender = self._participant(root, "cac:AccountingSupplierParty")
        receiver = self._participant(root, "cac:AccountingCustomerParty")
        return PeppolDocumentDescriptor(
            document_kind=document_kind.value,
            customization_id=customization_id,
            profile_id=profile_id,
            business_document_id=business_document_id,
            issue_date=issue_date,
            sender=sender,
            receiver=receiver,
        )

    @staticmethod
    def _required_text(root, path: str) -> str:
        values = root.xpath(path, namespaces=NAMESPACES)
        if len(values) != 1 or not values[0].text or not values[0].text.strip():
            raise PeppolDocumentError(f"UBL requires exactly one {path.split(':')[-1]}")
        return str(values[0].text).strip()

    def _participant(self, root, party_path: str) -> PeppolParticipant:
        values = root.xpath(
            f"{party_path}/cac:Party/cbc:EndpointID", namespaces=NAMESPACES
        )
        if len(values) != 1 or not values[0].text or not values[0].text.strip():
            raise PeppolDocumentError("UBL party requires exactly one EndpointID")
        scheme = values[0].get("schemeID")
        if not scheme or not scheme.strip():
            raise PeppolDocumentError("UBL EndpointID requires schemeID")
        return PeppolParticipant(scheme.strip(), values[0].text.strip())
