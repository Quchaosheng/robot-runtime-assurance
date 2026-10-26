"""Runs one scenario against one SUT and records what happened. It never decides pass/fail.

Fault injection is session-level by design: on this class of machine `tc`, `iptables`,
`vcan` and cgroup are unavailable without CAP_NET_ADMIN, so packet-level chaos would
be a claim we cannot back with evidence. What we can do honestly is control the
conversation: drop an ACK, delay it, answer with a stale epoch, kill the process,
cancel at the deadline, or hold a resource.

shortcut: faults act on the mock SUT and on the harness/SUT boundary only;
upgrade when a privileged runner (netns/vcan) is available.
"""

from __future__ import annotations

import json
import pathlib
import threading
import time

from .registry import load as load_scenario
from .timeline import Timeline
from .transports import TransportError, make_transport

TERMINAL = ("ack", "rejected", "cancelled", "timeout")


def _sut_args(scenario: dict) -> list[str]:
    args = list(scenario.get("sut_args", []))
    for fault in scenario.get("faults", []):
        if fault["kind"] == "drop_ack":
            args.append("--drop-ack")
        elif fault["kind"] == "delay_ack":
            args += ["--delay-ack", str(fault.get("delay_ms", 300))]
        elif fault["kind"] == "stale_ack":
            args.append("--stale-epoch")
    return args


def _terminal_for(timeline: Timeline, task_id: str) -> dict | None:
    for event in timeline.events:
        if event.get("task_id") == task_id and event["kind"] in TERMINAL:
            return event
    return None


def run_scenario(scenario: dict, out_dir: str | pathlib.Path) -> dict:
    scenario = {**scenario, "sut_args": _sut_args(scenario)}
    timeline = Timeline()
    transport = make_transport(scenario)
    faults = scenario.get("faults", [])
    timeline.add("run_start", scenario_id=scenario["scenario_id"], seed=scenario["seed"],
                 transport=scenario["transport"], faults=[f["kind"] for f in faults])

    try:
        transport.start(timeline)
    except TransportError as exc:
        timeline.add("run_end", outcome="transport_unavailable", detail=str(exc))
        return _finish(scenario, timeline, out_dir, transport_started=False)

    if scenario["transport"] == "workbench":
        _run_workbench(scenario, transport, timeline, faults)
    elif scenario["transport"] == "ros2action":
        _run_ros2(scenario, transport, timeline, faults)
    else:
        _run_session(scenario, transport, timeline, faults)

    transport.stop(timeline)
    timeline.add("run_end", outcome="completed")
    return _finish(scenario, timeline, out_dir, transport_started=True)


def _run_workbench(scenario: dict, transport, timeline: Timeline, faults: list[dict]) -> None:
    """Run the SUT's own CLI once per task and read the bundle it publishes.

    This is the black-box path: the only thing sent is the SUT's published command
    line, the only thing read is the evidence bundle it wrote. A kill fault kills
    that process on purpose; a timeout fault truncates our own wait. Either way the
    SUT's account of what happened is recorded as a claim, not as proof.
    """
    import pathlib
    import tempfile

    kill = _fault_any(faults, "kill_sut")
    for task in scenario["tasks"]:
        output_dir = pathlib.Path(tempfile.mkdtemp(prefix="assurance-workbench-"))
        timeline.add("note", detail="workbench run requested", scenario_id=task.get("sut_scenario"),
                     output_dir=str(output_dir))
        try:
            if kill is not None:
                result = _run_workbench_killed(transport, task, output_dir,
                                               at_ms=kill.get("at_ms", 500))
            else:
                result = transport.run_task(task["sut_scenario"], output_dir,
                                            runner=task.get("runner", "scripted"),
                                            timeout_s=int(task["deadline_ms"] / 1000) + 30)
        except TransportError as exc:
            timeline.add("note", detail=f"workbench run failed: {exc}")
            continue
        verified, detail = _verify_bundle(result.get("bundle"))
        timeline.add("note", detail="workbench run finished", exit_code=result["exit_code"],
                     bundle=result.get("bundle"), bundle_verified=verified,
                     sut_scenario=task.get("sut_scenario"),
                     checksum_detail=detail, stdout=result["stdout_tail"])
        if verified:
            timeline.add("bundle_verified", bundle=result.get("bundle"),
                         files="checksums.sha256", detail=detail)
        if kill is not None:
            # Not `requested`: the kill is the harness's own observation of the
            # process dying, which is exactly what I7 is about.
            timeline.add("sut_killed", code=result["exit_code"],
                         reason="assurance killed the SUT mid-run")
            leftover = _leftovers(output_dir)
            timeline.add("note", detail="post-kill leftovers", partial=leftover["partial"],
                         published=leftover["published"], output_dir=str(output_dir))
        if result.get("bundle"):
            transport.ingest_bundle(timeline, pathlib.Path(result["bundle"]),
                                    task_id=task["task_id"], requested_exit=kill is None)


def _leftovers(output_dir) -> dict:
    """What a killed run left behind. A published bundle after a kill is a bug."""
    import pathlib

    root = pathlib.Path(output_dir)
    return {
        "partial": sorted(str(p.name) for p in root.glob(".*.partial")),
        "published": sorted(str(p.name) for p in root.iterdir()
                            if p.is_dir() and not p.name.startswith(".")),
    }


def _verify_bundle(bundle: str | None) -> tuple[bool | None, str]:
    """Hash the SUT's bundle ourselves before reading it.

    A published bundle that does not match its own `checksums.sha256` is not
    evidence of anything, so the mismatch is recorded as a refutation rather than
    silently ingested.
    """
    import hashlib
    import pathlib

    if not bundle:
        return None, "no bundle published"
    root = pathlib.Path(bundle)
    sums = root / "checksums.sha256"
    if not sums.is_file():
        return None, "bundle has no checksums.sha256"
    for line in sums.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        expected, name = parts
        target = root / name
        if not target.is_file():
            return False, f"{name} is listed but missing"
        actual = hashlib.sha256(target.read_bytes()).hexdigest()
        if actual != expected:
            return False, f"{name} hashes to {actual[:12]}, bundle says {expected[:12]}"
    return True, "all files match checksums.sha256"


def _run_workbench_killed(transport, task: dict, output_dir, *, at_ms: int) -> dict:
    """Start the SUT, kill it mid-run, and report what it left behind."""
    import subprocess
    import sys
    import time

    env = {**__import__("os").environ}
    cli = transport.root / "tools/scripts/sim_cli.py"
    command = [sys.executable, str(cli), "run", task["sut_scenario"], "--runner",
               task.get("runner", "scripted"), "--output-dir", str(output_dir), "--json"]
    from .sut.workbench import _shim
    env["PYTHONPATH"] = _shim()
    proc = subprocess.Popen(command, cwd=str(transport.root), env=env, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    time.sleep(max(at_ms, 0) / 1000.0)
    proc.kill()
    out, err = proc.communicate()
    return {"argv": command, "exit_code": proc.returncode,
            "stdout_tail": out.strip().splitlines()[-1:] or [""],
            "stderr_tail": err.strip().splitlines()[-3:],
            "summary": {}, "bundle": None, "metadata": None}


def _fault_any(faults: list[dict], kind: str) -> dict | None:
    return next((fault for fault in faults if fault["kind"] == kind), None)


def _run_ros2(scenario: dict, transport, timeline: Timeline, faults: list[dict]) -> None:
    for task in scenario["tasks"]:
        timeout = _fault(scenario, task["task_id"], "task_timeout") is not None
        transport.run_task(task["task_id"], task["epoch"], int(task.get("order", 3)),
                           task["deadline_ms"], timeline, expect_timeout=timeout)
        transport.drain(timeline, until_ms=300)      # let late state land before judging


def _run_session(scenario: dict, transport, timeline: Timeline, faults: list[dict]) -> None:
    timers = _start_kill_timers(faults, transport, timeline)
    t0 = time.monotonic()
    started: set[str] = set()
    cancelled: set[str] = set()
    hard_end = t0 + max((t.get("start_after_ms") or 0) + t["deadline_ms"]
                         for t in scenario["tasks"]) / 1000.0
    hard_end += 1.5

    while time.monotonic() < hard_end:
        transport.drain(timeline, until_ms=20)
        now = time.monotonic() - t0
        for task in scenario["tasks"]:
            tid = task["task_id"]
            if tid in started or now < (task.get("start_after_ms") or 0) / 1000.0:
                continue
            expect_timeout = _fault(scenario, tid, "task_timeout") is not None
            try:
                transport.send("start", tid, task["epoch"], task["resource"], task["deadline_ms"])
            except TransportError as exc:
                timeline.add("note", detail=f"send failed for {tid}: {exc}")
                started.add(tid)
                continue
            started.add(tid)
            timeline.add("task_start", task_id=tid, epoch=task["epoch"],
                         deadline_ms=task["deadline_ms"], resource=task["resource"],
                         expect=task.get("expect", "done"), injected_timeout=expect_timeout)
        for task in scenario["tasks"]:
            tid = task["task_id"]
            if tid not in started or tid in cancelled:
                continue
            if _terminal_for(timeline, tid):
                continue
            start = next((e for e in timeline.of("task_start") if e.get("task_id") == tid), None)
            if start and start["t_ms"] + task["deadline_ms"] <= timeline.elapsed_ms():
                timeline.add("timeout", task_id=tid, epoch=task["epoch"])
                try:
                    transport.send("cancel", tid)
                    timeline.add("cancel_sent", task_id=tid)
                    transport.send("stop", tid)      # I4: a cancel must escalate to a STOP
                    timeline.add("stop_sent", task_id=tid)
                except TransportError:
                    pass
                cancelled.add(tid)
        if all(_terminal_for(timeline, t["task_id"]) or t["task_id"] in cancelled
               for t in scenario["tasks"]):
            break

    for timer in timers:
        timer.cancel()
    # A late answer is evidence: keep recording long enough for the delay we declared.
    late_window = max([f.get("delay_ms", 0) for f in faults if f["kind"] == "delay_ack"] or [0])
    transport.drain(timeline, until_ms=max(400, late_window + 300))


def _fault(scenario: dict, task_id: str, kind: str) -> dict | None:
    for fault in scenario.get("faults", []):
        if fault["task_id"] == task_id and fault["kind"] == kind:
            return fault
    return None


def _start_kill_timers(faults: list[dict], transport, timeline: Timeline) -> list[threading.Timer]:
    timers = []
    for fault in faults:
        if fault["kind"] == "kill_sut":
            timer = threading.Timer(fault.get("at_ms", 50) / 1000.0,
                                    lambda: transport.kill(timeline))
            timer.daemon = True
            timer.start()
            timers.append(timer)
    return timers


def _conflicts(scenario: dict) -> dict[str, str]:
    """Task -> the task already holding its resource. This is the injected conflict."""
    holders: dict[str, str] = {}
    conflicts: dict[str, str] = {}
    for task in scenario["tasks"]:
        holder = holders.setdefault(task["resource"], task["task_id"])
        if holder != task["task_id"]:
            conflicts[task["task_id"]] = holder
    return conflicts


def fault_coverage(scenario: dict, timeline: Timeline) -> list[dict]:
    """A fault you declared but never exercised is not a test - say so instead of counting it."""
    rows = []
    for fault in scenario.get("faults", []):
        kind, tid = fault["kind"], fault["task_id"]
        if kind == "drop_ack":
            ok = (not timeline.of("ack")
                  and any(e.get("task_id") == tid for e in timeline.of("timeout")))
        elif kind == "delay_ack":
            ack = next((e for e in timeline.of("ack") if e.get("task_id") == tid), None)
            ok = bool(ack) and ack["t_ms"] >= fault.get("delay_ms", 300) * 0.5
        elif kind == "kill_sut":
            ok = bool(timeline.of("sut_killed"))
        elif kind == "task_timeout":
            ok = any(e.get("task_id") == tid for e in timeline.of("timeout"))
        elif kind == "claim_conflict":
            # Injected means: the second task really did ask for a resource still held.
            ok = tid in _conflicts(scenario) and any(
                e.get("task_id") == tid for e in timeline.of("task_start"))
        elif kind == "stale_ack":
            ok = any(e.get("task_id") == tid for e in timeline.of("ack"))
        else:
            ok = False
        rows.append({"fault": kind, "task_id": tid, "exercised": ok})
    return rows


def _finish(scenario: dict, timeline: Timeline, out_dir, transport_started: bool) -> dict:
    from .evidence import write_bundle
    from .oracle import evaluate

    verdict = evaluate(scenario["claims"], timeline.events)
    coverage = fault_coverage(scenario, timeline)
    unexercised = [c for c in coverage if not c["exercised"]]
    if not transport_started:
        blocked = "transport_unavailable"
    elif unexercised:
        blocked = "fault_not_exercised"
    elif verdict["counts"]["insufficient_evidence"]:
        blocked = "insufficient_evidence"
    elif verdict["counts"]["refuted"]:
        blocked = "claim_refuted"
    else:
        blocked = None
    observed = ("refute" if verdict["counts"]["refuted"]
                else "insufficient" if verdict["counts"]["insufficient_evidence"]
                else "pass")
    expected = scenario.get("expect_verdict", "pass")
    matched = observed == expected
    # `blocked_by` explains a failed run. A run whose sample behaved as designed did not
    # fail, even when the thing it proved is that the SUT is broken.
    verdict = {**verdict, "expected_verdict": expected, "observed_verdict": observed,
               "sample_matched": matched, "passed": matched,
               "blocked_by": None if matched else blocked,
               "unexercised": unexercised}
    return write_bundle(scenario, timeline.events, verdict, coverage, out_dir)
