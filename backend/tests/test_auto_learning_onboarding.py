"""First run (§5): question ranking, dedupe, cap, and the coverage headline."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from backoffice.domain.models import LegalEntity, Quality, Transaction
from backoffice.learning.entity import EntityAssignment
from backoffice.learning.onboarding import (
    MAX_QUESTION_LIMIT,
    Candidate,
    CoverageItem,
    compute_coverage,
    confirm_line,
    coverage_items,
    recurring_expense_question,
    select_questions,
    uncertainty_for,
)
from backoffice.learning.questions import OptionKind, Question, QuestionKind, QuestionOption, SubjectFacts
from backoffice.learning.recurrence import Basis, Occurrence, learn_series

D = Decimal
HAZEL = LegalEntity(id="ent_hazel", tenant_id="t1", name="Hazel Tree, Lda.", country="PT", tax_id="509123456")
OAK = LegalEntity(id="ent_oak", tenant_id="t1", name="Oak Studio", country="PT", tax_id="516000000")


def question(qid: str, key: str | None) -> Question:
    return Question(
        id=qid,
        tenant_id="t1",
        kind=QuestionKind.WHICH_COMPANY,
        prompt="We aren't sure which company this belongs to.",
        options=(QuestionOption(id="personal", label="Personal", kind=OptionKind.PERSONAL),
                 QuestionOption(id="other", label="Other", kind=OptionKind.OTHER)),  # fmt: skip
        facts=SubjectFacts(subject_type="transaction", subject_id=qid, counterparty_key=key),
        series_key=key,
    )


def cand(qid: str, key: str | None, amount: str, n: int, u: str) -> Candidate:
    return Candidate(question=question(qid, key), amount=D(amount), occurrences=n, uncertainty=D(u))


# --------------------------------------------------------------------------- selection


def test_rank_by_value_dedupe_by_series_and_cap() -> None:
    candidates = [
        cand("q1", "vodafone", "92.40", 3, "1"),  # 277.20
        cand("q2", "vodafone", "92.40", 1, "1"),  # same series, lower value
        cand("q3", "ikea", "400", 1, "0.5"),  # 200
        cand("q4", "uber", "12", 30, "1"),  # 360
        cand("q5", "adobe", "24.59", 3, "0"),  # verified: never asked
        cand("q6", "rent", "1200", 3, "0.5"),  # 1800
        cand("q7", "cafe", "3", 20, "1"),  # 60
    ]
    chosen = select_questions(candidates)
    assert [c.question.id for c in chosen] == ["q6", "q4", "q1", "q3"]
    assert len(select_questions(candidates, limit=9)) == 5


def test_ties_are_deterministic_and_limit_is_bounded() -> None:
    a, b = cand("qa", "b-series", "10", 1, "1"), cand("qb", "a-series", "10", 1, "1")
    assert [c.question.id for c in select_questions([a, b])] == ["qb", "qa"]
    with pytest.raises(ValueError):
        select_questions([a], limit=10)
    with pytest.raises(ValueError):
        select_questions([a], limit=0)
    assert MAX_QUESTION_LIMIT == 9


def test_questions_without_series_are_not_merged() -> None:
    chosen = select_questions([cand("q1", None, "10", 1, "1"), cand("q2", None, "10", 1, "1")])
    assert len(chosen) == 2


def test_candidate_validation_and_uncertainty() -> None:
    with pytest.raises(ValidationError):
        cand("q", "k", "-1", 1, "1")
    with pytest.raises(ValidationError):
        cand("q", "k", "1", 0, "1")
    with pytest.raises(ValidationError):
        cand("q", "k", "1", 1, "1.5")
    with pytest.raises(ValidationError):
        Candidate(question=question("q", "k"), amount=1.5, occurrences=1, uncertainty=D(1))  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        CoverageItem(amount=1.5, explained=True)  # type: ignore[arg-type]
    assert uncertainty_for(None) == 1 and uncertainty_for(Quality.RED) == 1
    assert uncertainty_for(Quality.AMBER) == D("0.5") and uncertainty_for(Quality.GREEN) == 0


# --------------------------------------------------------------------------- coverage


def test_coverage_is_the_lower_share_rounded_down() -> None:
    items = [CoverageItem(amount=D("100"), explained=True) for _ in range(24)]
    items.append(CoverageItem(amount=D("60"), explained=False, series_key="vodafone"))
    cov = compute_coverage(items)
    # by count 24/25 = 96%, by volume 2400/2460 = 97.56% -> headline uses the lower, floored.
    assert cov.by_count == D("0.96")
    assert cov.percent == 96
    assert cov.headline == "We understand 96% of your business."
    projected = compute_coverage(items, assume_answered=["vodafone"])
    assert projected.percent == 100 and projected.explained_transactions == 25


def test_coverage_never_shows_100_while_something_is_unexplained() -> None:
    items = [CoverageItem(amount=D("1000"), explained=True) for _ in range(999)]
    items.append(CoverageItem(amount=D("0.01"), explained=False))
    assert compute_coverage(items).percent == 99


def test_coverage_foreign_currency_only_counts() -> None:
    items = [CoverageItem(amount=D("50"), explained=True), CoverageItem(amount=D("500"), explained=False, currency="USD")]
    cov = compute_coverage(items)
    assert cov.by_volume == 1 and cov.by_count == D("0.5") and cov.percent == 50


def test_coverage_empty_is_still_learning() -> None:
    cov = compute_coverage([])
    assert cov.percent is None and cov.headline == "We are still learning how your business works."


def test_coverage_items_from_assignments() -> None:
    txs = [
        Transaction(id=f"tx{i}", tenant_id="t1", account_id="a", booked_on=date(2026, 9, 1), amount=D("-10"),
                    counterparty="X")
        for i in range(4)
    ]  # fmt: skip
    assignments = {
        "tx0": EntityAssignment(subject_type="transaction", subject_id="tx0", entity_id="ent_hazel", private=False,
                                quality=Quality.GREEN, why=()),
        "tx1": EntityAssignment(subject_type="transaction", subject_id="tx1", entity_id=None, private=True,
                                quality=Quality.AMBER, why=()),
        "tx2": EntityAssignment(subject_type="transaction", subject_id="tx2", entity_id=None, private=False,
                                quality=Quality.RED, why=()),
    }  # fmt: skip
    items = coverage_items(txs, assignments)
    assert [i.explained for i in items] == [True, True, False, False]
    assert compute_coverage(items).percent == 50


def test_confirm_line() -> None:
    assert confirm_line(4) == "We need you to confirm 4 things."
    assert confirm_line(1) == "We need you to confirm one thing."
    assert confirm_line(0) == "Nothing needs you right now."


# --------------------------------------------------------------------------- §5 recurring question


def vodafone_series():
    occurrences = [Occurrence(on=date(2026, m, 24), amount=D("92.40"), label="VODAFONE PORTUGAL") for m in range(6, 10)]
    series = learn_series("vodafone", occurrences, basis=Basis.PAYMENTS, name="Vodafone")
    assert series is not None
    return series


def test_spec_recurring_question_single_company() -> None:
    q = recurring_expense_question(vodafone_series(), tenant_id="t1", entities=[HAZEL], category="telecom",
                                   category_label="telecom")  # fmt: skip
    assert q.prompt == "This €92.40 Vodafone expense appears every month."
    assert [o.label for o in q.options] == ["Company telecom", "Personal", "Other"]
    first = q.options[0]
    assert first.kind is OptionKind.CATEGORY and first.entity_id == "ent_hazel" and first.category == "telecom"
    assert q.series_key == "vodafone" and q.why == ("Seen 4 times since 24 June.",)


def test_recurring_question_multi_company_and_validation() -> None:
    q = recurring_expense_question(vodafone_series(), tenant_id="t1", entities=[OAK, HAZEL])
    assert [o.label for o in q.options] == ["Hazel Tree", "Oak Studio", "Personal", "Other"]
    with pytest.raises(ValueError):
        recurring_expense_question(vodafone_series(), tenant_id="t9", entities=[HAZEL])
    with pytest.raises(ValueError):
        recurring_expense_question(vodafone_series(), tenant_id="t1", entities=[HAZEL], category="telecom")
