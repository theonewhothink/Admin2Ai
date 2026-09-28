"""Historical payment patterns (§6, §20 'historic pattern, recurring sequence', §23).

The reconciliation engine never learns on its own: history is injected through
the :class:`PaymentHistory` protocol, keyed by the canonical supplier key from
:class:`~backoffice.reconciliation.suppliers.SupplierMatch` (the supplier id when
known). :func:`infer_cadence` and :meth:`PaymentPattern.learn` derive a pattern
from past, already-matched payments.
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import Enum
from itertools import pairwise
from typing import Protocol, runtime_checkable

__all__ = [
    "CADENCE_GAP_DAYS",
    "Cadence",
    "InMemoryPaymentHistory",
    "PaymentHistory",
    "PaymentPattern",
    "infer_cadence",
]


class Cadence(str, Enum):
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    YEARLY = "yearly"


# Inclusive day gaps accepted for each cadence (calendar months vary 28-31 days).
CADENCE_GAP_DAYS: dict[Cadence, tuple[int, int]] = {
    Cadence.WEEKLY: (6, 8),
    Cadence.MONTHLY: (26, 35),
    Cadence.QUARTERLY: (84, 98),
    Cadence.YEARLY: (355, 376),
}


def infer_cadence(dates: Iterable[date], *, min_occurrences: int = 3) -> Cadence | None:
    """Cadence of a recurring payment, or None when there is no clear rhythm.

    Needs ``min_occurrences`` distinct dates, a median gap inside one cadence
    band and at least two thirds of all gaps inside that band.
    """
    ordered = sorted(set(dates))
    if len(ordered) < max(2, min_occurrences):
        return None
    gaps = [(b - a).days for a, b in pairwise(ordered)]
    median = statistics.median(gaps)
    for cadence, (low, high) in CADENCE_GAP_DAYS.items():
        if low <= median <= high:
            inside = sum(1 for g in gaps if low <= g <= high)
            return cadence if inside * 3 >= len(gaps) * 2 else None
    return None


@dataclass(frozen=True)
class PaymentPattern:
    """How a supplier is usually paid.

    ``usual_amount`` is compared within ``amount_tolerance_ratio`` (recurring
    bills vary a little). ``usual_day`` is the day of month for monthly bills.
    ``card_last4`` lists cards this supplier is usually paid with.
    """

    cadence: Cadence | None
    usual_amount: Decimal | None = None
    amount_tolerance_ratio: Decimal = Decimal("0.10")
    usual_day: int | None = None
    day_tolerance: int = 4
    card_last4: frozenset[str] = frozenset()
    occurrences: int = 0

    def __post_init__(self) -> None:
        if isinstance(self.usual_amount, float) or isinstance(
            self.amount_tolerance_ratio, float
        ):
            raise TypeError("money must be Decimal, never float")
        if self.usual_day is not None and not 1 <= self.usual_day <= 31:
            raise ValueError("usual_day must be a day of the month")

    def amount_fits(self, amount: Decimal) -> bool:
        if self.usual_amount is None:
            return True
        usual = abs(self.usual_amount)
        return abs(abs(amount) - usual) <= usual * self.amount_tolerance_ratio

    def day_fits(self, on: date) -> bool:
        if self.usual_day is None or self.cadence is not Cadence.MONTHLY:
            return True
        distance = abs(on.day - self.usual_day)
        return min(distance, 31 - distance) <= self.day_tolerance

    def fits(self, on: date, amount: Decimal) -> bool:
        """True when a payment on ``on`` for ``amount`` continues the pattern."""
        return (
            self.cadence is not None and self.amount_fits(amount) and self.day_fits(on)
        )

    @classmethod
    def learn(
        cls,
        payments: Sequence[tuple[date, Decimal]],
        *,
        card_last4: Iterable[str] = (),
    ) -> PaymentPattern:
        """Derive a pattern from past (date, amount) payments of one supplier."""
        dates = [d for d, _ in payments]
        amounts = sorted(abs(a) for _, a in payments)
        cadence = infer_cadence(dates)
        usual_amount = amounts[len(amounts) // 2] if amounts else None
        usual_day = None
        if cadence is Cadence.MONTHLY:
            usual_day = int(statistics.median_low(sorted(d.day for d in dates)))
        return cls(
            cadence=cadence,
            usual_amount=usual_amount,
            usual_day=usual_day,
            card_last4=frozenset(card_last4),
            occurrences=len(set(dates)),
        )


@runtime_checkable
class PaymentHistory(Protocol):
    """Source of historical patterns, keyed by canonical supplier key."""

    def pattern_for(self, supplier_key: str) -> PaymentPattern | None: ...


@dataclass
class InMemoryPaymentHistory:
    """Mapping-backed :class:`PaymentHistory`."""

    patterns: Mapping[str, PaymentPattern] = field(default_factory=dict)

    def pattern_for(self, supplier_key: str) -> PaymentPattern | None:
        return self.patterns.get(supplier_key)
