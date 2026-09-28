"""Activation (§58), customer-success metrics (§59), Home summary (§35), Free Business Audit (§60)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from backoffice.closure import (
    AUDIT_DAYS,
    ActivationCondition,
    ActivationSignals,
    Activity,
    ActivityKind,
    Actor,
    BusinessAuditFindings,
    InteractionKind,
    Metric,
    Month,
    OwnerInteraction,
    PriceIncrease,
    audit_window,
    compute_month_status,
    customer_success,
    evaluate_activation,
    home_summary,
    is_owner_actor,
    owner_touched,
    render_business_audit,
)
from backoffice.domain.lifecycle import ORDER, Stage, TrackedItem
from backoffice.domain.models import Obligation, ObligationKind, Quality

UTC = timezone.utc
SEPT = Month(2026, 9)
SIGNUP = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)


def mins(n: float) -> datetime:
    return SIGNUP + timedelta(minutes=n)


# =========================================================================== activation (§58)


def test_activation_needs_all_six_conditions():
    signals = ActivationSignals(
        signed_up_at=SIGNUP,
        email_connected_at=mins(1),
        bank_connected_at=mins(2),
        historical_scan_completed_at=mins(9),
        first_document_found_at=mins(3.5),
        first_auto_match_at=mins(4),
        time_saved_seen_at=mins(10),
    )
    report = evaluate_activation(signals)
    assert report.activated and report.missing == ()
    assert report.activated_at == mins(10)
    assert report.time_to_first_value == timedelta(minutes=3.5)
    assert report.first_value_on_target is True


def test_missing_conditions_are_listed_and_time_to_first_value_is_strict():
    report = evaluate_activation(
        ActivationSignals(signed_up_at=SIGNUP, email_connected_at=mins(1), first_auto_match_at=mins(5))
    )
    assert not report.activated and report.activated_at is None
    assert report.met == (ActivationCondition.EMAIL_CONNECTED, ActivationCondition.TRANSACTION_AUTO_MATCHED)
    assert ActivationCondition.BANK_CONNECTED in report.missing
    assert report.time_to_first_value == timedelta(minutes=5)
    assert report.first_value_on_target is False  # "< 5 minutes"


def test_no_value_yet():
    report = evaluate_activation(ActivationSignals(signed_up_at=SIGNUP))
    assert report.time_to_first_value is None and report.first_value_on_target is None
    assert len(report.missing) == 6


def test_activation_signals_are_validated():
    with pytest.raises(ValueError):
        ActivationSignals(signed_up_at=SIGNUP, email_connected_at=SIGNUP - timedelta(seconds=1))
    with pytest.raises(ValueError):
        ActivationSignals(signed_up_at=datetime(2026, 9, 1, 10, 0))


# =========================================================================== customer success (§59)


def done_item(n: int, *, owner: bool = False, asked: bool = False) -> TrackedItem:
    item = TrackedItem(tenant_id="t1", subject_type="transaction", subject_id=f"tx_{n}")
    for stage in ORDER[1:4]:  # to VERIFIED
        item.advance(stage, actor="system", evidence_ids=["ev"], quality=Quality.GREEN)
    if asked:
        item.advance(Stage.NEEDS_OWNER, actor="system", evidence_ids=["ev_q"])
    actor = "owner:ana" if owner or asked else "system"
    for stage in ORDER[4:]:
        item.advance(stage, actor=actor, evidence_ids=["ev"], quality=Quality.GREEN)
    return item


def test_owner_actor_and_touch_detection():
    assert is_owner_actor("owner") and is_owner_actor("owner:ana")
    assert not is_owner_actor("owners") and not is_owner_actor("system")
    assert not owner_touched(done_item(1))
    assert owner_touched(done_item(2, owner=True))
    assert owner_touched(done_item(3, asked=True))


def at(day: int, month: int = 9) -> datetime:
    return datetime(2026, month, day, 12, tzinfo=UTC)


def test_customer_success_metrics():
    items = [done_item(i) for i in range(19)] + [done_item(19, asked=True)]
    items.append(TrackedItem(tenant_id="t1", subject_type="transaction", subject_id="tx_open"))  # not done
    activities = [Activity(ActivityKind.MISSING_DOCUMENT_DETECTED, at(3), "e1", subject_id=f"tx_{i}") for i in range(10)]
    activities += [Activity(ActivityKind.MISSING_DOCUMENT_RETRIEVED, at(5), "e1", subject_id=f"tx_{i}") for i in range(9)]
    activities += [
        Activity(ActivityKind.MISSING_DOCUMENT_RETRIEVED, at(6), "e1", Actor.OWNER, subject_id="tx_9"),
        Activity(ActivityKind.MISSING_DOCUMENT_RETRIEVED, at(6), "e1", subject_id="tx_never_detected"),
        Activity(ActivityKind.ACCOUNTANT_QUESTION_RESOLVED, at(7), "e1", Actor.OWNER, subject_id="q1"),
        Activity(ActivityKind.ACCOUNTANT_QUESTION_RESOLVED, at(7), "e1", Actor.SYSTEM, subject_id="q2"),
        Activity(ActivityKind.SILENT_ERROR_FOUND, at(2, 10), "e1", subject_id="tx_old"),  # another month
    ]
    interactions = [
        OwnerInteraction(at(3), 400, InteractionKind.ANSWER, "e1"),
        OwnerInteraction(at(4), 300, InteractionKind.APPROVAL, "e2"),
        OwnerInteraction(at(1, 8), 520, InteractionKind.ONBOARDING, None),
        OwnerInteraction(at(3, 10), 999, InteractionKind.ANSWER, "e1"),  # October
    ]
    report = customer_success(
        SEPT, items=items, activities=activities, interactions=interactions, accountant_question_baseline=10
    )
    owner = report.get(Metric.OWNER_ADMIN_MINUTES)
    assert (owner.value, owner.on_target) == (12, True)  # 700 s -> 12 minutes, rounded up
    zero = report.get(Metric.ZERO_TOUCH_RATE)
    assert (zero.value, zero.numerator, zero.denominator, zero.on_target) == (Decimal("95.0"), 19, 20, False)
    auto = report.get(Metric.AUTO_RESOLVED_MISSING_RATE)
    assert (auto.value, auto.numerator, auto.denominator, auto.on_target) == (Decimal("90.0"), 9, 10, False)
    errors = report.get(Metric.CRITICAL_SILENT_ERRORS)
    assert (errors.value, errors.on_target) == (0, True)
    onboarding = report.get(Metric.ONBOARDING_ACTIVE_MINUTES)
    assert (onboarding.value, onboarding.on_target) == (9, True)
    acct = report.get(Metric.ACCOUNTANT_QUESTIONS_NEEDING_OWNER)
    assert (acct.value, acct.on_target) == (Decimal("10.0"), True)
    unresolved = report.get(Metric.UNRESOLVED_MONTH_END_RATE)
    assert unresolved.value is None and unresolved.on_target is None  # no statuses given
    assert not report.on_target


def test_rates_round_towards_the_unfavourable_side():
    items = [done_item(0), done_item(1), done_item(2, owner=True)]
    zero = customer_success(SEPT, items=items).get(Metric.ZERO_TOUCH_RATE)
    assert zero.value == Decimal("66.6")  # 66.66… shown as 66.6, never 66.7


class Covered:
    name = "Gmail"
    healthy = True
    covered_from = datetime(2026, 1, 1, tzinfo=UTC)
    covered_until = datetime(2026, 10, 3, tzinfo=UTC)


NOW = datetime(2026, 10, 3, 9, 0, tzinfo=UTC)


def status_of(entity: str, done: int, open_: int = 0, **kw):
    items = [done_item(i) for i in range(done)]
    items += [TrackedItem(tenant_id="t1", subject_type="transaction", subject_id=f"tx_o{i}") for i in range(open_)]
    return compute_month_status(entity, SEPT, items, now=NOW, connectors=[Covered()], **kw)


def test_unresolved_month_end_rate_is_rounded_up():
    report = customer_success(SEPT, statuses=[status_of("e1", 299, 1), status_of("e2", 100)])
    unresolved = report.get(Metric.UNRESOLVED_MONTH_END_RATE)
    assert (unresolved.value, unresolved.on_target) == (Decimal("0.3"), True)
    worse = customer_success(SEPT, statuses=[status_of("e1", 99, 1)]).get(Metric.UNRESOLVED_MONTH_END_RATE)
    assert (worse.value, worse.on_target) == (Decimal("1.0"), False)


def test_metrics_without_data_are_not_judged():
    report = customer_success(SEPT)
    assert report.get(Metric.ZERO_TOUCH_RATE).on_target is None
    assert report.get(Metric.AUTO_RESOLVED_MISSING_RATE).on_target is None
    assert report.get(Metric.ONBOARDING_ACTIVE_MINUTES).value is None
    acct = report.get(Metric.ACCOUNTANT_QUESTIONS_NEEDING_OWNER)
    assert (acct.value, acct.on_target) == (0, None)  # no baseline: a count, not judged
    assert report.get(Metric.OWNER_ADMIN_MINUTES).value == 0
    assert report.on_target


def test_silent_errors_fail_the_target():
    acts = [Activity(ActivityKind.SILENT_ERROR_FOUND, at(9), "e1", subject_id="tx_1")] * 2
    errors = customer_success(SEPT, activities=acts).get(Metric.CRITICAL_SILENT_ERRORS)
    assert (errors.value, errors.on_target) == (1, False)


# =========================================================================== Home (§35)


def obligation(entity: str, due: date, title="Tax payment") -> Obligation:
    return Obligation(tenant_id="t1", entity_id=entity, kind=ObligationKind.TAX_DEADLINE, title=title, due_on=due)


def asked_status(entity: str, n_asked: int):
    items = [done_item(i) for i in range(10)]
    for i in range(n_asked):
        it = TrackedItem(tenant_id="t1", subject_type="transaction", subject_id=f"tx_q{i}")
        it.advance(Stage.NEEDS_OWNER, actor="system", evidence_ids=["ev_q"])
        items.append(it)
    return compute_month_status(entity, SEPT, items, now=NOW, connectors=[Covered()])


def test_home_summary_like_section_35():
    statuses = [asked_status("e_hazel", 1), status_of("e_oak", 5), asked_status("e_birch", 1)]
    names = {"e_hazel": "Hazel Tree", "e_oak": "Oak Studio", "e_birch": "Birch Café"}
    today = date(2026, 10, 3)
    obligations = [
        obligation("e_hazel", date(2026, 10, 5)),
        obligation("e_oak", date(2026, 10, 10), "Rent payment"),
        obligation("e_birch", date(2026, 10, 1), "Insurance renewal"),
        obligation("e_elsewhere", date(2026, 10, 4)),  # a company not on this Home
        obligation("e_oak", date(2026, 11, 30)),
    ]
    home = home_summary(statuses, names, today=today, obligations=obligations)
    assert home.headline == "I need 2 things from you."
    assert home.needs_you == 2 and home.due_soon_count == 3
    assert home.percent_closed == 25 * 100 // 27  # 25 of 27 items done -> 92
    assert home.month_line == "September 92% closed"
    assert home.counters == "Needs you 2 · Due soon 3 · September 92% closed"
    assert [c.line for c in home.companies] == [
        "Birch Café · Needs one answer",
        "Hazel Tree · Needs one answer",
        "Oak Studio · Closed",
    ]
    assert home.handled_for_you == ("25 transactions checked",)
    assert [d.line for d in home.due_soon] == [
        "Insurance renewal · 2 days late",
        "Tax payment · due in 2 days",
        "Rent payment · due in 7 days",
    ]


def test_home_headlines():
    names = {"e1": "One"}
    calm = home_summary([status_of("e1", 3)], names, today=date(2026, 10, 3))
    assert calm.headline == "Everything is under control."
    assert calm.month_line == "September is closed" and calm.percent_closed == 100
    one = home_summary([asked_status("e1", 1)], names, today=date(2026, 10, 3))
    assert one.headline == "I need one thing from you."
    risky = home_summary([status_of("e1", 3)], names, today=date(2026, 10, 3), risk_count=1)
    assert risky.headline == "Action required."


def test_home_percent_never_reaches_100_before_every_company_closes():
    statuses = [status_of("e1", 999), status_of("e2", 0, 1)]
    home = home_summary(statuses, {}, today=date(2026, 10, 3))
    assert home.percent_closed == 99
    assert {c.name for c in home.companies} == {"Your company"}


def test_home_summary_validation():
    with pytest.raises(ValueError):
        home_summary([], {}, today=date(2026, 10, 3))
    august = compute_month_status("e2", Month(2026, 8), [], now=NOW, connectors=[Covered()])
    with pytest.raises(ValueError):
        home_summary([status_of("e1", 1), august], {}, today=date(2026, 10, 3))
    with pytest.raises(ValueError):
        home_summary([status_of("e1", 1), status_of("e1", 2)], {}, today=date(2026, 10, 3))
    with pytest.raises(ValueError):
        home_summary([status_of("e1", 1)], {}, today=date(2026, 10, 3), risk_count=-1)


# =========================================================================== Free Business Audit (§60)


def test_audit_window_is_the_previous_90_days():
    start, end = audit_window(date(2026, 10, 1))
    assert end == date(2026, 9, 30)
    assert (end - start).days + 1 == AUDIT_DAYS
    with pytest.raises(ValueError):
        audit_window(date(2026, 10, 1), 0)


def test_business_audit_report():
    start, end = audit_window(date(2026, 10, 1))
    findings = BusinessAuditFindings(
        period_start=start,
        period_end=end,
        expenses=143,
        expenses_total=Decimal("12480.20"),
        documents_found=126,
        recurring_subscriptions=18,
        missing_evidence=11,
        other_company_items=1,
        increases=(PriceIncrease("Adobe", Decimal("24.59"), Decimal("29.99")),),
    )
    report = render_business_audit(findings)
    assert report.headline == "Here is what I found from 3 July to 30 September."
    assert report.lines == (
        "143 expenses, €12,480.20 in total",
        "126 documents found",
        "18 recurring subscriptions",
        "11 payments without a document",
        "1 subscription went up: Adobe €24.59 → €29.99",
        "1 payment looks like another company's",
    )
    assert report.call_to_action == "Let me manage this automatically."


def test_business_audit_validation():
    with pytest.raises(ValueError):
        PriceIncrease("Adobe", Decimal("29.99"), Decimal("24.59"))
    with pytest.raises(TypeError):
        BusinessAuditFindings(date(2026, 7, 1), date(2026, 9, 30), 1, 10.5, 0, 0, 0, 0)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        BusinessAuditFindings(date(2026, 9, 30), date(2026, 7, 1), 1, Decimal(1), 0, 0, 0, 0)
