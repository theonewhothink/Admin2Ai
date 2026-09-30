"""Recurring customer payments: memberships, tuition and monthly fees (checklist X12; cases 10, 25, 45).

Receipts lists
    Schools, gyms, clubs and clinics issue their receipts in their own software and export them as a list:
    one row per receipt, with its number, date, the member (student, patient, athlete) and who paid for them
    when someone else did (a parent), the period it is for ("Setembro 2026", "09/2026", "1.º período", "Term
    1"), the amount and how it was paid. :func:`read_receipts_list` reads such a CSV (Portuguese or English
    headings) into :class:`ReceiptRow` objects. A list is recognised by a receipt number, a person and an
    amount column; a till report or a payout report never is.

Periods
    :func:`period_of` turns what a row says into a key ("2026-09", or "term:1:2026" for a school term) and the
    words the owner reads ("September 2026", "the 1st term"). A payment's period is the month its bank line
    names ("MENSALIDADE SETEMBRO"), else the month it was paid in.

Returned direct debits
    :func:`returned_debit` recognises money going back to a payer because their direct debit was returned
    ("DEVOLUÇÃO DD", "COBRANÇA DEVOLVIDA", "RETURNED DIRECT DEBIT", "ESTORNO"): the membership payment it
    returns is not income, and its period is unpaid again.

Recurring payers
    :func:`recurring_payers` learns, like :mod:`backoffice.learning.recurrence`, which customers pay the same
    amount on a rhythm: every month, or once a school term (gaps of two months up to the summer break). A thin
    history is kept as untrusted (AMBER): it describes, it never closes anything.

Pure Python, no I/O. The words and layouts are conventions from Portuguese and English schools, gyms and
banks, not verified against every software's export (verified_as_of: never).
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from itertools import pairwise

from backoffice._reading import (
    MONTH_WORDS,
    column,
    csv_rows,
    first_date,
    money,
    month_named,
    tax_ids,
)
from backoffice.domain.models import Transaction
from backoffice.learning.keys import counterparty_key, fold
from backoffice.learning.plain import format_money, ordinal
from backoffice.learning.recurrence import (
    Basis,
    Cadence,
    Direction,
    Occurrence,
    learn_series,
)
from backoffice.reconciliation._text import fold as upper_fold

__all__ = ["PayerSeries", "ReceiptRow", "period_of", "read_receipts_list", "recurring_payers", "returned_debit"]

TERM_GAP_DAYS = 160  # the longest gap between two terms' payments: across the summer break
MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December")

_COLUMNS = {
    "number": ("recibo", "n recibo", "no recibo", "n o recibo", "numero recibo", "numero do recibo", "receipt",
               "receipt no", "receipt number", "n documento", "documento", "doc", "fatura recibo", "numero"),
    "date": ("data", "date", "data emissao", "data de emissao", "issue date", "data pagamento", "data do pagamento",
             "paid on", "payment date", "issued"),
    "member": ("aluno", "aluna", "socio", "socia", "membro", "utente", "atleta", "cliente", "nome", "student",
               "member", "customer", "client", "name", "patient"),
    "payer": ("encarregado de educacao", "encarregado", "pagador", "pago por", "payer", "paid by", "parent",
              "titular", "responsavel"),
    "tax_id": ("nif", "contribuinte", "nif cliente", "nif do cliente", "tax id", "vat", "vat number"),
    "period": ("referente a", "periodo", "mes", "mes de referencia", "mensalidade", "period", "month", "term",
               "for", "referencia", "descricao", "description"),
    "amount": ("valor", "montante", "total", "amount", "valor pago", "paid", "amount paid"),
    "method": ("forma de pagamento", "meio de pagamento", "pagamento", "metodo", "metodo de pagamento",
               "payment method", "method", "paid with"),
}
_TILL_COLUMNS = ("numerario", "multibanco", "cash", "card", "cartao")
_TERM = re.compile(r"(?<![a-z0-9])(?:(\d)\s*\.?\s*[ºo°]?\s*(?:periodo|trimestre|semestre|term)|(?:term|periodo)\s*"
                   r"(\d)|(\d)(?:st|nd|rd|th)\s+term)(?![a-z0-9])")
_CASH = re.compile(r"(?<![a-z])(?:numerario|dinheiro|cash|efectivo)(?![a-z])")

# A payment going back to its payer because the direct debit was returned (bank words, folded).
_RETURNED = re.compile(
    r"(?<![A-Z])(?:DEVOLUCAO|DEVOL\.?|DEV\.?\s+(?:DD|DEB\w*|COBR\w*|SEPA)|DD\s+DEVOLVIDO|DEBITO\s+(?:DIRECTO\s+|"
    r"DIRETO\s+)?DEVOLVIDO|COBRANCA\s+DEVOLVIDA|RETURNED\s+(?:DIRECT\s+)?DEBIT|DIRECT\s+DEBIT\s+RETURN(?:ED)?|"
    r"DD\s+RETURN(?:ED)?|UNPAID\s+(?:DIRECT\s+)?DEBIT|ESTORNO|REVERSAL\s+(?:OF\s+)?(?:DD|DIRECT\s+DEBIT)|"
    r"RECIBO\s+DEVUELTO|IMPAGADO)(?![A-Z])")


@dataclass(frozen=True)
class ReceiptRow:
    """One receipt of a receipts list."""

    number: str
    issued_on: date
    member: str
    amount: Decimal
    period: str  # "2026-09" | "term:1:2026"
    period_label: str  # "September 2026" | "the 1st term"
    payer: str | None = None
    tax_id: str | None = None
    cash: bool = False  # paid in cash at the desk: no bank line will come
    location: str = ""


def period_of(text: str, on: date) -> tuple[str, str]:
    """(key, words) of the period ``text`` names, else the month of ``on``."""
    folded = fold(text or "")
    term = _TERM.search(folded)
    if term:
        n = int(next(g for g in term.groups() if g))
        school_year = on.year if on.month >= 8 else on.year - 1
        return f"term:{n}:{school_year}", f"the {ordinal(n)} term"
    named = month_named(folded, default_year=on.year) if folded else None
    year, month = named if named else (on.year, on.month)
    return f"{year:04d}-{month:02d}", f"{MONTHS[month - 1]} {year}"


def payment_period(tx: Transaction) -> tuple[str, str]:
    """The period a payment is for: the month its bank line names, else the month it was paid in."""
    text = fold(f"{tx.description} {tx.reference or ''}")
    words = [w for w in re.findall(r"[a-z]+", text) if w in MONTH_WORDS and len(w) >= 3]
    named = month_named(text) or (month_named(" ".join(words), default_year=tx.booked_on.year) if words else None)
    if named is None and words:
        month = MONTH_WORDS[words[0]]
        named = (tx.booked_on.year - (1 if month > tx.booked_on.month + 6 else 0), month)
    year, month = named if named else (tx.booked_on.year, tx.booked_on.month)
    return f"{year:04d}-{month:02d}", f"{MONTHS[month - 1]} {year}"


def read_receipts_list(text: str) -> list[ReceiptRow]:
    """Every receipt of a receipts list (a CSV export), else ``[]``."""
    table = csv_rows(text or "")
    if table is None:
        return []
    header, rows = table
    if sum(1 for h in header if h in _TILL_COLUMNS) >= 2:
        return []  # a till report's columns: not a receipts list
    col = {role: column(header, aliases) for role, aliases in _COLUMNS.items()}
    if col["number"] is None or col["amount"] is None or (col["member"] is None and col["payer"] is None):
        return []
    if col["date"] is None and col["period"] is None:
        return []

    def cell(row: list[str], role: str) -> str:
        i = col[role]
        return row[i].strip() if i is not None and i < len(row) else ""

    out: list[ReceiptRow] = []
    for n, row in enumerate(rows, start=2):
        number, amount = cell(row, "number"), money(cell(row, "amount"))
        if not number or amount is None or re.match(r"^\s*total", fold(number)):
            continue
        issued = first_date(cell(row, "date"))
        period_text = cell(row, "period")
        if issued is None:
            named = month_named(period_text)
            if named is None:
                continue
            issued = date(named[0], named[1], 1)
        key, label = period_of(period_text, issued)
        member = cell(row, "member") or cell(row, "payer")
        ids = tax_ids(cell(row, "tax_id"))
        out.append(ReceiptRow(number=number, issued_on=issued, member=member, amount=amount, period=key,
                              period_label=label, payer=cell(row, "payer") or None, tax_id=ids[0] if ids else None,
                              cash=bool(_CASH.search(fold(cell(row, "method")))), location=f"csv:row {n}"))
    return out


def returned_debit(tx: Transaction) -> bool:
    """Money out that says a payer's direct debit (or payment) came back."""
    if tx.amount >= 0:
        return False
    return bool(_RETURNED.search(upper_fold(f"{tx.counterparty} {tx.description} {tx.reference or ''}")))


# --------------------------------------------------------------------------- who pays on a rhythm


@dataclass(frozen=True)
class PayerSeries:
    """A customer who pays the same amount on a rhythm."""

    key: str
    name: str
    amount: Decimal
    rhythm: str  # "every month" | "every term" | "every week" | ...
    payments: tuple[str, ...]  # payment ids, oldest first
    first_seen: date
    last_seen: date
    trusted: bool

    def phrase(self, currency: str = "EUR") -> str:
        """'pays €45.00 every month'"""
        return f"pays {format_money(self.amount, currency)} {self.rhythm}"


def _key(name: str) -> str | None:
    return counterparty_key(name)


def recurring_payers(payments: Iterable[Transaction], *, names: dict[str, str] | None = None) -> list[PayerSeries]:
    """Customers who paid the same amount on a rhythm (money in only), by payer."""
    groups: dict[str, list[Transaction]] = defaultdict(list)
    for tx in payments:
        if tx.amount <= 0:
            continue
        key = _key(tx.counterparty)
        if key:
            groups[key].append(tx)
    out: list[PayerSeries] = []
    for key, txs in sorted(groups.items()):
        txs.sort(key=lambda t: (t.booked_on, t.id))
        amounts = {t.amount for t in txs}
        if len(txs) < 2 or len(amounts) != 1:
            continue
        amount = txs[0].amount
        series = learn_series(key, [Occurrence(on=t.booked_on, amount=t.amount, currency=t.currency,
                                               label=t.counterparty, ref=t.id) for t in txs],
                              basis=Basis.PAYMENTS, direction=Direction.IN)
        rhythm, trusted = None, False
        if series is not None and series.cadence in (Cadence.MONTHLY, Cadence.WEEKLY):
            rhythm, trusted = series.spec.phrase, series.trusted
        elif all(60 <= (b.booked_on - a.booked_on).days <= TERM_GAP_DAYS for a, b in pairwise(txs)):
            rhythm, trusted = "every term", len(txs) >= 3
        elif series is not None:
            rhythm, trusted = series.spec.phrase, series.trusted
        if rhythm is None:
            continue
        name = (names or {}).get(key) or _display(txs[-1].counterparty)
        out.append(PayerSeries(key=key, name=name, amount=amount, rhythm=rhythm, payments=tuple(t.id for t in txs),
                               first_seen=txs[0].booked_on, last_seen=txs[-1].booked_on, trusted=trusted))
    return out


def _display(raw: str) -> str:
    name = " ".join((raw or "").split())
    return " ".join(w.capitalize() for w in name.split()) if name.isupper() else name


def same_payer(a: str | None, b: str | None) -> bool:
    """Two ways of writing one person's name: the same words, or all the words (two at least) of the shorter
    one in the longer one. Never a single shared word."""
    wa, wb = re.findall(r"[a-z0-9]+", fold(a or "")), re.findall(r"[a-z0-9]+", fold(b or ""))
    if not wa or not wb:
        return False
    if wa == wb:
        return True
    short, long_ = (wa, wb) if len(wa) <= len(wb) else (wb, wa)
    return len(short) >= 2 and set(short) <= set(long_)


def names_in(text: str, name: str) -> bool:
    """``name`` (two words at least) written in ``text``."""
    words = re.findall(r"[a-z0-9]+", fold(name or ""))
    folded = " ".join(re.findall(r"[a-z0-9]+", fold(text or "")))
    return len(words) >= 2 and f" {' '.join(words)} " in f" {folded} "


def taken(rows: Sequence[ReceiptRow]) -> Decimal:
    return sum((r.amount for r in rows), Decimal(0))
