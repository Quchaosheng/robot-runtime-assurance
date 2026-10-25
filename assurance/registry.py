"""Scenario registry. I1 starts here: an unregistered scenario cannot be executed."""

from __future__ import annotations

import json
import pathlib

HERE = pathlib.Path(__file__).parent
ROOT = HERE / "scenarios"


class ScenarioError(ValueError):
    pass


def registry() -> dict[str, pathlib.Path]:
    """Every shipped scenario, keyed by its path-relative id (`group/name`)."""
    found: dict[str, pathlib.Path] = {}
    for path in sorted(ROOT.rglob("*.json")):
        found[path.relative_to(ROOT).with_suffix("").as_posix()] = path
    return found


def load(name: str) -> dict:
    """Load by id or by path. Anything unresolved is refused, not guessed."""
    candidate = pathlib.Path(name)
    if candidate.exists() and candidate.suffix == ".json":
        scenario = json.loads(candidate.read_text())
        source = candidate.as_posix()
    else:
        path = registry().get(pathlib.Path(name).as_posix())
        if path is None:
            raise ScenarioError(f"unregistered scenario: {name}\nregistered: "
                                + ", ".join(registry()))
        scenario = json.loads(path.read_text())
        source = path.as_posix()

    required = ("scenario_id", "seed", "transport", "tasks", "claims")
    missing = [key for key in required if key not in scenario]
    if missing:
        raise ScenarioError(f"{source}: missing {missing}")
    if scenario["scenario_id"] != pathlib.Path(name).as_posix() and not candidate.exists():
        raise ScenarioError(f"{source}: scenario_id '{scenario['scenario_id']}' does not "
                            f"match its registry id '{name}'")
    if not scenario["tasks"]:
        raise ScenarioError(f"{source}: no tasks")
    if not scenario["claims"]:
        raise ScenarioError(f"{source}: no claims - a scenario that asserts nothing cannot pass")
    scenario.setdefault("expect_verdict", "pass")
    if scenario["expect_verdict"] not in ("pass", "refute", "insufficient"):
        raise ScenarioError(f"{source}: expect_verdict must be pass|refute|insufficient")
    return scenario
