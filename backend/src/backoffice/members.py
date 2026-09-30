"""Memberships, tuition and monthly fees: each payment with its period's receipt (checklist X12).

Customers who pay the same amount on a rhythm (every month, once a school term) are learned as recurring payers
(backoffice.memberships). Their evidence is the business's own receipts:

* a receipts list exported from the school's, gym's or club's software is read row by row: each receipt is a
  document of the business's own sales (for its member, period and amount), matched to the payment of the same
  person (the member, or who pays for them), the same amount and the same period (the month the bank line names,
  else the month it was paid in; a term's receipt, the payment within three weeks of it). Only one receipt may
  fit, never a guess; the bank then confirms the amount and the payer, and both close;
* a receipt paid in cash at the desk closes on its own (its cash goes to the cash box);
* a payment whose receipt is missing is listed for the owner and the accountant in plain words. The receipt is
  the business's own document: it is never asked from the customer;
* a direct debit that comes back ("DEVOLUÇÃO", "RETURNED DEBIT") is linked to the payment it returns: both
  close together with the two bank lines as evidence, neither is income, and the period is open again: its
  receipt waits for the member's next payment, with one plain line until it arrives.

Pure Python over the orchestrator's records, like the other agents.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from backoffice.closure import Month
from backoffice.domain.lifecycle import Stage, TrackedItem
from backoffice.domain.models import (
    Document,
    DocumentType,
    ExtractionMethod,
    FieldObservation,
    Quality,
)
from backoffice.learning import counterparty_key, day_month, format_money
from backoffice.learning.keys import fold
from backoffice.learning.plain import count_phrase, join_and
from backoffice.memberships import (
    PayerSeries,
    ReceiptRow,
    names_in,
    payment_period,
    read_receipts_list,
    recurring_payers,
    returned_debit,
    same_payer,
)
from backoffice.orchestrator import (
    OWNER_ACTOR,
    AnswerOutcome,
    CheckOption,
    DocumentRecord,
    IngestReport,
    NeedsYouRecord,
    TxRecord,
    _Agent,
    _unique_id,
)
from backoffice.reconciliation import EvidenceExpectation, ExpectationDecision

__all__ = ["MemberReceipt", "MembershipAgent", "payer_name"]

RETURN_WINDOW_DAYS = 60  # a direct debit comes back within this many days of being paid (a refund on request)
RETURN_SOON_DAYS = 14  # ... and most within days (a debit the payer's bank refused)
TERM_WINDOW_DAYS = 21  # a term's receipt and its payment are this close


@dataclass
class MemberReceipt:
    document_id: str
    row: ReceiptRow
    company_id: str | None
    status: str = "open"  # "open" | "paid" | "returned" (its payment came back: the period is unpaid again)
    payments: list[str] = field(default_factory=list)  # the payments it was matched to, in order
    returned_by: str | None = None  # the bank line that gave its payment back


def payer_name(tx: Any) -> str:
    """Who paid, as a person's name: 'ANA LOPES' on a bank line is 'Ana Lopes'."""
    name = " ".join((tx.counterparty or "").split())
    return " ".join(w.capitalize() for w in name.split()) if name.isupper() else name or "A customer"


class MembershipAgent(_Agent):
    name = "memberships"
    NEEDS_KINDS = ("member",)

    def __init__(self, orchestrator: Any) -> None:
        super().__init__(orchestrator)
        self._series: dict[str | None, tuple[tuple[int, int], list[PayerSeries]]] = {}

    # ------------------------------------------------------------------ reading

    def accept_text(self, text: str, evidence_ids: list[str], *, at: datetime, origin: str, report: IngestReport,
                    sender: str | None = None, message_text: str = "") -> bool:
        try:
            rows = read_receipts_list(text)
        except Exception as exc:  # a reader bug must never lose the upload: it is read like any other file
            self.log("receipts_read_failed", subject_id=evidence_ids[0], evidence_ids=evidence_ids,
                     response={"error": type(exc).__name__})
            return False
        if not rows:
            return False
        company = self._company(text)
        new = 0
        for row in rows:
            new += self._record(row, company, evidence_ids, at=at, origin=origin, report=report)
        report.already_known = not new
        if not new:
            report.message = "Got it. I already had these receipts."
            return True
        total = sum((r.amount for r in rows), Decimal(0))
        head = f"Got it. This is a list of {len(rows)} receipts ({format_money(total)} in all)"
        if company is None:
            needs = self._ask_company(rows, evidence_ids, at)
            report.message = f"{head}. I need one answer from you: {needs.prompt}"
        else:
            report.message = f"{head}. I will match each one with its payment."
        self.o.activity(at, "collected", f"Collected a list of {len(rows)} receipts from your software "
                        f"({format_money(total)} in all).", company, amount=total, evidence_ids=evidence_ids)
        return True

    def _company(self, text: str) -> str | None:
        repo = self.repo
        if len(repo.companies) == 1:
            return next(iter(repo.companies))
        folded = fold(text[:6000])
        named = {c.id for c in repo.companies.values() for n in (c.name, repo.legal_names.get(c.id) or c.name)
                 if len(fold(n)) >= 4 and fold(n) in folded}
        named |= {c for t in (repo.companies[x].tax_id for x in repo.companies) if t and t in text
                  for c in [repo.company_for_tax_id(t)] if c}
        return named.pop() if len(named) == 1 else None

    def _record(self, row: ReceiptRow, company: str | None, evidence_ids: list[str], *, at: datetime, origin: str,
                report: IngestReport) -> int:
        repo = self.repo
        same = next((r for r in repo.member_receipts.values() if r.row.number == row.number
                     and r.row.amount == row.amount and r.company_id == company), None)
        if same is not None:
            doc = repo.documents[same.document_id]
            fresh = [e for e in evidence_ids if e not in doc.evidence_ids]
            doc.evidence_ids = [*doc.evidence_ids, *fresh]
            doc.document = doc.document.model_copy(update={"evidence_ids": doc.evidence_ids})
            report.document_ids.append(doc.id)
            return 0
        doc_id = "doc_" + hashlib.sha256(f"{evidence_ids[0]}|{row.number}|{row.location}".encode()).hexdigest()[:16]
        company_record = repo.companies.get(company or "")
        observations = {
            "gross_amount": [FieldObservation(value=row.amount, source=evidence_ids[0], method=ExtractionMethod.API,
                                              confidence=0.95, location=row.location)],
            "invoice_number": [FieldObservation(value=row.number, source=evidence_ids[0], method=ExtractionMethod.API,
                                                confidence=0.95, location=row.location)],
        }
        document = Document(
            id=doc_id, tenant_id=repo.tenant_id, evidence_ids=list(evidence_ids), doc_type=DocumentType.RECEIPT,
            supplier_name=(repo.legal_names.get(company) or company_record.name) if company_record else "Your business",
            supplier_tax_id=company_record.tax_id if company_record else None, customer_tax_id=row.tax_id,
            invoice_number=row.number, issue_date=row.issued_on, gross_amount=row.amount,
            # The export is one source: the member's payment (or the cash at the desk) confirms it.
            quality=Quality.AMBER, entity_id=company)
        item = TrackedItem(id="item_" + doc_id, tenant_id=repo.tenant_id, subject_type="document", subject_id=doc_id)
        repo.items[item.id] = item
        record = DocumentRecord(document=document, evidence_ids=list(evidence_ids), origin=origin, received_at=at,
                                item_id=item.id, observations=observations, sales=True, customer=row.member,
                                text=f"{row.number} {row.member} {row.payer or ''} {row.period_label}", book="member",
                                country=repo.company_country(company))  # its company's country (§49)
        repo.documents[doc_id] = record
        self.o._classify_sensitive(record, record.text)  # by its own wording, like every document (§52)
        repo.member_receipts[doc_id] = MemberReceipt(document_id=doc_id, row=row, company_id=company)
        self.o.advance(item, Stage.ACQUIRED, evidence_ids, agent="discovery", note="Receipt received in a list.")
        self.o.advance(item, Stage.UNDERSTOOD, evidence_ids, agent=self.name,
                       note=f"{row.member}, {row.period_label}.")
        self.log("read_receipt", subject_id=doc_id, evidence_ids=evidence_ids,
                 values={"number": row.number, "member": row.member, "payer": row.payer or "", "period": row.period,
                         "amount": row.amount, "cash": row.cash, "location": row.location},
                 response={"company": company or ""})
        report.document_ids.append(doc_id)
        return 1

    def _ask_company(self, rows: list[ReceiptRow], evidence_ids: list[str], at: datetime) -> NeedsYouRecord:
        repo = self.repo
        first = next(r for r in repo.member_receipts.values() if r.row == rows[0])
        record = repo.documents[first.document_id]
        options = tuple(CheckOption(id=f"company:{c}", label=repo.company_name(c) or c, values={"company": c})
                        for c in sorted(repo.companies))
        needs_id = _unique_id(repo.needs, "nd_receipts_company")
        needs = NeedsYouRecord(id=needs_id, kind="member", subject_type="document", subject_id=record.id,
                               item_id=record.item_id, company_id=next(iter(sorted(repo.companies)), ""),
                               created_at=at, prompt="Which of your companies issued this list of receipts?",
                               why=(f"{len(rows)} receipts, from {rows[0].member} to {rows[-1].member}.",
                                    "It does not name one of your companies."), options=options)
        repo.needs[needs_id] = needs
        for r in repo.member_receipts.values():
            if r.row in rows and r.company_id is None:
                self.o.advance(repo.items[repo.documents[r.document_id].item_id], Stage.NEEDS_OWNER, evidence_ids,
                               agent=self.name, note="Which company issued it.")
        self.log("ask_owner", subject_id=record.id, evidence_ids=evidence_ids, response={"needs_you": needs_id})
        return needs

    def answer(self, needs: NeedsYouRecord, option_id: str, answer_ev: str, now: datetime) -> AnswerOutcome:
        repo = self.repo
        option = next((o for o in needs.options if o.id == option_id), None)
        if option is None:
            raise ValueError("not one of the options")
        needs.status, needs.answer, needs.answered_at = "answered", option.id, now
        company = option.values["company"]
        evidence = repo.documents[needs.subject_id].evidence_ids[0]
        changed = 0
        for r in repo.member_receipts.values():
            record = repo.documents[r.document_id]
            if r.company_id is not None or evidence not in record.evidence_ids:
                continue
            r.company_id = company
            record.country = repo.company_country(company)  # its company's country (§49)
            c = repo.companies[company]
            record.document = record.document.model_copy(update={
                "entity_id": company, "supplier_tax_id": c.tax_id, "supplier_name": repo.legal_names.get(company)
                or c.name})
            self.o.advance(repo.items[record.item_id], Stage.UNDERSTOOD, [*record.evidence_ids, answer_ev],
                           agent=self.name, actor=OWNER_ACTOR, note=f"You said {c.name} issued it.")
            changed += 1
        whose = f"{repo.company_name(company)}'s"
        return AnswerOutcome(ok=True, message=f"Done. The receipt is {whose}." if changed == 1 else
                             f"Done. The {changed} receipts are {whose}.")

    # ------------------------------------------------------------------ every run

    def settle(self, now: datetime) -> int:
        repo = self.repo
        moved = self._link_returns(now)
        if repo.member_receipts:
            moved += self._close_cash_receipts()
            moved += self._match()
        return moved

    def is_payment(self, rec: TxRecord) -> bool:
        """Money from a customer that a receipt of the business can prove (never a deposit paid ahead of the work,
        which its invoice takes off, backoffice.deposits)."""
        return (rec.tx.amount > 0 and rec.decision is not None and not rec.private
                and rec.decision.expectation is EvidenceExpectation.SALES_INVOICE
                and rec.decision.rule not in ("cash_deposit",) and rec.id not in self.repo.returned_payments
                and rec.id not in self.repo.deposits)

    def _match(self) -> int:
        repo = self.repo
        moved = 0
        open_receipts = [r for r in repo.member_receipts.values() if r.status in ("open", "returned")
                         and not r.row.cash and r.company_id is not None]
        if not open_receipts:
            return 0
        for rec in sorted(repo.transactions.values(), key=lambda r: (r.tx.booked_on, r.id)):
            if not self.is_payment(rec) or rec.document_ids or repo.items[rec.item_id].is_done:
                continue
            key, _ = payment_period(rec.tx)
            fits = [r for r in open_receipts if r.company_id == rec.company_id and r.row.amount == rec.tx.amount
                    and self._same_person(rec, r) and self._same_period(rec, r, key)]
            if len(fits) != 1:
                if len(fits) > 1:
                    rec.likely_document_ids = sorted(r.document_id for r in fits)
                continue
            self._take(rec, fits[0])
            open_receipts.remove(fits[0])
            moved += 1
        return moved

    @staticmethod
    def _same_person(rec: TxRecord, receipt: MemberReceipt) -> bool:
        row = receipt.row
        payer = rec.tx.counterparty
        text = f"{rec.tx.description} {rec.tx.reference or ''}"
        return (same_payer(payer, row.member) or same_payer(payer, row.payer) or names_in(text, row.member)
                or (bool(row.number) and fold(row.number) in fold(text)))

    @staticmethod
    def _same_period(rec: TxRecord, receipt: MemberReceipt, key: str) -> bool:
        if receipt.row.period.startswith("term:"):
            return abs((receipt.row.issued_on - rec.tx.booked_on).days) <= TERM_WINDOW_DAYS
        return receipt.row.period == key

    def _take(self, rec: TxRecord, receipt: MemberReceipt) -> None:
        repo = self.repo
        record = repo.documents[receipt.document_id]
        row = receipt.row
        again = receipt.status == "returned"
        receipt.payments.append(rec.id)
        receipt.status = "paid"
        rec.document_ids = [record.id]
        rec.likely_document_ids = []
        record.matched_tx_ids = [*record.matched_tx_ids, rec.id]
        record.hold_reason = ""
        # The bank confirms the amount and who paid: the list and the payment agree (two sources).
        record.document = record.document.model_copy(update={"quality": Quality.GREEN})
        record.observations.setdefault("gross_amount", []).append(FieldObservation(
            value=rec.tx.amount, source=rec.evidence_id, method=ExtractionMethod.BANK, confidence=1.0,
            location="the bank line"))
        series = self.series_of(rec)
        paid_by = f" (for {row.member})" if row.payer and not same_payer(rec.tx.counterparty, row.member) else ""
        rec.match_headline = f"Matched to your receipt {row.number} for {row.member}, {row.period_label}."
        rec.match_why = (
            f"Receipt {row.number}: {row.member}, {row.period_label}, {format_money(row.amount)}",
            f"Paid by: {payer_name(rec.tx)}{paid_by}",
            f"Amount: {format_money(rec.tx.amount, rec.tx.currency)}, the same",
            *([f"Usually: {series.phrase(rec.tx.currency)}"] if series is not None else []),
            *([(f"Paid again after the direct debit of "
                f"{day_month(repo.transactions[receipt.payments[-2]].tx.booked_on)} came back")]
              if again and len(receipt.payments) > 1 else []),
        )
        if again:
            prior = [repo.transactions[t].evidence_id for t in receipt.payments[:-1] if t in repo.transactions]
            back = repo.transactions.get(receipt.returned_by or "")
            rec.extra_evidence_ids += [*prior, *([back.evidence_id] if back else [])]
        self.log("match", subject_id=rec.id, evidence_ids=[rec.evidence_id, *record.evidence_ids],
                 values={"transactions": [rec.id], "documents": [record.id], "period": row.period},
                 validations=list(rec.match_why), response={"quality": Quality.GREEN.value, "kind": "member_receipt"})

    def _close_cash_receipts(self) -> int:
        """A receipt paid in cash at the desk: the list is the only record (its cash goes to the cash box)."""
        repo = self.repo
        moved = 0
        for r in sorted(repo.member_receipts.values(), key=lambda x: x.document_id):
            record = repo.documents[r.document_id]
            item = repo.items[record.item_id]
            if not r.row.cash or r.company_id is None or item.is_done or item.stage is Stage.NEEDS_OWNER:
                continue
            record.document = record.document.model_copy(update={"quality": Quality.GREEN})
            r.status = "paid"
            moved += self.o.closure._close(item, list(record.evidence_ids),
                                           note=f"Paid in cash at your desk: {r.row.member}, {r.row.period_label}.")
        return moved

    # ------------------------------------------------------------------ direct debits that come back

    def _link_returns(self, now: datetime) -> int:
        repo = self.repo
        moved = 0
        for back in sorted(repo.transactions.values(), key=lambda r: (r.tx.booked_on, r.id)):
            if back.id in repo.payment_returns or back.private or repo.items[back.item_id].is_done \
                    or not returned_debit(back.tx):
                continue
            if back.deposit_refund_of or (back.decision is not None and back.decision.rule == "deposit_refund"):
                continue  # a deposit given back when a booking was cancelled (backoffice.deposits)
            candidates = [r for r in repo.transactions.values()
                          if r.tx.amount == -back.tx.amount and r.company_id == back.company_id and not r.private
                          and r.id not in repo.returned_payments and r.tx.currency == back.tx.currency
                          and 0 <= (back.tx.booked_on - r.tx.booked_on).days <= RETURN_WINDOW_DAYS
                          and self._returnable(r) and self._same_payer(r, back)]
            if len(candidates) > 1:  # several payments could be it: only the one of the last days, if one
                candidates = [r for r in candidates if (back.tx.booked_on - r.tx.booked_on).days <= RETURN_SOON_DAYS]
            if len(candidates) != 1:
                continue  # none, or several it could be: never a guess
            self._returned(candidates[0], back, now)
            moved += 1
        return moved

    def _returnable(self, rec: TxRecord) -> bool:
        """A customer's payment a receipt proves (or already proved): never a deposit, a payout or cash paid in."""
        if rec.id in self.repo.deposits or rec.tx.amount <= 0:
            return False
        if any(d in self.repo.member_receipts for d in rec.document_ids):
            return True
        return (rec.decision is not None and rec.decision.expectation is EvidenceExpectation.SALES_INVOICE
                and rec.decision.rule != "cash_deposit")

    @staticmethod
    def _same_payer(paid: TxRecord, back: TxRecord) -> bool:
        a, b = paid.tx, back.tx
        if a.counterparty_iban and b.counterparty_iban:
            return a.counterparty_iban.replace(" ", "") == b.counterparty_iban.replace(" ", "")
        key = counterparty_key(a.counterparty)
        return (bool(key) and key == counterparty_key(b.counterparty)) or same_payer(a.counterparty, b.counterparty) \
            or names_in(f"{b.counterparty} {b.description}", a.counterparty)

    def _returned(self, paid: TxRecord, back: TxRecord, now: datetime) -> None:
        repo = self.repo
        repo.payment_returns[back.id] = paid.id
        repo.returned_payments[paid.id] = back.id
        who = payer_name(paid.tx)
        amount = format_money(paid.tx.amount, paid.tx.currency)
        paid_on, back_on = day_month(paid.tx.booked_on, repo.today()), day_month(back.tx.booked_on, repo.today())
        receipts = [repo.member_receipts[d] for d in paid.document_ids if d in repo.member_receipts]
        period = receipts[0].row.period_label if receipts else payment_period(paid.tx)[1]
        evidence = [back.evidence_id, paid.evidence_id]
        back.decision = ExpectationDecision(
            back.id, EvidenceExpectation.BANK_EVIDENCE_SUFFICES,
            f"{who}'s {amount} payment of {paid_on} came back. The two bank lines show it.", Quality.GREEN,
            "returned_debit")
        back.match_headline = f"{who}'s payment of {paid_on} came back."
        back.match_why = (f"Paid in: {amount} from {who} on {paid_on}", f"Came back: {amount} on {back_on}",
                          f"For: {period}")
        self.o.advance(repo.items[back.item_id], Stage.NOT_REQUIRED, evidence, agent=self.name, quality=Quality.GREEN,
                       note=f"It gives back {who}'s payment of {paid_on}: nothing was kept.")
        item = repo.items[paid.item_id]
        if not item.is_done:
            paid.decision = ExpectationDecision(
                paid.id, EvidenceExpectation.BANK_EVIDENCE_SUFFICES,
                f"{who}'s payment came back on {back_on}. The two bank lines show it.", Quality.GREEN, "returned_debit")
            paid.likely_document_ids = []
            self.o.advance(item, Stage.NOT_REQUIRED, evidence, agent=self.name, quality=Quality.GREEN,
                           note=f"It came back on {back_on}: nothing was kept.")
        for receipt in receipts:  # the period is open again: its receipt waits for the next payment
            receipt.status, receipt.returned_by = "returned", back.id
            record = repo.documents[receipt.document_id]
            record.hold_reason = self.returned_line(receipt)
            self.o.advance(repo.items[record.item_id], Stage.NEEDS_OWNER, [*record.evidence_ids, *evidence],
                           agent=self.name, quality=Quality.AMBER, note=f"Its payment came back on {back_on}.")
        self.log("returned_payment", subject_id=back.id, evidence_ids=evidence,
                 values={"payment": paid.id, "amount": paid.tx.amount, "period": period,
                         "receipts": [r.document_id for r in receipts]})
        self.o.activity(now, "checked", f"{who}'s {amount} payment for {period} came back on {back_on}. {period} is "
                        "unpaid again.", paid.company_id, amount=paid.tx.amount, currency=paid.tx.currency,
                        evidence_ids=evidence)

    def returned_line(self, receipt: MemberReceipt) -> str:
        repo = self.repo
        row = receipt.row
        back = repo.transactions.get(receipt.returned_by or "")
        when = f" on {day_month(back.tx.booked_on, repo.today())}" if back else ""
        return (f"{row.member}'s {format_money(row.amount)} payment for {row.period_label} came back{when}. "
                f"{row.period_label} is unpaid again: I will match {row.member}'s next payment to receipt {row.number}.")

    def kept_from_matching(self, rec: TxRecord) -> bool:
        return rec.id in self.repo.payment_returns or rec.id in self.repo.returned_payments

    # ------------------------------------------------------------------ recurring payers

    def series(self, company_id: str | None = None) -> list[PayerSeries]:
        """Customers who pay the same amount on a rhythm (learned again when payments come or go back)."""
        repo = self.repo
        stamp = (len(repo.transactions), len(repo.returned_payments))
        cached = self._series.get(company_id)
        if cached is not None and cached[0] == stamp:
            return cached[1]
        payments = [r.tx for r in repo.transactions.values() if self.is_payment(r)
                    and (company_id is None or r.company_id == company_id)]
        learned = recurring_payers(payments)
        self._series[company_id] = (stamp, learned)
        return learned

    def series_of(self, rec: TxRecord) -> PayerSeries | None:
        key = counterparty_key(rec.tx.counterparty)
        return next((s for s in self.series(rec.company_id) if s.key == key and rec.id in s.payments), None)

    def plan(self, rec: TxRecord) -> str | None:
        """A customer's payment without the business's receipt: listed for the owner, never asked of the customer."""
        repo = self.repo
        if rec.id in repo.returned_payments or not self.is_payment(rec) or rec.document_ids:
            return None
        series = self.series_of(rec)
        lists = any(r.company_id == rec.company_id for r in repo.member_receipts.values())
        if series is None and not lists:
            return None
        who = payer_name(rec.tx)
        amount = format_money(rec.tx.amount, rec.tx.currency)
        period = payment_period(rec.tx)[1]
        usually = f" ({who} usually {series.phrase(rec.tx.currency)})" if series is not None else ""
        if rec.likely_document_ids:
            return (f"{who} paid {amount} on {day_month(rec.tx.booked_on, repo.today())}{usually}. More than one of "
                    "your receipts could be for it, so I won't pick one on a guess.")
        return (f"{who} paid {amount} on {day_month(rec.tx.booked_on, repo.today())} for {period}{usually}. Your "
                "receipt for it is missing: issue it in your software and send me the receipts list. I won't ask "
                f"{who} for it: the receipt is yours to issue.")

    def month_lines(self, company_id: str, month: Month) -> list[dict[str, Any]]:
        """Receipts whose payment came back this month's period: unpaid again, in plain words."""
        repo = self.repo
        out = []
        for r in sorted(repo.member_receipts.values(), key=lambda x: (x.row.issued_on, x.document_id)):
            record = repo.documents[r.document_id]
            if r.status != "returned" or r.company_id != company_id or repo.items[record.item_id].is_done:
                continue
            if repo.item_month(repo.items[record.item_id]) != month:
                continue
            out.append({"id": f"r_{record.id}", "tone": "attention", "text": self.returned_line(r)})
        return out

    def view(self, company_id: str, month: Month) -> dict[str, Any] | None:
        """For the accountant: who pays on a rhythm, the month's receipts and the periods open again."""
        repo = self.repo
        series = self.series(company_id)
        receipts = [r for r in repo.member_receipts.values() if r.company_id == company_id
                    and Month.of(r.row.issued_on) == month]
        returned = [r for r in repo.member_receipts.values() if r.company_id == company_id and r.status == "returned"]
        if not series and not receipts and not returned:
            return None
        return {
            "recurringPayers": [{"name": s.name, "amount": float(s.amount), "rhythm": s.rhythm,
                                 "payments": len(s.payments), "since": s.first_seen.isoformat(),
                                 "learned": "trusted" if s.trusted else "still learning"} for s in series],
            "receipts": [{"id": r.document_id, "number": r.row.number, "member": r.row.member,
                          "period": r.row.period_label, "amount": float(r.row.amount),
                          "status": {"paid": "paid", "open": "waiting for its payment",
                                     "returned": "payment came back"}[r.status]}
                         for r in sorted(receipts, key=lambda x: (x.row.issued_on, x.row.number))],
            "unpaidAgain": [self.returned_line(r) for r in returned],
        }

    def income_line(self, lines: list[Any]) -> str | None:
        """'That includes €405.00 from 9 people who pay you every month.' (money in answers)"""
        repo = self.repo
        paid = {t for r in repo.member_receipts.values() for t in r.payments if t not in repo.returned_payments}
        desk = {r.document_id: r for r in repo.member_receipts.values() if r.row.cash and r.status == "paid"}
        regular = {t for s in self.series() for t in s.payments}
        mine = [x for x in lines if x.kind == "income" and (x.id in paid or x.id in regular or x.id in desk)]
        if not mine:
            return None
        people = {counterparty_key(repo.transactions[x.id].tx.counterparty) if x.id in repo.transactions
                  else counterparty_key(desk[x.id].row.member) for x in mine}
        total = sum((x.amount for x in mine), Decimal(0))
        rhythms = sorted({s.rhythm for s in self.series() if any(x.id in s.payments for x in mine)})
        how = f" who pay{'s' if len(people) == 1 else ''} you {join_and(rhythms)}" if rhythms else ""
        line = (f"That includes {format_money(total)} in memberships and fees from "
                f"{count_phrase(len(people), 'person', 'people')}{how}.")
        # A fee paid in another month than the one it is for: counted when paid, said for its own period.
        receipt_of = {t: r for r in repo.member_receipts.values() for t in r.payments}
        other: dict[str, Decimal] = {}
        for x in mine:
            receipt = receipt_of.get(x.id)
            if receipt is not None and not receipt.row.period.startswith("term:") \
                    and receipt.row.period != f"{x.on.year:04d}-{x.on.month:02d}":
                other[receipt.row.period_label] = other.get(receipt.row.period_label, Decimal(0)) + x.amount
        if other:
            line += " Of that, " + join_and([f"{format_money(v)} pays for {k}" for k, v in sorted(other.items())]) + "."
        return line


def returned_pair(repo: Any, tx_id: str) -> tuple[str, str] | None:
    """(payment, the bank line that gave it back) for either of the two, else None."""
    if tx_id in repo.payment_returns:
        return repo.payment_returns[tx_id], tx_id
    if tx_id in repo.returned_payments:
        return tx_id, repo.returned_payments[tx_id]
    return None

