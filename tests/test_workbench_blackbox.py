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
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
from assurance.targets import workbench_root

SUT_ROOT = workbench_root()

needs_sut = pytest.mark.skipif(
    SUT_ROOT is None,
    reason="no workbench checkout found (set ASSURANCE_WORKBENCH_ROOT)",
)


def run_cli(*args: str) -> tuple[dict, int]:
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    result = subprocess.run(
        [sys.executable, "-m", "assurance.cli", *args],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=600, check=False,
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


def run_into(tmp_path: pathlib.Path, scenario: str) -> tuple[dict, pathlib.Path]:
    """Run one scenario into a directory this test owns.

    Never into the repository's `runs/`: that directory is gitignored, so reading
    it means a fresh clone fails and a developer's machine can pass on artifacts a
    manual run left behind. That is exactly how this test was wrong the first time.
    """
    out = tmp_path / "runs"
    result = subprocess.run(
        [sys.executable, "-m", "assurance.cli", "run", scenario, "--out", str(out)],
        cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT)},
        capture_output=True, text=True, timeout=600, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout[result.stdout.index("{"):])
    return payload, pathlib.Path(payload["bundle"])


@needs_sut
def test_the_normal_run_publishes_a_verifiable_bundle(tmp_path: pathlib.Path) -> None:
    _payload, bundle = run_into(tmp_path, "workbench/normal-task")
    timeline = json.loads((bundle / "deterministic/timeline.json").read_text())
    verified = [e for e in timeline if e["kind"] == "bundle_verified"]
    assert verified, "the harness must hash the SUT's bundle itself"
    published = [e for e in timeline if e.get("detail") == "workbench run finished"]
    assert published[0]["bundle_verified"] is True
    assert published[0]["sut_scenario"] == "normal-001"


@needs_sut
def test_a_killed_run_publishes_nothing_and_claims_nothing(tmp_path: pathlib.Path) -> None:
    _payload, bundle = run_into(tmp_path, "workbench/runtime-killed")
    timeline = json.loads((bundle / "deterministic/timeline.json").read_text())
    deaths = [e for e in timeline if e["kind"] == "sut_killed"]
    assert deaths, "the kill must be recorded as an observation"
    assert not any(e["kind"] == "bundle_verified" for e in timeline), "a killed run must not publish a bundle"

    verdict = json.loads((bundle / "deterministic/verdict.json").read_text())
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
    """A black-box verdict cannot rest on shared code: the SUT must not be importable here.

    Checked on the parse tree rather than on the text, because a docstring that
    *talks about* importing workbench is not an import. The text version of this test
    failed for exactly that reason, which is a good argument for not grepping code.
    """
    import ast

    assert "workbench" not in sys.modules
    for path in (ROOT / "assurance").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("workbench"), f"{path} imports {alias.name}"
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith("workbench"), f"{path} imports {node.module}"
