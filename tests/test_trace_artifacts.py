"""Latency evidence: a diagnosis tool's artifact is admitted only if it re-derives.

RoboTraceOpt is not a task runtime, so driving it as a SUT would be a category
error. It enters this harness the way it really works - as files plus a published
summary - and the harness re-computes every digest itself.

The three outcomes are all covered, because a digest check that only ever passes
proves nothing:

* a matching artifact confirms the *artifact*, not the robot;
* an artifact edited after its summary was written is refuted;
* a summary with no local artifact is insufficient, never a pass.
"""

from __future__ import annotations

import hashlib
import json
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]

from assurance.oracle import evaluate
from assurance.trace_artifacts import (
    SUMMARY_SCHEMA,
    TraceArtifactError,
    count_events,
    load_summary,
    verify_digests,
)


def write_artifact(root: pathlib.Path, *, extra_line: str = "") -> tuple[pathlib.Path, pathlib.Path]:
    """A genuine one-event artifact plus the summary that describes it."""
    root.mkdir(parents=True, exist_ok=True)
    events = root / "normalized.jsonl"
    payload = '{"event_id":"e1","event_type":"ros2:rcl_init"}\n' + extra_line
    events.write_text(payload, encoding="utf-8")
    digest = hashlib.sha256(events.read_bytes()).hexdigest()
    summary = root / "summary.json"
    summary.write_text(json.dumps({
        "schema_version": SUMMARY_SCHEMA,
        "session_id": "test",
        "runtime_event_count": 1,
        "runtime_trace_count": 1,
        "ros2_event_count": 1,
        "accepted_count": 1,
        "background_count": 0,
        "ambiguous_count": 0,
        "accepted_rate": 1.0,
        "clock_status": "comparable",
        "runtime_normalized_sha256": digest,
    }, indent=2), encoding="utf-8")
    return summary, events


def test_a_matching_artifact_is_confirmed() -> None:
    timeline = [{
        "t_ms": 1, "kind": "trace_verified", "requested": True,
        "accepted_count": 1, "digests": [
            {"field": "runtime_normalized_sha256", "status": "match"},
        ],
    }]
    verdict = evaluate([{"claim_id": "c", "kind": "trace_artifact_verified"}], timeline)
    assert verdict["claims"][0]["status"] == "confirmed"
    assert verdict["claims"][0]["reason"] == "trace_digests_verified"


def test_an_edited_artifact_is_refuted(tmp_path: pathlib.Path) -> None:
    summary, events = write_artifact(tmp_path)
    loaded = load_summary(summary)
    events.write_text(events.read_text() + '{"event_id":"e2","event_type":"tampered"}\n')

    digests = verify_digests(loaded, {"runtime_normalized_sha256": events})
    assert digests[0]["status"] == "mismatch"

    timeline = [{"t_ms": 1, "kind": "trace_verified", "requested": True,
                 "accepted_count": 1, "digests": digests}]
    verdict = evaluate([{"claim_id": "c", "kind": "trace_artifact_verified"}], timeline)
    assert verdict["claims"][0]["status"] == "refuted"
    assert verdict["claims"][0]["reason"] == "trace_digest_mismatch"


def test_an_unchecked_digest_is_never_reported_as_ok(tmp_path: pathlib.Path) -> None:
    summary, _events = write_artifact(tmp_path)
    loaded = load_summary(summary)
    digests = verify_digests(loaded, {})
    assert digests[0]["status"] == "not_checked"

    timeline = [{"t_ms": 1, "kind": "trace_verified", "requested": True,
                 "accepted_count": 1, "digests": digests}]
    verdict = evaluate([{"claim_id": "c", "kind": "trace_artifact_verified"}], timeline)
    assert verdict["claims"][0]["status"] == "insufficient_evidence"
    assert verdict["claims"][0]["reason"] == "no_digest_could_be_checked"


def test_a_missing_artifact_is_insufficient_not_a_pass() -> None:
    verdict = evaluate([{"claim_id": "c", "kind": "trace_artifact_verified"}], [])
    assert verdict["claims"][0]["status"] == "insufficient_evidence"
    assert verdict["claims"][0]["reason"] == "no_trace_artifact"


def test_a_below_threshold_accepted_count_is_refuted() -> None:
    timeline = [{"t_ms": 1, "kind": "trace_verified", "requested": True,
                 "accepted_count": 3,
                 "digests": [{"field": "runtime_normalized_sha256", "status": "match"}]}]
    verdict = evaluate([{"claim_id": "c", "kind": "trace_artifact_verified",
                         "min_accepted_count": 10}], timeline)
    assert verdict["claims"][0]["status"] == "refuted"
    assert verdict["claims"][0]["reason"] == "trace_accepted_below_threshold"


def test_an_accepted_count_above_the_event_total_is_refused(tmp_path: pathlib.Path) -> None:
    summary, _events = write_artifact(tmp_path)
    payload = json.loads(summary.read_text())
    payload["accepted_count"] = 99
    summary.write_text(json.dumps(payload))
    with pytest.raises(TraceArtifactError, match="exceeds"):
        load_summary(summary)


def test_a_summary_from_another_schema_is_refused(tmp_path: pathlib.Path) -> None:
    summary, _events = write_artifact(tmp_path)
    payload = json.loads(summary.read_text())
    payload["schema_version"] = "association-evidence-summary/v2"
    summary.write_text(json.dumps(payload))
    with pytest.raises(TraceArtifactError, match="schema_version|declares"):
        load_summary(summary)


def test_event_count_refuses_a_malformed_line(tmp_path: pathlib.Path) -> None:
    events = tmp_path / "events.jsonl"
    events.write_text('{"a":1}\nnot json\n')
    with pytest.raises(TraceArtifactError, match="not valid JSON"):
        count_events(events)


def test_the_committed_robot_trace_opt_fixture_still_parses() -> None:
    """The reader is pinned to a real published summary, not only to our own bytes."""
    from assurance.targets import traceopt_fixture

    fixture = traceopt_fixture()
    if fixture is None:
        pytest.skip("RoboTraceOpt checkout not present (set ASSURANCE_TRACEOPT_ROOT)")
    summary = load_summary(fixture)
    assert summary["runtime_event_count"] == 642
    assert 0.0 <= summary["accepted_rate"] <= 1.0
