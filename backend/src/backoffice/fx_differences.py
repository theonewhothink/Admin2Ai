"""Exchange differences on invoices in another currency (checklist I11; cases 23, 24, 32, 36, 49).

A dollar or pound invoice closes against the euro charge when the bank's conversion line states the
invoice's own amount ("COMPRA AWS EMEA USD 125,00 TAXA 0,9215", checklist P7). The invoice was worth one
amount in euros on the day it was issued and cost another on the day it was paid: the bank's rate is not
the rate of the invoice date. That difference is the accountant's to book (an exchange gain or loss), so it
is recorded once the pair is closed on its evidence, as a plain line for the accountant:

    Exchange difference: €1.84 more than on the invoice date

The value on the invoice date comes from a reference rate (the European Central Bank's euro reference rate
for that day, or the latest before it) through an injectable :class:`~backoffice.reconciliation.FxRateSource`:
the server sets one from its configuration, tests use a fixed table, and the browser demo sets none. With no
source, or no rate for that day, nothing is recorded: a difference is never guessed (§3, §57).

The owner never reads any of it (§36).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING, Any

from backoffice.domain.lifecycle import Stage
from backoffice.learning import display_name, format_money
from backoffice.reconciliation import FxRateUnavailable

if TYPE_CHECKING:  # pragma: no cover
    from backoffice.orchestrator import Orchestrator

__all__ = ["ExchangeDifference", "record_exchange_differences"]

logger = logging.getLogger(__name__)
_CENT = Decimal("0.01")
_MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
           "November", "December")


def _day(value: date) -> str:
    return f"{value.day} {_MONTHS[value.month - 1]} {value.year}"


@dataclass(frozen=True)
class ExchangeDifference:
    """What an invoice in another currency was worth on its date, and what the bank actually charged (or paid in)."""

    document_id: str
    tx_id: str
    company_id: str | None
    direction: str  # "out": a purchase you paid; "in": your sale a customer paid
    currency: str  # the invoice's currency
    foreign_amount: Decimal  # the invoice's total in that currency
    invoice_date: date
    reference_rate: Decimal  # account-currency units for one unit of the invoice's currency, on the invoice date
    account_currency: str  # the currency the bank booked (the company's own)
    at_invoice_date: Decimal  # the invoice's total at the reference rate, to the cent
    booked: Decimal  # what the bank charged or paid in, in its own currency
    paid_on: date
    bank_rate: Decimal | None = None  # the rate the bank's conversion line states, when it states one

    @property
    def difference(self) -> Decimal:
        """Booked minus the value on the invoice date: positive when the bank moved more money than that."""
        return self.booked - self.at_invoice_date

    @property
    def line(self) -> str:
        """'Exchange difference: €1.84 more than on the invoice date'."""
        diff = self.difference
        if diff == 0:
            return "Exchange difference: none, the same as on the invoice date"
        amount = format_money(abs(diff), self.account_currency)
        return f"Exchange difference: {amount} {'more' if diff > 0 else 'less'} than on the invoice date"

    def detail(self, who: str, number: str | None) -> str:
        """The figures behind the line, for the accountant."""
        invoice = f"{who} invoice{' ' + number if number else ''}"
        rate = f"1 {self.currency} = {self.reference_rate.quantize(Decimal('0.0001'))} {self.account_currency}"
        moved = "charged" if self.direction == "out" else "received"
        bank = f" at {self.bank_rate} {self.account_currency}" if self.bank_rate is not None else ""
        return (f"{invoice} for {self.currency} {self.foreign_amount:,.2f}: "
                f"{format_money(self.at_invoice_date, self.account_currency)} at the reference rate on "
                f"{_day(self.invoice_date)} ({rate}); {format_money(self.booked, self.account_currency)} {moved} on "
                f"{_day(self.paid_on)}{bank}.")

    def facts(self) -> dict[str, Any]:
        """For the audit log (§55)."""
        return {"document": self.document_id, "payment": self.tx_id, "currency": self.currency,
                "foreign_amount": self.foreign_amount, "invoice_date": self.invoice_date,
                "reference_rate": self.reference_rate, "at_invoice_date": self.at_invoice_date,
                "booked": self.booked, "paid_on": self.paid_on, "bank_rate": self.bank_rate,
                "difference": self.difference}


def _difference(o: Orchestrator, record: Any, rec: Any) -> ExchangeDifference | None:
    """The difference for one invoice closed against one converted payment, or None when it cannot be known."""
    repo = o.repo
    doc = record.document
    charge = o.bank_charge(rec, record)
    if charge is None or not charge.converted or doc.issue_date is None or doc.gross_amount is None:
        return None  # the same currency, or no conversion line from the bank: nothing to compare
    foreign = abs(doc.gross_amount)
    if charge.amount != foreign:
        return None
    currency = (doc.currency or "").strip().upper()
    account = rec.tx.currency.strip().upper()
    try:
        rate = repo.fx_rates.rate(currency, account, doc.issue_date)
    except FxRateUnavailable as exc:  # the source is down: try again on a later run, never guess
        logger.warning("reference rate unavailable for %s/%s on %s: %s", currency, account, doc.issue_date, exc)
        return None
    except ValueError:
        return None
    if rate is None or rate <= 0:
        return None
    from backoffice.reconciliation import fx_from_text

    declared = fx_from_text(f"{rec.tx.counterparty} {rec.tx.description}", rec.tx.currency)
    return ExchangeDifference(
        document_id=record.id, tx_id=rec.id, company_id=rec.company_id,
        direction="in" if rec.tx.amount > 0 else "out", currency=currency, foreign_amount=foreign,
        invoice_date=doc.issue_date, reference_rate=rate, account_currency=account,
        at_invoice_date=(foreign * rate).quantize(_CENT, rounding=ROUND_HALF_UP), booked=abs(rec.tx.amount),
        paid_on=rec.tx.booked_on, bank_rate=declared.rate if declared is not None else None)


def record_exchange_differences(o: Orchestrator, now: datetime) -> list[str]:
    """Every invoice in another currency closed against one converted bank line gets its exchange difference,
    once, when a reference-rate source is set. Returns the documents recorded now."""
    repo = o.repo
    if repo.fx_rates is None:
        return []
    recorded: list[str] = []
    for record in sorted(repo.documents.values(), key=lambda d: d.id):
        if record.id in repo.fx_differences or len(record.matched_tx_ids) != 1:
            continue
        rec = repo.transactions.get(record.matched_tx_ids[0])
        if rec is None or repo.items[record.item_id].stage is not Stage.CLOSED \
                or repo.items[rec.item_id].stage is not Stage.CLOSED:
            continue  # only a pair closed on its evidence
        if (record.document.currency or "EUR").strip().upper() == rec.tx.currency.strip().upper():
            continue
        found = _difference(o, record, rec)
        if found is None:
            continue
        repo.fx_differences[record.id] = found
        recorded.append(record.id)
        o.log("reconciliation", "exchange_difference", subject_id=record.id,
              evidence_ids=[*record.evidence_ids, rec.evidence_id], values=found.facts(),
              response={"line": found.line})
    return recorded


def accountant_flag(o: Orchestrator, document_id: str) -> dict[str, str] | None:
    """The accountant's line for an invoice's exchange difference ({id, title, detail}), when there is one."""
    found = o.repo.fx_differences.get(document_id)
    record = o.repo.documents.get(document_id)
    if found is None or record is None or found.difference == 0:
        return None
    who = display_name(record.document.supplier_name) if not record.sales else "Your"
    return {"id": f"t_fx_{document_id}", "title": found.line,
            "detail": found.detail(who, record.document.invoice_number)}
