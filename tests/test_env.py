from __future__ import annotations

import pytest

from xarta import env


def test_extract_reports_missing_required_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("XARTA_TEST_VALUE", raising=False)

    with pytest.raises(env.ConfigurationError, match="XARTA_TEST_VALUE"):
        env.extract("XARTA_TEST_VALUE", optional=False)


def test_extract_reports_invalid_type(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XARTA_TEST_VALUE", "not-an-integer")

    with pytest.raises(env.ConfigurationError, match="valid int"):
        env.extract("XARTA_TEST_VALUE", dtype=int)


def test_extract_converts_valid_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XARTA_TEST_VALUE", "3")

    assert env.extract("XARTA_TEST_VALUE", dtype=int) == 3
