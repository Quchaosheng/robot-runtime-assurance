# Acceptance matrix

Which invariant each shipped scenario actually puts at risk, and what a correct tool
run must return. `expect_verdict` in the scenario file is the source of truth; this
table is the human-readable view and is kept in sync by `assure list`.

| Scenario | I1 | I2 | I3 | I4 | I5 | I6 | I7 | I8 | expect |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `nominal/normal-task` | x | | | | | x | x | | pass |
| `cancellation/cancel-then-stop` | | | | x | x | | | x | pass |
| `cancellation/late-ack-after-cancel` | | | | | | x | | | pass |
| `cancellation/stale-ack-does-not-pollute-next-task` | | | | | | x | | x | **refute** |
| `failures/ack-dropped` | | | | | | | x | x | pass |
| `failures/ack-late` | | | | | | x | | | pass |
| `failures/runtime-killed` | | | | | | | x | x | pass |
| `failures/task-timeout` | | | | x | x | | | x | pass |
| `failures/resource-claim-conflict` | | | x | | | | | | pass |
| `failures/evidence-missing` | | | | | | | | x | **insufficient** |
| `negative-controls/sut-grants-conflicting-claim` | | x | x | | | | | | **refute** |
| `negative-controls/sut-ignores-cancel` | | | | x | x | | | | **refute** |

Not covered by any shipped scenario yet: I2 as a positive test (it is structural and
only appears through the negative control). Adding a SUT mode that *accepts* a
device command from a policy adapter would make it directly testable.

## Why three scenarios must not pass

A harness that can only produce `pass` proves nothing. The three rows in bold are
regression detectors for the tool itself:

- `stale-ack-does-not-pollute-next-task` — if the oracle ever stops detecting a stale
  epoch, this flips to `pass` and CI fails.
- `sut-grants-conflicting-claim` — if `conflict_rejected` stops noticing a granted
  conflict, this flips to `pass`.
- `sut-ignores-cancel` — if `cancel_then_stop` stops requiring the STOP ACK, this
  flips to `pass`.
