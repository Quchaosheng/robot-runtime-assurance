"""Evidence levels: what a claim is allowed to be confirmed by.

The rule this module encodes is the reason the whole tool exists. A system that
reports its own success is not evidence of success - an ACK is a claim, not proof.
So evidence is ranked, every claim names the level it needs, and a claim whose
available evidence is below that level is `insufficient_evidence` and says which
level it was missing.

  L0  the SUT's own word        ACK, self-reported device state, its own verdict
  L1  process or artifact       a kill this harness performed, an exit code, a
                                checksum this harness computed
  L2  the bus                   a read-only CAN listener seeing the STOP frame, the
                                STOP_ACK, the mode in telemetry
  L3  physics                   a driver-enable pin, encoder edges - independent of
                                the firmware entirely

Conviction is not level-bound. If the subject says "done" and the bus shows no ACK
for that command, that is `refuted`: the subject's own words can be used against it.
Only confirmation requires external evidence.

Each transport declares the highest level it can produce, and a claim can never be
confirmed above it. That is what stops a mock run from looking like a bench result.
"""

from __future__ import annotations

from enum import IntEnum


class Level(IntEnum):
    """Ordered so that ``>=`` means "at least this independent"."""

    L0_SELF_REPORT = 0
    L1_PROCESS = 1
    L2_BUS = 2
    L3_PHYSICAL = 3

    @property
    def label(self) -> str:
        return {
            Level.L0_SELF_REPORT: "L0 自述",
            Level.L1_PROCESS: "L1 进程/产物",
            Level.L2_BUS: "L2 总线",
            Level.L3_PHYSICAL: "L3 物理",
        }[self]

    @classmethod
    def parse(cls, value: object) -> Level:
        """Accept ``L2``, ``2`` or the enum, and refuse anything else.

        A scenario file is a trust boundary: a typo in a level must stop the run, not
        silently downgrade the claim it was supposed to require.
        """
        if isinstance(value, Level):
            return value
        if isinstance(value, bool):
            raise ValueError("a boolean is not an evidence level")
        if isinstance(value, int):
            try:
                return cls(value)
            except ValueError as error:
                raise ValueError(f"{value} is not an evidence level (0..3)") from error
        if isinstance(value, str):
            text = value.strip().upper()
            if text.startswith("L") and text[1:].isdigit():
                return cls(int(text[1:]))
            if text.isdigit():
                return cls(int(text))
            for member in cls:
                if member.name == text:
                    return member
        raise ValueError(f"{value!r} is not an evidence level")


# The highest level each transport can produce. A transport absent from this map
# cannot confirm anything, which is the safe default.
TRANSPORT_CEILING: dict[str, Level] = {
    # A mock still lets the harness observe *its own* process and artifacts, which is
    # L1. What it cannot do is say anything about a device: a mock's device_state is
    # the subject's word and stays L0 by its event kind, not by the ceiling.
    "mock": Level.L1_PROCESS,
    "ros2action": Level.L1_PROCESS,
    "workbench": Level.L1_PROCESS,
    "socketcan": Level.L2_BUS,
    "traceartifact": Level.L1_PROCESS,
}

# Transports whose subject is this repository's own fixture rather than a real system.
# Their verdicts exercise the judging logic and must never enter the acceptance matrix.
SIMULATED_TRANSPORTS = frozenset({"mock", "ros2action"})


def ceiling_for(transport: str) -> Level:
    """The highest level a transport may produce; unknown transports get L0.

    Not an error: a new transport that has not declared its ceiling is treated as a
    self-reporting one until somebody says otherwise, so the failure mode is a claim
    that cannot be confirmed rather than one that is confirmed too easily.
    """
    return TRANSPORT_CEILING.get(str(transport), Level.L0_SELF_REPORT)


def is_simulated(transport: str) -> bool:
    return str(transport) in SIMULATED_TRANSPORTS


def describe() -> dict[str, object]:
    """What `assurance doctor` and the matrix command report about levels."""
    return {
        "levels": [
            {"value": int(level), "name": level.name, "label": level.label}
            for level in Level
        ],
        "transport_ceiling": {name: int(level) for name, level in TRANSPORT_CEILING.items()},
        "simulated": sorted(SIMULATED_TRANSPORTS),
    }
