"""BackOfficeService: every API endpoint as a plain method returning JSON-able dicts.

Pure Python over the orchestrator: no FastAPI, no threads, no network, no
filesystem. The same object serves the FastAPI app (``backoffice.api.app``)
and the web app running Python in the browser (Pyodide), through
:meth:`BackOfficeService.dispatch`.

Response shapes follow ``web/lib/types.ts`` exactly. Money is Decimal inside;
it becomes a JSON number only at this boundary, because the web contract says
``amount: number``. Everything an owner reads is plain language (§36, §70).

    svc = BackOfficeService.demo()
    status, body = svc.dispatch("GET", "/api/home", None)
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal
from typing import Any
from urllib.parse import unquote

from backoffice.closure import BlockerKind, BusinessAuditFindings, Month, PriceIncrease, due_soon
from backoffice.closure import render_business_audit
from backoffice.domain.lifecycle import Stage
from backoffice.domain.models import DocumentType, SourceKind
from backoffice.evidence import SharePayload, UploadRequest
from backoffice.fraud import mask_iban, normalize_iban
from backoffice.fraud.iban import is_valid_iban
from backoffice.language import connector_problem, greeting, since_phrase, status_headline
from backoffice.learning import (
    Answer,
    OptionKind,
    day_month,
    display_name,
    fold,
    format_money,
    learn_from_transactions,
    suggest_rule_from_answer,
)
from backoffice.orchestrator import (
    TZ,
    DocumentRecord,
    Account,
    ConnectorState,
    NeedsYouRecord,
    Orchestrator,
    Relationship,
    TxRecord,
)
from backoffice.domain.models import Supplier

__all__ = ["BackOfficeService", "ServiceError"]

BANK_CONSENT_DAYS = 180  # PSD2 access consent (RTS Art. 10, as amended 2022): renew at most every 180 days
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_SLUG = re.compile(r"[^a-z0-9]+")


def _default_vault() -> Any:
    """An in-process vault with a random key when the crypto library is present; None in the browser."""
    try:
        import os

        from backoffice.connectors.vault import LocalKeyProvider, TokenVault

        keys = LocalKeyProvider.from_env() if os.environ.get("BACKOFFICE_VAULT_KEY") else LocalKeyProvider(os.urandom(32))
        TokenVault(keys).store("probe", "probe", "probe", {"x": 1})
        return TokenVault(keys)
    except Exception:
        return None

ANSWER_FALLBACK = ("I could not find a clear answer to that yet. Try asking about a supplier, a payment, "
                   "or a month — for example, “Did we pay Vodafone?”")
_MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
           "november", "december")


class ServiceError(Exception):
    """A request the service refuses; ``status`` is the HTTP status, ``message`` owner-safe."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _num(value: Decimal | None) -> int | float | None:
    """JSON number for a Decimal amount (the web contract types money as ``number``)."""
    if value is None:
        return None
    value = value.quantize(Decimal("0.01"))
    return int(value) if value == value.to_integral_value() else float(value)


def _iso(value: date | datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


class BackOfficeService:
    """The owner's back office for one tenant, in memory."""

    def __init__(self, orchestrator: Orchestrator, *, vault: Any = None, authorizer: Any = None) -> None:
        self.orchestrator = orchestrator
        # Sign-in secrets live only in the vault (connectors/vault.py), never in the repository.
        self.vault = vault if vault is not None else _default_vault()
        self.authorizer = authorizer
        self.sign_in: dict[str, dict[str, Any]] = {}

    @classmethod
    def demo(cls) -> BackOfficeService:
        """Laura's three companies with September 2026 processed by the real pipeline."""
        from backoffice.demo import build_demo

        return cls(build_demo())

    @property
    def repo(self):  # type: ignore[no-untyped-def]
        return self.orchestrator.repo

    # ----------------------------------------------------------------- shared views

    def _now(self) -> datetime:
        return self.repo.clock.now()

    def _today(self) -> date:
        return self.repo.today()

    def _current_month(self) -> Month:
        """The month being closed: the one before today's."""
        return Month.of(self._today()).previous()

    def _company_month(self, company_id: str) -> Month:
        return self._current_month()

    def _status(self, company_id: str, month: Month):  # type: ignore[no-untyped-def]
        return self.orchestrator.month_status(company_id, month)

    def _company_name(self, company_id: str | None) -> str | None:
        return self.repo.company_name(company_id)

    def _open_needs(self, company_id: str | None = None) -> list[NeedsYouRecord]:
        return [n for n in self.repo.open_needs() if company_id is None or n.company_id == company_id]

    def _stale_connectors(self) -> list:  # type: ignore[type-arg]
        return [c for c in self.repo.connectors.values() if not c.healthy]

    # ----------------------------------------------------------------- Home

    def home(self) -> dict[str, Any]:
        now = self._now()
        month = self._current_month()
        needs = self._open_needs()
        stale = self._stale_connectors()
        risk = sum(1 for n in needs if n.kind == "approval")
        statuses = [self._status(c, month) for c in sorted(self.repo.companies)]
        done = sum((s.weighted_done if not s.closed else s.weighted_total) for s in statuses)
        total = sum((s.weighted_total for s in statuses), Decimal(0))
        if statuses and all(s.closed for s in statuses):
            percent = 100
        elif total > 0:
            percent = min(99, int((Decimal(done) * 100 / total).to_integral_value(rounding=ROUND_FLOOR)))
        else:
            percent = 0
        return {
            "greeting": greeting(now.astimezone(TZ)),
            "headline": status_headline(len(needs) + len(stale), risk),
            "needsYouCount": len(needs) + len(stale),
            "dueSoon": self._due_soon(),
            "currentMonth": {"key": str(month), "label": month.name, "percentClosed": percent},
            "companies": self.companies()["companies"],
            "handledPeriodLabel": "This week",
            "handled": self._handled(),
            "connections": self.connections()["connections"],
        }

    def _due_soon(self) -> list[dict[str, Any]]:
        today = self._today()
        items: list[dict[str, Any]] = []
        for n in self._open_needs():
            if n.kind != "approval":
                continue
            doc = self.repo.documents[n.subject_id]
            who = display_name(doc.document.supplier_name)
            items.append({
                "id": f"due_{n.id}", "title": f"{who} payment", "companyName": self._company_name(n.company_id) or "",
                "due": _iso(doc.document.due_date or doc.document.issue_date or today),
                "note": "On hold until you confirm the new bank details." if self.orchestrator._iban_changed(doc)
                else "On hold until you check the invoice.",
                "tone": "risk", "href": f"/needs-you#{n.id}",
            })
        open_obligations = [o.obligation for o in self.repo.obligations.values() if not o.satisfied_by]
        for due in due_soon(open_obligations, today, within_days=21):
            record = self.repo.obligations[due.obligation_id]
            amount = f"{format_money(due.amount, due.currency)} · " if due.amount is not None else ""
            reference = f"reference {record.reference}. " if record.reference else ""
            tone = "risk" if due.overdue else ("attention" if due.days_left <= 5 else "neutral")
            items.append({
                "id": f"due_{due.obligation_id}", "title": due.title,
                "companyName": self._company_name(due.entity_id) or "", "due": _iso(due.due_on),
                "note": f"{amount}{reference}I will check the payment when it goes out.".replace(" · reference", ", reference"),
                "tone": tone,
            })
        return sorted(items, key=lambda i: (i["due"], i["id"]))

    def _handled(self) -> list[dict[str, Any]]:
        since = self._now() - timedelta(days=7)
        recent = [a for a in self.repo.activity if a.at > since]
        counts = [
            ("h_docs", sum(1 for a in recent if a.kind in ("collected", "recovered") and "letter" not in a.text),
             "document collected", "documents collected"),
            ("h_missing", sum(1 for a in recent if a.kind == "recovered"),
             "missing invoice recovered", "missing invoices recovered"),
            ("h_supplier", sum(1 for a in recent if a.kind == "chased"),
             "supplier email handled", "supplier emails handled"),
            ("h_accountant", sum(1 for a in recent if a.kind == "answered" and "accountant" in a.text),
             "accountant question answered", "accountant questions answered"),
            ("h_protected", sum(1 for a in recent if a.kind == "protected" and "hold" in a.text),
             "payment protected", "payments protected"),
        ]
        return [{"id": i, "count": n, "label": one if n == 1 else many} for i, n, one, many in counts if n]

    # ----------------------------------------------------------------- Connections

    def connections(self) -> dict[str, Any]:
        now = self._now()
        out = []
        kind_source = {"email": SourceKind.EMAIL, "bank": SourceKind.BANK, "accountant": SourceKind.ACCOUNTANT}
        for c in self.repo.connectors.values():
            entry: dict[str, Any] = {
                "id": c.id, "name": c.name, "kind": c.kind, "account": c.account,
                "status": "healthy" if c.healthy else "stale", "lastSyncedAt": _iso(c.last_synced_at),
            }
            if not c.healthy:
                entry["lastSyncedLabel"] = since_phrase(c.last_synced_at, now, TZ) if c.last_synced_at else None
                problem = connector_problem(c.name, kind_source[c.kind], c.last_synced_at, now, tz=TZ)
                entry["message"] = f"{problem.title} {problem.detail}"
            out.append(entry)
        return {"connections": out}

    def mark_connection_stale(self, connection_id: str, since: datetime | None = None) -> dict[str, Any]:
        """A connector stopped syncing (§47–48): the month can never show green until it is back."""
        c = self.repo.connectors.get(connection_id)
        if c is None:
            raise ServiceError(404, "I can't find that connection.")
        since = since or (self._now() - timedelta(hours=18, minutes=48))
        c.healthy = False
        c.covered_until = min(c.covered_until or since, since)
        c.last_synced_at = since
        self.orchestrator.log("closure", "connector_stale", subject_id=c.id, values={"since": since})
        self.orchestrator.run()
        return {"ok": True, "connection": next(x for x in self.connections()["connections"] if x["id"] == c.id)}

    def reconnect(self, connection_id: str) -> dict[str, Any]:
        c = self.repo.connectors.get(connection_id)
        if c is None:
            raise ServiceError(404, "I can't find that connection.")
        c.healthy = True
        c.covered_until = c.last_synced_at = self._now()
        self.orchestrator.log("closure", "connector_reconnected", subject_id=c.id)
        self.orchestrator.run()
        return {"ok": True, "connection": next(x for x in self.connections()["connections"] if x["id"] == c.id)}

    # ----------------------------------------------------------------- Sources

    def sources(self) -> dict[str, Any]:
        """Everything the system is connected to or has learned about, grouped for the owner."""
        repo = self.repo
        names = {cid: e.name for cid, e in repo.companies.items()}
        conns = {c["id"]: c for c in self.connections()["connections"]}

        def conn(kind: str) -> list[dict[str, Any]]:
            return [c for c in conns.values() if c["kind"] == kind]

        def bank_status(bank: str) -> str:
            for c in conn("bank"):
                if c["name"] == bank:
                    return c["status"]
            return "not_connected"

        accounts = [
            {"id": a.id, "name": f"{a.bank} •••• {(a.iban or '')[-4:]}", "company": names.get(a.holder_id, ""),
             "detail": a.iban[:4] + " •••• " + a.iban[-4:] if a.iban else "", "status": bank_status(a.bank)}
            for a in repo.accounts.values() if a.iban
        ]
        cards = [
            {"id": a.id, "name": f"Card •••• {a.card_last4}", "company": names.get(a.holder_id, ""),
             "detail": a.bank + ("" if a.owned else " · personal card used for business"),
             "status": bank_status(a.bank)}
            for a in repo.accounts.values() if a.card_last4
        ]
        suppliers = []
        for s in repo.suppliers.values():
            keys = {k.upper() for k in [s.name, *s.aliases]}
            docs = [d for d in repo.documents.values() if d.supplier_id == s.id]
            txs = [t for t in repo.transactions.values() if any(k in t.tx.counterparty.upper() for k in keys)]
            companies = sorted({names.get(t.holder_id, "") for t in txs} - {""})
            last = max([t.tx.booked_on for t in txs], default=None)
            parts = [f"{len(docs)} document" + ("" if len(docs) == 1 else "s"),
                     f"{len(txs)} payment" + ("" if len(txs) == 1 else "s")]
            if s.known_ibans:
                parts.append("bank details on file")
            suppliers.append({"id": s.id, "name": s.name, "company": ", ".join(companies),
                              "detail": " · ".join(parts),
                              "lastSeen": last.isoformat() if last else None,
                              "status": "hold" if any(d.on_hold and not d.hold_released for d in docs) else "known"})
        suppliers.sort(key=lambda x: x["name"].lower())

        def rel(kind: str) -> list[dict[str, Any]]:
            return [{"id": r.id, "name": r.name, "company": names.get(r.company_id, ""), "detail": r.detail,
                     "foundIn": r.found_in, "renewsOn": r.renews_on.isoformat() if r.renews_on else None,
                     "status": "known"} for r in repo.relationships if r.kind == kind]

        groups = [
            ("email", "Email", "Where invoices, letters and receipts arrive.",
             [{"id": c["id"], "name": c["account"], "company": "All companies", "detail": c["name"],
               "status": c["status"], "lastSyncedAt": c.get("lastSyncedAt")} for c in conn("email")]),
            ("banks", "Bank accounts", "Every payment in and out is checked against evidence.", accounts),
            ("cards", "Cards", "Card spending is matched to receipts.", cards),
            ("accountant", "Accountant", "Receives the monthly package and asks questions here.",
             [{"id": c["id"], "name": c["name"], "company": "All companies", "detail": c["account"],
               "status": c["status"], "lastSyncedAt": c.get("lastSyncedAt")} for c in conn("accountant")]),
            ("suppliers", "Suppliers", "Recognised from invoices and payments.", suppliers),
            ("insurance", "Insurance", "Policies found in email and payments. I watch the renewal dates.",
             rel("insurance")),
            ("investments", "Investments", "Holdings and regular contributions.", rel("investment")),
            ("lenders", "Loans", "Repayments are matched to loan statements.", rel("lender")),
            ("government", "Tax and government", "Letters, deadlines and payments.", rel("government")),
        ]
        for _, _, _, items in groups:
            for item in items:
                label = self._sign_in_label(item["id"])
                if label is None and item.get("status") in ("healthy", "stale"):
                    bank = next((c for c in self.repo.connectors.values()
                                 if c.kind == "bank" and item.get("name", "").startswith(c.name)), None)
                    label = (self._sign_in_label(bank.id) if bank else None) or \
                        "Demo connection: no real sign-in was made."
                if label:
                    item["signIn"] = label
        return {"groups": [{"id": g, "title": t, "description": d, "items": items} for g, t, d, items in groups],
                "companies": [{"id": cid, "name": n} for cid, n in names.items()]}

    # ----------------------------------------------------------------- Adding and removing sources

    def _slug(self, prefix: str, name: str) -> str:
        base = f"{prefix}-" + (_SLUG.sub("-", name.lower()).strip("-") or "x")[:40]
        taken = {*self.repo.connectors, *self.repo.accounts, *self.repo.suppliers,
                 *(r.id for r in self.repo.relationships)}
        slug, n = base, 2
        while slug in taken:
            slug, n = f"{base}-{n}", n + 1
        return slug

    def _company(self, company_id: Any, *, required: bool = True) -> str | None:
        if company_id in (None, "", "all"):
            if required:
                raise ServiceError(400, "Choose which company this belongs to.")
            return None
        if company_id not in self.repo.companies:
            raise ServiceError(400, "I don't know that company.")
        return str(company_id)

    @staticmethod
    def _text(body: Mapping[str, Any], key: str, message: str, *, required: bool = True, limit: int = 120) -> str:
        value = body.get(key)
        value = value.strip() if isinstance(value, str) else ""
        if required and not value:
            raise ServiceError(400, message)
        if len(value) > limit:
            raise ServiceError(400, "That is too long.")
        return value

    def _sign_in_label(self, source_id: str) -> str | None:
        info = self.sign_in.get(source_id)
        if not info:
            return None
        if info.get("consent_until"):
            until = info["consent_until"]
            return (f"Bank consent until {until.day} {until.strftime('%B %Y')}. "
                    "I will remind you a week before; banks require this every 180 days.")
        if info.get("pending"):
            return "Waiting for you to finish signing in."
        if info.get("stored"):
            return "Signed in. Access renews automatically; no need to reconnect."
        return "Demo connection: no real sign-in was made."

    def add_source(self, body: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(body, Mapping):
            raise ServiceError(400, "Tell me what to add.")
        kind = body.get("kind")
        handler = {
            "email": self._add_email, "bank": self._add_bank, "card": self._add_card, "supplier": self._add_supplier,
            "insurance": self._add_relationship, "investment": self._add_relationship,
            "loan": self._add_relationship, "government": self._add_relationship,
        }.get(kind if isinstance(kind, str) else "")
        if handler is None:
            raise ServiceError(400, "I can add email, bank accounts, cards, suppliers, insurance, investments, "
                                    "loans and government offices.")
        result = handler(body)
        self.orchestrator.log("discovery", "source_added", subject_id=result["id"], values={"kind": kind})
        self.orchestrator.run()
        return {"ok": True, **result}

    def _add_email(self, body: Mapping[str, Any]) -> dict[str, Any]:
        address = self._text(body, "address", "Which email address?").lower()
        if not _EMAIL.match(address):
            raise ServiceError(400, "That doesn't look like an email address.")
        if any(c.kind == "email" and c.account.lower() == address for c in self.repo.connectors.values()):
            raise ServiceError(409, f"{address} is already connected.")
        provider = body.get("provider") or "google"
        if provider not in ("google", "microsoft", "imap"):
            raise ServiceError(400, "Choose Google, Microsoft or another provider.")
        company = self._company(body.get("companyId"), required=False)
        company_ids = (company,) if company else tuple(self.repo.companies)
        cid = self._slug("mail", address)
        now = self._now()
        name = {"google": "Gmail", "microsoft": "Outlook", "imap": "Email"}[provider]
        secret_stored = False
        if provider == "imap":
            host = self._text(body, "host", "Which mail server? For example imap.example.com.")
            password = body.get("password")
            if not isinstance(password, str) or not password:
                raise ServiceError(400, "Enter the app password for this mailbox.")
            if self.vault is not None:
                self.vault.store(self.repo.tenant_id, cid, "imap",
                                 {"host": host, "username": address, "password": password})
                secret_stored = True
            name = f"Email ({host})"
        authorize_url = None
        if provider in ("google", "microsoft") and self.authorizer is not None:
            try:
                authorize_url = self.authorizer.begin(provider, self.repo.tenant_id, cid, login_hint=address)
            except Exception:
                authorize_url = None
        pending = authorize_url is not None
        self.repo.add_connector(ConnectorState(
            id=cid, name=name, kind="email", account=address, company_ids=company_ids, healthy=not pending,
            covered_from=None if pending else now - timedelta(days=90),
            covered_until=None if pending else now, last_synced_at=None if pending else now))
        self.sign_in[cid] = {"provider": provider, "stored": secret_stored, "pending": pending}
        if not pending:
            self.orchestrator.activity(now, "checked", f"Connected {address} and read the last 90 days.",
                                       company if company else None)
        out: dict[str, Any] = {"id": cid, "message": "Almost done. Finish signing in." if pending else
                               f"Done. I now read {address}."}
        if authorize_url:
            out["authorizeUrl"] = authorize_url
        return out

    def _add_bank(self, body: Mapping[str, Any]) -> dict[str, Any]:
        bank = self._text(body, "bank", "Which bank?")
        company = self._company(body.get("companyId"))
        iban_raw = self._text(body, "iban", "", required=False, limit=42)
        iban = normalize_iban(iban_raw) if iban_raw else None
        if iban_raw and not (iban and is_valid_iban(iban)):
            raise ServiceError(400, "That IBAN doesn't look right. Check the digits.")
        if iban and any(a.iban == iban for a in self.repo.accounts.values()):
            raise ServiceError(409, "That account is already connected.")
        aid = self._slug("acct", f"{bank} {(iban or '')[-4:]}")
        self.repo.add_account(Account(id=aid, bank=bank, holder_id=company, iban=iban))
        entity = self.repo.companies[company]
        if iban and iban not in entity.own_ibans:
            entity.own_ibans.append(iban)
        now = self._now()
        existing = next((c for c in self.repo.connectors.values() if c.kind == "bank" and c.name == bank), None)
        if existing:
            if company not in existing.company_ids:
                existing.company_ids = (*existing.company_ids, company)
            cid = existing.id
        else:
            cid = self._slug("bank", bank)
            self.repo.add_connector(ConnectorState(
                id=cid, name=bank, kind="bank", account=entity.name, company_ids=(company,), healthy=True,
                covered_from=now - timedelta(days=90), covered_until=now, last_synced_at=now))
        until = (now + timedelta(days=BANK_CONSENT_DAYS)).date()
        self.sign_in[aid] = self.sign_in[cid] = {"provider": "open_banking", "consent_until": until}
        self.orchestrator.activity(now, "checked", f"Connected {bank} for {entity.name} and imported 90 days.",
                                   company)
        return {"id": aid, "message": f"Done. {bank} is connected for {entity.name}."}

    def _add_card(self, body: Mapping[str, Any]) -> dict[str, Any]:
        last4 = self._text(body, "last4", "The last 4 digits of the card.", limit=4)
        if not re.fullmatch(r"\d{4}", last4):
            raise ServiceError(400, "Enter exactly the last 4 digits.")
        if any(a.card_last4 == last4 for a in self.repo.accounts.values()):
            raise ServiceError(409, "That card is already here.")
        bank = self._text(body, "bank", "Which bank issued the card?")
        company = self._company(body.get("companyId"))
        aid = self._slug("card", last4)
        self.repo.add_account(Account(id=aid, bank=bank, holder_id=company, card_last4=last4,
                                      owned=not bool(body.get("personal"))))
        return {"id": aid, "message": f"Done. I will match card •••• {last4} to receipts."}

    def _add_supplier(self, body: Mapping[str, Any]) -> dict[str, Any]:
        name = self._text(body, "name", "What is the supplier called?")
        if any(s.name.lower() == name.lower() for s in self.repo.suppliers.values()):
            raise ServiceError(409, f"{name} is already a supplier.")
        tax_id = self._text(body, "taxId", "", required=False, limit=20) or None
        email = self._text(body, "email", "", required=False) or None
        if email and not _EMAIL.match(email):
            raise ServiceError(400, "That doesn't look like an email address.")
        sid = self._slug("sup", name)
        self.repo.add_supplier(Supplier(
            id=sid, tenant_id=self.repo.tenant_id, name=name, aliases=[name.upper()], tax_id=tax_id,
            email_domains=[email.split("@", 1)[1].lower()] if email else [], contact_email=email))
        return {"id": sid, "message": f"Done. I will look for {name} in email and payments."}

    def _add_relationship(self, body: Mapping[str, Any]) -> dict[str, Any]:
        kind = {"loan": "lender"}.get(body["kind"], body["kind"])
        name = self._text(body, "name", "What is it called?")
        company = self._company(body.get("companyId"))
        detail = self._text(body, "detail", "", required=False, limit=160)
        renews = body.get("renewsOn")
        renews_on = None
        if renews:
            try:
                renews_on = date.fromisoformat(str(renews))
            except ValueError:
                raise ServiceError(400, "Use a date like 2027-01-15.") from None
        rid = self._slug("rel", name)
        self.repo.relationships.append(Relationship(rid, kind, name, company, detail or "Added by you",
                                                    "Added by you", renews_on=renews_on))
        return {"id": rid, "message": f"Done. I will watch for {name}."}

    # ----------------------------------------------------------------- Chat operator

    @property
    def assistant(self):  # type: ignore[no-untyped-def]
        if getattr(self, "_assistant", None) is None:
            from backoffice.assistant import Operator

            self._assistant = Operator(self)
        return self._assistant

    def chat(self, body: Mapping[str, Any]) -> dict[str, Any]:
        message = body.get("message") if isinstance(body, Mapping) else None
        if not isinstance(message, str) or not message.strip():
            raise ServiceError(400, "Write what you need.")
        if len(message) > 4000:
            raise ServiceError(400, "That is too long. Try a shorter request.")
        history = body.get("history") if isinstance(body.get("history"), list) else []
        from backoffice.assistant import ClaudeBrain, RuleBrain

        brain = getattr(self, "brain", None)
        try:
            if brain is not None:
                return brain.handle(message, history)
            return RuleBrain(self.assistant).handle(message)
        except ValueError as exc:
            raise ServiceError(400, str(exc)) from None
        except Exception:
            if brain is not None:  # the model is unavailable: fall back to the rules
                return RuleBrain(self.assistant).handle(message)
            raise

    def chat_send(self, message_id: str, body: Mapping[str, Any] | None = None) -> dict[str, Any]:
        op = self.assistant
        msg = op.outbox.get(message_id)
        if msg is None:
            raise ServiceError(404, "I can't find that email.")
        if isinstance(body, Mapping) and body.get("cancel"):
            if msg.status == "draft":
                msg.status = "cancelled"
            return {"ok": True, "status": msg.status, "message": "Cancelled. Nothing was sent."}
        msg = op.send(message_id)
        return {"ok": True, "status": msg.status, "message": msg.delivery}

    def report_file(self, report_id: str) -> dict[str, Any]:
        r = self.assistant.reports.get(report_id)
        if r is None:
            raise ServiceError(404, "I can't find that report.")
        return {"filename": r["filename"], "contentType": "text/csv",
                "data": base64.b64encode(r["csv"].encode()).decode()}

    # ----------------------------------------------------------------- Documents (repository for the accountant)

    @staticmethod
    def _date_arg(body: Mapping[str, Any] | None, key: str) -> date | None:
        v = (body or {}).get(key)
        if not v:
            return None
        try:
            return date.fromisoformat(str(v))
        except ValueError:
            raise ServiceError(400, "Use dates like 2026-09-30.") from None

    def documents_list(self, body: Mapping[str, Any] | None = None) -> dict[str, Any]:
        b = body or {}
        items = self.assistant.documents(query=str(b.get("q", "")), supplier=str(b.get("supplier", "")),
                                         company_id=str(b.get("company", "")),
                                         date_from=self._date_arg(b, "from"), date_to=self._date_arg(b, "to"))
        return {"items": items, "companies": [{"id": c, "name": e.name} for c, e in self.repo.companies.items()],
                "total": len(items)}

    def document_download(self, document_id: str) -> dict[str, Any]:
        f = self.assistant.document_file(document_id)
        if f is None:
            raise ServiceError(404, "I can't find that document.")
        return {"filename": f[0], "contentType": f[1], "data": base64.b64encode(f[2]).decode()}

    def documents_export(self, body: Mapping[str, Any] | None = None) -> dict[str, Any]:
        b = body or {}
        name, data, count = self.assistant.export_zip(company_id=str(b.get("company", "")),
                                                      date_from=self._date_arg(b, "from"),
                                                      date_to=self._date_arg(b, "to"))
        return {"filename": name, "contentType": "application/zip", "count": count,
                "data": base64.b64encode(data).decode()}

    # ----------------------------------------------------------------- Monthly report delivery

    def _report_settings(self) -> dict[str, Any]:
        if getattr(self, "_report_cfg", None) is None:
            acct = self.repo.accountant
            self._report_cfg = {
                "recipients": ([{"email": acct.email, "name": acct.person, "role": "Accountant"}] if acct else []),
                "day": 3, "format": "zip", "includeDocuments": True,
                "companies": list(self.repo.companies), "copyOwner": True,
            }
        return self._report_cfg

    def report_settings(self, body: Mapping[str, Any] | None = None) -> dict[str, Any]:
        cfg = self._report_settings()
        if body:
            recipients = body.get("recipients", cfg["recipients"])
            if not isinstance(recipients, list) or len(recipients) > 20:
                raise ServiceError(400, "Add up to 20 recipients.")
            clean = []
            for r in recipients:
                email = (r.get("email") if isinstance(r, Mapping) else "") or ""
                if not _EMAIL.match(email.strip()):
                    raise ServiceError(400, f"“{email}” doesn't look like an email address.")
                clean.append({"email": email.strip().lower(), "name": str(r.get("name") or "")[:80],
                              "role": str(r.get("role") or "")[:40]})
            day = body.get("day", cfg["day"])
            if not isinstance(day, int) or not 1 <= day <= 10:
                raise ServiceError(400, "Choose a working day between 1 and 10.")
            fmt = body.get("format", cfg["format"])
            if fmt not in ("zip", "csv"):
                raise ServiceError(400, "Choose ZIP (documents and ledger) or CSV (ledger only).")
            companies = [c for c in body.get("companies", cfg["companies"]) if c in self.repo.companies]
            cfg.update(recipients=clean, day=day, format=fmt, companies=companies,
                       includeDocuments=bool(body.get("includeDocuments", cfg["includeDocuments"])),
                       copyOwner=bool(body.get("copyOwner", cfg["copyOwner"])))
            self.orchestrator.log("closure", "report_settings_changed", values={"recipients": len(clean)})
        names = {c: e.name for c, e in self.repo.companies.items()}
        who = ", ".join(r["email"] for r in cfg["recipients"]) or "nobody yet"
        return {**cfg, "companyNames": names, "ownerEmail": self.repo.owner.email,
                "summary": f"On working day {cfg['day']} of each month I send the closed month to {who}."}

    # ----------------------------------------------------------------- Accountant API keys (§28)

    def _keys(self) -> dict[str, dict[str, Any]]:
        if getattr(self, "_api_keys", None) is None:
            self._api_keys = {}
        return self._api_keys

    def api_keys(self) -> dict[str, Any]:
        return {"keys": [{k: v for k, v in rec.items() if k != "hash"} for rec in self._keys().values()]}

    def api_key_create(self, body: Mapping[str, Any] | None) -> dict[str, Any]:
        import hashlib
        import secrets

        name = str((body or {}).get("name") or "Accounting system").strip()[:60]
        secret = "bo_live_" + secrets.token_urlsafe(24)
        kid = f"key_{len(self._keys()) + 1:03d}"
        self._keys()[kid] = {"id": kid, "name": name, "prefix": secret[:12], "createdAt": self._now().isoformat(),
                             "hash": hashlib.sha256(secret.encode()).hexdigest(), "scope": "documents:read"}
        return {"ok": True, "id": kid, "key": secret,
                "message": "Copy this key now. I only show it once; I store a fingerprint, not the key."}

    def api_key_revoke(self, key_id: str) -> dict[str, Any]:
        if self._keys().pop(key_id, None) is None:
            raise ServiceError(404, "I can't find that key.")
        return {"ok": True, "message": "Revoked. Anything using it stops working now."}

    def api_authorize(self, secret: str | None) -> bool:
        import hashlib
        import hmac

        if not secret:
            return False
        digest = hashlib.sha256(secret.encode()).hexdigest()
        return any(hmac.compare_digest(digest, r["hash"]) for r in self._keys().values())

    def finish_sign_in(self, connection_id: str) -> None:
        """The provider confirmed consent and the vault holds the refresh token: start reading."""
        c = self.repo.connectors.get(connection_id)
        if c is None:
            return
        now = self._now()
        c.healthy, c.covered_from, c.covered_until, c.last_synced_at = True, now - timedelta(days=90), now, now
        self.sign_in[connection_id] = {**self.sign_in.get(connection_id, {}), "pending": False, "stored": True}
        self.orchestrator.activity(now, "checked", f"Connected {c.account}.")
        self.orchestrator.run()

    def remove_source(self, source_id: str) -> dict[str, Any]:
        repo = self.repo
        name = None
        signed_in = source_id in repo.connectors or source_id in repo.accounts
        if source_id in repo.connectors:
            c = repo.connectors.pop(source_id)
            name = c.account if c.kind == "email" else c.name
        elif source_id in repo.accounts:
            a = repo.accounts.pop(source_id)
            name = a.label
        elif source_id in repo.suppliers:
            name = repo.suppliers.pop(source_id).name
        else:
            for r in list(repo.relationships):
                if r.id == source_id:
                    repo.relationships.remove(r)
                    name = r.name
        if name is None:
            raise ServiceError(404, "I can't find that source.")
        self.sign_in.pop(source_id, None)
        if self.vault is not None:
            self.vault.delete(repo.tenant_id, source_id)  # stored sign-in is destroyed with the source
        self.orchestrator.log("discovery", "source_removed", subject_id=source_id)
        self.orchestrator.run()
        if signed_in:
            return {"ok": True, "message": f"Removed {name}. I no longer read it and its sign-in is deleted."}
        return {"ok": True, "message": f"Removed {name}."}

    # ----------------------------------------------------------------- Companies

    def companies(self) -> dict[str, Any]:
        month = self._current_month()
        out = []
        for company_id, entity in self.repo.companies.items():
            status = self._status(company_id, month)
            months = {str(m) for m in self.repo.months_for(company_id)} | {str(month)}
            closed_on = self.repo.closed_months.get((company_id, str(month)))
            kinds = {b.kind for b in status.blockers}
            if status.closed:
                tone, detail = "good", f"{month.name} closed on {day_month(closed_on or self._today(), self._today())}"
            else:
                detail = f"{month.name} · {status.percent_closed}% closed"
                if BlockerKind.CONNECTOR in kinds or BlockerKind.NO_SOURCES in kinds or BlockerKind.CONFLICT in kinds:
                    tone = "risk"
                elif status.needs_you:
                    tone = "attention"
                else:
                    tone = "good"
            out.append({
                "id": company_id, "name": entity.name, "legalName": self.repo.legal_names.get(company_id, entity.name),
                "taxId": entity.tax_id, "tone": tone, "statusLabel": status.company_status, "detail": detail,
                "currentMonth": str(month), "months": sorted(months, reverse=True),
                "pendingItemIds": [n.id for n in self._open_needs(company_id)],
            })
        return {"companies": out}

    def company(self, company_id: str) -> dict[str, Any]:
        found = next((c for c in self.companies()["companies"] if c["id"] == company_id), None)
        if found is None:
            raise ServiceError(404, "I can't find that company.")
        return found

    # ----------------------------------------------------------------- Month

    def month(self, company_id: str, yyyy_mm: str) -> dict[str, Any] | None:
        if company_id not in self.repo.companies:
            return None
        try:
            month = Month.parse(yyyy_mm)
        except (ValueError, TypeError):
            return None
        repo = self.repo
        status = self._status(company_id, month)
        items = repo.items_for(company_id, month)
        txs = [repo.transactions[i.subject_id] for i in items if i.subject_type == "transaction"]
        summary = status.summary
        result: dict[str, Any] = {
            "companyId": company_id,
            "month": str(month),
            "status": "closed" if status.closed else "open",
            "percentClosed": status.percent_closed,
            "transactionsTotal": len(txs),
            "stats": {
                "transactionsChecked": summary.transactions_checked,
                "documentsCollected": summary.documents_collected,
                "missingDocumentsRetrieved": summary.missing_documents_retrieved,
                "suppliersChased": summary.suppliers_chased,
                "accountantQuestionsResolved": summary.accountant_questions_resolved,
                "taxObligationsVerified": summary.tax_obligations_verified,
                "unresolvedIssues": summary.unresolved_issues,
                "minutesSpent": summary.owner_minutes,
            },
            "remaining": [] if status.closed else self._remaining(company_id, month, status, txs),
            "matched": self._matched(txs),
            "headline": status.headline,
        }
        closed_on = repo.closed_months.get((company_id, str(month)))
        if status.closed and closed_on:
            result["closedOn"] = closed_on.isoformat()
        notices = self._notices(company_id, month)
        if notices:
            result["notices"] = notices
        return result

    def _remaining(self, company_id: str, month: Month, status, txs: list[TxRecord]) -> list[dict[str, Any]]:  # type: ignore[no-untyped-def]
        repo = self.repo
        lines: list[dict[str, Any]] = []
        for b in status.blockers:
            if b.kind in (BlockerKind.CONNECTOR, BlockerKind.NO_SOURCES):
                lines.append({"id": f"r_{b.kind.value}_{len(lines)}", "text": b.message, "tone": "risk",
                              "href": "/settings#connections", "linkLabel": b.action or "Reconnect"})
        for n in self._open_needs(company_id):
            if n.kind != "choice" or repo.item_month(repo.items[n.item_id]) != month:
                continue
            rec = repo.transactions[n.subject_id]
            who = self.orchestrator.merchant_name(rec.tx)
            lines.append({"id": f"r_{n.id}", "tone": "attention", "href": f"/needs-you#{n.id}", "linkLabel": "Answer",
                          "text": f"I still need one thing from you: which company the {who} payment of "
                                  f"{format_money(abs(rec.tx.amount), rec.tx.currency)} belongs to."})
        for rec in sorted(txs, key=lambda r: (r.tx.booked_on, r.id)):
            item = repo.items[rec.item_id]
            if item.is_done or item.stage in (Stage.NEEDS_OWNER, Stage.CONFLICT) or rec.document_ids:
                continue
            if rec.decision is not None and rec.decision.requires_document:
                lines.append({"id": f"r_{rec.id}", "text": self.orchestrator.missing.plan(rec), "tone": "neutral"})
        for item in repo.items_for(company_id, month):
            if item.subject_type != "document" or item.is_done or item.stage in (Stage.NEEDS_OWNER, Stage.CONFLICT):
                continue
            doc = repo.documents[item.subject_id]
            if doc.matched_tx_ids:
                continue
            lines.append({"id": f"r_{doc.id}", "tone": "neutral",
                          "text": f"The {display_name(doc.document.supplier_name)} {doc.label.split(' ')[0].lower()} "
                                  "arrived. I'm matching it with its payment."})
        for b in status.blockers:
            if b.kind is BlockerKind.CONFLICT:
                lines.append({"id": f"r_conflict_{len(lines)}", "text": b.message + " I'm holding them until "
                              "someone checks.", "tone": "risk"})
            if b.kind is BlockerKind.OBLIGATION:
                lines.append({"id": f"r_obligation_{len(lines)}", "text": b.message, "tone": "attention"})
            if b.kind is BlockerKind.MONTH_NOT_OVER:
                lines.append({"id": f"r_not_over_{len(lines)}", "text": b.message, "tone": "neutral"})
        if lines and not any(line["tone"] == "risk" for line in lines):
            software = repo.accountant.firm if repo.accountant else "your accountant"
            lines.append({"id": "r_next", "tone": "neutral",
                          "text": f"Once those are done, I will send the month to {software}."})
        return lines

    def _matched(self, txs: list[TxRecord]) -> list[dict[str, Any]]:
        repo = self.repo
        out = []
        for rec in sorted(txs, key=lambda r: (r.tx.booked_on, r.id)):
            item = repo.items[rec.item_id]
            if item.stage is not Stage.CLOSED:
                continue
            if rec.document_ids:
                doc = repo.documents[rec.document_ids[0]]
                out.append({
                    "id": f"m_{rec.id}", "supplier": display_name(doc.document.supplier_name),
                    "description": doc.label.split(" · ")[0], "amount": _num(abs(rec.tx.amount)),
                    "currency": rec.tx.currency, "date": rec.tx.booked_on.isoformat(),
                    "reasons": [r.replace(": ", " ", 1) for r in rec.match_why],
                })
            elif rec.proof_evidence_ids:
                ob = next((o for o in repo.obligations.values() if rec.evidence_id in o.satisfied_by), None)
                amount = format_money(abs(rec.tx.amount), rec.tx.currency)
                reasons = [f"Tax letter asks for {amount}", f"Bank payment {amount}"]
                if ob is not None and ob.reference:
                    reasons.append(f"Reference {ob.reference} matches")
                if ob is not None and rec.tx.booked_on <= ob.obligation.due_on:
                    reasons.append(f"Paid before the deadline of {day_month(ob.obligation.due_on)}")
                out.append({"id": f"m_{rec.id}", "supplier": "Tax office", "description": "Tax payment",
                            "amount": _num(abs(rec.tx.amount)), "currency": rec.tx.currency,
                            "date": rec.tx.booked_on.isoformat(), "reasons": reasons})
        return out

    def _notices(self, company_id: str, month: Month) -> list[dict[str, Any]]:
        out = []
        for n in self._open_needs(company_id):
            if n.kind != "approval":
                continue
            doc = self.repo.documents[n.subject_id]
            doc_month = self.repo.item_month(self.repo.items[doc.item_id])
            who = display_name(doc.document.supplier_name)
            when = f"{doc_month.name}’s " if doc_month and doc_month != month else "The "
            reason = ("The bank details on the invoice changed." if self.orchestrator._iban_changed(doc)
                      else "Something on the invoice does not look right.")
            out.append({"id": f"n_{n.id}", "text": f"{when}{who} payment is on hold. {reason}", "tone": "risk",
                        "href": f"/needs-you#{n.id}", "linkLabel": "Review"})
        return out

    # ----------------------------------------------------------------- Needs you

    def needs_you(self) -> dict[str, Any]:
        items = []
        for n in self._open_needs():
            items.append(self._choice(n) if n.kind == "choice" else self._approval(n))
        return {"items": items}

    def _choice(self, n: NeedsYouRecord) -> dict[str, Any]:
        rec = self.repo.transactions[n.subject_id]
        question = n.question
        assert question is not None
        account = self.repo.accounts.get(rec.tx.account_id)
        item: dict[str, Any] = {
            "id": n.id, "kind": "choice", "tone": "attention", "eyebrow": "We need one answer",
            "merchant": self.orchestrator.merchant_name(rec.tx), "amount": _num(abs(rec.tx.amount)),
            "currency": rec.tx.currency, "date": rec.tx.booked_on.isoformat(), "companyId": n.company_id,
            "question": question.prompt,
            "options": [{"id": o.id, "label": o.label} for o in question.options],
            "why": list(n.why),
        }
        if account is not None:
            item["paidWith"] = account.label
        remember = self._remember_rule(n)
        if remember:
            item["remember"] = remember
        return item

    def _remember_rule(self, n: NeedsYouRecord) -> dict[str, Any] | None:
        question = n.question
        assert question is not None
        template, overrides = None, {}
        for option in question.options:
            proposal = suggest_rule_from_answer(question, Answer(
                question_id=question.id, option_id=option.id, answered_by="owner", answered_at=self._now()))
            if proposal is None:
                continue
            if option.kind is OptionKind.ENTITY and template is None and option.label in proposal.label:
                template = proposal.label.replace(option.label, "{choice}", 1)
            elif option.kind is not OptionKind.ENTITY:
                overrides[option.id] = proposal.label
        if template is None:
            return None
        rule: dict[str, Any] = {"template": template, "defaultChecked": True}
        if overrides:
            rule["overrides"] = overrides
        return rule

    def _approval(self, n: NeedsYouRecord) -> dict[str, Any]:
        doc = self.repo.documents[n.subject_id]
        d = doc.document
        who = display_name(d.supplier_name)
        supplier = self.repo.suppliers.get(doc.supplier_id or "")
        changed = self.orchestrator._iban_changed(doc)
        doc_month = self.repo.item_month(self.repo.items[doc.item_id])
        facts: list[dict[str, Any]] = []
        known = [i for i in (supplier.known_ibans if supplier else []) if normalize_iban(i) != normalize_iban(d.iban or "")]
        if known:
            facts.append({"label": "Paid until now", "value": mask_iban(known[0])})
        if d.iban:
            facts.append({"label": "On the new invoice" if changed else "On the invoice", "value": mask_iban(d.iban),
                          "tone": "risk"})
        if d.gross_amount is not None:
            facts.append({"label": "Amount", "value": format_money(d.gross_amount, d.currency)})
        last4 = normalize_iban(d.iban or "")[-4:]
        phone = self.repo.supplier_phones.get(doc.supplier_id or "")
        where = f"on {phone}. That number comes from your earlier invoices, not the new one." if phone else \
            "on a number you already had, not one from this invoice or email."
        month_name = doc_month.name if doc_month else "this"
        title = next((s.owner_line for s in (doc.fraud.signals if doc.fraud else ()) if s.hard_stop),
                     f"Something on {who}'s invoice does not look right.")
        return {
            "id": n.id, "kind": "approval", "tone": "risk", "eyebrow": "Payment on hold", "merchant": who,
            "title": title, "amount": _num(d.gross_amount), "currency": d.currency,
            "date": _iso(d.issue_date or doc.received_at.astimezone(TZ).date()), "companyId": n.company_id,
            "body": (f"The bank account on {month_name}’s invoice is different from the one you have paid before. "
                     f"I have blocked the payment until you confirm it is really {who}.") if changed else
                    (f"I have blocked the payment of {month_name}’s invoice until you confirm it is really {who}."),
            "facts": facts,
            "why": [*n.why, "Changed bank details are a common way invoice fraud happens. I never release these "
                            "without you."],
            "verification": {
                "optionLabel": f"Confirm with {who} by phone",
                "instruction": f"Call {who} {where} Ask them to confirm the account ending in {last4}.",
                "checkboxLabel": f"I called and {who} confirmed the account ending in {last4}.",
                "confirmLabel": "They confirmed it. Release the payment.",
                "confirmOptionId": "confirmed_by_phone",
                "confirmedMessage": "Done. The payment will go to the new account.",
            },
            "keepBlocked": {
                "label": "Keep blocked", "optionId": "keep_blocked",
                "message": f"Done. It stays blocked. I will ask {who} for a corrected invoice.",
            },
        }

    def answer(self, needs_id: str, option_id: str, remember: bool = False) -> dict[str, Any]:
        if not isinstance(option_id, str) or not option_id.strip():
            raise ServiceError(400, "Please pick one of the options.")
        try:
            outcome = self.orchestrator.answer(needs_id, option_id, remember=bool(remember))
        except KeyError:
            raise ServiceError(404, "I can't find that question any more.") from None
        except ValueError:
            raise ServiceError(400, "Please pick one of the options.") from None
        except PermissionError:
            raise ServiceError(409, "This one is already answered.") from None
        result: dict[str, Any] = {"ok": outcome.ok, "message": outcome.message}
        if outcome.learned:
            result["learned"] = outcome.learned
        if outcome.resolved_ids:
            result["alsoResolved"] = list(outcome.resolved_ids)
        return result

    # ----------------------------------------------------------------- Activity

    def activity(self) -> dict[str, Any]:
        items = []
        for a in sorted(self.repo.activity, key=lambda a: (a.at, a.id), reverse=True):
            entry: dict[str, Any] = {"id": a.id, "at": a.at.isoformat(), "kind": a.kind, "text": a.text}
            name = self._company_name(a.company_id)
            if name:
                entry["companyName"] = name
            if a.amount is not None:
                entry["amount"] = _num(a.amount)
                entry["currency"] = a.currency
            items.append(entry)
        return {"today": self._today().isoformat(), "items": items}

    # ----------------------------------------------------------------- Ask (§39)

    def ask(self, question: str) -> dict[str, Any]:
        if not isinstance(question, str) or not question.strip():
            raise ServiceError(400, "Ask me about a supplier, a payment or a month.")
        q = fold(question)
        for handler in (self._ask_supplier, self._ask_accountant, self._ask_subscriptions, self._ask_attention,
                        self._ask_amount, self._ask_month):
            found = handler(q)
            if found is not None:
                self.orchestrator.log("ask", "answer_question", response={"question": question,
                                                                          "evidence": [e["id"] for e in found["evidence"]]})
                return found
        return {"answer": ANSWER_FALLBACK, "evidence": []}

    def _tx_evidence(self, rec: TxRecord) -> dict[str, str]:
        account = self.repo.accounts.get(rec.tx.account_id)
        how = {"direct_debit": "Direct debit", "card": "Card payment", "transfer_out": "Transfer",
               "transfer_in": "Transfer in", "fee": "Bank charge"}.get(rec.tx.kind.value, "Payment")
        where = f" · {account.bank}" if account else ""
        return {"label": f"{how} {day_month(rec.tx.booked_on, self._today())} · "
                         f"{format_money(abs(rec.tx.amount), rec.tx.currency)}{where}", "id": rec.evidence_id}

    @staticmethod
    def _doc_evidence(doc: DocumentRecord) -> dict[str, str]:
        return {"label": f"{display_name(doc.document.supplier_name)} · {doc.label}", "id": doc.evidence_ids[0]}

    def _ask_supplier(self, q: str) -> dict[str, Any] | None:
        repo = self.repo
        supplier = None
        for s in sorted(repo.suppliers.values(), key=lambda s: s.id):
            names = [s.name, *s.aliases]
            if any(re.search(rf"\b{re.escape(fold(n).split()[0])}\b", q) for n in names if n.strip()):
                supplier = s
                break
        if supplier is None:
            return None
        who = display_name(supplier.name)
        resolver = repo.resolver()
        payments = sorted((r for r in repo.transactions.values()
                           if resolver.resolve_transaction(r.tx).supplier is not None
                           and resolver.resolve_transaction(r.tx).supplier.id == supplier.id and r.tx.amount < 0),
                          key=lambda r: (r.tx.booked_on, r.id), reverse=True)
        evidence: list[dict[str, str]] = []
        parts: list[str] = []
        if payments:
            rec = payments[0]
            when = day_month(rec.tx.booked_on, self._today())
            amount = format_money(abs(rec.tx.amount), rec.tx.currency)
            month = Month.of(rec.tx.booked_on).name
            if rec.document_ids:
                doc = repo.documents[rec.document_ids[0]]
                parts.append(f"For {month}, yes: {amount} on {when}, matched to {_doc_phrase(doc)}.")
                evidence += [self._doc_evidence(doc), self._tx_evidence(rec)]
            else:
                chase = repo.chases.get(rec.id)
                tail = (f" I asked {who} for the invoice on {day_month(chase.sent_at.astimezone(TZ).date(), self._today())}."
                        if chase else " I'm still looking for its invoice.")
                parts.append(f"Yes: {amount} on {when}.{tail}")
                evidence.append(self._tx_evidence(rec))
        else:
            parts.append(f"I can't see a payment to {who} yet.")
        for n in self._open_needs():
            if n.kind == "approval" and repo.documents[n.subject_id].supplier_id == supplier.id:
                doc = repo.documents[n.subject_id]
                m = repo.item_month(repo.items[doc.item_id])
                parts.append(f"{m.name if m else 'The next'}’s payment is on hold because the bank details on the new "
                             "invoice changed. I need you to confirm them.")
                evidence.append({"label": f"{m.name if m else 'Next'} payment on hold", "id": f"needs:{n.id}"})
                evidence.append(self._doc_evidence(doc))
        return {"answer": " ".join(parts), "evidence": evidence}

    def _ask_accountant(self, q: str) -> dict[str, Any] | None:
        if "accountant" not in q and "contabil" not in q:
            return None
        questions = sorted(self.repo.accountant_questions.values(), key=lambda x: (x.asked_at, x.id))
        if not questions:
            return {"answer": "Your accountant has not asked anything this month.", "evidence": []}
        answered = [x for x in questions if x.status == "answered"]
        waiting = [x for x in questions if x.status != "answered"]
        n = len(questions)
        head = "One question" if n == 1 else f"{_count_word(n).capitalize()} questions"
        parts = [f"{head}, {_count_word(len(answered))} answered." if waiting else f"{head}, all answered."]
        evidence = []
        for x in answered:
            parts.append(f"“{x.text}” {x.answer}")
            evidence.append({"label": f"Accountant question · {day_month(x.asked_at.astimezone(TZ).date(), self._today())}",
                             "id": x.evidence_id})
            evidence += [{"label": "Proof used in the answer", "id": e} for e in x.answer_evidence_ids[:2]]
        for x in waiting:
            parts.append(f"Still open: “{x.text}”")
            evidence.append({"label": "Accountant question", "id": x.evidence_id})
        return {"answer": " ".join(parts), "evidence": evidence}

    def _ask_subscriptions(self, q: str) -> dict[str, Any] | None:
        if not any(w in q for w in ("subscription", "increase", "went up", "price", "more expensive")):
            return None
        changes = self.orchestrator.price_changes()
        if not changes:
            return {"answer": "None of your regular costs went up in the last three months.", "evidence": []}
        lines = [f"{name}: {format_money(before)} → {format_money(after)}" for name, before, after, _ in changes]
        count = "One went up" if len(changes) == 1 else f"{len(changes)} went up"
        evidence = [{"label": f"{name} · {format_money(after)}", "id": ev[0]} for name, _, after, ev in changes if ev]
        return {"answer": f"{count} in the last three months. " + ". ".join(lines) + ".", "evidence": evidence}

    def _ask_attention(self, q: str) -> dict[str, Any] | None:
        if not any(w in q for w in ("attention", "need", "to do", "todo", "waiting", "pending")):
            return None
        needs = self._open_needs()
        stale = self._stale_connectors()
        if not needs and not stale:
            return {"answer": "Nothing. Everything is under control.", "evidence": []}
        parts, evidence = [], []
        for n in needs:
            if n.kind == "choice":
                rec = self.repo.transactions[n.subject_id]
                who = self.orchestrator.merchant_name(rec.tx)
                amount = format_money(abs(rec.tx.amount), rec.tx.currency)
                parts.append(f"{_article(who).capitalize()} {who} payment of {amount}: I need to know which company "
                             "it belongs to.")
                evidence.append({"label": f"{who} · {amount}", "id": f"needs:{n.id}"})
            else:
                doc = self.repo.documents[n.subject_id]
                who = display_name(doc.document.supplier_name)
                parts.append(f"{who}’s payment is on hold until you confirm their new bank details.")
                evidence.append({"label": f"{who} · payment on hold", "id": f"needs:{n.id}"})
        for c in stale:
            parts.append(f"{c.name} needs reconnecting.")
        count = len(needs) + len(stale)
        head = "One thing." if count == 1 else f"{_count_word(count).capitalize()} things."
        return {"answer": " ".join([head, *parts]), "evidence": evidence}

    def _ask_amount(self, q: str) -> dict[str, Any] | None:
        m = re.search(r"(?:€\s?(\d[\d.,]*)|(\d[\d.,]*)\s?(?:€|eur|euro))", q) or (
            re.search(r"\b(\d{2,}(?:[.,]\d{2})?)\b", q) if any(w in q for w in ("payment", "invoice", "transfer")) else None)
        if m is None:
            return None
        raw = next(g for g in m.groups() if g)
        raw = raw.rstrip(".,")
        try:
            amount = Decimal(raw.replace(",", "")) if re.fullmatch(r"\d{1,3}(,\d{3})*(\.\d+)?", raw) else \
                Decimal(raw.replace(".", "").replace(",", ".")) if "," in raw else Decimal(raw)
        except ArithmeticError:
            return None
        matches = sorted((r for r in self.repo.transactions.values() if abs(r.tx.amount) == amount),
                         key=lambda r: (r.tx.booked_on, r.id), reverse=True)
        if not matches:
            return {"answer": f"I can't find a payment of {format_money(amount)}.", "evidence": []}
        rec = matches[0]
        who = self.orchestrator.merchant_name(rec.tx)
        when = day_month(rec.tx.booked_on, self._today())
        month = Month.of(rec.tx.booked_on)
        evidence = [self._tx_evidence(rec)]
        if rec.document_ids:
            doc = self.repo.documents[rec.document_ids[0]]
            evidence.insert(0, self._doc_evidence(doc))
            checks = [f"the {r.split(':')[0].lower()}" for r in rec.match_why[:3]]
            agreed = ", ".join(checks[:-1]) + f" and {checks[-1]}" if len(checks) > 1 else "".join(checks)
            answer = (f"The {format_money(amount)} payment on {when} went to {who}. Its {_doc_phrase(doc)} "
                      f"arrived on {day_month(doc.received_at.astimezone(TZ).date(), self._today())}. "
                      f"{agreed[:1].upper()}{agreed[1:]} all agree.")
        else:
            answer = f"The {format_money(amount)} payment on {when} went to {who}. " + \
                self.orchestrator.missing.plan(rec)
        evidence.append({"label": f"{self._company_name(rec.company_id)} · {month.name}",
                         "id": f"month:{rec.company_id}:{month}"})
        return {"answer": answer, "evidence": evidence}

    def _ask_month(self, q: str) -> dict[str, Any] | None:
        named = next((i for i, name in enumerate(_MONTHS) if name in q), None)
        if named is None and not any(w in q for w in ("complete", "closed", "close", "month", "done", "finished")):
            return None
        current = self._current_month()
        month = current if named is None else Month(current.year if named + 1 <= current.month + 1 else current.year - 1,
                                                    named + 1)
        parts, evidence = [], []
        statuses = {c: self._status(c, month) for c in self.repo.companies}
        all_closed = all(s.closed for s in statuses.values())
        parts.append(f"Yes. {month.name} is closed for every company." if all_closed else
                     ("Almost." if any(s.closed for s in statuses.values()) or
                      min(s.percent_closed for s in statuses.values()) >= 60 else "Not yet."))
        for c, s in statuses.items():
            name = self._company_name(c)
            evidence.append({"label": f"{name} · {month.name} {'closed' if s.closed else f'{s.percent_closed}%'}",
                             "id": f"month:{c}:{month}"})
            if all_closed:
                continue
            if s.closed:
                parts.append(f"{name} is closed.")
            elif s.needs_you:
                parts.append(f"{name} needs {'one answer' if s.needs_you == 1 else f'{s.needs_you} answers'} from you.")
                evidence += [{"label": f"{name} · question", "id": f"needs:{n.id}"} for n in self._open_needs(c)
                             if self.repo.item_month(self.repo.items[n.item_id]) == month]
            else:
                reason = s.reasons()[0] if s.reasons() else ""
                parts.append(f"{name} is {s.percent_closed}% done. {reason}".strip())
        return {"answer": " ".join(parts), "evidence": evidence}

    # ----------------------------------------------------------------- Evidence in

    def upload_evidence(self, filename: str | None, content_type: str | None, data: bytes) -> dict[str, Any]:
        if not data:
            raise ServiceError(400, "There was nothing to save.")
        if len(data) > 25 * 1024 * 1024:
            raise ServiceError(413, "This file is too large to send.")
        report = self.orchestrator.ingest_file(data, filename=filename, content_type=content_type,
                                               source_kind=SourceKind.UPLOAD, origin="upload")
        return self._report(report)

    def _report(self, report) -> dict[str, Any]:  # type: ignore[no-untyped-def]
        repo = self.repo
        message = report.message
        docs = []
        for doc_id in dict.fromkeys(report.document_ids):
            doc = repo.documents[doc_id]
            matched = [repo.transactions[t] for t in doc.matched_tx_ids]
            docs.append({"id": doc.id, "label": doc.label, "supplier": display_name(doc.document.supplier_name),
                         "verified": doc.document.quality.value == "verified", "matched": bool(matched),
                         "evidenceIds": list(doc.evidence_ids)})
            if matched and not doc.on_hold and not report.already_known:
                rec = matched[0]
                message = (f"Got it. It matches the {format_money(abs(rec.tx.amount), rec.tx.currency)} payment to "
                           f"{self.orchestrator.merchant_name(rec.tx)} on {day_month(rec.tx.booked_on, self._today())}.")
        if report.obligation_ids:
            message = "Got it. I added the deadline from this letter and will check the payment."
        if report.transaction_ids:
            n = len(report.transaction_ids)
            message = f"Got it. I added {n} bank {'transaction' if n == 1 else 'transactions'}."
        if report.route == "unsupported":
            raise ServiceError(415, report.message)
        return {"ok": True, "message": message, "evidenceIds": list(dict.fromkeys(report.evidence_ids)),
                "documents": docs, "transactions": list(report.transaction_ids),
                "pendingLinks": list(report.pending_links), "storedOnly": bool(report.stored_only and not docs)}

    def upload_receipt(self, sha256: str, data: bytes, *, filename: str | None = None,
                       content_type: str | None = None, client_item_id: str | None = None,
                       source: str = "mobile_scan", captured_at: str | None = None) -> dict[str, Any]:
        """Offline mobile upload (§43): the phone deletes its copy only when our hash equals its hash."""
        try:
            source_kind = SourceKind(source)
        except ValueError:
            raise ServiceError(400, "Unknown source.") from None
        when = None
        if captured_at:
            try:
                when = datetime.fromisoformat(captured_at)
            except ValueError:
                raise ServiceError(400, "captured_at must be an ISO 8601 time with offset.") from None
        request = UploadRequest(
            tenant_id=self.repo.tenant_id, client_upload_id=client_item_id or f"rcpt-{(sha256 or '')[:40]}",
            sha256=sha256 or "", data=data, content_type=content_type, filename=filename, captured_at=when,
            source_kind=source_kind)
        receipt, report = self.orchestrator.receive_upload(request)
        body = receipt.as_dict()
        body["duplicate"] = receipt.status.value == "duplicate"
        if report is not None:
            body["message"] = self._report(report)["message"]
        if receipt.status.value == "hash_mismatch":
            body["error"] = "hash_mismatch"
            raise _Reply(422, body)
        if receipt.status.value == "rejected":
            raise _Reply(400, body)
        return body

    def share(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise ServiceError(400, "There was nothing to save.")
        kind = str(payload.get("kind") or ("url" if payload.get("url") else "file" if payload.get("dataBase64") or
                                           payload.get("data_base64") else "text"))
        source_app = payload.get("sourceApp") or payload.get("source_app")
        if kind == "url":
            share = SharePayload.for_url(str(payload.get("url") or ""), source_app=source_app)
        elif kind == "text":
            share = SharePayload.for_text(str(payload.get("text") or ""), source_app=source_app)
        elif kind == "file":
            data = _b64(payload.get("dataBase64") or payload.get("data_base64"))
            share = SharePayload.for_file(data, filename=payload.get("filename"),
                                          mime_type=payload.get("mimeType") or payload.get("mime_type"),
                                          source_app=source_app, is_screenshot=bool(payload.get("isScreenshot")))
        else:
            raise ServiceError(400, "I can take a link, some text or a file.")
        report = self.orchestrator.share(share)
        return self._report(report)

    # ----------------------------------------------------------------- Accountant (§28)

    def accountant_clients(self) -> dict[str, Any]:
        month = self._current_month()
        rows = []
        for company_id in self.repo.companies:
            rows.append(self._client_row(company_id, month))
        rows.sort(key=lambda r: (r["complete"], r["name"]))
        return {"clients": rows}

    def _client_row(self, company_id: str, month: Month) -> dict[str, Any]:
        status = self._status(company_id, month)
        waiting = [q for q in self.repo.accountant_questions.values()
                   if q.company_id == company_id and q.status != "answered"]
        return {"id": company_id, "name": self.repo.legal_names.get(company_id, company_id), "month": month.name,
                "complete": status.percent_closed, "missing": status.missing_documents, "needsAccountant": len(waiting)}

    def accountant_client(self, company_id: str) -> dict[str, Any]:
        if company_id not in self.repo.companies:
            raise ServiceError(404, "I can't find that client.")
        repo = self.repo
        month = self._current_month()
        row = self._client_row(company_id, month)
        status = self._status(company_id, month)
        items = repo.items_for(company_id, month)
        txs = [repo.transactions[i.subject_id] for i in items if i.subject_type == "transaction"]
        docs = [i for i in items if i.subject_type == "document"]
        matched = sum(1 for r in txs if r.document_ids or r.proof_evidence_ids)
        anomalies = [{"id": f"an_{n.id}", "title": f"{display_name(repo.documents[n.subject_id].document.supplier_name)}"
                      " bank details changed", "detail": "Payment blocked until the owner confirms by phone.",
                      "tone": "risk"} for n in self._open_needs(company_id) if n.kind == "approval"]
        for name, before, after, _ in self.orchestrator.price_changes():
            if any(self.orchestrator.merchant_name(r.tx) == name and r.company_id == company_id for r in txs):
                pct = ((after - before) * 100 / before).quantize(Decimal("1"))
                anomalies.append({"id": f"an_price_{name.lower()}", "title": f"{name} price went up {pct}%",
                                  "detail": f"{format_money(before)} → {format_money(after)}.", "tone": "attention"})
        questions = [{"id": q.id, "question": q.text, "status": "answered" if q.status == "answered" else "waiting",
                      **({"answer": q.answer} if q.answer else {})}
                     for q in sorted(repo.accountant_questions.values(), key=lambda q: q.id) if q.company_id == company_id]
        done = status.counts.done
        total = status.counts.total
        software = repo.accountant.software if repo.accountant else "your software"
        export = ({"state": "ready", "ready": total, "total": total,
                   "note": f"{month.name} is complete and ready to export to {software}."} if status.closed else
                  {"state": "partial", "ready": done, "total": total,
                   "note": f"{done} of {total} items are ready for {software}. The rest will follow when they close."})
        return {**row, "taxId": repo.companies[company_id].tax_id, "software": software,
                "evidence": [{"label": "Transactions", "value": str(len(txs))},
                             {"label": "Matched with a document", "value": str(matched)},
                             {"label": "Documents collected", "value": str(len(docs))},
                             {"label": "Still missing", "value": str(status.missing_documents)}],
                "anomalies": anomalies, "taxFlags": self._tax_flags(company_id, txs), "questions": questions,
                "exportState": export}

    def _tax_flags(self, company_id: str, txs: list[TxRecord]) -> list[dict[str, str]]:
        flags = []
        for rec in txs:
            for doc_id in rec.document_ids:
                doc = self.repo.documents[doc_id].document
                if doc.doc_type is DocumentType.INVOICE_RECEIPT and doc.vat_amount == 0 and \
                        doc.supplier_tax_id and doc.supplier_tax_id[:1] in "123":
                    flags.append({"id": f"t_{doc_id}",
                                  "title": f"Rent paid to a private landlord · {format_money(doc.gross_amount or 0)}",
                                  "detail": "No withholding shown on the receipt. Whether it applies is your call."})
        return flags

    def accountant_rule(self, text: str, scope: str = "client") -> dict[str, Any]:
        if not isinstance(text, str) or not text.strip():
            raise ServiceError(400, "Write the rule in one sentence, e.g. “Treat all Adobe subscriptions as Software”.")
        try:
            rule, affected = self.orchestrator.accountant_rule(text, scope or "client")
        except ValueError as exc:
            raise ServiceError(400, str(exc)) from None
        except PermissionError:
            raise ServiceError(409, "No accountant is connected yet.") from None
        noun = "payment" if affected == 1 else "payments"
        return {"ok": True, "rule": {"id": rule.id, "label": rule.label, "scope": rule.scope.value},
                "affected": affected, "message": f"Done. {rule.label}. It applies to {affected} {noun} so far."}

    # ----------------------------------------------------------------- Free business audit (§60) + audit log (§55)

    def audit(self) -> dict[str, Any]:
        repo = self.repo
        today = self._today()
        start = today - timedelta(days=90)
        repo.resolver()
        txs = [*repo.history_transactions, *(r.tx for r in repo.transactions.values())]
        own = {normalize_iban(i) for e in repo.entities for i in e.own_ibans}
        expenses = [t for t in txs if t.amount < 0 and start <= t.booked_on < today
                    and normalize_iban(t.counterparty_iban or "") not in own]
        total = sum((abs(t.amount) for t in expenses), Decimal(0))
        by_supplier: dict[str, Decimal] = {}
        for t in expenses:
            by_supplier[self.orchestrator.merchant_name(t)] = by_supplier.get(self.orchestrator.merchant_name(t),
                                                                              Decimal(0)) + abs(t.amount)
        top = sorted(by_supplier.items(), key=lambda kv: (-kv[1], kv[0]))[:3]
        origins = {"email": "in your email", "link": "behind links in emails", "upload": "uploaded",
                   "scan": "photographed on your phone", "share": "shared from your phone"}
        doc_counts: dict[str, int] = {}
        for d in repo.documents.values():
            doc_counts[d.origin] = doc_counts.get(d.origin, 0) + 1
        series = [s for s in learn_from_transactions([t for t in txs if t.amount < 0])]
        missing = [r for r in repo.transactions.values() if r.decision is not None and r.decision.requires_document
                   and not r.document_ids and not r.proof_evidence_ids and not r.private]
        changes = self.orchestrator.price_changes()
        other = [n for n in self._open_needs() if n.kind == "choice"]
        report = render_business_audit(BusinessAuditFindings(
            period_start=start, period_end=today - timedelta(days=1), expenses=len(expenses), expenses_total=total,
            documents_found=len(repo.documents), recurring_subscriptions=len(series), missing_evidence=len(missing),
            other_company_items=len(other),
            increases=tuple(PriceIncrease(name=n, before=b, after=a) for n, b, a, _ in changes)))
        findings = [
            {"id": "f_expenses", "value": format_money(total).split(".")[0], "label": "of expenses", "tone": "neutral",
             "examples": [f"{name}: {format_money(amount)}" for name, amount in top]},
            {"id": "f_docs", "value": str(len(repo.documents)), "label": "documents", "tone": "neutral",
             "examples": [f"{n} {origins.get(o, o)}" for o, n in sorted(doc_counts.items(), key=lambda kv: -kv[1])]},
            {"id": "f_subs", "value": str(len(series)), "label": "recurring costs", "tone": "neutral",
             "examples": [", ".join(sorted({display_name(s.display_name) for s in series}))] if series else []},
            {"id": "f_missing", "value": str(len(missing)), "label": "payments still missing a document",
             "tone": "attention" if missing else "good",
             "examples": [f"{self.orchestrator.merchant_name(r.tx)} {format_money(abs(r.tx.amount))} on "
                          f"{day_month(r.tx.booked_on, today)}" for r in missing[:3]]},
            {"id": "f_increased", "value": str(len(changes)), "label": "costs went up",
             "tone": "attention" if changes else "good",
             "examples": [f"{n} {format_money(b)} → {format_money(a)}" for n, b, a, _ in changes]},
            {"id": "f_other", "value": str(len(other)), "label": "items may belong to another company",
             "tone": "attention" if other else "good",
             "examples": [f"{self.orchestrator.merchant_name(repo.transactions[n.subject_id].tx)} "
                          f"{format_money(abs(repo.transactions[n.subject_id].tx.amount))} paid with "
                          f"{repo.accounts[repo.transactions[n.subject_id].tx.account_id].label}" for n in other]},
        ]
        chain = repo.audit.verify(repo.tenant_id)
        names = [e.name for e in repo.entities]
        company = names[0] if len(names) == 1 else ", ".join(names[:-1]) + f" and {names[-1]}"
        return {"companyName": company, "periodLabel": "the last 90 days", "headline": report.headline,
                "lines": list(report.lines), "callToAction": report.call_to_action, "findings": findings,
                "log": {"records": chain.checked, "intact": chain.ok, "head": chain.head_hash}}

    # ----------------------------------------------------------------- routing

    def dispatch(self, method: str, path: str, body_json: Any = None) -> tuple[int, dict[str, Any]]:
        """Route one request. ``body_json`` may be a dict, a JSON string, bytes or None.

        Returns ``(status, json)``; errors come back as ``{"error": ..., "message": ...}``
        with a plain message, never a stack trace (§48, §70).
        """
        method = (method or "GET").upper()
        raw_path, _, query = (path or "/").partition("?")
        path = unquote(raw_path).rstrip("/") or "/"
        try:
            body = _body(body_json)
            if not body and query:
                from urllib.parse import parse_qsl

                body = dict(parse_qsl(query))
            for verb, pattern, handler in self._routes():
                m = pattern.fullmatch(path)
                if m is None:
                    continue
                if verb != method:
                    continue
                result = handler(body, *m.groups())
                if result is None:
                    return 404, {"error": "not_found", "message": "I can't find that."}
                return 200, result
            if any(p.fullmatch(path) for _, p, _ in self._routes()):
                return 405, {"error": "method_not_allowed", "message": "That is not something I can do here."}
            return 404, {"error": "not_found", "message": "I can't find that."}
        except _Reply as reply:
            return reply.status, reply.body
        except ServiceError as exc:
            codes = {400: "bad_request", 404: "not_found", 409: "conflict", 413: "too_large", 415: "unsupported"}
            return exc.status, {"error": codes.get(exc.status, "error"), "message": exc.message}

    def _routes(self):  # type: ignore[no-untyped-def]
        r = re.compile
        seg = r"([^/]+)"
        return (
            ("GET", r("/healthz"), lambda b: {"ok": True}),
            ("GET", r("/api/home"), lambda b: self.home()),
            ("GET", r("/api/needs-you"), lambda b: self.needs_you()),
            ("POST", r(f"/api/needs-you/{seg}/answer"),
             lambda b, i: self.answer(i, _field(b, "option_id", "optionId"), bool(b.get("remember", False)))),
            ("GET", r("/api/activity"), lambda b: self.activity()),
            ("GET", r("/api/companies"), lambda b: self.companies()),
            ("GET", r(f"/api/companies/{seg}"), lambda b, i: self.company(i)),
            ("GET", r(f"/api/months/{seg}/{seg}"), lambda b, c, m: self.month(c, m)),
            ("POST", r("/api/ask"), lambda b: self.ask(_field(b, "question"))),
            ("POST", r("/api/evidence"),
             lambda b: self.upload_evidence(b.get("filename"), b.get("contentType") or b.get("content_type"),
                                            _b64(b.get("dataBase64") or b.get("data_base64")))),
            ("POST", r("/api/evidence/upload"),
             lambda b: self.upload_receipt(_field(b, "sha256"), _b64(b.get("dataBase64") or b.get("data_base64")),
                                           filename=b.get("filename"),
                                           content_type=b.get("contentType") or b.get("content_type"),
                                           client_item_id=b.get("client_item_id") or b.get("clientItemId"),
                                           source=b.get("source") or "mobile_scan",
                                           captured_at=b.get("captured_at") or b.get("capturedAt"))),
            ("POST", r("/api/receipts"),
             lambda b: self.upload_receipt(_field(b, "sha256"), _b64(b.get("dataBase64") or b.get("data_base64")),
                                           filename=b.get("filename"),
                                           content_type=b.get("contentType") or b.get("content_type"),
                                           client_item_id=b.get("client_item_id") or b.get("clientItemId"),
                                           source=b.get("source") or "mobile_scan",
                                           captured_at=b.get("captured_at") or b.get("capturedAt"))),
            ("POST", r("/api/share"), lambda b: self.share(b)),
            ("GET", r("/api/sources"), lambda b: self.sources()),
            ("POST", r("/api/chat"), lambda b: self.chat(b or {})),
            ("POST", r(f"/api/chat/outbox/{seg}/send"), lambda b, mid: self.chat_send(mid, b)),
            ("GET", r(f"/api/reports/{seg}/file"), lambda b, rid: self.report_file(rid)),
            ("GET", r("/api/documents"), lambda b: self.documents_list(b)),
            ("POST", r("/api/documents/export"), lambda b: self.documents_export(b)),
            ("GET", r(f"/api/documents/{seg}/file"), lambda b, did: self.document_download(did)),
            ("GET", r("/api/settings/report"), lambda b: self.report_settings()),
            ("POST", r("/api/settings/report"), lambda b: self.report_settings(b or {})),
            ("GET", r("/api/accountant/api-keys"), lambda b: self.api_keys()),
            ("POST", r("/api/accountant/api-keys"), lambda b: self.api_key_create(b)),
            ("POST", r(f"/api/accountant/api-keys/{seg}/revoke"), lambda b, kid: self.api_key_revoke(kid)),
            ("POST", r("/api/sources"), lambda b: self.add_source(b or {})),
            ("POST", r(f"/api/sources/{seg}/remove"), lambda b, sid: self.remove_source(sid)),
            ("GET", r("/api/connections"), lambda b: self.connections()),
            ("POST", r(f"/api/connections/{seg}/stale"), lambda b, i: self.mark_connection_stale(i)),
            ("POST", r(f"/api/connections/{seg}/reconnect"), lambda b, i: self.reconnect(i)),
            ("GET", r("/api/accountant/clients"), lambda b: self.accountant_clients()),
            ("GET", r(f"/api/accountant/clients/{seg}"), lambda b, i: self.accountant_client(i)),
            ("POST", r("/api/accountant/rules"),
             lambda b: self.accountant_rule(_field(b, "text"), str(b.get("scope") or "client"))),
            ("GET", r("/api/audit"), lambda b: self.audit()),
        )


class _Reply(Exception):
    """A complete non-200 reply (e.g. the §43 hash-mismatch receipt)."""

    def __init__(self, status: int, body: dict[str, Any]) -> None:
        super().__init__(status)
        self.status = status
        self.body = body


_NUMBER_WORDS = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten")


def _count_word(n: int) -> str:
    return _NUMBER_WORDS[n] if 0 <= n < len(_NUMBER_WORDS) else str(n)


def _article(word: str) -> str:
    return "an" if word[:1].lower() in "aeiou" else "a"


def _doc_phrase(doc: DocumentRecord) -> str:
    """'invoice FT VF2026/1183' / 'receipt FS ALF2026/118834' (kind in words, number as printed)."""
    kind = doc.label.split(" · ")[0]
    number = doc.document.invoice_number or ""
    words = kind[: -len(number)].strip() if number and kind.endswith(number) else kind
    return f"{words.lower()} {number}".strip()


def _body(body_json: Any) -> dict[str, Any]:
    if body_json is None or body_json == "" or body_json == b"":
        return {}
    if isinstance(body_json, (bytes, bytearray)):
        body_json = bytes(body_json).decode("utf-8", errors="replace")
    if isinstance(body_json, str):
        try:
            body_json = json.loads(body_json)
        except json.JSONDecodeError:
            raise ServiceError(400, "I couldn't read that request.") from None
    if not isinstance(body_json, Mapping):
        raise ServiceError(400, "I couldn't read that request.")
    return dict(body_json)


def _field(body: Mapping[str, Any], *names: str) -> str:
    for name in names:
        value = body.get(name)
        if isinstance(value, str) and value.strip():
            return value
    raise ServiceError(400, f"Missing {names[0]}.")


def _b64(value: Any) -> bytes:
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if not isinstance(value, str) or not value:
        raise ServiceError(400, "There was nothing to save.")
    try:
        return base64.b64decode(value, validate=False)
    except (binascii.Error, ValueError):
        raise ServiceError(400, "I couldn't read that file.") from None
