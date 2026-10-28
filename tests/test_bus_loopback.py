"""The end-to-end L2 path, exercised without a CAN interface.

This machine cannot create a vcan: `unshare -Urn` is refused and there is no
CAP_NET_ADMIN, so `ip link add vcan0 type vcan` returns Operation not permitted. That
is a property of the host, not of the observer, and it must not stop the observer's
logic from being tested.

So the socket is replaced with one that hands back the exact bytes the kernel would
return (`struct.pack("=IB3x8s", can_id, dlc, data)`), and everything above it runs for
real: the decode, the level tagging, the claim evaluation. What this does not prove is
that a real vcan delivers frames - that is the CI job's job, and it is stated as such
rather than implied.
"""

from __future__ import annotations

import struct

import pytest

from assurance.observers.bus import BusObserver
from assurance.oracle import evaluate
from assurance.runner import run_scenario
from assurance.timeline import Timeline
from assurance.wire.mcu_wire_v1 import DeviceMode, Fault, Frame, FrameKind, Opcode, Result, encode, stop_command


class FakeCanSocket:
    """Behaves like an AF_CAN raw socket whose queue holds the frames we put in it."""

    def __init__(self, frames: list[bytes]) -> None:
        self.pending = list(frames)
        self.closed = False
        self.sent: list[bytes] = []

    def settimeout(self, _timeout: float) -> None:
        pass

    def bind(self, _address) -> None:
        pass

    def recv(self, _size: int) -> bytes:
        if self.pending:
            return self.pending.pop(0)
        raise TimeoutError("no more frames")

    def close(self) -> None:
        self.closed = True


def kernel_frame(can_id: int, data: bytes) -> bytes:
    return struct.pack("=IB3x8s", can_id, len(data), data.ljust(8, b"\x00"))


def stop_ack(command_id: int, mode: DeviceMode = DeviceMode.STOPPED) -> bytes:
    arbitration_id, data = encode(
        Frame(
            kind=FrameKind.STOP_ACK, command_id=command_id, opcode=Opcode.STOP,
            result_code=Result.ACCEPTED, fault_code=Fault.NONE, device_mode=mode,
        )
    )
    return kernel_frame(arbitration_id, data)


def telemetry(mode: DeviceMode, fault: Fault = Fault.NONE, sequence: int = 1) -> bytes:
    arbitration_id, data = encode(
        Frame(kind=FrameKind.TELEMETRY, sequence_no=sequence, fault_code=fault, device_mode=mode)
    )
    return kernel_frame(arbitration_id, data)


def observer_with(frames: list[bytes]) -> BusObserver:
    observer = BusObserver("vcan0")
    observer.sock = FakeCanSocket(frames)  # type: ignore[assignment]
    return observer


def test_a_stop_and_its_ack_become_a_confirmed_l2_claim() -> None:
    """The full path: bytes from the socket -> decoded frames -> confirmed claim."""

    stop_id, stop_data = stop_command(0x8001)
    observer = observer_with([
        kernel_frame(stop_id, stop_data),
        stop_ack(0x8001),
        telemetry(DeviceMode.STOPPED),
    ])
    timeline = Timeline()
    observer.drain(until_ms=50)
    assert observer.record(timeline) == 3

    verdict = evaluate(
        [
            {"claim_id": "I4", "kind": "stop_on_bus", "requires": "L2"},
            {"claim_id": "I5a", "kind": "stop_ack_on_bus", "requires": "L2"},
            {"claim_id": "I5b", "kind": "mode_on_bus", "requires": "L2", "expect_mode": "stopped"},
        ],
        timeline.events,
    )
    assert [c["status"] for c in verdict["claims"]] == ["confirmed", "confirmed", "confirmed"]


def test_a_bus_that_only_carries_an_ack_refutes_the_stop_claim() -> None:
    """The device answered a stop nobody sent: the stop claim must not confirm."""

    observer = observer_with([stop_ack(0x8001)])
    timeline = Timeline()
    observer.drain(until_ms=50)
    observer.record(timeline)

    verdict = evaluate([{"claim_id": "I4", "kind": "stop_on_bus", "requires": "L2"}], timeline.events)
    assert verdict["claims"][0]["status"] == "refuted"


def test_a_watchdog_report_becomes_a_confirmed_failsafe_claim() -> None:
    observer = observer_with([telemetry(DeviceMode.FAULTED, Fault.WATCHDOG_EXPIRED)])
    timeline = Timeline()
    observer.drain(until_ms=50)
    observer.record(timeline)

    verdict = evaluate(
        [{"claim_id": "I7", "kind": "failsafe_on_link_loss", "requires": "L2", "budget_ms": 150}],
        timeline.events,
    )
    assert verdict["claims"][0]["status"] == "confirmed"
    assert verdict["claims"][0]["reason"] == "device_reported_watchdog_expiry"


def test_the_observer_records_nothing_it_did_not_receive() -> None:
    """A flattering observer is worse than none: empty queue means empty timeline."""

    observer = observer_with([])
    timeline = Timeline()
    observer.drain(until_ms=20)
    assert observer.record(timeline) == 0
    assert not [e for e in timeline.events if e["kind"] == "bus_frame"]


def test_the_socketcan_scenario_degrades_honestly_without_an_interface(tmp_path) -> None:
    """On a host with no vcan the run must say so, and the L2 claims must be insufficient."""

    import json
    import pathlib

    from assurance.registry import load

    scenario = load("socketcan/cancel-then-stop-on-bus")
    bundle = run_scenario(scenario, tmp_path / "run")
    verdict = json.loads((pathlib.Path(bundle["bundle"]) / "deterministic/verdict.json").read_text())
    statuses = {c["status"] for c in verdict["claims"]}
    if BusObserver("vcan0").available()[0]:
        pytest.skip("this host actually has vcan0, so the unavailable path cannot be tested")
    assert statuses == {"insufficient_evidence"}

    # Without an interface the transport refuses at start(), before the observer is
    # ever attached, so the reason lives on run_end rather than on a note. That is the
    # honest shape: the run says why it could not look, and the claims stay unprovable.
    timeline = json.loads((pathlib.Path(bundle["bundle"]) / "deterministic/timeline.json").read_text())
    endings = [e for e in timeline if e["kind"] == "run_end"]
    assert endings and endings[0]["outcome"] == "transport_unavailable", endings
    assert "CAP_NET_ADMIN" in endings[0]["detail"]
    assert not [e for e in timeline if e["kind"] == "bus_frame"], "no frames may be invented"
