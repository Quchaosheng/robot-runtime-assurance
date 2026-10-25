# Architecture

```
scenario (JSON, registered)
        |
   registry.load()                 I1: not registered -> refused, never guessed
        |
   runner.run_scenario()
        |  decides nothing; records everything
        +--> transport (mock | ros2action)  <-- black box boundary
        |         |
        |         +--> SUT process / ROS 2 action server
        |         |        ACK, device state, STOP ACK, death
        |         v
        |    timeline (append-only, monotonic t_ms)
        |         |
        |    fault injection at the boundary: drop/delay/stale ACK, kill, hold resource
        v
   oracle.evaluate(claims, timeline)      pure function: no clock, no I/O, no random
        |
   verdict: confirmed | refuted | insufficient_evidence
        |
   evidence.write_bundle()
        deterministic/  (re-judgable)      observed/  (not replayable)
        manifest + checksums + COMPLETE-last
```

## Design decisions and their reasons

**The runner never decides.** It only records. If the thing that runs the test also
judges it, a rerun can produce a different verdict from the same recording, and the
bundle stops being evidence.

**The oracle is a pure function of a list of events.** That is what makes
`assure verify` possible: re-judging the stored timeline must reproduce the same
digest, and a test tampers with the timeline to prove the check catches it.

**Two halves in the bundle.** `deterministic/` can be re-judged; `observed/` cannot be
replayed. Mixing them is how "replay" turns into a claim nobody can check.

**Fault injection is session-level.** On an unprivileged host, `tc`, `iptables`,
`vcan`, cgroup writes and `ptrace` are all unavailable (verified: `assure doctor`).
Packet-level chaos would therefore be a claim we cannot back with evidence. Instead we
control the conversation: an ACK that never comes, one that comes late, one that
carries the previous epoch, a process that dies, a resource that stays held. All of it
is deterministic and all of it is in the timeline.

**The ROS 2 transport uses a stock action type.** If the harness imported the SUT's
message package, the black-box boundary would already be broken. `Fibonacci` ships
with every ROS 2 install, so the harness stays independent of any SUT IDL.

**Negative controls carry `expect_verdict`.** `assure run` exits 0 when the run behaved
as designed — including when the design is "this broken SUT must be refuted". This is
what lets CI tell a working harness from a regressed one without a human reading logs.

## Known limits

- Faults are injected only at the harness↔SUT boundary, never inside the SUT.
- The mock SUT is a stand-in; it proves the decision path, not robot physics.
- Only the ROS 2 action transport touches a real middleware stack.
- No claim here is a functional-safety claim.
