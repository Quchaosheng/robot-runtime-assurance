# Where this tool sits relative to functional-safety standards

No standard text is reproduced here (they are copyrighted); only publicly described
method is used. The editions in force must be confirmed with whoever owns the
compliance paperwork of the system being tested.

This tool is a **black-box acceptance harness**. It answers "did this system reach the
state it said it would, under this fault, with evidence someone else can re-check". It
does not answer "is this system safe", and it cannot, because it holds no hazard
information about the system under test.

## ISO 12100 — risk assessment and risk reduction

| The standard asks for | This tool |
| --- | --- |
| Hazard identification | **No.** It never enumerates how the subject could injure anyone |
| Risk estimation | **No.** It has no mass, speed, geometry or exposure data |
| Risk reduction | **No.** It tests a subject; it does not design one |
| Residual risk | **No.** Out of scope by construction |

The distinction that matters: the invariants in [`invariants.md`](invariants.md) are
**runtime properties chosen from what a subject can be observed doing**, not safety
requirements derived from hazards. ISO 12100 runs top-down; this tool runs bottom-up.
That inversion is the gap, and no amount of scenario coverage closes it.

## ISO 13849-1 / 13849-2 — safety-related control systems, design and validation

| Concept | This tool |
| --- | --- |
| Safety function identification | **No** — an invariant is not a safety function |
| Required performance level (PLr) | **No** |
| Architectural category, MTTFd, DC | **No** — and it has no hardware data to derive them from |
| Specified-fault analysis | **No** — faults are injected as a *test technique*, not analysed |

ISO 13849-2 validation consumes a fault list derived from a hazard analysis. This tool
can inject named faults deterministically, which is a useful **input** to such an
activity. It is not the activity: there is no derived fault list, and every result is
`SIMULATED`.

## ISO 13482 — personal care robots

Applicability is **not assessed**. Whether a subject falls inside that standard's scope
is a product decision for the subject's owner. This tool takes no position.

## Sayable / not sayable

Supported by a file or test in this repository, or not.

| Sayable | Not sayable |
| --- | --- |
| "the subject refused a raw actuator command, and a scenario pins it" | "the safety function is achieved" |
| "a late ACK did not confirm a newer task" | "the system cannot hurt anyone" |
| "the verdict was `insufficient_evidence` because X was missing" | "the evidence is complete" |
| "this bundle can be re-judged by a third party" | "this bundle has been audited" |
| "the fault list covers these six named cases at session level" | "ISO 13849-2 validation performed" |
| "the harness never imports the subject" | "the system is certified" |

The test for any sentence: *can I point at a scenario and a bundle for it?* If not, it
belongs in the right column.

## What makes the positive claim sound

- verdicts are three-valued, and `confirmed` requires an **independent observation** —
  a subject's own ACK is recorded as a claim, never as proof;
- a run where the harness asked the subject to exit lands in `insufficient_evidence`,
  because a shutdown the harness requested proves nothing about the subject;
- a subject's self-reported device state is marked `subject_claim` and is excluded from
  the independent-observation set by a test;
- coverage never inflates: an unavailable transport is `BLOCKED` with a reason, never a
  skip that reads as a pass;
- the negative controls must keep refuting, or CI fails.

That is a defensible claim about **acceptance evidence**. It is not a safety claim.
