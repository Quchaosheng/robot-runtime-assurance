"""Read a latency-diagnosis tool's published artifacts as acceptance evidence.

RoboTraceOpt stays an independent project. This module never imports it: it reads
the two file contracts that project publishes -

  ``association-evidence-summary/v1``  a JSON summary (counts + sha256 digests)
  normalized event NDJSON               one ``NormalizedEvent``-shaped record per line

- and turns them into a claim this harness can judge.

Two rules keep this honest:

* **A digest that does not match its file is a refutation, not a warning.** The
  whole point of shipping a sha256 next to a count is that a reviewer can re-derive
  it. If the artifact moved, the evidence is not evidence.
* **A missing latency artifact never becomes a pass.** It is ``insufficient_evidence``
  with the missing path named, because "we did not look" and "it was fine" must not
  render the same way.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
from typing import Any

SUMMARY_SCHEMA = "association-evidence-summary/v1"

# The digests a summary may carry, mapped to the artifact each one is computed over.
# A summary that names a digest this reader cannot re-derive is refused rather than
# partially trusted.
DIGEST_FIELDS: dict[str, str] = {
    "ctf_sha256": "raw CTF trace",
    "runtime_normalized_sha256": "runtime normalized events",
    "ros2_normalized_sha256": "system normalized events",
    "association_report_sha256": "association report",
    "process_manifest_sha256": "process manifest",
    "clock_calibration_sha256": "clock calibration",
}


class TraceArtifactError(ValueError):
    """The latency artifact is unreadable or internally inconsistent."""


def load_summary(path: pathlib.Path) -> dict[str, Any]:
    """Read and structurally check one published summary."""

    path = pathlib.Path(path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise TraceArtifactError(f"cannot read {path}: {error}") from error
    except json.JSONDecodeError as error:
        raise TraceArtifactError(f"{path} is not valid JSON: {error}") from error
    if not isinstance(payload, dict):
        raise TraceArtifactError(f"{path} must be a JSON object")
    if payload.get("schema_version") != SUMMARY_SCHEMA:
        raise TraceArtifactError(
            f"{path} declares {payload.get('schema_version')!r}; this reader implements {SUMMARY_SCHEMA!r}"
        )
    for field in ("runtime_event_count", "ros2_event_count", "accepted_count"):
        value = payload.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise TraceArtifactError(f"{path}: {field} must be a non-negative integer")
    # Accepted events are a subset of the system events. A count above the total is
    # a producer bug, and the harness reports it instead of believing the number.
    if payload["accepted_count"] > payload["ros2_event_count"]:
        raise TraceArtifactError(
            f"{path}: accepted_count {payload['accepted_count']} exceeds ros2_event_count "
            f"{payload['ros2_event_count']}"
        )
    rate = payload.get("accepted_rate")
    if rate is not None:
        if not isinstance(rate, (int, float)) or isinstance(rate, bool) or not 0.0 <= rate <= 1.0:
            raise TraceArtifactError(f"{path}: accepted_rate must be within [0, 1]")
    return payload


def count_events(path: pathlib.Path) -> int:
    """Count non-blank NDJSON records; malformed lines raise rather than being skipped."""

    path = pathlib.Path(path)
    if not path.is_file():
        raise TraceArtifactError(f"no such artifact: {path}")
    count = 0
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            raise TraceArtifactError(f"{path}:{number} is not valid JSON: {error}") from error
        if not isinstance(record, dict):
            raise TraceArtifactError(f"{path}:{number} is not a JSON object")
        count += 1
    return count


def digest_of(path: pathlib.Path) -> str | None:
    path = pathlib.Path(path)
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_digests(summary: dict[str, Any], roots: dict[str, pathlib.Path]) -> list[dict[str, Any]]:
    """Re-derive each declared digest that a file is available for.

    ``roots`` maps a digest field to the artifact it covers. A field with no root is
    reported as ``not_checked`` - never as ``ok``.
    """
    results: list[dict[str, Any]] = []
    for field in sorted(DIGEST_FIELDS):
        declared = summary.get(field)
        if declared is None:
            continue
        root = roots.get(field)
        if root is None:
            results.append({"field": field, "status": "not_checked",
                            "detail": f"no local artifact supplied for {DIGEST_FIELDS[field]}"})
            continue
        actual = digest_of(root)
        if actual is None:
            results.append({"field": field, "status": "missing", "path": str(root)})
            continue
        results.append({
            "field": field,
            "status": "match" if actual == declared else "mismatch",
            "path": str(root),
            "declared": declared,
            "actual": actual,
        })
    return results
