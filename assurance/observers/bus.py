"""A read-only CAN listener: L2 evidence, produced without asking the subject anything.

This is the observer that turns "the runtime says it stopped" into "the STOP frame was
on the bus and the device answered it". It is deliberately a separate socket from the
transport's: an observer that shares the sender's handle can be fooled by the sender,
and the whole point of L2 is that it cannot.

Two properties matter more than throughput:

* **It never transmits.** The socket is bound read-only and ``send`` is not implemented.
  Independence is a property of the code path, not a promise in a comment.
* **It stamps frames with the kernel's time**, not the time the Python thread woke up.
  Under load those differ by milliseconds, which is the same order as the budgets being
  measured.

What this observer cannot do is worth stating too: on a virtual bus it sees ordering,
not physical latency. A frame that takes 3 ms on vcan proves the ordering rule and says
nothing about a real bus.
"""

from __future__ import annotations

import socket
import struct
import time
from dataclasses import dataclass, field

from ..timeline import Timeline
from ..wire.mcu_wire_v1 import CAN_ID_STOP, CAN_ID_STOP_ACK, CAN_ID_TELEMETRY, FrameKind, WireError, decode


@dataclass
class ObservedFrame:
    """One frame as the bus observer saw it."""

    can_id: int
    data: bytes
    timestamp_ns: int
    kind: str | None = None
    command_id: int | None = None
    detail: dict = field(default_factory=dict)


class BusObserver:
    """Collect frames from one interface without ever writing to it.

    ``available()`` is a real probe, in the same spirit as the SocketCAN transport: a
    host without the interface reports why, and the caller records that reason instead
    of an empty bus.
    """

    def __init__(self, iface: str = "vcan0") -> None:
        self.iface = iface
        self.sock: socket.socket | None = None
        self.frames: list[ObservedFrame] = []
        # False until start() proves the kernel accepted the timestamp request.
        self.kernel_timestamps = False

    # -- availability ------------------------------------------------------
    def available(self) -> tuple[bool, str]:
        import os

        if not hasattr(socket, "AF_CAN"):
            return False, "this Python has no AF_CAN support"
        if not os.path.isdir(f"/sys/class/net/{self.iface}"):
            return False, f"{self.iface} is absent (needs CAP_NET_ADMIN to create a vcan device)"
        try:
            probe = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
        except OSError as error:
            return False, f"AF_CAN socket refused: {error}"
        probe.close()
        return True, f"{self.iface} readable"

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        ok, why = self.available()
        if not ok:
            raise OSError(why)
        self.sock = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
        # Ask the kernel to attach its own receive timestamp to each frame. The constant
        # is not exposed by every Python build (this one lacks it entirely, and the
        # unguarded attribute access is what crashed the first CI run of the bus job),
        # so the value is looked up defensively. SO_TIMESTAMPNS is 35 on Linux.
        timestamp_option = getattr(socket, "SO_TIMESTAMPNS", 35)
        try:
            self.sock.setsockopt(socket.SOL_SOCKET, timestamp_option, 1)
            self.kernel_timestamps = True
        except OSError:
            # Without it the observer still works, but its clock is this process's,
            # which is a different claim. Recorded so the bundle does not pretend.
            self.kernel_timestamps = False
        self.sock.bind((self.iface,))
        self.sock.settimeout(0.2)

    def stop(self) -> None:
        if self.sock is not None:
            self.sock.close()
            self.sock = None

    def __enter__(self) -> BusObserver:
        self.start()
        return self

    def __exit__(self, *_exc) -> None:
        self.stop()

    # -- collection --------------------------------------------------------
    def drain(self, until_ms: int = 0) -> list[ObservedFrame]:
        """Collect whatever arrived. Never raises on a bad frame; it records it."""
        if self.sock is None:
            return []
        deadline = time.monotonic() + max(until_ms, 0) / 1000.0
        collected: list[ObservedFrame] = []
        while True:
            try:
                raw, stamp_ns = self._receive()
            except (TimeoutError, socket.timeout):
                if time.monotonic() >= deadline:
                    break
                continue
            except OSError:
                break
            frame = self._parse(raw, stamp_ns)
            if frame is not None:
                collected.append(frame)
                self.frames.append(frame)
            if until_ms and time.monotonic() >= deadline:
                break
        return collected

    def _receive(self) -> tuple[bytes, int]:
        """Read one frame and the kernel's receive timestamp, if it sent one.

        ``recvmsg`` is used rather than ``recv`` because the timestamp arrives as
        ancillary data. Falls back to the process clock when the kernel attached
        nothing, and the frame records which clock it came with.
        """
        if self.sock is None:
            raise OSError("observer socket is not open")
        raw, ancillary, _flags, _address = self.sock.recvmsg(16, socket.CMSG_SPACE(16))
        for level, kind, payload in ancillary:
            if (
                level == socket.SOL_SOCKET
                and kind in (getattr(socket, "SCM_TIMESTAMPNS", -1), 35)
                and len(payload) >= 16
            ):
                seconds, nanos = struct.unpack("=qq", payload[:16])
                return raw, seconds * 1_000_000_000 + nanos
        return raw, time.monotonic_ns()

    def _parse(self, raw: bytes, timestamp_ns: int | None = None) -> ObservedFrame | None:
        if len(raw) < 16:
            return None
        can_id, _dlc = struct.unpack("=II", raw[:8])
        data = raw[8:16]
        frame = ObservedFrame(
            can_id=can_id & 0x7FF,
            data=data,
            timestamp_ns=time.monotonic_ns() if timestamp_ns is None else timestamp_ns,
        )
        try:
            decoded = decode(frame.can_id, data)
        except WireError as error:
            frame.kind = "invalid"
            frame.detail = {"reason": str(error)}
            return frame
        frame.kind = decoded.kind.name
        frame.command_id = decoded.command_id or None
        frame.detail = {
            "device_mode": decoded.device_mode.name.lower(),
            "fault": decoded.fault_code.name.lower(),
            "result": decoded.result_code.name.lower(),
        }
        return frame

    # -- what a claim can ask this observer --------------------------------
    def saw_stop(self, command_id: int | None = None) -> ObservedFrame | None:
        """A STOP frame on the highest-priority id, optionally for one command."""
        for frame in self.frames:
            if frame.can_id == CAN_ID_STOP and (command_id is None or frame.command_id == command_id):
                return frame
        return None

    def saw_stop_ack(self, command_id: int | None = None) -> ObservedFrame | None:
        for frame in self.frames:
            if frame.can_id == CAN_ID_STOP_ACK and (command_id is None or frame.command_id == command_id):
                return frame
        return None

    def saw_safe_mode(self) -> ObservedFrame | None:
        """Telemetry reporting a mode that is not moving or holding."""
        for frame in self.frames:
            if frame.can_id == CAN_ID_TELEMETRY and frame.detail.get("device_mode") in {"stopped", "faulted", "idle"}:
                return frame
        return None

    def saw_watchdog_expired(self) -> ObservedFrame | None:
        for frame in self.frames:
            if frame.kind == FrameKind.TELEMETRY.name and frame.detail.get("fault") == "watchdog_expired":
                return frame
        return None

    def record(self, timeline: Timeline) -> int:
        """Write every observed frame into the run timeline as L2 evidence.

        The level is set here, once, rather than at each call site: a frame this
        observer saw did not come from the subject, and that is the entire reason the
        observation is worth recording.
        """
        for frame in self.frames:
            timeline.add(
                "bus_frame",
                level=2,
                can_id=frame.can_id,
                frame_kind=frame.kind,
                command_id=frame.command_id,
                clock="kernel" if self.kernel_timestamps else "monotonic",
                **frame.detail,
            )
        return len(self.frames)
