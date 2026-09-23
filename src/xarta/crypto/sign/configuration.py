from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

import orjson

from .policy import ELECTRONIC_SEAL_V1
from .policy import PdfSignatureProfile
from .policy import SignaturePolicy
from .policy import SignaturePolicyConfigurationError
from .policy import SignaturePolicyRegistry
from .policy import UnknownSignaturePolicy
from .policy import validate_policy_id

_POLICY_FIELDS = frozenset(
    {
        "profile",
        "signer_identity",
        "digest_algorithm",
        "timestamp_provider",
        "trust_policy",
        "development_only",
    }
)
_MAX_CONFIGURATION_BYTES = 1024 * 1024


@dataclass(frozen=True)
class SigningIdentityConfiguration:
    id: str
    provider: str


@dataclass(frozen=True)
class SignatureConfiguration:
    default_policy_id: str
    policies: SignaturePolicyRegistry
    signer_identities: Mapping[str, SigningIdentityConfiguration]


def parse_allow_development_policies(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise SignaturePolicyConfigurationError(
        "SIGNATURE_ALLOW_DEVELOPMENT_POLICIES must be true or false"
    )


def default_signature_configuration() -> SignatureConfiguration:
    identity = SigningIdentityConfiguration(
        id=ELECTRONIC_SEAL_V1.signer_identity,
        provider="local-pem",
    )
    return SignatureConfiguration(
        default_policy_id=ELECTRONIC_SEAL_V1.id,
        policies=SignaturePolicyRegistry((ELECTRONIC_SEAL_V1,)),
        signer_identities=MappingProxyType({identity.id: identity}),
    )


def load_signature_configuration(path: str) -> SignatureConfiguration:
    try:
        raw = Path(path).read_bytes()
        if len(raw) > _MAX_CONFIGURATION_BYTES:
            raise SignaturePolicyConfigurationError(
                "Signature configuration exceeds its size limit"
            )
        value = orjson.loads(raw)
    except (OSError, orjson.JSONDecodeError) as ex:
        raise SignaturePolicyConfigurationError(
            "Signature configuration is not valid JSON"
        ) from ex
    return parse_signature_configuration(value)


def parse_signature_configuration(value) -> SignatureConfiguration:
    required_fields = {
        "schema_version",
        "default_policy",
        "signer_identities",
        "policies",
    }
    if not isinstance(value, Mapping) or set(value) != required_fields:
        raise SignaturePolicyConfigurationError(
            "Signature configuration requires schema_version, default_policy, "
            "signer_identities and policies"
        )
    if isinstance(value["schema_version"], bool) or value["schema_version"] != 1:
        raise SignaturePolicyConfigurationError(
            "Unsupported signature configuration schema_version"
        )

    identities = _parse_signer_identities(value["signer_identities"])
    policies = _parse_policies(value["policies"], identities)
    try:
        default_policy_id = validate_policy_id(value["default_policy"])
        policies.get(default_policy_id)
    except (TypeError, UnknownSignaturePolicy, ValueError) as ex:
        raise SignaturePolicyConfigurationError(
            "Signature default policy is invalid or unavailable"
        ) from ex

    return SignatureConfiguration(
        default_policy_id=default_policy_id,
        policies=policies,
        signer_identities=MappingProxyType(identities),
    )


def _parse_signer_identities(value) -> dict[str, SigningIdentityConfiguration]:
    if not isinstance(value, Mapping) or not value:
        raise SignaturePolicyConfigurationError(
            "Signature signer_identities must be a non-empty object"
        )
    identities = {}
    for identity_id, definition in value.items():
        try:
            identity_id = validate_policy_id(identity_id)
        except (TypeError, ValueError) as ex:
            raise SignaturePolicyConfigurationError(
                "Signature signer identity ID is invalid"
            ) from ex
        if not isinstance(definition, Mapping) or set(definition) != {"provider"}:
            raise SignaturePolicyConfigurationError(
                f"Signature signer identity {identity_id!r} requires provider"
            )
        try:
            provider = validate_policy_id(definition["provider"])
        except (TypeError, ValueError) as ex:
            raise SignaturePolicyConfigurationError(
                f"Signature signer identity {identity_id!r} provider is invalid"
            ) from ex
        identities[identity_id] = SigningIdentityConfiguration(identity_id, provider)
    return identities


def _parse_policies(
    value,
    identities: Mapping[str, SigningIdentityConfiguration],
) -> SignaturePolicyRegistry:
    if not isinstance(value, Mapping) or not value:
        raise SignaturePolicyConfigurationError(
            "Signature policies must be a non-empty object"
        )
    policies = []
    for policy_id, definition in value.items():
        try:
            policy_id = validate_policy_id(policy_id)
        except (TypeError, ValueError) as ex:
            raise SignaturePolicyConfigurationError(
                "Signature policy ID is invalid"
            ) from ex
        if not isinstance(definition, Mapping):
            raise SignaturePolicyConfigurationError(
                f"Signature policy {policy_id!r} must be an object"
            )
        unknown = set(definition) - _POLICY_FIELDS
        required = {"profile", "signer_identity"}
        if unknown or not required <= set(definition):
            raise SignaturePolicyConfigurationError(
                f"Signature policy {policy_id!r} has invalid fields"
            )
        signer_identity = definition["signer_identity"]
        if signer_identity not in identities:
            raise SignaturePolicyConfigurationError(
                f"Signature policy {policy_id!r} references unknown signer identity"
            )
        development_only = definition.get("development_only", False)
        if not isinstance(development_only, bool):
            raise SignaturePolicyConfigurationError(
                f"Signature policy {policy_id!r} development_only must be boolean"
            )
        try:
            profile = PdfSignatureProfile(definition["profile"])
        except (TypeError, ValueError) as ex:
            raise SignaturePolicyConfigurationError(
                f"Signature policy {policy_id!r} profile is invalid"
            ) from ex
        policies.append(
            SignaturePolicy(
                id=policy_id,
                profile=profile,
                signer_identity=signer_identity,
                digest_algorithm=definition.get("digest_algorithm"),
                timestamp_provider=definition.get("timestamp_provider"),
                trust_policy=definition.get("trust_policy"),
                development_only=development_only,
            )
        )
    return SignaturePolicyRegistry(policies)
