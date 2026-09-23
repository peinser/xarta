from __future__ import annotations

import ipaddress

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from urllib.parse import urlsplit

import orjson

from xarta.crypto.sign.policy import SignaturePolicyConfigurationError
from xarta.crypto.sign.policy import validate_policy_id

_MAX_CONFIGURATION_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class TimestampProviderConfiguration:
    id: str
    provider: str
    endpoint: str
    timeout_seconds: float
    certificate_pem: str
    trust_root_pem: str
    chain_pem: str | None


def load_timestamp_provider_configurations(
    path: str,
) -> Mapping[str, TimestampProviderConfiguration]:
    try:
        raw = Path(path).read_bytes()
        if len(raw) > _MAX_CONFIGURATION_BYTES:
            raise SignaturePolicyConfigurationError(
                "Timestamp provider configuration exceeds its size limit"
            )
        value = orjson.loads(raw)
    except (OSError, orjson.JSONDecodeError) as ex:
        raise SignaturePolicyConfigurationError(
            "Timestamp provider configuration is not valid JSON"
        ) from ex
    return parse_timestamp_provider_configurations(value)


def parse_timestamp_provider_configurations(
    value: object,
) -> Mapping[str, TimestampProviderConfiguration]:
    if not isinstance(value, Mapping) or set(value) != {"providers"}:
        raise SignaturePolicyConfigurationError(
            "Timestamp provider configuration requires providers"
        )
    raw_providers = value["providers"]
    if not isinstance(raw_providers, Mapping) or not raw_providers:
        raise SignaturePolicyConfigurationError(
            "Timestamp providers must be a non-empty object"
        )
    providers = {}
    for provider_id, definition in raw_providers.items():
        try:
            provider_id = validate_policy_id(provider_id)
        except (TypeError, ValueError) as ex:
            raise SignaturePolicyConfigurationError(
                "Timestamp provider ID is invalid"
            ) from ex
        providers[provider_id] = _parse_provider(provider_id, definition)
    return MappingProxyType(providers)


def _parse_provider(
    provider_id: str, definition: object
) -> TimestampProviderConfiguration:
    required = {
        "provider",
        "endpoint",
        "timeout_seconds",
        "certificate_pem",
        "trust_root_pem",
    }
    optional = {"chain_pem"}
    if not isinstance(definition, Mapping) or not required <= set(definition):
        raise SignaturePolicyConfigurationError(
            f"Timestamp provider {provider_id!r} is incomplete"
        )
    if set(definition) - required - optional:
        raise SignaturePolicyConfigurationError(
            f"Timestamp provider {provider_id!r} has invalid fields"
        )
    provider = definition["provider"]
    if provider != "http-rfc3161":
        raise SignaturePolicyConfigurationError(
            f"Timestamp provider {provider_id!r} has unsupported provider"
        )
    endpoint = definition["endpoint"]
    if not isinstance(endpoint, str):
        raise SignaturePolicyConfigurationError(
            f"Timestamp provider {provider_id!r} endpoint is invalid"
        )
    parsed_endpoint = urlsplit(endpoint)
    if (
        parsed_endpoint.scheme not in {"http", "https"}
        or not parsed_endpoint.hostname
        or parsed_endpoint.username
        or parsed_endpoint.password
        or parsed_endpoint.fragment
    ):
        raise SignaturePolicyConfigurationError(
            f"Timestamp provider {provider_id!r} endpoint is invalid"
        )
    if parsed_endpoint.scheme == "http" and not _is_loopback(parsed_endpoint.hostname):
        raise SignaturePolicyConfigurationError(
            f"Timestamp provider {provider_id!r} requires HTTPS outside loopback"
        )
    timeout = definition["timeout_seconds"]
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, int | float)
        or timeout <= 0
        or timeout > 60
    ):
        raise SignaturePolicyConfigurationError(
            f"Timestamp provider {provider_id!r} timeout must be between 0 and 60 seconds"
        )
    paths = {}
    for field in ("certificate_pem", "trust_root_pem", "chain_pem"):
        path = definition.get(field)
        if field == "chain_pem" and path is None:
            paths[field] = None
            continue
        if not isinstance(path, str) or not path:
            raise SignaturePolicyConfigurationError(
                f"Timestamp provider {provider_id!r} {field} is invalid"
            )
        paths[field] = path
    return TimestampProviderConfiguration(
        id=provider_id,
        provider=provider,
        endpoint=endpoint,
        timeout_seconds=float(timeout),
        certificate_pem=paths["certificate_pem"],
        trust_root_pem=paths["trust_root_pem"],
        chain_pem=paths["chain_pem"],
    )


def _is_loopback(hostname: str) -> bool:
    if hostname == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False
