"""Run the generate/sign/archive scenario for pades-b-b-v1."""

from __future__ import annotations

from tests.scenarios.generate_sign_archive import PADES_B_B_POLICY
from tests.scenarios.generate_sign_archive import run

if __name__ == "__main__":
    run(PADES_B_B_POLICY)
