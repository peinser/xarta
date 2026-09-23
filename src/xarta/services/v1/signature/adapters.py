from __future__ import annotations

import asyncio
import datetime

from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field
from io import BytesIO
from typing import TYPE_CHECKING
from typing import Any
from typing import Protocol

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.dsa import DSAPrivateKey
from cryptography.hazmat.primitives.asymmetric.ec import ECDSA
from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurvePrivateKey
from cryptography.hazmat.primitives.asymmetric.ed448 import Ed448PrivateKey
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.mldsa import MLDSA44PrivateKey
from cryptography.hazmat.primitives.asymmetric.mldsa import MLDSA65PrivateKey
from cryptography.hazmat.primitives.asymmetric.mldsa import MLDSA87PrivateKey
from cryptography.hazmat.primitives.asymmetric.padding import PKCS1v15
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
from cryptography.x509 import BasicConstraints
from cryptography.x509 import ExtendedKeyUsage
from cryptography.x509 import KeyUsage
from cryptography.x509 import load_der_x509_certificate
from cryptography.x509.extensions import ExtensionNotFound
from cryptography.x509.oid import ExtendedKeyUsageOID
from pyhanko.keys import load_cert_from_pemder
from pyhanko.keys import load_certs_from_pemder
from pyhanko.keys import load_private_key_from_pemder
from pyhanko.pdf_utils.reader import PdfFileReader
from pyhanko.sign.fields import SigSeedSubFilter
from pyhanko.sign.signers import Signer
from pyhanko.sign.timestamps import HTTPTimeStamper
from pyhanko.sign.timestamps import TimeStamper
from pyhanko.sign.validation import validate_pdf_signature
from pyhanko.sign.validation.status import SignatureCoverageLevel
from pyhanko_certvalidator import ValidationContext
from pyhanko_certvalidator.registry import SimpleCertificateStore
from pyhanko_certvalidator.util import get_pyca_cryptography_hash
from pyhanko_certvalidator.util import process_pss_params

from xarta.adapters import AdapterRegistry
from xarta.crypto.sign.configuration import SignatureConfiguration
from xarta.crypto.sign.configuration import default_signature_configuration
from xarta.crypto.sign.pdf import sign
from xarta.crypto.sign.policy import PdfSignatureProfile
from xarta.crypto.sign.policy import SignaturePolicy
from xarta.crypto.sign.policy import SignaturePolicyRegistry
from xarta.crypto.sign.timestamp import TimestampProviderConfiguration


class SignatureAdapter(Protocol):
    async def sign(self, data: bytes, policy: SignaturePolicy) -> bytes: ...

    async def validates(
        self, signed: bytes, source: bytes, policy: SignaturePolicy
    ) -> bool: ...


class PdfSigningService(Protocol):
    async def sign(self, data: bytes, *, policy: SignaturePolicy) -> bytes: ...


class PdfValidationService(Protocol):
    async def matches_existing_output(
        self,
        *,
        source: bytes,
        signed: bytes,
        policy: SignaturePolicy,
    ) -> bool: ...


class LocalSigner(Signer):
    """pyHanko CMS signer that retains the parsed private key."""

    def __init__(
        self,
        private_pem: str,
        certificate_pem: str,
        chain_pem: str | None = None,
    ) -> None:
        signing_key = load_private_key_from_pemder(private_pem, passphrase=None)
        signing_cert = load_cert_from_pemder(certificate_pem)
        private_key = serialization.load_der_private_key(
            signing_key.dump(), password=None
        )
        if not isinstance(
            private_key,
            (
                RSAPrivateKey,
                DSAPrivateKey,
                EllipticCurvePrivateKey,
                Ed25519PrivateKey,
                Ed448PrivateKey,
                MLDSA44PrivateKey,
                MLDSA65PrivateKey,
                MLDSA87PrivateKey,
            ),
        ):
            raise ValueError("Unsupported document signing private key type")
        public_key = private_key.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        if public_key != signing_cert.public_key.dump():
            raise ValueError("Document signing private key does not match certificate")
        if signing_cert.ca:
            raise ValueError(
                "Document signing certificate must not be a CA certificate"
            )

        intermediate_certificates = (
            tuple(load_certs_from_pemder([chain_pem])) if chain_pem else ()
        )
        certificate_dumps = [
            certificate.dump() for certificate in intermediate_certificates
        ]
        if len(certificate_dumps) != len(set(certificate_dumps)):
            raise ValueError("Document signing certificate chain contains duplicates")
        if signing_cert.dump() in certificate_dumps:
            raise ValueError(
                "Document signing certificate chain contains the leaf certificate"
            )
        self._validate_certificate_chain(signing_cert, intermediate_certificates)

        certificate_store = SimpleCertificateStore.from_certs(intermediate_certificates)
        super().__init__(
            signing_cert=signing_cert,
            cert_registry=certificate_store,
            embed_roots=False,
        )
        self._private_key = private_key

    @staticmethod
    def _validate_certificate_chain(signing_cert, intermediate_certificates) -> None:
        if not intermediate_certificates:
            return
        current = load_der_x509_certificate(signing_cert.dump())
        remaining = [
            load_der_x509_certificate(certificate.dump())
            for certificate in intermediate_certificates
        ]
        now = datetime.datetime.now(datetime.UTC)
        while remaining:
            issuer = next(
                (
                    certificate
                    for certificate in remaining
                    if certificate.subject == current.issuer
                ),
                None,
            )
            if issuer is None or issuer.subject == issuer.issuer:
                raise ValueError(
                    "Document signing certificate chain is not constructible"
                )
            current.verify_directly_issued_by(issuer)
            if not issuer.extensions.get_extension_for_class(BasicConstraints).value.ca:
                raise ValueError("Document signing certificate issuer is not a CA")
            try:
                key_usage = issuer.extensions.get_extension_for_class(KeyUsage).value
            except ExtensionNotFound as ex:
                raise ValueError(
                    "Document signing certificate issuer requires key usage"
                ) from ex
            if not key_usage.key_cert_sign:
                raise ValueError(
                    "Document signing certificate issuer cannot sign certificates"
                )
            if not issuer.not_valid_before_utc <= now <= issuer.not_valid_after_utc:
                raise ValueError("Document signing certificate issuer is not valid")
            remaining.remove(issuer)
            current = issuer

    async def async_sign_raw(
        self, data: bytes, digest_algorithm: str, dry_run: bool = False
    ) -> bytes:
        signature_mechanism = self.get_signature_mechanism_for_digest(digest_algorithm)
        try:
            mechanism = signature_mechanism.signature_algo
        except ValueError:
            mechanism = signature_mechanism["algorithm"].native

        private_key = self._private_key
        if (
            dry_run
            and isinstance(private_key, RSAPrivateKey)
            and mechanism
            in {
                "rsassa_pkcs1v15",
                "rsassa_pss",
            }
        ):
            return bytes(private_key.key_size // 8)
        if mechanism == "rsassa_pkcs1v15" and isinstance(private_key, RSAPrivateKey):
            return private_key.sign(
                data, PKCS1v15(), get_pyca_cryptography_hash(digest_algorithm)
            )
        if mechanism == "rsassa_pss" and isinstance(private_key, RSAPrivateKey):
            padding, hash_algorithm = process_pss_params(
                signature_mechanism["parameters"]
            )
            return private_key.sign(data, padding, hash_algorithm)
        if mechanism == "ecdsa" and isinstance(private_key, EllipticCurvePrivateKey):
            return private_key.sign(
                data, ECDSA(get_pyca_cryptography_hash(digest_algorithm))
            )
        if mechanism == "dsa" and isinstance(private_key, DSAPrivateKey):
            return private_key.sign(data, get_pyca_cryptography_hash(digest_algorithm))
        if mechanism in {
            "ed25519",
            "ed448",
            "mldsa44",
            "mldsa65",
            "mldsa87",
        } and isinstance(
            private_key,
            (
                Ed25519PrivateKey,
                Ed448PrivateKey,
                MLDSA44PrivateKey,
                MLDSA65PrivateKey,
                MLDSA87PrivateKey,
            ),
        ):
            return private_key.sign(data)
        raise ValueError(
            f"Document signing key does not support signature mechanism {mechanism!r}"
        )


@dataclass(frozen=True)
class TimestampProvider:
    id: str
    timestamper: TimeStamper
    signing_certificate: Any
    trust_roots: tuple[Any, ...]
    other_certificates: tuple[Any, ...]

    def validation_context(self) -> ValidationContext:
        return ValidationContext(
            trust_roots=self.trust_roots,
            other_certs=self.other_certificates,
            allow_fetching=False,
        )


@dataclass(frozen=True)
class LocalSignatureAdapter:
    signer: Signer
    timestamp_providers: Mapping[str, TimestampProvider] = field(default_factory=dict)

    async def sign(self, data: bytes, policy: SignaturePolicy) -> bytes:
        # Keep synchronous pyHanko work off the Sanic loop so JetStream
        # heartbeats and other requests remain responsive during signing.
        return await asyncio.to_thread(self._sign, data, policy)

    def _sign(self, data: bytes, policy: SignaturePolicy) -> bytes:
        timestamp_provider = self._timestamp_provider(policy)
        timestamper = (
            timestamp_provider.timestamper if timestamp_provider is not None else None
        )
        if timestamper is None:
            signed = sign(data, self.signer, policy)
        else:
            signed = sign(data, self.signer, policy, timestamper)
        if policy.profile is PdfSignatureProfile.PADES_B_T and not self._validates(
            signed, data, policy
        ):
            raise RuntimeError("RFC 3161 signature timestamp validation failed")
        return signed

    async def validates(
        self, signed: bytes, source: bytes, policy: SignaturePolicy
    ) -> bool:
        return await asyncio.to_thread(self._validates, signed, source, policy)

    def _validates(self, signed: bytes, source: bytes, policy: SignaturePolicy) -> bool:
        if not signed.startswith(source) or self.signer.signing_cert is None:
            return False
        try:
            reader = PdfFileReader(BytesIO(signed))
            if not reader.embedded_signatures:
                return False
            embedded_signature = reader.embedded_signatures[-1]
            expected_subfilter = (
                SigSeedSubFilter.PADES.value
                if policy.profile
                in {PdfSignatureProfile.PADES_B_B, PdfSignatureProfile.PADES_B_T}
                else SigSeedSubFilter.ADOBE_PKCS7_DETACHED.value
            )
            if embedded_signature.sig_object["/SubFilter"] != expected_subfilter:
                return False
            digest_algorithm = embedded_signature.signer_info["digest_algorithm"][
                "algorithm"
            ].native
            if (
                policy.digest_algorithm is not None
                and digest_algorithm != policy.digest_algorithm
            ):
                return False
            if "/DSS" in reader.root:
                return False
            timestamp_provider = self._timestamp_provider(policy)
            status = validate_pdf_signature(
                embedded_signature,
                signer_validation_context=ValidationContext(
                    trust_roots=[self.signer.signing_cert], allow_fetching=False
                ),
                ts_validation_context=(
                    timestamp_provider.validation_context()
                    if timestamp_provider is not None
                    else None
                ),
            )
        except Exception:
            return False
        signature_valid = bool(
            status.intact
            and status.valid
            and status.trusted
            and status.coverage is SignatureCoverageLevel.ENTIRE_FILE
            and status.signing_cert.dump() == self.signer.signing_cert.dump()
        )
        if not signature_valid:
            return False
        timestamp_status = status.timestamp_validity
        if policy.profile is PdfSignatureProfile.PADES_B_B:
            return timestamp_status is None
        if policy.profile is PdfSignatureProfile.PADES_B_T:
            if timestamp_provider is None or timestamp_status is None:
                return False
            return bool(
                timestamp_status.intact
                and timestamp_status.valid
                and timestamp_status.trusted
                and timestamp_status.signing_cert.dump()
                == timestamp_provider.signing_certificate.dump()
            )
        return timestamp_status is None

    def _timestamp_provider(self, policy: SignaturePolicy) -> TimestampProvider | None:
        if policy.profile is not PdfSignatureProfile.PADES_B_T:
            return None
        provider_id = policy.timestamp_provider
        if provider_id is None:
            raise ValueError(
                f"Signature policy {policy.id!r} has no timestamp provider"
            )
        try:
            return self.timestamp_providers[provider_id]
        except KeyError as ex:
            raise ValueError(
                f"Signature policy {policy.id!r} references unknown timestamp provider"
            ) from ex


class LocalSignatureAdapterFactory:
    def validate(self, configuration: Mapping[str, Any]) -> None:
        for key in ("private_pem", "public_pem"):
            if not isinstance(configuration.get(key), str) or not configuration[key]:
                raise ValueError(f"Local signature adapter requires {key!r}")

    def create(self, configuration: Mapping[str, Any]) -> SignatureAdapter:
        try:
            signer = LocalSigner(
                configuration["private_pem"],
                configuration["public_pem"],
                configuration.get("chain_pem"),
            )
        except Exception as ex:
            raise ValueError(
                "Local signature adapter could not load its signer"
            ) from ex
        return LocalSignatureAdapter(
            signer=signer,
            timestamp_providers=configuration.get("timestamp_providers", {}),
        )


SIGNATURE_ADAPTERS = AdapterRegistry[SignatureAdapter](
    {"local": LocalSignatureAdapterFactory()}
)


@dataclass(frozen=True)
class PolicyDrivenPdfService:
    adapter: SignatureAdapter
    signer_identity_id: str

    async def sign(self, data: bytes, *, policy: SignaturePolicy) -> bytes:
        self._require_signer_identity(policy)
        return await self.adapter.sign(data, policy)

    async def matches_existing_output(
        self,
        *,
        source: bytes,
        signed: bytes,
        policy: SignaturePolicy,
    ) -> bool:
        self._require_signer_identity(policy)
        return await self.adapter.validates(signed, source, policy)

    def _require_signer_identity(self, policy: SignaturePolicy) -> None:
        if policy.signer_identity != self.signer_identity_id:
            raise ValueError(f"PDF profile {policy.profile.value!r} is not implemented")


@dataclass(frozen=True)
class SignatureComponents:
    policies: SignaturePolicyRegistry
    default_policy_id: str
    allow_development_policies: bool
    pdf_signer: PdfSigningService
    pdf_validator: PdfValidationService


def create_signature_components(
    configuration: Mapping[str, Any],
    registry: AdapterRegistry[SignatureAdapter] = SIGNATURE_ADAPTERS,
) -> SignatureComponents:
    adapter_name = configuration.get("adapter", "local")
    if not isinstance(adapter_name, str) or not adapter_name:
        raise ValueError("Signature adapter must be a non-empty string")
    signature_configuration = configuration.get(
        "signature_configuration", default_signature_configuration()
    )
    if not isinstance(signature_configuration, SignatureConfiguration):
        raise ValueError("Signature configuration is invalid")
    default_policy = configuration.get(
        "default_policy", signature_configuration.default_policy_id
    )
    if not isinstance(default_policy, str) or not default_policy:
        raise ValueError("Signature default policy must be a non-empty string")
    allow_development_policies = configuration.get("allow_development_policies", True)
    if not isinstance(allow_development_policies, bool):
        raise ValueError("Signature development-policy setting must be boolean")

    policies = signature_configuration.policies
    unsupported_profiles = {
        policy.profile
        for policy in policies.values()
        if policy.profile
        not in {
            PdfSignatureProfile.LEGACY_PDF_CMS,
            PdfSignatureProfile.PADES_B_B,
            PdfSignatureProfile.PADES_B_T,
        }
    }
    if unsupported_profiles:
        formatted_profiles = ", ".join(
            sorted(profile.value for profile in unsupported_profiles)
        )
        raise ValueError(
            f"Unsupported configured signature profiles: {formatted_profiles}"
        )
    policies.resolve(
        default_policy,
        default_policy=default_policy,
        allow_development_policies=allow_development_policies,
    )
    configured_identities = tuple(signature_configuration.signer_identities.values())
    if len(configured_identities) != 1:
        raise ValueError("The local signature service requires one signer identity")
    signer_identity = configured_identities[0]
    if signer_identity.provider != "local-pem":
        raise ValueError(f"Unsupported signer provider {signer_identity.provider!r}")
    timestamp_provider_configurations = configuration.get(
        "timestamp_provider_configurations", {}
    )
    if not isinstance(timestamp_provider_configurations, Mapping):
        raise ValueError("Timestamp provider configuration is invalid")
    timestamp_providers = _create_timestamp_providers(timestamp_provider_configurations)
    for policy in policies.values():
        if (
            policy.profile is PdfSignatureProfile.PADES_B_T
            and policy.timestamp_provider not in timestamp_providers
        ):
            raise ValueError(
                f"Signature policy {policy.id!r} references unknown timestamp provider"
            )
    adapter_configuration = dict(configuration)
    adapter_configuration["timestamp_providers"] = timestamp_providers
    adapter = registry.create(adapter_name, adapter_configuration)
    pdf_service = PolicyDrivenPdfService(adapter, signer_identity.id)
    return SignatureComponents(
        policies=policies,
        default_policy_id=default_policy,
        allow_development_policies=allow_development_policies,
        pdf_signer=pdf_service,
        pdf_validator=pdf_service,
    )


def _create_timestamp_providers(
    configurations: Mapping[str, TimestampProviderConfiguration],
) -> dict[str, TimestampProvider]:
    providers = {}
    for provider_id, configuration in configurations.items():
        if not isinstance(configuration, TimestampProviderConfiguration):
            raise ValueError("Timestamp provider configuration is invalid")
        try:
            signing_certificate = load_cert_from_pemder(configuration.certificate_pem)
            trust_roots = tuple(load_certs_from_pemder([configuration.trust_root_pem]))
            if not trust_roots:
                raise ValueError("Timestamp provider requires a trust root")
            other_certificates = (
                tuple(load_certs_from_pemder([configuration.chain_pem]))
                if configuration.chain_pem
                else ()
            )
            _validate_timestamp_certificate(signing_certificate)
            _validate_timestamp_certificate_chain(
                signing_certificate, other_certificates, trust_roots
            )
        except Exception as ex:
            raise ValueError(
                f"Timestamp provider {provider_id!r} trust material is invalid"
            ) from ex
        providers[provider_id] = TimestampProvider(
            id=provider_id,
            timestamper=HTTPTimeStamper(
                configuration.endpoint,
                https=configuration.endpoint.startswith("https://"),
                timeout=configuration.timeout_seconds,
            ),
            signing_certificate=signing_certificate,
            trust_roots=trust_roots,
            other_certificates=other_certificates,
        )
    return providers


def _validate_timestamp_certificate(certificate) -> None:
    parsed = load_der_x509_certificate(certificate.dump())
    now = datetime.datetime.now(datetime.UTC)
    if not parsed.not_valid_before_utc <= now <= parsed.not_valid_after_utc:
        raise ValueError("Timestamp signing certificate is not valid")
    if parsed.extensions.get_extension_for_class(BasicConstraints).value.ca:
        raise ValueError("Timestamp signing certificate must not be a CA")
    extension = parsed.extensions.get_extension_for_class(ExtendedKeyUsage)
    if not extension.critical or set(extension.value) != {
        ExtendedKeyUsageOID.TIME_STAMPING
    }:
        raise ValueError(
            "Timestamp signing certificate requires critical timeStamping EKU"
        )
    key_usage = parsed.extensions.get_extension_for_class(KeyUsage).value
    if not (key_usage.digital_signature or key_usage.content_commitment):
        raise ValueError("Timestamp signing certificate cannot sign timestamps")


def _validate_timestamp_certificate_chain(
    signing_certificate, other_certificates, trust_roots
) -> None:
    current = load_der_x509_certificate(signing_certificate.dump())
    intermediates = [
        load_der_x509_certificate(certificate.dump())
        for certificate in other_certificates
    ]
    roots = [
        load_der_x509_certificate(certificate.dump()) for certificate in trust_roots
    ]
    root_fingerprints = {
        root.public_bytes(serialization.Encoding.DER) for root in roots
    }
    now = datetime.datetime.now(datetime.UTC)
    visited = set()
    while current.public_bytes(serialization.Encoding.DER) not in root_fingerprints:
        fingerprint = current.public_bytes(serialization.Encoding.DER)
        if fingerprint in visited:
            raise ValueError("Timestamp certificate chain contains a cycle")
        visited.add(fingerprint)
        issuer = next(
            (
                certificate
                for certificate in (*intermediates, *roots)
                if certificate.subject == current.issuer
            ),
            None,
        )
        if issuer is None:
            raise ValueError("Timestamp certificate chain is not trusted")
        current.verify_directly_issued_by(issuer)
        if not issuer.extensions.get_extension_for_class(BasicConstraints).value.ca:
            raise ValueError("Timestamp certificate issuer is not a CA")
        key_usage = issuer.extensions.get_extension_for_class(KeyUsage).value
        if not key_usage.key_cert_sign:
            raise ValueError("Timestamp certificate issuer cannot sign certificates")
        if not issuer.not_valid_before_utc <= now <= issuer.not_valid_after_utc:
            raise ValueError("Timestamp certificate issuer is not valid")
        current = issuer
