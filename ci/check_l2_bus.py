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


def fail(message: str) -> int:
    """Report a failure both to the terminal and as a CI annotation.

    The job log endpoint is not always readable by the tools investigating a failure,
    while check-run annotations are. So every failure is emitted as an annotation too,
    and the message says which step broke rather than only the exit code.
    """
    print(f"FAIL: {message}")
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::error title=L2 bus check::{message}")
    return 1


def main() -> int:
    if not hasattr(socket, "AF_CAN"):
        return fail("this Python has no AF_CAN support")
    if not os.path.isdir(f"/sys/class/net/{IFACE}"):
        return fail(f"{IFACE} does not exist; the workflow must create it first")

    # The observer must attach before the frame is sent, or it will miss it.
    observer = BusObserver(IFACE)
    try:
        observer.start()
    except OSError as error:
        return fail(f"the observer could not attach to {IFACE}: {error}")

    arbitration_id, data = stop_command(0x8001)
    sender = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
    try:
        sender.bind((IFACE,))
        sender.send(struct.pack("=IB3x8s", arbitration_id, len(data), data.ljust(8, b"\x00")))
        time.sleep(0.05)
        frames = observer.drain(until_ms=250)
    except OSError as error:
        return fail(f"could not send or receive on {IFACE}: {error}")
    finally:
        sender.close()
        observer.stop()

    if not frames:
        return fail(
            f"sent one STOP frame on {IFACE} and the observer saw nothing. "
            "A green L2 job with no observation would be a false pass."
        )

    stops = [frame for frame in frames if frame.can_id == CAN_ID_STOP]
    if not stops:
        detail = ", ".join(f"0x{frame.can_id:03x}/{frame.kind}" for frame in frames[:5])
        return fail(f"{len(frames)} frame(s) observed but none was a STOP on 0x080: {detail}")

    frame = stops[0]
    try:
        decoded = decode(frame.can_id, frame.data)
    except WireError as error:
        return fail(f"the STOP frame did not decode: {error}")

    print(f"OK: {IFACE} carried a STOP frame the observer read back and decoded.")
    print(f"    can_id=0x{frame.can_id:03x} command_id=0x{decoded.command_id:04x} "
          f"opcode={decoded.opcode.name} observer_frames={len(frames)}")
    return 0


if __name__ == "__main__":
    # Any uncaught exception must still name itself in the CI annotation: an
    # AttributeError once escaped here and the job reported only "exit code 1".
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException as error:
        print(f"FAIL: unexpected {type(error).__name__}: {error}")
        if os.environ.get("GITHUB_ACTIONS") == "true":
            print(f"::error title=L2 bus check crashed::{type(error).__name__}: {error}")
        raise SystemExit(1) from error
