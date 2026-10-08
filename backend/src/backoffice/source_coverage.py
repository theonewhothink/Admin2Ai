"""What each source gave, in one plain line: the proof the owner asked for on Sources.

"We see all your card payments, and we found a matching invoice for each one." For every bank account and card,
each of its payments is in exactly one state (:data:`STATES`):

* ``proven``: closed with its evidence (an invoice, a receipt, the tax letter it pays; §3);
* ``not_needed``: no invoice is needed (money between your own accounts, a bank charge; §21);
* ``looking``: an invoice is needed and I am still looking for it (§22);
* ``needs_you``: it waits for the owner's answer (Needs you);
* ``personal``: the owner said it is not one of the companies' costs.

So for every account and card ``proven + not_needed + looking + needs_you + personal == payments``. A card's
payments are the card's, never also the bank account's: a payment made with a card that is added on its own is
counted for that card (its last 4 digits), whichever account the bank listed it under.

Mailboxes, cloud storage, accounting software and supplier websites say what was found there and when they were
last read; the accountant, the questions asked. The summary (:meth:`SourceCoverage.summary`) reads every source
at once and is never green while a connection is not being read or anything is still open (§47).

Pure Python over the repository (the live demo runs it in the browser).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from backoffice.domain.lifecycle import Stage
from backoffice.domain.models import DocumentType, SourceKind
from backoffice.language import since_phrase
from backoffice.learning import day_month, join_and
from backoffice.orchestrator import TZ, Account, ConnectorState, DocumentRecord, TxRecord

__all__ = ["STATES", "SourceCoverage"]

STATES = ("proven", "not_needed", "looking", "needs_you", "personal")
_COUNT_KEYS = {"proven": "proven", "not_needed": "notNeeded", "looking": "looking", "needs_you": "needsYou",
               "personal": "personal"}

# What a payment waits for, by what it needs (reconciliation.expected.EvidenceExpectation values).
_LOOKING_FOR = {
    "invoice": "the invoice", "sales_invoice": "the invoice", "receipt": "the receipt",
    "refund_or_credit_note": "the credit note", "tax_notice_or_proof": "the tax letter or payment proof",
    "payroll": "the payslip", "loan_statement": "the loan statement", "card_statement": "the card statement",
    "payout_report": "the payout report",
}
_INVOICES = frozenset({DocumentType.INVOICE, DocumentType.INVOICE_RECEIPT, DocumentType.DEBIT_NOTE})
_RECEIPTS = frozenset({DocumentType.SIMPLIFIED_INVOICE, DocumentType.RECEIPT})
_LETTER_WORDS = {"tax_authority": "Tax letter", "social_security": "Social Security letter"}
# Words a "no invoice needed" reason may start with, written in lower case after "No invoice needed: ".
_COMMON_START = frozenset("""money bank interest this these you it a an the transfer salary card tax payment
    payments rent fee fees charge refund your""".split())
_PLACE_WORDS = {"files": ("cloud storage", SourceKind.CLOUD_STORAGE),
                "accounting": ("accounting software", SourceKind.ACCOUNTING_SYSTEM)}


@dataclass(frozen=True)
class PaymentState:
    state: str
    text: str
    needs_id: str | None = None


def _plural(n: int, one: str, many: str) -> str:
    return one if n == 1 else many


def _kind_words(d: DocumentRecord) -> str:
    """What a document is, without its number: "Invoice", "Invoice-receipt", "Receipt", "Credit note"."""
    head = d.label.split(" · ")[0]
    number = d.document.invoice_number or ""
    return head[: -len(number)].strip() if number and head.endswith(number) else head


def _number(value: Decimal) -> int | float:
    """A JSON number for an amount (the web contract types money as ``number``)."""
    value = value.quantize(Decimal("0.01"))
    return int(value) if value == value.to_integral_value() else float(value)


class SourceCoverage:
    """Every source's coverage line, computed once per request from the repository as it is now."""

    def __init__(self, service: Any) -> None:
        self.svc = service
        self.repo = service.repo
        self.now = service._now()
        self.today: date = service._today()
        repo = self.repo
        # The card a payment was made with, when that card is a source of its own (its last 4 digits).
        cards: dict[str, list[Account]] = {}
        for a in repo.accounts.values():
            if a.card_last4:
                cards.setdefault(a.card_last4, []).append(a)
        self._owner: dict[str, str] = {}
        self._fed: dict[str, set[str]] = {}  # account or card -> the companies its payments are for
        for rec in repo.transactions.values():
            source = self._source_of(rec, cards)
            if source is not None:
                self._owner[rec.id] = source
                self._fed.setdefault(source, set()).add(rec.company_id)
        # Open questions, by what they are about (a payment, or the document matched to it).
        self._needs: dict[str, str] = {}
        for n in repo.open_needs():
            self._needs.setdefault(n.subject_id, n.id)
        self._states: dict[str, PaymentState] = {}
        self._first: date | None = min((r.tx.booked_on for r in repo.transactions.values()), default=None)

    # ----------------------------------------------------------------- payments

    def _source_of(self, rec: TxRecord, cards: Mapping[str, list[Account]]) -> str | None:
        account_id = rec.tx.account_id
        last4 = rec.tx.card_last4
        if last4 and last4 in cards:
            same = [a for a in cards[last4] if a.id == account_id]
            if same:
                return same[0].id
            if account_id not in self.repo.accounts or not self.repo.accounts[account_id].card_last4:
                bank = self.repo.accounts.get(account_id)
                for a in cards[last4]:  # the card of the same bank, then any card with those digits
                    if bank is not None and a.bank == bank.bank:
                        return a.id
                return cards[last4][0].id
        return account_id if account_id in self.repo.accounts else None

    def payments_of(self, account_id: str) -> list[TxRecord]:
        """The account's (or card's) payments, newest first."""
        recs = [r for r in self.repo.transactions.values() if self._owner.get(r.id) == account_id]
        return sorted(recs, key=lambda r: (r.tx.booked_on, r.id), reverse=True)

    def first_day(self) -> date | None:
        """The first day any payment was read for (what "since" means on Sources)."""
        return self._first

    def state(self, rec: TxRecord) -> PaymentState:
        found = self._states.get(rec.id)
        if found is None:
            found = self._states[rec.id] = self._state(rec)
        return found

    def _state(self, rec: TxRecord) -> PaymentState:
        repo = self.repo
        stage = repo.items[rec.item_id].stage
        if rec.private:
            return PaymentState("personal", "Personal: you said it is not for your companies.")
        if stage is Stage.CLOSED:
            return PaymentState("proven", self._proof_words(rec))
        if stage is Stage.NOT_REQUIRED:
            return PaymentState("not_needed", self._not_needed_words(rec))
        asked = self._needs.get(rec.id) or next((self._needs[d] for d in rec.document_ids if d in self._needs), None)
        if stage in (Stage.NEEDS_OWNER, Stage.CONFLICT) or asked is not None:
            return PaymentState("needs_you", "Needs your answer", asked)
        what = _LOOKING_FOR.get(rec.decision.expectation.value, "the document") if rec.decision else "the document"
        docs = [repo.documents[d] for d in rec.document_ids if d in repo.documents]
        if docs:
            return PaymentState("looking", f"{_kind_words(docs[0])} found, still checking it")
        since = rec.missing_since or rec.tx.booked_on
        return PaymentState("looking", f"Looking for {what} since {day_month(since, self.today)}")

    def _proof_words(self, rec: TxRecord) -> str:
        docs = [self.repo.documents[d] for d in rec.document_ids if d in self.repo.documents]
        if docs:
            return f"{_kind_words(docs[0])} found"
        for obligation in self.repo.obligations.values():
            if obligation.evidence_id in rec.proof_evidence_ids:
                return f"{_LETTER_WORDS.get(obligation.issuer, 'Letter')} found"
        return "Proof found"

    def _not_needed_words(self, rec: TxRecord) -> str:
        reason = ""
        if rec.decision is not None and not rec.decision.requires_document:
            reason = rec.decision.reason
        else:
            item = self.repo.items[rec.item_id]
            last = next((t for t in reversed(item.history) if t.to_stage is Stage.NOT_REQUIRED), None)
            reason = last.note if last is not None else ""
        first = reason.split(". ")[0].strip().rstrip(".")
        if not first or first.lower().startswith("no document") or first.lower().startswith("no invoice"):
            return "No invoice needed"
        word = first.split(" ", 1)[0]
        if word.lower() in _COMMON_START:
            return f"No invoice needed: {first[0].lower()}{first[1:]}"
        return f"No invoice needed. {first}."

    def counts(self, recs: Iterable[TxRecord]) -> dict[str, int]:
        out = {"payments": 0, **{_COUNT_KEYS[s]: 0 for s in STATES}}
        for rec in recs:
            out["payments"] += 1
            out[_COUNT_KEYS[self.state(rec).state]] += 1
        return out

    def _proof_noun(self, recs: Sequence[TxRecord]) -> str:
        """ "invoice" when every proven payment has an invoice or receipt; "invoice or proof" when one is proven
        by something else (the tax letter it pays, a payslip, a statement)."""
        for rec in recs:
            if self.state(rec).state != "proven":
                continue
            docs = [self.repo.documents[d] for d in rec.document_ids if d in self.repo.documents]
            if not any(d.document.doc_type in _INVOICES | _RECEIPTS for d in docs):
                return "invoice or proof"
        return "invoice"

    # ----------------------------------------------------------------- lines

    def account_line(self, account_id: str) -> dict[str, Any]:
        """ "6 payments since 1 September: all have their invoice." for one bank account or card."""
        recs = self.payments_of(account_id)
        counts = self.counts(recs)
        n = counts["payments"]
        first = self.first_day()
        since = f" since {day_month(first, self.today)}" if first else ""
        noun = self._proof_noun(recs)
        if n == 0:
            return {"text": f"No payments{since} yet." if since else "No payments yet.", "counts": counts}
        proven, none = counts["proven"], counts["notNeeded"]
        head = f"{n} {_plural(n, 'payment', 'payments')}{since}"
        if n == 1:
            said = {"proven": f"it has its {noun}", "not_needed": "it needs no invoice",
                    "looking": "I'm looking for its invoice", "needs_you": "it needs your answer",
                    "personal": "it is personal"}[self.state(recs[0]).state]
            return {"text": f"{head}: {said}.", "counts": counts}
        if proven == n:
            text = f"{head}: all have their {noun}."
        elif none == n:
            text = f"{head}: none needs an invoice."
        else:
            rest = self._rest(counts, proven_first=proven > 0)
            if proven:
                lead = f"{proven} of {n} {_plural(n, 'payment', 'payments')}{since} " \
                       f"{_plural(proven, 'has its', 'have their')} {noun}"
                text = f"{lead}; {join_and(rest)}."
            else:
                text = f"{head}: {join_and(rest)}."
        return {"text": text, "counts": counts}

    @staticmethod
    def _rest(counts: Mapping[str, int], *, proven_first: bool) -> list[str]:
        parts = []
        if counts["notNeeded"]:
            k = counts["notNeeded"]
            parts.append(f"{k} {_plural(k, 'needs', 'need')} none")
        if counts["looking"]:
            parts.append(f"I'm looking for {counts['looking']}")
        if counts["needsYou"]:
            k = counts["needsYou"]
            parts.append(f"{k} {_plural(k, 'needs', 'need')} your answer")
        if counts["personal"]:
            k = counts["personal"]
            parts.append(f"{k} {_plural(k, 'is', 'are')} personal")
        return parts

    def _last_read(self, c: ConnectorState) -> str:
        return since_phrase(c.last_synced_at, self.now, TZ) if c.last_synced_at else ""

    def _not_read(self, c: ConnectorState) -> str | None:
        """ "Not read since 14:42 yesterday" for a connection that stopped, None while it is read."""
        if c.healthy:
            return None
        if c.last_synced_at is None:
            return "Not read yet: waiting for you to sign in" if (self.svc.sign_in.get(c.id) or {}).get("pending") \
                else "Not read yet"
        return f"Not read since {self._last_read(c)}"

    def mailbox_line(self, c: ConnectorState, mailboxes: Sequence[ConnectorState]) -> dict[str, Any]:
        """ "Read since 1 June · 6 invoices and receipts found · last read 09:12"."""
        docs = self._mail_documents(c, mailboxes)
        parts = []
        stopped = self._not_read(c)
        if stopped is not None:
            parts.append(stopped)
        elif c.covered_from is not None:
            parts.append(f"Read since {day_month(c.covered_from.astimezone(TZ).date(), self.today)}")
        parts.append(self._found(docs))
        if stopped is None and c.last_synced_at is not None:
            parts.append(f"last read {self._last_read(c)}")
        return {"text": " · ".join(parts), "counts": {"documents": len(docs)}}

    def _mail_documents(self, c: ConnectorState, mailboxes: Sequence[ConnectorState]) -> list[DocumentRecord]:
        """The documents that came by email: per mailbox when the email says which one it was sent to, else all
        of them (one mailbox, or mail none of the mailboxes is named in)."""
        mail = [d for d in self.repo.documents.values()
                if d.origin == "email" or (d.origin == "link" and d.sender)]
        if len(mailboxes) <= 1:
            return mail
        mine = c.account.lower()
        others = {m.account.lower() for m in mailboxes}
        return [d for d in mail if mine in d.recipients or not (others & set(d.recipients))]

    @staticmethod
    def _found(docs: Sequence[DocumentRecord]) -> str:
        n = len(docs)
        if n == 0:
            return "nothing found yet"
        kinds = {d.document.doc_type for d in docs}
        if kinds <= _INVOICES:
            what = _plural(n, "invoice", "invoices")
        elif kinds <= _RECEIPTS:
            what = _plural(n, "receipt", "receipts")
        elif kinds <= _INVOICES | _RECEIPTS:
            what = "invoices and receipts"
        else:
            what = _plural(n, "document", "documents")
        return f"{n} {what} found"

    def place_line(self, c: ConnectorState, same_kind: Sequence[ConnectorState]) -> dict[str, Any]:
        """Cloud storage or accounting software: what was found there, and when it was last read."""
        words, kind = _PLACE_WORDS[c.kind]
        docs = [d for d in self.repo.documents.values() if self._has_source(d, kind)]
        if not docs:
            parts = ["Searched when an invoice is missing", "nothing found there yet"]
        else:
            parts = [f"{self._found(docs)} {'across your ' + words if len(same_kind) > 1 else 'there'}"]
        return {"text": self._with_reading(c, parts), "counts": {"documents": len(docs)}}

    def portal_line(self, c: ConnectorState) -> dict[str, Any]:
        """A supplier's website: the invoices fetched there (its suppliers' documents read from a website)."""
        repo = self.repo
        suppliers = {sid for sid, w in repo.supplier_websites.items() if getattr(w, "connection_id", None) == c.id}
        docs = [d for d in repo.documents.values()
                if d.supplier_id in suppliers and self._has_source(d, SourceKind.SUPPLIER_PORTAL)]
        if docs:
            parts = [f"{len(docs)} {_plural(len(docs), 'invoice', 'invoices')} fetched there"]
        else:
            parts = ["I sign in when an invoice is missing", "nothing fetched yet"]
        return {"text": self._with_reading(c, parts), "counts": {"documents": len(docs)}}

    def _with_reading(self, c: ConnectorState, parts: Sequence[str]) -> str:
        stopped = self._not_read(c)
        if stopped is not None:
            rest = [f"{p[0].lower()}{p[1:]}" for p in parts]
            return " · ".join([stopped, *rest])
        if c.last_synced_at is not None:
            return " · ".join([*parts, f"last read {self._last_read(c)}"])
        return " · ".join(parts)

    def _has_source(self, d: DocumentRecord, kind: SourceKind) -> bool:
        for e in d.evidence_ids:
            try:
                if self.repo.evidence(e).source_kind is kind:
                    return True
            except Exception:  # an original that is not on file: nothing to say about it
                continue
        return False

    def accountant_line(self, c: ConnectorState) -> dict[str, Any]:
        questions = list(self.repo.accountant_questions.values())
        n = len(questions)
        answered = sum(1 for q in questions if getattr(q, "status", "") == "answered")
        if n == 0:
            text = "No questions from them yet"
        elif answered == n:
            text = f"{n} {_plural(n, 'question', 'questions')} from them, all answered"
        else:
            text = f"{n} {_plural(n, 'question', 'questions')} from them: {answered} answered, {n - answered} open"
        if c.last_synced_at is not None:
            text += f" · last in touch {self._last_read(c)}"
        return {"text": text, "counts": {"questions": n, "answered": answered}}

    # ----------------------------------------------------------------- the summary

    def summary(self, reading: Mapping[str, int], stale: Sequence[ConnectorState],
                unlinked: Sequence[str] = ()) -> dict[str, Any]:
        """ "I read 1 mailbox, 3 bank accounts and 4 cards for your 3 companies." and the verdict on every payment:
        "I checked all 14 payments since 1 September: 7 have their invoice, 5 need none, ..."."""
        words = (("email", "mailbox", "mailboxes"), ("banks", "bank account", "bank accounts"),
                 ("cards", "card", "cards"), ("files", "cloud storage account", "cloud storage accounts"),
                 ("accounting", "accounting program", "accounting programs"),
                 ("portals", "supplier website", "supplier websites"))
        parts = [f"{reading[k]} {_plural(reading[k], one, many)}" for k, one, many in words if reading.get(k)]
        companies = list(self.repo.companies.values())
        whose = (f" for {companies[0].name}" if len(companies) == 1 else
                 f" for your {len(companies)} companies" if companies else "")
        reads = f"I read {join_and(parts)}{whose}." if parts else \
            "I don't read anything yet. Add your email and your bank below."
        recs = list(self.repo.transactions.values())
        counts = self.counts(recs)
        verdict = self._verdict(recs, counts)
        lines = [self._stale_sentence(c) for c in stale]
        if unlinked:
            lines.append(f"{join_and(list(unlinked))} {_plural(len(unlinked), 'is', 'are')} not linked to "
                         f"{_plural(len(unlinked), 'its bank', 'their banks')} yet, so I can't see "
                         f"{_plural(len(unlinked), 'its', 'their')} payments.")
        open_items = counts["looking"] + counts["needsYou"]
        tone = "good" if not lines and not open_items and parts else "attention"
        coverage = " ".join([*lines, verdict]) if verdict else " ".join(lines)
        return {"text": reads, "coverage": coverage, "tone": tone, "counts": counts}

    def _stale_sentence(self, c: ConnectorState) -> str:
        if c.last_synced_at is None:
            return f"{c.name} has not been read yet."
        return f"{c.name} has not been read since {self._last_read(c)}."

    def _verdict(self, recs: Sequence[TxRecord], counts: Mapping[str, int]) -> str:
        n = counts["payments"]
        first = self.first_day()
        since = f" since {day_month(first, self.today)}" if first else ""
        if n == 0:
            return "No payments to check yet."
        noun = self._proof_noun(recs)
        if n == 1:
            state = self.state(recs[0]).state
            said = {"proven": f"it has its {noun}", "not_needed": "it needs no invoice",
                    "looking": "I'm still looking for its invoice", "needs_you": "it needs your answer",
                    "personal": "it is personal"}[state]
            return f"I checked your one payment{since}: {said}."
        parts = []
        if counts["proven"]:
            k = counts["proven"]
            parts.append(f"{k} {_plural(k, 'has its', 'have their')} {noun}")
        if counts["notNeeded"]:
            k = counts["notNeeded"]
            parts.append(f"{k} {_plural(k, 'needs', 'need')} none")
        if counts["looking"]:
            parts.append(f"{counts['looking']} I'm still looking for")
        if counts["needsYou"]:
            k = counts["needsYou"]
            parts.append(f"{k} {_plural(k, 'needs', 'need')} your answer")
        if counts["personal"]:
            k = counts["personal"]
            parts.append(f"{k} {_plural(k, 'is', 'are')} personal")
        if counts["proven"] == n:
            return f"I checked all {n} payments{since}: every one has its {noun}."
        return f"I checked all {n} payments{since}: {join_and(parts)}."

    # ----------------------------------------------------------------- one account's payments

    def payment_rows(self, account_id: str) -> list[dict[str, Any]]:
        out = []
        for rec in self.payments_of(account_id):
            s = self.state(rec)
            detail = f"/payments/detail?id={rec.id}"
            href = (f"/needs-you#{s.needs_id}" if s.needs_id else "/needs-you") if s.state == "needs_you" else detail
            out.append({
                "id": rec.id, "date": rec.tx.booked_on.isoformat(), "merchant": self.svc.orchestrator.merchant_name(rec.tx),
                "amount": _number(abs(rec.tx.amount)),
                "currency": rec.tx.currency, "direction": "in" if rec.tx.amount > 0 else "out",
                "companyName": "" if rec.private else (self.repo.company_name(rec.company_id) or ""),
                "state": s.state, "stateText": s.text, "href": href, "detailHref": detail,
            })
        return out

    # ----------------------------------------------------------------- the companies

    def company_sources(self, company_id: str, connectors: Iterable[ConnectorState]) -> list[dict[str, Any]]:
        """The sources that feed one company: its mailboxes, bank accounts, cards, cloud storage, accounting
        software and supplier websites."""
        repo = self.repo
        out: list[dict[str, Any]] = []
        conns = list(connectors)
        for c in conns:
            if c.kind == "email" and company_id in c.company_ids:
                out.append({"id": c.id, "kind": "email", "name": c.account})
        for a in repo.accounts.values():
            if a.holder_id != company_id and company_id not in self._fed.get(a.id, ()):
                continue
            if a.card_last4:
                out.append({"id": a.id, "kind": "card", "name": f"Card •••• {a.card_last4}"})
            elif a.iban:
                out.append({"id": a.id, "kind": "bank", "name": f"{a.bank} •••• {a.iban[-4:]}"})
        for c in conns:
            if c.kind in ("files", "accounting", "portal") and company_id in c.company_ids:
                out.append({"id": c.id, "kind": c.kind, "name": c.name})
        order = {"email": 0, "bank": 1, "card": 2, "files": 3, "accounting": 4, "portal": 5}
        return sorted(out, key=lambda s: order[s["kind"]])
