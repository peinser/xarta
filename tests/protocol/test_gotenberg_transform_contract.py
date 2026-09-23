from __future__ import annotations

import os

from io import BytesIO

import aiohttp
import pytest

from pyhanko.pdf_utils.reader import PdfFileReader

from xarta.services.v1.transform.gotenberg import GotenbergTransformClient

pytestmark = pytest.mark.gotenberg_contract


def endpoint() -> str:
    if os.getenv("GOTENBERG_CONTRACT_ENABLED", "false").lower() != "true":
        pytest.skip(
            "Set GOTENBERG_CONTRACT_ENABLED=true to run the Gotenberg contract tests"
        )
    return os.getenv(
        "GOTENBERG_CONTRACT_ENDPOINT", "http://template-engine-gotenberg:3000"
    ).rstrip("/")


def page_count(document: bytes) -> int:
    return len(PdfFileReader(BytesIO(document), strict=True).root["/Pages"]["/Kids"])


async def test_gotenberg_transform_convert_merge_and_split_contract() -> None:
    async with aiohttp.ClientSession() as session:
        client = GotenbergTransformClient(
            session=session,
            endpoint=endpoint(),
            timeout=30.0,
            max_bytes=10 * 1024 * 1024,
        )
        first = await client.convert(
            b"First transform contract page",
            filename="input.txt",
            content_type="text/plain",
            trace="transform-contract-convert-1",
        )
        second = await client.convert(
            b"Second transform contract page",
            filename="input.txt",
            content_type="text/plain",
            trace="transform-contract-convert-2",
        )
        merged = await client.merge(
            [first, second],
            trace="transform-contract-merge",
        )
        split_first = await client.split(
            merged,
            start=1,
            end=1,
            trace="transform-contract-split-1",
        )
        split_second = await client.split(
            merged,
            start=2,
            end=2,
            trace="transform-contract-split-2",
        )

    assert first.startswith(b"%PDF-")
    assert second.startswith(b"%PDF-")
    assert page_count(merged) == 2
    assert page_count(split_first) == 1
    assert page_count(split_second) == 1
