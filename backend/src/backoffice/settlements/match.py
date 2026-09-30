"""Which bank payout does each payout report prove? (§3, §19, §20, §54, §57)

:func:`match_payouts` pairs payout reports (that add up) with payouts seen in
the bank, deterministically:

1. Same provider (a card-terminal line that names no acquirer fits any
   acquirer's report), same currency, and either the report's payout
   reference is on the bank line or the bank booked it within a few days of
   the report's payout date.
2. **Settled** when the report's net equals the bank amount to the cent and
   the pairing is unique on both sides (by reference first, then by date).
   Quality GREEN: the provider's own figures add up, and the bank confirms the
   net independently.
3. **Ambiguous** when several reports or payouts fit equally well: nothing is
   closed, both are listed (AMBER, never a silent pick).
4. **Amount differs** when the report and the bank payout are clearly the
   same payout (same reference, or the only payout and the only report of
   that provider around those days) but the amounts are not equal: a conflict
   (RED), one plain question, never a silent close.

Everything else waits: a payout whose report has not arrived, or a report
whose payout has not reached the bank yet.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import Enum

from backoffice.domain.models import Quality, Transaction
from backoffice.reconciliation import MatchKind, PayoutProvider, compatible_providers
from backoffice.reconciliation._text import compile_identifier, currency_code, format_money, fold, squash

from .report import SettlementReport

__all__ = ["PayoutCandidate", "PayoutConfig", "PayoutDecision", "PayoutOutcome", "likely_payouts", "match_payouts",
           "provider_label"]

_MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
           "November", "December")


@dataclass(frozen=True)
class PayoutCandidate:
    """A payout seen in the bank, with the provider its wording names."""

    transaction: Transaction
    provider: PayoutProvider


class PayoutOutcome(str, Enum):
    SETTLED = "settled"
    AMOUNT_DIFFERS = "amount_differs"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class PayoutConfig:
    days_before: int = 3  # the bank may book a payout slightly before the provider's own date
    days_after: int = 10  # bank credits follow a payout by a few business days
    conflict_days_before: int = 1  # without a reference, a difference is only asked about when the days fit closely
    conflict_days_after: int = 4
    quiet_days: int = 14  # ...and no other payout or report of that provider is this close

    def __post_init__(self) -> None:
        values = (self.days_before, self.days_after, self.conflict_days_before, self.conflict_days_after,
                  self.quiet_days)
        if min(values) < 0:
            raise ValueError("day windows cannot be negative")


@dataclass(frozen=True)
class PayoutDecision:
    """One report and one bank payout: settled, a difference to ask about, or ambiguous."""

    outcome: PayoutOutcome
    report_id: str
    transaction_id: str
    quality: Quality
    headline: str  # owner-facing, plain (§36)
    why: tuple[str, ...]  # owner-facing "Why?" lines (§54)
    by_reference: bool
    difference: Decimal = Decimal(0)  # bank amount minus the report's net
    alternatives: tuple[tuple[str, str], ...] = ()  # other (report id, transaction id) pairs that fit as well
    kind: MatchKind = MatchKind.PAYOUT_SETTLEMENT


def provider_label(report: SettlementReport, candidate: PayoutCandidate | None = None) -> str:
    """The most specific name: the acquirer the report names beats a bank line that names none."""
    if candidate is not None and not candidate.provider.is_card_terminal:
        return candidate.provider.label
    return report.provider.label


def _day(d: date) -> str:
    return f"{d.day} {_MONTHS[d.month - 1]}"


def _mentions(tx: Transaction, identifier: str | None) -> bool:
    """The report's payout reference stands on its own on the bank line (reference, description or name).

    Weak identifiers (short, or without a digit) never count: a stray '0918' is not evidence.
    """
    pattern = compile_identifier(identifier) if identifier else None
    if pattern is None:
        return False
    if tx.reference and squash(tx.reference) == squash(identifier):
        return True
    return any(pattern.search(fold(text)) for text in (tx.reference, tx.description, tx.counterparty) if text)


@dataclass(frozen=True)
class _Pair:
    report_id: str
    transaction_id: str
    by_reference: bool
    exact: bool
    gap: int  # bank day minus payout day (0 when the report has no date)


def match_payouts(
    payouts: Sequence[PayoutCandidate],
    reports: Mapping[str, SettlementReport],
    config: PayoutConfig | None = None,
) -> list[PayoutDecision]:
    """Decisions for the reports and payouts that can be paired now (see module docs).

    Only reports that add up should be passed in: one that does not is a
    conflict on its own and never proves a payout. Deterministic: the result
    does not depend on input order.
    """
    cfg = config or PayoutConfig()
    by_tx = {c.transaction.id: c for c in payouts}
    pairs: list[_Pair] = []
    for report_id, report in sorted(reports.items()):
        for _, cand in sorted(by_tx.items()):
            pair = _fit(report_id, report, cand, cfg)
            if pair is not None:
                pairs.append(pair)
    decisions: list[PayoutDecision] = []
    used_r: set[str] = set()
    used_t: set[str] = set()

    def open_pairs(keep) -> list[_Pair]:  # noqa: ANN001
        return [p for p in pairs if keep(p) and p.report_id not in used_r and p.transaction_id not in used_t]

    # 1-2. exact amounts: by reference first, then by date; unique on both sides.
    for keep in (lambda p: p.exact and p.by_reference, lambda p: p.exact):
        found = open_pairs(keep)
        per_r: dict[str, list[_Pair]] = defaultdict(list)
        per_t: dict[str, list[_Pair]] = defaultdict(list)
        for p in found:
            per_r[p.report_id].append(p)
            per_t[p.transaction_id].append(p)
        for p in sorted(found, key=lambda p: (abs(p.gap), p.report_id, p.transaction_id)):
            if p.report_id in used_r or p.transaction_id in used_t:
                continue
            rivals = {(q.report_id, q.transaction_id) for q in (*per_r[p.report_id], *per_t[p.transaction_id])
                      if q is not p}
            report, cand = reports[p.report_id], by_tx[p.transaction_id]
            if rivals:
                decisions.append(_ambiguous(p, report, cand, tuple(sorted(rivals))))
                used_r.update({p.report_id, *(r for r, _ in rivals)})
                used_t.update({p.transaction_id, *(t for _, t in rivals)})
                continue
            decisions.append(_settled(p, report, cand))
            used_r.add(p.report_id)
            used_t.add(p.transaction_id)
    # 3. the same payout, different amounts: by reference, or alone in a quiet, tight window.
    for p in open_pairs(lambda p: p.by_reference):
        others = [q for q in open_pairs(lambda q: q.by_reference)
                  if q is not p and (q.report_id == p.report_id or q.transaction_id == p.transaction_id)]
        if not others and p.report_id not in used_r and p.transaction_id not in used_t:
            decisions.append(_differs(p, reports[p.report_id], by_tx[p.transaction_id]))
            used_r.add(p.report_id)
            used_t.add(p.transaction_id)
    tight = open_pairs(lambda p: -cfg.conflict_days_before <= p.gap <= cfg.conflict_days_after)
    for p in tight:
        report, cand = reports[p.report_id], by_tx[p.transaction_id]
        if p.report_id in used_r or p.transaction_id in used_t or report.payout_date is None:
            continue
        near_tx = [c for c in payouts if c.transaction.id not in used_t and
                   compatible_providers(c.provider, report.provider) and
                   abs((c.transaction.booked_on - report.payout_date).days) <= cfg.quiet_days]
        near_r = [rid for rid, r in reports.items() if rid not in used_r and r.payout_date is not None and
                  compatible_providers(cand.provider, r.provider) and
                  abs((cand.transaction.booked_on - r.payout_date).days) <= cfg.quiet_days]
        if len(near_tx) == 1 and len(near_r) == 1:
            decisions.append(_differs(p, report, cand))
            used_r.add(p.report_id)
            used_t.add(p.transaction_id)
    return sorted(decisions, key=lambda d: (d.transaction_id, d.report_id))


def likely_payouts(report: SettlementReport, payouts: Sequence[PayoutCandidate],
                   config: PayoutConfig | None = None) -> tuple[str, ...]:
    """The bank payouts a report is probably about (reference first, else its days), whatever the amounts.

    For a report that cannot prove anything (it does not add up): the payout it
    concerns can say so instead of asking for a report that already arrived.
    Nothing is settled by this.
    """
    cfg = config or PayoutConfig()
    pairs = [p for c in sorted(payouts, key=lambda c: c.transaction.id) if (p := _fit("", report, c, cfg))]
    by_reference = tuple(p.transaction_id for p in pairs if p.by_reference)
    return by_reference or tuple(p.transaction_id for p in pairs)


def _fit(report_id: str, report: SettlementReport, cand: PayoutCandidate, cfg: PayoutConfig) -> _Pair | None:
    tx = cand.transaction
    if not compatible_providers(cand.provider, report.provider):
        return None
    if currency_code(tx.currency) != report.currency:
        return None
    by_reference = _mentions(tx, report.payout_id)
    gap = (tx.booked_on - report.payout_date).days if report.payout_date else 0
    in_window = report.payout_date is not None and -cfg.days_before <= gap <= cfg.days_after
    if not by_reference and not in_window:
        return None
    return _Pair(report_id, tx.id, by_reference, report.net == tx.amount, gap)


def _money(report: SettlementReport, value: Decimal) -> str:
    return format_money(value, report.currency)


def _settled(p: _Pair, report: SettlementReport, cand: PayoutCandidate) -> PayoutDecision:
    tx = cand.transaction
    label = provider_label(report, cand)
    why = list(report.breakdown())
    why.append("The report adds up to the cent")
    why.append(f"Arrived in your bank: {_money(report, tx.amount)} on {_day(tx.booked_on)}")
    if p.by_reference:
        why.append("Payout reference: the same on the report and the bank line")
    elif report.payout_date is not None:
        days = abs(p.gap)
        why.append("Dates: same day" if days == 0 else f"Dates: {days} day{'s' if days != 1 else ''} apart")
    return PayoutDecision(
        outcome=PayoutOutcome.SETTLED, report_id=p.report_id, transaction_id=tx.id, quality=Quality.GREEN,
        headline=f"Payout from {label} matched to its payout report.", why=tuple(why), by_reference=p.by_reference,
    )


def _differs(p: _Pair, report: SettlementReport, cand: PayoutCandidate) -> PayoutDecision:
    tx = cand.transaction
    label = provider_label(report, cand)
    difference = tx.amount - report.net
    when = f" on {_day(report.payout_date)}" if report.payout_date else ""
    headline = (f"The payout report from {label} says {_money(report, report.net)} was paid out{when}, but "
                f"{_money(report, tx.amount)} arrived in your bank on {_day(tx.booked_on)}.")
    why = (
        f"Paid out, as the report says: {_money(report, report.net)}",
        f"Arrived in your bank: {_money(report, tx.amount)}",
        f"Difference: {_money(report, abs(difference))}",
        "Payout reference: the same on the report and the bank line" if p.by_reference else
        f"It is the only payout from {label} around those days",
        "Until you answer, I won't count or close this payout.",
    )
    return PayoutDecision(
        outcome=PayoutOutcome.AMOUNT_DIFFERS, report_id=p.report_id, transaction_id=tx.id, quality=Quality.RED,
        headline=headline, why=why, by_reference=p.by_reference, difference=difference,
    )


def _ambiguous(p: _Pair, report: SettlementReport, cand: PayoutCandidate,
               rivals: tuple[tuple[str, str], ...]) -> PayoutDecision:
    label = provider_label(report, cand)
    return PayoutDecision(
        outcome=PayoutOutcome.AMBIGUOUS, report_id=p.report_id, transaction_id=cand.transaction.id,
        quality=Quality.AMBER,
        headline=f"More than one payout from {label} fits this payout report. Please confirm.",
        why=(*report.breakdown(), f"Arrived in your bank: {_money(report, cand.transaction.amount)}"),
        by_reference=p.by_reference, alternatives=rivals,
    )
