"""Employee cards and staff expenses: cardholders, receipts asked from them, expense claims.

Cardholders
    A company card (a card account, or a card ending seen on a bank account) can belong to an employee:
    name, email, optional phone. The owner says so (``POST /api/employees``, or with the card), or the
    bank's card details name the cardholder (``BankRow.cardholder``) and the card is learned for that
    person. The owner's word always wins over what a bank line says.

Receipts from the cardholder (§11, §22, §25)
    A payment on an employee's card that still misses its receipt or invoice is asked from that employee,
    never from the owner and never from the supplier: one plain email through the send path
    ("Hi Rui, please send the receipt for €48.20 at Leroy Merlin on 18 September — reply with a photo or
    forward it."). Like every email the back office writes, it counts as asked only once a transport
    accepted it. Reminders follow the supplier-chase cadence (``missing.ReminderPolicy``: after 6 days,
    then 4 more, at most 2, never on a weekend), in the same thread. A reply is matched to its request by
    In-Reply-To, References or the subject reference (``missing.match_reply``); its attachment is read
    like any document and matched to the payment. Only when the employee has not sent it after the
    reminders does the owner see one plain line ("Rui hasn't sent the receipt for ..."): never before.

Expense claims
    An employee (or the owner on their behalf) sends a receipt they paid with their own money. It becomes
    a claim for that employee (company, amount, the receipt as evidence) that the owner approves with one
    tap (owner approval level, ``ActionKind.EXPENSE_CLAIM_APPROVAL``: never automatic). The later transfer
    to the employee of exactly what is owed closes it. A receipt the company already paid (a payment on
    file for it) is never a claim. The receipt is the cost, counted once; the transfer paying it back is
    not a second cost (backoffice.spending).

Pure Python over the orchestrator's records, like the other agents.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from backoffice.domain.lifecycle import Stage
from backoffice.domain.models import DocumentType, Quality, TransactionKind
from backoffice.fraud import normalize_iban
from backoffice.learning import day_month, display_name, fold, format_money
from backoffice.learning.plain import count_phrase
from backoffice.missing import (
    ChaseMessage,
    ChaseThread,
    InboundEmail,
    Language,
    MatchMethod,
    ReminderPolicy,
    ReminderStep,
    match_reply,
    next_reminder,
    thread_token,
)
from backoffice.orchestrator import (
    CHASE_AFTER_DAYS,
    MESSAGE_ID_DOMAIN,
    OWNER_ACTOR,
    TZ,
    AnswerOutcome,
    CheckOption,
    DocumentRecord,
    NeedsYouRecord,
    OutgoingMessage,
    TxRecord,
    _Agent,
    _slug,
    _unique_id,
)
from backoffice.policy import ActionContext, ActionKind, Approval, authorize
from backoffice.policy.actions import Requirement
from backoffice.reconciliation import EvidenceExpectation, ExpectationDecision
from backoffice.reconciliation.expected import ACCEPTED_DOCUMENT_TYPES

__all__ = ["Employee", "ExpenseClaim", "ReceiptRequest", "StaffAgent", "same_person"]

_ZERO = Decimal("0")
# What a card payment's receipt can be: an invoice, invoice-receipt, simplified invoice or receipt (§21).
_RECEIPT_TYPES = ACCEPTED_DOCUMENT_TYPES[EvidenceExpectation.RECEIPT]
_ASKABLE = frozenset({EvidenceExpectation.INVOICE, EvidenceExpectation.RECEIPT})
_RECEIPT_WINDOW_DAYS = 7  # a receipt dated this close to the card payment can be its receipt
_EMAIL = re.compile(r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]+@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
                    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+")
CLAIM_STATUS_LABELS = {"waiting": "Waiting for approval", "approved": "Approved, to be paid back",
                       "paid": "Paid back", "declined": "Not paid back"}


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", fold(text or ""))


def same_person(a: str, b: str) -> bool:
    """Two ways of writing one person's name ("RUI COSTA", "Rui Miguel Costa"): the same words, or every
    word of the shorter one (at least first and last name) in the longer one. Never a single shared word."""
    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return False
    if wa == wb:
        return True
    short, long_ = (wa, wb) if len(wa) <= len(wb) else (wb, wa)
    return len(short) >= 2 and set(short) <= set(long_)


def plain_email(value: str | None) -> bool:
    """One plain address (no display name, no list): safe as an email header."""
    return bool(value) and _EMAIL.fullmatch(value or "") is not None


def person_name(raw: str) -> str:
    """A name as people write it: 'RUI COSTA' from a bank line becomes 'Rui Costa'."""
    name = " ".join((raw or "").split())[:120]
    return " ".join(w.capitalize() for w in name.split()) if name.isupper() else name


# --------------------------------------------------------------------------- records


@dataclass
class Employee:
    """Someone who works for the business: holds company cards, may pay expenses themselves."""

    id: str
    name: str
    email: str | None = None
    phone: str | None = None
    company_id: str | None = None
    cards: tuple[str, ...] = ()  # endings (last 4 digits) of the company cards they hold
    iban: str | None = None  # where they are paid back, when known
    learned_from: tuple[str, ...] = ()  # evidence of bank rows naming them as the cardholder (empty: set by you)
    not_cards: tuple[str, ...] = ()  # cards the owner took from them: never learned for them again

    @property
    def first_name(self) -> str:
        parts = self.name.split()
        return parts[0] if parts else self.name


@dataclass
class ReceiptRequest:
    """The receipt for one payment on an employee's card, asked from that employee.

    ``status``: ``asking`` (written, sent, reminded), ``escalated`` (not sent after the reminders: the
    owner's one line), ``owner`` (the owner said they would send it), ``received`` (the payment has its
    document) or ``cancelled`` (the payment was set aside). ``thread`` holds only what a transport accepted.
    """

    tx_id: str
    employee_id: str
    company_id: str
    token: str
    written_at: datetime
    messages: list[ChaseMessage] = field(default_factory=list)  # the request, then reminders, as written
    outbox_ids: list[str] = field(default_factory=list)
    thread: ChaseThread | None = None
    status: str = "asking"
    replies: list[str] = field(default_factory=list)  # evidence ids of the employee's replies
    offered: list[str] = field(default_factory=list)  # documents the employee sent in reply
    needs_id: str | None = None
    escalated_at: datetime | None = None
    rounds: int = 0  # times the owner said "ask again"
    received_at: datetime | None = None

    @property
    def asked(self) -> bool:
        return self.thread is not None

    @property
    def reminders_sent(self) -> int:
        return self.thread.reminders_sent if self.thread is not None else 0


@dataclass
class ExpenseClaim:
    """A receipt an employee paid with their own money, for the company to pay back.

    ``status``: ``waiting`` (for the owner's OK), ``approved`` (to be paid back), ``paid`` (the transfer
    to the employee closed it) or ``declined`` (not a company cost).
    """

    id: str
    employee_id: str
    company_id: str
    document_id: str
    amount: Decimal
    currency: str
    spent_on: date
    merchant: str
    evidence_ids: tuple[str, ...]
    submitted_by: str  # "employee" | "owner"
    submitted_at: datetime
    status: str = "waiting"
    needs_id: str | None = None
    approval_evidence_id: str | None = None
    approval_id: str | None = None
    approved_at: datetime | None = None
    paid_by_tx_id: str | None = None
    paid_at: datetime | None = None

    @property
    def fingerprint(self) -> str:
        """The facts the owner approves: who, how much, which receipt (a change voids the approval)."""
        facts = f"{self.id}|{self.employee_id}|{self.company_id}|{self.amount}|{self.currency}|{self.document_id}"
        return hashlib.sha256(facts.encode()).hexdigest()[:32]


# --------------------------------------------------------------------------- the agent


class StaffAgent(_Agent):
    """Cardholders, receipts asked from them, and expense claims (module docstring).

    Does nothing at all for a business without employees or expense claims (the demo).
    """

    name = "staff"
    REQUEST = "employee_receipt_request"
    REMINDER = "employee_receipt_reminder"
    MESSAGE_KINDS = frozenset({REQUEST, REMINDER})
    NEEDS_KINDS = ("receipt", "expense_claim")

    @property
    def active(self) -> bool:
        return bool(self.repo.employees or self.repo.expense_claims)

    # ----------------------------------------------------------------- who holds which card

    def holder_of(self, last4: str | None) -> Any:
        if not last4:
            return None
        return next((e for e in sorted(self.repo.employees.values(), key=lambda e: e.id) if last4 in e.cards), None)

    def cardholder(self, rec: TxRecord) -> Employee | None:
        """The employee whose card made this payment, if any."""
        if not self.repo.employees:
            return None
        return self.holder_of(rec.tx.card_last4)

    def asks_cardholder(self, rec: TxRecord) -> bool:
        """This payment's receipt is asked from its cardholder (so never from its supplier)."""
        emp = self.cardholder(rec)
        return emp is not None and plain_email(emp.email)

    def employee_by_email(self, email: str | None) -> Employee | None:
        wanted = (email or "").strip().lower()
        if not wanted:
            return None
        return next((e for e in self.repo.employees.values() if (e.email or "").lower() == wanted), None)

    def set_employee(self, *, name: str, email: str | None = None, phone: str | None = None,
                     company_id: str | None = None, cards: tuple[str, ...] | None = None, iban: str | None = None,
                     employee_id: str | None = None) -> Employee:
        """Add or update an employee (the owner's word). ``cards``: exactly the cards they hold (None: as
        before). A card belongs to one person: giving it to this employee takes it from anyone else, including
        a cardholder learned from the bank; a card taken from someone is never learned for them again."""
        repo = self.repo
        emp = repo.employees.get(employee_id or "")
        if emp is None:
            emp = Employee(id=_unique_id(repo.employees, "emp_" + _slug(name)), name=name)
            repo.employees[emp.id] = emp
        emp.name = name
        if email is not None:
            emp.email = email or None
        if phone is not None:
            emp.phone = phone or None
        if company_id is not None:
            emp.company_id = company_id or None
        if iban is not None:
            emp.iban = normalize_iban(iban) if iban else None
        if cards is not None:
            wanted = tuple(dict.fromkeys(cards))
            for other in repo.employees.values():
                taken = tuple(c for c in other.cards if c in wanted and other.id != emp.id)
                if taken:
                    other.cards = tuple(c for c in other.cards if c not in taken)
                    other.not_cards = tuple(dict.fromkeys((*other.not_cards, *taken)))
            dropped = tuple(c for c in emp.cards if c not in wanted)
            emp.cards = wanted
            emp.not_cards = tuple(dict.fromkeys(c for c in (*emp.not_cards, *dropped) if c not in wanted))
        self.log("set_employee", subject_id=emp.id, actor=OWNER_ACTOR,
                 values={"cards": list(emp.cards), "company": emp.company_id or "", "has_email": bool(emp.email)})
        return emp

    def learn(self, now: datetime) -> int:
        """Cards the bank's card details name the holder of: the card is that person's (§6 learning).

        A card someone already holds, the owner's own card and a name that fits several employees are left
        alone: nothing is decided on a guess.
        """
        repo = self.repo
        learned = 0
        for rec in sorted(repo.transactions.values(), key=lambda r: r.id):
            name, last4 = rec.cardholder, rec.tx.card_last4
            if not name or not last4 or self.holder_of(last4) is not None:
                continue
            if same_person(name, repo.owner.full_name):
                continue  # the owner's own card: asking them is asking the owner
            matches = [e for e in repo.employees.values() if same_person(e.name, name)]
            if len(matches) > 1 or any(last4 in e.not_cards for e in matches):
                continue  # several people fit, or you took this card from them: never on a guess
            if matches:
                emp = matches[0]
            else:
                emp = Employee(id=_unique_id(repo.employees, "emp_" + _slug(name)), name=person_name(name),
                               company_id=rec.holder_id)
                repo.employees[emp.id] = emp
            emp.cards = (*emp.cards, last4)
            emp.learned_from = (*emp.learned_from, rec.evidence_id)
            self.log("learn_cardholder", subject_id=emp.id, evidence_ids=[rec.evidence_id],
                     values={"card": last4, "name": emp.name})
            tail = "" if emp.email else f" Add {emp.first_name}'s email so I can ask {emp.first_name} for its receipts."
            self.o.activity(now, "learned", f"Learned from your bank: card •••• {last4} is {emp.name}'s card.{tail}",
                            rec.holder_id, evidence_ids=[rec.evidence_id], tag="staff")
            learned += 1
        return learned

    # ----------------------------------------------------------------- what the owner reads about a payment

    def _facts(self, rec: TxRecord) -> tuple[str, str, str]:
        amount = format_money(abs(rec.tx.amount), rec.tx.currency)
        merchant = " ".join(self.o.merchant_name(rec.tx).split())
        return amount, merchant, day_month(rec.tx.booked_on, self.repo.today())

    def plan(self, rec: TxRecord) -> str | None:
        """The next step for a payment on an employee's card without its receipt (None: not one)."""
        emp = self.cardholder(rec)
        if emp is None:
            return None
        amount, merchant, when = self._facts(rec)
        first = emp.first_name
        req = self.repo.receipt_requests.get(rec.id)
        if req is not None and (req.status == "cancelled" or req.employee_id != emp.id):
            req = None  # asked from someone whose card it no longer is
        if req is None:
            if not plain_email(emp.email):
                return (f"The {amount} payment at {merchant} on {when} was made with {emp.name}'s card. "
                        f"Add {first}'s email so I can ask {first} for the receipt.")
            return (f"The {amount} payment at {merchant} on {when} was made with {emp.name}'s card. If the receipt "
                    f"does not arrive, I will ask {first} for it.")
        if req.status == "escalated":
            return f"{first} hasn't sent the receipt for {amount} at {merchant} on {when}. I asked you what to do."
        if req.status == "owner":
            return (f"You said you would send the receipt for the {amount} payment at {merchant} on {when}. "
                    "I will match it when it arrives.")
        if not req.asked:
            return (f"I wrote to {first} asking for the receipt for the {amount} payment at {merchant} on {when}. "
                    "It is waiting to be sent.")
        text = f"I asked {first} for the receipt for the {amount} payment at {merchant} on {when}."
        if req.reminders_sent:
            text += f" I sent {count_phrase(req.reminders_sent, 'reminder')}."
        if self._waiting(req):
            text += " A reminder is waiting to be sent."
        return text

    # ----------------------------------------------------------------- asking the cardholder (§22)

    def follow_up(self, now: datetime) -> None:
        """Every run: receipts that arrived, claims to approve, new requests, reminders, the owner's one line."""
        if not self.active:
            return
        self._review(now)
        self._ask_approvals(now)
        self._ask_all(now)
        self._remind_all(now)

    def _askable(self, rec: TxRecord, today: date) -> bool:
        item = self.repo.items[rec.item_id]
        if rec.tx.amount >= 0 or rec.document_ids or rec.private or rec.proof_evidence_ids or rec.claim_ids:
            return False
        if item.is_done or item.stage in (Stage.NEEDS_OWNER, Stage.CONFLICT) or rec.likely_document_ids:
            return False  # decided, waiting for an answer, or a likely document is being confirmed
        if rec.decision is None or not rec.decision.requires_document or rec.decision.expectation not in _ASKABLE:
            return False  # a refund, payout or tax payment is not a receipt the cardholder has
        return (today - rec.tx.booked_on).days >= CHASE_AFTER_DAYS  # receipts often follow by a day or two

    def _ask_all(self, now: datetime) -> list[str]:
        repo = self.repo
        today = now.astimezone(TZ).date()
        written: list[str] = []
        for rec in sorted(repo.transactions.values(), key=lambda r: (r.tx.booked_on, r.id)):
            emp = self.cardholder(rec)
            earlier = repo.receipt_requests.get(rec.id)
            if earlier is not None and (earlier.status != "cancelled" or emp is None or earlier.employee_id == emp.id):
                continue  # asked already (a card that went to someone else is asked again, from them)
            if emp is None or not plain_email(emp.email) or not self._askable(rec, today):
                continue
            # Asking your own employee for a receipt is a routine reminder (§25: fully automatic).
            decision = authorize(ActionKind.REMINDER, repo.policy, ActionContext(
                tenant_id=repo.tenant_id, entity_id=rec.company_id, subject_id=rec.id))
            self.log("authorize_receipt_request", subject_id=rec.id, evidence_ids=[rec.evidence_id],
                     response={"allowed": decision.allowed_now, "reason": decision.reason_plain})
            if not decision.allowed_now:
                continue
            req = ReceiptRequest(tx_id=rec.id, employee_id=emp.id, company_id=rec.company_id,
                                 token=thread_token(repo.tenant_id, f"receipt:{rec.id}"), written_at=now)
            repo.receipt_requests[rec.id] = req
            self._write(req, rec, emp, now)
            written.append(rec.id)
        return written

    def _message_id(self, token: str, number: int, today: date) -> str:
        return f"<receipt-{token.lower()}-{number}-{today:%Y%m%d}@{MESSAGE_ID_DOMAIN}>"

    def _write(self, req: ReceiptRequest, rec: TxRecord, emp: Employee, now: datetime) -> OutgoingMessage:
        """The request, or (once the request went out) a reminder in the same thread."""
        today = now.astimezone(TZ).date()
        amount, merchant, when = self._facts(rec)
        ask = f"please send the receipt for {amount} at {merchant} on {when} — reply with a photo or forward it."
        company = self.repo.company_name(req.company_id) or ""
        tail = f"It was paid with card •••• {rec.tx.card_last4}.\n\nThank you,\n{company}".rstrip()
        thread = req.thread
        if thread is None:
            message = ChaseMessage(
                to=str(emp.email), subject=f"Receipt for {amount} at {merchant} on {when} (Ref. {req.token})",
                body=f"Hi {emp.first_name}, {ask}\n\n{tail}", language=Language.EN, token=req.token,
                message_id=self._message_id(req.token, 0, today))
            kind, headers = self.REQUEST, (("Message-ID", message.message_id),)
        else:
            number = thread.reminders_sent + 1
            subject = thread.subject if thread.subject.lower().startswith("re:") else f"Re: {thread.subject}"
            message = ChaseMessage(
                to=str(emp.email), subject=subject, body=f"Hi {emp.first_name}, a reminder: {ask}\n\n{tail}",
                language=Language.EN, token=req.token, message_id=self._message_id(req.token, number, today),
                in_reply_to=thread.sent[-1].message_id, references=thread.message_ids, reminder_number=number)
            kind = self.REMINDER
            headers = (("Message-ID", message.message_id), ("In-Reply-To", thread.sent[-1].message_id),
                       ("References", " ".join(thread.message_ids)))
        out = self.o.write_email(kind, rec.id, req.company_id, message.to, message.subject, message.body, now,
                                 headers=headers)
        req.messages.append(message)
        req.outbox_ids.append(out.id)
        self.log("write_receipt_request" if kind == self.REQUEST else "write_receipt_reminder", subject_id=rec.id,
                 evidence_ids=[rec.evidence_id], values={"employee": emp.id, "outbox_id": out.id,
                                                          "reminder": message.reminder_number})
        return out

    def _waiting(self, req: ReceiptRequest) -> bool:
        return any(not self.repo.outbox[m].sent for m in req.outbox_ids if m in self.repo.outbox)

    def _request_for(self, message: OutgoingMessage) -> tuple[ReceiptRequest, ChaseMessage] | None:
        req = self.repo.receipt_requests.get(message.subject_id)
        if req is None or message.id not in req.outbox_ids:
            return None
        return req, req.messages[req.outbox_ids.index(message.id)]

    def sent(self, message: OutgoingMessage, at: datetime) -> None:
        """A transport accepted the request or a reminder: now the employee has been asked."""
        found = self._request_for(message)
        if found is None:
            return
        req, chase = found
        on = at.astimezone(TZ).date()
        req.thread = ChaseThread.start(chase, on) if req.thread is None else req.thread.with_sent(chase, on)
        rec = self.repo.transactions[req.tx_id]
        emp = self.repo.employees.get(req.employee_id)
        first = emp.first_name if emp else "your employee"
        amount, merchant, _ = self._facts(rec)
        text = (f"Asked {first} for the receipt for the {amount} {merchant} payment." if chase.reminder_number == 0
                else f"Reminded {first} about the receipt for the {amount} {merchant} payment.")
        self.o.activity(at, "chased", text, req.company_id, evidence_ids=[rec.evidence_id], tag="staff")
        self.log("request_receipt" if chase.reminder_number == 0 else "remind_receipt", subject_id=rec.id,
                 evidence_ids=[rec.evidence_id], values={"to": chase.to, "subject": chase.subject},
                 response={"message_id": chase.message_id})

    def waiting_line(self, message: OutgoingMessage) -> tuple[str, list[str]]:
        """What the Activity feed says about a request written but not sent yet."""
        found = self._request_for(message)
        if found is None:
            return "Wrote an email. It is waiting to be sent.", []
        req, chase = found
        rec = self.repo.transactions[req.tx_id]
        emp = self.repo.employees.get(req.employee_id)
        first = emp.first_name if emp else "your employee"
        amount, merchant, _ = self._facts(rec)
        what = (f"Wrote to {first} asking for the receipt for the {amount} {merchant} payment." if
                chase.reminder_number == 0 else
                f"Wrote {first} a reminder about the receipt for the {amount} {merchant} payment.")
        return f"{what} It is waiting to be sent.", [rec.evidence_id]

    def _remind_all(self, now: datetime) -> None:
        """Reminders on the supplier-chase cadence; after the last one, one line for the owner."""
        repo = self.repo
        today = now.astimezone(TZ).date()
        for req in sorted(repo.receipt_requests.values(), key=lambda r: r.tx_id):
            if req.status != "asking" or req.thread is None or self._waiting(req):
                continue
            rec = repo.transactions.get(req.tx_id)
            emp = repo.employees.get(req.employee_id)
            if rec is None or emp is None or not plain_email(emp.email):
                continue
            policy = ReminderPolicy(max_reminders=2 + 2 * req.rounds)
            step = next_reminder(req.thread, policy, today)
            if step.step is ReminderStep.SEND_REMINDER:
                self._write(req, rec, emp, now)
            elif step.step is ReminderStep.ESCALATE:
                self._escalate(req, rec, emp, now)

    def _escalate(self, req: ReceiptRequest, rec: TxRecord, emp: Employee, now: datetime) -> None:
        """The employee has not sent it after the reminders: the owner's one plain line (§37)."""
        repo = self.repo
        amount, merchant, when = self._facts(rec)
        first = emp.first_name
        prompt = (f"{first} replied, but I still don't have the receipt for {amount} at {merchant} on {when}."
                  if req.replies else f"{first} hasn't sent the receipt for {amount} at {merchant} on {when}.")
        asked = req.thread.sent[0].sent_on if req.thread is not None else now.date()
        why = (f"It was paid with card •••• {rec.tx.card_last4}, {emp.name}'s card.",
               f"I asked {first} on {day_month(asked, repo.today())} and sent "
               f"{count_phrase(req.reminders_sent, 'reminder')}.")
        needs_id = _unique_id(repo.needs, f"nd_receipt_{_slug(first)}_{int(abs(rec.tx.amount))}")
        repo.needs[needs_id] = NeedsYouRecord(
            id=needs_id, kind="receipt", subject_type="transaction", subject_id=rec.id, item_id=rec.item_id,
            company_id=rec.company_id, created_at=now, prompt=prompt, why=why,
            options=(CheckOption("remind", f"Ask {first} again"), CheckOption("owner", "I'll send it myself")))
        req.status, req.needs_id, req.escalated_at = "escalated", needs_id, now
        self.o.advance(repo.items[rec.item_id], Stage.NEEDS_OWNER, [rec.evidence_id], agent=self.name, note=prompt)
        self.log("escalate_receipt", subject_id=rec.id, evidence_ids=[rec.evidence_id],
                 values={"employee": emp.id, "reminders": req.reminders_sent}, response={"needs_you": needs_id})

    # ----------------------------------------------------------------- the employee's answer

    def reply_to(self, parsed: Any, message_evidence_id: str, at: datetime) -> ReceiptRequest | None:
        """The request an inbound email answers, by its thread (In-Reply-To, References, subject reference).

        A reply found only by the reference in its subject must come from the employee's own address.
        """
        repo = self.repo
        open_requests = [r for r in repo.receipt_requests.values()
                         if r.thread is not None and r.status in ("asking", "escalated", "owner")]
        if not open_requests:
            return None
        sender = (parsed.sender.address if parsed.sender else "").strip().lower()
        inbound = InboundEmail(from_address=sender, subject=parsed.subject or "",
                               message_id=parsed.thread.message_id, in_reply_to=parsed.thread.in_reply_to,
                               references=parsed.thread.references)
        found = match_reply([r.thread for r in open_requests if r.thread is not None], inbound)
        if found is None:
            return None
        req = next(r for r in open_requests if r.thread is not None and r.thread.token == found.thread.token)
        emp = repo.employees.get(req.employee_id)
        if found.method is MatchMethod.SUBJECT_REFERENCE and (emp is None or sender != (emp.email or "").lower()):
            self.log("receipt_reply_refused", subject_id=req.tx_id, evidence_ids=[message_evidence_id],
                     response={"reason": "subject reference from another address"})
            return None
        req.replies.append(message_evidence_id)
        self.log("receipt_reply", subject_id=req.tx_id, evidence_ids=[message_evidence_id],
                 values={"method": found.method.value, "employee": req.employee_id})
        return req

    def replied(self, req: ReceiptRequest, document_ids: list[str], at: datetime) -> None:
        """The documents that came with the employee's reply: offered for their payment."""
        new = [d for d in dict.fromkeys(document_ids) if d not in req.offered]
        req.offered += new
        if new:
            return
        rec = self.repo.transactions[req.tx_id]
        emp = self.repo.employees.get(req.employee_id)
        amount, merchant, _ = self._facts(rec)
        self.o.activity(at, "checked", f"{emp.first_name if emp else 'Your employee'} replied about the {amount} "
                        f"{merchant} payment, but I found no receipt in the reply.", req.company_id,
                        evidence_ids=req.replies[-1:], tag="staff")

    def fits(self, rec: TxRecord, doc: DocumentRecord) -> bool:
        """Can this document be this card payment's receipt? Same amount and currency, dated close to it,
        a receipt or invoice nobody else has, not paid in cash, and its details not in conflict."""
        d = doc.document
        if doc.matched_tx_ids or doc.on_hold or doc.supporting or doc.sales or doc.paid_in_cash:
            return False
        if doc.claim_id is not None or d.gross_amount is None or d.quality is Quality.RED:
            return False
        if d.doc_type not in _RECEIPT_TYPES or d.doc_type is DocumentType.CREDIT_NOTE:
            return False
        if d.currency.strip().upper() != rec.tx.currency.strip().upper() or abs(d.gross_amount) != abs(rec.tx.amount):
            return False
        return d.issue_date is None or abs((rec.tx.booked_on - d.issue_date).days) <= _RECEIPT_WINDOW_DAYS

    def link(self, rec: TxRecord, doc: DocumentRecord, emp: Employee, how: str) -> None:
        """The receipt the cardholder sent, kept with the payment it was sent for. The bank's amount checks
        the receipt's total (§19); the payment closes only when the receipt is verified (§3)."""
        amount, _, _ = self._facts(rec)
        rec.document_ids = [doc.id]
        doc.matched_tx_ids = [rec.id]
        rec.likely_document_ids = []
        rec.match_why = (f"Sent by: {emp.name}, {how}", f"Amount: {amount} on both",
                         f"Card: •••• {rec.tx.card_last4}, {emp.first_name}'s card")
        rec.match_headline = f"{emp.first_name} sent the receipt for this payment."
        self.o.reverify_pair(rec, doc)
        self.log("match", subject_id=rec.id, evidence_ids=[rec.evidence_id, *doc.evidence_ids],
                 values={"transactions": [rec.id], "documents": [doc.id], "sent_by": emp.id},
                 validations=list(rec.match_why), response={"quality": doc.document.quality.value, "kind": how})

    def link_receipts(self) -> int:
        """A reply's receipt that reconciliation did not match (a photo read without the shop's tax number,
        say) is matched to the payment it was sent for, when it fits that payment exactly."""
        repo = self.repo
        if not repo.receipt_requests:
            return 0
        moved = 0
        for req in sorted(repo.receipt_requests.values(), key=lambda r: r.tx_id):
            rec = repo.transactions.get(req.tx_id)
            emp = repo.employees.get(req.employee_id)
            if not req.offered or rec is None or emp is None or rec.document_ids or repo.items[rec.item_id].is_done:
                continue
            doc = next((repo.documents[d] for d in req.offered
                        if d in repo.documents and self.fits(rec, repo.documents[d])), None)
            if doc is not None:
                self.link(rec, doc, emp, "in reply to my request")
                moved += 1
        return moved

    def open_card_payments(self, emp: Employee) -> list[TxRecord]:
        """Payments on this employee's cards still waiting for their receipt (what the employee sees)."""
        out = []
        for rec in sorted(self.repo.transactions.values(), key=lambda r: (r.tx.booked_on, r.id)):
            if rec.tx.card_last4 not in emp.cards or self.holder_of(rec.tx.card_last4) is not emp:
                continue
            item = self.repo.items[rec.item_id]
            if rec.tx.amount >= 0 or rec.document_ids or rec.private or item.is_done or rec.decision is None \
                    or not rec.decision.requires_document or rec.decision.expectation not in _ASKABLE:
                continue
            out.append(rec)
        return out

    def _review(self, now: datetime) -> None:
        """Requests whose payment has its document now (from the employee or anywhere else) are done; so are
        those whose payment was set aside, or whose card you said is not that person's any more."""
        repo = self.repo
        for req in sorted(repo.receipt_requests.values(), key=lambda r: r.tx_id):
            if req.status not in ("asking", "escalated", "owner"):
                continue
            rec = repo.transactions.get(req.tx_id)
            if rec is None:
                continue
            item = repo.items[rec.item_id]
            received = bool(rec.document_ids or rec.proof_evidence_ids)
            holder = self.cardholder(rec)
            moved_on = holder is None or holder.id != req.employee_id
            if not received and not rec.private and not item.is_done and not moved_on:
                continue
            req.status, req.received_at = ("received" if received else "cancelled"), now
            evidence = [e for d in rec.document_ids if d in repo.documents for e in repo.documents[d].evidence_ids]
            needs = repo.needs.get(req.needs_id or "")
            if needs is not None and needs.status == "open":
                needs.status, needs.resolution = "resolved", ("evidence" if received else "set_aside")
                if item.stage is Stage.NEEDS_OWNER:  # nothing left to ask: back to waiting for its checks
                    self.o.advance(item, Stage.UNDERSTOOD, [rec.evidence_id, *evidence], agent=self.name,
                                   note="The receipt arrived." if received else "Set aside.")
            if received:
                emp = repo.employees.get(req.employee_id)
                first = emp.first_name if emp else "your employee"
                amount, merchant, _ = self._facts(rec)
                text = (f"{first} sent the receipt for the {amount} {merchant} payment."
                        if any(d in req.offered for d in rec.document_ids) else
                        f"The receipt for the {amount} {merchant} payment on {first}'s card arrived.")
                self.o.activity(now, "recovered", text, rec.company_id, amount=abs(rec.tx.amount),
                                currency=rec.tx.currency, evidence_ids=evidence, tag="staff")
            self.log("receipt_request_done", subject_id=rec.id, evidence_ids=[rec.evidence_id, *evidence],
                     response={"status": req.status})

    # ----------------------------------------------------------------- the owner's answers

    def answer(self, needs: NeedsYouRecord, option_id: str, answer_ev: str, now: datetime) -> AnswerOutcome:
        if needs.kind == "receipt":
            return self._answer_receipt(needs, option_id, answer_ev, now)
        return self._answer_claim(needs, option_id, answer_ev, now)

    def _answer_receipt(self, needs: NeedsYouRecord, option_id: str, answer_ev: str, now: datetime) -> AnswerOutcome:
        repo = self.repo
        rec = repo.transactions[needs.subject_id]
        req = repo.receipt_requests[rec.id]
        emp = repo.employees[req.employee_id]
        needs.status, needs.answer, needs.answered_at = "answered", option_id, now
        item = repo.items[rec.item_id]
        amount, merchant, _ = self._facts(rec)
        if option_id == "remind":
            req.status, req.rounds = "asking", req.rounds + 1
            note = f"You asked me to ask {emp.first_name} again."
            message = f"Done. I asked {emp.first_name} again."
        else:
            req.status = "owner"
            note = "You said you would send the receipt."
            message = "Done. Send me the receipt when you have it: I will match it to the payment."
        self.o.advance(item, Stage.UNDERSTOOD, [rec.evidence_id, answer_ev], agent=self.name,
                       actor=f"{OWNER_ACTOR}:{repo.owner.email}", note=note)
        if option_id == "remind" and plain_email(emp.email) and req.thread is not None:
            self._write(req, rec, emp, now)
        self.o.activity(now, "answered", f"You told me what to do about the receipt for the {amount} {merchant} "
                        "payment.", rec.company_id, evidence_ids=[answer_ev], tag="staff")
        return AnswerOutcome(ok=True, message=message)

    # ----------------------------------------------------------------- expense claims

    def company_payment(self, doc: DocumentRecord, emp: Employee) -> TxRecord | None:
        """A company payment this receipt is for (so it is not the employee's own money): the payment its
        document matched, or one open payment of the same amount, currency and days, on this employee's
        card or to the same shop."""
        repo = self.repo
        for t in doc.matched_tx_ids:
            if t in repo.transactions:
                return repo.transactions[t]
        likely = [r for r in repo.transactions.values() if doc.id in r.likely_document_ids]
        if likely:
            return likely[0]
        shop = set(_words(doc.document.supplier_name)) - {"lda", "sa", "unipessoal", "s", "a"}
        found = []
        for rec in sorted(repo.transactions.values(), key=lambda r: r.id):
            if rec.tx.amount >= 0 or rec.document_ids or rec.private or rec.claim_ids or not self.fits(rec, doc):
                continue
            mine = bool(rec.tx.card_last4) and rec.tx.card_last4 in emp.cards
            same_shop = bool(shop & set(_words(f"{rec.tx.counterparty} {self.o.merchant_name(rec.tx)}")))
            if mine or same_shop:
                found.append(rec)
        return found[0] if found else None

    def claim(self, emp: Employee, doc: DocumentRecord, *, company_id: str | None, submitted_by: str,
              now: datetime) -> tuple[ExpenseClaim | None, str]:
        """An expense claim from one receipt (and what to say to whoever sent it)."""
        repo = self.repo
        d = doc.document
        you = submitted_by == "employee"
        if doc.claim_id is not None and doc.claim_id in repo.expense_claims:
            existing = repo.expense_claims[doc.claim_id]
            return existing, f"I already have this receipt. {self.claim_status_line(existing, you=you)}"
        paid = self.company_payment(doc, emp)
        if paid is not None:
            amount, merchant, when = self._facts(paid)
            card = f" with card •••• {paid.tx.card_last4}" if paid.tx.card_last4 else ""
            if not paid.document_ids and self.fits(paid, doc) and paid.tx.card_last4 in emp.cards:
                self.link(paid, doc, emp, "sent as a receipt")
            return None, (f"This receipt is for the {amount} payment at {merchant} on {when}, which the company "
                          f"paid{card}. There is nothing to pay back: I kept it with that payment.")
        if doc.sales or doc.supporting or doc.id in repo.statements or d.doc_type not in _RECEIPT_TYPES \
                or d.doc_type is DocumentType.CREDIT_NOTE:
            return None, "This is not a receipt or an invoice, so there is nothing to pay back."
        if d.gross_amount is None or d.gross_amount <= 0:
            return None, "I couldn't read the amount on this receipt. Please send a clearer photo."
        company = d.entity_id or company_id or emp.company_id or (
            next(iter(repo.companies)) if len(repo.companies) == 1 else None)
        if company is None or company not in repo.companies:
            return None, "Which company is this for? Send it again and choose the company."
        merchant = display_name(d.supplier_name, fallback="Shop")
        claim = ExpenseClaim(
            id=_unique_id(repo.expense_claims, "claim_" + d.id.removeprefix("doc_")), employee_id=emp.id,
            company_id=company, document_id=doc.id, amount=abs(d.gross_amount), currency=d.currency,
            spent_on=d.issue_date or doc.received_at.astimezone(TZ).date(), merchant=merchant,
            evidence_ids=tuple(doc.evidence_ids), submitted_by="employee" if you else "owner", submitted_at=now)
        repo.expense_claims[claim.id] = claim
        doc.claim_id = claim.id
        if d.entity_id is None:
            doc.document = d.model_copy(update={"entity_id": company})
        amount = format_money(claim.amount, claim.currency)
        self.log("submit_claim", subject_id=claim.id, evidence_ids=list(claim.evidence_ids),
                 actor=OWNER_ACTOR if not you else f"employee:{emp.id}",
                 values={"employee": emp.id, "amount": claim.amount, "currency": claim.currency, "company": company})
        self.o.activity(now, "collected", f"Collected {emp.name}'s {amount} receipt from {merchant}, paid with their "
                        "own money.", company, amount=claim.amount, currency=claim.currency,
                        evidence_ids=claim.evidence_ids, tag="staff")
        self._ask_approval(claim, now)
        return claim, (f"Got it. Your {amount} receipt from {merchant} is waiting for approval." if you else
                       f"Got it. {emp.first_name}'s {amount} receipt from {merchant} is waiting for your OK to pay it "
                       "back.")

    def claim_status_line(self, claim: ExpenseClaim, *, you: bool) -> str:
        emp = self.repo.employees.get(claim.employee_id)
        first = emp.first_name if emp else "your employee"
        amount = format_money(claim.amount, claim.currency)
        if claim.status == "waiting":
            return "It is waiting for approval." if you else f"It is waiting for your OK to pay {first} back."
        if claim.status == "approved":
            return (f"It is approved: {amount} to be paid back to you." if you else
                    f"You approved it: {amount} to be paid back to {first}.")
        if claim.status == "paid":
            rec = self.repo.transactions.get(claim.paid_by_tx_id or "")
            when = day_month(rec.tx.booked_on, self.repo.today()) if rec is not None else ""
            return f"It was paid back on {when}." if when else "It was paid back."
        return "It will not be paid back." if you else "You said not to pay it back."

    def _ask_approvals(self, now: datetime) -> None:
        for claim in sorted(self.repo.expense_claims.values(), key=lambda c: c.id):
            self._ask_approval(claim, now)

    def _ask_approval(self, claim: ExpenseClaim, now: datetime) -> None:
        """One tap for the owner: pay it back or not (§37). Asked once the receipt's details are not in
        conflict (a disagreement is asked about first)."""
        repo = self.repo
        if claim.status != "waiting" or claim.needs_id is not None:
            return
        doc = repo.documents[claim.document_id]
        item = repo.items[doc.item_id]
        if doc.document.quality is Quality.RED or item.stage in (Stage.CONFLICT, Stage.NEEDS_OWNER) or item.is_done:
            return
        emp = repo.employees.get(claim.employee_id)
        who = emp.name if emp else "Your employee"
        first = emp.first_name if emp else "them"
        amount = format_money(claim.amount, claim.currency)
        prompt = (f"{who} paid {amount} at {claim.merchant} on {day_month(claim.spent_on, repo.today())} with their "
                  "own money. Pay it back?")
        sent = (f"{first} sent the receipt." if claim.submitted_by == "employee" else
                f"You sent the receipt for {first}.")
        why = (sent, f"It is for {repo.company_name(claim.company_id)}.",
               f"I close it when the transfer paying {first} back shows in your bank.")
        needs_id = _unique_id(repo.needs, f"nd_claim_{_slug(first)}_{int(claim.amount)}")
        repo.needs[needs_id] = NeedsYouRecord(
            id=needs_id, kind="expense_claim", subject_type="document", subject_id=doc.id, item_id=doc.item_id,
            company_id=claim.company_id, created_at=now, prompt=prompt, why=why,
            options=(CheckOption("approve", f"Yes, pay {first} back"), CheckOption("decline", "No, don't pay it back")))
        claim.needs_id = needs_id
        self.o.advance(item, Stage.NEEDS_OWNER, list(doc.evidence_ids), agent=self.name, note=prompt)
        self.log("ask_owner", subject_id=claim.id, evidence_ids=list(claim.evidence_ids),
                 response={"needs_you": needs_id})

    def _answer_claim(self, needs: NeedsYouRecord, option_id: str, answer_ev: str, now: datetime) -> AnswerOutcome:
        repo = self.repo
        claim = next(c for c in repo.expense_claims.values() if c.needs_id == needs.id)
        doc = repo.documents[claim.document_id]
        item = repo.items[doc.item_id]
        emp = repo.employees.get(claim.employee_id)
        first = emp.first_name if emp else "your employee"
        amount = format_money(claim.amount, claim.currency)
        needs.status, needs.answer, needs.answered_at = "answered", option_id, now
        actor = f"{OWNER_ACTOR}:{repo.owner.email}"
        if option_id == "decline":
            claim.status, claim.approval_evidence_id = "declined", answer_ev
            self.o.advance(item, Stage.NOT_REQUIRED, [*doc.evidence_ids, answer_ev], agent=self.name, actor=actor,
                           quality=Quality.GREEN, note="You said not to pay it back. It is not a company cost.")
            self.log("decline_claim", subject_id=claim.id, evidence_ids=[answer_ev], actor=OWNER_ACTOR)
            self.o.activity(now, "answered", f"You said not to pay back {first}'s {amount} receipt from "
                            f"{claim.merchant}.", claim.company_id, evidence_ids=[answer_ev], tag="staff")
            return AnswerOutcome(ok=True, message=f"Done. {first}'s {amount} receipt is set aside: it is not a "
                                                  "company cost.")
        # The owner's approval, bound to the claim's facts (policy/actions.py: owner approval, never automatic).
        approval = Approval(tenant_id=repo.tenant_id, action=ActionKind.EXPENSE_CLAIM_APPROVAL, subject_id=claim.id,
                            level=Requirement.OWNER, approved_by=repo.owner.email, approved_at=now,
                            entity_id=claim.company_id, fingerprint=claim.fingerprint)
        decision = authorize(ActionKind.EXPENSE_CLAIM_APPROVAL, repo.policy, ActionContext(
            tenant_id=repo.tenant_id, entity_id=claim.company_id, subject_id=claim.id,
            fingerprint=claim.fingerprint, approval=approval))
        self.log("approve_claim", subject_id=claim.id, evidence_ids=[*claim.evidence_ids, answer_ev],
                 actor=OWNER_ACTOR, values={"amount": claim.amount, "employee": claim.employee_id},
                 response={"allowed": decision.allowed_now, "approval_id": decision.approval_id})
        if not decision.allowed_now:  # never: the approval is the owner's own, for these exact facts
            return AnswerOutcome(ok=False, message=decision.reason_plain)
        claim.status, claim.approval_evidence_id = "approved", answer_ev
        claim.approval_id, claim.approved_at = decision.approval_id, now
        self.o.advance(item, Stage.UNDERSTOOD, [*doc.evidence_ids, answer_ev], agent=self.name, actor=actor,
                       note=f"You approved it: {amount} to pay back to {first}.")
        self.o.activity(now, "answered", f"You approved {first}'s {amount} expense claim for {claim.merchant}.",
                        claim.company_id, amount=claim.amount, currency=claim.currency, evidence_ids=[answer_ev],
                        tag="staff")
        return AnswerOutcome(ok=True, message=f"Done. When you pay {first} back {amount}, I will match the transfer "
                                              "and close it.")

    # ----------------------------------------------------------------- paying employees back

    def _payee(self, rec: TxRecord) -> Employee | None:
        """The employee a transfer goes to: their bank account, or their full name as the beneficiary."""
        employees = sorted(self.repo.employees.values(), key=lambda e: e.id)
        iban = normalize_iban(rec.tx.counterparty_iban or "")
        found = [e for e in employees if iban and e.iban and normalize_iban(e.iban) == iban]
        if not found:
            found = [e for e in employees if same_person(e.name, rec.tx.counterparty)]
        return found[0] if len(found) == 1 else None

    def settle(self, now: datetime) -> int:
        """A transfer to an employee of exactly what one approved claim, or all of their approved claims, is
        for pays them back: the claims close, and so does the transfer (evidence: receipts, the owner's
        approvals and the bank). Anything else paid to them stays what it is."""
        repo = self.repo
        approved = [c for c in repo.expense_claims.values() if c.status == "approved"]
        if not approved:
            return 0
        moved = 0
        for rec in sorted(repo.transactions.values(), key=lambda r: (r.tx.booked_on, r.id)):
            if rec.tx.amount >= 0 or rec.tx.kind is TransactionKind.CARD or rec.claim_ids or rec.document_ids \
                    or rec.private or rec.proof_evidence_ids or repo.items[rec.item_id].is_done:
                continue
            emp = self._payee(rec)
            if emp is None:
                continue
            owed = sorted((c for c in repo.expense_claims.values() if c.status == "approved"
                           and c.employee_id == emp.id and c.company_id == rec.company_id
                           and c.currency.strip().upper() == rec.tx.currency.strip().upper()),
                          key=lambda c: (c.approved_at or now, c.id))
            paid = abs(rec.tx.amount)
            chosen = next(([c] for c in owed if c.amount == paid), None)
            if chosen is None and len(owed) > 1 and sum((c.amount for c in owed), _ZERO) == paid:
                chosen = owed
            if chosen:
                self._pay(rec, emp, chosen, now)
                moved += 1
        return moved

    def _pay(self, rec: TxRecord, emp: Employee, claims: list[ExpenseClaim], now: datetime) -> None:
        repo = self.repo
        amount = format_money(abs(rec.tx.amount), rec.tx.currency)
        what = (f"the {claims[0].merchant} receipt" if len(claims) == 1 else
                count_phrase(len(claims), "expense claim"))
        rec.claim_ids = [c.id for c in claims]
        rec.decision = ExpectationDecision(
            rec.tx.id, EvidenceExpectation.BANK_EVIDENCE_SUFFICES,
            f"Pays {emp.name} back for {what}. Its receipt and your approval cover it.", Quality.GREEN,
            "expense_claim")
        rec.match_headline = f"Paid {emp.first_name} back for {what}."
        rec.match_why = (f"Paid to: {emp.name}", f"Amount: {amount}, what you approved",
                         *(f"Receipt: {c.merchant}, {format_money(c.amount, c.currency)}, approved on "
                           f"{day_month((c.approved_at or now).astimezone(TZ).date(), repo.today())}" for c in claims))
        approvals = [c.approval_evidence_id for c in claims if c.approval_evidence_id]
        evidence = [rec.evidence_id, *(e for c in claims for e in c.evidence_ids), *approvals]
        item = repo.items[rec.item_id]
        if item.stage is Stage.ACQUIRED:  # just imported: understood first, one step at a time (§3)
            self.o.advance(item, Stage.UNDERSTOOD, [rec.evidence_id], agent=self.name, note=rec.decision.reason)
        self.o.closure._close(item, evidence, note=f"Paid {emp.first_name} back for {what}.")
        for c in claims:
            c.status, c.paid_by_tx_id, c.paid_at = "paid", rec.id, now
            doc = repo.documents[c.document_id]
            self.o.closure._close(repo.items[doc.item_id],
                                  [*doc.evidence_ids, *([c.approval_evidence_id] if c.approval_evidence_id else []),
                                   rec.evidence_id],
                                  note=f"Paid back to {emp.first_name} on {day_month(rec.tx.booked_on)}.")
        self.log("reimburse", subject_id=rec.id, evidence_ids=evidence,
                 values={"employee": emp.id, "claims": rec.claim_ids, "amount": abs(rec.tx.amount)})
        self.o.activity(now, "checked", f"Matched the {amount} transfer to {emp.first_name}: it pays back {what}.",
                        rec.company_id, amount=abs(rec.tx.amount), currency=rec.tx.currency, evidence_ids=evidence,
                        tag="staff")

    # ----------------------------------------------------------------- the auditor (§55, §57)

    def claim_problem(self, doc: DocumentRecord) -> str | None:
        claim = self.repo.expense_claims.get(doc.claim_id or "")
        if claim is None:
            return "The expense claim for this receipt is gone."
        if doc.document.quality is Quality.RED:
            return "The receipt's details no longer agree."
        if claim.status != "paid" or claim.paid_by_tx_id not in self.repo.transactions:
            return "The transfer paying this expense back is no longer on file."
        if doc.document.gross_amount is None or abs(doc.document.gross_amount) != claim.amount:
            return "The receipt and its expense claim no longer agree."
        return None

    def reimbursement_problem(self, rec: TxRecord) -> str | None:
        claims = [self.repo.expense_claims.get(c) for c in rec.claim_ids]
        if any(c is None or c.status != "paid" or c.paid_by_tx_id != rec.id for c in claims):
            return "The expense claims this transfer paid back changed."
        total = sum((c.amount for c in claims if c is not None), _ZERO)
        if total != abs(rec.tx.amount) or any(
                c is not None and c.currency.strip().upper() != rec.tx.currency.strip().upper() for c in claims):
            return "The transfer and the expense claims it paid back no longer agree."
        return None
