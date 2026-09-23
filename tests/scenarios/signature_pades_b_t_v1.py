"""Run the generate/sign/archive scenario with PAdES B-T."""

from __future__ import annotations

from tests.scenarios.generate_sign_archive import PADES_B_T_POLICY
from tests.scenarios.generate_sign_archive import run

if __name__ == "__main__":
    run(PADES_B_T_POLICY)
