"""Activation (§58), customer-success metrics (§59) and the Home summary (§35).

Targets below are the product targets written in docs/SPEC.md §58–59, not
regulatory figures. Rates are computed exactly (fractions) for the pass/fail
decision and displayed as percentages with one decimal, rounded towards the
*unfavourable* side so a metric never looks better than it is (§57).

Definitions (so every number can be traced):

* owner admin minutes — active owner time about the month, onboarding
  excluded, rounded up to whole minutes;
* zero-touch rate — of the items that are done (closed / not required with
  GREEN quality), the share the owner never acted on and was never asked about;
* auto-resolved missing documents — of the missing documents detected
  (distinct subjects), the share the system retrieved by itself;
* critical silent errors — verified values later proven wrong
  (``SILENT_ERROR_FOUND`` activities);
* unresolved month-end items — items not done over all items, from the month
  statuses;
* onboarding active time — all onboarding interactions, ever;
* accountant questions needing the owner — questions the owner had to answer,
  against a baseline when one is known.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone, tzinfo
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from enum import Enum
from fractions import Fraction

from backoffice.domain.lifecycle import TrackedItem
from backoffice.domain.models import Obligation

from ._text import MIDDLE_DOT, count_phrase, minutes_from_seconds, require_aware, require_count
from .activity import (
    Activity,
    ActivityKind,
    Actor,
    InteractionKind,
    OwnerInteraction,
    activities_for,
    distinct_subjects,
    interactions_for,
    owner_touched,
)
from .month import CloseSummary, ItemState, MonthStatus, classify_item
from .obligations import DEFAULT_DUE_SOON_DAYS, DueItem, due_soon
from .period import Month

__all__ = [
    "ACCOUNTANT_OWNER_SHARE_TARGET",
    "AUTO_RESOLVED_TARGET",
    "ONBOARDING_MINUTES_TARGET",
    "OWNER_MINUTES_TARGET",
    "TIME_TO_FIRST_VALUE_TARGET",
    "UNRESOLVED_TARGET",
    "ZERO_TOUCH_TARGET",
    "ActivationCondition",
    "ActivationReport",
    "ActivationSignals",
    "CompanyLine",
    "CustomerSuccessReport",
    "HomeSummary",
    "Metric",
    "MetricResult",
    "customer_success",
    "evaluate_activation",
    "home_summary",
]

# §58–59 product targets (docs/SPEC.md).
TIME_TO_FIRST_VALUE_TARGET = timedelta(minutes=5)  # < 5 minutes
OWNER_MINUTES_TARGET = 15  # < 15 minutes per month
ZERO_TOUCH_TARGET = Fraction(95, 100)  # > 95%
AUTO_RESOLVED_TARGET = Fraction(90, 100)  # > 90%
UNRESOLVED_TARGET = Fraction(1, 100)  # < 1%
ONBOARDING_MINUTES_TARGET = 10  # < 10 minutes
ACCOUNTANT_OWNER_SHARE_TARGET = Fraction(20, 100)  # < 20% of baseline


# =========================================================================== activation (§58)


class ActivationCondition(str, Enum):
    EMAIL_CONNECTED = "email_connected"
    BANK_CONNECTED = "bank_connected"
    HISTORICAL_SCAN_COMPLETE = "historical_scan_complete"
    DOCUMENT_FOUND = "document_found"
    TRANSACTION_AUTO_MATCHED = "transaction_auto_matched"
    TIME_SAVED_SEEN = "time_saved_seen"


_SIGNAL_FIELDS: dict[ActivationCondition, str] = {
    ActivationCondition.EMAIL_CONNECTED: "email_connected_at",
    ActivationCondition.BANK_CONNECTED: "bank_connected_at",
    ActivationCondition.HISTORICAL_SCAN_COMPLETE: "historical_scan_completed_at",
    ActivationCondition.DOCUMENT_FOUND: "first_document_found_at",
    ActivationCondition.TRANSACTION_AUTO_MATCHED: "first_auto_match_at",
    ActivationCondition.TIME_SAVED_SEEN: "time_saved_seen_at",
}
# The first moment the owner saw a concrete result.
_FIRST_VALUE = (
    ActivationCondition.DOCUMENT_FOUND,
    ActivationCondition.TRANSACTION_AUTO_MATCHED,
    ActivationCondition.TIME_SAVED_SEEN,
)


@dataclass(frozen=True, slots=True)
class ActivationSignals:
    """When each §58 activation event first happened (None = not yet)."""

    signed_up_at: datetime
    email_connected_at: datetime | None = None
    bank_connected_at: datetime | None = None
    historical_scan_completed_at: datetime | None = None
    first_document_found_at: datetime | None = None
    first_auto_match_at: datetime | None = None
    time_saved_seen_at: datetime | None = None

    def __post_init__(self) -> None:
        start = require_aware(self.signed_up_at, "signed_up_at")
        for name in _SIGNAL_FIELDS.values():
            value = getattr(self, name)
            if value is not None and require_aware(value, name) < start:
                raise ValueError(f"{name} is before sign-up")

    def at(self, condition: ActivationCondition) -> datetime | None:
        value: datetime | None = getattr(self, _SIGNAL_FIELDS[condition])
        return value


@dataclass(frozen=True, slots=True)
class ActivationReport:
    activated: bool
    met: tuple[ActivationCondition, ...]
    missing: tuple[ActivationCondition, ...]
    activated_at: datetime | None
    time_to_first_value: timedelta | None
    first_value_on_target: bool | None


def evaluate_activation(signals: ActivationSignals) -> ActivationReport:
    """All six §58 conditions, plus time to first value (target: under 5 minutes from sign-up)."""
    met = tuple(c for c in ActivationCondition if signals.at(c) is not None)
    missing = tuple(c for c in ActivationCondition if signals.at(c) is None)
    activated = not missing
    activated_at = max(signals.at(c) for c in met) if activated else None  # type: ignore[type-var]
    firsts = [t for c in _FIRST_VALUE if (t := signals.at(c)) is not None]
    ttfv = min(firsts) - signals.signed_up_at if firsts else None
    return ActivationReport(
        activated=activated,
        met=met,
        missing=missing,
        activated_at=activated_at,
        time_to_first_value=ttfv,
        first_value_on_target=None if ttfv is None else ttfv < TIME_TO_FIRST_VALUE_TARGET,
    )


# =========================================================================== customer success (§59)


class Metric(str, Enum):
    OWNER_ADMIN_MINUTES = "owner_admin_minutes"
    ZERO_TOUCH_RATE = "zero_touch_rate"
    AUTO_RESOLVED_MISSING_RATE = "auto_resolved_missing_rate"
    CRITICAL_SILENT_ERRORS = "critical_silent_errors"
    UNRESOLVED_MONTH_END_RATE = "unresolved_month_end_rate"
    ONBOARDING_ACTIVE_MINUTES = "onboarding_active_minutes"
    ACCOUNTANT_QUESTIONS_NEEDING_OWNER = "accountant_questions_needing_owner"


@dataclass(frozen=True, slots=True)
class MetricResult:
    """``value`` is minutes, a count, or a percentage (Decimal, one decimal). None = no data."""

    metric: Metric
    value: Decimal | int | None
    unit: str  # "minutes" | "percent" | "count"
    target: str
    on_target: bool | None
    numerator: int | None = None
    denominator: int | None = None


@dataclass(frozen=True, slots=True)
class CustomerSuccessReport:
    month: Month
    results: tuple[MetricResult, ...]

    def get(self, metric: Metric) -> MetricResult:
        return next(r for r in self.results if r.metric is metric)

    @property
    def on_target(self) -> bool:
        """Every metric with data meets its target."""
        return all(r.on_target for r in self.results if r.on_target is not None)


def _percent(ratio: Fraction, higher_is_better: bool) -> Decimal:
    """Percentage, one decimal, rounded towards the unfavourable side."""
    exact = Decimal(ratio.numerator * 100) / Decimal(ratio.denominator)
    rounding = ROUND_FLOOR if higher_is_better else ROUND_CEILING
    return exact.quantize(Decimal("0.1"), rounding=rounding)


def _rate(
    metric: Metric,
    numerator: int,
    denominator: int,
    target: Fraction,
    higher_is_better: bool,
    target_text: str,
) -> MetricResult:
    if denominator == 0:
        return MetricResult(metric, None, "percent", target_text, None, numerator, denominator)
    ratio = Fraction(numerator, denominator)
    ok = ratio > target if higher_is_better else ratio < target
    return MetricResult(
        metric, _percent(ratio, higher_is_better), "percent", target_text, ok, numerator, denominator
    )


def _owner_minutes(month: Month, interactions: Sequence[OwnerInteraction], tz: tzinfo) -> MetricResult:
    seconds = sum(i.active_seconds for i in interactions_for(interactions, month, tz))
    minutes = minutes_from_seconds(seconds)
    return MetricResult(
        Metric.OWNER_ADMIN_MINUTES, minutes, "minutes", f"< {OWNER_MINUTES_TARGET} minutes",
        minutes < OWNER_MINUTES_TARGET,
    )  # fmt: skip


def _zero_touch(items: Sequence[TrackedItem]) -> MetricResult:
    done = [i for i in items if classify_item(i) is ItemState.DONE]
    untouched = sum(1 for i in done if not owner_touched(i))
    return _rate(Metric.ZERO_TOUCH_RATE, untouched, len(done), ZERO_TOUCH_TARGET, True, "> 95%")


def _auto_resolved(acts: Sequence[Activity]) -> MetricResult:
    detected = {
        a.subject_id
        for a in acts
        if a.kind is ActivityKind.MISSING_DOCUMENT_DETECTED and a.subject_id is not None
    }
    automatic = {
        a.subject_id
        for a in acts
        if a.kind is ActivityKind.MISSING_DOCUMENT_RETRIEVED
        and a.actor is Actor.SYSTEM
        and a.subject_id in detected
    }
    return _rate(
        Metric.AUTO_RESOLVED_MISSING_RATE, len(automatic), len(detected), AUTO_RESOLVED_TARGET, True, "> 90%"
    )


def _silent_errors(acts: Sequence[Activity]) -> MetricResult:
    n = distinct_subjects(a for a in acts if a.kind is ActivityKind.SILENT_ERROR_FOUND)
    return MetricResult(Metric.CRITICAL_SILENT_ERRORS, n, "count", "0", n == 0)


def _unresolved(month: Month, statuses: Sequence[MonthStatus]) -> MetricResult:
    relevant = [s for s in statuses if s.month == month]
    total = sum(s.items_total for s in relevant)
    open_items = sum(s.items_total - s.items_done for s in relevant)
    return _rate(Metric.UNRESOLVED_MONTH_END_RATE, open_items, total, UNRESOLVED_TARGET, False, "< 1%")


def _onboarding(interactions: Sequence[OwnerInteraction]) -> MetricResult:
    onboarding = [i for i in interactions if i.kind is InteractionKind.ONBOARDING]
    target = f"< {ONBOARDING_MINUTES_TARGET} minutes"
    if not onboarding:
        return MetricResult(Metric.ONBOARDING_ACTIVE_MINUTES, None, "minutes", target, None)
    minutes = minutes_from_seconds(sum(i.active_seconds for i in onboarding))
    return MetricResult(
        Metric.ONBOARDING_ACTIVE_MINUTES, minutes, "minutes", target, minutes < ONBOARDING_MINUTES_TARGET
    )


def _accountant_owner(acts: Sequence[Activity], baseline: int | None) -> MetricResult:
    needed_owner = distinct_subjects(
        a for a in acts if a.kind is ActivityKind.ACCOUNTANT_QUESTION_RESOLVED and a.actor is Actor.OWNER
    )
    if baseline is None:
        return MetricResult(
            Metric.ACCOUNTANT_QUESTIONS_NEEDING_OWNER, needed_owner, "count", "< 20% of baseline", None,
            needed_owner, None,
        )  # fmt: skip
    return _rate(
        Metric.ACCOUNTANT_QUESTIONS_NEEDING_OWNER, needed_owner, require_count(baseline, "baseline"),
        ACCOUNTANT_OWNER_SHARE_TARGET, False, "< 20% of baseline",
    )  # fmt: skip


def customer_success(
    month: Month,
    *,
    items: Sequence[TrackedItem] = (),
    statuses: Sequence[MonthStatus] = (),
    activities: Sequence[Activity] = (),
    interactions: Sequence[OwnerInteraction] = (),
    accountant_question_baseline: int | None = None,
    tz: tzinfo = timezone.utc,
) -> CustomerSuccessReport:
    """§59 metrics for one account and month (all companies together).

    ``items`` are the month's tracked items across companies; ``statuses`` the
    month statuses (for unresolved month-end items). Activities and
    interactions may span months; they are filtered here (onboarding is not).
    """
    acts = activities_for(activities, month, tz)
    return CustomerSuccessReport(
        month=month,
        results=(
            _owner_minutes(month, interactions, tz),
            _zero_touch(items),
            _auto_resolved(acts),
            _silent_errors(acts),
            _unresolved(month, statuses),
            _onboarding(interactions),
            _accountant_owner(acts, accountant_question_baseline),
        ),
    )


# =========================================================================== Home (§35)


@dataclass(frozen=True, slots=True)
class CompanyLine:
    entity_id: str
    name: str
    status: str  # "Closed" / "On track" / "Needs one answer" / ...
    percent_closed: int

    @property
    def line(self) -> str:
        return f"{self.name}{MIDDLE_DOT}{self.status}"


@dataclass(frozen=True, slots=True)
class HomeSummary:
    headline: str  # "Everything is under control." / "I need 2 things from you." / "Action required."
    needs_you: int
    due_soon: tuple[DueItem, ...]
    month: Month
    percent_closed: int
    month_line: str  # "September 94% closed" / "September is closed"
    companies: tuple[CompanyLine, ...]
    handled_for_you: tuple[str, ...]

    @property
    def due_soon_count(self) -> int:
        return len(self.due_soon)

    @property
    def counters(self) -> str:
        """'Needs you 2 · Due soon 3 · September 94% closed'."""
        return MIDDLE_DOT.join(
            (f"Needs you {self.needs_you}", f"Due soon {self.due_soon_count}", self.month_line)
        )


def _headline(needs: int, risk: int) -> str:
    if risk:
        return "Action required."
    if needs == 1:
        return "I need one thing from you."
    if needs > 1:
        return f"I need {needs} things from you."
    return "Everything is under control."


def _combined_percent(statuses: Sequence[MonthStatus]) -> int:
    """Done share across companies, rounded down; 100 only when every company is closed.

    Each company contributes its own (possibly weighted) done share in proportion
    to its number of items, so one company's Home figure equals its own
    percentage, and unweighted companies reduce to done items / all items.
    """
    if all(s.closed for s in statuses):
        return 100
    total = sum(s.items_total for s in statuses)
    if total == 0:
        return 0
    done = sum((s.done_share * s.items_total for s in statuses), Fraction(0))
    return min(int(done * 100 / total), 99)


_HANDLED: tuple[tuple[str, str, Callable[[CloseSummary], int]], ...] = (
    ("transaction", "checked", lambda s: s.transactions_checked),
    ("document", "collected", lambda s: s.documents_collected),
    ("missing document", "retrieved automatically", lambda s: s.missing_documents_retrieved),
    ("supplier", "chased", lambda s: s.suppliers_chased),
    ("accountant question", "resolved", lambda s: s.accountant_questions_resolved),
    ("tax obligation", "verified", lambda s: s.tax_obligations_verified),
)


def _handled_for_you(statuses: Sequence[MonthStatus]) -> tuple[str, ...]:
    """Quiet success (§42): only the non-zero counts."""
    lines = []
    for noun, verb, pick in _HANDLED:
        n = sum(pick(s.summary) for s in statuses)
        if n:
            lines.append(f"{count_phrase(n, noun)} {verb}")
    return tuple(lines)


def home_summary(
    statuses: Sequence[MonthStatus],
    names: Mapping[str, str],
    *,
    today: date,
    obligations: Sequence[Obligation] = (),
    due_within_days: int = DEFAULT_DUE_SOON_DAYS,
    risk_count: int = 0,
) -> HomeSummary:
    """Home screen data (§34–35): needs you, due soon, month % closed, companies, handled for you.

    ``statuses`` are this month's statuses, one per company; ``names`` maps
    entity id to the company's display name. ``risk_count`` is real risk from
    the fraud checks (§26), which turns the headline into "Action required.".
    """
    if not statuses:
        raise ValueError("at least one company is required")
    months = {s.month for s in statuses}
    if len(months) != 1:
        raise ValueError("all statuses must be for the same month")
    if len({s.entity_id for s in statuses}) != len(statuses):
        raise ValueError("one status per company")
    require_count(risk_count, "risk_count")
    month = months.pop()
    shown = {s.entity_id for s in statuses}
    own_obligations = [o for o in obligations if o.entity_id in shown]
    needs = sum(s.needs_you for s in statuses)
    percent = _combined_percent(statuses)
    label = month.label(today)
    companies = sorted(
        (CompanyLine(s.entity_id, names.get(s.entity_id, "Your company"), s.company_status, s.percent_closed)
         for s in statuses),
        key=lambda c: (c.name.casefold(), c.entity_id),
    )  # fmt: skip
    return HomeSummary(
        headline=_headline(needs, risk_count),
        needs_you=needs,
        due_soon=tuple(due_soon(own_obligations, today, within_days=due_within_days)),
        month=month,
        percent_closed=percent,
        month_line=f"{label} is closed" if percent == 100 else f"{label} {percent}% closed",
        companies=tuple(companies),
        handled_for_you=_handled_for_you(statuses),
    )
