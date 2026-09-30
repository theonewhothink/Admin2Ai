"""Spain's national public holidays (fiestas nacionales comunes a todo el territorio).

1 January, 6 January, Good Friday, 1 May, 15 August, 12 October, 1 November, 6 December, 8 December
and 25 December (Estatuto de los Trabajadores art. 37.2 and the yearly BOE list). The regions move some
of these and add their own (Holy Thursday in most of them, Easter Monday in Catalonia ...), and towns add
two local ones: none of those are included, so a working day may still be a local holiday.
verified_as_of: 2026-09 (author knowledge, not re-checked online).
"""

from __future__ import annotations

from datetime import date, timedelta
from functools import lru_cache

from backoffice.countries.base import easter_sunday

__all__ = ["national_holidays"]

_FIXED = ((1, 1), (1, 6), (5, 1), (8, 15), (10, 12), (11, 1), (12, 6), (12, 8), (12, 25))


@lru_cache(maxsize=64)
def national_holidays(year: int) -> frozenset[date]:
    """Spain's national public holidays in ``year`` (Good Friday included; regional and local ones not)."""
    return frozenset({date(year, m, d) for m, d in _FIXED} | {easter_sunday(year) - timedelta(days=2)})
