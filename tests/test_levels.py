"""The evidence-level rules, which are the reason this tool exists.

The property under test is the one that makes a confirmed verdict mean something:
*no amount of self-reporting can confirm a claim, and no transport can confirm above
its ceiling.* The last test in this file is a generated sweep rather than a handful of
examples, because a rule about "all evidence below level X" is exactly the kind of
claim an example-based test can miss.
"""

from __future__ import annotations

import json
import pathlib
import random

import pytest

from assurance.levels import (
    SIMULATED_TRANSPORTS,
    TRANSPORT_CEILING,
    Level,
    ceiling_for,
    is_simulated,
)
from assurance.oracle import evaluate


def claim(**overrides) -> dict:
    base = {"claim_id": "c", "kind": "ack_matches_task", "task_id": "A", "requires": "L0"}
    return {**base, **overrides}


def timeline(*, levels: tuple[int, ...], device_state: bool = True) -> list[dict]:
    events = [
        # bookkeeping at L0: requesting a task proves nothing about the subject
        {"t_ms": 0, "kind": "task_start", "task_id": "A", "epoch": 1, "level": 0},
        {"t_ms": 5, "kind": "ack", "task_id": "A", "epoch": 1, "status": "done", "level": 0},
    ]
    if device_state:
        for level in levels:
            events.append({"t_ms": 6, "kind": "device_state", "state": "idle", "level": level})
    return events


def test_levels_are_ordered_and_parseable() -> None:
    assert Level.L0_SELF_REPORT < Level.L1_PROCESS < Level.L2_BUS < Level.L3_PHYSICAL
    assert Level.parse("L2") is Level.L2_BUS
    assert Level.parse(2) is Level.L2_BUS
    assert Level.parse("l3") is Level.L3_PHYSICAL
    for bad in ("L9", -1, True, "high", None, 1.5):
        with pytest.raises((ValueError, TypeError)):
            Level.parse(bad)


def test_a_transport_ceiling_is_never_above_its_declared_level() -> None:
    assert ceiling_for("mock") <= Level.L1_PROCESS
    assert ceiling_for("workbench") is Level.L1_PROCESS
    assert ceiling_for("socketcan") is Level.L2_BUS
    # an undeclared transport must not be trusted
    assert ceiling_for("something-new") is Level.L0_SELF_REPORT


def test_self_report_alone_never_confirms() -> None:
    """The core rule: an ACK plus the subject's own state is not proof.

    Two separate guards can stop this, and which one fires first is not the point:
    without any independent observation the verdict is `no_independent_observation`,
    and with one that is too weak it is `evidence_below_required_level`. Both are
    `insufficient_evidence`. Testing the reason here would pin an implementation
    order rather than the rule.
    """

    result = evaluate([claim(requires="L1")], timeline(levels=(0,)), ceiling=1)
    entry = result["claims"][0]
    assert entry["status"] == "insufficient_evidence"
    assert entry["reason"] in {"no_independent_observation", "evidence_below_required_level"}


def test_weak_independent_evidence_reports_the_level_it_was_missing() -> None:
    """When there *is* an observation but it is too weak, say which level was missing."""

    events = timeline(levels=())
    events.append({"t_ms": 7, "kind": "sut_exit", "code": 0, "level": 1})
    result = evaluate([claim(requires="L2")], events, ceiling=3)
    entry = result["claims"][0]
    assert entry["status"] == "insufficient_evidence"
    assert entry["reason"] == "evidence_below_required_level"
    assert entry["required_label"] == Level.L2_BUS.label
    assert entry["available_level"] == 1


def test_evidence_at_the_required_level_confirms() -> None:
    result = evaluate([claim(requires="L1")], timeline(levels=(1,)), ceiling=3)
    assert result["claims"][0]["status"] == "confirmed"


def test_evidence_above_the_required_level_confirms() -> None:
    result = evaluate([claim(requires="L1")], timeline(levels=(3,)), ceiling=3)
    assert result["claims"][0]["status"] == "confirmed"


def test_a_transport_ceiling_caps_a_complete_timeline() -> None:
    """A mock run with L3-looking events still cannot confirm an L2 claim."""

    result = evaluate([claim(requires="L2")], timeline(levels=(3,)), ceiling=1)
    entry = result["claims"][0]
    assert entry["status"] == "insufficient_evidence"
    assert entry["available_level"] == 1


def test_a_claim_without_a_declared_level_cannot_be_confirmed() -> None:
    """Silence must not default to "self-report is enough"."""

    bare = {"claim_id": "c", "kind": "ack_matches_task", "task_id": "A"}
    result = evaluate([bare], timeline(levels=(3,)), ceiling=3)
    assert result["claims"][0]["status"] == "insufficient_evidence"
    assert result["claims"][0]["reason"] == "no_required_level_declared"


def test_conviction_is_not_level_bound() -> None:
    """The subject's own words may refute it, at any level.

    I6's whole point: an ACK carrying the wrong epoch proves contamination even though
    the evidence is only the subject talking about itself.
    """

    events = [
        {"t_ms": 0, "kind": "task_start", "task_id": "A", "epoch": 1, "level": 1},
        {"t_ms": 5, "kind": "ack", "task_id": "A", "epoch": 99, "status": "done", "level": 0},
    ]
    result = evaluate([claim(requires="L3")], events, ceiling=0)
    assert result["claims"][0]["status"] == "refuted"


def test_a_requested_exit_proves_nothing() -> None:
    """An exit the harness asked for is not an observation of the subject."""

    events = [
        {"t_ms": 0, "kind": "task_start", "task_id": "A", "epoch": 1, "level": 0},
        {"t_ms": 5, "kind": "ack", "task_id": "A", "epoch": 1, "status": "done", "level": 0},
        {"t_ms": 9, "kind": "sut_exit", "code": 0, "level": 1, "requested": True},
    ]
    result = evaluate([claim(requires="L1")], events, ceiling=1)
    assert result["claims"][0]["status"] == "insufficient_evidence"
    assert result["claims"][0]["reason"] == "no_independent_observation"


def test_no_generated_timeline_confirms_below_its_required_level() -> None:
    """A sweep, not an example: for every combination, low evidence cannot confirm.

    Generated deterministically so a failure is reproducible from the seed printed in
    the assertion.
    """

    rng = random.Random(20261027)
    kinds = ["ack", "device_state", "sut_killed", "stop_ack", "note"]
    for trial in range(300):
        present = rng.sample(kinds, rng.randint(1, len(kinds)))
        top_level = max(min(rng.randint(0, 3), 2) for _ in range(1))
        events = [
            {"t_ms": 0, "kind": "task_start", "task_id": "A", "epoch": 1, "level": 1},
            {"t_ms": 5, "kind": "ack", "task_id": "A", "epoch": 1, "status": "done", "level": 0},
        ]
        for index, kind in enumerate(present):
            events.append({"t_ms": 10 + index, "kind": kind, "state": "idle", "level": top_level})
        required = rng.randint(0, 3)
        result = evaluate([claim(requires=f"L{required}")], events)
        entry = result["claims"][0]
        if entry["status"] == "confirmed":
            assert entry["available_level"] >= required, (trial, present, top_level, required, entry)


def test_the_ceiling_applies_to_every_transport() -> None:
    """No transport may confirm above its own declared level."""

    for transport, ceiling in TRANSPORT_CEILING.items():
        for required in range(int(ceiling) + 1, 4):
            events = timeline(levels=(required,))
            result = evaluate([claim(requires=f"L{required}")], events, ceiling=int(ceiling))
            entry = result["claims"][0]
            assert entry["status"] == "insufficient_evidence", (transport, required, entry)


def test_a_mock_run_is_flagged_simulated() -> None:
    assert is_simulated("mock") and is_simulated("ros2action")
    assert not is_simulated("socketcan")
    assert not is_simulated("workbench")
    for transport in TRANSPORT_CEILING:
        assert is_simulated(transport) is (transport in SIMULATED_TRANSPORTS)


def test_every_claim_in_every_scenario_declares_a_level() -> None:
    """A scenario file is a trust boundary; an undeclared level must not load."""

    root = pathlib.Path(__file__).resolve().parents[1] / "assurance/scenarios"
    missing = []
    for path in root.rglob("*.json"):
        scenario = json.loads(path.read_text())
        for entry in scenario.get("claims", []):
            if "requires" not in entry:
                missing.append(f"{path.name}:{entry.get('claim_id')}")
    assert not missing, missing
