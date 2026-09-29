# Direct Peppol participant registration discovery

```http
GET /api/v1/peppol/participants/0208/0308357159/registration
```

This read-only API performs SML DNS discovery followed by an SMP ServiceGroup GET.
It does not use a provider directory or infer registration from DNS alone. The path
scheme is the four-digit ISO 6523 ICD. The wire-level Peppol participant scheme is
`iso6523-actorid-upis`. `/api/peppol/` exposes the same endpoint.

## Three-state contract

```json
{
  "participant": {"scheme": "0208", "identifier": "0308357159"},
  "environment": "production",
  "dns_name": "<hash>.iso6523-actorid-upis.participant.sml.prod.tech.peppol.org.",
  "registered": null,
  "status": "indeterminate",
  "definitive": false,
  "retryable": true,
  "checked_at": "2026-09-28T12:00:00+00:00",
  "scope": "SML discovery and SMP ServiceGroup registration, not Access Point liveness or document support",
  "evidence": [
    {"stage": "dns", "code": "dns_records_found", "retryable": false, "ttl": 120},
    {"stage": "smp", "code": "smp_timeout", "retryable": true, "url": "https://smp.example/<encoded-participant>"}
  ]
}
```

- `registered: true` / `status: registered`: a discovered SMP returned the matching
  ServiceGroup.
- `registered: false` / `status: not_registered`: a conclusive protocol negative
  was observed, as defined below.
- `registered: null` / `status: indeterminate`: technical or protocol errors prevent
  a reliable positive/negative answer.

HTTP **200** means an observation completed, not that the participant is registered.
Invalid participant syntax returns **400**. Responses use `Cache-Control: no-store`.
`retryable` marks transient indeterminate results. A non-retryable indeterminate
result generally requires a configuration/protocol correction.

## Procedure and current SML zones

The implementation follows Helger's `PeppolNaptrURLProvider`,
`AbstractBDXLURLProvider`, and `SMPClientReadOnly.getServiceGroup`:

1. Lowercase `0208:0308357159`, UTF-8 encode, SHA-256 hash, Base32 encode without
   padding, and lowercase the encoded label.
2. Append `.iso6523-actorid-upis.<SML zone>.` and resolve NAPTR. The name is absolute;
   OS DNS search domains are disabled.
3. Select `Meta:SMP`, the lowest matching order, and ascending preference within that
   order. Terminal `U` records must use the empty (`.`) replacement name and a U-NAPTR
   regexp carrying an HTTP(S) URI as a constant: `!.*!<URI>!` (RFC 4848, as the SML
   publishes it) or the anchored `!^.*$!<URI>!` of the OASIS BDXL examples. Other
   expressions are rejected; no regular expression is executed. Never move to a
   higher order once a matching order is selected. Contact at most four alternatives.
4. GET `<SMP base>/<percent-encoded iso6523-actorid-upis::0208:0308357159>`.
5. Require the SMP ServiceGroup namespace, exactly one matching ParticipantIdentifier,
   and a ServiceMetadataReferenceCollection. A 200 with HTML, malformed XML, a wrong
   participant, or an unexpected root is inconclusive. An empty collection still
   establishes registration, but not document-type support.

Current environments use the Peppol SML zones introduced in March 2026:

- Production: `participant.sml.prod.tech.peppol.org.`
- Test: `participant.sml.test.tech.peppol.org.`

The deployment chooses the environment. Callers cannot supply an arbitrary DNS zone
or SMP URL. There is no fallback to former DIGIT zones or legacy MD5/CNAME discovery,
which could answer a different network question.

## Definitive negatives versus errors

| Observation | Result |
| --- | --- |
| NXDOMAIN with applicable SOA-backed negative proof | `false`, `dns_nxdomain` |
| NOERROR/NODATA for NAPTR with applicable SOA-backed negative proof | `false`, `dns_no_naptr` |
| Empty/referral reply without negative proof, or missing alias target | `null`, `dns_unproven_negative` |
| DNS timeout, SERVFAIL, REFUSED, other failure | `null`, distinct `dns_timeout`, `dns_servfail`, `dns_refused`, or `dns_failure` |
| NAPTR records but no `Meta:SMP` service | `null`, `dns_no_matching_smp_service` |
| Invalid record, URL, regexp, flags, or excessive alternatives | `null`, explicit `dns_*` protocol error |
| Direct HTTP 404 from all selected usable alternatives | `false`, `smp_not_found` |
| Matching ServiceGroup from any selected alternative | `true`, `smp_service_group_found` |
| Timeout, connection/DNS failure, TLS error, 401/403, 429, 5xx | `null`, explicit `smp_*` error |
| HTTP redirect | `null`, `smp_redirect`; redirects are not followed |
| Malformed, oversized, encoded, or mismatched response | `null`, explicit protocol error |
| Overall deadline, including admission queue | `null`, `discovery_timeout` |

With multiple alternatives, one positive establishes registration. A negative requires
all alternatives to be definitively negative. One 404 plus one timeout remains
indeterminate. Evidence from multiple attempts is preserved.

“Definitive” describes a **point-in-time protocol observation in this environment**,
not a permanent or independently cryptographically proven fact. The recursive DNS
resolver is trusted; Xarta does not independently validate DNSSEC. ServiceGroup
responses are unsigned by the SMP protocol. HTTPS certificates are verified whenever
HTTPS is used. Caches and participant migrations can change later observations.
A DNS negative establishes absence from discovery, not the absence of orphaned
records on every possible SMP.

This API does not ping an Access Point, verify signed service metadata, select an
invoice/process/transport profile, or guarantee delivery. Those are separate checks.

## HTTP sessions and resource bounds

`HTTPRequestManager` creates the normal shared session **per application process**,
available as `app.ctx.http_client_session` and the legacy global class reference.
Multiple services in a standalone process share it; separately deployed workers do not.

Peppol discovery owns an additional **long-lived application-scoped session**, created
once at service startup and closed at shutdown, including when both version aliases
are loaded. It uses the same factory, so its default User-Agent is configured before
the first request: `Xarta/<installed-version> (+https://github.com/peinser/xarta)`.
There is no session creation per participant lookup. The separate pool is necessary
because discovery uses a public-address-only resolver while internal service calls
must reach private deployment addresses.

The dedicated connector checks the actual addresses handed to aiohttp, rejecting any
non-public address; it does not validate once and then resolve again. Connection reuse
is enabled, DNS caching is disabled, ambient proxies/cookies are disabled, and redirects
are not followed. Only HTTP(S), ports 80/443, and credential/query/fragment-free base
URLs are accepted. Private literal addresses and private DNS results are rejected.
Metadata-reference URLs from the ServiceGroup are not fetched.

Responses are limited to 1 MiB and must be uncompressed. XML parsing rejects
DTDs/entities and disables network/entity expansion. No registration cache, database
record, workflow task, submission, callback, or reconciliation job is created.
DNS TTLs, including SOA-derived negative TTLs, are returned as evidence.

| Environment variable | Default |
| --- | --- |
| `PEPPOL_DISCOVERY_ENVIRONMENT` | `production`; `test` also supported |
| `PEPPOL_DISCOVERY_DNS_TIMEOUT` | 5 seconds |
| `PEPPOL_DISCOVERY_HTTP_TIMEOUT` | 10 seconds per SMP attempt |
| `PEPPOL_DISCOVERY_TOTAL_TIMEOUT` | 20 seconds across queue/DNS/SMP attempts |
| `PEPPOL_DISCOVERY_CONCURRENCY` | 10 per process |

Compose explicitly configures these variables and defaults development to `test`.
The split Compose stack provides a Peppol service behind its `peppol` profile.
Helm equivalents are under `services.peppol.discovery` and default to production.
These settings are independent of the configured sending provider's environment.

## Upstream references

Pinned interoperability reference: `phax/peppol-commons` commit
`8cb54fbe974cffcfc67d0ef88565da9b883939b2`:

- [U-NAPTR algorithm](https://github.com/phax/peppol-commons/blob/8cb54fbe974cffcfc67d0ef88565da9b883939b2/peppol-smp-client/src/main/java/com/helger/smpclient/url/dns/PeppolNaptrURLProvider.java)
- [DNS/error semantics](https://github.com/phax/peppol-commons/blob/8cb54fbe974cffcfc67d0ef88565da9b883939b2/peppol-smp-client/src/main/java/com/helger/smpclient/url/dns/AbstractBDXLURLProvider.java)
- [SMP ServiceGroup lookup](https://github.com/phax/peppol-commons/blob/8cb54fbe974cffcfc67d0ef88565da9b883939b2/peppol-smp-client/src/main/java/com/helger/smpclient/peppol/SMPClientReadOnly.java)
- [Current SML environments](https://github.com/phax/peppol-commons/blob/8cb54fbe974cffcfc67d0ef88565da9b883939b2/peppol-commons/src/main/java/com/helger/peppol/sml/ESML.java)
