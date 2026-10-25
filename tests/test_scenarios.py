"""End-to-end: every shipped scenario must produce the verdict it says it should.

This is the test that keeps the tool honest. If a negative control stops being
refuted, or a passing scenario starts passing for a different reason, it fails here.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from assurance.registry import registry
from assurance.runner import run_scenario


def needs_ros2(scenario: dict) -> bool:
    return scenario["transport"] != "mock"


def ros2_available() -> bool:
    from assurance.transports import Ros2ActionTransport
    return Ros2ActionTransport().available()[0]


@pytest.fixture(scope="module")
def bundles(tmp_path_factory):
    """Every scenario the host can actually run. ROS 2 ones are skipped, never faked."""
    root = tmp_path_factory.mktemp("bundles")
    results = {}
    for name, path in registry().items():
        scenario = json.loads(path.read_text())
        if needs_ros2(scenario) and not ros2_available():
            continue
        results[name] = run_scenario(scenario, root / name.replace("/", "-"))
    return results


def runnable_names() -> list[str]:
    names = []
    for name, path in registry().items():
        if needs_ros2(json.loads(path.read_text())) and not ros2_available():
            continue
        names.append(name)
    return names


def test_a_ros2_scenario_without_ros2_is_recorded_as_unavailable(tmp_path):
    """If the backend is missing the run must say so, not quietly pass or vanish."""
    scenario = json.loads((pathlib.Path(__file__).parent.parent / "assurance" / "scenarios" /
                           "nominal" / "ros2-task-timeout.json").read_text())
    if ros2_available():
        pytest.skip("ROS 2 is available here; this test is about its absence")
    result = run_scenario(scenario, tmp_path / "no-ros2")
    assert result["passed"] is False
    assert result["blocked_by"] == "transport_unavailable"


@pytest.mark.parametrize("name", runnable_names())
def test_scenario_matches_its_expected_verdict(bundles, name):
    result = bundles[name]
    assert result["sample_matched"] is True, (
        f"{name}: expected {result['expected_verdict']}, got {result['observed_verdict']} "
        f"(blocked by {result['blocked_by']})")
    assert not result["unexercised"], f"{name}: faults never injected: {result['unexercised']}"


@pytest.mark.parametrize("name", runnable_names())
def test_every_bundle_is_self_verifying(bundles, name):
    from assurance.evidence import verify_bundle

    check = verify_bundle(bundles[name]["bundle"])
    assert check["ok"] is True, check


def test_at_least_one_negative_control_exists_and_is_refuted(bundles):
    controls = [r for n, r in bundles.items() if n.startswith("negative-controls/")]
    assert controls, "no negative control: a harness that never fails proves nothing"
    assert all(r["observed_verdict"] == "refute" for r in controls)


def test_at_least_one_insufficient_case_exists(bundles):
    cases = [r for r in bundles.values() if r["observed_verdict"] == "insufficient"]
    assert cases, "no insufficient_evidence case: the three-state verdict is untested"


def test_faults_are_never_silently_skipped(bundles):
    for name, result in bundles.items():
        assert all(c["exercised"] for c in
                   json.loads((__import__("pathlib").Path(result["bundle"]) /
                               "deterministic" / "coverage.json").read_text())), name
