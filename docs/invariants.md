# Invariants

Every claim in every scenario cites exactly one of these. A claim that cites none is
rejected at review time; a scenario with no claims is rejected by the registry.

| ID | Invariant | Claim kind | How it is proven | What refutes it |
| --- | --- | --- | --- | --- |
| I1 | An unregistered scenario cannot be executed | `registered_scenario`, `bundle_published` | `registry.load()` refuses anything not on disk; the run records which scenario actually ran | the run's `scenario_id` differs from the claim's `scenario_ref` |
| I2 | A policy proposal cannot bypass the executor | *(structural)* | the harness exposes only `start`/`cancel`/`stop`; there is no code path to a device | the SUT grants a claim it should refuse (see `negative-controls/`) |
| I3 | On a resource claim conflict, the later task is rejected | `conflict_rejected` | a task asks for a resource still held | the claim is granted, or no decision is recorded |
| I4 | Cancelling escalates to a STOP | `cancel_then_stop` | `cancel_sent` is followed by `stop_sent` | `cancel_without_stop` |
| I5 | A STOP is ACKed and a terminal state is reported | `cancel_then_stop`, `terminal_state` | `stop_ack` after `stop_sent`, then a non-`busy` state | `stop_never_acknowledged`, `stop_ack_predates_the_stop`, `device_still_busy_after_stop_ack` |
| I6 | A late ACK must not complete or contaminate a newer run | `ack_matches_task`, `late_ack_recorded`, `no_late_completion` | every ACK carries the epoch of the task it answers | `ack_from_previous_epoch`, `late_ack_completed_an_abandoned_task` |
| I7 | Death/disconnect/timeout reaches the required terminal state | `no_ack_after_exit`, `terminal_state`, `no_false_success` | kill is observed, no ACK follows it, final state matches | `ack_after_process_exit`, `wrong_terminal_state` |
| I8 | Incomplete evidence yields insufficient, never pass | global rule | `no_independent_observation` caps any confirmed verdict; empty timelines are insufficient | a claim confirmed with no independent observation |

## The rule that makes the three-state verdict real

`confirmed` requires an **independent observation**: a `device_state` report, a
`sut_killed`, or an unrestrained `sut_exit`. An exit the harness asked for
(`requested: true`) does not count — otherwise every clean shutdown would confirm
itself.

Corollary: a scenario that only observes the ACK channel can never be `confirmed`,
only `insufficient`. That is why `failures/evidence-missing` exists as a shipped
sample: it is the test that the insufficient path still works.

## The black-box additions

Two claim kinds exist for a SUT this harness does not own. Both are deliberately
narrow, because a black-box harness that can confirm anything is worth nothing:

| Claim kind | Confirms | Can never confirm |
| --- | --- | --- |
| `bundle_published` | a bundle was published, its files match its own `checksums.sha256`, and the scenario that ran is the one requested | that any action succeeded |
| `kill_observed` | the process died, and no ACK followed the death | that the device stopped |

`bundle_published` is an observation of an *artifact* - the harness hashed the files
itself - so it counts as independent. `kill_observed` is an observation of a
*process*, which is what I7 is about. Neither is an observation of the robot, which
is why every workbench scenario cites only those two and never claims a physical
outcome.

A subject's `device_state` and `task_terminal` are ingested as `subject_claim: true`
and `requested: true`, which keeps them out of the independent-observation set. A
test (`test_a_subject_self_report_is_never_an_independent_observation`) fixes that.
