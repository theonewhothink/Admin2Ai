"""Deterministic orchestrator over named agents (§3, §46).

Never one giant agent. Each agent below is a small class that calls the real
modules of this package; the :class:`Orchestrator` decides, in a fixed order,
which agent runs on what. Nothing here guesses: every lifecycle transition
carries evidence ids (§3), and every step is written to the hash-chained audit
log (§55).

Pipeline for one piece of evidence::

    Discovery      what arrived (email, e-invoice, fiscal QR text, bank rows, letter)
    Retrieval      invoice links in emails, followed through registered portal adapters (§9, §10)
    Document       structured extraction: UBL, Portuguese fiscal QR + text fields (§13, §19); uploaded
                   PDFs and photos through the repository's document reader (backoffice.reading:
                   text layer, QR, then the OCR chain), when one is configured (§13-17)
    Verification   field-level GREEN / AMBER / RED (§18, §57); a disagreement becomes one plain
                   question for the owner, whose answer is stored as evidence (§19, §37)
    Fraud          hard stops: changed IBAN, recipient mismatch, ... (§26)
    Entity         which company (§51), taught rules (§38)
    Reconciliation expected evidence (§21), then transaction <-> document matching (§20)
    Obligation     tax letters become obligations; payments prove them (§24)
    Missing        a plan for every payment still without its document; supplier chasing (§22)
    Accountant     routine accountant questions answered from evidence (§28)
    Closure        lifecycle transitions and month status (§2, §27, §48)
    Auditor        re-checks closed items and reopens any that no longer hold (§55, §57)

The orchestrator is pure Python: no network, no threads started, no
filesystem. It runs unchanged in a browser (Pyodide). Time comes from an
explicit :class:`Clock`, so a replayed demo gives the same result every time.
The one exception is opt-in: a server that sets ``Repository.reader`` lets
that reader call its OCR engines while a PDF or photo is read.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from backoffice.audit import AuditEntry, AuditLog, InMemoryAuditStore
from backoffice.closure import Activity as ClosureActivity
from backoffice.closure import ActivityKind as ClosureKind
from backoffice.closure import (
    EvidenceFact,
    InteractionKind,
    Issuer,
    Month,
    MonthStatus,
    OwnerInteraction,
    ProofKind,
    compute_month_status,
    detect_obligation,
    satisfy,
)
from backoffice.countries.pt import (
    PACK,
    PTQRCode,
    QRCodeError,
    RateDataUnavailable,
    extract_text_fields,
    looks_like_pt_qr,
    parse_qr,
    qr_to_observations,
)
from backoffice.domain.lifecycle import IllegalTransition, Stage, TrackedItem
from backoffice.domain.models import (
    METHOD_RANK,
    CriticalField,
    Document,
    DocumentType,
    Evidence,
    EvidenceFormat,
    ExtractionMethod,
    FieldObservation,
    LegalEntity,
    Obligation,
    Quality,
    SourceKind,
    Supplier,
    Transaction,
    TransactionKind,
    VerifiedField,
)
from backoffice.evidence import (
    EmailIngestResult,
    EvidenceRegistry,
    IntegrityError,
    ObjectNotFound,
    ShareIntake,
    SharePayload,
    ShareOutcome,
    UploadReceipt,
    UploadRequest,
    UploadService,
    object_key,
    parse_key,
    sha256_hex,
)
from backoffice.extraction import StructuredFormatError, XMLSyntaxError, parse_einvoice
from backoffice.fraud import (
    ApproverKind,
    BeneficiaryChangeRefused,
    FraudAssessment,
    FraudCase,
    HardApproval,
    SignalKind,
    VerificationChannel,
    assess,
    mask_iban,
    normalize_iban,
    trust_iban,
)
from backoffice.learning import (
    Answer,
    EntityAssignment,
    OptionKind,
    OwnershipBook,
    Question,
    RuleAuthor,
    RuleBook,
    RuleScope,
    assign_entity,
    build_history,
    counterparty_key,
    day_month,
    detect_price_change,
    display_name,
    format_money,
    same_tax_id,
    suggest_rule_from_answer,
)
from backoffice.learning import Rule, RuleField, RuleMatch, RuleOutcome, RuleSubject
from backoffice.missing import ChaseFacts, ChaseMessage, activity_line, compose_request, thread_token
from backoffice.policy import ActionContext, ActionKind, Approval, Decision, TenantPolicy, authorize
from backoffice.policy.actions import Requirement
from backoffice.reconciliation import (
    ExpectationDecision,
    ExpectedEvidenceEngine,
    Match,
    SupplierResolver,
    reconcile,
)
from backoffice.verification import DocumentAssessment, assess_document, currency_mark, lineage
from backoffice.verification._display import field_label, join, method_label, show_many

__all__ = [
    "SYSTEM",
    "TZ",
    "Account",
    "AccountantProfile",
    "AccountantQuestion",
    "ActivityEntry",
    "AnswerOutcome",
    "BankRow",
    "ChaseRecord",
    "CheckOption",
    "Clock",
    "ConnectorState",
    "DocumentRecord",
    "IngestReport",
    "MemoryObjectStore",
    "NeedsYouRecord",
    "ObligationRecord",
    "Orchestrator",
    "OwnerProfile",
    "PortalDocument",
    "Repository",
    "RunReport",
    "TxRecord",
]

# Lisbon in summer time (WEST). A fixed offset keeps the browser build free of
# time-zone databases; the demo world lives entirely in September/October.
TZ = timezone(timedelta(hours=1), "WEST")
SYSTEM = "system"
OWNER_ACTOR = "owner"
CHASE_AFTER_DAYS = 3  # a payment this old without its invoice is worth a polite request (§22, §23)
ANSWER_SECONDS = 40  # owner time recorded for one tap on a Needs-You item (§59)

_ZERO = Decimal("0")


# --------------------------------------------------------------------------- infrastructure


class Clock:
    """Deterministic clock. Time only moves when someone moves it (never backwards)."""

    def __init__(self, now: datetime) -> None:
        if now.tzinfo is None:
            raise ValueError("the clock needs a timezone-aware time")
        self._now = now

    def now(self) -> datetime:
        return self._now

    def advance_to(self, at: datetime) -> datetime:
        if at.tzinfo is None:
            raise ValueError("the clock needs a timezone-aware time")
        if at > self._now:
            self._now = at
        return self._now

    def today(self) -> date:
        return self._now.astimezone(TZ).date()

    @contextmanager
    def peek(self, at: datetime) -> Iterator[datetime]:
        """Read views as of ``at`` (never earlier than now), then put the clock back.

        For read-only work in a live deployment: the owner sees today's date
        and greeting, while the stored state keeps the time of its last change.
        """
        if at.tzinfo is None:
            raise ValueError("the clock needs a timezone-aware time")
        before = self._now
        self._now = max(before, at)
        try:
            yield self._now
        finally:
            self._now = before


class MemoryObjectStore:
    """Write-once, content-addressed, hash-verified store kept in memory (browser/demo)."""

    def __init__(self) -> None:
        self._objects: dict[str, bytes] = {}
        self._types: dict[str, str] = {}

    def put_immutable(self, data: bytes, tenant: str, content_type: str) -> str:
        key = object_key(tenant, sha256_hex(bytes(data)))
        if key not in self._objects:
            self._objects[key] = bytes(data)
            self._types[key] = content_type
        return key

    def get(self, key: str) -> bytes:
        data = self._objects.get(key)
        if data is None:
            raise ObjectNotFound(key)
        _, digest = parse_key(key)
        if sha256_hex(data) != digest:
            raise IntegrityError(f"content of {key} does not match its hash")
        return data

    def exists(self, key: str) -> bool:
        return key in self._objects

    def __len__(self) -> int:
        return len(self._objects)


# --------------------------------------------------------------------------- records


@dataclass(frozen=True)
class OwnerProfile:
    first_name: str
    full_name: str
    email: str

    @property
    def initials(self) -> str:
        return "".join(p[0] for p in self.full_name.split()[:2]).upper()


@dataclass(frozen=True)
class AccountantProfile:
    id: str
    firm: str
    person: str
    email: str
    software: str = "TOConline"


@dataclass(frozen=True)
class Account:
    """A bank account or card. ``holder_id`` is the company billed; ``owned`` says the
    account belongs to that company only (a shared card is not owned by anyone)."""

    id: str
    bank: str
    holder_id: str
    iban: str | None = None
    card_last4: str | None = None
    owned: bool = True

    @property
    def label(self) -> str:
        if self.card_last4:
            return f"card •••• {self.card_last4}"
        return f"{self.bank} •••• {(self.iban or '')[-4:]}"


@dataclass
class ConnectorState:
    """What month closing needs from a connector (``ConnectorCoverage``, §47–48)."""

    id: str
    name: str
    kind: str  # "email" | "bank" | "accountant"
    account: str
    company_ids: tuple[str, ...]
    healthy: bool
    covered_from: datetime | None
    covered_until: datetime | None
    last_synced_at: datetime | None


@dataclass(frozen=True)
class PortalDocument:
    """What a registered supplier-portal adapter returns for one invoice link (§10)."""

    data: bytes
    filename: str
    content_type: str
    portal: str


@dataclass(frozen=True)
class BankRow:
    """One row from a bank or card feed (Open Banking / CSV). Negative amount = money out."""

    bank_id: str
    account_id: str
    booked_on: date
    amount: Decimal
    counterparty: str
    description: str = ""
    kind: TransactionKind = TransactionKind.CARD
    card_last4: str | None = None
    counterparty_iban: str | None = None
    reference: str | None = None
    currency: str = "EUR"

    def __post_init__(self) -> None:
        if isinstance(self.amount, float) or not isinstance(self.amount, Decimal):
            raise TypeError("money must be Decimal, never float")

    def to_json(self) -> dict[str, Any]:
        return {
            "bank_id": self.bank_id,
            "account_id": self.account_id,
            "booked_on": self.booked_on.isoformat(),
            "amount": str(self.amount),
            "currency": self.currency,
            "counterparty": self.counterparty,
            "description": self.description,
            "kind": self.kind.value,
            "card_last4": self.card_last4,
            "counterparty_iban": self.counterparty_iban,
            "reference": self.reference,
        }


@dataclass
class Relationship:
    """A counterparty the business has an ongoing relationship with, found in its sources (§6, §24)."""

    id: str
    kind: str  # "insurance" | "investment" | "government" | "lender"
    name: str
    company_id: str
    detail: str
    found_in: str  # where it was learned from, in plain words
    renews_on: date | None = None


@dataclass
class DocumentRecord:
    document: Document
    evidence_ids: list[str]
    origin: str  # "email" | "link" | "upload" | "scan" | "share"
    received_at: datetime
    item_id: str
    observations: dict[str, list[FieldObservation]]
    reasons: tuple[str, ...] = ()
    sender: str | None = None
    message_text: str = ""
    supplier_id: str | None = None
    fraud: FraudAssessment | None = None
    on_hold: bool = False  # a fraud hard stop: never matched, paid or closed without the owner
    hold_released: bool = False
    matched_tx_ids: list[str] = field(default_factory=list)
    retrieved: bool = False  # fetched by the system from a link or portal (§9)
    # Field-level verification (§18): every critical field's value, quality, reasons and the
    # observations behind it (each with value, source, method, confidence and location).
    checks: dict[str, VerifiedField] = field(default_factory=dict)
    # Values the owner confirmed when the sources disagreed (§19): method HUMAN, source = the answer.
    owner_values: dict[str, FieldObservation] = field(default_factory=dict)

    @property
    def id(self) -> str:
        return self.document.id

    @property
    def label(self) -> str:
        doc = self.document
        kind = _DOC_LABELS.get(doc.doc_type, "Document")
        number = f" {doc.invoice_number}" if doc.invoice_number else ""
        amount = f" · {format_money(doc.gross_amount, doc.currency)}" if doc.gross_amount is not None else ""
        return f"{kind}{number}{amount}"


@dataclass
class TxRecord:
    tx: Transaction
    evidence_id: str
    item_id: str
    holder_id: str
    decision: ExpectationDecision | None = None
    document_ids: list[str] = field(default_factory=list)
    match_why: tuple[str, ...] = ()
    match_headline: str = ""
    likely_document_ids: list[str] = field(default_factory=list)  # AMBER candidates, never closure
    assignment: EntityAssignment | None = None
    private: bool = False  # the owner said: personal / not one of my companies
    proof_evidence_ids: list[str] = field(default_factory=list)  # e.g. the tax letter a payment proves
    missing_since: date | None = None

    @property
    def id(self) -> str:
        return self.tx.id

    @property
    def company_id(self) -> str:
        return self.tx.entity_id or self.holder_id


@dataclass(frozen=True)
class CheckOption:
    """One answer to a "which value is right?" question about a document (§19, §37).

    ``values`` are the field values the owner confirms by choosing it (empty
    for "neither"); ``channels`` are the sources that showed them.
    """

    id: str
    label: str
    values: Mapping[str, Any] = field(default_factory=dict)
    channels: frozenset[str] = frozenset()


@dataclass
class NeedsYouRecord:
    id: str
    kind: str  # "choice" | "approval" | "check" (a document whose sources disagree)
    subject_type: str  # "transaction" | "document"
    subject_id: str
    item_id: str
    company_id: str
    created_at: datetime
    question: Question | None = None
    why: tuple[str, ...] = ()
    status: str = "open"  # "open" | "answered" | "resolved"
    answer: str | None = None
    answered_at: datetime | None = None
    resolution: str = ""
    prompt: str = ""  # "check": the question, in plain words
    options: tuple[CheckOption, ...] = ()  # "check": the answers


@dataclass(frozen=True)
class ActivityEntry:
    id: str
    at: datetime
    kind: str  # collected / recovered / chased / answered / checked / closed / protected / learned
    text: str
    company_id: str | None = None
    amount: Decimal | None = None
    currency: str = "EUR"
    evidence_ids: tuple[str, ...] = ()


@dataclass
class ObligationRecord:
    obligation: Obligation
    evidence_id: str
    title: str
    reasons: tuple[str, ...]
    reference: str | None
    satisfied_by: tuple[str, ...] = ()


@dataclass(frozen=True)
class ChaseRecord:
    tx_id: str
    supplier_id: str
    company_id: str
    message: ChaseMessage
    sent_at: datetime
    line: str


@dataclass
class AccountantQuestion:
    id: str
    company_id: str
    text: str
    evidence_id: str
    asked_at: datetime
    status: str = "waiting"  # "waiting" | "answered"
    answer: str | None = None
    answer_evidence_ids: tuple[str, ...] = ()


@dataclass
class IngestReport:
    """What happened to one arrival. ``message`` is owner-facing (§69)."""

    route: str
    message: str
    evidence_ids: list[str] = field(default_factory=list)
    document_ids: list[str] = field(default_factory=list)
    transaction_ids: list[str] = field(default_factory=list)
    obligation_ids: list[str] = field(default_factory=list)
    question_ids: list[str] = field(default_factory=list)
    pending_links: list[str] = field(default_factory=list)
    stored_only: bool = False
    already_known: bool = False  # every document in it was already on file


@dataclass
class RunReport:
    transitions: int = 0
    passes: int = 0
    reopened: list[str] = field(default_factory=list)
    chased: list[str] = field(default_factory=list)
    closed_months: list[tuple[str, str]] = field(default_factory=list)


@dataclass(frozen=True)
class AnswerOutcome:
    ok: bool
    message: str
    learned: str | None = None
    resolved_ids: tuple[str, ...] = ()


_DOC_LABELS = {
    DocumentType.INVOICE: "Invoice",
    DocumentType.INVOICE_RECEIPT: "Invoice-receipt",
    DocumentType.SIMPLIFIED_INVOICE: "Receipt",
    DocumentType.RECEIPT: "Receipt",
    DocumentType.CREDIT_NOTE: "Credit note",
    DocumentType.DEBIT_NOTE: "Debit note",
    DocumentType.TAX_NOTICE: "Tax notice",
    DocumentType.STATEMENT: "Statement",
    DocumentType.CONTRACT: "Contract",
}


# --------------------------------------------------------------------------- repository


class Repository:
    """Everything one tenant has, in memory. The service and the agents share it."""

    def __init__(self, *, tenant_id: str, owner: OwnerProfile, now: datetime) -> None:
        self.tenant_id = tenant_id
        self.owner = owner
        self.clock = Clock(now)
        self.companies: dict[str, LegalEntity] = {}
        self.legal_names: dict[str, str] = {}
        self.accounts: dict[str, Account] = {}
        self.suppliers: dict[str, Supplier] = {}
        self.supplier_phones: dict[str, str] = {}
        self.connectors: dict[str, ConnectorState] = {}
        self.accountant: AccountantProfile | None = None
        self.store = MemoryObjectStore()
        self.registry = EvidenceRegistry(self.store, clock=self.clock.now)
        self.intake = ShareIntake(self.registry, clock=self.clock.now)
        self.uploads = UploadService(self.registry, files=self.intake, clock=self.clock.now)
        self.documents: dict[str, DocumentRecord] = {}
        self.transactions: dict[str, TxRecord] = {}
        self.items: dict[str, TrackedItem] = {}
        self.needs: dict[str, NeedsYouRecord] = {}
        self.activity: list[ActivityEntry] = []
        self.closure_log: list[ClosureActivity] = []
        self.interactions: list[OwnerInteraction] = []
        self.obligations: dict[str, ObligationRecord] = {}
        self.chases: dict[str, ChaseRecord] = {}
        self.accountant_questions: dict[str, AccountantQuestion] = {}
        self.rulebook = RuleBook()
        self.policy = TenantPolicy(tenant_id=tenant_id)
        self.audit_store = InMemoryAuditStore()
        self.audit = AuditLog(self.audit_store, clock=self.clock.now)
        self.history_pairs: list[tuple[str, str]] = []
        self.relationships: list[Relationship] = []
        self.history_transactions: list[Transaction] = []
        self.portal: dict[str, PortalDocument] = {}
        self.pending_links: list[str] = []
        self.closed_months: dict[tuple[str, str], date] = {}
        self.recovered_tx_ids: set[str] = set()
        # Reads uploaded PDFs and photos (backoffice.reading.DocumentReader, set by the server from its
        # environment). None in the browser demo: such files are stored and wait, unread.
        self.reader: Any = None
        self.reads: dict[str, Any] = {}  # evidence id -> ReadOutcome (what was read, by which steps)

    # ----------------------------------------------------------------- set-up

    def add_company(self, *, id: str, name: str, legal_name: str, tax_id: str, ibans: Sequence[str] = ()) -> LegalEntity:
        entity = LegalEntity(id=id, tenant_id=self.tenant_id, name=name, country="PT", tax_id=tax_id,
                             own_ibans=list(ibans))
        self.companies[id] = entity
        self.legal_names[id] = legal_name
        return entity

    def add_account(self, account: Account) -> None:
        if account.holder_id not in self.companies:
            raise ValueError(f"unknown company {account.holder_id}")
        self.accounts[account.id] = account

    def add_supplier(self, supplier: Supplier, *, phone: str | None = None) -> Supplier:
        if supplier.tenant_id != self.tenant_id:
            raise ValueError("supplier belongs to another tenant")
        self.suppliers[supplier.id] = supplier
        if phone:
            self.supplier_phones[supplier.id] = phone
        return supplier

    def add_connector(self, connector: ConnectorState) -> None:
        self.connectors[connector.id] = connector

    # ----------------------------------------------------------------- queries

    @property
    def entities(self) -> list[LegalEntity]:
        return list(self.companies.values())

    def today(self) -> date:
        return self.clock.today()

    def company_name(self, company_id: str | None) -> str | None:
        entity = self.companies.get(company_id or "")
        return entity.name if entity else None

    def ownership(self) -> OwnershipBook:
        return OwnershipBook(
            accounts={a.id: a.holder_id for a in self.accounts.values() if a.owned and not a.card_last4},
            cards={a.card_last4: a.holder_id for a in self.accounts.values() if a.owned and a.card_last4},
            account_labels={a.id: a.label for a in self.accounts.values()},
        )

    def resolver(self) -> SupplierResolver:
        return SupplierResolver(sorted(self.suppliers.values(), key=lambda s: s.id))

    def supplier_for_tax_id(self, tax_id: str | None) -> Supplier | None:
        if not tax_id:
            return None
        for supplier in sorted(self.suppliers.values(), key=lambda s: s.id):
            if same_tax_id(supplier.tax_id, tax_id):
                return supplier
        return None

    def supplier_for_domain(self, domain: str | None) -> Supplier | None:
        if not domain:
            return None
        domain = domain.lower()
        for supplier in sorted(self.suppliers.values(), key=lambda s: s.id):
            if any(domain == d or domain.endswith("." + d) for d in supplier.email_domains):
                return supplier
        return None

    def own_tax_ids(self) -> list[str]:
        return [e.tax_id for e in self.entities]

    def document_for_evidence(self, evidence_id: str) -> DocumentRecord | None:
        return next((d for d in self.documents.values() if evidence_id in d.evidence_ids), None)

    def item_month(self, item: TrackedItem) -> Month | None:
        if item.subject_type == "transaction":
            rec = self.transactions.get(item.subject_id)
            return Month.of(rec.tx.booked_on) if rec else None
        if item.subject_type == "document":
            doc = self.documents.get(item.subject_id)
            if doc is None:
                return None
            day = doc.document.issue_date or doc.received_at.astimezone(TZ).date()
            return Month.of(day)
        return None

    def item_company(self, item: TrackedItem) -> str | None:
        if item.subject_type == "transaction":
            rec = self.transactions.get(item.subject_id)
            return rec.company_id if rec else None
        if item.subject_type == "document":
            doc = self.documents.get(item.subject_id)
            if doc is None:
                return None
            if doc.document.entity_id:
                return doc.document.entity_id
            for tx_id in doc.matched_tx_ids:
                rec = self.transactions.get(tx_id)
                if rec:
                    return rec.company_id
            likely = [r for r in self.transactions.values() if doc.id in r.likely_document_ids]
            return likely[0].company_id if likely else None
        return None

    def items_for(self, company_id: str, month: Month) -> list[TrackedItem]:
        out = []
        for item in self.items.values():
            if item.subject_type == "transaction" and self.transactions[item.subject_id].private:
                continue
            if self.item_company(item) == company_id and self.item_month(item) == month:
                out.append(item)
        return sorted(out, key=lambda i: i.id)

    def months_for(self, company_id: str) -> list[Month]:
        found = {self.item_month(i) for i in self.items.values() if self.item_company(i) == company_id}
        return sorted((m for m in found if m is not None), key=lambda m: (m.year, m.month), reverse=True)

    def connectors_for(self, company_id: str) -> list[ConnectorState]:
        return [c for c in self.connectors.values() if c.kind in ("email", "bank") and company_id in c.company_ids]

    def open_needs(self) -> list[NeedsYouRecord]:
        return sorted((n for n in self.needs.values() if n.status == "open"), key=lambda n: (n.created_at, n.id))

    def evidence(self, evidence_id: str) -> Evidence:
        return self.registry.get(self.tenant_id, evidence_id)


# --------------------------------------------------------------------------- agents


class _Agent:
    name = "agent"

    def __init__(self, orchestrator: Orchestrator) -> None:
        self.o = orchestrator

    @property
    def repo(self) -> Repository:
        return self.o.repo

    def log(self, action: str, *, subject_id: str | None = None, evidence_ids: Sequence[str] = (),
            values: Mapping[str, Any] | None = None, validations: Sequence[Any] = (), response: Any = None,
            actor: str = SYSTEM, parser: str | None = None) -> None:
        self.o.log(self.name, action, subject_id=subject_id, evidence_ids=evidence_ids, values=values,
                   validations=validations, response=response, actor=actor, parser=parser)


@dataclass
class _Part:
    """One readable piece of a document bundle.

    A ``"read"`` part is a PDF or photo read by the document reader: ``text``
    is its Stage 0 text (text layer and decoded QR payloads, produced by
    ``method``), read here like any text; ``observations`` are the OCR/VLM
    engines' readings, already labelled with their engine (§17-18).
    """

    evidence_id: str
    kind: str  # "text" | "ubl" | "email_body" | "read"
    text: str = ""
    data: bytes = b""
    method: ExtractionMethod = ExtractionMethod.EMBEDDED_TEXT
    observations: dict[str, list[FieldObservation]] = field(default_factory=dict)
    reading_text: str = ""  # an engine's transcription: supplier name and document type only
    supplier_name: str | None = None
    doc_type: DocumentType | None = None
    parser: str = ""


@dataclass
class _Extracted:
    observations: dict[str, list[FieldObservation]]
    doc_type: DocumentType
    supplier_name: str | None
    evidence_ids: list[str]
    qr: PTQRCode | None = None
    buyer_is_final_consumer: bool = False
    parsers: list[str] = field(default_factory=list)
    invoice_number: str | None = None


class DiscoveryAgent(_Agent):
    """Registers every arrival as immutable evidence and says what it is (§7, §8, §12)."""

    name = "discovery"

    def file(self, data: bytes, *, filename: str | None, content_type: str | None, source_kind: SourceKind,
             at: datetime) -> ShareOutcome:
        outcome = self.repo.intake.ingest_file(
            self.repo.tenant_id, data, filename=filename, mime_type=content_type, source_kind=source_kind, at=at,
        )
        self.log("register", evidence_ids=[r.evidence.id for r in outcome.registrations],
                 values={"route": outcome.route.value, "filename": filename or ""})
        return outcome

    def share(self, payload: SharePayload) -> ShareOutcome:
        outcome = self.repo.intake.accept(self.repo.tenant_id, payload)
        self.log("register_share", evidence_ids=[r.evidence.id for r in outcome.registrations],
                 values={"route": outcome.route.value})
        return outcome

    def bank_rows(self, rows: Sequence[BankRow], at: datetime) -> list[TxRecord]:
        created: list[TxRecord] = []
        for row in rows:
            account = self.repo.accounts.get(row.account_id)
            if account is None:
                raise ValueError(f"unknown account {row.account_id!r}")
            body = json.dumps(row.to_json(), sort_keys=True, separators=(",", ":")).encode()
            is_card = row.kind is TransactionKind.CARD
            reg = self.repo.registry.register(
                body, tenant_id=self.repo.tenant_id,
                source_kind=SourceKind.CARD if is_card else SourceKind.BANK,
                format=EvidenceFormat.CARD_TRANSACTION if is_card else EvidenceFormat.BANK_TRANSACTION,
                mime_type="application/json", retrieved_at=at,
                metadata={"bank": account.bank, "account_id": account.id},
            )
            tx_id = "tx_" + reg.evidence.id[3:19]
            if tx_id in self.repo.transactions:
                continue  # the same bank row seen twice is one payment
            tx = Transaction(
                id=tx_id, tenant_id=self.repo.tenant_id, account_id=row.account_id, booked_on=row.booked_on,
                amount=row.amount, currency=row.currency, counterparty=row.counterparty,
                description=row.description, kind=row.kind, card_last4=row.card_last4 or account.card_last4,
                counterparty_iban=row.counterparty_iban, reference=row.reference,
            )
            item = TrackedItem(id="item_" + tx_id, tenant_id=self.repo.tenant_id, subject_type="transaction",
                               subject_id=tx_id)
            self.repo.items[item.id] = item
            record = TxRecord(tx=tx, evidence_id=reg.evidence.id, item_id=item.id, holder_id=account.holder_id)
            self.repo.transactions[tx_id] = record
            self.o.advance(item, Stage.ACQUIRED, [reg.evidence.id], agent=self.name,
                           note="Bank transaction imported.")
            self.log("discover_transaction", subject_id=tx_id, evidence_ids=[reg.evidence.id],
                     values={"amount": row.amount, "counterparty": row.counterparty, "booked_on": row.booked_on})
            created.append(record)
        return created


class RetrievalAgent(_Agent):
    """Follows invoice links through registered portal adapters (§9, §10). Never guesses a URL."""

    name = "retrieval"

    def follow(self, url: str, *, supplier_hint: str | None, at: datetime, context: Mapping[str, Any]) -> str | None:
        page = self.repo.portal.get(url)
        if page is None:
            if url not in self.repo.pending_links:
                self.repo.pending_links.append(url)
            self.log("link_pending", values={"url": url})
            return None
        reg = self.repo.registry.register(
            page.data, tenant_id=self.repo.tenant_id, source_kind=SourceKind.SUPPLIER_PORTAL,
            format=EvidenceFormat.UBL if page.content_type.endswith("xml") else EvidenceFormat.TEXT,
            mime_type=page.content_type, filename=page.filename, original_url=url, retrieved_at=at,
            metadata={"portal": page.portal}, context=dict(context),
        )
        self.log("retrieve", subject_id=reg.evidence.id, evidence_ids=[reg.evidence.id],
                 values={"url": url, "portal": page.portal, "supplier": supplier_hint or ""})
        return reg.evidence.id


_QR_START = re.compile(r"A:\d{9}\*B:")
_MONEY_WITH_CURRENCY = re.compile(r"(?:€|EUR)\s?-?\d[\d.,\u00a0 ]*\d|-?\d[\d.,\u00a0 ]*\d\s?(?:€|EUR)")


class DocumentAgent(_Agent):
    """Stage 0 extraction: UBL e-invoices, Portuguese fiscal QR codes and text fields (§13, §19),
    plus the readings of uploaded PDFs and photos (§13-17)."""

    name = "document"

    def stage0_fields(self, text: str, source: str, method: ExtractionMethod) -> dict[str, list[FieldObservation]]:
        """Fields this agent reads from a file's own text and QR payloads (Stage 0 for the OCR router)."""
        extracted = self.read([_Part(source, "text", text=text, method=method)])
        return {} if extracted is None else {k: list(v) for k, v in extracted.observations.items()}

    def text_extractor(self) -> Any:
        """Reads Portuguese fields from an OCR engine's text (the router labels them with the engine)."""
        known = self.repo.own_tax_ids()

        def extract(text: str, source: str, method: ExtractionMethod) -> dict[CriticalField, list[FieldObservation]]:
            found: dict[CriticalField, list[FieldObservation]] = {}
            for obs in extract_text_fields(text, source, method=method, known_customer_tax_ids=known).observations:
                found.setdefault(obs.field, []).append(obs)
            currency = _text_currency(text, source, method)
            if currency is not None:
                found.setdefault(CriticalField.CURRENCY, []).append(currency)
            return found

        return extract

    def read(self, parts: Sequence[_Part]) -> _Extracted | None:
        observations: dict[str, list[FieldObservation]] = {}
        doc_type: DocumentType | None = None
        supplier_name: str | None = None
        qr: PTQRCode | None = None
        final_consumer = False
        parsers: list[str] = []
        known = self.repo.own_tax_ids()

        def add(field_: CriticalField | str, obs: FieldObservation) -> None:
            key = field_.value if isinstance(field_, CriticalField) else str(field_)
            observations.setdefault(key, []).append(obs)

        for part in parts:
            if part.kind == "ubl":
                try:
                    result = parse_einvoice(part.data, source=part.evidence_id)
                except (StructuredFormatError, XMLSyntaxError):
                    continue
                parsers.append(result.kind)
                for f, found in result.fields.items():
                    for obs in found:
                        add(f, obs)
                doc_type = doc_type or result.doc_type
                supplier_name = supplier_name or result.extras.get("supplier_name")
                continue
            if part.kind == "read":
                for name, found in sorted(part.observations.items()):
                    for obs in found:
                        add(name, obs)
                if part.observations:
                    parsers.append(f"ocr:{part.parser}" if part.parser else "ocr")
                supplier_name = supplier_name or part.supplier_name
                doc_type = doc_type or part.doc_type
            text = part.text
            qr_payloads, rest = _split_qr(text)
            for payload in qr_payloads:
                try:
                    code = parse_qr(payload)
                except QRCodeError:
                    continue
                qr = qr or code
                parsers.append("pt_fiscal_qr")
                final_consumer = final_consumer or code.buyer_is_final_consumer
                for obs in qr_to_observations(code, part.evidence_id):
                    add(obs.field, obs)
                add(CriticalField.CURRENCY, FieldObservation(
                    value=code.currency, source=part.evidence_id, method=ExtractionMethod.QR, confidence=0.95,
                    location="qr:amounts are in euro"))
                doc_type = doc_type or code.doc_type
            method = part.method
            fields = extract_text_fields(rest, part.evidence_id, method=method, known_customer_tax_ids=known)
            if fields.observations:
                parsers.append({"text": "pt_text_fields", "read": "pt_text_fields:pdf_text"}.get(
                    part.kind, "pt_text_fields:email_body"))
            final_consumer = final_consumer or fields.buyer_is_final_consumer
            for obs in fields.observations:
                add(obs.field, obs)
            if fields.buyer_is_final_consumer and len(fields.unassigned_tax_ids) == 1 and not any(
                    o.field is CriticalField.SUPPLIER_TAX_ID for o in fields.observations):
                # Sold to a final consumer: the one other tax number printed is the seller's.
                add(CriticalField.SUPPLIER_TAX_ID, FieldObservation(
                    value=fields.unassigned_tax_ids[0], source=part.evidence_id, method=method, confidence=0.5,
                    location="text:the only tax number besides the final consumer"))
            currency = _text_currency(rest, part.evidence_id, method)
            if currency is not None:
                add(CriticalField.CURRENCY, currency)
            supplier_name = supplier_name or _first_line(rest)
            if doc_type is None and fields.observations:
                doc_type = _text_doc_type(rest)
            if part.kind == "read" and part.observations:
                supplier_name = supplier_name or _first_line(part.reading_text)
                doc_type = doc_type or _text_doc_type(part.reading_text)
        if not observations:
            return None
        number = _first_value(observations, CriticalField.INVOICE_NUMBER)
        return _Extracted(
            observations=observations, doc_type=doc_type or DocumentType.INVOICE, supplier_name=supplier_name,
            evidence_ids=list(dict.fromkeys(p.evidence_id for p in parts)), qr=qr,
            buyer_is_final_consumer=final_consumer, parsers=parsers,
            invoice_number=str(number) if number is not None else None,
        )


class VerificationAgent(_Agent):
    """Field-level verification; a document is GREEN only when independent sources agree (§18, §57)."""

    name = "verification"

    def verify(self, observations: Mapping[str, list[FieldObservation]], doc_type: DocumentType,
               bank_amount: Decimal | None = None, subject_id: str | None = None,
               evidence_ids: Sequence[str] = (),
               owner: Mapping[str, FieldObservation] | None = None) -> tuple[dict[str, Any], Quality, tuple[str, ...]]:
        assessment = self.assess(observations, doc_type, bank_amount, subject_id=subject_id,
                                 evidence_ids=evidence_ids, owner=owner)
        return _settled_values(assessment), assessment.quality, assessment.reasons

    def assess(self, observations: Mapping[str, list[FieldObservation]], doc_type: DocumentType,
               bank_amount: Decimal | None = None, *, subject_id: str | None = None,
               evidence_ids: Sequence[str] = (),
               owner: Mapping[str, FieldObservation] | None = None) -> DocumentAssessment:
        """Every field graded with the observations behind it (§18). A value the owner confirmed
        replaces the readings that disagree with it (they stay on the record, §55)."""
        observations = _with_owner(observations, owner or {})
        issue = _first_value(observations, CriticalField.ISSUE_DATE)
        try:
            rates = PACK.vat_rates(issue) if isinstance(issue, date) else PACK.vat_rates(self.repo.today())
        except RateDataUnavailable:
            rates = ()
        assessment = assess_document(observations, rates, bank_amount, doc_type=doc_type)
        self.log("verify", subject_id=subject_id, evidence_ids=evidence_ids,
                 values={name: a.value for name, a in assessment.fields.items() if a.value is not None},
                 validations=[_validation(n, a) for n, a in sorted(assessment.fields.items())],
                 response={"quality": assessment.quality.value, "with_bank_amount": bank_amount is not None})
        return assessment


class FraudAgent(_Agent):
    """Hard stops before anything is paid or closed (§26). It can block; it can never approve."""

    name = "fraud"

    def check(self, record: DocumentRecord) -> FraudAssessment:
        supplier = self.repo.suppliers.get(record.supplier_id or "")
        history = [
            d.document for d in self.repo.documents.values()
            if d.id != record.id and d.supplier_id and d.supplier_id == record.supplier_id and not d.on_hold
        ]
        result = assess(FraudCase(
            entities=self.repo.entities, supplier=supplier, document=record.document,
            history=sorted(history, key=lambda d: (d.issue_date or date.min, d.id)),
            sender=record.sender, message_text=record.message_text,
        ))
        self.log("assess", subject_id=record.id, evidence_ids=record.evidence_ids,
                 values={"hard_stop": result.hard_stop},
                 validations=[{"signal": s.kind.value, "severity": s.severity.value} for s in result.signals],
                 response=result.owner_message)
        return result


class EntityAgent(_Agent):
    """Which of the owner's companies a payment belongs to (§46, §51), using taught rules (§38)."""

    name = "entity"

    def assign(self, rec: TxRecord) -> EntityAssignment:
        docs = [self.repo.documents[d].document for d in rec.document_ids if d in self.repo.documents]
        document = docs[0] if len(docs) == 1 and docs[0].customer_tax_id else None
        accountant_ids = [self.repo.accountant.id] if self.repo.accountant else []
        result = assign_entity(
            entities=self.repo.entities, transaction=rec.tx, document=document, ownership=self.repo.ownership(),
            rulebook=self.repo.rulebook, accountant_ids=accountant_ids,
            history=build_history(self.repo.history_pairs), today=self.repo.today(),
        )
        self.log("assign_entity", subject_id=rec.id, evidence_ids=[rec.evidence_id],
                 values={"entity_id": result.entity_id or "", "private": result.private},
                 validations=list(result.why), response={"quality": result.quality.value,
                                                         "asks_owner": result.needs_owner})
        return result

    def assign_document(self, record: DocumentRecord) -> str | None:
        doc = record.document
        if not doc.customer_tax_id:
            return None
        result = assign_entity(entities=self.repo.entities, document=doc, today=self.repo.today())
        self.log("assign_entity", subject_id=record.id, evidence_ids=record.evidence_ids,
                 values={"entity_id": result.entity_id or ""}, response={"quality": result.quality.value})
        return result.entity_id if result.quality is Quality.GREEN else None


class ReconciliationAgent(_Agent):
    """Expected evidence (§21), then transactions matched to documents (§20)."""

    name = "reconciliation"

    def classify(self, records: Sequence[TxRecord]) -> None:
        engine = ExpectedEvidenceEngine(entities=self.repo.entities, suppliers=self.repo.resolver())
        for rec in records:
            decision = engine.classify(rec.tx)
            rec.decision = decision
            self.log("expect", subject_id=rec.id, evidence_ids=[rec.evidence_id],
                     values={"expectation": decision.expectation.value, "rule": decision.rule},
                     response={"quality": decision.quality.value, "reason": decision.reason})

    def match(self) -> list[Match]:
        repo = self.repo
        txs = [r for r in repo.transactions.values()
               if r.decision is not None and not r.document_ids and not r.private
               and not repo.items[r.item_id].is_done
               and (r.decision.requires_document or r.decision.quality is not Quality.GREEN)]
        docs = [d for d in repo.documents.values()
                if not d.on_hold and not d.matched_tx_ids and d.document.quality is not Quality.RED
                and not repo.items[d.item_id].is_done]
        if not txs or not docs:
            return []
        decisions = {r.id: r.decision for r in txs if r.decision is not None}
        result = reconcile(
            [r.tx for r in sorted(txs, key=lambda r: r.id)],
            [d.document for d in sorted(docs, key=lambda d: d.id)],
            suppliers=repo.resolver(), own_tax_ids=repo.own_tax_ids(), expectations=decisions,
        )
        accepted = []
        for m in result.matches:
            evidence = [repo.transactions[t].evidence_id for t in m.transaction_ids]
            for d in m.document_ids:
                evidence += repo.documents[d].evidence_ids
            self.log("match", subject_id=m.id, evidence_ids=evidence,
                     values={"transactions": list(m.transaction_ids), "documents": list(m.document_ids)},
                     validations=list(m.why), response={"quality": m.quality.value, "kind": m.kind.value})
            if m.quality is Quality.GREEN and not m.is_ambiguous:
                accepted.append(m)
                for t in m.transaction_ids:
                    rec = repo.transactions[t]
                    rec.document_ids = list(m.document_ids)
                    rec.match_why = tuple(m.why)
                    rec.match_headline = m.headline
                    rec.likely_document_ids = []
                for d in m.document_ids:
                    repo.documents[d].matched_tx_ids = list(m.transaction_ids)
            else:
                for t in m.transaction_ids:
                    repo.transactions[t].likely_document_ids = list(m.document_ids)
        return accepted


class ObligationAgent(_Agent):
    """Letters become obligations (§24); a GREEN payment with the right amount and reference proves one."""

    name = "obligation"

    def detect(self, text: str, evidence_id: str, *, received_on: date, sender: str = "") -> ObligationRecord | None:
        finding = detect_obligation(
            text, tenant_id=self.repo.tenant_id, received_on=received_on, sender=sender,
            entities=self.repo.entities,
        )
        if finding is None or finding.issuer not in (Issuer.TAX_AUTHORITY, Issuer.SOCIAL_SECURITY):
            return None
        if finding.obligation is None:
            self.log("obligation_incomplete", evidence_ids=[evidence_id], values={"title": finding.title})
            return None
        obligation = finding.obligation.model_copy(update={"id": "obl_" + evidence_id[3:19]})
        record = ObligationRecord(obligation=obligation, evidence_id=evidence_id, title=finding.title,
                                  reasons=tuple(finding.reasons), reference=finding.reference)
        self.repo.obligations[obligation.id] = record
        self.log("detect_obligation", subject_id=obligation.id, evidence_ids=[evidence_id],
                 values={"kind": obligation.kind.value, "due_on": obligation.due_on, "amount": obligation.amount,
                         "entity_id": obligation.entity_id},
                 response={"quality": finding.quality.value})
        return record

    def prove(self) -> list[TxRecord]:
        """Tax payments that satisfy an open obligation: the payment item can close with the letter as proof."""
        proven: list[TxRecord] = []
        for ob in sorted(self.repo.obligations.values(), key=lambda o: o.obligation.id):
            if ob.satisfied_by:
                continue
            facts = [
                EvidenceFact(evidence_id=r.evidence_id, kind=ProofKind.PAYMENT, on=r.tx.booked_on,
                             quality=Quality.GREEN, amount=abs(r.tx.amount), currency=r.tx.currency,
                             reference=r.tx.reference)
                for r in self.repo.transactions.values()
                if r.company_id == ob.obligation.entity_id and r.tx.amount < 0 and r.decision is not None
                and r.decision.expectation.value == "tax_notice_or_proof"
            ]
            if not facts:
                continue
            result = satisfy(ob.obligation, sorted(facts, key=lambda f: f.evidence_id))
            self.log("satisfy", subject_id=ob.obligation.id, evidence_ids=[ob.evidence_id, *result.evidence_ids],
                     response={"satisfied": result.satisfied, "quality": result.quality.value},
                     validations=list(result.reasons))
            if not result.satisfied or result.quality is not Quality.GREEN:
                continue
            ob.obligation = result.obligation
            ob.satisfied_by = tuple(result.evidence_ids)
            for rec in self.repo.transactions.values():
                if rec.evidence_id in result.evidence_ids:
                    rec.proof_evidence_ids = [ob.evidence_id]
                    proven.append(rec)
        return proven


class MissingEvidenceAgent(_Agent):
    """A plan for every payment still without its document; polite supplier requests when allowed (§22, §25)."""

    name = "missing_evidence"

    def plan(self, rec: TxRecord) -> str:
        """Plain-language next step for one payment without its document."""
        amount = format_money(abs(rec.tx.amount), rec.tx.currency)
        who = self.o.merchant_name(rec.tx)
        when = day_month(rec.tx.booked_on, self.repo.today())
        chase = self.repo.chases.get(rec.id)
        if chase is not None:
            return (f"I asked {who} for the invoice for the {amount} payment on {when}. "
                    "Suppliers usually reply within a few days.")
        if rec.likely_document_ids:
            return f"I found a likely document for the {amount} payment to {who} on {when} and I'm confirming it."
        if rec.decision is not None and rec.decision.provider.value == "owner":
            return f"The {amount} payment to {who} on {when} is waiting for its receipt. I will match it when it arrives."
        if rec.decision is not None and rec.decision.provider.value == "government":
            return f"The {amount} tax payment on {when} is waiting for the tax notice or payment proof."
        return f"I'm looking for the document for the {amount} payment to {who} on {when}."

    def chase_all(self, now: datetime) -> list[str]:
        sent: list[str] = []
        today = now.astimezone(TZ).date()
        for rec in sorted(self.repo.transactions.values(), key=lambda r: (r.tx.booked_on, r.id)):
            if rec.id in self.repo.chases or rec.document_ids or rec.private or rec.tx.amount >= 0:
                continue
            item = self.repo.items[rec.item_id]
            if item.is_done or item.stage in (Stage.NEEDS_OWNER, Stage.CONFLICT):
                continue
            if rec.decision is None or not rec.decision.requires_document or rec.proof_evidence_ids:
                continue
            if (today - rec.tx.booked_on).days < CHASE_AFTER_DAYS:
                continue  # documents often follow the payment by a day or two: not missing yet
            if rec.missing_since is None:
                rec.missing_since = today
                self.repo.closure_log.append(ClosureActivity(
                    kind=ClosureKind.MISSING_DOCUMENT_DETECTED, at=now, entity_id=rec.company_id,
                    subject_id=rec.id, period=Month.of(rec.tx.booked_on)))
            if rec.decision.provider.value != "supplier" or rec.likely_document_ids or rec.tx.entity_id is None:
                continue
            match = self.repo.resolver().resolve_transaction(rec.tx)
            supplier = match.supplier
            if supplier is None or not supplier.contact_email:
                continue
            company = self.repo.companies[rec.tx.entity_id]
            decision = authorize(ActionKind.SUPPLIER_INVOICE_REQUEST, self.repo.policy, ActionContext(
                tenant_id=self.repo.tenant_id, entity_id=company.id, subject_id=rec.id))
            self.log("authorize_chase", subject_id=rec.id, evidence_ids=[rec.evidence_id],
                     response={"allowed": decision.allowed_now, "reason": decision.reason_plain})
            if not decision.allowed_now:
                continue
            facts = ChaseFacts.build(rec.tx, supplier, company)
            message = compose_request(facts, token=thread_token(self.repo.tenant_id, rec.id), today=today,
                                      message_id_domain="backoffice.example")
            line = activity_line(facts)
            self.repo.chases[rec.id] = ChaseRecord(tx_id=rec.id, supplier_id=supplier.id, company_id=company.id,
                                                   message=message, sent_at=now, line=line)
            self.repo.closure_log.append(ClosureActivity(
                kind=ClosureKind.SUPPLIER_CHASED, at=now, entity_id=company.id, subject_id=supplier.id,
                period=Month.of(rec.tx.booked_on)))
            self.o.activity(now, "chased", line, company.id, evidence_ids=[rec.evidence_id])
            self.log("request_invoice", subject_id=rec.id, evidence_ids=[rec.evidence_id],
                     values={"to": message.to, "subject": message.subject}, response={"message_id": message.message_id})
            sent.append(rec.id)
        return sent


_AMOUNT_IN_QUESTION = re.compile(r"(?:€\s?(\d[\d.,]*\d|\d)|(\d[\d.,]*\d|\d)\s?(?:€|eur(?:os?)?\b))", re.I)


class AccountantAgent(_Agent):
    """Answers routine accountant questions from evidence, when the owner allowed it (§25, §28)."""

    name = "accountant"

    def receive(self, text: str, evidence_id: str, at: datetime) -> list[AccountantQuestion]:
        found = []
        for i, line in enumerate(q.strip() for q in re.split(r"(?<=\?)\s+|\n", text)):
            if not line.endswith("?") or len(line) < 12:
                continue
            qid = f"aq_{evidence_id[3:15]}_{i}"
            if qid in self.repo.accountant_questions:
                continue
            company_id = self._company_for(line) or next(iter(self.repo.companies))
            question = AccountantQuestion(id=qid, company_id=company_id, text=line, evidence_id=evidence_id,
                                          asked_at=at)
            self.repo.accountant_questions[qid] = question
            self.repo.closure_log.append(ClosureActivity(
                kind=ClosureKind.ACCOUNTANT_QUESTION_ASKED, at=at, entity_id=company_id, subject_id=qid))
            self.log("question_received", subject_id=qid, evidence_ids=[evidence_id], values={"text": line})
            found.append(question)
        return found

    def _company_for(self, text: str) -> str | None:
        folded = text.casefold()
        for entity in self.repo.entities:
            if entity.name.casefold() in folded:
                return entity.id
        amount = _amount_in(text)
        if amount is not None:
            for rec in self.repo.transactions.values():
                if abs(rec.tx.amount) == amount:
                    return rec.company_id
        return None

    def answer_all(self, now: datetime) -> list[AccountantQuestion]:
        answered = []
        for q in sorted(self.repo.accountant_questions.values(), key=lambda q: q.id):
            if q.status != "waiting":
                continue
            amount = _amount_in(q.text)
            if amount is None:
                continue
            candidates = [r for r in self.repo.transactions.values()
                          if abs(r.tx.amount) == amount and r.company_id == q.company_id and r.document_ids
                          and self.repo.items[r.item_id].stage is Stage.CLOSED]
            if len(candidates) != 1:
                continue
            rec = candidates[0]
            decision = authorize(ActionKind.ROUTINE_ACCOUNTANT_RESPONSE, self.repo.policy, ActionContext(
                tenant_id=self.repo.tenant_id, entity_id=q.company_id, subject_id=q.id))
            if not decision.allowed_now:
                continue
            doc = self.repo.documents[rec.document_ids[0]]
            who = display_name(doc.document.supplier_name or rec.tx.counterparty)
            month_name = Month.of(rec.tx.booked_on).name
            answer = (f"Yes. The {format_money(abs(rec.tx.amount), rec.tx.currency)} payment to {who} on "
                      f"{day_month(rec.tx.booked_on, now.date())} matches the "
                      f"{doc.label.split(' · ')[0][:1].lower()}{doc.label.split(' · ')[0][1:]}"
                      f" for {month_name}.")
            q.status = "answered"
            q.answer = answer
            q.answer_evidence_ids = (rec.evidence_id, *doc.evidence_ids)
            self.repo.closure_log.append(ClosureActivity(
                kind=ClosureKind.ACCOUNTANT_QUESTION_RESOLVED, at=now, entity_id=q.company_id, subject_id=q.id,
                period=Month.of(rec.tx.booked_on)))
            what = _purpose(doc)
            self.o.activity(now, "answered",
                            f"Answered your accountant: the {format_money(abs(rec.tx.amount), rec.tx.currency)} "
                            f"payment to {who} is {what}.", q.company_id, evidence_ids=q.answer_evidence_ids)
            self.log("answer_accountant", subject_id=q.id, evidence_ids=list(q.answer_evidence_ids),
                     response={"answer": answer, "reason": decision.reason_plain})
            answered.append(q)
        return answered


class ClosureAgent(_Agent):
    """Moves items along the golden path with evidence (§3) and computes month status (§2, §48)."""

    name = "closure"

    def progress(self) -> int:
        repo = self.repo
        moved = 0
        for rec in sorted(repo.transactions.values(), key=lambda r: r.id):
            item = repo.items[rec.item_id]
            if item.is_done or item.stage is Stage.CONFLICT:
                continue
            if rec.private:
                continue
            if item.stage is Stage.ACQUIRED and rec.decision is not None:
                moved += self.o.advance(item, Stage.UNDERSTOOD, [rec.evidence_id], agent=self.name,
                                        note=rec.decision.reason)
            if rec.decision is None:
                continue
            question = self._open_question(rec)
            if rec.tx.entity_id is None:
                if question is not None and item.stage is not Stage.NEEDS_OWNER:
                    moved += self.o.advance(item, Stage.NEEDS_OWNER, [rec.evidence_id], agent="entity",
                                            note="Which company this belongs to.")
                continue
            if not rec.decision.requires_document and rec.decision.quality is Quality.GREEN:
                moved += self.o.advance(item, Stage.NOT_REQUIRED, [rec.evidence_id], agent=self.name,
                                        quality=Quality.GREEN, note=rec.decision.reason)
                continue
            if rec.proof_evidence_ids:
                evidence = [rec.evidence_id, *rec.proof_evidence_ids]
                moved += self._close(item, evidence, note="The payment matches the tax letter's amount and reference.")
                continue
            if not rec.document_ids:
                continue
            docs = [repo.documents[d] for d in rec.document_ids]
            if any(d.on_hold for d in docs) or any(d.document.quality is not Quality.GREEN for d in docs):
                continue
            evidence = [rec.evidence_id, *[e for d in docs for e in d.evidence_ids]]
            moved += self._close(item, evidence, note=rec.match_headline or "Matched to its document.")
            for d in docs:
                moved += self._close(repo.items[d.item_id], evidence, note="Matched to its payment.")
        return moved

    def _open_question(self, rec: TxRecord) -> NeedsYouRecord | None:
        return next((n for n in self.repo.needs.values()
                     if n.subject_id == rec.id and n.status == "open"), None)

    def _close(self, item: TrackedItem, evidence: list[str], *, note: str) -> int:
        moved = 0
        if item.is_done:
            return 0
        for stage in (Stage.VERIFIED, Stage.MATCHED, Stage.CONFIRMED, Stage.CLOSED):
            if _reached(item, stage) and item.stage is not Stage.NEEDS_OWNER:
                continue
            moved += self.o.advance(item, stage, evidence, agent=self.name, quality=Quality.GREEN,
                                    note=note if stage is Stage.CLOSED else "")
        return moved

    def status(self, company_id: str, month: Month, now: datetime | None = None) -> MonthStatus:
        repo = self.repo
        items = repo.items_for(company_id, month)
        tx_ids = {i.subject_id for i in items if i.subject_type == "transaction"}
        decisions = [repo.transactions[t].decision for t in sorted(tx_ids) if repo.transactions[t].decision]
        return compute_month_status(
            company_id, month, items, now=now or repo.clock.now(), connectors=repo.connectors_for(company_id),
            decisions=decisions, obligations=[o.obligation for o in repo.obligations.values()],
            activities=repo.closure_log, interactions=repo.interactions, tz=TZ,
        )

    def record_closures(self, now: datetime) -> list[tuple[str, str]]:
        closed = []
        for company_id in sorted(self.repo.companies):
            for month in self.repo.months_for(company_id):
                key = (company_id, str(month))
                status = self.status(company_id, month, now)
                if status.closed and key not in self.repo.closed_months:
                    self.repo.closed_months[key] = now.astimezone(TZ).date()
                    self.o.activity(now, "closed", f"Closed {month.name}. Nothing is left open.", company_id)
                    self.log("close_month", subject_id=f"{company_id}:{month}",
                             response={"percent_closed": status.percent_closed})
                    closed.append(key)
                elif not status.closed and key in self.repo.closed_months:
                    del self.repo.closed_months[key]
                    self.log("reopen_month", subject_id=f"{company_id}:{month}",
                             validations=status.reasons())
        return closed


class AuditorAgent(_Agent):
    """Re-checks every closed item against its evidence and reopens any that no longer hold (§55, §57)."""

    name = "auditor"

    def recheck(self) -> list[str]:
        reopened: list[str] = []
        repo = self.repo
        for item in sorted(repo.items.values(), key=lambda i: i.id):
            if item.stage is not Stage.CLOSED:
                continue
            problem = self._problem(item)
            if problem is None:
                continue
            evidence = list(item.history[-1].evidence_ids) if item.history else []
            self.o.advance(item, Stage.CONFLICT, evidence or [item.subject_id], agent=self.name, note=problem)
            self.log("reopen", subject_id=item.id, evidence_ids=evidence, response={"reason": problem})
            reopened.append(item.id)
        return reopened

    def _problem(self, item: TrackedItem) -> str | None:
        repo = self.repo
        if item.subject_type == "document":
            doc = repo.documents[item.subject_id]
            if doc.on_hold:
                return "This document is on hold."
            if doc.document.quality is not Quality.GREEN:
                return "The document's details no longer agree."
            return None
        rec = repo.transactions[item.subject_id]
        if rec.proof_evidence_ids:
            return None
        if not rec.document_ids:
            return "The payment has lost its document."
        docs = [repo.documents.get(d) for d in rec.document_ids]
        if any(d is None or d.on_hold or d.document.quality is not Quality.GREEN for d in docs):
            return "The document for this payment no longer checks out."
        total = sum((d.document.gross_amount or _ZERO) for d in docs if d is not None)
        if len(docs) == 1 and total != abs(rec.tx.amount):
            return "The amounts of the payment and its document no longer agree."
        if rec.tx.entity_id is None:
            return "It is no longer clear which company this belongs to."
        return None


# --------------------------------------------------------------------------- orchestrator


class Orchestrator:
    """The deterministic conductor (§46). Agents never call each other; only this class sequences them."""

    MAX_PASSES = 4

    def __init__(self, repo: Repository) -> None:
        self.repo = repo
        self.discovery = DiscoveryAgent(self)
        self.retrieval = RetrievalAgent(self)
        self.documents = DocumentAgent(self)
        self.verification = VerificationAgent(self)
        self.fraud = FraudAgent(self)
        self.entity = EntityAgent(self)
        self.reconciliation = ReconciliationAgent(self)
        self.obligations = ObligationAgent(self)
        self.missing = MissingEvidenceAgent(self)
        self.accountant = AccountantAgent(self)
        self.closure = ClosureAgent(self)
        self.auditor = AuditorAgent(self)
        self._activity_seq = 0

    # ----------------------------------------------------------------- shared helpers

    def log(self, agent: str, action: str, *, subject_id: str | None = None, evidence_ids: Sequence[str] = (),
            values: Mapping[str, Any] | None = None, validations: Sequence[Any] = (), response: Any = None,
            actor: str = SYSTEM, parser: str | None = None) -> None:
        self.repo.audit.record(self.repo.tenant_id, AuditEntry(
            actor=actor, action=action, agent=agent, parser=parser, subject_id=subject_id,
            evidence_ids=[e for e in evidence_ids if e], extracted_values=_auditable(values or {}),
            validations=_auditable(list(validations)), response=_auditable(response),
        ))

    def advance(self, item: TrackedItem, to: Stage, evidence_ids: Sequence[str], *, agent: str,
                quality: Quality | None = None, note: str = "", actor: str | None = None) -> int:
        """One evidence-carrying transition, stamped with the orchestrator's clock and audited (§3, §55)."""
        who = actor or f"{SYSTEM}:{agent}"
        try:
            item.advance(to, actor=who, evidence_ids=list(evidence_ids), quality=quality, note=note)
        except IllegalTransition as exc:
            self.log(agent, "transition_refused", subject_id=item.id, evidence_ids=evidence_ids,
                     response={"to": to.value, "reason": str(exc)})
            return 0
        item.history[-1] = item.history[-1].model_copy(update={"at": self.repo.clock.now()})
        self.log(agent, "transition", subject_id=item.id, evidence_ids=evidence_ids, actor=who,
                 values={"to": to.value, "quality": item.quality.value}, response=note or None)
        return 1

    def activity(self, at: datetime, kind: str, text: str, company_id: str | None = None, *,
                 amount: Decimal | None = None, currency: str = "EUR", evidence_ids: Sequence[str] = ()) -> None:
        self._activity_seq += 1
        self.repo.activity.append(ActivityEntry(
            id=f"a{self._activity_seq:04d}", at=at, kind=kind, text=text, company_id=company_id, amount=amount,
            currency=currency, evidence_ids=tuple(evidence_ids)))

    # ----------------------------------------------------------------- arrivals

    def ingest_file(self, data: bytes, *, filename: str | None = None, content_type: str | None = None,
                    source_kind: SourceKind = SourceKind.UPLOAD, at: datetime | None = None,
                    origin: str = "upload") -> IngestReport:
        at = self.repo.clock.advance_to(at) if at else self.repo.clock.now()
        if (filename or "").lower().endswith(".csv") or (content_type or "").startswith("text/csv"):
            rows = _parse_bank_csv(data)
            if rows is not None:
                return self.ingest_bank(rows, at=at)
        outcome = self.discovery.file(data, filename=filename, content_type=content_type,
                                      source_kind=source_kind, at=at)
        report = self._process_outcome(outcome, at=at, origin=origin)
        self.run(at)
        return report

    def share(self, payload: SharePayload, at: datetime | None = None) -> IngestReport:
        at = self.repo.clock.advance_to(at) if at else self.repo.clock.now()
        outcome = self.discovery.share(payload)
        report = self._process_outcome(outcome, at=at, origin="share")
        report.pending_links = []
        for url in outcome.pending_links:
            ev = self.retrieval.follow(url, supplier_hint=None, at=at, context={"shared": True})
            if ev is None:
                report.pending_links.append(url)
                continue
            report.evidence_ids.append(ev)
            self._document_from_parts(self._parts_for(ev), at=at, origin="link", retrieved=True, report=report)
        self.run(at)
        return report

    def receive_upload(self, request: UploadRequest) -> tuple[UploadReceipt, IngestReport | None]:
        at = self.repo.clock.now()
        receipt = self.repo.uploads.receive(request)
        self.discovery.log("receive_upload", subject_id=receipt.evidence_id,
                           evidence_ids=[receipt.evidence_id] if receipt.evidence_id else [],
                           response={"status": receipt.status.value})
        if receipt.evidence_id is None or receipt.status.value != "stored":
            return receipt, None
        report = IngestReport(route="upload", message="Got it.", evidence_ids=[receipt.evidence_id])
        evidence = self.repo.evidence(receipt.evidence_id)
        if evidence.format is EvidenceFormat.EML:
            data = self.repo.registry.open(self.repo.tenant_id, evidence.id)
            outcome = self.repo.intake.ingest_file(self.repo.tenant_id, data, filename=evidence.filename,
                                                   mime_type=evidence.mime_type, at=at)
            report = self._process_outcome(outcome, at=at, origin="scan")
        else:
            self._process_evidence(evidence, at=at, origin="scan", report=report)
        self.run(at)
        return receipt, report

    def ingest_bank(self, rows: Sequence[BankRow], *, at: datetime | None = None) -> IngestReport:
        at = self.repo.clock.advance_to(at) if at else self.repo.clock.now()
        created = self.discovery.bank_rows(rows, at)
        self.reconciliation.classify(created)
        report = IngestReport(route="bank", message="Got it.", evidence_ids=[r.evidence_id for r in created],
                              transaction_ids=[r.id for r in created])
        by_company: dict[str, int] = {}
        for rec in created:
            by_company[rec.holder_id] = by_company.get(rec.holder_id, 0) + 1
        self.run(at)
        for company_id, n in sorted(by_company.items()):
            done = sum(1 for r in created if r.holder_id == company_id and self.repo.items[r.item_id].is_done)
            noun = "bank transaction" if n == 1 else "bank transactions"
            tail = ""
            if done == n and n > 1:
                tail = " All of them are settled."
            elif done == n:
                tail = " It is settled."
            self.activity(at, "checked", f"Checked {n} new {noun}.{tail}", company_id,
                          evidence_ids=[r.evidence_id for r in created if r.holder_id == company_id])
        return report

    # ----------------------------------------------------------------- routing

    def _process_outcome(self, outcome: ShareOutcome, *, at: datetime, origin: str) -> IngestReport:
        report = IngestReport(route=outcome.route.value, message=outcome.owner_message,
                              evidence_ids=[r.evidence.id for r in outcome.registrations],
                              pending_links=list(outcome.pending_links))
        if outcome.email is not None:
            self._process_email(outcome.email, at=at, origin="email" if origin == "upload" else origin, report=report)
            return report
        for reg in outcome.registrations:
            self._process_evidence(reg.evidence, at=at, origin=origin, report=report)
        return report

    def _process_evidence(self, evidence: Evidence, *, at: datetime, origin: str, report: IngestReport) -> None:
        parts = self._parts_for(evidence.id)
        if not parts:
            report.stored_only = True
            report.message = self._unread_message(evidence)
            self.discovery.log("stored_for_reading", subject_id=evidence.id, evidence_ids=[evidence.id],
                               values={"format": evidence.format.value})
            return
        letter = _letter_text(parts)
        if letter:
            ob = self.obligations.detect(letter, evidence.id, received_on=at.astimezone(TZ).date())
            if ob is not None and not _QR_START.search(letter):
                report.obligation_ids.append(ob.obligation.id)
                self.activity(at, "collected", f"Read a letter about {ob.title.lower()}.",
                              ob.obligation.entity_id, amount=ob.obligation.amount, evidence_ids=[evidence.id])
                return
        record = self._document_from_parts(parts, at=at, origin=origin, retrieved=False, report=report)
        if record is None and any(p.kind == "read" for p in parts):
            report.message = "Got it. I stored it, but I couldn't find invoice details in it."

    def _unread_message(self, evidence: Evidence) -> str:
        """Why a stored file was not read, in plain words (§36, §70)."""
        if evidence.format not in _READABLE_FILES:
            return "Got it. I saved it and will read it shortly."
        if self.repo.reader is None:
            return "Got it. I stored it. Reading photos and PDFs is switched off in this demo."
        outcome = self.repo.reads.get(evidence.id)
        missing = {s.step for s in outcome.missing_readers()} if outcome is not None else set()
        if outcome is None or outcome.found_anything:
            return "Got it. I stored it, but I couldn't read it."
        if evidence.format is EvidenceFormat.PDF and "pdf_text" in missing:
            return "Got it. I stored it. Reading PDFs is not set up here yet."
        if "ocr" in missing:
            what = "scanned PDFs" if evidence.format is EvidenceFormat.PDF else "photos"
            return f"Got it. I stored it. Reading {what} is not set up here yet."
        return "Got it. I stored it, but I couldn't find invoice details in it."

    def _parts_for(self, evidence_id: str) -> list[_Part]:
        """The readable parts of one piece of evidence: text/XML directly; PDFs and photos through the reader."""
        evidence = self.repo.evidence(evidence_id)
        if evidence.format in _READABLE_FILES:
            return self._read_file(evidence)
        part = self._part_for(evidence_id)
        return [part] if part is not None else []

    def _read_file(self, evidence: Evidence) -> list[_Part]:
        """Stage 0 and the OCR chain for a PDF or photo (§13-17); nothing when no reader is configured."""
        repo = self.repo
        if repo.reader is None:
            return []
        outcome = repo.reads.get(evidence.id)
        if outcome is None:
            from backoffice.reading import ReadRequest  # server only: the browser demo has no reader

            data = repo.registry.open(repo.tenant_id, evidence.id)
            request = ReadRequest(
                tenant_id=repo.tenant_id, evidence_id=evidence.id, data=data, mime_type=evidence.mime_type,
                stage0_fields=lambda text, method: self.documents.stage0_fields(text, evidence.id, method),
                extractor=self.documents.text_extractor(),
            )
            try:
                outcome = repo.reader.read(request)
            except Exception as exc:  # a reader bug must never lose the upload: it stays stored
                self.documents.log("read_failed", subject_id=evidence.id, evidence_ids=[evidence.id],
                                   response={"error": type(exc).__name__})
                return []
            repo.reads[evidence.id] = outcome
            self.documents.log(
                "read_file", subject_id=evidence.id, evidence_ids=[evidence.id],
                values={"pages": outcome.page_count, "cost": outcome.cost, "engines": list(outcome.engines)},
                validations=[s.as_dict() for s in outcome.steps], response={"found": outcome.found_anything},
                parser=",".join(outcome.engines) or None)
        if not outcome.found_anything:
            return []
        parts = [_Part(evidence.id, "ubl", data=xml) for xml in outcome.embedded_xml]
        parts.append(_Part(
            evidence.id, "read", text=outcome.text, method=outcome.text_method,
            observations={name: list(found) for name, found in outcome.readings.items()},
            reading_text=outcome.reading_text, supplier_name=outcome.supplier_name, doc_type=outcome.doc_type,
            parser=",".join(outcome.engines),
        ))
        return parts

    def _part_for(self, evidence_id: str) -> _Part | None:
        evidence = self.repo.evidence(evidence_id)
        data = self.repo.registry.open(self.repo.tenant_id, evidence_id)
        if evidence.format in (EvidenceFormat.UBL, EvidenceFormat.XML):
            return _Part(evidence_id, "ubl", data=data)
        if evidence.format in (EvidenceFormat.TEXT, EvidenceFormat.QR, EvidenceFormat.CSV):
            try:
                return _Part(evidence_id, "text", text=data.decode("utf-8"))
            except UnicodeDecodeError:
                return None
        return None

    def _process_email(self, result: EmailIngestResult, *, at: datetime, origin: str, report: IngestReport) -> None:
        parsed = result.parsed
        message_id = result.message.evidence.id
        sender = parsed.sender.address if parsed.sender else None
        text = f"{parsed.subject}\n{parsed.text_body}"
        if self.repo.accountant and sender and sender.lower() == self.repo.accountant.email.lower():
            questions = self.accountant.receive(parsed.text_body, message_id, at)
            report.question_ids += [q.id for q in questions]
            return
        body_part = _Part(message_id, "email_body", text=parsed.text_body) if parsed.text_body.strip() else None
        groups: list[list[_Part]] = []
        for f in result.files:
            file_parts = self._parts_for(f.evidence.id)
            if file_parts:
                groups.append(file_parts)
            else:
                report.stored_only = True
        supplier = self.repo.supplier_for_domain(parsed.sender_domain)
        for link in parsed.invoice_links:
            ev = self.retrieval.follow(link.url, supplier_hint=supplier.name if supplier else None, at=at,
                                       context={"email_evidence_id": message_id})
            if ev is None:
                report.pending_links.append(link.url)
                continue
            report.evidence_ids.append(ev)
            self._document_from_parts(self._parts_for(ev), at=at, origin="link", retrieved=True,
                                      report=report, sender=sender, message_text=text, body=body_part)
        for file_parts in groups:
            letter = _letter_text(file_parts)
            if letter:
                ob = self.obligations.detect(letter, file_parts[0].evidence_id, received_on=at.astimezone(TZ).date(),
                                             sender=sender or "")
                if ob is not None and not _QR_START.search(letter):
                    report.obligation_ids.append(ob.obligation.id)
                    continue
            self._document_from_parts(file_parts, at=at, origin=origin, retrieved=False, report=report,
                                      sender=sender, message_text=text, body=body_part)
        for nested in result.attached_emails:
            self._process_email(nested, at=at, origin=origin, report=report)

    # ----------------------------------------------------------------- documents

    def _document_from_parts(self, parts: list[_Part], *, at: datetime, origin: str, retrieved: bool,
                             report: IngestReport, sender: str | None = None, message_text: str = "",
                             body: _Part | None = None) -> DocumentRecord | None:
        extracted = self.documents.read(parts)
        if extracted is None:
            report.stored_only = True
            return None
        if body is not None:
            # The email body joins only when it describes the same invoice (same number).
            body_read = self.documents.read([body])
            if body_read is not None and body_read.invoice_number and extracted.invoice_number and \
                    _same_number(body_read.invoice_number, extracted.invoice_number):
                extracted = self.documents.read([*parts, body]) or extracted
        self.documents.log("extract", evidence_ids=extracted.evidence_ids,
                           values={k: [o.value for o in v] for k, v in sorted(extracted.observations.items())},
                           parser=",".join(dict.fromkeys(extracted.parsers)) or None)
        assessment = self.verification.assess(extracted.observations, extracted.doc_type,
                                              evidence_ids=extracted.evidence_ids)
        values, quality, reasons = _settled_values(assessment), assessment.quality, assessment.reasons
        supplier = self.repo.supplier_for_tax_id(values.get("supplier_tax_id"))
        existing = self._duplicate_of(values, supplier)
        if existing is not None:
            return self._merge_duplicate(existing, extracted, report)
        doc_id = "doc_" + extracted.evidence_ids[0][3:19]
        customer = values.get("customer_tax_id")
        if extracted.buyer_is_final_consumer and customer and str(customer) == "999999990":
            customer = None  # sold to a final consumer: addressed to nobody in particular
        document = Document(
            id=doc_id, tenant_id=self.repo.tenant_id, evidence_ids=extracted.evidence_ids,
            doc_type=extracted.doc_type,
            supplier_name=supplier.name if supplier else display_name(extracted.supplier_name, fallback="Supplier"),
            supplier_tax_id=_text(values.get("supplier_tax_id")), customer_tax_id=_text(customer),
            invoice_number=_text(values.get("invoice_number")), issue_date=_date(values.get("issue_date")),
            due_date=_date(values.get("due_date")), currency=_text(values.get("currency")) or "EUR",
            net_amount=_dec(values.get("net_amount")), vat_amount=_dec(values.get("vat_amount")),
            gross_amount=_dec(values.get("gross_amount")), iban=_text(values.get("iban")),
            payment_reference=_text(values.get("payment_reference")), quality=quality,
        )
        item = TrackedItem(id="item_" + doc_id, tenant_id=self.repo.tenant_id, subject_type="document",
                           subject_id=doc_id)
        self.repo.items[item.id] = item
        record = DocumentRecord(
            document=document, evidence_ids=extracted.evidence_ids, origin=origin, received_at=at, item_id=item.id,
            observations=extracted.observations, reasons=reasons, sender=sender, message_text=message_text,
            supplier_id=supplier.id if supplier else None, retrieved=retrieved, checks=assessment.verified_fields,
        )
        self.repo.documents[doc_id] = record
        evidence = extracted.evidence_ids
        self.advance(item, Stage.ACQUIRED, evidence, agent="discovery", note="Document received.")
        self.advance(item, Stage.UNDERSTOOD, evidence, agent="document", note="Details read.")
        if quality is Quality.RED:
            self.advance(item, Stage.CONFLICT, evidence, agent="verification", note=" ".join(reasons))
        # Fraud runs on every supplier document, before anything can be matched or paid (§26).
        record.fraud = self.fraud.check(record)
        entity_id = self.entity.assign_document(record)
        if entity_id:
            record.document = record.document.model_copy(update={"entity_id": entity_id})
        if record.fraud.hard_stop:
            self._hold(record, at)
        company = self.repo.item_company(item)
        who = display_name(document.supplier_name)
        if retrieved:
            self.activity(at, "recovered", f"Recovered the {who} invoice from a link in your email.", company,
                          amount=document.gross_amount, currency=document.currency, evidence_ids=evidence)
        else:
            source = {"email": "your email", "scan": "your phone", "share": "something you shared"}.get(
                origin, "your upload")
            self.activity(at, "collected", f"Collected the {who} {_DOC_LABELS.get(document.doc_type, 'document').lower()}"
                          f" from {source}.", company, amount=document.gross_amount, currency=document.currency,
                          evidence_ids=evidence)
        report.document_ids.append(doc_id)
        if record.on_hold:
            report.message = f"Got it. I put the {who} payment on hold: {record.fraud.owner_message}"
        elif quality is Quality.RED:
            needs = self._ask_about_conflict(record, at)
            if needs is not None:
                report.message = f"Got it. I need one answer from you: {needs.prompt}"
        return record

    def _duplicate_of(self, values: Mapping[str, Any], supplier: Supplier | None) -> DocumentRecord | None:
        number = _text(values.get("invoice_number"))
        tax_id = _text(values.get("supplier_tax_id"))
        if not number or not tax_id:
            return None
        for rec in self.repo.documents.values():
            doc = rec.document
            if doc.invoice_number and _same_number(doc.invoice_number, number) and same_tax_id(doc.supplier_tax_id, tax_id):
                return rec
        return None

    def _merge_duplicate(self, record: DocumentRecord, extracted: _Extracted, report: IngestReport) -> DocumentRecord:
        """The same invoice again (another copy or channel): its observations join, nothing is duplicated."""
        new = [e for e in extracted.evidence_ids if e not in record.evidence_ids]
        if not new:
            report.document_ids.append(record.id)
            report.message = "Got it. I already had this one."
            report.already_known = True
            return record
        for name, obs in extracted.observations.items():
            record.observations.setdefault(name, []).extend(obs)
        record.evidence_ids = [*record.evidence_ids, *new]
        assessment = self.verification.assess(record.observations, record.document.doc_type, subject_id=record.id,
                                              evidence_ids=record.evidence_ids, owner=record.owner_values)
        record.document = record.document.model_copy(update={"quality": assessment.quality,
                                                             "evidence_ids": record.evidence_ids})
        record.reasons = assessment.reasons
        record.checks = assessment.verified_fields
        self.documents.log("merge_duplicate", subject_id=record.id, evidence_ids=new)
        report.document_ids.append(record.id)
        if assessment.quality is Quality.RED and not record.on_hold:
            now = self.repo.clock.now()
            item = self.repo.items[record.item_id]
            if item.stage is not Stage.CONFLICT:
                self.advance(item, Stage.CONFLICT, record.evidence_ids, agent="verification",
                             note=" ".join(assessment.reasons))
            needs = self._ask_about_conflict(record, now)
            if needs is not None:
                report.message = f"Got it. I need one answer from you: {needs.prompt}"
        return record

    def _hold(self, record: DocumentRecord, at: datetime) -> None:
        repo = self.repo
        record.on_hold = True
        item = repo.items[record.item_id]
        who = display_name(record.document.supplier_name)
        self.advance(item, Stage.NEEDS_OWNER, record.evidence_ids, agent="fraud",
                     note=record.fraud.owner_message if record.fraud else "")
        needs_id = _unique_id(repo.needs, f"nd_{_slug(who.split()[0])}_{'iban' if self._iban_changed(record) else 'hold'}")
        company = record.document.entity_id or self._holder_for_document(record)
        repo.needs[needs_id] = NeedsYouRecord(
            id=needs_id, kind="approval", subject_type="document", subject_id=record.id, item_id=item.id,
            company_id=company, created_at=at, why=tuple(s.owner_line for s in record.fraud.signals if s.hard_stop)
            if record.fraud else ())
        self.activity(at, "protected", f"Put the {who} payment on hold. The bank details on the invoice changed."
                      if self._iban_changed(record) else f"Put the {who} payment on hold. Something on the invoice "
                      "does not look right.", company, evidence_ids=record.evidence_ids)

    # ----------------------------------------------------------------- sources that disagree (§19, §37)

    def _ask_about_conflict(self, record: DocumentRecord, at: datetime) -> NeedsYouRecord | None:
        """One plain question when a document's sources disagree. Nothing is guessed meanwhile (§19)."""
        repo = self.repo
        if any(n.subject_id == record.id and n.status == "open" for n in repo.needs.values()):
            return None
        prompt, options, why = self._conflict_question(record)
        who = display_name(record.document.supplier_name)
        needs_id = _unique_id(repo.needs, f"nd_{_slug(who.split()[0])}_check")
        company = record.document.entity_id or self._holder_for_document(record)
        needs = NeedsYouRecord(
            id=needs_id, kind="check", subject_type="document", subject_id=record.id, item_id=record.item_id,
            company_id=company, created_at=at, why=why, prompt=prompt, options=options)
        repo.needs[needs_id] = needs
        self.verification.log("ask_owner", subject_id=record.id, evidence_ids=record.evidence_ids,
                              values={"options": [o.label for o in options]}, response={"needs_you": needs_id})
        line = (f"Found two different values on the {who} invoice. I asked you which is right." if len(options) > 1
                else f"The {who} invoice does not add up. I asked you what to do.")
        self.activity(at, "checked", line, company, evidence_ids=record.evidence_ids)
        return needs

    def _conflict_question(self, record: DocumentRecord) -> tuple[str, tuple[CheckOption, ...], tuple[str, ...]]:
        assessment = self.verification.assess(record.observations, record.document.doc_type,
                                              subject_id=record.id, evidence_ids=record.evidence_ids,
                                              owner=record.owner_values)
        red = [name for name in _CONFLICT_ORDER
               if name in assessment.fields and assessment.fields[name].quality is Quality.RED]
        red += sorted(n for n, a in assessment.fields.items() if a.quality is Quality.RED and n not in red)
        who = display_name(record.document.supplier_name)
        currency = record.document.currency
        observed = _with_owner(record.observations, record.owner_values)
        why = [r for n in red for r in assessment.fields[n].reasons]
        why.append("Until you answer, I won't match, pay or close this invoice.")
        neither = CheckOption(id="neither", label=f"Neither. I'll get a corrected invoice from {who}.")
        for lead in red:
            groups = _value_groups(lead, observed.get(lead, []))
            if len(groups) < 2:
                continue
            shown = show_many(lead, [value for value, _, _ in groups], currency)
            options = []
            for i, ((value, channels, methods), text) in enumerate(zip(groups, shown, strict=True), start=1):
                labels = list(dict.fromkeys(method_label(m) for m in methods))
                sources, verb = join(labels), ("shows" if len(labels) == 1 else "show")
                picks = {lead: value}
                for other in red:
                    if other != lead:
                        picked = _value_from(other, observed.get(other, []), channels)
                        if picked is not None:
                            picks[other] = picked
                options.append(CheckOption(id=f"source_{i}", label=f"{text}, as {sources} {verb}", values=picks,
                                           channels=channels))
            noun = field_label(lead).removeprefix("the ")
            prompt = f"Which is the right {noun} on the {who} invoice?"
            return prompt, (*options, neither), tuple(dict.fromkeys(why))
        prompt = f"The {who} invoice does not add up. What should I do?"
        set_aside = CheckOption(id="neither", label=f"Set it aside. I'll get a corrected invoice from {who}.")
        return prompt, (set_aside,), tuple(dict.fromkeys(why))

    def _apply_assessment(self, record: DocumentRecord, assessment: DocumentAssessment) -> None:
        """The document's fields and quality as verified now (a disputed field stays empty, §19)."""
        values = _settled_values(assessment)
        update: dict[str, Any] = {"quality": assessment.quality}
        for name in ("supplier_tax_id", "invoice_number", "iban", "payment_reference"):
            update[name] = _text(values.get(name))
        for name in ("net_amount", "vat_amount", "gross_amount"):
            update[name] = _dec(values.get(name))
        for name in ("issue_date", "due_date"):
            update[name] = _date(values.get(name))
        update["currency"] = _text(values.get("currency")) or record.document.currency
        record.document = record.document.model_copy(update=update)
        record.reasons = assessment.reasons
        record.checks = assessment.verified_fields

    def _iban_changed(self, record: DocumentRecord) -> bool:
        return bool(record.fraud and record.fraud.of_kind(SignalKind.CHANGED_IBAN))

    def _holder_for_document(self, record: DocumentRecord) -> str:
        supplier = record.supplier_id
        for rec in sorted(self.repo.transactions.values(), key=lambda r: r.tx.booked_on, reverse=True):
            for d in rec.document_ids:
                if self.repo.documents[d].supplier_id == supplier and supplier:
                    return rec.company_id
        return next(iter(self.repo.companies))

    # ----------------------------------------------------------------- the pipeline

    def run(self, at: datetime | None = None) -> RunReport:
        """Run every agent in the fixed order until nothing moves (at most ``MAX_PASSES``)."""
        now = self.repo.clock.advance_to(at) if at else self.repo.clock.now()
        report = RunReport()
        for _ in range(self.MAX_PASSES):
            report.passes += 1
            moved = self._entities(now)
            self.reconciliation.classify([r for r in self.repo.transactions.values() if r.decision is None])
            matches = self.reconciliation.match()
            for m in matches:
                self._reverify_with_bank(m)
            proven = self.obligations.prove()
            moved += self.closure.progress()
            moved += len(matches) + len(proven)
            report.transitions += moved
            if not moved:
                break
        report.chased = self.missing.chase_all(now)
        self.accountant.answer_all(now)
        report.reopened = self.auditor.recheck()
        report.closed_months = self.closure.record_closures(now)
        self._record_recovered(now)
        return report

    def _entities(self, now: datetime) -> int:
        moved = 0
        for rec in sorted(self.repo.transactions.values(), key=lambda r: r.id):
            if rec.tx.entity_id is not None or rec.private or self.repo.items[rec.item_id].is_done:
                continue
            open_q = next((n for n in self.repo.needs.values() if n.subject_id == rec.id and n.status == "open"), None)
            result = self.entity.assign(rec)
            rec.assignment = result
            if result.entity_id is not None and result.quality is Quality.GREEN:
                rec.tx = rec.tx.model_copy(update={"entity_id": result.entity_id})
                self.repo.history_pairs.append((counterparty_key(rec.tx.counterparty) or rec.id, result.entity_id))
                if open_q is not None:
                    open_q.status = "resolved"
                    open_q.resolution = "rule" if result.rule_id else "evidence"
                moved += 1
            elif result.private and result.quality is Quality.GREEN:
                rec.private = True
                if open_q is not None:
                    open_q.status = "resolved"
                    open_q.resolution = "rule"
                self.advance(self.repo.items[rec.item_id], Stage.NOT_REQUIRED, [rec.evidence_id], agent="entity",
                             quality=Quality.GREEN, note="Personal, by your rule.")
                moved += 1
            elif result.question is not None and open_q is None:
                amount = abs(rec.tx.amount)
                who = self.merchant_name(rec.tx)
                needs_id = _unique_id(self.repo.needs, f"nd_{_slug(who.split()[0])}_{int(amount)}")
                self.repo.needs[needs_id] = NeedsYouRecord(
                    id=needs_id, kind="choice", subject_type="transaction", subject_id=rec.id, item_id=rec.item_id,
                    company_id=rec.holder_id, created_at=now, question=result.question,
                    why=self._entity_why(rec, result))
                self.entity.log("ask_owner", subject_id=rec.id, evidence_ids=[rec.evidence_id],
                                response={"needs_you": needs_id})
        return moved

    def merchant_name(self, tx: Transaction) -> str:
        """Plain name of whoever was paid: the known supplier's name, else the cleaned bank descriptor."""
        supplier = self.repo.resolver().resolve_transaction(tx).supplier
        return display_name(supplier.name) if supplier else display_name(tx.counterparty)

    def _entity_why(self, rec: TxRecord, result: EntityAssignment) -> tuple[str, ...]:
        lines: list[str] = []
        account = self.repo.accounts.get(rec.tx.account_id)
        if account is not None and not account.owned:
            lines.append(f"It was paid with {account.label}, which you use for more than one company.")
        key = counterparty_key(rec.tx.counterparty)
        past = [t for k, t in self.repo.history_pairs if k == key]
        if past:
            names = sorted({self.repo.company_name(t) or "Personal" for t in past})
            who = self.merchant_name(rec.tx)
            times = "once" if len(past) == 1 else ("twice" if len(past) == 2 else f"{len(past)} times")
            lines.append(f"Earlier {who} payments went to {', '.join(names)} ({times}). That is not enough to be sure.")
        for line in result.why:
            if line not in lines:
                lines.append(line)
        return tuple(lines)

    def _reverify_with_bank(self, match: Match) -> None:
        """A 1-to-1 match lets the bank's booked amount confirm the document's total (§19)."""
        if len(match.transaction_ids) != 1 or len(match.document_ids) != 1:
            return
        rec = self.repo.transactions[match.transaction_ids[0]]
        doc = self.repo.documents[match.document_ids[0]]
        assessment = self.verification.assess(
            doc.observations, doc.document.doc_type, bank_amount=abs(rec.tx.amount), subject_id=doc.id,
            evidence_ids=[*doc.evidence_ids, rec.evidence_id], owner=doc.owner_values)
        doc.document = doc.document.model_copy(update={"quality": assessment.quality})
        doc.reasons = assessment.reasons
        doc.checks = assessment.verified_fields
        if doc.document.entity_id is None and rec.tx.entity_id:
            doc.document = doc.document.model_copy(update={"entity_id": rec.tx.entity_id})

    def _record_recovered(self, now: datetime) -> None:
        for rec in self.repo.transactions.values():
            if not rec.document_ids or rec.missing_since is None:
                continue
            if rec.id in self.repo.recovered_tx_ids:
                continue
            self.repo.recovered_tx_ids.add(rec.id)
            self.repo.closure_log.append(ClosureActivity(
                kind=ClosureKind.MISSING_DOCUMENT_RETRIEVED, at=now, entity_id=rec.company_id, subject_id=rec.id,
                period=Month.of(rec.tx.booked_on)))

    # ----------------------------------------------------------------- the owner's answers

    def answer(self, needs_id: str, option_id: str, *, remember: bool = False) -> AnswerOutcome:
        repo = self.repo
        needs = repo.needs.get(needs_id)
        if needs is None:
            raise KeyError(needs_id)
        if needs.status != "open":
            raise PermissionError("already answered")
        now = repo.clock.now()
        answer_ev = self._record_answer(needs, option_id, now)
        repo.interactions.append(OwnerInteraction(at=now, active_seconds=ANSWER_SECONDS,
                                                  kind=InteractionKind.APPROVAL if needs.kind == "approval"
                                                  else InteractionKind.ANSWER, entity_id=needs.company_id))
        if needs.kind == "approval":
            outcome = self._answer_approval(needs, option_id, answer_ev, now)
        elif needs.kind == "check":
            outcome = self._answer_check(needs, option_id, answer_ev, now)
        else:
            outcome = self._answer_choice(needs, option_id, remember, answer_ev, now)
        self.run(now)
        return outcome

    def _record_answer(self, needs: NeedsYouRecord, option_id: str, now: datetime) -> str:
        body = json.dumps({"needs_id": needs.id, "option_id": option_id, "answered_by": self.repo.owner.email,
                           "answered_at": now.isoformat(), "subject_id": needs.subject_id}, sort_keys=True).encode()
        reg = self.repo.registry.register(body, tenant_id=self.repo.tenant_id, source_kind=SourceKind.UPLOAD,
                                          format=EvidenceFormat.JSON, mime_type="application/json",
                                          retrieved_at=now, metadata={"kind": "owner_answer"})
        self.log("owner", "answer", subject_id=needs.id, evidence_ids=[reg.evidence.id], actor=OWNER_ACTOR,
                 values={"option_id": option_id})
        return reg.evidence.id

    def _answer_choice(self, needs: NeedsYouRecord, option_id: str, remember: bool, answer_ev: str,
                       now: datetime) -> AnswerOutcome:
        repo = self.repo
        question = needs.question
        assert question is not None
        option_id = _option_alias(question, option_id)
        try:
            option = question.option(option_id)
        except KeyError:
            raise ValueError("not one of the options") from None
        rec = repo.transactions[needs.subject_id]
        item = repo.items[rec.item_id]
        learned = None
        if remember:
            proposal = suggest_rule_from_answer(question, Answer(
                question_id=question.id, option_id=option.id, answered_by=OWNER_ACTOR, answered_at=now))
            if proposal is not None:
                rule = proposal.rule.model_copy(update={"id": "rule_" + hashlib.sha256(
                    f"{question.id}:{option.id}".encode()).hexdigest()[:12]})
                repo.rulebook.add(rule, reason="one-tap answer")
                learned = proposal.label
                self.activity(now, "learned", f"Learned: {proposal.label[0].lower()}{proposal.label[1:]}. "
                              "I will not ask again.", None, evidence_ids=[answer_ev])
                self.log("entity", "learn_rule", subject_id=rule.id, evidence_ids=[answer_ev], actor=OWNER_ACTOR,
                         values={"label": proposal.label})
        needs.status = "answered"
        needs.answer = option.id
        needs.answered_at = now
        who = self.merchant_name(rec.tx)
        if option.kind is OptionKind.ENTITY and option.entity_id:
            rec.tx = rec.tx.model_copy(update={"entity_id": option.entity_id})
            repo.history_pairs.append((counterparty_key(rec.tx.counterparty) or rec.id, option.entity_id))
            name = repo.company_name(option.entity_id)
            message = f"Done. The {who} payment is now with {name}."
            self.advance(item, Stage.UNDERSTOOD, [rec.evidence_id, answer_ev], agent="entity",
                         actor=f"{OWNER_ACTOR}:{repo.owner.email}", note=f"Belongs to {name}.")
        else:
            rec.private = True
            message = f"Done. The {who} payment is set aside as not one of your companies' costs."
            self.advance(item, Stage.NOT_REQUIRED, [rec.evidence_id, answer_ev], agent="entity",
                         actor=f"{OWNER_ACTOR}:{repo.owner.email}", quality=Quality.GREEN,
                         note="Personal or another business, as you said.")
        self.activity(now, "answered", f"You told me where the {who} payment belongs.", rec.company_id,
                      amount=abs(rec.tx.amount), currency=rec.tx.currency, evidence_ids=[answer_ev])
        resolved = self._entities(now) if remember else 0
        others = tuple(n.id for n in repo.needs.values() if n.status == "resolved" and n.resolution == "rule")
        return AnswerOutcome(ok=True, message=message, learned=learned, resolved_ids=others if resolved else ())

    def _answer_approval(self, needs: NeedsYouRecord, option_id: str, answer_ev: str, now: datetime) -> AnswerOutcome:
        repo = self.repo
        record = repo.documents[needs.subject_id]
        item = repo.items[record.item_id]
        who = display_name(record.document.supplier_name)
        if option_id == "keep_blocked":
            needs.status = "answered"
            needs.answer = option_id
            needs.answered_at = now
            self.advance(item, Stage.CONFLICT, [*record.evidence_ids, answer_ev], agent="fraud",
                         actor=f"{OWNER_ACTOR}:{repo.owner.email}", note="Kept blocked by the owner.")
            self.activity(now, "protected", f"Kept the {who} payment blocked. I will ask {who} for a corrected invoice.",
                          needs.company_id, evidence_ids=[answer_ev])
            return AnswerOutcome(ok=True, message=f"Done. It stays blocked. I will ask {who} for a corrected invoice.")
        if option_id != "confirmed_by_phone":
            raise ValueError("not one of the options")
        supplier = repo.suppliers.get(record.supplier_id or "")
        iban = record.document.iban
        if supplier is None or not iban:
            raise ValueError("nothing to confirm")
        fingerprint = self.payment_fingerprint(record)
        approval = Approval(tenant_id=repo.tenant_id, action=ActionKind.BANK_DETAIL_CHANGE, subject_id=record.id,
                            level=Requirement.HARD, approved_by=f"{OWNER_ACTOR}:{repo.owner.email}", approved_at=now,
                            entity_id=needs.company_id, fingerprint=fingerprint, acknowledged_risk=True)
        decision = authorize(ActionKind.BANK_DETAIL_CHANGE, repo.policy, ActionContext(
            tenant_id=repo.tenant_id, entity_id=needs.company_id, subject_id=record.id, fingerprint=fingerprint,
            beneficiary_changed=True, approval=approval))
        self.log("fraud", "authorize_bank_detail_change", subject_id=record.id, evidence_ids=[answer_ev],
                 actor=OWNER_ACTOR, response={"allowed": decision.allowed_now, "reason": decision.reason_plain})
        if not decision.allowed_now:
            raise PermissionError(decision.reason_plain)
        try:
            updated, change = trust_iban(supplier, iban, HardApproval(
                supplier_id=supplier.id, iban=iban, approver_id=repo.owner.email, approver_kind=ApproverKind.HUMAN,
                approved_at=now, verified_out_of_band=True, channel=VerificationChannel.PHONE_CALL_TO_KNOWN_NUMBER,
                evidence_id=answer_ev, note="Owner called the number on earlier invoices."))
        except BeneficiaryChangeRefused as exc:
            raise PermissionError(exc.reason) from None
        repo.suppliers[supplier.id] = updated
        self.log("fraud", "trust_iban", subject_id=supplier.id, evidence_ids=[answer_ev], actor=OWNER_ACTOR,
                 values={"iban": mask_iban(iban)}, response={"changed": change is not None})
        record.on_hold = False
        record.hold_released = True
        needs.status = "answered"
        needs.answer = option_id
        needs.answered_at = now
        self.advance(item, Stage.UNDERSTOOD, [*record.evidence_ids, answer_ev], agent="fraud",
                     actor=f"{OWNER_ACTOR}:{repo.owner.email}", note="New bank details confirmed by the owner by phone.")
        self.activity(now, "protected", f"You confirmed {who}'s new bank details by phone. The payment can go ahead.",
                      needs.company_id, evidence_ids=[answer_ev])
        return AnswerOutcome(ok=True, message="Done. The payment will go to the new account.")

    def _answer_check(self, needs: NeedsYouRecord, option_id: str, answer_ev: str, now: datetime) -> AnswerOutcome:
        """The owner said which source shows the right value (or neither). Their answer is evidence (§19, §55)."""
        repo = self.repo
        option = next((o for o in needs.options if o.id == option_id), None)
        if option is None:
            raise ValueError("not one of the options")
        record = repo.documents[needs.subject_id]
        item = repo.items[record.item_id]
        who = display_name(record.document.supplier_name)
        owner = f"{OWNER_ACTOR}:{repo.owner.email}"
        needs.status = "answered"
        needs.answer = option.id
        needs.answered_at = now
        if not option.values:
            self.verification.log("set_aside", subject_id=record.id, evidence_ids=[*record.evidence_ids, answer_ev],
                                  actor=OWNER_ACTOR, response={"reason": "neither value is right"})
            self.activity(now, "answered", f"You set the {who} invoice aside until a corrected one arrives.",
                          needs.company_id, evidence_ids=[answer_ev])
            return AnswerOutcome(ok=True, message=f"Done. I set it aside. When {who} sends a corrected invoice, "
                                                  "I will read it.")
        for name, value in option.values.items():
            record.owner_values[name] = FieldObservation(
                value=value, source=answer_ev, method=ExtractionMethod.HUMAN, confidence=1.0,
                location="owner answer: " + option.label)
        assessment = self.verification.assess(record.observations, record.document.doc_type, subject_id=record.id,
                                              evidence_ids=[*record.evidence_ids, answer_ev],
                                              owner=record.owner_values)
        self._apply_assessment(record, assessment)
        self.activity(now, "answered", f"You told me which values on the {who} invoice are right.", needs.company_id,
                      amount=record.document.gross_amount, currency=record.document.currency,
                      evidence_ids=[answer_ev])
        if assessment.quality is Quality.RED:
            follow_up = self._ask_about_conflict(record, now)
            ask = f" {follow_up.prompt}" if follow_up is not None else ""
            return AnswerOutcome(ok=True, message=f"Thanks. I still need one more answer.{ask}")
        self.advance(item, Stage.UNDERSTOOD, [*record.evidence_ids, answer_ev], agent="verification", actor=owner,
                     quality=assessment.quality, note=f"You said: {option.label}.")
        return AnswerOutcome(ok=True, message=f"Done. I will use {option.label.split(', as ')[0]} for the {who} "
                                              "invoice.")

    def payment_fingerprint(self, record: DocumentRecord) -> str:
        doc = record.document
        facts = f"{doc.id}|{normalize_iban(doc.iban or '')}|{doc.gross_amount}|{doc.currency}"
        return hashlib.sha256(facts.encode()).hexdigest()

    def payment_decision(self, document_id: str, approval: Approval | None = None) -> Decision:
        """May the payment of this document go out now? Money movement is always a hard approval (§25),
        and a document on a fraud hold stays blocked whatever the approval says (§26)."""
        record = self.repo.documents[document_id]
        supplier = self.repo.suppliers.get(record.supplier_id or "")
        known = {normalize_iban(i) for i in (supplier.known_ibans if supplier else [])}
        changed = bool(record.document.iban) and normalize_iban(record.document.iban or "") not in known
        context = ActionContext(
            tenant_id=self.repo.tenant_id, entity_id=record.document.entity_id, subject_id=record.id,
            fingerprint=self.payment_fingerprint(record), beneficiary_changed=changed or record.on_hold,
            fraud_hold=record.on_hold, approval=approval)
        return authorize(ActionKind.MONEY_MOVEMENT, self.repo.policy, context)

    # ----------------------------------------------------------------- accountant rules (§28)

    def accountant_rule(self, text: str, scope: str = "client", company_id: str | None = None) -> tuple[Rule, int]:
        repo = self.repo
        if repo.accountant is None:
            raise PermissionError("no accountant is connected")
        m = re.match(r"^\s*(?:treat|classify|book)\s+(?:all\s+)?(?P<who>.+?)\s+(?:subscriptions?\s+|payments?\s+|"
                     r"invoices?\s+|expenses?\s+|costs?\s+)?as\s+(?P<what>.+?)\s*\.?\s*$", text, re.I)
        if m is None:
            raise ValueError("I can read rules like “Treat all Adobe subscriptions as Software”.")
        who, what = m.group("who").strip(), m.group("what").strip()
        key = counterparty_key(who)
        supplier = repo.resolver().resolve(who).supplier
        if key is None:
            raise ValueError("I could not tell which supplier the rule is about.")
        all_clients = scope in ("all", "all_clients", "all_clients_of_accountant")
        rule_scope = RuleScope.ALL_CLIENTS_OF_ACCOUNTANT if all_clients else RuleScope.CLIENT
        now = repo.clock.now()
        rule = Rule(
            id="rule_" + hashlib.sha256(f"{text}|{scope}".encode()).hexdigest()[:12],
            author=RuleAuthor.ACCOUNTANT, author_id=repo.accountant.id, scope=rule_scope,
            tenant_id=None if all_clients else repo.tenant_id,
            match=RuleMatch(counterparty_key=supplier.id if supplier else key),
            outcome=RuleOutcome(category=what[:1].upper() + what[1:]), created_at=now,
            label=f"Treat all {display_name(supplier.name) if supplier else who} costs as {what}",
        )
        repo.rulebook.add(rule, reason="accountant rule")
        affected = 0
        accountant_ids = [repo.accountant.id]
        resolver = repo.resolver()
        for rec in repo.transactions.values():
            subject = RuleSubject.from_transaction(rec.tx, key=resolver.resolve_transaction(rec.tx).key)
            decision = repo.rulebook.evaluate(subject, tenant_id=repo.tenant_id, accountant_ids=accountant_ids)
            if decision.get(RuleField.CATEGORY) is not None and decision.category == rule.outcome.category:
                affected += 1
        self.log("accountant", "accountant_rule", subject_id=rule.id, actor=f"accountant:{repo.accountant.id}",
                 values={"text": text, "scope": rule_scope.value}, response={"affected": affected})
        self.activity(now, "learned", f"Your accountant taught me: {rule.label}.", company_id)
        return rule, affected

    # ----------------------------------------------------------------- views used by the service

    def month_status(self, company_id: str, month: Month) -> MonthStatus:
        return self.closure.status(company_id, month)

    def price_changes(self) -> list[tuple[str, Decimal, Decimal, list[str]]]:
        """Recurring costs that went up: (name, before, after, evidence ids) (§23, §60)."""
        out = []
        txs = [*self.repo.history_transactions, *(r.tx for r in self.repo.transactions.values())]
        by_key: dict[str, list[Transaction]] = {}
        resolver = self.repo.resolver()
        for tx in txs:
            if tx.amount >= 0:
                continue
            by_key.setdefault(resolver.resolve_transaction(tx).key, []).append(tx)
        for key, group in sorted(by_key.items()):
            ordered = sorted(group, key=lambda t: (t.booked_on, t.id))
            if len(ordered) < 3:
                continue
            name = self.merchant_name(ordered[-1])
            change = detect_price_change([(t.booked_on, abs(t.amount)) for t in ordered], ordered[-1].currency, name)
            if change is None or not change.increased:
                continue
            before, after = change.old_amount, change.new_amount
            evidence = [r.evidence_id for r in self.repo.transactions.values() if r.tx in ordered]
            out.append((name, before, after, evidence))
        return out


# --------------------------------------------------------------------------- helpers


def _reached(item: TrackedItem, stage: Stage) -> bool:
    from backoffice.domain.lifecycle import ORDER

    target = ORDER.index(stage)
    stages = [item.stage, *(t.to_stage for t in item.history)]
    return any(s in ORDER and ORDER.index(s) >= target for s in stages)


def _split_qr(text: str) -> tuple[list[str], str]:
    """Fiscal QR payloads found in a text (decoded QR codes), and the text without them."""
    payloads, rest = [], []
    for line in (text or "").splitlines():
        m = _QR_START.search(line)
        if m and looks_like_pt_qr(line[m.start():].strip()):
            payloads.append(line[m.start():].strip())
        else:
            rest.append(line)
    return payloads, "\n".join(rest)


def _text_currency(text: str, source: str, method: ExtractionMethod) -> FieldObservation | None:
    """The currency printed next to the document's total ("Total: 92,40 €")."""
    codes: set[str] = set()
    where = None
    for i, line in enumerate(text.splitlines(), start=1):
        if "total" not in line.casefold():
            continue
        for token in _MONEY_WITH_CURRENCY.findall(line):
            mark = currency_mark(token.replace("\u00a0", " ").strip())
            if mark is not None and len(mark.codes) == 1:
                codes.add(mark.codes[0])
                where = where or f"text:line {i}"
    if len(codes) != 1:
        return None
    return FieldObservation(value=codes.pop(), source=source, method=method, confidence=0.6, location=where)


def _text_doc_type(text: str) -> DocumentType:
    folded = text.casefold()
    if "nota de crédito" in folded or "nota de credito" in folded:
        return DocumentType.CREDIT_NOTE
    if "fatura-recibo" in folded or "fatura recibo" in folded:
        return DocumentType.INVOICE_RECEIPT
    if "fatura simplificada" in folded:
        return DocumentType.SIMPLIFIED_INVOICE
    if "fatura" in folded or "invoice" in folded:
        return DocumentType.INVOICE
    return DocumentType.RECEIPT


def _first_line(text: str) -> str | None:
    for line in (text or "").splitlines():
        line = line.strip()
        if line and not line.lower().startswith(("nif", "fatura", "invoice", "data", "atcud")):
            return line
    return None


_READABLE_FILES = frozenset({EvidenceFormat.PDF, EvidenceFormat.IMAGE, EvidenceFormat.SCREENSHOT})
# The field a "which value is right?" question leads with, most useful first.
_CONFLICT_ORDER = ("gross_amount", "vat_amount", "net_amount", "supplier_tax_id", "invoice_number", "issue_date",
                   "iban", "currency", "customer_tax_id", "due_date", "payment_reference")
_OPINION_FLOOR = 0.4  # below this confidence a reading neither supports nor contradicts (verification policy)
_AS_PRINTED = frozenset({"invoice_number", "supplier_tax_id", "customer_tax_id", "payment_reference"})


def _letter_text(parts: Sequence[_Part]) -> str:
    """The readable text of a file, for spotting letters with a deadline (§24)."""
    for part in parts:
        if part.kind == "text":
            return part.text
        if part.kind == "read":
            own = part.text if part.method is not ExtractionMethod.QR else ""
            return own or part.reading_text
    return ""


def _option_value(f: CriticalField, raw: Any) -> Any:
    """A value as the owner confirms it: amounts and dates typed, numbers and references as printed."""
    from backoffice.extraction.values import typed_value

    if f.value in _AS_PRINTED:
        return " ".join(str(raw).split())
    typed = typed_value(f, raw)
    return typed if typed is not None else raw


def _confident(observations: Sequence[FieldObservation]) -> list[FieldObservation]:
    return sorted((o for o in observations
                   if o.confidence >= _OPINION_FLOOR and o.method is not ExtractionMethod.ARITHMETIC),
                  key=lambda o: (-METHOD_RANK.get(o.method, 0), -o.confidence, o.source))


def _value_groups(name: str, observations: Sequence[FieldObservation]
                  ) -> list[tuple[Any, frozenset[str], tuple[ExtractionMethod, ...]]]:
    """The distinct values sources show for one field: (value, their channels, their methods), strongest first."""
    from backoffice.extraction.values import comparison_key, is_usable

    f = CriticalField(name)
    groups: dict[str, list[FieldObservation]] = {}
    for o in _confident(observations):
        if is_usable(f, o.value):
            groups.setdefault(comparison_key(f, o.value), []).append(o)
    return [(_option_value(f, found[0].value), frozenset(t for o in found for t in lineage(o)),
             tuple(dict.fromkeys(o.method for o in found))) for found in groups.values()]


def _value_from(name: str, observations: Sequence[FieldObservation], channels: frozenset[str]) -> Any:
    """What the chosen sources show for another disputed field, if they show it."""
    from backoffice.extraction.values import is_usable

    f = CriticalField(name)
    for o in _confident(observations):
        if lineage(o) & channels and is_usable(f, o.value):
            return _option_value(f, o.value)
    return None


def _settled_values(assessment: DocumentAssessment) -> dict[str, Any]:
    """Each field's value, None where the sources disagree (§19: never pick one)."""
    return {name: (a.value if a.quality is not Quality.RED else None) for name, a in assessment.fields.items()}


def _validation(name: str, assessment: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"field": name, "quality": assessment.quality.value}
    if assessment.supporting:
        out["sources"] = sorted({f"{o.method.value}@{o.source}" for o in assessment.supporting})
    return out


def _with_owner(observations: Mapping[str, Sequence[FieldObservation]],
                owner: Mapping[str, FieldObservation]) -> dict[str, list[FieldObservation]]:
    """Observations with the owner's confirmed values: for such a field, only readings that agree stay."""
    from backoffice.extraction.values import comparison_key

    out = {name: list(found) for name, found in observations.items()}
    for name, ruling in owner.items():
        f = CriticalField(name)
        key = comparison_key(f, ruling.value)
        out[name] = [o for o in out.get(name, []) if comparison_key(f, o.value) == key] + [ruling]
    return out


def _first_value(observations: Mapping[str, list[FieldObservation]], f: CriticalField) -> Any:
    found = observations.get(f.value) or []
    ranked = sorted(found, key=lambda o: -o.confidence)
    return ranked[0].value if ranked else None


def _same_number(a: str, b: str) -> bool:
    norm = lambda s: re.sub(r"[\s/_-]+", "", s).upper()  # noqa: E731
    return norm(a) == norm(b)


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _date(value: Any) -> date | None:
    return value if isinstance(value, date) and not isinstance(value, datetime) else None


def _dec(value: Any) -> Decimal | None:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return Decimal(value)
    return None


def _slug(text: str) -> str:
    from backoffice.learning import fold

    return re.sub(r"[^a-z0-9]+", "_", fold(text)).strip("_") or "item"


def _unique_id(existing: Mapping[str, Any], base: str) -> str:
    if base not in existing:
        return base
    n = 2
    while f"{base}_{n}" in existing:
        n += 1
    return f"{base}_{n}"


def _amount_in(text: str) -> Decimal | None:
    from backoffice.countries.pt import parse_pt_amount

    m = _AMOUNT_IN_QUESTION.search(text)
    if not m:
        return None
    raw = m.group(1) or m.group(2)
    if re.fullmatch(r"\d{1,3}(,\d{3})+(\.\d{2})?", raw):  # "1,200" / "1,200.00" (English grouping)
        return Decimal(raw.replace(",", ""))
    if re.fullmatch(r"\d+", raw):
        return Decimal(raw)
    value = parse_pt_amount(raw)
    if value is None:
        try:
            value = Decimal(raw)
        except InvalidOperation:
            return None
    return value


def _purpose(doc: DocumentRecord) -> str:
    kind = doc.document.doc_type
    if kind is DocumentType.INVOICE_RECEIPT and doc.document.vat_amount == 0:
        month = Month.of(doc.document.issue_date).name if doc.document.issue_date else "the month"
        return f"{month}'s rent, as its receipt shows"
    return f"for {doc.label.split(' · ')[0].lower()}"


def _option_alias(question: Question, option_id: str) -> str:
    """Accept the bare company id the web app may send ("hazel-tree" for "entity:hazel-tree")."""
    ids = {o.id for o in question.options}
    if option_id in ids:
        return option_id
    for candidate in (f"entity:{option_id}", option_id.replace("-", "_")):
        if candidate in ids:
            return candidate
    return option_id


def _auditable(value: Any) -> Any:
    """Plain JSON-safe values for the audit log (dates as ISO text, Decimal kept exact)."""
    if isinstance(value, Mapping):
        return {str(k): _auditable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_auditable(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat() if value.tzinfo else value.replace(tzinfo=timezone.utc).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if value is None or isinstance(value, (str, bool, int, Decimal)):
        return value
    if isinstance(value, float):
        return str(value)
    if hasattr(value, "value") and isinstance(getattr(value, "value"), str):
        return value.value
    return str(value)


def _parse_bank_csv(data: bytes) -> list[BankRow] | None:
    """A bank export: date,amount,counterparty,description,account[,card,iban,reference,kind]."""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or not {"date", "amount", "counterparty", "account"} <= {f.strip() for f in reader.fieldnames}:
        return None
    rows = []
    for i, raw in enumerate(reader):
        row = {k.strip(): (v or "").strip() for k, v in raw.items() if k}
        try:
            amount = Decimal(row["amount"])
            booked = date.fromisoformat(row["date"])
        except (InvalidOperation, ValueError):
            return None
        kind_text = row.get("kind") or ("card" if row.get("card") else "transfer_out" if amount < 0 else "transfer_in")
        try:
            kind = TransactionKind(kind_text)
        except ValueError:
            return None
        digest = hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest()[:16]
        rows.append(BankRow(
            bank_id=f"csv-{digest}-{i}", account_id=row["account"], booked_on=booked, amount=amount,
            counterparty=row["counterparty"], description=row.get("description", ""), kind=kind,
            card_last4=row.get("card") or None, counterparty_iban=row.get("iban") or None,
            reference=row.get("reference") or None,
        ))
    return rows


def local_datetime(day: date, hour: int = 9, minute: int = 0) -> datetime:
    """A time on ``day`` in the tenant's local time."""
    return datetime.combine(day, time(hour, minute), tzinfo=TZ)
