from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from eth_utils import is_address

SUPPORTED_NETWORKS = frozenset({"eip155:84532", "eip155:8453"})


@dataclass(frozen=True, slots=True)
class X402Configuration:
    enabled: bool
    intake_enabled: bool
    preview_enabled: bool
    facilitator_url: str | None
    network: str | None
    pay_to: str | None
    payment_timeout_seconds: int | None
    intake_resource_url: str | None
    preview_resource_url: str | None
    challenge_signing_key: str | None

    def protects(self, resource: str) -> bool:
        return (
            self.enabled
            and {
                "intake": self.intake_enabled,
                "preview": self.preview_enabled,
            }[resource]
        )


def parse_x402_configuration(value: dict) -> X402Configuration:
    enabled = _boolean(value, "enabled", False)
    intake_enabled = _boolean(value, "intake_enabled", False)
    preview_enabled = _boolean(value, "preview_enabled", False)
    if not enabled:
        return X402Configuration(
            False,
            intake_enabled,
            preview_enabled,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
        )
    facilitator_url = _url(value, "facilitator_url")
    intake_resource_url = _url(value, "intake_resource_url") if intake_enabled else None
    preview_resource_url = (
        _url(value, "preview_resource_url") if preview_enabled else None
    )
    network = value.get("network")
    if network not in SUPPORTED_NETWORKS:
        raise ValueError("x402 network must be eip155:84532 or eip155:8453")
    pay_to = value.get("pay_to")
    if not isinstance(pay_to, str) or not is_address(pay_to):
        raise ValueError("x402 pay_to must be an EVM address")
    if network == "eip155:8453" and facilitator_url == "https://x402.org/facilitator":
        raise ValueError(
            "The development facilitator must not be used for Base mainnet"
        )
    return X402Configuration(
        enabled=enabled,
        intake_enabled=intake_enabled,
        preview_enabled=preview_enabled,
        facilitator_url=facilitator_url,
        network=network,
        pay_to=pay_to,
        payment_timeout_seconds=_positive_integer(value, "payment_timeout_seconds"),
        intake_resource_url=intake_resource_url,
        preview_resource_url=preview_resource_url,
        challenge_signing_key=_signing_key(value),
    )


def _boolean(value: dict, name: str, default: bool) -> bool:
    result = value.get(name, default)
    if not isinstance(result, bool):
        raise ValueError(f"x402 {name} must be a boolean")
    return result


def _positive_integer(value: dict, name: str) -> int:
    result = value.get(name)
    if isinstance(result, bool) or not isinstance(result, int) or result <= 0:
        raise ValueError(f"x402 {name} must be a positive integer")
    return result


def _signing_key(value: dict) -> str:
    result = value.get("challenge_signing_key")
    if not isinstance(result, str) or len(result.encode()) < 32:
        raise ValueError("x402 challenge_signing_key must contain at least 32 bytes")
    return result


def _url(value: dict, name: str) -> str:
    result = value.get(name)
    if not isinstance(result, str):
        raise ValueError(f"x402 {name} must be an HTTPS URL")
    parsed = urlsplit(result)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username
        or parsed.password
    ):
        raise ValueError(f"x402 {name} must be an HTTPS URL without credentials")
    return result
