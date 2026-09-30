"""Month-end autopilot (§27): the dated close plan and how far along it is.

Processing is continuous; the plan only sets the latest day each step should
be finished, relative to the close day (Day 0)::

    Day −7  completeness audit
    Day −5  missing evidence retrieval
    Day −3  supplier chases
    Day  0  accountant package prepared
    Day +1  delivery confirmed (by the accountant, not just sent)
    Day +2  accountant queries handled
    Final   MONTH CLOSED

Steps have explicit dependencies. A step counts as done only when its own
completion check passes *and* everything it depends on is done, so the order
of the golden rule is kept even when work finishes early (§3). Days are
calendar days; weekends and public holidays are not moved (no holiday
calendar is encoded here).

The plan is pure data; a durable workflow (§45) waits on it and feeds
:class:`CloseProgress` snapshots to :func:`evaluate_schedule`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from enum import Enum

from ._text import count_phrase, day_month, require_count
from .month import MonthStatus
from .package import DeliveryState, PackageDelivery
from .period import Month

__all__ = [
    "STEP_OFFSETS",
    "CloseProgress",
    "CloseSchedule",
    "ScheduleStatus",
    "StepKind",
    "StepPlan",
    "StepState",
    "StepStatus",
    "evaluate_schedule",
    "plan_month_end",
]


class StepKind(str, Enum):
    COMPLETENESS_AUDIT = "completeness_audit"
    MISSING_EVIDENCE_RETRIEVAL = "missing_evidence_retrieval"
    SUPPLIER_CHASES = "supplier_chases"
    PACKAGE_PREPARED = "package_prepared"
    DELIVERY_CONFIRMED = "delivery_confirmed"
    ACCOUNTANT_QUERIES = "accountant_queries"
    MONTH_CLOSED = "month_closed"


# Offsets from the close day (§27). The final step has no day of its own; it
# is targeted for the last dated step and done only when the month is closed.
STEP_OFFSETS: dict[StepKind, int] = {
    StepKind.COMPLETENESS_AUDIT: -7,
    StepKind.MISSING_EVIDENCE_RETRIEVAL: -5,
    StepKind.SUPPLIER_CHASES: -3,
    StepKind.PACKAGE_PREPARED: 0,
    StepKind.DELIVERY_CONFIRMED: 1,
    StepKind.ACCOUNTANT_QUERIES: 2,
    StepKind.MONTH_CLOSED: 2,
}

_DEPENDS: dict[StepKind, tuple[StepKind, ...]] = {
    StepKind.COMPLETENESS_AUDIT: (),
    StepKind.MISSING_EVIDENCE_RETRIEVAL: (StepKind.COMPLETENESS_AUDIT,),
    StepKind.SUPPLIER_CHASES: (StepKind.MISSING_EVIDENCE_RETRIEVAL,),
    StepKind.PACKAGE_PREPARED: (StepKind.SUPPLIER_CHASES,),
    StepKind.DELIVERY_CONFIRMED: (StepKind.PACKAGE_PREPARED,),
    StepKind.ACCOUNTANT_QUERIES: (StepKind.DELIVERY_CONFIRMED,),
    StepKind.MONTH_CLOSED: (
        StepKind.COMPLETENESS_AUDIT,
        StepKind.MISSING_EVIDENCE_RETRIEVAL,
        StepKind.SUPPLIER_CHASES,
        StepKind.PACKAGE_PREPARED,
        StepKind.DELIVERY_CONFIRMED,
        StepKind.ACCOUNTANT_QUERIES,
    ),
}

_TITLES: dict[StepKind, str] = {
    StepKind.COMPLETENESS_AUDIT: "Check what's missing",
    StepKind.MISSING_EVIDENCE_RETRIEVAL: "Find missing documents",
    StepKind.SUPPLIER_CHASES: "Ask suppliers for what's still missing",
    StepKind.PACKAGE_PREPARED: "Prepare the package for your accountant",
    StepKind.DELIVERY_CONFIRMED: "Confirm your accountant has it",
    StepKind.ACCOUNTANT_QUERIES: "Answer your accountant's questions",
    StepKind.MONTH_CLOSED: "Close {month}",
}

_DONE_WHEN: dict[StepKind, str] = {
    StepKind.COMPLETENESS_AUDIT: "Every payment has been checked for the proof it needs.",
    StepKind.MISSING_EVIDENCE_RETRIEVAL: "Every missing document has been looked for in email, files and supplier sites.",
    StepKind.SUPPLIER_CHASES: "Every supplier with a missing document has been asked, or you have been.",
    StepKind.PACKAGE_PREPARED: "The month's package is ready.",
    StepKind.DELIVERY_CONFIRMED: "Your accountant has confirmed they received it.",
    StepKind.ACCOUNTANT_QUERIES: "Every question from your accountant has an answer.",
    StepKind.MONTH_CLOSED: "Everything is proven and nothing is left open.",
}


@dataclass(frozen=True, slots=True)
class StepPlan:
    """One dated step. Its window runs from ``starts_on`` (the previous step's day) to ``due_on``."""

    kind: StepKind
    offset: int  # days from the close day
    starts_on: date
    due_on: date
    title: str
    done_when: str
    depends_on: tuple[StepKind, ...]


@dataclass(frozen=True, slots=True)
class CloseSchedule:
    month: Month
    close_day: date
    steps: tuple[StepPlan, ...]

    def step(self, kind: StepKind) -> StepPlan:
        return next(s for s in self.steps if s.kind is kind)


def plan_month_end(month: Month, close_day: date) -> CloseSchedule:
    """Dated §27 plan for ``month`` with Day 0 on ``close_day`` (after the month ends)."""
    if close_day <= month.last_day:
        raise ValueError("the close day must be after the month ends")
    steps: list[StepPlan] = []
    for kind, offset in STEP_OFFSETS.items():
        due_on = close_day + timedelta(days=offset)
        steps.append(
            StepPlan(
                kind=kind,
                offset=offset,
                starts_on=steps[-1].due_on if steps else due_on,
                due_on=due_on,
                title=_TITLES[kind].format(month=month.name),
                done_when=_DONE_WHEN[kind],
                depends_on=_DEPENDS[kind],
            )
        )
    return CloseSchedule(month=month, close_day=close_day, steps=tuple(steps))


# --------------------------------------------------------------------------- progress


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class CloseProgress:
    """What the system knows right now. Counts are of missing *documents*.

    ``package_sha256`` is the hash of the month's latest package. When given,
    Day +1 only counts a confirmation of that exact package: if the package was
    rebuilt after it was sent (new evidence arrived), the old confirmation no
    longer proves the accountant has what they need.
    """

    audit_ran_on: date | None = None  # last completed completeness audit (local date)
    missing_documents: int = 0
    unsearched_missing: int = 0  # not yet searched in every automatic source (§22)
    unchased_missing: int = 0  # supplier not yet asked (and the owner not asked instead)
    delivery: PackageDelivery | None = None
    open_accountant_questions: int = 0
    month_status: MonthStatus | None = None
    package_sha256: str | None = None

    def __post_init__(self) -> None:
        for name in ("missing_documents", "unsearched_missing", "unchased_missing", "open_accountant_questions"):
            require_count(getattr(self, name), name)
        if self.unsearched_missing > self.missing_documents or self.unchased_missing > self.missing_documents:
            raise ValueError("cannot have more unsearched/unchased than missing documents")
        if self.package_sha256 is not None and not _SHA256.match(self.package_sha256):
            raise ValueError("package_sha256 must be a SHA-256 hex digest")


class StepState(str, Enum):
    DONE = "done"
    IN_PROGRESS = "in_progress"  # inside its window, dependencies done
    UPCOMING = "upcoming"  # its window has not started (it may still finish early)
    WAITING = "waiting"  # an earlier step is not done yet
    LATE = "late"  # past its day and not done


@dataclass(frozen=True, slots=True)
class StepStatus:
    plan: StepPlan
    state: StepState
    detail: str  # owner-facing; empty when done


@dataclass(frozen=True, slots=True)
class ScheduleStatus:
    month: Month
    today: date
    steps: tuple[StepStatus, ...]

    @property
    def closed(self) -> bool:
        return self.steps[-1].state is StepState.DONE

    @property
    def next_step(self) -> StepStatus | None:
        return next((s for s in self.steps if s.state is not StepState.DONE), None)

    @property
    def late(self) -> tuple[StepStatus, ...]:
        return tuple(s for s in self.steps if s.state is StepState.LATE)

    @property
    def headline(self) -> str:
        """'September is closed.' / 'Next: Find missing documents, by 28 September.'"""
        nxt = self.next_step
        if nxt is None:
            return f"{self.month.label(self.today)} is closed."
        return f"Next: {nxt.plan.title}, by {day_month(nxt.plan.due_on, self.today)}."


def _check(kind: StepKind, plan: StepPlan, p: CloseProgress, month: Month) -> tuple[bool, str]:
    """(passes, owner-facing detail when it does not)."""
    delivery = p.delivery if p.delivery is not None and p.delivery.month == month else None
    if kind is StepKind.COMPLETENESS_AUDIT:
        ok = p.audit_ran_on is not None and p.audit_ran_on >= plan.due_on
        return ok, "The check hasn't run yet."
    if kind is StepKind.MISSING_EVIDENCE_RETRIEVAL:
        n = p.unsearched_missing
        return n == 0, f"{count_phrase(n, 'document')} still to look for."
    if kind is StepKind.SUPPLIER_CHASES:
        n = p.unchased_missing
        return n == 0, f"{count_phrase(n, 'missing document')} still to ask for."
    if kind is StepKind.PACKAGE_PREPARED:
        return delivery is not None or p.package_sha256 is not None, "Not prepared yet."
    if kind is StepKind.DELIVERY_CONFIRMED:
        if delivery is not None and p.package_sha256 not in (None, delivery.package_sha256):
            return False, "The package changed after it was sent."
        return _delivery_check(delivery)
    if kind is StepKind.ACCOUNTANT_QUERIES:
        n = p.open_accountant_questions
        return n == 0, f"{count_phrase(n, 'question')} still open."
    status = p.month_status
    if status is None or status.month != month:
        return False, "Not closed yet."
    reasons = status.reasons()
    return status.closed, reasons[0] if reasons else "Not closed yet."


def _delivery_check(delivery: PackageDelivery | None) -> tuple[bool, str]:
    if delivery is None or delivery.state is DeliveryState.PREPARED:
        return False, "Not sent yet."
    if delivery.state is DeliveryState.DELIVERED:
        return False, "Sent. Waiting for your accountant to confirm."
    return True, ""


def evaluate_schedule(schedule: CloseSchedule, progress: CloseProgress, today: date) -> ScheduleStatus:
    """State of every step on ``today``. Deterministic: same inputs, same answer."""
    done: set[StepKind] = set()
    statuses = []
    for plan in schedule.steps:
        passes, detail = _check(plan.kind, plan, progress, schedule.month)
        deps_done = all(d in done for d in plan.depends_on)
        if passes and deps_done:
            done.add(plan.kind)
            statuses.append(StepStatus(plan, StepState.DONE, ""))
            continue
        if today > plan.due_on:
            state = StepState.LATE
        elif not deps_done:
            state = StepState.WAITING
        elif today < plan.starts_on:
            state = StepState.UPCOMING
        else:
            state = StepState.IN_PROGRESS
        if not deps_done and passes:
            detail = "Waiting for an earlier step."
        statuses.append(StepStatus(plan, state, detail))
    return ScheduleStatus(month=schedule.month, today=today, steps=tuple(statuses))
