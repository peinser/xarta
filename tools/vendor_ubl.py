"""Import pinned upstream validation assets, or verify the checked-in copies.

Run with --check for an offline integrity check. Downloads only occur when this
maintenance tool is explicitly invoked without --check, never in the service.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import urllib.request
import zipfile

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "src/xarta/ubl/resources"
COMMIT = "000e6fb9aba9cd6ca4586ef4174dc8bcd5dc9c55"
UPSTREAM = f"https://raw.githubusercontent.com/phax/phive-rules/{COMMIT}/phive-rules-peppol/src"
UBL_URL = "https://docs.oasis-open.org/ubl/os-UBL-2.1/UBL-2.1.zip"
UBL_SHA256 = "60b80d76394a8a2add90723ecb8e0e2e9d826775de9749df37a72d60703f86ed"
RULES = {
    "CEN-EN16931-UBL.xslt": "06a850a48703faa7e38cbcc7a8ded0f0645a941a0ee448598a02ae7197b86356",
    "PEPPOL-EN16931-UBL.xslt": "482a9d98062426d901194b754ecd0665b2c9e519fe3da8b7787477f0fc4ada6e",
}


def download(url: str, expected: str | None = None) -> bytes:
    with urllib.request.urlopen(url, timeout=120) as response:
        data = response.read()
    if expected is not None and hashlib.sha256(data).hexdigest() != expected:
        raise ValueError(f"Checksum mismatch for {url}")
    return data


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        manifest = json.loads((ASSETS / "manifest.json").read_text())
        for name, digest in manifest["files"].items():
            if hashlib.sha256((ASSETS / name).read_bytes()).hexdigest() != digest:
                raise ValueError(f"Vendored validation resource changed: {name}")
        print(f"Verified {len(manifest['files'])} vendored validation resources")
        return
    files = {}
    archive = zipfile.ZipFile(io.BytesIO(download(UBL_URL, UBL_SHA256)))
    for name in archive.namelist():
        if name.endswith(".xsd") and (
            name.startswith("xsd/common/")
            or name
            in {"xsd/maindoc/UBL-Invoice-2.1.xsd", "xsd/maindoc/UBL-CreditNote-2.1.xsd"}
        ):
            files[name] = archive.read(name)
    for name, digest in RULES.items():
        files[f"billing-3/2026.05/{name}"] = download(
            f"{UPSTREAM}/main/resources/external/schematron/openpeppol/2026.5/xslt/{name}",
            digest,
        )
    for name in ("LICENSE", "NOTICE"):
        files[f"billing-3/{name}"] = download(f"{UPSTREAM}/main/resources/{name}")
    for name in ("base-example.xml", "base-creditnote-correction.xml"):
        files[f"billing-3/examples/{name}"] = download(
            f"{UPSTREAM}/test/resources/external/test-files/openpeppol/2026.5/billing/{name}"
        )
    for name, data in files.items():
        destination = ASSETS / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
    manifest = {
        "ubl": {"version": "2.1", "url": UBL_URL, "sha256": UBL_SHA256},
        "billing": {"release": "2026.05", "phive_rules_commit": COMMIT},
        "files": {
            name: hashlib.sha256(data).hexdigest()
            for name, data in sorted(files.items())
        },
    }
    (ASSETS / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"Imported {len(files)} pinned upstream resources")


if __name__ == "__main__":
    main()
