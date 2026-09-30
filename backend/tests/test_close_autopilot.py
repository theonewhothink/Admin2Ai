"""Month-end autopilot (§27): dated plan, dependencies and completion checks."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone

import pytest

from backoffice.closure import (
    STEP_OFFSETS,
    CloseProgress,
    DeliveryChannel,
    Month,
    PackageDelivery,
    StepKind,
    StepState,
    compute_month_status,
    evaluate_schedule,
    plan_month_end,
)
from backoffice.domain.lifecycle import ORDER, TrackedItem
from backoffice.domain.models import Quality

SEPT = Month(2026, 9)
CLOSE_DAY = date(2026, 10, 5)
SHA = "a" * 64
T0 = datetime(2026, 10, 5, 9, 0, tzinfo=timezone.utc)


class Covered:
    name = "Gmail"
    healthy = True
    covered_from = datetime(2026, 1, 1, tzinfo=timezone.utc)
    covered_until = datetime(2026, 10, 5, tzinfo=timezone.utc)


def closed_item() -> TrackedItem:
    item = TrackedItem(tenant_id="t1", subject_type="transaction", subject_id="tx_1")
    for stage in ORDER[1:]:
        item.advance(stage, actor="system", evidence_ids=["ev"], quality=Quality.GREEN)
    return item


def month_status(closed: bool = True, month: Month = SEPT):
    items = [closed_item()] if closed else [TrackedItem(tenant_id="t1", subject_type="transaction", subject_id="tx_9")]
    return compute_month_status("ent_1", month, items, now=datetime(2026, 10, 7, 12, tzinfo=timezone.utc),
                                connectors=[Covered()])  # fmt: skip


def delivery(state: str = "confirmed", month: Month = SEPT) -> PackageDelivery:
    d = PackageDelivery(entity_id="ent_1", month=month, package_sha256=SHA, prepared_at=T0)
    if state in ("delivered", "confirmed"):
        d = d.deliver(at=T0, channel=DeliveryChannel.EMAIL, recipient="contabilista@example.pt", evidence_id="ev_mail")
    if state == "confirmed":
        d = d.confirm(at=datetime(2026, 10, 6, 10, tzinfo=timezone.utc), evidence_id="ev_reply")
    return d


def full_progress() -> CloseProgress:
    return CloseProgress(
        audit_ran_on=date(2026, 9, 28),
        delivery=delivery(),
        month_status=month_status(),
    )


# --------------------------------------------------------------------------- plan


def test_plan_dates_follow_section_27():
    plan = plan_month_end(SEPT, CLOSE_DAY)
    assert [(s.kind, s.due_on) for s in plan.steps] == [
        (StepKind.COMPLETENESS_AUDIT, date(2026, 9, 28)),
        (StepKind.MISSING_EVIDENCE_RETRIEVAL, date(2026, 9, 30)),
        (StepKind.SUPPLIER_CHASES, date(2026, 10, 2)),
        (StepKind.PACKAGE_PREPARED, date(2026, 10, 5)),
        (StepKind.DELIVERY_CONFIRMED, date(2026, 10, 6)),
        (StepKind.ACCOUNTANT_QUERIES, date(2026, 10, 7)),
        (StepKind.MONTH_CLOSED, date(2026, 10, 7)),
    ]
    assert [s.offset for s in plan.steps] == [-7, -5, -3, 0, 1, 2, 2]
    assert list(STEP_OFFSETS) == [s.kind for s in plan.steps]
    assert plan.step(StepKind.MONTH_CLOSED).title == "Close September"


def test_each_step_depends_on_the_one_before_and_the_final_on_all():
    plan = plan_month_end(SEPT, CLOSE_DAY)
    kinds = [s.kind for s in plan.steps]
    for i, step in enumerate(plan.steps[1:-1], start=1):
        assert step.depends_on == (kinds[i - 1],)
        assert step.starts_on == plan.steps[i - 1].due_on
    assert plan.steps[0].depends_on == ()
    assert plan.steps[-1].depends_on == tuple(kinds[:-1])


def test_close_day_must_be_after_the_month():
    with pytest.raises(ValueError):
        plan_month_end(SEPT, date(2026, 9, 30))


def test_plan_wording_is_plain():
    for step in plan_month_end(SEPT, CLOSE_DAY).steps:
        assert step.title and step.done_when.endswith(".")
        assert "reconcil" not in (step.title + step.done_when).lower()


# --------------------------------------------------------------------------- evaluation


def test_nothing_done_before_the_audit_day():
    status = evaluate_schedule(plan_month_end(SEPT, CLOSE_DAY), CloseProgress(), date(2026, 9, 20))
    assert [s.state for s in status.steps] == [StepState.UPCOMING] + [StepState.WAITING] * 6
    assert status.headline == "Next: Check what's missing, by 28 September."
    assert not status.closed


def test_everything_done_closes_the_month():
    status = evaluate_schedule(plan_month_end(SEPT, CLOSE_DAY), full_progress(), date(2026, 10, 7))
    assert all(s.state is StepState.DONE for s in status.steps)
    assert status.closed and status.next_step is None
    assert status.headline == "September is closed."


def test_an_audit_from_before_its_day_is_stale():
    progress = replace(full_progress(), audit_ran_on=date(2026, 9, 20))
    status = evaluate_schedule(plan_month_end(SEPT, CLOSE_DAY), progress, date(2026, 9, 28))
    first = status.steps[0]
    assert first.state is StepState.IN_PROGRESS and first.detail == "The check hasn't run yet."
    # Later checks pass on their own but wait for the audit: order is kept (§3).
    assert status.steps[1].state is StepState.WAITING
    assert status.steps[1].detail == "Waiting for an earlier step."


def test_retrieval_and_chases_report_what_is_left():
    progress = CloseProgress(audit_ran_on=date(2026, 9, 28), missing_documents=4, unsearched_missing=3,
                             unchased_missing=1)  # fmt: skip
    status = evaluate_schedule(plan_month_end(SEPT, CLOSE_DAY), progress, date(2026, 9, 29))
    audit, retrieval, chases = status.steps[:3]
    assert audit.state is StepState.DONE
    assert retrieval.state is StepState.IN_PROGRESS and retrieval.detail == "3 documents still to look for."
    assert chases.state is StepState.WAITING and chases.detail == "1 missing document still to ask for."


def test_sent_is_not_confirmed():
    progress = replace(full_progress(), delivery=delivery("delivered"))
    status = evaluate_schedule(plan_month_end(SEPT, CLOSE_DAY), progress, date(2026, 10, 6))
    step = next(s for s in status.steps if s.plan.kind is StepKind.DELIVERY_CONFIRMED)
    assert step.state is StepState.IN_PROGRESS
    assert step.detail == "Sent. Waiting for your accountant to confirm."
    assert status.next_step is step


def test_prepared_but_not_sent_and_missing_package():
    plan = plan_month_end(SEPT, CLOSE_DAY)
    prepared = evaluate_schedule(plan, replace(full_progress(), delivery=delivery("prepared")), date(2026, 10, 6))
    assert prepared.next_step is not None and prepared.next_step.detail == "Not sent yet."
    none = evaluate_schedule(plan, replace(full_progress(), delivery=None), date(2026, 10, 5))
    assert none.next_step is not None and none.next_step.plan.kind is StepKind.PACKAGE_PREPARED
    assert none.next_step.detail == "Not prepared yet."


def test_steps_past_their_day_are_late():
    progress = replace(full_progress(), open_accountant_questions=2)
    status = evaluate_schedule(plan_month_end(SEPT, CLOSE_DAY), progress, date(2026, 10, 9))
    assert [s.plan.kind for s in status.late] == [StepKind.ACCOUNTANT_QUERIES, StepKind.MONTH_CLOSED]
    assert status.late[0].detail == "2 questions still open."


def test_final_step_needs_the_month_status_of_this_month_to_be_closed():
    plan = plan_month_end(SEPT, CLOSE_DAY)
    open_month = evaluate_schedule(plan, replace(full_progress(), month_status=month_status(closed=False)),
                                   date(2026, 10, 7))  # fmt: skip
    assert not open_month.closed
    assert open_month.steps[-1].detail == "I'm still checking 1 payment."
    other = evaluate_schedule(plan, replace(full_progress(), month_status=month_status(month=Month(2026, 8))),
                              date(2026, 10, 7))  # fmt: skip
    assert not other.closed and other.steps[-1].detail == "Not closed yet."


def test_a_delivery_for_another_month_does_not_count():
    progress = replace(full_progress(), delivery=delivery(month=Month(2026, 8)))
    status = evaluate_schedule(plan_month_end(SEPT, CLOSE_DAY), progress, date(2026, 10, 7))
    assert status.next_step is not None and status.next_step.plan.kind is StepKind.PACKAGE_PREPARED


def test_progress_counts_are_validated():
    with pytest.raises(ValueError):
        CloseProgress(missing_documents=1, unsearched_missing=2)
    with pytest.raises(ValueError):
        CloseProgress(open_accountant_questions=-1)
    with pytest.raises(TypeError):
        CloseProgress(missing_documents=1.0)  # type: ignore[arg-type]


def test_evaluation_is_deterministic():
    plan = plan_month_end(SEPT, CLOSE_DAY)
    assert evaluate_schedule(plan, full_progress(), date(2026, 10, 1)) == evaluate_schedule(
        plan, full_progress(), date(2026, 10, 1)
    )
