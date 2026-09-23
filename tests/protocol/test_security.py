from __future__ import annotations

import pytest

from xarta.protocol.dag.webhook import WebhookNode
from xarta.protocol.document.request.bundle import DocumentBundleRequest


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/admin",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]/admin",
    ],
)
async def test_webhooks_reject_non_public_literal_addresses(url: str) -> None:
    node = WebhookNode(url=url)

    with pytest.raises(ValueError, match="public addresses"):
        await node._validate_destination()


@pytest.mark.parametrize(
    "filename",
    ["../secret", "/absolute", "directory/file", "directory\\file", "bad\nname"],
)
def test_bundles_reject_unsafe_member_names(filename: str) -> None:
    with pytest.raises(ValueError):
        DocumentBundleRequest.fromdict(
            {
                "documents": [
                    {
                        "id": "00000000-0000-0000-0000-000000000001",
                        "filename": filename,
                    }
                ]
            }
        )
