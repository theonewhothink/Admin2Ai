"""Free Business Audit report (§60).

A 90-day scan of a prospect's email and bank shows expenses, documents,
recurring subscriptions, missing evidence, subscriptions that went up and
items that look like they belong to another company, then offers
"Let me manage this automatically." The counts come from the scan; this
module only turns them into short, plain lines (§36, §69).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from ._text import count_phrase, day_month, format_money, require_count, require_money

__all__ = [
    "AUDIT_DAYS",
    "CALL_TO_ACTION",
    "BusinessAuditFindings",
    "BusinessAuditReport",
    "PriceIncrease",
    "audit_window",
    "render_business_audit",
]

AUDIT_DAYS = 90  # §60: the free audit scans the previous 90 days
CALL_TO_ACTION = "Let me manage this automatically."


def audit_window(today: date, days: int = AUDIT_DAYS) -> tuple[date, date]:
    """(first day, last day) of the scan, ending yesterday."""
    if days <= 0:
        raise ValueError("days must be positive")
    end = today - timedelta(days=1)
    return end - timedelta(days=days - 1), end


@dataclass(frozen=True, slots=True)
class PriceIncrease:
    name: str
    before: Decimal
    after: Decimal
    currency: str = "EUR"

    def __post_init__(self) -> None:
        if require_money(self.after, "after") <= require_money(self.before, "before"):
            raise ValueError("a price increase must go up")

    @property
    def line(self) -> str:
        """'Adobe €24.59 → €29.99'."""
        return f"{self.name} {format_money(self.before, self.currency)} → {format_money(self.after, self.currency)}"


@dataclass(frozen=True, slots=True)
class BusinessAuditFindings:
    period_start: date
    period_end: date
    expenses: int
    expenses_total: Decimal
    documents_found: int
    recurring_subscriptions: int
    missing_evidence: int
    other_company_items: int
    increases: tuple[PriceIncrease, ...] = ()
    currency: str = "EUR"

    def __post_init__(self) -> None:
        if self.period_end < self.period_start:
            raise ValueError("period ends before it starts")
        for name in ("expenses", "documents_found", "recurring_subscriptions", "missing_evidence",
                     "other_company_items"):  # fmt: skip
            require_count(getattr(self, name), name)
        if require_money(self.expenses_total, "expenses_total") < 0:
            raise ValueError("expenses_total is a magnitude")


@dataclass(frozen=True, slots=True)
class BusinessAuditReport:
    headline: str
    lines: tuple[str, ...]
    call_to_action: str = CALL_TO_ACTION


def render_business_audit(findings: BusinessAuditFindings) -> BusinessAuditReport:
    """Plain summary of the scan, e.g. '143 expenses, €12,480.20 in total'."""
    f = findings
    start, end = day_month(f.period_start, f.period_end), day_month(f.period_end, f.period_end)
    lines = [
        f"{count_phrase(f.expenses, 'expense')}, {format_money(f.expenses_total, f.currency)} in total",
        f"{count_phrase(f.documents_found, 'document')} found",
        f"{count_phrase(f.recurring_subscriptions, 'recurring subscription')}",
        f"{count_phrase(f.missing_evidence, 'payment')} without a document",
    ]
    if f.increases:
        changes = "; ".join(i.line for i in f.increases)
        lines.append(f"{count_phrase(len(f.increases), 'subscription')} went up: {changes}")
    if f.other_company_items:
        verb = "looks" if f.other_company_items == 1 else "look"
        lines.append(f"{count_phrase(f.other_company_items, 'payment')} {verb} like another company's")
    return BusinessAuditReport(
        headline=f"Here is what I found from {start} to {end}.",
        lines=tuple(lines),
    )
