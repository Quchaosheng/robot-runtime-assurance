# L3 bench: what to buy, how to wire it, how to reproduce

Status: **not built.** This page is the plan and the bill of materials, written before
any hardware is ordered. Nothing here is measured yet, and no number in this file is a
result.

## Why L3 needs different hardware from L2

L2 answers "did the STOP frame reach the device, and did the device answer". It cannot
answer "did the motor stop", because on a virtual bus the only clock is the scheduler's
and the only observer is another socket.

L3 needs two things L2 does not have:

- **a physical sensor** on the thing being claimed (the motor), read by a device that
  does not go through the firmware being tested;
- **a real timebase** shared by the CAN signal and that sensor, so "how long after the
  STOP did motion end" is one interval rather than a subtraction across clocks.

## Bill of materials

Prices are indicative and were not verified at order time.

| Part | Purpose | Indicative |
| --- | --- | --- |
| Nucleo-G474RE | safety MCU, matches the candidate in the subject's README | 150–200 |
| 3.3 V CAN transceiver (SN65HVD230 or TJA1051) ×2 | MCU to bus, one spare | 10–20 |
| USB-CAN adapter (candleLight / gs_usb) ×2 | one to drive, one listen-only | 60–150 each |
| Geared encoder motor (N20 / JGA25) | the thing being claimed about | 30–60 |
| Motor driver with an enable pin (TB6612 / DRV8833) | enable is what L3 observes | 10 |
| 24 MHz 8-channel logic analyser (fx2lafw, sigrok-supported) | CAN_RX + enable + encoder A/B on one clock | 50 |
| Emergency stop button + current-limited supply | hardware removal of energy | 100–300 |
| USB relay (optional) | repeatable disconnect experiments | 30 |

Roughly 600–1000 in the local currency, before spares.

**Why not a closed-firmware CAN joint motor:** its control loop is inside the thing being
judged, so the safety MCU layer disappears from the evidence chain. The whole point of
L3 is that the chain is Runtime → CAN → your MCU → driver → motor, with a sensor that
touches none of it.

## Wiring

```
Runtime host ──USB-CAN#1──┬── CAN bus ── transceiver ── Nucleo-G474 ── driver(EN) ── encoder motor
                          └── USB-CAN#2  (listen-only, L2)
logic analyser:  CAN_RX + EN + encoder A/B   (one timebase, L3)
e-stop: in series with the motor supply, not with the MCU
```

Three details that matter more than they look:

1. **USB-CAN#2 is set to listen-only in hardware.** Independence by configuration, not
   by code choosing not to transmit. A silent observer that *could* transmit is one
   refactor away from being a participant.
2. **The e-stop cuts motor power, not the MCU.** If it cut the MCU, the thing under test
   would stop observing at the same moment it stopped driving, and the evidence would
   end exactly when it became interesting.
3. **All logic-analyser channels share one clock.** CAN_RX, enable and both encoder
   lines on the same capture. An interval computed across two clocks is a guess with
   error bars nobody measured.

## Latency budgets are written before measuring

Each L3 scenario carries its budget in the scenario file, committed before the first
run. After seeing data, changing a budget is not allowed: it turns a measurement into a
negotiation with oneself. If the measured value misses the budget, that is the finding
and it goes in the report.

## The two L3 claims

| Claim | Invariant | What it requires |
| --- | --- | --- |
| `enable_cut` | I7 | after a kill or a disconnected link, the enable pin goes low within budget |
| `motion_ceased` | I5 | after the STOP frame is on the bus, encoder edges stop within budget and stay stopped |

Both are judged from the logic-analyser capture, not from anything the firmware says.

## Experiments

| Experiment | Repetitions | Fault |
| --- | --- | --- |
| cancel during motion | 300 | operator cancel |
| kill the runtime | 300 | process death |
| relay disconnect | 300 | link loss |

300 is not a round number: with zero violations, the rule of three bounds the failure
rate at 3/N with 95% confidence, which at N=300 is 1%. Fewer than 300 is
`insufficient_evidence`, not "probably fine".

## Negative control

A firmware build that does **not** cut the enable pin must be refuted by `enable_cut`.
A bench that cannot fail a deliberately broken build has not been shown to work.

Low-power motor only. Hand on the e-stop for every run. Current-limited supply. No
high-energy experiments.

## What this will also produce

A calibration of the software timestamp against the logic analyser's view of the same
frame going on the wire: the error distribution of `SO_TIMESTAMPNS` on this host. That
number belongs to RoboTraceOpt, which currently assumes software timestamps are
comparable.

## Status of each piece

| Piece | State |
| --- | --- |
| L2 observer and claims | done, green in CI |
| BOM chosen | this page |
| hardware ordered | not yet |
| `hal/stm32g474` | not written; needs the subject's `firmware/` boundary approval |
| logic analyser observer | not written; needs hardware to test against |
| L3 claims | specified, not implemented |
