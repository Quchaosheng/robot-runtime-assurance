"""The registry, the schemas, and the CLI contract."""

from __future__ import annotations

import json
import pathlib

import pytest

from assurance import registry as reg
from assurance.cli import main

SCHEMA_DIR = pathlib.Path(__file__).parent.parent / "assurance" / "schemas"


def test_every_scenario_matches_its_schema():
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((SCHEMA_DIR / "scenario.schema.json").read_text())
    for path in reg.registry().values():
        jsonschema.validate(json.loads(path.read_text()), schema)


def test_unregistered_scenario_is_refused():
    """I1: if it is not registered, it does not run."""
    with pytest.raises(reg.ScenarioError):
        reg.load("nominal/does-not-exist")


def test_scenario_id_must_match_its_registry_name():
    with pytest.raises(reg.ScenarioError):
        reg.load("nominal/normal-task.json/../failures/ack-late")


def test_a_scenario_without_claims_is_refused(tmp_path):
    path = tmp_path / "empty.json"
    path.write_text(json.dumps({
        "scenario_id": "x/y", "seed": 1, "transport": "mock",
        "tasks": [{"task_id": "T", "epoch": 1, "resource": "r", "deadline_ms": 10}],
        "claims": []}))
    with pytest.raises(reg.ScenarioError):
        reg.load(str(path))


def test_cli_doctor_reports_unavailable_backends_instead_of_hiding_them(capsys):
    assert main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "vcan0" in out and ("available" in out or "UNAVAILABLE" in out)


def test_cli_list_shows_every_scenario(capsys):
    assert main(["list"]) == 0
    out = capsys.readouterr().out
    for name in reg.registry():
        assert name in out


def test_cli_refuses_an_unregistered_scenario(capsys):
    with pytest.raises(SystemExit):
        main(["run", "not/registered"])


def test_cli_run_then_verify_then_junit(tmp_path, capsys):
    out = tmp_path / "nominal-normal-task--cli"
    assert main(["run", "nominal/normal-task", "--out", str(out)]) == 0
    capsys.readouterr()
    assert main(["verify", str(out)]) == 0
    capsys.readouterr()
    junit = tmp_path / "junit.xml"
    assert main(["junit", str(out), "--out", str(junit)]) == 0
    body = junit.read_text()
    assert "<testsuite" in body and "I1-scenario-registered" in body


def test_selftest_catches_a_stale_ack(capsys):
    assert main(["selftest"]) == 0
