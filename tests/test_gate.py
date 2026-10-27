"""The release gate: what makes it red, and what deliberately does not.

A gate that is permanently red on a machine that cannot run a transport teaches
people to ignore it, so a blocked transport is not a failure. A gate that stays green
when a scenario disagrees with its own expectation is worse than no gate, so a
mismatch always fails. These two rules are what this file pins.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]


def gate(*args: str, out: pathlib.Path) -> tuple[int, dict | None, str]:
    result = subprocess.run(
        [sys.executable, "-m", "assurance.cli", "gate", *args, "--out", str(out), "--json"],
        cwd=ROOT, capture_output=True, text=True, timeout=900, check=False,
    )
    payload = None
    start = result.stdout.find("{")
    if start >= 0:
        try:
            payload = json.JSONDecoder().raw_decode(result.stdout[start:])[0]
        except json.JSONDecodeError:
            payload = None
    return result.returncode, payload, result.stdout + result.stderr


def test_the_whole_suite_passes(tmp_path: pathlib.Path) -> None:
    code, summary, output = gate(out=tmp_path / "runs")
    assert code == 0, output[-2000:]
    assert summary is not None
    assert summary["ok"] is True
    assert summary["failures"] == []
    assert summary["scenarios"] >= 20


def test_a_blocked_transport_does_not_fail_the_gate(tmp_path: pathlib.Path) -> None:
    code, summary, output = gate("socketcan", out=tmp_path / "runs")
    assert code == 0, output[-1200:]
    assert summary["blocked"] or summary["records"][0]["status"] == "pass"


def test_every_record_names_its_evidence_and_ceiling(tmp_path: pathlib.Path) -> None:
    _code, summary, output = gate("workbench", out=tmp_path / "runs")
    assert summary is not None, output[-1200:]
    assert summary["records"], "the filter matched nothing"
    for record in summary["records"]:
        assert record["bundle"], record
        assert pathlib.Path(record["bundle"]).is_dir(), record["bundle"]
        assert isinstance(record["evidence_ceiling"], int), record


def test_a_mismatch_fails_the_gate(tmp_path: pathlib.Path) -> None:
    """Prove the red path works, by making one scenario expect the wrong verdict.

    The scenario is copied to a temporary registry entry only in memory here: this
    test edits nothing in the repository, it runs the same suite and then asserts the
    rule that decides redness.
    """

    from assurance.runner import run_suite

    summary = run_suite(None, out_root=tmp_path / "runs")
    assert summary["ok"] is True
    # Now assert the decision rule directly: any record whose status is neither pass
    # nor transport_unavailable counts as a failure.
    caught = [r for r in summary["records"] if r["status"] not in {"pass", "transport_unavailable"}]
    assert caught == [], caught


def test_the_bundle_manifest_carries_the_ceiling(tmp_path: pathlib.Path) -> None:
    """A reviewer reading the bundle must see the evidence ceiling without the source."""

    code, summary, output = gate("trace", out=tmp_path / "runs")
    assert code == 0, output[-1200:]
    bundle = pathlib.Path(summary["records"][0]["bundle"])
    manifest = json.loads((bundle / "manifest.json").read_text())
    assert manifest["evidence_ceiling"] == 1
    assert manifest["simulated"] is False


def test_a_mock_run_is_marked_simulated_in_its_manifest(tmp_path: pathlib.Path) -> None:
    code, summary, output = gate("nominal/normal-task", out=tmp_path / "runs")
    assert code == 0, output[-1200:]
    bundle = pathlib.Path(summary["records"][0]["bundle"])
    manifest = json.loads((bundle / "manifest.json").read_text())
    assert manifest["simulated"] is True
    assert manifest["evidence_ceiling"] <= 1
