"""Recurring expectations (§23), learned from history (§6, §60 "increased subscriptions").

From dated occurrences (bank/card transactions, or invoice arrivals) of one
counterparty this module learns:

* the cadence (weekly / monthly / quarterly / annual), with tolerance for
  month lengths, weekends and an occasional skipped period. Up to a quarter
  of the arrival days may be *off-cycle* extras (a one-off equipment invoice):
  they are counted in ``off_cycle`` and never shape the rhythm, the due window
  or the typical amount. Several documents on one day are one arrival;
* the anchor ("Vodafone around the 24th") and the grace window ("by the
  26th"), measured as the latest arrival relative to the anchor;
* the typical amount (median), its spread (MAD) and range;
* a price change between two stable price levels ("Adobe went from €24.59 to
  €27.06.");
* the next expected date and, given today, a plain overdue notice ("Vodafone
  normally issues an invoice by the 26th. Today is the 29th. Invoice missing.").

A series is only *trusted* (GREEN) once it has the minimum number of
observations for its cadence (plus one more for every off-cycle extra that
was set aside), most gaps fit the cadence and only the occasional period was
skipped. Gaps fit by length in days (within the cadence tolerance) or by
anchor periods (26 Feb and 22 Mar are one month apart around the 24th). Untrusted series
are returned as AMBER for display but never produce an overdue notice, so a
thin pattern never triggers chasing (§21, §57).
"""

from __future__ import annotations

import calendar
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from enum import Enum
from itertools import pairwise

from pydantic import BaseModel, ConfigDict

from backoffice.domain.models import Document, DocumentType, Quality, Transaction, TransactionKind

from .keys import counterparty_key, display_name, normalize_tax_id
from .plain import day_month, format_money, ordinal
from .stats import mad, median

__all__ = [
    "CADENCE_SPECS",
    "MAX_OFF_CYCLE_SHARE",
    "MIN_FIT_RATIO",
    "Basis",
    "Cadence",
    "CadenceSpec",
    "Direction",
    "ExpectedWindow",
    "Occurrence",
    "OverdueNotice",
    "PriceChange",
    "RecurringSeries",
    "check_overdue",
    "detect_price_change",
    "learn_from_documents",
    "learn_from_transactions",
    "learn_series",
    "next_expected",
]


class Cadence(str, Enum):
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    ANNUAL = "annual"


class Basis(str, Enum):
    PAYMENTS = "payments"  # bank or card transactions
    INVOICES = "invoices"  # invoice arrival (or issue) dates


class Direction(str, Enum):
    OUT = "out"
    IN = "in"


@dataclass(frozen=True)
class CadenceSpec:
    nominal_days: float  # average period length
    tolerance_days: int  # accepted distance of a gap from a whole number of periods
    months: int  # calendar months per period (0 for weekly)
    min_observations: int  # before the series is trusted
    ended_after_missed: int  # missed periods before we say "it may have stopped"
    phrase: str  # owner-facing rhythm


# Tolerances absorb month lengths (28-31 days) plus a few days of drift either side.
CADENCE_SPECS: dict[Cadence, CadenceSpec] = {
    Cadence.WEEKLY: CadenceSpec(7.0, 1, 0, 4, 3, "every week"),
    Cadence.MONTHLY: CadenceSpec(30.4375, 6, 1, 3, 3, "every month"),
    Cadence.QUARTERLY: CadenceSpec(91.3125, 12, 3, 3, 2, "every 3 months"),
    Cadence.ANNUAL: CadenceSpec(365.25, 20, 12, 2, 2, "every year"),
}
MIN_FIT_RATIO = Decimal("0.75")  # share of gaps that must fit the cadence
MAX_OFF_CYCLE_SHARE = Decimal("0.25")  # arrival days allowed off the rhythm (one-off extras)
PRICE_STABLE_TOLERANCE = Decimal("0.005")  # 0.5%: FX-billed subscriptions wobble a little
PRICE_MIN_CHANGE = Decimal("0.01")  # 1%: smaller moves are noise, not a price change


@dataclass(frozen=True)
class Occurrence:
    """One dated event of a series. ``amount`` is absolute; direction lives on the series."""

    on: date
    amount: Decimal | None = None
    currency: str = "EUR"
    label: str | None = None  # the raw counterparty name as seen
    ref: str | None = None  # transaction/document id (internal, never shown)

    def __post_init__(self) -> None:
        if isinstance(self.amount, float):
            raise TypeError("money must be Decimal, never float")


class PriceChange(BaseModel):
    model_config = ConfigDict(frozen=True)

    old_amount: Decimal
    new_amount: Decimal
    currency: str
    since: date
    message: str

    @property
    def increased(self) -> bool:
        return self.new_amount > self.old_amount

    @property
    def difference(self) -> Decimal:
        return self.new_amount - self.old_amount


class RecurringSeries(BaseModel):
    model_config = ConfigDict(frozen=True)

    key: str
    display_name: str
    basis: Basis
    direction: Direction
    cadence: Cadence
    observations: int
    first_seen: date
    last_seen: date
    regularity: Decimal  # share of gaps that fit the cadence, 0..1
    missed_in_history: int  # whole periods skipped between observations
    trusted: bool
    off_cycle: int = 0  # arrival days ignored as one-off extras
    anchor: int  # day of month (monthly and longer) or weekday 0=Monday (weekly)
    early_days: int  # earliest arrival relative to the anchor (<= 0)
    grace_days: int  # latest arrival relative to the anchor (>= 0)
    currency: str | None = None
    typical_amount: Decimal | None = None
    amount_mad: Decimal | None = None
    amount_min: Decimal | None = None
    amount_max: Decimal | None = None
    fixed_amount: bool = False
    price_change: PriceChange | None = None

    @property
    def quality(self) -> Quality:
        """GREEN once trusted; a thin or irregular pattern stays AMBER (§57)."""
        return Quality.GREEN if self.trusted else Quality.AMBER

    @property
    def spec(self) -> CadenceSpec:
        return CADENCE_SPECS[self.cadence]


class ExpectedWindow(BaseModel):
    model_config = ConfigDict(frozen=True)

    earliest: date
    expected: date  # the anchor date of the period
    due: date  # latest date it has historically arrived by


class OverdueNotice(BaseModel):
    model_config = ConfigDict(frozen=True)

    key: str
    display_name: str
    basis: Basis
    due_on: date  # due date of the first missed period
    missed_periods: int
    likely_ended: bool
    message: str


# --------------------------------------------------------------------------- calendar helpers


def _month_shift(year: int, month: int, delta: int) -> tuple[int, int]:
    index = year * 12 + (month - 1) + delta
    return index // 12, index % 12 + 1


def _on_day(year: int, month: int, day: int) -> date:
    """``day`` of the month, clipped to the month's length (31 -> 30 September)."""
    return date(year, month, min(day, calendar.monthrange(year, month)[1]))


def _anchor_date(value: date, cadence: Cadence, anchor: int) -> date:
    """The anchor date nearest to ``value`` (ties go to the earlier date)."""
    if cadence is Cadence.WEEKLY:
        return value + timedelta(days=((anchor - value.weekday() + 3) % 7) - 3)
    candidates = [
        _on_day(*_month_shift(value.year, value.month, k), anchor) for k in (-1, 0, 1)
    ]
    return min(candidates, key=lambda c: (abs((value - c).days), c))


def _step(anchor_date: date, cadence: Cadence, anchor: int, periods: int = 1) -> date:
    if cadence is Cadence.WEEKLY:
        return anchor_date + timedelta(days=7 * periods)
    months = CADENCE_SPECS[cadence].months * periods
    return _on_day(*_month_shift(anchor_date.year, anchor_date.month, months), anchor)


def _best_anchor(dates: Sequence[date], cadence: Cadence) -> int:
    """Anchor minimizing total distance to the observations (a circular median)."""
    candidates = range(7) if cadence is Cadence.WEEKLY else range(1, 32)

    def cost(candidate: int) -> int:
        return sum(abs((d - _anchor_date(d, cadence, candidate)).days) for d in dates)

    return min(candidates, key=lambda c: (cost(c), c))


# --------------------------------------------------------------------------- cadence detection


@dataclass(frozen=True)
class _Fit:
    cadence: Cadence
    ratio: Decimal
    missed: int


def _whole_periods(gap: int, units: int, spec: CadenceSpec) -> int | None:
    """Periods a gap spans, or None when it fits no whole number of periods.

    ``gap`` is in days; ``units`` is the distance between the two days' anchor
    periods (weeks for weekly, months otherwise). A gap fits when its length in
    days is within the tolerance of k periods, or when both days sit exactly k
    anchor periods apart (month lengths plus weekend drift on both ends can
    push a genuine monthly gap outside the day tolerance: 26 Feb -> 22 Mar).
    """
    k = round(gap / spec.nominal_days)
    if k >= 1 and abs(gap - k * spec.nominal_days) <= spec.tolerance_days:
        return k
    per = spec.months or 1
    if units >= per and units % per == 0:
        return units // per
    return None


def _anchor_units(value: date, cadence: Cadence, anchor: int) -> int:
    """Index of the anchor period a day belongs to (weeks for weekly, months otherwise)."""
    anchored = _anchor_date(value, cadence, anchor)
    if cadence is Cadence.WEEKLY:
        return anchored.toordinal() // 7
    return anchored.year * 12 + anchored.month - 1


def _fit(days: Sequence[date], cadence: Cadence, anchor: int) -> _Fit | None:
    spec = CADENCE_SPECS[cadence]
    gaps = list(pairwise(days))
    periods: list[int] = []
    for a, b in gaps:
        units = _anchor_units(b, cadence, anchor) - _anchor_units(a, cadence, anchor)
        k = _whole_periods((b - a).days, units, spec)
        if k is not None:
            periods.append(k)
    if not periods:
        return None
    ratio = Decimal(len(periods)) / Decimal(len(gaps))
    singles = sum(1 for k in periods if k == 1)
    # Most fitted gaps must be one period: a quarterly series is not "monthly, skipping two".
    if ratio < MIN_FIT_RATIO or singles * 2 <= len(periods):
        return None
    return _Fit(cadence, ratio, sum(k - 1 for k in periods))


def _offset(value: date, cadence: Cadence, anchor: int) -> int:
    return (value - _anchor_date(value, cadence, anchor)).days


def _missed_budget(observations: int) -> int:
    """Skipped periods tolerated in history: "an occasional skipped period" (§23)."""
    return max(1, observations // 4)


def _one_per_period(days: Sequence[date], cadence: Cadence, anchor: int) -> list[date]:
    """Keep the day closest to the anchor in each anchor period (ties: the earlier day)."""
    best: dict[int, date] = {}
    for day in days:
        period = _anchor_units(day, cadence, anchor)
        current = best.get(period)
        if current is None or (abs(_offset(day, cadence, anchor)), day) < (abs(_offset(current, cadence, anchor)), current):
            best[period] = day
    return sorted(best.values())


def _fit_series(dates: Sequence[date]) -> tuple[_Fit, int, list[date]] | None:
    """Cadence, anchor and on-cycle days of distinct, ordered arrival days (shortest cadence first).

    A day is on-cycle when it lies within the cadence tolerance of the anchor,
    and each anchor period keeps one on-cycle day. Everything else is an
    off-cycle extra: at most ``MAX_OFF_CYCLE_SHARE`` of the days. The on-cycle
    days alone must fit the cadence, skipping only the occasional period.
    """
    if len(dates) < 2:
        return None
    for cadence in Cadence:  # shortest first; each gap fits at most one plausible rhythm
        spec = CADENCE_SPECS[cadence]
        anchor = _best_anchor(dates, cadence)
        near = [d for d in dates if abs(_offset(d, cadence, anchor)) <= spec.tolerance_days]
        on_cycle = _one_per_period(near, cadence, anchor)
        extras = len(dates) - len(on_cycle)
        if len(on_cycle) < 2 or Decimal(extras) > MAX_OFF_CYCLE_SHARE * len(dates):
            continue
        fit = _fit(on_cycle, cadence, anchor)
        if fit is not None and fit.missed <= _missed_budget(len(on_cycle)):
            return fit, anchor, on_cycle
    return None


# --------------------------------------------------------------------------- amounts


def _group_runs(amounts: Sequence[tuple[date, Decimal]]) -> list[list[tuple[date, Decimal]]]:
    runs: list[list[tuple[date, Decimal]]] = []
    for on, amount in amounts:
        if runs:
            reference = runs[-1][0][1]
            if abs(amount - reference) <= abs(reference) * PRICE_STABLE_TOLERANCE:
                runs[-1].append((on, amount))
                continue
        runs.append([(on, amount)])
    return runs


def detect_price_change(
    amounts: Sequence[tuple[date, Decimal]], currency: str, name: str
) -> PriceChange | None:
    """Latest move between two stable price levels ("Adobe went from €24.59 to €27.06.").

    The earlier level must have been seen at least twice, so a one-off charge
    in a usage-based series is not mistaken for a new price. ``amounts`` must be
    in date order.
    """
    runs = _group_runs(amounts)
    if len(runs) < 2 or len(runs[-2]) < 2:
        return None
    old = median([a for _, a in runs[-2]])
    new = median([a for _, a in runs[-1]])
    if old <= 0 or abs(new - old) / old < PRICE_MIN_CHANGE:
        return None
    message = f"{name} went from {format_money(old, currency)} to {format_money(new, currency)}."
    return PriceChange(
        old_amount=old, new_amount=new, currency=currency, since=runs[-1][0][0], message=message
    )


@dataclass(frozen=True)
class _AmountProfile:
    currency: str | None = None
    typical: Decimal | None = None
    spread: Decimal | None = None
    low: Decimal | None = None
    high: Decimal | None = None
    fixed: bool = False
    change: PriceChange | None = None


def _amount_profile(occurrences: Sequence[Occurrence], name: str) -> _AmountProfile:
    priced = [o for o in occurrences if o.amount is not None]
    if not priced:
        return _AmountProfile()
    counts = Counter(o.currency for o in priced)
    currency = min(counts, key=lambda c: (-counts[c], c))  # majority currency, stable tie-break
    series = [(o.on, o.amount) for o in priced if o.currency == currency and o.amount is not None]
    values = [a for _, a in series]
    change = detect_price_change(series, currency, name)
    regime = [a for on, a in series if change is None or on >= change.since]
    return _AmountProfile(
        currency=currency,
        typical=median(regime),
        spread=mad(values),
        low=min(values),
        high=max(values),
        fixed=len(set(regime)) == 1,
        change=change,
    )


# --------------------------------------------------------------------------- learning


def _dedupe(occurrences: Iterable[Occurrence]) -> list[Occurrence]:
    """Date order; the same event seen twice (same day, same amount) counts once."""
    seen: set[tuple[date, Decimal | None, str]] = set()
    unique: list[Occurrence] = []
    for occ in sorted(occurrences, key=lambda o: (o.on, o.amount is None, o.amount or 0, o.ref or "")):
        marker = (occ.on, occ.amount, occ.currency)
        if marker in seen:
            continue
        seen.add(marker)
        unique.append(occ)
    return unique


def _series_name(occurrences: Sequence[Occurrence], key: str, given: str | None) -> str:
    if given:
        return given
    labels = Counter(o.label for o in occurrences if o.label)
    if labels:
        first_seen = {o.label: i for i, o in reversed(list(enumerate(occurrences))) if o.label}
        best = min(labels, key=lambda lbl: (-labels[lbl], first_seen[lbl]))
        return display_name(best)
    return display_name(key)


def learn_series(
    key: str,
    occurrences: Iterable[Occurrence],
    *,
    basis: Basis,
    direction: Direction = Direction.OUT,
    name: str | None = None,
) -> RecurringSeries | None:
    """Learn one series, or None when there is no clear rhythm (§23).

    Returns an untrusted (AMBER) series when the rhythm is clear but the
    history is shorter than the cadence's minimum.
    """
    unique = _dedupe(occurrences)
    fitted = _fit_series(sorted({o.on for o in unique}))
    if fitted is None:
        return None
    fit, anchor, days = fitted
    spec = CADENCE_SPECS[fit.cadence]
    off_cycle = len({o.on for o in unique}) - len(days)
    cycle_days = set(days)
    in_cycle = [o for o in unique if o.on in cycle_days]
    offsets = [_offset(d, fit.cadence, anchor) for d in days]
    label = _series_name(unique, key, name)
    amounts = _amount_profile(in_cycle, label)
    return RecurringSeries(
        key=key,
        display_name=label,
        basis=basis,
        direction=direction,
        cadence=fit.cadence,
        observations=len(days),
        first_seen=days[0],
        last_seen=days[-1],
        regularity=fit.ratio,
        missed_in_history=fit.missed,
        # Dropping extras is itself a judgement: each one costs a regular observation.
        trusted=len(days) >= spec.min_observations + off_cycle,
        off_cycle=off_cycle,
        anchor=anchor,
        early_days=min(0, min(offsets)),
        grace_days=max(0, max(offsets)),
        currency=amounts.currency,
        typical_amount=amounts.typical,
        amount_mad=amounts.spread,
        amount_min=amounts.low,
        amount_max=amounts.high,
        fixed_amount=amounts.fixed,
        price_change=amounts.change,
    )


KeyFunction = Callable[[str], "str | None"]


def learn_from_transactions(
    transactions: Iterable[Transaction],
    *,
    key: KeyFunction = counterparty_key,
    names: Mapping[str, str] | None = None,
) -> list[RecurringSeries]:
    """Series of payments per counterparty and direction (internal transfers excluded)."""
    groups: dict[tuple[str, Direction], list[Occurrence]] = defaultdict(list)
    for tx in transactions:
        if tx.kind is TransactionKind.INTERNAL or tx.amount == 0:
            continue
        series_key = key(tx.counterparty)
        if series_key is None:
            continue
        direction = Direction.OUT if tx.amount < 0 else Direction.IN
        groups[(series_key, direction)].append(
            Occurrence(on=tx.booked_on, amount=abs(tx.amount), currency=tx.currency,
                       label=tx.counterparty, ref=tx.id)
        )  # fmt: skip
    return _learn_groups(groups, Basis.PAYMENTS, names)


def learn_from_documents(
    documents: Iterable[Document],
    *,
    arrived_on: Mapping[str, date] | None = None,
    key: KeyFunction = counterparty_key,
    names: Mapping[str, str] | None = None,
) -> list[RecurringSeries]:
    """Series of invoice arrivals per supplier.

    ``arrived_on`` maps document id -> the day it reached the business (email
    date, portal date); the issue date is used when it is missing. Credit notes,
    and "invoices" with a negative total (credits in disguise), are not part of
    a supplier's invoicing rhythm.
    """
    arrivals = arrived_on or {}
    groups: dict[tuple[str, Direction], list[Occurrence]] = defaultdict(list)
    for doc in documents:
        if doc.doc_type is DocumentType.CREDIT_NOTE or (doc.signed_gross is not None and doc.signed_gross < 0):
            continue
        on = arrivals.get(doc.id) or doc.issue_date
        series_key = key(doc.supplier_name) if doc.supplier_name else None
        series_key = series_key or normalize_tax_id(doc.supplier_tax_id)
        if on is None or series_key is None:
            continue
        groups[(series_key, Direction.OUT)].append(
            Occurrence(on=on, amount=doc.gross_amount, currency=doc.currency,
                       label=doc.supplier_name, ref=doc.id)
        )  # fmt: skip
    return _learn_groups(groups, Basis.INVOICES, names)


def _learn_groups(
    groups: Mapping[tuple[str, Direction], list[Occurrence]],
    basis: Basis,
    names: Mapping[str, str] | None,
) -> list[RecurringSeries]:
    learned: list[RecurringSeries] = []
    for (series_key, direction), occurrences in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1].value)):
        series = learn_series(
            series_key, occurrences, basis=basis, direction=direction,
            name=(names or {}).get(series_key),
        )  # fmt: skip
        if series is not None:
            learned.append(series)
    return learned


# --------------------------------------------------------------------------- expectations


def _next_period(series: RecurringSeries, arrivals: Iterable[date]) -> date:
    """Anchor date of the first period not yet covered by history or ``arrivals``.

    An arrival covers the latest period whose window (anchor minus the cadence
    tolerance) has opened by then, so a late invoice still covers its own
    period. An arrival before the open period's window is an off-cycle extra or
    belongs to a period already covered: it covers nothing, so a one-off
    document can never silence a missing invoice (§22, §23).
    """
    tolerance = timedelta(days=series.spec.tolerance_days)
    expected = _step(_anchor_date(series.last_seen, series.cadence, series.anchor), series.cadence, series.anchor)
    for arrival in sorted(set(arrivals)):
        if arrival < expected - tolerance:
            continue
        while _step(expected, series.cadence, series.anchor) - tolerance <= arrival:
            expected = _step(expected, series.cadence, series.anchor)
        expected = _step(expected, series.cadence, series.anchor)
    return expected


def next_expected(series: RecurringSeries, arrivals: Iterable[date] = ()) -> ExpectedWindow:
    """Window of the next occurrence not covered by history or ``arrivals``."""
    return _window(series, _next_period(series, arrivals))


def _window(series: RecurringSeries, expected: date) -> ExpectedWindow:
    return ExpectedWindow(
        earliest=expected + timedelta(days=series.early_days),
        expected=expected,
        due=expected + timedelta(days=series.grace_days),
    )


def check_overdue(
    series: RecurringSeries, today: date, *, arrivals: Iterable[date] = ()
) -> OverdueNotice | None:
    """Plain notice when the next occurrence is past its usual date (§23), else None.

    Only trusted series can be overdue. Due dates are inclusive: arriving on the
    26th is on time for "by the 26th".
    """
    if not series.trusted:
        return None
    window = next_expected(series, arrivals)
    if today <= window.due:
        return None
    missed, expected = 0, window.expected
    while today > expected + timedelta(days=series.grace_days):
        missed += 1
        expected = _step(expected, series.cadence, series.anchor)
    likely_ended = missed >= series.spec.ended_after_missed
    return OverdueNotice(
        key=series.key,
        display_name=series.display_name,
        basis=series.basis,
        due_on=window.due,
        missed_periods=missed,
        likely_ended=likely_ended,
        message=_overdue_message(series, window.due, today, likely_ended),
    )


def _overdue_message(series: RecurringSeries, due: date, today: date, likely_ended: bool) -> str:
    name = series.display_name
    invoices = series.basis is Basis.INVOICES
    if likely_ended:
        since = day_month(series.last_seen, today)
        if invoices:
            return f"{name} has not sent an invoice since {since}. It may have stopped."
        if series.direction is Direction.OUT:
            return f"{name} has not been paid since {since}. It may have stopped."
        return f"{name} has not paid you since {since}. It may have stopped."
    tail = "Invoice missing." if invoices else "Payment missing."
    if series.cadence is Cadence.MONTHLY:
        by = f"by the {ordinal(due.day)}"
        same_month = (today.year, today.month) == (due.year, due.month)
        today_text = f"Today is the {ordinal(today.day)}." if same_month else f"Today is {day_month(today, due)}."
        return f"{_lead(series, by)} {today_text} {tail}"
    rhythm = series.spec.phrase
    return (
        f"{_lead(series, rhythm)} This one was due by {day_month(due, today)}. "
        f"Today is {day_month(today, due)}. {tail}"
    )


def _lead(series: RecurringSeries, when: str) -> str:
    name = series.display_name
    if series.basis is Basis.INVOICES:
        return f"{name} normally issues an invoice {when}."
    if series.direction is Direction.OUT:
        return f"{name} is normally paid {when}."
    return f"{name} normally pays you {when}."
