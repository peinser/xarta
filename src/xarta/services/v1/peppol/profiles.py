from __future__ import annotations

BILLING_PROFILE_01 = "urn:fdc:peppol.eu:2017:poacc:billing:01:1.0"
BILLING_PROFILE_02 = "urn:fdc:peppol.eu:2017:poacc:billing:02:1.0"
BIS_BILLING_3_CUSTOMIZATION_PREFIX = (
    "urn:cen.eu:en16931:2017#compliant#urn:fdc:peppol.eu:2017:poacc:billing:3.0"
)

_UBL_DOCUMENT_TYPES = {
    "invoice": "urn:oasis:names:specification:ubl:schema:xsd:Invoice-2::Invoice",
    "credit_note": (
        "urn:oasis:names:specification:ubl:schema:xsd:CreditNote-2::CreditNote"
    ),
}


def require_supported_profile(customization_id: str, profile_id: str) -> None:
    if profile_id != BILLING_PROFILE_01:
        raise ValueError("unsupported_profile")
    if not customization_id.startswith(BIS_BILLING_3_CUSTOMIZATION_PREFIX):
        raise ValueError("unsupported_profile")


def document_type_identifier(document_kind: str, customization_id: str) -> str:
    return f"{_UBL_DOCUMENT_TYPES[document_kind]}##{customization_id}::2.1"
