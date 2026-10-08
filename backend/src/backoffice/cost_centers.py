"""Cost centers as the owner sees them: each job, property, vehicle ... with its costs, income and proof.

Read-only views over the engine's records, used by the service
(``GET /api/companies/{id}/cost-centers``, ``GET /api/cost-centers/{id}``,
``GET /api/cost-centers/{id}/statement``) and by the chat ("how much did we
spend on Job Rua das Flores in September?"). Money comes only from payments
the engine has decided with a reason; every amount links to its evidence
(§39, §54). A cost center only ever shows its own company's payments and
documents.

A **statement** covers one period: the money received for the cost center
(rent and anything else), the costs put on it with their proof, the
management fee when one is set on it (a percent of the money received, a
fixed amount a month, or both) and what is left. For a property, apartment or
house it is the **owner statement**: what is due to its owner. A statement
with anything still open (a cost waiting for its invoice, an invoice not
paid yet, a payment put on it by history only, a payment waiting for the
owner to say where it goes) says so and is not final (§3).

Costs a client pays back (``backoffice.recharges``) are shown with what came
back for them.

Words are the business's own ("Job", "Property", "Vehicle"); no accounting
jargon (§36).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING, Any

from backoffice.closure import Month
from backoffice.countries import LazyPattern, pack_alternatives
from backoffice.domain.cost_centers import DEFAULT_KIND, CostCenter
from backoffice.domain.lifecycle import Stage
from backoffice.domain.models import CostAllocation
from backoffice.learning import day_month, display_name, fold, format_money
from backoffice.learning.cost_centers import noun_for, plural

if TYPE_CHECKING:  # pragma: no cover
    from backoffice.service import BackOfficeService

__all__ = ["CostCenterViews", "Period", "Totals"]

_ZERO = Decimal("0")
_DONE = (Stage.CLOSED, Stage.NOT_REQUIRED)


@dataclass(frozen=True)
class Period:
    start: date | None
    end: date | None
    label: str  # "September", "1 September to 15 October", "so far"
    phrase: str = ""  # "in September", "from 1 September to 15 October", "" (everything so far)

    def contains(self, day: date | None) -> bool:
        if day is None:
            return self.start is None and self.end is None
        return (self.start is None or day >= self.start) and (self.end is None or day <= self.end)

    def as_dict(self) -> dict[str, Any] | None:
        if self.start is None and self.end is None:
            return None
        return {"from": self.start.isoformat() if self.start else None,
                "to": self.end.isoformat() if self.end else None, "label": self.label}


ALL_TIME = Period(None, None, "so far", "")


@dataclass
class Totals:
    """One cost center's money over a period, with the payments and documents behind it."""

    spent: Decimal = _ZERO
    received: Decimal = _ZERO
    payments: list[dict[str, Any]] = field(default_factory=list)
    documents: list[dict[str, Any]] = field(default_factory=list)
    open_items: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[dict[str, str]] = field(default_factory=list)


def _num(value: Decimal | None) -> int | float | None:
    if value is None:
        return None
    value = value.quantize(Decimal("0.01"))
    return int(value) if value == value.to_integral_value() else float(value)


class CostCenterViews:
    """Per-cost-center summaries for one tenant (never changes anything)."""

    def __init__(self, svc: BackOfficeService) -> None:
        self.svc = svc
        self.repo = svc.repo
        self.today = svc._today()

    # ----------------------------------------------------------------- words

    def centers(self, company_id: str, *, active_only: bool = False) -> list[CostCenter]:
        return sorted((c for c in self.repo.cost_centers.values()
                       if c.company_id == company_id and (c.active or not active_only)),
                      key=lambda c: (not c.active, c.label.casefold(), c.id))

    def kind_for(self, company_id: str) -> str:
        """The company's own word: the kind most of its cost centers have ("Job" when it has none yet)."""
        kinds = [c.kind for c in self.centers(company_id, active_only=True)] or \
            [c.kind for c in self.centers(company_id)]
        if not kinds:
            return DEFAULT_KIND
        return max(dict.fromkeys(kinds), key=lambda k: kinds.count(k))

    def card(self, center: CostCenter) -> dict[str, Any]:
        fee = None
        if center.fee_percent is not None or center.fee_monthly is not None:
            fee = {"percent": _num(center.fee_percent), "monthly": _num(center.fee_monthly)}
        return {"id": center.id, "companyId": center.company_id, "name": center.name, "kind": center.kind,
                "label": center.label, "active": center.active, "identifiers": center.identifiers.as_dict(),
                "recharge": center.recharge, "owner": center.owner_name, "managementFee": fee,
                "isProperty": center.is_property}

    # ----------------------------------------------------------------- periods

    def period(self, body: Mapping[str, Any] | None) -> Period:
        """``month=2026-09`` or ``from``/``to`` (ISO dates); everything so far when neither is given."""
        from backoffice.service import ServiceError

        b = body or {}
        month = b.get("month")
        if month:
            try:
                m = Month.parse(str(month))
            except (ValueError, TypeError):
                raise ServiceError(400, "Use a month like 2026-09.") from None
            return Period(m.first_day, m.last_day, m.label(self.today), f"in {m.label(self.today)}")
        start = self.svc._date_arg(b, "from")
        end = self.svc._date_arg(b, "to")
        if start is None and end is None:
            return ALL_TIME
        if start and end and end < start:
            raise ServiceError(400, "The end date is before the start date.")
        label = (f"{day_month(start, self.today)} to {day_month(end, self.today)}" if start and end else
                 f"since {day_month(start, self.today)}" if start else f"until {day_month(end, self.today)}")  # type: ignore[arg-type]
        return Period(start, end, label, f"from {label}" if start and end else label)

    # ----------------------------------------------------------------- money

    def _why(self, allocation: CostAllocation) -> list[str]:
        lines = [*allocation.why, *(w for w in allocation.recharge_why if w not in allocation.why)]
        if allocation.quality.value != "verified" or allocation.recharge_method == "history":
            lines.append("Likely, not proven. Tell me if it is wrong.")
        return lines

    def shares(self, center: CostCenter, period: Period = ALL_TIME) -> list[tuple[Any, Decimal]]:
        """(payment record, the part of it on ``center``) for the company's payments in ``period``, oldest first."""
        out = []
        for rec in sorted(self.repo.transactions.values(), key=lambda r: (r.tx.booked_on, r.id)):
            allocation = rec.tx.cost_allocation
            if rec.private or rec.company_id != center.company_id or allocation is None:
                continue
            share = allocation.amount_for(center.id)
            if share and period.contains(rec.tx.booked_on):
                out.append((rec, share))
        return out

    def totals(self, center: CostCenter, period: Period = ALL_TIME) -> Totals:
        repo = self.repo
        svc = self.svc
        out = Totals()
        seen_docs: set[str] = set()
        for rec, share in self.shares(center, period):
            allocation = rec.tx.cost_allocation
            incoming = rec.tx.amount > 0
            if incoming:
                out.received += share
            else:
                out.spent += share
            item = repo.items[rec.item_id]
            evidence = [svc._tx_evidence(rec)]
            docs = [repo.documents[d] for d in rec.document_ids if d in repo.documents]
            evidence += [svc._doc_evidence(d) for d in docs]
            seen_docs.update(d.id for d in docs)
            closed = item.stage in _DONE
            out.payments.append({
                "id": rec.id, "date": rec.tx.booked_on.isoformat(), "merchant": svc.orchestrator.merchant_name(rec.tx),
                "direction": "in" if incoming else "out", "amount": _num(share), "total": _num(abs(rec.tx.amount)),
                "currency": rec.tx.currency, "split": allocation.is_split, "status": "closed" if closed else "open",
                "likely": allocation.quality.value != "verified", "why": self._why(allocation), "evidence": evidence,
                "toRecharge": bool(allocation.recharge_for(center.id)),
            })
            out.evidence += evidence
            if not closed:
                who = svc.orchestrator.merchant_name(rec.tx)
                missing = (rec.decision is not None and rec.decision.requires_document and not rec.document_ids
                           and not rec.proof_evidence_ids)
                text = (svc.orchestrator.missing.plan(rec) if missing else
                        f"The {who} payment of {format_money(abs(rec.tx.amount), rec.tx.currency)} on "
                        f"{day_month(rec.tx.booked_on, self.today)} is still being checked.")
                out.open_items.append({"id": rec.id, "text": text, "evidence": evidence[:1]})
        for record in sorted(repo.documents.values(), key=lambda d: (d.document.issue_date or date.min, d.id)):
            allocation = record.document.cost_allocation
            if allocation is None or not allocation.amount_for(center.id):
                continue
            company = repo.item_company(repo.items[record.item_id])
            if company != center.company_id:
                continue
            issued = record.document.issue_date or record.received_at.date()
            if not period.contains(issued):
                continue
            evidence = [svc._doc_evidence(record)]
            out.documents.append({
                "id": record.id, "date": issued.isoformat(), "supplier": display_name(record.document.supplier_name),
                "label": record.label, "amount": _num(allocation.amount_for(center.id)),
                "total": _num(allocation.total), "currency": allocation.currency, "split": allocation.is_split,
                "paid": bool(record.matched_tx_ids), "why": self._why(allocation), "evidence": evidence,
            })
            if record.id not in seen_docs:
                out.evidence += evidence
                if not record.matched_tx_ids:
                    out.open_items.append({
                        "id": record.id, "evidence": evidence,
                        "text": f"The {display_name(record.document.supplier_name)} "
                                f"{record.label.split(' · ')[0].lower()} is not paid yet."})
        unique: dict[str, dict[str, str]] = {}
        for e in out.evidence:
            unique.setdefault(e["id"], e)
        out.evidence = list(unique.values())
        return out

    # ----------------------------------------------------------------- views

    def company(self, company_id: str, body: Mapping[str, Any] | None = None) -> dict[str, Any]:
        repo = self.repo
        period = self.period(body)
        kind = self.kind_for(company_id)
        centers = self.centers(company_id)
        rows = []
        for center in centers:
            t = self.totals(center, period)
            rows.append({**self.card(center), "spent": _num(t.spent), "received": _num(t.received),
                         "payments": len(t.payments), "documents": len(t.documents), "openItems": len(t.open_items),
                         "currency": "EUR"})
        general_spent = general_received = _ZERO
        general_count = 0
        waiting: list[str] = []
        waiting_amount = _ZERO
        agent = self.svc.orchestrator.cost_centers
        for rec in sorted(repo.transactions.values(), key=lambda r: r.id):
            if rec.private or rec.company_id != company_id or not period.contains(rec.tx.booked_on):
                continue
            allocation = rec.tx.cost_allocation
            if allocation is not None and allocation.general:
                general_count += 1
                if rec.tx.amount > 0:
                    general_received += abs(rec.tx.amount)
                else:
                    general_spent += abs(rec.tx.amount)
            elif allocation is None and centers and rec.decision is not None and \
                    rec.decision.expectation in agent.ALLOCATABLE:
                waiting.append(rec.id)
                waiting_amount += abs(rec.tx.amount)
        needs = [n.id for n in repo.open_needs() if n.kind == "cost_center" and n.company_id == company_id]
        return {
            "companyId": company_id, "companyName": repo.company_name(company_id),
            "kind": kind, "kindPlural": plural(kind), "usesCostCenters": any(c.active for c in centers),
            "period": period.as_dict(), "costCenters": rows,
            "general": {"spent": _num(general_spent), "received": _num(general_received), "payments": general_count},
            "notDecided": {"payments": len(waiting), "amount": _num(waiting_amount), "needsYouIds": needs},
            "headline": self._headline(company_id, centers, len(waiting), needs),
        }

    def _headline(self, company_id: str, centers: list[CostCenter], waiting: int, needs: list[str]) -> str:
        active = [c for c in centers if c.active]
        if not active:
            return f"Add a {self.kind_for(company_id).lower()} and I will put each cost on the right one."
        noun = noun_for(active)
        if needs:
            one = "one payment" if len(needs) == 1 else f"{len(needs)} payments"
            return f"I need you to tell me which {noun} {one} {'is' if len(needs) == 1 else 'are'} for."
        if waiting:
            return f"Every cost has its {noun} or is waiting for its invoice."
        return f"Every cost is on its {noun}."

    def detail(self, center: CostCenter, body: Mapping[str, Any] | None = None) -> dict[str, Any]:
        period = self.period(body)
        t = self.totals(center, period)
        return {**self.card(center), "companyName": self.repo.company_name(center.company_id),
                "period": period.as_dict(), "currency": "EUR", "spent": _num(t.spent), "received": _num(t.received),
                "payments": t.payments, "documents": t.documents, "openItems": t.open_items, "evidence": t.evidence,
                "summary": self.summary_line(center, t, period), "recharged": self.recharge(center, period)}

    # ----------------------------------------------------------------- costs the client pays back

    def recharge(self, center: CostCenter, period: Period = ALL_TIME) -> dict[str, Any] | None:
        """What was bought for this client to pay back, what they paid back, and money held for them."""
        from backoffice.recharges import RechargeBook

        client = RechargeBook(self.repo).for_center(center.id)
        if client is None:
            return None
        found = client.summary(period.start, period.end)
        when = f" {period.phrase}" if period.phrase else ""
        if client.client_money and not found.received and not found.bought:
            text = f"Nothing received from or paid for {center.label}{when}. Their money is never your revenue."
        elif client.client_money:
            text = (f"{format_money(found.received)} received from {center.label}{when} is client money, not "
                    f"revenue. {format_money(found.bought)} was paid out for them.")
            if client.held > 0:
                text += f" {format_money(client.held)} of their money is still held for them."
        elif not found.bought:
            text = f"Nothing bought for {center.label} to recharge{when}."
        else:
            text = f"{format_money(found.bought)} bought for {center.label}{when}, to recharge to them."
            if found.paid_back >= found.bought:
                text += " All of it is paid back."
            elif found.paid_back:
                text += (f" {format_money(found.paid_back)} is already paid back; "
                         f"{format_money(found.outstanding)} is still to come.")
            else:
                text += " Nothing is paid back yet."
        svc = self.svc
        return {
            "clientMoney": client.client_money, "toRecharge": _num(found.bought), "paidBack": _num(found.paid_back),
            "outstanding": _num(found.outstanding), "received": _num(found.received), "held": _num(client.held),
            "costs": [{"id": rec.id, "date": rec.tx.booked_on.isoformat(),
                       "merchant": svc.orchestrator.merchant_name(rec.tx), "amount": _num(part),
                       "paidBack": _num(back), "evidence": [svc._tx_evidence(rec)]} for rec, part, back in found.costs],
            "receipts": [{"id": rec.id, "date": rec.tx.booked_on.isoformat(),
                          "merchant": svc.orchestrator.merchant_name(rec.tx), "amount": _num(part),
                          "evidence": [svc._tx_evidence(rec)]} for rec, part in found.receipts],
            "text": text,
        }

    # ----------------------------------------------------------------- the owner statement

    def statement(self, center: CostCenter, body: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Money received, costs with their proof, the management fee and what is left, for one period.

        The period is ``month=YYYY-MM`` or ``from``/``to``; without either, the month being closed.
        """
        b = dict(body or {})
        if not any(b.get(k) for k in ("month", "from", "to")):
            b["month"] = str(self.svc._current_month())
        period = self.period(b)
        t = self.totals(center, period)
        svc = self.svc
        received = [p for p in t.payments if p["direction"] == "in"]
        costs = [p for p in t.payments if p["direction"] == "out"]
        money_in = []
        for p in received:
            rec = self.repo.transactions[p["id"]]
            words = f"{rec.tx.counterparty} {rec.tx.description} {rec.tx.reference or ''}"
            kind = "rent" if _RENT.search(fold(words)) else "other"
            money_in.append({**p, "kind": kind, "label": "Rent" if kind == "rent" else "Money received"})
        fee = self._fee(center, period, t.received)
        net = t.received - t.spent - (fee[0] if fee else _ZERO)
        open_items = list(t.open_items)
        for p in t.payments:
            if p["likely"] and p["status"] == "closed":
                when = day_month(date.fromisoformat(p["date"]), self.today)
                open_items.append({"id": p["id"], "evidence": p["evidence"][:1],
                                   "text": f"The {p['merchant']} payment on {when} is on it by its history only, "
                                           "not proven."})
        waiting = [n for n in self.repo.open_needs() if n.kind in ("cost_center", "recharge")
                   and n.company_id == center.company_id and n.subject_type == "transaction"
                   and period.contains(self.repo.transactions[n.subject_id].tx.booked_on)]
        for n in waiting:
            rec = self.repo.transactions[n.subject_id]
            who = svc.orchestrator.merchant_name(rec.tx)
            noun = self.kind_for(center.company_id).lower()
            ask = (f"which {noun} it is for" if n.kind == "cost_center" else "whether the client pays it back")
            open_items.append({"id": n.id, "evidence": [svc._tx_evidence(rec)],
                               "text": f"The {who} payment of {format_money(abs(rec.tx.amount), rec.tx.currency)} "
                                       f"waits for you to say {ask}."})
        final = not open_items
        owner = center.owner_name or ("the owner" if center.is_property else "")
        title = (f"Owner statement · {center.label}" if center.is_property else f"Statement · {center.label}")
        title += f" · {period.label}" if period.start or period.end else ""
        when = f" {period.phrase}" if period.phrase else ""
        parts = [f"{center.label}{when}: {format_money(t.received)} received, {format_money(t.spent)} in costs"]
        if fee:
            parts[0] += f", {format_money(fee[0])} management fee"
        parts[0] += "."
        if center.is_property:
            who = owner[:1].upper() + owner[1:]
            parts.append(f"{who} is due {format_money(net)}." if net >= 0 else
                         f"The costs are more than the money received: {owner} owes {format_money(-net)}.")
        else:
            parts.append(f"What is left: {format_money(net)}.")
        parts.append("Final: every amount is proven." if final else
                     f"Not final: {len(open_items)} thing{'s are' if len(open_items) != 1 else ' is'} still open.")
        evidence = list(t.evidence)
        return {
            "costCenter": self.card(center), "companyName": self.repo.company_name(center.company_id),
            "title": title, "owner": center.owner_name, "ownerStatement": center.is_property,
            "period": period.as_dict(), "currency": "EUR",
            "moneyIn": money_in, "received": _num(t.received),
            "costs": costs, "spent": _num(t.spent),
            "managementFee": ({"amount": _num(fee[0]), "label": fee[1], "percent": _num(center.fee_percent),
                               "monthly": _num(center.fee_monthly)} if fee else None),
            "net": _num(net), "netDueToOwner": _num(net) if center.is_property else None,
            "documents": t.documents, "openItems": open_items, "final": final,
            "status": "final" if final else "not final", "evidence": evidence, "summary": " ".join(parts),
        }

    def _fee(self, center: CostCenter, period: Period, received: Decimal) -> tuple[Decimal, str] | None:
        """(the management fee for the period, how it was worked out) when one is set on the cost center."""
        if center.fee_percent is None and center.fee_monthly is None:
            return None
        amount = _ZERO
        words = []
        if center.fee_percent is not None:
            part = (received * center.fee_percent / Decimal(100)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            amount += part
            words.append(f"{_pct(center.fee_percent)} of {format_money(received)} received")
        if center.fee_monthly is not None:
            months = self._months(center, period)
            part = center.fee_monthly * months
            amount += part
            words.append(f"{format_money(center.fee_monthly)} a month for {months} month{'s' if months != 1 else ''}")
        return amount, "Management fee: " + " and ".join(words)

    def _months(self, center: CostCenter, period: Period) -> int:
        start, end = period.start, period.end
        if start is None or end is None:
            days = [rec.tx.booked_on for rec, _ in self.shares(center)]
            start = start or (min(days) if days else self.today)
            end = end or self.today
        return max(0, (end.year - start.year) * 12 + end.month - start.month + 1)

    def summary_line(self, center: CostCenter, t: Totals, period: Period) -> str:
        when = f" {period.phrase}" if period.phrase else ""
        if not t.payments and not t.documents:
            return f"Nothing for {center.label}{when} yet."
        parts = [f"{format_money(t.spent)} spent"]
        if t.received:
            parts.append(f"{format_money(t.received)} received")
        count = len(t.payments)
        parts.append(f"{count} payment{'s' if count != 1 else ''}")
        tail = f" {len(t.open_items)} still open." if t.open_items else ""
        return f"{center.label}{when}: {', '.join(parts)}.{tail}"


# Money received for a property that is its rent (or a stay's price), by the bank line's words (folded text; a
# pack's own in "cost_centers.rent").
_RENT = LazyPattern(lambda: (rf"(?<![a-z])(?:{pack_alternatives('cost_centers.rent')}|rent|rental|booking|airbnb|"
                             r"guest|stay)(?![a-z])"))


def _pct(value: Decimal | None) -> str:
    if value is None:
        return ""
    text = f"{value.normalize():f}" if value == value.to_integral_value() else f"{value:f}".rstrip("0").rstrip(".")
    return f"{text}%"
