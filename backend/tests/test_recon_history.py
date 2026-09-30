"""Historical payment patterns and the bounded subset search (§20, §23)."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from backoffice.reconciliation import (
    Cadence,
    InMemoryPaymentHistory,
    PaymentHistory,
    PaymentPattern,
    infer_cadence,
)
from backoffice.reconciliation._subset import find_subsets
from backoffice.reconciliation._text import (
    compile_identifier,
    format_money,
    same_tax_id,
    scale_to_int,
)

D = Decimal


# --- cadence


def monthly(day: int, months: int) -> list[date]:
    return [date(2026, m, day) for m in range(1, months + 1)]


def test_infer_cadence_bands() -> None:
    assert infer_cadence(monthly(24, 5)) is Cadence.MONTHLY
    assert (
        infer_cadence([date(2026, 1, 5) + timedelta(weeks=w) for w in range(5)])
        is Cadence.WEEKLY
    )
    assert (
        infer_cadence([date(2026, 1, 1), date(2026, 4, 1), date(2026, 7, 1)])
        is Cadence.QUARTERLY
    )
    assert (
        infer_cadence([date(2023, 3, 1), date(2024, 3, 1), date(2025, 3, 1)])
        is Cadence.YEARLY
    )


def test_infer_cadence_needs_enough_regular_points() -> None:
    assert infer_cadence(monthly(24, 2)) is None
    assert (
        infer_cadence(
            [date(2026, 1, 1), date(2026, 1, 20), date(2026, 3, 30), date(2026, 4, 2)]
        )
        is None
    )
    assert infer_cadence(monthly(24, 2), min_occurrences=2) is Cadence.MONTHLY


def test_pattern_learning_and_fit() -> None:
    pattern = PaymentPattern.learn(
        [(d, D("-92.40")) for d in monthly(24, 6)], card_last4=["4817"]
    )
    assert pattern.cadence is Cadence.MONTHLY and pattern.usual_day == 24
    assert pattern.usual_amount == D("92.40") and pattern.occurrences == 6
    assert pattern.fits(date(2026, 7, 26), D("-95.00"))
    assert not pattern.fits(date(2026, 7, 26), D("-150.00"))  # amount outside 10%
    assert not pattern.fits(date(2026, 7, 10), D("-92.40"))  # wrong time of month


def test_month_end_days_wrap_around() -> None:
    pattern = PaymentPattern(cadence=Cadence.MONTHLY, usual_day=30)
    assert pattern.day_fits(date(2026, 3, 1))


def test_pattern_without_cadence_never_fits() -> None:
    assert not PaymentPattern(cadence=None).fits(date(2026, 1, 1), D("1"))


def test_pattern_validation() -> None:
    with pytest.raises(TypeError):
        PaymentPattern(cadence=Cadence.MONTHLY, usual_amount=9.5)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        PaymentPattern(cadence=Cadence.MONTHLY, usual_day=32)


def test_in_memory_history_is_a_payment_history() -> None:
    history = InMemoryPaymentHistory({"sup_x": PaymentPattern(cadence=Cadence.WEEKLY)})
    assert isinstance(history, PaymentHistory)
    assert (
        history.pattern_for("sup_x") is not None
        and history.pattern_for("other") is None
    )


# --- subset search


def test_subset_search_finds_all_small_combinations_in_order() -> None:
    result = find_subsets([100, 150, 50, 200], 250)
    assert result.solutions == ((0, 1), (2, 3))  # depth-first, in the order given
    assert result.complete


def test_subset_search_handles_credit_notes() -> None:
    result = find_subsets([-10000, 2000, -5500], -8000)
    assert result.solutions == ((0, 1),) and result.complete


def test_subset_search_respects_size_bounds() -> None:
    assert find_subsets([5, 5, 5, 5], 20, max_size=3).solutions == ()
    assert find_subsets([10, 20], 10, min_size=2).solutions == ()
    assert find_subsets([10, 20], 10, min_size=1).solutions == ((0,),)


def test_subset_search_caps_and_says_so() -> None:
    unreachable = find_subsets(list(range(1, 40)), 10_000, node_limit=100)
    assert (
        unreachable.nodes == 0 and unreachable.complete
    )  # pruned by the bound, not capped
    evens_to_odd = find_subsets(list(range(2, 80, 2)), 101, node_limit=100)
    assert (
        evens_to_odd.capped and not evens_to_odd.complete and evens_to_odd.nodes == 101
    )


def test_subset_search_stops_after_enough_solutions() -> None:
    result = find_subsets([1] * 10, 2, max_solutions=3)
    assert len(result.solutions) == 3 and result.truncated and not result.complete


def test_subset_search_rejects_bad_bounds() -> None:
    with pytest.raises(ValueError):
        find_subsets([1], 1, min_size=0)
    with pytest.raises(ValueError):
        find_subsets([1], 1, min_size=3, max_size=2)


def test_subset_search_is_deterministic() -> None:
    values = [13, 7, 20, 5, 8, 15, 12]
    assert find_subsets(values, 20) == find_subsets(list(values), 20)


# --- text helpers


def test_scale_to_int_is_exact_for_any_precision() -> None:
    assert scale_to_int([D("1.5"), D("0.125"), D("-3")]) == ([1500, 125, -3000], 1000)
    assert scale_to_int([D("10")]) == ([1000], 100)


def test_money_formatting() -> None:
    assert format_money(D("1492.3")) == "€1,492.30"
    assert format_money(D("-12"), "GBP") == "−£12.00"
    assert format_money(D("5"), "CHF") == "CHF 5.00"
    with pytest.raises(TypeError):
        format_money(1.5)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        format_money(D("1"), "EURO")


def test_tax_ids_compare_with_or_without_country_prefix() -> None:
    assert same_tax_id("PT 502 544 180", "502544180")
    assert same_tax_id("ESB12345678", "B12345678")
    assert not same_tax_id("502544180", "502544181")
    assert not same_tax_id(None, "502544180")


def test_weak_identifiers_are_never_searched() -> None:
    assert compile_identifier("183") is None
    assert compile_identifier("FATURA") is None
    assert compile_identifier("123456") is not None
    assert compile_identifier("FT 2026/183") is not None
