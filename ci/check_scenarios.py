#!/usr/bin/env python3
"""Run every shipped scenario and fail when one disagrees with its own expectation.

A scenario names an `expect_verdict`. This check enforces it, including the negative
controls: a sample that is supposed to refute and instead passes means the harness
stopped noticing a broken subject, which is worse than a plain red test.

Two outcomes are not failures:

* a scenario whose transport is unavailable on this machine (no ROS 2, no vcan) is
  reported as BLOCKED - the run still records why and returns insufficient_evidence;
* a scenario the registry does not ship cannot occur, because the list comes from it.

Anything else - a mismatch, an unreadable result - exits non-zero.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys

# The script is run from the repository root by CI but may also be run from ci/, so the
# package has to be importable either way.
_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

BLOCKED_ON_UNAVAILABLE = {"transport_unavailable"}


def capability_available(name: str) -> tuple[bool, str]:
    """Is the environment capability a scenario declares actually present?

    A scenario may say it needs something the host must provide (today only `vcan`).
    Skipping it is not the same as passing it: the skip is printed with the reason and
    counted separately, so a run on a host without a bus never reads as "the L2
    scenarios passed".
    """
    if name == "vcan":
        from assurance.observers.bus import BusObserver

        ok, why = BusObserver("vcan0").available()
        return ok, why
    return False, f"unknown capability {name!r}"


def shipped_scenarios() -> list[str]:
    result = subprocess.run(
        [sys.executable, "-m", "assurance.cli", "list"],
        capture_output=True, text=True, check=True,
    )
    return [line.split()[0] for line in result.stdout.splitlines() if line.strip()]


def required_capability(scenario: str) -> str | None:
    from assurance.registry import load

    return load(scenario).get("requires_capability")


def parse_first_json(text: str) -> dict:
    """The CLI prints indented JSON followed by a per-claim summary."""
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object in output")
    return json.JSONDecoder().raw_decode(text[start:])[0]


def main() -> int:
    failures = 0
    blocked = 0
    skipped = 0
    for scenario in shipped_scenarios():
        needed = required_capability(scenario)
        if needed:
            ok, why = capability_available(needed)
            if not ok:
                skipped += 1
                print(f"SKIP     {scenario} (needs {needed}: {why})")
                continue
        result = subprocess.run(
            [sys.executable, "-m", "assurance.cli", "run", scenario],
            capture_output=True, text=True, check=False,
        )
        try:
            payload = parse_first_json(result.stdout)
        except (ValueError, json.JSONDecodeError) as error:
            print(f"UNREADABLE {scenario}: {error}")
            print(result.stdout[-800:])
            failures += 1
            continue
        reason = payload.get("blocked_by")
        if reason in BLOCKED_ON_UNAVAILABLE:
            blocked += 1
            print(f"BLOCKED  {scenario} (transport unavailable on this runner)")
        elif reason:
            print(f"MISMATCH {scenario}: {reason}")
            print(result.stdout[-800:])
            failures += 1
        else:
            print(f"OK       {scenario}")
    total = len(shipped_scenarios())

    # The README carries the generated matrix. A stale table is a stale claim, so it
    # is checked here rather than trusted to a reviewer's memory.
    readme = (pathlib.Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
    matrix = subprocess.run(
        [sys.executable, "-m", "assurance.cli", "matrix"],
        capture_output=True, text=True, check=True,
    ).stdout
    stale = [line for line in matrix.splitlines() if line.startswith("| ") and line not in readme]
    if stale:
        print(f"MATRIX STALE: {len(stale)} row(s) of the README matrix do not match")
        for line in stale[:5]:
            print("  " + line)
        failures += 1
    else:
        print("MATRIX OK  README table matches the register")

    print(f"\n{total} scenarios, {failures} mismatch(es), {blocked} blocked, {skipped} skipped")
    if skipped:
        print("A skipped scenario did not execute. Its invariants stay unproven on this host.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
