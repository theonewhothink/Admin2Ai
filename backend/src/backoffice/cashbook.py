"""The cash box and cash sales: till reports, cash paid into the bank, cash taken out of it (checklist X4, X5).

Till reports (backoffice.tills)
    Each day of a till report ("Relatório Z", "Fecho de caixa", "Z report", a CSV from the till software) is a
    document of the business's own sales, read into cash, card and other takings. One that does not add up is
    one plain question and is never used. The card part is proven by the card terminal's payout reports
    (backoffice.settlements): the card sales they show for that day must equal it to the cent; until one
    arrives the day waits, and a report showing another amount is said plainly and never closed. The cash part
    goes into the cash box. A day closes when its card part is proven (or it had none).

Cash paid into the bank
    A cash deposit ("DEPÓSITO NUMERÁRIO") is the till's cash going to the bank: it closes on the till reports
    whose cash it banks, oldest first, from the three weeks before it. What the deposits do not take stays in
    the till (the cash box), never dropped. A deposit larger than the till cash not yet banked stays open with
    one plain line. It is never counted as sales a second time: the till reports' cash is the sales.

The cash box (one per company)
    Cash taken out of the bank ("LEVANTAMENTO", a transfer to the cash box) and the till's cash sales go in;
    cash paid into the bank and purchases paid in cash (their receipts) go out. When a period ends, its
    balance is one plain line: what the box should hold, or how far an owner's count differs from it, never
    silently absorbed as a cost or as money in. The owner can count the box (their word is the evidence).

Pure Python over the orchestrator's records, like the other agents.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from backoffice.closure import Month
from backoffice.domain.lifecycle import Stage, TrackedItem
from backoffice.domain.models import (
    Document,
    DocumentType,
    EvidenceFormat,
    ExtractionMethod,
    FieldObservation,
    Quality,
    SourceKind,
)
from backoffice.learning import day_month, format_money
from backoffice.learning.plain import join_and
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
from backoffice.tills import (
    BANK,
    DEPOSIT,
    MONTHS,
    RECEIPT,
    TILL,
    CashBoxPeriod,
    CashCount,
    CashEntry,
    TillDay,
    cash_box_period,
    month_bounds,
    read_till_reports,
)

__all__ = ["CashAgent", "TillRecord"]

_ZERO = Decimal("0.00")
DEPOSIT_WINDOW_DAYS = 21  # a cash deposit banks the till cash of the three weeks before it
CARD_PAYOUT_DAYS = 4  # a card terminal pays a day's card sales within this many days
WITHDRAWAL_RULES = frozenset({"cash_withdrawal", "cash_box"})


@dataclass
class TillRecord:
    """One day of a till report and what proves it."""

    document_id: str
    till: TillDay
    company_id: str | None
    status: str = "open"  # "open" | "set_aside" (it did not add up; a corrected one will come)
    card_status: str = "waiting"  # "none" (no card takings) | "waiting" | "matched" | "differs"
    card_documents: list[str] = field(default_factory=list)  # the card terminal's payout reports that prove it
    card_found: Decimal | None = None  # the card sales those reports show for the day, when they differ
    banked: dict[str, Decimal] = field(default_factory=dict)  # cash deposit id -> part of this day's cash it banked

    @property
    def usable(self) -> bool:
        return self.status == "open" and self.till.adds_up and self.company_id is not None

    @property
    def cash_left(self) -> Decimal:
        """Cash from this day still in the till (not paid into the bank)."""
        return self.till.cash - sum(self.banked.values(), _ZERO)


class CashAgent(_Agent):
    name = "cash"
    NEEDS_KINDS = ("till",)

    # ------------------------------------------------------------------ reading

    def accept_text(self, text: str, evidence_ids: list[str], *, at: datetime, origin: str, report: IngestReport,
                    sender: str | None = None, message_text: str = "") -> bool:
        """Read ``text`` as till reports. False when it is not one (the caller reads it as usual)."""
        try:
            days = read_till_reports(text)
        except Exception as exc:  # a reader bug must never lose the upload: it is read like any other file
            self.log("till_read_failed", subject_id=evidence_ids[0], evidence_ids=evidence_ids,
                     response={"error": type(exc).__name__})
            return False
        if not days:
            return False
        made = [self.record(day, evidence_ids, at=at, origin=origin, report=report, text=text) for day in days]
        fresh = [r for r, new in made if new]
        repo = self.repo
        if not fresh:
            report.already_known, report.message = True, "Got it. I already had these till reports." \
                if len(made) > 1 else "Got it. I already had this till report."
            return True
        wrong = [r for r in fresh if not r.till.adds_up]
        nobody = [r for r in fresh if r.company_id is None]
        if len(fresh) == 1:
            t = fresh[0].till
            head = f"Got it. This is your till report for {day_month(t.day, repo.today())}: {t.split()}."
        else:
            first, last = min(r.till.day for r in fresh), max(r.till.day for r in fresh)
            cash = sum((r.till.cash for r in fresh), _ZERO)
            card = sum((r.till.card for r in fresh), _ZERO)
            head = (f"Got it. These are your till reports for {len(fresh)} days ({day_month(first)} to "
                    f"{day_month(last, repo.today())}): {format_money(cash)} in cash and {format_money(card)} by card.")
        if wrong or nobody:
            asked = [n for n in repo.needs.values() if n.status == "open" and n.kind == "till"
                     and n.subject_id in {r.document_id for r in (*wrong, *nobody)}]
            report.message = f"{head} I need one answer from you: {asked[0].prompt}" if asked else head
        else:
            report.message = (f"{head} I will match the card part with your card terminal's payouts and the cash "
                              "with what you pay into the bank.")
        return True

    def record(self, day: TillDay, evidence_ids: list[str], *, at: datetime, origin: str, report: IngestReport,
               text: str = "") -> tuple[TillRecord, bool]:
        repo = self.repo
        company = repo.company_for_tax_id(day.tax_id) if day.tax_id else None
        if company is None and len(repo.companies) == 1:
            company = next(iter(repo.companies))
        same = next((t for t in repo.till_days.values() if t.till.identity == day.identity
                     and t.company_id == company and t.status == "open"), None)
        if same is not None:  # the same day again (another copy, or the CSV and the printout)
            doc = repo.documents[same.document_id]
            new = [e for e in evidence_ids if e not in doc.evidence_ids]
            if new:
                doc.evidence_ids = [*doc.evidence_ids, *new]
                doc.document = doc.document.model_copy(update={"evidence_ids": doc.evidence_ids})
                self.log("merge_till_report", subject_id=doc.id, evidence_ids=new)
            report.document_ids.append(doc.id)
            return same, False
        seed = f"{evidence_ids[0]}|{day.day}|{day.number}|{day.total}|{day.location}"
        doc_id = "doc_" + hashlib.sha256(seed.encode()).hexdigest()[:16]
        method = ExtractionMethod.API if day.location.startswith("csv") else ExtractionMethod.EMBEDDED_TEXT
        observations = {
            "gross_amount": [FieldObservation(value=day.total, source=evidence_ids[0], method=method, confidence=0.95,
                                              location=day.location),
                             FieldObservation(value=day.parts, source="arithmetic", method=ExtractionMethod.ARITHMETIC,
                                              confidence=1.0, location="cash + card + other")],
            "issue_date": [FieldObservation(value=day.day, source=evidence_ids[0], method=method, confidence=0.95,
                                            location=day.location)],
        }
        quality = Quality.GREEN if day.adds_up else Quality.RED
        company_record = repo.companies.get(company or "")
        document = Document(
            id=doc_id, tenant_id=repo.tenant_id, evidence_ids=list(evidence_ids), doc_type=DocumentType.OTHER,
            supplier_name=(repo.legal_names.get(company) or company_record.name) if company_record else "Your till",
            supplier_tax_id=company_record.tax_id if company_record else day.tax_id,
            invoice_number=f"Z {day.number}" if day.number else None, issue_date=day.day, currency=day.currency,
            gross_amount=day.total, quality=quality, entity_id=company)
        item = TrackedItem(id="item_" + doc_id, tenant_id=repo.tenant_id, subject_type="document", subject_id=doc_id)
        repo.items[item.id] = item
        record = DocumentRecord(document=document, evidence_ids=list(evidence_ids), origin=origin, received_at=at,
                                item_id=item.id, observations=observations, sales=True, text=text[:4000],
                                reasons=() if day.adds_up else (day.mismatch(),), book="till",
                                country=repo.company_country(company))  # its company's country (§49)
        repo.documents[doc_id] = record
        self.o._classify_sensitive(record, record.text)  # by its own wording, like every document (§52)
        till = TillRecord(document_id=doc_id, till=day, company_id=company,
                          card_status="none" if day.card == 0 else "waiting")
        repo.till_days[doc_id] = till
        self.o.advance(item, Stage.ACQUIRED, evidence_ids, agent="discovery", note="Till report received.")
        self.o.advance(item, Stage.UNDERSTOOD, evidence_ids, agent=self.name,
                       note="Read the day's takings in cash, by card and in all.")
        self.log("read_till_report", subject_id=doc_id, evidence_ids=evidence_ids,
                 values={"day": day.day, "number": day.number, "cash": day.cash, "card": day.card, "other": day.other,
                         "total": day.total, "tax_id": day.tax_id or "", "location": day.location},
                 validations=[{"check": "adds_up", "ok": day.adds_up, "difference": day.total - day.parts}],
                 response={"quality": quality.value, "company": company or ""})
        self.o.activity(at, "collected", f"Collected the till report of {day_month(day.day, repo.today())}: "
                        f"{day.split()}.", company, amount=day.total, currency=day.currency,
                        evidence_ids=evidence_ids)
        report.document_ids.append(doc_id)
        when = day_month(day.day, repo.today())
        if not day.adds_up:
            self.o.advance(item, Stage.CONFLICT, evidence_ids, agent=self.name, note=day.mismatch())
            self._ask(record, till, at, prompt=f"The till report of {when} does not add up: {day.mismatch()} What "
                      "should I do?", why=(f"Cash: {day.money(day.cash)}", f"Card: {day.money(day.card)}",
                                           *([f"Other: {day.money(day.other)}"] if day.other else []),
                                           f"Total it states: {day.money(day.total)}",
                                           "Until you answer, I won't count or use this report."),
                      options=(CheckOption(id="aside", label="Set it aside. I'll send a corrected one."),))
        elif company is None:
            self.o.advance(item, Stage.NEEDS_OWNER, evidence_ids, agent=self.name, note="Which company it is for.")
            options = tuple(CheckOption(id=f"company:{c}", label=repo.company_name(c) or c,
                                        values={"company": c}) for c in sorted(repo.companies))
            self._ask(record, till, at, prompt=f"Which of your companies is the till report of {when} for?",
                      why=("It does not show a tax number I know.", f"Takings: {day.split()}."), options=options)
        return till, True

    def _ask(self, record: DocumentRecord, till: TillRecord, at: datetime, *, prompt: str, why: tuple[str, ...],
             options: tuple[CheckOption, ...]) -> NeedsYouRecord:
        repo = self.repo
        needs_id = _unique_id(repo.needs, "nd_till")
        needs = NeedsYouRecord(id=needs_id, kind="till", subject_type="document", subject_id=record.id,
                               item_id=record.item_id, company_id=till.company_id or next(iter(repo.companies), ""),
                               created_at=at, why=why, prompt=prompt, options=options)
        repo.needs[needs_id] = needs
        self.log("ask_owner", subject_id=record.id, evidence_ids=record.evidence_ids, values={"prompt": prompt},
                 response={"needs_you": needs_id})
        self.o.activity(at, "checked", f"I asked you about the till report of {day_month(till.till.day)}.",
                        till.company_id, evidence_ids=record.evidence_ids)
        return needs

    def answer(self, needs: NeedsYouRecord, option_id: str, answer_ev: str, now: datetime) -> AnswerOutcome:
        repo = self.repo
        option = next((o for o in needs.options if o.id == option_id), None)
        if option is None:
            raise ValueError("not one of the options")
        record = repo.documents[needs.subject_id]
        till = repo.till_days[record.id]
        item = repo.items[record.item_id]
        needs.status, needs.answer, needs.answered_at = "answered", option.id, now
        when = day_month(till.till.day, repo.today())
        if option_id == "aside":
            till.status = "set_aside"
            self.o.advance(item, Stage.NOT_REQUIRED, [*record.evidence_ids, answer_ev], agent=self.name,
                           actor=OWNER_ACTOR, note="Set aside: it does not add up. A corrected one will come.")
            self.o.activity(now, "answered", f"You set the till report of {when} aside until a corrected one "
                            "arrives.", till.company_id, evidence_ids=[answer_ev])
            return AnswerOutcome(ok=True, message="Done. I set it aside. When you send the corrected till report, "
                                                  "I will read it.")
        company = option.values["company"]
        till.company_id = company
        record.country = repo.company_country(company)  # its company's country (§49)
        company_record = repo.companies[company]
        record.document = record.document.model_copy(update={
            "entity_id": company, "supplier_tax_id": company_record.tax_id,
            "supplier_name": repo.legal_names.get(company) or company_record.name})
        self.o.advance(item, Stage.UNDERSTOOD, [*record.evidence_ids, answer_ev], agent=self.name,
                       actor=OWNER_ACTOR, note=f"You said it is {company_record.name}'s.")
        self.o.activity(now, "answered", f"You told me the till report of {when} is {company_record.name}'s.",
                        company, evidence_ids=[answer_ev])
        return AnswerOutcome(ok=True, message=f"Done. I counted it for {company_record.name}.")

    # ------------------------------------------------------------------ every run

    def settle(self, now: datetime) -> int:
        """Card takings against the card terminal's payout reports, cash deposits against the till's cash."""
        repo = self.repo
        if not repo.till_days and not any(self.is_deposit(r) for r in repo.transactions.values()):
            return 0
        moved = 0
        for till in sorted(repo.till_days.values(), key=lambda t: (t.till.day, t.document_id)):
            record = repo.documents[till.document_id]
            item = repo.items[record.item_id]
            if not till.usable or item.is_done or item.stage in (Stage.NEEDS_OWNER, Stage.CONFLICT):
                continue
            self._match_card(till)
            record.hold_reason = self.waiting_line(till)
            if till.card_status in ("none", "matched"):
                record.hold_reason = ""
                moved += self._close_day(till, record)
        moved += self._bank_deposits()
        return moved

    def _terminal_sales(self, company: str) -> list[tuple[Any, dict[date, Decimal]]]:
        """Settled card terminal payout reports of this company, with the card sales they show per day."""
        repo = self.repo
        out = []
        for s in sorted(repo.settlements.values(), key=lambda s: s.document_id):
            if not s.settled or not s.report.provider.is_card_terminal or s.transaction_id not in repo.transactions:
                continue
            if repo.transactions[s.transaction_id].company_id != company:
                continue
            per_day: dict[date, Decimal] = {}
            for line in s.report.lines:
                if line.on is not None:
                    per_day[line.on] = per_day.get(line.on, _ZERO) + line.sales - line.refunds - line.chargebacks
            out.append((s, per_day))
        return out

    def _match_card(self, till: TillRecord) -> None:
        if till.card_status in ("none", "matched") or till.company_id is None:
            return
        day = till.till.day
        terminal = self._terminal_sales(till.company_id)
        covering = [(s, per_day) for s, per_day in terminal if day in per_day]
        if covering:
            found = sum((per_day[day] for _, per_day in covering), _ZERO)
            if found == till.till.card:
                till.card_status, till.card_documents, till.card_found = "matched", [s.document_id for s, _ in
                                                                                    covering], None
            else:
                till.card_status, till.card_found = "differs", found
                till.card_documents = [s.document_id for s, _ in covering]
            return
        used = {d for t in self.repo.till_days.values() if t is not till for d in t.card_documents}
        undated = [s for s, per_day in terminal if not per_day and s.document_id not in used
                   and s.report.payout_date is not None and 0 < (s.report.payout_date - day).days <= CARD_PAYOUT_DAYS
                   and s.report.gross_sales - s.report.refunds == till.till.card]
        if len(undated) == 1:
            till.card_status, till.card_documents = "matched", [undated[0].document_id]

    def _close_day(self, till: TillRecord, record: DocumentRecord) -> int:
        repo = self.repo
        evidence = list(record.evidence_ids)
        for doc_id in till.card_documents:
            report_doc = repo.documents.get(doc_id)
            if report_doc is not None:
                evidence += report_doc.evidence_ids
            s = repo.settlements.get(doc_id)
            if s is not None and s.transaction_id in repo.transactions:
                evidence.append(repo.transactions[s.transaction_id].evidence_id)
        note = ("Its card takings match your card terminal's payout report; its cash went to the cash box."
                if till.card_status == "matched" else "All of it was cash: it went to the cash box.")
        return self.o.closure._close(repo.items[record.item_id], list(dict.fromkeys(evidence)), note=note)

    # ------------------------------------------------------------------ cash paid into the bank

    @staticmethod
    def is_deposit(rec: TxRecord) -> bool:
        return rec.decision is not None and rec.decision.rule == "cash_deposit" and rec.tx.amount > 0

    @staticmethod
    def is_withdrawal(rec: TxRecord) -> bool:
        return rec.decision is not None and rec.decision.rule in WITHDRAWAL_RULES and rec.tx.amount < 0

    def _available(self, rec: TxRecord) -> list[TillRecord]:
        """Till days of this deposit's company whose cash is not all in the bank yet, oldest first."""
        day = rec.tx.booked_on
        return sorted((t for t in self.repo.till_days.values()
                       if t.usable and t.company_id == rec.company_id and t.till.day <= day
                       and (day - t.till.day).days <= DEPOSIT_WINDOW_DAYS and t.cash_left > 0),
                      key=lambda t: (t.till.day, t.document_id))

    def _bank_deposits(self) -> int:
        repo = self.repo
        moved = 0
        for rec in sorted(repo.transactions.values(), key=lambda r: (r.tx.booked_on, r.id)):
            if not self.is_deposit(rec) or rec.private or rec.document_ids or rec.tx.entity_id is None \
                    or repo.items[rec.item_id].is_done:
                continue
            available = self._available(rec)
            amount = rec.tx.amount
            if sum((t.cash_left for t in available), _ZERO) < amount:
                continue  # not explained yet: the plan says so
            left, used = amount, []
            for t in available:
                if left <= 0:
                    break
                part = min(left, t.cash_left)
                t.banked[rec.id] = part
                used.append((t, part))
                left -= part
            rec.document_ids = [t.document_id for t, _ in used]
            rec.likely_document_ids = []
            days = join_and([day_month(t.till.day) for t, _ in used])
            kept = sum((t.cash_left for t, _ in used), _ZERO)
            rec.match_headline = "Cash from your till paid into the bank."
            rec.match_why = (
                f"Paid into the bank: {format_money(amount, rec.tx.currency)} in cash",
                (f"Cash sales in the till report{'s' if len(used) > 1 else ''} of {days}: "
                 f"{format_money(sum((t.till.cash for t, _ in used), _ZERO))}"),
                *([f"Still in the till from those days: {format_money(kept)}"] if kept else []),
            )
            self.log("bank_till_cash", subject_id=rec.id,
                     evidence_ids=[rec.evidence_id, *(e for t, _ in used for e in repo.documents[t.document_id].evidence_ids)],
                     values={"deposit": amount, "days": [str(t.till.day) for t, _ in used],
                             "parts": [part for _, part in used]}, validations=list(rec.match_why))
            moved += 1
        return moved

    def problem(self, rec: TxRecord) -> str | None:
        """For the auditor: None when this is not a cash deposit it matched; "" when it still holds."""
        if not self.is_deposit(rec) or not rec.document_ids:
            return None
        tills = [self.repo.till_days.get(d) for d in rec.document_ids]
        if any(t is None or not t.usable for t in tills):
            return "The cash paid in has lost its till reports."
        if sum((t.banked.get(rec.id, _ZERO) for t in tills if t is not None), _ZERO) != rec.tx.amount:
            return "The cash paid in no longer matches its till reports."
        return ""

    # ------------------------------------------------------------------ plain words

    def waiting_line(self, till: TillRecord) -> str:
        t = till.till
        when = day_month(t.day, self.repo.today())
        if till.card_status == "differs" and till.card_found is not None:
            return (f"The till report of {when} says {t.money(t.card)} was paid by card, but your card terminal's "
                    f"payout report shows {t.money(till.card_found)} of card sales that day. I won't close it until "
                    "you or your accountant check which is right.")
        if till.card_status == "waiting":
            return (f"The till report of {when} shows {t.money(t.card)} paid by card. I'm waiting for your card "
                    "terminal's payout report for that day to confirm it.")
        return ""

    def plan(self, rec: TxRecord) -> str | None:
        """The next step for cash paid into the bank that its till reports do not explain yet."""
        if not self.is_deposit(rec) or rec.document_ids:
            return None
        amount = format_money(rec.tx.amount, rec.tx.currency)
        when = day_month(rec.tx.booked_on, self.repo.today())
        available = self._available(rec)
        if not available:
            return (f"The {amount} of cash paid into the bank on {when} needs the till reports it comes from. Send me "
                    "the till reports (Z reports) of the days before, and I will match them.")
        cash = format_money(sum((t.cash_left for t in available), _ZERO))
        return (f"The {amount} of cash paid into the bank on {when} is more than the {cash} of till cash from the "
                "days before that is not in the bank yet. Send me the till reports that are missing, or tell me where "
                "the rest came from.")

    # ------------------------------------------------------------------ the cash box

    def entries(self, company_id: str) -> list[CashEntry]:
        repo = self.repo
        out: list[CashEntry] = []
        for rec in repo.transactions.values():
            if rec.private or rec.company_id != company_id:
                continue
            if self.is_withdrawal(rec):
                out.append(CashEntry(on=rec.tx.booked_on, amount=abs(rec.tx.amount), kind=BANK,
                                     label=self.o.merchant_name(rec.tx), subject_id=rec.id,
                                     evidence_ids=(rec.evidence_id,)))
            elif self.is_deposit(rec):
                out.append(CashEntry(on=rec.tx.booked_on, amount=-rec.tx.amount, kind=DEPOSIT,
                                     label="Paid into the bank", subject_id=rec.id, evidence_ids=(rec.evidence_id,)))
        for till in repo.till_days.values():
            if till.usable and till.company_id == company_id and till.till.cash:
                out.append(CashEntry(on=till.till.day, amount=till.till.cash, kind=TILL, label="Cash sales",
                                     subject_id=till.document_id,
                                     evidence_ids=tuple(repo.documents[till.document_id].evidence_ids)))
        for receipt in repo.member_receipts.values():  # a member who paid in cash at the desk (X12)
            if receipt.row.cash and receipt.status == "paid" and receipt.company_id == company_id:
                out.append(CashEntry(on=receipt.row.issued_on, amount=receipt.row.amount, kind=TILL,
                                     label=f"Paid in cash by {receipt.row.member}", subject_id=receipt.document_id,
                                     evidence_ids=tuple(repo.documents[receipt.document_id].evidence_ids)))
        for record in repo.documents.values():
            doc = record.document
            if not record.paid_in_cash or record.matched_tx_ids or record.claim_id is not None \
                    or doc.entity_id != company_id or doc.gross_amount is None \
                    or repo.items[record.item_id].stage is not Stage.CLOSED:
                continue
            out.append(CashEntry(on=doc.issue_date or record.received_at.astimezone(self._tz()).date(),
                                 amount=-abs(doc.gross_amount), kind=RECEIPT, label=doc.supplier_name or "Receipt",
                                 subject_id=record.id, evidence_ids=tuple(record.evidence_ids)))
        return sorted(out, key=lambda e: (e.on, e.subject_id))

    @staticmethod
    def _tz() -> Any:
        from backoffice.orchestrator import TZ

        return TZ

    def counts(self, company_id: str) -> list[CashCount]:
        return [c for c in self.repo.cash_counts if c.company_id == company_id]

    def period(self, company_id: str, month: Month) -> CashBoxPeriod | None:
        """The company's cash box over one month; None when it never had a cash box."""
        entries = self.entries(company_id)
        counts = self.counts(company_id)
        if not entries and not counts:
            return None
        start, end = month_bounds(month.year, month.month)
        return cash_box_period(entries, counts, start, end)

    def month_line(self, company_id: str, month: Month) -> dict[str, Any] | None:
        """The cash box in one plain line once the month is over (never before: cash is still moving)."""
        period = self.period(company_id, month)
        if period is None or not period.active or self.repo.today() <= month.last_day:
            return None
        return {"id": f"n_cash_box_{month}", "tone": "attention" if period.unexplained else "neutral",
                "text": period.line(period=MONTHS[month.month - 1])}

    def count(self, company_id: str, amount: Decimal, on: date, *, at: datetime) -> CashBoxPeriod:
        """The owner counted the cash box. Their word is the evidence; the difference is shown, never absorbed."""
        repo = self.repo
        body = json.dumps({"kind": "cash_count", "company": company_id, "amount": str(amount), "date": on.isoformat(),
                           "counted_by": repo.owner.email}, sort_keys=True).encode()
        reg = repo.registry.register(body, tenant_id=repo.tenant_id, source_kind=SourceKind.UPLOAD,
                                     format=EvidenceFormat.JSON, mime_type="application/json", retrieved_at=at,
                                     metadata={"kind": "cash_count"})
        counted = CashCount(company_id=company_id, on=on, amount=amount, evidence_id=reg.evidence.id)
        repo.cash_counts.append(counted)
        month = Month.of(on)
        period = self.period(company_id, month)
        assert period is not None
        self.log("cash_count", subject_id=f"{company_id}:{month}", evidence_ids=[reg.evidence.id], actor=OWNER_ACTOR,
                 values={"amount": amount, "date": on, "should_hold": period.expected_at_count},
                 response={"difference": period.difference})
        self.o.activity(at, "answered", f"You counted {format_money(amount)} in the cash box on {day_month(on)}.",
                        company_id, amount=amount, evidence_ids=[reg.evidence.id])
        return period

    def view(self, company_id: str, month: Month) -> dict[str, Any] | None:
        """The cash box and the till reports of one month, for the owner and the accountant."""
        repo = self.repo
        period = self.period(company_id, month)
        tills = sorted((t for t in repo.till_days.values() if t.company_id == company_id
                        and Month.of(t.till.day) == month), key=lambda t: t.till.day)
        if period is None and not tills:
            return None
        words = {BANK: "Taken from the bank", TILL: "Cash sales from the till", DEPOSIT: "Paid into the bank",
                 RECEIPT: "Cash receipts"}
        out: dict[str, Any] = {"month": str(month)}
        if period is not None:
            over = repo.today() > month.last_day
            out.update({
                "opening": _num(period.opening), "shouldHold": _num(period.closing),
                "cameIn": [{"label": words[k], "amount": _num(v)} for k, v in period.came_in.items()],
                "wentOut": [{"label": words[k], "amount": _num(v)} for k, v in period.went_out.items()],
                "counted": {"date": period.count.on.isoformat(), "amount": _num(period.count.amount),
                            "evidenceId": period.count.evidence_id} if period.count else None,
                "difference": _num(period.difference) if period.difference is not None else None,
                "unexplained": period.unexplained,
                "line": period.line(period=MONTHS[month.month - 1]) if over else "",
            })
        out["tillReports"] = [{
            "id": t.document_id, "date": t.till.day.isoformat(), "number": t.till.number, "cash": _num(t.till.cash),
            "card": _num(t.till.card), "other": _num(t.till.other), "total": _num(t.till.total),
            "addsUp": t.till.adds_up, "cardStatus": t.card_status,
            "cashInTheBank": _num(sum(t.banked.values(), _ZERO)), "cashInTheTill": _num(t.cash_left),
            "status": "set aside" if t.status == "set_aside" else
            ("closed" if repo.items[repo.documents[t.document_id].item_id].stage is Stage.CLOSED else "open"),
        } for t in tills]
        return out

    def lines_for(self, company_id: str | None = None) -> list[TillRecord]:
        return [t for t in self.repo.till_days.values() if t.usable
                and (company_id is None or t.company_id == company_id)]


def _num(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.01")))

