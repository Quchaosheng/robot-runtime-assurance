"""MCU CAN Wire V1, implemented independently from the written specification.

This is a second implementation, written from
`workbench-mobile-home-robot:docs/architecture/mcu-wire-v1.md` and cross-checked
against that repository's committed golden vectors. It deliberately does **not**
import anything from the system under test: if this file and the firmware disagreed,
importing their codec would hide exactly the disagreement this project exists to find.
The Python and C implementations agreeing is the evidence; sharing code would be the
absence of evidence.

Wire facts this module encodes, all from the specification:

  CAN ID  0x080 stop (host->MCU, highest priority)
          0x081 stop_ack (MCU->host)
          0x100 command (host->MCU)
          0x101 ack (MCU->host)
          0x180 telemetry (MCU->host)

  Every frame is DLC 8, big-endian multi-byte fields, byte 0 is the compact version
  0x10. Reserved bytes must be zero. An id selects exactly one frame kind.

  command / stop   [0]=version [1..2]=command_id [3]=opcode [4]=retry [5..7]=0
  ack / stop_ack   [0]=version [1..2]=command_id [3]=opcode [4]=retry
                   [5]=result [6]=fault [7]=device_mode
  telemetry        [0]=version [1..4]=sequence_no [5]=fault [6]=device_mode [7]=0

  command carries ids 0x0000..0x7fff and ordinary opcodes only; stop carries
  0x8000..0xffff and opcode stop only. Encoding a numeric enum is not enough: the
  result/fault/mode combination must also be one the protocol permits.

The decoder refuses the whole frame when anything is wrong, leaving the caller's
frame untouched. A decoder that returns a half-parsed frame is how an invalid STOP
becomes a plausible one.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import IntEnum

WIRE_VERSION_V1 = 0x10
WIRE_DLC = 8

CAN_ID_STOP = 0x080
CAN_ID_STOP_ACK = 0x081
CAN_ID_COMMAND = 0x100
CAN_ID_ACK = 0x101
CAN_ID_TELEMETRY = 0x180

COMMAND_ID_MAX = 0x7FFF
STOP_ID_MIN = 0x8000


class FrameKind(IntEnum):
    COMMAND = 0
    ACK = 1
    TELEMETRY = 2
    STOP = 3
    STOP_ACK = 4


class Opcode(IntEnum):
    RESERVED = 0
    MOVE = 1
    GRIP_OPEN = 2
    GRIP_CLOSE = 3
    HOLD = 4
    STOP = 5
    HEARTBEAT = 6


class Result(IntEnum):
    ACCEPTED = 0
    REJECTED = 1


class Fault(IntEnum):
    NONE = 0
    ACK_TIMEOUT = 1
    STOP_TIMEOUT = 2
    STOP_REJECTED = 3
    LINK_LOST = 4
    DUPLICATE_FRAME = 5
    WATCHDOG_EXPIRED = 6
    MALFORMED_FRAME = 7


class DeviceMode(IntEnum):
    IDLE = 0
    MOVING = 1
    HOLDING = 2
    STOPPED = 3
    FAULTED = 4


CAN_ID_FOR_KIND = {
    FrameKind.STOP: CAN_ID_STOP,
    FrameKind.STOP_ACK: CAN_ID_STOP_ACK,
    FrameKind.COMMAND: CAN_ID_COMMAND,
    FrameKind.ACK: CAN_ID_ACK,
    FrameKind.TELEMETRY: CAN_ID_TELEMETRY,
}
KIND_FOR_CAN_ID = {can_id: kind for kind, can_id in CAN_ID_FOR_KIND.items()}

# The modes a frame may legally report, per frame kind and result. Encoding 4 into
# the mode byte is not the same as "faulted is a valid outcome here".
FAULTS_ALLOWED = {
    FrameKind.COMMAND: frozenset(),
    FrameKind.STOP: frozenset(),
    FrameKind.ACK: frozenset({Fault.NONE, Fault.DUPLICATE_FRAME, Fault.MALFORMED_FRAME}),
    FrameKind.STOP_ACK: frozenset({Fault.NONE, Fault.STOP_REJECTED}),
    FrameKind.TELEMETRY: frozenset({Fault.NONE, Fault.LINK_LOST, Fault.WATCHDOG_EXPIRED}),
}


class WireError(ValueError):
    """A frame cannot be encoded or decoded under Wire V1."""


@dataclass(frozen=True)
class Frame:
    kind: FrameKind
    command_id: int = 0
    sequence_no: int = 0
    opcode: Opcode = Opcode.RESERVED
    retry_count: int = 0
    result_code: Result = Result.ACCEPTED
    fault_code: Fault = Fault.NONE
    device_mode: DeviceMode = DeviceMode.IDLE


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise WireError(message)


def _check_id_partition(frame: Frame) -> None:
    if frame.kind in (FrameKind.COMMAND, FrameKind.ACK):
        _require(0 <= frame.command_id <= COMMAND_ID_MAX, f"command_id {frame.command_id} is not in the ordinary range")
    elif frame.kind in (FrameKind.STOP, FrameKind.STOP_ACK):
        _require(
            STOP_ID_MIN <= frame.command_id <= 0xFFFF,
            f"command_id {frame.command_id} is not in the STOP range",
        )


def _check_opcode(frame: Frame) -> None:
    if frame.kind in (FrameKind.STOP, FrameKind.STOP_ACK):
        _require(frame.opcode is Opcode.STOP, "STOP traffic must carry opcode stop")
    elif frame.kind in (FrameKind.COMMAND, FrameKind.ACK):
        _require(frame.opcode is not Opcode.STOP, "ordinary traffic must not carry opcode stop")
        _require(frame.opcode is not Opcode.RESERVED, "opcode 0 is reserved")


def encode(frame: Frame) -> tuple[int, bytes]:
    """Logical frame -> (arbitration id, 8 data bytes).

    Every field is validated before a single byte is produced, so a rejected frame
    cannot leave a partially written buffer behind.
    """

    _require(isinstance(frame, Frame), "frame must be a Wire V1 Frame")
    _require(frame.kind in CAN_ID_FOR_KIND, f"unknown frame kind {frame.kind!r}")
    _require(0 <= frame.retry_count <= 0xFF, "retry_count must fit in one byte")
    _check_id_partition(frame)
    _check_opcode(frame)

    data = bytearray(WIRE_DLC)
    data[0] = WIRE_VERSION_V1

    if frame.kind in (FrameKind.COMMAND, FrameKind.STOP):
        data[1:3] = frame.command_id.to_bytes(2, "big")
        data[3] = int(frame.opcode)
        data[4] = frame.retry_count
    elif frame.kind in (FrameKind.ACK, FrameKind.STOP_ACK):
        _require(frame.fault_code in FAULTS_ALLOWED[frame.kind], f"fault {frame.fault_code.name} cannot appear in a {frame.kind.name}")
        if frame.result_code is Result.ACCEPTED:
            _require(frame.fault_code is Fault.NONE, "an accepted result cannot carry a fault")
            _require(
                frame.device_mode is not DeviceMode.FAULTED,
                "an accepted result cannot report the faulted mode",
            )
        else:
            _require(frame.fault_code is not Fault.NONE, "a rejected result must name its fault")
        data[1:3] = frame.command_id.to_bytes(2, "big")
        data[3] = int(frame.opcode)
        data[4] = frame.retry_count
        data[5] = int(frame.result_code)
        data[6] = int(frame.fault_code)
        data[7] = int(frame.device_mode)
    else:  # telemetry
        _require(0 <= frame.sequence_no <= 0xFFFFFFFF, "sequence_no must fit in four bytes")
        _require(frame.fault_code in FAULTS_ALLOWED[FrameKind.TELEMETRY], f"fault {frame.fault_code.name} cannot appear in telemetry")
        data[1:5] = frame.sequence_no.to_bytes(4, "big")
        data[5] = int(frame.fault_code)
        data[6] = int(frame.device_mode)

    return CAN_ID_FOR_KIND[frame.kind], bytes(data)


def decode(arbitration_id: int, data: bytes, *, length: int | None = None) -> Frame:
    """(arbitration id, bytes) -> logical frame, or raise.

    A frame is refused whole. The spec is explicit that an invalid id, dlc, version,
    reserved byte, enum or cross-field combination must not publish output, because a
    caller that receives a half-decoded STOP will act on it.
    """

    _require(isinstance(arbitration_id, int) and not isinstance(arbitration_id, bool), "arbitration_id must be an int")
    kind = KIND_FOR_CAN_ID.get(arbitration_id)
    _require(kind is not None, f"arbitration id 0x{arbitration_id:03x} is not a Wire V1 id")

    dlc = WIRE_DLC if length is None else length
    _require(dlc == WIRE_DLC, f"DLC must be {WIRE_DLC}, got {dlc}")
    _require(isinstance(data, bytes | bytearray) and len(data) == WIRE_DLC, f"expected {WIRE_DLC} data bytes")
    data = bytes(data)
    _require(data[0] == WIRE_VERSION_V1, f"version byte must be 0x{WIRE_VERSION_V1:02x}, got 0x{data[0]:02x}")

    if kind in (FrameKind.COMMAND, FrameKind.STOP):
        _require(data[5:] == b"\x00\x00\x00", "reserved bytes must be zero")
        return replace(
            _validate(
                Frame(
                    kind=kind,
                    command_id=int.from_bytes(data[1:3], "big"),
                    opcode=_enum_or_fail(Opcode, data[3], "opcode"),
                    retry_count=data[4],
                )
            )
        )

    if kind in (FrameKind.ACK, FrameKind.STOP_ACK):
        frame = Frame(
            kind=kind,
            command_id=int.from_bytes(data[1:3], "big"),
            opcode=_enum_or_fail(Opcode, data[3], "opcode"),
            retry_count=data[4],
            result_code=_enum_or_fail(Result, data[5], "result"),
            fault_code=_enum_or_fail(Fault, data[6], "fault"),
            device_mode=_enum_or_fail(DeviceMode, data[7], "device mode"),
        )
        return _validate(frame)

    _require(data[7] == 0, "telemetry reserved byte must be zero")
    frame = Frame(
        kind=kind,
        sequence_no=int.from_bytes(data[1:5], "big"),
        fault_code=_enum_or_fail(Fault, data[5], "fault"),
        device_mode=_enum_or_fail(DeviceMode, data[6], "device mode"),
    )
    return _validate(frame)


def _enum_or_fail(enum_type, value: int, label: str):
    try:
        return enum_type(value)
    except ValueError as error:
        raise WireError(f"{label} value {value} is reserved") from error


def _validate(frame: Frame) -> Frame:
    """Re-run the encoder's cross-field checks on a decoded frame."""

    _check_id_partition(frame)
    _check_opcode(frame)
    if frame.kind in (FrameKind.ACK, FrameKind.STOP_ACK):
        _require(frame.fault_code in FAULTS_ALLOWED[frame.kind], f"fault {frame.fault_code.name} cannot appear in a {frame.kind.name}")
        if frame.result_code is Result.ACCEPTED:
            _require(frame.fault_code is Fault.NONE, "an accepted result cannot carry a fault")
            _require(frame.device_mode is not DeviceMode.FAULTED, "an accepted result cannot report the faulted mode")
        else:
            _require(frame.fault_code is not Fault.NONE, "a rejected result must name its fault")
    elif frame.kind is FrameKind.TELEMETRY:
        _require(frame.fault_code in FAULTS_ALLOWED[FrameKind.TELEMETRY], "fault is not valid in telemetry")
    return frame


def stop_command(command_id: int, *, retry_count: int = 0) -> tuple[int, bytes]:
    """The one frame that matters most here: a STOP on the highest-priority id."""
    return encode(Frame(kind=FrameKind.STOP, command_id=command_id, opcode=Opcode.STOP, retry_count=retry_count))


def is_stop(arbitration_id: int, data: bytes, *, length: int | None = None) -> bool:
    try:
        return decode(arbitration_id, data, length=length).kind is FrameKind.STOP
    except WireError:
        return False
