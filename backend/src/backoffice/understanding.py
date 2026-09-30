"""Understanding the owner's message (§39): one intent plus its details, scored, never guessed.

The built-in chat has no language model, and on the live demo it *is* the
product. It reads a message as an **intent** ("how much did we spend", "is the
month closed", "find the invoice") plus **slots**: the period, companies,
suppliers, cost category, amount, email addresses and whether money went out
or came in.

Every intent is scored from the words that signal it (weights below), with
small boosts from the slots found. The best intent wins only when it is
clearly ahead: a weak best score, or two different readings that tie, become
one short clarifying question ("Do you want what you spent in August, or
whether August is closed?") instead of an answer to another question. Some
pairs are not really different readings (a question naming an amount and a
payment is a payment lookup, not spending); :data:`_PREFER` settles those.

Periods understand English and Portuguese month names, "last month", "this
year", quarters, "between X and Y", "since X", "yesterday", "last week",
"the last 30 days" and dates such as "21 September" or "21/09/2026". A month
without a year is the most recent one (in October 2026, "December" is December
2025). Small typos ("expences", "septmber", "vodaphone") are corrected against
the words that matter, never against ordinary words.

Pure Python standard library plus the engine's own text helpers: it runs in the
browser build (Pyodide) as well as on the server.
"""

from __future__ import annotations

import calendar
import difflib
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from backoffice.learning import counterparty_key, day_month, fold

__all__ = ["INTENTS", "Period", "Slots", "Understanding", "Vocabulary", "find_periods", "parse_amount",
           "month_period", "period_between", "understand"]

MONTH_NAMES = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
               "November", "December")
_EN = tuple(m.lower() for m in MONTH_NAMES)
_PT = ("janeiro", "fevereiro", "marco", "abril", "maio", "junho", "julho", "agosto", "setembro", "outubro",
       "novembro", "dezembro")
_MONTH_WORDS: dict[str, int] = {**{m: i for i, m in enumerate(_EN, 1)}, **{m: i for i, m in enumerate(_PT, 1)},
                                "jan": 1, "feb": 2, "fev": 2, "mar": 3, "apr": 4, "abr": 4, "may": 5, "mai": 5,
                                "jun": 6, "jul": 7, "aug": 8, "ago": 8, "sep": 9, "sept": 9, "set": 9, "oct": 10,
                                "out": 10, "nov": 11, "dec": 12, "dez": 12}
# Also ordinary words ("may I", "money out", "set up", "two months ago", "mar"): a month only in context.
_AMBIGUOUS = frozenset({"may", "mar", "ago", "set", "out"})
_MONTH_CONTEXT = frozenset({"in", "for", "of", "during", "since", "until", "till", "from", "to", "and", "between",
                            "last", "this", "next", "early", "late", "mid", "de", "em", "no", "na", "desde", "ate",
                            "by", "vs", "versus", "through", "before", "after", "than", "or", "e"})
_MONTH_NOUNS = frozenset({"expenses", "spending", "costs", "invoices", "report", "income", "vat", "payments",
                          "bills", "statement", "status", "close"})
_MON = "|".join(sorted(_MONTH_WORDS, key=len, reverse=True))
_NUMBER_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
                 "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "couple of": 2, "few": 3,
                 "um": 1, "uma": 1, "dois": 2, "duas": 2, "tres": 3, "seis": 6, "doze": 12}
_ORDINALS = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3, "fourth": 4, "4th": 4}

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_DOC_NUMBER = re.compile(r"\b[A-Z]{1,4}[\s-]?[A-Z]{0,4}\d{2,4}[/-]\d+\b|\b[A-Z]{2,}-\d{3,}(?:-\d+)*\b")


# --------------------------------------------------------------------------- periods


@dataclass(frozen=True)
class Period:
    """A span of days the owner named, with how to say it back."""

    start: date
    end: date
    grain: str  # day | week | month | quarter | year | range | rolling
    label: str  # "August", "2026", "1 to 15 September", "the last 30 days"
    phrase: str  # "in August", "in 2026 so far", "on 1 October", "since 1 August"

    def previous(self, today: date) -> Period:
        """The period just before, of the same kind (for comparisons)."""
        if self.grain == "month":
            prev = self.start - timedelta(days=1)
            return _month_period(prev.year, prev.month, today)
        if self.grain == "quarter":
            prev = self.start - timedelta(days=1)
            return _quarter_period(prev.year, (prev.month - 1) // 3 + 1, today)
        if self.grain == "year":
            return _year_period(self.start.year - 1, today)
        if self.grain == "day":
            return _day_period(self.start - timedelta(days=1), today)
        if self.grain == "week":
            return _week_period(self.start - timedelta(days=7), today)
        length = (self.end - self.start).days + 1
        return _range_period(self.start - timedelta(days=length), self.start - timedelta(days=1), today)


def _month_label(year: int, month: int, today: date) -> str:
    return MONTH_NAMES[month - 1] if year == today.year else f"{MONTH_NAMES[month - 1]} {year}"


def _month_period(year: int, month: int, today: date) -> Period:
    start = date(year, month, 1)
    end = date(year, month, calendar.monthrange(year, month)[1])
    label = _month_label(year, month, today)
    return Period(start, end, "month", label, f"in {label}" + (" so far" if start <= today <= end else ""))


def _quarter_period(year: int, q: int, today: date) -> Period:
    start = date(year, 3 * (q - 1) + 1, 1)
    last = 3 * q
    end = date(year, last, calendar.monthrange(year, last)[1])
    label = f"{MONTH_NAMES[start.month - 1]} to {MONTH_NAMES[last - 1]}" + ("" if year == today.year else f" {year}")
    return Period(start, end, "quarter", label, f"from {label}" + (" so far" if start <= today <= end else ""))


def _year_period(year: int, today: date) -> Period:
    return Period(date(year, 1, 1), date(year, 12, 31), "year", str(year),
                  f"in {year}" + (" so far" if year == today.year else ""))


def _day_period(day: date, today: date) -> Period:
    label = day_month(day, today)
    phrase = "today" if day == today else ("yesterday" if day == today - timedelta(days=1) else f"on {label}")
    return Period(day, day, "day", label, phrase)


def _week_period(day: date, today: date) -> Period:
    start = day - timedelta(days=day.weekday())
    end = start + timedelta(days=6)
    label = f"the week of {day_month(start, today)}"
    if start <= today <= end:
        phrase = "this week"
    elif start <= today - timedelta(days=7) <= end:
        phrase = "last week"
    else:
        phrase = f"in {label}"
    return Period(start, end, "week", label, phrase)


def _range_label(a: date, b: date, today: date) -> str:
    if a == b:
        return day_month(a, today)
    if (a.year, a.month) == (b.year, b.month):
        return f"{a.day} to {day_month(b, today)}"
    return f"{day_month(a, today)} to {day_month(b, today)}"


def _range_period(a: date, b: date, today: date) -> Period:
    if b < a:
        a, b = b, a
    # A range that is exactly a month or a year reads as one.
    if a.day == 1 and b == date(b.year, b.month, calendar.monthrange(b.year, b.month)[1]):
        if (a.year, a.month) == (b.year, b.month):
            return _month_period(a.year, a.month, today)
        if a.month == 1 and b.month == 12 and a.year == b.year:
            return _year_period(a.year, today)
        if a.year == b.year:
            label = f"{MONTH_NAMES[a.month - 1]} to {MONTH_NAMES[b.month - 1]}" + \
                ("" if a.year == today.year else f" {a.year}")
            return Period(a, b, "range", label, f"from {label}")
    if a == b:
        return _day_period(a, today)
    label = _range_label(a, b, today)
    return Period(a, b, "range", label, f"from {label}")


def month_period(year: int, month: int, today: date) -> Period:
    """One calendar month ("September", "December 2025"; "October so far" while it runs)."""
    return _month_period(year, month, today)


def period_between(a: date, b: date, today: date) -> Period:
    """The period from ``a`` to ``b`` inclusive, named the way an owner would ("September", "1 to 15 September")."""
    return _range_period(a, b, today)


def _add_months(day: date, months: int) -> date:
    total = day.year * 12 + day.month - 1 + months
    year, month = divmod(total, 12)
    return date(year, month + 1, min(day.day, calendar.monthrange(year, month + 1)[1]))


def _rolling(n: int, unit: str, today: date) -> Period:
    unit = {"dias": "day", "semanas": "week", "meses": "month", "anos": "year"}.get(unit, unit.rstrip("s"))
    if unit == "day":
        start = today - timedelta(days=n - 1)
    elif unit == "week":
        start = today - timedelta(days=7 * n - 1)
    elif unit == "month":
        start = _add_months(today, -n) + timedelta(days=1)
    else:
        start = _add_months(today, -12 * n) + timedelta(days=1)
    label = f"the last {n} {unit}s" if n != 1 else f"the last {unit}"
    return Period(start, today, "rolling", label, f"in {label}")


def _recent_year(month: int, day: int | None, today: date) -> int:
    """The year of the most recent such date: in October 2026, 'December' is December 2025."""
    if day is None:
        return today.year if month <= today.month else today.year - 1
    try:
        return today.year if date(today.year, month, day) <= today else today.year - 1
    except ValueError:
        return today.year


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _month_ok(t: str, start: int, end: int, word: str) -> bool:
    """An ambiguous month word counts only next to a preposition, a number or a money noun."""
    if word not in _AMBIGUOUS:
        return True
    before = t[:start].split()
    after = t[end:].split()
    prev = before[-1] if before else ""
    nxt = after[0].strip(".,") if after else ""
    return prev in _MONTH_CONTEXT or prev.isdigit() or nxt.isdigit() or nxt in _MONTH_NOUNS


def find_periods(t: str, today: date) -> tuple[list[Period], list[tuple[int, int]]]:
    """Every period named in folded text ``t``, in reading order, and the character spans used."""
    atoms: list[tuple[int, int, Period]] = []
    taken: list[tuple[int, int]] = []

    def free(a: int, b: int) -> bool:
        return all(b <= s or a >= e for s, e in taken)

    def add(a: int, b: int, p: Period | None) -> None:
        if p is not None and free(a, b):
            atoms.append((a, b, p))
            taken.append((a, b))

    # Days within one month: "1 to 15 September", "from 1-15 set".
    for m in re.finditer(rf"\b(?:from\s+|between\s+|de\s+|entre\s+)?(\d{{1,2}})(?:st|nd|rd|th)?\s*(?:-|to|and|until|"
                         rf"till|a|e|ate)\s*(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+|de\s+)?({_MON})\b"
                         rf"(?:,?\s+(?:de\s+)?(\d{{4}}))?", t):
        mon = _MONTH_WORDS[m[3]]
        year = int(m[4]) if m[4] else _recent_year(mon, int(m[1]), today)
        a, b = _safe_date(year, mon, int(m[1])), _safe_date(year, mon, int(m[2]))
        if a and b:
            add(m.start(), m.end(), _range_period(a, b, today))
    # ISO and numeric dates (day first, as in Portugal).
    for m in re.finditer(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", t):
        d = _safe_date(int(m[1]), int(m[2]), int(m[3]))
        add(m.start(), m.end(), _day_period(d, today) if d else None)
    for m in re.finditer(r"(?<![\d€.,])(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?(?![\d/])|(?<![\d€.,])(\d{1,2})[.-](\d{1,2})"
                         r"[.-](\d{4})\b", t):
        day, mon, yr = (m[1], m[2], m[3]) if m[1] else (m[4], m[5], m[6])
        year = (int(yr) + 2000 if len(yr) == 2 else int(yr)) if yr else _recent_year(int(mon), int(day), today)
        d = _safe_date(year, int(mon), int(day)) if 1 <= int(mon) <= 12 else None
        add(m.start(), m.end(), _day_period(d, today) if d else None)
    # "21 September 2026", "21st of september", "1 de setembro de 2026".
    for m in re.finditer(rf"\b(\d{{1,2}})(?:st|nd|rd|th|o)?\s+(?:of\s+|de\s+)?({_MON})\b(?:,?\s+(?:de\s+)?(\d{{4}}))?",
                         t):
        mon = _MONTH_WORDS[m[2]]
        year = int(m[3]) if m[3] else _recent_year(mon, int(m[1]), today)
        d = _safe_date(year, mon, int(m[1]))
        add(m.start(), m.end(), _day_period(d, today) if d else None)
    # "September 21", "sept 21st, 2026".
    for m in re.finditer(rf"\b({_MON})\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?!\s*(?:€|eur|euros?|days?|weeks?|months?|"
                         rf"years?|payments?))(?:,?\s+(\d{{4}}))?", t):
        mon = _MONTH_WORDS[m[1]]
        year = int(m[3]) if m[3] else _recent_year(mon, int(m[2]), today)
        d = _safe_date(year, mon, int(m[2]))
        add(m.start(), m.end(), _day_period(d, today) if d else None)
    # Relative words.
    this_month = _month_period(today.year, today.month, today)
    last_month_end = today.replace(day=1) - timedelta(days=1)
    q_now = (today.month - 1) // 3 + 1
    q_prev = (today.year, q_now - 1) if q_now > 1 else (today.year - 1, 4)
    relative: list[tuple[str, Any]] = [
        (r"\b(?:the )?day before yesterday\b|\banteontem\b", lambda m: _day_period(today - timedelta(days=2), today)),
        (r"\byesterday\b|\bontem\b", lambda m: _day_period(today - timedelta(days=1), today)),
        (r"\btoday\b|\bhoje\b", lambda m: _day_period(today, today)),
        (r"\b(?:this|current) week\b|\besta semana\b", lambda m: _week_period(today, today)),
        (r"\b(?:last|previous|prior) week\b|\bsemana passada\b",
         lambda m: _week_period(today - timedelta(days=7), today)),
        (r"\b(?:this|current) month\b|\beste mes\b|\bmonth to date\b|\bmtd\b", lambda m: this_month),
        (r"\b(?:last|previous|prior) month\b|\bmes passado\b|\bultimo mes\b|\bmes anterior\b",
         lambda m: _month_period(last_month_end.year, last_month_end.month, today)),
        (r"\b(?:this|current) quarter\b|\beste trimestre\b", lambda m: _quarter_period(today.year, q_now, today)),
        (r"\b(?:last|previous|prior) quarter\b|\btrimestre (?:passado|anterior)\b|\bultimo trimestre\b",
         lambda m: _quarter_period(q_prev[0], q_prev[1], today)),
        (r"\b(?:this|current) year\b|\beste ano\b|\byear to date\b|\bytd\b|\bso far this year\b",
         lambda m: _year_period(today.year, today)),
        (r"\b(?:last|previous|prior) year\b|\bano passado\b|\bano anterior\b",
         lambda m: _year_period(today.year - 1, today)),
        (r"\b(?:(?:over |in )?the )?(?:last|past|previous|ultim[oa]s|nos ultim[oa]s)\s+(\d{1,3}|a|one|two|three|four|"
         r"five|six|seven|eight|nine|ten|eleven|twelve|couple of|few|um|uma|dois|duas|tres|seis|doze)\s+"
         r"(days?|weeks?|months?|years?|dias|semanas|meses|anos)\b",
         lambda m: _rolling(int(m[1]) if m[1].isdigit() else _NUMBER_WORDS[m[1]], m[2], today)),
        (r"\b(?:the )?past (week|month|year)\b", lambda m: _rolling(1, m[1], today)),
    ]
    for pattern, build in relative:
        for m in re.finditer(pattern, t):
            try:
                add(m.start(), m.end(), build(m))
            except (ValueError, KeyError):
                continue
    # Quarters.
    for m in re.finditer(r"\bq([1-4])\b(?:\s+(?:of\s+)?(\d{4}))?|\b(first|second|third|fourth|1st|2nd|3rd|4th)\s+"
                         r"quarter\b(?:\s+(?:of\s+)?(\d{4}))?|\b([1-4])(?:o)?\s+trimestre\b(?:\s+(?:de\s+)?(\d{4}))?", t):
        q = int(m[1] or m[5] or 0) or _ORDINALS[m[3]]
        yr = m[2] or m[4] or m[6]
        year = int(yr) if yr else (today.year if q <= q_now else today.year - 1)
        add(m.start(), m.end(), _quarter_period(year, q, today))
    # Months, with an optional year.
    for m in re.finditer(rf"\b({_MON})\b\.?(?:\s+(?:of\s+|de\s+)?((?:19|20)\d{{2}})\b)?", t):
        if not _month_ok(t, m.start(1), m.end(1), m[1]):
            continue
        mon = _MONTH_WORDS[m[1]]
        year = int(m[2]) if m[2] else _recent_year(mon, None, today)
        add(m.start(), m.end(), _month_period(year, mon, today))
    # A year on its own: "in 2025", "2026 expenses".
    for m in re.finditer(r"(?<![\d€.,/-])((?:19|20)\d{2})(?![\d.,/-]|\s?(?:€|eur))", t):
        add(m.start(), m.end(), _year_period(int(m[1]), today))

    atoms.sort(key=lambda a: a[0])
    compare = bool(re.search(r"\b(?:vs|versus|compar\w*|than|against)\b", t))
    periods: list[Period] = []
    spans: list[tuple[int, int]] = []
    i = 0
    while i < len(atoms):
        a0, a1, p = atoms[i]
        before = t[max(0, a0 - 12):a0]
        if i + 1 < len(atoms):
            b0, b1, q = atoms[i + 1]
            joint = t[a1:b0]
            ranged = re.fullmatch(r"\s*(?:-|to|until|till|through|thru|ate)\s*", joint)
            paired = re.fullmatch(r"\s*(?:and|a|e)\s*", joint) and (
                re.search(r"\b(?:between|from|entre|de)\s*$", before) or not compare)
            if ranged or paired:
                periods.append(_range_period(p.start, q.end, today))
                spans.append((a0, b1))
                i += 2
                continue
        if re.search(r"\b(?:since|desde)\s*$", before) and p.start <= today:
            label = day_month(p.start, today) if p.grain == "day" else p.label
            periods.append(Period(p.start, today, "range", f"since {label}", f"since {label}"))
        else:
            periods.append(p)
        spans.append((a0, a1))
        i += 1
    return periods, spans


# --------------------------------------------------------------------------- amounts


def parse_amount(raw: str) -> Decimal | None:
    """'1.200,00' / '1,200.00' / '92.40' / '418' -> Decimal; None if it is not an amount."""
    s = raw.strip().rstrip(".,")
    if not s or not re.fullmatch(r"\d[\d.,]*", s):
        return None
    try:
        if "," in s and "." in s:
            dec = "," if s.rfind(",") > s.rfind(".") else "."
            thou = "." if dec == "," else ","
            return Decimal(s.replace(thou, "").replace(dec, "."))
        if "," in s:
            if re.fullmatch(r"\d{1,3}(?:,\d{3})+", s):
                return Decimal(s.replace(",", ""))
            return Decimal(s.replace(",", "."))
        if "." in s and re.fullmatch(r"\d{1,3}(?:\.\d{3})+", s):
            return Decimal(s.replace(".", ""))
        return Decimal(s)
    except InvalidOperation:
        return None


_PAYMENT_WORDS = re.compile(r"\b(?:payments?|paid|pay|invoices?|transfers?|charges?|charged|receipts?|bills?|debits?|"
                            r"transactions?|pagamentos?|faturas?)\b")


def _amount(t: str, spans: list[tuple[int, int]]) -> Decimal | None:
    def free(a: int, b: int) -> bool:
        return all(b <= s or a >= e for s, e in spans)

    for m in re.finditer(r"€\s?(\d[\d.,]*)|(\d[\d.,]*)\s?(?:€|eur\b|euros?\b)", t):
        value = parse_amount(m[1] or m[2])
        if value is not None and free(m.start(), m.end()):
            return value
    for m in re.finditer(r"(?<![\w.,/€-])(\d{1,3}(?:[.,]\d{3})*[.,]\d{2}|\d+[.,]\d{2})(?![\w/-]|[.,]\d)", t):
        value = parse_amount(m[1])
        if value is not None and free(m.start(), m.end()):
            return value
    if _PAYMENT_WORDS.search(t):
        for m in re.finditer(r"(?<![\w.,/€-])(\d{2,6})(?![\w.,/%-])", t):
            before = t[:m.start()].split()[-1:]
            after = t[m.end():].split()[:1]
            if not free(m.start(), m.end()) or 1900 <= int(m[1]) <= 2100:
                continue
            if before and before[0] in ("last", "first", "top", "recent", "latest", "past", "next"):
                continue
            if after and after[0] in ("days", "day", "weeks", "months", "years", "suppliers", "companies", "items",
                                      "things", "times", "invoices", "documents", "percent"):
                continue
            return Decimal(m[1])
    return None


# --------------------------------------------------------------------------- vocabulary


@dataclass(frozen=True)
class Vocabulary:
    """The names this business uses: its companies, suppliers, cost categories and accountant."""

    companies: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    company_names: Mapping[str, str] = field(default_factory=dict)
    suppliers: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    supplier_names: Mapping[str, str] = field(default_factory=dict)
    categories: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    category_labels: Mapping[str, str] = field(default_factory=dict)
    accountant: tuple[str, ...] = ()

    @classmethod
    def from_repo(cls, repo: Any, categories: Iterable[Any] = ()) -> Vocabulary:
        """Build from the engine's repository and the ledger's categories (``backoffice.spending``)."""
        companies: dict[str, tuple[str, ...]] = {}
        names: dict[str, str] = {}
        for cid, entity in repo.companies.items():
            base = _clean(entity.name)
            legal = _clean(repo.legal_names.get(cid, ""))
            variants = {base, base.replace(" ", ""), _clean(cid.replace("-", " ")), legal,
                        counterparty_key(repo.legal_names.get(cid, "")) or ""}
            companies[cid] = _variants(variants)
            names[cid] = entity.name
        company_words = {w for vs in companies.values() for v in vs for w in v.split()}
        suppliers: dict[str, tuple[str, ...]] = {}
        supplier_names: dict[str, str] = {}
        for s in repo.suppliers.values():
            variants = {_clean(s.name), counterparty_key(s.name) or ""}
            for alias in s.aliases:
                variants |= {_clean(alias), counterparty_key(alias) or ""}
            first = _clean(s.name).split(" ")[0] if s.name.strip() else ""
            if len(first) >= 4 and first not in _STOPWORDS and first not in company_words:
                variants.add(first)
            taken = {v for vs in companies.values() for v in vs}
            suppliers[s.id] = _variants(variants - taken)
            supplier_names[s.id] = s.name
        cats: dict[str, tuple[str, ...]] = {}
        labels: dict[str, str] = {}
        for c in categories:
            cats[c.id] = tuple(sorted({fold(w) for w in c.asked}, key=len, reverse=True))
            labels[c.id] = c.label
        rules = getattr(repo.rulebook, "rules", ())
        for rule in (rules() if callable(rules) else rules):
            label = getattr(rule.outcome, "category", None)
            if label and not any(fold(label) == fold(lbl) or fold(label) in cats.get(k, ())
                                 for k, lbl in labels.items()):
                cats[f"custom:{label}"] = (fold(label),)
                labels[f"custom:{label}"] = label
        accountant: set[str] = set()
        if repo.accountant is not None:
            person = _clean(repo.accountant.person or "")
            accountant |= {person, person.split(" ")[0] if person else "", _clean(repo.accountant.firm or "")}
        return cls(companies, names, suppliers, supplier_names, cats, labels,
                   tuple(sorted(w for w in accountant if len(w) >= 3)))

    def words(self) -> set[str]:
        """Every single word in these names (typos are corrected towards them)."""
        out: set[str] = set()
        for table in (self.companies, self.suppliers, self.categories):
            for variants in table.values():
                for v in variants:
                    out |= {w for w in v.split() if len(w) >= 4}
        out |= {w for a in self.accountant for w in a.split() if len(w) >= 4}
        return out


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", fold(text or ""))).strip()


def _variants(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted({v for v in values if v and len(v) >= 3}, key=len, reverse=True))


def _find(t: str, table: Mapping[str, tuple[str, ...]], blocked: list[tuple[int, int]]) -> list[tuple[int, int, str]]:
    """Non-overlapping name hits in ``t`` (longest names first), in reading order."""
    hits: list[tuple[int, int, str]] = []
    candidates = sorted(((v, key) for key, vs in table.items() for v in vs), key=lambda x: -len(x[0]))
    used = list(blocked)
    for variant, key in candidates:
        for m in re.finditer(rf"(?<![a-z0-9]){re.escape(variant)}(?![a-z0-9])", t):
            if all(m.end() <= s or m.start() >= e for s, e in used):
                hits.append((m.start(), m.end(), key))
                used.append((m.start(), m.end()))
    return sorted(hits)


# --------------------------------------------------------------------------- normalising


_STOPWORDS = frozenset({"the", "and", "for", "with", "from", "this", "that", "what", "which", "have", "your", "our",
                        "company", "companies", "business", "supplier", "portugal", "lisboa", "porto", "group",
                        "services", "service", "global", "new", "best", "good", "real", "home", "shop", "store"})

_REPLACEMENTS = (
    (r"[’‘`´]", "'"), (r"[“”]", '"'),
    (r"\bcan'?t\b", "cannot"), (r"\bwon'?t\b", "will not"),
    (r"\b(did|does|do|is|are|was|were|has|have|had|should|could|would)n'?t\b", r"\1 not"),
    (r"\bi'm\b|\bim\b", "i am"), (r"\b(i|we|you|they)'ve\b", r"\1 have"), (r"\b(we|you|they)'re\b", r"\1 are"),
    (r"\b(what|where|how|that|there|who|it|when)'s\b", r"\1 is"), (r"\b(what|where|how|who)s\b", r"\1 is"),
    (r"(\w)'s\b", r"\1"), (r"(\w)s' ", r"\1s "),
)

# Words that matter for understanding: small typos are corrected towards these.
_KEYWORDS = frozenset("""
expenses expense expenditure spending spend spent costs outgoings outflows income revenue received receive
earned invoices invoice receipts receipt documents document statement statements payments payment subscriptions
subscription recurring increased increase expensive prices accountant contabilista attention deadlines deadline
upcoming missing without unmatched closed complete completed finished connections connected disconnected
reconnect suspicious fraud blocked changed summary summarise summarize overview breakdown analyse analyze report
reports compare compared comparison versus quarter yesterday today tomorrow suppliers supplier companies company
balance profit forecast budget taxes activity handled happened reminder remind salaries payroll insurance
software internet electricity furniture travel meals restaurant transfer transfers refund refunds biggest largest
highest despesas gastos custos faturas recibos pagamentos receitas impostos
""".split()) | frozenset(_EN) | frozenset(_PT)

# Ordinary words that look like keywords ("sending" is not "spending"): never "corrected".
_COMMON = frozenset("""
sending send sent ending pending attending expected expect expensive exposure closer closet close closes clothes
import imported export support voice choice notice most host post month months mouth day days pavement recipe
recent recently decent account accounts count amount compare complete supply supplies description prescription
texas tasks task basket dues done gone bone none down owner water later latter letter better total totals hotel
hostel gravel level synergy officer offices interest internal external current recurrent occurring returning
turning where which while whose there their these those other others about above after again against before
being below between could would should doing during every first found great having might never often order
place right since small still thing things think three through under until using world write years yearly
quarterly weekly monthly daily anything something everything nothing please thanks thank hello morning evening
afternoon mention tension question questions answer answers tomorrow friday monday tuesday wednesday thursday
saturday sunday check checked issues issue problem problems change charge charged charges details detail block
email emails gmail working highest lowest smallest outgoing incoming outcome become welcome avenue venue sales
scale value values profits happen handle collected collect recovered recover cover covered record records missed
messing looking looked booking cooking previous prior earlier including include excluding exclude within
business businesses entity entities together overall combined altogether split group grouped category
categories type types kind kinds many more less least versus difference differ different similar same trend
higher lower decrease decreased went going rise rising fall falling pricing priced cheaper dearer pricier
spends spender spendy receiving receives receiver increases increasing reporting reported reporter reports
closing closure finish finishing inside outside income incomes invoiced invoicing billing billed paying payer
payee repayments repayment remember reminder reminders thousand hundred million euros euro cents money cash
weather whether winter summer spring autumn lisbon joke jokes funny sport sports football music movie
""".split())

_TYPO_CUTOFF = 0.82


def _normalise(text: str) -> str:
    t = text
    for pattern, repl in _REPLACEMENTS:
        t = re.sub(pattern, repl, t, flags=re.I)
    t = fold(t)
    t = re.sub(r"[^a-z0-9€.,/:@+\-' ]+", " ", t)
    t = re.sub(r"(?<![0-9])[.,](?![0-9])", " ", t)
    t = re.sub(r"'", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _correct(t: str, extra: set[str]) -> str:
    targets = sorted(_KEYWORDS | extra)
    known = _KEYWORDS | _COMMON | extra

    def fix(m: re.Match[str]) -> str:
        word = m[0]
        if len(word) < 5 or word in known or word in _MONTH_WORDS:
            return word
        # One slip of the finger, not another word: "weather" is not "water".
        slack = 2 if len(word) >= 10 else 1
        for close in difflib.get_close_matches(word, targets, n=3, cutoff=_TYPO_CUTOFF):
            if abs(len(close) - len(word)) <= slack:
                return close
        return word

    return re.sub(r"[a-z]+", fix, t)


# --------------------------------------------------------------------------- intents


INTENTS = ("spending", "income", "vat", "payment_lookup", "find_document", "report", "supplier_summary",
           "missing_invoices", "month_status", "needs_me", "deadlines", "subscriptions", "fraud", "connections",
           "accountant", "activity", "overview", "balance", "profit", "forecast")

_DOC = r"(?:invoices?|receipts?|documents?|docs?|bills?|faturas?|recibos?|statements?|letters?|pdfs?|copies|copy)"
_TAXWORD = r"\b(?:tax|taxes|vat|iva|impostos?|irs|irc|seguranca social|social security|withholding)\b"

# (intent, pattern, weight). An intent's score is its best matching weight, plus slot boosts below.
_CUES: tuple[tuple[str, str, float], ...] = (
    ("spending", r"\b(?:expenses?|expenditures?|spend|spends|spending|spent|costs?|outgoings?|outflows?|"
                 r"burn(?:ed|t)?|despesas?|gastos?|gastei|gastamos|gastaram|custos?)\b", 1.0),
    ("spending", r"\bmoney (?:out|going out|that went out|leaving)\b|\b(?:went|gone|go|going) out\b", 1.0),
    ("spending", r"\b(?:what|how much)\b.*\b(?:did|have|has|do|does)\b.*\b(?:pay|paid)\b", 0.9),
    ("spending", r"\bhow much\b.*\b(?:on|for)\b", 0.5),
    ("spending", r"\b(?:top|biggest|largest|main|highest)\b.*\b(?:suppliers?|vendors?|costs?|payments?|expenses?)\b",
     1.0),
    ("spending", r"\bpayments?\b|\bpagamentos\b", 0.5),
    ("spending", r"\bquanto\b.*\b(?:gast|pag)", 1.0),
    ("spending", r"\bwho did (?:we|i) pay\b|\b(?:paid|pay|spent|spend|cost|costs) (?:us )?the most\b|"
                 r"\b(?:list|show|which|who are|all)\b(?: of)?(?: my| our| the)? (?:suppliers|vendors)\b|"
                 r"\bmy suppliers\b|\baverage\b.*\b(?:spend|spending|costs?|expenses?)\b", 1.0),
    ("income", r"\b(?:income|revenues?|turnover|sales|earned|earn|earnings|inflows?|receitas?|recebemos|recebido|"
               r"recebi|vendas|faturacao)\b", 1.0),
    ("income", r"\brefunds?\b|\brefunded\b|\breimburse\w*\b|\bmoney back\b|\bcredit notes?\b", 1.0),
    ("income", r"\b(?:received|receive|got paid|get paid|getting paid|paid us|pay us|came in|come in|coming in|"
               r"money in|incoming)\b", 0.9),
    ("vat", r"\b(?:vat|iva)\b", 1.1),
    ("payment_lookup", r"^(?:so |and |but |ok |okay )?(?:did|have|has|was|were)\b.*\b(?:pay|paid|payment|go through|"
                       r"gone through|went through|debited|charged)\b", 1.0),
    ("payment_lookup", r"\bwhen did (?:we|i|you)\b.*\bpay\b|\b(?:last|latest|most recent) payment\b", 1.0),
    ("payment_lookup", r"\b(?:payments?|charges?|transactions?|debits?|transfers?|pagamentos?)\b", 0.45),
    ("find_document", rf"\b(?:find|show|get|where|send|forward|download|give|look(?:ing)? for|pull up|share|attach|"
                      rf"open|see|view|fetch|mail|email|need|want)\b.*\b{_DOC}\b", 1.2),
    ("find_document", rf"\b(?:which|what|any)\b.*\b{_DOC}\b.*\b(?:get|got|receive|received|arrive|arrived|come in|"
                      rf"came in)\b", 1.2),
    ("find_document", rf"\b{_DOC}\b", 0.5),
    ("report", r"\breports?\b|\brelatorios?\b", 1.3),
    ("report", r"\bexport\b", 0.9),
    ("supplier_summary", r"\b(?:summar\w*|overview|breakdown|analy[sz]\w*|check for (?:issues|problems)|"
                         r"anything (?:wrong|odd) with|history with|relationship with|review)\b", 1.3),
    ("missing_invoices", r"\bmissing\b|\bwithout (?:an? |their |its |the )?(?:invoices?|receipts?|documents?)\b|"
                         r"\bno (?:invoices?|receipts?)\b|\blooking for\b|\bunmatched\b|\bnot matched\b|"
                         r"\b(?:have not|not yet) (?:got|received|arrived)\b|\boutstanding (?:invoices?|documents?|"
                         r"receipts?)\b|\bchas(?:e|ed|ing)\b|\bem falta\b|\bfaltam?\b", 1.0),
    ("month_status", r"\b(?:closed?|closing|complete|completed|finished|finali[sz]ed|wrapped up|fechad[oa]|fechar|"
                     r"concluid[oa]|ready)\b", 0.4),
    ("month_status", r"\bdone\b", 0.3),
    ("month_status", r"\bmonth[ -]?end\b|\bmonth close\b|\bclose the month\b|\bclosing the month\b|"
                     r"\bfecho (?:do|de) mes\b", 1.1),
    ("month_status", r"\bhow far\b|\bprogress\b|\bstatus of\b|\bhow is\b.*\bgoing\b", 0.5),
    ("month_status", r"\bcan (?:we|i|you) close\b", 1.0),
    ("month_status", r"\bwhy\b.*\b(?:not|still)\b.*\b(?:closed|done|complete|finished|open)\b", 1.1),
    ("month_status", r"\b(?:missing|left|remaining|needed)\b.*\b(?:close|closing|finish)\b", 1.2),
    ("needs_me", r"\bneeds? (?:me|my|from me|anything from me)\b|\bmy (?:attention|input|answer|approval|decision)\b|"
                 r"\battention\b|\bwaiting (?:for|on) me\b|\bpending\b|\baction (?:required|needed)\b|"
                 r"\bquestions? for me\b|\bneeds you\b|\bdecisions?\b", 1.0),
    ("needs_me", r"\b(?:anything|something|what)\b.*\b(?:i|me)\b.*\b(?:need|have|should|must|got)\b.*\bto do\b", 1.0),
    ("needs_me", r"\bwhat should i do\b|\bto do\b|\bwhat do you need\b|\bdo you need anything\b", 0.9),
    ("needs_me", r"\bbelongs? to\b|\bwhich company\b.*\b(?:does|should|do|did)\b.*\b(?:belong|go)\b", 1.0),
    ("deadlines", r"\bdue\b|\boverdue\b|\bdeadlines?\b|\bupcoming\b|\bcoming up\b|\bprazos?\b|\bvencimentos?\b|"
                  r"\bnext\b.*\b(?:payments?|bills?|deadlines?|tax)\b", 1.0),
    ("deadlines", r"\b(?:need|have|must|got) to pay\b|\bto be paid\b|\bowe\b|\bbills? to pay\b|"
                  r"\bpay (?:soon|next|this week|next week)\b|\brenew\w*\b", 1.0),
    ("subscriptions", r"\bsubscri\w*\b|\brecurring\b|\bregular (?:costs?|payments?|bills?)\b|"
                      r"\bmonthly (?:costs?|payments?|bills?|charges?)\b|\bassinaturas?\b", 1.0),
    ("subscriptions", r"\b(?:went|gone|go|going|gone) up\b|\bincreas\w*\b|\bmore expensive\b|\bprice\b|\bprices\b|"
                      r"\bpricier\b|\baument\w*\b|\bsubiu\b|\bsubiram\b", 1.2),
    ("fraud", r"\bsafe\b|\blegit\w*\b|\bgenuine\b|\bscam\w*\b", 1.0),
    ("fraud", r"\bfraud\w*\b|\bscams?\b|\bsuspicious\b|\bphishing\b|\bbank details\b|\biban\b|"
              r"\bchanged (?:bank|account|details)\b|\bnew (?:bank account|account number|iban)\b|\bblocked\b|"
              r"\bon hold\b|\bheld\b|\bred flags?\b|\banything (?:odd|strange|wrong|weird|fishy)\b|\bfraude\b", 1.0),
    ("connections", r"\bconnections?\b|\bconnected\b|\bdisconnected\b|\bsync\w*\b|\breconnect\w*\b|\blinked\b|"
                    r"\bintegrations?\b", 1.0),
    ("connections", r"\b(?:gmail|outlook|email|inbox|bank|banks)\b.*\b(?:working|ok|up to date|reading|online)\b",
     1.0),
    ("accountant", r"\baccountants?\b|\bcontabilist\w*\b|\bcontabilidade\b|\bbookkeeper\b|\baccounting firm\b", 1.0),
    ("payment_lookup", r"\b(?:what happened|what is happening|what is going on|going on with|any news|news on|"
                       r"status of|update on)\b", 0.5),
    ("activity", r"\bwhat (?:have|did) you (?:done|do|been doing|handled|collected|found)\b|\bwhat happened\b|"
                 r"\bwhat is new\b|\brecent(?:ly)? (?:activity|work)\b|\bactivity\b|\bhandled\b|\bnews\b|\bupdates?\b",
     1.1),
    ("overview", r"\bhow (?:are|is) (?:we|things|business|the business|everything|it going|my business)\b|"
                 r"\boverview\b|\bstatus\b|\bdashboard\b|\bsituation\b|\beverything (?:ok|okay|fine|good|under control)\b|"
                 r"\ball good\b|\bany problems?\b|\bsummary\b", 0.8),
    ("balance", r"\bbalances?\b|\bhow much (?:money|cash) (?:do|have) (?:we|i)\b|\bcash (?:position|on hand|flow)\b|"
                r"\bsaldo\b|\bin the bank\b", 1.0),
    ("profit", r"\bprofit\w*\b|\bmargins?\b|\bp ?& ?l\b|\bprofit and loss\b|\bebitda\b|\blucro\b|\bnet income\b|"
               r"\bmake money\b|\bmaking money\b", 1.1),
    ("forecast", r"\bforecast\w*\b|\bpredict\w*\b|\bprojection\w*\b|\bbudget\w*\b|\bnext year\b|\bwill we spend\b",
     1.0),
)
_COMPILED = tuple((intent, re.compile(p), w) for intent, p, w in _CUES)

# Two readings that are really one question: which one answers it.
_PREFER: dict[frozenset[str], Any] = {
    frozenset({"spending", "payment_lookup"}):
        lambda s: "payment_lookup" if s.amount is not None or s.supplier_ids and not s.period else "spending",
    frozenset({"find_document", "payment_lookup"}): lambda s: "find_document",
    frozenset({"supplier_summary", "spending"}): lambda s: "supplier_summary",
    frozenset({"supplier_summary", "find_document"}): lambda s: "supplier_summary",
    frozenset({"missing_invoices", "find_document"}): lambda s: "missing_invoices",
    frozenset({"missing_invoices", "payment_lookup"}): lambda s: "missing_invoices",
    frozenset({"overview", "needs_me"}): lambda s: "needs_me",
    frozenset({"overview", "month_status"}): lambda s: "month_status",
    frozenset({"overview", "spending"}): lambda s: "spending",
    frozenset({"deadlines", "vat"}): lambda s: "deadlines",
    frozenset({"deadlines", "payment_lookup"}): lambda s: "deadlines",
    frozenset({"deadlines", "needs_me"}): lambda s: "deadlines",
    frozenset({"deadlines", "spending"}): lambda s: "deadlines",
    frozenset({"subscriptions", "spending"}): lambda s: "subscriptions",
    frozenset({"report", "spending"}): lambda s: "report",
    frozenset({"report", "find_document"}): lambda s: "report",
    frozenset({"vat", "spending"}): lambda s: "vat",
    frozenset({"vat", "payment_lookup"}): lambda s: "vat",
    frozenset({"fraud", "payment_lookup"}): lambda s: "fraud",
    frozenset({"fraud", "needs_me"}): lambda s: "fraud",
    frozenset({"accountant", "needs_me"}): lambda s: "accountant",
    frozenset({"income", "payment_lookup"}): lambda s: "income",
    frozenset({"profit", "income"}): lambda s: "profit",
    frozenset({"forecast", "spending"}): lambda s: "forecast",
    frozenset({"balance", "spending"}): lambda s: "balance",
}

THRESHOLD = 0.6
MARGIN = 0.15


@dataclass
class Slots:
    """The details of a message: when, which companies, suppliers and category, amount, recipients."""

    periods: list[Period] = field(default_factory=list)
    company_ids: list[str] = field(default_factory=list)
    all_companies: bool = False
    supplier_ids: list[str] = field(default_factory=list)
    category: str | None = None
    exclude: list[str] = field(default_factory=list)
    amount: Decimal | None = None
    emails: list[str] = field(default_factory=list)
    to_accountant: bool = False
    direction: str | None = None
    group_by: str | None = None
    compare: bool = False
    average: bool = False
    top: int | None = None
    doc_number: str = ""

    @property
    def period(self) -> Period | None:
        return self.periods[0] if self.periods else None

    def empty(self) -> bool:
        return not (self.periods or self.company_ids or self.supplier_ids or self.category or self.amount is not None)

    def merged_from(self, older: Slots) -> Slots:
        """These slots, completed with what an earlier question named (for follow-ups)."""
        return replace(self, periods=self.periods or older.periods,
                       company_ids=self.company_ids or ([] if self.all_companies else older.company_ids),
                       supplier_ids=self.supplier_ids or older.supplier_ids,
                       category=self.category or older.category, amount=self.amount
                       if self.amount is not None else older.amount, group_by=self.group_by or older.group_by)


@dataclass
class Understanding:
    """The chosen intent (or "clarify" and the one short question to ask), its score and the slots."""

    intent: str  # one of INTENTS, or "greeting" | "help" | "thanks" | "clarify" | "unknown"
    score: float
    slots: Slots
    text: str  # the normalised message
    scores: dict[str, float] = field(default_factory=dict)
    clarify: str = ""
    options: tuple[str, ...] = ()
    follow_up: bool = False


_GREETING = re.compile(r"^(?:(?:hi|hello|hey|hiya|yo|ola|oi|bom dia|boa tarde|boa noite|good (?:morning|afternoon|"
                       r"evening|day)|dear|greetings)\b[\s,!.]*(?:there|claude|operator|team)?[\s,!.]*)+")
_POLITE = re.compile(r"^(?:(?:please|pls|quick question|question|so|ok|okay|right|well|um|hmm|can you|could you|"
                     r"would you|will you|can i|could i|i want to know|i would like to know|i d like to know|"
                     r"id like to know|i wonder|i am wondering|tell me|let me know|do you know)\b[\s,:]*)+")
_HELP = re.compile(r"^(?:help|help me|what can you do|what do you do|how does this work|how do you work|"
                   r"what can i ask(?: you)?|what can you help with|what are you|who are you|commands|options|menu)"
                   r"[\s?!.]*$")
_THANKS = re.compile(r"^(?:(?:thanks?|thank you|thx|ty|cheers|obrigad[oa]|great|perfect|ok|okay|cool|nice|"
                     r"got it|brilliant|excellent|awesome|super|lovely|good|fine)(?: (?:a lot|so much|very much|"
                     r"again|for (?:that|this|the help|your help)|you))*[\s!.]*)+$")
# Words an elliptical follow-up may carry besides names, periods and categories:
# "and in September?", "what about Company C?", "same for last year", "e em agosto?".
_FOLLOW_FILLER = frozenset("""
and what about how same for also now then in on at of the a an it its that this those these them one ones
please only just by from to with is are was were did do does was so ok okay again too as well or vs versus
compared compare than last previous next this past year years month months quarter week weeks day days
e em no na nos nas de do da dos das para o a os as tambem mesmo e sobre ano mes semana
""".split())


def unrelated_words(text: str, vocab: Vocabulary) -> set[str]:
    """Content words in ``text`` that are not names, periods, categories, amounts or follow-up filler.

    An elliptical follow-up ("and in August?", "what about Adobe?") has none;
    "what is the weather in Porto?" has "weather", so it is a new question,
    never the previous one again.
    """
    names = {w for vs in (*vocab.companies.values(), *vocab.suppliers.values(), *vocab.categories.values())
             for v in vs for w in v.split()}
    words = re.findall(r"[a-z]+", text)
    known = _FOLLOW_FILLER | names | set(_EN) | set(_PT) | {m[:3] for m in _EN} | _KEYWORDS
    return {w for w in words if len(w) > 1 and w not in known}


_FOLLOW_UP = re.compile(r"^(?:and|what about|how about|and what about|and how about|same for|and for|also|now|"
                        r"then|e|e em|e no|e na)\b")


def understand(message: str, vocab: Vocabulary, today: date) -> Understanding:
    """Read one owner message into an intent and slots (see the module docstring)."""
    original = message.strip()
    slots = Slots(emails=_EMAIL.findall(original))
    number = _DOC_NUMBER.search(_EMAIL.sub(" ", original))
    slots.doc_number = number[0] if number else ""
    t = _normalise(_EMAIL.sub(" ", original))
    greeted = bool(_GREETING.match(t))
    t = _GREETING.sub("", t).strip()
    for _ in range(2):  # before and after "please", "can you", "tell me"
        if _HELP.match(t):
            return Understanding("help", 1.0, slots, t)
        if t and _THANKS.match(t):
            return Understanding("thanks", 1.0, slots, t)
        t = _POLITE.sub("", t).strip()
    if not t:
        return Understanding("greeting" if greeted else "unknown", 1.0 if greeted else 0.0, slots, t)
    t = _correct(t, vocab.words())
    if re.search(r"\bto (?:my|our|the) (?:accountant|contabilista|bookkeeper)\b", t):
        slots.to_accountant = True
        t = re.sub(r"\bto (?:my|our|the) (?:accountant|contabilista|bookkeeper)\b", " ", t).strip()

    slots.periods, spans = find_periods(t, today)
    blocked = list(spans)
    company_hits = _find(t, vocab.companies, blocked)
    blocked += [(a, b) for a, b, _ in company_hits]
    supplier_hits = _find(t, vocab.suppliers, blocked)
    blocked += [(a, b) for a, b, _ in supplier_hits]
    slots.company_ids = list(dict.fromkeys(k for _, _, k in company_hits))
    slots.supplier_ids = list(dict.fromkeys(k for _, _, k in supplier_hits))
    slots.all_companies = bool(re.search(r"\b(?:all|every|each|both|across|all of)\s+(?:of\s+)?(?:my\s+|the\s+|our\s+|"
                                         r"three\s+)?(?:companies|company|businesses|business|entities)\b|"
                                         r"\b(?:per|by) (?:company|business)\b|\boverall\b|\bcombined\b|"
                                         r"\baltogether\b|\btodas as empresas\b", t))
    excluded = re.search(r"\b(?:excluding|without|except|not counting|leaving out|apart from|other than|sem)\s+"
                         r"(?:the\s+)?(.+)$", t)
    exclusion_start = excluded.start() if excluded else len(t)
    for a, b, key in _find(t, vocab.categories, blocked):
        if a >= exclusion_start:
            slots.exclude.append(key)
        elif slots.category is None:
            slots.category = key
        blocked.append((a, b))
    slots.amount = _amount(t, spans)
    if re.search(r"\b(?:by|per|each|split by|grouped by)\s+(?:company|companies|business)\b|"
                 r"\bwhich compan(?:y|ies)\b", t):
        slots.group_by = "company"
    elif re.search(r"\b(?:by|per|each|split by|grouped by|across)\s+(?:supplier|vendor|merchant)s?\b|"
                   r"\b(?:top|biggest|largest|main|highest|which) (?:\d+ )?(?:suppliers?|vendors?)\b|"
                   r"\bwho (?:did we|do we|have we) (?:pay|paid|spend)\b|\bthe most\b|"
                   r"\b(?:list|show|all|my|our)(?: of)?(?: my| our| the)? (?:suppliers|vendors)\b", t):
        slots.group_by = "supplier"
    elif re.search(r"\b(?:by|per|each|split by|grouped by)\s+(?:category|categories|type|kind)\b|"
                   r"\b(?:on what|what on|what for|categories)\b", t):
        slots.group_by = "category"
    elif re.search(r"\b(?:by|per|each)\s+month\b|\bmonth by month\b|\bmonthly breakdown\b", t):
        slots.group_by = "month"
    slots.average = bool(re.search(r"\baverage\b|\bon average\b|\bper month\b|\ba month\b|\bmonthly average\b|"
                                   r"\bmedia\b|\bpor mes\b", t))
    slots.compare = bool(re.search(r"\b(?:vs|versus|compar\w*|than|difference|change from|up or down|more or less|"
                                   r"trend|against)\b", t)) or len(slots.periods) > 1
    if top := re.search(r"\btop (\d{1,2}|three|five|ten)\b", t):
        slots.top = int(top[1]) if top[1].isdigit() else _NUMBER_WORDS[top[1]]
    if re.search(r"\b(?:received|income|revenue|came in|come in|money in|earned|sales|receitas|recebi\w*)\b", t):
        slots.direction = "in"
    elif re.search(r"\b(?:spent|spend|spending|paid|pay|costs?|expenses?|outgoings?|money out|went out)\b", t):
        slots.direction = "out"

    scores = _score(t, slots, vocab)
    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], INTENTS.index(kv[0])))
    follow_up = bool(_FOLLOW_UP.match(t))
    u = Understanding("unknown", 0.0, slots, t, scores, follow_up=follow_up)
    if not ranked or ranked[0][1] < THRESHOLD:
        # Only names or a period and nothing else ("August?"): ask which question. With other words
        # the owner asked something I don't do ("who won the football yesterday?"): say so instead.
        vague = not slots.empty() and not (unrelated_words(t, vocab) and not ranked)
        u.intent, u.score = ("clarify", ranked[0][1] if ranked else 0.0) if vague else ("unknown", 0.0)
        if u.intent == "clarify":
            u.options = _slot_options(slots)
            u.clarify = _slot_question(slots, vocab, today)
        return u
    best, score = ranked[0]
    if len(ranked) > 1 and ranked[1][1] >= THRESHOLD and score - ranked[1][1] < MARGIN:
        other = ranked[1][0]
        settle = _PREFER.get(frozenset({best, other}))
        if settle is None:
            u.intent, u.score, u.options = "clarify", score, (best, other)
            u.clarify = (f"Do you mean {describe(best, slots, vocab, today)}, or "
                         f"{describe(other, slots, vocab, today)}?")
            return u
        best = settle(slots)
        score = scores[best]
    u.intent, u.score = best, score
    return u


def _score(t: str, s: Slots, vocab: Vocabulary) -> dict[str, float]:
    scores: dict[str, float] = {}
    for intent, pattern, weight in _COMPILED:
        if weight > scores.get(intent, 0.0) and pattern.search(t):
            scores[intent] = weight
    doc_noun = re.search(rf"\b{_DOC}\b", t) is not None
    monthish = any(p.grain in ("month", "quarter", "year") for p in s.periods) or \
        re.search(r"\bmonths?\b|\bmes\b", t) is not None
    weak_spending = scores.get("spending", 1.0) <= 0.5
    if "spending" in scores:
        base = scores["spending"]
        if base <= 0.5:  # only "payments" or "how much … on"
            if s.amount is not None or doc_noun:
                base = 0.3  # "the €418 payment" is a lookup; "invoices" is about documents
            elif s.periods or s.category:
                base = 0.9  # "payments in September", "how much on software"
        if base >= 0.9 and (s.periods or s.category or s.company_ids or s.supplier_ids):
            base += 0.1
        scores["spending"] = base
    if s.category and not scores:
        scores["spending"] = 0.7  # "software?" / "rent in August"
    if len(s.periods) > 1 and s.compare and max(scores.values(), default=0) < THRESHOLD:
        scores["spending"] = 0.9  # "August vs September"
    if "payment_lookup" in scores:
        if s.amount is not None:
            scores["payment_lookup"] = min(1.0, scores["payment_lookup"] + 0.55)
        if scores["payment_lookup"] >= 1.0 and weak_spending and "spending" in scores:
            scores["spending"] = min(scores["spending"], 0.8)  # "did the tax payment go through?" is yes or no
    elif s.amount is not None:
        scores["payment_lookup"] = 0.7
    if "find_document" in scores:
        if scores["find_document"] == 0.5 and (s.supplier_ids or s.amount is not None or s.doc_number or s.periods):
            scores["find_document"] = 0.8
        if s.doc_number:
            scores["find_document"] = max(scores["find_document"], 1.0)
    elif s.doc_number:
        scores["find_document"] = 0.9
    if "supplier_summary" in scores and not s.supplier_ids:
        del scores["supplier_summary"]
    if "missing_invoices" in scores and doc_noun:
        scores["missing_invoices"] += 0.3
    if "month_status" in scores and scores["month_status"] < 1.0:
        scores["month_status"] = scores["month_status"] + (0.6 if monthish or s.company_ids else 0.0)
    if "deadlines" in scores and re.search(_TAXWORD, t):
        scores["deadlines"] += 0.3
    if s.supplier_ids and re.search(r"\b(?:what happened|what is happening|what is going on|going on with|any news|"
                                    r"news on|status of|update on)\b", t):
        scores["payment_lookup"] = 1.2  # "what happened with the EDP invoice?": that supplier's latest state
    if s.supplier_ids and "activity" in scores:
        scores["activity"] = 0.5  # "what happened" about one supplier is not the week's activity
    if "accountant" not in scores and vocab.accountant and any(
            re.search(rf"\b{re.escape(w)}\b", t) for w in vocab.accountant):
        scores["accountant"] = 1.0
    if "accountant" in scores and re.search(r"\b(?:ask|asked|questions?|want|wants|need|needs|say|said)\b", t):
        scores["accountant"] += 0.1
    if "subscriptions" in scores and "spending" in scores and scores["subscriptions"] >= 1.0:
        scores["subscriptions"] += 0.1
    return {k: round(v, 3) for k, v in scores.items() if v > 0}


def _slot_options(s: Slots) -> tuple[str, ...]:
    if s.supplier_ids:
        return ("spending", "find_document", "supplier_summary")
    if s.periods or s.company_ids:
        return ("spending", "month_status")
    return ("spending",)


def _names(ids: Iterable[str], table: Mapping[str, str]) -> str:
    names = [table.get(i, i) for i in ids]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + f" and {names[-1]}"


def _slot_question(s: Slots, vocab: Vocabulary, today: date) -> str:
    if s.supplier_ids:
        who = _names(s.supplier_ids, vocab.supplier_names)
        return f"What would you like to know about {who}: what you paid, its invoices, or a summary with any issues?"
    if s.periods or s.company_ids:
        return f"Do you want {describe('spending', s, vocab, today)}, or {describe('month_status', s, vocab, today)}?"
    return "What would you like to know? For example “What did we spend in September?” or “What needs me?”"


def describe(intent: str, s: Slots, vocab: Vocabulary, today: date) -> str:
    """A few words for one reading of the question, used in clarifying questions."""
    p = s.period
    when = f" {p.phrase}" if p else ""
    who = _names(s.company_ids, vocab.company_names) if s.company_ids else ""
    sup = _names(s.supplier_ids, vocab.supplier_names) if s.supplier_ids else ""
    closing = date(today.year, today.month, 1) - timedelta(days=1)
    month = p.label if p is not None and p.grain == "month" else _month_label(closing.year, closing.month, today)
    if intent == "spending":
        on = f" on {vocab.category_labels.get(s.category, '').lower()}" if s.category else ""
        paid = f"what {who or 'you'} paid {sup}" if sup else f"what {who or 'you'} spent{on}"
        return paid + when
    return {
        "income": f"what came in{when}",
        "vat": f"the VAT{when}",
        "payment_lookup": f"the payment{' to ' + sup if sup else ''}{when}",
        "find_document": f"the {sup + ' ' if sup else ''}invoices{when}",
        "report": f"a report{when}",
        "supplier_summary": f"a summary of {sup or 'a supplier'}",
        "missing_invoices": "the payments still without an invoice",
        "month_status": f"whether {who + '’s ' if who else ''}{month} is closed",
        "needs_me": "what needs your answer",
        "deadlines": "what is due soon",
        "subscriptions": "your regular costs and price increases",
        "fraud": "payments held because bank details changed",
        "connections": "whether everything is connected",
        "accountant": "your accountant’s questions",
        "activity": "what I did recently",
        "overview": "an overview of your companies",
        "balance": "your bank balance",
        "profit": "your profit",
        "forecast": "a forecast",
    }.get(intent, intent.replace("_", " "))
