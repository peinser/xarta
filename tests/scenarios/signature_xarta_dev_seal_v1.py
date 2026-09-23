"""Run the generate/sign/archive scenario for xarta-dev-seal-v1."""

from __future__ import annotations

from tests.scenarios.generate_sign_archive import ELECTRONIC_SEAL_POLICY
from tests.scenarios.generate_sign_archive import run

if __name__ == "__main__":
    run(ELECTRONIC_SEAL_POLICY)
