"""Recurring expectations (§23): cadence, anchor, amounts, price changes, overdue copy."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from backoffice.domain.models import Document, DocumentType, Quality, Transaction, TransactionKind
from backoffice.learning.recurrence import (
    Basis,
    Cadence,
    Direction,
    Occurrence,
    check_overdue,
    detect_price_change,
    learn_from_documents,
    learn_from_transactions,
    learn_series,
    next_expected,
)

D = Decimal


def occ(on: date, amount: str | None = None, label: str | None = None) -> Occurrence:
    return Occurrence(on=on, amount=D(amount) if amount else None, label=label)


def vodafone_invoices() -> list[Occurrence]:
    # Around the 24th, never later than the 26th.
    days = [(2026, 4, 24), (2026, 5, 24), (2026, 6, 26), (2026, 7, 24), (2026, 8, 22)]
    return [occ(date(*d), "92.40", "VODAFONE PORTUGAL") for d in days]


# --------------------------------------------------------------------------- cadence


def test_monthly_invoice_series_learns_anchor_and_grace() -> None:
    series = learn_series("vodafone portugal", vodafone_invoices(), basis=Basis.INVOICES)
    assert series is not None
    assert series.cadence is Cadence.MONTHLY
    assert series.anchor == 24
    assert series.grace_days == 2
    assert series.early_days == -2
    assert series.trusted and series.quality is Quality.GREEN
    assert series.display_name == "Vodafone Portugal"
    assert series.typical_amount == D("92.40") and series.fixed_amount
    window = next_expected(series)
    assert window.expected == date(2026, 9, 24)
    assert window.due == date(2026, 9, 26)
    assert window.earliest == date(2026, 9, 22)


def test_spec_overdue_copy() -> None:
    series = learn_series("vodafone", vodafone_invoices(), basis=Basis.INVOICES, name="Vodafone")
    assert series is not None
    assert check_overdue(series, date(2026, 9, 26)) is None  # due date is inclusive
    notice = check_overdue(series, date(2026, 9, 29))
    assert notice is not None
    assert notice.message == "Vodafone normally issues an invoice by the 26th. Today is the 29th. Invoice missing."
    assert notice.due_on == date(2026, 9, 26)
    assert notice.missed_periods == 1 and not notice.likely_ended


def test_arrival_clears_overdue_and_moves_expectation() -> None:
    series = learn_series("vodafone", vodafone_invoices(), basis=Basis.INVOICES, name="Vodafone")
    assert series is not None
    assert check_overdue(series, date(2026, 9, 29), arrivals=[date(2026, 9, 25)]) is None
    assert next_expected(series, arrivals=[date(2026, 9, 25)]).expected == date(2026, 10, 24)
    # An older arrival changes nothing.
    assert check_overdue(series, date(2026, 9, 29), arrivals=[date(2026, 1, 2)]) is not None


def test_overdue_in_following_month_names_the_date() -> None:
    series = learn_series("vodafone", vodafone_invoices(), basis=Basis.INVOICES, name="Vodafone")
    notice = check_overdue(series, date(2026, 10, 2))  # type: ignore[arg-type]
    assert notice is not None
    assert notice.message == "Vodafone normally issues an invoice by the 26th. Today is 2 October. Invoice missing."


def test_long_silence_means_it_may_have_stopped() -> None:
    series = learn_series("vodafone", vodafone_invoices(), basis=Basis.INVOICES, name="Vodafone")
    notice = check_overdue(series, date(2026, 12, 1))  # type: ignore[arg-type]
    assert notice is not None and notice.likely_ended
    assert notice.missed_periods == 3
    assert notice.message == "Vodafone has not sent an invoice since 22 August. It may have stopped."


def test_rent_on_the_first_wraps_around_month_end() -> None:
    days = [date(2026, 3, 31), date(2026, 5, 1), date(2026, 6, 2), date(2026, 7, 1), date(2026, 7, 31)]
    series = learn_series("landlord", [occ(d, "1200") for d in days], basis=Basis.PAYMENTS, name="Rent")
    assert series is not None and series.cadence is Cadence.MONTHLY
    assert series.anchor == 1
    assert series.grace_days == 1
    window = next_expected(series)
    assert window.expected == date(2026, 9, 1)  # last seen 31 July belongs to August's period
    notice = check_overdue(series, date(2026, 9, 5))
    assert notice is not None
    assert notice.message == "Rent is normally paid by the 2nd. Today is the 5th. Payment missing."


def test_weekly_quarterly_annual_cadences() -> None:
    weekly = [date(2026, 8, 3) + timedelta(days=7 * i) for i in range(5)]
    s = learn_series("cleaner", [occ(d, "50") for d in weekly], basis=Basis.PAYMENTS)
    assert s is not None and s.cadence is Cadence.WEEKLY and s.anchor == 0  # Mondays

    quarterly = [date(2025, 10, 15), date(2026, 1, 15), date(2026, 4, 14), date(2026, 7, 15)]
    q = learn_series("insurer", [occ(d, "300") for d in quarterly], basis=Basis.INVOICES, name="Fidelidade")
    assert q is not None and q.cadence is Cadence.QUARTERLY
    assert next_expected(q).expected == date(2026, 10, 15)
    notice = check_overdue(q, date(2026, 10, 20))
    assert notice is not None
    assert notice.message == (
        "Fidelidade normally issues an invoice every 3 months. This one was due by 15 October. "
        "Today is 20 October. Invoice missing."
    )

    annual = [date(2024, 3, 10), date(2025, 3, 12)]
    a = learn_series("domain", [occ(d, "12") for d in annual], basis=Basis.PAYMENTS)
    assert a is not None and a.cadence is Cadence.ANNUAL and a.trusted


def test_monthly_with_one_skipped_month_is_still_monthly() -> None:
    days = [date(2026, 3, 5), date(2026, 4, 5), date(2026, 6, 5), date(2026, 7, 6), date(2026, 8, 5)]
    s = learn_series("x", [occ(d, "10") for d in days], basis=Basis.PAYMENTS)
    assert s is not None and s.cadence is Cadence.MONTHLY
    assert s.missed_in_history == 1


def test_irregular_or_single_occurrence_is_not_a_series() -> None:
    assert learn_series("x", [occ(date(2026, 1, 1), "1")], basis=Basis.PAYMENTS) is None
    days = [date(2026, 1, 1), date(2026, 1, 9), date(2026, 3, 2), date(2026, 3, 5), date(2026, 6, 20)]
    assert learn_series("x", [occ(d, "1") for d in days], basis=Basis.PAYMENTS) is None


def test_thin_history_is_amber_and_never_overdue() -> None:
    s = learn_series("x", [occ(date(2026, 7, 3), "9"), occ(date(2026, 8, 3), "9")], basis=Basis.INVOICES)
    assert s is not None and s.cadence is Cadence.MONTHLY
    assert not s.trusted and s.quality is Quality.AMBER  # AMBER is never promoted (§57)
    assert check_overdue(s, date(2026, 12, 31)) is None


def test_same_event_twice_counts_once() -> None:
    days = vodafone_invoices()
    s = learn_series("v", [*days, days[-1]], basis=Basis.INVOICES)
    assert s is not None and s.observations == 5


# --------------------------------------------------------------------------- amounts


def test_subscription_increase_detected() -> None:
    amounts = ["24.59"] * 4 + ["27.06"]
    dates = [date(2026, m, 3) for m in range(4, 9)]
    s = learn_series("adobe", [occ(d, a) for d, a in zip(dates, amounts)], basis=Basis.PAYMENTS, name="Adobe")
    assert s is not None and s.price_change is not None
    assert s.price_change.message == "Adobe went from €24.59 to €27.06."
    assert s.price_change.increased and s.price_change.difference == D("2.47")
    assert s.price_change.since == date(2026, 8, 3)
    assert s.typical_amount == D("27.06")  # the current price
    assert s.amount_min == D("24.59") and s.amount_max == D("27.06")


def test_one_off_spike_is_not_a_price_change() -> None:
    series = [(date(2026, m, 3), D(a)) for m, a in zip(range(3, 9), ["24.59", "24.59", "24.59", "30.00", "24.59", "24.59"])]
    assert detect_price_change(series, "EUR", "Adobe") is None


def test_small_fx_wobble_is_not_a_price_change() -> None:
    series = [(date(2026, m, 3), D(a)) for m, a in zip(range(3, 8), ["20.00", "20.05", "19.98", "20.02", "20.04"])]
    assert detect_price_change(series, "EUR", "Tool") is None


def test_price_decrease_reported_too() -> None:
    series = [(date(2026, m, 3), D(a)) for m, a in zip(range(3, 7), ["30.00", "30.00", "25.00", "25.00"])]
    change = detect_price_change(series, "EUR", "Tool")
    assert change is not None and not change.increased
    assert change.message == "Tool went from €30.00 to €25.00."


def test_usage_based_amounts_have_spread() -> None:
    amounts = ["92.40", "95.10", "91.00", "99.90", "92.40"]
    dates = [date(2026, m, 24) for m in range(4, 9)]
    s = learn_series("v", [occ(d, a) for d, a in zip(dates, amounts)], basis=Basis.INVOICES)
    assert s is not None
    assert s.typical_amount == D("92.40")
    assert not s.fixed_amount and s.amount_mad is not None and s.amount_mad > 0


# --------------------------------------------------------------------------- from domain objects


def tx(day: date, amount: str, counterparty: str, kind: TransactionKind = TransactionKind.CARD) -> Transaction:
    return Transaction(tenant_id="t1", account_id="acc1", booked_on=day, amount=D(amount),
                       counterparty=counterparty, kind=kind)  # fmt: skip


def test_learn_from_transactions_groups_by_counterparty_and_direction() -> None:
    txs = [tx(date(2026, m, 3), "-24.59", "PAYPAL *ADOBE 402-935") for m in range(4, 9)]
    txs += [tx(date(2026, m, 10), "500.00", "ACME CLIENT LDA") for m in range(4, 9)]
    txs += [tx(date(2026, m, 1), "-1000", "OWN SAVINGS", TransactionKind.INTERNAL) for m in range(4, 9)]
    learned = learn_from_transactions(txs)
    by_key = {(s.key, s.direction): s for s in learned}
    assert set(by_key) == {("adobe", Direction.OUT), ("acme client", Direction.IN)}
    assert by_key[("adobe", Direction.OUT)].display_name == "Adobe"
    income = by_key[("acme client", Direction.IN)]
    notice = check_overdue(income, date(2026, 9, 20))
    assert notice is not None and notice.message.startswith("ACME Client normally pays you by the 10th.")


def test_learn_from_documents_uses_arrival_dates_and_skips_credit_notes() -> None:
    docs = [
        Document(tenant_id="t1", evidence_ids=["e"], supplier_name="Vodafone", issue_date=date(2026, m, 1),
                 gross_amount=D("92.40"), doc_type=DocumentType.INVOICE)
        for m in range(4, 9)
    ]  # fmt: skip
    credit = Document(tenant_id="t1", evidence_ids=["e"], supplier_name="Vodafone", issue_date=date(2026, 6, 15),
                      gross_amount=D("10"), doc_type=DocumentType.CREDIT_NOTE)  # fmt: skip
    arrived = {d.id: d.issue_date + timedelta(days=23) for d in docs}  # type: ignore[operator]
    learned = learn_from_documents([*docs, credit], arrived_on=arrived)
    assert len(learned) == 1
    series = learned[0]
    assert series.basis is Basis.INVOICES and series.observations == 5
    assert series.anchor == 24


def test_float_amounts_refused() -> None:
    with pytest.raises(TypeError):
        Occurrence(on=date(2026, 1, 1), amount=1.5)  # type: ignore[arg-type]


def test_documents_without_name_group_by_normalized_tax_id() -> None:
    docs = [
        Document(tenant_id="t1", evidence_ids=["e"], supplier_tax_id=tax, issue_date=date(2026, m, 5), gross_amount=D("9"))
        for m, tax in zip(range(4, 8), ["PT509123456", "509 123 456", "PT509123456", "509123456"], strict=True)
    ]  # fmt: skip
    [series] = learn_from_documents(docs)
    assert series.key == "509123456" and series.observations == 4


def test_mixed_currency_uses_majority() -> None:
    items = [Occurrence(on=date(2026, m, 5), amount=D("10"), currency="EUR") for m in range(3, 8)]
    items.append(Occurrence(on=date(2026, 8, 5), amount=D("11"), currency="USD"))
    s = learn_series("x", items, basis=Basis.PAYMENTS)
    assert s is not None and s.currency == "EUR" and s.typical_amount == D("10")


@pytest.mark.parametrize("today", [date(2026, 9, 1), date(2026, 9, 24)])
def test_not_overdue_before_due(today: date) -> None:
    s = learn_series("v", vodafone_invoices(), basis=Basis.INVOICES)
    assert s is not None and check_overdue(s, today) is None
