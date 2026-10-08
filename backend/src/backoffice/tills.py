"""Till reports (Z reports) and the cash box, read and added up (checklist X4, X5; cases 2 and 19).

Till reports
    A shop's or restaurant's end-of-day report ("Relatório Z", "Fecho de caixa", "Z report", "End of day
    report"), from a text layer (a PDF's text, a photo read, a text file) or a CSV export from the till
    software (one row per day). Each day is read into its takings by way of payment: cash, card and any other
    (MB Way, vouchers), the total, the report's number and the business's tax number when printed. A day adds
    up when cash + card + other = the total it states (no total: the parts are the total). One that does not
    add up is never used: it is one plain question.

The cash box
    One per company: the till's cash and the petty cash float are the same box. What goes in: cash taken out
    of the bank for it, and the cash sales of the till reports. What goes out: cash paid into the bank, and
    purchases paid in cash (their receipts). The owner may count it; a count is taken as the truth from then
    on, and the difference between the count and what the box should hold is shown, never booked as a cost or
    as money in (never silently absorbed). :func:`cash_box_period` adds up one period and says it in one
    plain line.

Pure Python, no I/O. Money is Decimal. The layouts and words are conventions from English tills and, in the
country packs ("tills.*", backoffice.countries.wording), Portuguese ones, not verified against every till
software's export (verified_as_of: never).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from decimal import Decimal
from functools import cache

from backoffice._reading import amounts, column, csv_rows, first_date, money, tax_ids
from backoffice.countries import PACK_WORDS, LazyPattern, pack_alternatives, spliced
from backoffice.learning.keys import fold
from backoffice.learning.plain import day_month, format_money, join_and

__all__ = ["CashBoxPeriod", "CashCount", "CashEntry", "TillDay", "cash_box_period", "read_till_reports"]

_ZERO = Decimal("0.00")
MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December")

# --------------------------------------------------------------------------- till reports

# A country's own till words ("Relatório Z", "Fecho de caixa", "Multibanco", "Numerário") are in its pack
# (backoffice.countries.wording): "tills.<concept>", regular-expression alternatives over folded text.
_TITLE = LazyPattern(lambda: (
    rf"(?<![a-z])(?:{pack_alternatives('tills.title')}|"
    r"z[\s-]?report|end[\s-]+of[\s-]+day\s+report|daily\s+takings\s+report|informe\s+z|cierre\s+de\s+caja)"
    r"(?![a-z])"))
_NUMBER = LazyPattern(lambda: (rf"(?<![a-z])(?:{pack_alternatives('tills.number')}|z[\s-]?report|informe\s+z|z)\s*"
                               r"(?:n\.?\s*[ºo°]?\.?|no\.?|#|numero|number)?\s*[:.]?\s*(\d{1,8})(?![\d/.-])"))
_OTHER = LazyPattern(lambda: (rf"(?<![a-z])(?:{pack_alternatives('tills.other')}|other|vales?|vouchers?|cheques?|"
                              r"transferencias?)(?![a-z])"))
_CARD = LazyPattern(lambda: (rf"(?<![a-z])(?:{pack_alternatives('tills.card')}|cards?|visa|mastercard|amex|debito|"
                             r"credito)(?![a-z])"))
_CASH = LazyPattern(lambda: rf"(?<![a-z])(?:{pack_alternatives('tills.cash')}|cash|efectivo|especie)(?![a-z])")
_TOTAL = LazyPattern(lambda: rf"(?<![a-z])(?:total|{pack_alternatives('tills.total')}|takings|gross\s+sales)(?![a-z])")
_SKIP = LazyPattern(lambda: (rf"(?<![a-z])(?:iva|vat|{pack_alternatives('tills.skip')}|tax|base|change|refunds?|"
                             r"discounts?|voids?|float|abertura|opening)(?![a-z])"))
_DATE_LINE = LazyPattern(lambda: (rf"^\s*(?:data|date|dia|day)(?:\s+(?:{pack_alternatives('tills.date_suffix')}|"
                                   r"report))?\b"))

# CSV header cells by role (folded words, tried in order), with a pack's own where it has them ("tills.csv:<role>").
_P = PACK_WORDS
_CSV = {
    "date": ("data", "date", "dia", "day", _P, "business date", _P),
    "number": ("z", "n z", "no z", "n o z", "numero z", "z number", "z no", "report", "report no", _P, "numero"),
    "cash": (_P, "cash", "efectivo", _P, "cash sales"),
    "card": (_P, "card", "cards", _P, "card sales", _P),
    "other": (_P, "other", _P, "vales", "vouchers"),
    "total": ("total", _P, "total sales", "takings", _P, "gross sales"),
    "tax_id": ("nif", _P, "tax id", "vat number", _P),
}


@cache
def _csv_aliases() -> dict[str, tuple[str, ...]]:
    return {role: spliced(f"tills.csv:{role}", aliases) for role, aliases in _CSV.items()}


@dataclass(frozen=True)
class TillDay:
    """One day's takings from a till report."""

    day: date
    cash: Decimal
    card: Decimal
    other: Decimal
    total: Decimal
    total_stated: bool
    number: str | None = None
    tax_id: str | None = None
    currency: str = "EUR"
    location: str = ""  # where in the file ("line 7", "csv:row 3")

    @property
    def parts(self) -> Decimal:
        return self.cash + self.card + self.other

    @property
    def adds_up(self) -> bool:
        return self.parts == self.total

    @property
    def identity(self) -> tuple[date, str, Decimal]:
        return self.day, (self.number or "").lstrip("0"), self.total

    def money(self, value: Decimal) -> str:
        return format_money(value, self.currency)

    def label(self, today: date | None = None) -> str:
        return f"the till report of {day_month(self.day, today)}"

    def split(self) -> str:
        """'€225.00 in cash and €1,425.40 by card'"""
        parts = [f"{self.money(self.cash)} in cash", f"{self.money(self.card)} by card"]
        if self.other:
            parts.append(f"{self.money(self.other)} paid other ways")
        return join_and(parts)

    def mismatch(self) -> str:
        """Why it does not add up, in one plain sentence."""
        return (f"{self.split()[0].upper()}{self.split()[1:]} is {self.money(self.parts)}, but it says "
                f"{self.money(self.total)} in all.")


def read_till_reports(text: str) -> list[TillDay]:
    """Every day a till report file describes (a text layer or a CSV export), else ``[]``."""
    if not text or not text.strip():
        return []
    table = csv_rows(text)
    if table is not None:
        days = _from_csv(*table)
        if days:
            return days
    folded = fold(text)
    starts = [m.start() for m in _TITLE.finditer(folded)]
    if not starts:
        return []
    out: list[TillDay] = []
    original_lines = text.splitlines()
    folded_lines = [fold(line) for line in original_lines]
    offsets, pos = [], 0
    for line in folded_lines:
        offsets.append(pos)
        pos += len(line) + 1
    # One report per title; a title on the next line of the one before is the same report.
    bounds: list[int] = []
    for s in starts:
        line_no = max(i for i, o in enumerate(offsets) if o <= s) if offsets else 0
        if not bounds or line_no - bounds[-1] > 2:
            bounds.append(line_no)
    everywhere = tax_ids(text)
    for n, first in enumerate(bounds):
        first = 0 if n == 0 else first  # the business's name and tax number are printed above the first title
        last = bounds[n + 1] if n + 1 < len(bounds) else len(folded_lines)
        found = _from_lines(original_lines[first:last], folded_lines[first:last], first)
        if found is not None and found.tax_id is None and len(everywhere) == 1:
            found = replace(found, tax_id=everywhere[0])
        if found is not None:
            out.append(found)
    return out


def _currency(text: str) -> str:
    return "GBP" if "£" in text or re.search(r"\bGBP\b", text) else "EUR"


def _from_lines(lines: Sequence[str], folded: Sequence[str], offset: int) -> TillDay | None:
    joined = "\n".join(folded)
    day = next((first_date(line) for line in lines if _DATE_LINE.match(fold(line)) and first_date(line)), None)
    day = day or first_date("\n".join(lines))
    if day is None:
        return None
    found: dict[str, tuple[Decimal, int]] = {}
    for i, (raw, line) in enumerate(zip(lines, folded, strict=True)):
        values = amounts(raw)
        if not values or _SKIP.search(line) and not (_CASH.search(line) or _CARD.search(line)):
            continue
        if _OTHER.search(line):
            role = "other"
        elif _CARD.search(line):
            role = "card"
        elif _CASH.search(line):
            role = "cash"
        elif _TOTAL.search(line):
            role = "total"
        else:
            continue
        if role == "other" and role in found:
            found[role] = (found[role][0] + values[-1], found[role][1])
        elif role not in found:
            found[role] = (values[-1], offset + i + 1)
    if "cash" not in found and "card" not in found:
        return None
    number = _NUMBER.search(joined)
    cash = found.get("cash", (_ZERO, 0))[0]
    card = found.get("card", (_ZERO, 0))[0]
    other = found.get("other", (_ZERO, 0))[0]
    stated = "total" in found
    total = found["total"][0] if stated else cash + card + other
    ids = tax_ids("\n".join(lines))
    where = f"line {found.get('total', found.get('cash', found.get('card')))[1]}"
    return TillDay(day=day, cash=cash, card=card, other=other, total=total, total_stated=stated,
                   number=number.group(1) if number else None, tax_id=ids[0] if ids else None,
                   currency=_currency("\n".join(lines)), location=where)


def _from_csv(header: list[str], rows: list[list[str]]) -> list[TillDay]:
    col = {role: column(header, aliases) for role, aliases in _csv_aliases().items()}
    if col["date"] is None or col["cash"] is None or col["card"] is None:
        return []

    def cell(row: list[str], role: str) -> str:
        i = col[role]
        return row[i].strip() if i is not None and i < len(row) else ""

    out: list[TillDay] = []
    for n, row in enumerate(rows, start=2):
        if re.match(r"^\s*total", fold(cell(row, "date"))):
            continue  # the export's own total line
        day = first_date(cell(row, "date"))
        if day is None:
            continue
        values = {role: money(cell(row, role)) if cell(row, role) else None for role in ("cash", "card", "other",
                                                                                            "total")}
        if values["cash"] is None and values["card"] is None:
            continue
        cash, card, other = (values[r] or _ZERO for r in ("cash", "card", "other"))
        stated = values["total"] is not None
        ids = tax_ids(cell(row, "tax_id"))
        out.append(TillDay(day=day, cash=cash, card=card, other=other,
                           total=values["total"] if stated else cash + card + other,  # type: ignore[arg-type]
                           total_stated=stated, number=cell(row, "number") or None, tax_id=ids[0] if ids else None,
                           currency=_currency(" ".join(row)), location=f"csv:row {n}"))
    return out


# --------------------------------------------------------------------------- the cash box

BANK, TILL, DEPOSIT, RECEIPT = "bank", "till", "deposit", "receipt"


@dataclass(frozen=True)
class CashEntry:
    """One movement of the cash box: positive into it, negative out of it."""

    on: date
    amount: Decimal
    kind: str  # bank (taken out of the bank) | till (cash sales) | deposit (paid into the bank) | receipt (spent)
    label: str
    subject_id: str
    evidence_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class CashCount:
    """The owner counted the cash box: the amount in it on a day (their word is the evidence)."""

    company_id: str
    on: date
    amount: Decimal
    evidence_id: str
    currency: str = "EUR"


@dataclass
class CashBoxPeriod:
    """The cash box over one period: what it started with, what came in and went out, what it should hold."""

    start: date
    end: date
    opening: Decimal
    closing: Decimal  # what it should hold at the end (after the last count, from that count)
    came_in: dict[str, Decimal] = field(default_factory=dict)
    went_out: dict[str, Decimal] = field(default_factory=dict)
    entries: list[CashEntry] = field(default_factory=list)
    count: CashCount | None = None  # the last count in the period
    expected_at_count: Decimal | None = None  # what the box should have held when it was counted
    currency: str = "EUR"

    @property
    def difference(self) -> Decimal | None:
        """Counted minus what it should have held (None without a count)."""
        if self.count is None or self.expected_at_count is None:
            return None
        return self.count.amount - self.expected_at_count

    @property
    def active(self) -> bool:
        return bool(self.entries or self.count is not None or self.opening)

    def m(self, value: Decimal) -> str:
        return format_money(value, self.currency)

    def _flows(self) -> str:
        """'€200.00 taken from the bank in, €54.20 in cash receipts out' (and what it started with)."""
        words_in = {BANK: "taken from the bank", TILL: "in cash sales"}
        words_out = {RECEIPT: "in cash receipts", DEPOSIT: "paid into the bank"}
        came = [f"{self.m(v)} {words_in[k]}" for k, v in sorted(self.came_in.items()) if v]
        went = [f"{self.m(v)} {words_out[k]}" for k, v in sorted(self.went_out.items()) if v]
        parts = [f"it started with {self.m(self.opening)}"] if self.opening else []
        parts.append(f"{join_and(came)} came in" if came else "nothing came in")
        parts.append(f"{join_and(went)} went out" if went else "nothing went out")
        return "; ".join(parts)

    @property
    def unexplained(self) -> bool:
        """Something the records do not explain: a count that differs, or more cash spent than came in."""
        diff = self.difference
        return (diff is not None and diff != 0) or (self.count is None and self.closing < 0)

    def line(self, *, period: str) -> str:
        """The period in one plain line: what the box should hold, and anything not explained (never absorbed)."""
        diff = self.difference
        if self.count is not None and diff is not None:
            when = day_month(self.count.on)
            if diff == 0:
                return f"The cash box matches your count of {self.m(self.count.amount)} on {when}."
            side = "less" if diff < 0 else "more"
            return (f"Your count of {self.m(self.count.amount)} on {when} is {self.m(abs(diff))} {side} than the "
                    f"{self.m(self.expected_at_count or _ZERO)} the cash box should hold. I have not counted the "
                    "difference as a cost or as money in: it stays open until you or your accountant explain it.")
        if self.closing > 0:
            return (f"At the end of {period} the cash box should hold {self.m(self.closing)} ({self._flows()}). "
                    "Count it and tell me the amount, so any difference is not lost.")
        if self.closing < 0:
            return (f"At the end of {period} {self.m(-self.closing)} more cash went out of the cash box than came "
                    f"in ({self._flows()}). Some cash came from somewhere I can't see: tell me where, or send me "
                    "what is missing.")
        return f"The cash box is even at the end of {period} ({self._flows()})."


def cash_box_period(entries: Iterable[CashEntry], counts: Iterable[CashCount], start: date, end: date,
                    *, currency: str = "EUR") -> CashBoxPeriod:
    """Add up the cash box from ``start`` to ``end``: a count resets it to what was counted (the truth from then
    on); movements on the day of a count come before it."""
    ordered: list[tuple[date, int, CashEntry | CashCount]] = [(e.on, 0, e) for e in entries]
    ordered += [(c.on, 1, c) for c in counts]
    ordered.sort(key=lambda x: (x[0], x[1], getattr(x[2], "subject_id", "") or getattr(x[2], "evidence_id", "")))
    balance = _ZERO
    period = CashBoxPeriod(start=start, end=end, opening=_ZERO, closing=_ZERO, currency=currency)
    opened = False
    for on, _, thing in ordered:
        if on > end:
            break
        if on >= start and not opened:
            period.opening, opened = balance, True
        if isinstance(thing, CashCount):
            if on >= start:
                period.count, period.expected_at_count = thing, balance
            balance = thing.amount
            continue
        balance += thing.amount
        if on >= start:
            period.entries.append(thing)
            bucket = period.came_in if thing.amount > 0 else period.went_out
            bucket[thing.kind] = bucket.get(thing.kind, _ZERO) + abs(thing.amount)
    if not opened:
        period.opening = balance
    period.closing = balance
    return period


def month_bounds(year: int, month: int) -> tuple[date, date]:
    first = date(year, month, 1)
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    return first, nxt - timedelta(days=1)
