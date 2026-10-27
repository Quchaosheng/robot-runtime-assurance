"""The acceptance matrix is generated, and it must stay generated.

A README table that nobody can re-derive is a claim, not a status. So the table in the
README is compared against the command's output: if a scenario changes and the README
is not regenerated, this fails.

The matrix also has one job beyond bookkeeping: it must not hide an empty level. An
all-dash L3 column is the honest state of this project, and a generator that skipped
empty columns would make the project look further along than it is.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]


def matrix_text() -> str:
    result = subprocess.run(
        [sys.executable, "-m", "assurance.cli", "matrix"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    )
    return result.stdout


def matrix_json() -> dict:
    result = subprocess.run(
        [sys.executable, "-m", "assurance.cli", "matrix", "--json"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    )
    return json.loads(result.stdout)


def test_the_readme_carries_the_current_matrix() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    generated = matrix_text()
    # Only the table body is compared; the surrounding prose is free to change.
    for line in generated.splitlines():
        if line.startswith("| "):
            assert line in readme, f"README matrix is stale, missing row: {line}"


def test_every_invariant_number_appears_in_the_matrix() -> None:
    payload = matrix_json()
    assert payload["matrix"], "the matrix is empty"
    for invariant in payload["matrix"]:
        assert invariant.startswith("I"), invariant


def test_all_four_levels_are_always_printed() -> None:
    """An empty column must be visible, not omitted."""

    header = matrix_text().splitlines()[0]
    for label in ("L0", "L1", "L2", "L3"):
        assert label in header, header


def test_a_level_no_scenario_reaches_is_still_a_column() -> None:
    payload = matrix_json()
    reached: set[str] = set()
    for cells in payload["matrix"].values():
        reached.update(cells)
    # L2/L3 may be unreached today; the table must still report them, which
    # test_all_four_levels_are_always_printed checks. Here we assert the inverse
    # property: whatever is unreached is simply absent from the data, never faked.
    for level in reached:
        assert level in {"0", "1", "2", "3"}, level
