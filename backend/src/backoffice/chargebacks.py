"""Card chargebacks on the bank statement (checklist I7; cases 5, 25).

A customer disputes a card payment; the card network takes the money back from the business: the acquirer
(SIBS, REDUNIQ, a card terminal) or the payment provider debits the account ("CHARGEBACK TPA 1234567
COMPRA 17/09/2026", "DISPUTA", "CONTESTAÇÃO", "ESTORNO VENDA TPA", "REVERSAL"). When the business wins the
dispute, the money comes back ("ESTORNO CHARGEBACK", "DISPUTA GANHA", "CHARGEBACK REVERSAL"). Disputes the
provider takes out of a payout are already in its payout report (backoffice.settlements); this module is
about the ones the bank statement shows on their own.

Neither is an expense nor income: money taken back is the sale going back, money won back is the sale
coming back. So neither needs an invoice. Each is proven by what it is linked to, never by its wording
alone (§3):

* money taken back: the sale it takes back, found in a payout report that settled its payout (the sale's
  reference, or the payout's, printed on the bank line; or the one sale of that amount on the sale date
  the line prints). Found: both bank lines and the report are its evidence. Not found: one plain question
  (which payout that sale was in; or the owner confirms it is a disputed card payment), never a guess;
* money won back: the money taken back that it returns (same amount, same provider, earlier, only one).
  Both close, linked. Not found: one plain question.

"estorno" and "reversal" alone also mean an ordinary refund of a purchase: they count only next to a card
sale (a card terminal, a payout provider, "venda"), never next to "compra" on the business's own card. A
chargeback *fee* ("COMISSAO CHARGEBACK") is the bank's charge, a cost like any other. The wording tables are
bank-statement conventions (unverified against live feeds): extend them as data is seen.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from backoffice.domain.lifecycle import Stage
from backoffice.domain.models import Quality, Transaction
from backoffice.learning import day_month, display_name, fold, format_money
from backoffice.orchestrator import (
    OWNER_ACTOR,
    AnswerOutcome,
    CheckOption,
    NeedsYouRecord,
    TxRecord,
    _Agent,
    _slug,
    _unique_id,
)
from backoffice.reconciliation import (
    CARD_TERMINAL,
    PAYOUT_PROVIDERS,
    EvidenceExpectation,
    ExpectationDecision,
    PayoutProvider,
    compatible_providers,
    is_card_purchase,
    payout_provider,
)
from backoffice.reconciliation.bank import phrase_in

__all__ = [
    "CHARGEBACK_RULES",
    "ChargebackAgent",
    "ChargebackRecord",
    "ChargebackWords",
    "chargeback_words",
]

_ZERO = Decimal("0")
CHARGEBACK_RULES = frozenset({"chargeback", "chargeback_won"})
# Decisions a chargeback never overrides: learned by the owner or accountant, money between own accounts, a card
# paid off, taxes, loans, salaries, a grant, the tourist tax, a bank's own declared fee.
_KEEP_RULES = frozenset({"learned", "zero_amount", "own_iban", "own_company_iban", "bank_marked_internal", "own_name",
                         "card_repayment", "bank_fee", "bank_fee_document", "loan_wording", "employee_iban",
                         "payroll_wording", "grant", "tourist_tax", "tax_wording", "expense_claim", "security_deposit"})
QUESTION_KIND = "chargeback"
LOOK_BACK_DAYS = 180  # a dispute can come months after the sale

# Folded words. "chargeback", "disputa" and "contestação" say it outright; "estorno" and "reversal" only next to
# a card sale.
_SAID = re.compile(r"(?<![a-z])(?:chargebacks?|charge\s+backs?|disputas?|disputes?|disputed|contestacao|contestacoes"
                   r"|contestada|contestado|retrocessao)(?![a-z])")
_REVERSAL = re.compile(r"(?<![a-z])(?:estornos?|reversals?|reversao|reversed)(?![a-z])")
_SALE = re.compile(r"(?<![a-z])(?:tpa|venda|vendas|terminal|pos|merchant|acquirer|adquirente)(?![a-z])")
_PURCHASE = re.compile(r"(?<![a-z])(?:compra|compras|purchase)(?![a-z])")
# The bank's own charge for handling a dispute: a cost, not the disputed money.
_FEE = re.compile(r"(?<![a-z])(?:comissao|comissoes|fee|fees|encargos?|custos?|despesas?)(?![a-z])")
_DATE = re.compile(r"(?<!\d)(\d{1,2})[/.-](\d{1,2})(?:[/.-](\d{4}|\d{2}))?(?![\d/.-]?\d)")
_TOKEN = re.compile(r"[a-z0-9_-]*\d[a-z0-9_-]*")


def _squash(text: str | None) -> str:
    return re.sub(r"[^0-9a-z]", "", fold(text or ""))


@dataclass(frozen=True)
class ChargebackWords:
    """What a bank line says about a disputed card payment."""

    won: bool  # money in: a disputed payment won back
    provider: PayoutProvider | None  # the card terminal or payment provider it names
    sale_on: date | None  # the sale date the line prints, if any
    tokens: tuple[str, ...]  # references printed on the line (squashed), to find the sale or payout by


def _provider(text: str) -> PayoutProvider | None:
    for provider in (*PAYOUT_PROVIDERS, CARD_TERMINAL):
        if phrase_in(text, provider.bank_phrases):
            return provider
    return None


def _sale_date(folded: str, booked_on: date) -> date | None:
    """The first date on the line: the day of the sale (a year it leaves out: the latest one up to the booking)."""
    for m in _DATE.finditer(folded):
        day, month = int(m.group(1)), int(m.group(2))
        years = [int(m.group(3)) + (2000 if len(m.group(3)) == 2 else 0)] if m.group(3) else \
            [booked_on.year, booked_on.year - 1]
        for year in years:
            try:
                found = date(year, month, day)
            except ValueError:
                continue
            if found <= booked_on:
                return found
    return None


def chargeback_words(tx: Transaction) -> ChargebackWords | None:
    """A disputed card payment taken back (money out) or won back (money in), or None (module docstring)."""
    text = f"{tx.counterparty} {tx.description} {tx.reference or ''}"
    folded = fold(text)
    said = bool(_SAID.search(folded))
    if not said and not _REVERSAL.search(folded):
        return None
    if _FEE.search(folded):
        return None  # the bank's charge for the dispute: a cost like any other
    provider = _provider(text)
    if not said and (provider is None and not _SALE.search(folded) or _PURCHASE.search(folded)):
        return None  # a reversal of a purchase is an ordinary refund
    if tx.amount > 0 and is_card_purchase(tx):
        return None  # a purchase on your own card, disputed and refunded: money back from a supplier
    dates = {m.group(0) for m in _DATE.finditer(folded)}
    tokens = tuple(dict.fromkeys(_squash(t) for t in _TOKEN.findall(folded)
                                 if t not in dates and len(_squash(t)) >= 4))
    return ChargebackWords(won=tx.amount > 0, provider=provider, sale_on=_sale_date(folded, tx.booked_on),
                           tokens=tokens)


@dataclass
class ChargebackRecord:
    """One disputed card payment on the bank statement, and what it is linked to.

    ``status``: "open" (looking for what it is linked to), "linked" (to the sale it takes back, or the money
    taken back it returns), "won_back" (money taken back whose sale was never found, then won back: linked to
    the money that came back), "confirmed" (the owner said it is a disputed card payment, not in the payouts
    on file) or "not_chargeback" (the owner said it is something else).
    """

    tx_id: str
    company_id: str
    direction: str  # "out": taken back from you; "in": won back
    provider: str  # as the owner reads it after "from": "SIBS", "your card terminal"; "" when not named
    provider_key: str | None
    amount: Decimal
    currency: str
    on: date
    sale_on: date | None
    tokens: tuple[str, ...]
    status: str = "open"
    payout_tx_id: str | None = None  # money out: the payout the sale was paid out in
    report_document_id: str | None = None  # the payout report that lists the sale
    sale_on_report: date | None = None
    reverses: str | None = None  # money in: the money taken back it returns (its payment id)
    won_back_by: str | None = None  # money out: the money that came back for it
    answer_ev: str | None = None  # the owner's answer (evidence id)
    not_for: list[str] = field(default_factory=list)  # payouts or chargebacks the owner said it is not

    @property
    def who(self) -> str:
        return self.provider or "The card network"


class ChargebackAgent(_Agent):
    """Disputed card payments on the bank statement, linked to what proves them (module docstring).

    Named "reconciliation" in the audit and the diagram: this is the matcher's work.
    """

    name = "reconciliation"

    # ----------------------------------------------------------------- what a bank line is

    def decision(self, rec: TxRecord, current: ExpectationDecision) -> ExpectationDecision | None:
        """The expected evidence for a disputed card payment: the sale or chargeback it is linked to, not an
        invoice. None when the line is not one (or the owner said it is something else)."""
        found = self.repo.chargebacks.get(rec.id)
        if found is not None and found.status == "not_chargeback":
            return None
        if current.rule in _KEEP_RULES or current.rule.startswith("tax") or current.rule == "deposit_refund":
            return None
        words = chargeback_words(rec.tx)
        if words is None:
            return None
        if words.won:
            return ExpectationDecision(
                rec.tx.id, EvidenceExpectation.BANK_EVIDENCE_SUFFICES,
                "Money back from a disputed card payment you won. It is not new income: I link it to the money "
                "taken back.", Quality.AMBER, "chargeback_won")
        return ExpectationDecision(
            rec.tx.id, EvidenceExpectation.BANK_EVIDENCE_SUFFICES,
            "A card payment taken back after a customer disputed it. It is not a cost: I link it to the sale it "
            "takes back.", Quality.AMBER, "chargeback")

    def _record(self, rec: TxRecord) -> ChargebackRecord | None:
        repo = self.repo
        found = repo.chargebacks.get(rec.id)
        if found is not None:
            return found
        words = chargeback_words(rec.tx)
        if words is None:
            return None
        provider = words.provider
        cb = ChargebackRecord(
            tx_id=rec.id, company_id=rec.company_id, direction="in" if words.won else "out",
            provider=provider.title if provider is not None else "", provider_key=provider.key if provider else None,
            amount=abs(rec.tx.amount), currency=rec.tx.currency.strip().upper(), on=rec.tx.booked_on,
            sale_on=words.sale_on, tokens=words.tokens)
        repo.chargebacks[rec.id] = cb
        self.log("record_chargeback", subject_id=rec.id, evidence_ids=[rec.evidence_id],
                 values={"direction": cb.direction, "amount": cb.amount, "provider": cb.provider_key,
                         "sale_on": cb.sale_on})
        return cb

    def _open(self, rec: TxRecord) -> bool:
        item = self.repo.items[rec.item_id]
        return (not rec.private and rec.tx.entity_id is not None and not item.is_done and not rec.document_ids
                and rec.decision is not None and rec.decision.rule in CHARGEBACK_RULES)

    # ----------------------------------------------------------------- linking (before closing)

    def link(self, now: datetime) -> int:
        """Money taken back linked to its sale; money won back linked to the money taken back. Only one way to
        link each, never a guess."""
        repo = self.repo
        if not any(r.decision is not None and r.decision.rule in CHARGEBACK_RULES for r in repo.transactions.values()):
            return 0
        moved = 0
        for rec in sorted(repo.transactions.values(), key=lambda r: (r.tx.booked_on, r.id)):
            if rec.decision is None or rec.decision.rule not in CHARGEBACK_RULES:
                continue
            cb = self._record(rec)
            if cb is None or cb.status != "open" or not self._open(rec):
                continue
            if cb.direction == "out":
                found = self._sales(cb)
                if len(found) == 1:
                    payout, settlement, line_on = found[0]
                    self._link_sale(cb, rec, payout, settlement.document_id, line_on, answer_ev=None, now=now)
                    moved += 1
            else:
                taken = self._taken_back(cb)
                if len(taken) == 1:
                    self._link_won(cb, rec, taken[0], answer_ev=None, now=now)
                    moved += 1
        return moved

    def _compatible(self, cb: ChargebackRecord, provider: PayoutProvider | None) -> bool:
        if cb.provider_key is None or provider is None:
            return True
        mine = next((p for p in (*PAYOUT_PROVIDERS, CARD_TERMINAL) if p.key == cb.provider_key), None)
        return mine is None or compatible_providers(mine, provider)

    def _sales(self, cb: ChargebackRecord) -> list[tuple[TxRecord, Any, date | None]]:
        """(payout, its settled report, the sale's date) for each sale this could take back: a sale or payout
        reference printed on the bank line, or the one sale of this amount on the sale date it prints."""
        repo = self.repo
        out: dict[tuple[str, int], tuple[TxRecord, Any, date | None]] = {}
        for s in sorted(repo.settlements.values(), key=lambda s: s.document_id):
            payout = repo.transactions.get(s.transaction_id or "")
            report = s.report
            if not s.settled or payout is None or payout.company_id != cb.company_id or payout.id in cb.not_for:
                continue
            if payout.tx.booked_on > cb.on or (cb.on - payout.tx.booked_on).days > LOOK_BACK_DAYS:
                continue
            if report.currency != cb.currency or not self._compatible(cb, report.provider):
                continue
            payout_named = bool(report.payout_id) and _squash(report.payout_id) in cb.tokens
            for i, line in enumerate(report.lines):
                if line.kind.value != "sale" or line.sales != cb.amount:
                    continue
                by_reference = bool(line.reference) and _squash(line.reference) in cb.tokens
                by_date = cb.sale_on is not None and line.on == cb.sale_on
                if by_reference or payout_named or by_date:
                    out[(payout.id, i)] = (payout, s, line.on)
        return list(out.values())

    def _taken_back(self, cb: ChargebackRecord) -> list[ChargebackRecord]:
        """Money taken back that this money won back could return: same company, amount, currency and provider,
        earlier, not won back yet."""
        repo = self.repo
        mine = next((p for p in (*PAYOUT_PROVIDERS, CARD_TERMINAL) if p.key == cb.provider_key), None)
        return [o for o in sorted(repo.chargebacks.values(), key=lambda o: (o.on, o.tx_id))
                if o.direction == "out" and o.status in ("open", "linked", "confirmed") and o.won_back_by is None
                and o.company_id == cb.company_id and o.amount == cb.amount and o.currency == cb.currency
                and o.on <= cb.on and o.tx_id not in cb.not_for and self._compatible(o, mine)]

    def _payouts(self, cb: ChargebackRecord) -> list[TxRecord]:
        """Payouts from this provider the sale may have been in (for the owner's question), most recent first."""
        repo = self.repo
        out = []
        for r in repo.transactions.values():
            if r.company_id != cb.company_id or r.tx.amount <= 0 or r.id in cb.not_for or r.decision is None \
                    or r.decision.expectation is not EvidenceExpectation.PAYOUT_REPORT:
                continue
            if r.tx.booked_on > cb.on or (cb.on - r.tx.booked_on).days > LOOK_BACK_DAYS or r.tx.amount < cb.amount:
                continue
            if self._compatible(cb, payout_provider(r.tx)):
                out.append(r)
        return sorted(out, key=lambda r: (r.tx.booked_on, r.id), reverse=True)

    def _link_sale(self, cb: ChargebackRecord, rec: TxRecord, payout: TxRecord, report_document_id: str | None,
                   sale_on: date | None, *, answer_ev: str | None, now: datetime) -> None:
        repo = self.repo
        today = repo.today()
        cb.status, cb.payout_tx_id, cb.answer_ev = "linked", payout.id, answer_ev
        cb.report_document_id, cb.sale_on_report = report_document_id, sale_on
        money = format_money(cb.amount, cb.currency)
        paid = format_money(abs(payout.tx.amount), payout.tx.currency)
        payout_day = day_month(payout.tx.booked_on, today)
        sale = f"the sale of {day_month(sale_on, today)}" if sale_on else "a sale"
        how = ("You said: the sale was in this payout" if answer_ev else
               "Payout report: it lists the sale, of the same amount")
        rec.match_why = (f"Taken back: {money} on {day_month(cb.on, today)}",
                         f"Sale: {money}{' on ' + day_month(sale_on, today) if sale_on else ''}",
                         f"Paid out in the {paid} payout of {payout_day}", how)
        rec.match_headline = f"A disputed card payment taken back: {sale}, paid out on {payout_day}."
        rec.likely_document_ids = []
        evidence = [rec.evidence_id, payout.evidence_id,
                    *(repo.documents[report_document_id].evidence_ids if report_document_id in repo.documents else []),
                    *([answer_ev] if answer_ev else [])]
        self.log("link_chargeback", subject_id=rec.id, evidence_ids=evidence,
                 values={"payout": payout.id, "report": report_document_id, "sale_on": sale_on, "amount": cb.amount},
                 validations=list(rec.match_why), actor=OWNER_ACTOR if answer_ev else "system")
        self.o.activity(now, "checked", f"Linked the {money} {cb.who if cb.provider else 'the card network'} took "
                        f"back on {day_month(cb.on, today)} to {sale} in the payout of {payout_day}. It is not a cost.",
                        rec.company_id, amount=cb.amount, currency=cb.currency, evidence_ids=evidence)

    def _link_won(self, cb: ChargebackRecord, rec: TxRecord, taken: ChargebackRecord, *, answer_ev: str | None,
                  now: datetime) -> None:
        repo = self.repo
        today = repo.today()
        out = repo.transactions[taken.tx_id]
        cb.status, cb.reverses, cb.answer_ev = "linked", taken.tx_id, answer_ev
        taken.won_back_by = rec.id
        if taken.status == "open":
            taken.status = "won_back"  # linked to the money that came back, though its sale was never found
        for n in repo.needs.values():  # the money came back: nothing left to ask about what was taken back
            if n.subject_id == taken.tx_id and n.status == "open" and n.kind == QUESTION_KIND:
                n.status, n.resolution = "resolved", "evidence"
        money = format_money(cb.amount, cb.currency)
        how = "You said: it gives that money back" if answer_ev else "The same amount, from the same provider"
        rec.match_why = (f"Taken back: {money} on {day_month(taken.on, today)}",
                         f"Won back: {money} on {day_month(cb.on, today)}", how)
        taken_on = day_month(taken.on, today)
        rec.match_headline = f"A disputed card payment won back: the {money} taken back on {taken_on}."
        if not out.document_ids:
            out.match_why = (*(out.match_why or (f"Taken back: {money} on {taken_on}",)),
                             f"Won back: {money} on {day_month(cb.on, today)}")
            if taken.status == "won_back":  # its sale was never found: the money won back is what it is linked to
                out.match_headline = f"A disputed card payment taken back, then won back on {day_month(cb.on, today)}."
        evidence = [rec.evidence_id, out.evidence_id, *([answer_ev] if answer_ev else [])]
        self.log("link_chargeback_won", subject_id=rec.id, evidence_ids=evidence,
                 values={"taken_back": taken.tx_id, "amount": cb.amount}, validations=list(rec.match_why),
                 actor=OWNER_ACTOR if answer_ev else "system")
        self.o.activity(now, "checked", f"Linked the {money} that came back on {day_month(cb.on, today)} to the "
                        f"disputed card payment taken back on {day_month(taken.on, today)}. It is not new income.",
                        rec.company_id, amount=cb.amount, currency=cb.currency, evidence_ids=evidence)

    # ----------------------------------------------------------------- closing (the closure agent asks)

    def settled(self, rec: TxRecord) -> tuple[list[str], str] | None:
        """(evidence, note) for a disputed card payment that is linked, or confirmed by the owner; else None."""
        repo = self.repo
        cb = repo.chargebacks.get(rec.id)
        if cb is None or rec.document_ids:
            return None
        answer = [cb.answer_ev] if cb.answer_ev else []
        if cb.direction == "in" and cb.status == "linked":
            taken = repo.transactions.get(cb.reverses or "")
            if taken is None or abs(taken.tx.amount) != abs(rec.tx.amount):
                return None
            note = rec.match_headline or "A disputed card payment won back."
            return [rec.evidence_id, taken.evidence_id, *answer], note
        if cb.direction == "in" and cb.status == "confirmed" and cb.answer_ev:
            return [rec.evidence_id, cb.answer_ev], "Money back from a disputed card payment, as you confirmed."
        if cb.direction != "out":
            return None
        won = repo.transactions.get(cb.won_back_by or "")
        if cb.status == "linked":
            payout = repo.transactions.get(cb.payout_tx_id or "")
            if payout is None:
                return None
            report = repo.documents[cb.report_document_id].evidence_ids if cb.report_document_id in repo.documents \
                else []
            back = [won.evidence_id] if won is not None else []
            return [rec.evidence_id, payout.evidence_id, *report, *answer, *back], \
                rec.match_headline or "A disputed card payment taken back."
        if won is not None and abs(won.tx.amount) == abs(rec.tx.amount):  # taken back, then won back: both linked
            note = rec.match_headline or "A disputed card payment won back."
            return [rec.evidence_id, won.evidence_id, *answer], note
        if cb.status == "confirmed" and cb.answer_ev:
            return [rec.evidence_id, cb.answer_ev], "A disputed card payment taken back, as you confirmed."
        return None

    # ----------------------------------------------------------------- one question, never a guess

    def ask(self, now: datetime) -> None:
        repo = self.repo
        if not repo.chargebacks:
            return
        asked = {n.subject_id for n in repo.needs.values() if n.status == "open"}
        for cb in sorted(repo.chargebacks.values(), key=lambda c: (c.on, c.tx_id)):
            rec = repo.transactions.get(cb.tx_id)
            if rec is None or cb.status != "open" or cb.tx_id in asked or not self._open(rec):
                continue
            if cb.direction == "out" and cb.won_back_by is not None:
                continue  # won back: linked to the money that came back
            if repo.items[rec.item_id].stage is Stage.NEEDS_OWNER:
                continue
            (self._ask_taken if cb.direction == "out" else self._ask_won)(cb, rec, now)

    def _question(self, cb: ChargebackRecord, rec: TxRecord, prompt: str, options: list[CheckOption],
                  why: list[str], evidence: list[str], now: datetime, note: str) -> None:
        repo = self.repo
        needs_id = _unique_id(repo.needs, f"nd_{_slug((cb.provider or 'card').split()[0])}_disputed")
        repo.needs[needs_id] = NeedsYouRecord(
            id=needs_id, kind=QUESTION_KIND, subject_type="transaction", subject_id=rec.id, item_id=rec.item_id,
            company_id=rec.company_id, created_at=now, why=tuple(why), prompt=prompt, options=tuple(options))
        self.log("ask_owner", subject_id=rec.id, evidence_ids=evidence,
                 values={"options": [o.id for o in options]}, response={"needs_you": needs_id})
        self.o.activity(now, "checked", note, rec.company_id, amount=cb.amount, currency=cb.currency,
                        evidence_ids=evidence)
        self.o.advance(repo.items[rec.item_id], Stage.NEEDS_OWNER, evidence, agent=self.name, note=note)

    def _ask_taken(self, cb: ChargebackRecord, rec: TxRecord, now: datetime) -> None:
        today = self.repo.today()
        money = format_money(cb.amount, cb.currency)
        payouts = self._payouts(cb)[:3]
        options = [CheckOption(id=f"payout:{p.id}", label=f"The {format_money(abs(p.tx.amount), p.tx.currency)} "
                               f"payout of {day_month(p.tx.booked_on, today)}", values={"payout": p.id})
                   for p in payouts]
        if payouts:
            prompt = (f"{cb.who} took {money} back on {day_month(cb.on, today)} for a disputed card payment. Which "
                      "payout was that sale in?")
            options.append(CheckOption(id="sale", label="A card sale, but not in these payouts"))
        else:
            prompt = (f"{cb.who} took {money} back on {day_month(cb.on, today)}. Is it a card payment a customer "
                      "disputed?")
            options.append(CheckOption(id="sale", label="Yes, a customer disputed a card payment"))
        options.append(CheckOption(id="other", label="No, it is something else"))
        why = ["The bank line says a customer disputed a card payment and the money was taken back.",
               "I can't find that sale in your payout reports, so I won't link it on a guess.",
               "It is not a cost, so I don't count it as one."]
        self._question(cb, rec, prompt, options, why, [rec.evidence_id, *(p.evidence_id for p in payouts)], now,
                       f"Asked you which sale the {money} taken back on {day_month(cb.on, today)} is for.")

    def _ask_won(self, cb: ChargebackRecord, rec: TxRecord, now: datetime) -> None:
        today = self.repo.today()
        money = format_money(cb.amount, cb.currency)
        taken = self._taken_back(cb)[:3]
        options = [CheckOption(id=f"taken:{o.tx_id}", label=f"Yes, the {format_money(o.amount, o.currency)} taken "
                               f"back on {day_month(o.on, today)}", values={"taken": o.tx_id}) for o in taken]
        if taken:
            prompt = (f"{money} came back on {day_month(cb.on, today)} for a disputed card payment. Which money "
                      "taken back does it return?" if len(taken) > 1 else
                      f"{money} came back on {day_month(cb.on, today)} for a disputed card payment. Does it return "
                      f"the {format_money(taken[0].amount, taken[0].currency)} taken back on "
                      f"{day_month(taken[0].on, today)}?")
            options.append(CheckOption(id="won", label="It was won back, but not one of these"))
        else:
            prompt = (f"{money} came back on {day_month(cb.on, today)}. Is it a disputed card payment you won "
                      "back?")
            options.append(CheckOption(id="won", label="Yes, a disputed card payment I won back"))
        options.append(CheckOption(id="other", label="No, it is something else"))
        why = ["The bank line says it is money back from a disputed card payment.",
               "More than one payment taken back could fit, so I won't link them on a guess." if len(taken) > 1 else
               "I can't be sure which payment taken back it returns, so I won't link them on a guess." if taken else
               "I can't find the money taken back that it returns.",
               "It is not new income, so I don't count it as income."]
        self._question(cb, rec, prompt, options, why,
                       [rec.evidence_id, *(self.repo.transactions[o.tx_id].evidence_id for o in taken)], now,
                       f"Asked you about the {money} that came back on {day_month(cb.on, today)}.")

    def answer(self, needs: NeedsYouRecord, option_id: str, answer_ev: str, now: datetime) -> AnswerOutcome:
        """The owner's one tap on a disputed card payment (§19, §37). Their answer is evidence (§55)."""
        repo = self.repo
        option = next((o for o in needs.options if o.id == option_id), None)
        if option is None:
            raise ValueError("not one of the options")
        rec = repo.transactions[needs.subject_id]
        cb = repo.chargebacks[rec.id]
        item = repo.items[rec.item_id]
        owner = f"{OWNER_ACTOR}:{repo.owner.email}"
        money = format_money(cb.amount, cb.currency)
        needs.status, needs.answer, needs.answered_at = "answered", option.id, now
        kind, _, target = option.id.partition(":")
        if kind == "other":
            cb.status = "not_chargeback"
            cb.not_for += [o.values.get("payout") or o.values.get("taken") for o in needs.options
                           if o.values.get("payout") or o.values.get("taken")]
            rec.decision = self.o.reconciliation.engine().classify(rec.tx)
            self.log("not_a_chargeback", subject_id=rec.id, evidence_ids=[rec.evidence_id, answer_ev],
                     actor=OWNER_ACTOR)
            self.o.advance(item, Stage.UNDERSTOOD, [rec.evidence_id, answer_ev], agent=self.name, actor=owner,
                           note="Not a disputed card payment, as you said.")
            self.o.activity(now, "answered", f"You said the {money} is not a disputed card payment.", rec.company_id,
                            evidence_ids=[answer_ev])
            return AnswerOutcome(ok=True, message="Done. I'll treat it as an ordinary payment.")
        if kind == "payout":
            payout = repo.transactions.get(target)
            if payout is None:
                raise ValueError("not one of the options")
            settlement = next((s for s in repo.settlements.values() if s.settled and s.transaction_id == payout.id),
                              None)
            self._link_sale(cb, rec, payout, settlement.document_id if settlement else None, None, answer_ev=answer_ev,
                            now=now)
            message = f"Done. I linked the {money} taken back to the payout of {day_month(payout.tx.booked_on)}."
        elif kind == "taken":
            taken = repo.chargebacks.get(target)
            if taken is None or taken.won_back_by is not None:
                raise ValueError("That money taken back is already linked.")
            self._link_won(cb, rec, taken, answer_ev=answer_ev, now=now)
            message = f"Done. I linked the {money} that came back to the money taken back on {day_month(taken.on)}."
        else:  # "sale" / "won": the owner confirms it is a disputed card payment
            cb.status, cb.answer_ev = "confirmed", answer_ev
            self.log("confirm_chargeback", subject_id=rec.id, evidence_ids=[rec.evidence_id, answer_ev],
                     actor=OWNER_ACTOR, values={"direction": cb.direction})
            rec.match_headline = ("Money back from a disputed card payment, as you confirmed." if cb.direction == "in"
                                  else "A disputed card payment taken back, as you confirmed.")
            message = ("Done. I recorded it as a disputed card payment you won back." if cb.direction == "in" else
                       "Done. I recorded it as a disputed card payment taken back.")
        self.o.advance(item, Stage.UNDERSTOOD, [rec.evidence_id, answer_ev], agent=self.name, actor=owner,
                       note="As you said.")
        self.o.activity(now, "answered", f"You told me what the {money} disputed card payment is.", rec.company_id,
                        evidence_ids=[answer_ev])
        return AnswerOutcome(ok=True, message=message + " It is neither income nor a cost.")

    # ----------------------------------------------------------------- plain words

    def plan(self, rec: TxRecord) -> str | None:
        """The next step for a disputed card payment still open."""
        cb = self.repo.chargebacks.get(rec.id)
        if cb is None or cb.status in ("not_chargeback",):
            return None
        today = self.repo.today()
        money = format_money(cb.amount, cb.currency)
        when = day_month(cb.on, today)
        asked = any(n.subject_id == rec.id and n.status == "open" and n.kind == QUESTION_KIND
                    for n in self.repo.needs.values())
        if cb.direction == "in":
            return (f"{money} came back on {when} for a disputed card payment. "
                    + ("I asked you which payment taken back it returns." if asked else
                       "I'm linking it to the money taken back."))
        return (f"{cb.who} took {money} back on {when} for a disputed card payment. "
                + ("I asked you which sale it takes back." if asked else "I'm looking for the sale it takes back."))

    def text(self, rec: TxRecord) -> str | None:
        """Where a disputed card payment stands, for its details page."""
        cb = self.repo.chargebacks.get(rec.id)
        if cb is None or cb.status == "not_chargeback":
            return None
        if self.repo.items[rec.item_id].is_done and rec.match_headline:
            return rec.match_headline
        return self.plan(rec)

    def merchant(self, rec: TxRecord) -> str:
        cb = self.repo.chargebacks.get(rec.id)
        return cb.provider if cb is not None and cb.provider else display_name(rec.tx.counterparty)
