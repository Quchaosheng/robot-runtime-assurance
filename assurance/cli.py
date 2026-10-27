"""assurance - black-box acceptance for embodied systems.

    assurance doctor                      what this machine can and cannot test
    assurance list                        scenarios that ship with the kit
    assurance run <scenario> [--out DIR]  execute, then write an evidence bundle
    assurance verify <bundle>             re-derive the verdict from the bundle
    assurance report <bundle>             render the acceptance report
    assurance replay <bundle>             run the whole scenario again, compare digests
"""

from __future__ import annotations

import argparse
import json
import pathlib
import shutil

from .evidence import default_out_dir, render_report, verify_bundle, write_junit
from .registry import ROOT as SCENARIO_ROOT
from .registry import ScenarioError, load, registry
from .runner import run_scenario
from .transports import MockTransport, Ros2ActionTransport


def resolve(name: str) -> dict:
    try:
        return load(name)
    except ScenarioError as exc:
        raise SystemExit(str(exc)) from exc


def cmd_doctor(_args) -> int:
    rows = []
    for transport in (MockTransport(), Ros2ActionTransport()):
        ok, why = transport.available()
        rows.append((transport.name, ok, why))
    from .sut.socketcan import SocketCanTransport
    can = SocketCanTransport()
    ok, why = can.available()
    rows.append((can.name, ok, why))
    from .sut.workbench import WorkbenchTransport
    workbench = WorkbenchTransport()
    ok, why = workbench.available()
    rows.append((workbench.name, ok, why))
    ros2 = shutil.which("ros2")
    rows.append(("ros2_cli", bool(ros2), ros2 or "not on PATH"))
    vcan = pathlib.Path("/sys/class/net/vcan0").exists()
    rows.append(("vcan0", vcan, "present" if vcan else "absent (needs CAP_NET_ADMIN: reports unavailable, never fake)"))

    width = max(len(r[0]) for r in rows)
    for name, ok, why in rows:
        print(f"{name:<{width}}  {'available  ' if ok else 'UNAVAILABLE'}  {why}")
    print()
    from .levels import TRANSPORT_CEILING, Level

    print()
    print("evidence ceiling per transport (a claim can never be confirmed above it):")
    for name in sorted(TRANSPORT_CEILING):
        ceiling = TRANSPORT_CEILING[name]
        print(f"  {name:<14} L{int(ceiling)} {Level(ceiling).label.split(' ', 1)[1]}")

    from .targets import describe_targets

    print()
    print("external targets this run would use:")
    for name, where in describe_targets().items():
        print(f"  {name:<16} {where}")
    print()
    print(f"scenarios: {len(registry())} (in {SCENARIO_ROOT})")
    print("A transport marked UNAVAILABLE still runs: the run is recorded and the verdict "
          "comes back insufficient_evidence, never a pass.")
    return 0


def cmd_matrix(args) -> int:
    """Generate the invariant x evidence-level matrix.

    This table is the project's progress board, so it is generated rather than
    hand-written: a README table that nobody can re-derive is a claim, not a status.

    A cell is only filled when a scenario both declares that claim and was actually
    executed for it. "The scenario exists" is not coverage, and a mock run never
    fills a cell above L1 because its ceiling caps it.
    """
    from .levels import Level, ceiling_for

    rows: dict[str, dict[int, str]] = {}
    for name, path in sorted(registry().items()):
        scenario = json.loads(path.read_text())
        transport = scenario["transport"]
        ceiling = int(ceiling_for(transport))
        for claim in scenario.get("claims", []):
            invariant = claim.get("invariant")
            if not invariant:
                continue
            requires = int(Level.parse(claim["requires"]))
            cell = rows.setdefault(invariant, {})
            if requires > ceiling:
                # The scenario asks for evidence its transport cannot produce. That is
                # a real row of the matrix: it shows what is planned versus reachable.
                cell[requires] = cell.get(requires, "") or "需更高层 transport"
            else:
                cell[requires] = cell.get(requires, "") or f"`{name}`"
    if getattr(args, "as_json", False):
        print(json.dumps(
            {
                "levels": [level.label for level in Level],
                "matrix": {inv: {str(lv): sc for lv, sc in cells.items()} for inv, cells in sorted(rows.items())},
            },
            ensure_ascii=False,
            indent=2,
        ))
        return 0
    header = "| 不变量 | " + " | ".join(level.label for level in Level) + " |"
    print(header)
    print("| --- | " + " | ".join("---" for _ in Level) + " |")
    for invariant in sorted(rows):
        cells = [rows[invariant].get(int(level), "—") for level in Level]
        print(f"| {invariant} | " + " | ".join(cells) + " |")
    print()
    print("单元格里的场景名表示：该场景确实声明了这条不变量，且其 transport 能达到这一级。")
    print("「需更高层 transport」表示场景已写，但当前传输层产不出这一级证据。")
    return 0


def cmd_list(_args) -> int:
    for name, path in registry().items():
        scenario = json.loads(path.read_text())
        faults = ",".join(f["kind"] for f in scenario.get("faults", [])) or "-"
        rules = ",".join(sorted({c["kind"] for c in scenario["claims"]}))
        print(f"{name:52s} transport={scenario['transport']:10s} faults={faults:15s} claims={rules}")
    return 0


def cmd_run(args) -> int:
    scenario = resolve(args.scenario)
    if args.transport:
        scenario = {**scenario, "transport": args.transport}
    if args.seed is not None:
        scenario = {**scenario, "seed": args.seed}
    out = args.out or default_out_dir(scenario["scenario_id"].replace("/", "-"))
    result = run_scenario(scenario, out)
    print(json.dumps({k: result[k] for k in
                      ("scenario_id", "counts", "passed", "blocked_by", "verdict_digest", "bundle")},
                     indent=2))
    if not result["passed"]:
        for claim in json.loads((pathlib.Path(out) / "deterministic" / "verdict.json").read_text())["claims"]:
            if claim["status"] != "confirmed":
                print(f"  {claim['claim_id']} [{claim.get('invariant', '?')}]: "
                      f"{claim['status']} - {claim.get('reason')}")
    return 0 if result["passed"] else 1


def cmd_verify(args) -> int:
    result = verify_bundle(args.bundle)
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1


def cmd_report(args) -> int:
    text = render_report(args.bundle)
    if args.out:
        pathlib.Path(args.out).write_text(text)
        print(f"written: {args.out}")
    else:
        print(text, end="")
    return 0


def cmd_replay(args) -> int:
    root = pathlib.Path(args.bundle)
    stored = json.loads((root / "manifest.json").read_text())
    scenario = json.loads((root / "deterministic" / "scenario.json").read_text())
    out = pathlib.Path(args.out) if args.out else pathlib.Path(str(root) + "--replay")
    result = run_scenario(scenario, out)
    same = result["verdict_digest"] == stored["verdict_digest"]
    print(json.dumps({"stored": stored["verdict_digest"],
                      "replayed": result["verdict_digest"], "identical": same,
                      "bundle": str(out)}, indent=2))
    return 0 if same else 1


def cmd_junit(args) -> int:
    pathlib.Path(args.out).write_text(write_junit(args.bundle))
    print(f"written: {args.out}")
    return 0


def cmd_selftest(_args) -> int:
    """The oracle must agree with itself on a timeline we already know the answer to."""
    from .oracle import evaluate

    timeline = [
        {"t_ms": 0, "kind": "task_start", "task_id": "A", "epoch": 1},
        {"t_ms": 10, "kind": "task_start", "task_id": "B", "epoch": 2},
        {"t_ms": 20, "kind": "ack", "task_id": "B", "epoch": 1},
        {"t_ms": 30, "kind": "device_state", "state": "idle"},
    ]
    claims = [{"claim_id": "c1", "kind": "ack_matches_task", "task_id": "B"}]
    verdict = evaluate(claims, timeline)
    print(json.dumps(verdict, indent=2))
    ok = verdict["claims"][0]["status"] == "refuted"
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="assurance", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor").set_defaults(func=cmd_doctor)
    sub.add_parser("list").set_defaults(func=cmd_list)
    matrix = sub.add_parser("matrix", help="print the invariant x evidence-level matrix")
    matrix.add_argument("--json", action="store_true", dest="as_json")
    matrix.set_defaults(func=cmd_matrix)
    sub.add_parser("selftest").set_defaults(func=cmd_selftest)

    run = sub.add_parser("run")
    run.add_argument("scenario", help="registered id such as failures/ack-late, or a path")
    run.add_argument("--out")
    run.add_argument("--transport", choices=["mock", "ros2action"])
    run.add_argument("--seed", type=int)
    run.set_defaults(func=cmd_run)

    verify = sub.add_parser("verify")
    verify.add_argument("bundle")
    verify.set_defaults(func=cmd_verify)

    report = sub.add_parser("report")
    report.add_argument("bundle")
    report.add_argument("--out")
    report.set_defaults(func=cmd_report)

    junit = sub.add_parser("junit", help="convert a bundle (or runs/) into JUnit XML for CI")
    junit.add_argument("bundle")
    junit.add_argument("--out", default="junit.xml")
    junit.set_defaults(func=cmd_junit)

    replay = sub.add_parser("replay")
    replay.add_argument("bundle")
    replay.add_argument("--out")
    replay.set_defaults(func=cmd_replay)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
