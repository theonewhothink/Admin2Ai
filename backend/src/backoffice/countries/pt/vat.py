"""Portuguese VAT (IVA) rates by fiscal region (§50 "regional VAT differences").

Rates are a dated table: every entry says when it applied, where it comes
from and when it was last checked. Plausibility checks only answer for dates
the table covers; outside it they say "unknown" rather than guess.

Checks are deterministic: without a date they use the rates the table lists
as still in force (open-ended entries), never the wall clock. Callers
checking a document should always pass its issue date.
"""

from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum

from backoffice.countries.base import CountryPackError, VATBucket, VATRate

_CHECKED = date(2026, 9, 27)
_CENT = Decimal("0.01")


class PTRegion(str, Enum):
    """VAT fiscal spaces, as written in SAF-T TaxCountryRegion and QR I1/J1/K1."""

    MAINLAND = "PT"
    AZORES = "PT-AC"
    MADEIRA = "PT-MA"

    @classmethod
    def parse(cls, value: str | PTRegion) -> PTRegion:
        """Accepts "PT", "PT-AC", "PT-MA" and the ISO 3166-2 forms PT-20 / PT-30."""
        if isinstance(value, PTRegion):
            return value
        key = (value or "").strip().upper()
        key = {"PT-20": "PT-AC", "PT-30": "PT-MA"}.get(key, key)
        try:
            return cls(key)
        except ValueError:
            raise ValueError(f"not a Portuguese VAT region: {value!r}") from None


_R, _I, _N = VATBucket.REDUCED, VATBucket.INTERMEDIATE, VATBucket.NORMAL


def _rate(region: PTRegion, bucket: VATBucket, pct: str, start: date, end: date | None,
          source: str) -> VATRate:
    return VATRate(region.value, bucket, Decimal(pct) / 100, start, end, source, _CHECKED)


_MAINLAND_SRC = "CIVA art. 18(1); rates 6/13/23 since 2011-01-01 (Lei 55-A/2010)"
# Lei 63-A/2015 (30 June) cut the Azores reduced/intermediate rates from 5/10 to
# 4/9 with effect from 2015-07-01; the 18% normal rate was unchanged. Before
# that the Azores applied 5/10/18 (from 2014-01-01 per secondary sources only),
# so that period is deliberately NOT covered: checks answer "unknown".
_AZORES_18_SRC = "Lei 63-A/2015; 4/9/18 from 2015-07-01 until 2021-06-30"
_AZORES_16_SRC = "Decreto Legislativo Regional 15-A/2021/A; 4/9/16 from 2021-07-01"
_MADEIRA_SRC = "Lei 14-A/2012; 5/12/22 from 2012-04-01"
_MADEIRA_4_SRC = "Decreto Legislativo Regional 6/2024/M; reduced rate 4% from 2024-10-01"

# verified_as_of 2026-09-27 (see _CHECKED). Special temporary regimes, such as
# the 2023 zero-rated food basket, are not modelled: zero VAT is checked only
# when a caller explicitly allows it.
PT_VAT_RATES: tuple[VATRate, ...] = (
    _rate(PTRegion.MAINLAND, _R, "6", date(2011, 1, 1), None, _MAINLAND_SRC),
    _rate(PTRegion.MAINLAND, _I, "13", date(2011, 1, 1), None, _MAINLAND_SRC),
    _rate(PTRegion.MAINLAND, _N, "23", date(2011, 1, 1), None, _MAINLAND_SRC),
    _rate(PTRegion.AZORES, _R, "4", date(2015, 7, 1), date(2021, 6, 30), _AZORES_18_SRC),
    _rate(PTRegion.AZORES, _I, "9", date(2015, 7, 1), date(2021, 6, 30), _AZORES_18_SRC),
    _rate(PTRegion.AZORES, _N, "18", date(2015, 7, 1), date(2021, 6, 30), _AZORES_18_SRC),
    _rate(PTRegion.AZORES, _R, "4", date(2021, 7, 1), None, _AZORES_16_SRC),
    _rate(PTRegion.AZORES, _I, "9", date(2021, 7, 1), None, _AZORES_16_SRC),
    _rate(PTRegion.AZORES, _N, "16", date(2021, 7, 1), None, _AZORES_16_SRC),
    _rate(PTRegion.MADEIRA, _R, "5", date(2012, 4, 1), date(2024, 9, 30), _MADEIRA_SRC),
    _rate(PTRegion.MADEIRA, _R, "4", date(2024, 10, 1), None, _MADEIRA_4_SRC),
    _rate(PTRegion.MADEIRA, _I, "12", date(2012, 4, 1), None, _MADEIRA_SRC),
    _rate(PTRegion.MADEIRA, _N, "22", date(2012, 4, 1), None, _MADEIRA_SRC),
)


class RateCheck(str, Enum):
    PLAUSIBLE = "plausible"
    IMPLAUSIBLE = "implausible"
    UNKNOWN = "unknown"  # the table does not cover that region/date


class RateDataUnavailable(CountryPackError, LookupError):
    """No rate data for the requested region and date."""

    owner_message = "I don't have the VAT rates for that date, so I couldn't check the VAT."


def coverage_start(region: str | PTRegion) -> date:
    """First date the table covers for a region."""
    reg = PTRegion.parse(region).value
    return min(r.valid_from for r in PT_VAT_RATES if r.region == reg)


def rates_on(day: date, region: str | PTRegion | None = None) -> tuple[VATRate, ...]:
    """Entries in force on ``day`` (all regions when ``region`` is None)."""
    reg = PTRegion.parse(region).value if region is not None else None
    return tuple(
        r for r in PT_VAT_RATES if (reg is None or r.region == reg) and r.applies_on(day)
    )


def current_rates(region: str | PTRegion | None = None) -> tuple[VATRate, ...]:
    """Entries the table lists as still in force (no end date). Clock-free."""
    reg = PTRegion.parse(region).value if region is not None else None
    return tuple(
        r for r in PT_VAT_RATES if (reg is None or r.region == reg) and r.valid_to is None
    )


def _rates(region: str | PTRegion, on: date | None) -> tuple[VATRate, ...]:
    return current_rates(region) if on is None else rates_on(on, region)


def rate_for(region: str | PTRegion, bucket: VATBucket, day: date) -> Decimal | None:
    """The single rate for a region/bucket on a day, or None if not covered."""
    matches = [r.rate for r in rates_on(day, region) if r.bucket == bucket]
    return matches[0] if len(matches) == 1 else None


def expected_vat(net: Decimal, rate: Decimal) -> Decimal:
    """VAT on ``net`` at ``rate``, rounded half-up to the cent."""
    return (net * rate).quantize(_CENT, rounding=ROUND_HALF_UP)


def _tolerance(expected: Decimal) -> Decimal:
    # One cent plus 1% of the expected VAT: absorbs per-line rounding while
    # still separating neighbouring rates (e.g. 4% vs 9%, 13% vs 23%).
    return _CENT + abs(expected) * Decimal("0.01")


def match_rate(
    net: Decimal,
    vat: Decimal,
    region: str | PTRegion = PTRegion.MAINLAND,
    on: date | None = None,
    *,
    bucket: VATBucket | None = None,
) -> VATRate | None:
    """The table entry that explains ``vat`` on ``net``, if any.

    ``on`` None means "the rates currently in force" (see module docstring).
    """
    candidates = [r for r in _rates(region, on) if bucket is None or r.bucket == bucket]
    if not candidates:
        raise RateDataUnavailable(f"no VAT rates for {region} on {_when(on)}")
    if (net < 0) != (vat < 0) and net != 0 and vat != 0:
        return None  # a credit note flips both signs, never just one
    net_abs, vat_abs = abs(net), abs(vat)
    for entry in candidates:
        expected = expected_vat(net_abs, entry.rate)
        if abs(vat_abs - expected) <= _tolerance(expected):
            return entry
    return None


def check_rate(
    net: Decimal,
    vat: Decimal,
    region: str | PTRegion = PTRegion.MAINLAND,
    on: date | None = None,
    *,
    bucket: VATBucket | None = None,
    allow_zero: bool = False,
) -> RateCheck:
    """PLAUSIBLE / IMPLAUSIBLE / UNKNOWN for a net/VAT pair."""
    if net == 0 and vat == 0:
        return RateCheck.PLAUSIBLE
    if vat == 0 and allow_zero:
        return RateCheck.PLAUSIBLE
    try:
        found = match_rate(net, vat, region, on, bucket=bucket)
    except RateDataUnavailable:
        return RateCheck.UNKNOWN
    return RateCheck.PLAUSIBLE if found else RateCheck.IMPLAUSIBLE


def is_plausible_rate(
    net: Decimal,
    vat: Decimal,
    region: str | PTRegion = PTRegion.MAINLAND,
    on: date | None = None,
    *,
    bucket: VATBucket | None = None,
    allow_zero: bool = False,
) -> bool:
    """True when ``vat`` matches a rate in force in ``region`` on ``on``
    (the current rates when ``on`` is None).

    Zero VAT on a positive amount is only plausible with ``allow_zero`` (the
    caller must know the supply is exempt). Raises RateDataUnavailable when
    the table does not cover the date.
    """
    result = check_rate(net, vat, region, on, bucket=bucket, allow_zero=allow_zero)
    if result is RateCheck.UNKNOWN:
        raise RateDataUnavailable(f"no VAT rates for {region} on {_when(on)}")
    return result is RateCheck.PLAUSIBLE


def _when(on: date | None) -> str:
    return on.isoformat() if on is not None else "the current table"
