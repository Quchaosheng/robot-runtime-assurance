"""Transport for a system under test that is only reachable through its own CLI.

`WorkbenchTransport` drives `workbench-mobile-home-robot` the way a stranger would:
it runs the published command line, reads the evidence bundle that command publishes,
and translates the bundle's `events.jsonl` into this harness's timeline.

Two rules make this a black box rather than a wrapper:

* **Nothing is imported from the SUT.** The workbench package is never added to
  `sys.path`, so a passing verdict cannot be explained by shared state.
* **The SUT's own verdict is not evidence.** Workbench publishes a
  `task_terminal` status and per-action `device_state` values. Those are the
  subject's *claims*. This transport records them as claims; only the process
  boundary (a kill, an exit, a published bundle that never appeared) counts as an
  independent observation.

shortcut: the SUT is pinned to a `scripted` evidence class, so every verdict here
is about the runtime's rules, not about a robot moving; upgrade when a Gazebo or
hardware runner is available on the executing host.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys

from ..timeline import Timeline
from ..transports import TransportError

DEFAULT_SUT_ROOT = pathlib.Path(os.environ.get("ASSURANCE_WORKBENCH_ROOT", "/data/builds/caogao/wmhr"))


def _shim() -> str:
    """The local import shim, when the SUT is not installed as a package.

    A checkout without `pip install -e .` cannot resolve its own namespace
    packages. The shim lives outside the SUT so this harness still imports
    nothing from it; it only teaches the SUT's own interpreter where its files are.
    """
    return os.environ.get("ASSURANCE_WORKBENCH_SHIM", "/data/builds/caogao/.wbshim")


class WorkbenchTransport:
    name = "workbench"
    owns_process = True

    def __init__(self, sut_args: list[str] | None = None) -> None:
        self.root = pathlib.Path(os.environ.get("ASSURANCE_WORKBENCH_ROOT", str(DEFAULT_SUT_ROOT)))
        self.sut_args = sut_args or []
        self.proc: subprocess.Popen | None = None
        self.bundle: pathlib.Path | None = None
        self.published: dict | None = None

    def available(self) -> tuple[bool, str]:
        cli = self.root / "tools/scripts/sim_cli.py"
        if not cli.is_file():
            return False, f"no workbench checkout at {self.root}"
        return True, str(cli)

    # -- lifecycle ---------------------------------------------------------
    def start(self, timeline: Timeline) -> None:
        cli = self.root / "tools/scripts/sim_cli.py"
        if not cli.is_file():
            raise TransportError(f"workbench CLI not found at {cli}")
        self.bundle = None
        timeline.add("note", detail="workbench SUT configured", root=str(self.root), cli=str(cli))

    def stop(self, timeline: Timeline) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    # -- the one operation this harness is allowed to request --------------
    def run_task(self, scenario_id: str, output_dir: pathlib.Path, *, runner: str = "scripted",
                 timeout_s: int = 120) -> dict:
        """Run one registered SUT scenario and return its published metadata."""
        env = {**os.environ, "PYTHONPATH": _shim()}
        command = [
            sys.executable, str(self.root / "tools/scripts/sim_cli.py"),
            "run", scenario_id, "--runner", runner,
            "--output-dir", str(output_dir), "--json",
        ]
        self.proc = subprocess.Popen(
            command, cwd=str(self.root), env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        try:
            out, err = self.proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            out, err = self.proc.communicate()
            raise TransportError(f"workbench run exceeded {timeout_s}s") from None
        payload = _last_json(out) or {}
        runs = sorted(pathlib.Path(output_dir).glob("*/metadata.json"))
        self.published = payload
        return {
            "argv": command,
            "exit_code": self.proc.returncode,
            "stdout_tail": out.strip().splitlines()[-1:] or [""],
            "stderr_tail": err.strip().splitlines()[-3:],
            "summary": payload,
            "bundle": str(runs[0].parent) if runs else None,
            "metadata": json.loads(runs[0].read_text()) if runs else None,
        }

    def kill(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()

    # -- translation -------------------------------------------------------
    def ingest_bundle(self, timeline: Timeline, bundle: pathlib.Path, *, task_id: str,
                      requested_exit: bool = False) -> dict:
        """Turn a published workbench bundle into this harness's event shape.

        The mapping is deliberately one-way and lossy in the safe direction: a
        workbench `device_state` becomes a `device_state` event that is marked as
        the subject's claim, and the subject's `task_terminal` becomes the
        harness's `ack`. Neither is an independent observation.
        """
        bundle = pathlib.Path(bundle)
        events_path = bundle / "events.jsonl"
        if not events_path.is_file():
            timeline.add("note", detail="bundle has no events.jsonl", bundle=str(bundle))
            return {"events": 0, "bundle": str(bundle)}
        count = 0
        for line in events_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            _translate_one(timeline, record, task_id=task_id, source=str(bundle))
            count += 1
        if requested_exit:
            timeline.add("sut_exit", code=0, requested=True, reason="runner finished")
        return {"events": count, "bundle": str(bundle)}


def _translate_one(timeline: Timeline, record: dict, *, task_id: str, source: str) -> None:
    kind = record.get("event_type")
    payload = record.get("payload") or {}
    # The task epoch of a black-box run is its run identity, not the per-event
    # sequence number: every event in one run belongs to the same task generation.
    epoch = record.get("run_id")

    if kind == "task_accepted":
        timeline.add("task_start", task_id=task_id, epoch=epoch, source=source,
                     expect="done", subject_claim=True)
    elif kind == "action_result":
        outcome = payload.get("outcome")
        device = payload.get("device_state")
        # The subject's own account of one action. `requested` is set so these can
        # never be mistaken for an observation this harness made itself.
        timeline.add("ack", task_id=task_id, epoch=epoch, status=_ack_status(outcome),
                     action_id=payload.get("action_id"), subject_claim=True, requested=True,
                     device_state=device, source=source)
        if device:
            timeline.add("device_state", task_id=task_id, state=_device_state(device),
                         subject_claim=True, requested=True, source=source)
    elif kind == "verification":
        timeline.add("note", detail="workbench verification",
                     status=payload.get("status"), reason_code=payload.get("reason_code"),
                     source=source)
    elif kind == "task_terminal":
        timeline.add("ack", task_id=task_id, epoch=epoch,
                     status=_ack_status(payload.get("status")), terminal=True,
                     subject_claim=True, requested=True, source=source)


def _ack_status(value: object) -> str:
    text = str(value or "").lower()
    if text in {"confirmed", "completed", "succeeded", "done"}:
        return "done"
    if text in {"cancelled", "canceled", "stopped", "safe_stop"}:
        return "cancelled"
    return "rejected"


def _device_state(value: object) -> str:
    text = str(value or "").lower()
    if text in {"confirmed", "completed", "idle", "done"}:
        return "idle"
    if text in {"stopped", "safe_stop"}:
        return "safe_stop"
    return "busy" if text else "unknown"


def _last_json(text: str) -> dict | None:
    for chunk in text.strip().splitlines()[::-1]:
        chunk = chunk.strip()
        if chunk.startswith("{"):
            try:
                return json.loads(chunk)
            except json.JSONDecodeError:
                continue
    return None


def find_published(output_dir: pathlib.Path) -> list[pathlib.Path]:
    """Every published bundle under a run directory, newest last."""
    return sorted(path for path in pathlib.Path(output_dir).glob("*") if (path / "metadata.json").is_file())


def copy_bundle_into_evidence(source: pathlib.Path, destination: pathlib.Path) -> None:
    """Attach the subject's own bundle to ours, unmodified.

    Kept as a copy rather than a reference: a reviewer re-checking this evidence
    must not need the SUT's checkout, and the SUT must not be able to rewrite the
    artifact after the verdict was computed.
    """
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination)
