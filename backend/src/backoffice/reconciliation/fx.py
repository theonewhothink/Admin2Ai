"""Reference exchange rates for FX matching when the bank declares no conversion (§20).

Bank-declared FX (:class:`~backoffice.reconciliation.bank.FxDetails`) always
wins. Reference rates only make an FX match *plausible* (AMBER, §57): the spread
a card network or bank applies is unknown, so the difference is reported as an
estimated conversion cost, never hidden.

Adapters sit behind :class:`FxRateSource`. :class:`EcbFxRates` is a real client
for the European Central Bank data portal (euro foreign exchange reference
rates, series ``EXR/D.<CUR>.EUR.SP00.A`` = units of <CUR> per 1 EUR). httpx is
imported lazily so the package never needs it unless this adapter is used.
"""

from __future__ import annotations

import csv
import io
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, localcontext
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from ._text import currency_code, is_currency_code

if TYPE_CHECKING:  # pragma: no cover - typing only
    import httpx

__all__ = [
    "DatedFxRates",
    "EcbConfig",
    "EcbFxRates",
    "FxRateSource",
    "FxRateUnavailable",
    "StaticFxRates",
    "checked_code",
    "divide",
]

logger = logging.getLogger(__name__)
_ONE = Decimal(1)


class FxRateUnavailable(Exception):
    """The rate source could not be reached (developer-facing, never shown to owners)."""


@runtime_checkable
class FxRateSource(Protocol):
    def rate(self, base: str, quote: str, on: date) -> Decimal | None:
        """Units of ``quote`` for one unit of ``base`` on ``on`` (None when unknown).

        May raise :class:`FxRateUnavailable` when the source cannot be reached.
        """
        ...


def checked_code(value: str) -> str:
    """Normalized ISO code, or ValueError. Codes end up in request URLs, so
    nothing but three letters is ever let through."""
    if not is_currency_code(value):
        raise ValueError("not a currency code")
    return currency_code(value)


def divide(numerator: Decimal, denominator: Decimal) -> Decimal:
    """Exact-enough Decimal division (28 significant digits), never float."""
    with localcontext() as ctx:
        ctx.prec = 28
        return numerator / denominator


class DatedFxRates:
    """Reference rates from a fixed table {(base, quote, day): rate}, like the ECB's: a day without a rate
    (a weekend, a holiday) takes the latest one up to ``lookback_days`` before it; inverses are derived.

    Useful for tests, and for a table of reference rates loaded once (no network).
    """

    def __init__(self, rates: Mapping[tuple[str, str, date], Decimal], lookback_days: int = 7) -> None:
        table: dict[tuple[str, str], dict[date, Decimal]] = {}
        for (base, quote, day), value in rates.items():
            if isinstance(value, float) or not isinstance(value, Decimal) or value <= 0:
                raise ValueError("rates must be positive Decimals")
            table.setdefault((checked_code(base), checked_code(quote)), {})[day] = value
        self._rates = table
        self.lookback_days = lookback_days

    def _on(self, base: str, quote: str, on: date) -> Decimal | None:
        days = self._rates.get((base, quote))
        if not days:
            return None
        for back in range(self.lookback_days + 1):
            found = days.get(on - timedelta(days=back))
            if found is not None:
                return found
        return None

    def rate(self, base: str, quote: str, on: date) -> Decimal | None:
        base, quote = checked_code(base), checked_code(quote)
        if base == quote:
            return _ONE
        direct = self._on(base, quote, on)
        if direct is not None:
            return direct
        inverse = self._on(quote, base, on)
        return divide(_ONE, inverse) if inverse is not None else None


class StaticFxRates:
    """Fixed rates from a mapping {(base, quote): rate}; inverses are derived.

    Useful for tests and for rates a connector already supplied.
    """

    def __init__(self, rates: Mapping[tuple[str, str], Decimal]) -> None:
        table: dict[tuple[str, str], Decimal] = {}
        for (base, quote), value in rates.items():
            if isinstance(value, float) or not isinstance(value, Decimal) or value <= 0:
                raise ValueError("rates must be positive Decimals")
            table[(checked_code(base), checked_code(quote))] = value
        self._rates = table

    def rate(self, base: str, quote: str, on: date) -> Decimal | None:
        base, quote = checked_code(base), checked_code(quote)
        if base == quote:
            return _ONE
        direct = self._rates.get((base, quote))
        if direct is not None:
            return direct
        inverse = self._rates.get((quote, base))
        return divide(_ONE, inverse) if inverse is not None else None


@dataclass(frozen=True)
class EcbConfig:
    """ECB data portal settings. Rates are published on TARGET business days only,
    so ``lookback_days`` covers weekends and holidays."""

    base_url: str = "https://data-api.ecb.europa.eu/service/data/EXR"
    timeout_seconds: float = 10.0
    lookback_days: int = 7


class EcbFxRates:
    """Euro reference rates from the ECB data portal (SDMX REST, CSV format).

    Cross rates go through the euro. Results are cached per (currency, date).
    Pass ``client`` (an ``httpx.Client``) to control transport, proxies or tests.
    """

    def __init__(
        self, config: EcbConfig | None = None, client: httpx.Client | None = None
    ) -> None:
        import httpx  # optional dependency, imported only for this adapter

        self.config = config or EcbConfig()
        self._transport_error: type[Exception] = httpx.HTTPError
        self._client = client or httpx.Client(timeout=self.config.timeout_seconds)
        self._cache: dict[tuple[str, date], Decimal | None] = {}

    def close(self) -> None:
        self._client.close()

    def rate(self, base: str, quote: str, on: date) -> Decimal | None:
        """Units of ``quote`` per ``base``; ValueError for anything but ISO codes."""
        base, quote = checked_code(base), checked_code(quote)
        if base == quote:
            return _ONE
        per_eur_base = self._per_eur(base, on)
        per_eur_quote = self._per_eur(quote, on)
        if per_eur_base is None or per_eur_quote is None:
            return None
        return divide(per_eur_quote, per_eur_base)

    def _per_eur(self, currency: str, on: date) -> Decimal | None:
        if currency == "EUR":
            return _ONE
        key = (currency, on)
        if key not in self._cache:
            self._cache[key] = self._fetch(currency, on)
        return self._cache[key]

    def _fetch(self, currency: str, on: date) -> Decimal | None:
        start = on - timedelta(days=self.config.lookback_days)
        url = f"{self.config.base_url}/D.{currency}.EUR.SP00.A"
        params = {
            "startPeriod": start.isoformat(),
            "endPeriod": on.isoformat(),
            "format": "csvdata",
        }
        try:
            response = self._client.get(url, params=params)
        except self._transport_error as exc:
            raise FxRateUnavailable(f"ECB request failed for {currency}") from exc
        if response.status_code == 404:  # the ECB answers 404 when no data exists
            return None
        if response.status_code != 200:
            raise FxRateUnavailable(f"ECB answered HTTP {response.status_code}")
        return _latest_observation(response.text, on)


def _latest_observation(body: str, on: date) -> Decimal | None:
    """Latest OBS_VALUE with TIME_PERIOD on or before ``on`` from SDMX CSV."""
    reader = csv.DictReader(io.StringIO(body))
    if not reader.fieldnames or not {"TIME_PERIOD", "OBS_VALUE"} <= set(
        reader.fieldnames
    ):
        raise FxRateUnavailable("unexpected ECB response format")
    best: tuple[date, Decimal] | None = None
    for row in reader:
        try:
            day = date.fromisoformat(row["TIME_PERIOD"])
            value = Decimal(row["OBS_VALUE"])
        except (ValueError, InvalidOperation):
            logger.debug("skipping unreadable ECB row %r", row)
            continue
        if not value.is_finite():  # 'NaN' marks a missing observation
            continue
        if day <= on and value > 0 and (best is None or day > best[0]):
            best = (day, value)
    return best[1] if best else None
