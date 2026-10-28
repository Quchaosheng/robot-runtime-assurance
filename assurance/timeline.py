"""Append-only run timeline. Every verdict is computed from this, never from live state.

Two clocks are kept apart on purpose: `t_ms` (monotonic, run-relative) is what the
oracle reasons about; `wall` is only for humans reading the bundle later.
"""

from __future__ import annotations

import time

KINDS = {
    "run_start", "task_start", "ack", "rejected", "cancel_sent", "cancelled",
    "timeout", "sut_exit", "sut_killed", "device_state", "claim_granted",
    "claim_denied", "run_end", "note", "stop_sent", "stop_ack", "task_accepted", "bundle_verified", "trace_verified", "raw_command", "bus_frame",
}


# The evidence level each event kind can carry by default. A writer that knows
# better passes `level=` explicitly; the default exists so an event can never arrive
# without one, which is how a self-report would otherwise be counted as proof.
DEFAULT_LEVEL = {
    # the subject's own word
    "ack": 0,
    "rejected": 0,
    "cancelled": 0,
    "timeout": 0,
    "device_state": 0,
    "task_accepted": 0,
    "claim_granted": 0,
    "claim_denied": 0,
    # observed by the harness itself
    "sut_killed": 1,
    "sut_exit": 1,
    "bundle_verified": 1,
    "trace_verified": 1,
    "raw_command": 1,
    "run_start": 1,
    "run_end": 1,
    "note": 0,
    "task_start": 0,
    "cancel_sent": 1,
    "stop_sent": 1,
    # the bus
    "stop_ack": 2,
    # a frame this harness watched on the bus, never one the subject told it about
    "bus_frame": 2,
}


class Timeline:
    def __init__(self) -> None:
        self._t0 = time.monotonic()
        self.events: list[dict] = []

    def add(self, kind: str, level: int | None = None, **fields) -> dict:
        if kind not in KINDS:
            raise ValueError(f"unknown event kind: {kind}")
        resolved = DEFAULT_LEVEL.get(kind) if level is None else level
        if resolved is None:
            raise ValueError(f"event kind {kind!r} needs an explicit evidence level")
        if not isinstance(resolved, int) or isinstance(resolved, bool) or not 0 <= resolved <= 3:
            raise ValueError(f"evidence level must be 0..3, got {resolved!r}")
        event = {
            "t_ms": round((time.monotonic() - self._t0) * 1000),
            "kind": kind,
            "level": resolved,
            **fields,
        }
        self.events.append(event)
        return event

    def elapsed_ms(self) -> int:
        return round((time.monotonic() - self._t0) * 1000)

    def of(self, *kinds: str) -> list[dict]:
        return [e for e in self.events if e["kind"] in kinds]

    def first(self, kind: str, **match) -> dict | None:
        for e in self.events:
            if e["kind"] == kind and all(e.get(k) == v for k, v in match.items()):
                return e
        return None
