"""Spanish VAT (IVA) rates for the mainland and the Balearic Islands (§50 for Spain).

A dated table like Portugal's: every entry says when it applied and where it comes from. The
Canary Islands (IGIC) and Ceuta and Melilla (IPSI) have their own indirect taxes, not IVA: the
table does not cover them and checks there answer "unknown", never a guess.

* 21% general, 10% reduced, 4% super-reduced (Ley 37/1992 del IVA, art. 90-91; 21/10 since
  2012-09-01, Real Decreto-ley 20/2012).
* 0%: exempt and zero-rated operations (exports, intra-EU supplies, art. 20-25 LIVA), and the
  temporary 0% on basic foods (Real Decreto-ley 20/2022, extended to 2024-09-30).
* Temporary: 5% on oils and pasta (2023-01-01 to 2024-09-30) and 2% on olive oil (2024-10-01 to
  2024-12-31), Real Decreto-ley 4/2024.

verified_as_of: 2026-09 (author knowledge of the AEAT's rate tables; not re-checked online).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from backoffice.countries.base import CountryPackError, VATBucket, VATRate

__all__ = ["ES_VAT_RATES", "RateDataUnavailable", "check_rate", "rates_on"]

_CHECKED = date(2026, 9, 30)
_CENT = Decimal("0.01")
ES_MAINLAND = "ES"
# Regions with their own indirect tax instead of IVA.
_NOT_IVA = frozenset({"ES-CN", "ES-CE", "ES-ML", "CANARIAS", "CEUTA", "MELILLA"})

_LIVA = "Ley 37/1992 del IVA, art. 90-91; Real Decreto-ley 20/2012 (21% and 10% from 2012-09-01)"
_ZERO_SRC = "Ley 37/1992 del IVA, art. 20-25: exempt and zero-rated operations"
_FOOD_SRC = "Real Decreto-ley 20/2022 and 4/2024: temporary rates on basic foods, oils and pasta"


def _rate(bucket: VATBucket, pct: str, start: date, end: date | None, source: str) -> VATRate:
    return VATRate(ES_MAINLAND, bucket, Decimal(pct) / 100, start, end, source, _CHECKED)


ES_VAT_RATES: tuple[VATRate, ...] = (
    _rate(VATBucket.NORMAL, "21", date(2012, 9, 1), None, _LIVA),
    _rate(VATBucket.REDUCED, "10", date(2012, 9, 1), None, _LIVA),
    _rate(VATBucket.SUPER_REDUCED, "4", date(1995, 1, 1), None, _LIVA),
    _rate(VATBucket.ZERO, "0", date(1993, 1, 1), None, _ZERO_SRC),
    _rate(VATBucket.INTERMEDIATE, "5", date(2023, 1, 1), date(2024, 9, 30), _FOOD_SRC),
    _rate(VATBucket.SUPER_REDUCED, "2", date(2024, 10, 1), date(2024, 12, 31), _FOOD_SRC),
)


class RateDataUnavailable(CountryPackError, LookupError):
    """No IVA rate data for that region (the Canary Islands, Ceuta and Melilla have no IVA)."""


def _region(region: str | None) -> str | None:
    key = (region or ES_MAINLAND).strip().upper()
    if key in _NOT_IVA:
        return None
    return ES_MAINLAND if key in (ES_MAINLAND, "ES-IB", "ES-PM", "BALEARES") else None


def rates_on(day: date | None, region: str | None = None) -> tuple[VATRate, ...]:
    """IVA rates in force on ``day`` (without a day: the open-ended rates, never the clock)."""
    if _region(region) is None:
        return ()
    if day is None:
        return tuple(r for r in ES_VAT_RATES if r.valid_to is None)
    return tuple(r for r in ES_VAT_RATES if r.applies_on(day))


def check_rate(net: Decimal, vat: Decimal, *, on: date | None = None, region: str | None = None) -> bool | None:
    """True when ``vat`` is ``net`` at one of the rates in force (to the cent, one cent of rounding
    allowed); None when the table does not cover the region."""
    rates = rates_on(on, region)
    if not rates:
        return None
    for r in rates:
        expected = (net * r.rate).quantize(_CENT)
        if abs(expected - vat) <= _CENT:
            return True
    return False
