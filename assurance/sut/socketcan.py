"""A SocketCAN transport that is honest about what this host can actually do.

`AF_CAN` is part of the Python standard library, so opening a CAN socket needs no
dependency. What it does need is an interface, and creating a virtual one needs
`CAP_NET_ADMIN`. On a host where `ip link add vcan0 type vcan` returns
`Operation not permitted`, a CAN scenario cannot produce evidence, and this module
says so instead of simulating an interface.

The design rule this file follows: **the transport reports availability from a real
socket probe, never from a config flag.** A run on a host without an interface
returns `insufficient_evidence` with the reason, which is the truthful outcome.

Wire format is the one the reference robot already uses, so this adapter keeps the
same meaning as the device side rather than inventing a second protocol:

  arbitration id 0x100  command   data[0]=opcode, data[1:3]=command id (big endian)
  arbitration id 0x101  ack       data[1:3]=command id, data[3]=result
  arbitration id 0x102  stop      data[1:3]=command id
  arbitration id 0x103  stop ack  data[1:3]=command id
"""

from __future__ import annotations

import os
import socket
import struct
import time
from typing import Any

from ..timeline import Timeline
from ..transports import TransportError

CAN_IFACE = os.environ.get("ASSURANCE_CAN_IFACE", "vcan0")
CAN_ID_COMMAND = 0x100
CAN_ID_ACK = 0x101
CAN_ID_STOP = 0x102
CAN_ID_STOP_ACK = 0x103

OP_START = 0x01
OP_CANCEL = 0x02

RESULT_OK = 0x00
RESULT_REJECTED = 0x01


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
        op = str(parts[0])
        task_id = str(parts[1]) if len(parts) > 1 else ""
        self.command_id = (self.command_id + 1) & 0xFFFF
        opcode = OP_START if op == "start" else OP_CANCEL
        self._frame(CAN_ID_COMMAND, bytes([opcode]) + struct.pack(">H", self.command_id))
        self._frame(CAN_ID_STOP, b"\x00" + struct.pack(">H", self.command_id))

    def drain(self, timeline: Timeline, until_ms: int = 0) -> None:
        if self.sock is None:
            return
        deadline = time.monotonic() + max(until_ms, 0) / 1000.0
        while True:
            try:
                frame = self.sock.recv(16)
            except (TimeoutError, socket.timeout):
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

    def _record(self, can_id: int, data: bytes, timeline: Timeline) -> None:
        can_id &= socket.CAN_SFF_MASK if hasattr(socket, "CAN_SFF_MASK") else 0x7FF
        if can_id == CAN_ID_ACK and len(data) >= 4:
            command_id = int.from_bytes(data[1:3], "big")
            accepted = data[3] == RESULT_OK
            timeline.add("ack", task_id=f"can:{command_id}",
                         status="done" if accepted else "rejected", can_id=can_id)
        elif can_id == CAN_ID_STOP_ACK and len(data) >= 3:
            timeline.add("stop_ack", task_id=f"can:{int.from_bytes(data[1:3], 'big')}")
        else:
            timeline.add("note", detail="unexpected can frame", can_id=can_id,
                         payload=data.hex())


def decode_frame(frame: bytes) -> dict[str, Any]:
    """Parse one raw CAN frame into its fields; shared by the transport and tests."""
    can_id, dlc = struct.unpack("=II", frame[:8])
    return {"can_id": can_id & 0x7FF, "dlc": dlc, "data": frame[8:16][: min(dlc, 8)]}
