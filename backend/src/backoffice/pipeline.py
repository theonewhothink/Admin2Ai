"""What the system is doing, as data for the Diagram page (§3, §46, §54).

Every administrative event is a tracked item that moves along the golden rule
(Discover → Acquire → Understand → Verify → Match → Act → Confirm → Close),
one evidence-carrying step at a time, each step taken by a named agent. This
module reads those items and their histories straight from the engine, so the
diagram shows what actually happened, not an illustration:

* how many items are at each step right now, and how many passed through it;
* the side states (waiting for the owner, conflict, no document needed);
* where items came from (bank, cards, email, scans, portals, uploads);
* which agent did how much of the work;
* every item's journey: each step, who took it, when, why and on what evidence.

Plain language throughout (§36): the owner sees "Checked", not "VERIFIED".
"""

from __future__ import annotations

from collections import Counter
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from backoffice.domain.lifecycle import ORDER, Stage, TrackedItem
from backoffice.learning import day_month, display_name

if TYPE_CHECKING:  # pragma: no cover
    from backoffice.service import BackOfficeService

__all__ = ["AGENTS", "SOURCES", "STAGES", "build_pipeline"]

# The golden rule, in the owner's words.
STAGES: dict[Stage, tuple[str, str]] = {
    Stage.DISCOVERED: ("Found", "A new email, bank payment, card charge, scan or letter arrives."),
    Stage.ACQUIRED: ("Collected", "The original is fetched and stored untouched: the PDF, the bank line, the page "
                                  "behind a “View invoice” link."),
    Stage.UNDERSTOOD: ("Read", "Supplier, amounts, VAT, dates and bank details are read, and I decide what proof "
                               "this item needs."),
    Stage.VERIFIED: ("Checked", "The numbers are checked against each other and against a second source, such as "
                                "the invoice QR code or the bank."),
    Stage.MATCHED: ("Matched", "Each payment is paired with its invoice, receipt or proof."),
    Stage.ACTED: ("Acted", "Anything needed is done: chasing a supplier, answering the accountant."),
    Stage.CONFIRMED: ("Confirmed", "The result is confirmed by evidence, never assumed."),
    Stage.CLOSED: ("Closed", "Done, with proof. It counts toward closing the month."),
}
SIDE: dict[Stage, tuple[str, str]] = {
    Stage.NEEDS_OWNER: ("Waiting for you", "I need one answer or approval from you before I can go on."),
    Stage.CONFLICT: ("Conflict", "Two sources disagree. Nothing moves until a person decides."),
    Stage.NOT_REQUIRED: ("No document needed", "For example, money moved between your own accounts."),
}

# The agents (§46), named for the owner.
AGENTS: dict[str, tuple[str, str]] = {
    "discovery": ("Discovery", "Notices every new email, payment, card charge, scan and letter."),
    "retrieval": ("Retrieval", "Fetches the original document, including from links and supplier portals."),
    "document": ("Document reader", "Reads supplier, amounts, VAT, dates and bank details."),
    "verification": ("Checker", "Checks the numbers against each other and a second source."),
    "entity": ("Company assignment", "Works out which of your companies each item belongs to."),
    "reconciliation": ("Matcher", "Pairs payments with invoices."),
    "settlement": ("Payout checker", "Checks payouts from card terminals and sales platforms against their payout "
                                     "reports: sales, fees and refunds."),
    "closure": ("Closure", "Checks, matches and closes items, only with evidence."),
    "missing": ("Invoice chaser", "Finds missing invoices, and asks the supplier when they can't be found."),
    "obligation": ("Deadlines", "Tracks tax payments and other things with a due date."),
    "accountant": ("Accountant assistant", "Answers routine questions from your accountant."),
    "fraud": ("Fraud check", "Stops payments when bank details change or something looks wrong."),
    "auditor": ("Auditor", "Checks the system's own work."),
    "owner": ("You", "Answers and approvals you gave."),
}

SOURCES: dict[str, str] = {
    "bank": "Bank",
    "card": "Cards",
    "email": "Email",
    "mobile_scan": "Phone scans",
    "supplier_portal": "Supplier portals",
    "upload": "Uploads",
    "share": "Shared from your phone",
    "drive": "Cloud storage",
    "accountant": "Accountant",
    "government": "Government letters",
}

_QUALITY = {"verified": "verified", "likely": "likely", "conflict": "conflict"}
_UNITS = {"missing": ("supplier asked", "suppliers asked"), "obligation": ("deadline tracked", "deadlines tracked"),
          "accountant": ("accountant question", "accountant questions")}
_PAYMENT_KIND = {"direct_debit": "Direct debit", "card": "Card payment", "transfer_out": "Transfer",
                 "transfer_in": "Money in", "fee": "Bank charge"}


def _agent(actor: str) -> str:
    """'system:closure' → 'closure'; anything a person did → 'owner'."""
    kind, _, name = actor.partition(":")
    if kind == "system" and name:
        return name.split(":")[0]
    return "owner"


def _money(value: Any) -> float | None:
    if value is None:
        return None
    return float(abs(Decimal(str(value))))


def _source(svc: BackOfficeService, item: TrackedItem) -> str:
    for step in item.history:
        for ev_id in step.evidence_ids:
            try:
                kind = svc.repo.evidence(ev_id).source_kind
            except Exception:  # an id the registry doesn't hold: skip it
                continue
            return str(getattr(kind, "value", kind))
    return "other"


def _describe(svc: BackOfficeService, item: TrackedItem) -> dict[str, Any]:
    repo = svc.repo
    if item.subject_type == "transaction" and item.subject_id in repo.transactions:
        rec = repo.transactions[item.subject_id]
        doc = next((repo.documents[d] for d in rec.document_ids if d in repo.documents), None)
        name = display_name(doc.document.supplier_name) if doc else display_name(rec.tx.counterparty or "")
        kind = getattr(rec.tx.kind, "value", str(rec.tx.kind))
        how = _PAYMENT_KIND.get(kind, "Payment")
        if rec.tx.card_last4:
            how += f" •••• {rec.tx.card_last4}"
        return {"kind": "payment", "title": name or "Payment", "detail": how,
                "amount": _money(rec.tx.amount), "currency": rec.tx.currency, "date": rec.tx.booked_on.isoformat()}
    if item.subject_type == "document" and item.subject_id in repo.documents:
        rec = repo.documents[item.subject_id]
        d = rec.document
        issued = d.issue_date or rec.received_at.date()
        return {"kind": "document", "title": display_name(d.supplier_name or "") or "Document",
                "detail": rec.label.split(" · ")[0], "amount": _money(d.gross_amount), "currency": d.currency or "EUR",
                "date": issued.isoformat()}
    return {"kind": item.subject_type, "title": item.subject_type.capitalize(), "detail": "", "amount": None,
            "currency": "EUR", "date": None}


def _journey(item: TrackedItem) -> list[dict[str, Any]]:
    steps = []
    for t in item.history:
        stage = t.to_stage
        label = (STAGES.get(stage) or SIDE.get(stage) or (stage.value, ""))[0]
        agent = _agent(t.actor)
        steps.append({"stage": stage.value, "label": label, "agent": agent,
                      "agentLabel": AGENTS.get(agent, (agent.capitalize(), ""))[0],
                      "at": t.at.isoformat(), "note": t.note, "evidence": len(t.evidence_ids)})
    return steps


def _current(item: TrackedItem, note: str, need: Any, chase: Any) -> str:
    """What is happening with this item right now, in one sentence."""
    if item.stage is Stage.NEEDS_OWNER and need is not None:
        ask = need.question.prompt if need.question is not None else (need.why[0] if need.why else "")
        return f"Waiting for you: {ask}".strip()
    if chase is not None and item.stage not in (Stage.CLOSED, Stage.NOT_REQUIRED):
        return f"{chase.line} Waiting for their reply."
    if item.subject_type == "document" and item.stage is Stage.UNDERSTOOD:
        return "Read. Waiting to match it to a payment."
    return note or ""


def build_pipeline(svc: BackOfficeService) -> dict[str, Any]:
    repo = svc.repo
    items = list(repo.items.values())
    now = Counter(i.stage for i in items)
    reached = Counter(t.to_stage for i in items for t in i.history)
    reached.update(Stage.DISCOVERED for _ in items)  # every item starts by being found

    needs_by_item = {n.item_id: n for n in repo.open_needs()}
    chases = {c.tx_id: c for c in repo.chases.values()}
    rows = []
    for item in items:
        about = _describe(svc, item)
        company_id = repo.item_company(item)
        last = item.history[-1] if item.history else None
        open_ = item.stage not in (Stage.CLOSED, Stage.NOT_REQUIRED)
        row = {
            "id": item.id, **about,
            "company": svc._company_name(company_id) if company_id else None,
            "stage": item.stage.value,
            "stageLabel": (STAGES.get(item.stage) or SIDE.get(item.stage) or (item.stage.value, ""))[0],
            "quality": _QUALITY.get(item.quality.value, item.quality.value),
            "open": open_,
            "source": _source(svc, item),
            "reason": _current(item, last.note if last else "", needs_by_item.get(item.id), chases.get(item.subject_id)),
            "updatedAt": last.at.isoformat() if last else None,
            "journey": _journey(item),
        }
        if item.stage is Stage.NEEDS_OWNER:
            need = needs_by_item.get(item.id)
            row["href"] = f"/needs-you#{need.id}" if need else "/needs-you"
        rows.append(row)
    # Open items first (newest activity first), then the rest (newest first).
    rows.sort(key=lambda r: r["updatedAt"] or "", reverse=True)
    rows.sort(key=lambda r: not r["open"])

    work = Counter(_agent(t.actor) for i in items for t in i.history)
    extra = {"missing": len(repo.chases), "obligation": len(repo.obligations),
             "accountant": len(repo.accountant_questions)}
    agents = []
    for key, (label, what) in AGENTS.items():
        steps = work.get(key, 0)
        count = steps + extra.get(key, 0)
        if count:
            one, many = _UNITS.get(key, ("step", "steps")) if not steps else ("step", "steps")
            agents.append({"id": key, "label": label, "description": what, "count": count,
                           "unit": one if count == 1 else many})
    agents.sort(key=lambda a: -a["count"])

    sources = Counter(r["source"] for r in rows)
    companies = svc.companies()["companies"]
    obligations = sorted(repo.obligations.values(), key=lambda o: o.obligation.due_on)
    open_obligations = [o for o in obligations if not o.satisfied_by]
    outputs = [
        {"id": "months", "label": "Months", "items": [
            {"label": c["name"], "detail": c.get("detail") or "", "tone": c.get("tone") or "neutral",
             "href": f"/companies/{c['id']}"} for c in companies]},
        {"id": "accountant", "label": "Accountant", "items": [
            {"label": f"{len(repo.closed_months)} month{'s' if len(repo.closed_months) != 1 else ''} sent",
             "detail": "Each month goes to your accountant once it is closed.", "tone": "good" if repo.closed_months
             else "neutral"}]},
        {"id": "deadlines", "label": "Deadlines", "items": [
            {"label": f"{o.title} · {svc._company_name(o.obligation.entity_id) or ''}".strip(" ·"),
             "detail": ("Paid, with proof" if o.satisfied_by else f"Due {day_month(o.obligation.due_on, svc._today())}"),
             "tone": "good" if o.satisfied_by else "neutral"} for o in obligations]},
    ]

    open_count = sum(1 for r in rows if r["open"])
    return {
        "today": svc._today().isoformat(),
        "summary": {"items": len(rows), "open": open_count, "closed": now.get(Stage.CLOSED, 0),
                    "waiting": now.get(Stage.NEEDS_OWNER, 0), "conflicts": now.get(Stage.CONFLICT, 0),
                    "notRequired": now.get(Stage.NOT_REQUIRED, 0), "openDeadlines": len(open_obligations),
                    "steps": sum(len(i.history) for i in items)},
        "stages": [{"id": s.value, "label": STAGES[s][0], "description": STAGES[s][1], "now": now.get(s, 0),
                    "passed": reached.get(s, 0)} for s in ORDER],
        "side": [{"id": s.value, "label": label, "description": what, "now": now.get(s, 0)}
                 for s, (label, what) in SIDE.items()],
        "sources": [{"id": k, "label": SOURCES.get(k, k.replace("_", " ").capitalize()), "count": n}
                    for k, n in sources.most_common()],
        "agents": agents,
        "outputs": outputs,
        "items": rows,
    }
