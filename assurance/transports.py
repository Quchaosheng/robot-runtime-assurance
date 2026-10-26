"""Transports: how the harness talks to a system under test without importing it.

`MockTransport` drives `assurance.sut.mock_sut` over pipes (works anywhere).
`Ros2ActionTransport` drives a ROS 2 action server; rclpy is imported lazily so the
package stays importable (and testable) on machines without ROS 2.

A transport only translates. It does not judge, retry, or hide faults.
"""

from __future__ import annotations

import contextlib
import queue
import signal
import subprocess
import sys
import threading
import time

from .timeline import Timeline


class TransportError(RuntimeError):
    pass


class MockTransport:
    name = "mock"
    owns_process = True

    def __init__(self, sut_args: list[str] | None = None) -> None:
        self.sut_args = sut_args or []
        self.proc: subprocess.Popen | None = None
        self.events: queue.Queue[str] = queue.Queue()
        self.dead = threading.Event()

    def start(self, timeline: Timeline) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "assurance.sut.mock_sut", *self.sut_args],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        threading.Thread(target=self._pump, daemon=True).start()
        timeline.add("note", detail="mock SUT started", pid=self.proc.pid)

    def _pump(self) -> None:
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            self.events.put(line.rstrip("\n"))
        self.dead.set()

    def send(self, *parts: object) -> None:
        if not self.proc or not self.proc.stdin:
            raise TransportError("SUT not running")
        if self.proc.poll() is not None:
            raise TransportError(f"SUT already exited with {self.proc.returncode}")
        try:
            self.proc.stdin.write("CMD " + " ".join(str(p) for p in parts) + "\n")
            self.proc.stdin.flush()
        except BrokenPipeError as exc:
            raise TransportError("SUT pipe closed") from exc

    def drain(self, timeline: Timeline, until_ms: int = 0) -> None:
        """Move everything the SUT said into the timeline. Never blocks past until_ms."""
        stop = time.monotonic() + until_ms / 1000.0
        while True:
            try:
                line = self.events.get(timeout=max(0.0, stop - time.monotonic()) if until_ms else 0.01)
            except queue.Empty:
                if time.monotonic() >= stop:
                    return
                continue
            self._record(line, timeline)
            if until_ms and time.monotonic() >= stop:
                return

    def _record(self, line: str, timeline: Timeline) -> None:
        parts = line.split()
        if len(parts) < 2 or parts[0] != "EV":
            timeline.add("note", detail=f"unparsable:{line[:120]}")
            return
        kind, rest = parts[1], parts[2:]
        if kind == "ack" and len(rest) == 3:
            timeline.add("ack", task_id=rest[0], epoch=int(rest[1]), status=rest[2])
        elif kind in ("claim_granted", "claim_denied") and len(rest) == 2:
            timeline.add(kind, task_id=rest[0], epoch=int(rest[1]))
        elif kind == "state" and rest:
            timeline.add("device_state", state=rest[0])
        elif kind == "rejected" and len(rest) == 2:
            timeline.add("rejected", task_id=rest[0], detail=rest[1])
        elif kind == "stop_ack" and rest:
            timeline.add("stop_ack", task_id=rest[0])
        elif kind == "cancelled" and rest:
            timeline.add("cancelled", task_id=rest[0])
        elif kind in ("raw_accepted", "raw_rejected") and rest:
            timeline.add("raw_command", joint=rest[0], accepted=kind == "raw_accepted",
                         value=rest[1] if len(rest) > 1 else None)
        elif kind == "error":
            timeline.add("note", detail="sut_error:" + " ".join(rest))
        else:
            timeline.add("note", detail=f"unknown:{line[:120]}")

    def kill(self, timeline: Timeline) -> None:
        if not self.proc or self.proc.poll() is not None:
            timeline.add("note", detail="kill requested but SUT already gone")
            return
        self.proc.send_signal(signal.SIGKILL)
        code = self.proc.wait(timeout=5)
        timeline.add("sut_killed", signal="SIGKILL", returncode=code)

    def stop(self, timeline: Timeline) -> None:
        if not self.proc:
            return
        code = self.proc.poll()
        if code is None:
            with contextlib.suppress(TransportError):
                self.send("quit")
            try:
                code = self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                code = self.proc.wait(timeout=2)
            self.drain(timeline, until_ms=100)
        # A killed SUT already has its evidence; do not mint a second exit record.
        if not timeline.of("sut_killed", "sut_exit"):
            timeline.add("sut_exit", returncode=code, requested=True)

    def available(self) -> tuple[bool, str]:
        return True, "python subprocess"


class Ros2ActionTransport:
    """Drives `execute_task` (or any action) on a foreign ROS 2 node.

    The harness never imports the SUT's packages; it only knows the action name and
    the goal/result fields. Delayed and dropped ACKs come free from the real DDS stack.
    """

    name = "ros2action"
    owns_process = True

    def __init__(self, action: str = "execute_task", sut_args: list[str] | None = None,
                 node_name: str = "assurance_client") -> None:
        self.action = action
        self.sut_args = sut_args or []
        self.node_name = node_name
        self.node = None
        self.client = None
        self.proc: subprocess.Popen | None = None
        self._rclpy = None
        self._pending_states: list[str] = []
        self._String = self._Trigger = None

    def available(self) -> tuple[bool, str]:
        try:
            import rclpy  # noqa: F401
        except ModuleNotFoundError:
            return False, "rclpy not importable (source /opt/ros/<distro>/setup.bash)"
        return True, "rclpy importable"

    def start(self, timeline: Timeline) -> None:
        ok, why = self.available()
        if not ok:
            raise TransportError(why)
        import rclpy
        import rclpy.action
        from action_tutorials_interfaces.action import Fibonacci
        self._rclpy = rclpy
        self._action_type = Fibonacci
        if self.sut_args:
            self.proc = subprocess.Popen(
                [sys.executable, "-m", "assurance.sut.ros2_sut", *self.sut_args])
            timeline.add("note", detail="ros2 SUT subprocess started", pid=self.proc.pid)
        rclpy.init()
        from std_msgs.msg import String
        from std_srvs.srv import Trigger
        self._String, self._Trigger = String, Trigger
        self.node = rclpy.create_node(self.node_name)
        self.client = rclpy.action.ActionClient(self.node, Fibonacci, self.action)
        self.state_client = self.node.create_client(Trigger, "assurance_stop")
        self.node.create_subscription(String, "assurance_state", self._on_state, 10)

        timeline.add("note", detail=f"waiting for action server {self.action}")
        if not self._wait_for_server(20.0):
            raise TransportError(f"action server {self.action} not available")

    def _on_state(self, msg) -> None:
        self._pending_states.append(msg.data)

    def _wait_for_server(self, timeout_s: float) -> bool:
        """wait_for_server does not spin our callback group, so discovery needs help."""
        end = time.monotonic() + timeout_s
        while time.monotonic() < end:
            self._rclpy.spin_once(self.node, timeout_sec=0.05)
            if self.client.server_is_ready():
                return True
        return False

    def send(self, *parts: object) -> None:
        raise TransportError("ros2action transport takes goals via run_task(), not send()")

    def run_task(self, task_id: str, epoch: int, order: int, deadline_ms: int,
                 timeline: Timeline, expect_timeout: bool = False) -> None:
        """Send one goal and wait no longer than deadline_ms for the result."""
        timeline.add("task_start", task_id=task_id, epoch=epoch, deadline_ms=deadline_ms,
                     injected_timeout=expect_timeout)
        goal = self._action_type.Goal()
        goal.order = order
        sent = self.client.send_goal_async(goal)
        # Waiting for a server to accept a goal is discovery, not work: give it its own
        # budget, and start the task clock only once the goal is accepted.
        if not self._spin_until(sent, max(2000, deadline_ms)):
            timeline.add("timeout", task_id=task_id, phase="goal_accept")
            return
        handle = sent.result()
        if handle is None or not handle.accepted:
            timeline.add("rejected", task_id=task_id, detail="goal_rejected")
            return
        timeline.add("task_accepted", task_id=task_id, epoch=epoch)
        result = handle.get_result_async()
        if not self._spin_until(result, deadline_ms):
            timeline.add("timeout", task_id=task_id, epoch=epoch, phase="result")
            timeline.add("cancel_sent", task_id=task_id)
            handle.cancel_goal_async()
            self.send_stop(task_id, timeline)     # I4: cancel escalates to STOP
            self._spin_until(handle.get_result_async(), 500)   # a late answer is evidence
            return
        timeline.add("ack", task_id=task_id, epoch=epoch, status="done")

    def send_stop(self, task_id: str, timeline: Timeline) -> None:
        """Call the device's STOP service and record whether it acknowledged."""
        if not self.state_client.wait_for_service(timeout_sec=5.0):
            timeline.add("note", detail="no assurance_stop service offered")
            return
        timeline.add("stop_sent", task_id=task_id)
        future = self.state_client.call_async(self._Trigger.Request())
        if self._spin_until(future, 2000) and future.result() and future.result().success:
            timeline.add("stop_ack", task_id=task_id)
            self.drain(timeline, until_ms=150)     # let the settled state arrive
        else:
            timeline.add("note", detail="stop was refused or timed out", task_id=task_id)

    def _spin_until(self, future, deadline_ms: int) -> bool:
        end = time.monotonic() + deadline_ms / 1000.0
        while not future.done() and time.monotonic() < end:
            self._rclpy.spin_once(self.node, timeout_sec=0.05)
        return future.done()

    def drain(self, timeline: Timeline, until_ms: int = 0) -> None:
        end = time.monotonic() + until_ms / 1000.0
        while time.monotonic() < end:
            self._rclpy.spin_once(self.node, timeout_sec=0.02)
            self._flush_states(timeline)

    def flush(self, timeline: Timeline) -> None:
        self._flush_states(timeline)

    def _flush_states(self, timeline: Timeline) -> None:
        while self._pending_states:
            timeline.add("device_state", state=self._pending_states.pop(0))

    def kill(self, timeline: Timeline) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGKILL)
            timeline.add("sut_killed", signal="SIGKILL", returncode=self.proc.wait(timeout=5))
        else:
            timeline.add("note", detail="no SUT process owned by the ros2 transport to kill")

    def stop(self, timeline: Timeline) -> None:
        if self.node is not None:
            self.drain(timeline, until_ms=100)
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            timeline.add("sut_exit", returncode=self.proc.wait(timeout=5))
        if self.node is not None:
            self.node.destroy_node()
            self.node = None
        if self._rclpy is not None and self._rclpy.ok():
            self._rclpy.shutdown()


class ArtifactTransport:
    """A 'system under test' that is a published evidence artifact, not a process.

    RoboTraceOpt is a diagnosis tool, not a robot: driving it as if it were a task
    runtime would be a category error. It appears here as a transport so its
    latency evidence travels through the same verification path as everything else,
    while never being asked to produce an action or an ACK.
    """

    name = "traceartifact"
    owns_process = False

    def __init__(self, sut_args: list[str] | None = None) -> None:
        self.sut_args = sut_args or []

    def available(self) -> tuple[bool, str]:
        return True, "reads published artifacts; no process is launched"

    def start(self, timeline: Timeline) -> None:
        timeline.add("note", detail="artifact transport started")

    def stop(self, timeline: Timeline) -> None:
        timeline.add("note", detail="artifact transport finished")


def make_transport(scenario: dict):
    if scenario["transport"] == "traceartifact":
        return ArtifactTransport(scenario.get("sut_args", []))
    if scenario["transport"] == "mock":
        return MockTransport(scenario.get("sut_args", []))
    if scenario["transport"] == "socketcan":
        from .sut.socketcan import SocketCanTransport
        return SocketCanTransport(sut_args=scenario.get("sut_args", []))
    if scenario["transport"] == "workbench":
        from .sut.workbench import WorkbenchTransport
        return WorkbenchTransport(scenario.get("sut_args", []))
    return Ros2ActionTransport(sut_args=scenario.get("sut_args", []))
