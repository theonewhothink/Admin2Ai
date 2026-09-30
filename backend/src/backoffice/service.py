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
from collections.abc import Collection, Mapping, Sequence
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal
from typing import Any
from urllib.parse import unquote

from pydantic import ValidationError

from backoffice.closure import BlockerKind, BusinessAuditFindings, Month, PriceIncrease, due_soon
from backoffice.closure import AccountantQuestion as ClosureQuestion
from backoffice.closure import (
    PackageEntry,
    QuestionDirection,
    QuestionStatus,
    TaxFlag,
    build_client_view,
    render_business_audit,
)
from backoffice.closure import needs_accountant as closure_needs_accountant
from backoffice.domain.cost_centers import CostCenter, CostCenterIdentifiers, SplitError, to_cents
from backoffice.domain.lifecycle import Stage
from backoffice.domain.models import DocumentType, ObligationKind, SourceKind
from backoffice.fx_differences import accountant_flag as fx_accountant_flag
from backoffice.countries.foreign import vat_rates as foreign_vat_rates
from backoffice.evidence import IntegrityError, ObjectNotFound, SharePayload, UploadRequest
from backoffice.fraud import SignalKind, mask_iban, normalize_iban
from backoffice.fraud.engine import COUNTRY_NAMES
from backoffice.fraud.iban import is_valid_iban
from backoffice.verification import RateFit, check_rate
from backoffice.verification._display import percent
from backoffice.language import connector_problem, greeting, since_phrase, status_headline
from backoffice.learning import (
    Answer,
    OptionKind,
    day_month,
    display_name,
    format_money,
    join_and,
    learn_from_transactions,
    suggest_rule_from_answer,
)
from backoffice.learning.plain import count_phrase
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
from backoffice.billing import DEMO_PLAN, HOLDABLE_PATHS, PAID_PLANS, PLANS, BillingState, Usage, plan_for
from backoffice.line_prices import threshold_words, unit_money
from backoffice.purchases import CAPITAL_ASSET_FLAG, possible_capital_asset

__all__ = ["BackOfficeService", "ServiceError"]

BANK_CONSENT_DAYS = 180  # PSD2 access consent (RTS Art. 10, as amended 2022): renew at most every 180 days
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_SLUG = re.compile(r"[^a-z0-9]+")
# Needs-You questions shown as one plain choice with the engine's own prompt and options (§37).
_PLAIN_QUESTIONS = ("company", "cash", "obligation", "refund", "obligation_company", "statement", "recharge",
                    "part", "deposit", "deposit_refund", "deposit_kept", "chargeback",
                    "receipt", "expense_claim", "retake", "same_document",
                    "lease", "till", "member")  # leasing (X24), till reports (X5), receipts lists (X12)
# Open payments proven without a document: their plan still shows in the month (I7, X9).
_PLANNED_RULES = frozenset({"chargeback", "chargeback_won", "security_deposit"})
_ISSUER_NAMES = {"tax_authority": "Tax office", "social_security": "Social Security", "bank": "Your bank",
                 "landlord": "Your landlord", "insurer": "Your insurer", "municipality": "Your municipality",
                 "grant_agency": "Grant agency"}
_STAGE_WORDS = {Stage.DISCOVERED: "Found", Stage.ACQUIRED: "Received", Stage.UNDERSTOOD: "Read",
                Stage.VERIFIED: "Checked", Stage.MATCHED: "Matched", Stage.ACTED: "Done", Stage.CONFIRMED: "Confirmed",
                Stage.CLOSED: "Closed", Stage.NEEDS_OWNER: "Waiting for you", Stage.CONFLICT: "Details disagree",
                Stage.NOT_REQUIRED: "Nothing needed"}


# How the accountant sees a payment's reconciliation (closure.PackageEntry.status): label and tone.
_RECON_STATUS = {
    "closed": ("Matched", "good"),
    "not_required": ("No document needed", "good"),
    "conflict": ("Sources disagree", "risk"),
    "waiting_for_owner": ("Waiting for the owner", "attention"),
    "open": ("Looking for the document", "neutral"),
}
_EVIDENCE_KIND = {
    "bank_transaction": "payment", "card_transaction": "payment", "email": "email", "eml": "email",
    "government_notice": "letter", "pdf": "document", "image": "document", "screenshot": "document",
    "ubl": "document", "xml": "document", "text": "document", "html": "document", "qr": "document",
}
_EVIDENCE_WORD = {"payment": "Bank record", "email": "Email", "letter": "Letter", "document": "Original"}
_SOURCE_WORD = {
    "email": "Email", "bank": "Bank feed", "card": "Card feed", "supplier_portal": "Supplier website",
    "accounting_system": "Accounting software", "cloud_storage": "Cloud storage", "upload": "Uploaded",
    "mobile_scan": "Phone scan", "mobile_share": "Shared from a phone", "government": "Tax office",
    "accountant": "Accountant",
}


def _evidence_word(fmt: str, filename: str | None) -> str:
    """A plain name for a piece of evidence nothing else labels: its file name, else what it is."""
    return filename or _EVIDENCE_WORD.get(_EVIDENCE_KIND.get(fmt, ""), "File")


def _accountant_id(address: str) -> str:
    """The same accountant (one email address) has one id, whichever companies they look after."""
    return "acct-" + (_SLUG.sub("-", address.lower()).strip("-")[:40] or "x")


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
        # Sends the emails the owner confirms (backoffice.mailer); the demo's simulated outbox by default,
        # None for a real business until the server hands it one. Without it nothing counts as sent.
        self.mailer: Any = getattr(orchestrator, "transport", None)
        self.sign_in: dict[str, dict[str, Any]] = {}
        # A real business (production): a connection covers a period only once it has actually been
        # synced (§47, "never green without it"). The demo's connections are simulated and cover
        # their 90 days at once.
        self.real_sources = False
        # What each synced connection remembers between runs (connectors.base.ConnectorState as JSON).
        self.sync_states: dict[str, dict[str, Any]] = {}
        # Sensitive documents' originals read during the current request (§52): the production server records
        # them as an event with who read them (server/runtime.py); a service used directly (the demo) records
        # them at once, as read by ``viewer``.
        self._opened: list[str] = []
        self.inline_access_log = True
        self.viewer: tuple[str, str] = ("owner", "owner")
        # The plan and what the payment provider said (backoffice.billing). ``decides_holds``: this service decides
        # itself whether new evidence waits for the plan (the demo, tests); the production server decides before
        # recording each event and says so in it (server/runtime.py), so a replay follows the record.
        self.billing = BillingState()
        self.decides_holds = True

    @classmethod
    def demo(cls) -> BackOfficeService:
        """Laura's three companies with September 2026 processed by the real pipeline."""
        from backoffice.demo import build_demo

        svc = cls(build_demo())
        svc.billing = BillingState(plan="demo")  # the demo is never billed and never calls a payment provider
        return svc

    @classmethod
    def new_tenant(cls, tenant_id: str, *, owner_name: str, owner_email: str, now: datetime,
                   vault: Any = None, authorizer: Any = None) -> BackOfficeService:
        """An empty real business: the owner, no companies yet, the clock at ``now``.

        Companies come with :meth:`add_company`; everything else is learned
        from the evidence that arrives (§4 zero configuration).
        """
        from backoffice.orchestrator import OwnerProfile, Repository

        full_name = " ".join(str(owner_name or "").split())[:120]
        email = str(owner_email or "").strip().lower()
        if not full_name:
            raise ServiceError(400, "What is your name?")
        if not _EMAIL.match(email):
            raise ServiceError(400, "That doesn't look like an email address.")
        owner = OwnerProfile(first_name=full_name.split()[0], full_name=full_name, email=email)
        repo = Repository(tenant_id=tenant_id, owner=owner, now=now)
        svc = cls(Orchestrator(repo), vault=vault, authorizer=authorizer)
        svc.real_sources = True
        # Starts on Free; what arrived before today is the free audit's history and never counts (§60).
        svc.billing = BillingState(plan="free", started_on=now.astimezone(TZ).date())
        # A business that signs up goes through onboarding: its first run learns before it asks (§5), and the
        # time to first value and the owner's set-up time are measured from now (§58, §59).
        svc.orchestrator.begin_onboarding(now)
        svc.orchestrator.setup_step("account")
        return svc

    def add_company(self, name: Any, tax_id: Any, legal_name: Any = None, address: Any = None,
                    sector: Any = None, country: Any = None) -> dict[str, Any]:
        """Add one of the owner's companies, in its own country (Portugal unless ``country`` says otherwise,
        e.g. "ES"). Its tax number is checked by that country's pack (§49): a NIF in Portugal, a NIF, NIE or
        CIF in Spain; every later document, VAT amount and letter of the company goes through that pack.

        ``address`` (its postal address) and ``sector`` (its line of business, in plain words) are optional:
        they help tell which company an invoice is for (H3) and which purchases are clearly personal (X23).
        """
        from backoffice.countries import UnknownCountryError, company_pack

        try:
            pack = company_pack(str(country or "PT"))
        except UnknownCountryError:
            raise ServiceError(400, "I can't set up a company in that country yet. Portugal and Spain are "
                                    "supported.") from None
        name = " ".join(str(name or "").split())
        if not name:
            raise ServiceError(400, "What is the company called?")
        if len(name) > 120:
            raise ServiceError(400, "That name is too long.")
        legal = " ".join(str(legal_name or "").split()) or name
        if len(legal) > 160:
            raise ServiceError(400, "That legal name is too long.")
        nif = ""
        if tax_id not in (None, ""):
            check = pack.validate_tax_id(str(tax_id))
            if not check.valid or not check.normalized:
                raise ServiceError(400, check.message or "That tax number doesn't look right. Please check it.")
            nif = check.normalized
            if any(e.tax_id == nif and e.country == pack.country_code for e in self.repo.companies.values()):
                raise ServiceError(409, "That company is already here.")
        base = _SLUG.sub("-", name.lower()).strip("-")[:40] or "company"
        company_id, n = base, 2
        while company_id in self.repo.companies:
            company_id, n = f"{base}-{n}", n + 1
        self.repo.add_company(id=company_id, name=name, legal_name=legal, tax_id=nif,
                              address=" ".join(str(address or "").split())[:200] or None,
                              sector=" ".join(str(sector or "").split())[:80] or None, country=pack.country_code)
        self.orchestrator.milestone("company_added")
        self.orchestrator.setup_step("company", entity_id=company_id)
        # Sources that read for every company (mailboxes) now cover this one too; the business's
        # accountant looks after it until it gets its own (§28).
        for c in self.repo.connectors.values():
            if c.kind == "email" and company_id not in c.company_ids:
                c.company_ids = (*c.company_ids, company_id)
        self._sync_accountant_connectors()
        added: dict[str, Any] = {"tax_id": nif or None}
        if pack.country_code != "PT":  # recorded only for another country: Portuguese entries stay as they were
            added["country"] = pack.country_code
        self.orchestrator.log("entity", "company_added", subject_id=company_id, values=added,
                              actor=f"owner:{self.repo.owner.email}")
        self.orchestrator.run()
        self.billing_check()  # a company over the plan's allowance: the owner is told, nothing is refused
        return {"ok": True, "company": self.company(company_id), "message": f"Done. {name} is set up."}

    def set_accountant(self, email: Any, name: Any = None, software: Any = None, company_id: Any = None,
                       firm: Any = None) -> dict[str, Any]:
        """Name the accountant who receives the monthly package (§5 step 5, §28).

        Without ``company_id`` it is the business's accountant, used by every company that
        has none of its own. With ``company_id`` that company gets its own accountant (§51).
        """
        from backoffice.orchestrator import AccountantProfile

        company = self._company(company_id, required=False)
        address = str(email or "").strip().lower()
        if not _EMAIL.match(address) or len(address) > 254:
            raise ServiceError(400, "That doesn't look like an email address.")
        person = " ".join(str(name or "").split())[:120]
        tool = " ".join(str(software or "").split())[:60] or "your accountant's software"
        local, _, domain = address.partition("@")
        office = " ".join(str(firm or "").split())[:120] or person or domain
        profile = AccountantProfile(id=_accountant_id(address), firm=office, person=person or local, email=address,
                                    software=tool)
        if company is None:
            self.repo.accountant = profile
        else:
            self.repo.company_accountants[company] = profile
        self._sync_accountant_connectors(fresh=address)
        self.orchestrator.setup_step("accountant", entity_id=company)
        self.orchestrator.log("accountant", "accountant_set", subject_id=company or "accountant",
                              values={"company": company} if company else None,
                              actor=f"owner:{self.repo.owner.email}")
        self._authorize_delivery(company)
        self.orchestrator.run()
        out: dict[str, Any] = {"ok": True, "accountant": {"email": address, "name": person or None, "software": tool,
                                                          "firm": office, "companyId": company}}
        if company is None:
            out["message"] = f"Done. I will send the closed months to {address}."
        else:
            out["message"] = f"Done. I will send {self._company_name(company)}’s closed months to {address}."
        return out

    def clear_company_accountant(self, company_id: Any) -> dict[str, Any]:
        """The company goes back to the business's accountant (or to none)."""
        company = self._company(company_id)
        assert company is not None
        if self.repo.company_accountants.pop(company, None) is None:
            raise ServiceError(404, "This company already uses the business's accountant.")
        self._sync_accountant_connectors()
        self.orchestrator.log("accountant", "accountant_cleared", subject_id=company, values={"company": company},
                              actor=f"owner:{self.repo.owner.email}")
        self.orchestrator.run()
        name = self._company_name(company)
        default = self.repo.accountant
        return {"ok": True, "message": f"Done. {name} uses {default.email} again." if default else
                f"Done. {name} has no accountant now."}

    def accountant_settings(self, body: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """GET/POST ``/api/settings/accountant``: the business's accountant and each company's (§28, §51).

        POST ``{email, name?, firm?, software?, companyId?}`` sets one; ``{companyId, useDefault: true}``
        puts a company back on the business's accountant.
        """
        message = None
        if body:
            if body.get("useDefault") and body.get("companyId"):
                message = self.clear_company_accountant(body.get("companyId"))["message"]
            else:
                message = self.set_accountant(body.get("email"), body.get("name"), body.get("software"),
                                              body.get("companyId"), body.get("firm"))["message"]
        repo = self.repo

        def card(a: Any) -> dict[str, Any] | None:
            return None if a is None else {"email": a.email, "name": a.person, "firm": a.firm, "software": a.software}

        out: dict[str, Any] = {
            "default": card(repo.accountant),
            "companies": [{"companyId": c, "companyName": e.name, "accountant": card(repo.accountant_for(c)),
                           "own": c in repo.company_accountants} for c, e in repo.companies.items()],
        }
        if message:
            out["ok"], out["message"] = True, message
        return out

    def _sync_accountant_connectors(self, *, fresh: str | None = None) -> None:
        """One "accountant" source per accountant, covering the companies they look after.

        The business's accountant keeps the id "accountant"; a company's own accountant with
        another address gets "accountant-<address>". ``fresh`` (an address that was just set)
        starts that source's coverage now.
        """
        repo = self.repo
        now = self._now()
        served: dict[str, list[str]] = {}
        for company_id in repo.companies:
            a = repo.accountant_for(company_id)
            if a is not None:
                served.setdefault(a.email.lower(), []).append(company_id)
        wanted: dict[str, tuple[Any, list[str]]] = {}
        default = repo.accountant.email.lower() if repo.accountant is not None else None
        if repo.accountant is not None:
            wanted["accountant"] = (repo.accountant, served.get(default or "", []))
        for a in repo.company_accountants.values():
            key = a.email.lower()
            if key != default:
                wanted.setdefault("accountant-" + (_SLUG.sub("-", key).strip("-")[:40] or "x"),
                                  (a, served.get(key, [])))
        for cid in [c.id for c in repo.connectors.values() if c.kind == "accountant" and c.id not in wanted]:
            repo.connectors.pop(cid)
        for cid, (a, companies) in wanted.items():
            old = repo.connectors.get(cid)
            if old is not None and old.account == a.email and a.email != fresh:
                old.name, old.company_ids = a.firm, tuple(companies)
                continue
            repo.connectors.pop(cid, None)
            repo.add_connector(ConnectorState(
                id=cid, name=a.firm, kind="accountant", account=a.email, company_ids=tuple(companies),
                healthy=True, covered_from=now, covered_until=now, last_synced_at=now))

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
            if record.informational and due.overdue:
                continue  # it renewed on its own: nothing was ever due from the owner
            amount = f"{format_money(due.amount, due.currency)} · " if due.amount is not None else ""
            reference = f"reference {record.reference}. " if record.reference else ""
            tone = "risk" if due.overdue else ("attention" if due.days_left <= 5 else "neutral")
            if record.payable:
                step = self.orchestrator.obligations.payment_step(record)
                note = f"{amount}{reference}{step}".replace(" · reference", ", reference")
            else:
                note = self.orchestrator.obligations.next_step(record)
                tone = "neutral" if record.informational else tone
            items.append({
                "id": f"due_{due.obligation_id}", "title": due.title,
                "companyName": self._company_name(due.entity_id) or "", "due": _iso(due.due_on),
                "note": note, "tone": tone,
            })
        staged = self.orchestrator.staged
        for held in sorted(self.repo.retentions.values(), key=lambda r: r.document_id):
            # Money held back by a customer, due back soon (or overdue): still owed to you (checklist X8).
            record = self.repo.documents.get(held.document_id)
            if held.status != "held" or held.direction != "in" or held.until is None or record is None \
                    or (held.until - today).days > 21:
                continue
            words = staged.invoice_words(record)
            items.append({
                "id": f"due_held_{record.id}", "title": f"Amount held back by {held.party}",
                "companyName": self._company_name(staged.company_of(record)) or "", "due": _iso(held.until),
                "note": f"{words[:1].upper()}{words[1:]}: {staged.held_sentence(held)}",
                "tone": "attention" if held.until < today else "neutral",
            })
        for record in sorted(self.repo.documents.values(), key=lambda d: d.id):
            # An unpaid invoice with a due date it states (F8): due soon, or overdue in plain words.
            found = self.orchestrator.invoice_due(record, today)
            if found is None or found["days"] > 21:
                continue
            who = display_name(record.document.supplier_name)
            items.append({
                "id": f"due_{record.id}", "title": f"{who} invoice",
                "companyName": self._company_name(self.repo.item_company(self.repo.items[record.item_id])) or "",
                "due": _iso(found["due"]), "note": found["line"],
                "tone": "risk" if found["overdue"] else ("attention" if found["days"] <= 5 else "neutral"),
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
            ("h_supplier", sum(1 for a in recent if a.kind == "chased" and a.tag != "staff"),
             "supplier email handled", "supplier emails handled"),
            ("h_staff", sum(1 for a in recent if a.kind == "chased" and a.tag == "staff"),
             "receipt asked from your team", "receipts asked from your team"),
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
                if self.real_sources:
                    entry.update(self._reconnect_state(c))
            out.append(entry)
        return {"connections": out}

    def _reconnect_state(self, c: ConnectorState) -> dict[str, Any]:
        """Production only: what reconnecting this connection takes, and whether it is already under way."""
        info = self.sign_in.get(c.id) or {}
        provider = info.get("provider")
        out: dict[str, Any] = {}
        if c.kind == "email" and provider in ("google", "microsoft"):
            out["action"] = f"Sign in to {c.name} again"
        elif c.kind in ("email", "bank"):
            out["action"] = "Try again"
        waiting_for_owner = (self.sync_states.get(c.id) or {}).get("reconnect_required")
        if info.get("reconnecting") and not info.get("pending") and not waiting_for_owner:
            out["reconnect"] = "catching_up"  # signed in again (or retried): shown as connected once a sync works
        return out

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
        if self.real_sources:
            return self._reconnect_real(c)
        # The demo's connections are simulated: reconnecting one brings it straight back.
        c.healthy = True
        c.covered_until = c.last_synced_at = self._now()
        self._clear_reconnect(c.id)
        self.orchestrator.log("closure", "connector_reconnected", subject_id=c.id)
        self.orchestrator.run()
        return {"ok": True, "connection": self._connection(c.id)}

    def _connection(self, connection_id: str) -> dict[str, Any]:
        return next(x for x in self.connections()["connections"] if x["id"] == connection_id)

    def _reconnect_real(self, c: ConnectorState) -> dict[str, Any]:
        """A real connection comes back only through a new sign-in or a sync that works again (§47–48).

        Google and Microsoft mailboxes: the provider's sign-in address, for the owner to sign in again.
        Other mailboxes and banks: the saved sign-in is tried again. Either way the connection stays
        "needs reconnecting" until a sync has actually succeeded; nothing here says it is back.
        """
        info = dict(self.sign_in.get(c.id) or {})
        provider = info.get("provider")
        if c.kind == "email" and provider in ("google", "microsoft"):
            label = "Google" if provider == "google" else "Microsoft"
            url = None
            if self.authorizer is not None:
                try:
                    url = self.authorizer.begin(provider, self.repo.tenant_id, c.id, login_hint=c.account)
                except Exception:
                    url = None
            if not url:
                raise ServiceError(503, f"{label} sign-in is not set up on this server yet, so I can't reconnect "
                                        f"{c.account} from here.")
            self.sign_in[c.id] = {**info, "pending": True, "reconnecting": True}
            self.orchestrator.log("closure", "connector_sign_in_started", subject_id=c.id)
            return {"ok": True, "authorizeUrl": url, "message": f"Sign in to {c.name} again to reconnect {c.account}.",
                    "connection": self._connection(c.id)}
        if c.kind not in ("email", "bank"):
            raise ServiceError(409, "This connection does not need reconnecting.")
        self._clear_reconnect(c.id)
        self.sign_in[c.id] = {**info, "reconnecting": True}
        self.orchestrator.log("closure", "connector_retry_requested", subject_id=c.id)
        if c.kind == "email":
            message = (f"I'll try {c.account} again with its saved app password. It shows as connected once it has "
                       "synced. If the password changed, remove the mailbox in Sources and add it again.")
        else:
            message = (f"I'll try {c.name} again. It shows as connected once it has synced. If your bank asks you "
                       "to approve access again, link it again in Sources.")
        return {"ok": True, "message": message, "connection": self._connection(c.id)}

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
        holders = self.orchestrator.staff.holder_of
        cards = [
            {"id": a.id, "name": f"Card •••• {a.card_last4}", "company": names.get(a.holder_id, ""),
             "detail": a.bank + ("" if a.owned else " · personal card used for business") +
             (f" · {h.name}'s card" if (h := holders(a.card_last4)) is not None else ""),
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
        self.orchestrator.setup_step(f"source:{kind}")
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
        simulated = not pending and not self.real_sources
        self.repo.add_connector(ConnectorState(
            id=cid, name=name, kind="email", account=address, company_ids=company_ids, healthy=not pending,
            covered_from=now - timedelta(days=90) if simulated else None,
            covered_until=now if simulated else None, last_synced_at=now if simulated else None))
        self.sign_in[cid] = {"provider": provider, "stored": secret_stored, "pending": pending}
        if simulated:
            self.orchestrator.milestone("email_connected")
            self.orchestrator.activity(now, "checked", f"Connected {address} and read the last 90 days.",
                                       company if company else None)
        elif not pending:
            self.orchestrator.milestone("email_connected")
            if secret_stored:  # it can read now: the first run waits for its 90 days (§6)
                self.orchestrator.connection_reading(cid)
            self.orchestrator.activity(now, "checked", f"Connected {address}. I'm reading the last 90 days now.",
                                       company if company else None)
        out: dict[str, Any] = {"id": cid, "message": "Almost done. Finish signing in." if pending else
                               f"Done. I now read {address}."}
        if authorize_url:
            out["authorizeUrl"] = authorize_url
        return out

    def _add_bank(self, body: Mapping[str, Any], *, consent_until: date | None = None) -> dict[str, Any]:
        """A bank account. ``consent_until``: the owner authorised access at the bank (PSD2 consent)."""
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
            simulated = not self.real_sources
            self.repo.add_connector(ConnectorState(
                id=cid, name=bank, kind="bank", account=entity.name, company_ids=(company,), healthy=True,
                covered_from=now - timedelta(days=90) if simulated else None,
                covered_until=now if simulated else None, last_synced_at=now if simulated else None))
        if not self.real_sources:
            until = (now + timedelta(days=BANK_CONSENT_DAYS)).date()
            self.sign_in[aid] = self.sign_in[cid] = {"provider": "open_banking", "consent_until": until}
            self.orchestrator.milestone("bank_connected")
            self.orchestrator.activity(now, "checked", f"Connected {bank} for {entity.name} and imported 90 days.",
                                       company)
            return {"id": aid, "message": f"Done. {bank} is connected for {entity.name}."}
        if consent_until is None:  # added by hand: payments arrive once the bank is linked (or by export)
            self.sign_in[aid] = {"provider": "open_banking", "pending": True}
            self.sign_in.setdefault(cid, {"provider": "open_banking", "pending": True})
            return {"id": aid, "message": f"Done. I added {bank} for {entity.name}. Link it to your bank so I can "
                                          "import its payments, or send me a bank export."}
        self.sign_in[aid] = self.sign_in[cid] = {"provider": "open_banking", "consent_until": consent_until}
        self._clear_reconnect(cid)
        self.orchestrator.milestone("bank_connected")
        if not (self.sync_states.get(cid) or {}).get("last_successful_sync"):
            self.orchestrator.connection_reading(cid)  # the first run waits for its 90 days (§6)
        self.orchestrator.activity(now, "checked", f"Connected {bank} for {entity.name}. I'm importing the last "
                                   "90 days now.", company)
        return {"id": aid, "message": f"Done. {bank} is connected for {entity.name}."}

    def link_bank(self, bank: str, company_id: str, ibans: Sequence[str], consent_until: date) -> dict[str, Any]:
        """The owner authorised access at the bank (GoCardless, PSD2): its accounts join the company."""
        ids: list[str] = []
        for iban in list(ibans) or [""]:
            try:
                out = self._add_bank({"bank": bank, "companyId": company_id, "iban": iban},
                                     consent_until=consent_until)
            except ServiceError as exc:
                if exc.status != 409:  # an account already connected is fine
                    raise
                existing = next((a for a in self.repo.accounts.values() if iban and a.iban == normalize_iban(iban)),
                                None)
                if existing is None:
                    continue
                out = {"id": existing.id}
                self.sign_in[existing.id] = {"provider": "open_banking", "consent_until": consent_until}
            ids.append(out["id"])
        cid = next((c.id for c in self.repo.connectors.values() if c.kind == "bank" and c.name == bank), None)
        if cid is not None:
            self.sign_in[cid] = {"provider": "open_banking", "consent_until": consent_until}
        self.orchestrator.setup_step("bank", entity_id=company_id if company_id in self.repo.companies else None)
        self.orchestrator.log("discovery", "bank_linked", subject_id=cid, values={"accounts": len(ids)})
        self.orchestrator.run()
        return {"ok": True, "ids": ids, "connectionId": cid}

    # ----------------------------------------------------------------- synced imports (production worker)

    def _clear_reconnect(self, connection_id: str) -> None:
        state = self.sync_states.get(connection_id)
        if state and state.get("reconnect_required"):
            self.sync_states[connection_id] = {**state, "reconnect_required": False, "consecutive_failures": 0}

    def _synced(self, c: ConnectorState, state: Mapping[str, Any]) -> int | None:
        """Coverage from a real sync (connectors.base.ConnectorState as JSON).

        Returns how many days the first sync read (None for later syncs).
        """
        first = not (self.sync_states.get(c.id) or {}).get("last_successful_sync")
        self.sync_states[c.id] = dict(state)

        def when(key: str) -> datetime | None:
            value = state.get(key)
            return datetime.fromisoformat(value) if isinstance(value, str) and value else None

        start, end, last = when("coverage_start"), when("coverage_end"), when("last_successful_sync")
        gaps = [datetime.fromisoformat(g["end"]) for g in state.get("known_gaps") or [] if isinstance(g, Mapping)]
        if gaps and start is not None:  # a known hole: only what follows it is covered (never assumed complete)
            start = max([start, *gaps])
        c.healthy = not state.get("reconnect_required")
        c.covered_from, c.covered_until = start, end
        c.last_synced_at = last or c.last_synced_at
        info = self.sign_in.get(c.id) or {}
        if c.healthy and info.get("reconnecting"):  # the first sync after reconnecting worked: it is back
            self.sign_in[c.id] = {k: v for k, v in info.items() if k != "reconnecting"}
            self.orchestrator.activity(self._now(), "checked", f"{c.name} is connected again: {c.account} synced.")
        expires = when("auth_expires_at")
        if c.kind == "bank" and expires is not None:
            self.sign_in[c.id] = {**self.sign_in.get(c.id, {}), "provider": "open_banking",
                                  "consent_until": expires.astimezone(TZ).date(), "pending": False}
        if not first:
            return None
        self.orchestrator.connection_read(c.id)  # its first 90 days are in (§6)
        return max(1, round((end - start).total_seconds() / 86400)) if start and end else 90

    def sync_mail(self, connection_id: str, messages: Sequence[bytes], state: Mapping[str, Any] | None,
                  *, held: bool = False) -> dict[str, Any]:
        """Messages read from a connected mailbox, then (last batch) what the mailbox sync remembers.

        ``held``: the server decided these messages wait for the plan (backoffice.billing): they are kept and read
        once it covers them; the mailbox's own sync goes on as usual."""
        c = self.repo.connectors.get(connection_id)
        if c is None or c.kind != "email":
            raise ServiceError(404, "I can't find that mailbox.")
        if messages and (held or (self.decides_holds and self.intake_held())):
            self._hold({"kind": "mail", "connection": connection_id, "messages": [bytes(m) for m in messages]})
            messages = ()  # kept, waiting for the plan: read as soon as it covers them (backoffice.billing)
        for raw in messages:
            self.orchestrator.ingest_file(raw, filename="message.eml", content_type="message/rfc822",
                                          source_kind=SourceKind.EMAIL, origin="email")
        self.billing_check()
        if state is not None:
            days = self._synced(c, state)
            if days is not None:
                self.orchestrator.activity(self._now(), "checked", f"Read {c.account}: the last {days} days are in.")
            self.orchestrator.log("discovery", "connector_synced", subject_id=c.id,
                                  values={"messages": len(messages)})
            self.orchestrator.run()
        return {"ok": True, "messages": len(messages)}

    def sync_bank(self, connection_id: str, rows: Sequence[Any], state: Mapping[str, Any] | None) -> dict[str, Any]:
        """Booked transactions from a linked bank (open banking), then what the bank sync remembers."""
        c = self.repo.connectors.get(connection_id)
        if c is None or c.kind != "bank":
            raise ServiceError(404, "I can't find that bank connection.")
        known = [r for r in rows if r.account_id in self.repo.accounts]
        if known:
            self.orchestrator.ingest_bank(known)
        if state is not None:
            days = self._synced(c, state)
            if days is not None:
                self.orchestrator.activity(self._now(), "checked", f"Imported the last {days} days from {c.name}.")
            self.orchestrator.log("discovery", "connector_synced", subject_id=c.id, values={"rows": len(known)})
            self.orchestrator.run()
        return {"ok": True, "rows": len(known)}

    def follow_links(self, urls: Sequence[str]) -> dict[str, Any]:
        """Invoice links that were waiting, followed again (production: opened before the event was recorded)."""
        report = self.orchestrator.follow_waiting(list(urls))
        return {"ok": True, "documents": list(dict.fromkeys(report.document_ids)),
                "waiting": list(report.pending_links)}

    def links_waiting(self) -> list[str]:
        """Invoice links not opened yet, or whose site did not answer: the sync worker tries them again."""
        return list(self.repo.pending_links)

    def sync_failed(self, connection_id: str, state: Mapping[str, Any], *, reconnect: bool) -> dict[str, Any]:
        """A sync failed. Only the owner can fix a refused sign-in: the connection needs reconnecting (§47–48)."""
        c = self.repo.connectors.get(connection_id)
        if c is None:
            raise ServiceError(404, "I can't find that connection.")
        self.sync_states[c.id] = dict(state)
        if reconnect:
            self.orchestrator.connection_read(c.id)  # it will not finish its first read on its own: don't wait
        if reconnect and c.healthy:
            return self.mark_connection_stale(c.id, since=c.last_synced_at or self._now())
        self.orchestrator.log("discovery", "connector_failed", subject_id=c.id, values={"reconnect": reconnect})
        return {"ok": True}

    def _add_card(self, body: Mapping[str, Any]) -> dict[str, Any]:
        last4 = self._text(body, "last4", "The last 4 digits of the card.", limit=4)
        if not re.fullmatch(r"\d{4}", last4):
            raise ServiceError(400, "Enter exactly the last 4 digits.")
        if any(a.card_last4 == last4 for a in self.repo.accounts.values()):
            raise ServiceError(409, "That card is already here.")
        bank = self._text(body, "bank", "Which bank issued the card?")
        company = self._company(body.get("companyId"))
        held = bool(body.get("holderName") or body.get("holderEmail"))  # an employee's card (backoffice.staff)
        if held:
            self.staff_views.holder(body)  # checked before anything changes
        aid = self._slug("card", last4)
        self.repo.add_account(Account(id=aid, bank=bank, holder_id=company, card_last4=last4,
                                      owned=not bool(body.get("personal"))))
        if held:
            return {"id": aid, "message": self.staff_views.card_holder(aid, last4, body)}
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
            return RuleBrain(self.assistant).handle(message, history)
        except ValueError as exc:
            raise ServiceError(400, str(exc)) from None
        except Exception:
            if brain is not None:  # the model is unavailable: fall back to the rules
                return RuleBrain(self.assistant).handle(message, history)
            raise

    def pipeline(self) -> dict[str, Any]:
        """What the system is doing: every item's journey along the golden rule (Diagram page)."""
        from backoffice.pipeline import build_pipeline

        return build_pipeline(self)

    def internal(self, view: str, body: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """The team's internal dashboard (backoffice/internal.py); admin-only once sign-in exists."""
        from backoffice import internal

        return internal.handle(view, [self], body)

    def chat_tools(self) -> dict[str, Any]:
        """What the browser chat needs to run Claude itself: instructions, tools, today and the model."""
        from backoffice.assistant import SYSTEM, TOOLS, ClaudeBrain

        return {"system": SYSTEM, "tools": TOOLS, "today": self._today().isoformat(), "model": ClaudeBrain.MODEL}

    def chat_tool(self, body: Mapping[str, Any]) -> dict[str, Any]:
        """Run one tool for the browser chat. Tool failures come back as results the model can recover from."""
        from backoffice.assistant import TOOLS, run_tool

        name = body.get("name")
        args = body.get("input") if isinstance(body.get("input"), Mapping) else {}
        if not isinstance(name, str) or name not in {t["name"] for t in TOOLS}:
            raise ServiceError(400, "Unknown tool.")
        cards: list[dict[str, Any]] = []
        try:
            result = run_tool(self.assistant, name, dict(args), cards)
        except ServiceError as exc:
            return {"isError": True, "result": exc.message, "cards": cards}
        except (ValueError, KeyError, TypeError) as exc:
            return {"isError": True, "result": str(exc)[:500] or "That did not work.", "cards": cards}
        return {"isError": False, "result": json.loads(json.dumps(result, default=str)), "cards": cards}

    def task_create(self, body: Mapping[str, Any]) -> dict[str, Any]:
        due_raw = body.get("due")
        try:
            due = date.fromisoformat(str(due_raw)) if due_raw else None
        except ValueError:
            raise ServiceError(400, "The date must look like 2026-10-09.") from None
        try:
            task = self.assistant.add_task(str(body.get("title") or ""), due,
                                           str(body.get("companyId") or body.get("company_id") or ""))
        except ValueError as exc:
            raise ServiceError(400, str(exc)) from None
        return {"task": task, "tasks": self.assistant.list_tasks()}

    def task_done(self, task_id: str) -> dict[str, Any]:
        try:
            task = self.assistant.complete_task(task_id)
        except KeyError:
            raise ServiceError(404, "I can't find that task.") from None
        return {"task": task, "tasks": self.assistant.list_tasks()}

    def chat_send(self, message_id: str, body: Mapping[str, Any] | None = None) -> dict[str, Any]:
        op = self.assistant
        msg = op.outbox.get(message_id)
        if msg is None:
            raise ServiceError(404, "I can't find that email.")
        if isinstance(body, Mapping) and body.get("cancel"):
            if msg.status in ("draft", "waiting"):
                msg.status = "cancelled"
            if msg.status == "sent":
                return {"ok": True, "status": msg.status, "message": "It was already sent."}
            return {"ok": True, "status": msg.status, "message": "Cancelled. Nothing was sent."}
        msg = op.send(message_id)
        return {"ok": True, "status": msg.status, "message": msg.delivery}

    # ----------------------------------------------------------------- the send path (production: one event per email)

    def waiting_messages(self) -> list[str]:
        """Emails written or confirmed but not yet accepted by a mailer, oldest first. An email the back office
        wrote on a permission the owner switched off since is held back (§25), so it is not among them."""
        ids = [m.id for m in self.orchestrator.sendable_messages()]
        op = getattr(self, "_assistant", None)
        if op is not None:
            ids += [m.id for m in op.outbox.values() if m.status == "waiting"]
        return ids

    def send_waiting(self, message_id: str) -> dict[str, Any]:
        """Send one waiting email through ``self.mailer`` now. Raises when the mailer refuses it (it stays waiting)."""
        if self.mailer is None:
            raise ServiceError(409, "Email sending is not set up here.")
        if message_id in self.repo.outbox:
            sent = self.orchestrator.send_waiting(message_id, self.mailer)
        else:
            op = self.assistant
            msg = op.outbox.get(message_id)
            if msg is None:
                raise ServiceError(404, "I can't find that email.")
            sent = msg.status == "waiting" and op.send(message_id).status == "sent"
        return {"ok": True, "sent": bool(sent)}

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

    def _visible_documents(self, companies: Collection[str] | None) -> set[str] | None:
        """Ids of the documents whose company is one of ``companies`` (None: every document)."""
        if companies is None:
            return None
        return {d.id for d in self.repo.documents.values() if self._document_company(d) in companies}

    def documents_list(self, body: Mapping[str, Any] | None = None, *,
                       companies: Collection[str] | None = None) -> dict[str, Any]:
        b = body or {}
        company = str(b.get("company", ""))
        if companies is not None and company and company not in companies:
            raise ServiceError(404, "I can't find that client.")
        items = self.assistant.documents(query=str(b.get("q", "")), supplier=str(b.get("supplier", "")),
                                         company_id=company,
                                         date_from=self._date_arg(b, "from"), date_to=self._date_arg(b, "to"))
        visible = self._visible_documents(companies)
        if visible is not None:
            items = [i for i in items if i["id"] in visible]
        return {"items": items, "companies": [{"id": c, "name": e.name} for c, e in self.repo.companies.items()
                                              if companies is None or c in companies],
                "total": len(items)}

    def document_detail(self, document_id: str) -> dict[str, Any]:
        """One document: what it is, its proof and what it is matched to; for a supplier's account statement,
        each of its lines checked against your records (§28, §54). Never changes anything."""
        rec = self.repo.documents.get(document_id)
        base = self.document(document_id)
        if rec is None or base is None:
            raise ServiceError(404, "I can't find that document.")
        # Its lifecycle, the credit notes that correct it and the refund chain (§20), then the list's fields.
        detail = {**{k: v for k, v in base.items() if k != "status"}, "stage": base["status"],
                  **self.assistant.document_item(rec), "label": rec.label, "evidence": [self._doc_evidence(rec)],
                  "payments": base["payments"],
                  "supportsPayments": [self._tx_evidence(self.repo.transactions[t]) for t in rec.supports_tx_ids
                                       if t in self.repo.transactions],
                  "why": list(rec.reasons)}
        sr = self.repo.statements.get(document_id)
        if sr is not None:
            detail["statement"] = self._statement_view(sr)
        return detail

    def _statement_view(self, sr: Any) -> dict[str, Any]:
        """A supplier's account statement, line by line against your records, in plain words."""
        from backoffice.supplier_statements import not_listed_line, summary

        repo = self.repo
        record = repo.documents[sr.document_id]
        st = sr.statement
        who = display_name(record.document.supplier_name)
        today = self._today()
        cur = st.currency
        view: dict[str, Any] = {
            **st.as_dict(), "supplier": who, "supplierKnown": sr.supplier_id is not None, "booked": False,
            "note": "An account statement is never booked and never pays for anything. I use it to check your "
                    "records against the supplier's.",
        }
        check = sr.check
        if check is None:
            view.update({"complete": False, "missing": [], "differences": [], "notOnStatement": [],
                         "paymentsNotFound": [], "balance": None, "request": None, "needsYouId": None,
                         "summary": ("I couldn't tell which supplier this statement is from, so I haven't checked "
                                     "it. Add the supplier and I will." if sr.supplier_id is None else
                                     "I haven't checked it yet.")})
            return view

        def money(value: Any) -> str:
            return format_money(value if value is not None else Decimal("0"), cur)

        answered = set(sr.answered)
        printed = {x["row"]: x for x in st.as_dict()["lines"]}
        from backoffice.orchestrator import statement_line_key

        rows = []
        for r in check.rows:
            line = r.line
            word = line.word[:1].upper() + line.word[1:]
            name = f"{word} {line.number}" if line.number else word
            if r.status == "matched" and r.transaction_id:
                paid = repo.transactions[r.transaction_id]
                text = f"{name} of {money(line.amount)}: in your bank on {day_month(paid.tx.booked_on, today)}."
            elif r.status == "matched":
                text = f"{name} · {money(line.amount)}: you have it" + (
                    ", found by its amount and date." if r.by_amount else ".")
            elif r.status == "missing":
                text = f"{name} · {money(line.amount)}: you don't have it."
            elif r.status == "differs":
                text = f"{name}: {money(line.amount)} here, {money(r.our_amount)} on the {line.word} you have."
                if statement_line_key(line) in answered:
                    text += (" You said the statement is right." if sr.answer == "statement" else
                             f" You said the {line.word} is right.")
            elif r.status == "not_found":
                text = f"{name} of {money(line.amount)}: I can't find it in your bank."
            else:
                text = line.description or name
            row: dict[str, Any] = {**printed[line.row],
                                   "status": r.status, "text": text,
                                   "evidence": [{"id": e, "label": text} for e in r.evidence_ids[:1]]}
            if r.document_id and r.document_id in repo.documents:
                row["document"] = {"id": r.document_id, "label": repo.documents[r.document_id].label}
            if r.transaction_id and r.transaction_id in repo.transactions:
                row["payment"] = self._tx_evidence(repo.transactions[r.transaction_id])
            rows.append(row)
        request_text = self.orchestrator.statements.request_line(sr)
        message = repo.outbox.get(sr.request_id) if sr.request_id else None
        correction = repo.outbox.get(sr.correction_id) if sr.correction_id else None
        if check.balance_agrees:
            balance_text = f"The balance of {money(check.closing)} matches the invoices you have not paid yet."
        else:
            balance_text = (f"It says you owe {money(check.closing)}; the invoices you have not paid yet come to "
                            f"{money(check.our_open)}." + (f" {check.explained}" if check.explained else ""))
        needs = repo.needs.get(sr.needs_id or "")
        text = summary(check, today)
        if check.missing and request_text:
            text += f" {request_text}"
        view.update({
            "lines": rows, "complete": check.complete, "summary": text,
            "missing": [r for r in rows if r["status"] == "missing"],
            "differences": [r for r in rows if r["status"] == "differs"],
            "paymentsNotFound": [r for r in rows if r["status"] == "not_found"],
            "notOnStatement": [{"id": d.id, "label": d.label, "date": d.on.isoformat() if d.on else None,
                                "amount": _num(d.amount), "evidence": [self._doc_evidence(repo.documents[d.id])]}
                               for d in check.not_on_statement if d.id in repo.documents],
            "notOnStatementText": not_listed_line(check),
            "balance": {"statement": _num(check.closing), "ours": _num(check.our_open),
                        "difference": _num(check.difference), "agrees": check.balance_agrees, "text": balance_text},
            "request": ({"status": "sent" if message.sent else "waiting", "to": message.to, "text": request_text}
                        if message is not None else
                        ({"status": "not_sent", "text": request_text} if request_text else None)),
            "correction": ({"status": "sent" if correction.sent else "waiting", "to": correction.to}
                           if correction is not None else None),
            "needsYouId": needs.id if needs is not None and needs.status == "open" else None,
            "answer": sr.answer,
        })
        return view

    def document_download(self, document_id: str, *, companies: Collection[str] | None = None) -> dict[str, Any]:
        visible = self._visible_documents(companies)
        f = self.assistant.document_file(document_id) if visible is None or document_id in visible else None
        if f is None:
            raise ServiceError(404, "I can't find that document.")
        return {"filename": f[0], "contentType": f[1], "data": base64.b64encode(f[2]).decode()}

    def documents_export(self, body: Mapping[str, Any] | None = None, *,
                         companies: Collection[str] | None = None) -> dict[str, Any]:
        b = body or {}
        if companies is not None and str(b.get("company", "")) not in companies:
            raise ServiceError(403, "Choose one of your clients to export.")
        name, data, count = self.assistant.export_zip(company_id=str(b.get("company", "")),
                                                      date_from=self._date_arg(b, "from"),
                                                      date_to=self._date_arg(b, "to"))
        return {"filename": name, "contentType": "application/zip", "count": count,
                "data": base64.b64encode(data).decode()}

    # ----------------------------------------------------------------- one payment or document, with its "Why?" (§54)

    @staticmethod
    def _history(item) -> list[dict[str, Any]]:  # type: ignore[no-untyped-def]
        return [{"at": t.at.isoformat(), "stage": t.to_stage.value, "label": _STAGE_WORDS.get(t.to_stage, ""),
                 "note": t.note} for t in item.history]

    def _chain(self, **subject: str) -> list[dict[str, Any]]:
        """Invoice -> its payment -> credit note -> refund, when a credit note is involved (§20)."""
        return [{**step, "amount": _num(step["amount"]), "date": _iso(step["date"])}
                for step in self.orchestrator.refund_chain(**subject)]

    def transaction(self, tx_id: str) -> dict[str, Any] | None:
        """One payment: what it needs, its documents, why they match, its history and any refund chain."""
        repo = self.repo
        rec = repo.transactions.get(tx_id)
        if rec is None:
            return None
        item = repo.items[rec.item_id]
        needs_document = rec.decision is not None and rec.decision.requires_document
        out: dict[str, Any] = {
            "id": rec.id, "date": rec.tx.booked_on.isoformat(), "amount": _num(abs(rec.tx.amount)),
            "currency": rec.tx.currency, "direction": "in" if rec.tx.amount > 0 else "out",
            "counterparty": self.orchestrator.merchant_name(rec.tx), "companyId": rec.company_id,
            "companyName": self._company_name(rec.company_id) or "", "status": item.stage.value,
            "statusLabel": _STAGE_WORDS.get(item.stage, ""), "expects": rec.decision.reason if rec.decision else "",
            "documents": [{"id": d.id, "label": d.label, "evidenceIds": list(d.evidence_ids)}
                          for d in (repo.documents[i] for i in rec.document_ids if i in repo.documents)],
            "headline": rec.match_headline, "why": [r.replace(": ", " ", 1) for r in rec.match_why],
            "history": self._history(item), "chain": self._chain(tx_id=rec.id), "evidenceIds": [rec.evidence_id],
        }
        if not item.is_done and needs_document and not rec.document_ids:
            out["nextStep"] = self.orchestrator.missing.plan(rec)
            if rec.tx.amount < 0:  # one tap teaches it for every later payment (J7): POST .../evidence {need}
                out["evidenceChoices"] = [{"need": "none", "label": "It never has an invoice"},
                                          {"need": "invoice", "label": "It always has one"}]
        if rec.notes:  # plain facts worth knowing, never a hold ("paid with a different card than usual", L4)
            out["notes"] = list(rec.notes)
        # Deposits and invoices paid in parts (checklist X8): the deposit, each part, what is held back.
        parts = self._parts(tx_id=rec.id)
        if parts:
            out["parts"] = parts
        deposit = repo.deposits.get(rec.id)
        if deposit is not None:
            out["deposit"] = {"status": deposit.status, "text": self.orchestrator.staged.deposit_text(deposit)}
        disputed = self.orchestrator.chargebacks.text(rec)  # a disputed card payment (I7)
        if disputed is not None:
            out["disputed"] = {"status": repo.chargebacks[rec.id].status, "text": disputed}
            if not item.is_done:
                out["nextStep"] = disputed
        chain = self._import_chain("transaction", rec.id)
        if chain is not None:  # one piece of an import purchase (X28): its order, customs and duties, by reference
            out["importChain"] = chain
        return out

    def _import_chain(self, kind: str, subject_id: str) -> dict[str, Any] | None:
        """The import purchase a document, letter or payment is part of (backoffice.imports), as the details show
        it: "Part of order PO-2026-114: deposit, invoice, freight, customs, duties"."""
        from backoffice.imports import chain_for, chain_view

        chain = chain_for(self.repo, kind, subject_id, text_of=self.orchestrator.evidence_text)
        return chain_view(chain, today=self._today()) if chain is not None else None

    def _parts(self, **subject: str) -> list[dict[str, Any]]:
        """Deposit -> advance invoice -> invoice -> each payment -> the part held back (checklist X8)."""
        return [{**step, "amount": _num(step["amount"]), "date": _iso(step["date"])}
                for step in self.orchestrator.staged.story(**subject)]

    def document(self, document_id: str) -> dict[str, Any] | None:
        """One document: its details, the payments it proves, the credit notes that correct it, its history."""
        repo = self.repo
        record = repo.documents.get(document_id)
        if record is None:
            return None
        d = record.document
        item = repo.items[record.item_id]
        corrects = repo.documents.get(record.credit_for or "")
        out = self._document_view(record, d, item, corrects)
        due = self.orchestrator.invoice_due(record)  # a due date it states (F8)
        if due is not None:
            out.update({"due": _iso(due["due"]), "dueLine": due["line"]})
        staged = self.orchestrator.staged
        parts = self._parts(document_id=d.id)
        if parts:  # paid in parts (checklist X8): what was received, what is still to come, what is held back
            out["parts"] = parts
        left = staged.balance(record)
        if left is not None:
            out["received"] = {"received": _num(staged.received(record)),
                               "total": _num(abs(d.gross_amount or Decimal(0))),
                               "stillToCome": _num(left), "text": staged.waiting_sentence(record)}
        held = repo.retentions.get(d.id)
        if held is not None:
            out["heldBack"] = {"amount": _num(held.amount), "until": _iso(held.until), "status": held.status,
                               "text": staged.held_sentence(held) if held.status == "held" else
                               f"The {format_money(held.amount, held.currency)} held back was paid."}
        chain = self._import_chain("document", d.id)
        if chain is not None:  # one piece of an import purchase (X28)
            out["importChain"] = chain
        return out

    def _document_view(self, record: DocumentRecord, d: Any, item: Any, corrects: DocumentRecord | None
                       ) -> dict[str, Any]:
        repo = self.repo
        return {
            "id": d.id, "label": record.label, "supplier": display_name(d.supplier_name), "number": d.invoice_number or "",
            "type": d.doc_type.value.replace("_", " "), "date": _iso(d.issue_date or record.received_at.date()),
            "amount": _num(abs(d.gross_amount) if d.gross_amount is not None else None), "currency": d.currency,
            "companyId": d.entity_id or "", "companyName": self._company_name(d.entity_id) or "",
            "status": item.stage.value, "statusLabel": _STAGE_WORDS.get(item.stage, ""),
            "waiting": record.hold_reason,
            "corrects": ({"id": corrects.id, "label": corrects.label.split(" · ")[0]} if corrects else None),
            "creditNotes": [{"id": n.id, "label": n.label} for n in self.orchestrator.credits_for(d.id)],
            "payments": [self._tx_evidence(repo.transactions[t]) | {"transactionId": t}
                         for t in record.matched_tx_ids if t in repo.transactions],
            "history": self._history(item), "chain": self._chain(document_id=d.id),
            "evidenceIds": list(record.evidence_ids),
        }

    def evidence_need(self, tx_id: str, body: Mapping[str, Any] | None) -> dict[str, Any]:
        """``POST /api/transactions/{id}/evidence`` ``{need: "none" | "invoice"}``: the owner says this payment's
        counterparty never has an invoice, or always needs one (J7). Remembered for every later payment to it."""
        rec = self.repo.transactions.get(tx_id)
        if rec is None:
            raise ServiceError(404, "I can't find that payment.")
        need = str((body or {}).get("need") or "").strip().lower()
        if need not in ("none", "invoice"):
            raise ServiceError(400, "Tell me if it never has an invoice or always needs one.")
        if rec.tx.amount >= 0:
            raise ServiceError(400, "That is money in: there is no invoice to wait for from them.")
        if self.repo.items[rec.item_id].is_done or rec.document_ids:
            raise ServiceError(409, "This payment is already settled.")
        learned, changed = self.orchestrator.learn_evidence_need(tx_id, need, by="owner")
        others = changed - 1 if changed else 0
        tail = f" It also settles {others} other {'payment' if others == 1 else 'payments'}." if others > 0 else ""
        return {"ok": True, "message": f"Done. {learned.reason} I will remember this.{tail}", "changed": changed}

    def onboarding_status(self) -> dict[str, Any]:
        """``GET /api/onboarding``: the first run (§5) and how onboarding went (§58), from what was recorded."""
        from backoffice.learning import compute_coverage, confirm_line, coverage_items

        repo = self.repo
        ob = repo.onboarding
        if not ob.started:
            return {"started": False, "message": "Not measured yet: this business was set up by hand, not through "
                                                 "onboarding."}
        history = [r for r in repo.transactions.values() if r.id in ob.historical]
        coverage = compute_coverage(coverage_items([r.tx for r in history],
                                                   {r.id: r.assignment for r in history if r.assignment is not None}))
        open_ids = [n for n in ob.asked if (need := repo.needs.get(n)) is not None and need.status == "open"]
        report = self.orchestrator.activation()
        minutes = [i for i in repo.interactions if i.kind.value == "onboarding"]
        ttfv = report.time_to_first_value if report is not None else None
        return {
            "started": True, "startedAt": _iso(ob.started_at), "learning": ob.learning,
            "headline": "Learning how your business works…" if ob.learning else coverage.headline,
            "confirm": confirm_line(len(open_ids)), "questionIds": open_ids, "questionLimit": ob.question_limit,
            "deferred": len(ob.deferred),
            "activated": bool(report and report.activated),
            "missing": [c.value for c in report.missing] if report is not None else [],
            "timeToFirstValueSeconds": int(ttfv.total_seconds()) if ttfv is not None else None,
            "onboardingSeconds": sum(i.active_seconds for i in minutes) if minutes else None,
            "milestones": {k: v.isoformat() for k, v in sorted(ob.milestones.items(), key=lambda kv: kv[1])},
            "finishedAt": _iso(ob.finished_at),
        }

    def onboarding_seen(self, body: Mapping[str, Any] | None) -> dict[str, Any]:
        """``POST /api/onboarding/seen`` ``{what: "time_saved"}``: the app showed the owner the time saved (§58)."""
        what = str((body or {}).get("what") or "time_saved")
        if what != "time_saved":
            raise ServiceError(400, "I only record that the time saved was shown.")
        if not self.repo.onboarding.started:
            raise ServiceError(409, "This business was not set up through onboarding: there is nothing to record.")
        recorded = self.orchestrator.milestone("time_saved_seen")
        self.orchestrator.run()
        return {"ok": True, "recorded": recorded}

    def mailboxes(self, body: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """``GET/POST /api/settings/mailboxes``: which mailbox or alias receives which company's mail (H5).

        POST ``{address, companyId}`` sets one (``companyId`` empty removes it). Learned ones are listed too.
        """
        repo = self.repo
        message = None
        if body:
            address = str(body.get("address") or "").strip().lower()
            if not _EMAIL.match(address) or len(address) > 254:
                raise ServiceError(400, "That doesn't look like an email address.")
            company = self._company(body.get("companyId"), required=False)
            if company is None:
                repo.mailboxes.pop(address, None)
                message = f"Done. {address} no longer points to one company."
            else:
                repo.mailboxes[address] = company
                message = f"Done. Invoices sent to {address} are {self._company_name(company)}'s."
            self.orchestrator.log("entity", "mailbox_set", subject_id=address, values={"company": company or ""},
                                  actor=f"owner:{repo.owner.email}")
            self.orchestrator.run()
        mapped = repo.mailbox_map()
        out: dict[str, Any] = {"mailboxes": [
            {"address": a, "companyId": c, "companyName": self._company_name(c) or "",
             "source": "you" if a in repo.mailboxes else "learned"} for a, c in sorted(mapped.items())]}
        if message:
            out["ok"], out["message"] = True, message
        return out

    def company_profile(self, company_id: str, body: Mapping[str, Any] | None) -> dict[str, Any]:
        """``POST /api/companies/{id}/profile``: the company's address, line of business, and who runs its payroll.

        ``{address?, sector?, payroll?: "owner" | "accountant"}``. The address helps tell which company an
        invoice is for (H3); the line of business which purchases are clearly personal (X23); payroll who is
        asked for a missing payslip (J3).
        """
        company = self._company(company_id)
        assert company is not None
        repo = self.repo
        body = body or {}
        if "address" in body:
            address = " ".join(str(body.get("address") or "").split())[:200]
            if address:
                repo.company_addresses[company] = [address]
            else:
                repo.company_addresses.pop(company, None)
        if "sector" in body:
            sector = " ".join(str(body.get("sector") or "").split())[:80]
            if sector:
                repo.company_sectors[company] = sector
            else:
                repo.company_sectors.pop(company, None)
        if "payroll" in body:
            who = str(body.get("payroll") or "").strip().lower()
            if who not in ("owner", "accountant"):
                raise ServiceError(400, "Say who runs payroll: you or your accountant.")
            repo.payroll_by[company] = who
        self.orchestrator.log("entity", "company_profile", subject_id=company,
                              values={"address": bool(repo.company_addresses.get(company)),
                                      "sector": repo.company_sectors.get(company, ""),
                                      "payroll": repo.payroll_by.get(company, "")},
                              actor=f"owner:{repo.owner.email}")
        self.orchestrator.run()
        return {"ok": True, "message": f"Done. {self._company_name(company)} is updated.",
                "profile": {"address": (repo.company_addresses.get(company) or [None])[0],
                            "sector": repo.company_sectors.get(company),
                            "payroll": self.orchestrator.payroll.runner(company)}}

    # ----------------------------------------------------------------- deadlines from letters (§24)

    _CONFIRMATIONS = {
        "reply": [{"id": "sent", "label": "I sent what they asked for"}],
        "submission": [{"id": "filed", "label": "It is filed"}],
        "renewal": [{"id": "renewed", "label": "It is renewed", "needsDate": True},
                    {"id": "not_renewing", "label": "I'm not renewing it"}],
    }

    def obligations(self) -> dict[str, Any]:
        """Every deadline from a letter or message, with who does it, what proves it done and how it stands."""
        from backoffice.closure import VerificationCondition

        today = self._today()
        items = []
        for ob in sorted(self.repo.obligations.values(), key=lambda o: (o.obligation.due_on, o.obligation.id)):
            o = ob.obligation
            try:
                condition = VerificationCondition.parse(o.verification_condition).describe(today)
            except (ValueError, ArithmeticError):
                condition = o.required_evidence
            open_ = not ob.done and not ob.informational
            late = open_ and o.due_on < today
            items.append({
                "id": o.id, "title": ob.title, "kind": o.kind.value, "companyId": o.entity_id,
                "companyName": self._company_name(o.entity_id) or "", "due": o.due_on.isoformat(),
                "amount": _num(o.amount), "currency": "EUR", "reference": ob.reference or "",
                "responsible": "Your accountant" if o.responsible == "accountant" else "You",
                "consequence": o.consequence, "requiredProof": o.required_evidence, "condition": condition,
                "status": "done" if ob.done else ("information" if ob.informational else "open"),
                "tone": "good" if ob.done else ("risk" if late else "neutral" if ob.informational else "attention"),
                "nextStep": self.orchestrator.obligations.next_step(ob), "why": list(ob.reasons),
                "evidenceIds": [ob.evidence_id, *ob.satisfied_by],
                "confirmOptions": [] if ob.done or ob.payable else self._CONFIRMATIONS.get(ob.proof.value, []),
            })
        return {"today": today.isoformat(), "items": items}

    def obligation_done(self, obligation_id: str, body: Mapping[str, Any] | None) -> dict[str, Any]:
        """The owner says a deadline is done (sent, filed, renewed, not renewing), optionally with the file."""
        b = body or {}
        raw = b.get("dataBase64") or b.get("data_base64")
        try:
            result = self.orchestrator.confirm_obligation(
                obligation_id, str(b.get("outcome") or ""),
                valid_until=self._date_arg(b, "validUntil") or self._date_arg(b, "valid_until"),
                data=_b64(raw) if raw else None, filename=b.get("filename"),
                content_type=b.get("contentType") or b.get("content_type"))
        except KeyError:
            raise ServiceError(404, "I can't find that deadline.") from None
        except PermissionError as exc:
            raise ServiceError(409, "That one is already done." if str(exc) == "already done" else str(exc)) from None
        except ValueError as exc:
            raise ServiceError(400, str(exc)) from None
        item = next(i for i in self.obligations()["items"] if i["id"] == obligation_id)
        return {"ok": True, "message": result.message, "obligation": item}

    def expected_not_coming(self, expected_id: str) -> dict[str, Any]:
        """The owner says a supplier's usual invoice will not come this time (§23)."""
        try:
            result = self.orchestrator.expected_invoice_not_coming(expected_id)
        except KeyError:
            raise ServiceError(404, "I can't find that invoice.") from None
        except PermissionError:
            raise ServiceError(409, "That one is already settled.") from None
        return {"ok": True, "message": result.message}

    # ----------------------------------------------------------------- Monthly report delivery

    def _report_settings(self) -> dict[str, Any]:
        """The saved delivery settings, or the defaults while the owner has saved none.

        Defaults are computed on each read (never stored by a read), so an
        accountant or company added later is included, and reading never
        changes the tenant's state.
        """
        saved = getattr(self, "_report_cfg", None)
        if saved is not None:
            return saved
        recipients = []
        for acct in self.repo.accountants():  # each company's package goes to that company's accountant (§28)
            served = self.repo.accountant_companies(acct.email)
            entry: dict[str, Any] = {"email": acct.email, "name": acct.person, "role": "Accountant"}
            if len(served) != len(self.repo.companies):
                entry["companies"] = served
            recipients.append(entry)
        return {
            "recipients": recipients, "day": 3, "format": "zip", "includeDocuments": True,
            "companies": list(self.repo.companies), "copyOwner": True, "whenOpen": "send",
        }

    def report_settings(self, body: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """GET/POST ``/api/settings/report``: when and to whom each month's package goes (§27, §28).

        ``whenOpen``: a month with items still open on the day is sent with an honest note of what is open
        (``"send"``, the default) or waits until it is closed (``"wait"``).
        """
        cfg = self._report_settings()
        if body:
            cfg = dict(cfg)
            when_open = body.get("whenOpen", cfg.get("whenOpen", "send"))
            if when_open not in ("send", "wait"):
                raise ServiceError(400, "Choose whether to send a month with open items or wait until it closes.")
            cfg["whenOpen"] = when_open
            recipients = body.get("recipients", cfg["recipients"])
            if not isinstance(recipients, list) or len(recipients) > 20:
                raise ServiceError(400, "Add up to 20 recipients.")
            clean = []
            for r in recipients:
                email = (r.get("email") if isinstance(r, Mapping) else "") or ""
                if not _EMAIL.match(email.strip()):
                    raise ServiceError(400, f"“{email}” doesn't look like an email address.")
                entry = {"email": email.strip().lower(), "name": str(r.get("name") or "")[:80],
                         "role": str(r.get("role") or "")[:40]}
                if isinstance(r.get("companies"), list):  # this recipient gets only these companies' months
                    entry["companies"] = [c for c in r["companies"] if c in self.repo.companies]
                clean.append(entry)
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
            self._report_cfg = cfg
            self.repo.package_settings = dict(cfg)  # what the monthly delivery follows (backoffice.package_delivery)
            self.orchestrator.log("closure", "report_settings_changed", values={"recipients": len(clean)})
        names = {c: e.name for c, e in self.repo.companies.items()}
        who = ", ".join(r["email"] for r in cfg["recipients"]) or "nobody yet"
        return {**cfg, "companyNames": names, "ownerEmail": self.repo.owner.email,
                "summary": f"On working day {cfg['day']} of each month I send the closed month to {who}."}

    # ----------------------------------------------------------------- What I may do on my own (§25)

    # The owner's switches for what runs "automatically if authorized" (policy/actions.py): key, action, plain name,
    # what it means. Everything else keeps its level: owner approval, or hard approval every time.
    _AUTOMATION = (
        ("supplierRequests", "supplier_invoice_request", "Ask suppliers for missing invoices",
         "When a payment's invoice is missing, I write to the supplier and ask for it."),
        ("accountantReplies", "routine_accountant_response", "Answer your accountant's routine questions",
         "When your accountant asks about a payment and the documents answer it, I reply with the facts."),
        ("monthlyPackage", "document_delivery", "Send each month to your accountant",
         "After each month, I send your accountant the documents, the ledger and the originals."),
    )

    def automation_settings(self, body: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """GET/POST ``/api/settings/automation``: what I may do on my own (§25), in plain words.

        POST ``{supplierRequests?, accountantReplies?, monthlyPackage?: bool, companyId?}`` switches each one on or
        off, for every company or (``companyId``) one company. Only actions the policy lets an owner pre-grant can
        be switched on here (policy/actions.py); moving money, tax filings, bank details, binding terms and
        deleting originals always need a yes every time. Each change is recorded in the audit log.
        """
        from backoffice.policy import ActionKind, is_grantable

        repo = self.repo
        message = None
        if body:
            company = self._company(body.get("companyId"), required=False)
            changes = [(key, ActionKind(action), body[key]) for key, action, _, _ in self._AUTOMATION if key in body]
            if not changes:
                raise ServiceError(400, "Choose what to switch on or off.")
            if any(not isinstance(on, bool) for _, _, on in changes):
                raise ServiceError(400, "Switch it on or off with true or false.")
            for key, action, on in changes:
                if not is_grantable(action):  # the table above only lists grantable actions; never widen it here
                    raise ServiceError(400, "That always needs your yes, every time.")
                if on:
                    repo.policy = repo.policy.with_grant(action, granted_by=repo.owner.email, entity_id=company,
                                                         at=self._now())
                elif company is None:  # off for the business: off for every company too
                    repo.policy = repo.policy.without_grant(action)
                    for c in list(repo.companies):
                        repo.policy = repo.policy.without_grant(action, entity_id=c)
                else:
                    if any(g.action is action and g.entity_id is None for g in repo.policy.grants):
                        # On for the whole business, now off for one company: the others keep it.
                        repo.policy = repo.policy.without_grant(action)
                        for c in repo.companies:
                            if c != company:
                                repo.policy = repo.policy.with_grant(action, granted_by=repo.owner.email,
                                                                     entity_id=c, at=self._now())
                    repo.policy = repo.policy.without_grant(action, entity_id=company)
                self._automation_choices()[f"{key}:{company or '*'}"] = on
                self.orchestrator.log("policy", "automation_changed", subject_id=action.value,
                                      values={"on": on, "company": company}, actor=f"owner:{repo.owner.email}")
            self.orchestrator.run()  # switched on: what is waiting for it is written now, and sent by the send path
            message = self._automation_message(changes, company)
        items = []
        for key, action, label, detail in self._AUTOMATION:
            kind = ActionKind(action)
            everywhere = repo.policy.allows(kind, None)
            per_company = [{"companyId": c, "companyName": e.name, "on": repo.policy.allows(kind, c)}
                           for c, e in repo.companies.items()]
            if key == "monthlyPackage":
                detail = (f"On working day {self._report_settings()['day']} after each month, I send your "
                          "accountant the documents, the ledger and the originals.")
            items.append({"id": key, "label": label, "detail": detail, "on": everywhere,
                          "onFor": [p["companyId"] for p in per_company if p["on"]], "companies": per_company,
                          "level": "automatic_if_authorized", "levelLabel": "Only when you allow it"})
        on = [i["label"][0].lower() + i["label"][1:] for i in items if i["on"]]
        summary = (f"I {join_and(on)} on my own. Everything else waits for you." if on else
                   "I don't write to anyone on my own. Everything waits for you.")
        out: dict[str, Any] = {
            "items": items, "summary": summary,
            "never": "Moving money, tax filings, bank detail changes, accepting terms and deleting originals always "
                     "need your yes, every time.",
        }
        if message:
            out["ok"], out["message"] = True, message
        return out

    def _authorize_delivery(self, company: str | None) -> None:
        """Naming the accountant is the owner saying where each month goes ("I will send the closed months to
        ..."): sending it is switched on (§25 document delivery, §27), unless the owner switched it off themselves."""
        from backoffice.policy import ActionKind

        repo = self.repo
        choices = self._automation_choices()
        if choices.get("monthlyPackage:*") is False or (company and choices.get(f"monthlyPackage:{company}") is False):
            return
        if repo.policy.allows(ActionKind.DOCUMENT_DELIVERY, company):
            return
        repo.policy = repo.policy.with_grant(ActionKind.DOCUMENT_DELIVERY, granted_by=repo.owner.email,
                                             entity_id=company, at=self._now())
        self.orchestrator.log("policy", "automation_changed", subject_id=ActionKind.DOCUMENT_DELIVERY.value,
                              values={"on": True, "company": company, "because": "accountant_named"},
                              actor=f"owner:{repo.owner.email}")

    def _automation_choices(self) -> dict[str, bool]:
        """What the owner switched on or off themselves ("<key>:<company or *>"): never overridden later."""
        if getattr(self, "_automation", None) is None:
            self._automation = {}
        return self._automation

    def _automation_message(self, changes: Sequence[tuple[str, Any, bool]], company: str | None) -> str:
        where = f" for {self._company_name(company)}" if company else ""
        parts = []
        for key, _, on in changes:
            phrase = {"supplierRequests": ("I will ask suppliers for missing invoices",
                                           "I won't ask suppliers for invoices"),
                      "accountantReplies": ("I will answer your accountant's routine questions",
                                            "I won't answer your accountant on my own"),
                      "monthlyPackage": ("I will send each month to your accountant",
                                         "I won't send the months to your accountant on my own")}[key]
            parts.append(phrase[0] if on else phrase[1])
        return f"Done. {join_and(parts)}{where}."

    # ----------------------------------------------------------------- Accountant API keys (§28)

    def _keys(self) -> dict[str, dict[str, Any]]:
        if getattr(self, "_api_keys", None) is None:
            self._api_keys = {}
        return self._api_keys

    def api_keys(self) -> dict[str, Any]:
        return {"keys": [{k: v for k, v in rec.items() if k != "hash"} for rec in self._keys().values()]}

    @staticmethod
    def new_api_key() -> tuple[str, str, str]:
        """A fresh accountant API key: ``(secret, prefix, sha256 fingerprint)``."""
        import hashlib
        import secrets

        secret = "bo_live_" + secrets.token_urlsafe(24)
        return secret, secret[:12], hashlib.sha256(secret.encode()).hexdigest()

    def api_key_create(self, body: Mapping[str, Any] | None, *, secret: str | None = None,
                       fingerprint: tuple[str, str] | None = None) -> dict[str, Any]:
        """Issue a key. ``secret`` fixes the key; ``fingerprint`` = (prefix, sha256) re-creates a
        key whose secret is no longer known (an event-sourced tenant being rebuilt)."""
        import hashlib

        name = str((body or {}).get("name") or "Accounting system").strip()[:60]
        if fingerprint is not None:
            prefix, digest = fingerprint
        else:
            if secret is None:
                secret, _, _ = self.new_api_key()
            prefix, digest = secret[:12], hashlib.sha256(secret.encode()).hexdigest()
        # A running number, so a revoked key's id is never given to a new key.
        self._api_key_seq = getattr(self, "_api_key_seq", 0) + 1
        kid = f"key_{self._api_key_seq:03d}"
        self._keys()[kid] = {"id": kid, "name": name, "prefix": prefix, "createdAt": self._now().isoformat(),
                             "hash": digest, "scope": "documents:read"}
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
        reconnecting = bool((self.sign_in.get(connection_id) or {}).get("reconnecting"))
        if self.real_sources:  # covered once the first sync has read it (the sync worker)
            if not reconnecting:  # a reconnected mailbox shows as connected again only once a sync works
                c.healthy = True
            self._clear_reconnect(connection_id)
        else:
            c.healthy, c.covered_from, c.covered_until, c.last_synced_at = True, now - timedelta(days=90), now, now
        self.sign_in[connection_id] = {**self.sign_in.get(connection_id, {}), "pending": False, "stored": True}
        if c.kind == "email" and not reconnecting:
            self.orchestrator.milestone("email_connected")
            if self.real_sources and not (self.sync_states.get(connection_id) or {}).get("last_successful_sync"):
                self.orchestrator.connection_reading(connection_id)
            self.orchestrator.setup_step("email_sign_in")
        if reconnecting and self.real_sources:
            self.orchestrator.activity(now, "checked", f"You signed in to {c.account} again. I'm catching up now.")
        else:
            self.orchestrator.activity(now, "checked", f"Connected {c.account}.")
        self.orchestrator.run()

    def remove_source(self, source_id: str) -> dict[str, Any]:
        repo = self.repo
        name = None
        signed_in = source_id in repo.connectors or source_id in repo.accounts
        if source_id in repo.connectors:
            c = repo.connectors.pop(source_id)
            name = c.account if c.kind == "email" else c.name
            if c.kind == "accountant":  # that accountant no longer looks after any company
                if repo.accountant is not None and repo.accountant.email == c.account:
                    repo.accountant = None
                for company_id, a in list(repo.company_accountants.items()):
                    if a.email == c.account:
                        del repo.company_accountants[company_id]
                self._sync_accountant_connectors()
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
        self.orchestrator.connection_read(source_id)
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
                "taxId": entity.tax_id, "country": entity.country,
                "tone": tone, "statusLabel": status.company_status, "detail": detail,
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
        # The month's package for the accountant (§27): "September sent to your accountant on 6 October." only once a
        # transport accepted it; before that it says it is waiting (or held back).
        delivery = self.orchestrator.packages.owner_view(company_id, month, self._today())
        if delivery is not None:
            result["delivery"] = delivery
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
        for n in self._open_needs(company_id):
            if n.kind not in ("company", "cash", "obligation", "refund", "part", "deposit", "deposit_refund",
                              "deposit_kept", "chargeback", "receipt", "expense_claim", "lease", "till",
                              "member") or \
                    repo.item_month(repo.items[n.item_id]) != month:
                continue
            lines.append({"id": f"r_{n.id}", "tone": "attention", "href": f"/needs-you#{n.id}", "linkLabel": "Answer",
                          "text": f"I still need one thing from you. {n.prompt}"})
        for rec in sorted(txs, key=lambda r: (r.tx.booked_on, r.id)):
            item = repo.items[rec.item_id]
            if item.is_done or item.stage in (Stage.NEEDS_OWNER, Stage.CONFLICT):
                continue
            if rec.document_ids:
                if rec.hold_reason:  # matched, but a large first purchase needs more before it closes
                    lines.append({"id": f"r_{rec.id}", "text": rec.hold_reason, "tone": "attention"})
                continue
            if rec.decision is not None and (rec.decision.requires_document or rec.decision.rule in _PLANNED_RULES):
                lines.append({"id": f"r_{rec.id}", "text": self.orchestrator.missing.plan(rec), "tone": "neutral"})
        # A member's payment that came back: its period is unpaid again (X12).
        lines += self.orchestrator.members.month_lines(company_id, month)
        for expected in sorted(repo.expected_invoices.values(), key=lambda e: (e.due_on, e.id)):
            # A supplier's usual invoice that has not arrived (§23): missing until it does.
            if expected.status == "missing" and expected.company_id == company_id and expected.period == month:
                lines.append({"id": f"r_{expected.id}", "text": self.orchestrator.missing.plan_expected(expected),
                              "tone": "neutral"})
        for item in repo.items_for(company_id, month):
            if item.subject_type != "document" or item.is_done or item.stage in (Stage.NEEDS_OWNER, Stage.CONFLICT):
                continue
            doc = repo.documents[item.subject_id]
            if doc.hold_reason:
                lines.append({"id": f"r_{doc.id}", "text": doc.hold_reason, "tone": "attention"})
                continue
            if doc.claim_id is not None and doc.claim_id in repo.expense_claims:
                # An employee's own money (backoffice.staff): open until the transfer paying them back.
                claim = repo.expense_claims[doc.claim_id]
                person = repo.employees.get(claim.employee_id)
                lines.append({"id": f"r_{doc.id}", "tone": "neutral",
                              "text": f"{person.name if person else 'An employee'} paid "
                                      f"{format_money(claim.amount, claim.currency)} at {claim.merchant}. "
                                      f"{self.orchestrator.staff.claim_status_line(claim, you=False)}"})
                continue
            if doc.matched_tx_ids:
                continue
            if doc.document.doc_type is DocumentType.PAYOUT_REPORT:
                settlement = repo.settlements.get(doc.id)
                who = settlement.report.provider.label if settlement else display_name(doc.document.supplier_name)
                lines.append({"id": f"r_{doc.id}", "tone": "neutral",
                              "text": f"The payout report from {who} arrived. I'm waiting for that payout to reach "
                                      "your bank."})
                continue
            due = self.orchestrator.invoice_due(doc)
            if due is not None:  # it states when it is due (F8)
                lines.append({"id": f"r_{doc.id}", "tone": "attention" if due["overdue"] else "neutral",
                              "text": due["line"]})
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
            accountant = repo.accountant_for(company_id)
            software = accountant.firm if accountant else "your accountant"
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
            if rec.claim_ids:  # pays an employee back for expense claims (backoffice.staff)
                claim = repo.expense_claims.get(rec.claim_ids[0])
                person = repo.employees.get(claim.employee_id) if claim is not None else None
                out.append({"id": f"m_{rec.id}", "supplier": person.name if person else self.orchestrator.merchant_name(
                    rec.tx), "description": "Expense claim paid back", "amount": _num(abs(rec.tx.amount)),
                    "currency": rec.tx.currency, "date": rec.tx.booked_on.isoformat(),
                    "reasons": [r.replace(": ", " ", 1) for r in rec.match_why]})
                continue
            if rec.document_ids:
                doc = repo.documents[rec.document_ids[0]]
                out.append({
                    "id": f"m_{rec.id}", "supplier": display_name(doc.document.supplier_name),
                    "description": doc.label.split(" · ")[0], "amount": _num(abs(rec.tx.amount)),
                    "currency": rec.tx.currency, "date": rec.tx.booked_on.isoformat(),
                    "reasons": [r.replace(": ", " ", 1) for r in rec.match_why],
                })
            elif rec.proof_evidence_ids and rec.id in repo.lease_payments:
                # A leasing payment proven by its contract and the leasing company's statement (X24).
                out.append({"id": f"m_{rec.id}", "supplier": self.orchestrator.merchant_name(rec.tx),
                            "description": "Leasing payment", "amount": _num(abs(rec.tx.amount)),
                            "currency": rec.tx.currency, "date": rec.tx.booked_on.isoformat(),
                            "reasons": [r.replace(": ", " ", 1) for r in rec.match_why]})
            elif rec.proof_evidence_ids:
                supplier, description = self._letter_payment_words(rec)
                out.append({"id": f"m_{rec.id}", "supplier": supplier, "description": description,
                            "amount": _num(abs(rec.tx.amount)), "currency": rec.tx.currency,
                            "date": rec.tx.booked_on.isoformat(), "reasons": self._tax_reasons(rec)})
            elif self.orchestrator.chargebacks.settled(rec) is not None:
                # A disputed card payment taken back or won back, linked to what proves it (I7).
                cb = repo.chargebacks[rec.id]
                out.append({"id": f"m_{rec.id}", "supplier": self.orchestrator.chargebacks.merchant(rec),
                            "description": "Disputed card payment won back" if cb.direction == "in" else
                            "Disputed card payment taken back",
                            "amount": _num(abs(rec.tx.amount)), "currency": rec.tx.currency,
                            "date": rec.tx.booked_on.isoformat(),
                            "reasons": [r.replace(": ", " ", 1) for r in rec.match_why]})
            elif self.orchestrator.staged.settled_without_document(rec) is not None:
                # A deposit given back when a booking was cancelled, and the money that gave it back (X8).
                dep = repo.deposits.get(rec.deposit_refund_of or rec.id)
                noun = dep.noun.capitalize() if dep else "Deposit"
                out.append({"id": f"m_{rec.id}", "supplier": dep.party if dep else display_name(rec.tx.counterparty),
                            "description": f"{noun} given back" if rec.deposit_refund_of else noun,
                            "amount": _num(abs(rec.tx.amount)), "currency": rec.tx.currency,
                            "date": rec.tx.booked_on.isoformat(),
                            "reasons": [r.replace(": ", " ", 1) for r in rec.match_why]})
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
        staged = self.orchestrator.staged
        for held in sorted(self.repo.retentions.values(), key=lambda r: r.document_id):
            # Part of an invoice held back until the work is accepted: still owed, never a missing payment (X8).
            record = self.repo.documents.get(held.document_id)
            if held.status != "held" or record is None or staged.company_of(record) != company_id \
                    or self.repo.item_month(self.repo.items[record.item_id]) != month:
                continue
            words = staged.invoice_words(record)
            out.append({"id": f"n_held_{record.id}", "tone": "neutral",
                        "text": f"{words[:1].upper()}{words[1:]}: {staged.held_sentence(held)}"})
        # The cash box once the month is over: what it should hold, and anything not explained (X4).
        cash_box = self.orchestrator.cash.month_line(company_id, month)
        if cash_box is not None:
            out.append(cash_box)
        return out

    # ----------------------------------------------------------------- Needs you

    def needs_you(self) -> dict[str, Any]:
        items = []
        for n in self._open_needs():
            if n.kind == "check":
                items.append(self._check(n))
            elif n.kind in _PLAIN_QUESTIONS:
                items.append(self._question(n))
            elif n.kind == "cost_center":
                items.append(self._cost_center_item(n))
            else:
                items.append(self._choice(n) if n.kind == "choice" else self._approval(n))
        return {"items": items}

    def _question(self, n: NeedsYouRecord) -> dict[str, Any]:
        """Which company carries a payment, a cash receipt to confirm, whether a payment pays a letter, a
        refund that differs from its credit note, which company a letter is for, amounts that differ on a
        supplier's statement, or whether a client pays a cost back: one plain choice (§37, §51)."""
        if n.kind == "statement":
            return self._statement_item(n)
        if n.subject_type == "transaction":
            rec = self.repo.transactions[n.subject_id]
            merchant, amount = self.orchestrator.merchant_name(rec.tx), abs(rec.tx.amount)
            currency, day = rec.tx.currency, rec.tx.booked_on
        elif n.subject_type == "obligation":
            finding = self.repo.pending_obligations[n.subject_id].finding
            merchant = _ISSUER_NAMES.get(finding.issuer.value, "A letter")
            amount, currency = finding.amount or Decimal("0"), finding.currency
            day = finding.due_on
        elif n.subject_type == "evidence":  # a photo I can't read (backoffice.captures): nothing read from it
            merchant, amount, currency = "Photo of a receipt", None, "EUR"
            day = self.repo.evidence(n.subject_id).retrieved_at.astimezone(TZ).date()
        else:
            doc = self.repo.documents[n.subject_id]
            merchant, amount = display_name(doc.document.supplier_name), doc.document.gross_amount or Decimal("0")
            currency = doc.document.currency
            day = doc.document.issue_date or doc.received_at.astimezone(TZ).date()
        item = {
            "id": n.id, "kind": "choice", "tone": "attention", "eyebrow": "We need one answer",
            "merchant": merchant, "amount": _num(amount), "currency": currency, "date": _iso(day),
            "companyId": n.company_id, "question": n.prompt, "options": [{"id": o.id, "label": o.label}
                                                                        for o in n.options],
            "why": list(n.why),
        }
        if n.kind == "recharge" and n.question is not None:
            from backoffice.learning.cost_centers import recharge_rule

            overrides = {}
            for option in n.options:
                proposal = recharge_rule(n.question, option.id == "recharge", answered_by="owner",
                                         answered_at=self._now())
                if proposal is not None:
                    overrides[option.id] = proposal.label
            if overrides:
                item["remember"] = {"template": "Always {choice}", "defaultChecked": True, "overrides": overrides}
        return item

    def _statement_item(self, n: NeedsYouRecord) -> dict[str, Any]:
        """Amounts that differ on a supplier's account statement: which is right (never a silent change)."""
        doc = self.repo.documents[n.subject_id]
        sr = self.repo.statements.get(n.subject_id)
        rows = [r for r in (sr.check.differences if sr is not None and sr.check is not None else ())]
        amount = rows[0].line.amount if len(rows) == 1 else (sr.statement.closing if sr is not None else None)
        day = (sr.statement.end if sr is not None else None) or doc.received_at.astimezone(TZ).date()
        return {
            "id": n.id, "kind": "choice", "tone": "attention", "eyebrow": "We need one answer",
            "merchant": display_name(doc.document.supplier_name), "amount": _num(amount),
            "currency": sr.statement.currency if sr is not None else doc.document.currency, "date": _iso(day),
            "companyId": n.company_id, "question": n.prompt,
            "options": [{"id": o.id, "label": o.label} for o in n.options], "why": list(n.why),
        }

    def _cost_center_item(self, n: NeedsYouRecord) -> dict[str, Any]:
        """'Which job is this for?': one tap, "Always ..." learning, and a split for costs shared by several."""
        from backoffice.learning.cost_centers import SPLIT_OPTION, suggest_cost_center_rule

        rec = self.repo.transactions[n.subject_id]
        question = n.question
        assert question is not None
        account = self.repo.accounts.get(rec.tx.account_id)
        item: dict[str, Any] = {
            "id": n.id, "kind": "choice", "tone": "attention", "eyebrow": "We need one answer",
            "merchant": self.orchestrator.merchant_name(rec.tx), "amount": _num(abs(rec.tx.amount)),
            "currency": rec.tx.currency, "date": rec.tx.booked_on.isoformat(), "companyId": n.company_id,
            "question": question.prompt, "options": [{"id": o.id, "label": o.label} for o in question.options],
            "why": list(n.why),
        }
        if account is not None:
            item["paidWith"] = account.label
        template, overrides = None, {}
        for option in question.options:
            proposal = suggest_cost_center_rule(question, option.id, answered_by="owner", answered_at=self._now())
            if proposal is None:
                continue
            if option.kind is OptionKind.COST_CENTER and template is None and option.label in proposal.label:
                template = proposal.label.replace(option.label, "{choice}", 1)
            elif option.kind is not OptionKind.COST_CENTER:
                overrides[option.id] = proposal.label
        if template is not None:
            item["remember"] = {"template": template, "defaultChecked": True, **({"overrides": overrides}
                                                                                if overrides else {})}
        choices = [{"id": o.cost_center_id, "label": o.label} for o in question.options
                   if o.kind is OptionKind.COST_CENTER]
        if len(choices) > 1:
            item["split"] = {"optionId": SPLIT_OPTION, "label": "Split it between several",
                             "costCenters": choices, "total": _num(abs(rec.tx.amount)),
                             "hint": "Give an amount or a percentage for each. They must add up exactly."}
        return item

    def _check(self, n: NeedsYouRecord) -> dict[str, Any]:
        """A document whose sources disagree (§19), asked as a plain choice (§37)."""
        doc = self.repo.documents[n.subject_id]
        d = doc.document
        shown = d.gross_amount if d.gross_amount is not None else next(
            (o.values["gross_amount"] for o in n.options if isinstance(o.values.get("gross_amount"), Decimal)),
            Decimal("0"))
        return {
            "id": n.id, "kind": "choice", "tone": "attention", "eyebrow": "We need one answer",
            "merchant": display_name(d.supplier_name), "amount": _num(shown), "currency": d.currency,
            "date": _iso(d.issue_date or doc.received_at.astimezone(TZ).date()), "companyId": n.company_id,
            "question": n.prompt, "options": [{"id": o.id, "label": o.label} for o in n.options], "why": list(n.why),
        }

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
                # What the tap does: a request for a corrected invoice to the address on file, if there is one.
                "message": (f"Done. It stays blocked. I will ask {who} for a corrected invoice." if supplier is not None
                            and supplier.contact_email else "Done. It stays blocked."),
            },
        }

    def answer(self, needs_id: str, option_id: str, remember: bool = False, split: Any = None) -> dict[str, Any]:
        if not isinstance(option_id, str) or not option_id.strip():
            raise ServiceError(400, "Please pick one of the options.")
        try:
            outcome = self.orchestrator.answer(needs_id, option_id, remember=bool(remember), split=split)
        except KeyError:
            raise ServiceError(404, "I can't find that question any more.") from None
        except SplitError as exc:
            raise ServiceError(400, exc.message) from None
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

    # ----------------------------------------------------------------- Cost centers (jobs, properties, vehicles ...)

    @property
    def staff_views(self):  # type: ignore[no-untyped-def]
        from backoffice.staff_views import StaffViews

        return StaffViews(self)

    def dispatch_employee(self, method: str, path: str, body_json: Any, email: str) -> tuple[int, dict[str, Any]]:
        """Route one request from an employee (production ``employee`` role): their own open card payments
        and their receipt uploads, nothing else (backoffice.staff_views)."""
        r = re.compile
        routes = (
            ("GET", r("/api/employee/card-payments"), lambda b: self.staff_views.card_payments(email)),
            ("POST", r("/api/employee/receipts"),
             lambda b: self.staff_views.employee_receipt({k: v for k, v in b.items()
                                                          if k not in ("employeeId", "employee")}
                                                         | {"employeeEmail": email})),
        )
        return self._route(method, path, body_json, routes,
                           refuse=(403, {"error": "forbidden", "message": "You don't have access to that."}))

    def _cost_views(self):  # type: ignore[no-untyped-def]
        from backoffice.cost_centers import CostCenterViews

        return CostCenterViews(self)

    def _cost_center_record(self, cost_center_id: str) -> CostCenter:
        center = self.repo.cost_centers.get(cost_center_id)
        if center is None:
            raise ServiceError(404, "I can't find that one.")
        return center

    def cost_centers(self, company_id: str, body: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """A company's jobs (properties, vehicles ...): what each cost and brought in, and what is still open."""
        if company_id not in self.repo.companies:
            raise ServiceError(404, "I can't find that company.")
        return self._cost_views().company(company_id, body)

    def cost_center(self, cost_center_id: str, body: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """One job: money out and in, its payments and documents with the proof, and what is still open."""
        return self._cost_views().detail(self._cost_center_record(cost_center_id), body)

    def cost_center_statement(self, cost_center_id: str, body: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """A period's statement: money received, costs with their proof, the management fee and what is left
        (for a property, what is due to its owner). Not final while anything is still open."""
        return self._cost_views().statement(self._cost_center_record(cost_center_id), body)

    @staticmethod
    def _cost_center_terms(b: Mapping[str, Any]) -> dict[str, Any]:
        """The client pays its costs back; a property's owner; its management fee (percent and/or monthly)."""
        out: dict[str, Any] = {}
        if "recharge" in b:
            if not isinstance(b.get("recharge"), bool):
                raise ServiceError(400, "Say true or false.")
            out["recharge"] = b["recharge"]
        if "owner" in b:
            owner = b.get("owner")
            if owner in (None, ""):
                out["owner_name"] = None
            elif isinstance(owner, str) and " ".join(owner.split()):
                name = " ".join(owner.split())
                if len(name) > 80:
                    raise ServiceError(400, "That name is too long.")
                out["owner_name"] = name
            else:
                raise ServiceError(400, "Write the owner's name.")
        key = "managementFee" if "managementFee" in b else "management_fee" if "management_fee" in b else None
        if key is not None:
            fee = b.get(key)
            if fee is None:
                out["fee_percent"] = out["fee_monthly"] = None
            elif isinstance(fee, Mapping) and set(fee) <= {"percent", "monthly"} and any(
                    fee.get(k) not in (None, "") for k in ("percent", "monthly")):
                percent, monthly = fee.get("percent"), fee.get("monthly")
                try:
                    out["fee_percent"] = None if percent in (None, "") else to_cents(percent, what="percent")
                    out["fee_monthly"] = None if monthly in (None, "") else to_cents(monthly, what="amount")
                except SplitError as exc:
                    raise ServiceError(400, exc.message) from None
                if out["fee_percent"] is not None and not Decimal(0) < out["fee_percent"] <= Decimal(100):
                    raise ServiceError(400, "A fee is more than 0% and at most 100%.")
                if out["fee_monthly"] is not None and out["fee_monthly"] < 0:
                    raise ServiceError(400, "A monthly fee can't be below zero.")
            else:
                raise ServiceError(400, "Give the fee as a percent of what comes in, an amount a month, or both.")
        return out

    def _identifiers(self, company_id: str, raw: Any, current: CostCenterIdentifiers | None = None
                     ) -> CostCenterIdentifiers:
        if raw is None:
            return current or CostCenterIdentifiers()
        if not isinstance(raw, Mapping):
            raise ServiceError(400, "List what points to it: addresses, plates, cards, references, tax numbers, "
                                    "email addresses or keywords.")
        unknown = [k for k in raw if not isinstance(k, str) or CostCenterIdentifiers.field_for(k) is None]
        if unknown:
            raise ServiceError(400, "I can only recognise it by addresses, plates, cards, accounts, references, "
                                    "tax numbers, email addresses or keywords.")
        given = {CostCenterIdentifiers.field_for(k): v for k, v in raw.items()}
        merged = {**(current.model_dump() if current else {}), **given}
        try:
            identifiers = CostCenterIdentifiers(**merged)
        except (ValidationError, ValueError) as exc:
            detail = exc.errors()[0].get("msg", "") if isinstance(exc, ValidationError) else str(exc)
            detail = detail.removeprefix("Value error, ")
            raise ServiceError(400, (detail[:1].upper() + detail[1:] + ".") if detail else
                               "One of those doesn't look right.") from None
        for account_id in identifiers.accounts:
            account = self.repo.accounts.get(account_id)
            if account is None or account.holder_id != company_id:
                raise ServiceError(400, "I don't know that account for this company.")
        return identifiers

    def _cost_center_name(self, company_id: str, value: Any, *, skip: str | None = None) -> str:
        readable = isinstance(value, str) or (isinstance(value, int) and not isinstance(value, bool))
        name = " ".join(str(value).split()) if readable else ""
        if not name:
            raise ServiceError(400, "What is it called? For example the site's street, the plate or the client.")
        if len(name) > 80:
            raise ServiceError(400, "That name is too long.")
        clash = next((c for c in self.repo.cost_centers.values() if c.company_id == company_id and c.active
                      and c.id != skip and c.name.casefold() == name.casefold()), None)
        if clash is not None:
            raise ServiceError(409, f"{clash.label} is already here.")
        return name

    @staticmethod
    def _cost_center_kind(value: Any, default: str) -> str:
        if value in (None, ""):
            return default
        kind = " ".join(str(value).split()) if isinstance(value, str) else ""
        if not kind or len(kind) > 30 or not re.fullmatch(r"[^\W\d_]+(?:[ '-][^\W\d_]+)*", kind):
            raise ServiceError(400, "Use one or two words for the kind, like Job, Property, Vehicle or Client.")
        return kind[:1].upper() + kind[1:]

    def cost_center_create(self, company_id: str, body: Mapping[str, Any] | None) -> dict[str, Any]:
        """Add a job, property, vehicle, outlet, event, course or client to one company."""
        if company_id not in self.repo.companies:
            raise ServiceError(404, "I can't find that company.")
        b = body or {}
        name = self._cost_center_name(company_id, b.get("name"))
        kind = self._cost_center_kind(b.get("kind"), self._cost_views().kind_for(company_id))
        identifiers = self._identifiers(company_id, b.get("identifiers"))
        terms = self._cost_center_terms(b)
        base = "cc-" + (_SLUG.sub("-", name.lower()).strip("-")[:40] or "x")
        cid, n = base, 2
        while cid in self.repo.cost_centers:
            cid, n = f"{base}-{n}", n + 1
        center = CostCenter(id=cid, tenant_id=self.repo.tenant_id, company_id=company_id, name=name, kind=kind,
                            identifiers=identifiers, **terms)
        self.repo.cost_centers[cid] = center
        self.orchestrator.log("cost_center", "cost_center_added", subject_id=cid,
                              actor=f"owner:{self.repo.owner.email}",
                              values={"company_id": company_id, "name": name, "kind": kind,
                                      "identifiers": identifiers.as_dict(), **_terms_audit(terms)})
        self.orchestrator.activity(self._now(), "learned", f"Added {center.label}. I will put its costs on it.",
                                   company_id)
        self.orchestrator.run()
        return {"ok": True, "costCenter": self.cost_center(cid),
                "message": f"Done. {center.label} is set up. I will put its costs on it."}

    def cost_center_update(self, cost_center_id: str, body: Mapping[str, Any] | None) -> dict[str, Any]:
        """Rename it, change its kind or what points to it, or archive it (its past costs stay on it)."""
        center = self._cost_center_record(cost_center_id)
        b = body or {}
        if not any(k in b for k in ("name", "kind", "identifiers", "active", "recharge", "owner", "managementFee",
                                    "management_fee")):
            raise ServiceError(400, "Tell me what to change: its name, its kind, what points to it, or archive it.")
        update: dict[str, Any] = self._cost_center_terms(b)
        if "name" in b:
            update["name"] = self._cost_center_name(center.company_id, b.get("name"), skip=center.id)
        if "kind" in b:
            update["kind"] = self._cost_center_kind(b.get("kind"), center.kind)
        if "identifiers" in b:
            update["identifiers"] = self._identifiers(center.company_id, b.get("identifiers"), center.identifiers)
        if "active" in b:
            if not isinstance(b.get("active"), bool):
                raise ServiceError(400, "Say true or false.")
            update["active"] = b["active"]
            if b["active"] and not center.active:
                self._cost_center_name(center.company_id, update.get("name", center.name), skip=center.id)
        changed = center.model_copy(update=update)
        changed = CostCenter.model_validate(changed.model_dump())
        self.repo.cost_centers[center.id] = changed
        self.orchestrator.log("cost_center", "cost_center_changed", subject_id=center.id,
                              actor=f"owner:{self.repo.owner.email}",
                              values={"name": changed.name, "kind": changed.kind, "active": changed.active,
                                      "identifiers": changed.identifiers.as_dict(),
                                      **_terms_audit({k: v for k, v in update.items()
                                                      if k in ("recharge", "owner_name", "fee_percent",
                                                               "fee_monthly")})})
        self.orchestrator.run()
        if not changed.active:
            message = f"Done. {changed.label} is archived. Its past costs stay on it."
        elif changed.label != center.label:
            message = f"Done. It is now called {changed.label}."
        else:
            message = f"Done. I updated {changed.label}."
        return {"ok": True, "costCenter": self.cost_center(center.id), "message": message}

    def cost_center_allocate(self, body: Mapping[str, Any] | None) -> dict[str, Any]:
        """Put one payment or document on a job, on general costs, or split it (and optionally always do so)."""
        b = body or {}
        subject = _field(b, "subjectId", "subject_id")
        cost_center_id = b.get("costCenterId") or b.get("cost_center_id")
        general = bool(b.get("general", False))
        split = b.get("split")
        if sum(1 for x in (cost_center_id, general or None, split) if x is not None) != 1:
            raise ServiceError(400, "Choose one: where it goes, general costs, or how to split it.")
        recharge = b.get("recharge")
        if recharge is not None and not isinstance(recharge, bool):
            raise ServiceError(400, "Say true or false.")
        try:
            outcome = self.orchestrator.allocate_by_owner(
                subject, cost_center_id=cost_center_id if isinstance(cost_center_id, str) else None,
                general=general, split=split, remember=bool(b.get("remember", False)), recharge=recharge)
        except KeyError:
            raise ServiceError(404, "I can't find that payment or document.") from None
        except SplitError as exc:
            raise ServiceError(400, exc.message) from None
        result: dict[str, Any] = {"ok": outcome.ok, "message": outcome.message}
        if outcome.learned:
            result["learned"] = outcome.learned
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
        """One question, answered from evidence (§39). The same understanding as the chat's built-in brain."""
        if not isinstance(question, str) or not question.strip():
            raise ServiceError(400, "Ask me about a supplier, a payment or a month.")
        if len(question) > 4000:
            raise ServiceError(400, "That is too long. Try a shorter question.")
        from backoffice.assistant import RuleBrain

        return RuleBrain(self.assistant).ask(question)

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

    def _supplier_answer(self, supplier: Supplier) -> dict[str, Any]:
        """'Did we pay Vodafone?': the latest payment, its invoice or the chase, and any hold (§39)."""
        repo = self.repo
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
                if chase is not None and chase.sent_at is not None:
                    tail = (f" I asked {who} for the invoice on "
                            f"{day_month(chase.sent_at.astimezone(TZ).date(), self._today())}.")
                elif chase is not None:
                    tail = f" I wrote to {who} asking for the invoice. It is waiting to be sent."
                else:
                    tail = " I'm still looking for its invoice."
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

    def _accountant_answer(self) -> dict[str, Any]:
        questions = sorted(self.repo.accountant_questions.values(), key=lambda x: (x.asked_at, x.id))
        if not questions:
            return {"answer": "Your accountant has not asked anything this month.", "evidence": []}
        answered = [x for x in questions if x.status == "answered"]  # the answer reached the accountant
        written = [x for x in questions if x.status == "written"]  # written, waiting to be sent
        waiting = [x for x in questions if x.status not in ("answered", "written")]
        n = len(questions)
        head = "One question" if n == 1 else f"{_count_word(n).capitalize()} questions"
        parts = [f"{head}, {_count_word(len(answered))} answered." if written or waiting else f"{head}, all answered."]
        evidence = []
        for x in answered:
            parts.append(f"“{x.text}” {x.answer}")
            evidence.append({"label": f"Accountant question · {day_month(x.asked_at.astimezone(TZ).date(), self._today())}",
                             "id": x.evidence_id})
            evidence += [{"label": "Proof used in the answer", "id": e} for e in x.answer_evidence_ids[:2]]
        for x in written:
            parts.append(f"“{x.text}” I wrote this answer; it is waiting to be sent: {x.answer}")
            evidence.append({"label": "Accountant question", "id": x.evidence_id})
            evidence += [{"label": "Proof used in the answer", "id": e} for e in x.answer_evidence_ids[:2]]
        for x in waiting:
            parts.append(f"Still open: “{x.text}” I can't answer that from the documents, so it needs your answer.")
            evidence.append({"label": "Accountant question", "id": x.evidence_id})
        return {"answer": " ".join(parts), "evidence": evidence}

    def _price_answer(self) -> dict[str, Any]:
        changes = self.orchestrator.price_changes()
        # Prices on invoice lines (X20): a product that cost unusually more than its last purchases.
        unit = self.orchestrator.line_prices.recent_increases(self._today())
        if not changes and not unit:
            return {"answer": "None of your regular costs went up in the last three months.", "evidence": []}
        parts: list[str] = []
        evidence: list[dict[str, str]] = []
        if changes:
            lines = [f"{name}: {format_money(before)} → {format_money(after)}" for name, before, after, _ in changes]
            count = "One went up" if len(changes) == 1 else f"{len(changes)} went up"
            parts.append(f"{count} in the last three months. " + ". ".join(lines) + ".")
            evidence += [{"label": f"{name} · {format_money(after)}", "id": ev[0]}
                         for name, _, after, ev in changes if ev]
        if unit:
            head = ("One price on your invoices" if len(unit) == 1 else
                    f"{_count_word(len(unit)).capitalize()} prices on your invoices")
            parts.append(f"{head} went up {threshold_words()} or more against the last purchases: "
                         + "; ".join(c.line() for c in unit) + ".")
            evidence += [{"label": f"{c.purchase.supplier} {c.purchase.invoice}", "id": c.purchase.evidence_id}
                         for c in unit]
        return {"answer": " ".join(parts), "evidence": evidence}

    def _attention_answer(self) -> dict[str, Any]:
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
            elif n.kind == "check":
                who = display_name(self.repo.documents[n.subject_id].document.supplier_name)
                parts.append(n.prompt)
                evidence.append({"label": f"{who} · invoice to check", "id": f"needs:{n.id}"})
            elif n.kind in _PLAIN_QUESTIONS:
                item = self._question(n)
                parts.append(n.prompt)
                what = {"company": "which company", "cash": "cash receipt to confirm",
                        "obligation": "payment to confirm", "refund": "refund to confirm",
                        "obligation_company": "which company", "statement": "statement to check",
                        "recharge": "paid back by the client?", "part": "payment to confirm",
                        "deposit": "deposit to confirm", "deposit_refund": "deposit given back?",
                        "deposit_kept": "security deposit kept?", "chargeback": "disputed card payment",
                        "lease": "leasing payment to confirm", "till": "till report to check",
                        "member": "which company"}.get(n.kind, "one answer")
                evidence.append({"label": f"{item['merchant']} · {what}", "id": f"needs:{n.id}"})
            elif n.kind == "cost_center":
                rec = self.repo.transactions[n.subject_id]
                who = self.orchestrator.merchant_name(rec.tx)
                amount = format_money(abs(rec.tx.amount), rec.tx.currency)
                ask = n.question.prompt.lower().removesuffix("?") if n.question else "which one it is for"
                parts.append(f"{_article(who).capitalize()} {who} payment of {amount}: {ask}?")
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

    def _amount_answer(self, amount: Decimal) -> dict[str, Any] | None:
        """The latest tracked payment of exactly ``amount``, its document and how they were matched; None if none."""
        matches = sorted((r for r in self.repo.transactions.values() if abs(r.tx.amount) == amount),
                         key=lambda r: (r.tx.booked_on, r.id), reverse=True)
        if not matches:
            return None
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
            chain = self.orchestrator.refund_chain(tx_id=rec.id)
            if rec.tx.amount > 0 and chain:  # money back: the credit note, and the invoice it corrects (§20)
                answer = (f"The {format_money(amount)} refund on {when} came from {who}. {rec.match_headline} "
                          f"{agreed[:1].upper()}{agreed[1:]} all agree.")
            else:
                answer = (f"The {format_money(amount)} payment on {when} went to {who}. Its {_doc_phrase(doc)} "
                          f"arrived on {day_month(doc.received_at.astimezone(TZ).date(), self._today())}. "
                          f"{agreed[:1].upper()}{agreed[1:]} all agree.")
                if chain and rec.match_headline:
                    answer += f" {rec.match_headline}"
            evidence += [{"label": step["label"], "id": step["evidenceIds"][0]} for step in chain
                         if step["evidenceIds"] and step["id"] != rec.id and step["id"] not in rec.document_ids]
        elif rec.proof_evidence_ids:
            ob = self.orchestrator.obligations.proven_by(rec)
            reference = f", reference {ob.reference}" if ob is not None and ob.reference else ""
            letter = self.orchestrator.obligations.letter_word(ob)
            if ob is not None and ob.obligation.kind is ObligationKind.GRANT_PAYMENT:
                answer = (f"The {format_money(amount)} on {when} came from {who}. It is the grant the grant letter "
                          "announced, so nothing is missing.")
            elif ob is not None and ob.obligation.kind is ObligationKind.TOURIST_TAX:
                answer = (f"The {format_money(amount)} payment on {when} went to the municipality. It pays the tourist "
                          f"tax letter{reference}, so nothing is missing.")
            else:
                answer = (f"The {format_money(amount)} payment on {when} went to the tax office. It pays the tax letter"
                          f"{reference}, so nothing is missing.")
            evidence.insert(0, {"label": letter, "id": rec.proof_evidence_ids[0]})
        elif rec.decision is not None and not rec.decision.requires_document:
            answer = f"The {format_money(amount)} payment on {when} went to {who}. {rec.decision.reason}"
        else:
            answer = f"The {format_money(amount)} payment on {when} went to {who}. " + \
                self.orchestrator.missing.plan(rec)
        evidence.append({"label": f"{self._company_name(rec.company_id)} · {month.name}",
                         "id": f"month:{rec.company_id}:{month}"})
        return {"answer": answer, "evidence": evidence}

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
            if matched and not doc.on_hold and not report.already_known and doc.document.quality.value != "conflict":
                rec = matched[0]
                message = (f"Got it. It matches the {format_money(abs(rec.tx.amount), rec.tx.currency)} payment to "
                           f"{self.orchestrator.merchant_name(rec.tx)} on {day_month(rec.tx.booked_on, self._today())}.")
        if report.obligation_ids:
            message = self._letter_message(report)
        if report.transaction_ids:
            n = len(report.transaction_ids)
            message = f"Got it. I added {n} bank {'transaction' if n == 1 else 'transactions'}."
        if report.route == "unsupported":
            raise ServiceError(415, report.message)
        return {"ok": True, "message": message, "evidenceIds": list(dict.fromkeys(report.evidence_ids)),
                "documents": docs, "transactions": list(report.transaction_ids),
                "pendingLinks": list(report.pending_links), "storedOnly": bool(report.stored_only and not docs)}

    def _letter_message(self, report) -> str:  # type: ignore[no-untyped-def]
        """What the owner reads after sending a letter: the deadline it adds, or what it proved done (§24, §69)."""
        ob = self.repo.obligations.get(report.obligation_ids[-1])
        if ob is None:
            return "Got it."
        due = day_month(ob.obligation.due_on, self._today())
        if ob.obligation.id in report.confirmed_ids:
            return f"Got it. This closes “{ob.title}”. {ob.how}"
        if ob.done:
            return f"Got it. “{ob.title}” is already done. {ob.how}"
        if ob.payable:
            return "Got it. I added the deadline from this letter and will check the payment."
        if ob.informational:
            return f"Got it. {ob.title}: it renews on its own on {due}. Nothing to do unless you want to change it."
        return f"Got it. I added the deadline from this letter: {ob.title}, by {due}. " + \
            self.orchestrator.obligations.next_step(ob)

    def _upload_body(self, b: Mapping[str, Any]) -> dict[str, Any]:
        """``POST /api/evidence/upload`` (and ``/api/receipts``): the phone's upload, with what it sends about the
        capture (``capture_id``, ``page``, ``page_count``, ``hints``), JSON or multipart form fields."""
        return self.upload_receipt(_field(b, "sha256"), _b64(b.get("dataBase64") or b.get("data_base64")),
                                   filename=b.get("filename"),
                                   content_type=b.get("contentType") or b.get("content_type"),
                                   client_item_id=b.get("client_item_id") or b.get("clientItemId"),
                                   source=b.get("source") or "mobile_scan",
                                   captured_at=b.get("captured_at") or b.get("capturedAt"),
                                   capture=capture_fields(b))

    def upload_receipt(self, sha256: str, data: bytes, *, filename: str | None = None,
                       content_type: str | None = None, client_item_id: str | None = None,
                       source: str = "mobile_scan", captured_at: str | None = None,
                       capture: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Offline mobile upload (§43): the phone deletes its copy only when our hash equals its hash.

        ``capture`` (:func:`capture_fields`): the pages of one multi-page scan (kept together as one document)
        and the quality the phone noticed (a poor photo is read by the stronger engine, §11, §15).
        """
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
            source_kind=source_kind, capture=dict(capture or {}))
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

    def accountant_clients(self, companies: Collection[str] | None = None, *, prefix: str = "") -> dict[str, Any]:
        """The accountant's home table: one row per client company for the month being closed.

        ``companies`` limits the rows (an accountant who looks after some companies only);
        ``prefix`` goes before each row id (production: the business, so one accountant can
        list the companies of several businesses).
        """
        month = self._current_month()
        rows = [self._client_row(c, month, prefix=prefix) for c in self.repo.companies
                if companies is None or c in companies]
        rows.sort(key=lambda r: (r["complete"], r["name"]))
        return {"clients": rows}

    def _client_row(self, company_id: str, month: Month, *, prefix: str = "") -> dict[str, Any]:
        status = self._status(company_id, month)
        questions, flags = self._accountant_inputs(company_id, month)
        return {"id": f"{prefix}{company_id}", "name": self.repo.legal_names.get(company_id, company_id),
                "month": month.name, "complete": status.percent_closed, "missing": status.missing_documents,
                "needsAccountant": closure_needs_accountant(questions, flags)}

    def _month_txs(self, company_id: str, month: Month) -> list[TxRecord]:
        return [self.repo.transactions[i.subject_id] for i in self.repo.items_for(company_id, month)
                if i.subject_type == "transaction"]

    def _accountant_inputs(self, company_id: str, month: Month) -> tuple[list[Any], list[Any]]:
        """The month's questions and tax flags as the accountant workspace counts them (closure/accountant.py).

        "Needs accountant" counts what only the accountant can decide: tax flags, and questions
        routed *to* the accountant. The accountant's own questions are waiting for the owner or
        the system, so they are not the accountant's to settle.
        """
        repo = self.repo
        questions = [
            ClosureQuestion(question_id=q.id, text=q.text, direction=QuestionDirection.FROM_ACCOUNTANT,
                            status=QuestionStatus.ANSWERED if q.status == "answered" else
                            QuestionStatus.WAITING_FOR_OWNER, asked_at=q.asked_at)
            for q in sorted(repo.accountant_questions.values(), key=lambda q: q.id) if q.company_id == company_id]
        flags = [TaxFlag(flag_id=f["id"], item_id=item_id, description=f["title"])
                 for f, item_id in self._tax_flag_rows(company_id, self._month_txs(company_id, month), month)]
        return questions, flags

    def accountant_client(self, company_id: str, *, prefix: str = "",
                          companies: Collection[str] | None = None) -> dict[str, Any]:
        """One client company and month for the accountant (§28): the figures, the reconciliation
        (each payment with its document and the "Why?" behind the match), what is still open, the
        evidence as links, anomalies, tax flags, questions, rules and the export state.

        Links point at ``/api/accountant/clients/<prefix><company>/…``; ``companies`` (an
        accountant limited to some companies) leaves out evidence that also belongs to others.
        """
        if company_id not in self.repo.companies:
            raise ServiceError(404, "I can't find that client.")
        repo = self.repo
        month = self._current_month()
        ref = f"{prefix}{company_id}"
        row = self._client_row(company_id, month, prefix=prefix)
        status = self._status(company_id, month)
        items = repo.items_for(company_id, month)
        txs = [repo.transactions[i.subject_id] for i in items if i.subject_type == "transaction"]
        docs = [i for i in items if i.subject_type == "document"]
        matched = sum(1 for r in txs if r.document_ids or r.proof_evidence_ids)
        approvals = [n for n in self._open_needs(company_id) if n.kind == "approval"]
        anomalies = [{"id": f"an_{n.id}", "title": f"{display_name(repo.documents[n.subject_id].document.supplier_name)}"
                      " bank details changed", "detail": "Payment blocked until the owner confirms by phone.",
                      "tone": "risk"} for n in approvals]
        for name, before, after, _ in self.orchestrator.price_changes():
            if any(self.orchestrator.merchant_name(r.tx) == name and r.company_id == company_id for r in txs):
                pct = ((after - before) * 100 / before).quantize(Decimal("1"))
                anomalies.append({"id": f"an_price_{name.lower()}", "title": f"{name} price went up {pct}%",
                                  "detail": f"{format_money(before)} → {format_money(after)}.", "tone": "attention"})
        # Prices on this month's invoice lines that jumped against the product's last purchases (X20): a line, never
        # a hold.
        for c in self.orchestrator.line_prices.increases_in(company_id, month.first_day, month.last_day):
            p = c.purchase
            anomalies.append({
                "id": f"an_unit_{p.document_id}_{_SLUG.sub('-', p.product_key.lower()).strip('-')}",
                "title": f"{p.product} price went up {c.percent}%",
                "detail": f"{p.supplier}: {unit_money(c.before)} → {unit_money(c.after)} {p.per} on invoice "
                          f"{p.invoice}; {c.compared()}.",
                "tone": "attention"})
        anomalies += self._statement_anomalies(company_id)
        for record in self._month_documents(company_id, month, txs):
            # An unusual currency is a check, not a hold (checklist Q6): the accountant sees it here.
            signal = next((s for s in (record.fraud.signals if record.fraud else ())
                           if s.kind is SignalKind.UNUSUAL_CURRENCY and not s.hard_stop), None)
            if signal is not None:
                who = display_name(record.document.supplier_name)
                anomalies.append({"id": f"an_currency_{record.id}", "title": f"{who} invoice in an unusual currency",
                                  "detail": signal.owner_line, "tone": "attention"})
        index = self._evidence_index()
        allowed = frozenset(companies) if companies is not None else None

        def link(evidence_id: str, label: str) -> dict[str, str] | None:
            owners = index.get(evidence_id, set())
            if company_id not in owners or (allowed is not None and not owners <= allowed):
                return None  # not this client's, or it also holds a company this accountant may not see
            try:
                kind = _EVIDENCE_KIND.get(repo.evidence(evidence_id).format.value, "file")
            except Exception:
                kind = "file"
            return {"id": evidence_id, "label": label, "kind": kind,
                    "href": f"/api/accountant/clients/{ref}/evidence/{evidence_id}/file"}

        view, labels = self._client_view(company_id, month, status, items)
        questions = []
        for q in sorted(repo.accountant_questions.values(), key=lambda q: q.id):
            if q.company_id != company_id:
                continue
            asked = link(q.evidence_id, f"Your email · {day_month(q.asked_at.astimezone(TZ).date(), self._today())}")
            proof = [x for e in q.answer_evidence_ids[:3] if (x := link(e, labels.get(e, "Proof")))]
            # Answered only once the answer reached the accountant's mailbox (the send path accepted it).
            answered = q.status == "answered"
            questions.append({"id": q.id, "question": q.text, "status": "answered" if answered else "waiting",
                              **({"answer": q.answer} if q.answer and answered else {}),
                              "evidence": [x for x in (asked, *(proof if answered else ())) if x]})
        reconciliation = []
        for e in view.reconciliations:
            rec = repo.transactions[e.subject_id]
            status_label, tone = self._recon_status(e.status, rec)
            documents = []
            for d in e.documents:
                record = repo.documents.get(d.document_id)
                if record is None:
                    continue
                doc_link = link(record.evidence_ids[0], record.label) if record.evidence_ids else None
                documents.append({"id": d.document_id, "label": record.label,
                                  **({"href": doc_link["href"], "evidenceId": doc_link["id"]} if doc_link else {})})
            evidence = [x for ev in e.evidence
                        if (x := link(ev.id, labels.get(ev.id) or _evidence_word(ev.format.value, ev.filename)))]
            reconciliation.append({
                "id": rec.id, "itemId": e.item_id, "date": e.booked_on.isoformat(),
                "payee": self.orchestrator.merchant_name(rec.tx), "description": e.description,
                "amount": _num(abs(rec.tx.amount)), "direction": "in" if rec.tx.amount > 0 else "out",
                "currency": rec.tx.currency, "status": e.status, "statusLabel": status_label, "tone": tone,
                "documents": documents, "evidence": evidence, "why": list(e.why),
            })
        missing = [{"id": m.transaction_id, "date": m.booked_on.isoformat(),
                    "payee": self.orchestrator.merchant_name(repo.transactions[m.transaction_id].tx),
                    "amount": _num(abs(m.amount)) if m.amount is not None else None, "currency": m.currency,
                    "plan": self.orchestrator.missing.plan(repo.transactions[m.transaction_id])}
                   for m in view.missing]
        evidence_links = []
        for ev in view.evidence:
            x = link(ev.evidence_id, labels.get(ev.evidence_id) or _evidence_word(ev.format, ev.filename))
            if x is not None:
                evidence_links.append({**x, "source": ev.source,
                                       "sourceLabel": _SOURCE_WORD.get(ev.source, "Other"),
                                       "receivedAt": ev.retrieved_at.isoformat(), "filename": ev.filename,
                                       "itemIds": list(ev.item_ids)})
        accountant = repo.accountant_for(company_id)
        done = status.counts.done
        total = status.counts.total
        software = accountant.software if accountant else "your software"
        export = ({"state": "ready", "ready": total, "total": total,
                   "note": f"{month.name} is complete and ready to export to {software}."} if status.closed else
                  {"state": "partial", "ready": done, "total": total,
                   "note": f"{done} of {total} items are ready for {software}. The rest will follow when they close."})
        delivered = self.orchestrator.packages.owner_view(company_id, month, self._today())
        if delivered is not None and delivered.get("sentOn"):  # the package as the accountant sees it (§27 Day +1)
            export["delivery"] = {
                "state": delivered["state"], "sentOn": delivered["sentOn"], "confirmedOn": delivered.get("confirmedOn"),
                "text": f"Sent to you on {day_month(date.fromisoformat(delivered['sentOn']), self._today())}." + (
                    " You confirmed you have it." if delivered.get("confirmedOn") else
                    " Reply to that email to confirm you have it.")}
        rules = [{"id": r.id, "label": r.label, "scope": "client" if r.entity_ids or r.tenant_id else "all"}
                 for r in self.orchestrator.accountant_rules(company_id)]
        # Leases (X24), the cash box and till reports (X4, X5), memberships and fees (X12): only when the
        # company has them.
        books: dict[str, Any] = {}
        leases = self.orchestrator.leases.view(company_id)
        if leases:
            books["leases"] = leases
        cash_box = self.orchestrator.cash.view(company_id, month)
        if cash_box is not None:
            books["cashBox"] = cash_box
        members = self.orchestrator.members.view(company_id, month)
        if members is not None:
            books["memberships"] = members
        country = repo.companies[company_id].country
        return {**row, **books, "taxId": repo.companies[company_id].tax_id, "software": software,
                "country": country, "countryName": COUNTRY_NAMES.get(country, country),
                "period": {"key": str(month), "from": month.first_day.isoformat(),
                           "to": month.last_day.isoformat()},
                "accountant": {"name": accountant.person, "firm": accountant.firm, "email": accountant.email}
                if accountant else None,
                "evidence": [{"label": "Transactions", "value": str(len(txs))},
                             {"label": "Matched with a document", "value": str(matched)},
                             {"label": "Documents collected", "value": str(len(docs))},
                             {"label": "Still missing", "value": str(status.missing_documents)}],
                "evidenceLinks": evidence_links, "reconciliation": reconciliation, "missingDocuments": missing,
                "openReasons": list(view.open_reasons),
                "anomalies": anomalies, "taxFlags": self._tax_flags(company_id, txs, month), "questions": questions,
                "rules": rules, "exportState": export,
                "links": {"export": f"/api/accountant/clients/{ref}/export",
                          "rules": f"/api/accountant/clients/{ref}/rules"}}

    def _statement_anomalies(self, company_id: str) -> list[dict[str, str]]:
        """For the accountant: suppliers' statements that differ from the records, and the business's own
        documents a statement does not list (§28). Never booked, never a question for the owner by itself."""
        from backoffice.supplier_statements import not_listed_line, summary

        out = []
        for sr in sorted(self.repo.statements.values(), key=lambda s: s.document_id):
            if sr.company_id != company_id or sr.check is None or sr.check.complete:
                continue
            who = display_name(self.repo.documents[sr.document_id].document.supplier_name)
            detail = " ".join(p for p in (summary(sr.check, self._today()), not_listed_line(sr.check)) if p)
            out.append({"id": f"an_statement_{sr.document_id}", "title": f"{who}'s statement differs from the records",
                        "detail": detail, "tone": "attention"})
        return out

    def _month_documents(self, company_id: str, month: Month, txs: list[TxRecord]) -> list[DocumentRecord]:
        """The company's documents for the month: those its payments matched, and those dated in it."""
        repo = self.repo
        ids = [d for rec in txs for d in rec.document_ids]
        ids += [i.subject_id for i in repo.items_for(company_id, month) if i.subject_type == "document"]
        return [repo.documents[d] for d in sorted(dict.fromkeys(ids)) if d in repo.documents]

    def _client_view(self, company_id: str, month: Month, status: Any, items: Sequence[Any]
                     ) -> tuple[Any, dict[str, str]]:
        """closure/accountant.py's client view over the month's items, and a label for each evidence id."""
        repo = self.repo
        entries = []
        labels: dict[str, str] = {}
        for item in items:
            evidence_ids: list[str] = []
            if item.subject_type == "transaction":
                rec = repo.transactions[item.subject_id]
                records = [repo.documents[d] for d in rec.document_ids if d in repo.documents]
                labels[rec.evidence_id] = self._tx_evidence(rec)["label"]
                for p in rec.proof_evidence_ids:
                    labels.setdefault(p, self.orchestrator.obligations.letter_word(
                        self.orchestrator.obligations.proven_by(rec)))
                evidence_ids = [rec.evidence_id, *rec.proof_evidence_ids]
                kwargs: dict[str, Any] = {"transaction": rec.tx, "documents": [d.document for d in records],
                                          "why": self._recon_why(rec)}
            elif item.subject_type == "document":
                doc = repo.documents[item.subject_id]
                records = [doc]
                kwargs = {"documents": [doc.document],
                          "booked_on": doc.document.issue_date or doc.received_at.astimezone(TZ).date()}
            else:
                continue
            for d in records:
                evidence_ids += d.evidence_ids
            evidence = []
            for e in dict.fromkeys(evidence_ids):
                try:
                    evidence.append(repo.evidence(e))
                except Exception:  # an id without a stored original is shown nowhere
                    continue
            for d in records:
                for i, e in enumerate(d.evidence_ids):
                    found = next((ev for ev in evidence if ev.id == e), None)
                    labels.setdefault(e, self._doc_evidence(d)["label"] if i == 0 else
                                      f"{display_name(d.document.supplier_name)} · "
                                      f"{_evidence_word(found.format.value if found else '', None).lower()}")
            entry = PackageEntry.from_domain(item, evidence=evidence, **kwargs)
            entries.append(replace(entry, entity_id=company_id))  # items_for already chose the company
        decisions = [r.decision for r in self._month_txs(company_id, month) if r.decision is not None]
        questions, flags = self._accountant_inputs(company_id, month)
        view = build_client_view(repo.companies[company_id], status, entries=entries, decisions=decisions,
                                 tax_flags=flags, questions=questions)
        return view, labels

    def _recon_status(self, status: str, rec: TxRecord) -> tuple[str, str]:
        """Label and tone of one payment in the accountant's reconciliation list."""
        if status == "closed" and not rec.document_ids and rec.proof_evidence_ids:
            letter = self.orchestrator.obligations.letter_word(self.orchestrator.obligations.proven_by(rec))
            return f"Matched to the {letter.lower()}", "good"
        if status == "open":
            if rec.document_ids:
                return "Document found, being checked", "neutral"
            if rec.likely_document_ids:
                return "Likely match, being checked", "attention"
            if rec.decision is not None and rec.decision.requires_document:
                return "Document missing", "attention"
            return "In progress", "neutral"
        return _RECON_STATUS.get(status, ("In progress", "neutral"))

    def _recon_why(self, rec: TxRecord) -> list[str]:
        """The "Why?" of a payment's reconciliation (§54), in plain words."""
        if rec.document_ids:
            return [r.replace(": ", " ", 1) for r in rec.match_why]
        if rec.proof_evidence_ids:
            return self._tax_reasons(rec)
        if rec.id in self.repo.chargebacks and rec.match_why:  # a disputed card payment and what it is linked to
            return [r.replace(": ", " ", 1) for r in rec.match_why]
        if rec.decision is not None and not rec.decision.requires_document:
            return [rec.decision.reason]
        if rec.likely_document_ids:
            return ["A document looks like it belongs to this payment. I'm still checking it."]
        if rec.decision is not None and rec.decision.requires_document:
            return [self.orchestrator.missing.plan(rec)]  # what I'm doing to find it
        return []

    def _letter_payment_words(self, rec: TxRecord) -> tuple[str, str]:
        """(who, what) for a payment a letter proves: the tax office, the municipality's tourist tax, a grant."""
        ob = self.orchestrator.obligations.proven_by(rec)
        kind = ob.obligation.kind if ob is not None else None
        if kind is ObligationKind.GRANT_PAYMENT:
            return (ob.agency if ob is not None and ob.agency else display_name(rec.tx.counterparty)), "Grant received"
        if kind is ObligationKind.TOURIST_TAX:
            return "Municipality", "Tourist tax"
        return "Tax office", "Tax payment"

    def _tax_reasons(self, rec: TxRecord) -> list[str]:
        ob = self.orchestrator.obligations.proven_by(rec)
        amount = format_money(abs(rec.tx.amount), rec.tx.currency)
        if ob is not None and ob.obligation.kind is ObligationKind.GRANT_PAYMENT:
            reasons = [f"Grant letter announces {amount}", f"Money received {amount}"]
            if ob.agency:
                reasons.append(f"From {ob.agency}, as the letter says")
            return reasons
        letter = self.orchestrator.obligations.letter_word(ob)
        reasons = [f"{letter} asks for {amount}", f"Bank payment {amount}"]
        if ob is not None and ob.reference:
            reasons.append(f"Reference {ob.reference} matches")
        if ob is not None and rec.tx.booked_on <= ob.obligation.due_on:
            reasons.append(f"Paid before the deadline of {day_month(ob.obligation.due_on)}")
        return reasons

    def _tax_flag_rows(self, company_id: str, txs: Sequence[TxRecord], month: Month | None = None
                       ) -> list[tuple[dict[str, str], str]]:
        """What the accountant should look at (§28), each with the item it is about. Accountant-facing
        only: the owner is never asked."""
        repo = self.repo
        flags: list[tuple[dict[str, str], str]] = []
        # Each document once, with the item the flag is about: its payment's, else its own.
        records: dict[str, tuple[DocumentRecord, str]] = {}
        for rec in txs:
            for doc_id in rec.document_ids:
                records.setdefault(doc_id, (repo.documents[doc_id], rec.item_id))
        if month is not None:  # and the month's own documents: paid in cash, still waiting, or held
            for item in repo.items_for(company_id, month):
                record = repo.documents.get(item.subject_id) if item.subject_type == "document" else None
                if record is not None and not record.supporting:
                    records.setdefault(record.id, (record, record.item_id))
        for doc_id, (record, item_id) in records.items():
            doc = record.document
            if doc.doc_type is DocumentType.INVOICE_RECEIPT and doc.vat_amount == 0 and \
                    record.country == repo.companies[company_id].country and \
                    self.repo.pack_for(company_id).is_private_person(doc.supplier_tax_id):
                flags.append(({"id": f"t_{doc_id}",
                               "title": f"Rent paid to a private landlord · {format_money(doc.gross_amount or 0)}",
                               "detail": "No withholding shown on the receipt. Whether it applies is your call."},
                              item_id))
            if not record.sales and possible_capital_asset(doc, record.text):
                net = doc.net_amount if doc.net_amount is not None else doc.gross_amount
                paid = " Paid in cash." if record.paid_in_cash else ""
                flags.append(({"id": f"t_asset_{doc_id}",
                               "title": f"{CAPITAL_ASSET_FLAG} · {format_money(net or 0, doc.currency)} before VAT",
                               "detail": f"{display_name(doc.supplier_name)} invoice"
                                         f"{' ' + doc.invoice_number if doc.invoice_number else ''}. It may be "
                                         f"equipment to depreciate rather than a cost of the month.{paid}"},
                              item_id))
            foreign = _foreign_vat_flag(record)  # reverse charge, or VAT charged abroad (checklist X31)
            if foreign is not None:
                flags.append((foreign, item_id))
            exchange = fx_accountant_flag(self.orchestrator, doc_id)  # paid at another rate than its date's (I11)
            if exchange is not None:
                flags.append((exchange, item_id))
        for rec in sorted(repo.transactions.values(), key=lambda r: (r.tx.booked_on, r.id)):
            note = rec.company_note
            if note is None or company_id not in note or (month is not None and Month.of(rec.tx.booked_on) != month):
                continue
            paid_by, carried_by, named = note
            amount = format_money(abs(rec.tx.amount), rec.tx.currency)
            if paid_by != carried_by:
                flags.append(({"id": f"t_interco_{rec.id}",
                               "title": f"Inter-company payment · {amount}",
                               "detail": f"{repo.company_name(paid_by)} paid {repo.company_name(carried_by)}'s "
                                         f"{self.orchestrator.merchant_name(rec.tx)} invoice from its own account. "
                                         f"The owner confirmed {repo.company_name(carried_by)} carries it, so it owes "
                                         f"{repo.company_name(paid_by)} {amount}."},
                              rec.item_id))
            elif carried_by != named:
                flags.append(({"id": f"t_interco_{rec.id}",
                               "title": f"Invoice addressed to another company · {amount}",
                               "detail": f"The owner chose {repo.company_name(carried_by)} for a "
                                         f"{self.orchestrator.merchant_name(rec.tx)} invoice addressed to "
                                         f"{repo.company_name(named)}. Whether its VAT can be deducted is your call."},
                              rec.item_id))
        flags += self._import_vat_flags(company_id, txs, records)
        return flags

    def _import_vat_flags(self, company_id: str, txs: Sequence[TxRecord], records: Mapping[str, Any]
                          ) -> list[tuple[dict[str, str], str]]:
        """Import VAT paid at customs (X28): on the customs declaration, never on a supplier's invoice, so the
        accountant is shown it with its import purchase. Accountant-facing only: the owner is never asked."""
        from backoffice.imports import customs_vat, find_chains

        repo = self.repo
        text_of = self.orchestrator.evidence_text
        tx_ids = {r.id for r in txs}
        out: list[tuple[dict[str, str], str]] = []
        for chain in find_chains(repo, company_id=company_id, text_of=text_of):
            duties = [p for p in chain.pieces if p.role == "duties" and p.id in tx_ids]
            declared = [p for p in chain.pieces if p.role == "customs" and p.kind == "document" and p.id in records]
            if not duties and not declared:
                continue  # not in this month
            vat, customs = customs_vat(repo, chain, text_of)
            item_id = repo.transactions[duties[0].id].item_id if duties else repo.documents[declared[0].id].item_id
            mrn = f" (MRN {chain.mrn})" if chain.mrn else ""
            charged = f"charged {format_money(vat)} of import VAT" if vat is not None else "charges import VAT"
            paid = (f", paid to customs on {day_month(duties[0].on, self._today())}" if duties and duties[0].on
                    else ", not paid yet")
            out.append(({"id": f"t_import_vat_{chain.id}",
                         "title": IMPORT_VAT_FLAG + (f" · {format_money(vat)}" if vat is not None else ""),
                         "detail": f"Part of {chain.name}. The customs declaration{mrn} {charged}{paid}. It is not on "
                                   "any supplier's invoice. Whether and how it is deducted is your call."},
                        item_id))
        return out

    def _tax_flags(self, company_id: str, txs: Sequence[TxRecord], month: Month | None = None
                   ) -> list[dict[str, str]]:
        """What the accountant should look at (§28). Accountant-facing only: the owner is never asked."""
        return [f for f, _ in self._tax_flag_rows(company_id, txs, month)]

    # ----------------------------------------------------------------- evidence as files

    def _document_company(self, doc: DocumentRecord) -> str | None:
        return doc.document.entity_id or self.repo.item_company(self.repo.items[doc.item_id])

    def _evidence_index(self) -> dict[str, set[str]]:
        """Evidence id -> the companies whose records it supports ("?" when a record's company is not known)."""
        repo = self.repo
        index: dict[str, set[str]] = {}

        def add(evidence_id: str, company: str | None) -> None:
            if evidence_id:
                index.setdefault(evidence_id, set()).add(company or "?")

        for d in repo.documents.values():
            company = self._document_company(d)
            for e in d.evidence_ids:
                add(e, company)
        for r in repo.transactions.values():
            for e in (r.evidence_id, *r.proof_evidence_ids):
                add(e, None if r.private else r.company_id)
        for q in repo.accountant_questions.values():
            add(q.evidence_id, q.company_id)
        for o in repo.obligations.values():
            add(o.evidence_id, o.obligation.entity_id)
        return index

    def _evidence_filename(self, ev: Any) -> str:
        if ev.filename:
            return str(ev.filename)
        ext = {"application/json": ".json", "message/rfc822": ".eml", "text/plain": ".txt", "application/pdf": ".pdf",
               "application/xml": ".xml", "text/html": ".html"}.get(ev.mime_type or "", ".bin")
        return f"{ev.id}{ext}"

    # ----------------------------------------------------------------- sensitive documents (§52, checklist X32)

    def note_opened(self, document_ids: Sequence[str]) -> None:
        """A sensitive document's original is being read in this request (for the access log)."""
        self._opened.extend(d for d in document_ids if d not in self._opened)

    def take_opened(self) -> list[str]:
        """The sensitive documents whose originals the last request read (and forget them)."""
        opened, self._opened = list(self._opened), []
        return opened

    def record_access(self, document_ids: Sequence[str], *, who: str, role: str,
                      how: str = "opened the original") -> dict[str, Any]:
        """Log reads of sensitive documents' originals (the production server's ``documents.opened`` event)."""
        entries = self.orchestrator.record_access(document_ids, who=who, role=role, at=self._now(), how=how)
        return {"ok": True, "recorded": len(entries)}

    def access_log(self) -> dict[str, Any]:
        """``GET /api/documents/access-log``: every read of a sensitive document's original, newest first, and
        the documents that are sensitive (the owner only)."""
        repo = self.repo
        role_words = {"owner": "Owner", "admin": "Owner", "accountant": "Accountant", "api": "Accounting software"}
        entries = []
        for e in sorted(repo.document_access, key=lambda e: e.at, reverse=True):
            record = repo.documents.get(e.document_id)
            entries.append({"at": e.at.isoformat(), "documentId": e.document_id,
                            "document": record.label if record is not None else e.document_id, "who": e.who,
                            "role": e.role, "roleLabel": role_words.get(e.role, e.role.title()), "how": e.how})
        from backoffice.sensitivity import CATEGORY_WORDS

        sensitive = [{"id": d.id, "label": d.label, "companyId": d.document.entity_id or "",
                      "reason": CATEGORY_WORDS.get(d.sensitive or "", ""), "markedBy": d.sensitive_by or "wording"}
                     for d in sorted(repo.documents.values(), key=lambda d: d.id) if d.sensitive]
        return {"entries": entries, "sensitiveDocuments": sensitive}

    def document_sensitive(self, document_id: str, body: Mapping[str, Any] | None) -> dict[str, Any]:
        """``POST /api/documents/<id>/sensitive`` ``{sensitive: true|false}``: the owner marks a document."""
        value = (body or {}).get("sensitive", True)
        if not isinstance(value, bool):
            raise ServiceError(400, "Say true or false.")
        try:
            record = self.orchestrator.mark_sensitive(document_id, value)
        except KeyError:
            raise ServiceError(404, "I can't find that document.") from None
        message = ("Done. Only you and the company's accountant can see it now, and I note every time its original "
                   "is opened." if value else "Done. It is no longer marked sensitive.")
        return {"ok": True, "message": message,
                "document": {"id": record.id, "sensitive": bool(record.sensitive)}}

    def _opened_evidence(self, evidence_id: str) -> None:
        self.note_opened([d.id for d in self.repo.documents.values() if d.sensitive and evidence_id in d.evidence_ids])

    # ----------------------------------------------------------------- an outlet manager (checklist X37)

    def dispatch_manager(self, method: str, path: str, body_json: Any,
                         cost_centers: Collection[str]) -> tuple[int, dict[str, Any]]:
        """Route one request from an outlet manager (production ``manager`` role): their outlets' questions,
        documents, payments, spending and receipts, nothing else (backoffice.managers)."""
        from backoffice.managers import ManagerViews

        views = ManagerViews(self, cost_centers)
        who = self.viewer[0] if self.viewer and self.viewer[1] == "manager" else ""
        r = re.compile
        seg = r"([^/]+)"
        routes = (
            ("GET", r("/api/manager/outlets"), lambda b: views.outlets()),
            ("GET", r("/api/needs-you"), lambda b: views.needs_you()),
            ("POST", r(f"/api/needs-you/{seg}/answer"),
             lambda b, i: views.answer(i, _field(b, "option_id", "optionId"), bool(b.get("remember", False)),
                                       b.get("split"))),
            ("GET", r("/api/documents"), lambda b: views.documents(b)),
            ("GET", r(f"/api/documents/{seg}/file"), lambda b, d: views.document_file(d)),
            ("GET", r(f"/api/documents/{seg}"), lambda b, d: views.document(d)),
            ("GET", r(f"/api/transactions/{seg}"), lambda b, t: views.transaction(t)),
            ("GET", r(f"/api/cost-centers/{seg}"), lambda b, c: views.cost_center(c, b)),
            ("GET", r(f"/api/cost-centers/{seg}/statement"), lambda b, c: views.statement(c, b)),
            ("POST", r("/api/manager/receipts"), lambda b: views.receipt(b, by=who)),
        )
        return self._route(method, path, body_json, routes,
                           refuse=(403, {"error": "forbidden", "message": "You don't have access to that."}))

    def evidence_file(self, evidence_id: str, *, companies: Collection[str] | None = None,
                      company_id: str | None = None) -> dict[str, Any]:
        """One original, as stored (§55): ``{filename, contentType, data}`` (base64).

        ``company_id``: it must support that company's records. ``companies``: every record
        it supports must belong to one of them (an accountant limited to some companies, §52).
        """
        repo = self.repo
        owners = self._evidence_index().get(evidence_id, set())
        if company_id is not None and company_id not in owners:
            raise ServiceError(404, "I can't find that evidence.")
        if companies is not None and (not owners or not owners <= set(companies)):
            raise ServiceError(404, "I can't find that evidence.")
        try:
            ev = repo.registry.get(repo.tenant_id, evidence_id)
            data = repo.registry.open(repo.tenant_id, evidence_id)
        except (ObjectNotFound, IntegrityError, ValueError):
            raise ServiceError(404, "I can't find that evidence.") from None
        self._opened_evidence(evidence_id)
        return {"filename": self._evidence_filename(ev), "contentType": ev.mime_type or "application/octet-stream",
                "data": base64.b64encode(data).decode()}

    def accountant_evidence_file(self, company_id: str, evidence_id: str, *,
                                 companies: Collection[str] | None = None) -> dict[str, Any]:
        if company_id not in self.repo.companies:
            raise ServiceError(404, "I can't find that client.")
        return self.evidence_file(evidence_id, companies=companies, company_id=company_id)

    def accountant_export(self, company_id: str) -> dict[str, Any]:
        """The month's documents for one client company as a ZIP (originals, ledger.csv, manifest.json)."""
        if company_id not in self.repo.companies:
            raise ServiceError(404, "I can't find that client.")
        month = self._current_month()
        name, data, count = self.assistant.export_zip(company_id=company_id, date_from=month.first_day,
                                                      date_to=month.last_day)
        return {"filename": name, "contentType": "application/zip", "count": count,
                "data": base64.b64encode(data).decode()}

    # ----------------------------------------------------------------- accountant rules (§28)

    def accountant_rule(self, text: str, scope: str = "client", company_id: str | None = None) -> dict[str, Any]:
        if not isinstance(text, str) or not text.strip():
            raise ServiceError(400, "Write the rule in one sentence, e.g. “Treat all Adobe subscriptions as Software”.")
        if company_id is not None and company_id not in self.repo.companies:
            raise ServiceError(404, "I can't find that client.")
        try:
            rule, affected = self.orchestrator.accountant_rule(text, scope or "client", company_id)
        except PermissionError:
            raise ServiceError(409, "No accountant is connected yet.") from None
        except ValueError as exc:
            message = str(exc)
            if message.startswith("duplicate rule id"):
                raise ServiceError(409, "I already know that rule.") from None
            raise ServiceError(400, message) from None
        noun = "payment" if affected == 1 else "payments"
        where = f" for {self._company_name(company_id)}" if company_id and rule.entity_ids else ""
        return {"ok": True, "rule": {"id": rule.id, "label": rule.label, "scope": rule.scope.value,
                                     "companyIds": list(rule.entity_ids)},
                "affected": affected,
                "message": f"Done. {rule.label}{where}. It applies to {affected} {noun} so far."}

    # ----------------------------------------------------------------- clients invited by the accountant (§29)

    def _invitations(self) -> dict[str, dict[str, Any]]:
        if getattr(self, "_invites", None) is None:
            self._invites = {}
        return self._invites

    def invitations_list(self) -> dict[str, Any]:
        now = self._now()
        out = []
        for rec in sorted(self._invitations().values(), key=lambda r: r["id"], reverse=True):
            item = {k: v for k, v in rec.items() if k != "tokenHash"}
            msg = self.assistant.outbox.get(rec.get("messageId") or "")
            if rec["status"] == "waiting" and msg is not None and msg.status == "sent":
                item["status"], item["statusLabel"] = "sent", "Sent"  # a transport took it since
            if rec["acceptedAt"]:
                item["status"], item["statusLabel"] = "accepted", "Accepted"
            elif datetime.fromisoformat(rec["expiresAt"]) <= now:
                item["status"], item["statusLabel"] = "expired", "Expired"
            out.append(item)
        return {"invitations": out}

    def invite_client(self, body: Mapping[str, Any] | None) -> dict[str, Any]:
        """The accountant invites a client business by email: "Your accountant has enabled Back Office for you."

        The single-use token is created here and only its SHA-256 is kept. The email goes out
        through the send path (backoffice.mailer): it counts as sent only once the transport
        accepted it (the demo's is its simulated outbox); without one it is written and waits.
        """
        from backoffice import invitations as inv
        from backoffice.assistant import OutboxMessage
        from backoffice.mailer import is_simulated

        try:
            clean = inv.clean_invitation(dict(body or {}))
        except ValueError as exc:
            raise ServiceError(400, str(exc)) from None
        if clean.email == self.repo.owner.email.lower():
            raise ServiceError(400, "That is this business's own address. Enter your client's email.")
        now = self._now()
        _, digest = inv.new_token()
        expires = inv.expiry(now)
        acct = self.repo.accountant
        inviter = acct.person if acct else "Your accountant"
        firm = clean.firm or (acct.firm if acct else "")
        subject, text = inv.invitation_email(inviter=inviter, firm=firm, client_name=clean.client_name, link=None,
                                             expires_at=expires)
        op = self.assistant
        mailer = getattr(self, "mailer", None)
        if mailer is not None:
            try:
                mailer.send([clean.email], subject, text, [])  # sent only once the transport accepted it
            except Exception:
                raise ServiceError(502, "I couldn't send the invitation email. Please try again in a few minutes.") \
                    from None
            if is_simulated(mailer):
                delivery = "Recorded in the outbox. Email delivery is not connected in this demo."
                status, label = "demo_outbox", "In the demo outbox"
            else:
                delivery, status, label = "Sent.", "sent", "Sent"
        else:
            delivery = "Not sent yet: email sending is not set up here, so it is waiting to be sent."
            status, label = "waiting", "Waiting to be sent"
        msg = OutboxMessage(id=op._id("out"), to=[clean.email], subject=subject, body=text,
                            status="waiting" if status == "waiting" else "sent",
                            sent_at=None if status == "waiting" else now.isoformat(), delivery=delivery)
        op.outbox[msg.id] = msg
        self._invite_seq = getattr(self, "_invite_seq", 0) + 1
        iid = f"inv_{self._invite_seq:03d}"
        self._invitations()[iid] = {
            "id": iid, "email": clean.email, "clientName": clean.client_name or None, "taxIds": list(clean.tax_ids),
            "createdAt": now.isoformat(), "expiresAt": expires.isoformat(), "status": status, "statusLabel": label,
            "acceptedAt": None, "messageId": msg.id, "tokenHash": digest,
        }
        self.orchestrator.log("accountant", "client_invited", subject_id=iid,
                              values={"companies": len(clean.tax_ids), "delivery": status},
                              actor=f"accountant:{acct.id}" if acct else "system")
        public = next(i for i in self.invitations_list()["invitations"] if i["id"] == iid)
        message = (f"Done. I sent the invitation to {clean.email}." if status == "sent" else
                   f"Done. The invitation to {clean.email} is in the outbox. This demo does not send real email."
                   if status == "demo_outbox" else
                   f"The invitation to {clean.email} is written but not sent: email sending is not set up here.")
        return {"ok": status != "waiting", "invitation": public, "message": message}

    # ----------------------------------------------------------------- an accountant of some companies (§52)

    def dispatch_scoped(self, method: str, path: str, body_json: Any, companies: Collection[str], *,
                        prefix: str = "") -> tuple[int, dict[str, Any]]:
        """Route one request from an accountant who may see only ``companies`` (production memberships).

        Only the accountant's own routes answer, each filtered to those companies; every other
        route is refused. ``prefix`` is put before client ids in what comes back.
        """
        return self._route(method, path, body_json, self._scoped_routes(frozenset(companies), prefix),
                           refuse=(403, {"error": "forbidden", "message": "You don't have access to that."}))

    def _scoped_routes(self, allowed: frozenset[str], prefix: str):  # type: ignore[no-untyped-def]
        r = re.compile
        seg = r"([^/]+)"

        def mine(company_id: str) -> str:
            if company_id not in allowed or company_id not in self.repo.companies:
                raise ServiceError(404, "I can't find that client.")
            return company_id

        def company(company_id: str) -> dict[str, Any]:
            return self.company(mine(company_id))

        return (
            ("GET", r("/api/accountant/clients"), lambda b: self.accountant_clients(allowed, prefix=prefix)),
            ("GET", r(f"/api/accountant/clients/{seg}"),
             lambda b, c: self.accountant_client(mine(c), prefix=prefix, companies=allowed)),
            ("GET", r(f"/api/accountant/clients/{seg}/evidence/{seg}/file"),
             lambda b, c, e: self.accountant_evidence_file(mine(c), e, companies=allowed)),
            ("GET", r(f"/api/accountant/clients/{seg}/export"), lambda b, c: self.accountant_export(mine(c))),
            # Rules from an accountant of some companies stay with those companies.
            ("POST", r(f"/api/accountant/clients/{seg}/rules"),
             lambda b, c: self.accountant_rule(_field(b, "text"), "client", mine(c))),
            ("POST", r("/api/accountant/rules"),
             lambda b: self.accountant_rule(_field(b, "text"), "client", mine(str(b.get("companyId") or "")))),
            ("GET", r("/api/companies"),
             lambda b: {"companies": [c for c in self.companies()["companies"] if c["id"] in allowed]}),
            ("GET", r(f"/api/companies/{seg}"), lambda b, c: company(c)),
            ("GET", r(f"/api/months/{seg}/{seg}"), lambda b, c, m: self.month(mine(c), m)),
            ("GET", r("/api/documents"), lambda b: self.documents_list(b, companies=allowed)),
            ("GET", r(f"/api/documents/{seg}/file"), lambda b, d: self.document_download(d, companies=allowed)),
            ("POST", r("/api/documents/export"), lambda b: self.documents_export(b, companies=allowed)),
            ("GET", r(f"/api/evidence/{seg}/file"), lambda b, e: self.evidence_file(e, companies=allowed)),
        )

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
        unit = self.orchestrator.line_prices.recent_increases(today)  # prices on invoice lines that jumped (X20)
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
            {"id": "f_increased", "value": str(len(changes) + len(unit)), "label": "costs went up",
             "tone": "attention" if changes or unit else "good",
             "examples": [f"{n} {format_money(b)} → {format_money(a)}" for n, b, a, _ in changes]
             + [c.line() for c in unit]},
            {"id": "f_other", "value": str(len(other)), "label": "items may belong to another company",
             "tone": "attention" if other else "good",
             "examples": [f"{self.orchestrator.merchant_name(repo.transactions[n.subject_id].tx)} "
                          f"{format_money(abs(repo.transactions[n.subject_id].tx.amount))} paid with "
                          f"{repo.accounts[repo.transactions[n.subject_id].tx.account_id].label}" for n in other]},
        ]
        chain = repo.audit.verify(repo.tenant_id)
        names = [e.name for e in repo.entities]
        if not names:
            company = "Your business"
        else:
            company = names[0] if len(names) == 1 else ", ".join(names[:-1]) + f" and {names[-1]}"
        lines = list(report.lines)
        if unit:
            lines.append(f"{_count_word(len(unit)).capitalize()} {'price' if len(unit) == 1 else 'prices'} on "
                         f"invoices went up {threshold_words()} or more: " + "; ".join(c.line() for c in unit))
        return {"companyName": company, "periodLabel": "the last 90 days", "headline": report.headline,
                "lines": lines, "callToAction": report.call_to_action, "findings": findings,
                "log": {"records": chain.checked, "intact": chain.ok, "head": chain.head_hash}}

    # ----------------------------------------------------------------- Plan and billing (§60, §61)

    def billing_usage(self, *, today: date | None = None, users: int | None = None, clients: int = 0) -> Usage:
        """What the business uses this month: companies, and documents that arrived this month (those issued
        before it joined are the free audit's history and never count)."""
        day = today or self._today()
        month = Month.of(day)
        start = self.billing.started_on
        documents = 0
        for d in self.repo.documents.values():
            received = d.received_at.astimezone(TZ).date()
            if not month.contains(received):
                continue
            if start is not None and (d.document.issue_date or received) < start:
                continue
            documents += 1
        return Usage(str(month), len(self.repo.companies), documents, users, clients)

    def _billing_over(self, state: BillingState, day: date) -> tuple[list[str], date | None]:
        """The evidence limits (companies, documents) ``state`` is over on ``day``, and when its grace started."""
        usage = self.billing_usage(today=day)
        over = [n for n in state.over(usage, day) if n in ("companies", "documents")]
        return over, (state.over_since or day) if over else None

    def intake_held(self, day: date | None = None, state: BillingState | None = None) -> bool:
        """Would new evidence wait for the plan (kept, not read) on ``day``? (backoffice.billing)"""
        state = state or self.billing
        day = day or self._today()
        if not state.enforced or state.is_demo:
            return False
        over, since = self._billing_over(state, day)
        if not over:
            return False
        return replace(state, over_since=since, waiting=[]).holding(self.billing_usage(today=day), day)

    def release_due(self, day: date, event: Mapping[str, Any] | None = None) -> bool:
        """Would the evidence waiting for the plan be read on ``day`` (after the provider ``event``, if any)?"""
        if not self.billing.waiting:
            return False
        state = replace(self.billing, events=list(self.billing.events), waiting=[])
        if event is not None:
            state.apply(event, day)
        return not self.intake_held(day, state)

    def billing_check(self) -> None:
        """Keep the grace period's start: the first day over a limit the business is held to, cleared once it is
        within its limits again. The owner reads one plain line about it in Activity (§69)."""
        state = self.billing
        if not state.enforced or state.is_demo:
            return
        today = self._today()
        over, since = self._billing_over(state, today)
        if over and state.over_since is None:
            state.over_since = since
            self.orchestrator.log("billing", "over_limit", values={"over": over, "plan": state.effective(today).id})
            prompt = state.prompt(self.billing_usage(today=today), today)
            if prompt:
                self.orchestrator.activity(self._now(), "waiting", prompt)
        elif not over and state.over_since is not None:
            state.over_since = None
            self.orchestrator.log("billing", "within_limits", values={"plan": state.effective(today).id})

    def _hold(self, item: dict[str, Any]) -> None:
        state = self.billing
        state.waiting.append({**item, "at": self._now().isoformat()})
        self.orchestrator.log("billing", "evidence_held", values={"kind": item["kind"], "waiting": len(state.waiting)})
        if len(state.waiting) == 1:
            self.orchestrator.activity(self._now(), "waiting",
                                       f"Your {state.effective(self._today()).name} plan's limit is reached, so new "
                                       "documents are kept safely and wait until you upgrade or the month turns. "
                                       "Nothing is lost.")

    def hold_request(self, path: str, body_json: Any) -> tuple[int, dict[str, Any]]:
        """Keep one upload or share without reading it: it waits for the plan (backoffice.billing). The phone may
        delete its copy (the bytes are stored and their hash checked); nothing is ever lost."""
        import hashlib

        body = _body(body_json)
        message = "Saved. It waits for your plan: it is read as soon as you upgrade or the month turns."
        if path in ("/api/evidence/upload", "/api/receipts"):
            try:
                data = _b64(body.get("dataBase64") or body.get("data_base64"))
            except ServiceError as exc:
                return exc.status, {"error": "bad_request", "message": exc.message}
            digest = hashlib.sha256(data).hexdigest()
            claimed = str(body.get("sha256") or "").strip().lower()
            if not data or (claimed and claimed != digest):
                return self._route("POST", path, body, self._routes())  # refused as usual: nothing to keep
            self._hold({"kind": "request", "path": path, "body": dict(body)})
            return 200, {"client_upload_id": body.get("client_item_id") or body.get("clientItemId")
                         or f"rcpt-{digest[:40]}", "status": "stored", "sha256": digest,
                         "received_at": self._now().isoformat(), "evidence_id": None, "delete_local": True,
                         "retry": False, "reason": None, "owner_message": message, "signature": None,
                         "duplicate": False, "message": message, "held": True}
        if path == "/api/evidence" and not (body.get("dataBase64") or body.get("data_base64")):
            return self._route("POST", path, body, self._routes())  # nothing to keep: refused as usual
        self._hold({"kind": "request", "path": path, "body": dict(body)})
        return 200, {"ok": True, "message": message, "evidenceIds": [], "documents": [], "transactions": [],
                     "pendingLinks": [], "storedOnly": True, "held": True}

    def release_held(self) -> int:
        """Read what waited for the plan, in the order it arrived, once the plan covers it (an upgrade, a new
        month). Returns how many items were read."""
        state = self.billing
        if not state.waiting or self.intake_held():
            return 0
        items, state.waiting = list(state.waiting), []
        decides, self.decides_holds = self.decides_holds, False
        documents = 0
        try:
            for item in items:
                if item.get("kind") == "mail":
                    for raw in item.get("messages") or ():
                        report = self.orchestrator.ingest_file(raw, filename="message.eml",
                                                               content_type="message/rfc822",
                                                               source_kind=SourceKind.EMAIL, origin="email")
                        documents += len(set(report.document_ids))
                else:
                    status, out = self._route("POST", str(item.get("path")), item.get("body") or {}, self._routes())
                    documents += len(out.get("documents") or ()) if status == 200 else 0
        finally:
            self.decides_holds = decides
        self.orchestrator.log("billing", "held_evidence_released", values={"items": len(items),
                                                                          "documents": documents})
        self.orchestrator.activity(self._now(), "checked", f"Your plan covers it again: I read the "
                                   f"{count_phrase(len(items), 'item')} that were waiting.")
        self.orchestrator.run()
        self.billing_check()
        return len(items)

    def billing_event(self, event: Mapping[str, Any]) -> dict[str, Any]:
        """One payment-provider event (already verified and reduced, backoffice.billing.reduce_event), applied
        once; what waited for the plan is read when the new plan covers it."""
        state = self.billing
        if state.is_demo:
            raise ServiceError(409, "This is the demo, so nothing here is billed.")
        before = (state.plan, state.status, state.payment_failed_on)
        result = state.apply(event, self._today())
        if not result.get("applied"):
            return {"ok": True, "duplicate": True}
        self.orchestrator.log("billing", "provider_event", values={
            "type": str(event.get("type")), "event": str(event.get("id")), "plan": state.plan, "status": state.status},
            actor="system:billing")
        today = self._today()
        if (state.plan, state.status) != before[:2] and state.status == "active" and state.plan != "free":
            self.orchestrator.activity(self._now(), "checked", f"Your {state.chosen().name} plan is active.")
        if state.payment_failed_on is not None and before[2] is None:
            self.orchestrator.activity(self._now(), "waiting", state.notice(today) or "")
        if state.plan == "free" and before[0] != "free":
            self.orchestrator.activity(self._now(), "checked", "Your plan ended. You are on the Free plan now. "
                                       "Nothing is deleted.")
        self.billing_check()
        released = self.release_held()
        return {"ok": True, "plan": state.plan, "status": state.status, "released": released}

    def billing_tick(self) -> int:
        """The day's billing work (a new month, a grace period that ended): what waited is read when it may be."""
        self.billing_check()
        return self.release_held()

    def billing_view(self, body: Mapping[str, Any] | None = None, *, users: int | None = None, clients: int = 0,
                     payments: bool = False) -> dict[str, Any]:
        """``GET /api/billing`` (owner only): the plan, what the business uses, its limits and, when over one, one
        plain line. ``payments``: a payment provider is set up (the production server); the demo never has one."""
        state = self.billing
        today = self._today()
        plans = [p.public() for p in PLANS.values()]
        if state.is_demo:
            usage = self.billing_usage(users=users, clients=clients)
            return {"plan": {**DEMO_PLAN.public(), "status": "active", "renewsOn": None}, "demo": True,
                    "usage": usage.public(), "limits": DEMO_PLAN.public()["limits"], "over": [], "prompt": None,
                    "notice": None, "graceUntil": None, "waiting": 0, "held": False, "plans": plans,
                    "canUpgrade": False, "canManage": False,
                    "message": "This is the demo business, so nothing here is billed."}
        usage = self.billing_usage(users=users, clients=clients)
        plan = state.effective(today)
        grace = state.grace_until(Usage(usage.month, usage.companies, usage.documents, None, usage.clients), today)
        return {"plan": {**state.chosen().public(), "status": state.status, "renewsOn": _iso(state.renews_on)},
                "demo": False, "usage": usage.public(), "limits": plan.public()["limits"],
                "over": state.over(usage, today), "prompt": state.prompt(usage, today), "notice": state.notice(today),
                "graceUntil": _iso(grace) if state.enforced else None, "waiting": len(state.waiting),
                "held": self.intake_held(today), "plans": plans, "canUpgrade": payments,
                "canManage": payments and bool(state.customer), "message": None}

    def billing_refused(self, body: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """``POST /api/billing/checkout`` and ``/portal`` without a payment provider (the demo, the browser)."""
        if self.billing.is_demo:
            raise ServiceError(409, "This is the demo business, so there is nothing to pay.")
        raise ServiceError(409, "Payments are not set up here yet.")

    def billing_facts(self) -> dict[str, Any]:
        """What the server needs to start a checkout or open the billing portal (never shown as is)."""
        state = self.billing
        return {"plan": state.plan, "status": state.status, "customer": state.customer,
                "subscription": state.subscription, "demo": state.is_demo,
                "paidPlans": list(PAID_PLANS), "known": plan_for(state.plan) is not None}

    # ----------------------------------------------------------------- routing

    def dispatch(self, method: str, path: str, body_json: Any = None) -> tuple[int, dict[str, Any]]:
        """Route one request. ``body_json`` may be a dict, a JSON string, bytes or None.

        Returns ``(status, json)``; errors come back as ``{"error": ..., "message": ...}``
        with a plain message, never a stack trace (§48, §70).
        """
        if self.decides_holds and (method or "").upper() == "POST" and self.billing.enforced:
            target = unquote((path or "/").partition("?")[0]).rstrip("/")
            if target in HOLDABLE_PATHS and self.intake_held():
                return self.hold_request(target, body_json)
        status, out = self._route(method, path, body_json, self._routes())
        if self.billing.enforced and (method or "").upper() == "POST" and status == 200:
            self.billing_check()
        return status, out

    def _route(self, method: str, path: str, body_json: Any, routes: Sequence[Any], *,
               refuse: tuple[int, dict[str, Any]] | None = None) -> tuple[int, dict[str, Any]]:
        """``dispatch`` over ``routes``; ``refuse`` answers a path none of them knows (default 404)."""
        method = (method or "GET").upper()
        raw_path, _, query = (path or "/").partition("?")
        path = unquote(raw_path).rstrip("/") or "/"
        self._opened = []
        try:
            return self._route_one(method, path, query, body_json, routes, refuse)
        finally:
            if self.inline_access_log and self._opened:
                who, role = self.viewer
                self.record_access(self.take_opened(), who=who, role=role)

    def _route_one(self, method: str, path: str, query: str, body_json: Any, routes: Sequence[Any],
                   refuse: tuple[int, dict[str, Any]] | None) -> tuple[int, dict[str, Any]]:
        try:
            body = _body(body_json)
            if not body and query:
                from urllib.parse import parse_qsl

                body = dict(parse_qsl(query))
            for verb, pattern, handler in routes:
                m = pattern.fullmatch(path)
                if m is None:
                    continue
                if verb != method:
                    continue
                result = handler(body, *m.groups())
                if result is None:
                    return 404, {"error": "not_found", "message": "I can't find that."}
                return 200, result
            if refuse is not None:
                return refuse[0], dict(refuse[1])
            if any(p.fullmatch(path) for _, p, _ in routes):
                return 405, {"error": "method_not_allowed", "message": "That is not something I can do here."}
            return 404, {"error": "not_found", "message": "I can't find that."}
        except _Reply as reply:
            return reply.status, reply.body
        except ServiceError as exc:
            codes = {400: "bad_request", 403: "forbidden", 404: "not_found", 409: "conflict", 410: "gone",
                     413: "too_large", 415: "unsupported", 502: "unavailable"}
            return exc.status, {"error": codes.get(exc.status, "error"), "message": exc.message}

    def _routes(self):  # type: ignore[no-untyped-def]
        r = re.compile
        seg = r"([^/]+)"
        return (
            ("GET", r("/healthz"), lambda b: {"ok": True}),
            ("GET", r("/api/home"), lambda b: self.home()),
            ("GET", r("/api/needs-you"), lambda b: self.needs_you()),
            ("POST", r(f"/api/needs-you/{seg}/answer"),
             lambda b, i: self.answer(i, _field(b, "option_id", "optionId"), bool(b.get("remember", False)),
                                      b.get("split"))),
            ("GET", r("/api/activity"), lambda b: self.activity()),
            ("GET", r("/api/companies"), lambda b: self.companies()),
            ("GET", r(f"/api/companies/{seg}"), lambda b, i: self.company(i)),
            ("GET", r(f"/api/companies/{seg}/cost-centers"), lambda b, i: self.cost_centers(i, b)),
            ("POST", r(f"/api/companies/{seg}/cost-centers"), lambda b, i: self.cost_center_create(i, b)),
            ("POST", r("/api/cost-centers/allocate"), lambda b: self.cost_center_allocate(b)),
            ("GET", r(f"/api/cost-centers/{seg}"), lambda b, i: self.cost_center(i, b)),
            ("GET", r(f"/api/cost-centers/{seg}/statement"), lambda b, i: self.cost_center_statement(i, b)),
            ("POST", r(f"/api/cost-centers/{seg}"), lambda b, i: self.cost_center_update(i, b)),
            ("GET", r(f"/api/months/{seg}/{seg}"), lambda b, c, m: self.month(c, m)),
            ("POST", r("/api/ask"), lambda b: self.ask(_field(b, "question"))),
            ("POST", r("/api/evidence"),
             lambda b: self.upload_evidence(b.get("filename"), b.get("contentType") or b.get("content_type"),
                                            _b64(b.get("dataBase64") or b.get("data_base64")))),
            ("POST", r("/api/evidence/upload"), lambda b: self._upload_body(b)),
            ("POST", r("/api/receipts"), lambda b: self._upload_body(b)),
            ("POST", r("/api/share"), lambda b: self.share(b)),
            ("GET", r("/api/sources"), lambda b: self.sources()),
            ("POST", r("/api/chat"), lambda b: self.chat(b or {})),
            ("GET", r("/api/chat/tools"), lambda b: self.chat_tools()),
            ("POST", r("/api/chat/tool"), lambda b: self.chat_tool(b or {})),
            ("GET", r("/api/tasks"), lambda b: {"tasks": self.assistant.list_tasks(include_done=True)}),
            ("POST", r("/api/tasks"), lambda b: self.task_create(b or {})),
            ("POST", r(f"/api/tasks/{seg}/done"), lambda b, tid: self.task_done(tid)),
            ("POST", r(f"/api/chat/outbox/{seg}/send"), lambda b, mid: self.chat_send(mid, b)),
            ("GET", r(f"/api/reports/{seg}/file"), lambda b, rid: self.report_file(rid)),
            ("GET", r("/api/documents"), lambda b: self.documents_list(b)),
            ("POST", r("/api/documents/export"), lambda b: self.documents_export(b)),
            # Sensitive documents (§52): who opened their originals; the owner marks one (or not).
            ("GET", r("/api/documents/access-log"), lambda b: self.access_log()),
            ("POST", r(f"/api/documents/{seg}/sensitive"), lambda b, did: self.document_sensitive(did, b)),
            ("GET", r(f"/api/documents/{seg}/file"), lambda b, did: self.document_download(did)),
            ("GET", r(f"/api/documents/{seg}"), lambda b, did: self.document_detail(did)),
            ("GET", r(f"/api/transactions/{seg}"), lambda b, tid: self.transaction(tid)),
            ("POST", r(f"/api/transactions/{seg}/evidence"), lambda b, tid: self.evidence_need(tid, b)),
            ("GET", r("/api/onboarding"), lambda b: self.onboarding_status()),
            ("POST", r("/api/onboarding/seen"), lambda b: self.onboarding_seen(b)),
            ("GET", r("/api/settings/mailboxes"), lambda b: self.mailboxes()),
            ("POST", r("/api/settings/mailboxes"), lambda b: self.mailboxes(b or {"address": ""})),
            ("POST", r(f"/api/companies/{seg}/profile"), lambda b, c: self.company_profile(c, b)),
            ("GET", r("/api/obligations"), lambda b: self.obligations()),
            ("POST", r(f"/api/obligations/{seg}/done"), lambda b, oid: self.obligation_done(oid, b)),
            ("POST", r(f"/api/expected-invoices/{seg}/not-coming"), lambda b, eid: self.expected_not_coming(eid)),
            ("GET", r("/api/settings/report"), lambda b: self.report_settings()),
            ("POST", r("/api/settings/report"), lambda b: self.report_settings(b or {})),
            ("GET", r("/api/settings/automation"), lambda b: self.automation_settings()),
            ("POST", r("/api/settings/automation"), lambda b: self.automation_settings(b or {"nothing": True})),
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
            ("GET", r(f"/api/accountant/clients/{seg}/evidence/{seg}/file"),
             lambda b, c, e: self.accountant_evidence_file(c, e)),
            ("GET", r(f"/api/accountant/clients/{seg}/export"), lambda b, c: self.accountant_export(c)),
            ("POST", r(f"/api/accountant/clients/{seg}/rules"),
             lambda b, c: self.accountant_rule(_field(b, "text"), str(b.get("scope") or "client"), c)),
            ("POST", r("/api/accountant/rules"),
             lambda b: self.accountant_rule(_field(b, "text"), str(b.get("scope") or "client"),
                                            str(b["companyId"]) if b.get("companyId") else None)),
            ("GET", r("/api/accountant/invitations"), lambda b: self.invitations_list()),
            ("POST", r("/api/accountant/invitations"), lambda b: self.invite_client(b)),
            ("GET", r("/api/settings/accountant"), lambda b: self.accountant_settings()),
            ("POST", r("/api/settings/accountant"), lambda b: self.accountant_settings(b or {"email": ""})),
            ("GET", r(f"/api/evidence/{seg}/file"), lambda b, e: self.evidence_file(e)),
            ("GET", r("/api/audit"), lambda b: self.audit()),
            # The plan (backoffice.billing). Paying goes through the production server's payment provider only.
            ("GET", r("/api/billing"), lambda b: self.billing_view(b)),
            ("POST", r("/api/billing/(?:checkout|portal)"), lambda b: self.billing_refused(b)),
            ("GET", r("/api/pipeline"), lambda b: self.pipeline()),
            ("GET", r("/api/internal/(overview|operations|readiness|acceptance)"), lambda b, view: self.internal(view, b)),
            # Employee cards and staff expenses (backoffice.staff).
            ("GET", r("/api/employees"), lambda b: self.staff_views.employees()),
            ("POST", r("/api/employees"), lambda b: self.staff_views.save(b)),
            ("POST", r(f"/api/employees/{seg}"), lambda b, i: self.staff_views.save(b, i)),
            ("GET", r("/api/expense-claims"), lambda b: self.staff_views.claims()),
            ("POST", r("/api/expense-claims"), lambda b: self.staff_views.owner_claim(b)),
            ("GET", r("/api/employee/card-payments"),
             lambda b: self.staff_views.card_payments(str(b.get("employee") or b.get("employeeId") or "") or None)),
            ("POST", r("/api/employee/receipts"), lambda b: self.staff_views.employee_receipt(b)),
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


REVERSE_CHARGE_FLAG = "Possible reverse charge: VAT to be declared by you"
IMPORT_VAT_FLAG = "Import VAT paid at customs"
# Named by the country of the company the document is for (§49): Portugal's, Spain's...
_FOREIGN_VAT_FLAG = "Foreign VAT charged — may be reclaimable abroad, not deductible in {country}"
FOREIGN_VAT_FLAG = _FOREIGN_VAT_FLAG.format(country="Portugal")


def _foreign_vat_flag(record: DocumentRecord) -> dict[str, str] | None:
    """The accountant's VAT flag for a document from abroad (checklist X31); never shown to the owner.

    VAT charged by a foreign supplier cannot be deducted in Portugal (it may be reclaimed in the
    supplier's country). No VAT charged by an EU supplier with a valid VAT number, an invoice saying
    the VAT is reverse-charged, or a supplier outside the EU: the business may have to declare the
    VAT itself. Which applies (goods or services, B2B or not) is the accountant's call. "Foreign" is
    relative to the country of the company the document is for (a Spanish invoice to a Spanish
    company is domestic; to a Portuguese one, foreign).
    """
    issuer = record.issuer
    if issuer is None or not issuer.is_foreign:
        return None
    home = COUNTRY_NAMES.get(issuer.home, issuer.home)
    doc = record.document
    who = display_name(doc.supplier_name)
    where = COUNTRY_NAMES.get(issuer.country or "", issuer.country or "abroad")
    invoice = f"invoice {doc.invoice_number}" if doc.invoice_number else "the invoice"
    total = f" ({format_money(doc.gross_amount, doc.currency)})" if doc.gross_amount is not None else ""
    if doc.vat_amount is not None and doc.vat_amount > 0:
        detail = f"{who} ({where}) charged {format_money(doc.vat_amount, doc.currency)} of VAT on {invoice}{total}."
        valid = foreign_vat_rates(issuer.country, doc.issue_date)
        if valid and doc.net_amount and check_rate(doc.net_amount, doc.vat_amount, valid).fit is not RateFit.MATCHES:
            rate = (doc.vat_amount / doc.net_amount).quantize(Decimal("0.001"))
            detail += f" {percent(rate)} is not a VAT rate used in {where}."
        return {"id": f"t_foreign_vat_{doc.id}", "title": _FOREIGN_VAT_FLAG.format(country=home), "detail": detail}
    if not issuer.reverse_charge_candidate:
        return None
    if issuer.reverse_charge:
        why = "The invoice says the VAT is reverse-charged."
    elif issuer.in_eu:
        number = issuer.tax_number.printed if issuer.tax_number is not None else ""
        why = f"EU supplier with a valid VAT number ({number}), no VAT charged."
    else:
        why = "Supplier outside the EU, no VAT charged."
    return {"id": f"t_reverse_charge_{doc.id}", "title": REVERSE_CHARGE_FLAG,
            "detail": f"{who} ({where}), {invoice}{total}. {why}"}


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


def _terms_audit(terms: Mapping[str, Any]) -> dict[str, Any]:
    """A cost center's settings as the audit log records them (only those given)."""
    names = {"recharge": "recharge", "owner_name": "owner", "fee_percent": "fee_percent", "fee_monthly": "fee_monthly"}
    return {names[k]: v for k, v in terms.items() if k in names}


_CAPTURE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_PHONE_QUALITY = ("blurry", "glare", "too_dark", "overexposed")
MAX_CAPTURE_PAGES = 50


def capture_fields(body: Mapping[str, Any]) -> dict[str, Any]:
    """What the phone said about a capture (mobile/src/offline/uploader.ts), cleaned: ``{"id", "page",
    "pageCount"}`` for one page of a multi-page scan, and ``{"quality": [...]}`` for the problems it noticed.

    Multipart form fields arrive as text (``page=2``, ``hints='{"quality":["glare"]}'``). Anything unreadable
    is dropped, never guessed: hints are advice for reading, never evidence (§3, §18).
    """
    out: dict[str, Any] = {}
    cid = body.get("capture_id") or body.get("captureId")
    page, count = _small_int(body.get("page")), _small_int(body.get("page_count") or body.get("pageCount"))
    if isinstance(cid, str) and _CAPTURE_ID.match(cid.strip()) and page and count and page <= count <= \
            MAX_CAPTURE_PAGES:
        out.update(id=cid.strip(), page=page, pageCount=count)
    hints = body.get("hints")
    if isinstance(hints, str):
        try:
            hints = json.loads(hints)
        except (json.JSONDecodeError, ValueError):
            hints = None
    if isinstance(hints, Mapping) and isinstance(hints.get("quality"), list):
        quality = [q for q in dict.fromkeys(hints["quality"]) if isinstance(q, str) and q in _PHONE_QUALITY]
        if quality:
            out["quality"] = quality
    return out


def _small_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(str(value).strip()) if isinstance(value, (int, str)) else None
    except ValueError:
        return None
    return number if number is not None and 0 < number <= 1000 else None


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
