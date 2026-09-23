from __future__ import annotations

import re

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

_POLICY_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_MAX_POLICY_ID_LENGTH = 128
_PADES_DIGEST_ALGORITHMS = frozenset({"sha256", "sha384", "sha512"})


class SignatureError(Exception):
    """Base class for signature domain failures."""


class UnknownSignaturePolicy(SignatureError):
    """Raised when a requested signature policy is not registered."""


class ForbiddenSignaturePolicy(SignatureError):
    """Raised when a development policy is disabled by deployment policy."""


class SignaturePolicyConfigurationError(SignatureError):
    """Raised when signature policy configuration is inconsistent."""


class ExistingSignatureMismatch(SignatureError):
    """Raised when an immutable output does not match the requested operation."""


class PdfSignatureProfile(StrEnum):
    LEGACY_PDF_CMS = "legacy-pdf-cms"
    PADES_B_B = "pades-b-b"
    PADES_B_T = "pades-b-t"
    PADES_B_LT = "pades-b-lt"
    PADES_B_LTA = "pades-b-lta"


def validate_policy_id(policy_id: str) -> str:
    if not isinstance(policy_id, str):
        raise ValueError("Signature policy must be a string")
    if not policy_id or len(policy_id) > _MAX_POLICY_ID_LENGTH:
        raise ValueError(
            f"Signature policy must contain 1 to {_MAX_POLICY_ID_LENGTH} characters"
        )
    if _POLICY_ID.fullmatch(policy_id) is None:
        raise ValueError("Signature policy contains invalid characters")
    return policy_id


@dataclass(frozen=True)
class SignaturePolicy:
    id: str
    profile: PdfSignatureProfile
    signer_identity: str
    digest_algorithm: str | None
    timestamp_provider: str | None
    trust_policy: str | None
    development_only: bool = False

    def __post_init__(self) -> None:
        try:
            validate_policy_id(self.id)
            validate_policy_id(self.signer_identity)
            if self.timestamp_provider is not None:
                validate_policy_id(self.timestamp_provider)
            if self.trust_policy is not None:
                validate_policy_id(self.trust_policy)
        except ValueError as ex:
            raise SignaturePolicyConfigurationError(str(ex)) from ex

        if self.digest_algorithm is not None and (
            not isinstance(self.digest_algorithm, str) or not self.digest_algorithm
        ):
            raise SignaturePolicyConfigurationError(
                f"Signature policy {self.id!r} digest algorithm is invalid"
            )

        if self.profile is PdfSignatureProfile.LEGACY_PDF_CMS:
            if (
                self.digest_algorithm is not None
                or self.timestamp_provider is not None
                or self.trust_policy is not None
            ):
                raise SignaturePolicyConfigurationError(
                    f"Legacy signature policy {self.id!r} cannot configure a digest, "
                    "timestamping or trust"
                )
            return

        if self.digest_algorithm not in _PADES_DIGEST_ALGORITHMS:
            raise SignaturePolicyConfigurationError(
                f"PAdES signature policy {self.id!r} requires one of "
                f"{', '.join(sorted(_PADES_DIGEST_ALGORITHMS))}"
            )

        if self.profile is PdfSignatureProfile.PADES_B_B:
            if self.timestamp_provider is not None or self.trust_policy is not None:
                raise SignaturePolicyConfigurationError(
                    f"PAdES B-B policy {self.id!r} cannot configure timestamping or trust"
                )
            return

        if self.timestamp_provider is None:
            raise SignaturePolicyConfigurationError(
                f"Signature policy {self.id!r} requires a timestamp provider"
            )

        if (
            self.profile
            in {
                PdfSignatureProfile.PADES_B_LT,
                PdfSignatureProfile.PADES_B_LTA,
            }
            and self.trust_policy is None
        ):
            raise SignaturePolicyConfigurationError(
                f"Signature policy {self.id!r} requires a trust policy"
            )


ELECTRONIC_SEAL_V1 = SignaturePolicy(
    id="electronic-seal-v1",
    profile=PdfSignatureProfile.LEGACY_PDF_CMS,
    signer_identity="local-dev-v1",
    digest_algorithm=None,
    timestamp_provider=None,
    trust_policy=None,
    development_only=True,
)


class SignaturePolicyRegistry:
    def __init__(self, policies: Iterable[SignaturePolicy]) -> None:
        registered: dict[str, SignaturePolicy] = {}
        for policy in policies:
            if policy.id in registered:
                raise SignaturePolicyConfigurationError(
                    f"Duplicate signature policy {policy.id!r}"
                )
            registered[policy.id] = policy
        self._policies = registered

    def get(self, policy_id: str) -> SignaturePolicy:
        try:
            return self._policies[policy_id]
        except KeyError as ex:
            raise UnknownSignaturePolicy(
                f"Unknown signature policy {policy_id!r}"
            ) from ex

    def values(self) -> tuple[SignaturePolicy, ...]:
        return tuple(self._policies.values())

    def resolve(
        self,
        requested_policy: str | None,
        *,
        default_policy: str,
        allow_development_policies: bool,
    ) -> SignaturePolicy:
        policy_id = default_policy if requested_policy is None else requested_policy
        policy = self.get(policy_id)
        if policy.development_only and not allow_development_policies:
            raise ForbiddenSignaturePolicy(
                f"Development signature policy {policy.id!r} is not allowed"
            )
        return policy
