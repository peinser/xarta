"""Direct Peppol U-NAPTR discovery followed by an SMP ServiceGroup lookup.

Algorithm reference: Helger PeppolNaptrURLProvider and SMPClientReadOnly.
Absence is a protocol observation, never inferred from a transport failure.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import math
import re
import socket

from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import field
from datetime import UTC
from datetime import datetime
from typing import Any
from urllib.parse import quote
from urllib.parse import urlsplit

import aiohttp
import dns.asyncresolver
import dns.exception
import dns.message
import dns.rcode
import dns.rdatatype
import dns.resolver

from aiohttp.abc import AbstractResolver
from aiohttp.resolver import ThreadedResolver
from lxml import etree  # type: ignore[import-untyped]

from xarta.http.sessions import HTTPRequestManager
from xarta.services.v1.peppol.models import PeppolParticipant

PARTICIPANT_SCHEME = "iso6523-actorid-upis"
SML_ZONES = {
    "production": "participant.sml.prod.tech.peppol.org.",
    "test": "participant.sml.test.tech.peppol.org.",
}
SMP_NAMESPACE = "http://busdox.org/serviceMetadata/publishing/1.0/"
IDENTIFIER_NAMESPACE = "http://busdox.org/transport/identifiers/1.0/"
MAX_SMP_BYTES = 1024 * 1024


@dataclass(frozen=True)
class PeppolDiscoveryEvidence:
    stage: str
    code: str
    retryable: bool = False
    url: str | None = None
    http_status: int | None = None
    ttl: int | None = None
    services_count: int | None = None


@dataclass(frozen=True)
class PeppolRegistrationResult:
    participant: PeppolParticipant
    environment: str
    dns_name: str
    registered: bool | None
    evidence: tuple[PeppolDiscoveryEvidence, ...]
    checked_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def dict(self) -> dict[str, Any]:
        return {
            "participant": self.participant.dict(),
            "environment": self.environment,
            "dns_name": self.dns_name,
            "registered": self.registered,
            "status": (
                "indeterminate"
                if self.registered is None
                else "registered" if self.registered else "not_registered"
            ),
            "definitive": self.registered is not None,
            "retryable": self.registered is None
            and any(item.retryable for item in self.evidence),
            "checked_at": self.checked_at.isoformat(),
            "scope": "SML discovery and SMP ServiceGroup registration, not Access Point liveness or document support",
            "evidence": [
                {key: value for key, value in asdict(item).items() if value is not None}
                for item in self.evidence
            ],
        }


def participant_value(participant: PeppolParticipant) -> str:
    if not re.fullmatch(r"[0-9]{4}", participant.scheme):
        raise ValueError("Participant scheme must be a four-digit ISO 6523 ICD")
    if not re.fullmatch(r"[\x21-\x7e]{1,128}", participant.identifier) or any(
        char in participant.identifier for char in "/\\?#"
    ):
        raise ValueError(
            "Participant identifier must be 1-128 printable ASCII characters without path delimiters"
        )
    return f"{participant.scheme}:{participant.identifier}".lower()


def participant_dns_name(participant: PeppolParticipant, zone: str) -> str:
    digest = hashlib.sha256(participant_value(participant).encode("utf-8")).digest()
    label = base64.b32encode(digest).decode("ascii").rstrip("=").lower()
    return f"{label}.{PARTICIPANT_SCHEME}.{zone}"


class UnsafeSMPAddress(OSError):
    pass


def validate_smp_url(value: str) -> str:
    if len(value) > 2048 or any(ord(char) < 33 for char in value) or "\\" in value:
        raise ValueError("Invalid SMP URL")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.port not in {None, 80, 443}
        or "%" in parsed.hostname
    ):
        raise ValueError("Invalid SMP URL")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        pass
    else:
        if not address.is_global:
            raise UnsafeSMPAddress("SMP address is not publicly routable")
    return value.rstrip("/") + "/"


class PublicSMPResolver(AbstractResolver):
    """Check the addresses actually handed to aiohttp, preventing DNS rebinding."""

    def __init__(self) -> None:
        self.resolver = ThreadedResolver()

    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_INET):
        records = await self.resolver.resolve(host, port, socket.AddressFamily(family))
        if not records or any(
            not ipaddress.ip_address(item["host"]).is_global for item in records
        ):
            raise UnsafeSMPAddress("SMP DNS returned a non-public address")
        return records

    async def close(self) -> None:
        await self.resolver.close()


def naptr_uri(expression: bytes) -> str:
    """Return the constant URI of a U-NAPTR regexp.

    RFC 4848 permits only "!.*!<URI>!", which the Peppol SML publishes; OASIS BDXL
    examples use "!^.*$!<URI>!". Anything else is not U-NAPTR, so no regex engine.
    """
    parts = expression.decode("ascii").split("!")
    if len(parts) != 4 or parts[0] or parts[1] not in {".*", "^.*$"} or parts[3]:
        raise ValueError("Invalid U-NAPTR regular expression")
    return validate_smp_url(parts[2])


def _negative_ttl(message: dns.message.Message, name: str) -> int | None:
    """Require an SOA-backed negative answer; referrals/empty packets prove nothing."""
    if message.rcode() not in {dns.rcode.NXDOMAIN, dns.rcode.NOERROR}:
        return None
    if message.answer:  # e.g. an alias whose target is missing: configuration failure
        return None
    for rrset in message.authority:
        owner = rrset.name.to_text().lower()
        if rrset.rdtype == dns.rdatatype.SOA and (
            name == owner or name.endswith("." + owner)
        ):
            return int(min(rrset.ttl, *(item.minimum for item in rrset)))
    return None


def _service_group(body: bytes, participant: PeppolParticipant) -> int:
    try:
        if not body or len(body) > MAX_SMP_BYTES:
            raise ValueError("Invalid SMP response size")
        root = etree.fromstring(
            body,
            parser=etree.XMLParser(
                resolve_entities=False,
                load_dtd=False,
                no_network=True,
                recover=False,
                huge_tree=False,
            ),
        )
        if root.getroottree().docinfo.doctype or any(
            isinstance(item, etree._Entity) for item in root.iter()
        ):
            raise ValueError("SMP XML contains a DTD or entity")
    except (ValueError, etree.XMLSyntaxError) as ex:
        raise ValueError("smp_invalid_xml") from ex
    if root.tag != f"{{{SMP_NAMESPACE}}}ServiceGroup":
        raise ValueError("smp_invalid_response")
    identifiers = root.findall(f"{{{IDENTIFIER_NAMESPACE}}}ParticipantIdentifier")
    collections = root.findall(f"{{{SMP_NAMESPACE}}}ServiceMetadataReferenceCollection")
    if len(identifiers) != 1 or len(collections) != 1:
        raise ValueError("smp_invalid_response")
    identifier = identifiers[0]
    if identifier.get("scheme") != PARTICIPANT_SCHEME or (
        identifier.text or ""
    ).strip().lower() != participant_value(participant):
        raise ValueError("smp_participant_mismatch")
    references = collections[0].findall(f"{{{SMP_NAMESPACE}}}ServiceMetadataReference")
    if len(references) != len(collections[0]) or any(
        not item.get("href") for item in references
    ):
        raise ValueError("smp_invalid_response")
    return len(references)


class PeppolParticipantDiscovery:
    def __init__(
        self,
        *,
        environment: str = "production",
        dns_timeout: float = 5,
        http_timeout: float = 10,
        total_timeout: float = 20,
        concurrency: int = 10,
        resolver=None,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        if environment not in SML_ZONES:
            raise ValueError("Peppol discovery environment must be production or test")
        if (
            any(
                not math.isfinite(value) or value <= 0
                for value in (dns_timeout, http_timeout, total_timeout)
            )
            or concurrency < 1
        ):
            raise ValueError("Discovery timeouts and concurrency must be positive")
        self.environment = environment
        self.zone = SML_ZONES[environment]
        self.dns_timeout = dns_timeout
        self.http_timeout = http_timeout
        self.total_timeout = total_timeout
        self.concurrency = concurrency
        self.resolver = (
            resolver if resolver is not None else dns.asyncresolver.Resolver()
        )
        self._slots = asyncio.Semaphore(concurrency)
        self.session = session
        self._http_resolver: PublicSMPResolver | None = None

    @classmethod
    async def open(cls, **configuration) -> PeppolParticipantDiscovery:
        service = cls(**configuration)
        resolver = PublicSMPResolver()
        connector = aiohttp.TCPConnector(
            resolver=resolver,
            use_dns_cache=False,
            limit=service.concurrency,
        )
        try:
            service.session = await HTTPRequestManager.open(
                loop=asyncio.get_running_loop(),
                connector=connector,
                trust_env=False,
                cookie_jar=aiohttp.DummyCookieJar(),
                auto_decompress=False,
                headers={
                    "Accept": "application/xml, text/xml",
                    "Accept-Encoding": "identity",
                },
            )
            service._http_resolver = resolver
        except BaseException:
            await connector.close()
            await resolver.close()
            raise
        return service

    async def close(self) -> None:
        if self._http_resolver is not None:
            if self.session is not None:
                await self.session.close()
                self.session = None
            await self._http_resolver.close()
            self._http_resolver = None

    async def check(self, participant: PeppolParticipant) -> PeppolRegistrationResult:
        name = participant_dns_name(participant, self.zone)
        evidence: list[PeppolDiscoveryEvidence] = []
        registered = None
        try:
            async with asyncio.timeout(self.total_timeout), self._slots:
                registered = await self._check(participant, name, evidence)
        except TimeoutError:
            evidence.append(
                PeppolDiscoveryEvidence("discovery", "discovery_timeout", True)
            )
        return PeppolRegistrationResult(
            participant, self.environment, name, registered, tuple(evidence)
        )

    async def _check(self, participant, name, evidence) -> bool | None:
        try:
            answer = await self.resolver.resolve(
                name, "NAPTR", search=False, lifetime=self.dns_timeout
            )
        except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer) as ex:
            messages = (
                list(ex.responses().values())
                if isinstance(ex, dns.resolver.NXDOMAIN)
                else [ex.response()]
            )
            ttls = [_negative_ttl(message, name) for message in messages]
            proven = bool(ttls) and all(ttl is not None for ttl in ttls)
            code = (
                "dns_nxdomain"
                if isinstance(ex, dns.resolver.NXDOMAIN)
                else "dns_no_naptr"
            )
            evidence.append(
                PeppolDiscoveryEvidence(
                    "dns",
                    code if proven else "dns_unproven_negative",
                    not proven,
                    ttl=min(ttl for ttl in ttls if ttl is not None) if proven else None,
                )
            )
            return False if proven else None
        except (dns.exception.Timeout, TimeoutError):
            evidence.append(PeppolDiscoveryEvidence("dns", "dns_timeout", True))
            return None
        except dns.resolver.NoNameservers as ex:
            responses = [
                item[-1]
                for item in ex.kwargs.get("errors", [])
                if isinstance(item[-1], dns.message.Message)
            ]
            codes = {item.rcode() for item in responses}
            code = (
                "dns_servfail"
                if codes == {dns.rcode.SERVFAIL}
                else "dns_refused" if codes == {dns.rcode.REFUSED} else "dns_failure"
            )
            evidence.append(PeppolDiscoveryEvidence("dns", code, True))
            return None
        except (dns.exception.DNSException, OSError):
            evidence.append(PeppolDiscoveryEvidence("dns", "dns_failure", True))
            return None
        records = list(answer)
        ttl = answer.rrset.ttl if answer.rrset is not None else None
        evidence.append(PeppolDiscoveryEvidence("dns", "dns_records_found", ttl=ttl))
        if len(records) > 64:
            evidence.append(PeppolDiscoveryEvidence("dns", "dns_excessive_records"))
            return None
        matching = [item for item in records if item.service.lower() == b"meta:smp"]
        if not matching:
            evidence.append(
                PeppolDiscoveryEvidence("dns", "dns_no_matching_smp_service")
            )
            return None
        # Once a matching order has been selected, RFC 3403 forbids moving to a
        # different order. Preference orders the alternatives at that order.
        order = min(item.order for item in matching)
        candidates = sorted(
            (item for item in matching if item.order == order),
            key=lambda item: item.preference,
        )
        if len(candidates) > 4:
            evidence.append(PeppolDiscoveryEvidence("dns", "dns_excessive_endpoints"))
            return None
        all_absent = True
        for item in candidates:
            try:
                if item.flags.lower() != b"u" or item.replacement.to_text() != ".":
                    raise ValueError("Not a terminal U-NAPTR record")
                base = naptr_uri(item.regexp)
            except (ValueError, UnicodeError, UnsafeSMPAddress):
                all_absent = False
                evidence.append(
                    PeppolDiscoveryEvidence("dns", "dns_invalid_smp_record")
                )
                continue
            result, observation = await self._lookup_smp(base, participant)
            evidence.append(observation)
            if result is True:
                return True
            all_absent = all_absent and result is False
        return False if all_absent else None

    async def _lookup_smp(
        self, base: str, participant: PeppolParticipant
    ) -> tuple[bool | None, PeppolDiscoveryEvidence]:
        url = base + quote(
            f"{PARTICIPANT_SCHEME}::{participant_value(participant)}", safe=""
        )
        if self.session is None:
            raise RuntimeError("Peppol discovery HTTP session is not initialized")
        try:
            async with self.session.get(
                url,
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=self.http_timeout),
            ) as response:
                status = response.status
                if status == 404:
                    return False, PeppolDiscoveryEvidence(
                        "smp", "smp_not_found", url=url, http_status=status
                    )
                if status != 200:
                    code = (
                        "smp_redirect"
                        if 300 <= status < 400
                        else "smp_rate_limited" if status == 429 else "smp_http_error"
                    )
                    return None, PeppolDiscoveryEvidence(
                        "smp", code, status in {408, 429} or status >= 500, url, status
                    )
                if (
                    response.headers.get("Content-Encoding", "identity").lower()
                    != "identity"
                ):
                    return None, PeppolDiscoveryEvidence(
                        "smp", "smp_unsupported_encoding", url=url, http_status=status
                    )
                data = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    data.extend(chunk)
                    if len(data) > MAX_SMP_BYTES:
                        return None, PeppolDiscoveryEvidence(
                            "smp", "smp_response_too_large", url=url, http_status=status
                        )
                try:
                    count = _service_group(bytes(data), participant)
                except ValueError as ex:
                    return None, PeppolDiscoveryEvidence(
                        "smp", str(ex), url=url, http_status=status
                    )
                return True, PeppolDiscoveryEvidence(
                    "smp",
                    "smp_service_group_found",
                    url=url,
                    http_status=status,
                    services_count=count,
                )
        except TimeoutError:
            return None, PeppolDiscoveryEvidence("smp", "smp_timeout", True, url)
        except (aiohttp.ClientConnectorCertificateError, aiohttp.ClientSSLError):
            return None, PeppolDiscoveryEvidence("smp", "smp_tls_error", False, url)
        except aiohttp.ClientConnectorError as ex:
            code = (
                "smp_unsafe_address"
                if isinstance(ex.os_error, UnsafeSMPAddress)
                else (
                    "smp_dns_error"
                    if isinstance(ex, aiohttp.ClientConnectorDNSError)
                    else "smp_connection_error"
                )
            )
            return None, PeppolDiscoveryEvidence(
                "smp", code, code != "smp_unsafe_address", url
            )
        except (aiohttp.ClientError, OSError):
            return None, PeppolDiscoveryEvidence(
                "smp", "smp_transport_error", True, url
            )
