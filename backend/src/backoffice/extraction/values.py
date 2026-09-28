"""Parsing and normalization of critical field values (§18, §19).

Two observations of a field agree only when their normalized values are
equal. The parsers are strict on purpose: an ambiguous input such as
``"1.492"`` (1492 in Portugal, 1.492 elsewhere) or ``"05/09/2026"`` without a
known day order yields ``None`` instead of a guess (§19). Money is always
:class:`~decimal.Decimal`, never float.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any

from backoffice.domain.models import CriticalField

__all__ = [
    "AMOUNT_FIELDS",
    "COMMON_CURRENCIES",
    "DATE_FIELDS",
    "REFERENCE_FIELDS",
    "TAX_ID_FIELDS",
    "VAT_PREFIXES",
    "comparison_key",
    "decimal_key",
    "find_currency",
    "iban_is_valid",
    "is_usable",
    "normalize_currency",
    "normalize_iban",
    "normalize_reference",
    "normalize_tax_id",
    "parse_amount",
    "parse_date",
    "typed_value",
]

AMOUNT_FIELDS: frozenset[CriticalField] = frozenset(
    {CriticalField.GROSS_AMOUNT, CriticalField.NET_AMOUNT, CriticalField.VAT_AMOUNT}
)
DATE_FIELDS: frozenset[CriticalField] = frozenset(
    {CriticalField.ISSUE_DATE, CriticalField.DUE_DATE}
)
TAX_ID_FIELDS: frozenset[CriticalField] = frozenset(
    {CriticalField.SUPPLIER_TAX_ID, CriticalField.CUSTOMER_TAX_ID}
)
REFERENCE_FIELDS: frozenset[CriticalField] = frozenset(
    {CriticalField.INVOICE_NUMBER, CriticalField.PAYMENT_REFERENCE}
)

# --------------------------------------------------------------------------- amounts

# Only symbols that name exactly one currency. "$", "¥" and "kr" are shared by
# several currencies, so they never decide a currency on their own.
_CURRENCY_SYMBOLS: Mapping[str, str] = MappingProxyType({"€": "EUR", "£": "GBP", "₪": "ILS"})

# ISO 4217 codes recognised inside free text (a bare three-letter word such as
# "VAT" must never be read as a currency). Source: ISO 4217 active codes.
# verified_as_of: 2026-09 (author knowledge; extend from the official list).
COMMON_CURRENCIES: frozenset[str] = frozenset(
    {
        "EUR", "USD", "GBP", "CHF", "ILS", "SEK", "NOK", "DKK", "PLN", "CZK",
        "HUF", "RON", "BGN", "CAD", "AUD", "NZD", "JPY", "CNY", "BRL", "MXN",
    }
)  # fmt: skip

_CURRENCY_TOKEN = (
    r"(?:" + "|".join(sorted(COMMON_CURRENCIES)) + r"|[A-Z]{0,2}\$|[€£₪¥])"
)
# A currency code or symbol at either end of an amount ("EUR 483,60", "483.60 €").
# Only known codes: "VAT 90.43" is not an amount.
_EDGE_CURRENCY = re.compile(
    rf"^{_CURRENCY_TOKEN}\s*|\s*{_CURRENCY_TOKEN}$", re.IGNORECASE
)
# Thousands separators: spaces (incl. no-break, figure, thin, narrow no-break) and apostrophes.
_GROUPING_SPACES = re.compile("[\\s\u00a0\u2007\u2009\u202f']")
_NUMBER = re.compile(r"\d[\d.,]*")
_MINUS = "-\u2212"  # hyphen-minus and the Unicode minus sign


def parse_amount(value: object) -> Decimal | None:
    """A money amount as Decimal, or None when unreadable or ambiguous.

    Accepts "1.492,30", "1,492.30", "1 492,30", "€483.60", "483,60 EUR",
    "(12.50)" and "12.50-" (negative). A single separator followed by exactly
    three digits ("1.492") is ambiguous and yields None. Floats are converted
    through their shortest repr so binary noise never becomes money.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        converted = Decimal(repr(value))
        return converted if converted.is_finite() else None
    if isinstance(value, str):
        return _parse_amount_text(value)
    return None


def _strip_currency(text: str) -> str:
    return _EDGE_CURRENCY.sub("", text).strip()


def _parse_amount_text(text: str) -> Decimal | None:
    s = text.strip()
    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative, s = True, s[1:-1].strip()
    s = _strip_currency(s)
    if s[:1] and s[0] in _MINUS:
        negative, s = not negative, s[1:].lstrip()
    elif s.startswith("+"):
        s = s[1:].lstrip()
    if s[-1:] and s[-1] in _MINUS:
        negative, s = not negative, s[:-1].rstrip()
    s = _strip_currency(s)
    if not _space_grouping_ok(s):
        return None  # "483 60" is a lost decimal comma, not 48360
    s = _GROUPING_SPACES.sub("", s)
    if not _NUMBER.fullmatch(s) or s[-1] in ".,":
        return None
    number = _unsigned(s)
    if number is None:
        return None
    return -number if negative and number != 0 else number


def _space_grouping_ok(s: str) -> bool:
    """Spaces or apostrophes inside a number must be thousands grouping."""
    parts = re.split(_GROUPING_SPACES.pattern + "+", s)
    if len(parts) == 1:
        return True
    return (
        re.fullmatch(r"\d{1,3}", parts[0]) is not None
        and all(re.fullmatch(r"\d{3}", p) for p in parts[1:-1])
        and re.fullmatch(r"\d{3}(?:[.,]\d+)?", parts[-1]) is not None
    )


def _unsigned(s: str) -> Decimal | None:
    has_dot, has_comma = "." in s, "," in s
    try:
        if has_dot and has_comma:
            decimal_sep = "." if s.rfind(".") > s.rfind(",") else ","
            thousands = "," if decimal_sep == "." else "."
            whole, frac = s.rsplit(decimal_sep, 1)
            if decimal_sep in whole or thousands in frac or not _grouped(whole, thousands):
                return None
            return Decimal(whole.replace(thousands, "") + "." + frac)
        sep = "." if has_dot else "," if has_comma else None
        if sep is None:
            return Decimal(s)
        if s.count(sep) > 1:
            return Decimal(s.replace(sep, "")) if _grouped(s, sep) else None
        whole, frac = s.split(sep)
        if len(frac) == 3 and whole != "0":
            return None  # "1.492": thousands or decimals? Never guess (§19).
        return Decimal(whole + "." + frac)
    except InvalidOperation:
        return None


def _grouped(whole: str, sep: str) -> bool:
    """Thousands grouping: 1-3 leading digits, then groups of exactly three."""
    groups = whole.split(sep)
    if len(groups) == 1:
        return True
    return (
        1 <= len(groups[0]) <= 3
        and groups[0] != "0"
        and all(len(g) == 3 for g in groups[1:])
    )


def decimal_key(value: Decimal) -> str:
    """Canonical text for a Decimal: 483.6 and 483.60 share one key."""
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


# --------------------------------------------------------------------------- dates


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


# English, Portuguese and Spanish month names and common abbreviations.
_MONTHS: Mapping[str, int] = MappingProxyType(
    {
        _fold(name): number
        for number, names in enumerate(
            (
                ("jan", "january", "janeiro", "enero", "ene"),
                ("feb", "february", "fev", "fevereiro", "febrero"),
                ("mar", "march", "marco", "março", "marzo"),
                ("apr", "april", "abr", "abril"),
                ("may", "mai", "maio", "mayo"),
                ("jun", "june", "junho", "junio"),
                ("jul", "july", "julho", "julio"),
                ("aug", "august", "ago", "agosto"),
                ("sep", "sept", "september", "set", "setembro", "septiembre", "setiembre"),
                ("oct", "october", "out", "outubro", "octubre"),
                ("nov", "november", "novembro", "noviembre"),
                ("dec", "december", "dez", "dezembro", "dic", "diciembre"),
            ),
            start=1,
        )
        for name in names
    }
)

_ISO_DATE = re.compile(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?:[T ].*|Z|[+-]\d{2}:?\d{2})?")
_COMPACT_DATE = re.compile(r"(\d{4})(\d{2})(\d{2})")
_NUMERIC_DATE = re.compile(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})")
_DAY_MONTH_YEAR = re.compile(
    r"(\d{1,2})(?:st|nd|rd|th|º)?\.?\s*(?:de\s+)?([^\W\d_]+)\.?,?\s*(?:de\s+)?(\d{4})",
    re.IGNORECASE,
)
_MONTH_DAY_YEAR = re.compile(
    r"([^\W\d_]+)\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})", re.IGNORECASE
)


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_date(value: object, *, day_first: bool | None = None) -> date | None:
    """A calendar date, or None when unreadable or ambiguous.

    ISO ("2026-09-18", with optional time or zone), compact ("20260918"),
    numeric day/month/year and English/Portuguese/Spanish month names are
    accepted. For "05/09/2026" the day order must be known (``day_first``);
    when it is None only unambiguous numeric dates are accepted.
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    s = " ".join(value.split())
    if m := _ISO_DATE.fullmatch(s):
        return _safe_date(int(m[1]), int(m[2]), int(m[3]))
    if m := _COMPACT_DATE.fullmatch(s):
        return _safe_date(int(m[1]), int(m[2]), int(m[3]))
    if m := _NUMERIC_DATE.fullmatch(s):
        return _numeric_date(int(m[1]), int(m[2]), int(m[3]), day_first)
    if m := _DAY_MONTH_YEAR.fullmatch(s):
        month = _MONTHS.get(_fold(m[2]))
        return _safe_date(int(m[3]), month, int(m[1])) if month else None
    if m := _MONTH_DAY_YEAR.fullmatch(s):
        month = _MONTHS.get(_fold(m[1]))
        return _safe_date(int(m[3]), month, int(m[2])) if month else None
    return None


def _numeric_date(a: int, b: int, year: int, day_first: bool | None) -> date | None:
    if day_first is True:
        return _safe_date(year, b, a)
    if day_first is False:
        return _safe_date(year, a, b)
    if a == b or (a > 12 >= b):
        return _safe_date(year, b, a)
    if b > 12 >= a:
        return _safe_date(year, a, b)
    return None  # "05/09/2026": 5 September or 9 May? Never guess.


# --------------------------------------------------------------------------- identifiers

# VAT number country prefixes stripped before comparing tax ids, so that
# "PT509123457" and "509 123 457" agree. EU VIES member-state codes (Greece
# uses "EL"; "GR" is accepted as printed), "XI" (Northern Ireland) and "GB".
# Swiss "CHE-" UIDs are deliberately absent: their prefix is part of the id.
# Source: European Commission VIES. verified_as_of: 2026-09 (author knowledge).
VAT_PREFIXES: frozenset[str] = frozenset(
    {
        "AT", "BE", "BG", "CY", "CZ", "DE", "DK", "EE", "EL", "ES", "FI", "FR",
        "GR", "HR", "HU", "IE", "IT", "LT", "LU", "LV", "MT", "NL", "PL", "PT",
        "RO", "SE", "SI", "SK", "XI", "GB",
    }
)  # fmt: skip

_NON_ALNUM = re.compile(r"[^0-9A-Za-z]")


def normalize_tax_id(value: object) -> str | None:
    """Tax id without separators, upper case, VAT country prefix removed."""
    if value is None or isinstance(value, bool):
        return None
    compact = _NON_ALNUM.sub("", str(value)).upper()
    if len(compact) - 2 >= 8 and compact[:2] in VAT_PREFIXES:
        compact = compact[2:]
    return compact or None


def normalize_iban(value: object) -> str | None:
    """IBAN without spaces or hyphens, upper case; None when not alphanumeric."""
    if value is None or isinstance(value, bool):
        return None
    compact = re.sub(r"[\s-]", "", str(value)).upper()
    return compact if compact and compact.isascii() and compact.isalnum() else None


def iban_is_valid(value: object) -> bool:
    """ISO 13616 shape and mod-97 check."""
    iban = normalize_iban(value)
    if not iban or not 15 <= len(iban) <= 34:
        return False
    if not (iban[:2].isalpha() and iban[2:4].isdigit()):
        return False
    rearranged = iban[4:] + iban[:4]
    return int("".join(str(int(c, 36)) for c in rearranged)) % 97 == 1


def normalize_currency(value: object) -> str | None:
    """ISO 4217 code from a code or an unambiguous symbol; None otherwise."""
    if not isinstance(value, str):
        return None
    s = value.strip()
    if s in _CURRENCY_SYMBOLS:
        return _CURRENCY_SYMBOLS[s]
    if re.fullmatch(r"[A-Za-z]{3}", s):
        return s.upper()
    return None


_CURRENCY_CODE_IN_TEXT = re.compile(r"(?<![A-Za-z])([A-Z]{3})(?![A-Za-z])")


def find_currency(text: str) -> str | None:
    """The single currency named in ``text`` (symbol or known ISO code), if exactly one."""
    found = {code for sym, code in _CURRENCY_SYMBOLS.items() if sym in text}
    found |= {m for m in _CURRENCY_CODE_IN_TEXT.findall(text) if m in COMMON_CURRENCIES}
    return found.pop() if len(found) == 1 else None


def normalize_reference(value: object) -> str | None:
    """Document numbers and payment references: no whitespace, upper case."""
    if value is None or isinstance(value, bool):
        return None
    compact = re.sub(r"\s+", "", str(value)).upper()
    return compact or None


# --------------------------------------------------------------------------- dispatch


def typed_value(field: CriticalField, value: object, *, day_first: bool | None = None) -> Any:
    """``value`` parsed into its canonical type for ``field``, or None."""
    if field in AMOUNT_FIELDS:
        return parse_amount(value)
    if field in DATE_FIELDS:
        return parse_date(value, day_first=day_first)
    if field in TAX_ID_FIELDS:
        return normalize_tax_id(value)
    if field is CriticalField.IBAN:
        return normalize_iban(value)
    if field is CriticalField.CURRENCY:
        return normalize_currency(value)
    return normalize_reference(value)


def is_usable(field: CriticalField, value: object) -> bool:
    """Whether ``value`` is a real value for ``field`` (§18, §19).

    It must parse for the field's type; an IBAN must also pass mod-97, since
    a failed checksum is always a misread or a typo, never a bank account.
    """
    typed = typed_value(field, value)
    if typed is None:
        return False
    return iban_is_valid(typed) if field is CriticalField.IBAN else True


def comparison_key(field: CriticalField, value: object) -> str:
    """Key under which equal readings of ``field`` collide.

    Unparseable values keep a ``raw:`` key of their own, so garbage never
    silently matches a real value; it shows up as a disagreement instead.
    """
    typed = typed_value(field, value)
    if typed is None:
        return "raw:" + " ".join(str(value).split()).casefold()
    if isinstance(typed, Decimal):
        return decimal_key(typed)
    if isinstance(typed, date):
        return typed.isoformat()
    return str(typed)
