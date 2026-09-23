from __future__ import annotations

import os

from pathlib import Path
from uuid import uuid4

import aiohttp
import pytest

from xarta.protocol.document.source import DocumentSourceResult
from xarta.services.v1.peppol.inspection import PeppolDocumentInspector
from xarta.services.v1.peppol.models import PeppolParticipant
from xarta.services.v1.peppol.recommand import RecommandConfiguration
from xarta.services.v1.peppol.recommand import RecommandPeppolAdapter

pytestmark = pytest.mark.recommand_contract

FIXTURES = Path(__file__).parents[1] / "fixtures" / "peppol"


@pytest.mark.asyncio
async def test_recommand_isolated_playground_send_contract() -> None:
    api_key = os.getenv("RECOMMAND_CONTRACT_API_KEY")
    api_secret = os.getenv("RECOMMAND_CONTRACT_API_SECRET")
    webhook_secret = os.getenv("RECOMMAND_CONTRACT_WEBHOOK_SECRET")
    company_id = os.getenv("RECOMMAND_CONTRACT_COMPANY_ID")
    sender_id = os.getenv("RECOMMAND_CONTRACT_SENDER_ID")
    recipient_id = os.getenv("RECOMMAND_CONTRACT_RECIPIENT_ID")
    if not all(
        (api_key, api_secret, webhook_secret, company_id, sender_id, recipient_id)
    ):
        pytest.skip("Recommand playground contract credentials are not configured")
    if os.getenv("CONFIRM_RECOMMAND_PLAYGROUND") != "yes":
        pytest.skip("Set CONFIRM_RECOMMAND_PLAYGROUND=yes for an isolated playground")

    base_url = os.getenv(
        "RECOMMAND_CONTRACT_BASE_URL", "https://app.recommand.eu/api/v1"
    ).rstrip("/")
    sender_scheme = os.getenv("RECOMMAND_CONTRACT_SENDER_SCHEME", "0208")
    recipient_scheme = os.getenv("RECOMMAND_CONTRACT_RECIPIENT_SCHEME", "0208")
    headers = {"Authorization": aiohttp.encode_basic_auth(api_key, api_secret)}
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{base_url}/playgrounds/current", headers=headers
        ) as response:
            assert response.status == 200
            playground = await response.json()
        assert playground["playground"]["isPlayground"] is True

        adapter = RecommandPeppolAdapter(
            RecommandConfiguration.parse(
                {
                    "base_url": base_url,
                    "api_key": api_key,
                    "api_secret": api_secret,
                    "webhook_secret": webhook_secret,
                    "companies": [
                        {
                            "company_id": company_id,
                            "peppol_ids": [
                                {"scheme": sender_scheme, "identifier": sender_id}
                            ],
                        }
                    ],
                }
            ),
            session,
        )
        client = adapter.bind_sender(PeppolParticipant(sender_scheme, sender_id))

        content = (FIXTURES / "invoice-profile-01-valid.xml").read_bytes()
        content = content.replace(
            b'"0208">0123456789', f'"{sender_scheme}">{sender_id}'.encode()
        )
        content = content.replace(
            b'"0088">1234567890123',
            f'"{recipient_scheme}">{recipient_id}'.encode(),
        )
        document = DocumentSourceResult.parse(
            id=uuid4(), content_type="application/xml", data=content
        )
        descriptor = PeppolDocumentInspector().inspect(document)
        assert (
            await client.lookup_participant(descriptor.receiver, descriptor)
        ).registered

        sent = await client.submit_document(document.data, descriptor)
        assert sent.id
        assert sent.delivery_state.value == "delivery_confirmed"
