"""SocketCAN: a real adapter on a host that cannot run it, tested for honesty.

This machine has `AF_CAN` but not `CAP_NET_ADMIN`, so `ip link add vcan0 type vcan`
returns `Operation not permitted`. The adapter is written against the real socket API
anyway, because a mock bus would make the CAN row of the acceptance matrix mean
nothing.

The frame layout is no longer this module's own invention. It comes from
`assurance.wire.mcu_wire_v1`, which is checked byte-for-byte against the firmware's
committed vectors in `tests/test_wire_v1.py`. What remains here is the transport
behaviour: when it may run, what it emits per call, and how it reads the bus.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys

import pytest

from assurance.sut.socketcan import SocketCanTransport, can_socket_available, decode_frame, interface_present
from assurance.timeline import Timeline
from assurance.transports import TransportError
from assurance.wire.mcu_wire_v1 import (
    CAN_ID_STOP,
    DeviceMode,
    Fault,
    Frame,
    FrameKind,
    Opcode,
    Result,
    encode,
    stop_command,
)

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_availability_comes_from_a_real_probe_not_a_flag() -> None:
    ok, why = SocketCanTransport().available()
    present, detail = interface_present("vcan0")
    if not ok:
        assert "CAP_NET_ADMIN" in why or "AF_CAN" in why or "absent" in why
        assert not present
        assert "CAP_NET_ADMIN" in detail


def test_a_missing_interface_refuses_to_start() -> None:
    transport = SocketCanTransport(iface="vcan0")
    if transport.available()[0]:
        pytest.skip("this host actually has vcan0")
    with pytest.raises(TransportError):
        transport.start(Timeline())


def test_frame_layout_round_trips_through_the_spec_codec() -> None:
    """The transport writes Wire V1 bytes; there is no second encoding here."""

    arbitration_id, data = stop_command(0x8001)
    decoded = decode_frame(_raw(arbitration_id, data))
    assert decoded["can_id"] == CAN_ID_STOP
    assert decoded["data"][0] == 0x10
    assert decoded["data"][1:3] == b"\x80\x01"
    assert decoded["data"][3] == int(Opcode.STOP)


def test_ack_frames_map_to_a_verdict_shaped_event() -> None:
    transport = SocketCanTransport()
    timeline = Timeline()

    accepted = encode(
        Frame(
            kind=FrameKind.ACK, command_id=0x0013, opcode=Opcode.HOLD,
            result_code=Result.ACCEPTED, fault_code=Fault.NONE, device_mode=DeviceMode.HOLDING,
        )
    )
    transport._record(accepted[0], accepted[1], timeline)
    assert timeline.events[-1]["kind"] == "ack"
    assert timeline.events[-1]["status"] == "done"
    assert timeline.events[-1]["device_mode"] == "holding"

    rejected = encode(
        Frame(
            kind=FrameKind.ACK, command_id=0x0014, opcode=Opcode.GRIP_CLOSE,
            result_code=Result.REJECTED, fault_code=Fault.MALFORMED_FRAME, device_mode=DeviceMode.FAULTED,
        )
    )
    transport._record(rejected[0], rejected[1], timeline)
    assert timeline.events[-1]["status"] == "rejected"


def test_stop_ack_carries_the_device_mode() -> None:
    transport = SocketCanTransport()
    timeline = Timeline()
    arbitration_id, data = encode(
        Frame(
            kind=FrameKind.STOP_ACK, command_id=0x8001, opcode=Opcode.STOP,
            result_code=Result.ACCEPTED, fault_code=Fault.NONE, device_mode=DeviceMode.STOPPED,
        )
    )
    transport._record(arbitration_id, data, timeline)
    assert timeline.events[-1]["kind"] == "stop_ack"
    assert timeline.events[-1]["device_mode"] == "stopped"


def test_telemetry_reports_the_device_mode_and_watchdog_fault() -> None:
    """L2 evidence for "did the device enter a safe state" comes from telemetry."""

    transport = SocketCanTransport()
    timeline = Timeline()
    arbitration_id, data = encode(
        Frame(
            kind=FrameKind.TELEMETRY, sequence_no=7,
            fault_code=Fault.WATCHDOG_EXPIRED, device_mode=DeviceMode.FAULTED,
        )
    )
    transport._record(arbitration_id, data, timeline)
    event = timeline.events[-1]
    assert event["kind"] == "device_state"
    assert event["fault"] == "watchdog_expired"
    assert event["sequence_no"] == 7


def test_an_undecodable_frame_is_recorded_not_dropped() -> None:
    """A frame the codec refuses is a finding about the device, never silence."""

    transport = SocketCanTransport()
    timeline = Timeline()
    transport._record(0x080, bytes.fromhex("0080000500000000"), timeline)
    assert timeline.events[-1]["kind"] == "note"
    assert timeline.events[-1]["detail"] == "undecodable can frame"
    assert timeline.events[-1]["reason"]


def test_an_unknown_identifier_is_recorded_not_treated_as_traffic() -> None:
    transport = SocketCanTransport()
    timeline = Timeline()
    transport._record(0x7AA, b"\xde\xad\xbe\xef\x00\x00\x00\x00", timeline)
    assert timeline.events[-1]["kind"] == "note"


def test_the_can_scenario_reports_insufficient_and_names_the_reason(tmp_path: pathlib.Path) -> None:
    out = tmp_path / "runs"
    result = subprocess.run(
        [sys.executable, "-m", "assurance.cli", "run", "socketcan/device-not-on-this-host", "--out", str(out)],
        cwd=ROOT, env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(ROOT)},
        capture_output=True, text=True, timeout=300, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    bundle = pathlib.Path(json.loads(result.stdout[result.stdout.index("{"):])["bundle"])
    verdict = json.loads((bundle / "deterministic/verdict.json").read_text())
    assert verdict["counts"]["insufficient_evidence"] == 1
    assert verdict["counts"]["confirmed"] == 0

    timeline = json.loads((bundle / "deterministic/timeline.json").read_text())
    blocked = [event for event in timeline if event.get("outcome") == "transport_unavailable"]
    assert blocked, "the run must record why it could not execute"
    assert "CAP_NET_ADMIN" in blocked[0]["detail"]


def test_af_can_support_is_reported_separately_from_the_interface() -> None:
    """'no AF_CAN' and 'no interface' are different failures and must not merge."""
    ok, why = can_socket_available()
    assert isinstance(ok, bool)
    assert why


def _raw(arbitration_id: int, data: bytes) -> bytes:
    """Pack a frame the way the kernel returns it from a raw CAN socket."""
    import struct

    return struct.pack("=IB3x8s", arbitration_id, len(data), data.ljust(8, b"\x00"))
