"""Supplier account statements (extrato de conta corrente), read line by line and checked (§20, §22, §28).

A supplier's statement lists what it thinks the business owes it: its
invoices, credit notes and debit notes, the payments it received, and a
running and closing balance. It is supporting evidence only: never booked,
never enough to close a payment, never a second copy of an invoice (§3).
What it is good for is checking: this module reads its lines and compares
them with the business's own documents and bank payments from that supplier.

Reading (:func:`read_statement`)
    English layouts and a pack's (Portugal's: "supplier_statements.*"), from a text layer (a PDF's text, an email,
    a text file) or a CSV export. Each line keeps its date, document number,
    description, debit, credit and running balance. Which side a line is on
    follows from the running balance when there is one (the balance goes up
    by an invoice), else from the document's kind; a statement kept "the other
    way round" (invoices as credits) is turned the right way. Lines whose
    running balance does not follow from the line before, or a closing
    balance that is not the opening balance plus the lines, are reported:
    such a statement does not add up, and is shown as such.

Checking (:func:`check_statement`)
    * invoices, credit notes and debit notes: by document number, else by a
      unique amount a few days apart; a matched number with another amount is
      an amount difference (one plain question, never a silent change);
    * payments: by amount and date against the business's bank payments;
    * documents on the statement the business does not have: missing
      documents (the caller asks the supplier for them);
    * the business's own documents from the supplier's period missing from
      the statement: listed for the accountant;
    * the closing balance against the business's own open items (invoices
      not paid yet, less credit notes not used yet).

Pure Python, no I/O (runs in the browser build too). Money is Decimal.
"""

from __future__ import annotations

import csv
import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal, InvalidOperation
from functools import cache

from backoffice.countries import LazyPattern, pack_alternatives, pack_words
from backoffice.learning.keys import fold
from backoffice.learning.plain import count_phrase, day_month, format_money, join_and

__all__ = [
    "CREDIT_NOTE",
    "DEBIT_NOTE",
    "DOCUMENT_KINDS",
    "INVOICE",
    "PAYMENT",
    "OurDocument",
    "OurPayment",
    "RowCheck",
    "StatementCheck",
    "StatementLine",
    "SupplierStatement",
    "check_statement",
    "looks_like_statement",
    "read_statement",
    "same_number",
]

INVOICE = "invoice"
CREDIT_NOTE = "credit_note"
DEBIT_NOTE = "debit_note"
PAYMENT = "payment"
OPENING = "opening"
CLOSING = "closing"
OTHER = "other"
DOCUMENT_KINDS = frozenset({INVOICE, CREDIT_NOTE, DEBIT_NOTE})
_ZERO = Decimal("0.00")
_CENT = Decimal("0.01")
AMOUNT_MATCH_DAYS = 10  # a document found by amount alone must be dated this close to the line
PAYMENT_MATCH_DAYS = 7  # suppliers book a payment on the day it reaches them, not the day it left
_KIND_WORDS = {INVOICE: "invoice", CREDIT_NOTE: "credit note", DEBIT_NOTE: "debit note", PAYMENT: "payment",
               OTHER: "entry"}

# --------------------------------------------------------------------------- words

# What a statement calls itself, and what its lines say they are (folded text): English, and a pack's own words
# ("supplier_statements.<concept>": Portugal's "extrato de conta corrente", "saldo anterior", "nota de crédito").


def _or(concept: str) -> str:
    return "".join(f"|{w}" for w in pack_words(f"supplier_statements.{concept}"))


def _words(concept: str, english: str) -> LazyPattern:
    return LazyPattern(lambda: (rf"(?<![a-z])(?:{pack_alternatives(f'supplier_statements.{concept}')}|{english})"
                                r"(?![a-z])"))


_TITLE = _words("title", r"statement\s+of\s+account|account\s+statement|supplier\s+statement|customer\s+statement")
_OPENING_WORDS = _words("opening", r"opening\s+balance|balance\s+brought\s+forward|brought\s+forward|"
                                   r"previous\s+balance|balance\s+b/?f")
_CLOSING_WORDS = _words("closing", r"closing\s+balance|balance\s+due|amount\s+due|total\s+due|balance\s+outstanding|"
                                   r"outstanding\s+balance|total\s+outstanding|balance\s+carried\s+forward|"
                                   r"balance\s+c/?f")
_CREDIT_WORDS = _words("credit_note", r"credit\s+note|credit\s+memo")
_DEBIT_WORDS = _words("debit_note", r"debit\s+note")
_INVOICE_RECEIPT_WORDS = _words("invoice_receipt", r"invoice[\s-]+receipt")
_PAYMENT_WORDS = _words("payment", r"recibo|transferencia|payment|paid|transfer|remittance|receipt|"
                                   r"direct\s+debit")
_INVOICE_WORDS = _words("invoice", r"factura|invoice")
# Document-number prefixes by kind; a pack adds its own series ("supplier_statements.prefix:<kind>", plain:
# Portugal's "FT", "FR", "NC", "RG").
_PREFIXES: dict[str, tuple[str, ...]] = {
    INVOICE: ("FAC", "FA", "INV", "IN", "SI", "F"),
    CREDIT_NOTE: ("CN", "CRN", "CR"),
    DEBIT_NOTE: ("DN", "DBN"),
    PAYMENT: ("RE", "REC", "RC", "PAY", "PMT", "TRF"),
}


@cache
def _prefix_kinds() -> dict[str, str]:
    return {prefix: kind for kind, prefixes in _PREFIXES.items()
            for prefix in (*pack_words(f"supplier_statements.prefix:{kind}"), *prefixes)}


_DATE = re.compile(r"(?<![\d/.-])(?:(\d{1,2})[/.-](\d{1,2})[/.-](\d{4}|\d{2})|(\d{4})-(\d{2})-(\d{2}))(?![\d/.-])")
_CURRENCY = r"(?:€|EUR|£|GBP|\$|USD)"
_MONEY = re.compile(
    rf"(?<![\w/.,])(?P<lead>-|−|\()?\s?{_CURRENCY}?\s?"
    r"(?P<num>\d{1,3}(?:[.,   ]\d{3})+[.,]\d{2}|\d+[.,]\d{2})"
    rf"(?![.,]?\d)\s?{_CURRENCY}?(?P<trail>-|\))?(?![\w/])")
_DOC_NUMBER = re.compile(
    r"(?<![\w/])(?:(?P<prefix>[A-Z]{1,5})\s+)?(?P<body>[A-Z0-9][A-Z0-9.\-]{0,20}/\d{1,10})(?![\w/])"
    r"|(?<![\w/])(?P<code>[A-Z]{2,5}(?:-\d{1,12}|\d{2,12})(?:[-/]\d{1,10})?)(?![\w/])"
    r"|(?<![\w/])#(?P<hash>\d{3,12})(?![\w/])")
_PERIOD = LazyPattern(lambda: (
    rf"(?:period|de|from|between{_or('period_from')})\s*:?\s*(?P<a>\d{{1,2}}[/.-]\d{{1,2}}[/.-]\d{{2,4}}|"
    rf"\d{{4}}-\d{{2}}-\d{{2}})\s*(?:to|-|–|and|until{_or('period_to')})\s*"
    r"(?P<b>\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}|\d{4}-\d{2}-\d{2})"))
_AS_OF = LazyPattern(lambda: (
    rf"(?:{pack_alternatives('supplier_statements.as_of')}|statement\s+date|open\s+items\s+at|"
    r"as\s+(?:of|at)|data)\s*:?\s*"
    r"(?P<d>\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}|\d{4}-\d{2}-\d{2})"))


def _to_date(text: str) -> date | None:
    m = _DATE.search(text)
    if m is None:
        return None
    try:
        if m.group(4):
            return date(int(m.group(4)), int(m.group(5)), int(m.group(6)))
        year = int(m.group(3))
        year = year + 2000 if year < 100 else year
        return date(year, int(m.group(2)), int(m.group(1)))
    except ValueError:
        return None


def _to_money(num: str, *, negative: bool = False) -> Decimal | None:
    """'1.234,56' / '1,234.56' / '1 234,56' / '246.00' -> Decimal. Two decimals required."""
    digits = num.replace(" ", " ").replace(" ", " ").strip()
    m = re.fullmatch(r"(\d[\d., ]*?)[.,](\d{2})", digits)
    if m is None:
        return None
    whole = re.sub(r"[., ]", "", m.group(1))
    try:
        value = Decimal(f"{whole}.{m.group(2)}")
    except InvalidOperation:
        return None
    return -value if negative else value


def _cell_money(cell: str) -> Decimal | None:
    """A CSV cell: '1.234,56', '1234.56', '-12', '12,5', '(23.00)', '23,00 €'. Empty -> None."""
    text = re.sub(_CURRENCY, "", (cell or "").replace(" ", " ")).strip()
    if not text:
        return None
    negative = text.startswith(("-", "−", "(")) or text.endswith(("-", ")"))
    text = text.strip("-−() ").replace(" ", "")
    if not text or not re.fullmatch(r"[\d.,]+", text):
        return None
    last = max(text.rfind("."), text.rfind(","))
    if last >= 0 and len(text) - last - 1 in (1, 2):
        whole, cents = re.sub(r"[.,]", "", text[:last]) or "0", text[last + 1:]
        value = Decimal(f"{whole}.{cents}")
    else:
        value = Decimal(re.sub(r"[.,]", "", text))
    value = value.quantize(_CENT)
    return -value if negative else value


def same_number(a: str | None, b: str | None) -> bool:
    """The same document number, whatever the spacing and separators ('FT A/101' == 'FTA/101' == 'FT-A-101')."""
    if not a or not b:
        return False
    norm = lambda s: re.sub(r"[\s/_.\-]+", "", s).upper()  # noqa: E731
    return norm(a) == norm(b)


def _kind(number: str | None, description: str) -> str:
    text = fold(f"{description} {number or ''}")
    if not number and _OPENING_WORDS.search(text):
        return OPENING
    if not number and _CLOSING_WORDS.search(text):
        return CLOSING
    if _CREDIT_WORDS.search(text):
        return CREDIT_NOTE
    if _DEBIT_WORDS.search(text):
        return DEBIT_NOTE
    if _INVOICE_RECEIPT_WORDS.search(text):
        return INVOICE
    prefix = None
    if number:
        m = re.match(r"([A-Za-z]{1,5})", number)
        prefix = m.group(1).upper() if m else None
    if _PAYMENT_WORDS.search(text):
        return PAYMENT
    if _INVOICE_WORDS.search(text):
        return INVOICE
    if prefix in _prefix_kinds():
        return _prefix_kinds()[prefix]
    return OTHER


def _default_side(kind: str) -> int:
    """+1 when a line of this kind raises what the business owes, -1 when it lowers it."""
    return -1 if kind in (CREDIT_NOTE, PAYMENT) else 1


# --------------------------------------------------------------------------- the statement


@dataclass(frozen=True)
class StatementLine:
    """One line of a statement. ``debit`` raises what the business owes (an invoice), ``credit`` lowers it."""

    row: int  # 1-based position among the statement's lines
    on: date | None
    number: str | None  # the document number as printed
    description: str
    debit: Decimal
    credit: Decimal
    balance: Decimal | None  # the running balance printed on the line (what is owed after it)
    kind: str  # invoice | credit_note | debit_note | payment | other

    @property
    def amount(self) -> Decimal:
        return self.debit if self.debit else self.credit

    @property
    def effect(self) -> Decimal:
        return self.debit - self.credit

    @property
    def word(self) -> str:
        return _KIND_WORDS.get(self.kind, "entry")

    @property
    def is_document(self) -> bool:
        return self.kind in DOCUMENT_KINDS

    def phrase(self, today: date | None = None, currency: str = "EUR") -> str:
        """'invoice FT A/103 of 15 September (€80.00)'."""
        number = f" {self.number}" if self.number else ""
        when = f" of {day_month(self.on, today)}" if self.on else ""
        return f"{self.word}{number}{when} ({format_money(self.amount, currency)})"


@dataclass(frozen=True)
class SupplierStatement:
    lines: tuple[StatementLine, ...]
    opening: Decimal | None
    closing: Decimal | None  # printed, else the last running balance, else opening + lines
    closing_printed: bool
    period_start: date | None
    period_end: date | None
    currency: str = "EUR"
    layout: str = "text"  # "text" | "csv"
    problems: tuple[str, ...] = ()  # why it does not add up, in plain words

    @property
    def adds_up(self) -> bool:
        return not self.problems

    @property
    def start(self) -> date | None:
        days = [x.on for x in self.lines if x.on]
        return self.period_start or (min(days) if days else None)

    @property
    def end(self) -> date | None:
        days = [x.on for x in self.lines if x.on]
        return self.period_end or (max(days) if days else None)

    @property
    def movements_style(self) -> bool:
        """Lists every movement (payments too), not only what is still open."""
        return self.opening is not None or any(x.kind == PAYMENT for x in self.lines)

    def as_dict(self) -> dict[str, object]:
        return {
            "lines": [{"row": x.row, "date": x.on.isoformat() if x.on else None, "number": x.number,
                       "description": x.description, "kind": x.kind, "debit": _num(x.debit),
                       "credit": _num(x.credit), "balance": _num(x.balance)} for x in self.lines],
            "opening": _num(self.opening), "closing": _num(self.closing), "currency": self.currency,
            "from": self.start.isoformat() if self.start else None,
            "to": self.end.isoformat() if self.end else None, "addsUp": self.adds_up, "problems": list(self.problems),
        }


def _num(value: Decimal | None) -> int | float | None:
    if value is None:
        return None
    value = value.quantize(_CENT)
    return int(value) if value == value.to_integral_value() else float(value)


@dataclass
class _Raw:
    row: int
    on: date | None
    number: str | None
    description: str
    amounts: list[Decimal]
    kind: str
    columns: tuple[Decimal | None, Decimal | None, Decimal | None] | None = None  # CSV: debit, credit, balance
    signed: Decimal | None = None  # CSV: one signed amount column


def looks_like_statement(text: str) -> bool:
    """The text names itself a supplier's account statement."""
    return bool(_TITLE.search(fold(text or "")))


def read_statement(text: str, *, titled: bool = False, csv_only: bool = False) -> SupplierStatement | None:
    """The statement's lines, or None when ``text`` is not a readable statement.

    A CSV export is recognised by its header (a date, a document number and debit/credit/balance or amount
    columns). A text layout is read only when the text names itself a statement (or ``titled``, when the
    caller already knows it is one), so an invoice's lines are never taken for a statement. ``csv_only``
    reads CSV exports only.
    """
    if not text or not text.strip():
        return None
    raws = _read_csv(text)
    layout = "csv"
    if raws is None:
        if csv_only or not (titled or looks_like_statement(text)):
            return None
        raws = _read_text(text)
        layout = "text"
    if not raws:
        return None
    currency = _currency_of(text)
    period_start, period_end = _period(text)
    return _build(raws, layout=layout, currency=currency, period_start=period_start, period_end=period_end)


def _currency_of(text: str) -> str:
    found = {code for mark, code in (("€", "EUR"), ("EUR", "EUR"), ("£", "GBP"), ("GBP", "GBP"), ("USD", "USD"),
                                     ("US$", "USD")) if mark in text}
    return found.pop() if len(found) == 1 else "EUR"


def _period(text: str) -> tuple[date | None, date | None]:
    folded = fold(text)
    m = _PERIOD.search(folded)
    if m is not None:
        a, b = _to_date(m.group("a")), _to_date(m.group("b"))
        if a and b and a <= b:
            return a, b
    m = _AS_OF.search(folded)
    if m is not None:
        return None, _to_date(m.group("d"))
    return None, None


def _split_amounts(line: str) -> tuple[str, list[Decimal]]:
    """The line without its trailing amounts, and those amounts in order."""
    found = list(_MONEY.finditer(line))
    tail: list[re.Match[str]] = []
    end = len(line.rstrip())
    for m in reversed(found):
        between = line[m.end():end]
        if between.strip(" \t€$£:;|") and not re.fullmatch(r"\s*(?:EUR|GBP|USD)?\s*", between):
            break
        tail.insert(0, m)
        end = m.start()
    amounts = []
    for m in tail:
        negative = bool(m.group("lead")) or bool(m.group("trail"))
        value = _to_money(m.group("num"), negative=negative)
        if value is not None:
            amounts.append(value)
    head = line[:tail[0].start()] if tail else line
    return head, amounts


def _number_and_description(head: str) -> tuple[str | None, str]:
    m = _DOC_NUMBER.search(head)
    if m is None:
        return None, " ".join(head.split()).strip(" -:|;\t")
    if m.group("body"):
        number = f"{m.group('prefix')} {m.group('body')}" if m.group("prefix") else m.group("body")
    else:
        number = m.group("code") or f"#{m.group('hash')}"
    description = (head[:m.start()] + " " + head[m.end():]).strip(" -:|;\t")
    return " ".join(number.split()), " ".join(description.split())


def _read_text(text: str) -> list[_Raw]:
    out: list[_Raw] = []
    for raw_line in text.splitlines():
        line = raw_line.replace("\t", "  ").strip()
        if not line:
            continue
        head, amounts = _split_amounts(line)
        if not amounts:
            continue
        on = _to_date(head)
        if on is not None:
            m = _DATE.search(head)
            assert m is not None
            head = head[:m.start()] + " " + head[m.end():]
        number, description = _number_and_description(head)
        kind = _kind(number, description)
        if on is None and kind not in (OPENING, CLOSING):
            continue  # a total, a subtotal or a header: only dated lines are movements
        if kind in (OPENING, CLOSING) and len(amounts) > 1:
            amounts = amounts[-1:]
        out.append(_Raw(row=len(out) + 1, on=on, number=number, description=description, amounts=amounts,
                        kind=kind))
    return out


# A CSV export's headings by column (folded): English and Spanish, and a pack's ("supplier_statements.column:<name>").
_CSV_COLUMNS: dict[str, tuple[str, ...]] = {
    "date": ("date", "document date", "posting date"),
    "number": ("doc", "numero", "n doc", "document", "document no", "document number", "doc no", "reference", "ref",
               "referencia", "invoice", "invoice no", "invoice number"),
    "description": ("description", "details", "type", "document type", "narrative"),
    "debit": ("debito", "debit", "debits", "charges", "charge"),
    "credit": ("credito", "credit", "credits", "payments"),
    "balance": ("saldo", "balance", "running balance"),
    "amount": ("valor", "amount", "importe", "total"),
}


@cache
def _csv_columns() -> dict[str, frozenset[str]]:
    return {name: frozenset((*words, *pack_words(f"supplier_statements.column:{name}")))
            for name, words in _CSV_COLUMNS.items()}


def _header_key(cell: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", fold(cell)).replace(" o ", " ").split(" (")[0].strip()


def _read_csv(text: str) -> list[_Raw] | None:
    lines = text.splitlines()
    if len(lines) < 2:
        return None
    for i, line in enumerate(lines[:15]):
        delimiter = max((";", ",", "\t", "|"), key=line.count)
        if line.count(delimiter) < 2:
            continue
        header = next(csv.reader([line], delimiter=delimiter))
        columns: dict[str, int] = {}
        for index, cell in enumerate(header):
            key = " ".join(_header_key(cell).split())
            for name, words in _csv_columns().items():
                if name not in columns and key in words:
                    columns[name] = index
                    break
        money = {"debit", "credit"} <= set(columns) or "amount" in columns
        if not ("date" in columns and "number" in columns and money):
            continue
        return _csv_rows(lines[i + 1:], delimiter, columns)
    return None


def _csv_rows(lines: Sequence[str], delimiter: str, columns: dict[str, int]) -> list[_Raw]:
    out: list[_Raw] = []
    for cells in csv.reader(lines, delimiter=delimiter):
        if not any(c.strip() for c in cells):
            continue

        def cell(name: str, cells: list[str] = cells) -> str:
            index = columns.get(name)
            return cells[index].strip() if index is not None and index < len(cells) else ""

        on = _to_date(cell("date"))
        number = " ".join(cell("number").split()) or None
        description = " ".join(cell("description").split())
        debit, credit, balance = _cell_money(cell("debit")), _cell_money(cell("credit")), _cell_money(cell("balance"))
        signed = _cell_money(cell("amount")) if "amount" in columns else None
        kind = _kind(number, description or " ".join(c for c in cells if not _cell_money(c)))
        if on is None and kind not in (OPENING, CLOSING):
            continue
        if debit is None and credit is None and signed is None and balance is None:
            continue
        if kind in (OPENING, CLOSING):
            value = balance if balance is not None else signed if signed is not None else (debit or _ZERO) - (
                credit or _ZERO)
            out.append(_Raw(row=len(out) + 1, on=on, number=None, description=description, amounts=[value],
                            kind=kind))
            continue
        out.append(_Raw(row=len(out) + 1, on=on, number=number, description=description, amounts=[], kind=kind,
                        columns=(debit, credit, balance), signed=signed))
    return out


def _build(raws: Sequence[_Raw], *, layout: str, currency: str, period_start: date | None,
           period_end: date | None) -> SupplierStatement | None:
    """Sides, balances and the checks that the statement adds up."""
    opening_raw = next((r.amounts[-1] for r in raws if r.kind == OPENING and r.amounts), None)
    closing_raw = next((r.amounts[-1] for r in reversed(raws) if r.kind == CLOSING and r.amounts), None)
    moves = [r for r in raws if r.kind not in (OPENING, CLOSING)]
    if not moves:
        return None
    # First pass: each line's movement and running balance as printed (the statement's own orientation).
    prints: list[tuple[_Raw, Decimal, Decimal | None, bool]] = []  # (raw, effect as printed, balance, from balance)
    previous = opening_raw
    for r in moves:
        effect: Decimal
        balance: Decimal | None = None
        printed = False
        if r.columns is not None:
            debit, credit, balance = r.columns
            if debit is not None or credit is not None:
                effect = abs(debit or _ZERO) - abs(credit or _ZERO)
                if (debit is not None and debit < 0) or (credit is not None and credit < 0):
                    effect = (debit or _ZERO) - (credit or _ZERO)
                printed = True
            elif r.signed is not None:
                effect = r.signed
                printed = True
            else:
                effect = _ZERO
        elif len(r.amounts) >= 3:
            debit, credit, balance = r.amounts[-3:]
            effect = debit - credit
            printed = True
        elif len(r.amounts) == 2:
            move, balance = r.amounts
            if previous is None:
                # The first balance, with no opening balance printed: the movement is on the side that leaves
                # the smaller balance before it (on a tie, the side its kind is on).
                up, down = abs(balance - abs(move)), abs(balance + abs(move))
                side = 1 if up < down else -1 if down < up else _default_side(r.kind) * (-1 if move < 0 else 1)
                effect = abs(move) * side
                printed = True
            elif balance - previous != 0 and abs(balance - previous) == abs(move):
                effect = balance - previous
                printed = True
            else:
                effect = abs(move) * _default_side(r.kind) * (-1 if move < 0 else 1)
        else:
            move = r.amounts[0]
            effect = abs(move) * _default_side(r.kind) * (-1 if move < 0 else 1)
        if balance is not None and previous is None and printed:
            previous = balance - effect
            if opening_raw is None and r is moves[0]:
                opening_raw = previous
        prints.append((r, effect, balance, printed))
        if balance is not None:
            previous = balance
        elif previous is not None:
            previous = previous + effect
    # Orientation: invoices raise what is owed. A statement printed the other way round is turned over.
    votes = sum((1 if e > 0 else -1) for r, e, _, printed in prints if printed and r.kind == INVOICE and e != 0)
    if not votes:
        votes = sum((1 if e < 0 else -1) for r, e, _, printed in prints if printed and r.kind == PAYMENT and e != 0)
    flip = -1 if votes < 0 else 1
    lines: list[StatementLine] = []
    problems: list[str] = []
    running = opening_raw
    for index, (r, effect, balance, printed) in enumerate(prints, start=1):
        if balance is not None and running is not None and running + effect != balance:
            problems.append(f"The balance on line {index} does not follow from the line before.")
        running = balance if balance is not None else (running + effect if running is not None else None)
        owed = effect * flip if printed else effect
        lines.append(StatementLine(
            row=index, on=r.on, number=r.number, description=r.description or r.number or "",
            debit=owed if owed > 0 else _ZERO, credit=-owed if owed < 0 else _ZERO,
            balance=balance * flip + 0 if balance is not None else None, kind=r.kind))
    opening = opening_raw * flip + 0 if opening_raw is not None else None
    computed = (opening or _ZERO) + sum((x.effect for x in lines), _ZERO)
    last_balance = next((x.balance for x in reversed(lines) if x.balance is not None), None)
    closing = closing_raw * flip + 0 if closing_raw is not None else last_balance
    if closing is not None and closing != computed and not problems:
        problems.append(f"Its closing balance of {format_money(closing, currency)} is not the lines added up "
                        f"({format_money(computed, currency)}).")
    return SupplierStatement(
        lines=tuple(lines), opening=opening, closing=closing if closing is not None else computed,
        closing_printed=closing_raw is not None or last_balance is not None, period_start=period_start,
        period_end=period_end, currency=currency, layout=layout, problems=tuple(problems))


# --------------------------------------------------------------------------- checking against our records


@dataclass(frozen=True)
class OurDocument:
    """One of the business's own documents from this supplier, as the check needs it."""

    id: str
    kind: str  # invoice | credit_note | debit_note
    number: str | None
    amount: Decimal  # absolute
    on: date | None
    paid: bool  # matched to a payment, paid in cash, or an invoice-receipt
    label: str  # 'Invoice FT A/101 · €246.00'
    evidence_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class OurPayment:
    """One of the business's bank payments to this supplier."""

    id: str
    amount: Decimal  # absolute
    on: date
    evidence_id: str
    document_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class RowCheck:
    line: StatementLine
    status: str  # matched | missing | differs | not_found | info
    document_id: str | None = None
    transaction_id: str | None = None
    our_amount: Decimal | None = None
    by_amount: bool = False  # found by amount and date, not by number
    evidence_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class StatementCheck:
    rows: tuple[RowCheck, ...]
    not_on_statement: tuple[OurDocument, ...]
    our_open: Decimal
    closing: Decimal
    currency: str
    adds_up: bool
    problems: tuple[str, ...] = ()
    explained: str = ""  # why the balances differ, when the lines show it
    period: tuple[date | None, date | None] = (None, None)
    supplier: str = "the supplier"

    @property
    def matched(self) -> tuple[RowCheck, ...]:
        return tuple(r for r in self.rows if r.status == "matched")

    @property
    def missing(self) -> tuple[RowCheck, ...]:
        return tuple(r for r in self.rows if r.status == "missing")

    @property
    def differences(self) -> tuple[RowCheck, ...]:
        return tuple(r for r in self.rows if r.status == "differs")

    @property
    def payments_not_found(self) -> tuple[RowCheck, ...]:
        return tuple(r for r in self.rows if r.status == "not_found")

    @property
    def difference(self) -> Decimal:
        return self.closing - self.our_open

    @property
    def balance_agrees(self) -> bool:
        return self.difference == 0

    @property
    def complete(self) -> bool:
        return (self.adds_up and self.balance_agrees and not self.missing and not self.differences
                and not self.payments_not_found)

    def key(self) -> tuple[object, ...]:
        """What the check found, for noticing a change (not the wording)."""
        return (tuple((r.line.row, r.status, r.document_id, r.transaction_id) for r in self.rows),
                tuple(d.id for d in self.not_on_statement), self.our_open, self.closing)


def _document_kind(kind: str) -> str:
    return CREDIT_NOTE if kind == CREDIT_NOTE else DEBIT_NOTE if kind == DEBIT_NOTE else INVOICE


def check_statement(statement: SupplierStatement, documents: Sequence[OurDocument], payments: Sequence[OurPayment],
                    *, supplier: str = "the supplier") -> StatementCheck:
    """Each line against the business's own documents and payments from this supplier (module docstring)."""
    used_docs: set[str] = set()
    used_payments: set[str] = set()
    results: dict[int, RowCheck] = {}
    doc_lines = [x for x in statement.lines if x.is_document]
    # 1. by number: the same document, whatever its amount (a different amount is a difference, never a match)
    for x in doc_lines:
        if not x.number:
            continue
        wanted = _document_kind(x.kind)
        found = [d for d in documents if d.id not in used_docs and same_number(d.number, x.number)
                 and (d.kind == wanted or d.kind == INVOICE and wanted == DEBIT_NOTE)]
        if not found:
            continue
        doc = found[0]
        used_docs.add(doc.id)
        status = "matched" if doc.amount == x.amount else "differs"
        results[x.row] = RowCheck(line=x, status=status, document_id=doc.id, our_amount=doc.amount,
                                  evidence_ids=doc.evidence_ids)
    # 2. by amount, a few days apart, when exactly one document fits
    for x in doc_lines:
        if x.row in results:
            continue
        wanted = _document_kind(x.kind)
        fits = [d for d in documents if d.id not in used_docs and d.kind == wanted and d.amount == x.amount
                and (x.on is None or d.on is None or abs((d.on - x.on).days) <= AMOUNT_MATCH_DAYS)
                and not (x.number and d.number)]  # two different numbers are two different documents
        if len(fits) == 1:
            used_docs.add(fits[0].id)
            results[x.row] = RowCheck(line=x, status="matched", document_id=fits[0].id, our_amount=fits[0].amount,
                                      by_amount=True, evidence_ids=fits[0].evidence_ids)
        else:
            results[x.row] = RowCheck(line=x, status="missing")
    # 3. payments: the same amount, a few days apart, one bank payment each
    for x in statement.lines:
        if x.kind != PAYMENT:
            continue
        fits = sorted((p for p in payments if p.id not in used_payments and p.amount == x.amount
                       and (x.on is None or abs((p.on - x.on).days) <= PAYMENT_MATCH_DAYS)),
                      key=lambda p: (abs((p.on - x.on).days) if x.on else 0, p.on, p.id))
        if fits:
            used_payments.add(fits[0].id)
            results[x.row] = RowCheck(line=x, status="matched", transaction_id=fits[0].id, our_amount=fits[0].amount,
                                      evidence_ids=(fits[0].evidence_id,))
        else:
            results[x.row] = RowCheck(line=x, status="not_found")
    rows = tuple(results.get(x.row) or RowCheck(line=x, status="info") for x in statement.lines)
    # Our own documents from the statement's period that it does not list (only what is still open, for a
    # statement of open items).
    start, end = statement.start, statement.end
    not_listed = tuple(
        d for d in sorted(documents, key=lambda d: (d.on or date.min, d.id))
        if d.id not in used_docs and d.on is not None and (start is None or d.on >= start)
        and (end is None or d.on <= end) and (statement.movements_style or not d.paid))
    # What the business still owes by its own records: documents not paid yet, less credit notes not used yet.
    our_open = _ZERO
    for d in documents:
        if d.paid or (end is not None and d.on is not None and d.on > end):
            continue
        our_open += -d.amount if d.kind == CREDIT_NOTE else d.amount
    closing = statement.closing if statement.closing is not None else _ZERO
    check = StatementCheck(rows=rows, not_on_statement=not_listed, our_open=our_open, closing=closing,
                           currency=statement.currency, adds_up=statement.adds_up, problems=statement.problems,
                           period=(start, end), supplier=supplier)
    return _explained(check)


def _explained(check: StatementCheck) -> StatementCheck:
    gap = check.difference
    if gap == 0:
        return check
    missing = sum((r.line.effect for r in check.missing), _ZERO)
    differs = sum(((r.line.amount - (r.our_amount or _ZERO)) * (-1 if r.line.kind == CREDIT_NOTE else 1)
                   for r in check.differences), _ZERO)
    money = lambda v: format_money(abs(v), check.currency)  # noqa: E731
    if check.missing and gap == missing:
        what = join_and([r.line.phrase(currency=check.currency) for r in check.missing[:3]])
        text = f"The difference is what I don't have: {what}."
    elif check.differences and gap == differs:
        text = f"The difference of {money(gap)} is the amount difference on the statement."
    elif check.missing and check.differences and gap == missing + differs:
        text = f"The difference of {money(gap)} is the documents I don't have and the amount difference."
    else:
        text = ""
    return replace(check, explained=text)


# --------------------------------------------------------------------------- plain words


def summary(check: StatementCheck, today: date | None = None) -> str:
    """One or two plain sentences: does the statement match, and what is different (§36, §69)."""
    who = check.supplier
    money = lambda v: format_money(v, check.currency)  # noqa: E731
    lines = len(check.rows)
    if not check.adds_up:
        return (f"{who}'s statement does not add up, so I can't rely on it. {check.problems[0]} "
                "I kept it for your accountant.")
    if check.complete:
        return (f"{who}'s statement matches your records: {count_phrase(lines, 'line')}, all found, and the "
                f"balance of {money(check.closing)} is what you have not paid yet.")
    parts: list[str] = []
    if check.missing:
        docs = [r.line.phrase(today, check.currency) for r in check.missing]
        head = (f"It lists {count_phrase(len(docs), 'document')} I don't have: " if len(docs) > 1
                else "It lists a document I don't have: ")
        parts.append(head + join_and(docs[:4]) + ("" if len(docs) <= 4 else f" and {len(docs) - 4} more") + ".")
    for r in check.differences[:3]:
        parts.append(f"It shows {r.line.word} {r.line.number} as {money(r.line.amount)}, but the {r.line.word} "
                     f"says {money(r.our_amount or _ZERO)}.")
    if check.payments_not_found:
        pays = [r.line.phrase(today, check.currency) for r in check.payments_not_found]
        parts.append(f"I can't find {join_and(pays[:3])} in your bank.")
    if not check.balance_agrees:
        parts.append(f"It says you owe {money(check.closing)}; the invoices you have not paid yet come to "
                     f"{money(check.our_open)}.")
        if check.explained:
            parts.append(check.explained)
    found = sum(1 for r in check.rows if r.status == "matched")
    if lines == 1:
        head = f"{who}'s statement: its one line {'matches' if found else 'does not match'} your records."
    elif found == lines:
        head = f"{who}'s statement: all {lines} lines match your records."
    elif not found:
        head = f"{who}'s statement: none of its {lines} lines match your records yet."
    else:
        head = f"{who}'s statement: {found} of its {lines} lines match your records."
    return " ".join([head, *parts])


def not_listed_line(check: StatementCheck) -> str:
    """For the accountant: the business's own documents from the period that the statement does not list."""
    docs = check.not_on_statement
    if not docs:
        return ""
    names = join_and([d.label for d in docs[:4]]) + ("" if len(docs) <= 4 else f" and {len(docs) - 4} more")
    return f"Not on {check.supplier}'s statement, although it covers their dates: {names}."


def when_words(on: date | None, today: date | None = None) -> str:
    return day_month(on, today) if on else "an unknown date"

