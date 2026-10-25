"""Evidence bundle: what a reviewer needs to check the verdict without re-running it.

Split on purpose, because the two halves have different guarantees:

  deterministic/  scenario, verdict, coverage, timeline - re-running the oracle on
                  these files must reproduce the exact same verdict digest.
  observed/       raw event order and SUT stdio: comparable, never replayed.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import shutil

BUNDLE_VERSION = "assurance-bundle/1"


def _dump(path: pathlib.Path, obj) -> None:
    path.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n")


def _sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_bundle(scenario: dict, timeline: list[dict], verdict: dict,
                 coverage: list[dict], out_dir: str | pathlib.Path) -> dict:
    out = pathlib.Path(out_dir)
    if out.exists():
        # Replacing a bundle is safe; replacing anything else is a bug, not a rerun.
        if not (out / "manifest.json").exists() or not (out / "deterministic").is_dir():
            raise FileExistsError(f"refusing to replace {out}: not an evidence bundle")
        shutil.rmtree(out)          # a rerun replaces its own bundle, never merges into it
    (out / "deterministic").mkdir(parents=True)
    (out / "observed").mkdir()

    _dump(out / "deterministic" / "scenario.json", scenario)
    _dump(out / "deterministic" / "timeline.json", timeline)
    _dump(out / "deterministic" / "verdict.json", verdict)
    _dump(out / "deterministic" / "coverage.json", coverage)
    (out / "observed" / "events.jsonl").write_text(
        "".join(json.dumps(e, sort_keys=True) + "\n" for e in timeline))

    manifest = {
        "bundle_version": BUNDLE_VERSION,
        "scenario_id": scenario["scenario_id"],
        "seed": scenario["seed"],
        "transport": scenario["transport"],
        "verdict_digest": verdict["digest"],
        "passed": verdict["passed"],
        "blocked_by": verdict.get("blocked_by"),
        "expected_verdict": verdict.get("expected_verdict"),
        "observed_verdict": verdict.get("observed_verdict"),
        "sample_matched": verdict.get("sample_matched"),
        "unexercised": verdict.get("unexercised", []),
        "counts": verdict["counts"],
    }
    _dump(out / "manifest.json", manifest)

    sums = []
    for path in sorted(out.rglob("*")):
        if path.is_file() and path.name != "checksums.sha256":
            sums.append(f"{_sha256(path)}  {path.relative_to(out)}")
    (out / "checksums.sha256").write_text("\n".join(sums) + "\n")

    # The marker goes last: a half-written bundle must never look like a finished run.
    (out / "COMPLETE").write_text(verdict["digest"] + "\n")
    return {"bundle": str(out), **manifest}


def verify_bundle(path: str | pathlib.Path) -> dict:
    """Re-derive the verdict from the deterministic half and check the checksums."""
    root = pathlib.Path(path)
    if not (root / "COMPLETE").exists():
        return {"ok": False, "reason": "incomplete_bundle"}
    bad = []
    for line in (root / "checksums.sha256").read_text().splitlines():
        if not line.strip():
            continue
        digest, name = line.split("  ", 1)
        target = root / name
        if not target.exists() or _sha256(target) != digest:
            bad.append(name)
    from .oracle import evaluate

    scenario = json.loads((root / "deterministic" / "scenario.json").read_text())
    timeline = json.loads((root / "deterministic" / "timeline.json").read_text())
    stored = json.loads((root / "deterministic" / "verdict.json").read_text())
    replayed = evaluate(scenario["claims"], timeline)
    return {
        "ok": not bad and replayed["digest"] == stored["digest"],
        "checksums_bad": bad,
        "replayed_digest": replayed["digest"],
        "stored_digest": stored["digest"],
    }


def render_report(bundle: str | pathlib.Path) -> str:
    root = pathlib.Path(bundle)
    manifest = json.loads((root / "manifest.json").read_text())
    verdict = json.loads((root / "deterministic" / "verdict.json").read_text())
    coverage = json.loads((root / "deterministic" / "coverage.json").read_text())
    blocked = manifest.get("blocked_by")
    if manifest.get("expected_verdict") == "refute":
        outcome = "sample behaved as designed (refuted)" if manifest.get("observed_verdict") == "refute" \
            else "SAMPLE DID NOT FAIL AS DESIGNED"
    elif manifest.get("expected_verdict") == "insufficient":
        outcome = ("sample produced insufficient evidence, as designed"
                   if manifest.get("observed_verdict") == "insufficient"
                   else "SAMPLE PROVED MORE THAN IT COULD")
    else:
        outcome = "accepted" if manifest["passed"] else f"rejected ({blocked})"
    if not manifest["passed"] and not blocked:
        outcome = "run did not match the expected verdict"
    headline = f"- verdict: **{'PASS' if manifest['passed'] else 'FAIL'}** - {outcome}"
    lines = [
        f"# Acceptance report - {manifest['scenario_id']}",
        "",
        f"- transport: `{manifest['transport']}`   seed: `{manifest['seed']}`",
        headline,
        f"- claims: {verdict['counts']['confirmed']} confirmed / "
        f"{verdict['counts']['refuted']} refuted / "
        f"{verdict['counts']['insufficient_evidence']} insufficient",
        f"- digest: `{manifest['verdict_digest']}`",
        "",
        "## Claims",
        "",
        "| claim | status | why |",
        "| --- | --- | --- |",
    ]
    for row in verdict["claims"]:
        detail = row.get("reason", "")
        if "observed" in row:
            detail += f" (observed `{row['observed']}`, expected `{row.get('expected')}`)"
        lines.append(f"| `{row['claim_id']}` | {row['status']} | {detail} |")
    if coverage:
        lines += ["", "## Faults exercised", "", "| fault | task | exercised |", "| --- | --- | --- |"]
        for row in coverage:
            lines.append(f"| `{row['fault']}` | `{row['task_id']}` | {'yes' if row['exercised'] else 'NO'} |")
    missing = [c for c in verdict["claims"] if c["status"] == "insufficient_evidence"]
    if missing:
        lines += ["", "## Not proven", "",
                  "These claims were neither confirmed nor refuted - the run did not "
                  "produce the evidence they need:", ""]
        lines += [f"- `{c['claim_id']}`: missing {c.get('missing', 'unspecified')}" for c in missing]
    return "\n".join(lines) + "\n"


def write_junit(bundle: str | pathlib.Path) -> str:
    """One testcase per claim: CI can read it, and a skipped test is never a pass."""
    import xml.etree.ElementTree as ET

    root = pathlib.Path(bundle)
    manifest = json.loads((root / "manifest.json").read_text())
    verdict = json.loads((root / "deterministic" / "verdict.json").read_text())
    suite = ET.Element("testsuite", {
        "name": manifest["scenario_id"], "tests": str(len(verdict["claims"])),
        "failures": str(verdict["counts"]["refuted"]),
        "skipped": str(verdict["counts"]["insufficient_evidence"]),
    })
    for claim in verdict["claims"]:
        case = ET.SubElement(suite, "testcase", {
            "name": claim["claim_id"],
            "classname": claim.get("invariant") or claim["kind"],
        })
        if claim["status"] == "refuted":
            ET.SubElement(case, "failure", {"message": claim.get("reason", "refuted")})
        elif claim["status"] == "insufficient_evidence":
            ET.SubElement(case, "skipped", {"message": claim.get("reason", "insufficient")})
    return ET.tostring(suite, encoding="unicode")


def default_out_dir(scenario_id: str, runs_root: str = "runs") -> str:
    stamp = os.environ.get("ASSURANCE_RUN_ID") or "local"
    return str(pathlib.Path(runs_root) / f"{scenario_id}--{stamp}")
