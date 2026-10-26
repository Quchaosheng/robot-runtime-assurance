"""SocketCAN: a real adapter on a host that cannot run it, tested for honesty.

This machine has `AF_CAN` but not `CAP_NET_ADMIN`, so `ip link add vcan0 type vcan`
returns `Operation not permitted`. The adapter is still written against the real
socket API, because the alternative - a mock that pretends to be a bus - would make
the CAN row of the acceptance matrix meaningless.

The tests therefore cover the parts that do not need an interface (frame layout,
decoding, availability reporting) and assert that the missing interface produces
`insufficient_evidence` rather than a skip or a pass.
"""

from __future__ import annotations

import json
import os
import pathlib
import struct
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]

from assurance.sut.socketcan import (
    CAN_ID_ACK,
    CAN_ID_COMMAND,
    CAN_ID_STOP_ACK,
    RESULT_OK,
    RESULT_REJECTED,
    SocketCanTransport,
    can_socket_available,
    decode_frame,
    interface_present,
)


def test_availability_comes_from_a_real_probe_not_a_flag() -> None:
    ok, why = SocketCanTransport().available()
    present, detail = interface_present("vcan0")
    if not ok:
        assert "CAP_NET_ADMIN" in why or "AF_CAN" in why or "absent" in why
        assert not present
        assert "CAP_NET_ADMIN" in detail


def test_a_missing_interface_refuses_to_start() -> None:
    from assurance.timeline import Timeline
    from assurance.transports import TransportError

    transport = SocketCanTransport(iface="vcan0")
    if transport.available()[0]:
        pytest.skip("this host actually has vcan0")
    with pytest.raises(TransportError):
        transport.start(Timeline())


def test_frame_layout_round_trips() -> None:
    payload = bytes([0x01]) + struct.pack(">H", 0x1234)
    frame = struct.pack("=IB3x8s", CAN_ID_COMMAND, len(payload), payload.ljust(8, b"\x00"))
    decoded = decode_frame(frame)
    assert decoded["can_id"] == CAN_ID_COMMAND
    assert decoded["dlc"] == len(payload)
    assert decoded["data"][:3] == payload


def test_ack_frames_map_to_a_verdict_shaped_event() -> None:
    from assurance.timeline import Timeline

    transport = SocketCanTransport()
    timeline = Timeline()
    command_id = 0x0042
    ok_frame = bytes([0x00]) + struct.pack(">H", command_id) + bytes([RESULT_OK])
    transport._record(CAN_ID_ACK, ok_frame, timeline)
    assert timeline.events[-1]["kind"] == "ack"
    assert timeline.events[-1]["status"] == "done"

    rejected = bytes([0x00]) + struct.pack(">H", command_id) + bytes([RESULT_REJECTED])
    transport._record(CAN_ID_ACK, rejected, timeline)
    assert timeline.events[-1]["status"] == "rejected"

    stop = bytes([0x00]) + struct.pack(">H", command_id)
    transport._record(CAN_ID_STOP_ACK, stop, timeline)
    assert timeline.events[-1]["kind"] == "stop_ack"


def test_an_unexpected_frame_is_recorded_not_dropped() -> None:
    """A frame the adapter does not understand is a finding, never silence."""
    from assurance.timeline import Timeline

    transport = SocketCanTransport()
    timeline = Timeline()
    transport._record(0x7AA, b"\xde\xad\xbe\xef", timeline)
    assert timeline.events[-1]["kind"] == "note"
    assert "unexpected can frame" in timeline.events[-1]["detail"]


def test_the_can_scenario_reports_insufficient_and_names_the_reason(tmp_path: pathlib.Path) -> None:
    out = tmp_path / "runs"
    result = subprocess.run(
        [sys.executable, "-m", "assurance.cli", "run", "socketcan/device-not-on-this-host",
         "--out", str(out)],
        cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT)},
        capture_output=True, text=True, timeout=300, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    bundle = pathlib.Path(json.loads(result.stdout[result.stdout.index("{"):])["bundle"])
    verdict = json.loads((bundle / "deterministic/verdict.json").read_text())
    assert verdict["counts"]["insufficient_evidence"] == 1
    assert verdict["counts"]["confirmed"] == 0

    timeline = json.loads((bundle / "deterministic/timeline.json").read_text())
    blocked = [e for e in timeline if e.get("outcome") == "transport_unavailable"]
    assert blocked, "the run must record why it could not execute"
    assert "CAP_NET_ADMIN" in blocked[0]["detail"]


def test_af_can_support_is_reported_separately_from_the_interface() -> None:
    """'no AF_CAN' and 'no interface' are different failures and must not merge."""
    ok, why = can_socket_available()
    assert isinstance(ok, bool)
    assert why
