# Vendored UBL and Peppol validation resources

These are unmodified upstream resources, not Xarta implementations of the rules.

- `xsd/`: OASIS UBL 2.1 Invoice/CreditNote main schemas and their complete common
  schema dependencies. Source: https://docs.oasis-open.org/ubl/os-UBL-2.1/UBL-2.1.zip.
  Copyright and redistribution notices are retained within the XSD files,
  including the OASIS, UN/CEFACT, W3C, and ETSI notices applicable to each schema.
- `billing-3/2026.05/`: EN 16931 and Peppol BIS Billing 3 compiled Schematron XSLT
  from the May 2026 release, distributed by Philip Helger's `phive-rules-peppol`.
  The original LICENSE and NOTICE are retained under `billing-3/`.
- `billing-3/examples/`: upstream invoice and credit-note examples used in tests.

`manifest.json` records immutable upstream identities and SHA-256 checksums.
`python tools/vendor_ubl.py --check` verifies files offline. Maintainers can
reproduce the import with `python tools/vendor_ubl.py`; review pinned URLs,
checksums, licensing notices, and release applicability before updating it.
There are no runtime downloads or automatic rule updates.

The official rules include the Belgian enterprise-number rule
`PEPPOL-COMMON-R043` for identifiers with scheme `0208`, and the Peppol attachment
MIME subset (`PEPPOL-EN16931-CL001`). A country-specific legal/tax compliance
opinion is outside the scope of these validators.
