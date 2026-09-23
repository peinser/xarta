from __future__ import annotations

from pathlib import Path

import pytest

from xarta.crypto.sign.configuration import load_signature_configuration
from xarta.crypto.sign.configuration import parse_signature_configuration
from xarta.crypto.sign.policy import PdfSignatureProfile
from xarta.crypto.sign.policy import SignaturePolicyConfigurationError
from xarta.crypto.sign.timestamp import load_timestamp_provider_configurations
from xarta.crypto.sign.timestamp import parse_timestamp_provider_configurations

ROOT = Path(__file__).parents[2]


def configuration() -> dict:
    return {
        "schema_version": 1,
        "default_policy": "electronic-seal-v1",
        "signer_identities": {
            "seal-2026-v1": {"provider": "local-pem"},
        },
        "policies": {
            "electronic-seal-v1": {
                "profile": "legacy-pdf-cms",
                "signer_identity": "seal-2026-v1",
                "development_only": True,
            },
            "pades-b-b-v1": {
                "profile": "pades-b-b",
                "signer_identity": "seal-2026-v1",
                "digest_algorithm": "sha256",
                "development_only": True,
            },
        },
    }


def test_parse_signature_configuration_registers_default_and_pades() -> None:
    parsed = parse_signature_configuration(configuration())

    assert parsed.default_policy_id == "electronic-seal-v1"
    assert parsed.signer_identities["seal-2026-v1"].provider == "local-pem"
    assert parsed.policies.get("pades-b-b-v1").profile is PdfSignatureProfile.PADES_B_B
    assert parsed.policies.get("pades-b-b-v1").digest_algorithm == "sha256"


def test_development_signature_configuration_is_loadable() -> None:
    parsed = load_signature_configuration(str(ROOT / ".dev/conf/signature.json"))

    assert parsed.default_policy_id == "xarta-dev-seal-v1"
    assert parsed.policies.get("pades-b-b-v1").profile is PdfSignatureProfile.PADES_B_B
    assert parsed.policies.get("pades-b-t-v1").profile is PdfSignatureProfile.PADES_B_T
    assert parsed.policies.get("pades-b-t-v1").timestamp_provider == "local-dev-tsa"


def test_development_timestamp_provider_configuration_is_loadable() -> None:
    providers = load_timestamp_provider_configurations(
        str(ROOT / ".dev/conf/timestamp-providers.json")
    )

    assert providers["local-dev-tsa"].provider == "http-rfc3161"
    assert providers["local-dev-tsa"].timeout_seconds == 5


@pytest.mark.parametrize(
    ("change", "message"),
    (
        (lambda value: value.update(schema_version=2), "schema_version"),
        (lambda value: value.update(default_policy="missing"), "default policy"),
        (
            lambda value: value["policies"]["pades-b-b-v1"].update(
                signer_identity="missing"
            ),
            "unknown signer identity",
        ),
        (
            lambda value: value["policies"]["pades-b-b-v1"].update(profile="imaginary"),
            "profile is invalid",
        ),
        (
            lambda value: value["policies"]["pades-b-b-v1"].pop("digest_algorithm"),
            "requires one of",
        ),
        (
            lambda value: value["policies"]["pades-b-b-v1"].update(
                digest_algorithm="sha1"
            ),
            "requires one of",
        ),
    ),
)
def test_signature_configuration_rejects_invalid_semantics(change, message) -> None:
    configured = configuration()
    change(configured)

    with pytest.raises(SignaturePolicyConfigurationError, match=message):
        parse_signature_configuration(configured)


def timestamp_configuration() -> dict:
    return {
        "providers": {
            "test-tsa": {
                "provider": "http-rfc3161",
                "endpoint": "https://tsa.example.test/rfc3161",
                "timeout_seconds": 5,
                "certificate_pem": "/run/config/tsa.pem",
                "trust_root_pem": "/run/config/tsa-root.pem",
            }
        }
    }


@pytest.mark.parametrize(
    ("change", "message"),
    (
        (
            lambda value: value["providers"]["test-tsa"].update(provider="custom"),
            "unsupported provider",
        ),
        (
            lambda value: value["providers"]["test-tsa"].pop("endpoint"),
            "incomplete",
        ),
        (
            lambda value: value["providers"]["test-tsa"].update(
                endpoint="http://tsa.example.test"
            ),
            "requires HTTPS",
        ),
        (
            lambda value: value["providers"]["test-tsa"].update(
                endpoint="https://user:secret@tsa.example.test"
            ),
            "endpoint is invalid",
        ),
        (
            lambda value: value["providers"]["test-tsa"].update(timeout_seconds=0),
            "timeout",
        ),
    ),
)
def test_timestamp_provider_configuration_rejects_invalid_values(
    change, message
) -> None:
    configured = timestamp_configuration()
    change(configured)

    with pytest.raises(SignaturePolicyConfigurationError, match=message):
        parse_timestamp_provider_configurations(configured)
