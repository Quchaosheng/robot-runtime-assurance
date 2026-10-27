# Defect replay: would the gate have caught it?

A gate that has never seen a real defect is a hypothesis. This table tests the
hypothesis: take a bug the system under test already fixed, put it back, and run the
gate. What matters is not only *how many* it catches, but *at which evidence level* it
caught them — and which ones it could not catch at all, because L1 cannot see the thing
that was wrong.

Method for each row:

1. check out the commit **after** the fix, on a branch;
2. revert **only** the fix (not its tests), so the bug is back but nothing else moved;
3. run `python -m assurance.cli gate` against that checkout;
4. record: did the gate go red, at which scenario, and at which evidence level.

A row that the gate misses is worth more than a row it catches. It says precisely
where the current evidence level stops being enough, which is the argument for L2.

## Results

| # | Defect | Fixed in | Put back | Gate result | Level | Why |
| --- | --- | --- | --- | --- | --- | --- |
| D1 | Terminal pair the runtime cannot publish accepted as a confirmed completion (`ABORTED` + `COMPLETED` stored as `confirmed`) | `ba8411f` | pending | pending | L1 | — |
| D2 | Replay metrics computed from a non-canonical state | `799012d` | pending | pending | L1 | — |
| D3 | Simulation event source bound to a public interface by default | `1f46424` | pending | pending | L1 | — |
| D4 | Killed run leaves `.partial` staging residue forever | `d464635` | `1861a0a` | **missed** — gate stayed green | L1 | residue is a directory the harness never inspects |
| D5 | Verification reason/recovery outside the contract enum | `cf55912` | pending | pending | L0 | — |

## Why D1 is the interesting one

`ba8411f` describes a validator that accepted any `(action_status, outcome)` pair whose
two fields were each individually valid, while the runtime only ever emits three pairs.
An edited capture carrying `ABORTED` + `COMPLETED` was therefore stored as
`outcome=completed, device_state=confirmed` — **a confirmed completion that no run ever
produced.**

That is exactly the failure this project exists to find, and it is also exactly the
failure L1 may not be able to see. The corruption lives in the *ingest* of a capture,
not in the process boundary: the run finishes normally, the bundle is published, the
harness sees a healthy process and a valid checksum. Whether the recorded outcome was
legal is a property of the pair, and nothing outside the subject can check a pair it
never observes.

Which is the honest answer the replay is meant to produce: **L1 cannot catch D1.** It
needs either an L2 view of what the device actually did, or a claim that compares the
subject's own completion against an independent record. If the replay table shows D1 as
missed, that is a result, not a failure, and it is the argument for M3.

## What "caught" means here

The gate is red for exactly two reasons: a scenario ran and disagreed with its own
`expect_verdict`, or a scenario errored. A blocked transport is not a catch — it is
"this runner could not look", and counting it as a catch would inflate the number.

So a row is only marked **caught** when a scenario that *actually executed* went red.
Anything else is marked **missed**, with the reason.

## D4 in full: the first replay, and a miss

**What was done.** A worktree at `2b23660` (fixed), then `git revert d464635` to put the
bug back without touching anything else. Commit `1861a0a`, message
"replay: restore the .partial residue bug". The reverted checkout really is buggy:

```text
$ python3 tools/scripts/sim_cli.py run normal-001 --runner external --command ... &
$ kill -TERM %1
residue after SIGTERM: 1  runs/.normal-001-4a29dd8dccbd.lbzijyi8.partial
```

**What the gate said.**

```text
$ ASSURANCE_WORKBENCH_ROOT=/tmp/replay-d4/after python3 -m assurance.cli gate workbench
PASS  workbench/normal-task                   ceiling=L1
PASS  workbench/registered-scenarios-run      ceiling=L1
PASS  workbench/runtime-killed                ceiling=L1

3 scenarios, 0 blocked, 0 failed
```

**Missed.** The gate is green while the bug is present. This is the correct behaviour of
an L1 harness, and it is the most useful result in the table:

- the harness observes the SUT's *process* and its *published artifacts*;
- a staging directory that no process ever published is neither;
- `workbench/runtime-killed` tears the run down before a bundle exists, so the artifact
  it inspects never contains the residue.

An L2 or L3 harness would not see it either, and that is the point: this defect is not
invisible because the evidence level is too low. It is invisible because **no claim in
the register asks about it.** The fix is a claim (`no_staging_residue` at L1: the harness
inspects the output directory itself after a kill), not a higher evidence level.

That distinction is worth more than the catch would have been. It separates "we cannot
see it" from "we are not looking for it", and only the second one is a gap in the gate.

### Adding the claim, and what it revealed

`no_staging_residue` was added at L1 with the observation recorded by the runner. Run
against the same buggy checkout, the gate **still** passed:

```text
PASS  workbench/runtime-killed    ceiling=L1
1 scenarios, 0 blocked, 0 failed
```

The timeline shows why:

```json
{"detail": "post-kill leftovers", "partial": [], "partial_stale": [], "published": [],
 "output_dir": "/tmp/assurance-workbench-1ueujswo", "t_ms": 125}
```

The scenario kills at 120 ms, before the subject has created its staging directory, so
there is no residue to find. The claim is correct and the observation is honest - the
scenario simply cannot trigger this defect.

That is a third kind of miss, distinct from the other two:

| Miss | Meaning | Fix |
| --- | --- | --- |
| evidence too weak | the thing happened, nothing at this level can see it | raise the level (L2/L3) |
| never asked | no claim looks for it | add the claim |
| **scenario cannot trigger it** | the claim is right, the timing never reaches the defect | change the scenario's timing |

D4 is the third. The residue only appears when the kill lands *after* staging exists.

### Trying the timing change, and why it was reverted

Killing at 800 ms instead of 120 ms did make **a** claim go red - but not the one that
matters:

```text
I7-kill-observed-no-ack-or-bundle -> refuted | bundle_published_after_kill
I7-no-staging-residue             -> confirmed | no_stale_staging_residue
```

At 800 ms the subject has already published its bundle and exited, so:

- the first claim correctly refuted, because a killed run that published is a real
  problem - but it is a *different* problem (the kill arrived too late to be a kill);
- the residue claim confirmed, because a finished run leaves no staging behind.

The same 800 ms kill also made the **fixed** SUT go red, for the same reason. A scenario
that fails on healthy code is not a catch, it is a broken scenario.

So the timing was reverted to 120 ms and the fixed subject is green again:

```text
PASS  workbench/normal-task                  ceiling=L1
PASS  workbench/registered-scenarios-run     ceiling=L1
PASS  workbench/runtime-killed               ceiling=L1
3 scenarios, 0 blocked, 0 failed
```

### What this row actually establishes

D4 stays **missed**, and the reason is now precise: the residue bug needs an *external*
kill of a run whose staging already exists, and the workbench transport's own kill path
does not produce that shape. Writing the claim was still correct - it is the observation
the gate was missing - but a scenario that can reach the defect needs a different driver
(kill the published CLI process itself, not the scenario's inner timer).

This row is therefore the argument for the next piece of work rather than a catch. It is
recorded as a miss because calling it a catch would be false.

## Status

D4 has been replayed and recorded above. D1, D2, D3 and D5 are chosen but not yet run:
each needs its own worktree and a revert, and D1 in particular is expected to be a miss
for a more interesting reason (see the analysis above).

The gate must be judged against a **pinned harness tag** before these numbers go in the
README. A replay against a moving branch answers a question nobody will ask again
tomorrow.
