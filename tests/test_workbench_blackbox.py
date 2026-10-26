"""The black-box path: the harness drives a real SUT it never imports.

These tests run `workbench-mobile-home-robot` through its published CLI and read
the evidence bundle it writes. They are skipped, not faked, when that checkout is
absent: a passing test must mean a real run happened.

The three assertions that matter:

* a normal run publishes a bundle whose files match the SUT's own checksums;
* a SIGKILLed run publishes **nothing**, and the harness says so rather than
  inventing a terminal state;
* the SUT's own `device_state` and `task_terminal` never become an independent
  observation, so a subject's self-report cannot confirm its own claim.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SUT_ROOT = pathlib.Path(os.environ.get("ASSURANCE_WORKBENCH_ROOT", "/data/builds/caogao/wmhr"))
SHIM = os.environ.get("ASSURANCE_WORKBENCH_SHIM", "/data/builds/caogao/.wbshim")

needs_sut = pytest.mark.skipif(
    not (SUT_ROOT / "tools/scripts/sim_cli.py").is_file(),
    reason=f"workbench checkout not found at {SUT_ROOT}",
)


def run_cli(*args: str) -> tuple[dict, int]:
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    result = subprocess.run(
        [sys.executable, "-m", "assurance.cli", *args],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=600,
    )
    payload = {}
    for chunk in result.stdout.strip().splitlines()[::-1]:
        if chunk.strip().startswith("{"):
            try:
                payload = json.loads(result.stdout[result.stdout.index(chunk):])
            except json.JSONDecodeError:
                continue
            break
    return payload, result.returncode


def bundle_of(scenario_id: str) -> pathlib.Path:
    stamp = os.environ.get("ASSURANCE_RUN_ID") or "local"
    return ROOT / "runs" / f"{scenario_id}--{stamp}"


@needs_sut
def test_the_normal_run_publishes_a_verifiable_bundle(tmp_path: pathlib.Path) -> None:
    env = {**os.environ, "PYTHONPATH": str(ROOT), "ASSURANCE_RUN_ID": "test-normal"}
    result = subprocess.run(
        [sys.executable, "-m", "assurance.cli", "run", "workbench/normal-task"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=600,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    timeline = json.loads((bundle_of("workbench-normal-task") / "deterministic/timeline.json").read_text())
    verified = [e for e in timeline if e["kind"] == "bundle_verified"]
    assert verified, "the harness must hash the SUT's bundle itself"
    published = [e for e in timeline if e.get("detail") == "workbench run finished"]
    assert published[0]["bundle_verified"] is True
    assert published[0]["sut_scenario"] == "normal-001"


@needs_sut
def test_a_killed_run_publishes_nothing_and_claims_nothing(tmp_path: pathlib.Path) -> None:
    env = {**os.environ, "PYTHONPATH": str(ROOT), "ASSURANCE_RUN_ID": "test-killed"}
    result = subprocess.run(
        [sys.executable, "-m", "assurance.cli", "run", "workbench/runtime-killed"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=600,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    timeline = json.loads((bundle_of("workbench-runtime-killed") / "deterministic/timeline.json").read_text())
    deaths = [e for e in timeline if e["kind"] == "sut_killed"]
    assert deaths, "the kill must be recorded as an observation"
    assert not any(e["kind"] == "bundle_verified" for e in timeline), "a killed run must not publish a bundle"

    verdict = json.loads((bundle_of("workbench-runtime-killed") / "deterministic/verdict.json").read_text())
    claim = verdict["claims"][0]
    assert claim["status"] == "confirmed"
    assert claim["reason"] == "kill_observed_and_no_ack_followed"


@needs_sut
def test_a_bundle_published_after_the_kill_is_a_refutation() -> None:
    """The rule must fire when it should, not just pass on the happy path."""
    from assurance.oracle import evaluate

    timeline = [
        {"t_ms": 10, "kind": "sut_killed", "code": -9},
        {"t_ms": 11, "kind": "note", "detail": "post-kill leftovers",
         "published": ["normal-001-deadbeef"], "partial": []},
    ]
    verdict = evaluate([{"claim_id": "c", "kind": "kill_observed"}], timeline)
    assert verdict["claims"][0]["status"] == "refuted"
    assert verdict["claims"][0]["reason"] == "bundle_published_after_kill"


def test_a_subject_self_report_is_never_an_independent_observation() -> None:
    """The SUT's ACK and device_state are its claims, not evidence."""
    from assurance.oracle import evaluate

    timeline = [
        {"t_ms": 1, "kind": "task_start", "task_id": "T1", "epoch": "run-a"},
        {"t_ms": 2, "kind": "ack", "task_id": "T1", "epoch": "run-a", "status": "done",
         "requested": True, "subject_claim": True},
        {"t_ms": 3, "kind": "device_state", "task_id": "T1", "state": "idle",
         "requested": True, "subject_claim": True},
    ]
    verdict = evaluate([{"claim_id": "c", "kind": "ack_matches_task", "task_id": "T1"}], timeline)
    assert verdict["claims"][0]["status"] == "insufficient_evidence"
    assert verdict["claims"][0]["reason"] == "no_independent_observation"


def test_the_harness_never_imports_the_subject() -> None:
    """A black-box verdict cannot rest on shared code: the SUT must not be importable here."""
    assert "workbench" not in sys.modules
    source = (ROOT / "assurance/sut/workbench.py").read_text()
    assert "import workbench" not in source
    assert "from workbench" not in source
