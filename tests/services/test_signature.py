from __future__ import annotations

import asyncio
import datetime
import threading

from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from typing import cast
from uuid import uuid4

import pytest

from asn1crypto import tsp
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID
from cryptography.x509.oid import NameOID
from pyhanko.keys import load_cert_from_pemder
from pyhanko.keys import load_private_key_from_pemder
from pyhanko.pdf_utils import generic
from pyhanko.pdf_utils.reader import PdfFileReader
from pyhanko.pdf_utils.writer import PdfFileWriter
from pyhanko.sign.fields import SigSeedSubFilter
from pyhanko.sign.timestamps import DummyTimeStamper
from pyhanko.sign.timestamps import TimeStamper
from pyhanko.sign.timestamps import TimestampRequestError
from pyhanko.sign.validation import async_validate_pdf_signature
from pyhanko_certvalidator import ValidationContext

import xarta.protocol.dag

from xarta.adapters import UnknownAdapterError
from xarta.crypto.sign.configuration import parse_signature_configuration
from xarta.crypto.sign.policy import ELECTRONIC_SEAL_V1
from xarta.crypto.sign.policy import ExistingSignatureMismatch
from xarta.crypto.sign.policy import ForbiddenSignaturePolicy
from xarta.crypto.sign.policy import PdfSignatureProfile
from xarta.crypto.sign.policy import SignaturePolicy
from xarta.crypto.sign.policy import SignaturePolicyConfigurationError
from xarta.crypto.sign.policy import SignaturePolicyRegistry
from xarta.crypto.sign.policy import UnknownSignaturePolicy
from xarta.crypto.sign.timestamp import TimestampProviderConfiguration
from xarta.exceptions.protocol import PermanentError
from xarta.protocol.dag.signature import SignatureNode
from xarta.protocol.dag.signature.base import SignatureRequest
from xarta.protocol.document.source import DocumentSourceResult
from xarta.protocol.document.type import DocumentTypeIdentifier
from xarta.services.v1.signature import adapters as signature_adapters
from xarta.services.v1.signature import base as signature_service
from xarta.services.v1.signature.adapters import LocalSignatureAdapter
from xarta.services.v1.signature.adapters import LocalSignatureAdapterFactory
from xarta.services.v1.signature.adapters import LocalSigner
from xarta.services.v1.signature.adapters import PolicyDrivenPdfService
from xarta.services.v1.signature.adapters import SignatureComponents
from xarta.services.v1.signature.adapters import TimestampProvider
from xarta.services.v1.signature.adapters import create_signature_components
from xarta.services.v1.signature.nats import _worker


def signing_material(path: Path, name: str) -> tuple[Path, Path, rsa.RSAPrivateKey]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.datetime.now(tz=datetime.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(private_key, hashes.SHA256())
    )
    private_path = path / f"{name}-private.pem"
    certificate_path = path / f"{name}-certificate.pem"
    private_path.write_bytes(
        private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    certificate_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    return private_path, certificate_path, private_key


def components(adapter, **overrides) -> SignatureComponents:
    service = PolicyDrivenPdfService(adapter, ELECTRONIC_SEAL_V1.signer_identity)
    values = {
        "policies": SignaturePolicyRegistry((ELECTRONIC_SEAL_V1,)),
        "default_policy_id": ELECTRONIC_SEAL_V1.id,
        "allow_development_policies": True,
        "pdf_signer": service,
        "pdf_validator": service,
        **overrides,
    }
    return SignatureComponents(**values)


def signing_chain_material(path: Path) -> tuple[Path, Path, Path, Path]:
    now = datetime.datetime.now(tz=datetime.UTC)
    root_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    root_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test Root")])
    root = (
        x509.CertificateBuilder()
        .subject_name(root_name)
        .issuer_name(root_name)
        .public_key(root_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=1), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(root_key, hashes.SHA256())
    )

    intermediate_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    intermediate_name = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "Test Signing CA")]
    )
    intermediate = (
        x509.CertificateBuilder()
        .subject_name(intermediate_name)
        .issuer_name(root_name)
        .public_key(intermediate_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=20))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(root_key, hashes.SHA256())
    )

    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test Seal")]))
        .issuer_name(intermediate_name)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=10))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=True,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(intermediate_key, hashes.SHA256())
    )

    private_path = path / "leaf-private.pem"
    leaf_path = path / "leaf.pem"
    chain_path = path / "chain.pem"
    root_path = path / "root.pem"
    private_path.write_bytes(
        leaf_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    leaf_path.write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    chain_path.write_bytes(intermediate.public_bytes(serialization.Encoding.PEM))
    root_path.write_bytes(root.public_bytes(serialization.Encoding.PEM))
    return private_path, leaf_path, chain_path, root_path


def timestamp_provider(path: Path, name: str = "Test TSA") -> TimestampProvider:
    now = datetime.datetime.now(tz=datetime.UTC)
    root_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    root_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"{name} Root")])
    root = (
        x509.CertificateBuilder()
        .subject_name(root_name)
        .issuer_name(root_name)
        .public_key(root_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(root_key, hashes.SHA256())
    )
    tsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    tsa = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)]))
        .issuer_name(root_name)
        .public_key(tsa_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=10))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=True,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.TIME_STAMPING]), critical=True
        )
        .sign(root_key, hashes.SHA256())
    )
    root_path = path / f"{name}-root.pem"
    tsa_path = path / f"{name}-certificate.pem"
    key_path = path / f"{name}-private.pem"
    root_path.write_bytes(root.public_bytes(serialization.Encoding.PEM))
    tsa_path.write_bytes(tsa.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        tsa_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    tsa_certificate = load_cert_from_pemder(str(tsa_path))
    return TimestampProvider(
        id="test-tsa",
        timestamper=DummyTimeStamper(
            tsa_certificate,
            load_private_key_from_pemder(str(key_path), passphrase=None),
        ),
        signing_certificate=tsa_certificate,
        trust_roots=(load_cert_from_pemder(str(root_path)),),
        other_certificates=(),
    )


def pades_policy(profile: PdfSignatureProfile) -> SignaturePolicy:
    return SignaturePolicy(
        id=f"{profile.value}-v1",
        profile=profile,
        signer_identity="local-dev-v1",
        digest_algorithm="sha256",
        timestamp_provider=(
            "test-tsa" if profile is not PdfSignatureProfile.PADES_B_B else None
        ),
        trust_policy=(
            "test-trust"
            if profile
            in {PdfSignatureProfile.PADES_B_LT, PdfSignatureProfile.PADES_B_LTA}
            else None
        ),
        development_only=True,
    )


def pdf_source() -> bytes:
    source = BytesIO()
    writer = PdfFileWriter()
    writer.insert_page(
        generic.DictionaryObject(
            {
                generic.pdf_name("/Type"): generic.pdf_name("/Page"),
                generic.pdf_name("/MediaBox"): generic.ArrayObject(
                    [
                        generic.NumberObject(0),
                        generic.NumberObject(0),
                        generic.NumberObject(100),
                        generic.NumberObject(100),
                    ]
                ),
                generic.pdf_name("/Resources"): generic.DictionaryObject(),
            }
        )
    )
    writer.write(source)
    return source.getvalue()


def test_signature_request_is_declarative() -> None:
    request = SignatureNode(documents=[{"in": uuid4(), "out": uuid4()}]).interpret()[0]

    assert isinstance(request, SignatureRequest)
    assert not hasattr(request, "sign")
    assert request.policy is None


def test_signature_policy_round_trip_and_interpretation() -> None:
    node = SignatureNode(
        documents=[{"in": uuid4(), "out": uuid4()}],
        policy="pades-b-lt-v1",
    )

    restored = cast("SignatureNode", xarta.protocol.dag.parse(node.dict()))

    assert restored.policy == "pades-b-lt-v1"
    assert restored.dict()["policy"] == "pades-b-lt-v1"
    assert restored.interpret()[0].policy == "pades-b-lt-v1"


@pytest.mark.parametrize("policy", ("", " space", "a/b", "a" * 129))
def test_signature_node_rejects_invalid_policy_reference(policy: str) -> None:
    with pytest.raises(ValueError, match="Signature policy"):
        SignatureNode(documents=[], policy=policy)


def test_policy_registry_resolution_rules() -> None:
    registry = SignaturePolicyRegistry((ELECTRONIC_SEAL_V1,))

    assert (
        registry.resolve(
            None,
            default_policy=ELECTRONIC_SEAL_V1.id,
            allow_development_policies=True,
        )
        is ELECTRONIC_SEAL_V1
    )
    assert (
        registry.resolve(
            ELECTRONIC_SEAL_V1.id,
            default_policy="unused",
            allow_development_policies=True,
        )
        is ELECTRONIC_SEAL_V1
    )
    with pytest.raises(UnknownSignaturePolicy, match="'missing'"):
        registry.resolve(
            "missing",
            default_policy=ELECTRONIC_SEAL_V1.id,
            allow_development_policies=True,
        )
    with pytest.raises(ForbiddenSignaturePolicy, match="not allowed"):
        registry.resolve(
            None,
            default_policy=ELECTRONIC_SEAL_V1.id,
            allow_development_policies=False,
        )


def test_policy_profile_invariants_are_validated() -> None:
    with pytest.raises(SignaturePolicyConfigurationError, match="timestamp provider"):
        SignaturePolicy(
            id="pades-b-t-v1",
            profile=PdfSignatureProfile.PADES_B_T,
            signer_identity="signer-v1",
            digest_algorithm="sha256",
            timestamp_provider=None,
            trust_policy=None,
        )


@pytest.mark.asyncio
async def test_worker_classifies_unknown_policy_as_permanent() -> None:
    with pytest.raises(PermanentError) as captured:
        await _worker(
            SignatureNode(
                documents=[{"in": uuid4(), "out": uuid4()}], policy="missing"
            ),
            components=components(SimpleNamespace()),
        )

    assert captured.value.error_code == "signature_request_rejected"


@pytest.mark.asyncio
async def test_worker_orchestrates_adapter_and_preserves_output_metadata(
    monkeypatch,
) -> None:
    output_id = uuid4()
    document_type = DocumentTypeIdentifier("invoice")
    metadata = {"source": "test"}
    source_result = DocumentSourceResult(
        id=uuid4(),
        document_type=document_type,
        content_type="application/octet-stream",
        metadata=metadata,
        data=b"unsigned",
    )

    class Source:
        async def retrieve(self):
            return source_result

    class Adapter:
        async def sign(self, data: bytes, policy: SignaturePolicy) -> bytes:
            assert data == b"unsigned"
            assert policy is ELECTRONIC_SEAL_V1
            return b"signed"

    persisted = []

    async def persist(result):
        persisted.append(result)

    monkeypatch.setattr(DocumentSourceResult, "persist", persist)
    node = SignatureNode(documents=[])
    monkeypatch.setattr(
        node,
        "interpret",
        lambda: [
            SignatureRequest(
                source=cast("Any", Source()),
                output_id=output_id,
                policy=ELECTRONIC_SEAL_V1.id,
            )
        ],
    )

    result = await _worker(
        node,
        components=components(Adapter()),
    )

    assert [outcome.outcome for outcome in result.outcomes] == ["success"]
    assert len(persisted) == 1
    assert persisted[0].id == output_id
    assert persisted[0].data == b"signed"
    assert persisted[0].content_type == "application/pdf"
    assert persisted[0].document_type is document_type
    assert persisted[0].metadata is metadata


@pytest.mark.asyncio
async def test_worker_leaves_timestamp_transport_failure_retryable(monkeypatch) -> None:
    class Source:
        async def retrieve(self):
            return DocumentSourceResult(
                id=uuid4(),
                document_type=DocumentTypeIdentifier("invoice"),
                content_type="application/pdf",
                data=b"unsigned",
            )

    class Adapter:
        async def sign(self, _data: bytes, _policy: SignaturePolicy) -> bytes:
            raise TimeoutError("TSA unavailable")

    async def output_missing(_source, **_kwargs):
        raise FileNotFoundError

    persisted = []

    async def persist(result):
        persisted.append(result)

    monkeypatch.setattr(
        "xarta.protocol.document.source.GenerateDocumentSource.retrieve",
        output_missing,
    )
    monkeypatch.setattr(DocumentSourceResult, "persist", persist)
    policy = pades_policy(PdfSignatureProfile.PADES_B_T)
    adapter = Adapter()
    service = PolicyDrivenPdfService(adapter, policy.signer_identity)
    signature_components = SignatureComponents(
        policies=SignaturePolicyRegistry((policy,)),
        default_policy_id=policy.id,
        allow_development_policies=True,
        pdf_signer=service,
        pdf_validator=service,
    )
    node = SignatureNode(documents=[])
    monkeypatch.setattr(
        node,
        "interpret",
        lambda: [
            SignatureRequest(
                source=cast("Any", Source()),
                output_id=uuid4(),
                policy=policy.id,
            )
        ],
    )

    with pytest.raises(TimeoutError, match="TSA unavailable"):
        await _worker(node, components=signature_components)
    assert persisted == []


@pytest.mark.asyncio
async def test_worker_reuses_completed_output(monkeypatch) -> None:
    input_id = uuid4()
    output_id = uuid4()

    async def retrieve(source, **_kwargs):
        if source.id == output_id:
            return DocumentSourceResult(
                id=output_id,
                document_type=DocumentTypeIdentifier("invoice"),
                content_type="application/pdf",
                data=b"existing signature",
            )
        if source.id == input_id:
            return DocumentSourceResult(
                id=input_id,
                document_type=DocumentTypeIdentifier("invoice"),
                content_type="application/pdf",
                data=b"unsigned",
            )
        raise AssertionError("unexpected document")

    class Adapter:
        async def sign(self, _data: bytes, _policy: SignaturePolicy) -> bytes:
            raise AssertionError("completed output should not be signed again")

        async def validates(
            self, signed: bytes, source: bytes, policy: SignaturePolicy
        ) -> bool:
            assert signed == b"existing signature"
            assert source == b"unsigned"
            assert policy is ELECTRONIC_SEAL_V1
            return True

    monkeypatch.setattr(
        "xarta.protocol.document.source.GenerateDocumentSource.retrieve", retrieve
    )

    result = await _worker(
        SignatureNode(documents=[{"in": input_id, "out": output_id}]),
        components=components(Adapter()),
    )

    assert [outcome.outcome for outcome in result.outcomes] == ["success"]


@pytest.mark.asyncio
async def test_worker_rejects_mismatched_completed_output(monkeypatch) -> None:
    input_id = uuid4()
    output_id = uuid4()

    async def retrieve(source, **_kwargs):
        return DocumentSourceResult(
            id=source.id,
            document_type=DocumentTypeIdentifier("invoice"),
            content_type="application/pdf",
            data=b"existing" if source.id == output_id else b"unsigned",
        )

    class Adapter:
        async def sign(self, _data: bytes, _policy: SignaturePolicy) -> bytes:
            raise AssertionError("occupied output should not be overwritten")

        async def validates(
            self, _signed: bytes, _source: bytes, _policy: SignaturePolicy
        ) -> bool:
            return False

    monkeypatch.setattr(
        "xarta.protocol.document.source.GenerateDocumentSource.retrieve", retrieve
    )

    with pytest.raises(PermanentError, match="does not match its source") as captured:
        await _worker(
            SignatureNode(documents=[{"in": input_id, "out": output_id}]),
            components=components(Adapter()),
        )
    assert isinstance(captured.value.__cause__, ExistingSignatureMismatch)


@pytest.mark.asyncio
async def test_worker_signs_multiple_documents_sequentially(monkeypatch) -> None:
    output_ids = {uuid4(), uuid4()}
    active = 0
    maximum_active = 0

    async def retrieve(source, **_kwargs):
        if source.id in output_ids:
            raise FileNotFoundError
        return DocumentSourceResult(
            id=source.id,
            document_type=DocumentTypeIdentifier("invoice"),
            content_type="application/pdf",
            data=b"unsigned",
        )

    class Adapter:
        async def sign(self, _data: bytes, _policy: SignaturePolicy) -> bytes:
            nonlocal active, maximum_active
            active += 1
            maximum_active = max(maximum_active, active)
            await asyncio.sleep(0)
            active -= 1
            return b"signed"

    async def persist(_result):
        return None

    monkeypatch.setattr(
        "xarta.protocol.document.source.GenerateDocumentSource.retrieve", retrieve
    )
    monkeypatch.setattr(DocumentSourceResult, "persist", persist)
    input_ids = [uuid4(), uuid4()]

    await _worker(
        SignatureNode(
            documents=[
                {"in": input_id, "out": output_id}
                for input_id, output_id in zip(input_ids, output_ids, strict=True)
            ]
        ),
        components=components(Adapter()),
    )

    assert maximum_active == 1


def test_signature_components_reject_unknown_adapter() -> None:
    with pytest.raises(UnknownAdapterError, match="remote"):
        create_signature_components({"adapter": "remote"})


def test_signature_components_reject_forbidden_development_default() -> None:
    with pytest.raises(ForbiddenSignaturePolicy, match="not allowed"):
        create_signature_components(
            {
                "adapter": "local",
                "private_pem": "private.pem",
                "public_pem": "certificate.pem",
                "default_policy": ELECTRONIC_SEAL_V1.id,
                "allow_development_policies": False,
            }
        )


def test_local_adapter_validates_configuration() -> None:
    factory = LocalSignatureAdapterFactory()

    with pytest.raises(ValueError, match="private_pem"):
        factory.validate({"adapter": "local", "public_pem": "certificate.pem"})


def test_local_adapter_reports_signer_load_failure(monkeypatch) -> None:
    def fail(*_args):
        raise ValueError("invalid key")

    monkeypatch.setattr(signature_adapters, "LocalSigner", fail)

    with pytest.raises(ValueError, match="could not load"):
        create_signature_components(
            {
                "adapter": "local",
                "private_pem": "private.pem",
                "public_pem": "certificate.pem",
            }
        )


def test_local_signer_rejects_mismatched_certificate(tmp_path: Path) -> None:
    private_path, _, _ = signing_material(tmp_path, "private")
    _, certificate_path, _ = signing_material(tmp_path, "certificate")

    with pytest.raises(ValueError, match="does not match certificate"):
        LocalSigner(str(private_path), str(certificate_path))


@pytest.mark.asyncio
async def test_local_signer_reuses_parsed_private_key(
    monkeypatch, tmp_path: Path
) -> None:
    private_path, certificate_path, private_key = signing_material(tmp_path, "signer")
    signer = LocalSigner(str(private_path), str(certificate_path))

    def unexpected_reload(*_args, **_kwargs):
        raise AssertionError("private key was parsed again")

    monkeypatch.setattr(serialization, "load_der_private_key", unexpected_reload)
    signature = await signer.async_sign_raw(b"payload", "sha256")
    dry_run_signature = await signer.async_sign_raw(
        b"second payload", "sha256", dry_run=True
    )

    private_key.public_key().verify(
        signature, b"payload", padding.PKCS1v15(), hashes.SHA256()
    )
    assert dry_run_signature == bytes(private_key.key_size // 8)


@pytest.mark.asyncio
async def test_local_adapter_signs_off_the_event_loop(monkeypatch) -> None:
    event_loop_thread = threading.get_ident()
    signing_thread = None

    def sign(data, signer, policy):
        nonlocal signing_thread
        signing_thread = threading.get_ident()
        assert data == b"pdf"
        assert signer == "signer"
        assert policy is ELECTRONIC_SEAL_V1
        return b"signed"

    monkeypatch.setattr(signature_adapters, "sign", sign)

    assert (
        await LocalSignatureAdapter(cast("Any", "signer")).sign(
            b"pdf", ELECTRONIC_SEAL_V1
        )
        == b"signed"
    )
    assert signing_thread != event_loop_thread


@pytest.mark.asyncio
async def test_local_signer_creates_valid_pades_b_b_with_intermediate_chain(
    tmp_path: Path,
) -> None:
    private_path, leaf_path, chain_path, root_path = signing_chain_material(tmp_path)
    signer = LocalSigner(str(private_path), str(leaf_path), str(chain_path))
    adapter = LocalSignatureAdapter(signer)
    policy = parse_signature_configuration(
        {
            "schema_version": 1,
            "default_policy": "pades-b-b-v1",
            "signer_identities": {"local-dev-v1": {"provider": "local-pem"}},
            "policies": {
                "pades-b-b-v1": {
                    "profile": "pades-b-b",
                    "signer_identity": "local-dev-v1",
                    "digest_algorithm": "sha256",
                    "development_only": True,
                }
            },
        }
    ).policies.get("pades-b-b-v1")
    source = BytesIO()
    writer = PdfFileWriter()
    writer.insert_page(
        generic.DictionaryObject(
            {
                generic.pdf_name("/Type"): generic.pdf_name("/Page"),
                generic.pdf_name("/MediaBox"): generic.ArrayObject(
                    [
                        generic.NumberObject(0),
                        generic.NumberObject(0),
                        generic.NumberObject(100),
                        generic.NumberObject(100),
                    ]
                ),
                generic.pdf_name("/Resources"): generic.DictionaryObject(),
            }
        )
    )
    writer.write(source)

    signed = await adapter.sign(source.getvalue(), policy)

    reader = PdfFileReader(BytesIO(signed))
    assert len(reader.embedded_signatures) == 1
    embedded_signature = reader.embedded_signatures[0]
    assert embedded_signature.sig_object["/SubFilter"] == SigSeedSubFilter.PADES.value
    assert (
        embedded_signature.signer_info["digest_algorithm"]["algorithm"].native
        == "sha256"
    )
    signed_attributes = {
        attribute["type"].native
        for attribute in embedded_signature.signer_info["signed_attrs"]
    }
    assert "signing_certificate_v2" in signed_attributes

    expected_intermediate = x509.load_pem_x509_certificate(chain_path.read_bytes())
    expected_root = x509.load_pem_x509_certificate(root_path.read_bytes())
    embedded_certificates = {
        certificate.dump() for certificate in embedded_signature.other_embedded_certs
    }
    assert (
        expected_intermediate.public_bytes(serialization.Encoding.DER)
        in embedded_certificates
    )
    assert (
        expected_root.public_bytes(serialization.Encoding.DER)
        not in embedded_certificates
    )

    status = await async_validate_pdf_signature(
        embedded_signature,
        signer_validation_context=ValidationContext(
            trust_roots=[load_cert_from_pemder(str(root_path))],
            allow_fetching=False,
        ),
    )
    assert status.intact
    assert status.valid
    assert status.trusted
    assert await adapter.validates(signed, source.getvalue(), policy)
    assert not await adapter.validates(
        signed + b"\n% unsigned trailing data\n", source.getvalue(), policy
    )


@pytest.mark.asyncio
async def test_local_signer_creates_and_reuses_valid_pades_b_t(
    tmp_path: Path,
) -> None:
    private_path, certificate_path, _ = signing_material(tmp_path, "document-signer")
    signer = LocalSigner(str(private_path), str(certificate_path))
    provider = timestamp_provider(tmp_path)
    adapter = LocalSignatureAdapter(signer, {provider.id: provider})
    policy = pades_policy(PdfSignatureProfile.PADES_B_T)
    source = pdf_source()

    signed = await adapter.sign(source, policy)

    reader = PdfFileReader(BytesIO(signed))
    embedded_signature = reader.embedded_regular_signatures[0]
    assert embedded_signature.sig_object["/SubFilter"] == SigSeedSubFilter.PADES.value
    assert (
        embedded_signature.signer_info["digest_algorithm"]["algorithm"].native
        == "sha256"
    )
    unsigned_attributes = {
        attribute["type"].native
        for attribute in embedded_signature.signer_info["unsigned_attrs"]
    }
    assert "signature_time_stamp_token" in unsigned_attributes
    status = await async_validate_pdf_signature(
        embedded_signature,
        signer_validation_context=ValidationContext(
            trust_roots=[signer.signing_cert], allow_fetching=False
        ),
        ts_validation_context=provider.validation_context(),
    )
    assert status.intact
    assert status.valid
    assert status.trusted
    assert status.coverage.name == "ENTIRE_FILE"
    assert status.timestamp_validity is not None
    assert status.timestamp_validity.intact
    assert status.timestamp_validity.valid
    assert status.timestamp_validity.trusted
    assert (
        status.timestamp_validity.signing_cert.dump()
        == provider.signing_certificate.dump()
    )
    assert await adapter.validates(signed, source, policy)


@pytest.mark.asyncio
async def test_pades_b_b_output_is_not_reusable_for_pades_b_t(
    tmp_path: Path,
) -> None:
    private_path, certificate_path, _ = signing_material(tmp_path, "document-signer")
    signer = LocalSigner(str(private_path), str(certificate_path))
    provider = timestamp_provider(tmp_path)
    adapter = LocalSignatureAdapter(signer, {provider.id: provider})
    source = pdf_source()
    b_b_output = await adapter.sign(source, pades_policy(PdfSignatureProfile.PADES_B_B))

    assert not await adapter.validates(
        b_b_output, source, pades_policy(PdfSignatureProfile.PADES_B_T)
    )


@pytest.mark.asyncio
async def test_pades_b_t_output_requires_expected_timestamp_authority(
    tmp_path: Path,
) -> None:
    private_path, certificate_path, _ = signing_material(tmp_path, "document-signer")
    signer = LocalSigner(str(private_path), str(certificate_path))
    provider = timestamp_provider(tmp_path, "Expected TSA")
    source = pdf_source()
    policy = pades_policy(PdfSignatureProfile.PADES_B_T)
    signed = await LocalSignatureAdapter(signer, {provider.id: provider}).sign(
        source, policy
    )
    unexpected_provider = timestamp_provider(tmp_path, "Unexpected TSA")

    assert not await LocalSignatureAdapter(
        signer, {provider.id: unexpected_provider}
    ).validates(signed, source, policy)


@pytest.mark.asyncio
async def test_timestamp_failure_does_not_downgrade_to_pades_b_b(
    tmp_path: Path,
) -> None:
    class FailingTimeStamper(TimeStamper):
        async def async_request_tsa_response(self, req):
            del req
            raise TimestampRequestError("TSA unavailable")

    private_path, certificate_path, _ = signing_material(tmp_path, "document-signer")
    signer = LocalSigner(str(private_path), str(certificate_path))
    provider = timestamp_provider(tmp_path)
    failing_provider = TimestampProvider(
        id=provider.id,
        timestamper=FailingTimeStamper(),
        signing_certificate=provider.signing_certificate,
        trust_roots=provider.trust_roots,
        other_certificates=provider.other_certificates,
    )
    adapter = LocalSignatureAdapter(signer, {provider.id: failing_provider})

    with pytest.raises(TimestampRequestError, match="unavailable"):
        await adapter.sign(pdf_source(), pades_policy(PdfSignatureProfile.PADES_B_T))


@pytest.mark.asyncio
async def test_rejected_timestamp_response_does_not_produce_output(
    tmp_path: Path,
) -> None:
    class RejectingTimeStamper(TimeStamper):
        async def async_request_tsa_response(self, req):
            del req
            return tsp.TimeStampResp(
                {
                    "status": {
                        "status": "rejection",
                        "fail_info": {"bad_data_format"},
                    }
                }
            )

    private_path, certificate_path, _ = signing_material(tmp_path, "document-signer")
    signer = LocalSigner(str(private_path), str(certificate_path))
    provider = timestamp_provider(tmp_path)
    rejecting_provider = TimestampProvider(
        id=provider.id,
        timestamper=RejectingTimeStamper(),
        signing_certificate=provider.signing_certificate,
        trust_roots=provider.trust_roots,
        other_certificates=provider.other_certificates,
    )

    with pytest.raises(TimestampRequestError, match="bad_data_format"):
        await LocalSignatureAdapter(signer, {provider.id: rejecting_provider}).sign(
            pdf_source(), pades_policy(PdfSignatureProfile.PADES_B_T)
        )


@pytest.mark.parametrize(
    "profile", (PdfSignatureProfile.PADES_B_LT, PdfSignatureProfile.PADES_B_LTA)
)
def test_signature_components_keep_long_term_profiles_unavailable(profile) -> None:
    policy = pades_policy(profile)
    configuration = parse_signature_configuration(
        {
            "schema_version": 1,
            "default_policy": policy.id,
            "signer_identities": {"local-dev-v1": {"provider": "local-pem"}},
            "policies": {
                policy.id: {
                    "profile": policy.profile.value,
                    "signer_identity": policy.signer_identity,
                    "digest_algorithm": policy.digest_algorithm,
                    "timestamp_provider": policy.timestamp_provider,
                    "trust_policy": policy.trust_policy,
                    "development_only": True,
                }
            },
        }
    )

    with pytest.raises(ValueError, match=profile.value):
        create_signature_components({"signature_configuration": configuration})


def test_signature_components_reject_unknown_timestamp_provider() -> None:
    policy = pades_policy(PdfSignatureProfile.PADES_B_T)
    configuration = parse_signature_configuration(
        {
            "schema_version": 1,
            "default_policy": policy.id,
            "signer_identities": {"local-dev-v1": {"provider": "local-pem"}},
            "policies": {
                policy.id: {
                    "profile": policy.profile.value,
                    "signer_identity": policy.signer_identity,
                    "digest_algorithm": policy.digest_algorithm,
                    "timestamp_provider": policy.timestamp_provider,
                    "development_only": True,
                }
            },
        }
    )

    with pytest.raises(ValueError, match="unknown timestamp provider"):
        create_signature_components({"signature_configuration": configuration})


def test_signature_components_reject_untrusted_timestamp_certificate(
    tmp_path: Path,
) -> None:
    private_path, certificate_path, _ = signing_material(tmp_path, "document-signer")
    timestamp_provider(tmp_path, "Expected TSA")
    timestamp_provider(tmp_path, "Unrelated TSA")
    policy = pades_policy(PdfSignatureProfile.PADES_B_T)
    configuration = parse_signature_configuration(
        {
            "schema_version": 1,
            "default_policy": policy.id,
            "signer_identities": {"local-dev-v1": {"provider": "local-pem"}},
            "policies": {
                policy.id: {
                    "profile": policy.profile.value,
                    "signer_identity": policy.signer_identity,
                    "digest_algorithm": policy.digest_algorithm,
                    "timestamp_provider": policy.timestamp_provider,
                    "development_only": True,
                }
            },
        }
    )

    with pytest.raises(ValueError, match="trust material is invalid"):
        create_signature_components(
            {
                "signature_configuration": configuration,
                "private_pem": str(private_path),
                "public_pem": str(certificate_path),
                "timestamp_provider_configurations": {
                    "test-tsa": TimestampProviderConfiguration(
                        id="test-tsa",
                        provider="http-rfc3161",
                        endpoint="https://tsa.example.test",
                        timeout_seconds=5,
                        certificate_pem=str(tmp_path / "Expected TSA-certificate.pem"),
                        trust_root_pem=str(tmp_path / "Unrelated TSA-root.pem"),
                        chain_pem=None,
                    )
                },
            }
        )


@pytest.mark.asyncio
async def test_signature_components_load_once_for_copied_blueprints(
    monkeypatch,
) -> None:
    configured_components = components(SimpleNamespace())
    loads = 0
    storage_initializations = 0

    def create(_configuration):
        nonlocal loads
        loads += 1
        return configured_components

    async def register(**_kwargs):
        return None

    async def initialize_temporary_storage():
        nonlocal storage_initializations
        storage_initializations += 1

    monkeypatch.setattr(signature_service, "create_signature_components", create)
    monkeypatch.setattr(
        signature_service,
        "initialize_temporary_storage",
        initialize_temporary_storage,
    )
    monkeypatch.setattr(signature_service.NATS, "register", register)
    app = SimpleNamespace(ctx=SimpleNamespace())

    await signature_service._setup_signature(app)
    await signature_service._setup_signature(app)

    assert loads == 1
    assert storage_initializations == 1
    assert app.ctx.signature_components is configured_components


@pytest.mark.asyncio
async def test_empty_signature_node_needs_no_components() -> None:
    result = await _worker(SignatureNode(documents=[]))

    assert [outcome.outcome for outcome in result.outcomes] == ["success"]
