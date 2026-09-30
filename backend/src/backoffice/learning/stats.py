"""Robust statistics on money (§23 typical amounts, §26 unusual amounts).

Median and median absolute deviation (MAD) resist the one-off outlier that
would drag a mean. The modified z-score and its 3.5 cut-off follow Iglewicz &
Hoaglin (1993), "How to Detect and Handle Outliers": a statistical convention,
not a regulatory figure. All arithmetic stays in Decimal.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

__all__ = ["MODIFIED_Z_CONSTANT", "OUTLIER_Z", "mad", "median", "modified_z"]

# 0.6745 is the 0.75 quantile of the standard normal: it makes MAD comparable to a std dev.
MODIFIED_Z_CONSTANT = Decimal("0.6745")
OUTLIER_Z = Decimal("3.5")


def median(values: Sequence[Decimal]) -> Decimal:
    """Middle value (mean of the two middle values for an even count)."""
    if not values:
        raise ValueError("median of no values")
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def mad(values: Sequence[Decimal], center: Decimal | None = None) -> Decimal:
    """Median absolute deviation from ``center`` (default: the median)."""
    mid = median(values) if center is None else center
    return median([abs(v - mid) for v in values])


def modified_z(value: Decimal, center: Decimal, spread: Decimal) -> Decimal | None:
    """0.6745 * (value - median) / MAD, or None when MAD is zero (all values equal)."""
    if spread == 0:
        return None
    return MODIFIED_Z_CONSTANT * (value - center) / spread
