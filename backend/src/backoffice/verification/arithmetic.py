"""Arithmetic checks and ARITHMETIC observations (§18, §19).

Three things are checked:

* **Sum**: net + VAT (+ other charges such as stamp duty) = gross, within
  one cent per tax line (§19: "€393.17 + VAT = €483.60").

Amounts are compared as magnitudes (a credit note's sign follows its type).
``other_charges`` is the one signed input: positive adds to the total
(stamp duty, fees), negative takes something off (a discount after VAT),
both in the document's own direction.
* **Rate**: VAT / net against the allowed rates, injected by the caller
  (normally a country pack's ``vat_rates(...)``; objects with a ``.rate``
  attribute are accepted as they are). Rates are fractions: ``Decimal("0.23")``.
* **Tax lines**: each line's VAT against its rate; the lines' sums against
  the document totals (through derived observations, below).

:func:`derive_observations` turns a source's own numbers into ARITHMETIC
observations for verification: gross from net + VAT, a missing net or VAT
from the other two, and totals from tax lines. It only combines numbers
from the *same* channel, and never a stated total with a tax-line sum, so
a misread in one source never leaks into another source's check. The
derived value keeps that channel's lineage
(a QR code's own subtotals never "confirm" the QR code). A derived value
within the sum tolerance of a stated value reports the stated value:
rounding is not a disagreement.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from itertools import product
from typing import Any

from backoffice.domain.models import CriticalField, ExtractionMethod, FieldObservation

from ._display import money, percent
from .fields import channel_token, derived_location, lineage
from .normalize import CENT, NormalizeHints, normalize_value

__all__ = [
    "RATE_RELATIVE_TOLERANCE",
    "SUM_TOLERANCE_PER_LINE",
    "RateCheck",
    "RateFit",
    "SumCheck",
    "TaxBreakdown",
    "TaxLine",
    "allowed_rate_values",
    "check_rate",
    "check_sum",
    "check_tax_lines",
    "derive_gross",
    "derive_observations",
    "sum_tolerance",
]

ZERO = Decimal("0")
SUM_TOLERANCE_PER_LINE = CENT
# VAT computed per item and then summed drifts from net x rate by a few
# cents on long invoices. 0.1% of the net absorbs that and stays far below
# the gap between any two real rates (at least one point, i.e. 1% of net).
RATE_RELATIVE_TOLERANCE = Decimal("0.001")

_NET, _VAT, _GROSS = CriticalField.NET_AMOUNT, CriticalField.VAT_AMOUNT, CriticalField.GROSS_AMOUNT


def _money(value: object, what: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (Decimal, int)):
        raise TypeError(f"{what} must be Decimal (or int), never float")
    amount = Decimal(value)
    if not amount.is_finite():
        raise ValueError(f"{what} must be a finite amount")
    return amount


def _rate(value: object) -> Decimal:
    rate = getattr(value, "rate", value)
    if isinstance(rate, bool) or not isinstance(rate, (Decimal, int)):
        raise TypeError("VAT rates must be Decimal fractions, e.g. Decimal('0.23')")
    rate = Decimal(rate)
    if not ZERO <= rate < 1:
        raise ValueError(f"VAT rate {rate} is not a fraction between 0 and 1")
    return rate


def allowed_rate_values(allowed: Iterable[object]) -> tuple[Decimal, ...]:
    """Sorted distinct rates from Decimals or rate objects (``.rate``)."""
    return tuple(sorted({_rate(r) for r in allowed}))


def sum_tolerance(lines: int = 1) -> Decimal:
    """One cent per tax line (at least one)."""
    return SUM_TOLERANCE_PER_LINE * max(1, lines)


# --------------------------------------------------------------------------- sum


@dataclass(frozen=True)
class SumCheck:
    """net + VAT + other charges against the stated gross."""

    net: Decimal
    vat: Decimal
    gross: Decimal
    other_charges: Decimal
    tolerance: Decimal

    @property
    def expected(self) -> Decimal:
        return self.net + self.vat + self.other_charges

    @property
    def difference(self) -> Decimal:
        return self.gross - self.expected

    @property
    def ok(self) -> bool:
        return abs(self.difference) <= self.tolerance

    def explain(self, currency: str | None = None) -> str:
        if self.ok:
            return "The amounts add up."
        parts = f"{money(self.net, currency)} + {money(self.vat, currency)} VAT"
        if self.other_charges > 0:
            parts += f" + {money(self.other_charges, currency)} other charges"
        elif self.other_charges < 0:
            parts += f", with {money(-self.other_charges, currency)} taken off,"
        return (
            f"The amounts don't add up: {parts} is {money(self.expected, currency)}, "
            f"but the total shows {money(self.gross, currency)}."
        )


def check_sum(
    net: Decimal, vat: Decimal, gross: Decimal, *, other_charges: Decimal = ZERO, lines: int = 1
) -> SumCheck:
    """Compare magnitudes: a credit note's signs follow its type, not its numbers.

    ``other_charges`` keeps its sign (module docstring): a deduction is not a charge.
    """
    return SumCheck(
        net=abs(_money(net, "net")),
        vat=abs(_money(vat, "VAT")),
        gross=abs(_money(gross, "gross")),
        other_charges=_money(other_charges, "other charges"),
        tolerance=sum_tolerance(lines),
    )


# --------------------------------------------------------------------------- rate


class RateFit(str, Enum):
    MATCHES = "matches"  # VAT = net x one allowed rate
    MIXED = "mixed"  # between the lowest and highest rate: several rates may be combined
    UNEXPECTED = "unexpected"  # no allowed rate (or mix) explains it
    NOT_CHECKED = "not_checked"  # no rates supplied, or no VAT to check


@dataclass(frozen=True)
class RateCheck:
    fit: RateFit
    net: Decimal
    vat: Decimal
    rate: Decimal | None = None  # the allowed rate that matched
    stated_rate: Decimal | None = None
    line: int | None = None  # 1-based tax line, None for document totals
    stated_rate_allowed: bool | None = None  # None when no rate was printed

    @property
    def ratio(self) -> Decimal | None:
        return (self.vat / self.net).quantize(Decimal("0.0001")) if self.net else None

    def explain(self) -> str:
        where = f" on line {self.line}" if self.line else ""
        if self.fit is RateFit.MATCHES:
            return f"The VAT{where} matches the {percent(self.rate or ZERO)} rate."
        if self.fit is RateFit.MIXED:
            return "The VAT fits a mix of the expected rates."
        if self.fit is RateFit.NOT_CHECKED:
            return f"There is no VAT rate to check{where}."
        if self.stated_rate is not None and self.stated_rate_allowed is False:
            return f"The {percent(self.stated_rate)} VAT rate{where} isn't one of the expected rates."
        if self.stated_rate is not None:
            return f"The VAT{where} doesn't match its {percent(self.stated_rate)} rate."
        return f"The VAT{where} doesn't match any expected rate."


def _fits(net: Decimal, vat: Decimal, rate: Decimal) -> bool:
    return abs(vat - net * rate) <= max(CENT, net * RATE_RELATIVE_TOLERANCE)


def check_rate(
    net: Decimal,
    vat: Decimal,
    allowed_rates: Iterable[object],
    *,
    allow_mixed: bool = False,
    stated_rate: Decimal | None = None,
    line: int | None = None,
) -> RateCheck:
    """Does VAT / net match an allowed rate?

    ``allow_mixed`` is for document totals without a breakdown, where
    several rates may be blended. A zero VAT is only checked when zero is
    an allowed rate; exemptions are judged elsewhere, not guessed here.
    """
    rates = allowed_rate_values(allowed_rates)
    net, vat = abs(_money(net, "net")), abs(_money(vat, "VAT"))
    stated = _rate(stated_rate) if stated_rate is not None else None
    allowed = None if stated is None else stated in rates

    def result(fit: RateFit, rate: Decimal | None = None) -> RateCheck:
        return RateCheck(fit, net, vat, rate=rate, stated_rate=stated, line=line, stated_rate_allowed=allowed)

    if not rates or (vat == 0 and ZERO not in rates):
        return result(RateFit.NOT_CHECKED)
    if stated is not None:
        if not allowed:
            return result(RateFit.UNEXPECTED)
        return result(RateFit.MATCHES, stated) if _fits(net, vat, stated) else result(RateFit.UNEXPECTED)
    match = next((r for r in rates if _fits(net, vat, r)), None)
    if match is not None:
        return result(RateFit.MATCHES, match)
    if allow_mixed and len(rates) > 1 and net and rates[0] * net <= vat <= rates[-1] * net:
        return result(RateFit.MIXED)
    return result(RateFit.UNEXPECTED)


# --------------------------------------------------------------------------- tax lines


@dataclass(frozen=True)
class TaxLine:
    """One VAT line: taxable base, VAT and (when printed) the rate as a fraction."""

    net: Decimal
    vat: Decimal
    rate: Decimal | None = None

    def __post_init__(self) -> None:
        _money(self.net, "net")
        _money(self.vat, "VAT")
        if self.rate is not None:
            _rate(self.rate)


@dataclass(frozen=True)
class TaxBreakdown:
    """The tax lines as one source states them (e.g. QR fields I/J/K, UBL TaxSubtotal)."""

    lines: tuple[TaxLine, ...]
    source: str
    method: ExtractionMethod
    confidence: float

    def __post_init__(self) -> None:
        if not self.lines:
            raise ValueError("a tax breakdown needs at least one line")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be between 0 and 1")

    @property
    def channel(self) -> str:
        return channel_token(self.method, self.source)

    @property
    def total_net(self) -> Decimal:
        """Magnitude of the lines' sum: a negative (returned, discounted) line reduces it."""
        return abs(sum((line.net for line in self.lines), ZERO))

    @property
    def total_vat(self) -> Decimal:
        return abs(sum((line.vat for line in self.lines), ZERO))


def check_tax_lines(lines: Sequence[TaxLine], allowed_rates: Iterable[object]) -> tuple[RateCheck, ...]:
    """One rate check per line (1-based ``line``)."""
    rates = allowed_rate_values(allowed_rates)
    return tuple(
        check_rate(line.net, line.vat, rates, stated_rate=line.rate, line=i)
        for i, line in enumerate(lines, start=1)
    )


# --------------------------------------------------------------------------- derived observations


@dataclass(frozen=True)
class _Amount:
    """One clear amount, where it came from, and whether it was stated or summed from tax lines."""

    value: Decimal
    lineage: frozenset[str]
    source: str
    confidence: float
    from_lines: bool = False


def _get(
    observations: Mapping[Any, Iterable[FieldObservation]], field: CriticalField
) -> list[FieldObservation]:
    return [*observations.get(field, ()), *observations.get(field.value, ())]


def _amounts(
    observations: Mapping[Any, Iterable[FieldObservation]], field: CriticalField, hints: NormalizeHints | None
) -> list[_Amount]:
    found = []
    for obs in _get(observations, field):
        value = normalize_value(field, obs.value, hints).value
        if value is not None:
            found.append(_Amount(value, lineage(obs), obs.source, obs.confidence))
    return found


def _snap(exact: Decimal, targets: Iterable[Decimal], tolerance: Decimal) -> Decimal:
    near = [t for t in set(targets) if abs(t - exact) <= tolerance]
    return min(near, key=lambda t: (abs(t - exact), t)) if near else exact


def _derive(
    exact: Decimal, inputs: Sequence[_Amount], formula: str, targets: Iterable[Decimal], tolerance: Decimal
) -> FieldObservation | None:
    if exact < 0:  # magnitudes cannot be negative; the sum check reports it instead
        return None
    tokens = set().union(*(a.lineage for a in inputs))
    return FieldObservation(
        value=_snap(exact, targets, tolerance),
        source="+".join(dict.fromkeys(a.source for a in inputs)),
        method=ExtractionMethod.ARITHMETIC,
        confidence=min(a.confidence for a in inputs),
        location=derived_location(f"{formula}={exact}", tokens),
    )


def derive_gross(
    net: FieldObservation,
    vat: FieldObservation,
    *,
    other_charges: Decimal = ZERO,
    hints: NormalizeHints | None = None,
) -> FieldObservation | None:
    """ARITHMETIC gross = net + VAT (+ signed other charges); None if either is unreadable."""
    other = _money(other_charges, "other charges")
    parts = [_amounts({_NET: [net]}, _NET, hints), _amounts({_VAT: [vat]}, _VAT, hints)]
    if not all(parts):
        return None
    n, v = parts[0][0], parts[1][0]
    return _derive(n.value + v.value + other, [n, v], _formula("net_amount+vat_amount", other), (), CENT)


def _formula(base: str, other: Decimal, op: str = "+") -> str:
    return f"{base}{op}other_charges" if other else base


def _from_breakdown(
    breakdown: TaxBreakdown, total: Decimal, name: str, targets: list[Decimal]
) -> FieldObservation | None:
    source = _Amount(total, frozenset({breakdown.channel}), breakdown.source, breakdown.confidence)
    formula = f"sum of {len(breakdown.lines)} tax lines ({name})"
    return _derive(total, [source], formula, targets, sum_tolerance(len(breakdown.lines)))


def derive_observations(
    observations_by_field: Mapping[Any, Iterable[FieldObservation]],
    *,
    breakdowns: Iterable[TaxBreakdown] = (),
    other_charges: Decimal = ZERO,
    hints: NormalizeHints | None = None,
) -> dict[CriticalField, tuple[FieldObservation, ...]]:
    """ARITHMETIC observations for net, VAT and gross (rules in the module docstring)."""
    other = _money(other_charges, "other charges")
    direct = {f: _amounts(observations_by_field, f, hints) for f in (_NET, _VAT, _GROSS)}
    targets = {f: [a.value for a in amounts] for f, amounts in direct.items()}
    derived: dict[CriticalField, list[FieldObservation]] = {_NET: [], _VAT: [], _GROSS: []}
    lines_per_channel: dict[frozenset[str], int] = {}
    for b in breakdowns:
        for field, total in ((_NET, b.total_net), (_VAT, b.total_vat)):
            obs = _from_breakdown(b, total, field.value, targets[field])
            if obs is None:
                continue
            derived[field].append(obs)
            direct[field].append(_Amount(obs.value, frozenset({b.channel}), b.source, b.confidence, True))
        key = frozenset({b.channel})
        lines_per_channel[key] = max(lines_per_channel.get(key, 1), len(b.lines))
    for (channel, _from_lines), amounts in _groups(direct).items():
        tolerance = sum_tolerance(lines_per_channel.get(channel, 1))
        for field, obs in _combine(amounts, other, targets, tolerance):
            derived[field].append(obs)
    return {f: _unique(obs) for f, obs in derived.items() if obs}


_GroupKey = tuple[frozenset[str], bool]


def _groups(
    direct: Mapping[CriticalField, list[_Amount]],
) -> dict[_GroupKey, dict[CriticalField, list[_Amount]]]:
    """Amounts by channel, keeping stated totals apart from tax-line sums so they are never mixed."""
    groups: dict[_GroupKey, dict[CriticalField, list[_Amount]]] = {}
    for field, amounts in direct.items():
        for amount in amounts:
            key = (amount.lineage, amount.from_lines)
            groups.setdefault(key, {_NET: [], _VAT: [], _GROSS: []})[field].append(amount)
    return groups


def _combine(
    amounts: Mapping[CriticalField, list[_Amount]],
    other: Decimal,
    targets: Mapping[CriticalField, list[Decimal]],
    tolerance: Decimal,
) -> list[tuple[CriticalField, FieldObservation]]:
    """Within one channel: gross from net + VAT; a missing net or VAT from the other two."""
    nets, vats, grosses = amounts[_NET], amounts[_VAT], amounts[_GROSS]
    plans = [
        (_GROSS, n.value + v.value + other, [n, v], _formula("net_amount+vat_amount", other))
        for n, v in product(nets, vats)
    ]
    if not nets:
        plans += [
            (_NET, g.value - v.value - other, [g, v], _formula("gross_amount-vat_amount", other, "-"))
            for g, v in product(grosses, vats)
        ]
    if not vats:
        plans += [
            (_VAT, g.value - n.value - other, [g, n], _formula("gross_amount-net_amount", other, "-"))
            for g, n in product(grosses, nets)
        ]
    results = []
    for field, exact, inputs, formula in plans:
        obs = _derive(exact, inputs, formula, targets[field], tolerance)
        if obs is not None:
            results.append((field, obs))
    return results


def _unique(observations: list[FieldObservation]) -> tuple[FieldObservation, ...]:
    seen: dict[tuple[Any, frozenset[str]], FieldObservation] = {}
    for obs in observations:
        seen.setdefault((obs.value, lineage(obs)), obs)
    return tuple(seen.values())
