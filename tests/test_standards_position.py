"""The standards-position page must not quietly turn into an overclaim.

This repository says "assurance", "evidence" and "invariant", which is one careless
sentence away from "certified". The page in docs/ is the sentence boundary, so it is
tested like one: the claims it forbids must stay out of the sayable column, and every
standards name it promises to address must be there.
"""

from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
PAGE = ROOT / "docs/standards-position.md"

FORBIDDEN_IN_SAYABLE = [
    "safety function is achieved",
    "cannot hurt anyone",
    "evidence is complete",
    "has been audited",
    "ISO 13849-2 validation performed",
    "the system is certified",
]


def test_the_page_exists_and_names_all_four_standards() -> None:
    text = PAGE.read_text(encoding="utf-8")
    for standard in ("ISO 12100", "ISO 13849-1", "ISO 13849-2", "ISO 13482"):
        assert standard in text, standard


def test_the_sayable_column_contains_no_forbidden_claim() -> None:
    text = PAGE.read_text(encoding="utf-8")
    table = text.split("## Sayable / not sayable", 1)[1].split("##", 1)[0]
    for line in table.splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) != 2 or cells[0] == "Sayable" or set(cells[0]) == {"-"}:
            continue
        sayable = cells[0].lower()
        for phrase in FORBIDDEN_IN_SAYABLE:
            assert phrase.lower() not in sayable, f"overclaim in the sayable column: {cells[0]}"


def test_the_readme_keeps_the_disclaimer_and_a_pointer_to_the_page() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "not a functional safety certification implementation" in readme
    assert "docs/standards-position.md" in readme


def test_every_relative_link_in_the_page_resolves() -> None:
    text = PAGE.read_text(encoding="utf-8")
    for target in re.findall(r"\]\(([^)#:]+\.md)\)", text):
        assert (PAGE.parent / target).resolve().is_file(), target


def test_the_page_states_that_invariants_are_not_safety_functions() -> None:
    """This is the single most likely misreading of the whole project."""
    text = PAGE.read_text(encoding="utf-8").lower()
    assert "not safety requirements derived from hazards" in text or "not a safety function" in text
