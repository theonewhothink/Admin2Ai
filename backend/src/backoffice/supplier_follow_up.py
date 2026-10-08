"""After a supplier is asked for a missing invoice: its reply, reminders, and one plain line for the owner (§22, §45).

Once a transport has accepted the request (``MissingEvidenceAgent.sent``), the request has a thread
(``missing.ChaseThread``: the Message-IDs we sent and the reference in the subject). Then:

* **Replies.** An inbound email is matched to its request by In-Reply-To, then References, then the reference in
  its subject (``missing.match_reply``, as staff and accountant replies are); a match by the subject reference
  alone must come from the supplier's own domain. The reply's attachments and links are read like any document;
  the documents it gave are offered to the payment it answers. Reconciliation usually matches them; when it does
  not (an invoice read without the supplier's tax number, say), one that fits the payment exactly is linked to it
  here. The payment closes only when that document verifies (§3), and the request is then done: "EDP sent the
  invoice for the €64.10 payment in reply to my request." A reply without an invoice is noted as such, and the
  reminders go on.
* **Reminders.** No invoice after the cadence (``missing.ReminderPolicy``: after 6 days, then 4 more, at most 2,
  never on a weekend), a reminder goes out in the same thread (Re: subject, In-Reply-To, References) through the
  usual send path: written, counted as sent only once a transport accepted it, held back when asking suppliers is
  switched off.
* **The owner's one line.** After the last reminder, one plain question in Needs you: "EDP hasn't sent the invoice
  for €64.10 on 19 September." (or "EDP replied, but I still don't have ..."), with "Ask EDP again" and "I'll
  upload it". Never before, and never claiming a reply that did not come.

``Repository.chase_reminders`` is the cadence; None turns reminders off (the demo's frozen story, whose simulated
supplier never answers).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from backoffice.domain.lifecycle import Stage
from backoffice.domain.models import DocumentType, Quality
from backoffice.learning import day_month, display_name, format_money
from backoffice.learning.plain import count_phrase
from backoffice.missing import (
    ChaseFacts,
    ChaseThread,
    InboundEmail,
    MatchMethod,
    ReminderPolicy,
    ReminderStep,
    compose_reminder,
    match_reply,
    next_reminder,
)
from backoffice.orchestrator import (
    MESSAGE_ID_DOMAIN,
    OWNER_ACTOR,
    TZ,
    AnswerOutcome,
    ChaseRecord,
    CheckOption,
    DocumentRecord,
    NeedsYouRecord,
    OutgoingMessage,
    TxRecord,
    _Agent,
    _slug,
    _unique_id,
)
from backoffice.policy import ActionContext, ActionKind, authorize
from backoffice.purchases import PURCHASE_INVOICE_TYPES

__all__ = ["SupplierFollowUp"]

_FITS_BEFORE = timedelta(days=60)  # an invoice can be issued well before its payment ...
_FITS_AFTER = timedelta(days=30)  # ... and is sometimes issued after it


class SupplierFollowUp(_Agent):
    """Replies, reminders and the owner's one line for supplier requests (module docstring)."""

    name = "missing_evidence"
    NEEDS_KINDS = ("supplier_invoice",)
    MESSAGE_KINDS = ("supplier_reminder",)

    # ----------------------------------------------------------------- what we sent

    def started(self, chase: ChaseRecord, at: datetime) -> None:
        """The request went out: its thread starts (the reply and the reminders follow it)."""
        if chase.thread is None:
            chase.thread = ChaseThread.start(chase.message, at.astimezone(TZ).date())

    def _facts(self, chase: ChaseRecord, rec: TxRecord) -> ChaseFacts | None:
        supplier = self.repo.suppliers.get(chase.supplier_id)
        company = self.repo.companies.get(chase.company_id)
        if supplier is None or company is None or not supplier.contact_email:
            return None
        return ChaseFacts.build(rec.tx, supplier, company, invoice_number=self.o.statements.number_for(rec))

    def _waiting(self, chase: ChaseRecord) -> bool:
        """A reminder written that has not gone out yet (or is held back): no other is written meanwhile."""
        return any(not self.repo.outbox[m].sent for m in chase.reminder_outbox_ids if m in self.repo.outbox)

    def sent_reminder(self, message: OutgoingMessage, at: datetime) -> None:
        """A transport accepted a reminder: now it counts as sent (and the cadence goes on from it)."""
        chase = self.repo.chases.get(message.subject_id)
        if chase is None or message.id not in chase.reminder_outbox_ids or chase.thread is None:
            return
        reminder = chase.reminders[chase.reminder_outbox_ids.index(message.id)]
        chase.thread = chase.thread.with_sent(reminder, at.astimezone(TZ).date())
        rec = self.repo.transactions[chase.tx_id]
        who, amount = self._who(chase, rec), format_money(abs(rec.tx.amount), rec.tx.currency)
        self.o.activity(at, "chased", f"Reminded {who} about the invoice for the {amount} payment.", chase.company_id,
                        evidence_ids=[rec.evidence_id])
        self.log("remind_supplier", subject_id=rec.id, evidence_ids=[rec.evidence_id],
                 values={"to": reminder.to, "subject": reminder.subject, "reminder": reminder.reminder_number},
                 response={"message_id": reminder.message_id})

    def waiting_line(self, message: OutgoingMessage) -> tuple[str, list[str]]:
        chase = self.repo.chases.get(message.subject_id)
        rec = self.repo.transactions.get(message.subject_id)
        if chase is None or rec is None:
            return "Wrote an email. It is waiting to be sent.", []
        who, amount = self._who(chase, rec), format_money(abs(rec.tx.amount), rec.tx.currency)
        return (f"Wrote {who} a reminder about the invoice for the {amount} payment. It is waiting to be sent.",
                [rec.evidence_id])

    def _who(self, chase: ChaseRecord, rec: TxRecord) -> str:
        supplier = self.repo.suppliers.get(chase.supplier_id)
        return display_name(supplier.name) if supplier is not None else self.o.merchant_name(rec.tx)

    # ----------------------------------------------------------------- the supplier's reply

    def reply_to(self, parsed: Any, message_evidence_id: str, at: datetime) -> ChaseRecord | None:
        """The request an inbound email answers, by its thread; None when it answers none (never a guess)."""
        repo = self.repo
        open_chases = [c for c in repo.chases.values() if c.thread is not None and c.status != "received"]
        if not open_chases:
            return None
        sender = (parsed.sender.address if parsed.sender else "").strip().lower()
        inbound = InboundEmail(from_address=sender, subject=parsed.subject or "", message_id=parsed.thread.message_id,
                               in_reply_to=parsed.thread.in_reply_to, references=parsed.thread.references)
        found = match_reply([c.thread for c in open_chases if c.thread is not None], inbound)
        if found is None:
            return None
        chase = next(c for c in open_chases if c.thread is not None and c.thread.token == found.thread.token)
        if found.method is MatchMethod.SUBJECT_REFERENCE and not found.sender_matches:
            self.log("supplier_reply_refused", subject_id=chase.tx_id, evidence_ids=[message_evidence_id],
                     response={"reason": "subject reference from another domain"})
            return None
        if message_evidence_id in chase.replies:
            return chase
        chase.replies.append(message_evidence_id)
        chase.replied_at = at
        self.log("supplier_reply", subject_id=chase.tx_id, evidence_ids=[message_evidence_id],
                 values={"method": found.method.value, "same_domain": found.sender_matches})
        return chase

    def replied(self, chase: ChaseRecord, document_ids: list[str], at: datetime) -> None:
        """The documents the reply gave are offered to the payment it answers; a reply without one is noted."""
        new = [d for d in dict.fromkeys(document_ids) if d not in chase.offered]
        chase.offered += new
        if new:
            return
        rec = self.repo.transactions[chase.tx_id]
        who, amount = self._who(chase, rec), format_money(abs(rec.tx.amount), rec.tx.currency)
        self.o.activity(at, "checked", f"{who} replied about the {amount} payment, but there was no invoice in the "
                        "reply.", chase.company_id, evidence_ids=chase.replies[-1:])

    def fits(self, rec: TxRecord, doc: DocumentRecord) -> bool:
        """Can this document be what the payment misses? Its invoice (a credit note for money back), same amount
        and currency, dated around the payment, nobody else's, not on hold, its details not in conflict."""
        d = doc.document
        if doc.matched_tx_ids or doc.on_hold or doc.supporting or doc.sales or doc.claim_id is not None:
            return False
        if d.gross_amount is None or d.quality is Quality.RED:
            return False
        wanted = (DocumentType.CREDIT_NOTE,) if rec.tx.amount > 0 else tuple(PURCHASE_INVOICE_TYPES)
        if d.doc_type not in wanted:
            return False
        if d.currency.strip().upper() != rec.tx.currency.strip().upper() or abs(d.gross_amount) != abs(rec.tx.amount):
            return False
        return d.issue_date is None or rec.tx.booked_on - _FITS_BEFORE <= d.issue_date <= rec.tx.booked_on + _FITS_AFTER

    def link_replies(self) -> int:
        """A document a supplier sent in reply to my request that reconciliation did not match, linked to the
        payment it was sent for when it fits that payment exactly. It still closes only once verified (§3)."""
        repo = self.repo
        moved = 0
        for chase in sorted(repo.chases.values(), key=lambda c: c.tx_id):
            rec = repo.transactions.get(chase.tx_id)
            if not chase.offered or rec is None or rec.document_ids or repo.items[rec.item_id].is_done:
                continue
            doc = next((repo.documents[d] for d in chase.offered if d in repo.documents
                        and self.fits(rec, repo.documents[d])), None)
            if doc is None:
                continue
            who, amount = self._who(chase, rec), format_money(abs(rec.tx.amount), rec.tx.currency)
            rec.document_ids = [doc.id]
            doc.matched_tx_ids = [rec.id]
            rec.likely_document_ids = []
            rec.match_why = (f"Sent by: {who}, in reply to my request", f"Amount: {amount} on both")
            rec.match_headline = f"{who} sent the invoice in reply to my request."
            self.o.reverify_pair(rec, doc)
            self.log("match", subject_id=rec.id, evidence_ids=[rec.evidence_id, *doc.evidence_ids],
                     values={"transactions": [rec.id], "documents": [doc.id], "kind": "supplier_reply"},
                     validations=list(rec.match_why), response={"quality": doc.document.quality.value})
            moved += 1
        return moved

    def settle(self, now: datetime) -> None:
        """A request whose payment closed on its document is done; a reply that brought it says so once."""
        repo = self.repo
        for chase in sorted(repo.chases.values(), key=lambda c: c.tx_id):
            if chase.status == "received":
                continue
            rec = repo.transactions.get(chase.tx_id)
            if rec is None or not rec.document_ids or repo.items[rec.item_id].stage is not Stage.CLOSED:
                continue
            chase.status, chase.received_at = "received", now
            if chase.needs_id and (needs := repo.needs.get(chase.needs_id)) is not None and needs.status == "open":
                needs.status, needs.resolution = "resolved", "evidence"
            if any(d in chase.offered for d in rec.document_ids):
                who, amount = self._who(chase, rec), format_money(abs(rec.tx.amount), rec.tx.currency)
                self.o.activity(now, "recovered", f"{who} sent the invoice for the {amount} payment in reply to my "
                                "request.", chase.company_id, amount=abs(rec.tx.amount), currency=rec.tx.currency,
                                evidence_ids=[rec.evidence_id, *chase.replies[-1:]])
            self.log("supplier_request_done", subject_id=rec.id, evidence_ids=[rec.evidence_id],
                     values={"documents": list(rec.document_ids), "replies": len(chase.replies)})

    # ----------------------------------------------------------------- reminders and the owner's line

    def remind(self, now: datetime) -> list[str]:
        """Reminders on the cadence, through the send path; after the last one, the owner's one line."""
        repo = self.repo
        base = repo.chase_reminders
        if base is None:
            return []
        today = now.astimezone(TZ).date()
        written: list[str] = []
        for chase in sorted(repo.chases.values(), key=lambda c: c.tx_id):
            if chase.status != "asking" or chase.thread is None or self._waiting(chase):
                continue
            rec = repo.transactions.get(chase.tx_id)
            if rec is None or rec.document_ids or rec.private:
                continue
            item = repo.items[rec.item_id]
            if item.is_done or item.stage in (Stage.NEEDS_OWNER, Stage.CONFLICT):
                continue
            policy = ReminderPolicy(first_after_days=base.first_after_days, then_every_days=base.then_every_days,
                                    max_reminders=base.max_reminders + 2 * chase.rounds,
                                    skip_weekends=base.skip_weekends)
            step = next_reminder(chase.thread, policy, today)
            if step.step is ReminderStep.SEND_REMINDER:
                if self._write_reminder(chase, rec, now):
                    written.append(rec.id)
            elif step.step is ReminderStep.ESCALATE:
                self._escalate(chase, rec, now)
        return written

    def _write_reminder(self, chase: ChaseRecord, rec: TxRecord, now: datetime) -> bool:
        decision = authorize(ActionKind.SUPPLIER_INVOICE_REQUEST, self.repo.policy, ActionContext(
            tenant_id=self.repo.tenant_id, entity_id=chase.company_id, subject_id=rec.id))
        if not decision.allowed_now:
            return False  # asking suppliers is switched off: no reminder, and no claim of one
        facts = self._facts(chase, rec)
        if facts is None or chase.thread is None:
            return False
        reminder = compose_reminder(facts, chase.thread, today=now.astimezone(TZ).date(),
                                    message_id_domain=MESSAGE_ID_DOMAIN)
        headers = (("Message-ID", reminder.message_id), ("In-Reply-To", reminder.in_reply_to or ""),
                   ("References", " ".join(reminder.references)))
        out = self.o.write_email("supplier_reminder", rec.id, chase.company_id, reminder.to, reminder.subject,
                                 reminder.body, now, headers=headers)
        chase.reminders.append(reminder)
        chase.reminder_outbox_ids.append(out.id)
        self.log("write_reminder", subject_id=rec.id, evidence_ids=[rec.evidence_id],
                 values={"to": reminder.to, "subject": reminder.subject, "outbox_id": out.id,
                         "reminder": reminder.reminder_number})
        return True

    def _escalate(self, chase: ChaseRecord, rec: TxRecord, now: datetime) -> None:
        """No invoice after the reminders: one plain line for the owner (§37), never before."""
        repo = self.repo
        who = self._who(chase, rec)
        amount = format_money(abs(rec.tx.amount), rec.tx.currency)
        when = day_month(rec.tx.booked_on, repo.today())
        prompt = (f"{who} replied, but I still don't have the invoice for {amount} on {when}." if chase.replies
                  else f"{who} hasn't sent the invoice for {amount} on {when}.")
        thread = chase.thread
        asked = thread.sent[0].sent_on if thread is not None and thread.sent else now.date()
        reminders = thread.reminders_sent if thread is not None else 0
        why = [f"I asked {who} on {day_month(asked, repo.today())} and sent {count_phrase(reminders, 'reminder')}."]
        searched = self.o.search.searched_sentence(rec.id)
        if searched:
            why.insert(0, searched)
        needs_id = _unique_id(repo.needs, f"nd_invoice_{_slug(who.split()[0] if who else 'supplier')}_"
                                          f"{int(abs(rec.tx.amount))}")
        repo.needs[needs_id] = NeedsYouRecord(
            id=needs_id, kind="supplier_invoice", subject_type="transaction", subject_id=rec.id, item_id=rec.item_id,
            company_id=rec.company_id, created_at=now, prompt=prompt, why=tuple(why),
            options=(CheckOption("ask_again", f"Ask {who} again"), CheckOption("upload", "I'll upload it")))
        chase.status, chase.needs_id, chase.escalated_at = "escalated", needs_id, now
        self.o.advance(repo.items[rec.item_id], Stage.NEEDS_OWNER, [rec.evidence_id], agent=self.name, note=prompt)
        self.log("escalate_supplier_request", subject_id=rec.id, evidence_ids=[rec.evidence_id],
                 values={"supplier": chase.supplier_id, "reminders": reminders, "replies": len(chase.replies)},
                 response={"needs_you": needs_id})

    def answer(self, needs: NeedsYouRecord, option_id: str, answer_ev: str, now: datetime) -> AnswerOutcome:
        repo = self.repo
        rec = repo.transactions[needs.subject_id]
        chase = repo.chases[rec.id]
        rec_who = self._who(chase, rec)
        needs.status, needs.answer, needs.answered_at = "answered", option_id, now
        if option_id == "ask_again":
            chase.status, chase.rounds = "asking", chase.rounds + 1
            note, message = f"You asked me to ask {rec_who} again.", f"Done. I will ask {rec_who} again."
        else:
            chase.status = "owner"
            note = "You said you would upload the invoice."
            message = "Done. Send me the invoice when you have it: I will match it to the payment."
        self.o.advance(repo.items[rec.item_id], Stage.UNDERSTOOD, [rec.evidence_id, answer_ev], agent=self.name,
                       actor=f"{OWNER_ACTOR}:{repo.owner.email}", note=note)
        if option_id == "ask_again":
            self._write_reminder(chase, rec, now)
        self.o.activity(now, "answered", f"You told me what to do about the invoice for the "
                        f"{format_money(abs(rec.tx.amount), rec.tx.currency)} {rec_who} payment.", rec.company_id,
                        evidence_ids=[answer_ev])
        return AnswerOutcome(ok=True, message=message)

    # ----------------------------------------------------------------- plain words

    def plan(self, rec: TxRecord, chase: ChaseRecord) -> str | None:
        """The next step once the supplier was asked, from what actually happened in its thread."""
        if chase.thread is None:
            return None
        who = self._who(chase, rec)
        amount = format_money(abs(rec.tx.amount), rec.tx.currency)
        when = day_month(rec.tx.booked_on, self.repo.today())
        if chase.status == "escalated":
            return (f"{who} replied, but I still don't have the invoice for the {amount} payment on {when}. I asked "
                    "you what to do." if chase.replies else
                    f"{who} hasn't sent the invoice for the {amount} payment on {when}. I asked you what to do.")
        if chase.status == "owner":
            return f"You said you would upload the invoice for the {amount} payment on {when}. I will match it."
        if chase.offered:
            return (f"{who} replied to my request with a document for the {amount} payment on {when}. I'm checking "
                    "it against the payment.")
        reminders = chase.thread.reminders_sent
        if chase.replies and chase.replied_at is not None:
            replied = day_month(chase.replied_at.astimezone(TZ).date(), self.repo.today())
            return (f"{who} replied on {replied}, but there was no invoice in the reply. I will remind them if it "
                    "does not come.")
        if reminders:
            last = day_month(chase.thread.sent[-1].sent_on, self.repo.today())
            return (f"I asked {who} for the invoice for the {amount} payment on {when} and sent "
                    f"{count_phrase(reminders, 'reminder')}, the last on {last}.")
        return None
