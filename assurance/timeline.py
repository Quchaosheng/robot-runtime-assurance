"""Append-only run timeline. Every verdict is computed from this, never from live state.

Two clocks are kept apart on purpose: `t_ms` (monotonic, run-relative) is what the
oracle reasons about; `wall` is only for humans reading the bundle later.
"""

from __future__ import annotations

import time

KINDS = {
    "run_start", "task_start", "ack", "rejected", "cancel_sent", "cancelled",
    "timeout", "sut_exit", "sut_killed", "device_state", "claim_granted",
    "claim_denied", "run_end", "note", "stop_sent", "stop_ack", "task_accepted",
}


class Timeline:
    def __init__(self) -> None:
        self._t0 = time.monotonic()
        self.events: list[dict] = []

    def add(self, kind: str, **fields) -> dict:
        if kind not in KINDS:
            raise ValueError(f"unknown event kind: {kind}")
        event = {"t_ms": round((time.monotonic() - self._t0) * 1000), "kind": kind, **fields}
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
