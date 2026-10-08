"""Leasing and renting contracts: each monthly payment matched to its line of the plan (checklist X24).

A contract (backoffice.leases) is read into a payment plan, kept as a document of its own (never booked,
never a payment's proof by itself), and the leasing company becomes a known supplier: its tax number, its
name as banks write it, and the address its invoices come from. Then, on every run:

* each payment to the leasing company is matched to the line of the plan due within ten days of it. The
  same amount: it is that month's payment. Another amount: one plain question (never a silent change), and
  the payment waits for the answer;
* a monthly payment closes on the leasing company's invoice for it (same amount, the contract number when the
  invoice prints it, dated around the payment's due date), or, where the country's practice lets the contract
  stand as the tax document (backoffice.leases.CONTRACT_WITH_STATEMENT), on the contract plus the leasing
  company's statement showing the payment. A monthly invoice that does not come is looked for and asked for
  like any other missing invoice (the leasing company is a supplier with an address);
* the accountant sees each lease: the asset, the monthly payment and its VAT, what has been paid and what is
  still to come; a vehicle's plate names its vehicle cost center, when the company keeps cost centers.

Pure Python over the orchestrator's records, like the other agents.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal
from typing import Any

from backoffice.countries import pack_text
from backoffice.domain.lifecycle import Stage, TrackedItem
from backoffice.domain.models import Document, DocumentType, Quality, Supplier
from backoffice.learning import day_month, display_name, fold, format_money
from backoffice.learning.keys import same_tax_id
from backoffice.leases import LeaseContract, LeaseLine, read_lease
from backoffice.orchestrator import (
    OWNER_ACTOR,
    AnswerOutcome,
    CheckOption,
    DocumentRecord,
    IngestReport,
    NeedsYouRecord,
    TxRecord,
    _Agent,
    _slug,
    _unique_id,
)
from backoffice.reconciliation import EvidenceExpectation, ExpectationDecision
from backoffice.supplier_statements import PAYMENT

__all__ = ["LeaseAgent", "LeaseRecord"]

_INVOICES = frozenset({DocumentType.INVOICE, DocumentType.INVOICE_RECEIPT, DocumentType.DEBIT_NOTE})
INVOICE_BEFORE_DAYS = 25  # a leasing company invoices a rent up to this many days before it is due
INVOICE_AFTER_DAYS = 20  # ... or this many days after
STATEMENT_DAYS = 7  # a statement books a payment within this many days of the bank


@dataclass
class LeaseRecord:
    document_id: str
    contract: LeaseContract
    company_id: str | None
    supplier_id: str | None
    status: str = "open"  # "open" | "set_aside" (its amounts do not add up; a corrected one will come)
    paid: dict[int, str] = field(default_factory=dict)  # line of the plan -> the payment that paid it
    amounts: dict[int, Decimal] = field(default_factory=dict)  # a line the owner confirmed at another amount
    not_lease: list[str] = field(default_factory=list)  # payments the owner said are not a monthly payment

    @property
    def usable(self) -> bool:
        return self.status == "open" and self.contract.complete and self.company_id is not None

    def expected(self, line: LeaseLine) -> Decimal:
        return self.amounts.get(line.number, line.amount)


class LeaseAgent(_Agent):
    name = "leasing"
    NEEDS_KINDS = ("lease",)

    # ------------------------------------------------------------------ reading

    def accept_text(self, text: str, evidence_ids: list[str], *, at: datetime, origin: str, report: IngestReport,
                    sender: str | None = None, message_text: str = "") -> bool:
        try:
            contract = read_lease(text, home=self.repo.primary_country())
        except Exception as exc:  # a reader bug must never lose the upload: it is read like any other file
            self.log("lease_read_failed", subject_id=evidence_ids[0], evidence_ids=evidence_ids,
                     response={"error": type(exc).__name__})
            return False
        if contract is None:
            return False
        repo = self.repo
        same = next((lease for lease in repo.leases.values() if lease.contract.number and
                     lease.contract.number == contract.number and
                     same_tax_id(lease.contract.lessor_tax_id, contract.lessor_tax_id)), None)
        if same is not None:
            doc = repo.documents[same.document_id]
            new = [e for e in evidence_ids if e not in doc.evidence_ids]
            doc.evidence_ids = [*doc.evidence_ids, *new]
            doc.document = doc.document.model_copy(update={"evidence_ids": doc.evidence_ids})
            report.document_ids.append(doc.id)
            report.already_known = not new
            report.message = "Got it. I already had this contract."
            return True
        company = repo.company_for_tax_id(contract.customer_tax_id) if contract.customer_tax_id else None
        if company is None and len(repo.companies) == 1:
            company = next(iter(repo.companies))
        if company is not None and contract.country == repo.primary_country():
            # Its company's country's practice (§49): a Spanish company's contract is not read as Portuguese.
            contract = replace(contract, country=repo.company_country(company))
        supplier = self._supplier(contract, sender)
        doc_id = "doc_" + hashlib.sha256(f"{evidence_ids[0]}|lease|{contract.number}".encode()).hexdigest()[:16]
        quality = Quality.RED if contract.problems else Quality.GREEN if contract.complete else Quality.AMBER
        document = Document(
            id=doc_id, tenant_id=repo.tenant_id, evidence_ids=list(evidence_ids), doc_type=DocumentType.CONTRACT,
            supplier_name=supplier.name if supplier else display_name(contract.lessor),
            supplier_tax_id=contract.lessor_tax_id, customer_tax_id=contract.customer_tax_id,
            invoice_number=contract.number, currency=contract.currency, net_amount=contract.net,
            vat_amount=contract.vat, quality=quality, entity_id=company)
        item = TrackedItem(id="item_" + doc_id, tenant_id=repo.tenant_id, subject_type="document", subject_id=doc_id)
        repo.items[item.id] = item
        record = DocumentRecord(document=document, evidence_ids=list(evidence_ids), origin=origin, received_at=at,
                                item_id=item.id, observations={}, reasons=contract.problems, sender=sender,
                                message_text=message_text, supplier_id=supplier.id if supplier else None,
                                text=text[:20_000], book="lease" if contract.kind == "leasing" else "renting",
                                country=repo.company_country(company))  # its company's country (§49)
        repo.documents[doc_id] = record
        self.o._classify_sensitive(record, text, message_text)  # by its own wording, like every document (§52)
        lease = LeaseRecord(document_id=doc_id, contract=contract, company_id=company,
                            supplier_id=supplier.id if supplier else None)
        repo.leases[doc_id] = lease
        self.o.advance(item, Stage.ACQUIRED, evidence_ids, agent="discovery", note="Contract received.")
        self.o.advance(item, Stage.UNDERSTOOD, evidence_ids, agent=self.name, note="Read its payment plan.")
        self.log("read_lease", subject_id=doc_id, evidence_ids=evidence_ids,
                 values={"lessor": contract.lessor, "lessor_tax_id": contract.lessor_tax_id or "",
                         "number": contract.number or "", "asset": contract.asset or "", "plate": contract.plate or "",
                         "start": contract.start, "term": contract.term, "net": contract.net, "vat": contract.vat,
                         "gross": contract.gross, "residual": contract.residual, "country": contract.country},
                 validations=[{"check": "adds_up", "ok": not contract.problems},
                              {"check": "complete", "missing": list(contract.missing)}],
                 response={"quality": quality.value, "company": company or ""})
        who = document.supplier_name or "the leasing company"
        today = repo.today()
        head = f"your {contract.word} contract with {who} for {contract.what}"
        if contract.problems:
            self.o.advance(item, Stage.CONFLICT, evidence_ids, agent=self.name, note=" ".join(contract.problems))
            needs = self._ask_document(record, lease, at, prompt=f"The {contract.word} contract with {who} does not add "
                                       f"up: {contract.problems[0]} What should I do?",
                                       options=(CheckOption(id="aside", label="Set it aside. I'll send a corrected "
                                                                                "one."),))
            report.message = f"Got it. This is {head}. I need one answer from you: {needs.prompt}"
        elif contract.missing:
            record.hold_reason = (f"I read {head}, but I could not find {' or '.join(contract.missing)} in it. Send "
                                  "me the page with the payment plan.")
            report.message = f"Got it. {record.hold_reason[:1].upper()}{record.hold_reason[1:]}"
        elif company is None:
            self.o.advance(item, Stage.NEEDS_OWNER, evidence_ids, agent=self.name, note="Which company it is for.")
            options = tuple(CheckOption(id=f"company:{c}", label=repo.company_name(c) or c, values={"company": c})
                            for c in sorted(repo.companies))
            needs = self._ask_document(record, lease, at, prompt=f"Which of your companies is the {contract.word} "
                                       f"contract with {who} for?", options=options)
            report.message = f"Got it. This is {head}. I need one answer from you: {needs.prompt}"
        else:
            self._keep(record, lease)
            invoices = "" if contract.contract_suffices else f" and look for {who}'s monthly invoice"
            report.message = (f"Got it. This is {head}: {contract.describe(today)}. I will match each payment to it"
                              f"{invoices}.")
        self.o.activity(at, "collected", f"Collected {head}: {contract.describe(today)}.", company,
                        amount=contract.gross, currency=contract.currency, evidence_ids=evidence_ids)
        report.document_ids.append(doc_id)
        # Payments to the leasing company, decided before it was known (a payment of a plan keeps its line).
        self.o.redecide(lambda r: r.tx.amount < 0 and r.id not in repo.lease_payments)
        return True

    def _keep(self, record: DocumentRecord, lease: LeaseRecord) -> None:
        item = self.repo.items[record.item_id]
        self.o.advance(item, Stage.NOT_REQUIRED, record.evidence_ids, agent=self.name, quality=Quality.GREEN,
                       note=f"{lease.contract.word.capitalize()} contract kept: it sets the monthly payments for "
                            f"{lease.contract.what}.")

    def _supplier(self, contract: LeaseContract, sender: str | None) -> Supplier | None:
        """The leasing company as a known supplier: found by its tax number, else added from the contract."""
        repo = self.repo
        email = contract.lessor_email or (sender.lower() if sender and "@" in sender else None)
        found = repo.supplier_for_tax_id(contract.lessor_tax_id) if contract.lessor_tax_id else None
        if found is not None:
            if email and not found.contact_email:
                found = found.model_copy(update={"contact_email": email})
                repo.suppliers[found.id] = found
            return found
        if contract.lessor == "the leasing company":
            return None
        name = display_name(contract.lessor)
        aliases = list(dict.fromkeys(a for a in (contract.lessor.upper(), name.upper()) if a))
        supplier = Supplier(id=_unique_id(repo.suppliers, f"sup_{_slug(name)}"), tenant_id=repo.tenant_id, name=name,
                            aliases=aliases, tax_id=contract.lessor_tax_id, countries=[contract.country],
                            contact_email=email, email_domains=[email.rsplit("@", 1)[1]] if email else [])
        return repo.add_supplier(supplier)

    def _ask_document(self, record: DocumentRecord, lease: LeaseRecord, at: datetime, *, prompt: str,
                      options: tuple[CheckOption, ...]) -> NeedsYouRecord:
        repo = self.repo
        needs_id = _unique_id(repo.needs, "nd_lease")
        needs = NeedsYouRecord(id=needs_id, kind="lease", subject_type="document", subject_id=record.id,
                               item_id=record.item_id, company_id=lease.company_id or next(iter(repo.companies), ""),
                               created_at=at, prompt=prompt, options=options,
                               why=(f"Monthly payment: {lease.contract.describe(repo.today())}.",
                                    "Until you answer, I won't match payments to it."))
        repo.needs[needs_id] = needs
        self.log("ask_owner", subject_id=record.id, evidence_ids=record.evidence_ids, values={"prompt": prompt},
                 response={"needs_you": needs_id})
        return needs

    # ------------------------------------------------------------------ every run

    def settle(self, now: datetime) -> int:
        repo = self.repo
        if not repo.leases:
            return 0
        moved = 0
        for lease in sorted(repo.leases.values(), key=lambda x: x.document_id):
            if not lease.usable:
                continue
            moved += self._link(lease, now)
            moved += self._match(lease)
        return moved

    def pays_lessor(self, rec: TxRecord, lease: LeaseRecord) -> bool:
        found = self.repo.resolver().resolve_transaction(rec.tx).supplier
        if found is not None and found.id == lease.supplier_id:
            return True
        number = lease.contract.number
        return bool(number) and fold(number) in fold(f"{rec.tx.description} {rec.tx.reference or ''}")

    def _asked(self, rec: TxRecord) -> bool:
        return any(n.status == "open" and n.kind == "lease" and n.subject_id == rec.id for n in self.repo.needs.values())

    def _link(self, lease: LeaseRecord, now: datetime) -> int:
        repo = self.repo
        moved = 0
        for rec in sorted(repo.transactions.values(), key=lambda r: (r.tx.booked_on, r.id)):
            if rec.tx.amount >= 0 or rec.private or rec.id in repo.lease_payments \
                    or rec.id in lease.not_lease or rec.company_id != lease.company_id:
                continue
            if self._asked(rec) or rec.tx.currency.strip().upper() != lease.contract.currency \
                    or not self.pays_lessor(rec, lease):
                continue
            line = lease.contract.line_near(rec.tx.booked_on, skip=set(lease.paid))
            if line is None:
                continue  # not a payment of the plan: an ordinary payment to the leasing company
            if rec.document_ids or repo.items[rec.item_id].is_done:
                # Already proven before the contract arrived (its invoice matched it): only noted on the plan.
                if abs(rec.tx.amount) == lease.expected(line):
                    lease.paid[line.number] = rec.id
                    repo.lease_payments[rec.id] = (lease.document_id, line.number)
                continue
            if abs(rec.tx.amount) == lease.expected(line):
                self._take(lease, rec, line)
                moved += 1
            else:
                self._ask_amount(lease, rec, line, now)
        return moved

    def line_of(self, rec: TxRecord) -> tuple[LeaseRecord, LeaseLine] | None:
        found = self.repo.lease_payments.get(rec.id)
        if found is None:
            return None
        lease = self.repo.leases[found[0]]
        line = next(x for x in lease.contract.schedule() if x.number == found[1])
        return lease, line

    def _words(self, lease: LeaseRecord, line: LeaseLine) -> str:
        c = lease.contract
        if line.residual:
            return f"the final payment to keep {c.what}"
        return f"payment {line.number} of {c.term} for {c.what}"

    def _who(self, lease: LeaseRecord) -> str:
        supplier = self.repo.suppliers.get(lease.supplier_id or "")
        return supplier.name if supplier else display_name(lease.contract.lessor)

    def _take(self, lease: LeaseRecord, rec: TxRecord, line: LeaseLine) -> None:
        repo = self.repo
        c = lease.contract
        lease.paid[line.number] = rec.id
        repo.lease_payments[rec.id] = (lease.document_id, line.number)
        who = self._who(lease)
        covers = (f"The {c.word} contract and {who}'s statement cover it." if c.contract_suffices else
                  f"{who}'s monthly invoice covers it.")
        what = self._words(lease, line)
        reason = f"{c.word.capitalize()} {what}. {covers}" if not line.residual else f"The {what[4:]}. {covers}"
        rec.decision = ExpectationDecision(rec.id, EvidenceExpectation.INVOICE, reason, Quality.GREEN, "lease")
        self.log("lease_payment", subject_id=rec.id, evidence_ids=[rec.evidence_id, *repo.documents[
            lease.document_id].evidence_ids], values={"contract": c.number or "", "line": line.number,
                                                      "due": line.due, "expected": lease.expected(line)},
                 response={"reason": reason})

    def _ask_amount(self, lease: LeaseRecord, rec: TxRecord, line: LeaseLine, now: datetime) -> None:
        repo = self.repo
        c = lease.contract
        who = self._who(lease)
        amount = c.m(abs(rec.tx.amount))
        expected = c.m(lease.expected(line))
        when = day_month(rec.tx.booked_on, repo.today())
        prompt = (f"The {amount} payment to {who} on {when} is not the {expected} monthly payment in your {c.word} "
                  f"contract for {c.what}. What is it?")
        why = ((f"{c.word.capitalize()} contract{' ' + c.number if c.number else ''}: {self._words(lease, line)}, "
                f"due {day_month(line.due, repo.today())}."), f"Monthly payment in the contract: {expected}.",
               f"Paid: {amount}.", "Until you answer, I won't close this payment.")
        options = (CheckOption(id="changed", label="This month's payment. The amount changed.",
                               values={"lease": lease.document_id, "line": str(line.number)}),
                   CheckOption(id="other", label="Something else, not the monthly payment.",
                               values={"lease": lease.document_id}))
        needs_id = _unique_id(repo.needs, f"nd_{_slug(who.split()[0])}_lease")
        repo.needs[needs_id] = NeedsYouRecord(id=needs_id, kind="lease", subject_type="transaction",
                                              subject_id=rec.id, item_id=rec.item_id, company_id=rec.company_id,
                                              created_at=now, why=why, prompt=prompt, options=options)
        evidence = [rec.evidence_id, *repo.documents[lease.document_id].evidence_ids]
        self.log("ask_owner", subject_id=rec.id, evidence_ids=evidence,
                 values={"expected": lease.expected(line), "paid": abs(rec.tx.amount), "line": line.number},
                 response={"needs_you": needs_id})
        self.o.activity(now, "checked", f"The {amount} payment to {who} is not the {expected} in your {c.word} "
                        "contract. I asked you about it.", rec.company_id, amount=abs(rec.tx.amount),
                        currency=rec.tx.currency, evidence_ids=evidence)
        self.o.advance(repo.items[rec.item_id], Stage.NEEDS_OWNER, evidence, agent=self.name,
                       note="The amount is not the one in the contract.")

    def _match(self, lease: LeaseRecord) -> int:
        """Each monthly payment of this lease with the leasing company's invoice for it, or (where the contract
        can stand as the tax document) the leasing company's statement showing it."""
        repo = self.repo
        c = lease.contract
        moved = 0
        for number, tx_id in sorted(lease.paid.items()):
            rec = repo.transactions.get(tx_id)
            if rec is None or rec.document_ids or rec.proof_evidence_ids or repo.items[rec.item_id].is_done:
                continue
            line = next(x for x in c.schedule() if x.number == number)
            amount = abs(rec.tx.amount)
            invoices = [d for d in repo.documents.values()
                        if not d.book and not d.sales and not d.supporting and not d.on_hold and not d.matched_tx_ids
                        and d.document.doc_type in _INVOICES and d.document.quality is Quality.GREEN
                        and (d.supplier_id == lease.supplier_id or same_tax_id(d.document.supplier_tax_id,
                                                                               c.lessor_tax_id))
                        and d.document.gross_amount == amount and d.claim_id is None]
            named = [d for d in invoices if c.number and fold(c.number) in fold(d.text)]
            pool = named or [d for d in invoices if not any(
                other.contract.number and fold(other.contract.number) in fold(d.text)
                for other in repo.leases.values() if other is not lease)]
            pool = [d for d in pool if d.document.issue_date is not None
                    and -INVOICE_BEFORE_DAYS <= (d.document.issue_date - line.due).days <= INVOICE_AFTER_DAYS]
            if len(pool) == 1:
                doc = pool[0]
                self._matched(lease, rec, line, doc)
                moved += 1
                continue
            if len(pool) > 1:
                rec.likely_document_ids = sorted(d.id for d in pool)
                continue
            if c.contract_suffices:
                moved += self._by_statement(lease, rec, line)
        return moved

    def _matched(self, lease: LeaseRecord, rec: TxRecord, line: LeaseLine, doc: DocumentRecord) -> None:
        repo = self.repo
        c = lease.contract
        rec.document_ids = [doc.id]
        rec.likely_document_ids = []
        doc.matched_tx_ids = [rec.id]
        number = f" {doc.document.invoice_number}" if doc.document.invoice_number else ""
        rec.match_headline = f"Matched to {self._who(lease)}'s invoice for {self._words(lease, line)}."
        rec.match_why = (
            (f"{c.word.capitalize()} contract{' ' + c.number if c.number else ''}: {self._words(lease, line)}, "
             f"due {day_month(line.due, repo.today())}"),
            f"Monthly payment in the contract: {c.m(lease.expected(line))}",
            f"Invoice{number}: {c.m(doc.document.gross_amount or Decimal(0))}",
            f"Paid: {c.m(abs(rec.tx.amount))}, the same",
            *([f"The invoice names contract {c.number}"] if c.number and fold(c.number) in fold(doc.text) else []),
        )
        self.log("match", subject_id=rec.id, evidence_ids=[rec.evidence_id, *doc.evidence_ids,
                                                             *repo.documents[lease.document_id].evidence_ids],
                 values={"transactions": [rec.id], "documents": [doc.id], "line": line.number},
                 validations=list(rec.match_why), response={"quality": Quality.GREEN.value, "kind": "lease"})

    def _by_statement(self, lease: LeaseRecord, rec: TxRecord, line: LeaseLine) -> int:
        repo = self.repo
        c = lease.contract
        amount = abs(rec.tx.amount)
        for sr in sorted(repo.statements.values(), key=lambda s: s.document_id):
            doc = repo.documents.get(sr.document_id)
            if doc is None or (sr.supplier_id or doc.supplier_id) != lease.supplier_id:
                continue
            shown = next((x for x in sr.statement.lines if x.kind == PAYMENT and x.amount == amount and x.on
                          and abs((x.on - rec.tx.booked_on).days) <= STATEMENT_DAYS), None)
            if shown is None:
                continue
            contract_doc = repo.documents[lease.document_id]
            rec.proof_evidence_ids = [*contract_doc.evidence_ids, *doc.evidence_ids]
            who = self._who(lease)
            rec.proof_note = (f"{self._words(lease, line)[:1].upper()}{self._words(lease, line)[1:]}: the {c.word} "
                              f"contract sets it out with its VAT, and {who}'s statement shows it paid on "
                              f"{day_month(shown.on)}.")
            rec.match_headline = f"Proven by the {c.word} contract and {who}'s statement."
            rec.match_why = (
                (f"{c.word.capitalize()} contract{' ' + c.number if c.number else ''}: {self._words(lease, line)}, "
                 f"due {day_month(line.due, repo.today())}"),
                (f"Monthly payment in the contract: {c.m(lease.expected(line))} ({c.m(c.net or Decimal(0))} + VAT "
                 f"{c.m(c.vat or Decimal(0))})"),
                f"{who}'s statement: {c.m(shown.amount)} paid on {day_month(shown.on)}",
                f"Paid: {c.m(amount)}, the same",
            )
            self.log("prove_by_statement", subject_id=rec.id,
                     evidence_ids=[rec.evidence_id, *rec.proof_evidence_ids],
                     values={"line": line.number, "statement": doc.id}, validations=list(rec.match_why))
            return 1
        return 0

    def kept_from_matching(self, rec: TxRecord) -> bool:
        """A monthly payment is matched here (to the right line and invoice), never by amount alone."""
        return rec.id in self.repo.lease_payments or self._asked(rec)

    # ------------------------------------------------------------------ the owner's answer

    def answer(self, needs: NeedsYouRecord, option_id: str, answer_ev: str, now: datetime) -> AnswerOutcome:
        repo = self.repo
        option = next((o for o in needs.options if o.id == option_id), None)
        if option is None:
            raise ValueError("not one of the options")
        needs.status, needs.answer, needs.answered_at = "answered", option.id, now
        if needs.subject_type == "document":
            record = repo.documents[needs.subject_id]
            lease = repo.leases[record.id]
            item = repo.items[record.item_id]
            if option_id == "aside":
                lease.status = "set_aside"
                self.o.advance(item, Stage.NOT_REQUIRED, [*record.evidence_ids, answer_ev], agent=self.name,
                               actor=OWNER_ACTOR, note="Set aside: its amounts do not add up.")
                return AnswerOutcome(ok=True, message="Done. I set it aside. When you send the corrected contract, "
                                                      "I will read it.")
            company = option.values["company"]
            lease.company_id = company
            record.country = repo.company_country(company)  # its company's country, and its practice (§49)
            if lease.contract.country == repo.primary_country():
                lease.contract = replace(lease.contract, country=record.country)
            record.document = record.document.model_copy(update={"entity_id": company})
            self.o.advance(item, Stage.UNDERSTOOD, [*record.evidence_ids, answer_ev], agent=self.name,
                           actor=OWNER_ACTOR, note=f"You said it is {repo.company_name(company)}'s.")
            self._keep(record, lease)
            return AnswerOutcome(ok=True, message=f"Done. It is {repo.company_name(company)}'s. I will match each "
                                                  "payment to it.")
        rec = repo.transactions[needs.subject_id]
        lease = repo.leases[option.values["lease"]]
        item = repo.items[rec.item_id]
        who = self._who(lease)
        c = lease.contract
        if option_id == "changed":
            line = next(x for x in c.schedule() if x.number == int(option.values["line"]))
            if line.number in lease.paid:  # taken since the question was asked: the answer cannot apply any more
                return AnswerOutcome(ok=False, message="That month's payment is already matched to another payment.")
            lease.amounts[line.number] = abs(rec.tx.amount)
            self._take(lease, rec, line)
            rec.extra_evidence_ids.append(answer_ev)
            self.o.advance(item, Stage.UNDERSTOOD, [rec.evidence_id, answer_ev], agent=self.name, actor=OWNER_ACTOR,
                           note=f"You said it is {self._words(lease, line)}.")
            self.o.activity(now, "answered", f"You told me the {c.m(abs(rec.tx.amount))} payment to {who} is "
                            f"{self._words(lease, line)}.", rec.company_id, evidence_ids=[answer_ev])
            needed = f"{who}'s statement" if c.contract_suffices else f"{who}'s invoice"
            return AnswerOutcome(ok=True, message=f"Done. It is {self._words(lease, line)}, at "
                                                  f"{c.m(abs(rec.tx.amount))}. It closes when {needed} arrives.")
        lease.not_lease.append(rec.id)
        self.o.advance(item, Stage.UNDERSTOOD, [rec.evidence_id, answer_ev], agent=self.name, actor=OWNER_ACTOR,
                       note="You said it is not a monthly payment of the contract.")
        return AnswerOutcome(ok=True, message=f"Done. It stays an ordinary payment to {who}: I will look for its "
                                              "invoice.")

    # ------------------------------------------------------------------ plain words and views

    def plan(self, rec: TxRecord) -> str | None:
        repo = self.repo
        if self._asked(rec):
            lease = next(repo.leases[o.values["lease"]] for n in repo.needs.values() if n.status == "open"
                         and n.kind == "lease" and n.subject_id == rec.id for o in n.options[:1])
            return (f"The {format_money(abs(rec.tx.amount), rec.tx.currency)} payment to {self._who(lease)} on "
                    f"{day_month(rec.tx.booked_on, repo.today())} is not the monthly payment in your "
                    f"{lease.contract.word} contract. I asked you about it.")
        found = self.line_of(rec)
        if found is None or rec.document_ids or rec.proof_evidence_ids or rec.id in repo.chases:
            return None
        lease, line = found
        who = self._who(lease)
        head = (f"The {lease.contract.m(abs(rec.tx.amount))} payment to {who} on "
                f"{day_month(rec.tx.booked_on, repo.today())} is {self._words(lease, line)}.")
        if rec.likely_document_ids:
            return f"{head} I found more than one invoice from {who} that could be it, so I won't pick one on a guess."
        if lease.contract.contract_suffices:
            return f"{head} I need {who}'s statement showing it; with it, the {lease.contract.word} contract covers it."
        return f"{head} I'm waiting for {who}'s invoice for it."

    def texts_for(self, rec: TxRecord) -> list[tuple[str, str]]:
        """What the contract says about the asset, for choosing a cost center (a vehicle's plate)."""
        found = self.line_of(rec)
        if found is None:
            return []
        c = found[0].contract
        plate = f"{pack_text('leases.plate_word', 'Registration')} {c.plate}" if c.plate else None
        text = " ".join(p for p in (c.asset, plate) if p)
        return [("the leasing contract", text)] if text else []

    def view(self, company_id: str) -> list[dict[str, Any]]:
        """Each lease of the company for the accountant: the asset, the payments, what is paid and still to come."""
        repo = self.repo
        today = repo.today()
        out = []
        for lease in sorted(repo.leases.values(), key=lambda x: x.document_id):
            if lease.company_id != company_id or not lease.contract.complete:
                continue
            c = lease.contract
            plan = c.schedule()
            monthly = [x for x in plan if not x.residual]
            due = [x for x in monthly if x.due <= today]
            to_come = [x for x in monthly if x.due > today]
            paid_closed = [n for n, t in lease.paid.items() if t in repo.transactions
                           and repo.items[repo.transactions[t].item_id].stage is Stage.CLOSED]
            remaining = sum((lease.expected(x) for x in to_come), Decimal(0)) + (c.residual or Decimal(0))
            out.append({
                "id": lease.document_id, "kind": c.word, "leasingCompany": self._who(lease),
                "contract": c.number, "asset": c.asset, "plate": c.plate, "currency": c.currency,
                "start": c.start.isoformat() if c.start else None, "term": c.term,
                "monthly": _num(c.gross), "monthlyBeforeVat": _num(c.net), "monthlyVat": _num(c.vat),
                "residual": _num(c.residual) if c.residual is not None else None,
                "paymentsDue": len(due), "paymentsMatched": len(lease.paid), "paymentsClosed": len(paid_closed),
                "remainingPayments": len(to_come), "remainingAmount": _num(remaining),
                "next": to_come[0].due.isoformat() if to_come else None,
                "evidence": list(repo.documents[lease.document_id].evidence_ids),
                "line": (f"{c.word.capitalize()} with {self._who(lease)} for {c.what}: {len(to_come)} of {c.term} "
                         f"monthly payments of {c.m(c.gross or Decimal(0))} still to come"
                         f"{', then ' + c.m(c.residual) + ' to keep it' if c.residual else ''}."),
            })
        return out


def _num(value: Decimal | None) -> float | None:
    return float(value.quantize(Decimal("0.01"))) if value is not None else None
