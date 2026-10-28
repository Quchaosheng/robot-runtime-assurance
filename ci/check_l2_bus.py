#!/usr/bin/env python3
"""Prove the L2 setup works before trusting any L2 verdict.

A bus-level CI job has a failure mode worse than a red build: if the observer attaches
to nothing, every claim comes back `insufficient`, the job exits 0, and the pipeline
shows green while no evidence was ever collected. This script makes that impossible to
mistake for success.

It does the smallest possible end-to-end check:

  1. open a writer socket on the interface and send one STOP frame;
  2. open the read-only observer on the same interface;
  3. require the observer to read back a frame that decodes as that STOP.

Exits 0 only when a frame really travelled through the kernel and was decoded by the
Wire V1 codec. Anything else is a setup failure and says which step failed.
"""

from __future__ import annotations

import os
import socket
import struct
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from assurance.observers.bus import BusObserver
from assurance.wire.mcu_wire_v1 import CAN_ID_STOP, WireError, decode, stop_command

IFACE = os.environ.get("ASSURANCE_CAN_IFACE", "vcan0")


def main() -> int:
    if not hasattr(socket, "AF_CAN"):
        print("FAIL: this Python has no AF_CAN support")
        return 1
    if not os.path.isdir(f"/sys/class/net/{IFACE}"):
        print(f"FAIL: {IFACE} does not exist; the workflow must create it first")
        return 1

    # The observer must attach before the frame is sent, or it will miss it.
    observer = BusObserver(IFACE)
    try:
        observer.start()
    except OSError as error:
        print(f"FAIL: the observer could not attach to {IFACE}: {error}")
        return 1

    arbitration_id, data = stop_command(0x8001)
    sender = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
    try:
        sender.bind((IFACE,))
        sender.send(struct.pack("=IB3x8s", arbitration_id, len(data), data.ljust(8, b"\x00")))
        time.sleep(0.05)
        frames = observer.drain(until_ms=250)
    except OSError as error:
        print(f"FAIL: could not send or receive on {IFACE}: {error}")
        return 1
    finally:
        sender.close()
        observer.stop()

    if not frames:
        print(f"FAIL: sent one STOP frame on {IFACE} and the observer saw nothing.")
        print("      A green L2 job with no observation would be a false pass, so this is fatal.")
        return 1

    stops = [frame for frame in frames if frame.can_id == CAN_ID_STOP]
    if not stops:
        print(f"FAIL: {len(frames)} frame(s) observed but none was a STOP on 0x080:")
        for frame in frames[:5]:
            print(f"      0x{frame.can_id:03x} {frame.data.hex(' ')} kind={frame.kind}")
        return 1

    frame = stops[0]
    try:
        decoded = decode(frame.can_id, frame.data)
    except WireError as error:
        print(f"FAIL: the STOP frame did not decode: {error}")
        return 1

    print(f"OK: {IFACE} carried a STOP frame the observer read back and decoded.")
    print(f"    can_id=0x{frame.can_id:03x} command_id=0x{decoded.command_id:04x} "
          f"opcode={decoded.opcode.name} observer_frames={len(frames)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
