"""Small, strict readers shared by the lease, membership and cash modules: money, dates, months, tax numbers.

Portuguese and English conventions ("1.234,56 €", "£1,234.56", "17/09/2026", "17 de setembro de 2026",
"September 2026", "set/26"). Money always has two decimals, so a date or a tax number is never read as an
amount. Pure Python; nothing here guesses: a value that cannot be read is None.
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Sequence
from datetime import date
from decimal import Decimal, InvalidOperation

from backoffice.learning.keys import fold

__all__ = ["MONTH_WORDS", "amounts", "csv_rows", "first_date", "money", "month_named", "tax_ids"]

# Folded month names and their usual abbreviations (Portuguese, English, Spanish).
MONTH_WORDS: dict[str, int] = {}
for _n, _words in enumerate((
        ("janeiro", "january", "enero", "jan"), ("fevereiro", "february", "febrero", "fev", "feb"),
        ("marco", "march", "marzo", "mar"), ("abril", "april", "abr", "apr"), ("maio", "may", "mayo", "mai"),
        ("junho", "june", "junio", "jun"), ("julho", "july", "julio", "jul"),
        ("agosto", "august", "ago", "aug"), ("setembro", "september", "septiembre", "set", "sep", "sept"),
        ("outubro", "october", "octubre", "out", "oct"), ("novembro", "november", "noviembre", "nov"),
        ("dezembro", "december", "diciembre", "dez", "dec", "dic")), start=1):
    for _w in _words:
        MONTH_WORDS[_w] = _n
_FULL_MONTHS = frozenset(w for w in MONTH_WORDS if len(w) >= 4 or w == "may")

_CURRENCY = r"(?:€|EUR|£|GBP|\$|USD)"
_MONEY = re.compile(
    rf"(?<![\w/.,:-])(?P<neg>-|−)?\s?{_CURRENCY}?\s?"
    r"(?P<num>\d{1,3}(?:[.   ]\d{3})+,\d{2}|\d{1,3}(?:,\d{3})+\.\d{2}|\d+[.,]\d{2})"
    rf"(?![.,]?\d)(?!\s?%)\s?{_CURRENCY}?")
_NUMERIC_DATE = re.compile(r"(?<![\d/.-])(?:(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})|(\d{4})-(\d{2})-(\d{2}))(?![\d/.-])")
_WORD_DATE = re.compile(r"(?<!\d)(\d{1,2})(?:\.?º|st|nd|rd|th)?\s+(?:de\s+|of\s+)?([a-z]{3,10})\.?,?\s+(?:de\s+)?(\d{4})")
_MONTH_YEAR = re.compile(r"(?<![a-z0-9])([a-z]{3,10})\.?\s*(?:de\s+|/|-|\s)\s*(\d{4}|\d{2})(?![\d/])")
_NUMERIC_MONTH = re.compile(r"(?<![\d/.-])(?:(\d{1,2})[/.-](\d{4})|(\d{4})-(\d{2}))(?![\d/.-])")
_PT_NIF = re.compile(r"(?<![\dA-Z])(?:PT\s?)?([1-9]\d{8})(?!\d)")
_GB_VAT = re.compile(r"(?<![A-Z0-9])GB\s?(\d{3}\s?\d{4}\s?\d{2}(?:\s?\d{3})?)(?!\d)")
# A tax number with a letter in it (a Spanish NIF, NIE or CIF), with an optional country prefix: read through the
# packs of the countries a company can run in (tax_ids), never on its shape alone.
_LETTERED_TAX_ID = re.compile(r"(?<![A-Za-z0-9])(?:[A-Z]{2}[ \-]?)?[A-Z0-9][ \-.]?\d{7}[ \-.]?[A-Z0-9](?![A-Za-z0-9])")
_TAX_PREFIX = re.compile(r"^[A-Z]{2}[ \-]?(?=[A-Z0-9][ \-.]?\d{7})")


def money(raw: str | None) -> Decimal | None:
    """One amount written by a person or a program ('1.234,56 €', '£1,234.56', '-12,30'), else None."""
    if raw is None:
        return None
    found = amounts(str(raw))
    return found[0] if len(found) == 1 else None


def amounts(text: str) -> list[Decimal]:
    """Every amount (two decimals) on a line, in order."""
    out: list[Decimal] = []
    for m in _MONEY.finditer(text or ""):
        num = m.group("num")
        comma, dot = num.rfind(","), num.rfind(".")
        if comma > dot:
            num = re.sub(r"[.   ]", "", num[:comma]) + "." + num[comma + 1:]
        else:
            num = num.replace(",", "")
        try:
            value = Decimal(num)
        except InvalidOperation:
            continue
        out.append(-value if m.group("neg") else value)
    return out


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def first_date(text: str) -> date | None:
    """The first date written in ``text`` ('17/09/2026', '2026-09-17', '17 de setembro de 2026')."""
    found: list[tuple[int, date]] = []
    for m in _NUMERIC_DATE.finditer(text or ""):
        day = _safe_date(int(m.group(3)), int(m.group(2)), int(m.group(1))) if m.group(1) else \
            _safe_date(int(m.group(4)), int(m.group(5)), int(m.group(6)))
        if day is not None:
            found.append((m.start(), day))
            break
    folded = fold(text or "")
    for m in _WORD_DATE.finditer(folded):
        month = MONTH_WORDS.get(m.group(2))
        day = _safe_date(int(m.group(3)), month, int(m.group(1))) if month else None
        if day is not None:
            found.append((m.start(), day))
            break
    return min(found)[1] if found else None


def month_named(text: str, *, default_year: int | None = None) -> tuple[int, int] | None:
    """(year, month) a period is written as: 'setembro 2026', 'September', '09/2026', '2026-09', 'set/26'."""
    folded = fold(text or "")
    m = _NUMERIC_MONTH.search(folded)
    if m:
        year, month = (int(m.group(2)), int(m.group(1))) if m.group(1) else (int(m.group(3)), int(m.group(4)))
        if 1 <= month <= 12:
            return year, month
    for m in _MONTH_YEAR.finditer(folded):
        month = MONTH_WORDS.get(m.group(1))
        if month:
            year = int(m.group(2))
            return (2000 + year if year < 100 else year), month
    if default_year is not None:
        for word in re.findall(r"[a-z]+", folded):
            if word in _FULL_MONTHS:  # a whole month name only: 'out' or 'set' alone are ordinary words
                return default_year, MONTH_WORDS[word]
    return None


def tax_ids(text: str) -> list[str]:
    """Tax numbers written in ``text``: Portuguese NIFs (9 digits), the lettered tax numbers of the other countries
    a company can run in, each kept only when that country's pack checks it (a Spanish NIF, NIE or CIF with its
    check character: 'B12345674', '12345678Z'; §49), and UK VAT numbers ('GB123456789')."""
    out = [m.group(1) for m in _PT_NIF.finditer(text or "")]
    out += _pack_tax_ids(text or "")
    out += ["GB" + re.sub(r"\s", "", m.group(1)) for m in _GB_VAT.finditer(text or "")]
    return list(dict.fromkeys(out))


def _pack_tax_ids(text: str) -> list[str]:
    """Lettered tax numbers ('B-1234567-4', 'ES B12345674', 'X1234567L') a company country's pack validates."""
    from backoffice.countries import CountryPackError, company_countries, company_pack

    candidates = [m.group(0) for m in _LETTERED_TAX_ID.finditer(text)]
    candidates = [c for c in candidates if re.search(r"[A-Z]", _TAX_PREFIX.sub("", c))]
    if not candidates:
        return []
    out: list[str] = []
    packs = [company_pack(country) for country in company_countries()]
    for raw in candidates:
        for pack in packs:
            try:
                check = pack.validate_tax_id(raw)
            except (CountryPackError, ValueError):
                continue
            if check.valid and check.normalized:
                out.append(check.normalized)
                break
    return out


def csv_rows(text: str) -> tuple[list[str], list[list[str]]] | None:
    """A CSV export's header (folded) and rows, whatever its separator (',', ';' or tab); None when it is not one."""
    lines = [line for line in (text or "").splitlines() if line.strip()]
    if len(lines) < 2:
        return None
    first = lines[0]
    delimiter = max((",", ";", "\t"), key=lambda d: first.count(d))
    if first.count(delimiter) < 2:
        return None
    try:
        rows = list(csv.reader(io.StringIO("\n".join(lines)), delimiter=delimiter))
    except csv.Error:
        return None
    header = [" ".join(re.findall(r"[a-z0-9]+", fold(h))) for h in rows[0]]
    return header, [r for r in rows[1:] if any(c.strip() for c in r)]


def column(header: Sequence[str], aliases: Sequence[str]) -> int | None:
    """The first header cell equal to one of ``aliases`` (folded words), else one that starts with one."""
    for alias in aliases:
        if alias in header:
            return header.index(alias)
    for alias in aliases:
        for i, h in enumerate(header):
            if h.startswith(alias + " "):
                return i
    return None
