"""L2: evidence from the wire, produced without asking the subject.

The observer exists to answer questions the subject cannot answer about itself. Two
things make it trustworthy and both are tested here:

* **it cannot transmit.** There is no `send` on it, so "the listener only listened" is a
  property of the code rather than a promise in a docstring;
* **it reads frames with the codec written from the specification.** A frame it cannot
  decode is recorded as invalid, not dropped - a malformed frame on the bus is a
  finding about the device.

The claims are tested from both sides, because a claim that only ever confirms has not
been shown to discriminate.
"""

from __future__ import annotations

import struct

from assurance.observers.bus import BusObserver, ObservedFrame
from assurance.oracle import evaluate
from assurance.timeline import Timeline
from assurance.wire.mcu_wire_v1 import (
    DeviceMode,
    Fault,
    Frame,
    FrameKind,
    Opcode,
    Result,
    encode,
    stop_command,
)


def raw(can_id: int, data: bytes) -> bytes:
    """Pack a frame the way the kernel hands it to a raw CAN socket."""
    return struct.pack("=IB3x8s", can_id, len(data), data.ljust(8, b"\x00"))


def stop_ack_frame(command_id: int = 0x8001, *, mode: DeviceMode = DeviceMode.STOPPED) -> bytes:
    arbitration_id, data = encode(
        Frame(
            kind=FrameKind.STOP_ACK, command_id=command_id, opcode=Opcode.STOP,
            result_code=Result.ACCEPTED, fault_code=Fault.NONE, device_mode=mode,
        )
    )
    return raw(arbitration_id, data)


def telemetry_frame(*, mode: DeviceMode, fault: Fault = Fault.NONE, sequence: int = 1) -> bytes:
    arbitration_id, data = encode(
        Frame(kind=FrameKind.TELEMETRY, sequence_no=sequence, fault_code=fault, device_mode=mode)
    )
    return raw(arbitration_id, data)


def test_the_observer_cannot_transmit() -> None:
    """Independence is structural. A listener with a send path is not a listener."""
    observer = BusObserver()
    assert not hasattr(observer, "send")
    assert not hasattr(observer, "inject")
    assert not hasattr(BusObserver, "send")


def test_availability_is_probed_not_assumed() -> None:
    ok, why = BusObserver("vcan0").available()
    if not ok:
        assert "CAP_NET_ADMIN" in why or "AF_CAN" in why or "absent" in why


def test_a_stop_frame_decodes_to_l2() -> None:
    observer = BusObserver()
    arbitration_id, data = stop_command(0x8001)
    frame = observer._parse(raw(arbitration_id, data))
    assert frame is not None
    assert frame.kind == "STOP"
    assert frame.command_id == 0x8001
    assert frame.can_id == 0x080


def test_telemetry_carries_the_mode_and_the_fault() -> None:
    observer = BusObserver()
    frame = observer._parse(telemetry_frame(mode=DeviceMode.FAULTED, fault=Fault.WATCHDOG_EXPIRED))
    assert frame is not None
    assert frame.detail["device_mode"] == "faulted"
    assert frame.detail["fault"] == "watchdog_expired"


def test_an_undecodable_frame_is_recorded_as_invalid() -> None:
    """A frame the codec refuses is a finding about the device, never silence."""
    observer = BusObserver()
    frame = observer._parse(raw(0x080, bytes.fromhex("0080000500000000")))
    assert frame is not None
    assert frame.kind == "invalid"
    assert frame.detail.get("reason")


def test_observed_frames_enter_the_timeline_at_l2() -> None:
    observer = BusObserver()
    observer.frames = [
        ObservedFrame(can_id=0x080, data=b"", timestamp_ns=1, kind="STOP", command_id=0x8001,
                      detail={"device_mode": "idle", "fault": "none", "result": "accepted"}),
    ]
    timeline = Timeline()
    assert observer.record(timeline) == 1
    assert timeline.events[0]["level"] == 2
    assert timeline.events[0]["frame_kind"] == "STOP"


# -- the claims ------------------------------------------------------------


def bus_events(*frames: dict) -> list[dict]:
    return [{"t_ms": 10 + index, "kind": "bus_frame", "level": 2, **frame} for index, frame in enumerate(frames)]


def test_stop_on_bus_confirms_from_a_frame_alone() -> None:
    timeline = bus_events({"frame_kind": "STOP", "can_id": 0x080, "command_id": 0x8001})
    verdict = evaluate([{"claim_id": "c", "kind": "stop_on_bus", "requires": "L2"}], timeline)
    assert verdict["claims"][0]["status"] == "confirmed"


def test_stop_on_bus_refutes_when_the_bus_was_quiet() -> None:
    """The claim is falsifiable: a watched bus with no STOP is a violation."""
    timeline = bus_events({"frame_kind": "COMMAND", "can_id": 0x100, "command_id": 1})
    verdict = evaluate([{"claim_id": "c", "kind": "stop_on_bus", "requires": "L2"}], timeline)
    assert verdict["claims"][0]["status"] == "refuted"
    assert verdict["claims"][0]["reason"] == "no_stop_frame_on_bus"


def test_stop_on_bus_is_insufficient_without_any_observer() -> None:
    verdict = evaluate([{"claim_id": "c", "kind": "stop_on_bus", "requires": "L2"}], [])
    assert verdict["claims"][0]["status"] == "insufficient_evidence"
    assert verdict["claims"][0]["reason"] == "no_bus_observation"


def test_a_self_reported_ack_is_not_a_bus_frame() -> None:
    """The rule the whole level system rests on, checked at the claim itself."""
    timeline = [
        {"t_ms": 1, "kind": "ack", "level": 0, "task_id": "A", "status": "done"},
        {"t_ms": 2, "kind": "stop_ack", "level": 2, "task_id": "A"},
    ]
    verdict = evaluate([{"claim_id": "c", "kind": "stop_on_bus", "requires": "L2"}], timeline)
    assert verdict["claims"][0]["status"] == "insufficient_evidence"


def test_stop_ack_correlates_with_its_stop() -> None:
    timeline = bus_events(
        {"frame_kind": "STOP", "can_id": 0x080, "command_id": 0x8001},
        {"frame_kind": "STOP_ACK", "can_id": 0x081, "command_id": 0x8001, "device_mode": "stopped"},
    )
    verdict = evaluate([{"claim_id": "c", "kind": "stop_ack_on_bus", "requires": "L2"}], timeline)
    entry = verdict["claims"][0]
    assert entry["status"] == "confirmed"
    assert entry["latency_ms"] == 1


def test_a_stop_ack_for_another_command_is_refuted() -> None:
    """Correlation is the point: an answer to a different command is not an answer."""
    timeline = bus_events(
        {"frame_kind": "STOP", "can_id": 0x080, "command_id": 0x8001},
        {"frame_kind": "STOP_ACK", "can_id": 0x081, "command_id": 0x8002},
    )
    verdict = evaluate([{"claim_id": "c", "kind": "stop_ack_on_bus", "requires": "L2"}], timeline)
    assert verdict["claims"][0]["status"] == "refuted"
    assert verdict["claims"][0]["reason"] == "stop_ack_ids_do_not_match_any_stop"


def test_a_stop_ack_that_arrived_before_its_stop_is_refuted() -> None:
    timeline = [
        {"t_ms": 20, "kind": "bus_frame", "level": 2, "frame_kind": "STOP", "can_id": 0x080, "command_id": 0x8001},
        {"t_ms": 10, "kind": "bus_frame", "level": 2, "frame_kind": "STOP_ACK", "can_id": 0x081, "command_id": 0x8001},
    ]
    verdict = evaluate([{"claim_id": "c", "kind": "stop_ack_on_bus", "requires": "L2"}], timeline)
    assert verdict["claims"][0]["status"] == "refuted"
    assert verdict["claims"][0]["reason"] == "stop_ack_predates_its_stop"


def test_mode_on_bus_confirms_when_telemetry_reports_the_safe_mode() -> None:
    timeline = bus_events({"frame_kind": "TELEMETRY", "can_id": 0x180, "device_mode": "stopped"})
    verdict = evaluate([{"claim_id": "c", "kind": "mode_on_bus", "requires": "L2", "expect_mode": "stopped"}], timeline)
    assert verdict["claims"][0]["status"] == "confirmed"


def test_mode_on_bus_refutes_when_the_device_stayed_moving() -> None:
    timeline = bus_events({"frame_kind": "TELEMETRY", "can_id": 0x180, "device_mode": "moving"})
    verdict = evaluate([{"claim_id": "c", "kind": "mode_on_bus", "requires": "L2", "expect_mode": "stopped"}], timeline)
    assert verdict["claims"][0]["status"] == "refuted"
    assert verdict["claims"][0]["observed"] == ["moving"]


def test_failsafe_confirms_on_a_watchdog_report() -> None:
    timeline = bus_events({"frame_kind": "TELEMETRY", "can_id": 0x180, "fault": "watchdog_expired"})
    verdict = evaluate([{"claim_id": "c", "kind": "failsafe_on_link_loss", "requires": "L2", "budget_ms": 150}], timeline)
    assert verdict["claims"][0]["status"] == "confirmed"


def test_failsafe_refutes_when_no_watchdog_was_ever_reported() -> None:
    timeline = bus_events({"frame_kind": "TELEMETRY", "can_id": 0x180, "fault": "none"})
    verdict = evaluate([{"claim_id": "c", "kind": "failsafe_on_link_loss", "requires": "L2", "budget_ms": 150}], timeline)
    assert verdict["claims"][0]["status"] == "refuted"


def test_stale_ack_ignored_needs_both_halves() -> None:
    """The replay must be visible *and* must not have completed anything."""
    replayed = bus_events({"frame_kind": "ACK", "can_id": 0x101, "command_id": 0x0013})
    confirmed = evaluate([{"claim_id": "c", "kind": "stale_ack_ignored", "requires": "L2", "stale_command_id": 0x0013}], replayed)
    assert confirmed["claims"][0]["status"] == "confirmed"

    with_completion = [*replayed, {"t_ms": 99, "kind": "ack", "level": 2, "status": "done", "task_id": "B"}]
    refuted = evaluate([{"claim_id": "c", "kind": "stale_ack_ignored", "requires": "L2", "stale_command_id": 0x0013}], with_completion)
    assert refuted["claims"][0]["status"] == "refuted"
    assert refuted["claims"][0]["reason"] == "stale_ack_completed_a_task"


def test_stale_ack_ignored_is_insufficient_when_the_replay_never_happened() -> None:
    """No replay means the injector did not fire, not that the property held."""
    timeline = bus_events({"frame_kind": "COMMAND", "can_id": 0x100, "command_id": 0x0014})
    verdict = evaluate([{"claim_id": "c", "kind": "stale_ack_ignored", "requires": "L2", "stale_command_id": 0x0013}], timeline)
    assert verdict["claims"][0]["status"] == "insufficient_evidence"
    assert verdict["claims"][0]["reason"] == "stale_ack_never_observed"
