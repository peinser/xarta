from __future__ import annotations

import asyncio
import socket
import ssl

from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import dns.exception
import dns.message
import dns.name
import dns.rcode
import dns.rdataclass
import dns.rdatatype
import dns.resolver
import dns.rrset
import orjson
import pytest

from aiohttp import web

from xarta.http.sessions import USER_AGENT
from xarta.http.sessions import HTTPRequestManager
from xarta.services.v1.peppol.api import participant_registration
from xarta.services.v1.peppol.discovery import IDENTIFIER_NAMESPACE
from xarta.services.v1.peppol.discovery import MAX_SMP_BYTES
from xarta.services.v1.peppol.discovery import PARTICIPANT_SCHEME
from xarta.services.v1.peppol.discovery import SML_ZONES
from xarta.services.v1.peppol.discovery import SMP_NAMESPACE
from xarta.services.v1.peppol.discovery import PeppolDiscoveryEvidence
from xarta.services.v1.peppol.discovery import PeppolParticipantDiscovery
from xarta.services.v1.peppol.discovery import PublicSMPResolver
from xarta.services.v1.peppol.discovery import UnsafeSMPAddress
from xarta.services.v1.peppol.discovery import _service_group
from xarta.services.v1.peppol.discovery import naptr_uri
from xarta.services.v1.peppol.discovery import participant_dns_name
from xarta.services.v1.peppol.discovery import validate_smp_url
from xarta.services.v1.peppol.models import PeppolParticipant

PARTICIPANT = PeppolParticipant("0208", "0308357159")


def record(
    url="https://smp.example/", order=10, preference=10, service=b"Meta:SMP", flags=b"U"
):
    return SimpleNamespace(
        order=order,
        preference=preference,
        service=service,
        flags=flags,
        regexp=f"!^.*$!{url}!".encode(),
        replacement=dns.name.root,
    )


def answer(*records):
    class Answer(list):
        rrset = SimpleNamespace(ttl=120)

    return Answer(records)


def discovery(result=None, error=None, session=None):
    return PeppolParticipantDiscovery(
        resolver=SimpleNamespace(
            resolve=AsyncMock(return_value=result, side_effect=error)
        ),
        session=session,
    )


@pytest.fixture
async def http_session():
    session = await HTTPRequestManager.open(loop=asyncio.get_running_loop())
    try:
        yield session
    finally:
        await session.close()


def negative(nxdomain=True, proof=True):
    name = participant_dns_name(PARTICIPANT, SML_ZONES["production"])
    message = dns.message.make_response(dns.message.make_query(name, "NAPTR"))
    message.set_rcode(dns.rcode.NXDOMAIN if nxdomain else dns.rcode.NOERROR)
    if proof:
        message.authority.append(
            dns.rrset.from_text(
                SML_ZONES["production"],
                600,
                "IN",
                "SOA",
                "ns.example. hostmaster.example. 1 3600 600 86400 120",
            )
        )
    return (
        dns.resolver.NXDOMAIN(
            qnames=[dns.name.from_text(name)],
            responses={dns.name.from_text(name): message},
        )
        if nxdomain
        else dns.resolver.NoAnswer(response=message)
    )


def group(identifier="0208:0308357159", collection=True):
    return f"""<ServiceGroup xmlns="{SMP_NAMESPACE}">
      <id:ParticipantIdentifier xmlns:id="{IDENTIFIER_NAMESPACE}" scheme="{PARTICIPANT_SCHEME}">{identifier}</id:ParticipantIdentifier>
      {"<ServiceMetadataReferenceCollection/>" if collection else ""}
    </ServiceGroup>""".encode()


@pytest.mark.parametrize(
    ("scheme", "identifier", "hash_value"),
    [
        (
            "0088",
            "1234567890123",
            "sjsyvccmqyjxk3weuapffq4x3umcrf4qryherj4vovhmonh7gccq",
        ),
        ("0088", "123ABC", "y7dzfxaf3d4cjz4kcgrxtec6twvcga4ky7zwa5boif6mswd4tdrq"),
        ("9915", "test", "eh5boavaktmbgzyh2a63dz4qov33fvp5nsdvqklucfraayoodw6a"),
    ],
)
def test_matches_helger_and_peppol_policy_dns_vectors(scheme, identifier, hash_value):
    assert (
        participant_dns_name(PeppolParticipant(scheme, identifier), SML_ZONES["test"])
        == f"{hash_value}.iso6523-actorid-upis.participant.sml.test.tech.peppol.org."
    )


@pytest.mark.parametrize("nxdomain", [True, False])
@pytest.mark.parametrize("proof", [True, False])
async def test_only_proven_negative_dns_is_definitive(nxdomain, proof):
    service = discovery(error=negative(nxdomain, proof))
    result = await service.check(PARTICIPANT)
    assert result.registered is (False if proof else None)
    assert result.dict()["definitive"] is proof
    if proof:
        assert result.evidence[0].ttl == 120
    service.resolver.resolve.assert_awaited_once_with(
        result.dns_name, "NAPTR", search=False, lifetime=5
    )


@pytest.mark.parametrize(
    "error",
    [dns.exception.Timeout(), OSError("DNS unavailable"), dns.resolver.NoNameservers()],
)
async def test_dns_technical_failures_are_never_negative(error):
    result = await discovery(error=error).check(PARTICIPANT)
    assert result.registered is None and result.dict()["retryable"]


@pytest.mark.parametrize(
    ("rcode", "code"),
    [(dns.rcode.SERVFAIL, "dns_servfail"), (dns.rcode.REFUSED, "dns_refused")],
)
async def test_dns_failure_codes_remain_distinct(rcode, code):
    query = dns.message.make_query("example.test.", "NAPTR")
    message = dns.message.make_response(query)
    message.set_rcode(rcode)
    error = dns.resolver.NoNameservers(
        request=query, errors=[("server", False, 53, "failure", message)]
    )
    result = await discovery(error=error).check(PARTICIPANT)
    assert result.registered is None and result.evidence[0].code == code


@pytest.mark.parametrize(
    "records",
    [
        [],
        [record(service=b"other")],
        [record(flags=b"S")],
        [record("file:///tmp/file")],
        [record("http://127.0.0.1/")],
    ],
)
async def test_unusable_discovery_records_are_indeterminate(records):
    result = await discovery(answer(*records)).check(PARTICIPANT)
    assert result.registered is None


async def test_dns_record_is_not_proof_of_smp_registration(monkeypatch):
    service = discovery(answer(record()))
    monkeypatch.setattr(
        service,
        "_lookup_smp",
        AsyncMock(
            return_value=(None, PeppolDiscoveryEvidence("smp", "smp_timeout", True))
        ),
    )
    result = await service.check(PARTICIPANT)
    assert result.registered is None and not result.dict()["definitive"]


@pytest.mark.parametrize(
    ("results", "expected"),
    [([False, False], False), ([False, None], None), ([None, True], True)],
)
async def test_multiple_smp_alternatives_aggregate_without_false_negatives(
    monkeypatch, results, expected
):
    service = discovery(
        answer(
            record("https://first.example", preference=1),
            record("https://second.example", preference=2),
        )
    )
    responses = [
        (
            value,
            PeppolDiscoveryEvidence(
                "smp",
                (
                    "smp_not_found"
                    if value is False
                    else "smp_timeout" if value is None else "smp_service_group_found"
                ),
            ),
        )
        for value in results
    ]
    lookup = AsyncMock(side_effect=responses)
    monkeypatch.setattr(service, "_lookup_smp", lookup)
    result = await service.check(PARTICIPANT)
    assert result.registered is expected
    assert lookup.await_args_list[0].args[0] == "https://first.example/"


async def test_higher_naptr_order_is_not_used_after_matching_order(monkeypatch):
    service = discovery(
        answer(record(order=1), record("https://higher.example", order=2))
    )
    lookup = AsyncMock(
        return_value=(False, PeppolDiscoveryEvidence("smp", "smp_not_found"))
    )
    monkeypatch.setattr(service, "_lookup_smp", lookup)
    assert (await service.check(PARTICIPANT)).registered is False
    assert lookup.await_count == 1


def test_naptr_uri_and_url_policy():
    # RFC 4848 form (published by the Peppol SML) and the anchored BDXL form.
    for expression in (b"!.*!https://smp.example!", b"!^.*$!https://smp.example!"):
        assert naptr_uri(expression) == "https://smp.example/"
    for expression in (
        b"!^(.+)\\.example$!https://smp.example/\\1!",
        b"!^B-.*$!https://smp.example!",
        b"!.*!https://smp.example!i",
        b"#.*#https://smp.example#",
    ):
        with pytest.raises(ValueError):
            naptr_uri(expression)
    for value in (
        "https://user:pass@smp.example/",
        "http://127.0.0.1/",
        "http://[::1]/",
        "file:///etc/passwd",
        "https://smp.example/?x=1",
    ):
        with pytest.raises((ValueError, UnsafeSMPAddress)):
            validate_smp_url(value)


async def test_resolver_rejects_mixed_public_private_addresses(monkeypatch):
    resolver = PublicSMPResolver()
    monkeypatch.setattr(
        resolver.resolver,
        "resolve",
        AsyncMock(return_value=[{"host": "8.8.8.8"}, {"host": "127.0.0.1"}]),
    )
    try:
        with pytest.raises(UnsafeSMPAddress):
            await resolver.resolve("smp.example", 443, socket.AF_UNSPEC)
    finally:
        await resolver.close()


@pytest.mark.parametrize(
    "body",
    [
        group("0208:other"),
        group(collection=False),
        b"<html/>",
        b"<broken",
        b"<!DOCTYPE x><x/>",
    ],
)
def test_reject_malformed_or_mismatched_service_group(body):
    with pytest.raises(ValueError):
        _service_group(body, PARTICIPANT)


def test_empty_service_group_is_registration_not_document_support():
    assert _service_group(group(), PARTICIPANT) == 0


@pytest.mark.parametrize(
    ("status", "body", "expected", "code"),
    [
        (200, group(), True, "smp_service_group_found"),
        (404, b"", False, "smp_not_found"),
        (500, b"", None, "smp_http_error"),
        (403, b"", None, "smp_http_error"),
        (429, b"", None, "smp_rate_limited"),
        (302, b"", None, "smp_redirect"),
        (200, b"<html>no participant</html>", None, "smp_invalid_response"),
        (200, b"x" * (MAX_SMP_BYTES + 1), None, "smp_response_too_large"),
    ],
)
async def test_real_http_client_classification(
    status, body, expected, code, http_session
):
    app = web.Application()
    paths = []

    async def handler(request):
        assert request.headers["User-Agent"] == USER_AGENT
        paths.append(request.raw_path)
        return web.Response(
            status=status,
            body=body,
            headers={"Location": "http://127.0.0.1/elsewhere"} if status == 302 else {},
        )

    app.router.add_get("/{path:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    server = web.TCPSite(runner, "127.0.0.1", 0)
    await server.start()
    try:
        port = server._server.sockets[0].getsockname()[1]
        result, evidence = await discovery(session=http_session)._lookup_smp(
            f"http://127.0.0.1:{port}/smp/", PARTICIPANT
        )
        assert result is expected and evidence.code == code
        assert len(paths) == 1  # never follow redirects
        assert (
            paths[0] == "/smp/iso6523-actorid-upis::0208:0308357159"
            or paths[0] == "/smp/iso6523-actorid-upis%3A%3A0208%3A0308357159"
        )
    finally:
        await runner.cleanup()


async def test_http_timeout_is_indeterminate(http_session):
    app = web.Application()

    async def handler(request):
        await asyncio.sleep(0.05)
        return web.Response(body=group())

    app.router.add_get("/{path:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    server = web.TCPSite(runner, "127.0.0.1", 0)
    await server.start()
    try:
        port = server._server.sockets[0].getsockname()[1]
        service = PeppolParticipantDiscovery(http_timeout=0.01, session=http_session)
        result, evidence = await service._lookup_smp(
            f"http://127.0.0.1:{port}/", PARTICIPANT
        )
        assert result is None and evidence.code == "smp_timeout"
    finally:
        await runner.cleanup()


async def test_api_preserves_indeterminate_result():
    service = discovery(error=dns.exception.Timeout())
    request = SimpleNamespace(
        app=SimpleNamespace(ctx=SimpleNamespace(peppol_discovery=service))
    )
    response = await participant_registration(request, "0208", "0308357159")
    assert response.status == 200
    body = orjson.loads(response.body)
    assert body["registered"] is None and body["status"] == "indeterminate"
    assert not body["definitive"]
    assert (await participant_registration(request, "invalid", "x")).status == 400


async def test_total_deadline_includes_dns_and_queue_wait():
    resolver = SimpleNamespace(
        resolve=AsyncMock(side_effect=lambda *args, **kwargs: None)
    )

    async def stalled(*args, **kwargs):
        await asyncio.sleep(1)

    resolver.resolve = stalled
    service = PeppolParticipantDiscovery(resolver=resolver, total_timeout=0.01)
    result = await service.check(PARTICIPANT)
    assert result.registered is None and result.evidence[-1].code == "discovery_timeout"


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (
            aiohttp.ClientConnectorCertificateError(
                SimpleNamespace(), ssl.SSLCertVerificationError("invalid certificate")
            ),
            "smp_tls_error",
        ),
        (
            aiohttp.ClientConnectorError(
                SimpleNamespace(), OSError("connection refused")
            ),
            "smp_connection_error",
        ),
        (
            aiohttp.ClientConnectorError(
                SimpleNamespace(), UnsafeSMPAddress("private")
            ),
            "smp_unsafe_address",
        ),
    ],
)
async def test_http_transport_failures_are_indeterminate(monkeypatch, error, code):
    class Session:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def get(self, *args, **kwargs):
            raise error

    result, evidence = await discovery(session=Session())._lookup_smp(
        "https://smp.example/", PARTICIPANT
    )
    assert result is None and evidence.code == code


async def test_discovery_session_is_created_once_with_user_agent_and_closed():
    service = await PeppolParticipantDiscovery.open()
    session = service.session
    assert session.headers["User-Agent"] == USER_AGENT
    assert session.headers["Accept-Encoding"] == "identity"
    assert session.connector._use_dns_cache is False
    assert isinstance(session.connector._resolver, PublicSMPResolver)
    assert service.session is session
    await service.close()
    await service.close()
    assert session.closed and service.session is None
