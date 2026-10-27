"""The oracle is the product. These tests pin the verdicts it is allowed to give."""

from __future__ import annotations

import json

import pytest

from assurance.oracle import CONFIRMED, INSUFFICIENT, REFUTED, evaluate


def verdict(claims, timeline):
    """Judge claims, defaulting `requires` to L0.

    These cases are about the *claim rules*, not about evidence levels. Level
    behaviour has its own file (tests/test_levels.py) so a change to one does not
    silently rewrite the expectations of the other.
    """
    annotated = [{**claim, "requires": claim.get("requires", "L0")} for claim in claims]
    return evaluate(annotated, timeline)


def test_clean_run_confirms_everything():
    timeline = [
        {"t_ms": 0, "kind": "task_start", "task_id": "A", "epoch": 1, "expect": "done"},
        {"t_ms": 5, "kind": "ack", "task_id": "A", "epoch": 1, "status": "done"},
        {"t_ms": 6, "kind": "device_state", "state": "idle", "level": 1},
    ]
    result = verdict([{"claim_id": "c", "kind": "ack_matches_task", "task_id": "A"}], timeline)
    assert result["claims"][0]["status"] == CONFIRMED
    assert result["passed"] is True


def test_stale_ack_refutes_the_task_it_would_have_confirmed():
    timeline = [
        {"t_ms": 0, "kind": "task_start", "task_id": "A", "epoch": 1},
        {"t_ms": 10, "kind": "task_start", "task_id": "B", "epoch": 2},
        {"t_ms": 20, "kind": "ack", "task_id": "B", "epoch": 1},
        {"t_ms": 30, "kind": "device_state", "state": "idle"},
    ]
    result = verdict([{"claim_id": "c", "kind": "ack_matches_task", "task_id": "B"}], timeline)
    claim = result["claims"][0]
    assert claim["status"] == REFUTED
    assert claim["reason"] == "ack_from_previous_epoch"


def test_no_independent_observation_caps_a_confirmed_verdict():
    """An ACK is a claim by the SUT, not proof. Without a device state it is not confirmed."""
    timeline = [
        {"t_ms": 0, "kind": "task_start", "task_id": "A", "epoch": 1},
        {"t_ms": 5, "kind": "ack", "task_id": "A", "epoch": 1, "status": "done"},
    ]
    result = verdict([{"claim_id": "c", "kind": "ack_matches_task", "task_id": "A"}], timeline)
    assert result["claims"][0]["status"] == INSUFFICIENT
    assert result["claims"][0]["reason"] == "no_independent_observation"


def test_a_harness_requested_exit_is_not_evidence():
    timeline = [
        {"t_ms": 0, "kind": "task_start", "task_id": "A", "epoch": 1},
        {"t_ms": 5, "kind": "ack", "task_id": "A", "epoch": 1, "status": "done"},
        {"t_ms": 9, "kind": "sut_exit", "returncode": 0, "requested": True},
    ]
    result = verdict([{"claim_id": "c", "kind": "ack_matches_task", "task_id": "A"}], timeline)
    assert result["claims"][0]["status"] == INSUFFICIENT


def test_empty_timeline_is_never_a_pass():
    result = verdict([{"claim_id": "c", "kind": "no_false_success", "task_id": "A"}], [])
    assert result["claims"][0]["status"] == INSUFFICIENT
    assert result["passed"] is False


def test_cancel_without_stop_refutes_i4():
    timeline = [
        {"t_ms": 0, "kind": "task_start", "task_id": "A", "epoch": 1},
        {"t_ms": 5, "kind": "cancel_sent", "task_id": "A"},
        {"t_ms": 6, "kind": "device_state", "state": "idle", "level": 1},
    ]
    result = verdict([{"claim_id": "c", "kind": "cancel_then_stop", "task_id": "A"}], timeline)
    assert result["claims"][0]["status"] == REFUTED
    assert result["claims"][0]["reason"] == "cancel_without_stop"


def test_stop_without_ack_refutes_i5():
    timeline = [
        {"t_ms": 0, "kind": "task_start", "task_id": "A", "epoch": 1},
        {"t_ms": 5, "kind": "cancel_sent", "task_id": "A"},
        {"t_ms": 6, "kind": "stop_sent", "task_id": "A"},
        {"t_ms": 7, "kind": "device_state", "state": "idle"},
    ]
    result = verdict([{"claim_id": "c", "kind": "cancel_then_stop", "task_id": "A"}], timeline)
    assert result["claims"][0]["status"] == REFUTED
    assert result["claims"][0]["reason"] == "stop_never_acknowledged"


def test_stop_ack_predating_the_stop_is_refused():
    timeline = [
        {"t_ms": 0, "kind": "task_start", "task_id": "A", "epoch": 1},
        {"t_ms": 5, "kind": "cancel_sent", "task_id": "A"},
        {"t_ms": 6, "kind": "stop_ack", "task_id": "A"},
        {"t_ms": 7, "kind": "stop_sent", "task_id": "A"},
    ]
    result = verdict([{"claim_id": "c", "kind": "cancel_then_stop", "task_id": "A"}], timeline)
    assert result["claims"][0]["status"] == REFUTED
    assert result["claims"][0]["reason"] == "stop_ack_predates_the_stop"


def test_late_ack_is_insufficient_when_we_stopped_recording():
    timeline = [
        {"t_ms": 0, "kind": "task_start", "task_id": "A", "epoch": 1},
        {"t_ms": 5, "kind": "timeout", "task_id": "A"},
    ]
    result = verdict([{"claim_id": "c", "kind": "late_ack_recorded", "task_id": "A",
                       "min_delay_ms": 100}], timeline)
    assert result["claims"][0]["status"] == INSUFFICIENT
    assert result["claims"][0]["reason"] == "late_ack_not_captured"


def test_late_ack_that_completes_an_abandoned_task_is_refuted():
    timeline = [
        {"t_ms": 0, "kind": "task_start", "task_id": "A", "epoch": 1, "expect": "timeout"},
        {"t_ms": 5, "kind": "timeout", "task_id": "A"},
        {"t_ms": 300, "kind": "ack", "task_id": "A", "epoch": 1, "status": "done"},
        {"t_ms": 310, "kind": "device_state", "state": "idle"},
    ]
    result = verdict([{"claim_id": "c", "kind": "no_late_completion", "task_id": "A"}], timeline)
    assert result["claims"][0]["status"] == REFUTED


def test_ack_after_death_is_refuted_even_if_recorded_last():
    timeline = [
        {"t_ms": 0, "kind": "task_start", "task_id": "A", "epoch": 1},
        {"t_ms": 50, "kind": "ack", "task_id": "A", "epoch": 1, "status": "done"},
        {"t_ms": 10, "kind": "sut_killed", "signal": "SIGKILL"},
    ]
    result = verdict([{"claim_id": "c", "kind": "no_ack_after_exit"}], timeline)
    assert result["claims"][0]["status"] == REFUTED
    assert result["claims"][0]["reason"] == "ack_after_process_exit"


def test_unknown_claim_kind_is_a_loud_error():
    with pytest.raises(ValueError):
        verdict([{"claim_id": "c", "kind": "trust_me"}], [])


def test_verdict_is_replayable_from_its_own_files(tmp_path):
    from assurance.evidence import verify_bundle, write_bundle

    timeline = [
        {"t_ms": 0, "kind": "task_start", "task_id": "A", "epoch": 1, "expect": "done"},
        {"t_ms": 5, "kind": "ack", "task_id": "A", "epoch": 1, "status": "done"},
        {"t_ms": 6, "kind": "device_state", "state": "idle", "level": 1},
    ]
    scenario = {"scenario_id": "unit/example", "seed": 1, "transport": "mock",
                "tasks": [{"task_id": "A", "epoch": 1, "resource": "r", "deadline_ms": 10}],
                "claims": [{"claim_id": "c", "kind": "ack_matches_task", "task_id": "A"}]}
    result = evaluate(scenario["claims"], timeline)
    result.update(expected_verdict="pass", observed_verdict="pass", blocked_by=None)
    out = tmp_path / "unit-example--test"
    write_bundle(scenario, timeline, result, [], out)
    assert verify_bundle(out)["ok"] is True

    (out / "deterministic" / "verdict.json").write_text(json.dumps({**result, "digest": "0" * 16}))
    assert verify_bundle(out)["ok"] is False


def test_tampering_with_the_timeline_is_detected(tmp_path):
    from assurance.evidence import verify_bundle, write_bundle

    scenario = {"scenario_id": "unit/tamper", "seed": 1, "transport": "mock",
                "tasks": [{"task_id": "A", "epoch": 1, "resource": "r", "deadline_ms": 10}],
                "claims": [{"claim_id": "c", "kind": "ack_matches_task", "task_id": "A"}]}
    timeline = [{"t_ms": 0, "kind": "device_state", "state": "idle"}]
    result = evaluate(scenario["claims"], timeline)
    result.update(expected_verdict="pass", observed_verdict="insufficient", blocked_by=None)
    out = tmp_path / "unit-tamper--test"
    write_bundle(scenario, timeline, result, [], out)
    assert verify_bundle(out)["ok"] is True
    (out / "deterministic" / "timeline.json").write_text('[{"t_ms": 0, "kind": "sut_killed"}]')
    assert verify_bundle(out)["ok"] is False
