"""I2 proved from both sides.

The invariant says a policy proposal cannot bypass the executor and reach a device
directly. Previously this was only a structural claim - the harness has no code path
to a device - which meant nothing could fail if a SUT had no executor boundary at
all.

Now a scenario can send one raw actuator command and the SUT's answer is judged:

* a device that refuses it confirms the boundary exists;
* a device that obeys it is refuted, which is the negative control.

The rule that makes this honest: a run that never sent the probe is
`insufficient_evidence`. Silence is not compliance.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]


def run(scenario: str, tag: str) -> tuple[dict, dict]:
    env = {**os.environ, "PYTHONPATH": str(ROOT), "ASSURANCE_RUN_ID": tag}
    result = subprocess.run(
        [sys.executable, "-m", "assurance.cli", "run", scenario],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=300,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    bundle = ROOT / "runs" / f"{scenario.replace('/', '-')}--{tag}"
    verdict = json.loads((bundle / "deterministic/verdict.json").read_text())
    return verdict, json.loads((bundle / "deterministic/timeline.json").read_text())


def test_a_refusing_device_confirms_the_executor_boundary() -> None:
    verdict, timeline = run("policy/raw-command-refused", "i2-ok")
    claim = verdict["claims"][0]
    assert claim["status"] == "confirmed"
    assert claim["reason"] == "device_refused_every_raw_command"
    assert any(e["kind"] == "raw_command" and not e["accepted"] for e in timeline)


def test_a_device_that_obeys_a_raw_command_is_refuted() -> None:
    verdict, timeline = run("negative-controls/sut-accepts-raw-command", "i2-bad")
    claim = verdict["claims"][0]
    assert claim["status"] == "refuted"
    assert claim["reason"] == "device_obeyed_a_raw_command"
    assert any(e["kind"] == "raw_command" and e["accepted"] for e in timeline)


def test_no_probe_means_insufficient_not_compliance() -> None:
    from assurance.oracle import evaluate

    verdict = evaluate([{"claim_id": "c", "kind": "raw_command_refused"}], [])
    assert verdict["claims"][0]["status"] == "insufficient_evidence"
    assert verdict["claims"][0]["reason"] == "no_raw_probe_sent"


def test_a_probe_with_no_answer_is_insufficient() -> None:
    """A device that stays silent about the probe has not demonstrated anything."""
    from assurance.oracle import evaluate

    timeline = [{"t_ms": 1, "kind": "note", "detail": "raw command sent", "joint": "joint_1"}]
    verdict = evaluate([{"claim_id": "c", "kind": "raw_command_refused"}], timeline)
    assert verdict["claims"][0]["status"] == "insufficient_evidence"
    assert verdict["claims"][0]["reason"] == "no_raw_verdict_recorded"


def test_the_mock_sut_really_implements_both_policies() -> None:
    """The negative control is only meaningful if the SUT can actually misbehave."""
    from assurance.sut import mock_sut

    source = pathlib.Path(mock_sut.__file__).read_text()
    assert '"reject"' in source and '"accept"' in source
    assert "raw_accepted" in source and "raw_rejected" in source
