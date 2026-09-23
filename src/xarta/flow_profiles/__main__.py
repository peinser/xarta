from __future__ import annotations

import argparse

from pathlib import Path

import orjson

from .configuration import calculate_flow_profile_fingerprint
from .configuration import parse_flow_profiles


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate Xarta flow profiles")
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser("validate")
    validate.add_argument("path")
    fingerprint = subparsers.add_parser("fingerprint")
    fingerprint.add_argument("path")
    fingerprint.add_argument("name")
    fingerprint.add_argument("version", type=int)
    lock = subparsers.add_parser("validate-lock")
    lock.add_argument("path")
    lock.add_argument("lock_path")
    history = subparsers.add_parser("check-lock-history")
    history.add_argument("current_lock_path")
    history.add_argument("baseline_lock_path")
    arguments = parser.parse_args()

    if arguments.command == "check-lock-history":
        current = _load_lock(arguments.current_lock_path)
        baseline = _load_lock(arguments.baseline_lock_path)
        for reference, fingerprint_value in baseline.items():
            received = current.get(reference)
            if received != fingerprint_value:
                raise SystemExit(
                    f"Immutable flow profile lock changed for {reference}: "
                    f"expected {fingerprint_value}, received {received}"
                )
        print(f"Preserved {len(baseline)} historical flow profile locks")
        return

    configured = orjson.loads(Path(arguments.path).read_bytes())
    if arguments.command == "validate":
        registry = parse_flow_profiles(configured)
        print(f"Validated {len(registry.profiles)} immutable flow profile versions")
        return
    if arguments.command == "validate-lock":
        registry = parse_flow_profiles(configured)
        locked = _load_lock(arguments.lock_path)
        for profile in registry.profiles.values():
            reference = str(profile.reference)
            expected = locked.get(reference)
            if expected != profile.fingerprint:
                raise SystemExit(
                    f"Flow profile lock mismatch for {reference}: expected "
                    f"{expected}, received {profile.fingerprint}"
                )
        print(f"Validated {len(registry.profiles)} flow profile locks")
        return
    definition = configured["profiles"][arguments.name]["versions"][
        str(arguments.version)
    ]
    print(
        calculate_flow_profile_fingerprint(
            arguments.name, arguments.version, definition
        )
    )


def _load_lock(path: str) -> dict[str, str]:
    value = orjson.loads(Path(path).read_bytes())
    if not isinstance(value, dict) or any(
        not isinstance(reference, str)
        or not reference
        or not isinstance(fingerprint, str)
        or not fingerprint.startswith("sha256:")
        for reference, fingerprint in value.items()
    ):
        raise SystemExit(
            "Flow profile lock must map references to SHA-256 fingerprints"
        )
    return value


if __name__ == "__main__":
    main()
