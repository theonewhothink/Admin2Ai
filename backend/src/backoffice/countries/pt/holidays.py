"""Portugal's national public holidays (Código do Trabalho, art. 234).

The fixed ones, Good Friday, Easter Sunday and Corpus Christi. Municipal holidays (Lisbon's 13 June,
Porto's 24 June ...) are not national and are not included. A working day skips these (the monthly
accountant package goes on one, backoffice.package_delivery).
"""

from __future__ import annotations

from datetime import date, timedelta
from functools import lru_cache

from backoffice.countries.base import easter_sunday

__all__ = ["national_holidays"]

_FIXED = ((1, 1), (4, 25), (5, 1), (6, 10), (8, 15), (10, 5), (11, 1), (12, 1), (12, 8), (12, 25))


@lru_cache(maxsize=64)
def national_holidays(year: int) -> frozenset[date]:
    """Portugal's national public holidays in ``year``."""
    easter = easter_sunday(year)
    return frozenset({date(year, m, d) for m, d in _FIXED} | {easter - timedelta(days=2), easter,
                                                               easter + timedelta(days=60)})
