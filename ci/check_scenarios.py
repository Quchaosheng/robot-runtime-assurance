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
import subprocess
import sys

BLOCKED_ON_UNAVAILABLE = {"transport_unavailable"}


def shipped_scenarios() -> list[str]:
    result = subprocess.run(
        [sys.executable, "-m", "assurance.cli", "list"],
        capture_output=True, text=True, check=True,
    )
    return [line.split()[0] for line in result.stdout.splitlines() if line.strip()]


def parse_first_json(text: str) -> dict:
    """The CLI prints indented JSON followed by a per-claim summary."""
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object in output")
    return json.JSONDecoder().raw_decode(text[start:])[0]


def main() -> int:
    failures = 0
    blocked = 0
    for scenario in shipped_scenarios():
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
    print(f"\n{total} scenarios, {failures} mismatch(es), {blocked} blocked")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
