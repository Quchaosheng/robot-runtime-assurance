"""Verdicts over a recorded timeline. Pure: no clock, no I/O, no randomness.

A claim can only be `confirmed` when a protocol outcome is backed by an
independent observation (the SUT's own state report, or its process exit).
Protocol agreement alone is `insufficient_evidence` - an ACK is a claim, not proof.
"""

from __future__ import annotations

import hashlib
import json

CONFIRMED, REFUTED, INSUFFICIENT = "confirmed", "refuted", "insufficient_evidence"


def _terminal(timeline: list[dict], task_id: str) -> list[dict]:
    return [e for e in timeline
            if e.get("task_id") == task_id
            and e["kind"] in ("ack", "rejected", "cancelled", "timeout")]


def _independent_observations(timeline: list[dict]) -> list[str]:
    """Evidence that does not come from the same channel as the ACK."""
    # An exit the harness asked for proves nothing about the SUT; a kill or a crash does.
    kinds = {e["kind"] for e in timeline if not e.get("requested")}
    independent = [k for k in ("device_state", "sut_exit", "sut_killed") if k in kinds]
    # A bundle this harness hashed itself is an observation of the *artifact*, not a
    # report from the subject. It can confirm a provenance claim; it can never
    # confirm that the robot did anything.
    if any(e["kind"] == "bundle_verified" for e in timeline):
        independent.append("bundle_verified")
    # Same reasoning for a latency artifact: the harness re-derived the digests.
    if any(e["kind"] == "trace_verified"
           and any(d["status"] == "match" for d in e.get("digests", []))
           for e in timeline):
        independent.append("trace_verified")
    return independent


def _verdict(status: str, reason: str, **extra) -> dict:
    return {"status": status, "reason": reason, **extra}


def _no_false_success(claim: dict, timeline: list[dict]) -> dict:
    tid = claim.get("task_id")
    terms = _terminal(timeline, tid)
    start = next((e for e in timeline if e["kind"] == "task_start" and e.get("task_id") == tid), None)
    expected = (start or {}).get("expect", "done")
    if any(e["kind"] == "ack" and e.get("status") == "done" for e in terms) \
            and expected != "done":
        return _verdict(REFUTED, "sut_reported_done", task_id=tid, expected=expected)
    if not terms:
        return _verdict(INSUFFICIENT, "no_terminal_signal", missing=["ack|rejected|timeout"],
                        task_id=tid)
    return _verdict(CONFIRMED, "no_done_ack_recorded", task_id=tid)


def _terminal_state(claim: dict, timeline: list[dict]) -> dict:
    want = claim.get("expect_state", "safe_stop")
    death = [e for e in timeline if e["kind"] in ("sut_exit", "sut_killed")]
    alive_until = min((e["t_ms"] for e in death), default=None)
    seen = [e for e in timeline if e["kind"] == "device_state"
            and (alive_until is None or e["t_ms"] <= alive_until)]
    if not seen:
        return _verdict(INSUFFICIENT, "no_device_state_reported", missing=["device_state"])
    got = seen[-1].get("state")
    if got != want:
        return _verdict(REFUTED, "wrong_terminal_state", expected=want, observed=got)
    if not any(e["kind"] in ("sut_exit", "sut_killed", "cancelled", "rejected", "timeout")
               for e in timeline):
        return _verdict(INSUFFICIENT, "state_seen_but_no_shutdown_evidence",
                        missing=["sut_exit|sut_killed"])
    return _verdict(CONFIRMED, "terminal_state_matches", observed=got)


def _ack_matches_task(claim: dict, timeline: list[dict]) -> dict:
    """The anti-contamination claim: an ACK must carry the epoch of the task it answers."""
    tid = claim.get("task_id")
    starts = {e["task_id"]: e.get("epoch") for e in timeline if e["kind"] == "task_start"}
    acks = [e for e in timeline if e["kind"] == "ack" and e.get("task_id") == tid]
    if not acks:
        return _verdict(INSUFFICIENT, "no_ack_recorded", missing=["ack"], task_id=tid)
    epoch = starts.get(tid)
    for ack in acks:
        if epoch is not None and ack.get("epoch") != epoch:
            return _verdict(REFUTED, "ack_from_previous_epoch",
                            task_id=tid, task_epoch=epoch, ack_epoch=ack.get("epoch"))
    return _verdict(CONFIRMED, "all_acks_carry_task_epoch", task_id=tid)


def _conflict_rejected(claim: dict, timeline: list[dict]) -> dict:
    tid = claim.get("task_id")
    denied = [e for e in timeline if e["kind"] == "claim_denied" and e.get("task_id") == tid]
    granted = [e for e in timeline if e["kind"] == "claim_granted" and e.get("task_id") == tid]
    if denied and not granted:
        return _verdict(CONFIRMED, "conflicting_claim_rejected", task_id=tid)
    if granted:
        return _verdict(REFUTED, "conflicting_claim_granted", task_id=tid)
    return _verdict(INSUFFICIENT, "no_claim_decision_recorded", missing=["claim_granted|claim_denied"],
                    task_id=tid)


def _no_ack_after_exit(claim: dict, timeline: list[dict]) -> dict:
    exits = [e for e in timeline if e["kind"] in ("sut_exit", "sut_killed")]
    if not exits:
        return _verdict(INSUFFICIENT, "sut_never_exited", missing=["sut_exit|sut_killed"])
    # Use the earliest death, not the first one listed: event order is not a promise.
    dead_at = min(e["t_ms"] for e in exits)
    late = [e for e in timeline if e["kind"] == "ack" and e["t_ms"] > dead_at]
    if late:
        return _verdict(REFUTED, "ack_after_process_exit", dead_at_ms=dead_at,
                        late_ack={"task_id": late[0].get("task_id"),
                                  "t_ms": late[0]["t_ms"]})
    return _verdict(CONFIRMED, "no_ack_after_exit", dead_at_ms=dead_at)


def _registered_scenario(claim: dict, timeline: list[dict]) -> dict:
    """I1: only registered scenarios execute. A run that names one is evidence enough."""
    ref = claim.get("scenario_ref")
    start = next((e for e in timeline if e["kind"] == "run_start"), None)
    if start is None:
        return _verdict(INSUFFICIENT, "no_run_start", missing=["run_start"])
    if start.get("scenario_id") != ref:
        return _verdict(REFUTED, "ran_a_different_scenario",
                        expected=ref, observed=start.get("scenario_id"))
    return _verdict(CONFIRMED, "scenario_registered_and_ran", observed=ref)


def _cancel_then_stop(claim: dict, timeline: list[dict]) -> dict:
    """I4 then I5: a cancel must escalate to a STOP, and the device must ACK that STOP."""
    tid = claim.get("task_id")
    cancel = [e for e in timeline if e["kind"] == "cancel_sent" and e.get("task_id") == tid]
    stop = [e for e in timeline if e["kind"] == "stop_sent" and e.get("task_id") == tid]
    ack = [e for e in timeline if e["kind"] == "stop_ack" and e.get("task_id") == tid]
    if not cancel:
        return _verdict(INSUFFICIENT, "no_cancel_was_sent", missing=["cancel_sent"], task_id=tid)
    if not stop:
        return _verdict(REFUTED, "cancel_without_stop", task_id=tid)       # I4 violated
    if not ack:
        return _verdict(REFUTED, "stop_never_acknowledged", task_id=tid)   # I5 violated
    if ack[0]["t_ms"] < stop[0]["t_ms"]:
        return _verdict(REFUTED, "stop_ack_predates_the_stop", task_id=tid)
    # Judge the state the device settled into, not one already in flight when the ACK
    # landed: a stale `busy` published before the stop must not refute a valid STOP.
    states = [e for e in timeline
              if e["kind"] == "device_state" and e["t_ms"] >= ack[0]["t_ms"]]
    if not states:
        return _verdict(INSUFFICIENT, "stop_acked_but_no_settled_state",
                        missing=["device_state after stop_ack"], task_id=tid)
    settled = states[-1].get("state")
    if settled == "busy":
        return _verdict(REFUTED, "device_still_busy_after_stop_ack", task_id=tid)
    return _verdict(CONFIRMED, "stop_sent_and_acknowledged", task_id=tid,
                    acked_at_ms=ack[0]["t_ms"], settled_state=settled)


def _no_late_completion(claim: dict, timeline: list[dict]) -> dict:
    """I6: an ACK that arrives after we gave up must not turn into a completion."""
    tid = claim.get("task_id")
    timeouts = [e for e in timeline if e["kind"] == "timeout" and e.get("task_id") == tid]
    if not timeouts:
        return _verdict(INSUFFICIENT, "task_never_timed_out", missing=["timeout"], task_id=tid)
    dead_at = timeouts[0]["t_ms"]
    late_done = [e for e in timeline
                 if e["kind"] == "ack" and e.get("task_id") == tid
                 and e["t_ms"] > dead_at and e.get("status") == "done"]
    cancelled = [e for e in timeline if e["kind"] == "cancelled" and e.get("task_id") == tid]
    if late_done and not cancelled:
        return _verdict(REFUTED, "late_ack_completed_an_abandoned_task", task_id=tid)
    return _verdict(CONFIRMED, "late_ack_stayed_a_late_ack", task_id=tid,
                    late_acks=len(late_done), cancelled=bool(cancelled))


def _late_ack_recorded(claim: dict, timeline: list[dict]) -> dict:
    """A late ACK is only evidence if it is in the timeline *and* marked against a deadline."""
    tid = claim.get("task_id")
    minimum = claim.get("min_delay_ms", 0)
    timeouts = [e for e in timeline if e["kind"] == "timeout" and e.get("task_id") == tid]
    acks = [e for e in timeline if e["kind"] == "ack" and e.get("task_id") == tid]
    if not timeouts:
        return _verdict(INSUFFICIENT, "task_never_timed_out", missing=["timeout"], task_id=tid)
    if not acks:
        # The device answered, but we stopped recording too early to prove when.
        return {"status": INSUFFICIENT, "reason": "late_ack_not_captured",
                "missing": ["ack after deadline"], "task_id": tid}
    gap = acks[0]["t_ms"] - timeouts[0]["t_ms"]
    if gap < minimum:
        return _verdict(REFUTED, "ack_arrived_too_soon_to_be_late",
                        gap_ms=gap, minimum_ms=minimum, task_id=tid)
    return _verdict(CONFIRMED, "late_ack_recorded_after_deadline", gap_ms=gap, task_id=tid)


def _bundle_published(claim: dict, timeline: list[dict]) -> dict:
    """I1 for a black-box SUT: the run must publish a verifiable bundle, and the
    scenario it actually ran must be the one that was asked for.

    This is the harness's own observation - it hashed the bundle's files itself -
    so it is *not* capped by the independent-observation rule the way an ACK is.
    It still cannot prove the robot moved; it proves which scenario ran and that
    the artifact was not rewritten after the fact.
    """
    ref = claim.get("scenario_ref")
    published = [e for e in timeline if e["kind"] == "note" and "bundle" in e]
    if not published:
        return _verdict(INSUFFICIENT, "no_bundle_published", missing=["published_bundle"])
    observed = next((e.get("sut_scenario") for e in published if e.get("sut_scenario")), None)
    if ref is not None and observed != ref:
        return _verdict(REFUTED, "ran_a_different_scenario", expected=ref, observed=observed)
    unverified = [e for e in published if e.get("bundle_verified") is False]
    if unverified:
        return _verdict(REFUTED, "bundle_checksum_mismatch",
                        detail=unverified[0].get("detail"))
    return _verdict(CONFIRMED, "bundle_published_and_checksum_verified",
                    observed=observed, bundles=len(published))


def _kill_observed(claim: dict, timeline: list[dict]) -> dict:
    """I7 for a killed SUT: the kill must be visible, and nothing may ACK after it.

    A kill this harness performed is an independent observation of the *process*,
    which is exactly what the claim is about. It says nothing about the device.
    """
    deaths = [e for e in timeline if e["kind"] in ("sut_exit", "sut_killed")]
    if not deaths:
        return _verdict(INSUFFICIENT, "no_death_observed", missing=["sut_killed|sut_exit"])
    death = deaths[0]
    after = [e for e in timeline
             if e["kind"] == "ack" and not e.get("requested") and e["t_ms"] > death["t_ms"]]
    if after:
        return _verdict(REFUTED, "ack_after_process_exit",
                        count=len(after), t_ms=after[0]["t_ms"])
    # A killed run that still managed to publish a bundle was not killed early enough
    # to prove anything, so it cannot be counted as a pass.
    leftovers = [e for e in timeline
                 if e["kind"] == "note" and e.get("detail") == "post-kill leftovers"]
    if leftovers and leftovers[0].get("published"):
        return _verdict(REFUTED, "bundle_published_after_kill",
                        bundles=leftovers[0]["published"])
    # A `.partial` directory is the honest residue of a killed run: the SUT never
    # claimed it published anything. It is recorded as a finding, not a failure,
    # because the missing artifact is the correct outcome - but a reviewer has to be
    # told it is there, or "no bundle" and "no trace at all" look identical.
    partial = leftovers[0].get("partial") if leftovers else []
    return _verdict(CONFIRMED, "kill_observed_and_no_ack_followed",
                    code=death.get("code"), t_ms=death["t_ms"], partial_leftovers=partial)


def _trace_artifact_verified(claim: dict, timeline: list[dict]) -> dict:
    """I9: a latency artifact is evidence only if its declared digests re-derive.

    The harness computed every digest in the timeline itself, so this is an
    observation of an artifact rather than a report from the subject. It can never
    confirm that a task succeeded - only that the timing evidence is intact and
    internally consistent.
    """
    verified = [e for e in timeline if e["kind"] == "trace_verified"]
    if not verified:
        return _verdict(INSUFFICIENT, "no_trace_artifact", missing=["trace_verified"])
    seen = verified[0]
    mismatches = [d for d in seen.get("digests", []) if d["status"] == "mismatch"]
    if mismatches:
        return _verdict(REFUTED, "trace_digest_mismatch",
                        fields=[d["field"] for d in mismatches],
                        detail=mismatches[0].get("detail"))
    missing = [d for d in seen.get("digests", []) if d["status"] == "missing"]
    if missing:
        return _verdict(REFUTED, "trace_artifact_missing",
                        fields=[d["field"] for d in missing])
    checked = [d for d in seen.get("digests", []) if d["status"] == "match"]
    if not checked:
        return _verdict(INSUFFICIENT, "no_digest_could_be_checked",
                        missing=["at least one re-derivable digest"])
    want = claim.get("min_accepted_count")
    if want is not None and seen.get("accepted_count", 0) < want:
        return _verdict(REFUTED, "trace_accepted_below_threshold",
                        expected=want, observed=seen.get("accepted_count"))
    return _verdict(CONFIRMED, "trace_digests_verified",
                    checked=len(checked), accepted=seen.get("accepted_count"))


def _raw_command_refused(claim: dict, timeline: list[dict]) -> dict:
    """I2: a proposed action cannot reach the device without passing the executor.

    A SUT that accepts a raw actuator command has no executor boundary to speak of,
    which is exactly the failure this claim must catch. A run that never sent a probe
    is insufficient: silence is not compliance.
    """
    sent = [e for e in timeline if e["kind"] == "note" and e.get("detail") == "raw command sent"]
    seen = [e for e in timeline if e["kind"] == "raw_command"]
    if not sent:
        return _verdict(INSUFFICIENT, "no_raw_probe_sent", missing=["raw command"])
    if not seen:
        return _verdict(INSUFFICIENT, "no_raw_verdict_recorded",
                        missing=["raw_accepted|raw_rejected"])
    accepted = [e for e in seen if e.get("accepted")]
    if accepted:
        return _verdict(REFUTED, "device_obeyed_a_raw_command",
                        joint=accepted[0].get("joint"), value=accepted[0].get("value"))
    return _verdict(CONFIRMED, "device_refused_every_raw_command",
                    probes=len(seen), joint=seen[0].get("joint"))


RULES = {
    "no_false_success": _no_false_success,
    "terminal_state": _terminal_state,
    "ack_matches_task": _ack_matches_task,
    "conflict_rejected": _conflict_rejected,
    "no_ack_after_exit": _no_ack_after_exit,
    "late_ack_recorded": _late_ack_recorded,
    "registered_scenario": _registered_scenario,
    "cancel_then_stop": _cancel_then_stop,
    "no_late_completion": _no_late_completion,
    "bundle_published": _bundle_published,
    "kill_observed": _kill_observed,
    "trace_artifact_verified": _trace_artifact_verified,
    "raw_command_refused": _raw_command_refused,
}


def evaluate(claims: list[dict], timeline: list[dict]) -> dict:
    results = []
    for claim in claims:
        rule = RULES.get(claim["kind"])
        if rule is None:
            raise ValueError(f"unknown claim kind: {claim['kind']}")
        outcome = rule(claim, timeline)
        # A confirmed verdict still needs a second, independent view of the system.
        if outcome["status"] == CONFIRMED and not _independent_observations(timeline):
            outcome = _verdict(INSUFFICIENT, "no_independent_observation",
                               missing=["device_state|sut_exit|sut_killed"],
                               would_have_been=outcome["reason"])
        results.append({"claim_id": claim["claim_id"], "kind": claim["kind"],
                        **{k: v for k, v in claim.items() if k not in ("claim_id", "kind")},
                        **outcome})
    counts = {s: sum(1 for r in results if r["status"] == s)
              for s in (CONFIRMED, REFUTED, INSUFFICIENT)}
    body = json.dumps(results, sort_keys=True)
    return {"claims": results, "counts": counts,
            "digest": hashlib.sha256(body.encode()).hexdigest()[:16],
            "passed": counts[REFUTED] == 0 and counts[INSUFFICIENT] == 0}
