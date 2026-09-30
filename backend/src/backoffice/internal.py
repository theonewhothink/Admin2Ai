"""The team's internal dashboard ("Admin OS"): how the product is doing, from the engine's own records.

This is for the people running Admin2Ai, not for small-business owners, so it
may name agents, audit actions and stages. Every figure is computed here from
the engine (tracked items, month statuses, the closure log, connectors, the
audit chain); only the go-live checklist is data (``backoffice.readiness``).

    GET /api/internal/overview     Command Center: §59 targets, health, fixes, pipeline, tenants
    GET /api/internal/operations   recent activity and the audit trail (``?limit=`` newest records)
    GET /api/internal/readiness    the go-live checklist

Every view takes a list of tenants (one :class:`BackOfficeService` each). The
demo has one tenant; in production the caller passes every tenant it serves.

Access: these paths are for admins only once sign-in exists. The single check
is :func:`admin_only` (true for every path under :data:`ADMIN_PREFIX`); the web
app's counterpart is ``canOpenInternal`` in web/lib/internal-access.ts.

Definitions (so every number can be traced; rates round towards the
unfavourable side, like ``closure.metrics``, so nothing looks better than it is):

* period — the month being closed (the month before the engine's today);
* zero-touch processing — the period's items that are done (closed with proof,
  or needing no document, GREEN) and were never shown to the owner, over all
  the period's items: items still open count against it (§59 target above 95%);
* missing invoices recovered automatically — of the missing documents detected
  for the period, the share the system retrieved by itself (above 90%);
* unresolved month-end items — the period's items not done, over all of them
  (below 1%);
* owner admin minutes — the engine's own figure for the period: recorded owner
  time, rounded up to whole minutes. The engine records a fixed 40 seconds per
  answer or approval until the apps report measured time, so it is an estimate
  (under 15 minutes);
* evidence quality — of all finished items, the share that are GREEN and whose
  every step carries evidence (§3, §57);
* connection health — connectors syncing, over all connectors (§47);
* time to first value, activated, onboarding active time (§58, §59) — from the
  milestones each business recorded while it went through onboarding
  (backoffice.onboarding): the first document found, the first payment matched
  by the system or the owner seeing the time saved, counted from the account's
  creation (the slowest business is shown); activated once all six §58
  conditions were met; the owner's onboarding spans summed (an estimate: 40
  seconds per step until the apps measure real time). A business set up by hand
  (the demo) went through no onboarding: "Not measured yet".
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from fractions import Fraction
from typing import TYPE_CHECKING, Any

from backoffice.closure import ItemState, Metric, Month, classify_item, customer_success, due_soon, owner_touched
from backoffice.closure.metrics import (
    AUTO_RESOLVED_TARGET,
    ONBOARDING_MINUTES_TARGET,
    OWNER_MINUTES_TARGET,
    TIME_TO_FIRST_VALUE_TARGET,
    UNRESOLVED_TARGET,
    ZERO_TOUCH_TARGET,
    ActivationCondition,
)
from backoffice.domain.lifecycle import TERMINAL, Stage, TrackedItem
from backoffice.domain.models import Quality
from backoffice.learning import display_name, format_money
from backoffice.orchestrator import ANSWER_SECONDS, TZ
from backoffice.pipeline import _UNITS, SIDE, STAGES, build_pipeline
from backoffice.readiness import readiness

if TYPE_CHECKING:  # pragma: no cover
    from backoffice.service import BackOfficeService

__all__ = ["ADMIN_PREFIX", "admin_only", "handle", "operations", "overview"]

ADMIN_PREFIX = "/api/internal/"
DEADLINE_DAYS = 7  # deadlines this close are a critical fix
AUDIT_LIMIT = 50  # audit records shown by default, newest first
AUDIT_LIMIT_MAX = 1000
SEVERITY_ORDER = {"red": 0, "amber": 1, "blue": 2}


def admin_only(path: str) -> bool:
    """True for every internal path: the one place that says these need an admin."""
    return (path.split("?", 1)[0].rstrip("/") + "/").startswith(ADMIN_PREFIX)


def handle(view: str, tenants: Sequence[BackOfficeService], body: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Route ``/api/internal/<view>`` (called from ``BackOfficeService._routes``)."""
    if view == "overview":
        return overview(tenants)
    if view == "operations":
        return operations(tenants, limit=_limit((body or {}).get("limit")))
    if view == "readiness":
        return readiness()
    if view == "acceptance":
        from backoffice.acceptance import acceptance

        return acceptance()
    raise KeyError(view)


# --------------------------------------------------------------------------- figures per tenant


def _percent(ratio: Fraction, higher_is_better: bool) -> float:
    """Percentage with one decimal, rounded towards the unfavourable side (as ``closure.metrics``)."""
    exact = Decimal(ratio.numerator * 100) / Decimal(ratio.denominator)
    value = exact.quantize(Decimal("0.1"), rounding=ROUND_FLOOR if higher_is_better else ROUND_CEILING)
    return float(value)


def _pct_label(value: float) -> str:
    return f"{value:.1f}".removesuffix(".0") + "%"


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _iso(value: date | datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


@dataclass
class _Tenant:
    """Everything the overview needs from one tenant, read once."""

    svc: BackOfficeService
    month: Month
    items: list[TrackedItem]  # the period's items, every company
    statuses: list[Any]  # MonthStatus per company, same order as companies
    done_untouched: int
    missing_detected: int
    missing_recovered: int
    open_items: int
    total_items: int
    owner_minutes: int
    owner_minutes_now: int  # the current calendar month so far
    finished: int  # all tracked items that are finished (closed / no document needed)
    finished_proven: int  # … that are GREEN with evidence on every step
    connectors_ok: int
    connectors_total: int


def _read(svc: BackOfficeService) -> _Tenant:
    repo = svc.repo
    month = svc._current_month()
    companies = sorted(repo.companies)
    items = [i for c in companies for i in repo.items_for(c, month)]
    statuses = [svc._status(c, month) for c in companies]
    report = customer_success(month, items=items, statuses=statuses, activities=repo.closure_log,
                              interactions=repo.interactions, tz=TZ)
    missing = report.get(Metric.AUTO_RESOLVED_MISSING_RATE)
    unresolved = report.get(Metric.UNRESOLVED_MONTH_END_RATE)
    minutes = report.get(Metric.OWNER_ADMIN_MINUTES)
    now_month = Month.of(svc._today())
    minutes_now = customer_success(now_month, interactions=repo.interactions, tz=TZ).get(Metric.OWNER_ADMIN_MINUTES)
    finished = [i for i in repo.items.values() if i.stage in TERMINAL]
    proven = [i for i in finished if i.quality is Quality.GREEN and all(t.evidence_ids for t in i.history)]
    connectors = list(repo.connectors.values())
    return _Tenant(
        svc=svc, month=month, items=items, statuses=statuses,
        done_untouched=sum(1 for i in items if classify_item(i) is ItemState.DONE and not owner_touched(i)),
        missing_detected=missing.denominator or 0, missing_recovered=missing.numerator or 0,
        open_items=unresolved.numerator or 0, total_items=unresolved.denominator or 0,
        owner_minutes=int(minutes.value or 0), owner_minutes_now=int(minutes_now.value or 0),
        finished=len(finished), finished_proven=len(proven),
        connectors_ok=sum(1 for c in connectors if c.healthy), connectors_total=len(connectors),
    )  # fmt: skip


# --------------------------------------------------------------------------- golden totals (§59)


def _needed_above(count: int, total: int, target: Fraction) -> int:
    """How many more of ``total`` must succeed for count/total to be above ``target``."""
    return max(0, math.floor(target * total) + 1 - count)


def _allowed_below(total: int, target: Fraction) -> int:
    """The most items that may stay open for open/total to be below ``target``."""
    return max(0, math.ceil(target * total) - 1)


def _golden(tenants: Sequence[_Tenant]) -> list[dict[str, Any]]:
    month = tenants[0].month
    items = sum(len(t.items) for t in tenants)
    untouched = sum(t.done_untouched for t in tenants)
    detected = sum(t.missing_detected for t in tenants)
    recovered = sum(t.missing_recovered for t in tenants)
    total = sum(t.total_items for t in tenants)
    open_items = sum(t.open_items for t in tenants)
    # Owner time is per owner: the dashboard shows the busiest owner (with one tenant, simply theirs).
    minutes = max(t.owner_minutes for t in tenants)
    minutes_now = max(t.owner_minutes_now for t in tenants)
    now_name = Month.of(tenants[0].svc._today()).name

    out: list[dict[str, Any]] = []

    # Zero-touch processing: done without the owner, over all the period's items.
    if items:
        value = _percent(Fraction(untouched, items), True)
        need = _needed_above(untouched, items, ZERO_TOUCH_TARGET)
        out.append({
            "id": "zero_touch", "label": "Zero-touch processing", "target": 95, "targetLabel": "Above 95%",
            "hasData": True, "value": value, "ring": value, "ringLabel": _pct_label(value), "ringCaption": "no owner",
            "count": untouched, "countLabel": f"of {_plural(items, 'item', 'items')}",
            "onTarget": Fraction(untouched, items) > ZERO_TOUCH_TARGET,
            "remaining": need, "remainingLabel": "On target" if not need else
            f"{_plural(need, 'more item', 'more items')} to reach target",
            "detail": f"{month.name} items finished with proof and never shown to the owner.", "estimate": False,
        })
    else:
        out.append(_empty("zero_touch", "Zero-touch processing", 95, "Above 95%", f"No {month.name} items yet."))

    # Missing invoices recovered automatically.
    if detected:
        value = _percent(Fraction(recovered, detected), True)
        need = _needed_above(recovered, detected, AUTO_RESOLVED_TARGET)
        out.append({
            "id": "recovered", "label": "Missing invoices recovered", "target": 90, "targetLabel": "Above 90%",
            "hasData": True, "value": value, "ring": value, "ringLabel": _pct_label(value), "ringCaption": "by itself",
            "count": recovered, "countLabel": f"of {detected} missing",
            "onTarget": Fraction(recovered, detected) > AUTO_RESOLVED_TARGET,
            "remaining": need, "remainingLabel": "On target" if not need else f"{need} more to reach target",
            "detail": "Found or fetched by the system, without anyone asking the owner.", "estimate": False,
        })
    else:
        out.append(_empty("recovered", "Missing invoices recovered", 90, "Above 90%",
                          f"No missing invoices detected for {month.name}."))

    # Unresolved month-end items.
    if total:
        value = _percent(Fraction(open_items, total), False)
        allowed = _allowed_below(total, UNRESOLVED_TARGET)
        need = max(0, open_items - allowed)
        out.append({
            "id": "unresolved", "label": "Unresolved month-end items", "target": 1, "targetLabel": "Below 1%",
            "hasData": True, "value": value, "ring": value, "ringLabel": _pct_label(value), "ringCaption": "still open",
            "count": open_items, "countLabel": f"of {_plural(total, 'item', 'items')}",
            "onTarget": Fraction(open_items, total) < UNRESOLVED_TARGET,
            "remaining": need, "remainingLabel": "On target" if not need else f"{need} to resolve to reach target",
            "detail": f"{month.name} items not finished yet, across every company.", "estimate": False,
        })
    else:
        out.append(_empty("unresolved", "Unresolved month-end items", 1, "Below 1%", f"No {month.name} items yet."))

    # Owner admin minutes (the engine's own figure; an estimate until the apps measure it).
    ring = min(100.0, round(minutes * 100 / OWNER_MINUTES_TARGET, 1))
    spare = max(0, OWNER_MINUTES_TARGET - 1 - minutes)
    over = max(0, minutes - (OWNER_MINUTES_TARGET - 1))
    out.append({
        "id": "owner_minutes", "label": "Owner admin minutes", "target": OWNER_MINUTES_TARGET,
        "targetLabel": f"Under {OWNER_MINUTES_TARGET} min", "hasData": True, "value": minutes, "ring": ring,
        "ringLabel": _pct_label(ring), "ringCaption": f"of {OWNER_MINUTES_TARGET} min",
        "count": minutes, "countLabel": "minute" if minutes == 1 else "minutes",
        "onTarget": minutes < OWNER_MINUTES_TARGET, "remaining": over,
        "remainingLabel": (f"{spare} min to spare" if spare else "On target") if not over else f"{over} min over target",
        "detail": f"Counted as {ANSWER_SECONDS} seconds per answer or approval. {now_name} so far: {minutes_now} min.",
        "estimate": True,
    })
    return out


def _empty(id_: str, label: str, target: int, target_label: str, detail: str) -> dict[str, Any]:
    return {"id": id_, "label": label, "target": target, "targetLabel": target_label, "hasData": False,
            "value": None, "ring": 0, "ringLabel": "–", "ringCaption": "no data", "count": 0,
            "countLabel": "no data yet", "onTarget": None, "remaining": 0, "remainingLabel": "No data yet",
            "detail": detail, "estimate": False}


# --------------------------------------------------------------------------- health


def _attainment(tenants: Sequence[_Tenant]) -> list[Fraction]:
    """How close each golden total is to its target, each capped at 1 (no data: left out)."""
    parts: list[Fraction] = []
    items = sum(len(t.items) for t in tenants)
    if items:
        parts.append(min(Fraction(1), Fraction(sum(t.done_untouched for t in tenants), items) / ZERO_TOUCH_TARGET))
    detected = sum(t.missing_detected for t in tenants)
    if detected:
        parts.append(min(Fraction(1), Fraction(sum(t.missing_recovered for t in tenants), detected) /
                         AUTO_RESOLVED_TARGET))
    total = sum(t.total_items for t in tenants)
    if total:
        resolved = 1 - Fraction(sum(t.open_items for t in tenants), total)
        parts.append(min(Fraction(1), resolved / (1 - UNRESOLVED_TARGET)))
    minutes = max(t.owner_minutes for t in tenants)
    allowed = OWNER_MINUTES_TARGET - 1
    parts.append(Fraction(1) if minutes <= allowed else Fraction(allowed, minutes))
    return parts


def _tone(score: int | None) -> str:
    if score is None:
        return "neutral"
    return "good" if score >= 80 else "attention" if score >= 60 else "risk"


def _health(tenants: Sequence[_Tenant]) -> dict[str, Any]:
    attain = _attainment(tenants)
    automation = math.floor(sum(attain, Fraction(0)) * 100 / len(attain))
    finished = sum(t.finished for t in tenants)
    proven = sum(t.finished_proven for t in tenants)
    evidence = math.floor(Fraction(proven * 100, finished)) if finished else None
    conn_total = sum(t.connectors_total for t in tenants)
    conn_ok = sum(t.connectors_ok for t in tenants)
    connections = math.floor(Fraction(conn_ok * 100, conn_total)) if conn_total else 0
    parts = [
        {"id": "automation", "label": "Automation", "score": automation,
         "detail": f"Average of the {len(attain)} targets above with data, each counted up to 100%."},
        {"id": "evidence", "label": "Evidence quality", "score": evidence,
         "detail": (f"{proven} of {_plural(finished, 'finished item', 'finished items')} are green with evidence on "
                    "every step." if finished else "No finished items yet.")},
        {"id": "connections", "label": "Connection health", "score": connections,
         "detail": (f"{conn_ok} of {_plural(conn_total, 'connection', 'connections')} syncing." if conn_total
                    else "Nothing is connected yet.")},
    ]
    scored = [p["score"] for p in parts if p["score"] is not None]
    score = math.floor(Fraction(sum(scored), len(scored))) if scored else None
    return {"score": score, "tone": _tone(score), "parts": parts}


# --------------------------------------------------------------------------- critical fixes


def _fixes(t: _Tenant, rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    svc = t.svc
    repo = svc.repo
    tenant = repo.tenant_id
    fixes: list[dict[str, Any]] = []

    def add(severity: str, id_: str, label: str, detail: str, company_id: str | None, href: str | None) -> None:
        fixes.append({"id": f"{tenant}:{id_}", "severity": severity, "label": label, "detail": detail,
                      "tenant": tenant, "company": svc._company_name(company_id) if company_id else None,
                      "href": href})

    for n in repo.open_needs():
        if n.kind == "approval":
            doc = repo.documents[n.subject_id]
            who = display_name(doc.document.supplier_name)
            amount = (f" of {format_money(doc.document.gross_amount, doc.document.currency)}"
                      if doc.document.gross_amount is not None else "")
            if svc.orchestrator._iban_changed(doc):
                add("red", n.id, f"{who} changed its bank details",
                    f"Payment{amount} on hold until the owner confirms by phone.", n.company_id, f"/needs-you#{n.id}")
            else:
                add("red", n.id, f"{who} invoice does not look right",
                    f"Payment{amount} on hold until the owner checks it.", n.company_id, f"/needs-you#{n.id}")
    for row in rows:
        if row["open"] and (row["stage"] == Stage.CONFLICT.value or row["quality"] == Quality.RED.value):
            add("red", row["id"], f"Sources disagree: {row['title']}",
                row.get("reason") or "Nothing moves until a person decides.",
                repo.item_company(repo.items[row["id"]]), "/diagram")
    for c in svc.connections()["connections"]:
        if c["status"] != "healthy":
            companies = repo.connectors[c["id"]].company_ids
            add("amber", f"conn:{c['id']}", f"{c['name']} is not syncing ({c['account']})",
                c.get("message") or "The month cannot close until it syncs again.",
                companies[0] if len(companies) == 1 else None, "/sources")
    open_obligations = [o.obligation for o in repo.obligations.values() if not o.satisfied_by and not o.informational]
    for due in due_soon(open_obligations, svc._today(), within_days=DEADLINE_DAYS):
        amount = format_money(due.amount, due.currency) if due.amount is not None else None
        seen = "No payment seen yet" if repo.obligations[due.obligation_id].payable else "No proof seen yet"
        detail = " · ".join(p for p in (amount, f"Due {due.due_on.isoformat()}", seen) if p)
        add("red" if due.overdue else "amber", f"due:{due.obligation_id}", f"{due.title} {due.when}", f"{detail}.",
            due.entity_id, None)
    for n in repo.open_needs():
        if n.kind in ("choice", "cost_center"):
            rec = repo.transactions[n.subject_id]
            who = svc.orchestrator.merchant_name(rec.tx)
            prompt = n.question.prompt if n.question is not None else "The owner needs to answer one question."
            add("blue", n.id, f"{who} {format_money(abs(rec.tx.amount), rec.tx.currency)}: waiting for the owner",
                prompt, n.company_id, f"/needs-you#{n.id}")
        elif n.kind in ("company", "cash", "obligation", "refund", "obligation_company", "statement", "recharge",
                        "part", "deposit", "deposit_refund", "deposit_kept", "chargeback"):
            # which company carries it; a cash receipt to confirm; a letter's payment; a refund; a letter's company;
            # a supplier statement that disagrees; a cost to recharge to a client; a part payment, a deposit or a
            # deposit given back to confirm; the part of a security deposit kept; a disputed card payment
            shown = svc._question(n)
            amount = format_money(Decimal(str(shown["amount"] or 0)), shown["currency"])
            add("blue", n.id, f"{shown['merchant']} {amount}: waiting for the owner", n.prompt, n.company_id,
                f"/needs-you#{n.id}")
    return fixes


# --------------------------------------------------------------------------- pipeline, tenants, connections


def _pipeline(pipes: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The golden-rule steps and agents' work, summed over tenants (``build_pipeline`` per tenant)."""
    first = pipes[0]
    summary = {k: sum(p["summary"][k] for p in pipes) for k in first["summary"]}

    def merge(key: str, fields: tuple[str, ...]) -> list[dict[str, Any]]:
        rows: dict[str, dict[str, Any]] = {}
        for p in pipes:
            for row in p[key]:
                seen = rows.setdefault(row["id"], {k: v for k, v in row.items() if k not in fields} |
                                       {f: 0 for f in fields})
                for f in fields:
                    seen[f] += row.get(f, 0)
        return list(rows.values())

    agents = merge("agents", ("count",))
    for a in agents:
        one, many = ("step", "steps") if a["unit"] in ("step", "steps") else _UNITS.get(a["id"], (a["unit"],) * 2)
        a["unit"] = one if a["count"] == 1 else many
    agents.sort(key=lambda a: (-a["count"], a["id"]))
    return {"summary": summary, "stages": merge("stages", ("now", "passed")), "side": merge("side", ("now",)),
            "agents": agents}


def _tenant_row(t: _Tenant, chain: Any) -> dict[str, Any]:
    svc = t.svc
    repo = svc.repo
    listed = {c["id"]: c for c in svc.companies()["companies"]}
    companies = []
    for company_id, status in zip(sorted(repo.companies), t.statuses, strict=True):
        c = listed[company_id]
        closed_on = repo.closed_months.get((company_id, str(t.month)))
        companies.append({
            "id": company_id, "name": c["name"], "legalName": c["legalName"], "taxId": c["taxId"],
            "tone": c["tone"], "statusLabel": c["statusLabel"], "percentClosed": status.percent_closed,
            "itemsDone": status.items_done, "itemsTotal": status.items_total, "needsYou": status.needs_you,
            "missingDocuments": status.missing_documents, "closedOn": _iso(closed_on),
        })
    return {
        "id": repo.tenant_id, "owner": repo.owner.full_name, "email": repo.owner.email,
        "month": str(t.month), "monthLabel": t.month.name, "companies": companies,
        "items": len(repo.items), "openItems": sum(1 for i in repo.items.values() if i.stage not in TERMINAL),
        "needsYou": len(repo.open_needs()),
        "connections": {"healthy": t.connectors_ok, "total": t.connectors_total},
        "audit": {"records": chain.checked, "intact": chain.ok},
    }


def _connections(t: _Tenant) -> list[dict[str, Any]]:
    svc = t.svc
    repo = svc.repo
    out = []
    for c in svc.connections()["connections"]:
        state = repo.connectors[c["id"]]
        out.append({
            "id": f"{repo.tenant_id}:{c['id']}", "tenant": repo.tenant_id, "name": c["name"], "kind": c["kind"],
            "account": c["account"], "status": c["status"],
            "companies": [svc._company_name(x) or x for x in state.company_ids],
            "lastSyncedAt": _iso(state.last_synced_at), "coveredFrom": _iso(state.covered_from),
            "coveredUntil": _iso(state.covered_until), "message": c.get("message"),
            "signIn": svc._sign_in_label(c["id"]) or "Demo connection: no real sign-in was made.",
        })
    return out


# --------------------------------------------------------------------------- targets (§59, all of them)


def _targets(tenants: Sequence[_Tenant], golden: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    by_id = {g["id"]: g for g in golden}
    reports = [customer_success(t.month, items=t.items, statuses=t.statuses, activities=t.svc.repo.closure_log,
                                interactions=t.svc.repo.interactions, tz=TZ) for t in tenants]
    silent = sum(int(r.get(Metric.CRITICAL_SILENT_ERRORS).value or 0) for r in reports)
    onboarding = [r.get(Metric.ONBOARDING_ACTIVE_MINUTES).value for r in reports]
    onboarding_known = [int(v) for v in onboarding if v is not None]
    accountant = sum(int(r.get(Metric.ACCOUNTANT_QUESTIONS_NEEDING_OWNER).numerator or 0) for r in reports)
    first_value, activated = _activation_rows(tenants)

    def from_golden(id_: str, definition: str) -> dict[str, Any]:
        g = by_id[id_]
        return {"id": id_, "label": g["label"], "value": g["value"],
                "display": ("–" if not g["hasData"] else f"{g['value']} min" if id_ == "owner_minutes"
                            else _pct_label(g["value"])),
                "target": g["targetLabel"], "onTarget": g["onTarget"], "estimate": g["estimate"],
                "evidence": (f"Estimated for {tenants[0].month.name}" if id_ == "owner_minutes"
                             else f"{g['count']} {g['countLabel']}"),
                "definition": definition}

    return [
        from_golden("owner_minutes", f"Recorded owner time about the month, rounded up. {ANSWER_SECONDS} seconds are "
                                     "recorded per answer or approval until the apps measure real time."),
        from_golden("zero_touch", "Items finished with proof and never shown to the owner, over all the month's "
                                  "items. Items still open count against it."),
        from_golden("recovered", "Missing documents the system found or fetched by itself, over all missing "
                                 "documents it detected for the month."),
        {"id": "silent_errors", "label": "Critical silent errors", "value": silent, "display": str(silent),
         "target": "0", "onTarget": silent == 0, "estimate": False,
         "evidence": "None found" if not silent else _plural(silent, "value proven wrong", "values proven wrong"),
         "definition": "Values marked verified that later turned out wrong. Any at all is a stop-the-line problem."},
        from_golden("unresolved", "Items of the month not finished yet, over all of the month's items."),
        {"id": "onboarding_minutes", "label": "Onboarding active time",
         "value": max(onboarding_known) if onboarding_known else None,
         "display": f"{max(onboarding_known)} min" if onboarding_known else NOT_MEASURED,
         "target": f"Under {ONBOARDING_MINUTES_TARGET} min",
         "onTarget": (max(onboarding_known) < ONBOARDING_MINUTES_TARGET) if onboarding_known else None,
         "estimate": bool(onboarding_known),
         "evidence": (f"Recorded from {_plural(_onboarding_spans(tenants), 'owner step', 'owner steps')} during "
                      f"onboarding, {ANSWER_SECONDS} seconds each" if onboarding_known else NO_ONBOARDING),
         "definition": "The owner's active time while onboarding was open: each set-up step (sign-up, company, email, "
                       "bank, accountant) and each answer to a first-run question is one span. "
                       f"{ANSWER_SECONDS} seconds are recorded per span until the apps measure real time."},
        first_value,
        activated,
        {"id": "accountant_owner", "label": "Accountant questions needing the owner", "value": accountant,
         "display": str(accountant), "target": "Under 20% of baseline", "onTarget": None, "estimate": False,
         "evidence": "No baseline recorded yet, so the share cannot be worked out.",
         "definition": "Accountant questions the owner had to answer, against how many they used to get."},
    ]


NOT_MEASURED = "Not measured yet"
NO_ONBOARDING = "Not measured yet: no business here went through onboarding (the demo was set up by hand)."
_CONDITION_WORDS = {
    ActivationCondition.EMAIL_CONNECTED: "email connected",
    ActivationCondition.BANK_CONNECTED: "bank connected",
    ActivationCondition.HISTORICAL_SCAN_COMPLETE: "last 90 days read",
    ActivationCondition.DOCUMENT_FOUND: "a document found",
    ActivationCondition.TRANSACTION_AUTO_MATCHED: "a payment matched by itself",
    ActivationCondition.TIME_SAVED_SEEN: "the owner saw the time saved",
}


def _onboarding_spans(tenants: Sequence[_Tenant]) -> int:
    return sum(t.svc.repo.onboarding.estimated_spans for t in tenants)


def _activation_rows(tenants: Sequence[_Tenant]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Time to first value and activated (§58), from each business's recorded onboarding milestones."""
    reports = [r for t in tenants if (r := t.svc.orchestrator.activation()) is not None]
    firsts = [r.time_to_first_value for r in reports if r.time_to_first_value is not None]
    target_minutes = int(TIME_TO_FIRST_VALUE_TARGET.total_seconds() // 60)
    if firsts:
        slowest = max(firsts)
        minutes = math.ceil(slowest.total_seconds() / 6) / 10  # one decimal, rounded up: never better than it was
        first_value = {
            "id": "time_to_first_value", "label": "Time to first value", "value": minutes,
            "display": f"{minutes:g} min", "target": f"Under {target_minutes} min",
            "onTarget": all(r.first_value_on_target for r in reports if r.time_to_first_value is not None),
            "estimate": False,
            "evidence": (f"Slowest of {_plural(len(firsts), 'business', 'businesses')}: from account creation to the "
                         "first document found or payment matched"),
            "definition": "From account creation to the first concrete result the owner can see: a document found, a "
                          "payment matched by itself, or the time saved shown.",
        }
    else:
        first_value = {
            "id": "time_to_first_value", "label": "Time to first value", "value": None, "display": NOT_MEASURED,
            "target": f"Under {target_minutes} min", "onTarget": None, "estimate": False,
            "evidence": NO_ONBOARDING if not reports else "No document found or payment matched yet.",
            "definition": "From account creation to the first concrete result the owner can see: a document found, a "
                          "payment matched by itself, or the time saved shown.",
        }
    if reports:
        done = sum(1 for r in reports if r.activated)
        missing = sorted({_CONDITION_WORDS[c] for r in reports for c in r.missing})
        activated = {
            "id": "activated", "label": "Activated", "value": done,
            "display": ("Yes" if done else "No") if len(reports) == 1 else f"{done} of {len(reports)}",
            "target": "All six conditions met", "onTarget": done == len(reports), "estimate": False,
            "evidence": "All six conditions met" if not missing else f"Not yet: {', '.join(missing)}",
            "definition": "Email and bank connected, the last 90 days read, a document found, a payment matched by "
                          "itself, and the owner saw the time saved.",
        }
    else:
        activated = {
            "id": "activated", "label": "Activated", "value": None, "display": NOT_MEASURED,
            "target": "All six conditions met", "onTarget": None, "estimate": False, "evidence": NO_ONBOARDING,
            "definition": "Email and bank connected, the last 90 days read, a document found, a payment matched by "
                          "itself, and the owner saw the time saved.",
        }
    return first_value, activated


# --------------------------------------------------------------------------- the views


def overview(tenants: Sequence[BackOfficeService]) -> dict[str, Any]:
    """Command Center: targets, health, critical fixes, pipeline, tenants, connections, readiness."""
    if not tenants:
        raise ValueError("at least one tenant is required")
    read = [_read(s) for s in tenants]
    pipes = [build_pipeline(s) for s in tenants]
    chains = [s.repo.audit.verify(s.repo.tenant_id) for s in tenants]
    golden = _golden(read)
    fixes = [f for t, p in zip(read, pipes, strict=True) for f in _fixes(t, p["items"])]
    fixes.sort(key=lambda f: SEVERITY_ORDER[f["severity"]])
    first = tenants[0]
    companies = sum(len(s.repo.companies) for s in tenants)
    return {
        "generatedAt": first._now().isoformat(),
        "today": first._today().isoformat(),
        "period": {"key": str(read[0].month), "label": f"{read[0].month.name} {read[0].month.year}"},
        "golden": golden,
        "health": _health(read),
        "fixes": fixes,
        "pipeline": _pipeline(pipes),
        "tenants": [_tenant_row(t, c) for t, c in zip(read, chains, strict=True)],
        "connections": [c for t in read for c in _connections(t)],
        "targets": _targets(read, golden),
        "readiness": readiness(),
        "quick": [
            {"id": "tenants", "label": "Tenants", "value": len(tenants)},
            {"id": "companies", "label": "Companies", "value": companies},
            {"id": "items", "label": "Items tracked", "value": sum(p["summary"]["items"] for p in pipes)},
            {"id": "audit", "label": "Audit records", "value": sum(c.checked for c in chains)},
        ],
    }


def _limit(raw: Any) -> int:
    try:
        value = int(raw) if raw not in (None, "") else AUDIT_LIMIT
    except (TypeError, ValueError):
        return AUDIT_LIMIT
    return max(1, min(AUDIT_LIMIT_MAX, value))


def _short(value: Any, width: int = 48) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    text = value if isinstance(value, str) else ", ".join(map(str, value)) if isinstance(value, list) else str(value)
    return text if len(text) <= width else text[: width - 1] + "…"


def _audit_summary(data: Mapping[str, Any]) -> str:
    """One readable line for an audit record (the full record stays in the chain)."""
    values = data.get("extracted_values") or {}
    response = data.get("response")
    parts: list[str] = []
    if data.get("action") == "transition" and isinstance(values, Mapping):
        to = values.get("to")
        try:
            stage = Stage(to)
            label = (STAGES.get(stage) or SIDE.get(stage) or (str(to), ""))[0]
        except ValueError:
            label = str(to)
        parts.append(f"→ {label}")
        if values.get("quality"):
            parts.append(str(values["quality"]))
    elif isinstance(values, Mapping):
        parts += [f"{k.replace('_', ' ')}: {_short(v)}" for k, v in list(values.items())[:3] if v not in ("", None)]
    if isinstance(response, str) and response:
        parts.append(response)
    elif isinstance(response, Mapping) and response:
        parts += [f"{k.replace('_', ' ')}: {_short(v)}" for k, v in list(response.items())[:2]]
    validations = data.get("validations") or []
    if validations:
        first = validations[0]
        if isinstance(first, Mapping):
            first = first.get("signal") or first.get("message") or first
        parts.append(_short(first, 80))
    line = " · ".join(p for p in parts if p)
    return line if len(line) <= 180 else line[:179] + "…"


def operations(tenants: Sequence[BackOfficeService], *, limit: int = AUDIT_LIMIT) -> dict[str, Any]:
    """Recent activity across tenants and the newest audit records, with each chain's verification."""
    if not tenants:
        raise ValueError("at least one tenant is required")
    activity: list[dict[str, Any]] = []
    entries: list[dict[str, Any]] = []
    chains = []
    agents: Counter[str] = Counter()
    for svc in tenants:
        repo = svc.repo
        tenant = repo.tenant_id
        for a in repo.activity:
            row: dict[str, Any] = {"id": f"{tenant}:{a.id}", "at": a.at.isoformat(), "kind": a.kind, "text": a.text,
                                   "tenant": tenant, "company": svc._company_name(a.company_id),
                                   "evidence": len(a.evidence_ids)}
            if a.amount is not None:
                row["amount"] = float(a.amount)
                row["currency"] = a.currency
            activity.append(row)
        report = repo.audit.verify(tenant)
        chains.append({"tenant": tenant, "records": report.checked, "intact": report.ok, "head": report.head_hash,
                       "problem": report.problem.value if report.problem else None, "detail": report.detail})
        for record in repo.audit_store.records(tenant):
            data = record.data()
            agent = data.get("agent") or "—"
            agents[agent] += 1
            entries.append({
                "id": f"{tenant}:{record.seq}", "tenant": tenant, "seq": record.seq, "at": data.get("at"),
                "actor": data.get("actor"), "agent": agent, "action": data.get("action"),
                "subject": data.get("subject_id"), "evidence": len(data.get("evidence_ids") or []),
                "model": data.get("model"), "parser": data.get("parser"), "summary": _audit_summary(data),
                "hash": record.hash[:12],
            })
    activity.sort(key=lambda a: (a["at"], a["id"]), reverse=True)
    entries.sort(key=lambda e: (e["at"] or "", e["tenant"], e["seq"]), reverse=True)
    first = tenants[0]
    return {
        "generatedAt": first._now().isoformat(),
        "today": first._today().isoformat(),
        "activity": activity,
        "audit": {
            "records": sum(c["records"] for c in chains),
            "intact": all(c["intact"] for c in chains),
            "chains": chains,
            "agents": [{"id": k, "count": n} for k, n in sorted(agents.items(), key=lambda kv: (-kv[1], kv[0]))],
            "shown": min(limit, len(entries)),
            "entries": entries[:limit],
        },
    }
