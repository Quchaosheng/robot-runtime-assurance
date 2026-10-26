"""Deterministic mock SUT: one process, one line protocol, no ROS, no dependencies.

It exists to be measured and to be broken. Every behaviour is selected by a flag,
never by chance, so a run can be replayed by re-running the same command.

stdin   CMD start <task_id> <epoch> <resource> <deadline_ms>
        CMD cancel <task_id>
        CMD quit
        CMD stop <task_id>
        CMD raw <joint> <value>       (only when the raw policy accepts it)
stdout  EV claim_granted|claim_denied <task_id> <epoch>
        EV stop_ack <task_id>
        EV ack <task_id> <epoch> <done|rejected>
        EV state <idle|busy|safe_stop>
        EV cancelled <task_id>
        EV error <text>
"""

from __future__ import annotations

import argparse
import queue
import sys
import threading
import time


class Sut:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.held: dict[str, str] = {}          # resource -> task_id
        self.pending: list[dict] = []           # scheduled work
        self.epoch_floor = 0                    # stale-epoch mode: report this instead
        self.out: queue.Queue[str] = queue.Queue()
        self.running = True

    def emit(self, *parts: object) -> None:
        if self.args.silent and parts[0] == "state":
            return                      # a device that reports no state cannot be confirmed
        self.out.put("EV " + " ".join(str(p) for p in parts))

    # -- commands ---------------------------------------------------------
    def handle(self, line: str) -> None:
        parts = line.split()
        if len(parts) < 2 or parts[0] != "CMD":
            self.emit("error", "bad_command")
            return
        op = parts[1]
        if op == "start" and len(parts) == 6:
            self.start(parts[2], int(parts[3]), parts[4], int(parts[5]))
        elif op == "cancel" and len(parts) == 3:
            self.cancel(parts[2])
        elif op == "stop" and len(parts) == 3:
            self.stop(parts[2])
        elif op == "raw" and len(parts) == 4:
            self.raw(parts[2], parts[3])
        elif op == "quit":
            self.running = False
        else:
            self.emit("error", "bad_command")

    def raw(self, joint: str, value: str) -> None:
        """A direct actuator command, the thing a policy bypass would look like.

        `reject` is the only correct behaviour for a real executor. `accept` exists so
        a negative control can prove the harness notices a device that obeys a raw
        command the model should never have been able to send.
        """
        if self.args.raw_policy == "reject":
            self.emit("raw_rejected", joint)
            return
        self.emit("raw_accepted", joint, value)
        self.emit("state", "busy")

    def start(self, task_id: str, epoch: int, resource: str, deadline_ms: int) -> None:
        holder = self.held.get(resource)
        if holder is not None and self.args.claim_policy == "deny":
            self.emit("claim_denied", task_id, epoch)
            return
        self.held[resource] = task_id
        self.emit("claim_granted", task_id, epoch)
        self.emit("state", "busy")
        # Work takes as long as it takes; the deadline is the caller's problem, not ours.
        self.pending.append({"task_id": task_id, "epoch": epoch, "resource": resource,
                             "due": time.monotonic() + self.args.work_ms / 1000.0})

    def cancel(self, task_id: str) -> None:
        if self.args.cancel_policy == "ignore":
            return                      # keep working: the harness must notice
        self.pending = [p for p in self.pending if p["task_id"] != task_id]
        self.release(task_id)
        self.emit("cancelled", task_id)

    def stop(self, task_id: str) -> None:
        """A STOP the device may refuse to answer. I5 is about the ACK, not the request."""
        if self.args.cancel_policy == "ignore":
            return                      # keep working and stay silent: the harness must notice
        self.pending = [p for p in self.pending if p["task_id"] != task_id]
        self.release(task_id)
        self.emit("stop_ack", task_id)
        self.emit("state", "idle")

    def release(self, task_id: str) -> None:
        for resource, holder in list(self.held.items()):
            if holder == task_id:
                del self.held[resource]
        if not self.held and not self.pending:
            self.emit("state", "idle")

    # -- loop -------------------------------------------------------------
    def run(self) -> None:
        threading.Thread(target=self._reader, daemon=True).start()
        while self.running:
            now = time.monotonic()
            for job in list(self.pending):
                if job["due"] <= now:
                    self.pending.remove(job)
                    self.complete(job)
            time.sleep(0.005)
        if self.held or self.pending:
            self.emit("state", "safe_stop")   # stopped with work outstanding: say so
        else:
            self.emit("state", "idle")

    def complete(self, job: dict) -> None:
        if self.args.drop_ack:
            self.release(job["task_id"])       # device did the work, silently
            return
        if self.args.delay_ack:
            time.sleep(self.args.delay_ack / 1000.0)
        epoch = self.epoch_floor if self.args.stale_epoch else job["epoch"]
        self.emit("ack", job["task_id"], epoch, "done")
        self.release(job["task_id"])

    def _reader(self) -> None:
        for line in sys.stdin:
            line = line.strip()
            if line:
                self.handle(line)
                if not self.running:
                    return
        self.running = False  # parent closed stdin: treat as a normal shutdown


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--work-ms", type=int, default=40)
    ap.add_argument("--claim-policy", default="deny", choices=["deny", "grant-all"])
    ap.add_argument("--cancel-policy", default="honour", choices=["honour", "ignore"])
    ap.add_argument("--drop-ack", action="store_true")
    ap.add_argument("--delay-ack", type=int, default=0, metavar="MS")
    ap.add_argument("--stale-epoch", action="store_true")
    ap.add_argument("--silent", action="store_true", help="never report device state")
    ap.add_argument("--raw-policy", default="reject", choices=["reject", "accept"],
                    help="what the device does with a direct actuator command (I2)")
    sut = Sut(ap.parse_args())
    threading.Thread(target=sut.run, daemon=True).start()
    while sut.running:
        try:
            line = sut.out.get(timeout=0.05)
        except queue.Empty:
            continue
        print(line, flush=True)
    while not sut.out.empty():
        print(sut.out.get(), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
