"""The operator chat: ask things and get things done (§39, §25).

The owner writes in plain language ("find the Vodafone invoice and send it to
marc@…", "summarise Adobe over the past 2 years and check for issues"). The
operator answers from evidence and prepares actions.

Two brains, one set of tools:

* :class:`ClaudeBrain` (server, when ``ANTHROPIC_API_KEY`` is set): Claude
  plans with tool use over the tools below.
* The browser chat (web/lib/claude.ts, when the owner adds their own
  Anthropic key): the same loop in the browser; it reads ``SYSTEM`` and
  ``TOOLS`` from ``GET /api/chat/tools`` and runs each tool with
  ``POST /api/chat/tool``, so both brains share :func:`run_tool`.
* :class:`RuleBrain` (always available, also in the browser build): a
  deterministic parser for the common requests.

Tools read evidence, *prepare* actions, record answers the owner gave in the
chat, and keep the owner's task list. Anything that leaves the business (an
email) becomes a draft the owner confirms with one tap (§25: external
communication needs the owner); nothing is sent by the model, no money moves,
and changed bank details are only ever confirmed by the owner (§26).
"""

from __future__ import annotations

import csv
import io
import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from backoffice.service import BackOfficeService

__all__ = ["CHANGING_TOOLS", "ClaudeBrain", "Operator", "OutboxMessage", "OwnerTask", "RuleBrain", "SYSTEM", "TOOLS",
           "run_tool"]

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
     "november", "december"], start=1)}
_MONTHS.update({k[:3]: v for k, v in list(_MONTHS.items())})


def _d(value: date) -> str:
    return f"{value.day} {value:%b %Y}"


def _money(value: Decimal | None) -> str:
    if value is None:
        return "—"
    return f"€{value.quantize(Decimal('0.01')):,.2f}"


@dataclass
class OutboxMessage:
    id: str
    to: list[str]
    subject: str
    body: str
    attachments: list[dict[str, str]] = field(default_factory=list)  # {"kind": "document"|"report", "id", "name"}
    status: str = "draft"  # draft -> sent | cancelled
    sent_at: str | None = None
    delivery: str = ""


@dataclass
class OwnerTask:
    """Something the owner asked to be reminded of or to keep track of."""

    id: str
    title: str
    due: date | None = None
    company_id: str = ""
    status: str = "open"  # open -> done
    created_at: str = ""
    done_at: str | None = None


# ----------------------------------------------------------------------------- tools


class Operator:
    """Evidence-backed tools over the service's repository."""

    def __init__(self, service: BackOfficeService) -> None:
        self.svc = service
        self.outbox: dict[str, OutboxMessage] = {}
        self.reports: dict[str, dict[str, Any]] = {}
        self.tasks: dict[str, OwnerTask] = {}
        self._seq = 0

    @property
    def repo(self):  # type: ignore[no-untyped-def]
        return self.svc.repo

    def _id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}{self._seq:04d}"

    def _company(self, company_id: str | None) -> str:
        return self.repo.company_name(company_id) or ""

    # -- documents ------------------------------------------------------------

    def documents(self, *, query: str = "", supplier: str = "", company_id: str = "",
                  date_from: date | None = None, date_to: date | None = None,
                  amount: Decimal | None = None) -> list[dict[str, Any]]:
        out = []
        q = query.lower().strip()
        for rec in self.repo.documents.values():
            d = rec.document
            issued = d.issue_date or rec.received_at.date()
            name = d.supplier_name or ""
            if supplier and supplier.lower() not in name.lower():
                continue
            if company_id and d.entity_id != company_id:
                continue
            if date_from and issued < date_from or date_to and issued > date_to:
                continue
            if amount is not None and (d.gross_amount is None or abs(d.gross_amount - amount) > Decimal("0.01")):
                continue
            hay = " ".join(filter(None, [name, d.invoice_number, d.doc_type.value, self._company(d.entity_id)])).lower()
            if q and not all(t in hay for t in q.split()):
                continue
            ev = self.repo.registry.get(self.repo.tenant_id, rec.evidence_ids[0]) if rec.evidence_ids else None
            out.append({
                "id": d.id, "supplier": name or "Unknown", "number": d.invoice_number or "",
                "type": d.doc_type.value.replace("_", " "), "date": issued.isoformat(),
                "amount": float(d.gross_amount) if d.gross_amount is not None else None,
                "currency": d.currency, "companyId": d.entity_id or "", "company": self._company(d.entity_id),
                "status": "on hold" if rec.on_hold and not rec.hold_released else
                          ("matched" if rec.matched_tx_ids else "waiting for payment"),
                "quality": d.quality.name.lower(), "origin": rec.origin,
                "filename": (ev.filename if ev and ev.filename else f"{d.id}.bin"),
                "evidenceIds": list(rec.evidence_ids),
            })
        out.sort(key=lambda x: (x["date"], x["supplier"]), reverse=True)
        return out

    def document_file(self, document_id: str) -> tuple[str, str, bytes] | None:
        rec = self.repo.documents.get(document_id)
        if rec is None or not rec.evidence_ids:
            return None
        ev = self.repo.registry.get(self.repo.tenant_id, rec.evidence_ids[0])
        data = self.repo.registry.open(self.repo.tenant_id, rec.evidence_ids[0])
        return (ev.filename or f"{document_id}.bin", ev.mime_type or "application/octet-stream", data)

    def export_zip(self, *, company_id: str = "", date_from: date | None = None,
                   date_to: date | None = None) -> tuple[str, bytes, int]:
        """All documents in a period as a ZIP: originals, ledger.csv and manifest.json."""
        import zipfile

        docs = self.documents(company_id=company_id, date_from=date_from, date_to=date_to)
        buf = io.BytesIO()
        ledger = io.StringIO()
        w = csv.writer(ledger, delimiter=";")
        w.writerow(["date", "company", "supplier", "number", "type", "gross", "currency", "status", "file", "sha256"])
        manifest = []
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for d in docs:
                f = self.document_file(d["id"])
                path = ""
                sha = ""
                if f:
                    safe = re.sub(r"[^\w.-]+", "_", f[0])[:80]
                    path = f"documents/{d['date']}_{d['id']}_{safe}"
                    z.writestr(path, f[2])
                    sha = self.repo.registry.get(self.repo.tenant_id, d["evidenceIds"][0]).sha256
                w.writerow([d["date"], d["company"], d["supplier"], d["number"], d["type"],
                            "" if d["amount"] is None else f"{d['amount']:.2f}", d["currency"], d["status"], path, sha])
                manifest.append({**d, "file": path, "sha256": sha})
            z.writestr("ledger.csv", "﻿" + ledger.getvalue())
            z.writestr("manifest.json", json.dumps({"documents": manifest}, indent=2, ensure_ascii=False))
        label = self._company(company_id).replace(" ", "-") or "all-companies"
        span = f"{date_from or 'start'}_{date_to or 'today'}"
        return f"documents_{label}_{span}.zip", buf.getvalue(), len(docs)

    # -- spending -------------------------------------------------------------

    def _payments(self) -> list[Any]:
        return [*self.repo.history_transactions, *(r.tx for r in self.repo.transactions.values())]

    def _needs_invoice(self, tx_id: str) -> bool:
        """Only payments the expected-evidence engine says need an invoice or receipt (§21)."""
        rec = self.repo.transactions.get(tx_id)
        if rec is None or rec.private or rec.decision is None:
            return False
        return rec.decision.expectation.value in ("invoice", "receipt")

    def _coverage_start(self) -> date:
        txs = self._payments()
        return min((t.booked_on for t in txs), default=self.svc._today())

    def supplier_summary(self, supplier: str, *, date_from: date | None = None,
                         date_to: date | None = None) -> dict[str, Any]:
        s = supplier.strip().lower()
        known = next((x for x in self.repo.suppliers.values()
                      if s in x.name.lower() or any(s in a.lower() for a in x.aliases)), None)
        keys = {k.upper() for k in ([known.name, *known.aliases] if known else [supplier])}
        today = self.svc._today()
        date_to = date_to or today
        pays = [t for t in self._payments() if any(k in t.counterparty.upper() for k in keys)
                and (date_from is None or t.booked_on >= date_from) and t.booked_on <= date_to]
        name = known.name if known else supplier.strip()
        docs = self.documents(supplier=name, date_from=date_from, date_to=date_to)
        spent = sum((-t.amount for t in pays if t.amount < 0), Decimal(0))
        refunds = sum((t.amount for t in pays if t.amount > 0), Decimal(0))
        by_month: dict[str, Decimal] = defaultdict(Decimal)
        for t in pays:
            if t.amount < 0:
                by_month[t.booked_on.strftime("%Y-%m")] += -t.amount
        issues = []
        amounts = [(-t.amount, t.booked_on) for t in sorted(pays, key=lambda t: t.booked_on) if t.amount < 0]
        for (a1, d1), (a2, d2) in zip(amounts, amounts[1:]):
            if a2 > a1 and (a2 - a1) / a1 >= Decimal("0.05"):
                issues.append(f"Price went up from {_money(a1)} to {_money(a2)} on {_d(d2)}.")
        matched = {tx for d in self.repo.documents.values() for tx in d.matched_tx_ids}
        unbacked = [t for t in pays if t.amount < 0 and t.id not in matched and self._needs_invoice(t.id)]
        for t in unbacked:
            issues.append(f"No invoice yet for the {_money(-t.amount)} payment on {_d(t.booked_on)}.")
        numbers = [d["number"] for d in docs if d["number"]]
        for n in sorted({n for n in numbers if numbers.count(n) > 1}):
            issues.append(f"Invoice {n} appears more than once.")
        for d in docs:
            if d["status"] == "on hold":
                issues.append(f"Invoice {d['number'] or d['id']} is on hold: the bank details changed.")
        coverage = self._coverage_start()
        note = ""
        if date_from and date_from < coverage:
            note = (f"I only have records from {coverage.day} {coverage:%B %Y}, so this covers "
                    f"{_d(coverage)} to {_d(date_to)}.")
        return {
            "supplier": name, "from": (max(date_from, coverage) if date_from else coverage).isoformat(),
            "to": date_to.isoformat(), "payments": len([t for t in pays if t.amount < 0]),
            "spent": float(spent), "refunds": float(refunds), "documents": len(docs),
            "byMonth": [{"month": k, "amount": float(v)} for k, v in sorted(by_month.items())],
            "average": float((spent / len(amounts)).quantize(Decimal("0.01"))) if amounts else 0.0,
            "issues": issues, "coverageNote": note,
            "evidence": [{"label": f"{d['supplier']} {d['number']}".strip(), "id": d["id"]} for d in docs[:6]],
        }

    def period_report(self, date_from: date, date_to: date, company_id: str = "") -> dict[str, Any]:
        if date_to < date_from:
            date_from, date_to = date_to, date_from
        own = {i for e in self.repo.entities for i in e.own_ibans}
        rows = [t for t in self._payments() if date_from <= t.booked_on <= date_to
                and (not company_id or (t.entity_id or self.repo.accounts.get(t.account_id) and
                                        self.repo.accounts[t.account_id].holder_id) == company_id)
                and (t.counterparty_iban or "") not in own]
        out = sum((-t.amount for t in rows if t.amount < 0), Decimal(0))
        inflow = sum((t.amount for t in rows if t.amount > 0), Decimal(0))
        by_sup: dict[str, Decimal] = defaultdict(Decimal)
        for t in rows:
            if t.amount < 0:
                by_sup[t.counterparty.split("*")[0].strip().title()] += -t.amount
        docs = self.documents(company_id=company_id, date_from=date_from, date_to=date_to)
        matched = {tx for d in self.repo.documents.values() for tx in d.matched_tx_ids}
        missing = [t for t in rows if t.amount < 0 and t.id not in matched and self._needs_invoice(t.id)]
        rid = self._id("rpt")
        title = (f"{self._company(company_id) or 'All companies'} · "
                 f"{_d(date_from)} – {_d(date_to)}")
        csv_buf = io.StringIO()
        w = csv.writer(csv_buf, delimiter=";")
        w.writerow(["date", "counterparty", "amount", "currency", "has_invoice"])
        for t in sorted(rows, key=lambda t: t.booked_on):
            w.writerow([t.booked_on.isoformat(), t.counterparty, f"{t.amount:.2f}", t.currency,
                        "yes" if t.id in matched else ("n/a" if t.amount > 0 else "no")])
        report = {
            "id": rid, "title": title, "from": date_from.isoformat(), "to": date_to.isoformat(),
            "spent": float(out), "received": float(inflow), "payments": len(rows), "documents": len(docs),
            "missingInvoices": len(missing),
            "topSuppliers": [{"name": k, "amount": float(v)}
                             for k, v in sorted(by_sup.items(), key=lambda kv: -kv[1])[:8]],
            "csv": "﻿" + csv_buf.getvalue(), "filename": f"report_{date_from}_{date_to}.csv",
        }
        self.reports[rid] = report
        return report

    # -- outbox ---------------------------------------------------------------

    def draft_email(self, to: list[str], subject: str, body: str,
                    attachments: list[dict[str, str]] | None = None) -> OutboxMessage:
        bad = [a for a in to if not _EMAIL.fullmatch(a)]
        if not to or bad:
            raise ValueError("I need a valid email address to send this to.")
        msg = OutboxMessage(id=self._id("out"), to=to, subject=subject.strip()[:200], body=body.strip()[:5000],
                            attachments=attachments or [])
        self.outbox[msg.id] = msg
        return msg

    def send(self, message_id: str) -> OutboxMessage:
        """Owner confirmed (§25). Delivery goes through the configured mailer, if any."""
        msg = self.outbox.get(message_id)
        if msg is None:
            raise KeyError(message_id)
        if msg.status != "draft":
            return msg
        mailer = getattr(self.svc, "mailer", None)
        if mailer is not None:
            files = []
            for a in msg.attachments:
                if a["kind"] == "document" and (f := self.document_file(a["id"])):
                    files.append(f)
                elif a["kind"] == "report" and (r := self.reports.get(a["id"])):
                    files.append((r["filename"], "text/csv", r["csv"].encode()))
            mailer.send(msg.to, msg.subject, msg.body, files)
            msg.delivery = "Sent."
        else:
            msg.delivery = "Recorded in the outbox. Email delivery is not connected in this demo."
        msg.status = "sent"
        msg.sent_at = self.svc._now().isoformat()
        self.svc.orchestrator.activity(self.svc._now(), "answered",
                                       f"Sent “{msg.subject}” to {', '.join(msg.to)}.")
        return msg

    # -- tasks ----------------------------------------------------------------

    def task_dict(self, t: OwnerTask) -> dict[str, Any]:
        return {"id": t.id, "title": t.title, "due": t.due.isoformat() if t.due else None,
                "companyId": t.company_id or None, "company": self._company(t.company_id) or None,
                "status": t.status, "createdAt": t.created_at, "doneAt": t.done_at}

    def add_task(self, title: str, due: date | None = None, company_id: str = "") -> dict[str, Any]:
        title = " ".join(str(title or "").split())[:200]
        if not title:
            raise ValueError("Tell me what the task is.")
        if company_id and company_id not in self.repo.companies:
            raise ValueError("I don't know that company.")
        t = OwnerTask(id=self._id("task"), title=title, due=due, company_id=company_id,
                      created_at=self.svc._now().isoformat())
        self.tasks[t.id] = t
        return self.task_dict(t)

    def list_tasks(self, include_done: bool = False) -> list[dict[str, Any]]:
        items = [t for t in self.tasks.values() if include_done or t.status == "open"]
        # Open first, then by due date (no date last), then in the order they were added.
        items.sort(key=lambda t: (t.status != "open", t.due is None, t.due or date.max, t.id))
        return [self.task_dict(t) for t in items]

    def complete_task(self, task_id: str) -> dict[str, Any]:
        t = self.tasks.get(task_id)
        if t is None:
            raise KeyError(task_id)
        if t.status != "done":
            t.status = "done"
            t.done_at = self.svc._now().isoformat()
        return self.task_dict(t)


# ----------------------------------------------------------------------------- date parsing


def _parse_day(text: str, today: date, *, end: bool = False) -> date | None:
    t = text.strip().lower().rstrip(".,")
    try:
        return date.fromisoformat(t)
    except ValueError:
        pass
    m = re.fullmatch(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4})", t)
    if m:
        d, mo, y = int(m[1]), int(m[2]), int(m[3])
        return date(y + 2000 if y < 100 else y, mo, d)
    m = re.fullmatch(r"(\d{1,2})(?:st|nd|rd|th)?\s+([a-z]+)\s*(\d{4})?", t) or None
    if m and m[2] in _MONTHS:
        return date(int(m[3] or today.year), _MONTHS[m[2]], int(m[1]))
    m = re.fullmatch(r"([a-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?,?\s*(\d{4})?", t)
    if m and m[1] in _MONTHS:
        return date(int(m[3] or today.year), _MONTHS[m[1]], int(m[2]))
    m = re.fullmatch(r"([a-z]+)\s*(\d{4})?", t)
    if m and m[1] in _MONTHS:
        y, mo = int(m[2] or today.year), _MONTHS[m[1]]
        if end:
            nxt = date(y + (mo == 12), mo % 12 + 1, 1)
            return nxt - timedelta(days=1)
        return date(y, mo, 1)
    return None


def _period(text: str, today: date) -> tuple[date, date] | None:
    t = text.lower()
    m = re.search(r"(?:between|from)\s+(.+?)\s+(?:and|to|until|-)\s+(.+?)(?:\s+(?:for|and send|to\s+\S+@)|[?.!]|$)", t)
    if m:
        a, b = _parse_day(m[1], today), _parse_day(m[2], today, end=True)
        if a and b:
            return a, b
    m = re.search(r"(?:past|last)\s+(\d+|one|two|three|six|twelve)?\s*(year|month|week|day)s?", t)
    if m:
        words = {"one": 1, "two": 2, "three": 3, "six": 6, "twelve": 12}
        n = int(words.get(m[1] or "one", m[1] or 1))
        days = {"year": 365, "month": 30, "week": 7, "day": 1}[m[2]] * n
        if m[2] == "month" and n == 1 and "last month" in t:
            first = today.replace(day=1)
            prev_end = first - timedelta(days=1)
            return prev_end.replace(day=1), prev_end
        return today - timedelta(days=days), today
    for name, num in _MONTHS.items():
        if len(name) > 3 and re.search(rf"\b{name}\b", t):
            y = int(mm[1]) if (mm := re.search(rf"{name}\s+(\d{{4}})", t)) else today.year
            start = date(y, num, 1)
            return start, _parse_day(f"{name} {y}", today, end=True) or start
    return None


# ----------------------------------------------------------------------------- brains


def _reply(text: str, cards: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"reply": text, "cards": cards or []}


_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_DUE = re.compile(
    r"\s*\b(?:(?:on|by|before|until|for)\s+)?(?:(?P<rel>today|tonight|tomorrow)"
    r"|(?:next\s+)?(?P<wd>monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
    r"|(?P<abs>\d{4}-\d{2}-\d{2}|\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}"
    r"|\d{1,2}(?:st|nd|rd|th)?\s+[a-z]+(?:\s+\d{4})?|[a-z]+\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s*\d{4})?))\b[.!]?\s*$")


def _due(text: str, today: date) -> tuple[date | None, str]:
    """A due date at the end of a task ("… on Friday", "… by 5 October"), and the text without it."""
    m = _DUE.search(text.lower())
    if not m:
        return None, text
    due: date | None = None
    if m["rel"]:
        due = today if m["rel"] in ("today", "tonight") else today + timedelta(days=1)
    elif m["wd"]:
        ahead = (_WEEKDAYS.index(m["wd"]) - today.weekday()) % 7
        due = today + timedelta(days=ahead or 7)
    elif m["abs"]:
        try:
            due = _parse_day(m["abs"], today)
        except ValueError:
            due = None
    if due is None:
        return None, text
    return due, text[: m.start()].rstrip(" ,")


def _task_title(text: str) -> str:
    t = text.strip().rstrip(".!")
    return t[:1].upper() + t[1:]


def _tasks_card(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {"type": "tasks", "items": items}


_ADD_TASK = re.compile(
    r"^\s*(?:please\s+)?(?:(?:can you\s+)?remind me\s+(?:to\s+)?|(?:add|create|make|new)\s+(?:a\s+)?(?:new\s+)?"
    r"(?:task|to-?do|reminder)\s*(?:to\s+|:|-)?\s*|(?:task|to-?do)\s*:\s*)(?P<what>.+)$", re.I)
_DONE_TASK = re.compile(
    r"^\s*(?:mark\s+(?:the\s+)?(?:task\s+)?(?P<a>.+?)\s+(?:as\s+)?(?:done|complete|completed|finished)"
    r"|tick off\s+(?:the\s+)?(?:task\s+)?(?P<c>.+?)"
    r"|(?:done|finished|completed?)\s*[:\-]\s*(?P<b>.+))\s*[.!]?\s*$", re.I)
_LIST_TASKS = re.compile(r"\b(?:my|open|all|the)\s+(?:tasks|to-?dos|reminders)\b|^\s*(?:tasks|to-?dos|reminders|"
                         r"to-?do list|task list)\s*\??\s*$", re.I)
_PAY = re.compile(r"^\s*(?:please\s+)?(?:pay|transfer|wire|send money|make (?:a|the) payment)\b", re.I)
_HELLO = re.compile(r"^\s*(?:hi|hello|hey|ol[aá]|good (?:morning|afternoon|evening)|what can you do|help)\b", re.I)


class RuleBrain:
    """Deterministic understanding of the common requests. No model, no network."""

    def __init__(self, op: Operator) -> None:
        self.op = op

    def _supplier_in(self, text: str) -> str | None:
        t = text.lower()
        for s in self.op.repo.suppliers.values():
            for n in [s.name, *s.aliases]:
                if n and re.search(rf"\b{re.escape(n.lower())}\b", t):
                    return s.name
        return None

    def _tasks(self, t: str, today: date) -> dict[str, Any] | None:
        op = self.op
        if m := _ADD_TASK.match(t):
            due, what = _due(m["what"], today)
            if not what.strip():
                return _reply("Tell me what the task is.")
            task = op.add_task(_task_title(what), due)
            when = f" for {_d(due)}" if due else ""
            return _reply(f"Added to your tasks{when}: {task['title']}.", [_tasks_card(op.list_tasks())])
        if m := _DONE_TASK.match(t):
            words = set(re.findall(r"[a-z0-9]{3,}", (m["a"] or m["b"] or m["c"] or "").lower())) - {"the", "task"}
            open_tasks = [x for x in op.tasks.values() if x.status == "open"]
            scored = sorted(((len(words & set(re.findall(r"[a-z0-9]{3,}", x.title.lower()))), x.id) for x in open_tasks),
                            reverse=True)
            if not scored or scored[0][0] == 0:
                return _reply("I can't find an open task like that.", [_tasks_card(op.list_tasks())] if open_tasks else [])
            done = op.complete_task(scored[0][1])
            return _reply(f"Done: {done['title']}.", [_tasks_card(op.list_tasks())] if op.list_tasks() else [])
        if _LIST_TASKS.search(t):
            items = op.list_tasks()
            if not items:
                return _reply("You have no open tasks. Say “remind me to …” to add one.")
            return _reply(f"You have {len(items)} open task{'s' if len(items) != 1 else ''}.", [_tasks_card(items)])
        return None

    def handle(self, message: str) -> dict[str, Any]:
        op, t = self.op, message.strip()
        low = t.lower()
        today = op.svc._today()

        if (answer := self._tasks(t, today)) is not None:
            return answer
        if _PAY.match(t):
            text = ("I don't move money. You make payments from your bank; I check the invoice and the bank details "
                    "first and tell you if something looks wrong.")
            held = [n for n in op.svc.needs_you()["items"] if n.get("kind") == "approval"]
            if held:
                text += f" {held[0]['merchant']}'s payment is on hold until you confirm it in Needs you."
            return _reply(text, [{"type": "evidence", "items": [
                {"label": f"{n['merchant']} · {n.get('eyebrow', 'Needs you')}", "id": f"needs:{n['id']}"} for n in held]}]
                if held else [])
        if _HELLO.match(t):
            return _reply("Hello. Ask me about your companies, payments, invoices or suppliers, or give me a task: "
                          "“Find the Vodafone invoice and send it to marc@…”, “Summarise Adobe for the past year”, "
                          "“Remind me to call the accountant on Friday”.")
        emails = _EMAIL.findall(t)
        wants_send = bool(emails) and bool(re.search(r"\b(send|email|mail|forward|share)\b", low))

        if re.search(r"\b(summar|overview|breakdown|analy[sz]e|check)\w*", low) and (sup := self._supplier_in(t)):
            span = _period(low, today)
            s = op.supplier_summary(sup, date_from=span[0] if span else None, date_to=span[1] if span else None)
            text = (f"{s['supplier']}: {_money(Decimal(str(s['spent'])))} across {s['payments']} payments "
                    f"and {s['documents']} document{'s' if s['documents'] != 1 else ''}.")
            text += f" {s['coverageNote']}" if s["coverageNote"] else ""
            text += (" I found no issues." if not s["issues"] else
                     f" I found {len(s['issues'])} thing{'s' if len(s['issues']) != 1 else ''} to look at.")
            cards: list[dict[str, Any]] = [{"type": "summary", **s}]
            if wants_send:
                msg = op.draft_email(emails, f"{s['supplier']} spending summary",
                                     text + "\n\n" + "\n".join(f"- {i}" for i in s["issues"]))
                cards.append(_draft_card(msg))
            return _reply(text, cards)

        if re.search(r"\breport\b", low):
            span = _period(low, today)
            if span is None:
                first = today.replace(day=1)
                end = first - timedelta(days=1)
                span = (end.replace(day=1), end)
            company = next((cid for cid, e in op.repo.companies.items() if e.name.lower() in low), "")
            r = op.period_report(span[0], span[1], company)
            text = (f"Report ready for {r['title']}: {_money(Decimal(str(r['spent'])))} spent across "
                    f"{r['payments']} payments, {r['documents']} documents, "
                    f"{r['missingInvoices']} payment{'s' if r['missingInvoices'] != 1 else ''} still without an invoice.")
            cards = [{"type": "report", **{k: v for k, v in r.items() if k != "csv"}}]
            if wants_send:
                msg = op.draft_email(emails, f"Report {r['title']}",
                                     f"Hello,\n\nAttached is the report for {r['title']}.\n\nBest regards",
                                     [{"kind": "report", "id": r["id"], "name": r["filename"]}])
                cards.append(_draft_card(msg))
                text += " The email is ready. Tap Send to deliver it."
            return _reply(text, cards)

        if re.search(r"\b(find|show|get|where|send|forward)\b", low) and re.search(r"\b(invoice|receipt|document|bill|fatura)s?\b", low):
            amount = None
            if m := re.search(r"€\s?([\d.,]+)|([\d.,]+)\s?(?:€|eur)", low):
                raw = (m[1] or m[2]).replace(",", "")
                try:
                    amount = Decimal(raw)
                except Exception:
                    amount = None
            sup = self._supplier_in(t) or ""
            number = m[0] if (m := re.search(r"\b[A-Z]{1,4}[\s-]?\d{2,4}[/-]\d+\b|\b[A-Z]{2,}-\d{3,}(?:-\d+)*\b", t)) else ""
            span = _period(low, today)
            docs = op.documents(supplier=sup, amount=amount, query=number,
                                date_from=span[0] if span else None, date_to=span[1] if span else None)
            if not docs:
                return _reply("I couldn't find a matching document. Tell me the supplier, the amount or the month.")
            pick = docs[0]
            text = (f"Found {pick['supplier']} {pick['number']} · {_money(Decimal(str(pick['amount'] or 0)))} · "
                    f"{pick['date']}." + (f" {len(docs) - 1} more match." if len(docs) > 1 else ""))
            cards = [{"type": "documents", "items": docs[:5]}]
            if wants_send:
                msg = op.draft_email(emails, f"Invoice {pick['number'] or pick['supplier']}",
                                     f"Hello,\n\nAttached is {pick['supplier']} invoice {pick['number']} "
                                     f"({_money(Decimal(str(pick['amount'] or 0)))}, {pick['date']}).\n\nBest regards",
                                     [{"kind": "document", "id": pick["id"], "name": pick["filename"]}])
                cards.append(_draft_card(msg))
                text += " The email is ready. Tap Send to deliver it."
            return _reply(text, cards)

        ans = op.svc.ask(t)
        return _reply(ans.get("answer", ""), [{"type": "evidence", "items": ans.get("evidence", [])}]
                      if ans.get("evidence") else [])


def _draft_card(msg: OutboxMessage) -> dict[str, Any]:
    return {"type": "email", "id": msg.id, "to": msg.to, "subject": msg.subject, "body": msg.body,
            "attachments": msg.attachments, "status": msg.status}


# The tools the model can use (the server brain, and the browser chat via /api/chat/tool).
_S = {"type": "string"}
_DATE = {"type": "string", "description": "ISO date, YYYY-MM-DD"}
TOOLS: list[dict[str, Any]] = [
    {"name": "business_status", "description": "Overview right now: each company's month-end status, what needs "
     "the owner (with question ids and options), payments due soon, connection problems and what was handled "
     "recently. Start here for general questions.",
     "input_schema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "month_status", "description": "Month-end detail for one company: completeness, what is missing and "
     "why, with evidence. Month is YYYY-MM.",
     "input_schema": {"type": "object", "properties": {"company_id": _S, "month": _S},
                      "required": ["company_id", "month"], "additionalProperties": False}},
    {"name": "recent_activity", "description": "What the operator did recently (documents collected, invoices "
     "recovered, questions answered), newest first.",
     "input_schema": {"type": "object", "properties": {"limit": {"type": "integer"}}, "additionalProperties": False}},
    {"name": "connections_status", "description": "Health of connected email, banks, cards and other sources.",
     "input_schema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "search_documents", "description": "Find invoices, receipts and other documents in the evidence store. "
     "All filters optional.",
     "input_schema": {"type": "object", "properties": {
         "query": _S, "supplier": _S, "company_id": _S, "date_from": _DATE, "date_to": _DATE,
         "amount": {"type": "number"}}, "additionalProperties": False}},
    {"name": "supplier_summary", "description": "Spending with one supplier over a period, by month, plus issues "
     "(price increases, payments without invoice, duplicate invoices, changed bank details).",
     "input_schema": {"type": "object", "properties": {"supplier": _S, "date_from": _DATE, "date_to": _DATE},
                      "required": ["supplier"], "additionalProperties": False}},
    {"name": "period_report", "description": "Build a spending report for a period (optionally one company). "
     "Returns a report id that can be attached to an email.",
     "input_schema": {"type": "object", "properties": {"date_from": _DATE, "date_to": _DATE, "company_id": _S},
                      "required": ["date_from", "date_to"], "additionalProperties": False}},
    {"name": "draft_email", "description": "Prepare an email with document or report attachments. It is NOT sent: "
     "the owner reviews it and taps Send.",
     "input_schema": {"type": "object", "properties": {
         "to": {"type": "array", "items": _S}, "subject": _S, "body": _S,
         "document_ids": {"type": "array", "items": _S}, "report_ids": {"type": "array", "items": _S}},
         "required": ["to", "subject", "body"], "additionalProperties": False}},
    {"name": "answer_question", "description": "Record the owner's answer to one of the open questions from "
     "business_status (for example which company a payment belongs to). Use ONLY when the owner has clearly "
     "given the answer in this conversation. Payment approvals and changed bank details can never be answered "
     "here: the owner confirms those in Needs you.",
     "input_schema": {"type": "object", "properties": {
         "question_id": _S, "option_id": _S,
         "remember": {"type": "boolean", "description": "Apply the same answer to future similar payments"}},
         "required": ["question_id", "option_id"], "additionalProperties": False}},
    {"name": "create_task", "description": "Add a task or reminder to the owner's task list.",
     "input_schema": {"type": "object", "properties": {"title": _S, "due_date": _DATE, "company_id": _S},
                      "required": ["title"], "additionalProperties": False}},
    {"name": "list_tasks", "description": "The owner's tasks (open ones unless include_done).",
     "input_schema": {"type": "object", "properties": {"include_done": {"type": "boolean"}},
                      "additionalProperties": False}},
    {"name": "complete_task", "description": "Mark one of the owner's tasks as done.",
     "input_schema": {"type": "object", "properties": {"task_id": _S}, "required": ["task_id"],
                      "additionalProperties": False}},
]

SYSTEM = ("You are the back-office operator for a small business owner: an excellent financial administrator. "
          "Be short, calm and precise; plain language, no accounting jargon, never chatty. Answer only from tool "
          "results, never from memory, and cite document numbers and amounts. Money is in euros. "
          "You can: look things up, prepare reports, draft emails (the owner taps Send), record the owner's "
          "answers to open questions, and keep the owner's task list. You never move money, never approve "
          "payments or changed bank details, and never send anything yourself; say so plainly when asked. "
          "If data does not cover the period asked, say which period it covers. When something is done, say "
          "“Done.” and what changed.")

# Tools that change something: the browser keeps and replays them (web/lib/engine.ts).
CHANGING_TOOLS = frozenset({"period_report", "draft_email", "answer_question", "create_task", "complete_task"})


def _iso(v: Any) -> date | None:
    try:
        return date.fromisoformat(str(v)) if v else None
    except ValueError:
        return None


def run_tool(op: Operator, name: str, args: dict[str, Any], cards: list[dict[str, Any]]) -> Any:
    """Run one tool against the engine. Raises ValueError/KeyError with a message the model can act on."""
    svc = op.svc
    if name == "business_status":
        home = svc.home()
        needs = []
        for n in svc.needs_you()["items"]:
            item = {"question_id": n["id"], "kind": n.get("kind"), "merchant": n.get("merchant"),
                    "amount": n.get("amount"), "date": n.get("date"),
                    "question": n.get("question") or n.get("title"),
                    "owner_must_confirm_in_needs_you": n.get("kind") == "approval"}
            if n.get("kind") != "approval":
                item["options"] = [{"option_id": o["id"], "label": o["label"]} for o in n.get("options", [])]
            needs.append(item)
        companies = [{"company_id": c["id"], "name": c["name"], "status": c.get("statusLabel"),
                      "detail": c.get("detail"), "current_month": c.get("currentMonth")}
                     for c in svc.companies()["companies"]]
        return {"today": svc._today().isoformat(), "headline": home.get("status") or home.get("headline"),
                "companies": companies, "needs_owner": needs, "due_soon": home.get("dueSoon"),
                "handled_recently": home.get("handled"), "open_tasks": op.list_tasks()}
    if name == "month_status":
        m = svc.month(str(args.get("company_id", "")), str(args.get("month", "")))
        if m is None:
            raise ValueError("No month-end data for that company and month.")
        return m
    if name == "recent_activity":
        limit = max(1, min(int(args.get("limit") or 15), 50))
        return svc.activity()["items"][:limit]
    if name == "connections_status":
        return svc.connections()
    if name == "search_documents":
        amount = Decimal(str(args["amount"])) if args.get("amount") is not None else None
        docs = op.documents(query=args.get("query", ""), supplier=args.get("supplier", ""),
                            company_id=args.get("company_id", ""), date_from=_iso(args.get("date_from")),
                            date_to=_iso(args.get("date_to")), amount=amount)
        if docs:
            cards.append({"type": "documents", "items": docs[:5]})
        return docs[:20]
    if name == "supplier_summary":
        s = op.supplier_summary(args["supplier"], date_from=_iso(args.get("date_from")),
                                date_to=_iso(args.get("date_to")))
        cards.append({"type": "summary", **s})
        return s
    if name == "period_report":
        a, b = _iso(args.get("date_from")), _iso(args.get("date_to"))
        if not a or not b:
            raise ValueError("dates must be YYYY-MM-DD")
        r = op.period_report(a, b, args.get("company_id", ""))
        cards.append({"type": "report", **{k: v for k, v in r.items() if k != "csv"}})
        return {k: v for k, v in r.items() if k != "csv"}
    if name == "draft_email":
        att = [{"kind": "document", "id": i, "name": (op.document_file(i) or (i,))[0]}
               for i in args.get("document_ids", []) if i in op.repo.documents]
        att += [{"kind": "report", "id": i, "name": op.reports[i]["filename"]}
                for i in args.get("report_ids", []) if i in op.reports]
        msg = op.draft_email(list(args["to"]), args["subject"], args["body"], att)
        cards.append(_draft_card(msg))
        return {"draft_id": msg.id, "status": "waiting for the owner to tap Send"}
    if name == "answer_question":
        qid, option = str(args.get("question_id", "")), str(args.get("option_id", ""))
        item = next((n for n in svc.needs_you()["items"] if n["id"] == qid), None)
        if item is None:
            raise ValueError("That question is not open any more.")
        if item.get("kind") == "approval":
            cards.append({"type": "evidence", "items": [{"label": f"{item.get('merchant')} · confirm in Needs you",
                                                         "id": f"needs:{qid}"}]})
            raise ValueError("This needs the owner's own confirmation in Needs you (payment approvals and changed "
                             "bank details are never answered in chat).")
        return svc.answer(qid, option, bool(args.get("remember", False)))
    if name == "create_task":
        due = _iso(args.get("due_date"))
        if args.get("due_date") and due is None:
            raise ValueError("due_date must be YYYY-MM-DD")
        task = op.add_task(str(args.get("title", "")), due, str(args.get("company_id") or ""))
        cards.append(_tasks_card(op.list_tasks()))
        return task
    if name == "list_tasks":
        items = op.list_tasks(include_done=bool(args.get("include_done", False)))
        if items:
            cards.append(_tasks_card(items))
        return items
    if name == "complete_task":
        try:
            task = op.complete_task(str(args.get("task_id", "")))
        except KeyError:
            raise ValueError("No task with that id. Use list_tasks.") from None
        cards.append(_tasks_card(op.list_tasks(include_done=True)))
        return task
    raise ValueError(f"unknown tool {name}")


class ClaudeBrain:
    """Claude with tool use over :class:`Operator` (manual loop, server only)."""

    MODEL = "claude-sonnet-5-5"

    def __init__(self, op: Operator, client: Any = None, max_turns: int = 8) -> None:
        self.op = op
        if client is None:
            import anthropic  # lazy: server-only dependency

            client = anthropic.Anthropic()
        self.client = client
        self.max_turns = max_turns

    def _run(self, name: str, args: dict[str, Any], cards: list[dict[str, Any]]) -> Any:
        return run_tool(self.op, name, args, cards)

    def handle(self, message: str, history: list[dict[str, str]] | None = None) -> dict[str, Any]:
        messages: list[dict[str, Any]] = [
            {"role": h["role"], "content": h["content"]} for h in (history or [])[-10:]
            if h.get("role") in ("user", "assistant") and isinstance(h.get("content"), str)
        ]
        messages.append({"role": "user", "content": f"Today is {self.op.svc._today().isoformat()}. {message}"})
        cards: list[dict[str, Any]] = []
        for _ in range(self.max_turns):
            response = self.client.messages.create(
                model=self.MODEL, max_tokens=16000, system=SYSTEM, tools=TOOLS, messages=messages,
                output_config={"effort": "medium"},
            )
            if response.stop_reason == "refusal":
                return _reply("I can't help with that one.", cards)
            messages.append({"role": "assistant", "content": response.content})
            uses = [b for b in response.content if b.type == "tool_use"]
            if response.stop_reason != "tool_use" or not uses:
                text = "".join(b.text for b in response.content if b.type == "text").strip()
                return _reply(text or "Done.", cards)
            results = []
            for b in uses:
                try:
                    out = self._run(b.name, dict(b.input), cards)
                    results.append({"type": "tool_result", "tool_use_id": b.id,
                                    "content": json.dumps(out, default=str)[:60000]})
                except Exception as exc:  # the model sees the failure and can recover
                    results.append({"type": "tool_result", "tool_use_id": b.id, "is_error": True,
                                    "content": str(exc)[:500]})
            messages.append({"role": "user", "content": results})
        return _reply("That took too many steps. Try asking in smaller parts.", cards)
