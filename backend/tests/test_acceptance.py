"""The QA page (backoffice.acceptance): the 50 cases and the checklist, with honest verdicts.

Every ``pass`` must be proven by a test that exists; every reference must resolve.
"""

from __future__ import annotations

import ast
import json
import re
from functools import cache
from pathlib import Path

import pytest

from backoffice.acceptance import CASES, CHECKS, EXTRA, SECTIONS, acceptance
from backoffice.service import BackOfficeService

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"


@cache
def _python_tests(path: Path) -> frozenset[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return frozenset(n.name for n in ast.walk(tree)
                     if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test_"))


def _resolves(ref: str) -> bool:
    """``tests/x.py::test_y`` (a test function), ``mobile/...test.ts › it name``, or a file path."""
    if "::" in ref:
        file, name = ref.split("::", 1)
        path = BACKEND / file
        return path.is_file() and name in _python_tests(path)
    if " › " in ref:
        file, name = ref.split(" › ", 1)
        path = ROOT / file
        return path.is_file() and (f'it("{name}"' in path.read_text(encoding="utf-8")
                                   or f"it('{name}'" in path.read_text(encoding="utf-8"))
    return (ROOT / ref).exists()


def _is_test(ref: str) -> bool:
    return ("::" in ref and ref.split("::", 1)[1].startswith("test_")) or " › " in ref or ref.endswith("e2e.py")


def test_the_document_is_complete() -> None:
    assert [s[0] for s in SECTIONS] == list("ABCDEFGHIJKLMNOPQRSTU")
    sizes = {letter: len(checks) for letter, _, checks in SECTIONS}
    assert sizes == {"A": 11, "B": 9, "C": 10, "D": 15, "E": 11, "F": 11, "G": 6, "H": 10, "I": 13, "J": 7,
                     "K": 12, "L": 8, "M": 11, "N": 10, "O": 8, "P": 7, "Q": 8, "R": 8, "S": 8, "T": 10, "U": 11}
    assert sum(sizes.values()) == 204
    assert [k.number for k in CASES] == list(range(1, 51))
    assert len({c.id for _, _, cs in SECTIONS for c in cs} | {c.id for c in EXTRA}) == len(CHECKS)


@pytest.mark.parametrize("check", list(CHECKS.values()), ids=lambda c: c.id)
def test_every_verdict_is_honest(check) -> None:
    assert check.status in ("pass", "partial", "missing")
    assert check.text.strip() and check.where.strip()
    for ref in check.evidence:
        assert _resolves(ref), f"{check.id}: evidence does not exist: {ref}"
    if check.status == "pass":
        assert any(_is_test(ref) for ref in check.evidence), f"{check.id} passes without a test that proves it"
    else:
        assert check.note.strip(), f"{check.id} is not passing and must say why"


@pytest.mark.parametrize("case", CASES, ids=lambda k: f"case{k.number:02d}")
def test_every_case_points_at_real_checks(case) -> None:
    assert case.title and case.quote and case.pass_test and case.handles and case.decisive
    for _, ref in case.handles:
        assert ref in CHECKS, ref
    for ref in case.decisive:
        assert ref in CHECKS, ref


def test_a_case_passes_only_when_its_pass_test_and_everything_it_handles_pass() -> None:
    data = acceptance()
    for row in data["cases"]:
        handled = [h["status"] for h in row["handles"]]
        test = [c["status"] for c in row["passTest"]["checks"]]
        if row["verdict"] == "pass":
            assert all(s == "pass" for s in handled + test)
        elif row["verdict"] == "missing":
            assert all(s == "missing" for s in test)
        assert row["covered"] == handled.count("pass")
        assert all(g["status"] != "pass" for g in row["gaps"])


def test_totals_add_up() -> None:
    s = acceptance()["summary"]
    assert s["checklist"]["total"] == 204
    assert s["cases"]["total"] == 50
    for key in ("cases", "passTests", "checklist", "sector"):
        c = s[key]
        assert c["pass"] + c["partial"] + c["missing"] == c["total"]
    assert s["percent"] == s["checklist"]["pass"] * 100 // 204


def test_the_endpoint_serves_it_and_it_is_json() -> None:
    svc = BackOfficeService.demo()
    status, body = svc.dispatch("GET", "/api/internal/acceptance", None)
    assert status == 200
    assert body == json.loads(json.dumps(acceptance()))


def test_notes_are_one_short_line() -> None:
    """The team reads these on the QA page: one line each, no line breaks."""
    for c in CHECKS.values():
        assert "\n" not in c.note and len(c.note) <= 200, c.id


def test_newly_fixed_behaviour_is_cited() -> None:
    """The fixes this QA pass made are proven where the checklist says they pass."""
    cited = " ".join(ref for c in CHECKS.values() for ref in c.evidence)
    for file in ("test_acceptance_engine_fixes.py", "test_acceptance_honesty_fixes.py",
                 "test_acceptance_settlements.py", "test_acceptance_cost_centers.py"):
        assert file in cited
    assert re.search(r"test_acceptance_engine_fixes\.py::test_a_credit_note", cited)
