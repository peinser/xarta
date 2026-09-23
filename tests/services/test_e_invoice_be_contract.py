from __future__ import annotations

import os

import aiohttp
import pytest

from xarta.services.v1.peppol.e_invoice_be import EInvoiceBePeppolAdapter
from xarta.services.v1.peppol.e_invoice_be.configuration import EInvoiceBeConfiguration
from xarta.services.v1.peppol.models import PeppolParticipant

pytestmark = pytest.mark.e_invoice_be_contract


@pytest.mark.asyncio
async def test_e_invoice_be_sandbox_account_identity() -> None:
    api_key = os.getenv("E_INVOICE_BE_CONTRACT_API_KEY")
    webhook_secret = os.getenv("E_INVOICE_BE_CONTRACT_WEBHOOK_SECRET")
    if not api_key or not webhook_secret:
        pytest.skip("e-invoice.be contract credentials are not configured")
    sender_scheme = os.getenv("E_INVOICE_BE_CONTRACT_SENDER_SCHEME", "0208")
    sender_id = os.getenv("E_INVOICE_BE_CONTRACT_SENDER_ID", "required")
    async with aiohttp.ClientSession() as session:
        adapter = EInvoiceBePeppolAdapter(
            EInvoiceBeConfiguration.parse(
                {
                    "base_url": os.getenv(
                        "E_INVOICE_BE_CONTRACT_BASE_URL", "https://api.e-invoice.be"
                    ),
                    "tenants": [
                        {
                            "tenant_id": os.getenv(
                                "E_INVOICE_BE_CONTRACT_TENANT_ID", "contract-tenant"
                            ),
                            "api_key": api_key,
                            "webhook_secret": webhook_secret,
                            "peppol_ids": [
                                {"scheme": sender_scheme, "identifier": sender_id}
                            ],
                        }
                    ],
                }
            ),
            session,
        )
        await adapter.bind_sender(
            PeppolParticipant(sender_scheme, sender_id)
        ).validate_account_identity()
