"""Cost centers as the owner sees them: each job, property, vehicle ... with its costs, income and proof.

Read-only views over the engine's records, used by the service
(``GET /api/companies/{id}/cost-centers``, ``GET /api/cost-centers/{id}``)
and by the chat ("how much did we spend on Job Rua das Flores in
September?"). Money comes only from payments the engine has decided with a
reason; every amount links to its evidence (§39, §54). A cost center only ever
shows its own company's payments and documents.

Words are the business's own ("Job", "Property", "Vehicle"); no accounting
jargon (§36).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from backoffice.closure import Month
from backoffice.domain.cost_centers import DEFAULT_KIND, CostCenter
from backoffice.domain.lifecycle import Stage
from backoffice.domain.models import CostAllocation
from backoffice.learning import day_month, display_name, format_money
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
        return {"id": center.id, "companyId": center.company_id, "name": center.name, "kind": center.kind,
                "label": center.label, "active": center.active, "identifiers": center.identifiers.as_dict()}

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
        lines = list(allocation.why)
        if allocation.quality.value != "verified":
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
                "summary": self.summary_line(center, t, period)}

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
