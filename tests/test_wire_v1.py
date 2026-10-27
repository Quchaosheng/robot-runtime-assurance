"""Wire V1 conformance, checked against the SUT's own committed vectors.

The previous version of this repository tested the CAN codec by encoding a frame and
decoding it again. That only proves the codec agrees with itself; it cannot notice
that both halves use an identifier the device never sends. It shipped with STOP on
0x102 where the specification says 0x080, and with a version byte missing.

So these tests never invent a vector. Every expected byte comes from
`tests/vectors/mcu_wire_v1_golden.json`, which was copied from the firmware's own
committed golden_vectors[] and from interfaces/examples/mcu-frame-stop-ack.json.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from assurance.wire.mcu_wire_v1 import (
    CAN_ID_ACK,
    CAN_ID_COMMAND,
    CAN_ID_STOP,
    CAN_ID_STOP_ACK,
    CAN_ID_TELEMETRY,
    WIRE_VERSION_V1,
    DeviceMode,
    Fault,
    Frame,
    FrameKind,
    Opcode,
    Result,
    WireError,
    decode,
    encode,
    is_stop,
    stop_command,
)

VECTORS = json.loads((pathlib.Path(__file__).parent / "vectors/mcu_wire_v1_golden.json").read_text())["vectors"]


def build(spec: dict) -> Frame:
    return Frame(
        kind=FrameKind[spec["kind"]],
        command_id=spec.get("command_id", 0),
        sequence_no=spec.get("sequence_no", 0),
        opcode=Opcode[spec.get("opcode", "RESERVED")],
        retry_count=spec.get("retry_count", 0),
        result_code=Result[spec.get("result_code", "ACCEPTED")],
        fault_code=Fault[spec.get("fault_code", "NONE")],
        device_mode=DeviceMode[spec.get("device_mode", "IDLE")],
    )


@pytest.mark.parametrize("vector", VECTORS, ids=[vector["name"] for vector in VECTORS])
def test_the_encoder_reproduces_the_suts_own_bytes(vector: dict) -> None:
    arbitration_id, data = encode(build(vector["frame"]))
    assert arbitration_id == vector["arbitration_id"], vector["name"]
    assert data.hex(" ") == vector["data"].lower(), vector["name"]


@pytest.mark.parametrize("vector", VECTORS, ids=[vector["name"] for vector in VECTORS])
def test_the_decoder_reads_the_suts_own_bytes(vector: dict) -> None:
    data = bytes.fromhex(vector["data"].replace(" ", ""))
    assert decode(vector["arbitration_id"], data) == build(vector["frame"]), vector["name"]


def test_stop_uses_the_highest_priority_identifier() -> None:
    """The bug this file replaced: STOP was sent on 0x102, which no device listens for."""
    arbitration_id, data = stop_command(0x8001)
    assert arbitration_id == CAN_ID_STOP == 0x080
    assert data[0] == WIRE_VERSION_V1
    assert data[1:3] == b"\x80\x01"
    assert data[3] == int(Opcode.STOP)


def test_sending_a_stop_does_not_also_emit_a_second_frame() -> None:
    """The other bug: every send() wrote a STOP frame alongside the real command.

    A stray STOP after every ordinary command makes the bus say the robot stopped when
    it did not, which is worse than silence - an observer would believe it.
    """
    frames = stop_command(0x8000)
    assert isinstance(frames, tuple) and len(frames) == 2, "stop_command returns exactly one frame"
    arbitration_id, data = frames
    assert decode(arbitration_id, data).kind is FrameKind.STOP


def test_the_frame_ids_are_the_ones_the_specification_names() -> None:
    assert (CAN_ID_STOP, CAN_ID_STOP_ACK, CAN_ID_COMMAND, CAN_ID_ACK, CAN_ID_TELEMETRY) == (
        0x080, 0x081, 0x100, 0x101, 0x180,
    )


def test_the_command_id_partitions_do_not_overlap() -> None:
    """Ordinary ids stop at 0x7fff and STOP ids start at 0x8000, so the id selects the
    kind of traffic. An ordinary command must never be valid as a STOP."""

    with pytest.raises(WireError):
        encode(Frame(kind=FrameKind.STOP, command_id=0x7FFF, opcode=Opcode.STOP))
    with pytest.raises(WireError):
        encode(Frame(kind=FrameKind.COMMAND, command_id=0x8000, opcode=Opcode.MOVE))


def test_an_ordinary_frame_cannot_carry_the_stop_opcode() -> None:
    with pytest.raises(WireError):
        encode(Frame(kind=FrameKind.COMMAND, command_id=1, opcode=Opcode.STOP))


def test_opcode_zero_is_refused() -> None:
    """Zero is reserved so a zero-filled buffer cannot execute anything."""

    with pytest.raises(WireError):
        encode(Frame(kind=FrameKind.COMMAND, command_id=1, opcode=Opcode.RESERVED))


@pytest.mark.parametrize(
    ("arbitration_id", "data", "reason"),
    [
        (0x102, bytes.fromhex("10800000 05000000".replace(" ", "")), "wrong STOP identifier"),
        (0x103, bytes.fromhex("1080000500000000".replace(" ", "")), "wrong STOP_ACK identifier"),
        (0x080, bytes.fromhex("0080000500000000".replace(" ", "")), "version byte is not 0x10"),
        (0x080, bytes.fromhex("1080000500000001".replace(" ", "")), "reserved byte is not zero"),
        (0x080, bytes.fromhex("107f000500000000".replace(" ", "")), "STOP id below 0x8000"),
        (0x080, bytes.fromhex("1080000100000000".replace(" ", "")), "STOP with a non-stop opcode"),
        (0x081, bytes.fromhex("1080010500000004".replace(" ", "")), "accepted result reporting faulted mode"),
        (0x081, bytes.fromhex("1080010500000100".replace(" ", "")), "accepted result carrying a fault"),
        (0x081, bytes.fromhex("1080010500010000".replace(" ", "")), "rejected result without a fault"),
        (0x180, bytes.fromhex("1000000000000001".replace(" ", "")), "telemetry reserved byte not zero"),
        (0x180, bytes.fromhex("10000000000100 00".replace(" ", "")), "fault not valid in telemetry"),
    ],
)
def test_the_decoder_refuses_each_broken_frame(arbitration_id: int, data: bytes, reason: str) -> None:
    with pytest.raises(WireError):
        decode(arbitration_id, data)


def test_the_decoder_refuses_a_short_or_long_frame() -> None:
    good = bytes.fromhex("1080010500000003")
    with pytest.raises(WireError):
        decode(0x081, good[:7])
    with pytest.raises(WireError):
        decode(0x081, good + b"\x00")
    with pytest.raises(WireError):
        decode(0x081, good, length=7)


def test_a_rejected_frame_leaves_nothing_half_decoded() -> None:
    """decode() returns a value or raises; there is no partially valid result."""

    with pytest.raises(WireError):
        decode(0x081, bytes.fromhex("1080010500000104"))
    # and a valid frame still decodes after a failed one
    assert decode(0x081, bytes.fromhex("1080010500000003")).device_mode is DeviceMode.STOPPED


def test_is_stop_only_accepts_a_real_stop() -> None:
    assert is_stop(*stop_command(0x8001))
    assert not is_stop(CAN_ID_STOP_ACK, bytes.fromhex("1080010500000003"))
    assert not is_stop(0x102, bytes.fromhex("1080000500000000"))
