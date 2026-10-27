# Robot Runtime Assurance and System Acceptance

A black-box assurance harness for embodied robot runtimes: it drives a system you do
not own the internals of, injects the faults that break task safety, and returns
`confirmed` / `refuted` / `insufficient_evidence` with an evidence bundle someone else
can re-check.

> This project is a runtime assurance and system acceptance prototype.
> It is not a functional safety certification implementation.
> 本项目是运行时保障与系统验收原型，不构成功能安全认证或完整安全系统。
>
> Where it sits relative to ISO 12100, ISO 13849-1, ISO 13849-2 and ISO 13482 — one row
> per concept, plus a sayable / not-sayable table — is in
> [`docs/standards-position.md`](docs/standards-position.md).

## What problem this solves

Existing tooling tells you a process exited, a topic published, a trace spanned 3 ms,
a node came up, or a log replays. None of that answers the questions a system owner
actually asks after a delivery:

| Question | Here |
| --- | --- |
| After a cancel, did the robot actually stop? | `I4`, `I5` claims |
| Did an old task's late ACK confirm the new task? | `I6`, `late_ack_recorded` + `ack_matches_task` |
| After the device dropped off, did it reach the required terminal state? | `I7`, `terminal_state` |
| On a resource conflict, was the second task *rejected*, not queued? | `I3`, `conflict_rejected` |
| Is this pass, fail, or **not provable**? | three-state verdict, never a silent pass |
| Can someone else re-check this acceptance? | `python3 -m assurance.cli verify` + `python3 -m assurance.cli replay` on the bundle |

## The invariants (the actual product)

These come before any feature. Every claim in every scenario cites one of them.

```
I1  An unregistered scenario cannot be executed.
I2  A policy proposal cannot bypass the executor and reach the device directly.
I3  On a resource claim conflict, the later task must be rejected.
I4  Cancelling a task must escalate to a STOP.
I5  A STOP must be followed by the device's ACK and a terminal-state report.
I6  A late ACK must not complete or contaminate a newer run.
I7  Device disconnect, process death, or timeout must reach the required terminal state.
I8  With incomplete evidence the verdict must be insufficient, never pass.
```

I2 is proved from **both sides**, and it used to be only a structural claim. The
harness still has no code path into a device, but a scenario may now send one raw
actuator command and the SUT's answer is judged:

- a device that refuses it (`policy/raw-command-refused`) confirms the executor
  boundary exists;
- a device that obeys it (`negative-controls/sut-accepts-raw-command`) is refuted,
  which is what makes the invariant falsifiable;
- a run that never sent the probe, or a device that stayed silent, is
  `insufficient_evidence`. Silence is not compliance.

## Verdicts are three-valued, and that is the point

| Status | Meaning | When the tool is allowed to say it |
| --- | --- | --- |
| `confirmed` | every required piece of evidence exists and the invariant holds | only with an independent observation (device state, or a kill/crash - **not** an exit the harness requested) |
| `refuted` | evidence proves the invariant was violated | a concrete event contradicts it |
| `insufficient_evidence` | the evidence needed simply is not there | missing event, no observation, stopped recording too early |

An ACK is a *claim by the SUT*, not proof. A run where the harness shut the SUT down
cleanly has no independent observation, so it lands in `insufficient_evidence`,
never `confirmed`. This is deliberate and covered by tests.

## Quickstart

No ROS 2 required for the default path.

```bash
git clone https://github.com/Quchaosheng/robot-runtime-assurance && cd robot-runtime-assurance
python3 -m pip install -e ".[dev]"

python3 -m assurance.cli doctor                      # what this machine can and cannot test
python3 -m assurance.cli list                        # 20 shipped scenarios
python3 -m assurance.cli run nominal/normal-task     # writes runs/nominal-normal-task--local
python3 -m assurance.cli report runs/nominal-normal-task--local
python3 -m assurance.cli verify runs/nominal-normal-task--local
python3 -m assurance.cli replay runs/nominal-normal-task--local
python3 -m assurance.cli junit  runs/nominal-normal-task--local --out junit.xml
```

`python3 -m assurance.cli run` exits non-zero when the run did not match the scenario's
`expect_verdict`, so it drops straight into CI.

## Reports

- [R01 feasibility study](reports/R01-具身系统验收工具链-可行性研究.md)
- [R02 recall card](reports/R02-背诵卡.md)
- [R03 first delivery](reports/R03-交付说明.md)
- [R04 second delivery: black-box target, I2, SocketCAN, latency evidence](reports/R04-交付说明-第二轮.md)

## Pointing the harness at a real system

Nothing in this repo hardcodes an absolute path. A target is found by looking for a
sibling checkout next to this one (`wmhr`, `workbench-mobile-home-robot`,
`RoboTraceOpt`, `research/...`), identified by a marker file rather than by name:

| Target | Env var | Marker that must exist |
| --- | --- | --- |
| workbench | `ASSURANCE_WORKBENCH_ROOT` | `tools/scripts/sim_cli.py` |
| import shim (optional) | `ASSURANCE_WORKBENCH_SHIM` | `sitecustomize.py` |
| RoboTraceOpt | `ASSURANCE_TRACEOPT_ROOT` | `diagnosis/` |

`python3 -m assurance.cli doctor` prints exactly what it resolved, and a target it
cannot find is reported **unavailable** rather than guessed at. Scenarios that need
it are then `insufficient_evidence`, not silently skipped.

If the workbench checkout is not `pip install -e .`-installed, point
`ASSURANCE_WORKBENCH_SHIM` at a directory containing a `sitecustomize.py` that maps
its namespace packages - the harness never imports the subject, it only tells the
subject's own interpreter where its own files are.

## The black-box target: workbench-mobile-home-robot

The harness ships a `workbench` transport that drives a **real** system under test
through its published CLI and reads the evidence bundle it writes. It is the first
target this project did not build itself, which is the only way the black-box claim
means anything.

```bash
python3 -m assurance.cli doctor                      # workbench   available   <path>/sim_cli.py
python3 -m assurance.cli run workbench/normal-task
python3 -m assurance.cli run workbench/runtime-killed
```

Three rules keep it black-box:

| Rule | Why |
| --- | --- |
| the SUT's package is never put on `sys.path` | a verdict cannot rest on shared code |
| the SUT's own `device_state` / `task_terminal` are recorded as **subject claims** and are never independent observations | otherwise the subject confirms itself |
| the harness hashes the SUT's `checksums.sha256` itself, before reading the bundle | a bundle that does not match is a refutation, not evidence |

Two claim kinds exist only for this path:

- `bundle_published` — the run must publish a bundle whose files match its own
  checksums, and the scenario it ran must be the one that was requested. This is
  the harness's observation of an artifact, so it can confirm a provenance claim;
  it can never confirm that a robot moved.
- `kill_observed` — the kill must be visible and nothing may ACK after it. A kill
  the harness performed is an independent observation of the *process*, which is
  exactly what the claim is about.

### What this already found

Running the real SUT was not a formality. Two behaviours only showed up when the
process was driven from outside:

| Finding | Evidence |
| --- | --- |
| A SIGKILLed run publishes **no artifacts at all** (exit `-9`), so "no bundle" is the correct outcome and was previously indistinguishable from "the harness never looked" | `workbench/runtime-killed` |
| A `.partial` staging directory is the only residue of a killed run, and it is left on disk | recorded as `partial_leftovers` in the verdict |
| The SUT's per-event `sequence_no` is **not** a task epoch; mapping it as one made every ACK look stale | fixed in `assurance/sut/workbench.py` |

Given a kill that lands after the bundle is published, the oracle returns
`refuted: bundle_published_after_kill`. That rule is tested directly rather than
only exercised by the happy path.

## Scenarios that ship

| Scenario | Exercises | Expected |
| --- | --- | --- |
| `nominal/normal-task` | happy path, all four claims | pass |
| `cancellation/cancel-then-stop` | cancel → STOP → device ACK (I4/I5) | pass |
| `cancellation/late-ack-after-cancel` | late ACK is recorded, does not resurrect the task (I6) | pass |
| `cancellation/stale-ack-does-not-pollute-next-task` | task B must not be confirmed by A's stale ACK (I6) | **refute** |
| `failures/ack-dropped` | device works, ACK lost → timeout, idle | pass |
| `failures/ack-late` | ACK after deadline is evidence, not a pass | pass |
| `failures/runtime-killed` | SIGKILL mid-task, no ACK afterwards | pass |
| `failures/task-timeout` | deadline exceeded → cancel → STOP ACK | pass |
| `failures/resource-claim-conflict` | second claimant refused (I3) | pass |
| `failures/evidence-missing` | device reports no state → must be unprovable (I8) | **insufficient** |
| `nominal/ros2-task-timeout` | the same I4/I5 over a real ROS 2 action server + DDS | pass (ROS 2 only) |
| `negative-controls/sut-grants-conflicting-claim` | broken SUT must be caught | **refute** |
| `negative-controls/sut-ignores-cancel` | broken SUT must be caught | **refute** |
| `negative-controls/sut-accepts-raw-command` | a SUT with no executor boundary must be caught (I2) | **refute** |
| `policy/raw-command-refused` | the executor refuses a direct actuator command (I2) | pass |
| `trace/latency-evidence-verified` | a latency summary with no local artifact is unprovable | **insufficient** |
| `socketcan/device-not-on-this-host` | no vcan interface: honest insufficient, never a pass | **insufficient** |
| `workbench/normal-task` | the real workbench runtime, bundle + checksums (I1, I6) | pass |
| `workbench/registered-scenarios-run` | the same path through the registered-scenario claim | pass |
| `workbench/runtime-killed` | SIGKILL mid-run: no bundle, no ACK after death (I7) | pass |

`expect_verdict` is part of the scenario. A negative control that stops being refuted
fails CI — that is how the tool proves it can still detect a regression in itself.

## Fault injection: what is real and what is not

Injection is **session-level**, at the harness↔SUT boundary: drop an ACK, delay it,
answer with a stale epoch, kill the process, hold a resource, cancel at the deadline.
This is a deliberate constraint, not a shortcut — see `docs/architecture.md`:

```
tc netem        unavailable (needs CAP_NET_ADMIN)
iptables        unavailable
vcan device     unavailable (RTNETLINK: Operation not permitted)
cgroup writes   unavailable
ptrace          unavailable
```

So this tool does **not** claim to test the kernel CAN stack under packet loss. It
tests that the *runtime and device protocol* stay safe when the conversation is
hostile. `python3 -m assurance.cli doctor` prints each backend's availability and reason; nothing is
silently skipped.

## Transports

| Transport | Needs | What it proves |
| --- | --- | --- |
| `mock` | Python only | the whole decision path, deterministically |
| `ros2action` | `source /opt/ros/<distro>/setup.bash` | the same claims over a real DDS action server, with real deadline/cancel timing |

The ROS 2 path drives `action_tutorials_interfaces/Fibonacci`, a stock action, so the
harness needs **no generated message package from the SUT** - importing the SUT's IDL
would already break the black-box boundary.

```bash
source /opt/ros/jazzy/setup.bash
python3 -m assurance.cli doctor                       # ros2action: available
python3 -m assurance.cli run nominal/ros2-task-timeout
```

ROS 2 ships pytest plugins written for pytest 7; they crash pytest 9 at collection.
Under ROS 2 use `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -p no:randomly`
(what `ci/run-matrix.sh` does). Without ROS 2 the plain command works.

## Evidence bundle

```
runs/<scenario>--<id>/
  deterministic/     scenario, timeline, verdict, coverage   <- re-judging these must reproduce the digest
  observed/          raw event stream                        <- comparable, never replayed
  manifest.json
  checksums.sha256
  COMPLETE                                           <- written last, so half a bundle is never a run
```

`python3 -m assurance.cli verify` recomputes every checksum and re-derives the verdict from
`deterministic/`. Tampering with the timeline is detected (tested).

## Replay: three kinds, only one promised in v0.1

| Kind | Status | Meaning |
| --- | --- | --- |
| evidence replay | **implemented** | re-derive the verdict from the bundle; digest must match |
| fixture replay | **implemented** | re-run the scenario against the deterministic mock SUT |
| hardware replay | out of scope | physical robots are not bit-reproducible; not promised |

`python3 -m assurance.cli replay <bundle>` does fixture replay and compares the verdict digest.

## Status

- **150 passed** with no ROS 2 and no workbench checkout; 4 skipped that name the
  missing target and the variable to set. `python3 -m pytest` reproduces this.
  With ROS 2 sourced the ROS 2 scenario also runs.
- **20 scenarios**: 4 must be refuted and 3 must come back unprovable, every run.
  `python3 ci/check_scenarios.py` reproduces this and fails on any mismatch.
- Zero runtime dependencies for the default path (`mock` transport, stdlib only).
- Two-pass CI (`ci/run-matrix.sh`): without ROS 2, then with it. Both must pass.

### Not implemented yet, on purpose

gRPC and SocketCAN adapters, cross-layer trace import (RoboTraceOpt), a web UI, and
anything resembling certification. The mock SUT is a stand-in; the ROS 2 path is the
only real transport today.

## Repository layout

```
assurance/
  schemas/        scenario.schema.json, event.schema.json, evidence.schema.json
  scenarios/      nominal/ cancellation/ failures/ negative-controls/
  registry.py     I1: unregistered scenarios are refused
  transports.py   mock + ros2action
  runner.py       executes a scenario, never decides pass/fail
  oracle.py       timeline -> three-state verdict (pure function)
  evidence.py     bundle writing, checksum + replay verification, JUnit
  timeline.py     append-only run timeline
  cli.py          assure: doctor list run verify report replay junit selftest
  sut/            deterministic mock SUT + ROS 2 action SUT
docs/             architecture.md, invariants.md, acceptance-matrix.md
tests/            oracle, scenarios end-to-end, registry + CLI
```

## License

Apache-2.0. See [LICENSE](LICENSE).
## Evidence levels

A subject's own report is not evidence. Evidence is ranked, every claim names the
level it needs, and a claim below that level is `insufficient_evidence` with the
missing level named. See [docs/evidence-levels.md](docs/evidence-levels.md).

| Level | Source | Confirms? |
| --- | --- | --- |
| L0 | the subject's own word (ACK, self-reported state) | no — can only convict |
| L1 | the harness's own observation (kill, exit, checksum) | yes |
| L2 | a read-only CAN listener (STOP frame, STOP_ACK, telemetry mode) | yes |
| L3 | an independent sensor (enable pin, encoder edges) | yes |

### Acceptance matrix

Generated by `python3 -m assurance.cli matrix` — this table is never hand-written:

```text
| 不变量 | L0 自述 | L1 进程/产物 | L2 总线 | L3 物理 |
| --- | --- | --- | --- | --- |
| I1 | — | `nominal/normal-task` | — | — |
| I2 | `negative-controls/sut-accepts-raw-command` | — | — | — |
| I3 | `failures/resource-claim-conflict` | — | — | — |
| I4 | `cancellation/cancel-then-stop` | — | — | — |
| I5 | `cancellation/cancel-then-stop` | — | — | — |
| I6 | `cancellation/late-ack-after-cancel` | — | — | — |
| I7 | `failures/ack-dropped` | `failures/runtime-killed` | — | — |
| I8 | `cancellation/cancel-then-stop` | `trace/latency-evidence-verified` | — | — |

单元格里的场景名表示：该场景确实声明了这条不变量，且其 transport 能达到这一级。
「需更高层 transport」表示场景已写，但当前传输层产不出这一级证据。```

An empty L2 or L3 column is the project's real state, not a missing table row.

