"""Canonical values for critical fields (§18).

Every source writes numbers, dates and identifiers its own way: "1.492,30",
"1,492.30", "€ 1 492,30" and ``Decimal("1492.3")`` are one amount. Agreement
between sources (§18-19) can only be judged on one canonical form, so every
observation passes through :func:`normalize_value` first.

Normalization never guesses. A value that can be read more than one way
("1.492" without a locale, "03/04/2026" without a day order) comes back
*ambiguous* with every reading listed; a :class:`NormalizeHints` resolves it.
A value that fails a check digit (IBAN, RF reference) is *invalid*, not
"close enough".

Canonical forms:

* amounts: ``Decimal`` quantized to cents. Critical amount fields are
  magnitudes; the sign of a document follows its type (see
  ``Document.signed_gross``), so ``"-483,60"`` and ``"483.60"`` agree.
* currency: ISO 4217 code.
* dates: ``datetime.date`` (a datetime keeps its own calendar date).
* tax ids: upper-case alphanumerics without country prefix or spaces.
* IBAN: upper-case, no spaces, mod-97 checked.
* invoice numbers: upper-case with single spaces and the series kept
  ("FT 2026/183"); compared without spaces (:func:`invoice_number_key`).
* payment references: upper-case alphanumerics, RF check digits verified.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from enum import Enum
from types import MappingProxyType
from typing import Any

from backoffice.domain.models import CriticalField

__all__ = [
    "AMOUNT_FIELDS",
    "CENT",
    "DATE_FIELDS",
    "CurrencyMark",
    "currency_mark",
    "IBAN_LENGTHS",
    "ISO_4217",
    "Normalized",
    "NormalizeHints",
    "NormNote",
    "as_critical_field",
    "comparison_key",
    "fails_check_digits",
    "field_name",
    "iban_is_valid",
    "invoice_number_key",
    "normalize_amount",
    "normalize_currency",
    "normalize_date",
    "normalize_iban",
    "normalize_invoice_number",
    "normalize_payment_reference",
    "normalize_tax_id",
    "normalize_text",
    "normalize_value",
    "split_tax_id",
]

CENT = Decimal("0.01")

AMOUNT_FIELDS: frozenset[CriticalField] = frozenset(
    {CriticalField.GROSS_AMOUNT, CriticalField.NET_AMOUNT, CriticalField.VAT_AMOUNT}
)
DATE_FIELDS: frozenset[CriticalField] = frozenset({CriticalField.ISSUE_DATE, CriticalField.DUE_DATE})
TAX_ID_FIELDS: frozenset[CriticalField] = frozenset(
    {CriticalField.SUPPLIER_TAX_ID, CriticalField.CUSTOMER_TAX_ID}
)


# --------------------------------------------------------------------------- results


class NormNote(str, Enum):
    """Why a value is not simply clear."""

    UNREADABLE = "unreadable"  # not a value of this kind at all
    AMBIGUOUS = "ambiguous"  # more than one reading; see candidates
    INVALID = "invalid"  # shaped right, but a check digit or length rule fails
    UNKNOWN_CODE = "unknown_code"  # e.g. a three-letter code that is not ISO 4217
    ROUNDED = "rounded"  # an amount had digits below one cent


@dataclass(frozen=True)
class Normalized:
    """Outcome of normalizing one raw value.

    ``candidates`` is empty when unreadable, has one item when clear and
    several when ambiguous. Nothing downstream may pick among several.
    """

    candidates: tuple[Any, ...] = ()
    note: NormNote | None = None

    @property
    def value(self) -> Any | None:
        return self.candidates[0] if len(self.candidates) == 1 else None

    @property
    def readable(self) -> bool:
        return bool(self.candidates)

    @property
    def clear(self) -> bool:
        return len(self.candidates) == 1

    @property
    def ambiguous(self) -> bool:
        return len(self.candidates) > 1


def _clear(value: Any, note: NormNote | None = None) -> Normalized:
    return Normalized((value,), note)


def _unreadable(note: NormNote = NormNote.UNREADABLE) -> Normalized:
    return Normalized((), note)


def _choices(values: list[Any], note: NormNote | None = None) -> Normalized:
    unique = tuple(dict.fromkeys(values))
    if len(unique) > 1:
        return Normalized(unique, NormNote.AMBIGUOUS)
    return Normalized(unique, note)


# --------------------------------------------------------------------------- hints

# Decimal separator and day order by locale (CLDR conventions). verified_as_of
# 2026-09-27 from the author's knowledge of CLDR, not re-checked against a
# live release. Locales not listed resolve to None: the caller must say.
_LOCALE_DECIMAL: Mapping[str, str] = MappingProxyType({
    "pt": ",", "es": ",", "fr": ",", "it": ",", "de": ",", "nl": ",",
    "en": ".", "he": ".", "iw": ".",
    "de-ch": ".", "de-li": ".", "es-mx": ".", "en-za": ",",
})  # fmt: skip
_LOCALE_DAY_FIRST: Mapping[str, bool] = MappingProxyType({
    "pt": True, "es": True, "fr": True, "it": True, "de": True, "nl": True,
    "he": True, "iw": True,
    "en-gb": True, "en-ie": True, "en-au": True, "en-nz": True, "en-za": True,
    "en-us": False,
})  # fmt: skip


def _locale_lookup(table: Mapping[str, Any], locale: str | None) -> Any | None:
    if not locale:
        return None
    tag = locale.strip().lower().replace("_", "-")
    if tag in table:
        return table[tag]
    return table.get(tag.split("-", 1)[0])


@dataclass(frozen=True)
class NormalizeHints:
    """How to read ambiguous numbers and dates.

    Explicit ``decimal_separator`` / ``day_first`` win over ``locale``
    (e.g. "pt-PT"). Hints only break ties: a value whose own shape is
    unambiguous ("1,492.30", "18/09/2026") is read by its shape.
    """

    locale: str | None = None
    decimal_separator: str | None = None
    day_first: bool | None = None

    def __post_init__(self) -> None:
        if self.decimal_separator not in (None, ",", "."):
            raise ValueError("decimal_separator must be ',' or '.'")

    def resolved_decimal_separator(self) -> str | None:
        if self.decimal_separator is not None:
            return self.decimal_separator
        return _locale_lookup(_LOCALE_DECIMAL, self.locale)

    def resolved_day_first(self) -> bool | None:
        if self.day_first is not None:
            return self.day_first
        return _locale_lookup(_LOCALE_DAY_FIRST, self.locale)


NO_HINTS = NormalizeHints()


# --------------------------------------------------------------------------- text helpers

_DASHES = str.maketrans({c: "-" for c in "‐‑‒–—―−﹘﹣－"})
_SLASHES = str.maketrans({c: "/" for c in "⁄∕／"})


def _nfkc(text: str) -> str:
    return unicodedata.normalize("NFKC", text).translate(_DASHES).translate(_SLASHES)


def _fold(text: str) -> str:
    """Lower-case, accent-free, single-spaced."""
    decomposed = unicodedata.normalize("NFKD", _nfkc(text).casefold())
    plain = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(plain.split())


def normalize_text(value: object) -> Normalized:
    """Fallback for fields without a specific rule: single-spaced text."""
    if value is None or isinstance(value, (list, tuple, set, dict)):
        return _unreadable()
    if isinstance(value, str):
        text = " ".join(_nfkc(value).split())
        return _clear(text) if text else _unreadable()
    return _clear(value)


# --------------------------------------------------------------------------- amounts

_NUMBER = re.compile(r"(?P<head>[^\d]*?)(?P<num>\d(?:[\d.,'’ ]*\d)?)(?P<tail>[^\d]*)")
_SPACE_GROUP = " '’"


def normalize_amount(value: object, *, decimal_separator: str | None = None) -> Normalized:
    """Money as ``Decimal`` quantized to cents, sign kept (§18).

    Strings may carry a currency symbol or code, grouping by ".", ",",
    space or apostrophe, a leading/trailing minus or accounting parentheses.
    A lone separator followed by exactly three digits ("1.492") is
    ambiguous unless ``decimal_separator`` says which it is. Floats are
    read through their shortest repr, never through binary arithmetic.
    """
    if isinstance(value, bool):
        return _unreadable()
    if isinstance(value, (Decimal, int, float)):
        return _from_number(value)
    if not isinstance(value, str):
        return _unreadable()
    return _from_text(value, decimal_separator)


def _from_number(value: Decimal | int | float) -> Normalized:
    try:
        number = Decimal(repr(value)) if isinstance(value, float) else Decimal(value)
    except InvalidOperation:
        return _unreadable()
    if not number.is_finite():
        return _unreadable()
    return _quantized([number])


def _quantized(numbers: list[Decimal]) -> Normalized:
    cents = [n.quantize(CENT, rounding=ROUND_HALF_UP) for n in numbers]
    rounded = any(c != n for c, n in zip(cents, numbers))
    return _choices(cents, NormNote.ROUNDED if rounded else None)


def _from_text(text: str, decimal_separator: str | None) -> Normalized:
    match = _NUMBER.fullmatch(_nfkc(text).strip())
    if match is None:
        return _unreadable()
    negative = _sign(match.group("head"), match.group("tail"))
    if negative is None:
        return _unreadable()
    numbers = _parse_number(match.group("num"), decimal_separator)
    if numbers is None:
        return _unreadable()
    return _quantized([-n if negative else n for n in numbers])


def _sign(head: str, tail: str) -> bool | None:
    """True for negative, False for positive, None when the wrapping is not money."""
    head, tail = head.replace(" ", ""), tail.replace(" ", "")
    minus = head.count("-") + tail.count("-")
    parens = head.count("(") + tail.count(")")
    if minus > 1 or head.count("+") > 1 or parens not in (0, 2) or (minus and parens):
        return None
    if parens and not (head.count("(") == 1 and tail.count(")") == 1):
        return None
    rest = [part.strip("-+()") for part in (head, tail)]
    if any(ch in "-+()" for part in rest for ch in part):
        return None
    labels = [part for part in rest if part]
    if len(labels) > 1 or (labels and not normalize_currency(labels[0]).readable):
        return None
    return bool(minus or parens)


@dataclass(frozen=True)
class CurrencyMark:
    """A currency written next to an amount: ``written`` as printed, ``codes`` it may stand for."""

    written: str
    codes: tuple[str, ...]

    def compatible(self, other: CurrencyMark) -> bool:
        return not set(self.codes).isdisjoint(other.codes)


def currency_mark(value: object) -> CurrencyMark | None:
    """The currency printed with an amount ("483,60 €" -> EUR), or None when none is printed.

    Only readable amounts count: "$483.60" and "€483.60" are the same
    number in two currencies, which verification must not call agreement
    (§19). ``"$"`` keeps every currency it may stand for.
    """
    if not isinstance(value, str) or not normalize_amount(value).readable:
        return None
    match = _NUMBER.fullmatch(_nfkc(value).strip())
    if match is None:  # pragma: no cover - readable amounts always match
        return None
    labels = [p.replace(" ", "").strip("-+()") for p in (match.group("head"), match.group("tail"))]
    written = next((label for label in labels if label), None)
    if written is None:
        return None
    return CurrencyMark(written, normalize_currency(written).candidates)


def _parse_number(num: str, hint: str | None) -> list[Decimal] | None:
    has_dot, has_comma = "." in num, "," in num
    if has_dot and has_comma:
        decimal = "." if num.rfind(".") > num.rfind(",") else ","
        return _with_decimal(num, decimal, group=("," if decimal == "." else "."))
    if has_dot or has_comma:
        return _single_separator(num, "." if has_dot else ",", hint)
    return [Decimal(_digits(num))] if _grouped(num, _SPACE_GROUP) else None


def _with_decimal(num: str, decimal: str, group: str) -> list[Decimal] | None:
    whole, frac = num.rsplit(decimal, 1)
    if decimal in whole or not frac.isdigit() or not _grouped(whole, group + _SPACE_GROUP):
        return None
    return [Decimal(f"{_digits(whole)}.{frac}")]


def _single_separator(num: str, sep: str, hint: str | None) -> list[Decimal] | None:
    parts = num.split(sep)
    if len(parts) > 2:  # "1.234.567": a repeated separator can only group
        return [Decimal(_digits(num))] if _grouped(num, sep) else None
    whole, frac = parts
    if not frac.isdigit() or not _grouped(whole, _SPACE_GROUP):
        return None
    as_decimal = Decimal(f"{_digits(whole)}.{frac}")
    if len(frac) != 3 or not _can_be_group_head(whole):
        return [as_decimal]
    as_group = Decimal(_digits(whole) + frac)
    if hint == sep:
        return [as_decimal]
    if hint is not None:
        return [as_group]
    return [as_group, as_decimal]  # "1.492": thousands or decimals; say both


def _can_be_group_head(whole: str) -> bool:
    return whole.isdigit() and 1 <= len(whole) <= 3 and not whole.startswith("0")


def _grouped(text: str, separators: str) -> bool:
    """Digits, optionally grouped in threes by any of ``separators``."""
    parts = re.split("[" + re.escape(separators) + "]", text) if separators else [text]
    if not all(p.isdigit() for p in parts):
        return False
    if len(parts) == 1:
        return True
    head, rest = parts[0], parts[1:]
    return 1 <= len(head) <= 3 and not head.startswith("0") and all(len(p) == 3 for p in rest)


def _digits(text: str) -> str:
    return "".join(c for c in text if c.isdigit())


# --------------------------------------------------------------------------- currency

# Active ISO 4217 codes, plus recently withdrawn ones still seen on older
# documents (ANG, BGN, HRK, SLL, ZWL). verified_as_of 2026-09-27 from the
# author's knowledge of the SIX list; not re-checked online. Fund and metal
# codes are left out on purpose.
ISO_4217: frozenset[str] = frozenset(
    """
AED AFN ALL AMD ANG AOA ARS AUD AWG AZN BAM BBD BDT BGN BHD BIF BMD BND BOB BRL
BSD BTN BWP BYN BZD CAD CDF CHF CLP CNY COP CRC CUP CVE CZK DJF DKK DOP DZD EGP
ERN ETB EUR FJD FKP GBP GEL GHS GIP GMD GNF GTQ GYD HKD HNL HRK HTG HUF IDR ILS
INR IQD IRR ISK JMD JOD JPY KES KGS KHR KMF KPW KRW KWD KYD KZT LAK LBP LKR LRD
LSL LYD MAD MDL MGA MKD MMK MNT MOP MRU MUR MVR MWK MXN MYR MZN NAD NGN NIO NOK
NPR NZD OMR PAB PEN PGK PHP PKR PLN PYG QAR RON RSD RUB RWF SAR SBD SCR SDG SEK
SGD SHP SLE SLL SOS SRD SSP STN SVC SYP SZL THB TJS TMT TND TOP TRY TTD TWD TZS
UAH UGX USD UYU UZS VED VES VND VUV WST XAF XCD XCG XOF XPF YER ZAR ZMW ZWG ZWL
""".split()
)

# Symbols and names. A symbol shared by several currencies stays ambiguous.
_CURRENCY_MARKS: Mapping[str, tuple[str, ...]] = MappingProxyType({
    "€": ("EUR",), "EURO": ("EUR",), "EUROS": ("EUR",),
    "£": ("GBP",), "₪": ("ILS",), "NIS": ("ILS",), "₹": ("INR",), "₩": ("KRW",),
    "₺": ("TRY",), "ZŁ": ("PLN",), "KČ": ("CZK",), "FT": ("HUF",), "LEI": ("RON",),
    "SFR": ("CHF",), "US$": ("USD",), "R$": ("BRL",), "C$": ("CAD",), "CA$": ("CAD",),
    "A$": ("AUD",), "AU$": ("AUD",), "NZ$": ("NZD",), "HK$": ("HKD",), "S$": ("SGD",),
    "$": ("USD", "CAD", "AUD", "NZD", "SGD", "HKD", "MXN"),
    "¥": ("JPY", "CNY"),
    "KR": ("SEK", "NOK", "DKK", "ISK"),
})  # fmt: skip


def normalize_currency(value: object) -> Normalized:
    """ISO 4217 code from a code, symbol or name ("€", "eur", "Euros")."""
    if not isinstance(value, str):
        return _unreadable()
    key = "".join(_nfkc(value).split()).upper().rstrip(".")
    if not key:
        return _unreadable()
    if key in _CURRENCY_MARKS:
        return _choices(list(_CURRENCY_MARKS[key]))
    if re.fullmatch(r"[A-Z]{3}", key):
        return _clear(key) if key in ISO_4217 else _unreadable(NormNote.UNKNOWN_CODE)
    return _unreadable()


# --------------------------------------------------------------------------- dates

_MONTH_NAMES: dict[int, tuple[str, ...]] = {
    1: ("january", "jan", "janeiro", "enero", "ene", "janvier", "janv", "gennaio", "gen",
        "januar", "janner", "januari"),
    2: ("february", "feb", "fevereiro", "fev", "febrero", "fevrier", "fevr", "febbraio",
        "februar", "februari"),
    3: ("march", "mar", "marco", "marzo", "mars", "marz", "mrz", "maart"),
    4: ("april", "apr", "abril", "abr", "avril", "avr", "aprile"),
    5: ("may", "maio", "mayo", "mai", "maggio", "mag", "mei"),
    6: ("june", "jun", "junho", "junio", "juin", "giugno", "giu", "juni"),
    7: ("july", "jul", "julho", "julio", "juillet", "juil", "luglio", "lug", "juli"),
    8: ("august", "aug", "agosto", "ago", "aout", "augustus"),
    9: ("september", "sep", "sept", "setembro", "set", "septiembre", "septembre",
        "settembre"),
    10: ("october", "oct", "outubro", "out", "octubre", "octobre", "ottobre", "ott",
         "oktober", "okt"),
    11: ("november", "nov", "novembro", "noviembre", "novembre"),
    12: ("december", "dec", "dezembro", "dez", "diciembre", "dic", "decembre", "dicembre",
         "dezember"),
}  # fmt: skip
MONTHS: Mapping[str, int] = MappingProxyType(
    {name: number for number, names in _MONTH_NAMES.items() for name in names}
)

_WEEKDAY = re.compile(
    r"^(?:mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)(?:day|sday|nesday|urday|rsday)?\.?,?\s+"
)
_TIME = re.compile(r"[t ]\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?\s*(?:am|pm)?\s*(?:z|utc|gmt|[+-]\d{2}:?\d{2})?$")
_ISO = re.compile(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})")
_COMPACT = re.compile(r"(\d{4})(\d{2})(\d{2})")
_NUMERIC = re.compile(r"(\d{1,2})([/.-])(\d{1,2})\2(\d{4}|\d{2})")
_TEXT_DMY = re.compile(
    r"(\d{1,2})(?:st|nd|rd|th|o)?\.?(?:\s*de\s+|[\s/.-]+)([a-z]+)\.?(?:\s+de\s+|[\s/.,-]+)(\d{4}|\d{2})"
)
_TEXT_MDY = re.compile(r"([a-z]+)\.?[\s/.-]*(\d{1,2})(?:st|nd|rd|th)?,?[\s/.-]+(\d{4})")
_YEARS = range(1900, 2201)


def normalize_date(value: object, *, day_first: bool | None = None) -> Normalized:
    """A calendar date from ISO, YYYYMMDD, numeric or month-name text.

    Numeric dates whose day and month could swap ("03/04/2026",
    "03.04.2026") are ambiguous unless ``day_first`` is given; a reading
    that is impossible ("09/18/2026" day-first) is simply dropped. Dots are
    not taken as a day-first promise: the order is resolved only by the
    value itself or a hint (§18). Two-digit years mean 2000-2099. A
    datetime keeps its own calendar date.
    """
    if isinstance(value, datetime):
        return _clear(value.date())
    if isinstance(value, date):
        return _clear(value)
    if not isinstance(value, str):
        return _unreadable()
    text = _TIME.sub("", _WEEKDAY.sub("", _fold(value))).strip()
    for parse in (_iso_date, _numeric_date, _text_date):
        result = parse(text, day_first)
        if result is not None:
            return result
    return _unreadable()


def _safe_date(year: int, month: int, day: int) -> date | None:
    if year not in _YEARS:
        return None
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _one(found: date | None) -> Normalized:
    return _clear(found) if found else _unreadable()


def _iso_date(text: str, _day_first: bool | None) -> Normalized | None:
    match = _ISO.fullmatch(text) or _COMPACT.fullmatch(text)
    if match is None:
        return None
    year, month, day = (int(g) for g in match.groups())
    return _one(_safe_date(year, month, day))


def _year(text: str) -> int:
    return 2000 + int(text) if len(text) == 2 else int(text)


def _numeric_date(text: str, day_first: bool | None) -> Normalized | None:
    match = _NUMERIC.fullmatch(text)
    if match is None:
        return None
    a, b, y = int(match.group(1)), int(match.group(3)), _year(match.group(4))
    dmy = _safe_date(y, b, a)
    mdy = _safe_date(y, a, b)
    readings = [d for d in dict.fromkeys((dmy, mdy)) if d is not None]
    if len(readings) < 2:
        return _one(readings[0] if readings else None)
    if day_first is None:
        return _choices(readings)
    return _clear(dmy if day_first else mdy)


def _text_date(text: str, _day_first: bool | None) -> Normalized | None:
    match = _TEXT_DMY.fullmatch(text)
    if match is not None:
        day, month, year = match.group(1), match.group(2), match.group(3)
    else:
        match = _TEXT_MDY.fullmatch(text)
        if match is None:
            return None
        month, day, year = match.group(1), match.group(2), match.group(3)
    number = MONTHS.get(month)
    return _one(_safe_date(_year(year), number, int(day)) if number else None)


# --------------------------------------------------------------------------- tax ids

# Country prefixes written in front of VAT numbers: the EU VIES prefixes
# (Greece is "EL"; "XI" is Northern Ireland) plus common non-EU ones.
_TAX_PREFIXES = frozenset(
    "AT BE BG CY CZ DE DK EE EL ES FI FR HR HU IE IT LT LU LV MT NL PL PT RO SE SI SK XI "
    "GB GR NO CH IS LI".split()
)
_TAX_SUFFIXES = ("MWST", "TVA", "IVA", "MVA")
# Labels printed in front of the number ("NIF: 503 504 564", "USt-IdNr.
# DE..."). Longest first; none is a country prefix, so none can eat one.
_TAX_LABELS = tuple(
    sorted(
        "NUMERODECONTRIBUINTE CONTRIBUINTE VATREGNO VATREG VATNUMBER VATNO VATID VAT "
        "USTIDNR USTID UIDNR NIPC NIF NIE CIF PIVA BTW".split(),
        key=len,
        reverse=True,
    )
)
_TAX_ID = re.compile(r"[A-Z0-9]{5,15}")


def _strip_label(compact: str) -> str:
    for label in _TAX_LABELS:
        rest = compact[len(label) :]
        if compact.startswith(label) and any(c.isdigit() for c in rest):
            return rest
    return compact


def split_tax_id(value: object) -> tuple[str | None, str | None]:
    """(country prefix or None, bare number or None). "PT 123 456 789" -> ("PT", "123456789").

    A printed label ("NIF:", "VAT No.") is not part of the number.
    """
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None, None
    compact = _strip_label(re.sub(r"[^A-Z0-9]", "", _fold(str(value)).upper()))
    for suffix in _TAX_SUFFIXES:
        if compact.endswith(suffix) and any(c.isdigit() for c in compact[: -len(suffix)]):
            compact = compact[: -len(suffix)]
    country, number = _strip_prefix(compact)
    if not _TAX_ID.fullmatch(number) or not any(c.isdigit() for c in number):
        return None, None
    return country, number


def _strip_prefix(compact: str) -> tuple[str | None, str]:
    if compact.startswith("CHE") and any(c.isdigit() for c in compact[3:]):
        return "CH", compact[3:]
    head, rest = compact[:2], compact[2:]
    if head in _TAX_PREFIXES and any(c.isdigit() for c in rest):
        return ("GR" if head == "EL" else head), rest
    return None, compact


def normalize_tax_id(value: object) -> Normalized:
    """Tax / VAT id without country prefix, spaces or punctuation.

    Two sources that print "PT123456789" and "123 456 789" agree. The
    country itself is not compared here (see :func:`split_tax_id`).
    """
    _country, number = split_tax_id(value)
    return _clear(number) if number else _unreadable()


# --------------------------------------------------------------------------- IBAN

# IBAN length per country (SWIFT IBAN registry). verified_as_of 2026-09-27
# from the author's knowledge of the registry; not re-checked online.
# Countries not listed are accepted on the mod-97 check alone.
IBAN_LENGTHS: Mapping[str, int] = MappingProxyType({
    "AD": 24, "AE": 23, "AL": 28, "AT": 20, "AZ": 28, "BA": 20, "BE": 16, "BG": 22,
    "BH": 22, "BR": 29, "BY": 28, "CH": 21, "CR": 22, "CY": 28, "CZ": 24, "DE": 22,
    "DK": 18, "DO": 28, "EE": 20, "EG": 29, "ES": 24, "FI": 18, "FO": 18, "FR": 27,
    "GB": 22, "GE": 22, "GI": 23, "GL": 18, "GR": 27, "GT": 28, "HR": 21, "HU": 28,
    "IE": 22, "IL": 23, "IQ": 23, "IS": 26, "IT": 27, "JO": 30, "KW": 30, "KZ": 20,
    "LB": 28, "LC": 32, "LI": 21, "LT": 20, "LU": 20, "LV": 21, "MC": 27, "MD": 24,
    "ME": 22, "MK": 19, "MR": 27, "MT": 31, "MU": 30, "NL": 18, "NO": 15, "PK": 24,
    "PL": 28, "PS": 29, "PT": 25, "QA": 29, "RO": 24, "RS": 22, "SA": 24, "SC": 31,
    "SE": 24, "SI": 19, "SK": 24, "SM": 27, "ST": 25, "SV": 28, "TL": 23, "TN": 24,
    "TR": 26, "UA": 29, "VA": 22, "VG": 24, "XK": 20,
})  # fmt: skip
_IBAN_SHAPE = re.compile(r"[A-Z]{2}\d{2}[A-Z0-9]{11,30}")


def _mod97(text: str) -> int:
    """ISO 7064 MOD 97-10 remainder with letters as 10..35."""
    rearranged = text[4:] + text[:4]
    return int("".join(str(int(c, 36)) for c in rearranged)) % 97


def _iban_compact(value: str) -> str:
    return re.sub(r"[\s.:-]", "", _nfkc(value).upper()).removeprefix("IBAN")


def normalize_iban(value: object) -> Normalized:
    """Upper-case IBAN without spaces; INVALID when length or mod-97 fails."""
    if not isinstance(value, str):
        return _unreadable()
    compact = _iban_compact(value)
    if not _IBAN_SHAPE.fullmatch(compact):
        return _unreadable()
    expected = IBAN_LENGTHS.get(compact[:2])
    if (expected is not None and len(compact) != expected) or _mod97(compact) != 1:
        return _unreadable(NormNote.INVALID)
    return _clear(compact)


def iban_is_valid(value: object) -> bool:
    return normalize_iban(value).clear


def fails_check_digits(name: CriticalField | str, value: object) -> bool:
    """A complete IBAN or RF reference whose own check digits are wrong.

    Unlike a truncated or garbled value (a partial read), this is a
    different value stated in full: a typo on the document, or an edit.
    """
    if not isinstance(value, str):
        return False
    field = as_critical_field(name)
    if field is CriticalField.IBAN:
        compact = _iban_compact(value)
        expected = IBAN_LENGTHS.get(compact[:2], len(compact))
        return bool(_IBAN_SHAPE.fullmatch(compact)) and len(compact) == expected and _mod97(compact) != 1
    if field is CriticalField.PAYMENT_REFERENCE:
        compact = re.sub(r"[\s./-]", "", _nfkc(value).upper())
        return bool(_RF.fullmatch(compact)) and _mod97(compact) != 1
    return False


# --------------------------------------------------------------------------- invoice numbers, references


def normalize_invoice_number(value: object) -> Normalized:
    """Upper-case, single-spaced, series kept: " ft 2026 / 183 " -> "FT 2026/183"."""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return _unreadable()
    text = " ".join(_nfkc(str(value)).upper().split()).lstrip("#").strip()
    text = re.sub(r"\s*([/-])\s*", r"\1", text)
    return _clear(text) if any(c.isalnum() for c in text) else _unreadable()


def invoice_number_key(value: str) -> str:
    """Comparison form: "FT 2026/183" and "FT2026/183" are one number."""
    return "".join(value.split())


_RF = re.compile(r"RF\d{2}[A-Z0-9]{1,21}")


def normalize_payment_reference(value: object) -> Normalized:
    """Upper-case alphanumerics; ISO 11649 "RF" references must pass mod-97."""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return _unreadable()
    compact = re.sub(r"[\s./-]", "", _nfkc(str(value)).upper())
    if not re.fullmatch(r"[A-Z0-9]+", compact):
        return _unreadable()
    if _RF.fullmatch(compact) and _mod97(compact) != 1:
        return _unreadable(NormNote.INVALID)
    return _clear(compact)


# --------------------------------------------------------------------------- dispatch


def as_critical_field(name: CriticalField | str) -> CriticalField | None:
    if isinstance(name, CriticalField):
        return name
    try:
        return CriticalField(name)
    except ValueError:
        return None


def field_name(name: CriticalField | str) -> str:
    return name.value if isinstance(name, CriticalField) else str(name)


def _magnitudes(result: Normalized) -> Normalized:
    if not result.readable:
        return result
    return _choices([abs(c) for c in result.candidates], result.note)


def normalize_value(
    name: CriticalField | str, value: object, hints: NormalizeHints | None = None
) -> Normalized:
    """Canonical value(s) of ``value`` for field ``name`` (rules in the module docstring)."""
    hints = hints or NO_HINTS
    field = as_critical_field(name)
    if field in AMOUNT_FIELDS:
        return _magnitudes(normalize_amount(value, decimal_separator=hints.resolved_decimal_separator()))
    if field in DATE_FIELDS:
        return normalize_date(value, day_first=hints.resolved_day_first())
    rule = _RULES.get(field) if field is not None else None
    return rule(value) if rule is not None else normalize_text(value)


_RULES: Mapping[CriticalField, Callable[[object], Normalized]] = MappingProxyType({
    CriticalField.CURRENCY: normalize_currency,
    CriticalField.SUPPLIER_TAX_ID: normalize_tax_id,
    CriticalField.CUSTOMER_TAX_ID: normalize_tax_id,
    CriticalField.IBAN: normalize_iban,
    CriticalField.INVOICE_NUMBER: normalize_invoice_number,
    CriticalField.PAYMENT_REFERENCE: normalize_payment_reference,
})  # fmt: skip


def comparison_key(name: CriticalField | str, canonical: Any) -> Any:
    """Hashable key under which two canonical values count as the same."""
    field = as_critical_field(name)
    if field is CriticalField.INVOICE_NUMBER and isinstance(canonical, str):
        return invoice_number_key(canonical)
    if field is None and isinstance(canonical, str):
        return canonical.casefold()
    return canonical
