from __future__ import annotations

from pathlib import Path

from tests.scenarios.generate_sign_archive import ELECTRONIC_SEAL_POLICY
from tests.scenarios.generate_sign_archive import PADES_B_B_POLICY
from tests.scenarios.generate_sign_archive import PADES_B_T_POLICY
from tests.scenarios.generate_sign_archive import flow
from xarta.crypto.sign.configuration import load_signature_configuration

ROOT = Path(__file__).parents[2]
SCENARIO_POLICIES = (ELECTRONIC_SEAL_POLICY, PADES_B_B_POLICY, PADES_B_T_POLICY)


def test_every_configured_signature_policy_has_a_scenario() -> None:
    configuration = load_signature_configuration(str(ROOT / ".dev/conf/signature.json"))

    assert {policy.id for policy in configuration.policies.values()} == {
        policy.id for policy in SCENARIO_POLICIES
    }


def test_policy_scenarios_pin_the_requested_policy() -> None:
    for policy in SCENARIO_POLICIES:
        definition, _ = flow(policy)
        signature_node = definition["dag"]["on"]["success"][0]

        assert signature_node["policy"] == policy.id
