"""A SocketCAN transport that is honest about what this host can actually do.

`AF_CAN` is part of the Python standard library, so opening a CAN socket needs no
dependency. What it does need is an interface, and creating a virtual one needs
`CAP_NET_ADMIN`. On a host where `ip link add vcan0 type vcan` returns
`Operation not permitted`, a CAN scenario cannot produce evidence, and this module
says so instead of simulating an interface.

The design rule this file follows: **the transport reports availability from a real
socket probe, never from a config flag.** A run on a host without an interface
returns `insufficient_evidence` with the reason, which is the truthful outcome.

Wire format comes from `assurance.wire.mcu_wire_v1`, which is written from the
system under test's own specification and cross-checked against its committed byte
vectors. An earlier version of this module invented a layout (`0x102` for STOP,
opcode in byte 0 where the protocol requires a version byte) and its tests only ever
encoded and decoded with itself, so nothing could notice. Test vectors now come from
the firmware, not from here.
"""

from __future__ import annotations

import os
import socket
import struct
import time
from typing import Any

from ..timeline import Timeline
from ..transports import TransportError
from ..wire.mcu_wire_v1 import (
    Frame,
    FrameKind,
    Opcode,
    WireError,
    decode,
    stop_command,
)
from ..wire.mcu_wire_v1 import (
    encode as encode_frame,
)

CAN_IFACE = os.environ.get("ASSURANCE_CAN_IFACE", "vcan0")

# Ordinary traffic and STOP traffic use different opcodes and different id ranges;
# this transport only ever needs to issue an ordinary local-motion command or a STOP.
OP_MOVE = Opcode.MOVE


def interface_present(iface: str = CAN_IFACE) -> tuple[bool, str]:
    """Does this host have the interface at all? A directory check, not a guess."""
    path = f"/sys/class/net/{iface}"
    if not os.path.isdir(path):
        return False, f"{iface} is absent (needs CAP_NET_ADMIN to create a vcan device)"
    return True, path


def can_socket_available() -> tuple[bool, str]:
    """Can we build an AF_CAN socket? Distinguishes 'no support' from 'no interface'."""
    if not hasattr(socket, "AF_CAN"):
        return False, "this Python has no AF_CAN support"
    try:
        probe = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
    except OSError as error:
        return False, f"AF_CAN socket refused: {error}"
    probe.close()
    return True, "AF_CAN socket can be created"


class SocketCanTransport:
    """Drive a CAN-speaking device. Reports unavailability instead of faking it."""

    name = "socketcan"
    owns_process = False

    def __init__(self, sut_args: list[str] | None = None, iface: str | None = None) -> None:
        self.iface = iface or CAN_IFACE
        self.sut_args = sut_args or []
        self.sock: socket.socket | None = None
        self.command_id = 0
        self.pending: dict[int, str] = {}

    # -- availability ------------------------------------------------------
    def available(self) -> tuple[bool, str]:
        ok, why = can_socket_available()
        if not ok:
            return False, why
        present, detail = interface_present(self.iface)
        if not present:
            return False, detail
        return True, f"{self.iface} ready"

    # -- lifecycle ---------------------------------------------------------
    def start(self, timeline: Timeline) -> None:
        ok, why = self.available()
        if not ok:
            raise TransportError(why)
        try:
            self.sock = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
            self.sock.bind((self.iface,))
            self.sock.settimeout(0.5)
        except OSError as error:
            raise TransportError(f"cannot bind {self.iface}: {error}") from error
        timeline.add("note", detail="socketcan bound", iface=self.iface)

    def stop(self, timeline: Timeline) -> None:
        if self.sock is not None:
            self.sock.close()
            self.sock = None
            timeline.add("note", detail="socketcan closed")

    # -- wire format -------------------------------------------------------
    def _frame(self, frame_id: int, payload: bytes) -> None:
        if self.sock is None:
            raise TransportError("CAN socket is not open")
        data = payload.ljust(8, b"\x00")
        try:
            self.sock.send(struct.pack("=IB3x8s", frame_id, len(payload), data))
        except OSError as error:
            raise TransportError(f"send on {self.iface} failed: {error}") from error

    def send(self, *parts: object) -> None:
        """Issue one command. Exactly one frame leaves per call.

        The previous version also wrote a STOP frame after every ordinary command.
        That is worse than noise: an observer would read a stop on the bus and
        conclude the machine halted when it did not.
        """
        op = str(parts[0])
        task_id = str(parts[1]) if len(parts) > 1 else ""
        if op == "stop":
            self.command_id = 0x8000 | ((self.command_id + 1) & 0x7FFF)
            arbitration_id, data = stop_command(self.command_id)
        else:
            self.command_id = (self.command_id + 1) & 0x7FFF
            arbitration_id, data = encode_frame(
                Frame(kind=FrameKind.COMMAND, command_id=self.command_id, opcode=OP_MOVE)
            )
        # Remember which task a command id belongs to. Without this the ACK for
        # command 7 could be attributed to whichever task happens to be in flight,
        # which is the contamination I6 exists to prevent.
        self.pending[self.command_id] = task_id
        self._frame(arbitration_id, data)

    def drain(self, timeline: Timeline, until_ms: int = 0) -> None:
        if self.sock is None:
            return
        deadline = time.monotonic() + max(until_ms, 0) / 1000.0
        while True:
            try:
                frame = self.sock.recv(16)
            except TimeoutError:
                if time.monotonic() >= deadline:
                    return
                continue
            except OSError as error:
                timeline.add("note", detail=f"can receive failed: {error}")
                return
            if len(frame) < 16:
                continue
            can_id, dlc = struct.unpack("=II", frame[:8])
            data = frame[8:16][: min(dlc, 8)]
            self._record(can_id, data, timeline)
            if until_ms and time.monotonic() >= deadline:
                return

    def _task_for(self, command_id: int) -> str:
        """The task a command id was issued for, or an explicit unknown.

        `can:<id>` is kept for a command this transport never sent: that is an
        uncorrelated ACK, and naming it as if it belonged to a task would be a lie.
        """
        return self.pending.get(command_id, f"can-uncorrelated:{command_id}")

    def _record(self, can_id: int, data: bytes, timeline: Timeline) -> None:
        """Translate one bus frame into a timeline event.

        A frame the codec refuses is recorded as an observation, never dropped: an
        unparseable frame on the bus is a finding about the device, and silence about
        it would read as a quiet bus.
        """
        can_id &= socket.CAN_SFF_MASK if hasattr(socket, "CAN_SFF_MASK") else 0x7FF
        try:
            frame = decode(can_id, data)
        except WireError as error:
            timeline.add("note", detail="undecodable can frame", can_id=can_id,
                         payload=data.hex(), reason=str(error))
            return
        if frame.kind is FrameKind.ACK:
            from ..wire.mcu_wire_v1 import Result

            timeline.add("ack", task_id=self._task_for(frame.command_id),
                         status="done" if frame.result_code is Result.ACCEPTED else "rejected",
                         command_id=frame.command_id, can_id=can_id,
                         device_mode=frame.device_mode.name.lower())
        elif frame.kind is FrameKind.STOP_ACK:
            timeline.add("stop_ack", task_id=self._task_for(frame.command_id),
                         command_id=frame.command_id, can_id=can_id,
                         device_mode=frame.device_mode.name.lower())
        elif frame.kind is FrameKind.TELEMETRY:
            timeline.add("device_state", state=frame.device_mode.name.lower(),
                         sequence_no=frame.sequence_no, fault=frame.fault_code.name.lower(),
                         can_id=can_id)
        else:
            timeline.add("note", detail="unexpected can frame kind", can_id=can_id,
                         kind=frame.kind.name)


def decode_frame(frame: bytes) -> dict[str, Any]:
    """Parse one raw CAN frame into its fields; shared by the transport and tests."""
    can_id, dlc = struct.unpack("=II", frame[:8])
    return {"can_id": can_id & 0x7FF, "dlc": dlc, "data": frame[8:16][: min(dlc, 8)]}
