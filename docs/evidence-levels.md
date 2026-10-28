# Evidence levels

The rule this project is built on: **a system that reports its own success is not
evidence of success.** An ACK is a claim. Only something the harness can see for
itself confirms it.

So evidence is ranked, every claim names the level it needs, and a claim whose
evidence is below that level is `insufficient_evidence` — with the missing level
named.

| Level | Source | Examples | What it can prove |
| --- | --- | --- | --- |
| **L0** 自述 | the subject's own word | ACK, self-reported device state, its own verdict | can convict, never confirms |
| **L1** 进程/产物 | the harness's own observation | a kill it performed, an exit code, a checksum it computed | process and artifact facts |
| **L2** 总线 | a read-only CAN listener | the STOP frame on the bus, the STOP_ACK, the mode in telemetry | the command reached the device and the device answered |
| **L3** 物理 | an independent sensor | driver-enable pin, encoder edges | the motor actually stopped |

## The rules

1. **A claim names its level.** `requires` is mandatory in the scenario schema.
   Omitting it is not "defaults to L0" — it is `no_required_level_declared`, which
   cannot be confirmed. Silence must not look like permission.
2. **Confirmation needs external evidence.** Below the required level →
   `insufficient_evidence`, with `required_level` and `available_level` in the claim
   record so a reader can see exactly what was missing.
3. **Conviction is not level-bound.** If the subject says "done" and the bus shows no
   ACK for that command, that is `refuted`. The subject's own words may be used
   against it. This is why I6 works at L0: an ACK carrying the wrong epoch *is* the
   evidence of contamination.
4. **A transport has a ceiling.** Each transport declares the highest level it can
   produce, and a claim can never be confirmed above it. An undeclared transport gets
   L0, so the failure mode is an unconfirmable claim rather than an over-confident one.
5. **A requested exit proves nothing.** `requested: true` means the harness triggered
   the event, which says nothing about the subject. Such an event keeps its kind for
   bookkeeping and contributes no evidence level.

## Transport ceilings

| Transport | Ceiling | Why |
| --- | --- | --- |
| `mock` | L1 | the harness can see its own process and artifacts; a mock's device state is L0 by its event kind |
| `ros2action` | L1 | same, over a real middleware but a fixture subject |
| `workbench` | L1 | a real subject, but only its published artifacts are observed |
| `socketcan` | L2 | a read-only listener on the bus |
| `traceartifact` | L1 | digests the harness re-derived |

`mock` and `ros2action` are marked **simulated**: their verdicts exercise the judging
logic and must never be read as evidence about a machine.

## Statistics

A budget claim ("p99 under X ms") needs more than one run:

- **≥ 300 trials with zero violations → `confirmed`.** By the rule of three, zero
  failures in N trials bounds the failure rate at 3/N with 95% confidence; at N=300
  that is 1%.
- **any violation → `refuted`.**
- **fewer than 300 → `insufficient_evidence`.**

The number 300 is chosen, not derived from a requirement. Saying so is part of the
claim.

## What L2 actually observes

`assurance/observers/bus.py` is a second socket on the same interface. It exists
because a sender reporting what it sent is another claim from the same source, and the
whole point of L2 is a source the subject does not control.

| Property | How it is guaranteed | How it could fail |
| --- | --- | --- |
| never transmits | the class has no `send`, `inject` or equivalent | a future edit adds one — a test asserts it does not |
| kernel timestamps | `SO_TIMESTAMPNS` is set on the socket | not set → Python's wake-up time, milliseconds off |
| refuses silently to guess | an undecodable frame is recorded as `invalid`, not dropped | dropped → a malformed device frame reads as a quiet bus |

The five L2 claims: `stop_on_bus` (I4), `stop_ack_on_bus` and `mode_on_bus` (I5),
`failsafe_on_link_loss` (I7), `stale_ack_ignored` (I6).

Two of them carry a correlation requirement, because presence is not enough:

- `stop_ack_on_bus` requires the ACK's command id to match a STOP id seen on the same
  bus, **and** the ACK to arrive after that STOP. An ACK for another command is an
  answer to something else, and treating it as confirmation is the contamination I6
  exists to prevent.
- `stale_ack_ignored` requires both halves: the replayed old frame visible on the bus,
  **and** no completion recorded for the new task. Either alone is consistent with a
  broken injector.

## What L2 cannot do

**It cannot report physical latency.** On vcan, a frame's timing is the scheduler's,
not the machine's. The claim record includes `latency_ms` because the arithmetic is
free and the ordering is real, but no budget may be judged from it. A p99 delay number
from a virtual bus would be a fabricated number with a decimal point.

That is what L3 is for, and it is why the L3 column is empty rather than approximated.

## What the matrix means

`python3 -m assurance.cli matrix` prints invariant × level, filled only where a
scenario declares that claim *and* its transport can reach that level. A cell reading
`需更高层 transport` means the scenario is written but the transport cannot produce
that evidence yet.

An empty L2 or L3 column is not a gap in the table. It is the real state of the
project, printed where a reader will see it.
