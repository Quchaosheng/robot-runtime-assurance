"""Where to find the systems and artifacts this harness drives.

Nothing in this repository hardcodes an absolute path. Every location resolves in
this order:

1. an explicit environment variable (`ASSURANCE_WORKBENCH_ROOT`, ...);
2. a sibling directory next to this checkout, named by convention;
3. nothing - and then the caller reports the target as unavailable rather than
   guessing.

Point 3 is the important one. A harness that silently substitutes a path it found
by accident produces evidence for a system the reviewer did not choose; refusing is
the only honest answer.
"""

from __future__ import annotations

import os
import pathlib

# The directory that contains this checkout. Cloning `wmhr` and this repo side by
# side - the layout both projects document - is enough for the default to work.
CHECKOUT_PARENT = pathlib.Path(__file__).resolve().parents[2]

# Environment variable -> candidate locations, relative to the directory that holds
# this checkout. Several names per target because the real layout of a development
# tree varies: a sibling checkout, or one nested under a research directory.
KNOWN_TARGETS: dict[str, tuple[str, ...]] = {
    "ASSURANCE_WORKBENCH_ROOT": ("wmhr", "workbench-mobile-home-robot", "research/wmhr"),
    "ASSURANCE_TRACEOPT_ROOT": ("RoboTraceOpt", "research/RoboTraceOpt"),
    "ASSURANCE_WORKBENCH_SHIM": (".wbshim", "research/.wbshim"),
}


def resolve(variable: str, *, marker: str | None = None) -> pathlib.Path | None:
    """Resolve one target, or return None when this machine simply does not have it.

    ``marker`` is a relative path that must exist for the candidate to count, so a
    directory that merely happens to share the expected name is not mistaken for
    the system under test.
    """
    explicit = os.environ.get(variable)
    if explicit:
        candidate = pathlib.Path(explicit).expanduser()
        return candidate if _usable(candidate, marker) else None

    for relative in KNOWN_TARGETS.get(variable, ()):
        candidate = CHECKOUT_PARENT / relative
        if _usable(candidate, marker):
            return candidate
    return None


def _usable(candidate: pathlib.Path, marker: str | None) -> bool:
    if not candidate.exists():
        return False
    return marker is None or (candidate / marker).exists()


def workbench_root() -> pathlib.Path | None:
    """The workbench checkout, identified by its CLI rather than by its name."""
    return resolve("ASSURANCE_WORKBENCH_ROOT", marker="tools/scripts/sim_cli.py")


def workbench_shim() -> str | None:
    """The optional import shim for a workbench checkout that is not installed."""
    found = resolve("ASSURANCE_WORKBENCH_SHIM", marker="sitecustomize.py")
    return str(found) if found else None


def traceopt_root() -> pathlib.Path | None:
    return resolve("ASSURANCE_TRACEOPT_ROOT", marker="diagnosis")


def traceopt_fixture(name: str = "tests/fixtures/association/w1_real_summary.json") -> pathlib.Path | None:
    root = traceopt_root()
    if root is None:
        return None
    candidate = root / name
    return candidate if candidate.is_file() else None


def describe_targets() -> dict[str, str]:
    """What this machine resolved, for `assurance doctor` and for the README."""
    found = {
        "workbench": workbench_root(),
        "workbench_shim": workbench_shim(),
        "RoboTraceOpt": traceopt_root(),
    }
    return {
        name: (str(value) if value else "not found (set the matching ASSURANCE_* variable)")
        for name, value in found.items()
    }
