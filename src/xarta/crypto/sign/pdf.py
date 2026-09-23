r"""
A module for signing PDF's.
"""

from __future__ import annotations

from io import BytesIO

# The following is a small hack to bypass the fact that pyHanko registers itself as a PDF Producer.
# https://github.com/MatthiasValvekens/pyHanko/blob/7e790bea82c17fab4b12e5beaa91d4d13f9a725c/pyhanko/pdf_utils/metadata/info.py#L73
import pyhanko.pdf_utils.metadata.model

from xarta.pdf import PRODUCER as XARTA_PRODUCER

pyhanko.pdf_utils.metadata.model.VENDOR = XARTA_PRODUCER

from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
from pyhanko.sign.fields import SigSeedSubFilter
from pyhanko.sign.fields import enumerate_sig_fields
from pyhanko.sign.signers import PdfSignatureMetadata
from pyhanko.sign.signers import PdfSigner
from pyhanko.sign.signers import Signer
from pyhanko.sign.timestamps import TimeStamper

from xarta.crypto.sign.policy import PdfSignatureProfile
from xarta.crypto.sign.policy import SignaturePolicy
from xarta.crypto.sign.policy import SignaturePolicyConfigurationError


def sign(
    data: bytes,
    signer: Signer,
    policy: SignaturePolicy,
    timestamper: TimeStamper | None = None,
) -> bytes:
    r"""
    Attempts to digitally sign the specified byte-sequence under the assumption
    it encodes a PDF.
    """
    binary = BytesIO(data)
    writer = IncrementalPdfFileWriter(binary, strict=False)

    # Retrieve the existing signature fields.
    existing_signature_field_names = {
        tuple[0] for tuple in enumerate_sig_fields(writer)
    }

    # Determine the next Signature field name.
    field_name = "Signature"
    field_index = 1

    # Check if the field_name exists.
    while field_name in existing_signature_field_names:
        field_name = f"Signature{field_index}"
        field_index += 1

    meta = create_signature_metadata(policy=policy, field_name=field_name)

    if policy.profile is PdfSignatureProfile.PADES_B_T and timestamper is None:
        raise SignaturePolicyConfigurationError(
            "PAdES B-T signing requires a resolved timestamp provider"
        )

    pdf_signer = PdfSigner(
        signature_meta=meta,
        signer=signer,
        timestamper=timestamper,
    )

    out = pdf_signer.sign_pdf(
        pdf_out=writer,
    )

    return bytes(out.getvalue())


def create_signature_metadata(
    *, policy: SignaturePolicy, field_name: str
) -> PdfSignatureMetadata:
    if policy.profile is PdfSignatureProfile.LEGACY_PDF_CMS:
        return PdfSignatureMetadata(field_name=field_name)
    if policy.profile in {
        PdfSignatureProfile.PADES_B_B,
        PdfSignatureProfile.PADES_B_T,
    }:
        return PdfSignatureMetadata(
            field_name=field_name,
            md_algorithm=policy.digest_algorithm,
            subfilter=SigSeedSubFilter.PADES,
        )
    if policy.profile is PdfSignatureProfile.PADES_B_LT:
        raise SignaturePolicyConfigurationError(
            "PAdES B-LT signing requires timestamp and trust providers"
        )
    if policy.profile is PdfSignatureProfile.PADES_B_LTA:
        raise SignaturePolicyConfigurationError(
            "PAdES B-LTA signing requires timestamp, trust and preservation support"
        )
    raise SignaturePolicyConfigurationError(
        f"Unsupported PDF signature profile {policy.profile.value!r}"
    )
