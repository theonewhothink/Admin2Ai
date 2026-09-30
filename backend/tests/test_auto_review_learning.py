"""Adversarial review of learning (§5, §23, §38, §46): defects reproduced before their fix."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from backoffice.domain.models import Document, LegalEntity, Quality, Transaction
from backoffice.learning.entity import EntityAssignment, OwnershipBook, assign_entity
from backoffice.learning.onboarding import candidates_from_assignments, select_questions
from backoffice.learning.questions import Answer
from backoffice.learning.recurrence import Basis, Cadence, Occurrence, check_overdue, learn_series, next_expected
from backoffice.learning.rules import (
    PERSONAL,
    Rule,
    RuleAuthor,
    RuleBook,
    RuleMatch,
    RuleOutcome,
    RuleScope,
    RuleSubject,
    suggest_rule_from_answer,
)

D = Decimal
T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)
HAZEL = LegalEntity(id="ent_hazel", tenant_id="t1", name="Hazel Tree, Lda.", country="PT", tax_id="509123456")
OAK = LegalEntity(id="ent_oak", tenant_id="t1", name="Oak Studio", country="PT", tax_id="516000000")
SUPPLIER_ID = "sup_0a1b2c3d4e5f6a7b"


# --------------------------------------------------------------------------- rule keys


def test_canonical_supplier_id_keys_survive_normalization() -> None:
    """Defect: 'sup_0a1b…' was reduced to 'sup', so the rule never matched its own supplier
    and did match a counterparty literally called 'SUP'."""
    match = RuleMatch(counterparty_key=SUPPLIER_ID)
    assert match.counterparty_key == SUPPLIER_ID
    assert match.matches(RuleSubject(counterparty_key=SUPPLIER_ID))
    assert not match.matches(RuleSubject(counterparty_key="sup"))
    assert not match.matches(RuleSubject(counterparty_key="sup_ffffffffffffffff"))
    assert RuleMatch(counterparty_key="SUP_VODA").counterparty_key == "sup_voda"
    # Names still normalize, on both sides.
    assert RuleMatch(counterparty_key="IKEA ALFRAGIDE 1234").counterparty_key == "ikea alfragide"
    assert RuleMatch(counterparty_key="ikea").matches(RuleSubject(counterparty_key="IKEA"))


def test_one_tap_rule_keyed_by_supplier_id_applies_next_time() -> None:
    ownership = OwnershipBook(cards={"4817": PERSONAL})
    payment = Transaction(id="tx_1", tenant_id="t1", account_id="acc", booked_on=date(2026, 9, 12),
                          amount=D("-84.50"), counterparty="IKEA ALFRAGIDE", card_last4="4817")  # fmt: skip
    first = assign_entity(entities=[HAZEL, OAK], transaction=payment, ownership=ownership, key=SUPPLIER_ID)
    assert first.question is not None
    answer = Answer(question_id=first.question.id, option_id="entity:ent_hazel", answered_by="owner1", answered_at=T0)
    proposal = suggest_rule_from_answer(first.question, answer)
    assert proposal is not None
    book = RuleBook()
    book.add(proposal.rule)
    later = payment.model_copy(update={"id": "tx_2", "booked_on": date(2026, 10, 12)})
    second = assign_entity(entities=[HAZEL, OAK], transaction=later, ownership=ownership, rulebook=book, key=SUPPLIER_ID)
    assert second.entity_id == "ent_hazel" and second.question is None
    assert second.rule_id == proposal.rule.id


# --------------------------------------------------------------------------- recurrence robustness


def _vodafone(extra: list[tuple[int, int, str]] = ()) -> list[Occurrence]:  # type: ignore[assignment]
    days = [(3, 24), (4, 23), (5, 25), (6, 24), (7, 26), (8, 24)]
    occurrences = [Occurrence(on=date(2026, m, d), amount=D("92.40")) for m, d in days]
    occurrences += [Occurrence(on=date(2026, m, d), amount=D(a)) for m, d, a in extra]
    return occurrences


def test_one_off_extra_invoice_does_not_break_the_series() -> None:
    """Defect: one extra invoice (a phone bought on 8 May) made a clear monthly series vanish."""
    series = learn_series("vodafone", _vodafone([(5, 8, "300.00")]), basis=Basis.INVOICES, name="Vodafone")
    assert series is not None and series.cadence is Cadence.MONTHLY and series.trusted
    assert series.anchor == 24 and series.early_days == -1 and series.grace_days == 2
    assert series.off_cycle == 1
    assert series.typical_amount == D("92.40") and series.amount_max == D("92.40")
    notice = check_overdue(series, date(2026, 9, 29))
    assert notice is not None
    assert notice.message == "Vodafone normally issues an invoice by the 26th. Today is the 29th. Invoice missing."


def test_off_cycle_extra_never_widens_the_due_window() -> None:
    """Defect: an extra invoice mid-month stretched 'by the 26th' to 'by the 8th' of next month."""
    occurrences = _vodafone([(8, 8, "300.00"), (6, 9, "15.00")])
    series = learn_series("vodafone", occurrences, basis=Basis.INVOICES, name="Vodafone")
    assert series is not None
    assert next_expected(series).due == date(2026, 9, 26)


def test_two_invoices_on_the_same_day_are_one_arrival() -> None:
    """Defect: a supplier sending two invoices each month (mobile + internet) was 'not a series'."""
    occurrences = []
    for month in range(3, 9):
        occurrences += [Occurrence(on=date(2026, month, 24), amount=D("30.00")),
                        Occurrence(on=date(2026, month, 24), amount=D("60.00"))]  # fmt: skip
    series = learn_series("vodafone", occurrences, basis=Basis.INVOICES, name="Vodafone")
    assert series is not None and series.cadence is Cadence.MONTHLY
    assert series.observations == 6 and series.anchor == 24


def test_off_cycle_arrival_does_not_silence_the_missing_invoice() -> None:
    """Defect: any document from the supplier (a one-off on 10 September) cleared September's invoice."""
    series = learn_series("vodafone", _vodafone(), basis=Basis.INVOICES, name="Vodafone")
    assert series is not None
    assert check_overdue(series, date(2026, 9, 29), arrivals=[date(2026, 9, 10)]) is not None
    assert check_overdue(series, date(2026, 9, 29), arrivals=[date(2026, 9, 25)]) is None
    assert next_expected(series, arrivals=[date(2026, 9, 10)]).expected == date(2026, 9, 24)


def test_too_many_extras_is_not_a_series() -> None:
    occurrences = _vodafone([(3, 8, "1"), (4, 9, "1"), (5, 10, "1"), (6, 11, "1")])
    assert learn_series("vodafone", occurrences, basis=Basis.INVOICES) is None


# --------------------------------------------------------------------------- entity: tax number country


def test_invoice_to_same_digits_in_another_country_is_not_ours() -> None:
    """Defect: an invoice to ES509123456 was assigned GREEN to the Portuguese company 509123456."""
    invoice = Document(id="doc_1", tenant_id="t1", evidence_ids=["ev"], supplier_name="Acme",
                       customer_tax_id="ES509123456", gross_amount=D("10"))  # fmt: skip
    result = assign_entity(entities=[HAZEL, OAK], document=invoice)
    assert result.entity_id is None and result.question is not None
    ours = assign_entity(entities=[HAZEL, OAK], document=invoice.model_copy(update={"customer_tax_id": "PT509123456"}))
    assert ours.entity_id == "ent_hazel" and ours.quality is Quality.GREEN


# --------------------------------------------------------------------------- onboarding candidates


def _payment(n: int, counterparty: str, amount: str, card: str = "4817") -> Transaction:
    return Transaction(id=f"tx_{n}", tenant_id="t1", account_id="acc", booked_on=date(2026, 9, n),
                       amount=D(amount), counterparty=counterparty, card_last4=card)  # fmt: skip


def test_candidates_from_entity_results_group_by_series() -> None:
    ownership = OwnershipBook(cards={"4817": PERSONAL, "1111": "ent_hazel"})
    payments = [
        _payment(1, "VODAFONE PORTUGAL", "-92.40"),
        _payment(2, "VODAFONE PORTUGAL", "-95.00"),
        _payment(3, "VODAFONE PORTUGAL", "-90.00"),
        _payment(4, "IKEA ALFRAGIDE", "-400.00"),
        _payment(5, "PINGO DOCE", "-12.00", card="1111"),  # company card: no question
    ]
    results: dict[str, EntityAssignment] = {
        p.id: assign_entity(entities=[HAZEL, OAK], transaction=p, ownership=ownership) for p in payments
    }
    candidates = candidates_from_assignments(payments, results)
    by_key = {c.series_key: c for c in candidates}
    assert set(by_key) == {"vodafone portugal", "ikea alfragide"}
    vodafone = by_key["vodafone portugal"]
    assert vodafone.occurrences == 3 and vodafone.amount == D("92.40")
    assert vodafone.uncertainty == D(1)  # no assignment at all
    assert vodafone.question.facts.subject_id == "tx_3"  # most recent occurrence asks for the series
    chosen = select_questions(candidates, limit=4)
    assert [c.series_key for c in chosen] == ["ikea alfragide", "vodafone portugal"]  # 400 > 3 x 92.40


# --------------------------------------------------------------------------- small contract gaps


def test_rule_subject_refuses_float_money() -> None:
    """Defect: RuleSubject(amount=84.5) was accepted and compared against Decimal bounds."""
    import pytest

    with pytest.raises(TypeError):
        RuleSubject(counterparty_key="ikea", amount=84.5)  # type: ignore[arg-type]


def test_recurring_question_names_the_year_of_an_older_start() -> None:
    """Defect: a 12-month import said 'since 24 October' for October of last year."""
    from backoffice.learning.onboarding import recurring_expense_question

    occurrences = [Occurrence(on=date(2025 + (m > 12), (m - 1) % 12 + 1, 24), amount=D("92.40")) for m in range(10, 16)]
    series = learn_series("vodafone", occurrences, basis=Basis.INVOICES, name="Vodafone")
    assert series is not None
    q = recurring_expense_question(series, tenant_id="t1", entities=[HAZEL], today=date(2026, 3, 1))
    assert q.why == ("Seen 6 times since 24 October 2025.",)


def test_addressee_why_line_is_one_clean_line() -> None:
    invoice = Document(id="doc_1", tenant_id="t1", evidence_ids=["ev"], supplier_name="Acme",
                       customer_tax_id=" PT 999\n999   990 ", gross_amount=D("10"))  # fmt: skip
    result = assign_entity(entities=[HAZEL, OAK], document=invoice)
    assert "Invoice addressed to another company (tax number PT 999 999 990)" in result.why


def test_rule_audit_times_are_timezone_aware() -> None:
    """Defect: the audit trail (§55) accepted naive datetimes."""
    import pytest
    from pydantic import ValidationError

    from backoffice.learning.rules import RuleEvent, RuleEventKind

    with pytest.raises(ValidationError):
        RuleEvent(at=datetime(2026, 9, 1), kind=RuleEventKind.CREATED, rule_id="r", actor="a")
    book = RuleBook()
    rule = Rule(author=RuleAuthor.OWNER, author_id="o", scope=RuleScope.TENANT, tenant_id="t1",
                match=RuleMatch(counterparty_key="ikea"), outcome=RuleOutcome(entity_id="ent_hazel"), created_at=T0)  # fmt: skip
    book.add(rule)
    with pytest.raises(ValidationError):
        book.deactivate(rule.id, actor="o", at=datetime(2026, 9, 2))
    assert book.get(rule.id).active  # a rejected change leaves the book untouched


def test_negative_total_invoice_is_not_part_of_the_rhythm_or_amounts() -> None:
    """Defect: an 'invoice' with a negative total (a credit in disguise) became the series minimum."""
    from backoffice.learning.recurrence import learn_from_documents

    docs = [Document(id=f"d{m}", tenant_id="t1", evidence_ids=["e"], supplier_name="Acme", issue_date=date(2026, m, 5),
                     gross_amount=D("-50") if m == 6 else D("50")) for m in range(3, 10)]  # fmt: skip
    [series] = learn_from_documents(docs)
    assert series.amount_min == D("50") and series.observations == 6


def test_weekend_drift_around_february_is_still_monthly() -> None:
    """Defect: 26 Feb -> 22 Mar is 24 days (outside the 6-day gap tolerance) although both
    arrivals are within 2 days of the 24th, so a short, clear monthly series was 'not a series'."""
    occurrences = [Occurrence(on=d, amount=D("39.00")) for d in
                   (date(2026, 1, 24), date(2026, 2, 26), date(2026, 3, 22), date(2026, 4, 24))]  # fmt: skip
    series = learn_series("adobe", occurrences, basis=Basis.INVOICES, name="Adobe")
    assert series is not None and series.cadence is Cadence.MONTHLY and series.anchor == 24
    assert series.missed_in_history == 0 and series.trusted


def test_period_matching_does_not_turn_quarterly_into_monthly() -> None:
    days = [date(2025, 1, 15), date(2025, 4, 15), date(2025, 7, 15), date(2025, 10, 15), date(2026, 1, 15)]
    series = learn_series("insurer", [Occurrence(on=d) for d in days], basis=Basis.INVOICES)
    assert series is not None and series.cadence is Cadence.QUARTERLY


def test_frequent_gaps_are_not_a_trusted_rhythm() -> None:
    """Only the occasional skipped month is tolerated: 6 invoices spread over 12 months
    would make 'Invoice missing' fire in months that are legitimately skipped (§21)."""
    days = [date(2025, m, 20) for m in (1, 3, 6, 10, 11, 12)]
    series = learn_series("seasonal", [Occurrence(on=d) for d in days], basis=Basis.INVOICES)
    assert series is None or not series.trusted


def test_two_arrivals_in_one_period_keep_the_one_on_the_rhythm() -> None:
    days = [date(2026, m, 24) for m in range(3, 9)] + [date(2026, 6, 19)]  # 19 June is near the 24th but a second one
    series = learn_series("vodafone", [Occurrence(on=d) for d in days], basis=Basis.INVOICES)
    assert series is not None and series.observations == 6 and series.off_cycle == 1
    assert series.early_days == 0 and series.grace_days == 0


def test_setting_extras_aside_costs_trust() -> None:
    days = [date(2026, 6, 24), date(2026, 7, 24), date(2026, 8, 24), date(2026, 8, 8)]
    series = learn_series("x", [Occurrence(on=d) for d in days], basis=Basis.INVOICES)
    assert series is not None and series.off_cycle == 1 and not series.trusted
    assert check_overdue(series, date(2026, 12, 1)) is None  # untrusted: never chases


def test_learning_is_independent_of_input_order() -> None:
    import random

    occurrences = _vodafone([(5, 8, "300.00"), (8, 9, "12.00")])
    expected = learn_series("vodafone", occurrences, basis=Basis.INVOICES)
    rng = random.Random(3)
    for _ in range(20):
        shuffled = occurrences[:]
        rng.shuffle(shuffled)
        assert learn_series("vodafone", shuffled, basis=Basis.INVOICES) == expected
