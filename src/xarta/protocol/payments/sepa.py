r"""
Data definities surrounding SEPA.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class SEPAIdentification(StrEnum):
    SCT: str = "SCT"  # SEPA Credit Transfer
    SDD: str = "SDD"  # SEPA Direct Debit
    B2B: str = "B2B"  # SEPA Direct Debit (Business-to-Business)


@dataclass(frozen=True)
class SEPATransactionData:
    iban: str  # Beneficiary's IBAN
    name: str  # Beneficiary's name
    amount: str  # Payment amount in EUR (format "EUR123.45")
    identification: SEPAIdentification = SEPAIdentification.SCT  # SEPA payment type
    bic: str | None = None  # Bank Identifier Code (optional)
    purpose: str | None = None
    invoice: str | None = None
    text: str | None = None
    information: str | None = None


@dataclass(frozen=True)
class SEPAQRData(SEPATransactionData):
    r"""
    Service Tag:	BCD
    Version:	001
    Character set:	1
    Identification:	SCT
    BIC:	BPOTBEB1
    Name:	Red Cross
    IBAN:	BE72000000001616
    Amount:	EUR1
    Reason (4 chars max):	CHAR
    Ref of invoice:	Empty line or REFINVOICE
    Or text:	Urgency fund or Empty line
    Information:	Sample EPC QR code
    """

    service_tag: str = "BCD"  # Constant value for SEPA QR Codes
    version: str = "001"  # EPC QR Code version (always "001")
    charset: str = "1"  # Character set (1 = UTF-8)

    def payload(self) -> str:
        return "\n".join(
            [
                self.service_tag,
                self.version,
                self.charset,
                self.identification.value,
                self.bic or "",
                self.name,
                self.iban,
                self.amount or "",
                self.purpose or "",
                self.invoice or "",
                self.text or "",
                self.information or "",
            ]
        )
