"""Target resolution: a location is found by evidence, never by a lucky name.

A harness that silently substitutes a path it found by accident produces evidence
for a system the reviewer did not choose. These tests pin the three rules that
prevent that:

* a candidate only counts when its marker file exists, so a directory that merely
  shares the expected name is refused;
* an explicit environment variable wins, and an unusable explicit value yields
  "not found" rather than falling back to a default the user did not ask for;
* the shipped scenarios contain no absolute path, so the repository works for
  somebody who clones it somewhere else.
"""

from __future__ import annotations

import json
import pathlib

from assurance import targets


def test_a_candidate_without_its_marker_is_refused(tmp_path: pathlib.Path, monkeypatch) -> None:
    fake = tmp_path / "wmhr"
    fake.mkdir()
    monkeypatch.setattr(targets, "CHECKOUT_PARENT", tmp_path)
    monkeypatch.delenv("ASSURANCE_WORKBENCH_ROOT", raising=False)
    assert targets.workbench_root() is None


def test_a_candidate_with_its_marker_is_accepted(tmp_path: pathlib.Path, monkeypatch) -> None:
    fake = tmp_path / "wmhr"
    (fake / "tools/scripts").mkdir(parents=True)
    (fake / "tools/scripts/sim_cli.py").write_text("# stand-in\n")
    monkeypatch.setattr(targets, "CHECKOUT_PARENT", tmp_path)
    monkeypatch.delenv("ASSURANCE_WORKBENCH_ROOT", raising=False)
    assert targets.workbench_root() == fake


def test_an_explicit_variable_wins(tmp_path: pathlib.Path, monkeypatch) -> None:
    chosen = tmp_path / "somewhere"
    (chosen / "tools/scripts").mkdir(parents=True)
    (chosen / "tools/scripts/sim_cli.py").write_text("# stand-in\n")
    monkeypatch.setenv("ASSURANCE_WORKBENCH_ROOT", str(chosen))
    assert targets.workbench_root() == chosen


def test_an_unusable_explicit_variable_does_not_fall_back(tmp_path: pathlib.Path, monkeypatch) -> None:
    """Falling back would run a system the user did not name. Refusing is the point."""
    monkeypatch.setenv("ASSURANCE_WORKBENCH_ROOT", str(tmp_path / "no-such-checkout"))
    assert targets.workbench_root() is None


def test_no_shipped_scenario_carries_an_absolute_path() -> None:
    root = pathlib.Path(__file__).resolve().parents[1] / "assurance/scenarios"
    offenders = []
    for path in root.rglob("*.json"):
        text = path.read_text(encoding="utf-8")
        payload = json.loads(text)
        artifact = payload.get("trace_artifact") or {}
        for value in [artifact.get("summary"), *(artifact.get("digests") or {}).values()]:
            if isinstance(value, str) and value.startswith("/"):
                offenders.append(f"{path.name}: {value}")
    assert not offenders, offenders


def test_no_source_file_hardcodes_a_build_path() -> None:
    """The harness must not know where this author keeps their checkouts."""
    root = pathlib.Path(__file__).resolve().parents[1]
    offenders = []
    for path in list((root / "assurance").rglob("*.py")) + list((root / "ci").rglob("*.py")):
        if "/data/builds" in path.read_text(encoding="utf-8"):
            offenders.append(str(path.relative_to(root)))
    assert not offenders, offenders


def test_describe_targets_names_the_variable_to_set(tmp_path: pathlib.Path, monkeypatch) -> None:
    monkeypatch.setattr(targets, "CHECKOUT_PARENT", tmp_path)
    for variable in targets.KNOWN_TARGETS:
        monkeypatch.delenv(variable, raising=False)
    described = targets.describe_targets()
    assert all("ASSURANCE_" in value for value in described.values())
