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
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from backoffice.closure import Month
from backoffice.learning import counterparty_key, day_month, display_name, fold, format_money, learn_from_transactions
from backoffice.learning.plain import count_phrase, join_and
from backoffice.mailer import SIMULATED_NOTE, is_simulated
from backoffice.spending import CATEGORIES, MONTH_NAMES, Ledger, Line, Money, category_label
from backoffice.understanding import (
    Period,
    Slots,
    Understanding,
    Vocabulary,
    month_period,
    period_between,
    understand,
    unrelated_words,
)

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
    # draft -> sent (a mailer accepted it) | waiting (confirmed, no mailer yet) -> sent | cancelled
    status: str = "draft"
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
            out.append(self.document_item(rec))
        out.sort(key=lambda x: (x["date"], x["supplier"]), reverse=True)
        return out

    def document_item(self, rec: Any) -> dict[str, Any]:
        """One document as the documents list and the accountant's export show it."""
        d = rec.document
        issued = d.issue_date or rec.received_at.date()
        name = d.supplier_name or ""
        ev = self.repo.registry.get(self.repo.tenant_id, rec.evidence_ids[0]) if rec.evidence_ids else None
        if rec.on_hold and not rec.hold_released:
            status = "on hold"
        elif rec.supporting:
            status = "supporting evidence, not an invoice"
        elif rec.matched_tx_ids:
            status = "matched"
        elif rec.paid_in_cash:
            status = "paid in cash"
        else:
            status = "waiting for payment"
        return {
            "id": d.id, "supplier": name or "Unknown", "number": d.invoice_number or "",
            "type": d.doc_type.value.replace("_", " "), "date": issued.isoformat(),
            "amount": float(d.gross_amount) if d.gross_amount is not None else None,
            "currency": d.currency, "companyId": d.entity_id or "", "company": self._company(d.entity_id),
            "status": status,
            # For the accountant (§28): a pro-forma, quote, delivery note, order or account statement
            # is supporting evidence only and is never booked.
            "booking": "supporting" if rec.supporting else "booked",
            "quality": d.quality.name.lower(), "origin": rec.origin,
            "filename": (ev.filename if ev and ev.filename else f"{d.id}.bin"),
            "evidenceIds": list(rec.evidence_ids),
        }

    def document_file(self, document_id: str) -> tuple[str, str, bytes] | None:
        rec = self.repo.documents.get(document_id)
        if rec is None or not rec.evidence_ids:
            return None
        ev = self.repo.registry.get(self.repo.tenant_id, rec.evidence_ids[0])
        data = self.repo.registry.open(self.repo.tenant_id, rec.evidence_ids[0])
        return (ev.filename or f"{document_id}.bin", ev.mime_type or "application/octet-stream", data)

    def export_zip(self, *, company_id: str = "", date_from: date | None = None,
                   date_to: date | None = None) -> tuple[str, bytes, int]:
        """All documents in a period as a ZIP: originals, ledger.csv and manifest.json.

        Supporting evidence (a pro-forma, quote, delivery note, order or account statement) is
        marked ``supporting`` in the ``booking`` column and filed under ``documents/supporting/``:
        it is kept for the accountant to see, never to book (§3, §28).
        """
        import zipfile

        docs = self.documents(company_id=company_id, date_from=date_from, date_to=date_to)
        buf = io.BytesIO()
        ledger = io.StringIO()
        w = csv.writer(ledger, delimiter=";")
        w.writerow(["date", "company", "supplier", "number", "type", "gross", "currency", "status", "booking", "file",
                    "sha256"])
        manifest = []
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for d in docs:
                f = self.document_file(d["id"])
                path = ""
                sha = ""
                if f:
                    safe = re.sub(r"[^\w.-]+", "_", f[0])[:80]
                    folder = "documents/supporting" if d["booking"] == "supporting" else "documents"
                    path = f"{folder}/{d['date']}_{d['id']}_{safe}"
                    z.writestr(path, f[2])
                    sha = self.repo.registry.get(self.repo.tenant_id, d["evidenceIds"][0]).sha256
                w.writerow([d["date"], d["company"], d["supplier"], d["number"], d["type"],
                            "" if d["amount"] is None else f"{d['amount']:.2f}", d["currency"], d["status"],
                            d["booking"], path, sha])
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
        """Owner confirmed (§25). It counts as sent only when the configured mailer accepted it.

        Without a mailer it waits ("waiting") and says so; a mailer that refuses raises, and nothing
        is marked sent (the production server then voids the change, backoffice.server.runtime).
        """
        msg = self.outbox.get(message_id)
        if msg is None:
            raise KeyError(message_id)
        if msg.status not in ("draft", "waiting"):
            return msg
        now = self.svc._now()
        mailer = getattr(self.svc, "mailer", None)
        if mailer is None:
            if msg.status == "draft":
                msg.status = "waiting"
                self.svc.orchestrator.activity(now, "waiting", f"Wrote “{msg.subject}” to {', '.join(msg.to)}. "
                                                               "It is waiting to be sent.")
            msg.delivery = "Not sent yet: email sending is not set up here, so it is waiting to be sent."
            return msg
        files = []
        for a in msg.attachments:
            if a["kind"] == "document" and (f := self.document_file(a["id"])):
                files.append(f)
            elif a["kind"] == "report" and (r := self.reports.get(a["id"])):
                files.append((r["filename"], "text/csv", r["csv"].encode()))
        mailer.send(msg.to, msg.subject, msg.body, files)
        msg.delivery = f"Sent. {SIMULATED_NOTE}" if is_simulated(mailer) else "Sent."
        msg.status = "sent"
        msg.sent_at = now.isoformat()
        self.svc.orchestrator.activity(now, "answered", f"Sent “{msg.subject}” to {', '.join(msg.to)}.")
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
# Money never moves from the chat, and changed bank details are only ever confirmed by the owner (§25–26).
_PAY = re.compile(r"^\s*(?:(?:please|pls|can you|could you|would you|will you|go ahead and|just)\s+)*"
                  r"(?:pay|transfer|wire|send money|send (?:the )?payment|make (?:a|the) payment|settle|"
                  r"approve (?:the )?(?:payment|transfer))\b", re.I)
_RELEASE = re.compile(r"^\s*(?:(?:please|pls|can you|could you|would you|will you|go ahead and|just)\s+)*"
                      r"(?:(?:approve|release|unblock|accept)\b.*\b(?:payment|transfer|bank details|iban|account)|"
                      r"(?:confirm|change|update)\b.*\b(?:bank details|iban|new account|account number))\b", re.I)
# "Put the IKEA payment on Hazel Tree", "the €418 payment is personal": the owner answering an open question.
_ASSIGN = re.compile(r"^\s*(?:(?:please|pls|ok|okay|can you|could you|just)\s+)*(?:put|assign|move|book|charge|"
                     r"allocate|file|record|mark)\b"
                     r"|^\s*(?:the\s+)?[\w€.,' ]{1,40}?\b(?:is|was|goes|belongs)\s+(?:for|to|with|under|personal|"
                     r"private)\b"
                     r"|^\s*(?:it|that|this)(?:'s| is| was)\s+(?:for|personal|private)\b", re.I)
# "The right amount is €483.60", "use the QR value", "set it aside": the owner settling a document whose details
# disagree. That is only ever done with their own tap in Needs you (§19, §37), never from the chat.
_SETTLE = re.compile(
    r"\b(?:right|correct|real|true|actual|proper)\s+(?:one|amount|total|value|figure|number|date|vat|iban|nif|"
    r"tax number|invoice number)\b"
    r"|\buse\s+(?:the\s+)?(?:qr|pdf|photo|scan|text|email|first|second|other|bank|printed)\b"
    r"|\b(?:amount|total|value|vat|iban|date|number)\s+(?:is|was|should be)\s+(?:€|\d|the\s+(?:qr|pdf|first|second))"
    r"|\bset\s+(?:it|that|this|the\s+[\w ]{1,30}?invoice)\s+aside\b|\bneither\b", re.I)
_SEND = re.compile(r"\b(?:send|email|e-mail|mail|forward|share)\b", re.I)
_INCREASE = re.compile(r"\b(?:went|gone|go|going) up\b|\bincreas\w*|\bmore expensive\b|\bprices?\b|\bpricier\b|"
                       r"\baument\w*|\bsubiu\b|\bsubiram\b")
_OPTION_WORDS = {
    "spending": r"\b(?:paid|pay|spent|spend|spending|costs?|how much|total|money)\b",
    "find_document": r"\b(?:invoices?|receipts?|documents?|bills?|faturas?)\b",
    "supplier_summary": r"\b(?:summar\w*|overview|issues?|problems?|check)\b",
    "month_status": r"\b(?:closed?|complete|done|finished|status|ready|closing)\b",
    "payment_lookup": r"\b(?:payments?)\b",
}
_ACTIONS = frozenset({"report"})
# A question about a supplier's account statement ("Does our Vodafone statement match?").
_STATEMENT_Q = re.compile(r"\b(?:account\s+)?statements?\b|\bextratos?\b|\bconta\s+corrente\b", re.I)
_CADENCE = {"weekly": "a week", "monthly": "a month", "quarterly": "a quarter", "annual": "a year"}

HELP_TEXT = ("I answer from your records and prepare things for you to confirm. Ask what you spent or received in "
             "a period (by company, supplier or category), whether a month is closed, what needs you, what is due, "
             "which payments still have no invoice, VAT, subscriptions that went up, or for an invoice or a report "
             "to send. I can also keep your tasks: “Remind me to call the accountant on Friday”.")
FALLBACK_TEXT = ("I can't answer that from your records. I can tell you what you spent or received, whether a month is "
                 "closed, what needs you, what is due, and find invoices and payments. For example: “What did we "
                 "spend in September?” or “Did we pay Vodafone?”")


@dataclass
class _Answer:
    """One reply: plain text, cards for the chat, and the evidence behind it (chips)."""

    text: str
    cards: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[dict[str, str]] = field(default_factory=list)

    def chat(self) -> dict[str, Any]:
        cards = list(self.cards)
        if self.evidence:
            cards.append({"type": "evidence", "items": self.evidence})
        return _reply(self.text, cards)

    def all_evidence(self) -> list[dict[str, str]]:
        """Chips plus what the cards cite (for POST /api/ask, which has no cards)."""
        out = list(self.evidence)
        for card in self.cards:
            if card.get("type") == "documents":
                out += [{"label": f"{d['supplier']} {d['number']}".strip(), "id": d["evidenceIds"][0]}
                        for d in card["items"] if d.get("evidenceIds")]
            elif card.get("type") == "spending":
                out += [{"label": p["evidence"], "id": p["id"]} for p in card["payments"] if p["id"]][:6]
            elif card.get("type") == "evidence":
                out += card["items"]
        seen: set[str] = set()
        unique = []
        for e in out:
            if e["id"] not in seen:
                seen.add(e["id"])
                unique.append({"label": e["label"], "id": e["id"]})
        return unique

    def ask(self) -> dict[str, Any]:
        return {"answer": self.text, "evidence": self.all_evidence()}


class RuleBrain:
    """The built-in brain: understands the owner's message (backoffice.understanding), answers from the
    engine's records (backoffice.spending and the service), and prepares actions. No model, no network."""

    def __init__(self, op: Operator) -> None:
        self.op = op
        self.svc = op.svc
        self.repo = op.repo
        self.today = op.svc._today()
        self.vocab = Vocabulary.from_repo(op.repo, CATEGORIES)
        self._ledger: Ledger | None = None

    @property
    def ledger(self) -> Ledger:
        if self._ledger is None:
            self._ledger = Ledger(self.svc)
        return self._ledger

    # -- entry points ---------------------------------------------------------

    def handle(self, message: str, history: list[dict[str, str]] | None = None) -> dict[str, Any]:
        """The chat (POST /api/chat): may prepare reports, email drafts and tasks."""
        return self._respond(message, history, actions=True).chat()

    def ask(self, question: str) -> dict[str, Any]:
        """POST /api/ask: the same understanding, read-only, answer plus evidence."""
        return self._respond(question, None, actions=False).ask()

    def understand(self, message: str, history: list[dict[str, str]] | None = None) -> Understanding:
        return self._with_history(understand(message, self.vocab, self.today), history)

    def _respond(self, message: str, history: list[dict[str, str]] | None, *, actions: bool) -> _Answer:
        t = message.strip()
        if actions and (answer := self._tasks(t)) is not None:
            return answer
        if not actions and _LIST_TASKS.search(t):
            items = self.op.list_tasks()
            return _Answer(f"You have {count_phrase(len(items), 'open task')}." if items else "You have no open tasks.")
        if _PAY.match(t) or _RELEASE.match(t):
            return self._refuse_money()
        if _SETTLE.search(t) and not t.endswith("?"):
            conflicts = [n for n in self.repo.open_needs() if n.kind == "check"]
            if conflicts:
                return self._refuse_owner_tap(conflicts)
        if actions and _ASSIGN.match(t) and not t.endswith("?") and (answer := self._assign(t)) is not None:
            return answer
        u = self.understand(t, history)
        if _STATEMENT_Q.search(t) and (answer := self._statements(u)) is not None:
            self.svc.orchestrator.log("ask", "answer_question", response={
                "question": t[:500], "intent": "supplier_statement",
                "evidence": [e["id"] for e in answer.all_evidence()]})
            return answer
        if not actions and (u.intent in _ACTIONS or u.slots.emails or u.slots.to_accountant):
            return _Answer("I can prepare that in the chat, where you check it and confirm with one tap.")
        answer: _Answer = getattr(self, f"_i_{u.intent}")(u)
        self.svc.orchestrator.log("ask", "answer_question", response={
            "question": t[:500], "intent": u.intent, "evidence": [e["id"] for e in answer.all_evidence()]})
        return answer

    def _with_history(self, u: Understanding, history: list[dict[str, str]] | None) -> Understanding:
        """Follow-ups ("and in August?", "what about Company C?") and answers to a clarifying question.

        Only an elliptical message borrows from earlier turns: one with nothing in it
        but names, periods, categories and filler. "What is the weather in Porto?"
        after "is September closed?" is a new question and never gets the old answer.
        """
        if not history or u.intent in ("greeting", "thanks", "help"):
            return u
        turns = [h for h in history if isinstance(h, dict) and isinstance(h.get("content"), str)]
        users = [h["content"] for h in turns if h.get("role") == "user"][-4:]
        replies = [h["content"] for h in turns if h.get("role") == "assistant"]
        asked_back = bool(replies) and replies[-1].rstrip().endswith("?")
        if not users:
            return u
        if asked_back:  # the owner answers the question I asked ("the first one", "spending")
            older = understand(users[-1], self.vocab, self.today)
            if older.intent == "clarify":
                pick = self._pick(u, older.options)
                if pick is not None:
                    return replace(u, intent=pick, score=max(u.score, 0.6), slots=u.slots.merged_from(older.slots))
        if not (u.intent in ("clarify", "unknown") or u.follow_up) or unrelated_words(u.text, self.vocab):
            return u
        ctx = self._thread(users)
        if ctx is None:
            return u
        if u.intent in ("clarify", "unknown"):
            # "is it closed?" after a spending question: this message's own weak reading wins over the old one.
            weak = max(u.scores, key=lambda k: u.scores[k]) if u.scores else ctx.intent
            return replace(u, intent=weak, score=max(u.scores.get(weak, 0.0), ctx.score),
                           slots=u.slots.merged_from(ctx.slots))
        return replace(u, slots=u.slots.merged_from(ctx.slots))

    def _thread(self, users: list[str]) -> Understanding | None:
        """The question the conversation is about, with each follow-up's changes applied in order."""
        ctx: Understanding | None = None
        for text in users:
            older = understand(text, self.vocab, self.today)
            if older.intent in ("greeting", "thanks", "help"):
                continue
            if unrelated_words(older.text, self.vocab) and older.intent in ("clarify", "unknown"):
                ctx = None  # an unrelated question breaks the thread
            elif older.intent not in ("clarify", "unknown") and not older.follow_up:
                ctx = older
            elif ctx is not None:
                intent = ctx.intent if older.intent in ("clarify", "unknown") else older.intent
                ctx = replace(ctx, intent=intent, slots=older.slots.merged_from(ctx.slots))
            elif older.intent not in ("clarify", "unknown"):
                ctx = older
        return ctx

    @staticmethod
    def _pick(u: Understanding, options: tuple[str, ...]) -> str | None:
        t = u.text
        for i, pattern in enumerate((r"\b(?:first|1st|former|the first one|option 1)\b",
                                     r"\b(?:second|2nd|latter|option 2)\b", r"\b(?:third|3rd|option 3)\b")):
            if i < len(options) and re.search(pattern, t):
                return options[i]
        for option in options:
            if option in _OPTION_WORDS and re.search(_OPTION_WORDS[option], t):
                return option
        return u.intent if u.intent in options else None

    # -- helpers --------------------------------------------------------------

    def _m(self, amount: Decimal) -> str:
        return format_money(amount)

    def _day(self, value: date) -> str:
        return day_month(value, self.today)

    def _long(self, value: date) -> str:
        return f"{value.day} {value:%B} {value.year}"

    def _closing_month(self) -> Period:
        end = self.today.replace(day=1) - timedelta(days=1)
        return month_period(end.year, end.month, self.today)

    def _since_records(self) -> Period:
        first, _ = self.ledger.coverage()
        label = f"since {self._day(first)}"
        return Period(first, self.today, "range", label, label)

    def _companies(self, ids: list[str]) -> str:
        return join_and([self.repo.company_name(c) or c for c in ids])

    def _suppliers(self, ids: list[str]) -> str:
        return join_and([self.repo.suppliers[s].name for s in ids if s in self.repo.suppliers])

    def _coverage_line(self) -> str:
        first, last = self.ledger.coverage()
        return f"My records cover {self._long(first)} to {self._long(last)}."

    def _held_for(self, supplier_ids: list[str]) -> tuple[list[str], list[dict[str, str]]]:
        """Payments on hold for these suppliers because the bank details changed (§26)."""
        texts, chips = [], []
        for n in self.repo.open_needs():
            if n.kind != "approval":
                continue
            doc = self.repo.documents.get(n.subject_id)
            if doc is None or doc.supplier_id not in supplier_ids:
                continue
            month = self.repo.item_month(self.repo.items[doc.item_id])
            amount = f" {self._m(doc.document.gross_amount)}" if doc.document.gross_amount is not None else ""
            texts.append(f"{month.name if month else 'The next'}’s{amount} invoice is on hold: the bank details "
                         "changed, so I need you to confirm them in Needs you.")
            chips.append({"label": f"{display_name(doc.document.supplier_name)} · payment on hold",
                          "id": f"needs:{n.id}"})
        return texts, chips

    def _assign(self, t: str) -> _Answer | None:
        """The owner says which company a payment belongs to (the same answer as in Needs you, §37–38).

        Only questions about a company are answered here; payment approvals and changed bank
        details are never answered from the chat (§26).
        """
        u = understand(t, self.vocab, self.today)
        s = u.slots
        personal = re.search(r"\b(?:personal|private|mine|me personally|not (?:a |for (?:the |my |any )?)?company)\b",
                             u.text)
        if len(s.cost_center_ids) == 1 and not s.company_ids and not personal:
            return self._assign_cost_center(u)
        if len(s.company_ids) != 1 and not personal:
            return None
        open_choices = [n for n in self.repo.open_needs() if n.kind == "choice"]
        resolver = self.repo.resolver()
        matching = []
        for n in open_choices:
            rec = self.repo.transactions[n.subject_id]
            supplier = resolver.resolve_transaction(rec.tx).supplier
            if s.supplier_ids and (supplier is None or supplier.id not in s.supplier_ids):
                continue
            if s.amount is not None and abs(abs(rec.tx.amount) - s.amount) > Decimal("0.005"):
                continue
            matching.append(n)
        if not matching and (s.supplier_ids or s.amount is not None):
            # Not a plain choice: an approval or a conflict about that supplier or amount needs the owner's tap.
            blocked = []
            for n in self.repo.open_needs():
                doc = self.repo.documents.get(n.subject_id) if n.kind in ("check", "approval") else None
                if doc is None:
                    continue
                if s.supplier_ids and doc.supplier_id not in s.supplier_ids:
                    continue
                gross = doc.document.gross_amount
                if s.amount is not None and (gross is None or abs(abs(gross) - s.amount) > Decimal("0.005")):
                    continue
                blocked.append(n)
            if blocked:
                return self._refuse_owner_tap(blocked)
        if not matching or (len(matching) > 1 and not (s.supplier_ids or s.amount is not None)):
            return None
        if len(matching) > 1:
            return _Answer("More than one payment fits. Tell me the supplier and the amount.")
        need = matching[0]
        options = {o.id for o in need.question.options} if need.question else set()
        option = "personal" if personal and "personal" in options else f"entity:{s.company_ids[0]}" \
            if s.company_ids else ""
        if option not in options:
            return None
        remember = bool(re.search(r"\b(?:always|remember|from now on|every time|in future)\b", u.text))
        result = self.svc.answer(need.id, option, remember)
        text = result["message"]
        if result.get("learned"):
            text += f" {result['learned']}."
        return _Answer(text)

    def _refuse_owner_tap(self, needs: list[Any]) -> _Answer:
        """Conflicts and approvals are never settled from the chat: only the owner's own tap in Needs you (§19, §26)."""
        parts, chips = [], []
        if any(n.kind == "check" for n in needs):
            parts.append("I can't settle that from the chat. When a document's details disagree, you choose the "
                         "right one yourself in Needs you, with one tap.")
        if any(n.kind == "approval" for n in needs):
            parts.append("A payment on hold is only released by you, in Needs you, after you check it with the "
                         "supplier on a number you already had.")
        for n in needs:
            doc = self.repo.documents.get(n.subject_id)
            who = display_name(doc.document.supplier_name) if doc is not None else "Needs you"
            what = "payment on hold" if n.kind == "approval" else "invoice to check"
            chips.append({"label": f"{who} · {what}", "id": f"needs:{n.id}"})
        return _Answer(" ".join(parts), evidence=chips)

    def _assign_cost_center(self, u: Understanding) -> _Answer | None:
        """'Put the Leroy Merlin payment on Job Rua das Flores': the same answer as "Which job is this for?"."""
        s = u.slots
        option = f"cc:{s.cost_center_ids[0]}"
        resolver = self.repo.resolver()
        matching = []
        for n in self.repo.open_needs():
            if n.kind != "cost_center" or n.question is None or option not in {o.id for o in n.question.options}:
                continue
            rec = self.repo.transactions[n.subject_id]
            supplier = resolver.resolve_transaction(rec.tx).supplier
            if s.supplier_ids and (supplier is None or supplier.id not in s.supplier_ids):
                continue
            if s.amount is not None and abs(abs(rec.tx.amount) - s.amount) > Decimal("0.005"):
                continue
            matching.append(n)
        if not matching:
            return None
        if len(matching) > 1 and not (s.supplier_ids or s.amount is not None):
            return None
        if len(matching) > 1:
            return _Answer("More than one payment fits. Tell me the supplier and the amount.")
        remember = bool(re.search(r"\b(?:always|remember|from now on|every time|in future)\b", u.text))
        result = self.svc.answer(matching[0].id, option, remember)
        text = result["message"]
        if result.get("learned"):
            text += f" {result['learned']}."
        return _Answer(text)

    def _refuse_money(self) -> _Answer:
        text = ("I don't move money. You make payments from your bank; I check the invoice and the bank details "
                "first and tell you if something looks wrong.")
        held = [n for n in self.svc.needs_you()["items"] if n.get("kind") == "approval"]
        if held:
            text += (f" {held[0]['merchant']}'s payment is on hold until you confirm it in Needs you, after calling "
                     f"{held[0]['merchant']} on a number you already had.")
        return _Answer(text, evidence=[{"label": f"{n['merchant']} · {n.get('eyebrow', 'Needs you')}",
                                        "id": f"needs:{n['id']}"} for n in held])

    def _tasks(self, t: str) -> _Answer | None:
        op, today = self.op, self.today
        if m := _ADD_TASK.match(t):
            due, what = _due(m["what"], today)
            if not what.strip():
                return _Answer("Tell me what the task is.")
            task = op.add_task(_task_title(what), due)
            when = f" for {_d(due)}" if due else ""
            return _Answer(f"Added to your tasks{when}: {task['title']}.", [_tasks_card(op.list_tasks())])
        if m := _DONE_TASK.match(t):
            words = set(re.findall(r"[a-z0-9]{3,}", (m["a"] or m["b"] or m["c"] or "").lower())) - {"the", "task"}
            open_tasks = [x for x in op.tasks.values() if x.status == "open"]
            scored = sorted(((len(words & set(re.findall(r"[a-z0-9]{3,}", x.title.lower()))), x.id) for x in open_tasks),
                            reverse=True)
            if not scored or scored[0][0] == 0:
                return _Answer("I can't find an open task like that.", [_tasks_card(op.list_tasks())] if open_tasks else [])
            done = op.complete_task(scored[0][1])
            return _Answer(f"Done: {done['title']}.", [_tasks_card(op.list_tasks())] if op.list_tasks() else [])
        if _LIST_TASKS.search(t):
            items = op.list_tasks()
            if not items:
                return _Answer("You have no open tasks. Say “remind me to …” to add one.")
            return _Answer(f"You have {len(items)} open task{'s' if len(items) != 1 else ''}.", [_tasks_card(items)])
        return None

    # -- money ----------------------------------------------------------------

    def _compare_with(self, u: Understanding, period: Period) -> tuple[date, date, str] | None:
        s = u.slots
        if len(s.periods) > 1:
            other = s.periods[1]
            return other.start, other.end, other.label
        prev = period.previous(self.today)
        if s.compare:
            return prev.start, prev.end, prev.label
        first, _ = self.ledger.coverage()
        tracked = self.ledger.tracked_from()
        if period.grain not in ("month", "quarter", "year") or prev.start < first or tracked is None:
            return None
        same = (prev.end < tracked) == (period.end < tracked) and (prev.start >= tracked) == (period.start >= tracked)
        return (prev.start, prev.end, prev.label) if same else None

    def _cost_center_money(self, u: Understanding, *, direction: str) -> _Answer:
        """'How much did we spend on Job Rua das Flores in September?': the parts of payments on that job."""
        from backoffice.cost_centers import CostCenterViews
        from backoffice.cost_centers import Period as CostPeriod

        s = u.slots
        period = s.period or self._closing_month()
        views = CostCenterViews(self.svc)
        window = CostPeriod(period.start, period.end, period.label, period.phrase)
        resolver = self.repo.resolver()
        texts: list[str] = []
        chips: list[dict[str, str]] = []
        for cid in s.cost_center_ids:
            center = self.repo.cost_centers.get(cid)
            if center is None:
                continue
            found = [(rec, share) for rec, share in views.shares(center, window)
                     if (rec.tx.amount > 0) == (direction == "in")]
            if s.supplier_ids:
                found = [(rec, share) for rec, share in found
                         if (m := resolver.resolve_transaction(rec.tx).supplier) is not None and m.id in s.supplier_ids]
            sup = f" with {self._suppliers(s.supplier_ids)}" if s.supplier_ids else ""
            if not found:
                texts.append(f"Nothing came in for {center.label}{sup} {period.phrase}." if direction == "in" else
                             f"Nothing went out for {center.label}{sup} {period.phrase}.")
                continue
            total = sum((share for _, share in found), Decimal(0))
            count = count_phrase(len(found), "payment")
            verb = f"You received {self._m(total)} for" if direction == "in" else f"You spent {self._m(total)} on"
            line = f"{verb} {center.label}{sup} {period.phrase}: {count}."
            if len(found) > 1:
                biggest, share = max(found, key=lambda x: (x[1], x[0].id))
                line += f" The biggest was {self.svc.orchestrator.merchant_name(biggest.tx)}, {self._m(share)}."
            if any(rec.tx.cost_allocation is not None and rec.tx.cost_allocation.is_split for rec, _ in found):
                line += " Shared costs count only their part."
            recharged = sum((rec.tx.cost_allocation.recharge_for(cid) for rec, _ in found
                             if rec.tx.cost_allocation is not None), Decimal(0))
            if direction == "out" and recharged:
                line += (" All of it is theirs to pay back." if recharged == total else
                         f" {self._m(recharged)} of it is theirs to pay back.")
            texts.append(line)
            chips += [self.svc._tx_evidence(rec) for rec, _ in sorted(found, key=lambda x: (-x[1], x[0].id))[:6]]
        waiting = [n for n in self.repo.open_needs() if n.kind == "cost_center" and any(
            c.company_id == n.company_id for c in (self.repo.cost_centers.get(i) for i in s.cost_center_ids) if c)]
        if waiting:
            texts.append(f"{count_phrase(len(waiting), 'payment').capitalize()} still "
                         f"{'waits' if len(waiting) == 1 else 'wait'} for you to say where "
                         f"{'it goes' if len(waiting) == 1 else 'they go'}.")
            chips += [{"label": "Needs you", "id": f"needs:{n.id}"} for n in waiting[:3]]
        return _Answer(" ".join(texts) or "I can't find that one.", [], chips)

    def _money_answer(self, u: Understanding, *, direction: str) -> _Answer:
        s = u.slots
        if s.cost_center_ids:
            return self._cost_center_money(u, direction=direction)
        over_time = (s.group_by == "month" or s.average or bool(s.supplier_ids and direction == "out")
                     or bool(re.search(r"\bsuppliers\b|\bvendors\b|\bwho did (?:we|i) pay\b|\bthe most\b", u.text)))
        if s.average and not s.group_by:
            s = replace(s, group_by="month")
            u = replace(u, slots=s)
        period = s.period or (self._since_records() if over_time else self._closing_month())
        category = s.category if direction == "out" else None
        m = self.ledger.money(period.start, period.end, direction=direction, company_ids=s.company_ids,
                              supplier_ids=s.supplier_ids if direction == "out" else (), category=category,
                              exclude=s.exclude, group_by=s.group_by, compare=self._compare_with(u, period))
        text = self._money_text(m, period, u) if direction == "out" else self._income_text(m, period, u)
        chips: list[dict[str, str]] = []
        if direction == "out" and s.supplier_ids:
            held, chips = self._held_for(s.supplier_ids)
            text = " ".join([text, *held])
        return _Answer(text, [self._money_card(m, period, s)] if m.covered else [], chips)

    def _money_text(self, m: Money, p: Period, u: Understanding) -> str:
        s = u.slots
        who = self._companies(s.company_ids)
        sup = self._suppliers(s.supplier_ids)
        cat = category_label(s.category).lower() if s.category else ""
        if m.covered is None:
            return f"I have no payment records {p.phrase}. {self._coverage_line()}"
        parts: list[str] = []
        if not m.lines and m.recharged:
            # Everything that went out was bought for clients: none of it is the business's own cost.
            parts.append(f"Your payments to {sup} {p.phrase} were all bought for clients." if sup else
                         f"{who or 'You'} had no costs of your own {p.phrase}.")
        elif not m.lines:
            if sup:
                parts.append(f"I found no payments to {sup}{' from ' + who if who else ''} {p.phrase}.")
            elif cat:
                parts.append(f"{who or 'You'} spent nothing on {cat} {p.phrase}.")
            else:
                parts.append(f"{who} spent nothing {p.phrase}." if who else f"Nothing went out {p.phrase}.")
        else:
            total = self._m(m.total)
            if sup:
                head = f"{who or 'You'} paid {sup} {total} {p.phrase}"
            elif s.category in _PAID_IN:
                head = f"{who or 'You'} paid {total} in {_PAID_IN[s.category]} {p.phrase}"
            elif cat:
                head = f"{who or 'You'} spent {total} on {cat} {p.phrase}"
            else:
                head = f"{who or 'You'} spent {total} {p.phrase}"
            rows = [f"{label} {self._m(amount)}" for label, amount, _ in m.rows]
            if m.count == 1:
                x = m.lines[0]
                on = "" if p.grain == "day" else f" on {self._day(x.on)}"
                parts.append(f"{head}{',' + on if on else ''}." if sup else f"{head}: {_payee(x.merchant)}{on}.")
            elif len(m.rows) == 1 and m.group_by == "supplier":
                parts.append(f"{head}, across {m.count} payments to {m.rows[0][0]}.")
            else:
                parts.append(f"{head}, across {m.count} payments.")
                if m.group_by == "company" or m.group_by == "category":
                    parts.append(join_and(rows[:6]) + ".")
                elif m.group_by == "month":
                    if len(rows) > 1 and len(rows) <= 6:
                        parts.append(f"By month: {join_and(rows)}.")
                elif s.top:
                    parts.append(f"Top {min(s.top, len(rows))}: {join_and(rows[:s.top])}.")
                elif re.search(r"\b(?:list|all|my|our)\b.*\b(?:suppliers|vendors)\b", u.text):
                    more = f" and {len(rows) - 8} more" if len(rows) > 8 else ""
                    parts.append(f"You paid {len(rows)} suppliers: {', '.join(rows[:8]) if more else join_and(rows)}"
                                 f"{more}.")
                elif len(rows) <= 3:
                    parts[-1] = parts[-1][:-1] + f": {join_and(rows)}."
                else:
                    parts.append(f"Biggest: {join_and(rows[:3])}.")
            named = [(k, w) for k, w in (("tax", "taxes"), ("bank_fee", "bank fees"), ("payroll", "salaries"),
                                          ("loan", "loan repayments"), ("platform_fee", "payment and platform fees"))
                     if m.by_kind.get(k)]
            if not sup and not s.category and named and m.by_kind.get("cost"):
                parts.append("That includes " + join_and([f"{self._m(m.by_kind[k])} in {w}" for k, w in named]) + ".")
            elif not sup and not s.category and len(named) == 1 and len(m.by_kind) == 1:
                parts.append(f"All of it is {named[0][1]}.")
            if s.exclude:
                parts.append(f"I left out {join_and([category_label(c).lower() for c in s.exclude])}, as you asked.")
        if m.left_out and not sup and not s.category:
            moved = sum((x.amount for x in m.left_out if x.kind == "transfer"), Decimal(0))
            cards = sum((x.amount for x in m.left_out if x.kind == "card_repayment"), Decimal(0))
            between = "your companies" if all(x.merchant in self.vocab.company_names.values() or
                                              x.merchant in self.repo.legal_names.values()
                                              for x in m.left_out if x.kind == "transfer") else "your own accounts"
            if moved:
                parts.append(f"I left out {self._m(moved)} moved between {between}.")
            if cards:
                parts.append(f"I left out {self._m(cards)} paying off cards: their purchases are counted one by one.")
        parts += self._recharge_lines(m)
        parts += self._pending_lines(m, s)
        if m.private_count:
            parts.append(f"I left out {count_phrase(m.private_count, 'payment')} you marked as not for your companies.")
        if m.other_currency:
            parts.append(f"I left out {count_phrase(m.other_currency, 'payment')} in other currencies.")
        if m.previous is not None and s.compare:
            parts.append(self._comparison(m))
        if s.average and m.lines:
            parts.append(self._average(m, p))
        parts += self._coverage_notes(m, p)
        return " ".join(x for x in parts if x)

    def _average(self, m: Money, p: Period) -> str:
        """The average over the whole months the records cover (the running month is left out)."""
        first, _ = self.ledger.coverage()
        start = max(p.start, first)
        month = (start.year, start.month) if start.day == 1 else (start.year + start.month // 12, start.month % 12 + 1)
        last = self.today.replace(day=1) - timedelta(days=1)
        last = min(last, p.end)
        months: list[tuple[int, int]] = []
        while (month[0], month[1]) <= (last.year, last.month):
            months.append(month)
            month = (month[0] + month[1] // 12, month[1] % 12 + 1)
        if len(months) < 2:
            return ""
        total = sum((x.amount for x in m.lines if (x.on.year, x.on.month) in months), Decimal(0))
        avg = (total / len(months)).quantize(Decimal("0.01"))
        name = lambda ym: month_period(ym[0], ym[1], self.today).label  # noqa: E731
        return f"On average {self._m(avg)} a month over {len(months)} whole months, {name(months[0])} to " \
               f"{name(months[-1])}."

    def _pending_lines(self, m: Money, s: Any) -> list[str]:
        out = []
        if m.pending:
            if len(m.pending) == 1:
                x = m.pending[0]
                out.append(f"Not counted yet: the {self._m(x.amount)} {x.merchant} payment on {self._day(x.on)}, "
                           "until you tell me which company it belongs to.")
            else:
                out.append(f"Not counted yet: {len(m.pending)} payments waiting for you to say which company they "
                           "belong to.")
        elif m.group_by == "company" and any(x.pending for x in m.lines):
            waiting = [x for x in m.lines if x.pending]
            if len(waiting) == 1:
                out.append(f"The {self._m(waiting[0].amount)} {waiting[0].merchant} payment is waiting for you to "
                           "say which company it belongs to.")
            else:
                out.append(f"{len(waiting)} payments are waiting for you to say which company they belong to.")
        return out

    def _comparison(self, m: Money) -> str:
        prev, label = m.previous, m.previous_label
        assert prev is not None
        if prev.covered is None:
            return f"I have no records for {label} to compare with."
        diff = m.total - prev.total
        verb = "came in" if m.direction == "in" else "went out"
        if not prev.lines:
            text = f"Nothing {verb} in {label}."
        elif diff == 0:
            text = f"That is the same as {label}."
        else:
            text = f"That is {self._m(abs(diff))} {'more' if diff > 0 else 'less'} than {label} ({self._m(prev.total)})."
        tracked = self.ledger.tracked_from()
        if tracked and ((prev.end < tracked) != (m.end < tracked)):
            older = prev if prev.end < tracked else m
            months = join_and(older.history_months) or "The earlier period"
            text += f" {months} only {'has' if len(older.history_months) <= 1 else 'have'} the bank history imported " \
                    "when you connected, so this is not like for like."
        return text

    def _coverage_notes(self, m: Money, p: Period) -> list[str]:
        out = []
        first, _ = self.ledger.coverage()
        if m.covered and p.start < first:
            out.append(f"My records start on {self._long(first)}.")
        if m.history_months:
            one = len(m.history_months) == 1
            out.append(f"{join_and(m.history_months)} {'comes' if one else 'come'} from the bank history imported when "
                       f"you connected, so I haven't checked {'its' if one else 'their'} invoices.")
        return out

    def _income_text(self, m: Money, p: Period, u: Understanding) -> str:
        who = self._companies(u.slots.company_ids)
        if m.covered is None:
            return f"I have no payment records {p.phrase}. {self._coverage_line()}"
        parts = []
        moved = [x for x in m.left_out if x.kind == "transfer"]
        if not m.lines and m.waiting_payouts:
            # Only payouts whose reports have not proven them: a net payout is never counted as sales.
            parts.append(f"I can't count any sales{' for ' + who if who else ''} {p.phrase} yet.")
        elif not m.lines:
            head = f"Nothing came in from customers{' for ' + who if who else ''} {p.phrase}, and no refunds."
            if len(moved) == 1:
                x = moved[0]
                target = self.repo.company_name(x.company_id) or "your account"
                head += f" The only money in was {self._m(x.amount)} from {x.merchant} to {target}, a transfer " \
                        "between your companies."
            elif moved:
                head += f" The only money in was {self._m(sum((x.amount for x in moved), Decimal(0)))} moved " \
                        "between your own accounts and companies."
            parts.append(head)
        elif all(x.kind == "sales" for x in m.lines) and self._sales_totals(m) is not None:
            providers = join_and(list(dict.fromkeys(x.provider for x in m.lines if x.provider)))
            parts.append(f"{self._m(m.total)} came in{' for ' + who if who else ''} {p.phrase}, all of it sales "
                         f"through {providers}.")
            parts += self._sales_lines(m, intro=False)
            if moved:
                parts.append(f"I left out {self._m(sum((x.amount for x in moved), Decimal(0)))} moved between your "
                             "own accounts and companies.")
        else:
            rows = [f"{label} {self._m(amount)}" for label, amount, _ in m.rows]
            head = f"{self._m(m.total)} came in{' for ' + who if who else ''} {p.phrase}"
            parts.append(f"{head}, across {m.count} payments: {join_and(rows[:3])}." if m.count > 1 else
                         f"{head}: {m.lines[0].merchant} on {self._day(m.lines[0].on)}.")
            parts += self._sales_lines(m)
            if moved:
                parts.append(f"I left out {self._m(sum((x.amount for x in moved), Decimal(0)))} moved between your "
                             "own accounts and companies.")
        parts += self._paid_back_lines(m)
        parts += self._waiting_payout_lines(m)
        if m.previous is not None and u.slots.compare:
            parts.append(self._comparison(m))
        parts += self._coverage_notes(m, p)
        return " ".join(parts)

    def _statements(self, u: Understanding) -> _Answer | None:
        """'Does our Vodafone statement match?': each supplier's latest account statement, checked (§20, §28)."""
        from backoffice.supplier_statements import summary

        repo = self.repo
        if re.search(r"\b(?:bank|card|credit card|owner'?s?)\s+statements?\b", u.text) or u.slots.cost_center_ids:
            return None  # a bank or card statement, or a property's owner statement, is not a supplier's
        wanted = set(u.slots.supplier_ids)
        found = [sr for sr in repo.statements.values()
                 if sr.document_id in repo.documents and (not wanted or sr.supplier_id in wanted)]
        if not found:
            if not wanted and not repo.statements and not re.search(r"\bsuppliers?\b|\bextratos?\b", u.text):
                return None
            who = self._suppliers(sorted(wanted))
            return _Answer(f"I don't have an account statement from {who}." if who else
                           "I don't have any supplier account statements yet.")
        latest: dict[str, Any] = {}
        for sr in sorted(found, key=lambda s: (s.statement.end or date.min, s.document_id)):
            latest[sr.supplier_id or sr.document_id] = sr
        texts: list[str] = []
        chips: list[dict[str, str]] = []
        for sr in latest.values():
            record = repo.documents[sr.document_id]
            if sr.check is None:
                texts.append(f"I couldn't tell which supplier the statement {record.label.lower()} is from, so I "
                             "haven't checked it.")
            else:
                text = summary(sr.check, self.today)
                request = self.svc.orchestrator.statements.request_line(sr)
                if sr.check.missing and request:
                    text += f" {request}"
                texts.append(text)
            chips.append({"label": f"{display_name(record.document.supplier_name)} · account statement",
                          "id": record.evidence_ids[0]})
            if sr.check is not None:
                for row in (*sr.check.differences, *sr.check.matched):
                    if row.document_id and row.document_id in repo.documents and len(chips) < 8:
                        chips.append(self.svc._doc_evidence(repo.documents[row.document_id]))
            needs = repo.needs.get(sr.needs_id or "")
            if needs is not None and needs.status == "open":
                chips.append({"label": "Needs you", "id": f"needs:{needs.id}"})
        return _Answer(" ".join(texts), [], chips)

    def _client_label(self, cost_center_id: str) -> str:
        center = self.repo.cost_centers.get(cost_center_id)
        return center.label if center is not None else "a client"

    def _recharge_lines(self, m: Money) -> list[str]:
        """'I left out €2,400.00 bought for Client Lume, to recharge to them; €1,800.00 of it is already paid back.'"""
        if not m.recharged:
            return []
        by_client: dict[str, list[Decimal]] = {}
        for x in m.recharged:
            sums = by_client.setdefault(x.client, [Decimal(0), Decimal(0)])
            sums[0] += x.amount
            sums[1] += x.recovered
        out = []
        book = self.ledger.recharges
        for cid, (bought, back) in sorted(by_client.items(), key=lambda kv: (-kv[1][0], kv[0]))[:3]:
            client = book.for_center(cid) if book is not None else None
            if client is not None and client.client_money:
                line = (f"I left out {self._m(bought)} paid for {self._client_label(cid)} out of their own money: it "
                        "is not your cost.")
                if back < bought:
                    line += f" {self._m(bought - back)} of it is still to come from them."
                out.append(line)
                continue
            line = f"I left out {self._m(bought)} bought for {self._client_label(cid)}, to recharge to them"
            if back >= bought:
                line += "; all of it is already paid back"
            elif back:
                line += f"; {self._m(back)} of it is already paid back"
            out.append(line + ".")
        if len(by_client) > 3:
            rest = sum((v[0] for k, v in by_client.items()), Decimal(0)) - sum(
                (v[0] for _, v in sorted(by_client.items(), key=lambda kv: (-kv[1][0], kv[0]))[:3]), Decimal(0))
            out.append(f"And {self._m(rest)} bought for {len(by_client) - 3} more clients, to recharge to them.")
        return out

    def _paid_back_lines(self, m: Money) -> list[str]:
        """Money from clients that is not revenue: costs they paid back, and client money held for them."""
        out = []
        by_client: dict[tuple[str, str], Decimal] = {}
        for x in m.paid_back:
            by_client[(x.client, x.kind)] = by_client.get((x.client, x.kind), Decimal(0)) + x.amount
        book = self.ledger.recharges
        for (cid, kind), amount in sorted(by_client.items()):
            who = self._client_label(cid)
            if kind == "client_money":
                line = f"I left out {self._m(amount)} of client money from {who}: it is theirs, not revenue."
                client = book.for_center(cid) if book is not None else None
                if client is not None and client.held > 0:
                    line += f" {self._m(client.held)} of their money is still held for them."
                out.append(line)
            else:
                out.append(f"I left out {self._m(amount)} that {who} paid back for costs you bought for them.")
        return out

    def _sales_totals(self, m: Money) -> dict[str, Decimal] | None:
        """Gross sales proven by payout reports, and what the providers kept from them."""
        sales = [x for x in m.sales if x.currency == "EUR"]
        if not sales:
            return None
        total = lambda attr: sum((getattr(x, attr) for x in sales), Decimal(0))  # noqa: E731
        return {"sales": total("amount"), "fees": total("fees"), "refunds": total("refunds"),
                "chargebacks": total("chargebacks"), "adjustments": total("adjustments"), "paid_out": total("paid_out")}

    def _sales_lines(self, m: Money, *, intro: bool = True) -> list[str]:
        """'That includes €1,000.00 in sales through Stripe. From that, €29.00 went in fees ...' (§20, §36)."""
        t = self._sales_totals(m)
        if t is None:
            return []
        providers = join_and(list(dict.fromkeys(x.provider for x in m.sales if x.provider)))
        kept = []
        words = sorted({x.fee_word for x in m.sales if x.fee_word}) or ["fees"]
        fee_word = " and ".join(words)
        if t["fees"]:
            kept.append(f"{self._m(t['fees'])} went in {fee_word}" if t["fees"] > 0 else
                        f"{self._m(-t['fees'])} in {fee_word} came back")
        if t["refunds"]:
            kept.append(f"{self._m(t['refunds'])} was refunded to customers")
        if t["chargebacks"]:
            kept.append(f"{self._m(t['chargebacks'])} was taken back in disputed card payments")
        if t["adjustments"] < 0:
            kept.append(f"{self._m(-t['adjustments'])} went in other deductions")
        elif t["adjustments"] > 0:
            kept.append(f"{self._m(t['adjustments'])} was added in other adjustments")
        lines = [f"That includes {self._m(t['sales'])} in sales through {providers}."] if intro else []
        if kept:
            lines.append(f"From that, {join_and(kept)}, so {self._m(t['paid_out'])} reached your bank.")
        else:
            lines.append("All of it reached your bank.")
        return lines

    def _waiting_payout_lines(self, m: Money) -> list[str]:
        """'Not counted yet: the €951.00 payout from Stripe on 18 September, report not received yet.'"""
        waiting = m.waiting_payouts
        if not waiting:
            return []
        if len(waiting) == 1:
            x = waiting[0]
            return [f"Not counted yet: the {self._m(x.amount)} payout from {x.provider} on {self._day(x.on)}, "
                    f"{_PAYOUT_WAIT[x.status]}."]
        total = self._m(sum((x.amount for x in waiting), Decimal(0)))
        providers = join_and(list(dict.fromkeys(x.provider for x in waiting)))
        why = "reports not received yet" if all(x.status == "report_missing" for x in waiting) else \
            "until their payout reports arrive and add up"
        return [f"Not counted yet: {len(waiting)} payouts from {providers} ({total} in all), {why}."]

    def _money_card(self, m: Money, p: Period, s: Slots) -> dict[str, Any]:
        scope = [self._companies(s.company_ids)] if s.company_ids else []
        if s.supplier_ids:
            scope.append(self._suppliers(s.supplier_ids))
        if s.category:
            scope.append(category_label(s.category))
        title = " · ".join([*(scope or ["Money in" if m.direction == "in" else "Spending"]), p.label])
        notes = []
        if m.by_kind.get("tax") and s.category != "tax":
            notes.append(f"Includes {self._m(m.by_kind['tax'])} in taxes.")
        if m.by_kind.get("bank_fee") and s.category != "bank_fees":
            notes.append(f"Includes {self._m(m.by_kind['bank_fee'])} in bank fees.")
        if m.by_kind.get("platform_fee") and s.category != "platform_fees" and m.direction == "out":
            notes.append(f"Includes {self._m(m.by_kind['platform_fee'])} in payment and platform fees.")
        if m.direction == "in":
            notes += self._sales_lines(m)
            notes += self._waiting_payout_lines(m)
        moved = sum((x.amount for x in m.left_out), Decimal(0))
        if moved:
            notes.append(f"Left out {self._m(moved)} moved between your own accounts and companies.")
        notes += self._pending_lines(m, s)
        coverage = " ".join(self._coverage_notes(m, p)) if m.covered else self._coverage_line()
        previous = None
        if m.previous is not None and m.previous.covered is not None:
            previous = {"label": m.previous_label, "total": float(m.previous.total),
                        "change": float(m.total - m.previous.total)}
        ledger = self.ledger
        payments = [{**ledger.payment_dict(x), "evidence": ledger.payment_label(x)}
                    for x in sorted(m.lines, key=lambda x: (x.on, x.id), reverse=True)]
        return {
            "type": "spending", "direction": m.direction, "title": title, "periodLabel": p.label,
            "from": p.start.isoformat(), "to": min(p.end, self.today).isoformat(),
            "totalLabel": "Came in" if m.direction == "in" else "Spent", "unit": "payment",
            "total": float(m.total), "count": m.count, "groupBy": m.group_by,
            "rows": [{"label": label, "amount": float(amount), "count": n} for label, amount, n in m.rows[:12]],
            "previous": previous, "notes": notes, "coverageNote": coverage,
            "payments": payments[:12], "more": max(0, len(payments) - 12),
        }

    def payment_facts(self, x: Line) -> dict[str, Any]:
        """One payment for the model: plain facts, with the evidence id to cite."""
        facts = {"date": x.on.isoformat(), "merchant": x.merchant, "amount_eur": float(x.amount),
                 "direction": x.direction, "company": self.repo.company_name(x.company_id),
                 "company_not_decided_yet": x.pending, "kind": x.kind, "category": x.category_label,
                 "invoice": self.ledger.payment_dict(x)["invoice"], "evidence_id": x.evidence_id,
                 "from_imported_bank_history": x.history}
        if x.kind == "payout":
            facts["payout_report"] = {"settled": "matched", "report_disagrees": "does_not_match_the_bank",
                                      "report_does_not_add_up": "does_not_add_up"}.get(x.status, "not_received_yet")
            facts["note"] = "A net payout from a card terminal or sales platform, not customer revenue by itself."
        if x.kind == "sales":
            facts.update({"fees_eur": float(x.fees), "refunds_eur": float(x.refunds),
                          "disputed_payments_eur": float(x.chargebacks), "adjustments_eur": float(x.adjustments),
                          "paid_out_to_bank_eur": float(x.paid_out)})
        return facts

    def money_facts(self, m: Money, p: Period) -> dict[str, Any]:
        """A spending or money-in result for the model (spending_summary)."""
        first, last = self.ledger.coverage()
        kinds = {"cost": "supplier_costs", "tax": "taxes", "bank_fee": "bank_fees", "payroll": "salaries",
                 "loan": "loan_repayments", "income": "customer_payments", "refund": "refunds",
                 "interest": "bank_interest", "tax_refund": "tax_refunds",
                 "sales": "sales_through_card_terminals_and_platforms", "platform_fee": "payment_and_platform_fees"}
        out: dict[str, Any] = {
            "period": {"from": p.start.isoformat(), "to": p.end.isoformat(), "label": p.label},
            "records_cover": {"from": first.isoformat(), "to": last.isoformat()},
            "records_cover_period": m.covered is not None,
            "direction": m.direction, "total_eur": float(m.total), "payments": m.count, "group_by": m.group_by,
            "breakdown": [{"label": label, "amount_eur": float(a), "payments": n} for label, a, n in m.rows],
            "included": {kinds.get(k, k): float(v) for k, v in m.by_kind.items()},
            "left_out": {
                "transfers_between_own_accounts_and_companies_eur":
                    float(sum((x.amount for x in m.left_out if x.kind == "transfer"), Decimal(0))),
                "card_repayments_eur": float(sum((x.amount for x in m.left_out if x.kind == "card_repayment"),
                                                 Decimal(0))),
                "payments_marked_not_for_the_companies": m.private_count,
                "payments_in_other_currencies": m.other_currency},
            "waiting_for_owner_to_choose_company": [self.payment_facts(x) for x in m.pending],
            "notes": self._coverage_notes(m, p) if m.covered else [self._coverage_line()],
            "payment_list": [self.payment_facts(x) for x in sorted(m.lines, key=lambda x: (x.on, x.id),
                                                                   reverse=True)[:40]],
        }
        totals = self._sales_totals(m)
        if totals is not None:
            out["sales_from_payout_reports"] = {
                "gross_sales_eur": float(totals["sales"]), "fees_and_commission_eur": float(totals["fees"]),
                "refunded_to_customers_eur": float(totals["refunds"]),
                "disputed_payments_eur": float(totals["chargebacks"]),
                "other_adjustments_eur": float(totals["adjustments"]),
                "paid_out_to_bank_eur": float(totals["paid_out"]),
                "note": "Gross sales count as money in; the fees are costs; the net payout itself is not counted."}
        if m.waiting_payouts:
            out["payouts_not_counted_yet"] = [{**self.payment_facts(x), "provider": x.provider,
                                               "why": _PAYOUT_WAIT_FACT[x.status]} for x in m.waiting_payouts]
        if m.previous is not None:
            out["previous_period"] = {"label": m.previous_label, "records_cover_period": m.previous.covered is not None,
                                      "total_eur": float(m.previous.total),
                                      "difference_eur": float(m.total - m.previous.total)}
        return out

    # -- intents: money -------------------------------------------------------

    def _i_spending(self, u: Understanding) -> _Answer:
        return self._money_answer(u, direction="out")

    def _i_income(self, u: Understanding) -> _Answer:
        return self._money_answer(u, direction="in")

    def _i_vat(self, u: Understanding) -> _Answer:
        return self.vat_answer(u.slots.period or self._closing_month(), u.slots.company_ids)

    def vat_answer(self, p: Period, company_ids: list[str]) -> _Answer:
        first, _ = self.ledger.coverage()
        if p.end < first or p.start > self.today:
            return _Answer(f"I have no records {p.phrase}. {self._coverage_line()}")
        v = self.ledger.vat(p.start, p.end, company_ids)
        with_vat = [d for d in v.purchases if d["vat"]]
        who = self._companies(company_ids)
        parts = []
        if with_vat:
            biggest = sorted(with_vat, key=lambda d: -d["vat"])[:3]
            listed = [f"{d['supplier']} {self._m(d['vat'])}" for d in biggest]
            if len(with_vat) > 3:
                listed.append(f"{len(with_vat) - 3} more")
            parts.append(f"VAT on {who + '’s' if who else 'your'} purchase invoices and receipts {p.phrase}: "
                         f"{self._m(v.purchase_vat)} across {count_phrase(len(with_vat), 'document')} "
                         f"({join_and(listed)}).")
        else:
            parts.append(f"I found no purchase invoices with VAT {p.phrase}.")
        for x in v.paid_to_state:
            ref = re.search(r"(\d{4})/(\d{2})", x.description)
            of = ""
            if ref and 1 <= int(ref[2]) <= 12:
                of = f" (for {MONTH_NAMES[int(ref[2]) - 1]}{'' if int(ref[1]) == self.today.year else ' ' + ref[1]})"
            parts.append(f"You paid {self._m(x.amount)} VAT to the tax office on {self._day(x.on)}{of}.")
        if v.sales:
            parts.append(f"VAT on your sales invoices: {self._m(v.sales_vat)}.")
        else:
            parts.append("I have no sales invoices, so I can't tell the VAT you charged.")
        if v.pending:
            parts.append("Not counted: " + join_and([f"the {d['supplier']} receipt of {self._day(d['date'])}"
                                                      for d in v.pending]) + ", until you tell me which company.")
        rows: dict[str, list[Any]] = {}
        for d in with_vat:
            row = rows.setdefault(d["supplier"], [Decimal(0), 0])
            row[0] += d["vat"]
            row[1] += 1
        card = {
            "type": "spending", "direction": "out", "title": " · ".join([*([who] if who else []), "VAT", p.label]),
            "periodLabel": p.label, "from": p.start.isoformat(), "to": min(p.end, self.today).isoformat(),
            "totalLabel": "VAT on purchases", "unit": "document", "total": float(v.purchase_vat),
            "count": len(with_vat), "groupBy": "supplier",
            "rows": [{"label": k, "amount": float(a), "count": n} for k, (a, n) in
                     sorted(rows.items(), key=lambda kv: -kv[1][0])],
            "previous": None, "notes": parts[1:], "coverageNote": "",
            "payments": [{"id": d["evidenceId"] or "", "date": d["date"].isoformat(),
                          "label": f"{d['supplier']} {d['number']}".strip(),
                          "company": self.repo.company_name(d["company"]) or "Not decided yet",
                          "amount": float(d["vat"]), "invoice": "on hold" if d["onHold"] else "matched",
                          "category": "VAT", "evidence": f"{d['supplier']} {d['number']}".strip()}
                         for d in with_vat][:12],
            "more": max(0, len(with_vat) - 12),
        }
        chips = [{"label": self.ledger.payment_label(x), "id": x.evidence_id} for x in v.paid_to_state if x.evidence_id]
        return _Answer(" ".join(parts), [card], chips)

    def _i_payment_lookup(self, u: Understanding) -> _Answer:
        s = u.slots
        p = s.period
        if s.amount is not None:
            lines = self.ledger.select(start=p.start if p else None, end=p.end if p else None, amount=s.amount)
            if lines:
                x = lines[-1]
                if not x.history:
                    found = self.svc._amount_answer(s.amount)
                    if found is not None and any(e["id"] == x.evidence_id for e in found["evidence"]):
                        return _Answer(found["answer"], evidence=found["evidence"])
                where = "came from" if x.direction == "in" else "went to"
                text = f"The {self._m(x.amount)} payment on {self._day(x.on)} {where} {x.merchant}."
                if x.paid_in_cash:
                    text += " It was paid in cash, as the receipt shows."
                if x.history:
                    text += " It comes from the bank history imported when you connected, so I haven't checked its invoice."
                return _Answer(text, evidence=[{"label": self.ledger.payment_label(x), "id": x.evidence_id}]
                               if x.evidence_id else [])
            return _Answer(f"I can't find a payment of {self._m(s.amount)}{' ' + p.phrase if p else ''}. "
                           f"{self._coverage_line()}")
        if s.supplier_ids:
            sid = s.supplier_ids[0]
            name = self.repo.suppliers[sid].name
            if p is None and re.search(r"\bwhen\b|\blast\b|\blatest\b|\bmost recent\b|\bhappen\w*\b|"
                                       r"\bgoing on\b|\bstatus\b|\bnews\b|\bupdate\b", u.text):
                return self._last_payment(sid)
            if p is None:
                found = self.svc._supplier_answer(self.repo.suppliers[sid])
                return _Answer(found["answer"], evidence=found["evidence"])
            lines = self.ledger.select(start=p.start, end=p.end, direction="out", supplier_ids=[sid])
            if not lines:
                held, chips = self._held_for([sid])
                return _Answer(" ".join([f"No, I found no payment to {name} {p.phrase}.", *held]), evidence=chips)
            paid = join_and([f"{self._m(x.amount)} on {self._day(x.on)}" for x in lines])
            return _Answer(f"Yes: {paid}.", evidence=[{"label": self.ledger.payment_label(x), "id": x.evidence_id}
                                                      for x in lines if x.evidence_id])
        if s.category:
            lines = self.ledger.select(start=p.start if p else None, end=p.end if p else None, direction="out",
                                       category=s.category, company_ids=s.company_ids)
            label = category_label(s.category).lower()
            parts = []
            if lines:
                shown = lines[-3:] if p is not None else lines[-1:]
                parts.append("Yes: " + join_and([
                    f"{self._m(x.amount)} to {_payee(x.merchant)} on {self._day(x.on)}"
                    + (f" ({self.repo.company_name(x.company_id)})" if x.company_id else "") for x in shown]) + ".")
            else:
                parts.append(f"I found no {label} payment{' ' + p.phrase if p else ''}.")
            if s.category == "tax":
                for ob in self.repo.obligations.values():
                    if not ob.satisfied_by and (not s.company_ids or ob.obligation.entity_id in s.company_ids):
                        amount = f"{self._m(ob.obligation.amount)} " if ob.obligation.amount is not None else ""
                        parts.append(f"{self.repo.company_name(ob.obligation.entity_id)}’s {amount}is due on "
                                     f"{self._day(ob.obligation.due_on)} and not paid yet.")
            return _Answer(" ".join(parts), evidence=[{"label": self.ledger.payment_label(x), "id": x.evidence_id}
                                                      for x in lines[-3:] if x.evidence_id])
        if p is not None:
            return self._money_answer(u, direction=u.slots.direction or "out")
        return _Answer("Which payment? Tell me the supplier, the amount or the day.")

    def _last_payment(self, supplier_id: str) -> _Answer:
        """'When did we last pay Adobe?': the latest payment, its invoice, and any hold."""
        name = self.repo.suppliers[supplier_id].name
        lines = self.ledger.select(direction="out", supplier_ids=[supplier_id])
        if not lines:
            return _Answer(f"I can't see a payment to {name}. {self._coverage_line()}")
        x = lines[-1]
        rec = self.repo.transactions.get(x.id)
        doc = self.repo.documents.get(rec.document_ids[0]) if rec is not None and rec.document_ids else None
        matched = ""
        if doc is not None:
            number = f" {doc.document.invoice_number}" if doc.document.invoice_number else ""
            matched = f", matched to its invoice{number}"
        elif x.history:
            matched = ", from the bank history imported when you connected"
        plan = ""
        if rec is not None and doc is None and x.needs_document and not x.has_document:
            plan = self.svc.orchestrator.missing.plan(rec)
        held, chips = self._held_for([supplier_id])
        evidence = [{"label": self.ledger.payment_label(x), "id": x.evidence_id}] if x.evidence_id else []
        return _Answer(" ".join([f"The last payment to {name} was {self._m(x.amount)} on {self._day(x.on)}{matched}.",
                                 *([plan] if plan else []), *held]), evidence=evidence + chips)

    def _i_missing_invoices(self, u: Understanding) -> _Answer:
        s = u.slots
        p = s.period
        items = self.ledger.missing(start=p.start if p else None, end=p.end if p else None, company_ids=s.company_ids)
        payouts = self.ledger.missing_reports(start=p.start if p else None, end=p.end if p else None,
                                              company_ids=s.company_ids)
        when = f" {p.phrase}" if p else ""
        reports = [plan for _, plan in payouts[:3]]
        if len(payouts) > 1:
            reports.insert(0, f"{len(payouts)} payouts are waiting for payout reports that add up.")
        report_chips = [{"label": self.ledger.payment_label(x), "id": x.evidence_id} for x, _ in payouts
                        if x.evidence_id]
        if not items:
            return _Answer(" ".join([f"Every payment that needs an invoice has one{when}.", *reports]),
                           evidence=report_chips)
        head = "One payment still has no invoice" if len(items) == 1 else \
            f"{count_phrase(len(items), 'payment').capitalize()} still have no invoice"
        plans = [plan for _, plan in items[:4]]
        more = f" And {len(items) - 4} more." if len(items) > 4 else ""
        chips = [{"label": self.ledger.payment_label(x), "id": x.evidence_id} for x, _ in items if x.evidence_id]
        months = {(x.company_id, f"{x.on:%Y-%m}") for x, _ in items if x.company_id}
        chips += [{"label": f"{self.repo.company_name(c)} · {MONTH_NAMES[int(m[5:]) - 1]}", "id": f"month:{c}:{m}"}
                  for c, m in sorted(months)]
        return _Answer(" ".join([f"{head}{when}. " + " ".join(plans) + more, *reports]), evidence=chips + report_chips)

    # -- intents: documents and reports --------------------------------------

    def _recipients(self, s: Any) -> list[str]:
        emails = list(s.emails)
        if s.to_accountant and self.repo.accountant and self.repo.accountant.email not in emails:
            emails.append(self.repo.accountant.email)
        return emails

    def _i_find_document(self, u: Understanding) -> _Answer:
        op, s, p = self.op, u.slots, u.slots.period
        sup = self.repo.suppliers[s.supplier_ids[0]].name if s.supplier_ids else ""
        docs = op.documents(supplier=sup, amount=s.amount, query=s.doc_number, company_id=(s.company_ids or [""])[0],
                            date_from=p.start if p else None, date_to=p.end if p else None)
        emails = self._recipients(s)
        if not docs:
            if s.amount is not None:
                lines = [x for x in self.ledger.select(start=p.start if p else None, end=p.end if p else None,
                                                       amount=s.amount) if x.direction == "out"]
                if lines:
                    x = lines[-1]
                    rec = self.repo.transactions.get(x.id)
                    plan = self.svc.orchestrator.missing.plan(rec) if rec is not None and not x.has_document else ""
                    return _Answer(f"The {self._m(x.amount)} payment to {x.merchant} on {self._day(x.on)} has no "
                                   f"invoice yet. {plan}".strip(),
                                   evidence=[{"label": self.ledger.payment_label(x), "id": x.evidence_id}]
                                   if x.evidence_id else [])
                return _Answer(f"I can't find an invoice or a payment of {self._m(s.amount)}"
                               f"{' ' + p.phrase if p else ''}.")
            if sup:
                waiting = [(x, plan) for x, plan in self.ledger.missing(start=p.start if p else None,
                                                                         end=p.end if p else None)
                           if x.supplier_id == s.supplier_ids[0]]
                if waiting:
                    x, plan = waiting[-1]
                    return _Answer(f"I don't have the {sup} invoice yet. {plan}",
                                   evidence=[{"label": self.ledger.payment_label(x), "id": x.evidence_id}]
                                   if x.evidence_id else [])
                return _Answer(f"I couldn't find a document from {sup}{' ' + p.phrase if p else ''}.")
            return _Answer(f"I couldn't find a document{' ' + p.phrase if p else ''}. Tell me the supplier, "
                           "the amount or the month.")
        pick = docs[0]
        amount = self._m(Decimal(str(pick["amount"] or 0)))
        issued = self._day(date.fromisoformat(pick["date"]))
        listing = not (sup or s.amount is not None or s.doc_number) and len(docs) > 1
        if listing:
            text = (f"I found {len(docs)} documents{' ' + p.phrase if p else ''}. The latest: {pick['supplier']} "
                    f"{pick['number']} · {amount} · {issued}.")
        else:
            text = (f"Found {pick['supplier']} {pick['number']} · {amount} · {issued}." +
                    (f" {len(docs) - 1} more match." if len(docs) > 1 else ""))
        cards: list[dict[str, Any]] = [{"type": "documents", "items": docs[:5]}]
        if emails and _SEND.search(u.text):
            msg = op.draft_email(emails, f"Invoice {pick['number'] or pick['supplier']}",
                                 f"Hello,\n\nAttached is {pick['supplier']} invoice {pick['number']} "
                                 f"({amount}, {pick['date']}).\n\nBest regards",
                                 [{"kind": "document", "id": pick["id"], "name": pick["filename"]}])
            cards.append(_draft_card(msg))
            text += " The email is ready. Tap Send to deliver it."
        return _Answer(text, cards)

    def _i_report(self, u: Understanding) -> _Answer:
        op, s = self.op, u.slots
        p = s.period or self._closing_month()
        company = s.company_ids[0] if len(s.company_ids) == 1 else ""
        r = op.period_report(p.start, min(p.end, self.today) if p.start <= self.today else p.end, company)
        text = (f"Report ready for {r['title']}: {_money(Decimal(str(r['spent'])))} spent across "
                f"{r['payments']} payments, {r['documents']} documents, "
                f"{r['missingInvoices']} payment{'s' if r['missingInvoices'] != 1 else ''} still without an invoice.")
        cards: list[dict[str, Any]] = [{"type": "report", **{k: v for k, v in r.items() if k != "csv"}}]
        emails = self._recipients(s)
        if emails:
            msg = op.draft_email(emails, f"Report {r['title']}",
                                 f"Hello,\n\nAttached is the report for {r['title']}.\n\nBest regards",
                                 [{"kind": "report", "id": r["id"], "name": r["filename"]}])
            cards.append(_draft_card(msg))
            text += " The email is ready. Tap Send to deliver it."
        return _Answer(text, cards)

    def _i_supplier_summary(self, u: Understanding) -> _Answer:
        op, s, p = self.op, u.slots, u.slots.period
        name = self.repo.suppliers[s.supplier_ids[0]].name
        summary = op.supplier_summary(name, date_from=p.start if p else None,
                                      date_to=min(p.end, self.today) if p else None)
        text = (f"{summary['supplier']}: {_money(Decimal(str(summary['spent'])))} across {summary['payments']} payments "
                f"and {summary['documents']} document{'s' if summary['documents'] != 1 else ''}.")
        text += f" {summary['coverageNote']}" if summary["coverageNote"] else ""
        text += (" I found no issues." if not summary["issues"] else
                 f" I found {len(summary['issues'])} thing{'s' if len(summary['issues']) != 1 else ''} to look at.")
        cards: list[dict[str, Any]] = [{"type": "summary", **summary}]
        emails = self._recipients(s)
        if emails and _SEND.search(u.text):
            msg = op.draft_email(emails, f"{summary['supplier']} spending summary",
                                 text + "\n\n" + "\n".join(f"- {i}" for i in summary["issues"]))
            cards.append(_draft_card(msg))
        return _Answer(text, cards)

    # -- intents: status --------------------------------------------------------

    def _i_month_status(self, u: Understanding) -> _Answer:
        s = u.slots
        current = self.svc._current_month()
        p = s.period
        if p is None:
            month = current
        else:
            anchor = min(p.end, self.today)
            month = Month.of(anchor) if p.grain in ("month", "day", "week") else Month.of(min(anchor, current.last_day))
        companies = s.company_ids or list(self.repo.companies)
        chips = [{"label": f"{self.repo.company_name(c)} · {month.name}", "id": f"month:{c}:{month}"}
                 for c in companies]
        today_month = Month.of(self.today)
        if (month.year, month.month) > (today_month.year, today_month.month):
            return _Answer(f"{month.label(self.today)} hasn't started yet.")
        tracked = [m for c in self.repo.companies for m in self.repo.months_for(c)]
        first = min(tracked, key=lambda m: (m.year, m.month)) if tracked else None
        statuses = {c: self.svc._status(c, month) for c in companies}
        if month == today_month:
            parts = [f"{month.name} isn't over yet, so it can't close."]
            for c, st in statuses.items():
                if st.needs_you:
                    n = st.needs_you
                    parts.append(f"{self.repo.company_name(c)} already needs {'one answer' if n == 1 else f'{n} answers'}"
                                 " from you.")
            return _Answer(" ".join(parts), evidence=chips)
        records_from, _ = self.ledger.coverage()
        if month.last_day < records_from:
            return _Answer(f"I have no records for {month.label(self.today)}. {self._coverage_line()}")
        if first is not None and (month.year, month.month) < (first.year, first.month):
            return _Answer(f"I started closing months with {first.label(self.today)}. {month.label(self.today)} comes "
                           "from the bank history I imported when you connected, so there is nothing to close.")
        parts: list[str] = []
        evidence: list[dict[str, str]] = []
        all_closed = all(st.closed for st in statuses.values())
        if len(companies) == 1:
            c = companies[0]
            st = statuses[c]
            name = self.repo.company_name(c)
            closed_on = self.repo.closed_months.get((c, str(month)))
            if st.closed:
                parts.append(f"Yes. {name}’s {month.name} is closed" + (f", since {self._day(closed_on)}." if closed_on
                                                                         else "."))
            elif st.needs_you:
                parts.append(f"Not yet. {name}’s {month.name} is {st.percent_closed}% closed: I need "
                             f"{'one answer' if st.needs_you == 1 else f'{st.needs_you} answers'} from you.")
            else:
                reason = st.reasons()[0] if st.reasons() else ""
                parts.append(f"Not yet. {name}’s {month.name} is {st.percent_closed}% closed. {reason}".strip())
        else:
            parts.append(f"Yes. {month.name} is closed for every company." if all_closed else
                         ("Almost." if any(st.closed for st in statuses.values()) or
                          min(st.percent_closed for st in statuses.values()) >= 60 else "Not yet."))
            for c, st in statuses.items():
                if all_closed:
                    continue
                name = self.repo.company_name(c)
                if st.closed:
                    parts.append(f"{name} is closed.")
                elif st.needs_you:
                    parts.append(f"{name} needs {'one answer' if st.needs_you == 1 else f'{st.needs_you} answers'} "
                                 "from you.")
                else:
                    reason = st.reasons()[0] if st.reasons() else ""
                    parts.append(f"{name} is {st.percent_closed}% done. {reason}".strip())
        for c in companies:
            evidence.append({"label": f"{self.repo.company_name(c)} · {month.name} "
                                      f"{'closed' if statuses[c].closed else f'{statuses[c].percent_closed}%'}",
                             "id": f"month:{c}:{month}"})
            evidence += [{"label": f"{self.repo.company_name(c)} · question", "id": f"needs:{n.id}"}
                         for n in self.repo.open_needs() if n.company_id == c and n.kind == "choice"
                         and self.repo.item_month(self.repo.items[n.item_id]) == month]
        return _Answer(" ".join(parts), evidence=evidence)

    def _i_needs_me(self, u: Understanding) -> _Answer:
        found = self.svc._attention_answer()
        text = found["answer"]
        tasks = self.op.list_tasks()
        cards = []
        if tasks:
            text += f" You also have {count_phrase(len(tasks), 'open task')}."
            cards.append(_tasks_card(tasks))
        return _Answer(text, cards, found["evidence"])

    def _i_deadlines(self, u: Understanding) -> _Answer:
        s = u.slots
        if re.search(r"\b(?:renew\w*|insurance|seguros?|policy|policies)\b", u.text):
            return self._renewals(u)
        items = self.svc._due_soon()
        names = {self.repo.company_name(c) for c in s.company_ids}
        if names:
            items = [i for i in items if i["companyName"] in names]
        taxes = s.category == "tax" or re.search(r"\b(?:tax|taxes|vat|iva|impostos?)\b", u.text)
        if taxes:
            items = [i for i in items if i["id"].startswith("due_obl_")]
        parts = []
        if not items:
            parts.append(f"No {'tax ' if taxes else ''}payments are due in the next three weeks.")
        else:
            parts.append("One thing is due soon." if len(items) == 1 else
                         f"{_count_word(len(items)).capitalize()} things are due soon.")
            for i in items:
                note = i["note"][:1].lower() + i["note"][1:]
                parts.append(f"{i['title']} for {i['companyName']}, {self._day(date.fromisoformat(i['due']))}: {note}")
        evidence = []
        for i in items:
            if i["id"].startswith("due_nd_"):
                evidence.append({"label": f"{i['title']} · on hold", "id": f"needs:{i['id'][4:]}"})
            elif (ob := self.repo.obligations.get(i["id"][4:])) is not None:
                evidence.append({"label": f"{i['title']} · {i['companyName']} letter", "id": ob.evidence_id})
        return _Answer(" ".join(parts), evidence=evidence)

    def _renewals(self, u: Understanding) -> _Answer:
        coming = sorted((r for r in self.repo.relationships if r.renews_on and r.renews_on >= self.today
                         and (not u.slots.company_ids or r.company_id in u.slots.company_ids)),
                        key=lambda r: (r.renews_on, r.name))
        named = [r for r in coming if _clean_name(r.name) and re.search(rf"\b{re.escape(_clean_name(r.name))}\b",
                                                                          u.text)]
        if not coming:
            return _Answer("I don't know of any renewals coming up.")
        return _Answer(" ".join(f"{r.name} ({self.repo.company_name(r.company_id)}) renews on {self._day(r.renews_on)}: "
                                f"{r.detail[:1].lower()}{r.detail[1:]}." for r in (named or coming)))

    def _i_subscriptions(self, u: Understanding) -> _Answer:
        prices = self.svc._price_answer()
        if _INCREASE.search(u.text):
            return _Answer(prices["answer"], evidence=prices["evidence"])
        costs = {x.id for x in self.ledger.lines if x.direction == "out" and x.kind == "cost" and not x.private}
        txs = [t for t in [*self.repo.history_transactions, *(r.tx for r in self.repo.transactions.values())]
               if t.id in costs]
        series = [r for r in learn_from_transactions(txs) if r.trusted]
        if not series:
            return _Answer("I haven't seen any regular costs yet.", evidence=prices["evidence"])
        names = {counterparty_key(a): sup.name for sup in self.repo.suppliers.values() for a in [sup.name, *sup.aliases]}
        listed = [f"{names.get(r.key, display_name(r.display_name))} {self._m(r.typical_amount or Decimal(0))} "
                  f"{_CADENCE.get(r.cadence.value, '')}".strip() for r in sorted(series, key=lambda r: r.key)]
        text = f"You have {count_phrase(len(series), 'regular cost')}: {join_and(listed)}."
        changes = self.svc.orchestrator.price_changes()
        if changes:
            text += " " + ("One went up: " if len(changes) == 1 else f"{len(changes)} went up: ") + \
                join_and([f"{n} {self._m(b)} → {self._m(a)}" for n, b, a, _ in changes]) + "."
        return _Answer(text, evidence=prices["evidence"])

    def _i_fraud(self, u: Understanding) -> _Answer:
        held = [n for n in self.svc.needs_you()["items"] if n.get("kind") == "approval"]
        if not held:
            return _Answer("Nothing looks suspicious. No bank details changed and no payment is on hold.")
        parts, chips = [], []
        for n in held:
            new = next((f["value"] for f in n.get("facts", []) if f.get("tone") == "risk"), "")
            parts.append(f"{n['title']}" + (f" The new account is {new}." if new else "") +
                         f" I blocked the payment. Only you can release it, in Needs you, after calling "
                         f"{n['merchant']} on a number you already had.")
            chips.append({"label": f"{n['merchant']} · payment on hold", "id": f"needs:{n['id']}"})
        return _Answer(" ".join(parts), evidence=chips)

    def _i_connections(self, u: Understanding) -> _Answer:
        conns = self.svc.connections()["connections"]
        named = [c for c in conns if re.search(rf"\b{re.escape(_clean_name(c['name']))}\b", u.text)
                 or (c["kind"] == "email" and re.search(r"\b(?:email|inbox|mail)\b", u.text))
                 or (c["kind"] == "bank" and re.search(r"\bbanks?\b", u.text))]
        pool = named or conns
        stale = [c for c in pool if c["status"] != "healthy"]
        if stale:
            return _Answer(" ".join(c.get("message") or f"{c['name']} needs reconnecting." for c in stale))
        names = join_and([c["name"] + (f" ({c['account']})" if c["kind"] == "email" else "") for c in pool])
        last = max((c["lastSyncedAt"] for c in pool if c.get("lastSyncedAt")), default=None)
        when = ""
        if last:
            at = datetime.fromisoformat(last)
            when = f" The last sync was at {at:%H:%M}" + (" today." if at.date() == self.today else
                                                         f" on {self._day(at.date())}.")
        if named:
            return _Answer(f"{names} {'is' if len(pool) == 1 else 'are'} connected.{when}")
        return _Answer(f"Everything is connected: {names}.{when}")

    def _i_accountant(self, u: Understanding) -> _Answer:
        acct = self.repo.accountant
        if acct is not None and re.search(r"\bwho\b|\bcontact\b|\bemail address\b|\bdetails\b|\bname\b", u.text):
            waiting = sum(1 for q in self.repo.accountant_questions.values() if q.status != "answered")
            text = f"Your accountant is {acct.person} at {acct.firm} ({acct.email}), working in {acct.software}."
            if waiting:
                text += f" {'One question' if waiting == 1 else f'{waiting} questions'} from them "
                text += "is still open." if waiting == 1 else "are still open."
            return _Answer(text)
        found = self.svc._accountant_answer()
        return _Answer(found["answer"], evidence=found["evidence"])

    def _i_activity(self, u: Understanding) -> _Answer:
        handled = self.svc._handled()
        items = self.svc.activity()["items"]
        parts = []
        if handled:
            parts.append("This week I handled " + join_and([f"{_count_word(h['count'])} {h['label']}"
                                                            for h in handled]) + ".")
        if items:
            parts.append("Latest: " + " ".join(i["text"] for i in items[:3]))
        return _Answer(" ".join(parts) or "Nothing new this week.")

    def _i_overview(self, u: Understanding) -> _Answer:
        home = self.svc.home()
        month = home["currentMonth"]
        parts = [home["headline"]]
        if home["needsYouCount"]:
            parts.append(f"{_count_word(home['needsYouCount']).capitalize()} "
                         f"{'thing needs' if home['needsYouCount'] == 1 else 'things need'} you.")
        parts.append(f"{month['label']} is {month['percentClosed']}% closed: " +
                     join_and([f"{c['name']} {c['statusLabel'].lower()}" for c in home["companies"]]) + ".")
        if home["dueSoon"]:
            parts.append(f"{_count_word(len(home['dueSoon'])).capitalize()} "
                         f"{'payment is' if len(home['dueSoon']) == 1 else 'payments are'} due soon.")
        chips = [{"label": f"{c['name']} · {month['label']}", "id": f"month:{c['id']}:{month['key']}"}
                 for c in home["companies"]]
        return _Answer(" ".join(parts), evidence=chips)

    # -- intents: limits, small talk, unclear ---------------------------------

    def _i_balance(self, u: Understanding) -> _Answer:
        return _Answer("I don't see account balances, only the payments going in and out. Ask me what you spent or "
                       "what came in for any period.")

    def _i_profit(self, u: Understanding) -> _Answer:
        return _Answer("I can't work out profit: I see your payments and purchase invoices, not your sales. I can tell "
                       "you what you spent and what came in for any period.")

    def _i_forecast(self, u: Understanding) -> _Answer:
        return _Answer("I don't make forecasts. I can show what you spent in past months, your regular costs and what "
                       "is due soon.")

    def _i_greeting(self, u: Understanding) -> _Answer:
        return _Answer("Hello. Ask me about your companies, payments, invoices or suppliers, or give me a task: "
                       "“What did we spend in September?”, “Find the Vodafone invoice and send it to marc@…”, "
                       "“Remind me to call the accountant on Friday”.")

    def _i_help(self, u: Understanding) -> _Answer:
        return _Answer(HELP_TEXT)

    def _i_thanks(self, u: Understanding) -> _Answer:
        return _Answer("You're welcome.")

    def _i_clarify(self, u: Understanding) -> _Answer:
        return _Answer(u.clarify or "Could you say that another way?")

    def _i_unknown(self, u: Understanding) -> _Answer:
        return _Answer(FALLBACK_TEXT)


# Why a payout is not counted yet (§20): in a sentence, and for the model.
_PAYOUT_WAIT = {"report_missing": "report not received yet",
                "report_disagrees": "its report does not match what arrived in your bank",
                "report_does_not_add_up": "its report does not add up"}
_PAYOUT_WAIT_FACT = {"report_missing": "payout report not received yet",
                     "report_disagrees": "its report does not match the bank",
                     "report_does_not_add_up": "its report does not add up"}
_PAID_IN = {"tax": "taxes", "bank_fees": "bank fees", "payroll": "salaries", "loan": "loan repayments",
            "platform_fees": "payment and platform fees"}


def _payee(merchant: str) -> str:
    """'Tax office' in a sentence: 'to the tax office'."""
    return "the tax office" if merchant == "Tax office" else merchant


def _clean_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", fold(name)).strip()


_NUMBER_WORDS = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten")


def _count_word(n: int) -> str:
    return _NUMBER_WORDS[n] if 0 <= n < len(_NUMBER_WORDS) else str(n)


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
    {"name": "spending_summary", "description": "What was spent (money out that is a real cost) or what came "
     "in (direction \"in\") over a period, from the bank records: total, breakdown, what was included and left "
     "out, and the payments behind it. Use it for ANY question about expenses, spending, costs, outgoings, "
     "income or money received. Transfers between the owner's own accounts and companies are left out; taxes "
     "and bank fees are included and reported separately. Payouts from card terminals and payment or sales "
     "platforms (SIBS, Stripe, PayPal, Booking.com, Glovo, ...) are net settlements, never customer revenue: "
     "once their payout report matched the bank, money in counts their gross sales and reports the fees, "
     "refunds and disputed payments (sales_from_payout_reports); payouts without a report are listed in "
     "payouts_not_counted_yet and are not counted. Filters are optional: company_id, supplier (name), "
     "category (tax, bank_fees, payroll, loan, rent, telecom, energy, software, travel, meals, office, "
     "insurance, platform_fees, other). group_by: supplier, company, category or month. compare_previous adds the period of "
     "the same length just before. Say which period the records cover when records_cover_period is false or "
     "notes mention it.",
     "input_schema": {"type": "object", "properties": {
         "date_from": _DATE, "date_to": _DATE, "company_id": _S, "supplier": _S, "category": _S,
         "group_by": {"type": "string", "enum": ["supplier", "company", "category", "month"]},
         "direction": {"type": "string", "enum": ["out", "in"]}, "compare_previous": {"type": "boolean"}},
         "required": ["date_from", "date_to"], "additionalProperties": False}},
    {"name": "find_payments", "description": "Individual bank payments (in and out) matching an amount, dates, a "
     "supplier, a company or a category, newest first, with whether each has its invoice. All filters optional.",
     "input_schema": {"type": "object", "properties": {
         "amount": {"type": "number"}, "date_from": _DATE, "date_to": _DATE, "supplier": _S, "company_id": _S,
         "category": _S, "direction": {"type": "string", "enum": ["out", "in"]}}, "additionalProperties": False}},
    {"name": "missing_invoices", "description": "Payments that need an invoice or receipt and do not have one yet, "
     "and payouts from card terminals and sales platforms still waiting for their payout report, with what the "
     "operator is doing about each (for example the supplier was asked). All filters optional.",
     "input_schema": {"type": "object", "properties": {"company_id": _S, "date_from": _DATE, "date_to": _DATE},
                      "additionalProperties": False}},
    {"name": "vat_summary", "description": "VAT on purchase and sales documents dated in a period, and VAT paid "
     "to the tax office in it.",
     "input_schema": {"type": "object", "properties": {"date_from": _DATE, "date_to": _DATE, "company_id": _S},
                      "required": ["date_from", "date_to"], "additionalProperties": False}},
    {"name": "recurring_costs", "description": "Regular costs (subscriptions, monthly bills) learned from the "
     "payments, and the ones whose price went up.",
     "input_schema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "due_soon", "description": "What is due: payments on hold, tax and other deadlines from letters, "
     "unpaid tax obligations and upcoming renewals.",
     "input_schema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "accountant_questions", "description": "The accountant's questions, with the answers given and the "
     "ones still open.",
     "input_schema": {"type": "object", "properties": {}, "additionalProperties": False}},
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
     "given the answer in this conversation, and only for plain choices. Payment approvals, changed bank details "
     "and conflicts (a document whose details disagree) can never be answered here: the owner taps those in "
     "Needs you.",
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
          "payments or changed bank details, never settle a document whose details disagree (the owner taps those "
          "in Needs you), and never send anything yourself; say so plainly when asked. "
          "Answer the question that was asked. For expenses, spending, costs or money received use "
          "spending_summary (spending is money out that is a real cost: transfers between the owner's own "
          "accounts and companies are left out; taxes and bank fees are included and named separately; say so "
          "briefly). Use find_payments for one payment, missing_invoices for payments without an invoice, "
          "vat_summary for VAT, and month_status only when the owner asks whether a month is closed or complete. "
          "A month without a year is the most recent one. If data does not cover the period asked, say which "
          "period it covers. Write amounts like €1,200.00 and dates like 2 September. When something is done, "
          "say “Done.” and what changed.")

# Tools that change something: the browser keeps and replays them (web/lib/engine.ts).
CHANGING_TOOLS = frozenset({"period_report", "draft_email", "answer_question", "create_task", "complete_task"})


def _iso(v: Any) -> date | None:
    try:
        return date.fromisoformat(str(v)) if v else None
    except ValueError:
        return None


def _needs_kind(op: Operator, needs_id: str) -> str:
    """The engine's own kind of an open question: "choice", "check" (details disagree) or "approval".

    Needs You shows a "check" to the owner as a choice card; the chat must still treat it as a conflict.
    Unknown ids count as the strictest kind.
    """
    record = op.repo.needs.get(needs_id)
    return record.kind if record is not None else "approval"


def run_tool(op: Operator, name: str, args: dict[str, Any], cards: list[dict[str, Any]]) -> Any:
    """Run one tool against the engine. Raises ValueError/KeyError with a message the model can act on."""
    svc = op.svc
    if name == "business_status":
        home = svc.home()
        needs = []
        for n in svc.needs_you()["items"]:
            kind = _needs_kind(op, n["id"])
            item = {"question_id": n["id"], "kind": {"check": "conflict"}.get(kind, kind), "merchant": n.get("merchant"),
                    "amount": n.get("amount"), "date": n.get("date"),
                    "question": n.get("question") or n.get("title"),
                    "owner_must_confirm_in_needs_you": kind != "choice"}
            if kind == "choice":  # only plain choices can be answered from the chat
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
        # Decided on the engine's own record: a conflict is shown to the web as a choice, but it is not one.
        kind = _needs_kind(op, qid)
        if kind != "choice":
            what = "confirm" if kind == "approval" else "check"
            cards.append({"type": "evidence", "items": [{"label": f"{item.get('merchant')} · {what} in Needs you",
                                                         "id": f"needs:{qid}"}]})
            raise ValueError("This needs the owner's own tap in Needs you: payment approvals, changed bank details "
                             "and documents whose details disagree are never answered in chat.")
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
    if name == "spending_summary":
        brain = RuleBrain(op)
        period = _tool_period(args, svc._today())
        assert period is not None
        direction = str(args.get("direction") or "out")
        if direction not in ("out", "in"):
            raise ValueError("direction is out or in")
        group = args.get("group_by") or None
        if group not in (None, "supplier", "company", "category", "month"):
            raise ValueError("group_by is supplier, company, category or month")
        slots = Slots(periods=[period], company_ids=_tool_companies(op, args.get("company_id")),
                      supplier_ids=_tool_suppliers(op, args.get("supplier")),
                      category=_tool_category(brain, args.get("category")), group_by=group,
                      compare=bool(args.get("compare_previous")))
        prev = period.previous(svc._today())
        m = brain.ledger.money(period.start, period.end, direction=direction, company_ids=slots.company_ids,
                               supplier_ids=slots.supplier_ids, category=slots.category, group_by=group,
                               compare=(prev.start, prev.end, prev.label) if slots.compare else None)
        if m.covered:
            cards.append(brain._money_card(m, period, slots))
        return brain.money_facts(m, period)
    if name == "find_payments":
        brain = RuleBrain(op)
        period = _tool_period(args, svc._today(), required=False)
        amount = Decimal(str(args["amount"])).copy_abs() if args.get("amount") is not None else None
        direction = args.get("direction") or None
        if direction not in (None, "out", "in"):
            raise ValueError("direction is out or in")
        found = brain.ledger.select(start=period.start if period else None, end=period.end if period else None,
                                    direction=direction, company_ids=_tool_companies(op, args.get("company_id")),
                                    supplier_ids=_tool_suppliers(op, args.get("supplier")),
                                    category=_tool_category(brain, args.get("category")), amount=amount)
        found = sorted(found, key=lambda x: (x.on, x.id), reverse=True)
        chips = [{"label": brain.ledger.payment_label(x), "id": x.evidence_id} for x in found[:8] if x.evidence_id]
        if chips:
            cards.append({"type": "evidence", "items": chips})
        first, last = brain.ledger.coverage()
        return {"records_cover": {"from": first.isoformat(), "to": last.isoformat()},
                "payments": [brain.payment_facts(x) for x in found[:50]], "more": max(0, len(found) - 50)}
    if name == "missing_invoices":
        brain = RuleBrain(op)
        period = _tool_period(args, svc._today(), required=False)
        items = brain.ledger.missing(start=period.start if period else None, end=period.end if period else None,
                                     company_ids=_tool_companies(op, args.get("company_id")))
        items += brain.ledger.missing_reports(start=period.start if period else None,
                                              end=period.end if period else None,
                                              company_ids=_tool_companies(op, args.get("company_id")))
        chips = [{"label": brain.ledger.payment_label(x), "id": x.evidence_id} for x, _ in items if x.evidence_id]
        if chips:
            cards.append({"type": "evidence", "items": chips[:8]})
        return [{**brain.payment_facts(x), "what_i_am_doing": plan} for x, plan in items]
    if name == "vat_summary":
        brain = RuleBrain(op)
        period = _tool_period(args, svc._today())
        assert period is not None
        companies = _tool_companies(op, args.get("company_id"))
        answer = brain.vat_answer(period, companies)
        cards.extend(answer.chat()["cards"])
        v = brain.ledger.vat(period.start, period.end, companies)
        return {"summary": answer.text, "purchase_vat_eur": float(v.purchase_vat), "sales_vat_eur": float(v.sales_vat),
                "purchase_documents": [{**d, "date": d["date"].isoformat(), "vat": float(d["vat"]),
                                        "gross": float(d["gross"])} for d in v.purchases],
                "vat_paid_to_tax_office": [brain.payment_facts(x) for x in v.paid_to_state]}
    if name == "recurring_costs":
        brain = RuleBrain(op)
        answer = brain._i_subscriptions(understand("what are my subscriptions", brain.vocab, svc._today()))
        if answer.evidence:
            cards.append({"type": "evidence", "items": answer.evidence})
        return {"summary": answer.text,
                "price_increases": [{"name": n, "before_eur": float(b), "after_eur": float(a)}
                                    for n, b, a, _ in svc.orchestrator.price_changes()]}
    if name == "due_soon":
        today = svc._today()
        return {"today": today.isoformat(), "due_soon": svc._due_soon(),
                "unpaid_tax_and_other_obligations": [
                    {"title": o.title, "company": op.repo.company_name(o.obligation.entity_id),
                     "due": o.obligation.due_on.isoformat(),
                     "amount_eur": float(o.obligation.amount) if o.obligation.amount is not None else None,
                     "reference": o.reference} for o in op.repo.obligations.values() if not o.satisfied_by],
                "renewals": [{"name": r.name, "company": op.repo.company_name(r.company_id),
                              "renews_on": r.renews_on.isoformat(), "detail": r.detail}
                             for r in op.repo.relationships if r.renews_on and r.renews_on >= today]}
    if name == "accountant_questions":
        return [{"id": q.id, "company": op.repo.company_name(q.company_id), "question": q.text, "status": q.status,
                 "answer": q.answer, "asked_on": q.asked_at.date().isoformat()}
                for q in sorted(op.repo.accountant_questions.values(), key=lambda q: (q.asked_at, q.id))]
    raise ValueError(f"unknown tool {name}")


def _tool_period(args: dict[str, Any], today: date, *, required: bool = True) -> Period | None:
    raw_a, raw_b = args.get("date_from"), args.get("date_to")
    a, b = _iso(raw_a), _iso(raw_b)
    if raw_a and a is None or raw_b and b is None:
        raise ValueError("dates must be YYYY-MM-DD")
    if a is None and b is None:
        if required:
            raise ValueError("date_from and date_to are required (YYYY-MM-DD)")
        return None
    return period_between(a or date(2000, 1, 1), b or today, today)


def _tool_companies(op: Operator, value: Any) -> list[str]:
    if value in (None, "", "all"):
        return []
    if value in op.repo.companies:
        return [str(value)]
    for cid, entity in op.repo.companies.items():
        if fold(entity.name) == fold(str(value)):
            return [cid]
    raise ValueError(f"Unknown company_id. Use one of: {', '.join(op.repo.companies)}.")


def _tool_suppliers(op: Operator, value: Any) -> list[str]:
    if not value:
        return []
    wanted = fold(str(value)).strip()
    for sup in sorted(op.repo.suppliers.values(), key=lambda x: x.id):
        names = [fold(n) for n in (sup.name, *sup.aliases)]
        if any(wanted == n or wanted in n or n in wanted for n in names):
            return [sup.id]
    known = ", ".join(sorted(x.name for x in op.repo.suppliers.values()))
    raise ValueError(f"I don't know a supplier called {value}. Known suppliers: {known}.")


def _tool_category(brain: RuleBrain, value: Any) -> str | None:
    if not value:
        return None
    wanted = fold(str(value)).strip()
    for c in CATEGORIES:
        if wanted in (c.id, fold(c.label)) or wanted.replace(" ", "_") == c.id or wanted in c.asked:
            return c.id
    if wanted in ("other", "other costs"):
        return "other"
    for key, label in brain.vocab.category_labels.items():
        if key.startswith("custom:") and fold(label) == wanted:
            return key
    raise ValueError("Unknown category. Use one of: " + ", ".join([c.id for c in CATEGORIES] + ["other"]) + ".")


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
