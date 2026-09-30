"""Deterministic orchestrator over named agents (§3, §46).

Never one giant agent. Each agent below is a small class that calls the real
modules of this package; the :class:`Orchestrator` decides, in a fixed order,
which agent runs on what. Nothing here guesses: every lifecycle transition
carries evidence ids (§3), and every step is written to the hash-chained audit
log (§55).

Pipeline for one piece of evidence::

    Discovery      what arrived (email, e-invoice, fiscal QR text, bank rows, letter)
    Retrieval      invoice links in emails and shared from the phone, followed through the repository's link
                   source (§9, §10): the demo's portal adapters, or on the production server what was fetched
                   before the event was recorded (server/links.py). Files and rendered pages are kept with their
                   provenance; a sign-in wall is the owner's to pass; an unsafe link is never opened; a link that
                   no longer works starts the missing-evidence path. An invoice written in the email itself (plain
                   text, HTML, schema.org data) is read as a document when nothing attached or linked gives one
    Document       structured extraction: UBL, Portuguese fiscal QR + text fields (§13, §19); uploaded
                   PDFs and photos through the repository's document reader (backoffice.reading:
                   text layer, QR, then the OCR chain), when one is configured (§13-17). The issuer's
                   country comes first: a document from abroad is read with its own labels (P7)
    Verification   field-level GREEN / AMBER / RED (§18, §57); a disagreement becomes one plain
                   question for the owner, whose answer is stored as evidence (§19, §37). A document
                   from abroad is checked by rules that hold anywhere, the bank confirming it (P7)
    Fraud          hard stops: changed IBAN, recipient mismatch, ... (§26)
    Entity         which company (§51), taught rules (§38); the billing name, billing address and receiving
                   mailbox are hints that never override a tax number (H2, H3, H5); a clearly personal shop on a
                   company card is one question (X23). A new business's first run asks only its few most
                   valuable questions once its history is in, and defers the rest (§5, backoffice.onboarding)
    Payroll        payslips prove salaries, paid to the employee's account or worded as a salary, only with
                   that month's payslip for that employee; a missing one is asked of whoever runs payroll (J3)
    Reconciliation expected evidence (§21, with what the owner or accountant taught, J7), then transaction <->
                   document matching (§20): only accounting
                   documents of the kind the payment needs prove it; a pro-forma, quote, delivery note,
                   order or supplier statement is kept as supporting evidence only; a credit note linked
                   to its invoice is netted against it
    Parts          invoices paid in parts (checklist X8, I2, I4, I5): a deposit (the bank line says so, or
                   pays an advance invoice) is held, never income, until the final invoice takes it off (its
                   amounts add up that way, or the owner confirms); several payments or a part payment are
                   kept on proof (the reference and the customer, or supplier) or the owner's tap; a part
                   held back by the customer stays owed until released; a deposit given back is linked to it
    Settlement     payouts from card terminals and payment / sales platforms: the provider's payout
                   report is read (CSV / JSON), must add up to the cent, and must equal the bank payout;
                   then gross sales, fees and refunds are known and the payout closes (§20, §21)
    Obligation     letters and messages become obligations (§24): tax and Social Security, bank and KYC
                   requests, official requests, filings, insurance / contract / licence renewals, rent and
                   debts, in Portuguese and English. Each is done only by its required proof: its payment
                   (a payment to someone the bank line does not identify is one question), or for the rest a
                   confirming letter or message that fits exactly one, or the owner's confirmation stored as
                   evidence. A renewal that happens on its own is information only; a letter naming none of
                   your companies is one question
    Missing        a plan for every payment still without its document; supplier chasing (§22); money back
                   without its credit note is asked for the same way; a supplier's usual invoice that is
                   overdue past its learned rhythm is raised once, asked for, and closed when it arrives (§23);
                   the invoice behind a link that no longer works is looked for, then asked for at once
    Refunds        a refund closes on the credit note it matches, and that credit note's invoice is shown with
                   it; another amount is one question; money back to a customer needs your own credit note
    Accountant     routine accountant questions answered from evidence (§28)
    Closure        lifecycle transitions and month status (§2, §27, §48); an invoice naming another of your
                   companies is a question (§51); a large first purchase needs the buyer's tax number and
                   totals that add up (§26); a receipt paid in cash closes on its own evidence (§11)
    Cost center    which job, property, vehicle, outlet, event, course or client each cost is for, with its
                   reason; exact splits; one question otherwise. Only for companies that keep cost centers
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
from dataclasses import dataclass, field, replace
from functools import lru_cache
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from backoffice.accountant_questions import PaymentFacts, amounts_in, answer_question, description_lines
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
    detect_confirmation,
    detect_obligation,
    evaluate_activation,
    normalize_reference,
    owner_touched,
    proof_for,
    satisfy,
)
from backoffice.closure.month import EXPECTED_INVOICE
from backoffice.closure.obligations import VerificationCondition, grant_agency, pack_vocabulary
from backoffice.fx_differences import record_exchange_differences
from backoffice.countries.foreign import IssuerProfile, detect_issuer, read_foreign_text
from backoffice.deposits import (
    customer_name,
    deposit_wording,
    is_advance_invoice,
    mentions_security_deposit,
    read_terms,
    says_held_back,
    security_deposit_wording,
)
from backoffice.countries.foreign import vat_rates as foreign_vat_rates
from backoffice.countries import (
    CompanyPack,
    CountryPackError,
    FiscalQRError,
    FiscalQRResult,
    company_countries,
    company_pack,
)
from backoffice.domain.cost_centers import (
    CostCenter,
    SplitError,
    split_by_amounts,
    split_by_percent,
    split_by_weights,
    to_cents,
)
from backoffice.domain.lifecycle import IllegalTransition, Stage, TrackedItem
from backoffice.domain.models import (
    METHOD_RANK,
    SUPPORTING_DOCUMENT_TYPES,
    AllocationMethod,
    AllocationShare,
    CostAllocation,
    CriticalField,
    Document,
    DocumentType,
    Evidence,
    EvidenceFormat,
    ExtractionMethod,
    FieldObservation,
    LegalEntity,
    Obligation,
    ObligationKind,
    Quality,
    SourceKind,
    Supplier,
    Transaction,
    TransactionKind,
    VatPart,
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
from backoffice.evidence.email import EmailParseError, parse_eml
from backoffice.evidence.html_signals import html_to_text
from backoffice.evidence.links import (
    BLOCKED_MESSAGE,
    FetchResult,
    LinkOutcome,
    authentication_message,
    register_fetch,
    sign_in_message,
)
from backoffice.evidence.retrieval import LinkSource, PortalLinks, email_invoice_links
from backoffice.extraction import StructuredFormatError, XMLSyntaxError, parse_einvoice
from backoffice.extraction.fields import StructuredDataError
from backoffice.extraction.htmldata import extract_html_structured
from backoffice.extraction.addressee import read_billing_party
from backoffice.extraction.invoicelines import InvoiceDetails, read_invoice_details
from backoffice.fraud import (
    ApproverKind,
    BeneficiaryChangeRefused,
    FraudAssessment,
    FraudCase,
    HardApproval,
    SignalKind,
    VerificationChannel,
    assess,
    email_domain,
    find_ibans,
    is_valid_iban,
    mask_iban,
    new_beneficiary_ibans,
    normalize_iban,
    registrable_domain,
    trust_iban,
)
from backoffice.fraud.engine import COUNTRY_NAMES
from backoffice.learning import (
    Addressee,
    Answer,
    Basis,
    CompanyDirectory,
    EntityAssignment,
    Occurrence,
    OptionKind,
    OwnershipBook,
    Question,
    RecurringSeries,
    RuleAuthor,
    RuleBook,
    RuleScope,
    assign_entity,
    candidates_from_assignments,
    check_overdue,
    compute_coverage,
    confirm_line,
    counterparty_key,
    coverage_items,
    day_month,
    detect_price_change,
    display_name,
    find_payment,
    format_money,
    learn_series,
    method_of,
    next_expected,
    join_and,
    match_key,
    normalize_tax_id,
    payment_method_note,
    personal_signal,
    qualified_tax_id,
    same_tax_id,
    select_questions,
    suggest_rule_from_answer,
)
from backoffice.learning import GENERAL, Rule, RuleField, RuleMatch, RuleOutcome, RuleSubject
from backoffice.learning.entity import HISTORY_MIN_COUNT
from backoffice.learning.cost_centers import (
    SPLIT_OPTION,
    CostCenterDecision,
    CostCenterFacts,
    cost_center_question,
    decide_cost_center,
    decide_recharge,
    history_counts,
    noun_for,
    percents_of,
    recharge_rule,
    suggest_cost_center_rule,
)
from backoffice.learning.plain import count_phrase
from backoffice.mailer import is_simulated
from backoffice import onboarding as ob_
from backoffice.onboarding import OnboardingState
from backoffice.payroll import Payslip, person_name, read_payslip, salary_proof
from backoffice.missing import (
    ChaseFacts,
    ChaseMessage,
    LinkChaseFacts,
    RecurringChaseFacts,
    StatementItem,
    activity_line,
    choose_language,
    clean_invoice_number,
    compose_correction_request,
    compose_link_request,
    compose_recurring_request,
    compose_request,
    recurring_activity_line,
    compose_statement_request,
    thread_token,
)
from backoffice.policy import ActionContext, ActionKind, Approval, Decision, TenantPolicy, authorize
from backoffice.policy.actions import Requirement
from backoffice.purchases import PURCHASE_INVOICE_TYPES, is_high_value
from backoffice.reconciliation import (
    EvidenceExpectation,
    ExpectationDecision,
    ExpectedEvidenceEngine,
    InMemoryExpectationOverrides,
    LearnedExpectation,
    Match,
    MatchKind,
    PayoutProvider,
    SupplierResolver,
    compatible_providers,
    fx_from_text,
    payout_provider,
    provider_named,
    reconcile,
)
from backoffice.reconciliation._text import fold as bank_fold
from backoffice.reconciliation._text import squash
from backoffice.reconciliation.engine import MatchTag
from backoffice.reconciliation.scoring import document_flow, identifier_in
from backoffice.settlements import (
    PayoutCandidate,
    PayoutDecision,
    PayoutOutcome,
    SettlementReport,
    SettlementReportError,
    likely_payouts,
    match_payouts,
    parse_settlement_reports,
    provider_label,
)
from backoffice.supplier_statements import CREDIT_NOTE as STATEMENT_CREDIT_NOTE
from backoffice.supplier_statements import DEBIT_NOTE as STATEMENT_DEBIT_NOTE
from backoffice.supplier_statements import INVOICE as STATEMENT_INVOICE
from backoffice.supplier_statements import (
    OurDocument,
    OurPayment,
    RowCheck,
    StatementCheck,
    SupplierStatement,
    check_statement,
    read_statement,
)
from backoffice.verification import (
    BankCharge,
    DocumentAssessment,
    ForeignRules,
    assess_document,
    assess_foreign,
    check_sum,
    currency_mark,
    lineage,
)
from backoffice.verification.foreign import PAYMENT_LAG_DAYS, PAYMENT_LEAD_DAYS
from backoffice.verification._display import field_label, join, method_label, show_many
from backoffice.verification.duplicates import same_document_kind

__all__ = [
    "SYSTEM",
    "TZ",
    "Account",
    "AccountantProfile",
    "AccountantQuestion",
    "ActivityEntry",
    "AnswerOutcome",
    "BankRow",
    "BrokenLinkRecord",
    "ChaseRecord",
    "CheckOption",
    "Clock",
    "ConnectorState",
    "DepositRecord",
    "DocumentRecord",
    "ExpectedInvoiceRecord",
    "IngestReport",
    "LinkRecord",
    "MemoryObjectStore",
    "NeedsYouRecord",
    "ObligationRecord",
    "Orchestrator",
    "OutgoingMessage",
    "OwnerProfile",
    "PortalDocument",
    "Repository",
    "RetentionRecord",
    "RunReport",
    "SettlementRecord",
    "TxRecord",
]

# Lisbon in summer time (WEST). A fixed offset keeps the browser build free of
# time-zone databases; the demo world lives entirely in September/October.
TZ = timezone(timedelta(hours=1), "WEST")
SYSTEM = "system"
OWNER_ACTOR = "owner"
CHASE_AFTER_DAYS = 3  # a payment this old without its invoice is worth a polite request (§22, §23)
LINK_GIVE_UP_AFTER = timedelta(days=3)  # an invoice link whose site has not answered for this long: another way
ANSWER_SECONDS = 40  # owner time recorded for one tap on a Needs-You item (§59)
MESSAGE_ID_DOMAIN = "backoffice.example"  # right-hand side of the Message-IDs of the emails we write
# Emails the back office writes on its own, and the permission each needs (§25). It is checked again when the
# email is sent: switched off in the owner's settings meanwhile, what was written but not sent is held back.
GATED_MESSAGES: Mapping[str, ActionKind] = {
    "supplier_request": ActionKind.SUPPLIER_INVOICE_REQUEST,
    "link_request": ActionKind.SUPPLIER_INVOICE_REQUEST,
    "expected_invoice_request": ActionKind.SUPPLIER_INVOICE_REQUEST,
    "statement_request": ActionKind.SUPPLIER_INVOICE_REQUEST,
    "accountant_answer": ActionKind.ROUTINE_ACCOUNTANT_RESPONSE,
    "payslip_request": ActionKind.ROUTINE_ACCOUNTANT_RESPONSE,
    "accountant_package": ActionKind.DOCUMENT_DELIVERY,
}

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
    # The cardholder's name when the bank's card details give it (employee cards, backoffice.staff).
    cardholder: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.amount, float) or not isinstance(self.amount, Decimal):
            raise TypeError("money must be Decimal, never float")

    def to_json(self) -> dict[str, Any]:
        out = {
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
        if self.cardholder:  # only when the bank named one: every other row keeps its evidence id
            out["cardholder"] = self.cardholder
        return out


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
    # Held for bank details that arrived after its payment was already proven and closed (a later copy that adds
    # or changes them, checklist Q4): the new account is blocked until the owner verifies it, but the payment made
    # before it appeared stays proven (the auditor does not reopen it).
    late_bank_hold: bool = False
    matched_tx_ids: list[str] = field(default_factory=list)
    retrieved: bool = False  # fetched by the system from a link or portal (§9)
    # Field-level verification (§18): every critical field's value, quality, reasons and the
    # observations behind it (each with value, source, method, confidence and location).
    checks: dict[str, VerifiedField] = field(default_factory=dict)
    # Values the owner confirmed when the sources disagreed (§19): method HUMAN, source = the answer.
    owner_values: dict[str, FieldObservation] = field(default_factory=dict)
    sales: bool = False  # issued by one of the business's own companies: money in, not a purchase
    paid_in_cash: bool = False  # the document itself says it was paid in cash: no bank line to wait for
    referenced_number: str | None = None  # a credit note: the invoice number it says it corrects
    credit_for: str | None = None  # a credit note linked to that invoice (document id)
    supports_tx_ids: list[str] = field(default_factory=list)  # supporting evidence for these payments, never proof
    text: str = ""  # the document's readable text (for wording checks such as equipment)
    owner_confirmed: str | None = None  # evidence id of the owner's answer confirming a cash receipt
    hold_reason: str = ""  # plain reason it waits although nothing is missing (e.g. a large first purchase)
    recipients: tuple[str, ...] = ()  # addresses the email was sent to (an alias can point to a cost center)
    # A receipt an employee paid with their own money: the expense claim it belongs to (backoffice.staff).
    # It never proves a company payment; the transfer that pays the employee back closes it.
    claim_id: str | None = None
    # Who issued it and from which country (checklist P7): a foreign issuer is checked by rules that
    # hold anywhere, never by the company's own country's. None, or a domestic issuer: the home rules.
    issuer: IssuerProfile | None = None
    # The country of the company it is for (§49): its pack read it and checks its VAT.
    country: str = "PT"
    # Deposits, staged payments and amounts held back (checklist X8, I2, I4, I5).
    advance: bool = False  # an advance or deposit invoice ("Fatura de adiantamento")
    terms: Any = None  # deposits.Terms: the deposits it takes off, the amount due now, the part held back
    customer: str | None = None  # the customer printed on your own invoice ("Cliente: ...")
    part_paid: dict[str, Decimal] = field(default_factory=dict)  # payment id -> the part of that payment paying this
    netted: dict[str, Decimal] = field(default_factory=dict)  # advance invoice id -> the amount it takes off
    linked_advances: list[str] = field(default_factory=list)  # advance invoices it names (taken off or already inside)
    applied: dict[int, str] = field(default_factory=dict)  # deposit it states (index) -> the payment or advance invoice
    part_answers: list[str] = field(default_factory=list)  # the owner's answers linking parts to it (evidence ids)
    # Who it is made out to besides the tax number: the billing name and address printed on it (H2, H3).
    billing_name: str | None = None
    billing_address: str | None = None
    payslip: Payslip | None = None  # a payslip: the salary it proves (checklist J3)
    # Read and matched by its own agent, never by amount in the general matching: "lease" or "renting" (a leasing
    # or renting contract, backoffice.leasing), "till" (a day of a till report, backoffice.cashbook), "member" (a
    # receipt from a receipts list, backoffice.members).
    book: str = ""
    # Sensitive (checklist X32): "medical", "legal", "hr" (its wording or kind) or "owner" (the owner said so).
    # Never sent to an external AI, never shown to employees or outlet managers, every read of it logged.
    sensitive: str | None = None
    sensitive_by: str = ""  # "wording" | "owner"
    # The outlet (cost center) a manager sent it for (their own upload is theirs to see).
    uploaded_for: str | None = None

    @property
    def id(self) -> str:
        return self.document.id

    @property
    def is_foreign(self) -> bool:
        return self.issuer is not None and self.issuer.is_foreign

    @property
    def supporting(self) -> bool:
        """Not an accounting document (a pro-forma, quote, delivery note...): never closes anything (§3)."""
        return self.document.doc_type in SUPPORTING_DOCUMENT_TYPES

    @property
    def label(self) -> str:
        doc = self.document
        kind = _DOC_LABELS.get(doc.doc_type, "Document")
        number = f" {doc.invoice_number}" if doc.invoice_number else ""
        if doc.doc_type is DocumentType.PAYOUT_REPORT:  # "Payout report of 18 September": the date, not the provider's id
            day = doc.issue_date
            number = f" of {day.day} {_MONTH_NAMES[day.month - 1]}" if day else ""
        elif self.book == "till":  # "Till report of 17 September · €1,650.40"
            kind, day = "Till report", doc.issue_date
            number = f" of {day.day} {_MONTH_NAMES[day.month - 1]}" if day else ""
        elif self.book in ("lease", "renting"):
            kind = "Leasing contract" if self.book == "lease" else "Renting contract"
        amount = f" · {format_money(doc.gross_amount, doc.currency)}" if doc.gross_amount is not None else ""
        return f"{kind}{number}{amount}"


@dataclass(frozen=True)
class AccessEntry:
    """One read of a sensitive document's original (§52): who, when, which document, and how."""

    at: datetime
    document_id: str
    who: str  # an email address, "owner" in the demo, or "accounting software (API key)"
    role: str  # "owner" | "accountant" | "admin" | "api"
    how: str = "opened the original"


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
    # Why ``proof_evidence_ids`` prove it, in plain words, when it is not a tax letter ("The grant letter's ...",
    # a leasing contract and statement).
    proof_note: str = ""
    missing_since: date | None = None
    # Documents kept with the payment that do not prove it (a pro-forma, a receipt where an invoice is needed).
    supporting_document_ids: list[str] = field(default_factory=list)
    hold_reason: str = ""  # plain reason a matched payment is not closed yet
    company_answer_ev: str | None = None  # the owner's answer on which company carries it (evidence id)
    # (paid by, carried by, company the invoice names) when the owner decided between two of their companies.
    company_note: tuple[str, str, str] | None = None
    # A refund the owner said is part of a credit note for more (evidence id of that answer).
    refund_answer_ev: str | None = None
    # Credit notes the owner said it is not for; also invoices it is not part of, and deposits it does not give back.
    not_for_document_ids: list[str] = field(default_factory=list)
    part_answer_ev: str | None = None  # the owner said this payment is part of an invoice (evidence id)
    deposit_refund_of: str | None = None  # the deposit this payment gave back (that deposit's payment id)
    cardholder: str | None = None  # the cardholder the bank's card details name (backoffice.staff)
    claim_ids: list[str] = field(default_factory=list)  # expense claims this transfer pays back (backoffice.staff)
    notes: list[str] = field(default_factory=list)  # plain facts worth knowing, never a hold ("paid from ...")
    # More evidence its closing carries: the owner's answers and earlier bank lines that explain it (e.g. a
    # direct debit that came back before this payment of the same period, backoffice.members).
    extra_evidence_ids: list[str] = field(default_factory=list)

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
    kind: str  # "choice" | "approval" | "check" (a document whose sources disagree) | "cost_center" (which job)
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
    tag: str = ""  # "staff": about an employee's receipt or expense claim (backoffice.staff)


@dataclass
class ObligationRecord:
    """One obligation from a letter or message (§24), with who wrote it and how it was done.

    It is done only by its required proof (``proof``): a payment for what is to be paid; for the
    rest a confirming letter or message, or the owner's explicit confirmation, stored as evidence.
    A renewal the letter says happens on its own is ``informational``: shown, never blocking.
    """

    obligation: Obligation
    evidence_id: str
    title: str
    reasons: tuple[str, ...]
    reference: str | None
    satisfied_by: tuple[str, ...] = ()
    issuer: str = ""  # Issuer value: tax_authority, social_security, bank, landlord, insurer, other
    received_on: date | None = None
    sender: str = ""  # "Name <address>" when it came by email
    payee_supplier_id: str | None = None  # a known supplier the letter names (tax number or email domain)
    payee_ibans: tuple[str, ...] = ()  # bank accounts printed in the letter
    payee_key: str | None = None  # the sender's name as a bank line shows it
    informational: bool = False  # renews on its own: nothing to do unless the owner wants a change
    confirmed_by: tuple[str, ...] = ()  # the owner's confirmation (and any file with it), as evidence
    declined_tx_ids: list[str] = field(default_factory=list)  # payments the owner said do not pay it
    how: str = ""  # plain words once done: "Paid on 2 November." / "Renewed on 20 October."
    agency: str | None = None  # a grant letter: the agency it names ("IFAP"), as a bank line would name it too

    @property
    def proof(self) -> ProofKind:
        return proof_for(self.obligation.kind)

    @property
    def payable(self) -> bool:
        return self.proof is ProofKind.PAYMENT

    @property
    def done(self) -> bool:
        return bool(self.satisfied_by)

    @property
    def sender_domain(self) -> str | None:
        host = email_domain(self.sender) if "@" in self.sender else None
        return registrable_domain(host) if host else None


@dataclass
class PendingObligation:
    """A letter with a deadline that names none of the owner's companies (§24, §51).

    It becomes an obligation only when the owner says which company it is for; nothing is tracked
    against a company on a guess. ``status``: "open" (asked), "recorded" or "declined" (not theirs).
    """

    id: str
    finding: Any  # closure.ObligationFinding
    evidence_id: str
    received_on: date
    sender: str
    text: str
    status: str = "open"


@dataclass
class ExpectedInvoiceRecord:
    """A supplier's usual invoice that is overdue (§23).

    Raised once per period, from a learned invoice rhythm; the supplier is asked for it
    when the policy allows (§22, §25). It closes only when an invoice from that supplier
    for that company arrives (the invoice is the evidence), or when the owner says it is
    not coming (their answer is the evidence).
    """

    id: str
    series_key: str
    supplier_id: str | None
    supplier_name: str
    company_id: str
    expected_on: date  # the period's usual date
    due_on: date  # it normally arrives by this day
    earliest: date  # an invoice from this day on covers the period
    notice: str  # as said when raised: "Vodafone normally issues an invoice by the 26th. Today is the 29th. ..."
    raised_at: datetime
    item_id: str
    series: RecurringSeries
    arrivals: tuple[date, ...]  # the invoice dates the rhythm was learned from
    learned_from: tuple[str, ...]  # their document ids
    searched: tuple[str, ...] = ()  # where I looked, in plain words
    status: str = "missing"  # "missing" | "received" | "not_coming"
    document_id: str | None = None  # the invoice that arrived
    message: ChaseMessage | None = None  # the request to the supplier, once written
    line: str = ""  # the Activity line once it is sent
    outbox_id: str = ""
    written_at: datetime | None = None
    sent_at: datetime | None = None

    @property
    def period(self) -> Month:
        return Month.of(self.expected_on)

    @property
    def sent(self) -> bool:
        return self.sent_at is not None


@dataclass
class SettlementRecord:
    """A payout report (a provider's settlement statement) and what it proves (§20, §21).

    ``status``: ``waiting`` (for its payout in the bank), ``settled`` (the bank payout
    ``transaction_id`` matched it to the cent), ``does_not_add_up`` (its own figures
    disagree: a conflict question), ``conflict`` (it disagrees with the bank payout:
    a question), ``set_aside`` (the owner is getting a corrected one) or ``replaced``
    (a corrected report for the same payout settled it).
    """

    document_id: str
    report: SettlementReport
    status: str = "waiting"
    transaction_id: str | None = None  # the bank payout it settled, or is (probably) about
    headline: str = ""
    problem: str = ""  # "does_not_add_up" | "bank_differs": why it cannot prove its payout
    commission_document_ids: list[str] = field(default_factory=list)  # the provider's invoices for these fees

    @property
    def settled(self) -> bool:
        return self.status == "settled"


@dataclass
class DepositRecord:
    """Money paid ahead of the work (checklist X8, I4): a customer's deposit, retainer or advance, or one you
    paid a supplier before its final invoice.

    Recorded from the bank line that says so (or names a quote or contract), or from the advance invoice it
    paid. On its own it is neither income nor a finished purchase: the final invoice takes it off
    (``applied``), or it goes back when a booking is cancelled (``refunded``). ``refunds``: the payments that
    gave it back, with how much each gave back.
    """

    tx_id: str
    company_id: str
    direction: str  # "in": a customer paid it to you; "out": you paid it to a supplier
    party: str  # the customer's or supplier's name, as the owner reads it
    amount: Decimal
    currency: str
    received_on: date
    why: str  # why it is a deposit, in plain words
    reference: str | None = None  # the quote or contract the bank line names
    advance_document_id: str | None = None  # the advance invoice it paid
    status: str = "held"  # "held" | "applied" | "refunded"
    applied_to: str | None = None  # the final invoice that takes it off (document id)
    refunds: dict[str, Decimal] = field(default_factory=dict)
    not_for_document_ids: list[str] = field(default_factory=list)  # invoices the owner said it is not part of
    # A refundable security deposit (checklist X9): held for the customer, never income for the work. It closes
    # when it goes back; a part kept (damage, charges) becomes income only with an invoice for it or the owner's
    # confirmation (``kept`` and ``kept_answer``, status "kept").
    security: bool = False
    kept: Decimal = _ZERO
    kept_answer: str | None = None  # evidence id of the owner's answer that they kept it
    kept_later_at: Decimal | None = None  # what had gone back when the owner said the rest goes back later

    @property
    def returned(self) -> Decimal:
        return sum(self.refunds.values(), _ZERO)

    @property
    def available(self) -> Decimal:
        """What of it can still be taken off an invoice (or is still held): all of it, less what went back or
        was kept."""
        return self.amount - self.returned - self.kept

    @property
    def noun(self) -> str:
        return "security deposit" if self.security else "deposit"


@dataclass
class RetentionRecord:
    """The part of an invoice held back until the work is accepted (checklist X8): still owed, due when released.

    Never a missing payment: the invoice closes for the part paid, and this stays open on its own, visible to
    the owner, until the money held back arrives (``released``).
    """

    document_id: str
    direction: str  # "in": the customer holds it back from you; "out": you hold it back from a supplier
    party: str
    amount: Decimal
    currency: str
    until: date | None = None
    percent: Decimal | None = None
    status: str = "held"  # "held" | "released"
    released_tx_ids: list[str] = field(default_factory=list)


@dataclass
class ChaseRecord:
    """A request to a supplier for a missing invoice (§22).

    Written when the policy allows it; it counts as asked only once a transport
    accepted it (``sent_at``). Until then every screen says it is waiting to be sent.
    """

    tx_id: str
    supplier_id: str
    company_id: str
    message: ChaseMessage
    written_at: datetime
    line: str  # the Activity line once it is sent
    outbox_id: str = ""
    sent_at: datetime | None = None
    link_id: str | None = None  # asked because the invoice link in the supplier's email no longer works

    @property
    def sent(self) -> bool:
        return self.sent_at is not None


@dataclass
class LinkRecord:
    """One invoice link from an email or shared from the phone, and what following it gave (§9).

    ``status``: "waiting" (not opened yet, or the site did not answer: tried again later), "retrieved"
    (the file behind it, or the page itself, is stored as evidence with its provenance), "sign_in" (the
    supplier's site asks the owner to sign in or for a code), "blocked" (it did not look safe: never
    opened) or "broken" (it no longer works: the invoice is looked for and asked for another way, §22).
    """

    url: str
    status: str
    first_seen: datetime
    email_evidence_id: str | None = None  # the email it came in; None when it was shared from the phone
    shared: bool = False
    supplier_id: str | None = None
    supplier_name: str = ""
    evidence_ids: tuple[str, ...] = ()  # what it gave that is read as documents
    snapshot_ids: tuple[str, ...] = ()  # screenshots of rendered pages (kept, never read)
    message: str = ""  # what the owner is told, in plain words
    reason: str = ""  # internal code from the fetch ("http_404", "login_required", ...), never shown
    tries: int = 0


@dataclass
class BrokenLinkRecord:
    """An invoice link that no longer works (§9 → §22): the invoice is looked for, then asked for.

    Closed only by the invoice arriving, never by the request; the request counts as asked only once a
    transport accepted it (like every email the back office writes).
    """

    id: str
    url: str
    supplier_id: str | None
    supplier_name: str
    since: date  # the day the email (or the shared link) arrived
    at: datetime
    email_evidence_id: str | None = None
    shared: bool = False
    status: str = "missing"  # "missing" | "requested" | "received"
    tx_id: str | None = None  # the payment the request asks about, when there is one
    company_id: str | None = None
    message: ChaseMessage | None = None  # a request not tied to a payment (none was found)
    outbox_id: str = ""
    written_at: datetime | None = None
    sent_at: datetime | None = None
    document_id: str | None = None  # the invoice that arrived since
    searched: tuple[str, ...] = ()  # where I looked, in plain words
    note: str = ""  # why nothing was asked yet, in plain words

    @property
    def where(self) -> str:
        return "The link you shared" if self.shared else f"The link in {_possessive(self.supplier_name)} email"

    @property
    def asked_line(self) -> str:
        """'The link in Vodafone's email no longer works, so I asked Vodafone for the invoice.'"""
        return f"{self.where} no longer works, so I asked {self.supplier_name} for the invoice."

    @property
    def waiting_line(self) -> str:
        return (f"{self.where} no longer works, so I wrote to {self.supplier_name} asking for the invoice. "
                "It is waiting to be sent.")


@dataclass
class OutgoingMessage:
    """An email the back office writes itself: a supplier request or an answer to the accountant.

    ``status`` is "waiting" until a transport (backoffice.mailer) accepted it, then "sent".
    """

    id: str
    kind: str  # "supplier_request" | "correction_request" | "accountant_answer"
    subject_id: str  # the payment, the held document or the accountant's question
    company_id: str | None
    to: str
    subject: str
    body: str
    written_at: datetime
    headers: tuple[tuple[str, str], ...] = ()
    status: str = "waiting"  # "waiting" -> "sent"
    sent_at: datetime | None = None
    announced: bool = False  # the owner was told it is waiting to be sent
    failures: int = 0
    # Attachments (file name, content type, bytes), e.g. the monthly accountant package. The bytes are let go
    # once a transport accepted the email; ``attached`` keeps the file names.
    files: tuple[tuple[str, str, bytes], ...] = field(default=(), repr=False)
    attached: tuple[str, ...] = ()
    cc: tuple[str, ...] = ()  # further recipients of the same email (e.g. the owner's copy of the package)

    @property
    def sent(self) -> bool:
        return self.status == "sent"


@dataclass
class StatementRecord:
    """A supplier's account statement read line by line, and what checking it against the records found.

    ``check`` is worked out again on every run, so a document that arrives later matches its line. The
    request to the supplier for the documents it lists that the business never received is written once;
    like every email the back office writes, it counts as asked only once a transport accepted it.
    """

    document_id: str
    statement: SupplierStatement
    check: StatementCheck | None = None
    supplier_id: str | None = None
    company_id: str | None = None
    request_id: str = ""  # outbox id of the request for the documents it lists that the business doesn't have
    requested: tuple[str, ...] = ()  # the lines that request asked for
    request_note: str = ""  # why no request was written, in plain words
    correction_id: str = ""  # outbox id of the request for corrected documents (after the owner's answer)
    needs_id: str | None = None  # the one question about amounts that differ
    answer: str | None = None  # "document" | "statement": the owner's answer about those amounts
    answered: tuple[str, ...] = ()  # the lines that answer covers
    detected: list[str] = field(default_factory=list)  # lines recorded as missing documents
    retrieved: list[str] = field(default_factory=list)  # ... that arrived since


@dataclass
class AccountantQuestion:
    id: str
    company_id: str
    text: str
    evidence_id: str
    asked_at: datetime
    # "waiting": open (for the owner unless I can answer it from evidence); "written": my answer is
    # written and waiting to be sent; "answered": the answer reached the accountant's mailbox.
    status: str = "waiting"
    answer: str | None = None
    answer_evidence_ids: tuple[str, ...] = ()
    subject: str = ""  # of the accountant's email, to reply in the same thread
    message_id: str | None = None
    period: Month | None = None  # the month of the payment the answer is about
    outbox_id: str = ""
    accountant_id: str = ""  # who asked (the company's accountant, §28)


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
    needs_ids: list[str] = field(default_factory=list)  # questions for the owner this arrival raised (Needs You)
    pending_links: list[str] = field(default_factory=list)
    stored_only: bool = False
    already_known: bool = False  # every document in it was already on file
    confirmed_ids: list[str] = field(default_factory=list)  # obligations this letter proved done


@dataclass
class RunReport:
    transitions: int = 0
    passes: int = 0
    reopened: list[str] = field(default_factory=list)
    chased: list[str] = field(default_factory=list)  # payments whose supplier request was written in this run
    sent: list[str] = field(default_factory=list)  # outgoing messages a transport accepted in this run
    expected: list[str] = field(default_factory=list)  # usual invoices found overdue in this run (§23)
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
    DocumentType.PRO_FORMA: "Pro-forma",
    DocumentType.QUOTE: "Quote",
    DocumentType.DELIVERY_NOTE: "Delivery note",
    DocumentType.ORDER_CONFIRMATION: "Order confirmation",
    DocumentType.SUPPLIER_STATEMENT: "Account statement",
    DocumentType.PAYOUT_REPORT: "Payout report",
    DocumentType.PAYROLL: "Payslip",
}
# Documents a purchase paid in cash can be evidenced by (§11: the owner photographs the receipt).
_CASH_DOCUMENTS = frozenset({DocumentType.RECEIPT, *PURCHASE_INVOICE_TYPES})
# Questions about deposits, part payments and deposits given back (checklist X8), answered by the staged agent.
_STAGED_QUESTIONS = ("part", "deposit", "deposit_refund", "deposit_kept")
# Bank lines proven by something other than an invoice: a grant by its letter (X30), a chargeback by the sale
# it takes back or the chargeback it reverses (I7). Never matched to documents.
_OWN_PROOF_RULES = frozenset({"grant", "chargeback", "chargeback_won"})
# Documents paid after they are issued, so a due date on them can pass unpaid (F8). An invoice-receipt or a
# simplified invoice from a till is paid when it is issued.
_PAYABLE_LATER = frozenset({DocumentType.INVOICE, DocumentType.DEBIT_NOTE})


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
        # The business's accountant (the default), and companies that have their own (§28, §51).
        self.accountant: AccountantProfile | None = None
        self.company_accountants: dict[str, AccountantProfile] = {}
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
        self.pending_obligations: dict[str, PendingObligation] = {}  # letters waiting for "which company?"
        # A supplier's usual invoice that is overdue (§23), keyed by its own id; tracked as an item too.
        self.expected_invoices: dict[str, ExpectedInvoiceRecord] = {}
        self.settlements: dict[str, SettlementRecord] = {}  # payout reports, keyed by their document id
        # Money paid ahead of the work, keyed by its payment id; parts of invoices held back, by the invoice id.
        self.deposits: dict[str, DepositRecord] = {}
        self.retentions: dict[str, RetentionRecord] = {}
        self.statements: dict[str, StatementRecord] = {}  # suppliers' account statements, by their document id
        self.chases: dict[str, ChaseRecord] = {}
        # Emails the back office wrote itself, in the order written; each is "sent" only once a transport took it.
        self.outbox: dict[str, OutgoingMessage] = {}
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
        # What is behind an invoice link (§9, §10): the demo's portal adapters, or on the production server
        # what was fetched before the event was recorded (server/links.py). Never the network directly.
        self.links: LinkSource = PortalLinks(self.portal, clock=self.clock.now)
        self.links_seen: dict[str, LinkRecord] = {}  # every invoice link followed, by URL
        self.broken_links: dict[str, BrokenLinkRecord] = {}  # links that no longer work, by their own id
        self.closed_months: dict[tuple[str, str], date] = {}
        self.recovered_tx_ids: set[str] = set()
        # Reads uploaded PDFs and photos (backoffice.reading.DocumentReader, set by the server from its
        # environment). None in the browser demo: such files are stored and wait, unread.
        self.reader: Any = None
        self.reads: dict[str, Any] = {}  # evidence id -> ReadOutcome (what was read, by which steps)
        # Jobs, properties, vehicles, outlets, events, courses or clients of each company (cost centers).
        self.cost_centers: dict[str, CostCenter] = {}
        # Employees who hold company cards or pay expenses themselves (backoffice.staff): by their own id;
        # receipts asked from a cardholder, by payment id; expense claims to pay back, by their own id.
        self.employees: dict[str, Any] = {}
        self.receipt_requests: dict[str, Any] = {}
        self.expense_claims: dict[str, Any] = {}
        # A new business's first run and onboarding (§5, §58, §59; backoffice.onboarding). Empty for a business
        # set up by hand, such as the demo: nothing is measured for it.
        self.onboarding = OnboardingState()
        # What the companies are known by besides their tax numbers (H2, H3, H5), and their line of business.
        self.company_addresses: dict[str, list[str]] = {}
        self.company_sectors: dict[str, str] = {}
        self.mailboxes: dict[str, str] = {}  # a mailbox or alias -> the company the owner said it belongs to
        # Learned expected evidence (§21, J7): the owner's or accountant's answer ("this never has an invoice"),
        # used for every later payment to that counterparty. Company-limited accountant rules keep their own.
        self.expectation_overrides = InMemoryExpectationOverrides()
        self.company_expectation_overrides: dict[str, InMemoryExpectationOverrides] = {}
        # Payroll (J3): employees known from their payslips (IBAN -> name, payroll_employees), who runs payroll for a company
        # ("owner" | "accountant"), and the requests written for missing payslips (payment id -> outbox id).
        self.payroll_employees: dict[str, str] = {}
        self.payroll_by: dict[str, str] = {}
        # Every read of a sensitive document's original (who, when, which document), for the owner (§52).
        self.document_access: list[AccessEntry] = []
        self.payslip_requests: dict[str, str] = {}
        # Disputed card payments on the bank statement, by their payment id (backoffice.chargebacks, I7).
        self.chargebacks: dict[str, Any] = {}
        # Reference exchange rates (reconciliation.FxRateSource) for exchange differences (backoffice.fx_differences,
        # I11): set by the server from its configuration; None in the browser demo, where nothing is fetched and no
        # difference is ever guessed. The differences found, by the document they are about.
        self.fx_rates: Any = None
        self.fx_differences: dict[str, Any] = {}
        # Leasing and renting contracts by their document id, and the payments of their plans (payment id ->
        # (contract document id, line of the plan)) (checklist X24, backoffice.leasing).
        self.leases: dict[str, Any] = {}
        self.lease_payments: dict[str, tuple[str, int]] = {}
        # Days of till reports by their document id, and the owner's counts of the cash box (checklist X4, X5,
        # backoffice.cashbook).
        self.till_days: dict[str, Any] = {}
        self.cash_counts: list[Any] = []
        # Receipts from receipts lists by their document id; direct debits that came back: the bank line giving
        # the money back -> the payment it returns, and the other way round (checklist X12, backoffice.members).
        self.member_receipts: dict[str, Any] = {}
        self.payment_returns: dict[str, str] = {}
        self.returned_payments: dict[str, str] = {}
        # The monthly accountant package (backoffice.package_delivery): the owner's report settings as last saved
        # (None: the defaults), and every package written, by its own id.
        self.package_settings: dict[str, Any] | None = None
        self.packages: dict[str, Any] = {}
        # Photos from the phone (backoffice.captures): the pages of multi-page scans, by capture id, and documents
        # read without their number that look like one on file, waiting for the owner, by question id.
        self.captures: dict[str, Any] = {}
        self.pending_copies: dict[str, Any] = {}

    # ----------------------------------------------------------------- set-up

    def add_company(self, *, id: str, name: str, legal_name: str, tax_id: str, ibans: Sequence[str] = (),
                    address: str | None = None, sector: str | None = None, country: str = "PT") -> LegalEntity:
        """One of the owner's companies, in its own country (§49): every document, VAT amount and letter of
        the company is read and checked by that country's pack. Raises UnknownCountryError for a country
        without a pack that can run a company."""
        code = company_pack(country).country_code
        entity = LegalEntity(id=id, tenant_id=self.tenant_id, name=name, country=code, tax_id=tax_id,
                             own_ibans=list(ibans))
        self.companies[id] = entity
        self.legal_names[id] = legal_name
        if address and address.strip():
            self.company_addresses[id] = [" ".join(address.split())]
        if sector and sector.strip():
            self.company_sectors[id] = " ".join(sector.split())
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

    def company_country(self, company_id: str | None) -> str:
        """The company's country (its pack reads its documents); the business's first one when unknown."""
        entity = self.companies.get(company_id or "")
        return entity.country if entity is not None else self.primary_country()

    def primary_country(self) -> str:
        """The country of the business's first company (Portugal before any company is added)."""
        return next((e.country for e in self.companies.values()), "PT")

    def countries(self) -> tuple[str, ...]:
        """The countries of the business's companies, first company's first (Portugal when there is none)."""
        return tuple(dict.fromkeys(e.country for e in self.companies.values())) or ("PT",)

    def pack_for(self, company_id: str | None) -> CompanyPack:
        return company_pack(self.company_country(company_id))

    def account_countries(self) -> dict[str, str]:
        """Each bank account's (or card's) company's country: its pack's wording reads the account's lines (§49)."""
        return {a.id: self.company_country(a.holder_id) for a in self.accounts.values()}

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

    def company_for_tax_id(self, tax_id: str | None) -> str | None:
        """Which of the business's own companies has this tax number, if any."""
        if not tax_id:
            return None
        return next((e.id for e in self.entities if same_tax_id(e.tax_id, tax_id)), None)

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
        if item.subject_type == EXPECTED_INVOICE:
            expected = self.expected_invoices.get(item.subject_id)
            return expected.period if expected else None
        return None

    def item_company(self, item: TrackedItem) -> str | None:
        if item.subject_type == "transaction":
            rec = self.transactions.get(item.subject_id)
            return rec.company_id if rec else None
        if item.subject_type == EXPECTED_INVOICE:
            expected = self.expected_invoices.get(item.subject_id)
            return expected.company_id if expected else None
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

    # ----------------------------------------------------------------- accountants (§28, §51)

    def accountant_for(self, company_id: str | None) -> AccountantProfile | None:
        """The accountant of ``company_id``: its own, else the business's (the default)."""
        return self.company_accountants.get(company_id or "") or self.accountant

    def accountant_companies(self, email: str) -> list[str]:
        """Companies whose accountant (own or default) has this email address, in order."""
        wanted = (email or "").strip().lower()
        return [c for c in self.companies if (a := self.accountant_for(c)) is not None and a.email.lower() == wanted]

    def accountants(self) -> list[AccountantProfile]:
        """Every accountant who serves at least one company (the default first), one per email address."""
        out: dict[str, AccountantProfile] = {}
        for company_id in self.companies:
            a = self.accountant_for(company_id)
            if a is not None:
                out.setdefault(a.email.lower(), a)
        if self.accountant is not None and self.accountant.email.lower() in out:
            out = {self.accountant.email.lower(): self.accountant, **out}
        return list(out.values())

    def accountant_by_email(self, email: str | None) -> AccountantProfile | None:
        """The accountant with this address, when they serve at least one company."""
        wanted = (email or "").strip().lower()
        return next((a for a in self.accountants() if a.email.lower() == wanted), None) if wanted else None

    def accountant_ids_for(self, company_id: str | None) -> list[str]:
        """Accountants whose rules may be used for ``company_id``'s payments (§28: only their own clients)."""
        a = self.accountant_for(company_id)
        return [a.id] if a is not None else []

    def open_needs(self) -> list[NeedsYouRecord]:
        return sorted((n for n in self.needs.values() if n.status == "open"), key=lambda n: (n.created_at, n.id))

    # ----------------------------------------------------------------- what the companies are known by (H2-H5)

    def shared_mailboxes(self) -> set[str]:
        """Connected mailboxes that read for more than one company: never a sign of one company."""
        return {c.account.strip().lower() for c in self.connectors.values()
                if c.kind == "email" and len(c.company_ids) > 1}

    def mailbox_map(self) -> dict[str, str]:
        """Mailbox or alias -> the company whose mail it receives (H5), strongest source last so it wins:

        learned (an address that received at least three invoices, every one made out to the same company
        by its tax number; never a mailbox shared by several companies), a mailbox connected for one
        company only, then what the owner said.
        """
        shared = self.shared_mailboxes()
        counts: dict[str, dict[str, int]] = {}
        # Only documents that came to a mailbox count; their order does not matter (the result is sorted), so
        # the rest are skipped before any tax number is looked at (high volume, checklist X33).
        for d in self.documents.values():
            if not d.recipients:
                continue
            company = None if d.sales else self.company_for_tax_id(d.document.customer_tax_id)
            if company is None:
                continue
            for address in d.recipients:
                if address in shared:
                    continue
                bucket = counts.setdefault(address, {})
                bucket[company] = bucket.get(company, 0) + 1
        out = {a: next(iter(by)) for a, by in sorted(counts.items())
               if len(by) == 1 and sum(by.values()) >= HISTORY_MIN_COUNT}
        for c in sorted(self.connectors.values(), key=lambda c: c.id):
            if c.kind == "email" and len(c.company_ids) == 1 and c.company_ids[0] in self.companies:
                out[c.account.strip().lower()] = c.company_ids[0]
        out.update({a: c for a, c in self.mailboxes.items() if c in self.companies})
        return out

    def directory(self) -> CompanyDirectory:
        """Each company's legal and trade names, addresses and mailboxes, for the Entity Agent (H2, H3, H5)."""
        return CompanyDirectory(
            names={c: tuple(n for n in (self.legal_names.get(c),) if n) for c in sorted(self.companies)},
            addresses={c: tuple(a) for c, a in sorted(self.company_addresses.items()) if a and c in self.companies},
            mailboxes=self.mailbox_map(),
        )

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
    qr: FiscalQRResult | None = None  # the home country's fiscal QR code, read (§13 Stage 0)
    buyer_is_final_consumer: bool = False
    parsers: list[str] = field(default_factory=list)
    invoice_number: str | None = None
    issuer_tax_id: str | None = None  # one of the business's own tax numbers, when it issued the document
    referenced_number: str | None = None  # the invoice a credit note says it corrects
    paid_in_cash: bool = False
    text: str = ""
    statement: SupplierStatement | None = None  # a supplier's account statement, read line by line
    issuer: IssuerProfile | None = None
    billing_name: str | None = None  # the customer name printed on it (H2)
    billing_address: str | None = None  # the billing address printed on it (H3)
    home: str = "PT"  # the country of the company it is for: its pack read it (§49)


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
            record = TxRecord(tx=tx, evidence_id=reg.evidence.id, item_id=item.id, holder_id=account.holder_id,
                              cardholder=" ".join((row.cardholder or "").split())[:120] or None)
            self.repo.transactions[tx_id] = record
            self.o.advance(item, Stage.ACQUIRED, [reg.evidence.id], agent=self.name,
                           note="Bank transaction imported.")
            self.log("discover_transaction", subject_id=tx_id, evidence_ids=[reg.evidence.id],
                     values={"amount": row.amount, "counterparty": row.counterparty, "booked_on": row.booked_on})
            created.append(record)
        return created


class RetrievalAgent(_Agent):
    """Follows invoice links through the repository's link source (§9, §10). Never guesses a URL.

    The source is the demo's portal adapters, or on the production server what was fetched before the
    event was recorded (a replay never opens a link). What comes back is stored with its provenance
    (original and final URL, redirects, retrieval time; the page itself when no file is behind it). A
    sign-in wall becomes the owner's sign-in state; an unsafe link is never opened; a link that no longer
    works starts the missing-evidence path (§22); a site that did not answer is tried again later.
    """

    name = "retrieval"

    def follow(self, url: str, *, supplier: Supplier | None, at: datetime, context: Mapping[str, Any],
               source_kind: SourceKind = SourceKind.EMAIL, email_evidence_id: str | None = None,
               shared: bool = False) -> LinkRecord:
        repo = self.repo
        record = repo.links_seen.get(url)
        if record is None:
            record = LinkRecord(url=url, status="waiting", first_seen=at, email_evidence_id=email_evidence_id,
                                shared=shared)
        if supplier is not None:
            record.supplier_id, record.supplier_name = supplier.id, display_name(supplier.name)
        result = repo.links.fetch(url, supplier_name=supplier.name if supplier else None)
        if result is None:
            if record.status != "waiting":
                return record  # followed before (read again): what it gave is already on file
            if url not in repo.pending_links:
                repo.pending_links.append(url)
            self.log("link_pending", values={"url": url})
            repo.links_seen.setdefault(url, record)
            return record
        repo.links_seen[url] = record
        record.tries += 1
        record.supplier_name = record.supplier_name or display_name(result.supplier, fallback="The supplier")
        busy = result.outcome is LinkOutcome.UNAVAILABLE and result.retryable
        gave_up = busy and at - record.first_seen >= LINK_GIVE_UP_AFTER  # down for days: another way
        if url in repo.pending_links and (not busy or gave_up):
            repo.pending_links.remove(url)
        if gave_up:
            self._broken(record, result, at)
        elif result.portal is not None:
            self._from_portal(record, result, at, context)
        elif result.content is not None and result.format is not None:
            self._retrieved(record, result, source_kind, context)
        elif result.outcome in (LinkOutcome.LOGIN_REQUIRED, LinkOutcome.MFA_REQUIRED):
            self._needs_sign_in(record, result, at)
        elif result.outcome is LinkOutcome.BLOCKED_UNSAFE:
            record.status, record.reason, record.message = "blocked", result.reason or "unsafe", BLOCKED_MESSAGE
            self.log("link_blocked", values={"url": url, "reason": record.reason})
        elif result.expired:
            self._broken(record, result, at)
        else:  # a timeout, an outage, a busy site: tried again later
            record.status, record.reason = "waiting", result.reason or "unavailable"
            if url not in repo.pending_links:
                repo.pending_links.append(url)
            self.log("link_retry_later", values={"url": url, "reason": record.reason})
        return record

    def _from_portal(self, record: LinkRecord, result: FetchResult, at: datetime, context: Mapping[str, Any]) -> None:
        """A supplier portal adapter (§10) handed the document over."""
        assert result.content is not None and result.format is not None
        reg = self.repo.registry.register(
            result.content, tenant_id=self.repo.tenant_id, source_kind=SourceKind.SUPPLIER_PORTAL,
            format=result.format, mime_type=result.record.content_type or "application/octet-stream",
            filename=result.filename, original_url=record.url, retrieved_at=at, metadata={"portal": result.portal},
            context=dict(context),
        )
        record.status, record.evidence_ids = "retrieved", (reg.evidence.id,)
        self.log("retrieve", subject_id=reg.evidence.id, evidence_ids=[reg.evidence.id],
                 values={"url": record.url, "portal": result.portal, "supplier": result.supplier or ""})

    def _retrieved(self, record: LinkRecord, result: FetchResult, source_kind: SourceKind,
                   context: Mapping[str, Any]) -> None:
        """The file behind the link, or the page itself when there is none (§9 steps 6-10)."""
        regs = register_fetch(result, self.repo.registry, tenant_id=self.repo.tenant_id, source_kind=source_kind,
                              context=context)
        shot = sha256_hex(result.snapshot_png) if result.snapshot_png else None
        record.evidence_ids = tuple(dict.fromkeys(r.evidence.id for r in regs if r.evidence.sha256 != shot))
        record.snapshot_ids = tuple(r.evidence.id for r in regs if r.evidence.sha256 == shot)
        record.status, record.reason = "retrieved", result.outcome.value
        fetched = result.record
        self.log("retrieve", subject_id=record.evidence_ids[0] if record.evidence_ids else None,
                 evidence_ids=[*record.evidence_ids, *record.snapshot_ids],
                 values={"url": fetched.original_url, "final_url": fetched.final_url, "outcome": result.outcome.value,
                         "redirects": max(0, len(fetched.redirect_chain) - 1), "retrieved_at": fetched.retrieved_at,
                         "supplier": record.supplier_name},
                 response={"status": fetched.status_code, "sha256": fetched.sha256, "via_browser": result.via_browser})

    def _needs_sign_in(self, record: LinkRecord, result: FetchResult, at: datetime) -> None:
        """§9: the supplier's site wants the owner to sign in (or a code): the owner is told, once."""
        who = record.supplier_name or "The supplier"
        mfa = result.outcome is LinkOutcome.MFA_REQUIRED
        message = authentication_message(who) if mfa else sign_in_message(who)
        first = record.status != "sign_in"
        record.status, record.reason, record.message = "sign_in", result.outcome.value, message
        self.log("link_needs_sign_in", values={"url": record.url, "outcome": result.outcome.value, "supplier": who})
        if first:
            where = "The link you shared" if record.shared else "The invoice link in its email"
            self.o.activity(at, "checked", f"{message} {where} opens a sign-in page, so I could not get the "
                            "invoice from it yet.", self.o.supplier_company(record.supplier_id))

    def _broken(self, record: LinkRecord, result: FetchResult, at: datetime) -> None:
        """It no longer works (gone, 404/410): look for the invoice, then ask for it (§22)."""
        record.status, record.reason = "broken", result.reason or "unavailable"
        self.log("link_broken", values={"url": record.url, "reason": record.reason, "supplier": record.supplier_name})
        link_id = "lnk_" + hashlib.sha256(record.url.encode("utf-8")).hexdigest()[:16]
        if link_id in self.repo.broken_links:
            return
        since = at.astimezone(TZ).date()
        if record.email_evidence_id:  # the day the supplier's email was sent (else when it arrived)
            try:
                email = self.repo.evidence(record.email_evidence_id)
                sent = email.metadata.get("date")
                since = (datetime.fromisoformat(sent) if isinstance(sent, str) else email.retrieved_at
                         ).astimezone(TZ).date()
            except Exception:  # noqa: BLE001 - the arrival day is enough
                pass
        self.repo.broken_links[link_id] = BrokenLinkRecord(
            id=link_id, url=record.url, supplier_id=record.supplier_id,
            supplier_name=record.supplier_name or "the supplier", since=since, at=at,
            email_evidence_id=record.email_evidence_id, shared=record.shared)


_MONEY_WITH_CURRENCY = re.compile(r"(?:€|EUR)\s?-?\d[\d.,\u00a0 ]*\d|-?\d[\d.,\u00a0 ]*\d\s?(?:€|EUR)")


class DocumentAgent(_Agent):
    """Stage 0 extraction: UBL e-invoices, fiscal QR codes and text fields (§13, §19), plus the readings
    of uploaded PDFs and photos (§13-17), each through the pack of the company's own country (§49).

    Which of the business's companies a document is for decides its home country (the only one when
    all companies share one; else the company it names, its issuer's company for its own sale). The
    issuer's country is decided next (checklist P7): a document from another country than the home
    one is read with the international labels and its own conventions (day order, currency, VAT
    number), never as if it were domestic because the company is. A document one of the business's
    own companies issued (its own sales invoice) keeps its company's reading.
    """

    name = "document"

    def own_tax_ids(self) -> list[str]:
        """The business's own tax numbers with their country ("PT516123459"): on a purchase, the customer."""
        return [qualified_tax_id(e.tax_id, e.country) or e.tax_id for e in self.repo.entities]

    def named_countries(self, text: str) -> set[str]:
        """The countries of the business's companies whose tax number the text prints."""
        return {e.country for e in self.repo.entities if e.tax_id and _names_own_tax_id(text, e.tax_id)}

    def home_country(self, texts: Sequence[str], *, own_issuer: str | None = None,
                     fiscal: Sequence[tuple[str, str]] = (), numbers: Sequence[str] = ()) -> str:
        """The country of the company a document is for (its pack reads it).

        One country for the whole business: that one (a Portuguese business reads everything as
        before). Several: the issuing company's for its own sale; else the country of the
        companies the document names by tax number, when they agree; else the issuer's country
        when one of the companies is there (a domestic purchase); else the first company's.
        """
        countries = self.repo.countries()
        if len(countries) == 1:
            return countries[0]
        if own_issuer is not None:
            company = self.repo.company_for_tax_id(own_issuer)
            return self.repo.company_country(company)
        joined = "\n".join(texts)
        named = self.named_countries(joined)
        if len(named) == 1:
            return named.pop()
        primary = self.repo.primary_country()
        guess = detect_issuer(joined, own_tax_ids=self.own_tax_ids(), fiscal_qr=fiscal[0][0] if fiscal else False,
                              structured_tax_ids=numbers, home=primary)
        if guess.country in countries:
            return guess.country
        return primary

    def stage0_fields(self, text: str, source: str, method: ExtractionMethod,
                      hint: dict[str, str] | None = None) -> dict[str, list[FieldObservation]]:
        """Fields this agent reads from a file's own text and QR payloads (Stage 0 for the OCR router).

        ``hint`` remembers, for the OCR engines that read the same file next, whether one of
        the business's own companies issued it (its own sales invoice).
        """
        extracted = self.read([_Part(source, "text", text=text, method=method)])
        if hint is not None and extracted is not None and extracted.issuer_tax_id:
            hint["issuer"] = extracted.issuer_tax_id
        return {} if extracted is None else {k: list(v) for k, v in extracted.observations.items()}

    def text_extractor(self, hint: Mapping[str, str] | None = None) -> Any:
        """Reads fields from an OCR engine's text (the router labels them with the engine): the home
        country's fields, or the international labels when the text comes from abroad."""
        own = self.own_tax_ids()

        def extract(text: str, source: str, method: ExtractionMethod) -> dict[CriticalField, list[FieldObservation]]:
            issuer = (hint or {}).get("issuer") or self.own_issuer([_Part(source, "text", text=text, method=method)])
            found: dict[CriticalField, list[FieldObservation]] = {}
            payloads, rest = _split_qr(text)
            home = self.home_country([rest], own_issuer=issuer, fiscal=payloads)
            if issuer is None:  # not the business's own sale: who issued it, and from which country (P7)
                profile = detect_issuer(rest, own_tax_ids=own, fiscal_qr=payloads[0][0] if payloads else False,
                                        home=home)
                if profile.reads_foreign:
                    for name, obs in read_foreign_text(rest, source, method=method, issuer=profile,
                                                       own_tax_ids=own).observations.items():
                        found.setdefault(name, []).extend(obs)
                    return found
            customers, suppliers = self._roles(issuer)
            for obs in company_pack(home).read_text(text, source, method=method, known_customer_tax_ids=customers,
                                                    known_supplier_tax_ids=suppliers).observations:
                found.setdefault(obs.field, []).append(obs)
            currency = _text_currency(text, source, method)
            if currency is not None:
                found.setdefault(CriticalField.CURRENCY, []).append(currency)
            return found

        return extract

    def _roles(self, issuer: str | None) -> tuple[list[str], list[str]]:
        """(known buyers, known issuers) for the text reader: the business's own tax numbers are
        the buyer's, except the one that issued the document (its own sales invoice)."""
        own = self.repo.own_tax_ids()
        if issuer is None:
            return own, []
        return [t for t in own if not same_tax_id(t, issuer)], [issuer]

    def own_issuer(self, parts: Sequence[_Part]) -> str | None:
        """The business's own tax number when one of its companies issued the document, else None.

        Structured sources decide when there are any (the fiscal QR code's issuer, the
        e-invoice's supplier); otherwise the text, but only where it names the roles (a
        labelled supplier, or a labelled customer and exactly one other tax number).
        """
        own = self.repo.own_tax_ids()
        if not own:
            return None
        structured: list[str] = []
        worded: list[str] = []
        for part in parts:
            if part.kind == "ubl":
                try:
                    result = parse_einvoice(part.data, source=part.evidence_id)
                except (StructuredFormatError, XMLSyntaxError):
                    continue
                structured += [str(o.value) for o in result.fields.get(CriticalField.SUPPLIER_TAX_ID, ())]
                continue
            if part.kind == "html":
                for found in _html_structured(part.data, part.evidence_id):
                    structured += [str(o.value) for o in found.fields.get(CriticalField.SUPPLIER_TAX_ID, ())]
                continue
            if part.kind == "read":
                structured += [str(o.value) for o in part.observations.get(CriticalField.SUPPLIER_TAX_ID.value, ())
                               if o.method in (ExtractionMethod.QR, ExtractionMethod.STRUCTURED_XML)]
            payloads, rest = _split_qr(part.text)
            for country, payload in payloads:
                found = _fiscal_qr(country, payload, part.evidence_id)
                if found is not None and found.issuer_tax_id:
                    structured.append(found.issuer_tax_id)
            for country in self.repo.countries():  # each company's own labels
                neutral = company_pack(country).read_text(rest, part.evidence_id, method=part.method)
                worded += [str(o.value) for o in neutral.observations if o.field is CriticalField.SUPPLIER_TAX_ID]
        for value in structured or worded:
            mine = next((t for t in own if same_tax_id(t, value)), None)
            if mine is not None:
                return mine
        return None

    def _signals(self, parts: Sequence[_Part], structured: Mapping[int, Any]
                 ) -> tuple[list[str], list[tuple[str, str]], list[str]]:
        """(texts without fiscal QR payloads, the fiscal QR payloads with their country, the supplier tax
        numbers structured copies give), from all of a document's readable parts together."""
        texts: list[str] = []
        fiscal: list[tuple[str, str]] = []
        numbers: list[str] = []
        for i, part in enumerate(parts):
            if part.kind in ("ubl", "html"):
                found = structured.get(i)
                for result in found if isinstance(found, tuple) else (found,) if found is not None else ():
                    numbers += [str(o.value) for o in result.fields.get(CriticalField.SUPPLIER_TAX_ID, ())]
                continue
            payloads, rest = _split_qr(part.text)
            fiscal += payloads
            texts.append(rest)
            if part.kind == "read":
                texts.append(part.reading_text)
        return texts, fiscal, numbers

    def issuer_of(self, parts: Sequence[_Part], structured: Mapping[int, Any], home: str) -> IssuerProfile:
        """Who issued the document, from all its readable parts together (a fiscal QR code names its country),
        relative to the home country of the company it is for."""
        texts, fiscal, numbers = self._signals(parts, structured)
        return detect_issuer("\n".join(texts), own_tax_ids=self.own_tax_ids(),
                             fiscal_qr=fiscal[0][0] if fiscal else False, structured_tax_ids=numbers, home=home)

    def read(self, parts: Sequence[_Part]) -> _Extracted | None:
        observations: dict[str, list[FieldObservation]] = {}
        # The document's kind by where it was read, most trusted first when choosing (§13: structured first).
        kinds: dict[str, DocumentType] = {}
        receipt_fallback = False  # text with fields but no word naming its kind: read as a receipt
        supplier_name: str | None = None
        qr: FiscalQRResult | None = None
        final_consumer = False
        parsers: list[str] = []
        structured: dict[int, Any] = {}  # e-invoices (one result each) and HTML data (a tuple of results each)
        for i, part in enumerate(parts):
            if part.kind == "ubl":
                try:
                    structured[i] = parse_einvoice(part.data, source=part.evidence_id)
                except (StructuredFormatError, XMLSyntaxError):
                    continue
            elif part.kind == "html":
                structured[i] = _html_structured(part.data, part.evidence_id)
        own_issuer = self.own_issuer(parts)
        customers, suppliers = self._roles(own_issuer)
        # Which company it is for decides its home country, whose pack reads it (§49): never another's.
        signals = self._signals(parts, structured)
        home = self.home_country(signals[0], own_issuer=own_issuer, fiscal=signals[1], numbers=signals[2])
        pack = company_pack(home)
        code = home.lower()
        # Who issued it and from which country (checklist P7). The business's own sale is its own
        # company's document: read its company's way, with its roles, never as a purchase from abroad.
        issuer = None if own_issuer is not None else self.issuer_of(parts, structured, home)
        # From abroad (or in another language with no country to go by): its own labels and conventions.
        abroad = issuer is not None and issuer.reads_foreign
        own = self.own_tax_ids() if abroad else []
        stated: list[Decimal] = []
        texts: list[str] = []
        reference: str | None = None
        billed_name: str | None = None  # the customer name and address an e-invoice gives (H2, H3)
        billed_address: str | None = None

        def add(field_: CriticalField | str, obs: FieldObservation) -> None:
            key = field_.value if isinstance(field_, CriticalField) else str(field_)
            observations.setdefault(key, []).append(obs)

        for i, part in enumerate(parts):
            if part.kind == "ubl":
                result = structured.get(i)
                if result is None:
                    continue
                parsers.append(result.kind)
                for f, found in result.fields.items():
                    for obs in found:
                        add(f, obs)
                if result.doc_type is not None:
                    kinds.setdefault("ubl", result.doc_type)
                supplier_name = supplier_name or result.extras.get("supplier_name")
                reference = reference or result.extras.get("invoice_reference")
                billed_name = billed_name or result.extras.get("customer_name")
                billed_address = billed_address or result.extras.get("customer_address")
                continue
            if part.kind == "html":  # schema.org Invoice / Order data in an HTML email or page (§13 Stage 0)
                for result in structured.get(i, ()):
                    parsers.append(result.kind)
                    for f, found in result.fields.items():
                        for obs in found:
                            add(f, obs)
                    supplier_name = supplier_name or result.extras.get("supplier_name")
                    if result.kind.endswith("_invoice"):
                        kinds.setdefault("html", DocumentType.INVOICE)
                continue
            if part.kind == "read":
                for name, found in sorted(part.observations.items()):
                    for obs in found:
                        add(name, obs)
                if part.observations:
                    parsers.append(f"ocr:{part.parser}" if part.parser else "ocr")
                supplier_name = supplier_name or part.supplier_name
                if part.doc_type is not None:
                    kinds.setdefault("reading", part.doc_type)
            text = part.text
            qr_payloads, rest = _split_qr(text)
            for country, payload in qr_payloads:
                if country != home:
                    continue  # another country's code: it named the issuer's country, it is not read as ours
                found = _fiscal_qr(country, payload, part.evidence_id)
                if found is None:
                    continue
                qr = qr or found
                parsers.append(f"{code}_fiscal_qr")
                final_consumer = final_consumer or found.buyer_is_final_consumer
                for obs in found.observations:
                    add(obs.field, obs)
                add(CriticalField.CURRENCY, FieldObservation(
                    value=found.currency, source=part.evidence_id, method=ExtractionMethod.QR, confidence=0.95,
                    location="qr:amounts are in euro"))
                if found.native_doc_type:  # a code that says which kind of document it is on
                    kinds.setdefault("qr", found.doc_type)
            method = part.method
            if abroad:
                assert issuer is not None
                foreign = read_foreign_text(rest, part.evidence_id, method=method, issuer=issuer, own_tax_ids=own)
                if foreign.observations:
                    parsers.append({"text": "intl_text_fields", "read": "intl_text_fields:pdf_text"}.get(
                        part.kind, "intl_text_fields:email_body"))
                for name, found in foreign.observations.items():
                    for obs in found:
                        add(name, obs)
                stated += foreign.stated_rates
                read_any = bool(foreign.observations)
            else:
                fields = pack.read_text(rest, part.evidence_id, method=method, known_customer_tax_ids=customers,
                                        known_supplier_tax_ids=suppliers)
                if fields.observations:
                    parsers.append({"text": f"{code}_text_fields", "read": f"{code}_text_fields:pdf_text"}.get(
                        part.kind, f"{code}_text_fields:email_body"))
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
                read_any = bool(fields.observations)
            supplier_name = supplier_name or part.supplier_name or _first_line(rest, foreign=abroad, pack=pack)
            if read_any:
                named = _doc_kind(rest, pack, foreign=abroad)
                if named is not None:
                    kinds.setdefault("text", named)
                receipt_fallback = True
            if rest.strip():
                texts.append(rest)
                reference = reference or _referenced_invoice(rest)
            if part.kind == "read" and part.observations:
                supplier_name = supplier_name or _first_line(part.reading_text, foreign=abroad, pack=pack)
                named = _doc_kind(part.reading_text, pack, foreign=abroad)
                if named is not None:
                    kinds.setdefault("reading_text", named)
                receipt_fallback = True
                if part.reading_text.strip():
                    texts.append(part.reading_text)
                    reference = reference or _referenced_invoice(part.reading_text)
        joined = "\n".join(texts)
        statement: SupplierStatement | None = None
        if not observations:
            # A supplier's account statement (a CSV export, or a text that names itself one) may carry no
            # invoice fields at all: it is still kept, as a statement, and checked line by line.
            statement = read_statement(joined) if joined.strip() and "qr" not in kinds else None
            if statement is None:
                return None
            supplier_name = _statement_issuer(joined)
        number = _first_value(observations, CriticalField.INVOICE_NUMBER)
        doc_type = next((kinds[k] for k in ("qr", "ubl", "text", "html", "reading", "reading_text") if k in kinds),
                        DocumentType.RECEIPT if receipt_fallback else DocumentType.INVOICE)
        if statement is None and "qr" not in kinds and "ubl" not in kinds and joined.strip():
            if doc_type is DocumentType.SUPPLIER_STATEMENT:
                statement = read_statement(joined, titled=True)
            else:
                statement = read_statement(joined, csv_only=True)  # a statement exported as CSV names no kind
        if statement is not None:
            doc_type = DocumentType.SUPPLIER_STATEMENT
            number = None  # the numbers and amounts on a statement are its lines' documents, never its own
            for name in _NOT_A_STATEMENT_FIELD:
                observations.pop(name, None)
        if stated and issuer is not None:
            issuer = replace(issuer, stated_rates=tuple(dict.fromkeys(stated)))
        printed = read_billing_party(joined)  # "Cliente: ..." and the address under it, when labelled
        return _Extracted(
            billing_name=billed_name or printed.name, billing_address=billed_address or printed.address,
            observations=observations, doc_type=doc_type, supplier_name=supplier_name,
            evidence_ids=list(dict.fromkeys(p.evidence_id for p in parts)), qr=qr,
            buyer_is_final_consumer=final_consumer, parsers=[*parsers, *(["supplier_statement"] if statement else [])],
            invoice_number=str(number) if number is not None else None,
            issuer_tax_id=own_issuer, referenced_number=reference, paid_in_cash=_says_paid_in_cash(joined),
            text=joined[:_TEXT_KEPT], statement=statement, issuer=issuer, home=home,
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
               owner: Mapping[str, FieldObservation] | None = None, bank_currency: str | None = None,
               issuer: IssuerProfile | None = None, bank: BankCharge | None = None,
               log: bool = True, country: str | None = None) -> DocumentAssessment:
        """Every field graded with the observations behind it (§18). A value the owner confirmed
        replaces the readings that disagree with it (they stay on the record, §55).

        A document from abroad (``issuer``) is checked by rules that hold anywhere, with its own
        country's VAT rates, and ``bank`` (the matching payment, in the document's currency) as the
        independent source (checklist P7). A domestic document is checked with the VAT rates of its
        company's country (``country``, else the issuer profile's home), through that country's pack.
        """
        observations = _with_owner(observations, owner or {})
        issue = _first_value(observations, CriticalField.ISSUE_DATE)
        foreign = issuer is not None and issuer.is_foreign
        if foreign:
            assert issuer is not None
            on = issue if isinstance(issue, date) else self.repo.today()
            assessment = assess_foreign(observations, _foreign_rules(issuer, on), doc_type=doc_type, bank=bank)
            response: dict[str, Any] = {"quality": assessment.quality.value, "with_bank_amount": bank is not None,
                                        "issuer_country": issuer.country}
        else:
            pack = company_pack(country or (issuer.home if issuer is not None else self.repo.primary_country()))
            try:
                rates = pack.vat_rates(issue) if isinstance(issue, date) else pack.vat_rates(self.repo.today())
            except CountryPackError:
                rates = ()
            assessment = assess_document(observations, rates, bank_amount, doc_type=doc_type,
                                         bank_currency=bank_currency)
            response = {"quality": assessment.quality.value, "with_bank_amount": bank_amount is not None}
        if log:
            self.log("verify", subject_id=subject_id, evidence_ids=evidence_ids,
                     values={name: a.value for name, a in assessment.fields.items() if a.value is not None},
                     validations=[_validation(n, a) for n, a in sorted(assessment.fields.items())],
                     response=response)
        return assessment


class FraudAgent(_Agent):
    """Hard stops before anything is paid or closed (§26). It can block; it can never approve."""

    name = "fraud"

    def check(self, record: DocumentRecord, *, later_copy: bool = False, sender: str | None = None,
              message_text: str | None = None) -> FraudAssessment:
        """Every §26 check on a supplier document. ``later_copy``: its bank details came with a later copy of it
        (or the owner's choice between two copies), delivered by ``sender`` with ``message_text`` (that copy's
        email) rather than the first copy's: they are judged exactly like a first copy's (checklist Q4)."""
        supplier = self.repo.suppliers.get(record.supplier_id or "")
        history = [
            d.document for d in self.repo.documents.values()
            if d.id != record.id and d.supplier_id and d.supplier_id == record.supplier_id and not d.on_hold
        ]
        delivered_by = record.sender if sender is None and not later_copy else sender
        # A receipt one of your employees passes on (their reply, their forward) did not come from the supplier:
        # their address is not a changed supplier domain. Every other check still runs (backoffice.staff).
        sender = None if self.o.staff.employee_by_email(delivered_by) is not None else delivered_by
        text = record.message_text if message_text is None and not later_copy else (message_text or "")
        result = assess(FraudCase(
            entities=self.repo.entities, supplier=supplier, document=record.document,
            history=sorted(history, key=lambda d: (d.issue_date or date.min, d.id)),
            sender=sender, message_text=text, iban_on_later_copy=later_copy,
        ))
        self.log("assess", subject_id=record.id, evidence_ids=record.evidence_ids,
                 values={"hard_stop": result.hard_stop},
                 validations=[{"signal": s.kind.value, "severity": s.severity.value} for s in result.signals],
                 response=result.owner_message)
        return result


class EntityAgent(_Agent):
    """Which of the owner's companies a payment belongs to (§46, §51), using taught rules (§38).

    Besides the tax number, the billing name and address printed on the invoice and the mailbox or alias it
    arrived at count as hints (H2, H3, H5): they never override the tax number, and a disagreement is the one
    question. A company card at a clearly personal shop, with no history of the company paying it, is asked
    rather than booked on the card's word (X23).
    """

    name = "entity"

    def addressee(self, record: DocumentRecord) -> Addressee:
        """What the document says about who it is for, besides the tax number (never for your own sales)."""
        if record.sales:
            return Addressee()
        return Addressee(name=record.billing_name, address=record.billing_address, mailboxes=record.recipients)

    def personal_hint(self, rec: TxRecord) -> str | None:
        """'a streaming service' when this card purchase is at a shop that is clearly personal for the company
        the card belongs to; None for anything else (a known supplier, a transfer, money in)."""
        tx = rec.tx
        if tx.amount >= 0 or not tx.card_last4:
            return None
        owner = self.repo.ownership().cards.get(tx.card_last4)
        if owner is None or owner not in self.repo.companies:
            return None
        known = self.repo.resolver().resolve_transaction(tx).supplier is not None
        return personal_signal(tx.counterparty, tx.description, sector=self.repo.company_sectors.get(owner),
                               known_supplier=known)

    _history_cache: tuple[list[tuple[str, str]], int, dict[str, dict[str, int]]] | None = None

    def _history(self) -> dict[str, dict[str, int]]:
        """``build_history(repo.history_pairs)``, kept up to date as pairs are appended rather than rebuilt for every
        payment (the list only ever grows; a new or shorter list is built afresh). Read-only for its users."""
        pairs = self.repo.history_pairs
        cached = self._history_cache
        if cached is None or cached[0] is not pairs or cached[1] > len(pairs):
            history: dict[str, dict[str, int]] = {}
            seen = 0
        else:
            _, seen, history = cached
        if seen < len(pairs):
            for key, target in pairs[seen:]:
                bucket = history.setdefault(key, {})
                bucket[target] = bucket.get(target, 0) + 1
        self._history_cache = (pairs, len(pairs), history)
        return history

    def assign(self, rec: TxRecord) -> EntityAssignment:
        docs = [self.repo.documents[d] for d in rec.document_ids if d in self.repo.documents]
        record = docs[0] if len(docs) == 1 else None
        addressee = self.addressee(record) if record is not None else None
        hinted = addressee is not None and not addressee.empty
        document = record.document if record is not None and (record.document.customer_tax_id or hinted) else None
        accountant_ids = self.repo.accountant_ids_for(rec.company_id)
        result = assign_entity(
            entities=self.repo.entities, transaction=rec.tx, document=document, ownership=self.repo.ownership(),
            rulebook=self.repo.rulebook, accountant_ids=accountant_ids,
            history=self._history(), today=self.repo.today(),
            addressee=addressee if document is not None and hinted else None,
            directory=self.repo.directory() if document is not None and hinted else None,
            personal_signal=self.personal_hint(rec),
        )
        self.log("assign_entity", subject_id=rec.id, evidence_ids=[rec.evidence_id],
                 values={"entity_id": result.entity_id or "", "private": result.private},
                 validations=list(result.why), response={"quality": result.quality.value,
                                                         "asks_owner": result.needs_owner})
        return result

    def assign_document(self, record: DocumentRecord) -> str | None:
        doc = record.document
        addressee = self.addressee(record)
        if not doc.customer_tax_id and addressee.empty:
            return None
        result = assign_entity(entities=self.repo.entities, document=doc, today=self.repo.today(),
                               addressee=None if addressee.empty else addressee,
                               directory=None if addressee.empty else self.repo.directory())
        self.log("assign_entity", subject_id=record.id, evidence_ids=record.evidence_ids,
                 values={"entity_id": result.entity_id or ""}, validations=list(result.why),
                 response={"quality": result.quality.value})
        return result.entity_id if result.quality is Quality.GREEN else None


class ReconciliationAgent(_Agent):
    """Expected evidence (§21), then transactions matched to documents (§20)."""

    name = "reconciliation"

    def engine(self) -> ExpectedEvidenceEngine:
        """The expected-evidence rules with what was learned (J7) and the employees known from payslips (J3)."""
        return ExpectedEvidenceEngine(entities=self.repo.entities, suppliers=self.repo.resolver(),
                                      overrides=_LearnedExpectations(self.repo),
                                      employee_ibans=sorted(self.repo.payroll_employees),
                                      account_countries=self.repo.account_countries())

    def classify(self, records: Sequence[TxRecord]) -> None:
        if not records:
            return
        engine = self.engine()
        customers = self.o.customer_payments()  # who paid your own invoices, looked up once (high volume, X33)
        for rec in records:
            decision = engine.classify(rec.tx)
            # Money back to a customer or depositor; a card payment a customer disputed (I7).
            decision = (self._customer_refund(rec, decision, customers) or self.o.chargebacks.decision(rec, decision)
                        or decision)
            rec.decision = decision
            self.log("expect", subject_id=rec.id, evidence_ids=[rec.evidence_id],
                     values={"expectation": decision.expectation.value, "rule": decision.rule},
                     response={"quality": decision.quality.value, "reason": decision.reason})

    def customer_refunds(self) -> int:
        """Money out already decided as needing a supplier's invoice, now known to go back to one of your
        customers (their payment of your own invoice arrived since): it needs your own credit note (§20)."""
        moved = 0
        customers = self.o.customer_payments()  # deciding refunds changes none of them: looked up once
        for rec in sorted(self.repo.transactions.values(), key=lambda r: r.id):
            if rec.decision is None or rec.document_ids or self.repo.items[rec.item_id].is_done:
                continue
            found = self._customer_refund(rec, rec.decision, customers)
            if found is None:
                continue
            rec.decision = found
            self.log("expect", subject_id=rec.id, evidence_ids=[rec.evidence_id],
                     values={"expectation": found.expectation.value, "rule": found.rule},
                     response={"quality": found.quality.value, "reason": found.reason})
            moved += 1
        return moved

    def _customer_refund(self, rec: TxRecord, decision: ExpectationDecision,
                         customers: Sequence[TxRecord] | None = None) -> ExpectationDecision | None:
        """Money out to someone who paid one of your own sales invoices: a refund to your customer, covered
        by your own credit note, not by a supplier's invoice. Same bank account: GREEN; same name: AMBER."""
        if rec.tx.amount >= 0 or decision.rule not in ("payment_out", "card_in_shop"):
            return None
        customer = self.o.customer_of(rec.tx, customers)
        if customer is None:
            # Money back to someone whose deposit you hold (a cancelled booking): linked to that deposit (X8).
            payer = self.o.staged.deposit_payer(rec)
            if payer is None:
                return None
            name, by_account = payer
            return ExpectationDecision(rec.tx.id, EvidenceExpectation.REFUND_OR_CREDIT_NOTE,
                                       f"Money back to {name}, who paid you a deposit.",
                                       Quality.GREEN if by_account else Quality.AMBER, "deposit_refund")
        name, by_account = customer
        return ExpectationDecision(rec.tx.id, EvidenceExpectation.REFUND_OR_CREDIT_NOTE,
                                   f"Money back to your customer {name}. Your own credit note covers it.",
                                   Quality.GREEN if by_account else Quality.AMBER, "customer_refund")

    def match(self) -> list[Match]:
        """Payments matched to the documents that prove them (§20).

        Only accounting documents take part: a pro-forma, quote, delivery note, order or
        supplier statement never proves a payment (§3), nor does a receipt paid in cash (no bank
        line) or a document already kept as supporting evidence. A match only counts when the
        document is a kind the payment needs (ACCEPTED_DOCUMENT_TYPES, §21, when that need is
        certain); otherwise the document stays with the payment as supporting evidence and
        the payment stays open. An invoice with a linked credit note is matched net of it,
        then, for a payment of the full amount, as it stands.

        An invoice paid in parts (instalments, milestones, a deposit taken off, a part held back) takes
        part with what is still to pay on it (checklist X8, I2, I5). Several payments, or a part payment,
        are kept when the engine is certain, or when every payment quotes the invoice and comes from its
        customer or supplier (:meth:`StagedPaymentsAgent.proven`); anything less waits for the owner's tap.
        """
        repo = self.repo
        staged = self.o.staged
        # Payouts and payout reports are paired by the settlement agent, never with an invoice.
        # A salary known by the employee's account is proven by the payroll agent, by its payslip only (J3).
        txs = [r for r in repo.transactions.values()
               if r.decision is not None and not r.document_ids and not r.private
               and not repo.items[r.item_id].is_done
               and r.decision.expectation is not EvidenceExpectation.PAYOUT_REPORT
               and not (r.decision.expectation is EvidenceExpectation.PAYROLL and r.decision.quality is Quality.GREEN)
               # A grant (its letter), a chargeback (the sale it takes back) and a security deposit held for a
               # customer (its return) are never proven by an invoice (checklist X30, I7, X9).
               and r.decision.rule not in _OWN_PROOF_RULES
               and not (r.id in repo.deposits and repo.deposits[r.id].security)
               and (r.decision.requires_document or r.decision.quality is not Quality.GREEN)
               and not self.o.matched_elsewhere(r)]
        docs = [d for d in repo.documents.values()
                if not d.on_hold and d.document.quality is not Quality.RED
                and (not d.matched_tx_ids or staged.open_for_parts(d))
                and d.document.doc_type not in (DocumentType.PAYOUT_REPORT, DocumentType.PAYROLL)
                and not repo.items[d.item_id].is_done
                and not d.supporting and not d.paid_in_cash and not d.supports_tx_ids and staged.matchable(d)
                and d.claim_id is None  # paid with an employee's own money: an expense claim (backoffice.staff)
                and not d.book]  # a leasing contract, a till report or a receipt from a list: matched by its agent
        accepted: list[Match] = []
        if txs and docs:
            netted = self._netting(docs)
            accepted = self._reconcile(txs, docs, netted)
            if netted:  # an invoice paid in full although a credit note was issued for it: the credit waits
                left_tx = [r for r in txs if not r.document_ids]
                left_docs = [d for d in docs if not d.matched_tx_ids and not d.supports_tx_ids]
                if left_tx and any(d.id in netted for d in left_docs):
                    accepted += self._reconcile(left_tx, left_docs, {})
        self._attach_supporting()
        return accepted

    def _netting(self, docs: Sequence[DocumentRecord]) -> dict[str, list[DocumentRecord]]:
        """Open invoices with open linked credit notes smaller than them: invoice id -> credit notes."""
        by_id = {d.id: d for d in docs}
        credits: dict[str, list[DocumentRecord]] = {}
        for d in sorted(docs, key=lambda d: d.id):
            if d.document.doc_type is DocumentType.CREDIT_NOTE and d.credit_for in by_id:
                credits.setdefault(d.credit_for, []).append(d)
        netted = {}
        for invoice_id, notes in credits.items():
            invoice = by_id[invoice_id].document
            if invoice.gross_amount is None or any(
                    n.document.gross_amount is None or n.document.currency != invoice.currency for n in notes):
                continue
            credit = sum((abs(n.document.gross_amount or _ZERO) for n in notes), _ZERO)
            if credit < abs(invoice.gross_amount):
                netted[invoice_id] = notes
        return netted

    def _reconcile(self, txs: Sequence[TxRecord], docs: Sequence[DocumentRecord],
                   netted: Mapping[str, list[DocumentRecord]]) -> list[Match]:
        repo = self.repo
        own = repo.own_tax_ids()
        absorbed = {n.id for notes in netted.values() for n in notes}
        pool = [d for d in docs if d.id not in absorbed]
        balances: dict[str, Decimal] = {}
        for invoice_id, notes in netted.items():
            flow = document_flow(repo.documents[invoice_id].document, own) or _ZERO
            balances[invoice_id] = flow + sum((document_flow(n.document, own) or _ZERO for n in notes), _ZERO)
        for d in pool:  # paid in parts: only what is still to pay on it can match (checklist X8, I2, I5)
            left = self.o.staged.balance(d)
            flow = document_flow(d.document, own)
            if left is not None and d.id not in balances and flow:
                balances[d.id] = left if flow > 0 else -left
        decisions = {r.id: r.decision for r in txs if r.decision is not None}
        # A document from abroad has no second source of its own: the one payment that confirms it
        # may match it, and only that payment (checklist P7).
        confirmed = self.o.bank_confirmations(txs, pool)
        result = reconcile(
            [r.tx for r in sorted(txs, key=lambda r: r.id)],
            [d.document.model_copy(update={"quality": Quality.GREEN})
             if d.id in confirmed and self.o.addressed_elsewhere(d) is None else d.document
             for d in sorted(pool, key=lambda d: d.id)],
            suppliers=repo.resolver(), own_tax_ids=own, expectations=decisions, document_balances=balances,
        )
        accepted = []
        for m in result.matches:
            green = m.quality is Quality.GREEN and not m.is_ambiguous
            abroad = [d for d in m.document_ids if d in confirmed]
            if green and abroad:
                green = (len(m.transaction_ids) == 1 and len(m.document_ids) == 1
                         and confirmed[m.document_ids[0]] == m.transaction_ids[0])
            quality = Quality.AMBER if abroad and not green else m.quality
            evidence = [repo.transactions[t].evidence_id for t in m.transaction_ids]
            for d in m.document_ids:
                evidence += repo.documents[d].evidence_ids
            self.log("match", subject_id=m.id, evidence_ids=evidence,
                     values={"transactions": list(m.transaction_ids), "documents": list(m.document_ids)},
                     validations=list(m.why), response={"quality": quality.value, "kind": m.kind.value})
            if self.o.staged.in_parts(m):
                # Several payments for one invoice, a part payment, or the rest of an invoice paid in parts.
                proven = (green and not abroad) or self.o.staged.proven(m)
                refused = self._wrong_kind(m) if proven else None
                if proven and refused is None:
                    accepted.append(m)
                    self.o.staged.record_parts(m)
                elif refused is not None:
                    self.log("not_proof", subject_id=m.id, evidence_ids=evidence, response={"reason": refused})
                    for t in m.transaction_ids:
                        self.o.support(repo.transactions[t], [repo.documents[d] for d in m.document_ids])
                else:
                    for t in m.transaction_ids:
                        repo.transactions[t].likely_document_ids = list(m.document_ids)
                continue
            refused = self._wrong_kind(m) if green else None
            if green and refused is None:
                accepted.append(m)
                notes = [n for d in m.document_ids for n in netted.get(d, [])]
                why = tuple(m.why) + tuple(
                    f"Credit note{' ' + n.document.invoice_number if n.document.invoice_number else ''}: "
                    f"{format_money(abs(n.document.gross_amount or _ZERO), n.document.currency)} taken off"
                    for n in notes)
                # A refund: the credit note it matches, and the invoice that credit note corrects (§20).
                chain, chain_headline = self.o.refund_chain_lines(
                    [repo.transactions[t] for t in m.transaction_ids], [repo.documents[d] for d in m.document_ids])
                why += chain
                for t in m.transaction_ids:
                    rec = repo.transactions[t]
                    rec.document_ids = [*m.document_ids, *(n.id for n in notes)]
                    rec.match_why = why
                    rec.match_headline = chain_headline or m.headline
                    rec.likely_document_ids = []
                for d in [*m.document_ids, *(n.id for n in notes)]:
                    repo.documents[d].matched_tx_ids = list(m.transaction_ids)
            elif refused is not None:
                # The right amount, but not the document this payment needs: kept with it as supporting
                # evidence; the payment stays open and its invoice is still looked for (§22).
                self.log("not_proof", subject_id=m.id, evidence_ids=evidence, response={"reason": refused})
                for t in m.transaction_ids:
                    self.o.support(repo.transactions[t], [repo.documents[d] for d in m.document_ids])
            else:
                for t in m.transaction_ids:
                    repo.transactions[t].likely_document_ids = list(m.document_ids)
        return accepted

    def _wrong_kind(self, m: Match) -> str | None:
        """Why the matched documents cannot prove these payments, or None when they can (§21).

        Applied where the need is certain (a GREEN decision, e.g. a known supplier's invoice);
        a credit note netted inside the payment is not what is judged.
        """
        records = [self.repo.documents[d] for d in m.document_ids]
        docs = [r.document for r in records]
        kinds = {d.doc_type for d in docs if d.doc_type is not DocumentType.CREDIT_NOTE} or {d.doc_type for d in docs}
        for t in m.transaction_ids:
            rec = self.repo.transactions[t]
            decision = rec.decision
            if decision is None or not decision.requires_document or decision.quality is not Quality.GREEN:
                continue
            if rec.tx.amount > 0 and all(r.sales for r in records):
                continue  # your own invoice, paid by a customer who also happens to be a supplier
            if not kinds <= decision.accepted_document_types:
                return (f"{', '.join(sorted(k.value for k in kinds))} cannot stand for the "
                        f"{decision.expectation.value} this payment needs")
        return None

    def _attach_supporting(self) -> None:
        """Pro-formas, quotes, delivery notes... kept with the payment they relate to (never as its proof)."""
        repo = self.repo
        # A statement read line by line is checked by the statement agent: its balance is not a purchase amount.
        docs = [d for d in repo.documents.values()
                if d.supporting and not d.supports_tx_ids and not d.on_hold and d.document.gross_amount is not None
                and d.id not in repo.statements and not d.book]
        txs = [r for r in repo.transactions.values()
               if r.decision is not None and r.decision.requires_document and not r.private and r.tx.amount != 0]
        if not docs or not txs:
            return
        result = reconcile(
            [r.tx for r in sorted(txs, key=lambda r: r.id)],
            [d.document for d in sorted(docs, key=lambda d: d.id)],
            suppliers=repo.resolver(), own_tax_ids=repo.own_tax_ids(),
        )
        for m in result.matches:
            if m.is_ambiguous or m.quality is Quality.RED:
                continue
            self.log("keep_supporting", subject_id=m.id,
                     evidence_ids=[*(repo.transactions[t].evidence_id for t in m.transaction_ids),
                                   *(e for d in m.document_ids for e in repo.documents[d].evidence_ids)],
                     values={"transactions": list(m.transaction_ids), "documents": list(m.document_ids)})
            for t in m.transaction_ids:
                self.o.support(repo.transactions[t], [repo.documents[d] for d in m.document_ids])


class _LearnedExpectations:
    """What evidence a counterparty's payments need, as the owner or accountant taught it (§21, J7).

    The pipeline's :class:`ExpectationOverrides`: a company-limited accountant rule for the payment's
    company first, then what applies to every company (the owner's answers, accountant rules for all).
    """

    def __init__(self, repo: Repository) -> None:
        self.repo = repo

    def lookup(self, transaction: Transaction, supplier_key: str) -> LearnedExpectation | None:
        rec = self.repo.transactions.get(transaction.id)
        company = rec.company_id if rec is not None else transaction.entity_id
        scoped = self.repo.company_expectation_overrides.get(company or "")
        found = scoped.lookup(transaction, supplier_key) if scoped is not None else None
        return found or self.repo.expectation_overrides.lookup(transaction, supplier_key)


class PayrollAgent(_Agent):
    """Payslips prove salaries (§21, checklist J3; :mod:`backoffice.payroll`).

    A payslip ("recibo de vencimento") is read as a PAYROLL document for the employee, from the company
    its employer tax number names. A salary paid to an employee's account (known from a payslip) is
    certainly a salary; one worded as a salary is likely one. Either is closed only by that month's
    payslip for that employee, with the net pay to the cent. A salary without it stays open with a plain
    request to whoever runs payroll: the accountant (an email, when the owner allowed routine messages to
    the accountant) or the owner. Social Security and IRS withholding payments keep their tax rules.
    """

    name = "payroll"

    def accept(self, parts: Sequence[_Part], *, at: datetime, origin: str, report: IngestReport,
               sender: str | None = None, message_text: str = "", recipients: tuple[str, ...] = ()) -> bool:
        """A payslip becomes a PAYROLL document (True); anything else is left to the other agents (False)."""
        text = "\n".join(p.text or p.reading_text for p in parts if p.kind in ("text", "read"))
        payslip = read_payslip(text)
        if payslip is None:
            return False
        repo = self.repo
        evidence = list(dict.fromkeys(p.evidence_id for p in parts))
        same = next((d for d in repo.documents.values() if d.payslip is not None
                     and d.payslip.period == payslip.period and d.payslip.net == payslip.net
                     and fold_name(d.payslip.employee) == fold_name(payslip.employee)), None)
        if same is not None:  # the same payslip again: one document, every copy kept as evidence
            new = [e for e in evidence if e not in same.evidence_ids]
            same.evidence_ids = [*same.evidence_ids, *new]
            same.document = same.document.model_copy(update={"evidence_ids": same.evidence_ids})
            report.document_ids.append(same.id)
            report.already_known = not new
            report.message = "Got it. I already had this payslip." if not new else "Got it."
            return True
        company = repo.company_for_tax_id(payslip.employer_tax_id)
        doc_id = "doc_" + evidence[0][3:19]
        quality = Quality.RED if payslip.quality is Quality.RED else Quality.AMBER
        document = Document(
            id=doc_id, tenant_id=repo.tenant_id, evidence_ids=evidence, doc_type=DocumentType.PAYROLL,
            supplier_name=payslip.employee, supplier_tax_id=payslip.employee_tax_id,
            customer_tax_id=payslip.employer_tax_id, issue_date=payslip.issued_on or payslip.period.last_day,
            currency=payslip.currency, gross_amount=payslip.net, iban=payslip.employee_iban, quality=quality,
            entity_id=company,
        )
        item = TrackedItem(id="item_" + doc_id, tenant_id=repo.tenant_id, subject_type="document", subject_id=doc_id)
        repo.items[item.id] = item
        record = DocumentRecord(document=document, evidence_ids=evidence, origin=origin, received_at=at,
                                item_id=item.id, observations={}, reasons=payslip.reasons(), sender=sender,
                                message_text=message_text, recipients=recipients, text=text[:_TEXT_KEPT],
                                payslip=payslip, country=repo.company_country(company))
        repo.documents[doc_id] = record
        self.o._classify_sensitive(record, text)  # a payslip always: pay and staff information
        if payslip.employee_iban:
            repo.payroll_employees[normalize_iban(payslip.employee_iban)] = payslip.employee
        self.o.advance(item, Stage.ACQUIRED, evidence, agent="discovery", note="Payslip received.")
        self.o.advance(item, Stage.UNDERSTOOD, evidence, agent=self.name, note=" ".join(payslip.reasons()))
        if quality is Quality.RED:
            self.o.advance(item, Stage.CONFLICT, evidence, agent=self.name, note=" ".join(payslip.reasons()))
        self.log("read_payslip", subject_id=doc_id, evidence_ids=evidence,
                 values={"employee": payslip.employee, "period": str(payslip.period), "net": payslip.net,
                         "gross": payslip.gross, "employer_tax_id": payslip.employer_tax_id or ""},
                 response={"quality": quality.value})
        month = f"{payslip.period.name} {payslip.period.year}"
        self.o.activity(at, "collected", f"Collected {payslip.employee}'s payslip for {month}.", company,
                        amount=payslip.net, currency=payslip.currency, evidence_ids=evidence)
        report.document_ids.append(doc_id)
        report.message = (f"Got it. This is {payslip.employee}'s payslip for {month}. It does not add up, so it "
                          "can't prove the salary yet." if quality is Quality.RED else
                          f"Got it. This is {payslip.employee}'s payslip for {month}. I will match it with the "
                          "salary.")
        return True

    def reclassify(self) -> int:
        """Payments to an employee's account, decided before their payslip showed the account: salaries now."""
        repo = self.repo
        if not repo.payroll_employees:
            return 0
        engine = self.o.reconciliation.engine()
        moved = 0
        for rec in sorted(repo.transactions.values(), key=lambda r: r.id):
            if rec.decision is None or rec.document_ids or rec.private or repo.items[rec.item_id].is_done:
                continue
            if rec.decision.expectation is EvidenceExpectation.PAYROLL and rec.decision.quality is Quality.GREEN:
                continue
            if normalize_iban(rec.tx.counterparty_iban or "") not in repo.payroll_employees:
                continue
            decision = engine.classify(rec.tx)
            if decision.expectation is not EvidenceExpectation.PAYROLL:
                continue
            rec.decision = decision
            self.log("expect", subject_id=rec.id, evidence_ids=[rec.evidence_id],
                     values={"expectation": decision.expectation.value, "rule": decision.rule},
                     response={"quality": decision.quality.value, "reason": decision.reason})
            moved += 1
        return moved

    def payslips(self) -> list[DocumentRecord]:
        return sorted((d for d in self.repo.documents.values() if d.payslip is not None and not d.matched_tx_ids
                       and d.document.quality is not Quality.RED), key=lambda d: d.id)

    def prove(self) -> int:
        """Each open salary with exactly one payslip that proves it (same employee, month and net pay)."""
        repo = self.repo
        slips = self.payslips()
        if not slips:
            return 0
        moved = 0
        for rec in sorted(repo.transactions.values(), key=lambda r: (r.tx.booked_on, r.id)):
            if rec.decision is None or rec.decision.expectation is not EvidenceExpectation.PAYROLL:
                continue
            if rec.document_ids or rec.private or repo.items[rec.item_id].is_done:
                continue
            found = [(d, proof) for d in slips if not d.matched_tx_ids
                     and (proof := salary_proof(rec.tx, d.payslip, known_ibans=repo.payroll_employees)) is not None]
            if len(found) != 1:
                continue  # none yet, or two payslips fit: never a guess
            doc, proof = found[0]
            payslip = doc.payslip
            assert payslip is not None
            rec.document_ids = [doc.id]
            rec.match_why = (*proof.why, *payslip.reasons())
            rec.match_headline = proof.headline
            rec.likely_document_ids = []
            doc.matched_tx_ids = [rec.id]
            update: dict[str, Any] = {}
            if doc.document.quality is Quality.AMBER:
                update["quality"] = Quality.GREEN  # the bank confirms the net pay the payslip states
            if doc.document.entity_id is None and rec.tx.entity_id:
                update["entity_id"] = rec.tx.entity_id
            if update:
                doc.document = doc.document.model_copy(update=update)
            self.log("match", subject_id=rec.id, evidence_ids=[rec.evidence_id, *doc.evidence_ids],
                     values={"transactions": [rec.id], "documents": [doc.id]}, validations=list(proof.why),
                     response={"quality": doc.document.quality.value, "kind": "payslip"})
            moved += 1
        return moved

    def runner(self, company_id: str) -> str:
        """Who runs payroll for the company: what the owner said, else the accountant when there is one."""
        said = self.repo.payroll_by.get(company_id)
        if said in ("owner", "accountant"):
            return said if said == "owner" or self.repo.accountant_for(company_id) is not None else "owner"
        return "accountant" if self.repo.accountant_for(company_id) is not None else "owner"

    def missing(self, now: datetime) -> list[TxRecord]:
        today = now.astimezone(TZ).date()
        return [r for r in sorted(self.repo.transactions.values(), key=lambda r: (r.tx.booked_on, r.id))
                if r.decision is not None and r.decision.expectation is EvidenceExpectation.PAYROLL
                and not r.document_ids and not r.private and r.tx.amount < 0
                and not self.repo.items[r.item_id].is_done
                and (today - r.tx.booked_on).days >= CHASE_AFTER_DAYS]

    def request_missing(self, now: datetime) -> list[str]:
        """One email to the accountant per company and month for the payslips still missing, when the accountant
        runs payroll and the owner allowed routine messages to them (§25). Returns the outbox ids written."""
        repo = self.repo
        groups: dict[tuple[str, Month], list[TxRecord]] = {}
        for rec in self.missing(now):
            if rec.id in repo.payslip_requests or self.runner(rec.company_id) != "accountant":
                continue
            groups.setdefault((rec.company_id, Month.of(rec.tx.booked_on)), []).append(rec)
        written: list[str] = []
        for (company_id, month), recs in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1])):
            accountant = repo.accountant_for(company_id)
            if accountant is None:
                continue
            decision = authorize(ActionKind.ROUTINE_ACCOUNTANT_RESPONSE, repo.policy, ActionContext(
                tenant_id=repo.tenant_id, entity_id=company_id, subject_id=recs[0].id))
            self.log("authorize_payslip_request", subject_id=recs[0].id,
                     response={"allowed": decision.allowed_now, "reason": decision.reason_plain})
            if not decision.allowed_now:
                continue
            name = repo.company_name(company_id) or "the company"
            lines = [f"- {self.employee_name(r)}: {format_money(abs(r.tx.amount), r.tx.currency)} paid on "
                     f"{day_month(r.tx.booked_on, repo.today())}" for r in recs]
            body = (f"Hello {accountant.person},\n\nCould you please send the payslips for {name}'s salaries of "
                    f"{month.name} {month.year}? I need them to match these payments:\n" + "\n".join(lines)
                    + f"\n\nThank you,\n{repo.owner.full_name}\n")
            out = self.o.write_email("payslip_request", recs[0].id, company_id, accountant.email,
                                     f"Payslips for {name}, {month.name} {month.year}", body, now)
            for r in recs:
                repo.payslip_requests[r.id] = out.id
            self.log("write_request", subject_id=recs[0].id, evidence_ids=[r.evidence_id for r in recs],
                     values={"to": accountant.email, "outbox_id": out.id, "salaries": len(recs)})
            written.append(out.id)
        return written

    def employee_name(self, rec: TxRecord) -> str:
        """Who a salary was paid to, as a person's name: from the IBAN of a payslip on file, else the bank line."""
        known = self.repo.payroll_employees.get(normalize_iban(rec.tx.counterparty_iban or ""))
        return known or person_name(self.o.merchant_name(rec.tx))

    def plan(self, rec: TxRecord, amount: str, who: str, when: str) -> str:
        """Plain words for a salary still waiting for its payslip."""
        who = self.employee_name(rec)
        month = Month.of(rec.tx.booked_on).name
        base = f"The {amount} salary to {who} on {when} needs {who}'s payslip for {month}."
        company = rec.company_id
        accountant = self.repo.accountant_for(company)
        message = self.repo.outbox.get(self.repo.payslip_requests.get(rec.id, ""))
        if message is not None and accountant is not None:
            if message.sent:
                return f"{base} I asked {accountant.firm} for it."
            return f"{base} I wrote to {accountant.firm} asking for it. It is waiting to be sent."
        if self.runner(company) == "accountant" and accountant is not None:
            return f"{base} Your accountant runs payroll: I will ask {accountant.firm} for it."
        return f"{base} Please send it to me, and I will match it."


def fold_name(name: str | None) -> str:
    """A person's name as compared between two payslips."""
    return counterparty_key(name) or ""


_REPORT_FORMATS = frozenset({EvidenceFormat.CSV, EvidenceFormat.JSON})
_COMMISSION_DOCS = frozenset({DocumentType.INVOICE, DocumentType.INVOICE_RECEIPT, DocumentType.SIMPLIFIED_INVOICE})
_MONTH_NAMES = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
                "November", "December")


class SettlementAgent(_Agent):
    """Payouts from card terminals and payment / sales platforms (§20, §21).

    A payout into the bank is the provider's net settlement, not customer revenue:
    its evidence is the provider's payout report. This agent reads such reports
    (structured CSV / JSON, never OCR), refuses any that do not add up to the
    cent (one plain question), pairs each with its bank payout (same provider,
    currency and reference or days; the net must equal the bank amount to the
    cent) and matches the provider's commission invoice to the fees the payouts
    kept. Only a payout proven this way closes, with the report as evidence (§3).
    """

    name = "settlement"

    # ------------------------------------------------------------------ reading

    def accept(self, evidence: Evidence, *, at: datetime, origin: str, report: IngestReport, hint: str = "") -> bool:
        """Read ``evidence`` as a payout report. False when it is not one (the caller reads it as usual)."""
        if evidence.format not in _REPORT_FORMATS:
            return False
        data = self.repo.registry.open(self.repo.tenant_id, evidence.id)
        try:
            found = parse_settlement_reports(data, source=evidence.id, filename=evidence.filename, hint=hint)
        except SettlementReportError as exc:
            report.stored_only = True
            report.message = f"Got it. I stored it. It looks like a payout report, but {_lower_first(str(exc))}"
            self.log("report_unreadable", subject_id=evidence.id, evidence_ids=[evidence.id],
                     response={"reason": str(exc)})
            return True
        except Exception as exc:  # a reader bug must never lose the upload: it is read like any other file
            self.log("report_read_failed", subject_id=evidence.id, evidence_ids=[evidence.id],
                     response={"error": type(exc).__name__})
            return False
        if not found:
            return False
        for settlement in found:
            self.record(settlement, evidence, at=at, origin=origin, report=report)
        return True

    def record(self, found: SettlementReport, evidence: Evidence, *, at: datetime, origin: str,
               report: IngestReport) -> DocumentRecord:
        repo = self.repo
        same = self._same_payout(found)
        if same is not None:  # the same payout again (another copy, or CSV and JSON): one record, more evidence
            doc = repo.documents[same.document_id]
            report.document_ids.append(doc.id)
            if evidence.id in doc.evidence_ids:
                report.message, report.already_known = "Got it. I already had this one.", True
                return doc
            doc.evidence_ids = [*doc.evidence_ids, evidence.id]
            for name, obs in found.observations.items():
                doc.observations.setdefault(name, []).extend(obs)
            doc.document = doc.document.model_copy(update={"evidence_ids": doc.evidence_ids})
            report.message, report.already_known = "Got it. I already had this payout report.", True
            self.log("merge_report", subject_id=doc.id, evidence_ids=[evidence.id])
            return doc
        seed = f"{evidence.id}|{found.provider.key}|{found.payout_id}|{found.payout_date}|{found.currency}|{found.net}"
        doc_id = "doc_" + hashlib.sha256(seed.encode()).hexdigest()[:16]
        if doc_id in repo.documents:
            report.document_ids.append(doc_id)
            report.message, report.already_known = "Got it. I already had this one.", True
            return repo.documents[doc_id]
        quality = Quality.RED if not found.adds_up else (Quality.GREEN if found.net_stated else Quality.AMBER)
        company = self._company_hint(found)
        document = Document(
            id=doc_id, tenant_id=repo.tenant_id, evidence_ids=[evidence.id], doc_type=DocumentType.PAYOUT_REPORT,
            supplier_name=found.provider.title, invoice_number=found.payout_id,
            payment_reference=found.payout_id, issue_date=found.payout_date, currency=found.currency,
            gross_amount=found.net, quality=quality,
        )
        item = TrackedItem(id="item_" + doc_id, tenant_id=repo.tenant_id, subject_type="document", subject_id=doc_id)
        repo.items[item.id] = item
        record = DocumentRecord(
            document=document, evidence_ids=[evidence.id], origin=origin, received_at=at, item_id=item.id,
            observations={name: list(obs) for name, obs in found.observations.items()},
            reasons=() if found.adds_up else (found.mismatch_sentence(),), checks=self._checks(found),
            country=repo.company_country(company),
        )
        repo.documents[doc_id] = record
        settlement = SettlementRecord(document_id=doc_id, report=found,
                                      status="waiting" if found.adds_up else "does_not_add_up",
                                      problem="" if found.adds_up else "does_not_add_up")
        repo.settlements[doc_id] = settlement
        self.o.advance(item, Stage.ACQUIRED, [evidence.id], agent="discovery", note="Payout report received.")
        self.o.advance(item, Stage.UNDERSTOOD, [evidence.id], agent=self.name,
                       note="Read the sales, fees, refunds and the amount paid out.")
        self.log("read_report", subject_id=doc_id, evidence_ids=[evidence.id], parser=found.format,
                 values={"provider": found.provider.key, "payout_id": found.payout_id,
                         "payout_date": found.payout_date, "currency": found.currency,
                         "gross_sales": found.gross_sales, "fees": found.fees, "refunds": found.refunds,
                         "chargebacks": found.chargebacks, "adjustments": found.adjustments, "net": found.net,
                         "lines": len(found.lines)},
                 validations=[{"check": "adds_up", "ok": found.adds_up, "difference": found.difference,
                               "rows_that_do_not_add_up": len(found.rows_that_do_not_add_up)}],
                 response={"quality": quality.value})
        label = found.provider.label
        on = f" on {day_month(found.payout_date, repo.today())}" if found.payout_date else ""
        self.o.activity(at, "collected", f"Collected the payout report from {label}: "
                        f"{found.money(found.net)} paid out{on}.", company, amount=found.net,
                        currency=found.currency, evidence_ids=[evidence.id])
        report.document_ids.append(doc_id)
        if not found.adds_up:
            self.o.advance(item, Stage.CONFLICT, [evidence.id], agent=self.name, note=found.mismatch_sentence())
            needs = self._ask(
                record, found, company, at,
                prompt=f"The payout report from {label} does not add up: {found.mismatch_sentence()} "
                       "What should I do?",
                why=(*found.breakdown(), "Until you answer, I won't count or close this payout."),
                option=f"Set it aside. I'll get a corrected report from {label}.")
            report.message = f"Got it. I need one answer from you: {needs.prompt}"
        return record

    def _same_payout(self, found: SettlementReport) -> SettlementRecord | None:
        if not found.payout_id or not found.adds_up:
            return None
        return next((s for s in self.repo.settlements.values()
                     if s.report.identity == found.identity and s.report.adds_up
                     and s.status in ("waiting", "settled")), None)

    def _company_hint(self, found: SettlementReport) -> str | None:
        """Whose payout this is, before it is paired: the payouts that could be it, or the only company."""
        repo = self.repo
        companies = {r.company_id for r in repo.transactions.values()
                     if r.decision is not None and r.decision.expectation is EvidenceExpectation.PAYOUT_REPORT
                     and (p := payout_provider(r.tx)) is not None and compatible_providers(p, found.provider)}
        if len(companies) == 1:
            return companies.pop()
        return next(iter(repo.companies)) if len(repo.companies) == 1 else None

    def _checks(self, found: SettlementReport, bank: FieldObservation | None = None) -> dict[str, VerifiedField]:
        """Each figure with the observations behind it (§18): GREEN when the report adds up."""
        checks: dict[str, VerifiedField] = {}
        amounts = {"gross_sales", "fees", "refunds", "chargebacks", "adjustments", "net_amount"}
        for name, obs in found.observations.items():
            observed = [*obs, *([bank] if bank is not None and name == "net_amount" else [])]
            stated = next((o for o in observed if o.method is not ExtractionMethod.ARITHMETIC), observed[0])
            if name in amounts and not found.adds_up:
                quality, reasons = Quality.RED, [found.mismatch_sentence()]
            elif name == "net_amount" and not found.net_stated and bank is None:
                quality, reasons = Quality.AMBER, ["The report does not state the payout; the bank will confirm it."]
            else:
                quality, reasons = Quality.GREEN, []
            checks[name] = VerifiedField(name=name, value=stated.value, quality=quality, observations=observed,
                                         reasons=reasons)
        return checks

    def _ask(self, record: DocumentRecord, found: SettlementReport, company: str | None, at: datetime, *,
             prompt: str, why: Sequence[str], option: str) -> NeedsYouRecord:
        """One plain question about a payout report; nothing is counted or closed meanwhile (§19, §37)."""
        repo = self.repo
        company = company or record.document.entity_id or next(iter(repo.companies))
        needs_id = _unique_id(repo.needs, f"nd_{found.provider.key}_payout")
        needs = NeedsYouRecord(id=needs_id, kind="check", subject_type="document", subject_id=record.id,
                               item_id=record.item_id, company_id=company, created_at=at,
                               why=tuple(dict.fromkeys(why)), prompt=prompt,
                               options=(CheckOption(id="neither", label=option),))
        repo.needs[needs_id] = needs
        self.log("ask_owner", subject_id=record.id, evidence_ids=record.evidence_ids,
                 values={"prompt": prompt}, response={"needs_you": needs_id})
        label = found.provider.label
        if found.adds_up:
            line = f"The payout report from {label} does not match what arrived in your bank. I asked you about it."
        else:
            line = f"The payout report from {label} does not add up. I asked you what to do."
        self.o.activity(at, "checked", line, company, evidence_ids=record.evidence_ids)
        return needs

    # ------------------------------------------------------------------ pairing (every run)

    def settle(self, now: datetime) -> int:
        """Pair waiting reports with bank payouts; match commission invoices to the fees kept."""
        repo = self.repo
        waiting = {doc_id: s.report for doc_id, s in sorted(repo.settlements.items())
                   if s.status == "waiting" and not repo.items[repo.documents[doc_id].item_id].is_done}
        payouts = []
        for rec in sorted(repo.transactions.values(), key=lambda r: r.id):
            if rec.private or rec.document_ids or rec.decision is None or repo.items[rec.item_id].is_done:
                continue
            if rec.decision.expectation is not EvidenceExpectation.PAYOUT_REPORT:
                continue
            provider = payout_provider(rec.tx) or provider_named(rec.tx.counterparty, rec.tx.description)
            if provider is not None:
                payouts.append(PayoutCandidate(rec.tx, provider))
        moved = 0
        if waiting and payouts:
            candidates = {c.transaction.id: c for c in payouts}
            for decision in match_payouts(payouts, waiting):
                moved += self._apply(decision, candidates[decision.transaction_id], now)
        self._link_unusable(payouts)
        return moved + self._commissions(now)

    def _link_unusable(self, payouts: Sequence[PayoutCandidate]) -> None:
        """A report that does not add up names its payout: that payout says so instead of asking for a report."""
        repo = self.repo
        for doc_id, s in sorted(repo.settlements.items()):
            if s.problem != "does_not_add_up" or s.transaction_id or s.status not in ("does_not_add_up", "set_aside"):
                continue
            found = likely_payouts(s.report, [c for c in payouts if not repo.transactions[c.transaction.id].document_ids])
            if len(found) != 1:
                continue
            rec, doc = repo.transactions[found[0]], repo.documents[doc_id]
            s.transaction_id = rec.id
            rec.likely_document_ids = sorted({*rec.likely_document_ids, doc_id})
            if doc.document.entity_id is None:
                doc.document = doc.document.model_copy(update={"entity_id": rec.company_id})
            self.log("link_report", subject_id=doc_id, evidence_ids=[rec.evidence_id, *doc.evidence_ids],
                     values={"transaction": rec.id}, response={"quality": Quality.RED.value})

    def _apply(self, decision: PayoutDecision, candidate: PayoutCandidate, now: datetime) -> int:
        repo = self.repo
        rec = repo.transactions[decision.transaction_id]
        doc = repo.documents[decision.report_id]
        settlement = repo.settlements[decision.report_id]
        found = settlement.report
        label = provider_label(found, candidate)
        evidence = [rec.evidence_id, *doc.evidence_ids]
        company = rec.company_id
        if decision.outcome is PayoutOutcome.AMBIGUOUS:  # never a silent pick: every candidate stays open
            reports = sorted({decision.report_id, *(r for r, _ in decision.alternatives)})
            for tx_id in sorted({rec.id, *(t for _, t in decision.alternatives)}):
                other = repo.transactions[tx_id]
                if other.likely_document_ids != reports:
                    other.likely_document_ids = reports
                    self.log("payout_ambiguous", subject_id=tx_id, evidence_ids=[other.evidence_id],
                             validations=list(decision.why), values={"reports": reports},
                             response={"quality": decision.quality.value})
            return 0
        if doc.document.entity_id is None:
            doc.document = doc.document.model_copy(update={"entity_id": company})
        settlement.transaction_id = rec.id
        settlement.headline = decision.headline
        if decision.outcome is PayoutOutcome.AMOUNT_DIFFERS:
            settlement.status, settlement.problem = "conflict", "bank_differs"
            rec.likely_document_ids = [doc.id]
            self.log("payout_differs", subject_id=doc.id, evidence_ids=evidence, validations=list(decision.why),
                     values={"transaction": rec.id, "report_net": found.net, "bank": rec.tx.amount},
                     response={"quality": decision.quality.value, "kind": decision.kind.value})
            self.o.advance(repo.items[doc.item_id], Stage.CONFLICT, evidence, agent=self.name,
                           note=decision.headline)
            self._ask(doc, found, company, now, prompt=f"{decision.headline} What should I do?", why=decision.why,
                      option=f"Set it aside. I'll ask {label} about the difference.")
            return 1
        # Settled: the report adds up and the bank confirms its net, to the cent (§3).
        bank = FieldObservation(value=rec.tx.amount, source=rec.evidence_id, method=ExtractionMethod.BANK,
                                confidence=0.99, location="bank: the payout as booked")
        doc.observations.setdefault("net_amount", []).append(bank)
        doc.checks = self._checks(found, bank)
        doc.document = doc.document.model_copy(update={"quality": Quality.GREEN})
        doc.matched_tx_ids = [rec.id]
        rec.document_ids = [doc.id]
        rec.match_why = decision.why
        rec.match_headline = decision.headline
        rec.likely_document_ids = []
        settlement.status = "settled"
        self.log("settle_payout", subject_id=doc.id, evidence_ids=evidence, validations=list(decision.why),
                 values={"transaction": rec.id, "gross_sales": found.gross_sales, "fees": found.fees,
                         "refunds": found.refunds, "chargebacks": found.chargebacks,
                         "adjustments": found.adjustments, "net": found.net, "bank": rec.tx.amount},
                 response={"quality": decision.quality.value, "kind": MatchKind.PAYOUT_SETTLEMENT.value,
                           "by_reference": decision.by_reference})
        kept = [f"{found.money(found.fees)} in {found.provider.fee_word}"] if found.fees else []
        if found.refunds:
            kept.append(f"{found.money(found.refunds)} refunded")
        if found.chargebacks:
            kept.append(f"{found.money(found.chargebacks)} in disputed payments")
        tail = f", {_join_and(kept)}" if kept else ""
        self.o.activity(now, "checked", f"Matched the {found.money(rec.tx.amount)} payout from {label} to its "
                        f"report: {found.money(found.gross_sales)} in sales{tail}.", company,
                        amount=rec.tx.amount, currency=found.currency, evidence_ids=evidence)
        self._retire_replaced(settlement, rec, now)
        return 1

    def _retire_replaced(self, settled: SettlementRecord, rec: TxRecord, now: datetime) -> None:
        """Reports a corrected one replaced: dismissed with the settled report as evidence."""
        repo = self.repo
        key = (settled.report.payout_id or "").strip().upper()
        doc = repo.documents[settled.document_id]
        for other in sorted(repo.settlements.values(), key=lambda s: s.document_id):
            if other is settled or other.status not in ("does_not_add_up", "conflict", "set_aside"):
                continue
            same_id = bool(key) and (other.report.payout_id or "").strip().upper() == key and \
                compatible_providers(other.report.provider, settled.report.provider)
            if not same_id and other.transaction_id != rec.id:
                continue
            other.status = "replaced"
            old = repo.documents[other.document_id]
            self.o.advance(repo.items[old.item_id], Stage.NOT_REQUIRED, [*doc.evidence_ids, rec.evidence_id],
                           agent=self.name, quality=Quality.GREEN, note="Replaced by a corrected payout report.")
            for needs in repo.needs.values():
                if needs.subject_id == old.id and needs.status == "open":
                    needs.status, needs.resolution = "resolved", "evidence"
            self.log("replace_report", subject_id=old.id, evidence_ids=[*doc.evidence_ids, rec.evidence_id])

    def _commissions(self, now: datetime) -> int:
        """The provider's commission invoice, matched to the fees its payouts kept (never to a bank payment)."""
        repo = self.repo
        settled = [s for s in sorted(repo.settlements.values(), key=lambda s: s.document_id) if s.settled]
        if not settled:
            return 0
        moved = 0
        for s in settled:  # an invoice matched earlier that has since been verified closes now
            for doc_id in s.commission_document_ids:
                doc = repo.documents[doc_id]
                if doc.document.quality is Quality.GREEN and not repo.items[doc.item_id].is_done:
                    moved += self._close_commission(doc, [s])
        for doc in sorted(repo.documents.values(), key=lambda d: d.id):
            d = doc.document
            if d.doc_type not in _COMMISSION_DOCS or doc.on_hold or doc.matched_tx_ids or d.gross_amount is None:
                continue
            if d.quality is Quality.RED or repo.items[doc.item_id].is_done:
                continue
            supplier = repo.suppliers.get(doc.supplier_id or "")
            provider = provider_named(d.supplier_name, *(([supplier.name, *supplier.aliases]) if supplier else ()))
            if provider is None:
                continue
            chosen = self._fees_for(d, provider, settled)
            if chosen is None:
                continue
            moved += self._attach_commission(doc, chosen, now)
        return moved

    def _fees_for(self, d: Document, provider: PayoutProvider,
                  settled: Sequence[SettlementRecord]) -> list[SettlementRecord] | None:
        """One payout whose fees are the invoice total, else one month of payouts whose fees add up to it."""
        same = [s for s in settled if compatible_providers(s.report.provider, provider) and s.report.fees > 0
                and s.report.currency == (d.currency or "EUR").upper() and not s.commission_document_ids]
        exact = [s for s in same if s.report.fees == d.gross_amount]
        if len(exact) == 1:
            return exact
        if exact or d.issue_date is None:
            return None  # two payouts fit equally well: never a silent pick
        issued = d.issue_date
        before = (issued.year - (issued.month == 1), 12 if issued.month == 1 else issued.month - 1)
        fits = []
        for year, month in (before, (issued.year, issued.month)):
            group = [s for s in same if s.report.payout_date is not None
                     and (s.report.payout_date.year, s.report.payout_date.month) == (year, month)]
            if len(group) > 1 and sum((s.report.fees for s in group), _ZERO) == d.gross_amount:
                fits.append(group)
        return fits[0] if len(fits) == 1 else None

    def _attach_commission(self, doc: DocumentRecord, chosen: list[SettlementRecord], now: datetime) -> int:
        repo = self.repo
        reports = [repo.documents[s.document_id] for s in chosen]
        report_evidence = [e for r in reports for e in r.evidence_ids]
        fees = sum((s.report.fees for s in chosen), _ZERO)
        provider = chosen[0].report.provider
        where = ("payout report: the " + provider.fee_word + " taken from the payout" if len(chosen) == 1 else
                 f"payout reports: the {provider.fee_word} taken from {len(chosen)} payouts")
        doc.observations.setdefault("gross_amount", []).append(FieldObservation(
            value=fees, source=report_evidence[0], method=ExtractionMethod.API, confidence=0.95, location=where))
        assessment = self.o.verification.assess(doc.observations, doc.document.doc_type, subject_id=doc.id,
                                                evidence_ids=[*doc.evidence_ids, *report_evidence],
                                                owner=doc.owner_values, issuer=doc.issuer, country=doc.country)
        self.o._apply_assessment(doc, assessment)
        tx_ids = [s.transaction_id for s in chosen if s.transaction_id]
        doc.matched_tx_ids = list(tx_ids)
        company = repo.transactions[tx_ids[0]].company_id if tx_ids else None
        if doc.document.entity_id is None and company:
            doc.document = doc.document.model_copy(update={"entity_id": company})
        for s in chosen:
            s.commission_document_ids.append(doc.id)
        self.log("match_commission_invoice", subject_id=doc.id, evidence_ids=[*doc.evidence_ids, *report_evidence],
                 values={"payouts": tx_ids, "fees": fees}, response={"quality": doc.document.quality.value})
        who = provider.title
        noun = "payout" if len(chosen) == 1 else "payouts"
        self.o.activity(now, "checked", f"Matched the {who} invoice to the {provider.fee_word} taken from your "
                        f"{noun}. Nothing to pay: it was already kept from the {noun}.", company,
                        amount=fees, currency=doc.document.currency, evidence_ids=[*doc.evidence_ids, *report_evidence])
        return 1 + (self._close_commission(doc, chosen) if doc.document.quality is Quality.GREEN else 0)

    def _close_commission(self, doc: DocumentRecord, chosen: Sequence[SettlementRecord]) -> int:
        repo = self.repo
        evidence = [*doc.evidence_ids]
        for s in chosen:
            evidence += repo.documents[s.document_id].evidence_ids
            if s.transaction_id:
                evidence.append(repo.transactions[s.transaction_id].evidence_id)
        word = chosen[0].report.provider.fee_word
        return self.o.closure._close(repo.items[doc.item_id], list(dict.fromkeys(evidence)),
                                     note=f"Its total matches the {word} taken from the payouts.")

    # ------------------------------------------------------------------ plain words

    def plan(self, rec: TxRecord, amount: str, when: str) -> str:
        """The next step for a payout still without its report (§22), in plain words."""
        mine = [s for s in self.repo.settlements.values() if s.transaction_id == rec.id]
        provider = payout_provider(rec.tx) or provider_named(rec.tx.counterparty, rec.tx.description)
        label = provider_label(mine[0].report) if mine else (provider.label if provider else "the provider")
        for s in mine:
            if s.status not in ("conflict", "does_not_add_up", "set_aside"):
                continue
            problem = ("came with a payout report that does not add up" if s.problem == "does_not_add_up"
                       else "does not match its payout report")
            next_step = ("I'm waiting for a corrected one." if s.status == "set_aside" else
                         "I asked you what to do." if s.problem == "does_not_add_up" else "I asked you about it.")
            return f"The {amount} payout from {label} on {when} {problem}. {next_step}"
        if rec.likely_document_ids:
            return (f"The {amount} payout from {label} on {when} and its payout report could be paired more than "
                    "one way. I won't pair them on a guess.")
        return (f"The {amount} payout from {label} on {when} needs its payout report, so I can count the sales, "
                f"{provider.fee_word if provider else 'fees'} and refunds behind it. Upload the report here or "
                "forward the email it came in.")


def _lower_first(text: str) -> str:
    return text[:1].lower() + text[1:] if text else text


def _join_and(items: Sequence[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


_TAX_ISSUERS = frozenset({Issuer.TAX_AUTHORITY.value, Issuer.SOCIAL_SECURITY.value})
# How a letter about each kind of obligation reads in the Activity feed (§42, §69). The tax office's
# payment letters keep "Read a letter about tax payment.".
_OBLIGATION_ARRIVED: dict[ObligationKind, str] = {
    ObligationKind.KYC_REQUEST: "Read a message from your bank asking for updated details.",
    ObligationKind.BANK_REQUEST: "Read a request from your bank.",
    ObligationKind.INSURANCE_RENEWAL: "Read a letter about an insurance renewal.",
    ObligationKind.CONTRACT_RENEWAL: "Read a letter about a contract renewal.",
    ObligationKind.LICENSE_RENEWAL: "Read a letter about a licence renewal.",
    ObligationKind.RENT: "Read a letter about a rent payment.",
    ObligationKind.DEBT_COLLECTION: "Read a debt collection letter.",
    ObligationKind.PAYMENT_DEADLINE: "Read a letter about a payment due.",
    ObligationKind.FILING: "Read a letter about a return to file.",
    ObligationKind.TOURIST_TAX: "Read a letter about the tourist tax to pay.",
    ObligationKind.TOURIST_TAX_DECLARATION: "Read a letter about the tourist tax declaration.",
    ObligationKind.GRANT_DOCUMENTS: "Read a letter about your grant: documents to send.",
    ObligationKind.GRANT_PAYMENT: "Read a letter about a grant payment to receive.",
    ObligationKind.VAT_RETURN: "Read a letter about the VAT return.",
}
_RENEWED_THING = {ObligationKind.INSURANCE_RENEWAL: "policy", ObligationKind.LICENSE_RENEWAL: "licence",
                  ObligationKind.CONTRACT_RENEWAL: "contract"}
_PAYMENT_LETTER = {ObligationKind.RENT: "rent letter", ObligationKind.DEBT_COLLECTION: "debt collection letter",
                   ObligationKind.PAYMENT_DEADLINE: "payment letter", ObligationKind.TAX_DEADLINE: "tax letter",
                   ObligationKind.TOURIST_TAX: "tourist tax letter", ObligationKind.GRANT_PAYMENT: "grant letter"}
# Payments that never pay a letter: money moved between your own accounts, a card paid off.
_NOT_A_LETTER_PAYMENT = frozenset({EvidenceExpectation.NONE_INTERNAL_TRANSFER, EvidenceExpectation.CARD_STATEMENT})


class ObligationAgent(_Agent):
    """Letters and messages become obligations (§24); each is done only by its required proof (§3).

    What is to be paid is proven by the payment: the tax office's letters by the tax payment (amount
    and reference); rent, debts and other payments due by a payment of the right amount that carries
    the letter's reference or goes to whoever wrote it. A payment of the right amount to someone the
    bank line does not identify is one plain question, never a guess. Everything else (answering a
    request, filing, renewing) is proven by a confirming letter or message that fits exactly one open
    obligation, or by the owner's explicit confirmation, stored as evidence. A renewal the letter says
    happens on its own is shown for information and never holds a month open.
    """

    name = "obligation"

    # ------------------------------------------------------------------ each company's country (§49)

    def vocabulary(self) -> dict[str, tuple[str, ...]] | None:
        """The letter wording of the business's companies' countries (their packs, §49), added to the core's
        English; None when their packs add nothing (the letter is then read in its company's country's)."""
        return pack_vocabulary(self.repo.countries()) or None

    def calendar(self, now: datetime) -> list[ObligationRecord]:
        """Obligations a company's country sets by the calendar, with no letter (Spain's quarterly VAT return,
        modelo 303): each once per company and period, from its own country's pack. Nothing for a country
        whose deadlines arrive by letter (Portugal)."""
        repo = self.repo
        today = now.astimezone(TZ).date()
        made: list[ObligationRecord] = []
        for company_id, entity in sorted(repo.companies.items()):
            for periodic in company_pack(entity.country).periodic_obligations(company_id, today):
                oid = "obl_" + hashlib.sha256(periodic.key.encode("utf-8")).hexdigest()[:16]
                if oid in repo.obligations:
                    continue
                body = json.dumps({"kind": "calendar", "country": entity.country, "obligation": periodic.kind,
                                   "period": periodic.period, "company": company_id,
                                   "due_on": periodic.due_on.isoformat(), "title": periodic.title},
                                  sort_keys=True).encode()
                reg = repo.registry.register(body, tenant_id=repo.tenant_id, source_kind=SourceKind.GOVERNMENT,
                                             format=EvidenceFormat.JSON, mime_type="application/json",
                                             retrieved_at=now, metadata={"kind": "country_calendar"})
                condition = VerificationCondition(proof=proof_for(ObligationKind(periodic.kind)), by=periodic.due_on,
                                                  since=today)
                obligation = Obligation(
                    id=oid, tenant_id=repo.tenant_id, entity_id=company_id, kind=ObligationKind(periodic.kind),
                    title=periodic.title, due_on=periodic.due_on, responsible=periodic.responsible,
                    consequence=periodic.consequence, required_evidence=periodic.required_evidence,
                    verification_condition=condition.encode())
                record = ObligationRecord(
                    obligation=obligation, evidence_id=reg.evidence.id, title=periodic.title,
                    reasons=(*periodic.reasons, f"Due {day_month(periodic.due_on, today)}"),
                    reference=None, issuer=Issuer.TAX_AUTHORITY.value, received_on=today)
                repo.obligations[oid] = record
                self.log("calendar_obligation", subject_id=oid, evidence_ids=[reg.evidence.id],
                         values={"kind": periodic.kind, "period": periodic.period, "due_on": periodic.due_on,
                                 "entity_id": company_id, "country": entity.country})
                self.o.activity(now, "collected", f"Added the deadline for the {periodic.title[0].lower()}"
                                f"{periodic.title[1:]}: {day_month(periodic.due_on, today)}.", company_id,
                                evidence_ids=[reg.evidence.id])
                made.append(record)
        return made

    # ------------------------------------------------------------------ letters that ask for something

    def detect(self, text: str, evidence_id: str, *, received_on: date,
               sender: str = "") -> ObligationRecord | PendingObligation | None:
        """The obligation a letter or message creates; a pending one when it names none of your companies."""
        finding = detect_obligation(
            text, tenant_id=self.repo.tenant_id, received_on=received_on, sender=sender,
            entities=self.repo.entities, vocabulary=self.vocabulary(),
        )
        if finding is None:
            return None
        tax = finding.issuer.value in _TAX_ISSUERS
        if not tax and _reads_as_accounting_document(text):
            return None  # an invoice or receipt with a due date: its payment proves it (§20), not a letter
        oid = "obl_" + evidence_id[3:19]
        if oid in self.repo.obligations:  # the same letter again
            return self.repo.obligations[oid]
        if oid in self.repo.pending_obligations:  # already asked about (or set aside by the owner)
            return self.repo.pending_obligations[oid]
        if finding.obligation is None:
            if finding.due_on is not None and finding.entity_id is None and self.repo.companies:
                return self._ask_which_company(finding, evidence_id, received_on=received_on, sender=sender,
                                               text=text)
            self.log("obligation_incomplete", evidence_ids=[evidence_id], values={"title": finding.title})
            return None
        return self.record(finding, evidence_id, received_on=received_on, sender=sender, text=text)

    def record(self, finding: Any, evidence_id: str, *, received_on: date, sender: str, text: str,
               entity_id: str | None = None) -> ObligationRecord:
        """Keep one obligation (once: the same letter again, or the same deadline from another channel, is
        the one already on file)."""
        repo = self.repo
        oid = "obl_" + evidence_id[3:19]
        if oid in repo.obligations:
            return repo.obligations[oid]
        base = finding.obligation or Obligation(
            tenant_id=repo.tenant_id, entity_id=entity_id, kind=finding.kind, title=finding.title,
            due_on=finding.due_on, amount=finding.amount, responsible=finding.responsible,
            consequence=finding.consequence, required_evidence=finding.required_evidence,
            verification_condition=finding.condition.encode())
        obligation = base.model_copy(update={"id": oid})
        # A periodic VAT return is one per company and deadline: a letter about it is the one on file.
        periodic = obligation.kind is ObligationKind.VAT_RETURN
        same = next((o for o in sorted(repo.obligations.values(), key=lambda o: o.obligation.id)
                     if not o.done and o.obligation.entity_id == obligation.entity_id
                     and o.obligation.kind is obligation.kind and o.obligation.due_on == obligation.due_on
                     and (periodic or (o.obligation.amount == obligation.amount
                                       and o.reference == finding.reference))), None)
        if same is not None:
            self.log("obligation_already_known", subject_id=same.obligation.id,
                     evidence_ids=[evidence_id, same.evidence_id])
            return same
        payee_supplier, ibans, key = self._payee(text, sender)
        record = ObligationRecord(
            obligation=obligation, evidence_id=evidence_id, title=finding.title, reasons=tuple(finding.reasons),
            reference=finding.reference, issuer=finding.issuer.value, received_on=received_on, sender=sender,
            payee_supplier_id=payee_supplier, payee_ibans=ibans, payee_key=key,
            informational=finding.renews_on_its_own, agency=getattr(finding, "agency", None))
        repo.obligations[oid] = record
        values: dict[str, Any] = {"kind": obligation.kind.value, "due_on": obligation.due_on,
                                  "amount": obligation.amount, "entity_id": obligation.entity_id}
        if finding.issuer.value not in _TAX_ISSUERS:
            values.update(issuer=finding.issuer.value, responsible=obligation.responsible,
                          proof=record.proof.value, informational=record.informational)
        self.log("detect_obligation", subject_id=oid, evidence_ids=[evidence_id], values=values,
                 response={"quality": finding.quality.value})
        return record

    def _payee(self, text: str, sender: str) -> tuple[str | None, tuple[str, ...], str | None]:
        """Who the letter asks to be paid: a known supplier it names (by tax number or the sender's email
        domain), the bank accounts it prints, and the sender's name as a bank line would show it."""
        repo = self.repo
        own = set(repo.own_tax_ids())
        supplier = next((s for s in sorted(repo.suppliers.values(), key=lambda s: s.id)
                         if s.tax_id and not any(same_tax_id(s.tax_id, t) for t in own)
                         and _names_tax_id(text, s.tax_id)), None)
        if supplier is None and "@" in sender:
            supplier = repo.supplier_for_domain(email_domain(sender))
        own_ibans = {normalize_iban(i) for e in repo.entities for i in e.own_ibans}
        ibans = tuple(dict.fromkeys(i for i in (normalize_iban(x) for x in find_ibans(text)) if i not in own_ibans))
        name = sender.split("<", 1)[0].strip().strip('"') if "<" in sender else ""
        return (supplier.id if supplier else None), ibans, (counterparty_key(name) if name else None)

    def _ask_which_company(self, finding: Any, evidence_id: str, *, received_on: date, sender: str,
                           text: str) -> PendingObligation:
        """A letter with a deadline that names none of your companies: one plain question (§37, §51)."""
        repo = self.repo
        pid = "obl_" + evidence_id[3:19]
        pending = PendingObligation(pid, finding, evidence_id, received_on, sender, text)
        repo.pending_obligations[pid] = pending
        now = repo.clock.now()
        options = [CheckOption(id=f"company:{e.id}", label=e.name, values={"company": e.id})
                   for e in sorted(repo.entities, key=lambda e: e.name)]
        options.append(CheckOption(id="none", label="None of them. It is personal or not for my companies."))
        due = day_month(finding.due_on, repo.today())
        why = [*(r for r in finding.reasons if not r.startswith("Due ")), f"Due {due}"]
        why.append("It does not name one of your companies, so I won't guess which one.")
        prompt = f"{finding.title}: which of your companies is this letter for?"
        needs_id = _unique_id(repo.needs, f"nd_{_slug(finding.title.split()[0])}_letter")
        repo.needs[needs_id] = NeedsYouRecord(
            id=needs_id, kind="obligation_company", subject_type="obligation", subject_id=pid, item_id="",
            company_id="", created_at=now, why=tuple(why), prompt=prompt, options=tuple(options))
        self.log("ask_owner", subject_id=pid, evidence_ids=[evidence_id],
                 values={"title": finding.title, "due_on": finding.due_on}, response={"needs_you": needs_id})
        self.o.activity(now, "collected", f"Read a letter about “{finding.title}”. It does not say which of your "
                        "companies it is for, so I asked you.", None, amount=finding.amount,
                        evidence_ids=[evidence_id])
        return pending

    # ------------------------------------------------------------------ letters that say it was done

    def confirm_from(self, text: str, evidence_id: str, *, received_on: date,
                     sender: str = "") -> ObligationRecord | None:
        """A letter or message saying something asked for was done ("Recebemos os seus documentos", "A sua
        licença foi renovada até ..."). It closes an obligation only when it fits exactly one open one of
        that kind, for that company, from the same sender, with the same reference (§3); else nothing."""
        finding = detect_confirmation(text, received_on=received_on, sender=sender, entities=self.repo.entities,
                                      vocabulary=self.vocabulary())
        if finding is None:
            return None
        domain = email_domain(sender) if "@" in sender else None
        domain = registrable_domain(domain) if domain else None
        reference = normalize_reference(finding.reference) if finding.reference else None
        candidates = [
            ob for ob in sorted(self.repo.obligations.values(), key=lambda o: o.obligation.id)
            if not ob.done and not ob.payable and ob.obligation.kind in finding.kinds
            and (finding.entity_id is None or ob.obligation.entity_id == finding.entity_id)
            and not (domain and ob.sender_domain and domain != ob.sender_domain)
            and not (finding.issuer is not Issuer.OTHER and ob.issuer not in ("", Issuer.OTHER.value)
                     and ob.issuer != finding.issuer.value)
            and not (reference and ob.reference and normalize_reference(ob.reference) != reference)
        ]
        self.log("read_confirmation", evidence_ids=[evidence_id],
                 values={"proof": finding.proof.value, "candidates": [ob.obligation.id for ob in candidates]},
                 validations=list(finding.reasons))
        if len(candidates) != 1:
            return None  # none on file, or more than one would fit: never closed on a guess
        ob = candidates[0]
        result = satisfy(ob.obligation, [finding.fact(evidence_id)])
        self.log("satisfy", subject_id=ob.obligation.id, evidence_ids=[ob.evidence_id, evidence_id],
                 response={"satisfied": result.satisfied, "quality": result.quality.value},
                 validations=list(result.reasons))
        if result.satisfied and result.quality is Quality.GREEN:
            self._done(ob, result, how=_confirmed_how(finding.proof, received_on, finding.valid_until))
        return ob

    # ------------------------------------------------------------------ payments

    def prove(self) -> list[TxRecord]:
        """Payments that prove an open obligation (§24). Tax payments close with the tax letter as their proof."""
        proven: list[TxRecord] = []
        for ob in sorted(self.repo.obligations.values(), key=lambda o: o.obligation.id):
            if ob.done or not ob.payable:
                continue
            if ob.obligation.kind in (ObligationKind.TAX_DEADLINE, ObligationKind.TOURIST_TAX):
                proven += self._prove_tax(ob)
            elif ob.obligation.kind is ObligationKind.GRANT_PAYMENT:
                proven += self._prove_grant(ob)
            else:
                self._prove_payment(ob)
        return proven

    @staticmethod
    def _payment_fact(r: TxRecord) -> EvidenceFact:
        return EvidenceFact(evidence_id=r.evidence_id, kind=ProofKind.PAYMENT, on=r.tx.booked_on,
                            quality=Quality.GREEN, amount=abs(r.tx.amount), currency=r.tx.currency,
                            reference=r.tx.reference)

    def _prove_tax(self, ob: ObligationRecord) -> list[TxRecord]:
        """Tax payments, and the tourist tax paid to the municipality (only by a payment its bank line calls
        tourist tax: never an ordinary tax payment of the same amount, X26)."""
        tourist = ob.obligation.kind is ObligationKind.TOURIST_TAX
        facts = [self._payment_fact(r) for r in self.repo.transactions.values()
                 if r.company_id == ob.obligation.entity_id and r.tx.amount < 0 and r.decision is not None
                 and r.decision.expectation.value == "tax_notice_or_proof"
                 and (r.decision.rule == "tourist_tax") == tourist]
        if not facts:
            return []
        result = satisfy(ob.obligation, sorted(facts, key=lambda f: f.evidence_id))
        self.log("satisfy", subject_id=ob.obligation.id, evidence_ids=[ob.evidence_id, *result.evidence_ids],
                 response={"satisfied": result.satisfied, "quality": result.quality.value},
                 validations=list(result.reasons))
        if not result.satisfied or result.quality is not Quality.GREEN:
            return []
        ob.obligation = result.obligation
        ob.satisfied_by = tuple(result.evidence_ids)
        ob.how = result.reasons[0]
        proven = []
        for rec in self.repo.transactions.values():
            if rec.evidence_id in result.evidence_ids:
                rec.proof_evidence_ids = [ob.evidence_id]
                if tourist:
                    rec.proof_note = "The payment matches the tourist tax letter's amount and reference."
                proven.append(rec)
        return proven

    # ------------------------------------------------------------------ grants (checklist X30)

    def grant_from(self, ob: ObligationRecord, rec: TxRecord) -> bool:
        """The bank line shows the money came from whoever wrote the grant letter: the agency it names, the
        letter's reference, the account it prints, or the sender's own name."""
        tx = rec.tx
        text = f"{tx.counterparty} {tx.description} {tx.reference or ''}"
        if ob.agency and grant_agency(text) == ob.agency:
            return True
        if ob.reference and ob.reference in normalize_reference(text):
            return True
        if ob.payee_ibans and tx.counterparty_iban and normalize_iban(tx.counterparty_iban) in ob.payee_ibans:
            return True
        return bool(ob.payee_key and counterparty_key(tx.counterparty) == ob.payee_key)

    def _prove_grant(self, ob: ObligationRecord) -> list[TxRecord]:
        """A grant payment announced by a letter is done only when the money arrives: the amount the letter
        gives, from the agency that wrote it. The right amount from someone the bank line does not identify
        is one plain question, never a guess. The payment is proven by the letter, recorded as a grant."""
        repo = self.repo
        candidates = sorted((r for r in repo.transactions.values()
                             if r.company_id == ob.obligation.entity_id and r.tx.amount > 0 and not r.private
                             and r.decision is not None and r.decision.expectation not in _NOT_A_LETTER_PAYMENT
                             and not r.document_ids and not r.proof_evidence_ids and r.id not in ob.declined_tx_ids
                             and not repo.items[r.item_id].is_done),
                            key=lambda r: (r.tx.booked_on, r.id))
        identified = [r for r in candidates if self.grant_from(ob, r)]
        if identified:
            result = satisfy(ob.obligation, [self._payment_fact(r) for r in identified])
            self.log("satisfy", subject_id=ob.obligation.id, evidence_ids=[ob.evidence_id, *result.evidence_ids],
                     response={"satisfied": result.satisfied, "quality": result.quality.value},
                     validations=list(result.reasons))
            if result.satisfied and result.quality is Quality.GREEN:
                proven = [r for r in identified if r.evidence_id in result.evidence_ids]
                self.grant_received(ob, proven, result)
                return proven
        if any(n.status == "open" and n.kind == "obligation" and n.options
               and n.options[0].values.get("obligation") == ob.obligation.id for n in repo.needs.values()):
            return []
        named = {r.id for r in identified}
        for rec in candidates:
            if rec.id in named or rec.tx.entity_id is None or rec.decision is None or rec.decision.rule != "grant":
                continue
            if satisfy(ob.obligation, [self._payment_fact(rec)]).satisfied:
                self.o.ask_about_obligation_payment(ob, rec)
                return []
        return []

    def grant_received(self, ob: ObligationRecord, recs: Sequence[TxRecord], result: Any, *,
                       answer_ev: str | None = None) -> None:
        """The grant arrived: the letter is done, and it is the proof of the money received (a grant, not a sale)."""
        last = max(r.tx.booked_on for r in recs)
        how = f"Received on {day_month(last)}." + (" You confirmed it." if answer_ev else "")
        self._done(ob, result, how=how, extra=(answer_ev,) if answer_ev else ())
        for rec in recs:
            rec.proof_evidence_ids = [ob.evidence_id, *([answer_ev] if answer_ev else [])]
            rec.proof_note = "The grant letter's amount matches the money received."
            if rec.decision is None or rec.decision.rule != "grant":  # the letter says what it is: a grant
                rec.decision = ExpectationDecision(
                    rec.tx.id, EvidenceExpectation.TAX_NOTICE_OR_PROOF,
                    "A grant or subsidy. The letter about it covers it: it is not a sale.", Quality.GREEN, "grant")
            self.log("grant_received", subject_id=rec.id, evidence_ids=[rec.evidence_id, *rec.proof_evidence_ids],
                     values={"obligation": ob.obligation.id, "amount": abs(rec.tx.amount), "agency": ob.agency})

    def proven_by(self, rec: TxRecord) -> ObligationRecord | None:
        """The letter a payment proves (a tax, tourist tax or grant letter), if any."""
        return next((o for o in sorted(self.repo.obligations.values(), key=lambda o: o.obligation.id)
                     if rec.evidence_id in o.satisfied_by), None)

    def letter_word(self, ob: ObligationRecord | None) -> str:
        """'Tax letter', 'Tourist tax letter', 'Grant letter'."""
        kind = ob.obligation.kind if ob is not None else ObligationKind.TAX_DEADLINE
        return {ObligationKind.TOURIST_TAX: "Tourist tax letter",
                ObligationKind.GRANT_PAYMENT: "Grant letter"}.get(kind, "Tax letter")

    def payment_step(self, ob: ObligationRecord) -> str:
        """What I do about a letter that is done by a payment."""
        if ob.obligation.kind is ObligationKind.GRANT_PAYMENT:
            return "I will check that the money arrives in your bank."
        return "I will check the payment when it goes out."

    def _candidates(self, ob: ObligationRecord) -> list[TxRecord]:
        return sorted((r for r in self.repo.transactions.values()
                       if r.company_id == ob.obligation.entity_id and r.tx.amount < 0 and not r.private
                       and r.decision is not None and r.decision.expectation not in _NOT_A_LETTER_PAYMENT
                       and r.id not in ob.declined_tx_ids),
                      key=lambda r: (r.tx.booked_on, r.id))

    def pays_issuer(self, ob: ObligationRecord, rec: TxRecord) -> bool:
        """The bank line shows the payment went to whoever wrote the letter: the account the letter prints,
        the supplier it names, or the sender's own name. A letter's reference is checked by :func:`satisfy`."""
        tx = rec.tx
        if ob.reference:
            return True
        if ob.payee_ibans and tx.counterparty_iban and normalize_iban(tx.counterparty_iban) in ob.payee_ibans:
            return True
        if ob.payee_supplier_id:
            found = self.repo.resolver().resolve_transaction(tx).supplier
            if found is not None and found.id == ob.payee_supplier_id:
                return True
        return bool(ob.payee_key and counterparty_key(tx.counterparty) == ob.payee_key)

    def _prove_payment(self, ob: ObligationRecord) -> None:
        """Rent, a debt or another payment due: proven by a payment of the right amount to whoever wrote
        (or carrying the letter's reference). The right amount to someone else is one plain question."""
        candidates = self._candidates(ob)
        identified = [r for r in candidates if self.pays_issuer(ob, r)]
        if identified:
            result = satisfy(ob.obligation, [self._payment_fact(r) for r in identified])
            self.log("satisfy", subject_id=ob.obligation.id, evidence_ids=[ob.evidence_id, *result.evidence_ids],
                     response={"satisfied": result.satisfied, "quality": result.quality.value},
                     validations=list(result.reasons))
            if result.satisfied and result.quality is Quality.GREEN:
                self._done(ob, result, how=" ".join(result.reasons))
                return
        if any(n.status == "open" and n.kind == "obligation" and n.options
               and n.options[0].values.get("obligation") == ob.obligation.id for n in self.repo.needs.values()):
            return
        named = {r.id for r in identified}
        for rec in candidates:
            if rec.id in named or rec.tx.entity_id is None:
                continue
            if satisfy(ob.obligation, [self._payment_fact(rec)]).satisfied:
                self.o.ask_about_obligation_payment(ob, rec)
                return

    def _done(self, ob: ObligationRecord, result: Any, *, how: str, extra: Sequence[str] = ()) -> None:
        """Record the proof (never removed later, §3) and say so once in the Activity feed."""
        merged = sorted({*result.obligation.satisfied_by_evidence_ids, *extra})
        ob.obligation = result.obligation.model_copy(update={"satisfied_by_evidence_ids": merged})
        ob.satisfied_by = tuple(dict.fromkeys([*result.evidence_ids, *extra]))
        ob.how = how
        self.log("obligation_done", subject_id=ob.obligation.id, evidence_ids=[ob.evidence_id, *ob.satisfied_by],
                 values={"how": how})
        self.o.activity(self.repo.clock.now(), "checked", f"{ob.title}: done. {how}", ob.obligation.entity_id,
                        amount=ob.obligation.amount, evidence_ids=list(ob.satisfied_by))

    # ------------------------------------------------------------------ plain words

    def arrived_line(self, ob: ObligationRecord) -> str:
        """The Activity line for a new letter (§42)."""
        kind = ob.obligation.kind
        if kind is ObligationKind.TAX_DEADLINE:
            return f"Read a letter about {ob.title.lower()}."
        if kind is ObligationKind.GOVERNMENT_REQUEST:
            title = _lower_first(ob.title)
            return f"Read {'an' if title[:1] in 'aeiou' else 'a'} {title}."
        line = _OBLIGATION_ARRIVED.get(kind, "Read a letter with a deadline.")
        if ob.informational:
            line += " It renews on its own."
        return line

    def next_step(self, ob: ObligationRecord) -> str:
        """What happens next, in plain words (Home "Due soon", the obligations list)."""
        kind = ob.obligation.kind
        if ob.done:
            return ob.how or "Done, with proof."
        if ob.informational:
            return "It renews on its own. Nothing to do unless you want to change or end it."
        if ob.payable:
            return self.payment_step(ob)
        if ob.proof is ProofKind.SUBMISSION:
            who = "Your accountant files this. " if ob.obligation.responsible == "accountant" else ""
            if kind is ObligationKind.TOURIST_TAX_DECLARATION:
                return ("I close it when the municipality confirms the declaration, or when you tell me it is "
                        "submitted.")
            return f"{who}I close it when the filing receipt arrives."
        if ob.proof is ProofKind.RENEWAL:
            return (f"I close it when the renewed {_RENEWED_THING.get(kind, 'contract')} arrives, or when you tell me "
                    "you are not renewing.")
        if kind is ObligationKind.GRANT_DOCUMENTS:
            return ("When you have sent the documents, forward me the agency's confirmation or tell me they are "
                    "sent.")
        return "When you have sent what they ask for, forward me their confirmation or tell me it is done."

    def payment_letter(self, ob: ObligationRecord) -> str:
        return _PAYMENT_LETTER.get(ob.obligation.kind, "letter")


def _confirmed_how(proof: ProofKind, on: date, valid_until: date | None = None, *, by_owner: bool = False) -> str:
    """How an obligation that is not a payment was done, in plain words."""
    day = day_month(on)
    until = day_month(valid_until, on) if valid_until is not None else ""  # the year when it is not this one
    if by_owner:
        if proof is ProofKind.DECISION:
            return f"You told me on {day} that you are not renewing it."
        if proof is ProofKind.RENEWAL and valid_until is not None:
            return f"You told me on {day} that it is renewed until {until}."
        if proof is ProofKind.SUBMISSION:
            return f"You told me on {day} that it was filed."
        return f"You told me on {day} that it was sent."
    if proof is ProofKind.DECISION:
        return f"Confirmed on {day} that it will not be renewed."
    if proof is ProofKind.RENEWAL and valid_until is not None:
        return f"Renewed until {until}."
    if proof is ProofKind.SUBMISSION:
        return f"The filing receipt arrived on {day}."
    return f"They confirmed on {day} that they received it."


def _names_tax_id(text: str, tax_id: str) -> bool:
    """'NIF: 234 567 899' / '234.567.899' / 'PT234567899' all name tax number 234567899."""
    digits = re.sub(r"\D", "", tax_id or "")
    if len(digits) < 5:
        return False
    return re.search(r"(?<![\d.])" + r"[ .]?".join(digits) + r"(?![\d]|[.]\d)", text or "") is not None


def _reads_as_accounting_document(text: str) -> bool:
    """A fiscal document rather than a letter: a fiscal QR code (Portugal's, Spain's), or a numbered invoice,
    receipt or note title ("Fatura n.º FT 2026/183", "Invoice 2026/77"). Its payment proves it (§20)."""
    if _split_qr(text or "")[0]:
        return True
    from backoffice.learning import fold

    for raw in (text or "").splitlines():
        line = fold(raw)
        if any(ch.isdigit() for ch in line) and any(pattern.match(line) for _, pattern in _KIND_TITLES):
            return True
    return False


class MissingEvidenceAgent(_Agent):
    """A plan for every payment still without its document; polite supplier requests when allowed (§22, §25).

    Money back from a supplier without its credit note is chased the same way (the request asks for
    the credit note). From learned invoice rhythms (§23), a supplier's usual invoice that is overdue
    becomes one missing item, once per period: I look for it, ask the supplier when the policy
    allows, and close it only when the invoice arrives.
    """

    name = "missing_evidence"

    def plan(self, rec: TxRecord) -> str:
        """Plain-language next step for one payment without its document."""
        amount = format_money(abs(rec.tx.amount), rec.tx.currency)
        who = self.o.merchant_name(rec.tx)
        when = day_month(rec.tx.booked_on, self.repo.today())
        if rec.id in self.repo.onboarding.deferred and rec.tx.entity_id is None:
            return (f"I will ask you later which company the {amount} payment to {who} on {when} is for. Your first "
                    "answers may settle it.")
        if rec.decision is not None and rec.decision.expectation is EvidenceExpectation.PAYOUT_REPORT:
            return self.o.settlement.plan(rec, amount, when)
        disputed = self.o.chargebacks.plan(rec) if rec.id in self.repo.chargebacks else None
        if disputed is not None:  # a disputed card payment (I7): linked to its sale, not to an invoice
            return disputed
        # A leasing payment, cash paid into the bank, a member's payment without its receipt (X24, X5, X12).
        book = self.o.leases.plan(rec) or self.o.cash.plan(rec) or self.o.members.plan(rec)
        if book is not None:
            return book
        staged = None if rec.supporting_document_ids or rec.id in self.repo.chases else self.o.staged.plan(rec)
        if staged is not None:  # a deposit waiting for its invoice, or money that may give one back (X8)
            return staged
        if rec.decision is not None and rec.decision.expectation is EvidenceExpectation.REFUND_OR_CREDIT_NOTE:
            return self._refund_plan(rec, amount, who, when)
        staff = self.o.staff.plan(rec)  # paid with an employee's card: the receipt is asked from them
        if staff is not None:
            return staff
        if rec.decision is not None and rec.decision.expectation is EvidenceExpectation.PAYROLL:
            return self.o.payroll.plan(rec, amount, who, when)
        chase = self.repo.chases.get(rec.id)
        link = self.repo.broken_links.get(chase.link_id or "") if chase is not None else None
        if chase is not None and link is not None:  # the invoice link in its email no longer works
            return link.asked_line if chase.sent else link.waiting_line
        if chase is not None and chase.sent:
            return (f"I asked {who} for the invoice for the {amount} payment on {when}. "
                    "Suppliers usually reply within a few days.")
        written = self.repo.outbox.get(chase.outbox_id) if chase is not None else None
        if written is not None and self.o.held_back(written):
            return (f"I wrote to {who} asking for the invoice for the {amount} payment on {when}, but asking "
                    "suppliers for invoices is switched off, so I have not sent it.")
        if chase is not None:
            return (f"I wrote to {who} asking for the invoice for the {amount} payment on {when}. "
                    "It is waiting to be sent.")
        if rec.likely_document_ids:
            return f"I found a likely document for the {amount} payment to {who} on {when} and I'm confirming it."
        if rec.supporting_document_ids:
            doc = self.repo.documents.get(rec.supporting_document_ids[0])
            kind = _DOC_LABELS.get(doc.document.doc_type, "document").lower() if doc else "document"
            return (f"I have the {kind} for the {amount} payment to {who} on {when}. It is not an invoice, "
                    "so I'm still looking for the invoice.")
        if rec.decision is not None and rec.decision.provider.value == "owner":
            return f"The {amount} payment to {who} on {when} is waiting for its receipt. I will match it when it arrives."
        if rec.decision is not None and rec.decision.rule == "grant":  # checklist X30
            payer = display_name(rec.tx.counterparty)
            if any(n.subject_id == rec.id and n.status == "open" and n.kind == "obligation"
                   for n in self.repo.needs.values()):
                return f"The {amount} from {payer} on {when} may be the grant in a letter. I asked you about it."
            return (f"The {amount} from {payer} on {when} is a grant or subsidy, not a sale. Forward me the letter "
                    "about it (the approval or the payment notice) and I will close it.")
        if rec.decision is not None and rec.decision.rule == "tourist_tax":  # checklist X26
            return (f"The {amount} tourist tax payment on {when} is waiting for the municipality's notice or "
                    "payment proof.")
        if rec.decision is not None and rec.decision.provider.value == "government":
            return f"The {amount} tax payment on {when} is waiting for the tax notice or payment proof."
        return f"I'm looking for the document for the {amount} payment to {who} on {when}."

    def _refund_plan(self, rec: TxRecord, amount: str, who: str, when: str) -> str:
        """Money back: from a supplier, it needs the supplier's credit note; to a customer, your own (§20)."""
        if rec.decision is not None and rec.decision.rule == "customer_refund":
            if rec.likely_document_ids:
                return (f"I found your credit note that likely covers the {amount} refund to {who} on {when} and "
                        "I'm confirming it.")
            return (f"The {amount} refund to {who} on {when} needs your own credit note. Make it where you make your "
                    "invoices and send it to me: I will match it.")
        if any(n.subject_id == rec.id and n.status == "open" and n.kind == "refund" for n in self.repo.needs.values()):
            return (f"{who} refunded {amount} on {when}, but its credit note is for a different amount. "
                    "I asked you about it.")
        chase = self.repo.chases.get(rec.id)
        if chase is not None and chase.sent:
            return (f"I asked {who} for the credit note for the {amount} refund on {when}. "
                    "Suppliers usually reply within a few days.")
        if chase is not None:
            return (f"I wrote to {who} asking for the credit note for the {amount} refund on {when}. "
                    "It is waiting to be sent.")
        if rec.likely_document_ids:
            return f"I found a likely credit note for the {amount} refund from {who} on {when} and I'm confirming it."
        return f"I'm looking for the credit note for the {amount} refund from {who} on {when}."

    def chase_all(self, now: datetime) -> list[str]:
        """Write a request for every payment whose invoice is missing, when the policy allows it (§22, §25).

        Money back from a supplier without its credit note is asked for the same way. Requests go to
        the outbox; they count as asked only once a transport sends them (:meth:`Orchestrator.deliver`).
        Returns the payments whose request was written now.
        """
        written: list[str] = []
        today = now.astimezone(TZ).date()
        for rec in sorted(self.repo.transactions.values(), key=lambda r: (r.tx.booked_on, r.id)):
            payout = rec.decision is not None and rec.decision.expectation is EvidenceExpectation.PAYOUT_REPORT
            refund = (rec.decision is not None and rec.tx.amount > 0
                      and rec.decision.expectation is EvidenceExpectation.REFUND_OR_CREDIT_NOTE)
            if rec.id in self.repo.chases or rec.document_ids or rec.private or (
                    rec.tx.amount >= 0 and not payout and not refund):
                continue  # customers' money is not chased; a payout or a supplier refund still misses its document
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
            if self.o.staff.asks_cardholder(rec):
                continue  # paid with an employee's card: the receipt is asked from them, not the supplier
            match = self.repo.resolver().resolve_transaction(rec.tx)
            supplier = match.supplier
            if supplier is None or not supplier.contact_email:
                continue
            company = self.repo.companies[rec.tx.entity_id]
            if any(b.status == "requested" and b.tx_id is None and b.supplier_id == supplier.id
                   and b.company_id == company.id for b in self.repo.broken_links.values()):
                continue  # already asked for the invoice behind a link that no longer works: not twice
            if self._write_chase(rec, supplier, company, now) is not None:
                written.append(rec.id)
        return written

    def _write_chase(self, rec: TxRecord, supplier: Supplier, company: LegalEntity, now: datetime, *,
                     link: BrokenLinkRecord | None = None) -> ChaseRecord | None:
        """Write the request for one payment's invoice when the policy allows it (§22, §25); None otherwise."""
        decision = authorize(ActionKind.SUPPLIER_INVOICE_REQUEST, self.repo.policy, ActionContext(
            tenant_id=self.repo.tenant_id, entity_id=company.id, subject_id=rec.id))
        self.log("authorize_chase", subject_id=rec.id, evidence_ids=[rec.evidence_id],
                 response={"allowed": decision.allowed_now, "reason": decision.reason_plain})
        if not decision.allowed_now:
            return None
        # A supplier's statement may name the invoice this payment is missing: the request then asks for it.
        facts = ChaseFacts.build(rec.tx, supplier, company, invoice_number=self.o.statements.number_for(rec))
        message = compose_request(facts, token=thread_token(self.repo.tenant_id, rec.id),
                                  today=now.astimezone(TZ).date(), message_id_domain=MESSAGE_ID_DOMAIN)
        out = self.o.write_email("supplier_request", rec.id, company.id, message.to, message.subject, message.body,
                                 now, headers=(("Message-ID", message.message_id),))
        chase = ChaseRecord(tx_id=rec.id, supplier_id=supplier.id, company_id=company.id, message=message,
                            written_at=now, line=link.asked_line if link is not None else activity_line(facts),
                            outbox_id=out.id, link_id=link.id if link is not None else None)
        self.repo.chases[rec.id] = chase
        values = {"to": message.to, "subject": message.subject, "outbox_id": out.id}
        if link is not None:
            values["broken_link"] = link.id
        self.log("write_request", subject_id=rec.id, evidence_ids=[rec.evidence_id], values=values)
        return chase

    # ------------------------------------------------------------------ invoice links that no longer work (§9, §22)

    def chase_broken_links(self, now: datetime) -> list[str]:
        """An invoice link that no longer works: look for the invoice first, then ask the supplier for it.

        With a payment to that supplier still without its invoice, the request asks for that payment's
        invoice (the usual request, written at once rather than after a few days); without one, it asks for
        the invoice from the supplier's email. Either way it goes out through the same send path and counts
        as asked only once a transport accepted it. The item closes only when the invoice arrives (§3).
        Returns the links a request was written for now.
        """
        repo = self.repo
        written: list[str] = []
        for record in sorted(repo.broken_links.values(), key=lambda r: r.id):
            if record.status == "received":
                continue
            found = self._invoice_since(record)
            if found is not None:
                record.status, record.document_id = "received", found.id
                self.log("broken_link_resolved", subject_id=record.id, evidence_ids=found.evidence_ids,
                         values={"document": found.id})
                continue
            if record.status != "missing":
                continue
            record.searched = ("I looked through your email and the documents you sent me: the invoice is not there.",)
            supplier = repo.suppliers.get(record.supplier_id or "")
            if supplier is None or not supplier.contact_email:
                record.note = f"I don't have an email address for {record.supplier_name}, so I can't ask for it."
                continue
            rec = self._payment_for(record, supplier)
            if rec is not None:
                if rec.id in repo.chases:  # already asked about that payment: that request covers it
                    record.status, record.tx_id, record.company_id = "requested", rec.id, rec.company_id
                    continue
                company = repo.companies.get(rec.tx.entity_id or "")
                if company is None:
                    record.note = "I'm waiting to know which of your companies the payment is for before I ask."
                    continue
                chase = self._write_chase(rec, supplier, company, now, link=record)
                if chase is None:
                    record.note = "Asking suppliers for invoices is not switched on, so I have not asked."
                    continue
                record.status, record.tx_id, record.company_id = "requested", rec.id, company.id
                record.outbox_id, record.written_at = chase.outbox_id, now
                written.append(record.id)
                continue
            company = repo.companies.get(self.o.supplier_company(supplier.id) or "")
            if company is None:
                record.note = "I'm waiting for the payment to know which of your companies the invoice is for."
                continue
            decision = authorize(ActionKind.SUPPLIER_INVOICE_REQUEST, repo.policy, ActionContext(
                tenant_id=repo.tenant_id, entity_id=company.id, subject_id=record.id))
            self.log("authorize_chase", subject_id=record.id,
                     response={"allowed": decision.allowed_now, "reason": decision.reason_plain})
            if not decision.allowed_now:
                record.note = "Asking suppliers for invoices is not switched on, so I have not asked."
                continue
            facts = LinkChaseFacts.build(supplier, company, emailed_on=record.since)
            message = compose_link_request(facts, token=thread_token(repo.tenant_id, record.id),
                                           today=now.astimezone(TZ).date(), message_id_domain=MESSAGE_ID_DOMAIN)
            out = self.o.write_email("link_request", record.id, company.id, message.to, message.subject, message.body,
                                     now, headers=(("Message-ID", message.message_id),))
            record.status, record.company_id, record.message = "requested", company.id, message
            record.outbox_id, record.written_at = out.id, now
            self.log("write_request", subject_id=record.id,
                     values={"to": message.to, "subject": message.subject, "outbox_id": out.id})
            written.append(record.id)
        return written

    def _invoice_since(self, record: BrokenLinkRecord) -> DocumentRecord | None:
        """The invoice that link was for, when it is on file: from that supplier, arrived around the link or since
        (the same email's attachment or words, another channel, the supplier's reply), or its payment's."""
        if record.tx_id:
            rec = self.repo.transactions.get(record.tx_id)
            if rec is not None and rec.document_ids:
                return self.repo.documents.get(rec.document_ids[0])
        if record.email_evidence_id:  # the same email gave it (its attachment or its own words)
            registry, tenant = self.repo.registry, self.repo.tenant_id
            for d in sorted(self.repo.documents.values(), key=lambda d: d.id):
                if d.sales or d.supporting:
                    continue
                if record.email_evidence_id in d.evidence_ids or any(
                        s.context.get("parent_evidence_id") == record.email_evidence_id
                        for e in d.evidence_ids for s in registry.sightings(tenant, e)):
                    return d
        if not record.supplier_id:
            return None
        earliest = record.since - timedelta(days=3)
        found = [d for d in self.repo.documents.values()
                 if d.supplier_id == record.supplier_id and not d.sales and not d.supporting
                 and d.document.doc_type in PURCHASE_INVOICE_TYPES and _arrived_on(d) >= earliest]
        return min(found, key=lambda d: (d.received_at, d.id)) if found else None

    def _payment_for(self, record: BrokenLinkRecord, supplier: Supplier) -> TxRecord | None:
        """A payment to that supplier, around the day of its email, still without its invoice."""
        resolver = self.repo.resolver()
        near: list[tuple[int, str, TxRecord]] = []
        for rec in self.repo.transactions.values():
            if rec.tx.amount >= 0 or rec.private or rec.document_ids or rec.likely_document_ids:
                continue
            if self.repo.items[rec.item_id].is_done:
                continue
            if rec.decision is not None and not rec.decision.requires_document:
                continue
            days = abs((rec.tx.booked_on - record.since).days)
            if days > 45:
                continue
            match = resolver.resolve_transaction(rec.tx)
            if match.supplier is not None and match.supplier.id == supplier.id:
                near.append((days, rec.id, rec))
        return min(near, key=lambda n: (n[0], n[1]))[2] if near else None

    def sent_link_request(self, record: BrokenLinkRecord, at: datetime) -> None:
        """A transport accepted the request for the invoice behind a link that no longer works."""
        record.sent_at = at
        self.repo.closure_log.append(ClosureActivity(
            kind=ClosureKind.SUPPLIER_CHASED, at=at, entity_id=record.company_id or "",
            subject_id=record.supplier_id or record.id, period=Month.of(record.since)))
        self.o.activity(at, "chased", record.asked_line, record.company_id,
                        evidence_ids=[record.email_evidence_id] if record.email_evidence_id else [])
        self.log("request_invoice", subject_id=record.id,
                 values={"to": record.message.to if record.message else "", "url": record.url},
                 response={"message_id": record.message.message_id if record.message else None})

    def sent(self, chase: ChaseRecord, at: datetime) -> None:
        """A transport accepted the request: now the supplier has been asked (§22, month summary)."""
        rec = self.repo.transactions[chase.tx_id]
        chase.sent_at = at
        if chase.link_id and chase.link_id in self.repo.broken_links:
            self.repo.broken_links[chase.link_id].sent_at = at
        self.repo.closure_log.append(ClosureActivity(
            kind=ClosureKind.SUPPLIER_CHASED, at=at, entity_id=chase.company_id, subject_id=chase.supplier_id,
            period=Month.of(rec.tx.booked_on)))
        self.o.activity(at, "chased", chase.line, chase.company_id, evidence_ids=[rec.evidence_id])
        self.log("request_invoice", subject_id=rec.id, evidence_ids=[rec.evidence_id],
                 values={"to": chase.message.to, "subject": chase.message.subject},
                 response={"message_id": chase.message.message_id})

    # ------------------------------------------------------------------ usual invoices that have not arrived (§23)

    def invoice_groups(self) -> dict[tuple[str, str], list[DocumentRecord]]:
        """Each supplier's purchase invoices to each of your companies, oldest first: a rhythm is learned
        per supplier and company. Sales documents, credit notes and supporting documents never count."""
        groups: dict[tuple[str, str], list[DocumentRecord]] = {}
        for d in self.repo.documents.values():
            doc = d.document
            if d.sales or d.supporting or doc.doc_type not in PURCHASE_INVOICE_TYPES or not doc.entity_id:
                continue
            digits = re.sub(r"\D", "", doc.supplier_tax_id or "")
            key = d.supplier_id or (f"nif:{digits}" if digits else None)
            if key is not None:
                groups.setdefault((key, doc.entity_id), []).append(d)
        for docs in groups.values():
            docs.sort(key=lambda d: (_arrived_on(d), d.id))
        return groups

    def _covered(self, key: str, company_id: str, docs: Sequence[DocumentRecord]) -> tuple[date, ...]:
        """Periods covered: the invoices that arrived, and the ones the owner said are not coming."""
        told = [e.expected_on for e in self.repo.expected_invoices.values()
                if e.series_key == key and e.company_id == company_id and e.status == "not_coming"]
        return tuple(sorted({*(_arrived_on(d) for d in docs), *told}))

    def check_recurring(self, now: datetime) -> list[str]:
        """Every run (§23): an invoice that arrived closes what was waiting for it; then each learned invoice
        rhythm is checked, and a supplier's usual invoice that is overdue past its grace is raised once as a
        missing item. A supplier without a trusted rhythm never is (§57). Returns the items raised now."""
        repo = self.repo
        today = now.astimezone(TZ).date()
        groups = self.invoice_groups()
        self._receive_expected(groups, now)
        raised: list[str] = []
        for (key, company_id), docs in sorted(groups.items()):
            if company_id not in repo.companies:
                continue
            supplier = repo.suppliers.get(key)
            name = display_name(supplier.name if supplier else docs[-1].document.supplier_name, fallback="Supplier")
            occurrences = [Occurrence(on=_arrived_on(d), amount=d.document.gross_amount, currency=d.document.currency,
                                      label=d.document.supplier_name, ref=d.id) for d in docs]
            series = learn_series(key, occurrences, basis=Basis.INVOICES, name=name)
            if series is None or not series.trusted:
                continue
            covered = self._covered(key, company_id, docs)
            notice = check_overdue(series, today, arrivals=covered)
            if notice is None or notice.likely_ended:
                continue  # on time, or it looks like it stopped: nothing to chase
            window = next_expected(series, covered)
            if not self._mail_read_past(company_id, window.due):
                continue  # the mailbox has not been read that far: it may be sitting there unread (§47-48)
            rid = "exp_" + hashlib.sha256(
                f"{repo.tenant_id}|{key}|{company_id}|{window.expected.isoformat()}".encode()).hexdigest()[:16]
            if rid in repo.expected_invoices:
                continue  # raised once per period
            self._raise_expected(rid, key=key, supplier=supplier, name=name, company_id=company_id, series=series,
                                 window=window, message=notice.message, docs=docs, covered=covered, now=now)
            raised.append(rid)
        return raised

    def _mail_read_past(self, company_id: str, day: date) -> bool:
        """Every mailbox serving this company is healthy and has been read beyond ``day``: only then can an
        invoice due by that day be called missing (a mailbox still importing or out of sync may hold it)."""
        mailboxes = [c for c in self.repo.connectors.values() if c.kind == "email" and company_id in c.company_ids]
        return all(c.healthy and c.covered_until is not None and c.covered_until.astimezone(TZ).date() > day
                   for c in mailboxes)

    def _raise_expected(self, rid: str, *, key: str, supplier: Supplier | None, name: str, company_id: str,
                        series: RecurringSeries, window: Any, message: str, docs: Sequence[DocumentRecord],
                        covered: tuple[date, ...], now: datetime) -> ExpectedInvoiceRecord:
        repo = self.repo
        evidence = [d.evidence_ids[0] for d in docs[-3:] if d.evidence_ids]  # the invoices the rhythm comes from
        item = TrackedItem(id="item_" + rid, tenant_id=repo.tenant_id, subject_type=EXPECTED_INVOICE, subject_id=rid)
        repo.items[item.id] = item
        record = ExpectedInvoiceRecord(
            id=rid, series_key=key, supplier_id=supplier.id if supplier else None, supplier_name=name,
            company_id=company_id, expected_on=window.expected, due_on=window.due,
            earliest=window.expected - timedelta(days=series.spec.tolerance_days), notice=message, raised_at=now,
            item_id=item.id, series=series, arrivals=covered, learned_from=tuple(d.id for d in docs))
        repo.expected_invoices[rid] = record
        record.searched = self._search(record)
        count = len(docs)
        self.o.advance(item, Stage.ACQUIRED, evidence, agent=self.name,
                       note=f"Learned from {count} earlier {name} invoice{'s' if count != 1 else ''}.")
        self.o.advance(item, Stage.UNDERSTOOD, evidence, agent=self.name, note=message)
        repo.closure_log.append(ClosureActivity(
            kind=ClosureKind.MISSING_DOCUMENT_DETECTED, at=now, entity_id=company_id, subject_id=rid,
            period=record.period))
        self.o.activity(now, "checked", message, company_id, evidence_ids=evidence)
        self.log("expected_invoice_missing", subject_id=rid, evidence_ids=evidence,
                 values={"supplier": name, "expected_on": window.expected, "due_on": window.due,
                         "cadence": series.cadence.value, "observations": series.observations},
                 validations=list(record.searched))
        return record

    def _search(self, record: ExpectedInvoiceRecord) -> tuple[str, ...]:
        """Where I looked before asking anyone, in plain words (§22: current email, documents on file, links)."""
        from urllib.parse import urlparse

        repo = self.repo
        lines = ["I looked through your email and the documents you sent me: it is not there."]
        loose = [d for d in repo.documents.values()
                 if record.supplier_id and d.supplier_id == record.supplier_id and not d.document.entity_id
                 and _arrived_on(d) >= record.earliest]
        if loose:
            lines.append(f"A {record.supplier_name} document arrived that does not show which of your companies it "
                         "is for, so I did not count it.")
        supplier = repo.suppliers.get(record.supplier_id or "")
        domains = [d.lower() for d in (supplier.email_domains if supplier else [])]
        waiting = [u for u in repo.pending_links
                   if any((urlparse(u).hostname or "").lower().endswith(d) for d in domains)]
        if waiting:
            lines.append(f"An invoice link from {record.supplier_name} is waiting to be opened.")
        return tuple(lines)

    def _receive_expected(self, groups: Mapping[tuple[str, str], list[DocumentRecord]], now: datetime) -> None:
        """An invoice from that supplier for that company, dated in the period or later, closes the item: the
        invoice is its evidence (§3). Never a guess: another supplier or company does not count."""
        repo = self.repo
        for record in sorted(repo.expected_invoices.values(), key=lambda e: e.id):
            if record.status != "missing":
                continue
            arrived = [d for d in groups.get((record.series_key, record.company_id), [])
                       if _arrived_on(d) >= record.earliest]
            if not arrived:
                continue
            doc = arrived[0]
            record.status = "received"
            record.document_id = doc.id
            evidence = list(doc.evidence_ids)
            self.o.closure._close(repo.items[record.item_id], evidence,
                                  note=f"The {record.supplier_name} invoice arrived: {doc.label}.")
            repo.closure_log.append(ClosureActivity(
                kind=ClosureKind.MISSING_DOCUMENT_RETRIEVED, at=now, entity_id=record.company_id,
                subject_id=record.id, period=record.period))
            self.o.activity(now, "recovered", f"The {record.supplier_name} invoice for {record.period.name} arrived.",
                            record.company_id, amount=doc.document.gross_amount, currency=doc.document.currency,
                            evidence_ids=evidence)
            self.log("expected_invoice_arrived", subject_id=record.id, evidence_ids=evidence,
                     values={"document": doc.id})

    def chase_expected(self, now: datetime) -> list[str]:
        """Ask the supplier for its overdue usual invoice, once, when the policy allows it (§22, §25): the
        same send path as every request (it counts as asked only once a transport accepted it)."""
        repo = self.repo
        today = now.astimezone(TZ).date()
        written: list[str] = []
        for record in sorted(repo.expected_invoices.values(), key=lambda e: e.id):
            if record.status != "missing" or record.message is not None:
                continue
            supplier = repo.suppliers.get(record.supplier_id or "")
            company = repo.companies.get(record.company_id)
            if supplier is None or not supplier.contact_email or company is None:
                continue
            decision = authorize(ActionKind.SUPPLIER_INVOICE_REQUEST, repo.policy, ActionContext(
                tenant_id=repo.tenant_id, entity_id=company.id, subject_id=record.id))
            self.log("authorize_chase", subject_id=record.id,
                     response={"allowed": decision.allowed_now, "reason": decision.reason_plain})
            if not decision.allowed_now:
                continue
            facts = RecurringChaseFacts.build(supplier, company, period=record.expected_on, usually_by=record.due_on)
            message = compose_recurring_request(facts, token=thread_token(repo.tenant_id, record.id), today=today,
                                                message_id_domain=MESSAGE_ID_DOMAIN)
            out = self.o.write_email("expected_invoice_request", record.id, company.id, message.to, message.subject,
                                     message.body, now, headers=(("Message-ID", message.message_id),))
            record.message, record.line, record.outbox_id, record.written_at = (
                message, recurring_activity_line(facts), out.id, now)
            self.log("write_request", subject_id=record.id,
                     values={"to": message.to, "subject": message.subject, "outbox_id": out.id})
            written.append(record.id)
        return written

    def sent_expected(self, record: ExpectedInvoiceRecord, at: datetime) -> None:
        """A transport accepted the request for the usual invoice: now the supplier has been asked."""
        record.sent_at = at
        self.repo.closure_log.append(ClosureActivity(
            kind=ClosureKind.SUPPLIER_CHASED, at=at, entity_id=record.company_id,
            subject_id=record.supplier_id or record.id, period=record.period))
        self.o.activity(at, "chased", record.line, record.company_id)
        if record.message is not None:
            self.log("request_invoice", subject_id=record.id,
                     values={"to": record.message.to, "subject": record.message.subject},
                     response={"message_id": record.message.message_id})

    def current_notice(self, record: ExpectedInvoiceRecord) -> str:
        """The notice as of today ("... Today is the 29th. Invoice missing.")."""
        notice = check_overdue(record.series, self.repo.today(), arrivals=record.arrivals)
        return notice.message if notice is not None else record.notice

    def plan_expected(self, record: ExpectedInvoiceRecord) -> str:
        """Plain-language next step for a usual invoice that has not arrived."""
        name, month = record.supplier_name, record.period.name
        if record.status == "received":
            return f"The {name} invoice for {month} arrived."
        if record.status == "not_coming":
            return f"You told me the {name} invoice for {month} is not coming."
        notice = self.current_notice(record)
        if record.sent:
            return f"{notice} I asked {name} for it."
        if record.message is not None:
            return f"{notice} I wrote to {name} asking for it. It is waiting to be sent."
        return f"{notice} I will match it when it arrives."


def _arrived_on(record: DocumentRecord) -> date:
    """The day an invoice belongs to for its supplier's rhythm: its own date, else the day it reached us."""
    return record.document.issue_date or record.received_at.astimezone(TZ).date()


# The business's own documents a supplier's statement lists, by the kind the statement gives them.
_STATEMENT_KINDS: dict[DocumentType, str] = {
    DocumentType.INVOICE: STATEMENT_INVOICE, DocumentType.INVOICE_RECEIPT: STATEMENT_INVOICE,
    DocumentType.SIMPLIFIED_INVOICE: STATEMENT_INVOICE, DocumentType.CREDIT_NOTE: STATEMENT_CREDIT_NOTE,
    DocumentType.DEBIT_NOTE: STATEMENT_DEBIT_NOTE,
}


def statement_line_key(line: Any) -> str:
    """A statement line's lasting name: its kind and document number (else its position and amount)."""
    if line.number:
        return f"{line.kind}:{re.sub(r'[^0-9A-Za-z]+', '', line.number).upper()}"
    return f"{line.kind}:row{line.row}:{line.amount}"


class StatementAgent(_Agent):
    """Suppliers' account statements (extrato de conta corrente), checked line by line (§20, §22, §28).

    A statement is supporting evidence only: never booked, never a copy of an invoice, never proof of a
    payment (§3). Each line is compared with the business's own documents and bank payments from that
    supplier (backoffice.supplier_statements). Documents it lists that the business never received are
    missing documents: the supplier is asked for them in one email per statement, when the owner allowed
    supplier requests (a document already paid from the bank is asked for with that payment instead). An
    amount that differs from the business's own document is one plain question, never a silent change.
    The business's documents the statement does not list, and its closing balance against the invoices
    not paid yet, are shown for the accountant.
    """

    name = "statement"

    def register(self, record: DocumentRecord, statement: SupplierStatement) -> StatementRecord:
        repo = self.repo
        sr = StatementRecord(document_id=record.id, statement=statement)
        repo.statements[record.id] = sr
        if record.supplier_id is None and not record.sales:
            supplier = self.supplier_of(record)
            if supplier is not None:
                record.supplier_id = supplier.id
                record.document = record.document.model_copy(update={"supplier_name": supplier.name})
        self.log("read_statement", subject_id=record.id, evidence_ids=record.evidence_ids,
                 values={"lines": len(statement.lines), "opening": statement.opening, "closing": statement.closing,
                         "layout": statement.layout},
                 response={"adds_up": statement.adds_up, "problems": list(statement.problems)})
        return sr

    # ----------------------------------------------------------------- who and what

    def supplier_of(self, record: DocumentRecord) -> Supplier | None:
        """The supplier that sent it: its tax number, the sender's domain, else the one supplier it names."""
        from backoffice.learning import fold

        repo = self.repo
        supplier = repo.suppliers.get(record.supplier_id or "") or repo.supplier_for_tax_id(
            record.document.supplier_tax_id)
        if supplier is None and record.sender and "@" in record.sender:
            supplier = repo.supplier_for_domain(record.sender.rsplit("@", 1)[1])
        if supplier is None:
            text = fold(f"{record.document.supplier_name or ''}\n{record.text[:4000]}")
            named = [s for s in sorted(repo.suppliers.values(), key=lambda s: s.id)
                     if any(len(fold(n)) >= 3 and re.search(rf"(?<![a-z0-9]){re.escape(fold(n))}(?![a-z0-9])", text)
                            for n in (s.name, *s.aliases))]
            supplier = named[0] if len(named) == 1 else None
        return supplier

    def company_of(self, record: DocumentRecord, supplier: Supplier | None = None) -> str | None:
        """Which of your companies it is for: the one it names, your only one, or the only one this
        supplier's own documents and payments belong to."""
        from backoffice.learning import fold

        repo = self.repo
        company = repo.item_company(repo.items[record.item_id])
        if company is None and len(repo.companies) == 1:
            company = next(iter(repo.companies))
        if company is None:
            text = fold(record.text[:4000])
            named = {c.id for c in repo.companies.values()
                     for n in (c.name, repo.legal_names.get(c.id) or c.name)
                     if len(fold(n)) >= 4 and re.search(rf"(?<![a-z0-9]){re.escape(fold(n))}(?![a-z0-9])", text)}
            company = named.pop() if len(named) == 1 else None
        if company is None and supplier is not None:
            docs = {repo.item_company(repo.items[d.item_id]) for d in repo.documents.values()
                    if d.id != record.id and d.supplier_id == supplier.id and not d.supporting}
            resolver = repo.resolver()
            paid = {r.company_id for r in repo.transactions.values() if r.tx.entity_id and not r.private
                    and (found := resolver.resolve_transaction(r.tx).supplier) is not None and found.id == supplier.id}
            known = {c for c in docs | paid if c}
            company = known.pop() if len(known) == 1 else None
        return company

    def ours(self, supplier: Supplier, company: str | None) -> tuple[list[OurDocument], list[OurPayment]]:
        """The business's own documents from this supplier and its bank payments to it."""
        repo = self.repo
        docs: list[OurDocument] = []
        for d in sorted(repo.documents.values(), key=lambda d: d.id):
            doc = d.document
            if d.supporting or d.sales or doc.doc_type not in _STATEMENT_KINDS or doc.gross_amount is None:
                continue
            if d.supplier_id != supplier.id and not (doc.supplier_tax_id and same_tax_id(doc.supplier_tax_id,
                                                                                          supplier.tax_id)):
                continue
            owner = repo.item_company(repo.items[d.item_id])
            if company is not None and owner is not None and owner != company:
                continue
            paid = (repo.items[d.item_id].is_done or bool(d.matched_tx_ids) or d.paid_in_cash
                    or doc.doc_type is DocumentType.INVOICE_RECEIPT)
            docs.append(OurDocument(id=d.id, kind=_STATEMENT_KINDS[doc.doc_type], number=doc.invoice_number,
                                    amount=abs(doc.gross_amount), paid=paid, label=d.label,
                                    on=doc.issue_date or d.received_at.astimezone(TZ).date(),
                                    evidence_ids=tuple(d.evidence_ids)))
        resolver = repo.resolver()
        payments: list[OurPayment] = []
        for r in sorted(repo.transactions.values(), key=lambda r: r.id):
            if r.tx.amount >= 0 or r.private or (company is not None and r.company_id != company):
                continue
            found = resolver.resolve_transaction(r.tx).supplier
            if found is None or found.id != supplier.id:
                continue
            payments.append(OurPayment(id=r.id, amount=abs(r.tx.amount), on=r.tx.booked_on, evidence_id=r.evidence_id,
                                       document_ids=tuple(r.document_ids)))
        return docs, payments

    def _unmatched_payment(self, line: Any, supplier: Supplier, company: str | None) -> TxRecord | None:
        """A bank payment to this supplier of this document's amount that is still without its document."""
        resolver = self.repo.resolver()
        for r in sorted(self.repo.transactions.values(), key=lambda r: (r.tx.booked_on, r.id)):
            if r.tx.amount >= 0 or r.private or r.document_ids or abs(r.tx.amount) != line.amount:
                continue
            if company is not None and r.company_id != company:
                continue
            if line.on is not None and r.tx.booked_on < line.on - timedelta(days=3):
                continue
            found = resolver.resolve_transaction(r.tx).supplier
            if found is not None and found.id == supplier.id:
                return r
        return None

    def number_for(self, rec: TxRecord) -> str | None:
        """The invoice number a supplier's statement gives the document this payment is missing, if exactly one."""
        repo = self.repo
        if not repo.statements:
            return None
        supplier = repo.resolver().resolve_transaction(rec.tx).supplier
        if supplier is None:
            return None
        found: set[str] = set()
        for sr in repo.statements.values():
            if sr.check is None or sr.supplier_id != supplier.id or (sr.company_id and sr.company_id != rec.company_id):
                continue
            for row in sr.check.missing:
                line = row.line
                if line.kind == STATEMENT_INVOICE and line.number and line.amount == abs(rec.tx.amount) and (
                        line.on is None or line.on <= rec.tx.booked_on + timedelta(days=3)):
                    number = clean_invoice_number(line.number)
                    if number:
                        found.add(number)
        return found.pop() if len(found) == 1 else None

    # ----------------------------------------------------------------- the pass

    def review(self, now: datetime) -> int:
        """Check every statement against the records as they are now; ask for what is missing, once."""
        repo = self.repo
        changed = 0
        for sr in sorted(repo.statements.values(), key=lambda s: s.document_id):
            record = repo.documents.get(sr.document_id)
            if record is None or record.sales:
                continue
            supplier = self.supplier_of(record)
            company = self.company_of(record, supplier)
            sr.supplier_id, sr.company_id = (supplier.id if supplier else None), company
            if supplier is None:
                continue  # nothing to compare it with: shown as such, nothing asked
            docs, payments = self.ours(supplier, company)
            check = check_statement(sr.statement, docs, payments, supplier=display_name(supplier.name))
            if sr.check is None or sr.check.key() != check.key():
                self.log("check_statement", subject_id=record.id,
                         evidence_ids=[*record.evidence_ids, *dict.fromkeys(e for r in check.rows
                                                                            for e in r.evidence_ids)],
                         values={"matched": [r.line.row for r in check.matched],
                                 "missing": [r.line.row for r in check.missing],
                                 "differs": [r.line.row for r in check.differences],
                                 "payments_not_found": [r.line.row for r in check.payments_not_found],
                                 "not_on_statement": [d.id for d in check.not_on_statement],
                                 "closing": check.closing, "our_open": check.our_open},
                         response={"complete": check.complete})
                changed += 1
            sr.check = check
            self._track_missing(sr, record, check, company, now)
            self._request(sr, record, supplier, company, now)
            self._ask_about_differences(sr, record, check, company, now)
        return changed

    def _track_missing(self, sr: StatementRecord, record: DocumentRecord, check: StatementCheck, company: str | None,
                       now: datetime) -> None:
        """Documents it lists that the business doesn't have are missing documents; so is their arrival later."""
        if company is None:
            return
        repo = self.repo
        for row in check.missing:
            key = statement_line_key(row.line)
            if key in sr.detected:
                continue
            sr.detected.append(key)
            repo.closure_log.append(ClosureActivity(
                kind=ClosureKind.MISSING_DOCUMENT_DETECTED, at=now, entity_id=company, subject_id=f"{record.id}:{key}",
                period=Month.of(row.line.on or now.astimezone(TZ).date())))
        found = {statement_line_key(r.line) for r in check.rows if r.line.is_document and r.status != "missing"}
        for key in sr.detected:
            if key in found and key not in sr.retrieved:
                sr.retrieved.append(key)
                row = next(r for r in check.rows if statement_line_key(r.line) == key)
                repo.closure_log.append(ClosureActivity(
                    kind=ClosureKind.MISSING_DOCUMENT_RETRIEVED, at=now, entity_id=company,
                    subject_id=f"{record.id}:{key}", period=Month.of(row.line.on or now.astimezone(TZ).date())))

    def _request(self, sr: StatementRecord, record: DocumentRecord, supplier: Supplier, company: str | None,
                 now: datetime) -> None:
        """One email asking the supplier for the documents its statement lists that never arrived (§22, §25)."""
        repo = self.repo
        if sr.request_id or sr.check is None:
            return
        rows = [r for r in sr.check.missing if self._unmatched_payment(r.line, supplier, company) is None]
        if not rows:
            sr.request_note = ""
            return
        who = display_name(supplier.name)
        entity = repo.companies.get(company or "")
        if entity is None:
            sr.request_note = "I don't know which of your companies this statement is for, so I haven't asked yet."
            return
        if not supplier.contact_email:
            sr.request_note = f"I don't have an email address for {who}, so please ask them yourself."
            return
        decision = authorize(ActionKind.SUPPLIER_INVOICE_REQUEST, repo.policy, ActionContext(
            tenant_id=repo.tenant_id, entity_id=entity.id, subject_id=record.id))
        if sr.request_note != decision.reason_plain:
            self.log("authorize_statement_request", subject_id=record.id, evidence_ids=record.evidence_ids,
                     response={"allowed": decision.allowed_now, "reason": decision.reason_plain})
        if not decision.allowed_now:
            sr.request_note = decision.reason_plain
            return
        today = now.astimezone(TZ).date()
        try:
            items = [StatementItem(kind=r.line.kind, number=clean_invoice_number(r.line.number), on=r.line.on,
                                   amount=r.line.amount) for r in rows]
            message = compose_statement_request(
                supplier_name=who, supplier_email=supplier.contact_email, company_name=entity.name,
                company_tax_id=entity.tax_id, company_country=entity.country, items=items,
                token=thread_token(repo.tenant_id, record.id), today=today, message_id_domain=MESSAGE_ID_DOMAIN,
                currency=sr.statement.currency, language=choose_language(supplier, supplier.contact_email))
        except ValueError:
            sr.request_note = f"I couldn't write to {who} about this, so please ask them yourself."
            return
        out = self.o.write_email("statement_request", record.id, entity.id, message.to, message.subject, message.body,
                                 now, headers=(("Message-ID", message.message_id),))
        sr.request_id, sr.request_note = out.id, ""
        sr.requested = tuple(statement_line_key(r.line) for r in rows)
        self.log("write_statement_request", subject_id=record.id, evidence_ids=record.evidence_ids,
                 values={"to": message.to, "subject": message.subject, "outbox_id": out.id,
                         "documents": [r.line.number or r.line.row for r in rows]})

    def request_line(self, sr: StatementRecord) -> str:
        """Where the request for the missing documents stands, in plain words ("" when nothing is missing)."""
        record = self.repo.documents.get(sr.document_id)
        who = display_name(record.document.supplier_name) if record is not None else "the supplier"
        message = self.repo.outbox.get(sr.request_id) if sr.request_id else None
        if message is not None:
            them = "it" if len(sr.requested) == 1 else "them"
            return (f"I asked {who} for {them}." if message.sent else
                    f"I wrote to {who} asking for {them}. It is waiting to be sent.")
        if sr.check is not None and sr.check.missing:
            if sr.request_note and sr.request_note[:1].isupper() and sr.request_note.endswith("."):
                return sr.request_note
            if sr.request_note:
                note = sr.request_note.rstrip(".")
                return f"I haven't asked {who} for them: {note[:1].lower()}{note[1:]}."
            if len(sr.check.missing) == 1:
                return "It is already paid from your bank: I ask for it with that payment."
            return "They are already paid from your bank: I ask for each one with its payment."
        return ""

    # ----------------------------------------------------------------- amounts that differ: one question

    def _ask_about_differences(self, sr: StatementRecord, record: DocumentRecord, check: StatementCheck,
                               company: str | None, now: datetime) -> None:
        repo = self.repo
        rows = [r for r in check.differences if statement_line_key(r.line) not in sr.answered]
        asked = repo.needs.get(sr.needs_id or "")
        if asked is not None and asked.status == "open":
            if not rows:  # the documents agree now (a corrected document arrived): nothing to ask any more
                asked.status, asked.resolution = "resolved", "statement_evidence"
            return
        if not rows or company is None:
            return
        who = display_name(record.document.supplier_name)
        prompt, options, why = self._question(rows, who, check.currency)
        needs_id = _unique_id(repo.needs, f"nd_{_slug(who.split()[0])}_statement")
        repo.needs[needs_id] = NeedsYouRecord(
            id=needs_id, kind="statement", subject_type="document", subject_id=record.id, item_id=record.item_id,
            company_id=company, created_at=now, why=why, prompt=prompt, options=options)
        sr.needs_id = needs_id
        self.log("ask_owner", subject_id=record.id,
                 evidence_ids=[*record.evidence_ids, *(e for r in rows for e in r.evidence_ids)],
                 validations=list(why), response={"needs_you": needs_id})

    @staticmethod
    def _question(rows: Sequence[RowCheck], who: str, currency: str
                  ) -> tuple[str, tuple[CheckOption, ...], tuple[str, ...]]:
        def money(value: Decimal | None) -> str:
            return format_money(value if value is not None else _ZERO, currency)

        why = tuple(f"{r.line.word.capitalize()} {r.line.number}: {money(r.line.amount)} on the statement, "
                    f"{money(r.our_amount)} on the {r.line.word} you have." for r in rows)
        why += ("I never change an amount without you. If the statement is right, I will ask them for a "
                "corrected document.",)
        if len(rows) == 1:
            r = rows[0]
            prompt = (f"{who}'s statement shows {r.line.word} {r.line.number} as {money(r.line.amount)}, but the "
                      f"{r.line.word} says {money(r.our_amount)}. Which is right?")
            options = (CheckOption(id="document", label=f"The {r.line.word}: {money(r.our_amount)}"),
                       CheckOption(id="statement", label=f"The statement: {money(r.line.amount)}"))
        else:
            prompt = f"{who}'s statement shows different amounts for {len(rows)} documents. Which are right?"
            options = (CheckOption(id="document", label="The documents you have"),
                       CheckOption(id="statement", label="The statement"))
        return prompt, options, why

    def answer(self, needs: NeedsYouRecord, option_id: str, answer_ev: str, now: datetime) -> AnswerOutcome:
        """The owner said which amounts are right. The documents themselves are never changed."""
        repo = self.repo
        sr = next(s for s in repo.statements.values() if s.needs_id == needs.id)
        record = repo.documents[sr.document_id]
        check = sr.check
        rows = [r for r in (check.differences if check else ()) if statement_line_key(r.line) not in sr.answered]
        sr.answer = option_id
        sr.answered = (*sr.answered, *(statement_line_key(r.line) for r in rows))
        needs.status, needs.answer, needs.answered_at = "answered", option_id, now
        who = display_name(record.document.supplier_name)
        one = len(rows) <= 1
        self.log("answer_statement", subject_id=record.id, evidence_ids=[answer_ev, *record.evidence_ids],
                 actor=OWNER_ACTOR, values={"option": option_id, "lines": [r.line.row for r in rows]})
        self.o.activity(now, "answered", f"You told me which amounts are right on {who}'s statement.", needs.company_id,
                        evidence_ids=[answer_ev])
        if option_id == "document":
            if one:
                word = rows[0].line.word if rows else "document"
                text = (f"Done. The {word} stays as it is. I noted the difference on {who}'s statement for your "
                        "accountant.")
            else:
                text = (f"Done. Your documents stay as they are. I noted the differences on {who}'s statement for "
                        "your accountant.")
            return AnswerOutcome(ok=True, message=text)
        out, note = self._request_corrections(sr, record, rows, needs.company_id, answer_ev, now)
        corrected = "the corrected document arrives" if one else "the corrected documents arrive"
        if out is not None:
            sr.correction_id = out.id
            return AnswerOutcome(ok=True, message=f"Done. Nothing changes until {corrected}. I will ask {who} for "
                                                  f"{'it' if one else 'them'}.")
        return AnswerOutcome(ok=True, message=f"Done. Nothing changes until {corrected}. {note}")

    def _request_corrections(self, sr: StatementRecord, record: DocumentRecord, rows: Sequence[RowCheck],
                             company_id: str, answer_ev: str, now: datetime) -> tuple[OutgoingMessage | None, str]:
        """The owner said the statement is right: ask for corrected documents (their tap is the approval)."""
        repo = self.repo
        supplier = repo.suppliers.get(sr.supplier_id or "")
        company = repo.companies.get(company_id)
        who = display_name(record.document.supplier_name)
        yourself = f"Please ask {who} for the corrected {'document' if len(rows) <= 1 else 'documents'} yourself."
        if supplier is None or not supplier.contact_email or company is None or not rows:
            return None, yourself
        approval = Approval(tenant_id=repo.tenant_id, action=ActionKind.SUPPLIER_INVOICE_REQUEST, subject_id=record.id,
                            level=Requirement.OWNER, approved_by=f"{OWNER_ACTOR}:{repo.owner.email}", approved_at=now,
                            entity_id=company.id)
        decision = authorize(ActionKind.SUPPLIER_INVOICE_REQUEST, repo.policy, ActionContext(
            tenant_id=repo.tenant_id, entity_id=company.id, subject_id=record.id, unusual=True, approval=approval))
        self.log("authorize_statement_correction", subject_id=record.id, evidence_ids=[answer_ev], actor=OWNER_ACTOR,
                 response={"allowed": decision.allowed_now, "reason": decision.reason_plain})
        if not decision.allowed_now:
            return None, yourself
        try:
            items = [StatementItem(kind=r.line.kind, number=clean_invoice_number(r.line.number), on=r.line.on,
                                   amount=r.line.amount, ours=r.our_amount) for r in rows]
            message = compose_statement_request(
                supplier_name=who, supplier_email=supplier.contact_email, company_name=company.name,
                company_tax_id=company.tax_id, company_country=company.country, items=items,
                token=thread_token(repo.tenant_id, f"{record.id}:corrections"), today=now.astimezone(TZ).date(),
                message_id_domain=MESSAGE_ID_DOMAIN, currency=sr.statement.currency,
                language=choose_language(supplier, supplier.contact_email), corrections=True)
        except ValueError:
            return None, yourself
        out = self.o.write_email("statement_correction", record.id, company.id, message.to, message.subject,
                                 message.body, now, headers=(("Message-ID", message.message_id),))
        return out, ""

    # ----------------------------------------------------------------- emails

    def describe(self, message: OutgoingMessage) -> tuple[str, str]:
        """(what was asked for, in words that follow "asking for"), and who was asked."""
        sr = self.repo.statements.get(message.subject_id)
        record = self.repo.documents.get(message.subject_id)
        who = display_name(record.document.supplier_name) if record is not None else "the supplier"
        if message.kind == "statement_correction":
            return "corrected documents where its statement differs", who
        n = len(sr.requested) if sr is not None else 0
        rows = [r for r in (sr.check.rows if sr is not None and sr.check is not None else ())
                if statement_line_key(r.line) in (sr.requested if sr is not None else ())]
        if n == 1 and len(rows) == 1 and rows[0].line.number:
            line = rows[0].line
            return f"{line.word} {line.number}, which its statement lists but I don't have", who
        return f"the {count_phrase(n, 'document')} its statement lists that I don't have", who

    def sent(self, message: OutgoingMessage, at: datetime) -> None:
        """A transport accepted the request: only now has the supplier been asked (§22)."""
        repo = self.repo
        sr = repo.statements.get(message.subject_id)
        record = repo.documents.get(message.subject_id)
        if sr is None or record is None:
            return
        what, who = self.describe(message)
        end = sr.statement.end or at.astimezone(TZ).date()
        repo.closure_log.append(ClosureActivity(
            kind=ClosureKind.SUPPLIER_CHASED, at=at, entity_id=message.company_id or sr.company_id or "",
            subject_id=sr.supplier_id or record.id, period=Month.of(end)))
        self.o.activity(at, "chased", f"Asked {who} for {what}.", message.company_id, evidence_ids=record.evidence_ids)
        self.log("request_statement_documents", subject_id=record.id, evidence_ids=record.evidence_ids,
                 values={"to": message.to, "subject": message.subject, "kind": message.kind})


_KIND_WORDS = {TransactionKind.TRANSFER_OUT: "transfer", TransactionKind.DIRECT_DEBIT: "direct debit",
               TransactionKind.CARD: "card payment", TransactionKind.FEE: "bank charge"}


class AccountantAgent(_Agent):
    """Answers routine accountant questions from evidence, when the owner allowed it (§25, §28)."""

    name = "accountant"

    def receive(self, text: str, evidence_id: str, at: datetime,
                accountant: AccountantProfile | None = None, *, subject: str = "",
                message_id: str | None = None) -> list[AccountantQuestion]:
        """Questions in an accountant's email, each filed under one of *that accountant's* companies.

        A company with its own accountant only takes questions from that accountant; the
        business's accountant speaks for the other companies (§28, §51).
        """
        allowed = self.repo.accountant_companies(accountant.email) if accountant is not None else \
            list(self.repo.companies)
        if not allowed:
            return []
        found = []
        for i, line in enumerate(q.strip() for q in re.split(r"(?<=\?)\s+|\n", text)):
            if not line.endswith("?") or len(line) < 12:
                continue
            qid = f"aq_{evidence_id[3:15]}_{i}"
            if qid in self.repo.accountant_questions:
                continue
            company_id = self._company_for(line, allowed) or allowed[0]
            question = AccountantQuestion(id=qid, company_id=company_id, text=line, evidence_id=evidence_id,
                                          asked_at=at, subject=" ".join(subject.split())[:200],
                                          message_id=_reply_to(message_id),
                                          accountant_id=accountant.id if accountant else "")
            self.repo.accountant_questions[qid] = question
            self.repo.closure_log.append(ClosureActivity(
                kind=ClosureKind.ACCOUNTANT_QUESTION_ASKED, at=at, entity_id=company_id, subject_id=qid))
            self.log("question_received", subject_id=qid, evidence_ids=[evidence_id],
                     values={"text": line, "company": company_id, "accountant": question.accountant_id})
            found.append(question)
        return found

    def _company_for(self, text: str, allowed: Sequence[str]) -> str | None:
        folded = text.casefold()
        for entity in self.repo.entities:
            if entity.id in allowed and entity.name.casefold() in folded:
                return entity.id
        amount = _amount_in(text)
        if amount is not None:
            for rec in self.repo.transactions.values():
                if abs(rec.tx.amount) == amount and rec.company_id in allowed:
                    return rec.company_id
        return None

    def answer_all(self, now: datetime) -> list[AccountantQuestion]:
        """Write answers to the questions I truly understand, from evidence, when the owner allowed it (§25, §28).

        A question is answered only when it is about exactly one closed payment with its document and every
        claim in it is supported (backoffice.accountant_questions); anything else stays open for the owner.
        The answer is written to the outbox; it counts as answered only once a transport sent it.
        """
        written = []
        for q in sorted(self.repo.accountant_questions.values(), key=lambda q: q.id):
            if q.status != "waiting":
                continue
            acct = self.asker(q)
            if acct is None:
                continue
            amounts = set(amounts_in(q.text))
            if len(amounts) != 1:
                continue
            amount = amounts.pop()
            candidates = [r for r in self.repo.transactions.values()
                          if abs(r.tx.amount) == amount and r.company_id == q.company_id and r.document_ids
                          and self.repo.items[r.item_id].stage is Stage.CLOSED]
            if len(candidates) != 1:
                continue
            rec = candidates[0]
            doc = self.repo.documents[rec.document_ids[0]]
            reply = answer_question(q.text, self._facts(rec, doc), now.astimezone(TZ).date())
            if reply is None:
                continue  # not something I can answer from the evidence: it stays open for the owner
            decision = authorize(ActionKind.ROUTINE_ACCOUNTANT_RESPONSE, self.repo.policy, ActionContext(
                tenant_id=self.repo.tenant_id, entity_id=q.company_id, subject_id=q.id))
            if not decision.allowed_now:
                continue
            q.status = "written"
            q.answer = reply.text
            q.answer_evidence_ids = (rec.evidence_id, *doc.evidence_ids)
            q.period = Month.of(rec.tx.booked_on)
            subject = q.subject if q.subject.lower().startswith("re:") else f"Re: {q.subject or 'your question'}"
            headers = [("Message-ID", f"<answer-{q.id.replace('_', '-')}@{MESSAGE_ID_DOMAIN}>")]
            if q.message_id:
                headers += [("In-Reply-To", q.message_id), ("References", q.message_id)]
            first = acct.person.split()[0] if acct.person.strip() else ""
            body = (f"Hello{' ' + first if first else ''},\n\nYou asked: “{q.text}”\n{reply.text}\n\n"
                    f"Kind regards,\n{self.repo.owner.full_name}")
            out = self.o.write_email("accountant_answer", q.id, q.company_id, acct.email, subject[:200], body, now,
                                     headers=tuple(headers))
            q.outbox_id = out.id
            self.log("answer_accountant", subject_id=q.id, evidence_ids=list(q.answer_evidence_ids),
                     values={"claims": list(reply.claims), "outbox_id": out.id},
                     response={"answer": reply.text, "reason": decision.reason_plain})
            written.append(q)
        return written

    def asker(self, q: AccountantQuestion) -> AccountantProfile | None:
        """Who the answer goes to: the accountant who asked, else the company's accountant (§28, §51)."""
        if q.accountant_id:
            found = next((a for a in self.repo.accountants() if a.id == q.accountant_id), None)
            if found is not None:
                return found
        return self.repo.accountant_for(q.company_id)

    def sent(self, q: AccountantQuestion, at: datetime) -> None:
        """A transport delivered my answer: only now is the question answered for the accountant."""
        q.status = "answered"
        self.repo.closure_log.append(ClosureActivity(
            kind=ClosureKind.ACCOUNTANT_QUESTION_RESOLVED, at=at, entity_id=q.company_id, subject_id=q.id,
            period=q.period))
        rec = next((r for r in self.repo.transactions.values() if r.evidence_id in q.answer_evidence_ids), None)
        about = ""
        if rec is not None:
            about = (f" about the {format_money(abs(rec.tx.amount), rec.tx.currency)} payment to "
                     f"{self.o.merchant_name(rec.tx)}")
        self.o.activity(at, "answered", f"Answered your accountant{about}.", q.company_id,
                        evidence_ids=q.answer_evidence_ids)

    def _facts(self, rec: TxRecord, doc: DocumentRecord) -> PaymentFacts:
        """What the evidence says about one payment and its document, for checking a question's claims."""
        repo = self.repo
        supplier = repo.resolver().resolve_transaction(rec.tx).supplier
        if supplier is None:
            supplier = repo.suppliers.get(doc.supplier_id or "")
        names = [rec.tx.counterparty, doc.document.supplier_name or ""]
        if supplier is not None:
            names += [supplier.name, *supplier.aliases]
        company = repo.companies.get(rec.company_id)
        own = (company.name, repo.legal_names.get(company.id, company.name)) if company else ()
        others = tuple(n for c in repo.companies.values() if company is None or c.id != company.id
                       for n in (c.name, repo.legal_names.get(c.id, c.name)))
        word = _DOC_LABELS.get(doc.document.doc_type, "Document").lower()
        number = doc.document.invoice_number
        return PaymentFacts(
            amount=abs(rec.tx.amount), currency=rec.tx.currency, booked_on=rec.tx.booked_on,
            kind=_KIND_WORDS.get(rec.tx.kind, "payment"), payee=self.o.merchant_name(rec.tx),
            payee_names=tuple(n for n in names if n), company_names=own, other_company_names=others,
            document=f"{word} {number}" if number else f"its {word}", document_word=word,
            document_date=doc.document.issue_date,
            description=tuple(line for e in doc.evidence_ids for line in description_lines(self.o.evidence_text(e))),
        )


class StagedPaymentsAgent(_Agent):
    """Invoices paid in parts: deposits, milestones and instalments, and parts held back (checklist X8, I2, I4, I5).

    * **Deposits.** Money paid ahead of the work (a bank line that says "sinal", "adiantamento", "deposit",
      "retainer", or names a quote or contract; or the payment of an advance invoice) is recorded as a
      deposit for that customer (or supplier), not as income. The final invoice takes it off when it says
      so and its amounts add up that way (total - deposits = amount due), or when the owner confirms it
      with one tap; the rest is then matched as the balance. A deposit given back when a booking is
      cancelled is linked to it: to the same bank account, it closes both; otherwise one question.
    * **Several payments, part payments.** An invoice keeps what is still to pay on it; each payment that
      quotes it and comes from its customer (or supplier) is kept as a part, with its own evidence, and
      the invoice closes when the parts add up to it exactly. A part payment leaves a plain "€X of €Y
      received". Without the reference, or from someone else: one question, never a guess.
    * **Held back.** A staged invoice whose customer keeps part of it until the work is accepted
      ("Retenção de garantia 5%") closes for the part paid; the part held back stays owed, due on its
      release date, and is matched when it arrives.

    Named "reconciliation" in the audit and the diagram: this is the matcher's work.
    """

    name = "reconciliation"
    _TYPES = frozenset({DocumentType.INVOICE, DocumentType.INVOICE_RECEIPT, DocumentType.DEBIT_NOTE})

    def __init__(self, orchestrator: Orchestrator) -> None:
        super().__init__(orchestrator)
        # {customer tax number: bank accounts it paid your invoices from}, built once per pass (never stale for
        # long: every pass starts afresh), so a busy tenant is not scanned once per payment and invoice.
        self._customer_accounts: dict[str, set[str]] | None = None

    # ----------------------------------------------------------------- reading a document

    def read(self, record: DocumentRecord) -> None:
        """An advance invoice, the deposits a final invoice takes off, the part held back, its customer."""
        doc = record.document
        if record.supporting or doc.doc_type not in self._TYPES or doc.gross_amount is None \
                or record.id in self.repo.statements:
            return
        record.advance = is_advance_invoice(record.text)
        if record.sales:
            record.customer = customer_name(record.text)
        terms = read_terms(record.text, advance=record.advance)
        if not terms.empty:
            record.terms = terms
        held = terms.held_back_for(doc.gross_amount)
        if held is not None:
            self.repo.retentions[record.id] = RetentionRecord(
                document_id=record.id, direction="in" if record.sales else "out", party=self.party(record),
                amount=held.amount, currency=doc.currency, until=held.until, percent=held.percent)
        if record.advance or not terms.empty:
            self.log("read_terms", subject_id=record.id, evidence_ids=record.evidence_ids,
                     values={"advance": record.advance, "deducted": terms.deducted, "amount_due": terms.amount_due,
                             "held_back": held.amount if held else None,
                             "until": held.until.isoformat() if held and held.until else None},
                     validations=[d.line for d in terms.deductions])

    # ----------------------------------------------------------------- what is still to pay

    def stated(self, record: DocumentRecord) -> Decimal:
        """Deposits the invoice takes off a total that includes them (its own amounts add up that way)."""
        terms, gross = record.terms, record.document.gross_amount
        if terms is None or gross is None or terms.mode(gross) != "includes":
            return _ZERO
        return terms.deducted

    def unapplied(self, record: DocumentRecord) -> Decimal:
        """What the invoice takes off that is not linked to a deposit or advance invoice yet."""
        if not self.stated(record):
            return _ZERO
        return sum((d.amount for i, d in enumerate(record.terms.deductions) if i not in record.applied), _ZERO)

    def held(self, record: DocumentRecord) -> Decimal:
        found = self.repo.retentions.get(record.id)
        return found.amount if found is not None and found.status == "held" else _ZERO

    def received(self, record: DocumentRecord) -> Decimal:
        """Every part paid or taken off so far: payments, deposits and advance invoices."""
        return sum(record.part_paid.values(), _ZERO) + sum(record.netted.values(), _ZERO)

    def is_staged(self, record: DocumentRecord) -> bool:
        """Paid in parts: some part is linked to it already, or part of it is held back. A deposit it only says it
        takes off changes nothing until it is linked (so a stated deposit never blocks the invoice's own payment)."""
        if record.document.gross_amount is None or record.document.doc_type is DocumentType.CREDIT_NOTE:
            return False
        return bool(record.part_paid or record.netted or record.id in self.repo.retentions)

    def balance(self, record: DocumentRecord) -> Decimal | None:
        """What is still to pay on an invoice paid in parts (None for one that is not)."""
        if not self.is_staged(record):
            return None
        total = abs(record.document.gross_amount or _ZERO)
        return total - self.received(record) - self.held(record)

    def matchable(self, record: DocumentRecord) -> bool:
        left = self.balance(record)
        return left is None or left > 0

    def open_for_parts(self, record: DocumentRecord) -> bool:
        left = self.balance(record)
        return left is not None and left > 0

    def settled(self, record: DocumentRecord) -> bool:
        """Every part is in: nothing left to pay (a part held back by the customer aside)."""
        return self.balance(record) == _ZERO

    def room(self, record: DocumentRecord) -> Decimal:
        """How much a deposit or payment could still fill on it."""
        left = self.balance(record)
        return abs(record.document.gross_amount or _ZERO) if left is None else left

    # ----------------------------------------------------------------- who, which, how much

    def party(self, record: DocumentRecord) -> str:
        """The customer of your own invoice, or the supplier of a purchase, as the owner reads it."""
        if record.sales:
            return display_name(record.customer, fallback="the customer") if record.customer else "the customer"
        return display_name(record.document.supplier_name)

    def number(self, record: DocumentRecord) -> str:
        return record.document.invoice_number or "on file"

    def invoice_words(self, record: DocumentRecord) -> str:
        """'invoice FT HT2026/31' (yours) or 'the Papelaria Norte invoice FT A/183' (a supplier's)."""
        if record.sales:
            return f"invoice {self.number(record)}"
        return f"the {display_name(record.document.supplier_name)} invoice {self.number(record)}"

    def money(self, amount: Decimal, record: DocumentRecord) -> str:
        return format_money(amount, record.document.currency)

    def company_of(self, record: DocumentRecord) -> str | None:
        return record.document.entity_id or self.repo.item_company(self.repo.items[record.item_id])

    def references(self, rec: TxRecord, record: DocumentRecord) -> bool:
        """The bank line quotes the invoice's number or payment reference."""
        tx, doc = rec.tx, record.document
        if not doc.invoice_number and not doc.payment_reference:
            return False
        folded = bank_fold(f"{tx.counterparty} | {tx.description} | {tx.reference or ''}")
        texts = [(folded, squash(folded))]
        if identifier_in(doc.invoice_number, texts) or identifier_in(doc.payment_reference, texts):
            return True
        return bool(doc.payment_reference and tx.reference and squash(doc.payment_reference) == squash(tx.reference))

    def party_matches(self, rec: TxRecord, record: DocumentRecord) -> bool:
        """The payment comes from the invoice's customer (yours) or goes to its supplier (a purchase)."""
        repo = self.repo
        if record.sales:
            payer = counterparty_key(rec.tx.counterparty)
            if payer and record.customer and counterparty_key(record.customer) == payer:
                return True
            iban = normalize_iban(rec.tx.counterparty_iban) if rec.tx.counterparty_iban else None
            customer = record.document.customer_tax_id
            if not iban or not customer:
                return False
            return iban in self._accounts_of(customer)
        supplier = repo.resolver().resolve_transaction(rec.tx).supplier
        if supplier is None:
            return False
        return supplier.id == record.supplier_id or bool(
            supplier.tax_id and record.document.supplier_tax_id
            and same_tax_id(supplier.tax_id, record.document.supplier_tax_id))

    def _accounts_of(self, customer_tax_id: str) -> set[str]:
        """The bank accounts a customer (by tax number) paid your own invoices from before."""
        if self._customer_accounts is None:
            repo = self.repo
            found: dict[str, set[str]] = {}
            for r in repo.transactions.values():
                if r.tx.amount <= 0 or not r.tx.counterparty_iban:
                    continue
                for d in r.document_ids:
                    doc = repo.documents.get(d)
                    if doc is not None and doc.sales and doc.document.customer_tax_id:
                        key = normalize_tax_id(doc.document.customer_tax_id) or doc.document.customer_tax_id
                        found.setdefault(key, set()).add(normalize_iban(r.tx.counterparty_iban))
            self._customer_accounts = found
        return self._customer_accounts.get(normalize_tax_id(customer_tax_id) or customer_tax_id, set())

    def _usable(self, record: DocumentRecord) -> bool:
        """An invoice a part can be linked to: read and checked, not on hold, not waiting for an answer."""
        item = self.repo.items[record.item_id]
        return (not record.on_hold and record.document.quality is Quality.GREEN and not record.supporting
                and not record.advance and record.document.gross_amount is not None
                and record.document.doc_type in self._TYPES
                and item.stage not in (Stage.NEEDS_OWNER, Stage.CONFLICT))

    def _open_tx(self, rec: TxRecord) -> bool:
        item = self.repo.items[rec.item_id]
        return (not rec.document_ids and not rec.private and rec.tx.entity_id is not None and not item.is_done
                and item.stage not in (Stage.NEEDS_OWNER, Stage.CONFLICT))

    def _asked(self, rec_id: str, kinds: Sequence[str] = ()) -> bool:
        """An open question about this payment (of one of ``kinds``, when given)."""
        return any(n.subject_id == rec_id and n.status == "open" and (not kinds or n.kind in kinds)
                   for n in self.repo.needs.values())

    def _same_way(self, rec: TxRecord, record: DocumentRecord) -> bool:
        """Money in for your own invoice, money out for a supplier's, same company, same currency."""
        return ((rec.tx.amount > 0) == record.sales and rec.tx.currency == record.document.currency
                and self.company_of(record) in (None, rec.company_id))

    # ----------------------------------------------------------------- matches from the engine

    def in_parts(self, m: Match) -> bool:
        """A match that pays an invoice in parts: several payments, a part payment, or the rest of one."""
        docs = [self.repo.documents[d] for d in m.document_ids]
        if any(d.document.doc_type is DocumentType.CREDIT_NOTE for d in docs):
            return False
        return m.kind in (MatchKind.MANY_TO_ONE, MatchKind.PARTIAL) or any(self.is_staged(d) for d in docs)

    def proven(self, m: Match) -> bool:
        """Parts the engine was not certain of, kept only on proof: one invoice, the exact money, no near
        amount or currency conversion, and every payment quotes the invoice and comes from its customer (or
        goes to its supplier). Anything less waits for the owner (§3, §19)."""
        if len(m.document_ids) != 1 or m.quality is Quality.RED or m.is_ambiguous:
            return False
        if m.tags & {MatchTag.NEAR_AMOUNT, MatchTag.FX, MatchTag.SEARCH_CAPPED, MatchTag.AMBIGUOUS}:
            return False
        record = self.repo.documents[m.document_ids[0]]
        if record.document.quality is not Quality.GREEN or record.on_hold:
            return False
        self._customer_accounts = None  # payments matched earlier in this pass count too
        txs = [self.repo.transactions[t] for t in m.transaction_ids]
        return all(self._same_way(r, record) and self.references(r, record) and self.party_matches(r, record)
                   for r in txs)

    def record_parts(self, m: Match) -> None:
        """Keep each payment of the match as a part of its invoice, with its own evidence and a plain "Why?"."""
        repo = self.repo
        for a in m.allocations:
            record = repo.documents[a.document_id]
            record.part_paid[a.transaction_id] = record.part_paid.get(a.transaction_id, _ZERO) + abs(a.amount)
            if a.transaction_id not in record.matched_tx_ids:
                record.matched_tx_ids.append(a.transaction_id)
        for t in m.transaction_ids:
            rec = repo.transactions[t]
            rec.document_ids = list(m.document_ids)
            rec.likely_document_ids = []
            record = repo.documents[m.document_ids[0]]
            customer = [f"Customer: {self.party(record)}, as on the invoice"] if record.sales and \
                self.party_matches(rec, record) else []
            rec.match_why = (*m.why, *customer, *self.progress_lines(record))
            rec.match_headline = self.headline(record, len(m.transaction_ids))
        self.log("keep_parts", subject_id=m.id,
                 evidence_ids=[*(repo.transactions[t].evidence_id for t in m.transaction_ids),
                               *(e for d in m.document_ids for e in repo.documents[d].evidence_ids)],
                 values={"parts": [[a.transaction_id, a.document_id, abs(a.amount)] for a in m.allocations]},
                 response={"kind": m.kind.value, "quality": m.quality.value})

    def progress_lines(self, record: DocumentRecord) -> tuple[str, ...]:
        """'Received so far: €500.00 of €1,230.00', 'Still to come: €730.00', the part held back."""
        total = abs(record.document.gross_amount or _ZERO)
        so_far = "Received so far" if record.sales else "Paid so far"
        lines = [f"{so_far}: {self.money(self.received(record), record)} of {self.money(total, record)}"]
        left = self.balance(record) or _ZERO
        if left > 0:
            lines.append(f"{'Still to come' if record.sales else 'Still to pay'}: {self.money(left, record)}")
        held = self.repo.retentions.get(record.id)
        if held is not None and held.status == "held":
            lines.append(f"{'Held back by the customer' if record.sales else 'Held back by you'}: "
                         f"{self.money(held.amount, record)}{self._until(held)}")
        return tuple(lines)

    def _until(self, held: RetentionRecord) -> str:
        return f" until {day_month(held.until, self.repo.today())}" if held.until else \
            " until the work is accepted"

    def headline(self, record: DocumentRecord, payments: int = 1) -> str:
        """One calm sentence for a payment that pays part of an invoice (§36, §69)."""
        words = self.invoice_words(record)
        left = self.balance(record) or _ZERO
        if left > 0:
            still = "is still to come" if record.sales else "is still to pay"
            return f"Part of {words}. {self.money(left, record)} {still}."
        if payments > 1:
            return f"{payments} payments cover {words}."
        held = self.repo.retentions.get(record.id)
        if held is not None and held.status == "held":
            money = self.money(held.amount, record)
            return f"The rest of {words}, apart from the {money} held back{self._until(held)}."
        return f"The rest of {words}. It is paid in full."

    def held_sentence(self, held: RetentionRecord) -> str:
        """'€500.00 held back by the customer until 30 June 2027.' (never 'retention', §36)."""
        record = self.repo.documents[held.document_id]
        money = self.money(held.amount, record)
        if held.direction == "in":
            return f"{money} held back by the customer until {self._until(held).removeprefix(' until ')}."
        return f"{money} you hold back from {held.party} until {self._until(held).removeprefix(' until ')}."

    def cost_center(self, dep: DepositRecord) -> CostCenter | None:
        """The job, event or client the deposit is for, when its payment is on exactly one."""
        allocation = self.repo.transactions[dep.tx_id].tx.cost_allocation
        if allocation is None or allocation.general or len(allocation.shares) != 1:
            return None
        return self.repo.cost_centers.get(allocation.shares[0].cost_center_id)

    def deposit_text(self, dep: DepositRecord) -> str:
        """Where a deposit stands, in one plain sentence."""
        repo = self.repo
        money = format_money(dep.amount, dep.currency)
        when = day_month(dep.received_on, repo.today())
        if dep.security:
            return self._security_text(dep, money, when)
        base = (f"Deposit of {money} from {dep.party} on {when}" if dep.direction == "in" else
                f"Deposit of {money} paid to {dep.party} on {when}")
        center = self.cost_center(dep)
        if center is not None:
            base += f" for {center.label}"
        final = repo.documents.get(dep.applied_to or "")
        advance = repo.documents.get(dep.advance_document_id or "")
        if dep.status == "applied" and final is not None:
            return f"{base}, taken off {self.invoice_words(final)}."
        if dep.status == "refunded":
            return f"{base}, given back in full."
        if dep.returned:
            return (f"{base}. {format_money(dep.returned, dep.currency)} of it went back; "
                    f"{format_money(dep.available, dep.currency)} was kept.")
        if advance is not None:
            return f"{base}, on advance invoice {self.number(advance)}. The final invoice will take it off."
        whose = "your" if dep.direction == "in" else "its"
        return f"{base}. It is kept as a deposit until {whose} invoice takes it off."

    def _security_text(self, dep: DepositRecord, money: str, when: str) -> str:
        """Where a security deposit stands: held for the customer, given back, or partly kept (X9)."""
        base = f"Security deposit of {money} from {dep.party} on {when}"
        back = format_money(dep.returned, dep.currency)
        went = f"{back} went back to them; " if dep.returned else ""
        final = self.repo.documents.get(dep.applied_to or "")
        if dep.status == "applied" and final is not None:
            kept = format_money(dep.available, dep.currency)
            return f"{base}. {went}{kept} was kept for {self.invoice_words(final)}."
        if dep.status == "kept":
            return f"{base}. {went}{format_money(dep.kept, dep.currency)} was kept, as you confirmed."
        if dep.status == "refunded":
            return f"{base}, given back in full."
        if dep.returned:
            return f"{base}. {went}{format_money(dep.available, dep.currency)} is still held for them."
        return f"{base}, held for them. It is theirs until it goes back or you keep part of it."

    def waiting_sentence(self, record: DocumentRecord) -> str:
        """Why an invoice paid in parts is still open, in plain words."""
        left = self.balance(record) or _ZERO
        total = self.money(abs(record.document.gross_amount or _ZERO), record)
        received = self.money(self.received(record), record)
        missing = self.unapplied(record)
        if left > 0 and left == missing:  # everything else is in: only the deposit it takes off is not found yet
            words = self.invoice_words(record)
            return (f"{words[:1].upper()}{words[1:]} takes off a {self.money(missing, record)} deposit that I can't "
                    "find in your bank records yet.")
        if left > 0 and record.sales:
            return (f"{received} of {total} received for invoice {self.number(record)}. "
                    f"{self.money(left, record)} is still to come.")
        if left > 0:
            return (f"{received} of {total} paid on {self.invoice_words(record)}. "
                    f"{self.money(left, record)} is still to pay.")
        return ""

    def _released(self, record: DocumentRecord) -> set[str]:
        """Payments that paid exactly the part held back on this invoice (not the rest together with it)."""
        held = self.repo.retentions.get(record.id)
        if held is None:
            return set()
        return {t for t in held.released_tx_ids if record.part_paid.get(t) == held.amount}

    def closing_note(self, record: DocumentRecord) -> str:
        """'Paid in full: the €1,500.00 deposit of 2 August and €3,500.00 on 20 September.'"""
        repo = self.repo
        parts: list[str] = []
        for doc_id, amount in record.netted.items():
            other = repo.documents.get(doc_id)
            parts.append(f"advance invoice {self.number(other) if other else 'on file'} ({self.money(amount, record)})")
        paid = sorted((repo.transactions[t] for t in record.part_paid if t in repo.transactions),
                      key=lambda r: (r.tx.booked_on, r.id))
        released = self._released(record)
        for rec in paid:
            amount = self.money(record.part_paid[rec.id], record)
            when = day_month(rec.tx.booked_on, repo.today())
            dep = repo.deposits.get(rec.id)
            if dep is not None and dep.applied_to == record.id:
                parts.append(f"the {amount} deposit of {when}")
            elif rec.id in released:
                parts.append(f"the {amount} held back, paid on {when}")
            else:
                parts.append(f"{amount} on {when}")
        held = repo.retentions.get(record.id)
        if held is not None and held.status == "held":
            return f"Paid: {join_and(parts)}. {self.held_sentence(held)}"
        return f"Paid in full: {join_and(parts)}."

    # ----------------------------------------------------------------- the pass (before matching)

    def settle(self, now: datetime) -> int:
        """Record deposits, link them to advance and final invoices, match parts held back and deposits given
        back. Runs before matching, so the rest of an invoice matches what is still to pay on it."""
        if not self.repo.transactions:
            return 0
        self._customer_accounts = None
        moved = self._register_deposits(now)
        moved += self._link_advance_invoices(now)
        moved += self._apply_stated(now)
        moved += self._release_held(now)
        moved += self._refund_deposits(now)
        moved += self._keep_security(now)
        return moved

    def _register_deposits(self, now: datetime) -> int:
        repo = self.repo
        moved = 0
        for dep in repo.deposits.values():
            # Matched to a document since it was recorded (it quoted an invoice after all, or its advance invoice
            # matched it): that document now settles it, so it is no longer waiting for one.
            rec = repo.transactions[dep.tx_id]
            if dep.status != "held" or dep.advance_document_id is not None or not rec.document_ids:
                continue
            first = repo.documents.get(rec.document_ids[0])
            if first is not None and first.advance:
                dep.advance_document_id = first.id
            elif first is not None:
                dep.status, dep.applied_to = "applied", first.id
        quoted: list[DocumentRecord] | None = None  # invoices still open, which a payment may quote
        for rec in sorted(repo.transactions.values(), key=lambda r: (r.tx.booked_on, r.id)):
            if rec.id in repo.deposits or rec.private or rec.tx.entity_id is None or rec.decision is None:
                continue
            advance = None
            if len(rec.document_ids) == 1 and rec.document_ids[0] in repo.documents:
                advance = repo.documents[rec.document_ids[0]]
                if not advance.advance:
                    continue
            elif rec.document_ids or repo.items[rec.item_id].is_done:
                continue
            words = None
            security = None
            if advance is None:
                wanted = EvidenceExpectation.SALES_INVOICE if rec.tx.amount > 0 else EvidenceExpectation.INVOICE
                if rec.decision.expectation is not wanted:
                    continue
                if rec.tx.amount > 0:  # a customer's refundable security deposit (X9), before any other deposit
                    security = security_deposit_wording(rec.tx.counterparty, rec.tx.description, rec.tx.reference)
                    if security is not None and security.giving_back:
                        continue  # money back saying it returns a deposit is not a deposit received
                words = None if security is not None else deposit_wording(
                    rec.tx.counterparty, rec.tx.description, rec.tx.reference, outgoing=rec.tx.amount < 0)
                if words is None and security is None:
                    continue
                if quoted is None:
                    quoted = [d for d in repo.documents.values() if d.document.invoice_number and not d.supporting
                              and not d.advance and not repo.items[d.item_id].is_done]
                if any(self._same_way(rec, d) and self.references(rec, d) for d in quoted):
                    continue  # it quotes an invoice of its own: it pays that invoice, it is not money paid ahead
            incoming = rec.tx.amount > 0
            party = display_name(rec.tx.counterparty) if incoming else self.o.merchant_name(rec.tx)
            why = security.why if security is not None else words.why if words is not None else (
                f"It pays {'your' if incoming else 'the'} advance invoice {self.number(advance)}.")
            dep = DepositRecord(tx_id=rec.id, company_id=rec.company_id, direction="in" if incoming else "out",
                                party=party, amount=abs(rec.tx.amount), currency=rec.tx.currency,
                                received_on=rec.tx.booked_on, why=why,
                                reference=words.reference if words is not None else None,
                                advance_document_id=advance.id if advance is not None else None,
                                security=security is not None)
            repo.deposits[rec.id] = dep
            values: dict[str, Any] = {"direction": dep.direction, "amount": dep.amount, "reference": dep.reference,
                                      "advance_invoice": dep.advance_document_id}
            if dep.security:
                values["security"] = True
                # The customer's money, held for them: no invoice covers it, its return does (X9).
                rec.decision = ExpectationDecision(
                    rec.tx.id, EvidenceExpectation.BANK_EVIDENCE_SUFFICES,
                    f"A security deposit held for {party}. It closes when it goes back to them: no invoice needed.",
                    Quality.AMBER, "security_deposit")
                self.o.reconciliation.log("expect", subject_id=rec.id, evidence_ids=[rec.evidence_id],
                                          values={"expectation": rec.decision.expectation.value,
                                                  "rule": rec.decision.rule},
                                          response={"quality": rec.decision.quality.value,
                                                    "reason": rec.decision.reason})
            self.log("record_deposit", subject_id=rec.id,
                     evidence_ids=[rec.evidence_id, *(advance.evidence_ids if advance else [])],
                     values=values, response={"why": why})
            money = format_money(dep.amount, dep.currency)
            text = (f"Recorded a {money} security deposit from {party}. I hold it for them: it is not income."
                    if dep.security else
                    f"Recorded a {money} deposit from {party}. It is not income until the work is invoiced."
                    if incoming else f"Recorded a {money} deposit paid to {party}. Its invoice will take it off.")
            self.o.activity(now, "checked", text, rec.company_id, amount=dep.amount, currency=dep.currency,
                            evidence_ids=[rec.evidence_id])
            moved += 1
        return moved

    def _link_advance_invoices(self, now: datetime) -> int:
        """A deposit and the advance invoice made for it: the same customer (or supplier), the same amount,
        dated around it, and only one way to pair them."""
        repo = self.repo
        deposits = [d for d in repo.deposits.values() if d.status == "held" and d.advance_document_id is None
                    and not d.security and d.available == d.amount and self._open_tx(repo.transactions[d.tx_id])]
        advances = [r for r in repo.documents.values() if r.advance and not r.matched_tx_ids and not r.on_hold
                    and r.document.quality is Quality.GREEN and not repo.items[r.item_id].is_done
                    and repo.items[r.item_id].stage not in (Stage.NEEDS_OWNER, Stage.CONFLICT)]
        if not deposits or not advances:
            return 0
        fits: dict[str, list[DocumentRecord]] = {}
        for dep in deposits:
            rec = repo.transactions[dep.tx_id]
            for adv in advances:
                day = adv.document.issue_date
                if not self._same_way(rec, adv) or abs(adv.document.gross_amount or _ZERO) != dep.amount:
                    continue
                if day is not None and not -7 <= (day - dep.received_on).days <= 45:
                    continue
                if self.party_matches(rec, adv):
                    fits.setdefault(dep.tx_id, []).append(adv)
        moved = 0
        for tx_id, found in sorted(fits.items()):
            if len(found) != 1 or sum(1 for other in fits.values() if found[0] in other) != 1:
                continue  # more than one way to pair them: never on a guess
            adv, rec, dep = found[0], repo.transactions[tx_id], repo.deposits[tx_id]
            rec.document_ids, rec.likely_document_ids = [adv.id], []
            adv.matched_tx_ids = [rec.id]
            dep.advance_document_id = adv.id
            who = "Customer" if rec.tx.amount > 0 else "Supplier"
            rec.match_why = (f"Advance invoice total: {self.money(abs(adv.document.gross_amount or _ZERO), adv)}",
                             f"{'Money received' if rec.tx.amount > 0 else 'Bank charge'}: "
                             f"{format_money(abs(rec.tx.amount), rec.tx.currency)}",
                             f"{who}: {dep.party}, as on the invoice", dep.why.removesuffix("."))
            rec.match_headline = f"Deposit paid by advance invoice {self.number(adv)}."
            self.log("match", subject_id=rec.id, evidence_ids=[rec.evidence_id, *adv.evidence_ids],
                     values={"transactions": [rec.id], "documents": [adv.id]}, validations=list(rec.match_why),
                     response={"quality": Quality.GREEN.value, "kind": "advance_invoice"})
            moved += 1
        return moved

    def _deposits_for(self, record: DocumentRecord, amount: Decimal | None = None, on: date | None = None,
                      ) -> list[DepositRecord]:
        """Held deposits that can be part of this invoice: same customer or supplier, company and currency,
        paid before it (a week's grace), of this amount when one is given."""
        repo = self.repo
        issued = record.document.issue_date or repo.today()
        out = []
        for dep in sorted(repo.deposits.values(), key=lambda d: (d.received_on, d.tx_id)):
            rec = repo.transactions[dep.tx_id]
            if dep.status != "held" or dep.advance_document_id is not None or dep.available <= 0 or dep.security:
                continue  # a security deposit is the customer's money: kept only by :meth:`_keep_security`
            if record.id in dep.not_for_document_ids or not self._open_tx(rec) or not self._same_way(rec, record):
                continue
            if (dep.received_on - issued).days > 7 or (amount is not None and dep.available != amount):
                continue
            if on is not None and dep.received_on != on:
                continue
            if self.party_matches(rec, record):
                out.append(dep)
        return out

    def _apply_stated(self, now: datetime) -> int:
        """The deposits and advance invoices a final invoice says it takes off, linked when the evidence is
        exact: the advance invoice it names, or the one deposit of that amount from that customer."""
        repo = self.repo
        moved = 0
        for record in sorted(repo.documents.values(), key=lambda d: d.id):
            terms = record.terms
            if terms is None or not terms.deductions or repo.items[record.item_id].is_done \
                    or not self._usable(record):
                continue
            mode = terms.mode(record.document.gross_amount or _ZERO)
            claimed: set[str] = set(record.applied.values())
            for i, ded in enumerate(terms.deductions):
                if i in record.applied:
                    continue
                advance = self._advance_named(record, ded.number) if ded.number else None
                if advance is not None:
                    if mode == "includes" and abs(advance.document.gross_amount or _ZERO) == ded.amount:
                        record.netted[advance.id] = ded.amount
                    elif mode != "inside":
                        continue
                    record.applied[i] = advance.id
                    claimed.add(advance.id)
                    if advance.id not in record.linked_advances:
                        record.linked_advances.append(advance.id)
                    for dep in repo.deposits.values():
                        if dep.advance_document_id == advance.id and dep.status == "held":
                            dep.status, dep.applied_to = "applied", record.id
                    self.log("take_off_advance", subject_id=record.id,
                             evidence_ids=[*record.evidence_ids, *advance.evidence_ids],
                             values={"advance_invoice": advance.id, "amount": ded.amount, "mode": mode},
                             validations=[ded.line])
                    self.o.activity(now, "checked", f"Linked advance invoice {self.number(advance)} to "
                                    f"{self.invoice_words(record)}.", self.company_of(record),
                                    amount=ded.amount, currency=record.document.currency,
                                    evidence_ids=record.evidence_ids)
                    moved += 1
                    continue
                if mode != "includes":
                    continue
                found = [d for d in self._deposits_for(record, ded.amount, ded.on) if d.tx_id not in claimed]
                if len(found) != 1 or any(
                        j != i and j not in record.applied and d.amount == ded.amount and d.on in (None, ded.on)
                        for j, d in enumerate(terms.deductions)):
                    continue  # none, or more than one way to pair them: the owner is asked instead
                self.apply_deposit(record, found[0], index=i, answer_ev=None, now=now, stated=ded.line)
                claimed.add(found[0].tx_id)
                moved += 1
        return moved

    def _advance_named(self, record: DocumentRecord, number: str | None) -> DocumentRecord | None:
        if not number:
            return None
        return next((d for d in sorted(self.repo.documents.values(), key=lambda d: d.id)
                     if d.id != record.id and d.document.invoice_number
                     and _same_number(d.document.invoice_number, number)
                     and same_tax_id(d.document.supplier_tax_id, record.document.supplier_tax_id)), None)

    def apply_deposit(self, record: DocumentRecord, dep: DepositRecord, *, index: int | None, answer_ev: str | None,
                      now: datetime, stated: str = "") -> None:
        """Take a held deposit off a final invoice: its payment becomes a part of that invoice."""
        repo = self.repo
        rec = repo.transactions[dep.tx_id]
        amount = dep.available
        record.part_paid[rec.id] = amount
        if rec.id not in record.matched_tx_ids:
            record.matched_tx_ids.append(rec.id)
        if index is None and record.terms is not None and self.stated(record):
            index = next((i for i, d in enumerate(record.terms.deductions)
                          if i not in record.applied and d.amount == amount), None)
        if index is not None:
            record.applied[index] = rec.id
        if answer_ev:
            record.part_answers.append(answer_ev)
            rec.part_answer_ev = answer_ev
        rec.document_ids, rec.likely_document_ids = [record.id], []
        dep.status, dep.applied_to = "applied", record.id
        words = self.invoice_words(record)
        said = (f"{words[:1].upper()}{words[1:]} takes off: {self.money(amount, record)}" if stated else
                "You said: it is part of this invoice")
        who = "Customer" if record.sales else "Supplier"
        rec.match_why = (f"Deposit {'received' if record.sales else 'paid'}: {self.money(amount, record)} on "
                         f"{day_month(dep.received_on, repo.today())}", said,
                         f"{who}: {dep.party}, as on the invoice", *self.progress_lines(record))
        rec.match_headline = f"Deposit taken off {words}."
        if dep.security:
            money = format_money(dep.amount, dep.currency)
            back = [f"Given back: {format_money(dep.returned, dep.currency)}"] if dep.returned else []
            rec.match_why = (f"Security deposit received: {money} on {day_month(dep.received_on, repo.today())}",
                             *back, f"Kept for {words}: {self.money(amount, record)}",
                             f"{who}: {dep.party}, as on the invoice")
            rec.match_headline = f"The {self.money(amount, record)} kept from the security deposit pays {words}."
        for n in repo.needs.values():
            if n.subject_id == rec.id and n.status == "open" and n.kind == "deposit" and not answer_ev:
                n.status, n.resolution = "resolved", "evidence"
        self.log("take_off_deposit", subject_id=rec.id,
                 evidence_ids=[rec.evidence_id, *record.evidence_ids, *([answer_ev] if answer_ev else [])],
                 values={"invoice": record.id, "amount": amount}, validations=list(rec.match_why),
                 actor=OWNER_ACTOR if answer_ev else SYSTEM, response={"kind": "deposit"})
        line = (f"The {self.money(amount, record)} kept from {dep.party}'s security deposit pays {words}."
                if dep.security else
                f"Took the {self.money(amount, record)} deposit from {dep.party} off {words}." if record.sales else
                f"Took the {self.money(amount, record)} deposit you paid {dep.party} off {words}.")
        self.o.activity(now, "checked", line, rec.company_id, amount=amount, currency=record.document.currency,
                        evidence_ids=[rec.evidence_id, *record.evidence_ids])

    def _release_held(self, now: datetime) -> int:
        """The part held back arrives: the same amount, from the same customer (or to the same supplier),
        quoting the invoice or saying it is the amount held back. Only one way to pair them."""
        repo = self.repo
        held = [r for r in repo.retentions.values() if r.status == "held" and r.document_id in repo.documents]
        if not held:
            return 0
        fits: dict[str, list[RetentionRecord]] = {}
        for rec in sorted(repo.transactions.values(), key=lambda r: r.id):
            if not self._open_tx(rec) or rec.id in repo.deposits:
                continue
            for r in held:
                record = repo.documents[r.document_id]
                left = self.balance(record) or _ZERO
                # The part held back, or everything still owed on it, the part held back included (not held
                # back after all): both settle what was held back.
                if not self._same_way(rec, record) or abs(rec.tx.amount) not in (r.amount, left + r.amount) \
                        or record.on_hold:
                    continue
                if record.document.issue_date and rec.tx.booked_on < record.document.issue_date:
                    continue
                said = self.references(rec, record) or says_held_back(rec.tx.counterparty, rec.tx.description,
                                                                      rec.tx.reference)
                if said and self.party_matches(rec, record):
                    fits.setdefault(rec.id, []).append(r)
        moved = 0
        for tx_id, found in sorted(fits.items()):
            if len(found) != 1 or sum(1 for other in fits.values() if found[0] in other) != 1:
                continue
            self.release(found[0], repo.transactions[tx_id], answer_ev=None, now=now)
            moved += 1
        return moved

    def release(self, held: RetentionRecord, rec: TxRecord, *, answer_ev: str | None, now: datetime) -> None:
        repo = self.repo
        record = repo.documents[held.document_id]
        money = self.money(held.amount, record)
        whole = abs(rec.tx.amount) != held.amount  # the rest of the invoice, the part held back included
        record.part_paid[rec.id] = abs(rec.tx.amount)
        if rec.id not in record.matched_tx_ids:
            record.matched_tx_ids.append(rec.id)
        held.status = "released"
        held.released_tx_ids.append(rec.id)
        if answer_ev:
            record.part_answers.append(answer_ev)
            rec.part_answer_ev = answer_ev
        rec.document_ids, rec.likely_document_ids = [record.id], []
        how = ("You said: it is the amount held back" if answer_ev else
               "Invoice number: found in the bank details" if self.references(rec, record) else
               "The bank line says it is the amount held back")
        who = "Customer" if record.sales else "Supplier"
        rec.match_why = (f"Held back on {self.invoice_words(record)}: {money}",
                         f"{'Money received' if rec.tx.amount > 0 else 'Bank charge'}: "
                         f"{format_money(abs(rec.tx.amount), rec.tx.currency)}", how,
                         f"{who}: {held.party}, as on the invoice")
        words = self.invoice_words(record)
        rec.match_headline = (f"The rest of {words}, the {money} held back included. It is paid in full." if whole
                              else f"The {money} held back on {words}, now paid.")
        self.log("release_held_back", subject_id=rec.id,
                 evidence_ids=[rec.evidence_id, *record.evidence_ids, *([answer_ev] if answer_ev else [])],
                 values={"invoice": record.id, "amount": held.amount}, validations=list(rec.match_why),
                 actor=OWNER_ACTOR if answer_ev else SYSTEM)
        self.o.activity(now, "checked", f"The {money} held back on {self.invoice_words(record)} arrived.",
                        rec.company_id, amount=held.amount, currency=held.currency, evidence_ids=[rec.evidence_id])

    # ----------------------------------------------------------------- deposits given back

    def deposit_payer(self, rec: TxRecord) -> tuple[str, bool] | None:
        """(name, same bank account) when this payment goes back to someone who paid you a deposit you hold."""
        found = self.deposits_given_back_by(rec)
        if not found:
            return None
        iban = normalize_iban(rec.tx.counterparty_iban) if rec.tx.counterparty_iban else None
        same = any(iban and (t := self.repo.transactions[d.tx_id].tx).counterparty_iban
                   and normalize_iban(t.counterparty_iban) == iban for d in found)
        return found[0].party, bool(same)

    def deposits_given_back_by(self, rec: TxRecord) -> list[DepositRecord]:
        """Held deposits this money out could give back: from the same payer (bank account or name), same
        company and currency, paid before it, with at least this much left."""
        repo = self.repo
        if rec.tx.amount >= 0:
            return []
        key = counterparty_key(rec.tx.counterparty)
        iban = normalize_iban(rec.tx.counterparty_iban) if rec.tx.counterparty_iban else None
        out = []
        for dep in sorted(repo.deposits.values(), key=lambda d: (d.received_on, d.tx_id)):
            paid = repo.transactions[dep.tx_id]
            if dep.direction != "in" or dep.status == "applied" or dep.available < abs(rec.tx.amount):
                continue
            if dep.tx_id in rec.not_for_document_ids or dep.company_id != rec.company_id \
                    or dep.currency != rec.tx.currency or dep.received_on > rec.tx.booked_on:
                continue
            came_from = normalize_iban(paid.tx.counterparty_iban) if paid.tx.counterparty_iban else None
            if (iban and came_from == iban) or (key and counterparty_key(paid.tx.counterparty) == key):
                out.append(dep)
        return out

    def _refund_deposits(self, now: datetime) -> int:
        """A deposit given back in full to the bank account it came from: both close, linked (the booking was
        cancelled). Anything else is one question."""
        repo = self.repo
        moved = 0
        for rec in sorted(repo.transactions.values(), key=lambda r: (r.tx.booked_on, r.id)):
            if rec.decision is None or rec.decision.rule != "deposit_refund" or rec.deposit_refund_of \
                    or not self._open_tx(rec):
                continue
            found = self.deposits_given_back_by(rec)
            iban = normalize_iban(rec.tx.counterparty_iban) if rec.tx.counterparty_iban else None
            exact = [d for d in found if d.advance_document_id is None and d.available == abs(rec.tx.amount)
                     and iban and repo.transactions[d.tx_id].tx.counterparty_iban
                     and normalize_iban(repo.transactions[d.tx_id].tx.counterparty_iban) == iban]
            if len(found) == 1 and len(exact) == 1:
                self.give_back(exact[0], rec, answer_ev=None, now=now)
                moved += 1
                continue
            # Part of a security deposit going back (the rest kept for damage, or paid back later): to the same
            # account, saying it gives the security deposit back, and the only one it could be (X9).
            dep = found[0] if len(found) == 1 else None
            said = security_deposit_wording(rec.tx.counterparty, rec.tx.description, rec.tx.reference)
            came_from = repo.transactions[dep.tx_id].tx.counterparty_iban if dep is not None else None
            if dep is not None and dep.security and said is not None and iban and came_from \
                    and normalize_iban(came_from) == iban:
                self.give_back(dep, rec, answer_ev=None, now=now)
                moved += 1
        return moved

    def keep_invoice(self, dep: DepositRecord) -> DocumentRecord | None:
        """Your invoice for the part of a security deposit you keep (damage, charges): to the same customer, for
        exactly what is still held, dated after the deposit, and only one. It counts once some of the deposit
        went back (the rest is what was kept) or when it says it is taken from the security deposit."""
        repo = self.repo
        rec = repo.transactions[dep.tx_id]
        found = []
        for record in sorted(repo.documents.values(), key=lambda d: d.id):
            doc = record.document
            if not record.sales or record.matched_tx_ids or repo.items[record.item_id].is_done \
                    or not self._usable(record) or not self._same_way(rec, record) \
                    or record.id in dep.not_for_document_ids:
                continue
            if abs(doc.gross_amount or _ZERO) != dep.available or (doc.issue_date and doc.issue_date < dep.received_on):
                continue
            if not (dep.returned > 0 or mentions_security_deposit(record.text)):
                continue
            if self.party_matches(rec, record):
                found.append(record)
        return found[0] if len(found) == 1 else None

    def _keep_security(self, now: datetime) -> int:
        """The part of a security deposit that was kept, paid by your invoice for it: the deposit pays that
        invoice (it becomes income with the invoice as its evidence, X9)."""
        moved = 0
        for dep in sorted(self.repo.deposits.values(), key=lambda d: (d.received_on, d.tx_id)):
            if not dep.security or dep.status != "held" or dep.available <= 0 or dep.kept_answer:
                continue
            rec = self.repo.transactions[dep.tx_id]
            if rec.document_ids or rec.private or self.repo.items[rec.item_id].is_done:
                continue
            record = self.keep_invoice(dep)
            if record is None:
                continue
            for n in self.repo.needs.values():
                if n.subject_id == dep.tx_id and n.status == "open" and n.kind == "deposit_kept":
                    n.status, n.resolution = "resolved", "evidence"
            self.apply_deposit(record, dep, index=None, answer_ev=None, now=now)
            moved += 1
        return moved

    def give_back(self, dep: DepositRecord, rec: TxRecord, *, answer_ev: str | None, now: datetime) -> None:
        repo = self.repo
        amount = abs(rec.tx.amount)
        dep.refunds[rec.id] = amount
        if dep.available == 0:
            dep.status = "refunded"
        rec.deposit_refund_of = dep.tx_id
        rec.likely_document_ids = []
        if answer_ev:
            rec.part_answer_ev = answer_ev
        money = format_money(dep.amount, dep.currency)
        when = day_month(dep.received_on, repo.today())
        how = (f"You said: it gives the {dep.noun} back" if answer_ev else
               f"Bank account: the one the {dep.noun} came from")
        rec.match_why = (f"{dep.noun.capitalize()} received: {money} from {dep.party} on {when}",
                         f"Money paid back: {format_money(amount, rec.tx.currency)}", how)
        rec.match_headline = f"Refund of the {money} {dep.noun} {dep.party} paid on {when}."
        if dep.security and dep.available > 0:
            rec.match_why = (*rec.match_why, f"Still held for them: {format_money(dep.available, dep.currency)}")
        self.log("give_back_deposit", subject_id=rec.id,
                 evidence_ids=[rec.evidence_id, repo.transactions[dep.tx_id].evidence_id,
                               *([answer_ev] if answer_ev else [])],
                 values={"deposit": dep.tx_id, "amount": amount}, validations=list(rec.match_why),
                 actor=OWNER_ACTOR if answer_ev else SYSTEM)
        self.o.activity(now, "checked", f"Linked the {format_money(amount, rec.tx.currency)} paid to {dep.party} to "
                        f"the {dep.noun} they paid on {when}.", rec.company_id, amount=amount, currency=rec.tx.currency,
                        evidence_ids=[rec.evidence_id])

    def settled_without_document(self, rec: TxRecord) -> tuple[list[str], str] | None:
        """(evidence, note) for a payment proven by a deposit given back, or a deposit given back in full."""
        repo = self.repo
        if rec.deposit_refund_of:
            dep = repo.deposits.get(rec.deposit_refund_of)
            paid = repo.transactions.get(rec.deposit_refund_of)
            if dep is None or paid is None or dep.refunds.get(rec.id) != abs(rec.tx.amount):
                return None
            evidence = [rec.evidence_id, paid.evidence_id, *([rec.part_answer_ev] if rec.part_answer_ev else [])]
            return evidence, rec.match_headline or "Refund of a deposit."
        dep = repo.deposits.get(rec.id)
        if dep is None or dep.status not in ("refunded", "kept") or dep.available != 0 or rec.document_ids \
                or dep.advance_document_id is not None:
            return None
        refunds = sorted((repo.transactions[t] for t in dep.refunds if t in repo.transactions),
                         key=lambda r: (r.tx.booked_on, r.id))
        if dep.status == "kept":  # a security deposit: what went back, and the rest kept on the owner's word (X9)
            if not dep.security or not dep.kept_answer or dep.returned + dep.kept != dep.amount:
                return None
            evidence = [rec.evidence_id, *(r.evidence_id for r in refunds),
                        *(r.part_answer_ev for r in refunds if r.part_answer_ev), dep.kept_answer]
            kept = format_money(dep.kept, dep.currency)
            if refunds:
                back = format_money(dep.returned, dep.currency)
                on = day_month(refunds[-1].tx.booked_on, repo.today())
                return evidence, f"Security deposit settled: {back} given back on {on}, {kept} kept, as you confirmed."
            return evidence, f"Security deposit kept in full ({kept}), as you confirmed."
        if not refunds or sum(dep.refunds.values(), _ZERO) != dep.amount:
            return None
        evidence = [rec.evidence_id, *(r.evidence_id for r in refunds),
                    *(r.part_answer_ev for r in refunds if r.part_answer_ev)]
        noun = dep.noun.capitalize()
        return evidence, f"{noun} given back in full on {day_month(refunds[-1].tx.booked_on, repo.today())}."

    # ----------------------------------------------------------------- one question, never a guess

    def ask(self, now: datetime) -> None:
        if not self.repo.transactions:
            return
        self._customer_accounts = None
        docs = self._open_documents()
        self._ask_deposits(now, docs)
        self._ask_parts(now, docs)
        self._ask_refunds(now)
        self._ask_kept(now)

    def _ask_kept(self, now: datetime) -> None:
        """Part of a security deposit went back and no invoice is for the rest: did the owner keep it? One question;
        what is kept becomes income only on that answer (X9)."""
        repo = self.repo
        for dep in sorted(repo.deposits.values(), key=lambda d: (d.received_on, d.tx_id)):
            rec = repo.transactions[dep.tx_id]
            if not dep.security or dep.status != "held" or dep.returned <= 0 or dep.available <= 0 \
                    or dep.kept_answer or dep.kept_later_at == dep.returned or self._asked(rec.id) \
                    or not self._open_tx(rec) or self.keep_invoice(dep) is not None:
                continue
            money, back, rest = (format_money(v, dep.currency) for v in (dep.amount, dep.returned, dep.available))
            when = day_month(dep.received_on, repo.today())
            prompt = (f"You gave back {back} of the {money} security deposit {dep.party} paid on {when}. "
                      f"Did you keep the other {rest}?")
            options = [CheckOption(id="kept", label=f"Yes, I kept {rest} for damage or another charge",
                                   values={"deposit": dep.tx_id}),
                       CheckOption(id="later", label="No, it will go back to them later",
                                   values={"deposit": dep.tx_id})]
            why = [f"It is a security deposit: it belongs to {dep.party} until it goes back or you keep part of it.",
                   f"{back} went back to them.",
                   "What you keep counts as income only with your OK or an invoice for it, so I won't count it on a "
                   "guess."]
            refunds = [repo.transactions[t].evidence_id for t in dep.refunds if t in repo.transactions]
            self._new_question("deposit_kept", rec, prompt, options, why, [rec.evidence_id, *refunds], now,
                               f"Asked you whether you kept {rest} of {dep.party}'s security deposit.")

    def _asking(self) -> list[TxRecord]:
        """Payments that could be asked about: open, not waiting for an answer, in booking order."""
        asked = {n.subject_id for n in self.repo.needs.values() if n.status == "open"}
        return sorted((r for r in self.repo.transactions.values() if r.id not in asked and self._open_tx(r)),
                      key=lambda r: (r.tx.booked_on, r.id))

    def _new_question(self, kind: str, rec: TxRecord, prompt: str, options: Sequence[CheckOption],
                      why: Sequence[str], evidence: Sequence[str], now: datetime, note: str) -> None:
        repo = self.repo
        who = display_name(rec.tx.counterparty) if rec.tx.amount > 0 else self.o.merchant_name(rec.tx)
        needs_id = _unique_id(repo.needs, f"nd_{_slug(who.split()[0])}_{kind}")
        repo.needs[needs_id] = NeedsYouRecord(
            id=needs_id, kind=kind, subject_type="transaction", subject_id=rec.id, item_id=rec.item_id,
            company_id=rec.company_id, created_at=now, why=tuple(why), prompt=prompt, options=tuple(options))
        self.log("ask_owner", subject_id=rec.id, evidence_ids=list(evidence),
                 values={"options": [o.id for o in options]}, response={"needs_you": needs_id})
        self.o.activity(now, "checked", note, rec.company_id, amount=abs(rec.tx.amount), currency=rec.tx.currency,
                        evidence_ids=list(evidence))
        self.o.advance(repo.items[rec.item_id], Stage.NEEDS_OWNER, list(evidence), agent=self.name, note=note)

    def _open_documents(self) -> list[DocumentRecord]:
        """Invoices a part could still be linked to, oldest first (worked out once per round of questions)."""
        repo = self.repo
        return sorted((r for r in repo.documents.values() if not repo.items[r.item_id].is_done and self._usable(r)),
                      key=lambda d: (d.document.issue_date or date.min, d.id))

    def _candidates(self, rec: TxRecord, amount: Decimal, docs: Sequence[DocumentRecord], *,
                    deposit: bool) -> list[DocumentRecord]:
        """Invoices this payment (or deposit) may be part of: the same way, room left for it, and quoted by it or
        from the same customer. A supplier's invoice counts only when the payment quotes it (the rest is chased)."""
        out = []
        for record in docs:
            if not self._same_way(rec, record) or record.id in rec.not_for_document_ids or record.credit_for:
                continue
            if not record.sales and not deposit and not self.references(rec, record):
                continue
            terms = record.terms
            if deposit and terms is not None and terms.mode(record.document.gross_amount or _ZERO) == "inside":
                continue  # its total already has its deposits taken off
            staged = self.is_staged(record)
            room = self.room(record)
            if room < amount or (not staged and room == amount and not deposit):
                continue
            issued = record.document.issue_date
            if issued is not None and (rec.tx.booked_on - issued).days < -120:
                continue
            if self.references(rec, record) or self.party_matches(rec, record):
                out.append(record)
        return out

    def _ask_deposits(self, now: datetime, docs: Sequence[DocumentRecord]) -> None:
        """A held deposit and an invoice to the same customer that does not say it takes it off: is it part?"""
        repo = self.repo
        for dep in sorted(repo.deposits.values(), key=lambda d: (d.received_on, d.tx_id)):
            rec = repo.transactions[dep.tx_id]
            if dep.status != "held" or dep.advance_document_id is not None or dep.available <= 0 \
                    or dep.security or not self._open_tx(rec) or self._asked(rec.id):
                continue
            found = [r for r in self._candidates(rec, dep.available, docs, deposit=True)
                     if r.id not in dep.not_for_document_ids and self.party_matches(rec, r)][:3]
            if not found:
                continue
            money = format_money(dep.available, dep.currency)
            when = day_month(dep.received_on, repo.today())
            if rec.tx.amount > 0:
                head = f"{dep.party} paid you a {money} deposit on {when}."
            else:
                head = f"You paid {dep.party} a {money} deposit on {when}."
            if len(found) == 1:
                total = self.money(abs(found[0].document.gross_amount or _ZERO), found[0])
                prompt = f"{head} Is it part of {self.invoice_words(found[0])} ({total})?"
            else:
                prompt = f"{head} Which invoice is it part of?"
            options = [CheckOption(id=f"part:{r.id}", label=f"Yes, take it off {self.invoice_words(r)}",
                                   values={"document": r.id}) for r in found]
            options.append(CheckOption(id="other", label="No, it is for something else",
                                       values={"documents": ",".join(r.id for r in found)}))
            why = [dep.why, *(f"{self.invoice_words(r)[:1].upper()}{self.invoice_words(r)[1:]} is for "
                              f"{self.money(abs(r.document.gross_amount or _ZERO), r)}"
                              f"{'' if r.terms is None or not r.terms.deductions else ' and takes off a deposit'}."
                              for r in found),
                   "Nothing says for sure which invoice it belongs to, so I won't take it off on a guess.",
                   "Until you answer, I keep it as a deposit."]
            self._new_question("deposit", rec, prompt, options, why,
                               [rec.evidence_id, *(e for r in found for e in r.evidence_ids)], now,
                               f"Asked you which invoice the {money} deposit from {dep.party} is part of."
                               if rec.tx.amount > 0 else
                               f"Asked you which invoice the {money} deposit paid to {dep.party} is part of.")

    def _ask_parts(self, now: datetime, docs: Sequence[DocumentRecord]) -> None:
        """A payment that may be part of an invoice (or the part held back) but is not proven: one question."""
        repo = self.repo
        if not docs and not repo.retentions:
            return
        for rec in self._asking():
            if rec.id in repo.deposits or rec.decision is None:
                continue
            wanted = EvidenceExpectation.SALES_INVOICE if rec.tx.amount > 0 else EvidenceExpectation.INVOICE
            if rec.decision.expectation is not wanted:
                continue
            amount = abs(rec.tx.amount)
            found = self._candidates(rec, amount, docs, deposit=False)[:3]
            if rec.likely_document_ids and not set(rec.likely_document_ids) & {r.id for r in found}:
                continue  # it likely pays another document in full: that one is being confirmed, not asked here
            held = [h for h in repo.retentions.values()
                    if h.status == "held" and h.amount == amount and h.document_id in repo.documents
                    and h.document_id not in rec.not_for_document_ids
                    and self._same_way(rec, repo.documents[h.document_id])
                    and not repo.documents[h.document_id].on_hold
                    and self.party_matches(rec, repo.documents[h.document_id])][:2]
            if not found and not held:
                continue
            who = display_name(rec.tx.counterparty) if rec.tx.amount > 0 else self.o.merchant_name(rec.tx)
            money = format_money(amount, rec.tx.currency)
            when = day_month(rec.tx.booked_on, repo.today())
            head = f"{who} paid {money} on {when}." if rec.tx.amount > 0 else f"You paid {who} {money} on {when}."
            options: list[CheckOption] = []
            why: list[str] = []
            for r in found:
                left = self.balance(r)
                rest = left is not None and left == amount
                label = (f"Yes, the rest of {self.invoice_words(r)}" if rest else
                         f"Yes, part of {self.invoice_words(r)}")
                options.append(CheckOption(id=f"part:{r.id}", label=label, values={"document": r.id}))
                total = self.money(abs(r.document.gross_amount or _ZERO), r)
                still = f", {self.money(left, r)} of it still to come" if left is not None and r.sales else \
                    f", {self.money(left, r)} of it still to pay" if left is not None else ""
                why.append(f"{self.invoice_words(r)[:1].upper()}{self.invoice_words(r)[1:]} is for {total}{still}.")
            for h in held:
                record = repo.documents[h.document_id]
                options.append(CheckOption(id=f"held:{record.id}",
                                           label=f"Yes, the {self.money(h.amount, record)} held back on "
                                                 f"{self.invoice_words(record)}", values={"document": record.id}))
                why.append(self.held_sentence(h))
            if len(options) == 1:
                target = options[0].label.removeprefix("Yes, ")
                prompt = f"{head} Is it {target}?"
            else:
                prompt = f"{head} What is it for?"
            options.append(CheckOption(id="other", label="No, it is for something else",
                                       values={"documents": ",".join([*(r.id for r in found),
                                                                      *(h.document_id for h in held)])}))
            why += ["The payment does not quote the invoice, or comes from someone else, so I won't link them on a "
                    "guess." if found else "The bank line does not say what it is for, so I won't link them on a "
                                           "guess.",
                    "Until you answer, I won't close this payment."]
            evidence = [rec.evidence_id, *(e for r in found for e in r.evidence_ids),
                        *(e for h in held for e in repo.documents[h.document_id].evidence_ids)]
            self._new_question("part", rec, prompt, options, why, evidence, now,
                               f"Asked you what the {money} from {who} is for." if rec.tx.amount > 0 else
                               f"Asked you what the {money} paid to {who} is for.")

    def _ask_refunds(self, now: datetime) -> None:
        """Money out to someone whose deposit you hold, not proven to give it back: one question."""
        repo = self.repo
        for rec in sorted(repo.transactions.values(), key=lambda r: (r.tx.booked_on, r.id)):
            if rec.decision is None or rec.decision.rule != "deposit_refund" or rec.deposit_refund_of \
                    or not self._open_tx(rec) or self._asked(rec.id):
                continue
            found = self.deposits_given_back_by(rec)[:3]
            if not found:
                continue
            money = format_money(abs(rec.tx.amount), rec.tx.currency)
            when = day_month(rec.tx.booked_on, repo.today())
            who = found[0].party
            options = [CheckOption(id=f"refund:{d.tx_id}",
                                   label=f"Yes, it gives back the {format_money(d.available, d.currency)} deposit of "
                                         f"{day_month(d.received_on, repo.today())}", values={"deposit": d.tx_id})
                       for d in found]
            options.append(CheckOption(id="other", label="No, it is for something else",
                                       values={"deposits": ",".join(d.tx_id for d in found)}))
            if len(found) == 1:
                d = found[0]
                deposit = format_money(d.available, d.currency)
                prompt = (f"You paid {who} {money} on {when}. Does it give back the {deposit} deposit they paid on "
                          f"{day_month(d.received_on, repo.today())}?")
            else:
                prompt = f"You paid {who} {money} on {when}. Which of their deposits does it give back?"
            why = [f"{who} paid you a {format_money(d.amount, d.currency)} deposit on "
                   f"{day_month(d.received_on, repo.today())}." for d in found]
            partial = any(d.available != abs(rec.tx.amount) for d in found)
            why.append("The amounts are not the same, so I won't link them on a guess." if partial else
                       "It did not go back to the bank account the deposit came from, so I won't link them on a "
                       "guess.")
            why.append("Until you answer, I won't close this payment.")
            evidence = [rec.evidence_id, *(repo.transactions[d.tx_id].evidence_id for d in found)]
            self._new_question("deposit_refund", rec, prompt, options, why, evidence, now,
                               f"Asked you whether the {money} paid to {who} gives back their deposit.")

    def answer(self, needs: NeedsYouRecord, option_id: str, answer_ev: str, now: datetime) -> AnswerOutcome:
        """The owner's one tap on a deposit, a part payment or a deposit given back (§19, §37)."""
        repo = self.repo
        option = next((o for o in needs.options if o.id == option_id), None)
        if option is None:
            raise ValueError("not one of the options")
        rec = repo.transactions[needs.subject_id]
        item = repo.items[rec.item_id]
        owner = f"{OWNER_ACTOR}:{repo.owner.email}"
        money = format_money(abs(rec.tx.amount), rec.tx.currency)
        if option.id == "other":
            for key in ("documents", "deposits"):
                for other in str(option.values.get(key, "")).split(","):
                    if not other:
                        continue
                    if needs.kind == "deposit" and rec.id in repo.deposits:
                        repo.deposits[rec.id].not_for_document_ids.append(other)
                    elif other not in rec.not_for_document_ids:
                        rec.not_for_document_ids.append(other)
            needs.status, needs.answer, needs.answered_at = "answered", option.id, now
            if needs.kind == "deposit_refund":  # an ordinary payment after all: it needs its own invoice
                engine = ExpectedEvidenceEngine(entities=repo.entities, suppliers=repo.resolver(),
                                                account_countries=repo.account_countries())
                rec.decision = engine.classify(rec.tx)
            self.o.advance(item, Stage.UNDERSTOOD, [rec.evidence_id, answer_ev], agent=self.name, actor=owner,
                           note="Not linked, as you said.")
            self.o.activity(now, "answered", f"You said the {money} is for something else.", rec.company_id,
                            evidence_ids=[answer_ev])
            return AnswerOutcome(ok=True, message="Done. I'll keep them apart.")
        if needs.kind == "deposit_kept":
            return self._answer_kept(needs, option, rec, answer_ev, now)
        kind, _, target = option.id.partition(":")
        if needs.kind == "deposit_refund":
            dep = repo.deposits.get(target)
            if dep is None or dep.available < abs(rec.tx.amount):
                raise ValueError("That deposit has less than this left.")
            needs.status, needs.answer, needs.answered_at = "answered", option.id, now
            self.give_back(dep, rec, answer_ev=answer_ev, now=now)
            self.o.advance(item, Stage.UNDERSTOOD, [rec.evidence_id, answer_ev], agent=self.name, actor=owner,
                           note="Gives the deposit back, as you said.")
            kept = format_money(dep.available, dep.currency)
            message = (f"Done. I linked it to the {format_money(dep.amount, dep.currency)} deposit of "
                       f"{day_month(dep.received_on, repo.today())}.")
            if dep.available > 0:
                message += f" {kept} of it was kept: it needs your own invoice."
            return AnswerOutcome(ok=True, message=message)
        record = repo.documents.get(target)
        if record is None:
            raise ValueError("not one of the options")
        if kind == "held":
            held = repo.retentions.get(record.id)
            if held is None or held.status != "held" or held.amount != abs(rec.tx.amount):
                raise ValueError("That amount is no longer held back.")
            needs.status, needs.answer, needs.answered_at = "answered", option.id, now
            self.release(held, rec, answer_ev=answer_ev, now=now)
            self.o.advance(item, Stage.UNDERSTOOD, [rec.evidence_id, *record.evidence_ids, answer_ev],
                           agent=self.name, actor=owner, note="The amount held back, as you said.")
            return AnswerOutcome(ok=True, message=f"Done. The {self.money(held.amount, record)} held back on "
                                                  f"{self.invoice_words(record)} is paid.")
        if needs.kind == "deposit":
            dep = repo.deposits[rec.id]
            if dep.status != "held" or dep.available > self.room(record):
                raise ValueError("That invoice has less than this left to pay.")
            needs.status, needs.answer, needs.answered_at = "answered", option.id, now
            self.apply_deposit(record, dep, index=None, answer_ev=answer_ev, now=now)
        else:
            amount = abs(rec.tx.amount)
            left = self.balance(record)
            if (left if left is not None else abs(record.document.gross_amount or _ZERO)) < amount:
                raise ValueError("That invoice has less than this left to pay.")
            needs.status, needs.answer, needs.answered_at = "answered", option.id, now
            record.part_paid[rec.id] = amount
            if rec.id not in record.matched_tx_ids:
                record.matched_tx_ids.append(rec.id)
            record.part_answers.append(answer_ev)
            rec.part_answer_ev = answer_ev
            rec.document_ids, rec.likely_document_ids = [record.id], []
            who = "Customer" if record.sales else "Supplier"
            rec.match_why = (f"Invoice total: {self.money(abs(record.document.gross_amount or _ZERO), record)}",
                             f"{'Money received' if rec.tx.amount > 0 else 'Bank charge'}: {money}",
                             "You said: it is part of this invoice", f"{who}: {self.party(record)}",
                             *self.progress_lines(record))
            rec.match_headline = self.headline(record)
            self.log("keep_parts", subject_id=rec.id, evidence_ids=[rec.evidence_id, *record.evidence_ids, answer_ev],
                     values={"parts": [[rec.id, record.id, amount]]}, actor=OWNER_ACTOR,
                     validations=list(rec.match_why), response={"kind": "owner_confirmed"})
        self.o.advance(item, Stage.UNDERSTOOD, [rec.evidence_id, *record.evidence_ids, answer_ev], agent=self.name,
                       actor=owner, note=f"Part of {self.invoice_words(record)}, as you said.")
        self.o.activity(now, "answered", f"You said the {money} is part of {self.invoice_words(record)}.",
                        rec.company_id, amount=abs(rec.tx.amount), currency=rec.tx.currency, evidence_ids=[answer_ev])
        left = self.balance(record) or _ZERO
        words = self.invoice_words(record)
        if left > 0:
            still = "is still to come" if record.sales else "is still to pay"
            return AnswerOutcome(ok=True, message=f"Done. I counted it as part of {words}. "
                                                  f"{self.money(left, record)} {still}.")
        held = repo.retentions.get(record.id)
        if held is not None and held.status == "held":
            return AnswerOutcome(ok=True, message=f"Done. {words[:1].upper()}{words[1:]} is paid, apart from the "
                                                  f"{self.money(held.amount, record)} held back{self._until(held)}.")
        return AnswerOutcome(ok=True, message=f"Done. {words[:1].upper()}{words[1:]} is paid in full.")

    def _answer_kept(self, needs: NeedsYouRecord, option: CheckOption, rec: TxRecord, answer_ev: str,
                     now: datetime) -> AnswerOutcome:
        """The owner said whether they kept the rest of a security deposit (their answer is the evidence)."""
        repo = self.repo
        dep = repo.deposits.get(rec.id)
        if dep is None or not dep.security or dep.status != "held" or dep.available <= 0:
            raise ValueError("That deposit is no longer held.")
        needs.status, needs.answer, needs.answered_at = "answered", option.id, now
        owner = f"{OWNER_ACTOR}:{repo.owner.email}"
        rest = format_money(dep.available, dep.currency)
        item = repo.items[rec.item_id]
        if option.id == "later":
            dep.kept_later_at = dep.returned
            self.log("security_deposit_still_held", subject_id=rec.id, evidence_ids=[rec.evidence_id, answer_ev],
                     values={"held": dep.available}, actor=OWNER_ACTOR)
            self.o.advance(item, Stage.UNDERSTOOD, [rec.evidence_id, answer_ev], agent=self.name, actor=owner,
                           note="Still held for them, as you said.")
            self.o.activity(now, "answered", f"You said the other {rest} of {dep.party}'s security deposit goes "
                            "back to them later.", rec.company_id, evidence_ids=[answer_ev])
            return AnswerOutcome(ok=True, message=f"Done. I keep holding {rest} for {dep.party}.")
        dep.kept, dep.kept_answer, dep.status = dep.available, answer_ev, "kept"
        self.log("security_deposit_kept", subject_id=rec.id, evidence_ids=[rec.evidence_id, answer_ev],
                 values={"kept": dep.kept, "returned": dep.returned}, actor=OWNER_ACTOR)
        self.o.advance(item, Stage.UNDERSTOOD, [rec.evidence_id, answer_ev], agent=self.name, actor=owner,
                       note=f"{rest} kept, as you said.")
        self.o.activity(now, "answered", f"You said you kept {rest} of {dep.party}'s security deposit. I counted it "
                        "as income.", rec.company_id, amount=dep.kept, currency=dep.currency,
                        evidence_ids=[rec.evidence_id, answer_ev])
        return AnswerOutcome(ok=True, message=f"Done. I counted the {rest} you kept as income.")

    # ----------------------------------------------------------------- plans and the story, for the owner

    def plan(self, rec: TxRecord) -> str | None:
        """The next step for a deposit still waiting for its invoice, or for money that may give one back."""
        repo = self.repo
        dep = repo.deposits.get(rec.id)
        money = format_money(abs(rec.tx.amount), rec.tx.currency)
        when = day_month(rec.tx.booked_on, repo.today())
        if dep is not None and dep.security and not rec.document_ids:
            money = format_money(dep.amount, dep.currency)
            if dep.returned and dep.available > 0:
                back, rest = format_money(dep.returned, dep.currency), format_money(dep.available, dep.currency)
                if self._asked(rec.id, ("deposit_kept",)):
                    return (f"{back} of the {money} security deposit {dep.party} paid on {when} went back to them. "
                            f"I asked you whether you kept the other {rest}.")
                return (f"{back} of the {money} security deposit {dep.party} paid on {when} went back to them. "
                        f"{rest} is still held for them.")
            return (f"{dep.party} paid a {money} security deposit on {when}. I hold it for them, not as income: it "
                    "closes when it goes back to them.")
        if dep is not None and not rec.document_ids:
            asked = self._asked(rec.id, ("deposit",))
            if dep.returned and dep.available > 0:
                return (f"{format_money(dep.available, dep.currency)} of the {money} deposit {dep.party} paid on "
                        f"{when} was kept when the rest went back. It needs your own invoice for that part.")
            for_what = f" for {dep.reference}" if dep.reference else ""
            paid = (f"{dep.party} paid a {money} deposit on {when}{for_what}." if dep.direction == "in" else
                    f"You paid {dep.party} a {money} deposit on {when}{for_what}.")
            if asked:
                return f"{paid} I asked you which invoice it is part of."
            if dep.direction == "in":
                return (f"{paid} I keep it as a deposit, not as income yet: your invoice for the work will take it "
                        "off. Make it where you make your invoices and send it to me.")
            return f"{paid} I keep it as a deposit: its final invoice will take it off."
        if rec.decision is not None and rec.decision.rule == "deposit_refund" and not rec.deposit_refund_of:
            who = self.o.merchant_name(rec.tx)
            if self._asked(rec.id, ("deposit_refund",)):
                return f"The {money} paid to {who} on {when} may give back their deposit. I asked you about it."
            return f"The {money} paid to {who} on {when} may give back their deposit. I'm confirming it."
        if self._asked(rec.id, ("part",)):
            if rec.tx.amount > 0:
                return f"{display_name(rec.tx.counterparty)} paid {money} on {when}. I asked you what it is for."
            return f"You paid {self.o.merchant_name(rec.tx)} {money} on {when}. I asked you what it is for."
        return None

    def story(self, *, tx_id: str | None = None, document_id: str | None = None) -> list[dict[str, Any]]:
        """The steps around a deposit or an invoice paid in parts, oldest first: deposit, advance invoice,
        invoice, each payment, the part held back, a deposit given back. Empty when none apply."""
        repo = self.repo
        record: DocumentRecord | None = None
        dep: DepositRecord | None = None
        if tx_id is not None and tx_id in repo.transactions:
            rec = repo.transactions[tx_id]
            dep = repo.deposits.get(rec.deposit_refund_of or tx_id)
            for d in rec.document_ids:
                if d in repo.documents and self.is_staged(repo.documents[d]):
                    record = repo.documents[d]
            if record is None and dep is not None and dep.applied_to in repo.documents:
                record = repo.documents[dep.applied_to]
        elif document_id is not None and document_id in repo.documents:
            record = repo.documents[document_id]
            if not self.is_staged(record) and not record.linked_advances:
                record = None
        if record is None and dep is None:
            return []
        steps: list[dict[str, Any]] = []
        seen: set[str] = set()

        def add(step: str, subject_id: str, label: str, day: date | None, amount: Decimal | None, currency: str,
                evidence: Sequence[str]) -> None:
            if subject_id not in seen:
                seen.add(subject_id)
                steps.append({"step": step, "id": subject_id, "label": label, "date": day, "amount": amount,
                              "currency": currency, "evidenceIds": list(evidence)})

        def payment(r: TxRecord, step: str, label: str) -> None:
            add(step, r.id, label, r.tx.booked_on, abs(r.tx.amount), r.tx.currency, [r.evidence_id])

        def advance_step(advance: DocumentRecord | None) -> None:
            if advance is not None:
                doc = advance.document
                add("advance_invoice", advance.id, f"Advance invoice {self.number(advance)}", doc.issue_date,
                    abs(doc.gross_amount or _ZERO), doc.currency, advance.evidence_ids)

        deposits = [d for d in repo.deposits.values() if record is not None and d.applied_to == record.id]
        if dep is not None and dep not in deposits:
            deposits.insert(0, dep)
        for d in sorted(deposits, key=lambda d: (d.received_on, d.tx_id)):
            paid = repo.transactions[d.tx_id]
            money = format_money(d.amount, d.currency)
            when = day_month(d.received_on, repo.today())
            noun = d.noun.capitalize()
            payment(paid, "deposit", f"{noun} of {money} from {d.party} on {when}" if d.direction == "in" else
                    f"{noun} of {money} paid to {d.party} on {when}")
            advance_step(repo.documents.get(d.advance_document_id or ""))
            for t in d.refunds:
                back = repo.transactions.get(t)
                if back is not None:
                    given = format_money(d.refunds[t], d.currency)
                    payment(back, "deposit_refund",
                            f"Given back to {d.party}: {given} on {day_month(back.tx.booked_on, repo.today())}")
            if d.status == "kept" and d.kept_answer:  # the part of a security deposit kept, on the owner's word
                add("deposit_kept", f"kept:{d.tx_id}", f"Kept, as you confirmed: {format_money(d.kept, d.currency)}",
                    None, d.kept, d.currency, [d.kept_answer])
        if record is not None:
            for a in record.linked_advances:
                advance_step(repo.documents.get(a))
            doc = record.document
            add("invoice", record.id, f"Invoice {self.number(record)}", doc.issue_date,
                abs(doc.gross_amount or _ZERO), doc.currency, record.evidence_ids)
            held = repo.retentions.get(record.id)
            released = self._released(record)
            for r in sorted((repo.transactions[t] for t in record.part_paid if t in repo.transactions),
                            key=lambda r: (r.tx.booked_on, r.id)):
                if r.id in released:
                    continue
                money = format_money(record.part_paid[r.id], doc.currency)
                when = day_month(r.tx.booked_on, repo.today())
                payment(r, "payment", f"{display_name(r.tx.counterparty)} paid {money} on {when}" if r.tx.amount > 0
                        else f"Paid {money} to {self.o.merchant_name(r.tx)} on {when}")
            if held is not None:
                if held.status == "held":
                    add("held_back", f"held:{record.id}", self.held_sentence(held).removesuffix("."), held.until,
                        held.amount, held.currency, record.evidence_ids)
                for t in sorted(released):
                    r = repo.transactions[t]
                    money = format_money(held.amount, held.currency)
                    payment(r, "held_back_paid",
                            f"The {money} held back, paid on {day_month(r.tx.booked_on, repo.today())}")
        return steps


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
                # A learned "no document needed" carries the owner's or accountant's answer as evidence (§54).
                taught = self.o.learned_evidence(rec) if rec.decision.rule == "learned" else None
                moved += self.o.advance(item, Stage.NOT_REQUIRED, [rec.evidence_id, *([taught] if taught else [])],
                                        agent=self.name, quality=Quality.GREEN, note=rec.decision.reason)
                continue
            if rec.proof_evidence_ids:
                evidence = [rec.evidence_id, *rec.proof_evidence_ids, *rec.extra_evidence_ids]
                moved += self._close(item, evidence, note=rec.proof_note or
                                     "The payment matches the tax letter's amount and reference.")
                continue
            given_back = self.o.staged.settled_without_document(rec)
            if given_back is not None:  # a deposit given back, and the money that gave it back (checklist X8)
                evidence, note = given_back
                moved += self._close(item, evidence, note=note)
                continue
            disputed = self.o.chargebacks.settled(rec)
            if disputed is not None:  # a disputed card payment linked to its sale, or to the money won back (I7)
                evidence, note = disputed
                moved += self._close(item, evidence, note=note)
                continue
            if not rec.document_ids:
                continue
            docs = [repo.documents[d] for d in rec.document_ids]
            if any(d.on_hold for d in docs) or any(d.document.quality is not Quality.GREEN for d in docs):
                continue
            # The invoice names one of your other companies: never closed on a guess (§51).
            named = self.o.company_mismatch(rec, docs)
            if named is not None:
                moved += self.o.ask_which_company(rec, docs, named)
                continue
            # A large first purchase from someone new needs more than a matching amount (§26, §57).
            hold = self.o.large_purchase_hold(docs, rec.company_id)
            if hold is not None:
                self.o.note_hold(rec, hold, rec.company_id, [rec.evidence_id, *[e for d in docs for e in d.evidence_ids]])
                continue
            rec.hold_reason = ""
            evidence = [rec.evidence_id, *[e for d in docs for e in d.evidence_ids]]
            if rec.company_answer_ev:
                evidence.append(rec.company_answer_ev)
            if rec.refund_answer_ev:  # the owner said this refund is part of a credit note for more
                evidence.append(rec.refund_answer_ev)
            if rec.part_answer_ev:  # the owner said this payment is part of an invoice
                evidence.append(rec.part_answer_ev)
            evidence += [e for e in rec.extra_evidence_ids if e not in evidence]
            moved += self._close(item, evidence, note=rec.match_headline or "Matched to its document.")
            for d in docs:
                if d.book == "till":
                    continue  # a till report closes on its card takings, not on a cash deposit (backoffice.cashbook)
                if self.o.staged.is_staged(d):
                    continue  # paid in parts: it closes once every part is in (``_settle_staged``)
                if self.o.is_supplier_refund(rec, d):
                    # A credit note closes once all its money is back, with every refund as evidence.
                    if self.o.credit_left(d) != 0:
                        continue
                    refunds = self.o.refunds_of(d)
                    moved += self._close(repo.items[d.item_id],
                                         [*d.evidence_ids, *(r.evidence_id for r in refunds),
                                          *(r.refund_answer_ev for r in refunds if r.refund_answer_ev)],
                                         note=f"Refunded in full on {day_month(refunds[-1].tx.booked_on)}.")
                    d.hold_reason = ""
                    continue
                moved += self._close(repo.items[d.item_id], evidence, note="Matched to its payment.")
        moved += self._settle_staged()
        moved += self._settle_supporting()
        moved += self._close_cancelled_invoices()
        moved += self._close_cash_purchases()
        return moved

    def _settle_staged(self) -> int:
        """An invoice paid in parts closes when its parts add up to it exactly, each part closed on its own
        evidence (checklist X8, I2, I5). A part held back by the customer stays owed on its own; until then a
        plain line says what was received and what is still to come."""
        repo = self.repo
        staged = self.o.staged
        moved = 0
        for record in sorted(repo.documents.values(), key=lambda d: d.id):
            item = repo.items[record.item_id]
            if not staged.is_staged(record) or item.is_done or record.on_hold \
                    or item.stage in (Stage.NEEDS_OWNER, Stage.CONFLICT):
                continue
            if record.document.quality is not Quality.GREEN:
                continue
            parts = [repo.transactions[t] for t in record.part_paid if t in repo.transactions]
            advances = [repo.documents[d] for d in record.netted if d in repo.documents]
            closed = all(repo.items[r.item_id].stage is Stage.CLOSED for r in parts) and all(
                repo.items[a.item_id].stage is Stage.CLOSED for a in advances)
            if staged.settled(record) and closed and (parts or advances):
                evidence = [*record.evidence_ids, *(r.evidence_id for r in parts),
                            *(e for a in advances for e in a.evidence_ids), *record.part_answers]
                record.hold_reason = ""
                moved += self._close(item, list(dict.fromkeys(evidence)), note=staged.closing_note(record))
                continue
            if parts or advances or staged.settled(record):
                record.hold_reason = staged.waiting_sentence(record)
        return moved

    def _settle_supporting(self) -> int:
        """Supporting evidence never needs closing: a pro-forma, quote or delivery note right away, other
        documents kept with a payment once that payment is closed on its real document (§3)."""
        repo = self.repo
        moved = 0
        for record in sorted(repo.documents.values(), key=lambda d: d.id):
            item = repo.items[record.item_id]
            if item.is_done or record.on_hold or item.stage in (Stage.NEEDS_OWNER, Stage.CONFLICT) \
                    or record.matched_tx_ids:
                continue
            if record.supporting:
                kind = _DOC_LABELS.get(record.document.doc_type, "document")
                moved += self.o.advance(item, Stage.NOT_REQUIRED, record.evidence_ids, agent=self.name,
                                        quality=Quality.GREEN,
                                        note=f"{kind} kept as supporting evidence. It is not an invoice.")
                continue
            closed = [repo.transactions[t] for t in record.supports_tx_ids
                      if t in repo.transactions and repo.items[repo.transactions[t].item_id].stage is Stage.CLOSED]
            if closed:
                evidence = [*record.evidence_ids, *(r.evidence_id for r in closed)]
                moved += self.o.advance(item, Stage.NOT_REQUIRED, evidence, agent=self.name, quality=Quality.GREEN,
                                        note="Kept with the payment as supporting evidence.")
        return moved

    def _close_cancelled_invoices(self) -> int:
        """An invoice fully cancelled by its linked credit notes: both close together, no payment due."""
        repo = self.repo
        moved = 0
        # Each invoice's linked credit notes, looked up once (closing changes no link; high volume, X33).
        linked: dict[str, list[DocumentRecord]] = {}
        for d in sorted(repo.documents.values(), key=lambda d: d.id):
            if d.credit_for:
                linked.setdefault(d.credit_for, []).append(d)
        if not linked:
            return 0
        for record in sorted(repo.documents.values(), key=lambda d: d.id):
            item = repo.items[record.item_id]
            notes = linked.get(record.id, [])
            if not notes or item.is_done or record.matched_tx_ids or record.on_hold:
                continue
            docs = [record, *notes]
            if any(d.on_hold or d.matched_tx_ids or d.document.quality is not Quality.GREEN for d in docs):
                continue
            if any(repo.items[d.item_id].stage in (Stage.NEEDS_OWNER, Stage.CONFLICT) for d in docs):
                continue
            credit = sum((abs(n.document.gross_amount or _ZERO) for n in notes), _ZERO)
            if record.document.gross_amount is None or credit != abs(record.document.gross_amount) or any(
                    n.document.currency != record.document.currency for n in notes):
                continue
            evidence = [e for d in docs for e in d.evidence_ids]
            for d in docs:
                moved += self._close(repo.items[d.item_id], evidence,
                                     note="The credit note cancels this invoice in full. Nothing is due.")
        return moved

    def _close_cash_purchases(self) -> int:
        """A receipt that says it was paid in cash closes on its own evidence (§3, §11): no bank line will come.

        Verified and addressed to one of your companies: closed as that company's cash cost in its
        month. Otherwise one plain question (which company, and is the reading right), never a
        permanent blocker.
        """
        repo = self.repo
        moved = 0
        for record in sorted(repo.documents.values(), key=lambda d: d.id):
            item = repo.items[record.item_id]
            if not record.paid_in_cash or record.matched_tx_ids or record.on_hold or item.is_done:
                continue
            if item.stage in (Stage.NEEDS_OWNER, Stage.CONFLICT) or record.claim_id is not None:
                continue  # an employee's own money is an expense claim, not the company's cash (backoffice.staff)
            company = record.document.entity_id
            verified = record.document.quality is Quality.GREEN or record.owner_confirmed is not None
            if company is None or not verified:
                moved += self.o.ask_about_cash(record)
                continue
            hold = self.o.large_purchase_hold([record], company)
            if hold is not None:
                self.o.note_hold(record, hold, company, record.evidence_ids)
                continue
            record.hold_reason = ""
            evidence = [*record.evidence_ids, *([record.owner_confirmed] if record.owner_confirmed else [])]
            moved += self._close(item, evidence, note="Paid in cash, as the receipt shows.")
            who = display_name(record.document.supplier_name)
            amount = format_money(record.document.gross_amount or _ZERO, record.document.currency)
            self.o.activity(repo.clock.now(), "checked", f"Recorded the {amount} cash purchase at {who} for "
                            f"{repo.company_name(company)}.", company, amount=record.document.gross_amount,
                            currency=record.document.currency, evidence_ids=evidence)
        return moved

    def _open_question(self, rec: TxRecord) -> NeedsYouRecord | None:
        return next((n for n in self.repo.needs.values()
                     if n.subject_id == rec.id and n.status == "open"), None)

    def _close(self, item: TrackedItem, evidence: list[str], *, note: str) -> int:
        moved = 0
        if item.is_done:
            return 0
        if item.stage is Stage.NEEDS_OWNER and any(t.to_stage is Stage.CLOSED for t in item.history):
            # Closed once, opened again (a member's payment that came back), now proven again: it resumes closed.
            return self.o.advance(item, Stage.CLOSED, evidence, agent=self.name, quality=Quality.GREEN, note=note)
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
        # A payment already matched to its documents is not missing one (it may still wait, e.g. a held
        # large purchase or a question): only the others count as "still looking for a document".
        decisions = [repo.transactions[t].decision for t in sorted(tx_ids)
                     if repo.transactions[t].decision and not repo.transactions[t].document_ids]
        # A renewal the letter says happens on its own is information: it never holds a month open (§24).
        obligations = [o.obligation for o in repo.obligations.values() if not o.informational]
        return compute_month_status(
            company_id, month, items, now=now or repo.clock.now(), connectors=repo.connectors_for(company_id),
            decisions=decisions, obligations=obligations,
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
        if item.subject_type == EXPECTED_INVOICE:
            expected = repo.expected_invoices.get(item.subject_id)
            if expected is None or expected.document_id not in repo.documents:
                return "The invoice that arrived for it is no longer on file."
            return None
        if item.subject_type == "document":
            doc = repo.documents[item.subject_id]
            if doc.on_hold and not doc.late_bank_hold:
                return "This document is on hold."
            if doc.supporting:
                return "A document that is not an invoice cannot close anything."
            if doc.claim_id is not None:  # an expense claim: closed by its approval and the transfer paying it back
                return self.o.staff.claim_problem(doc)
            if doc.owner_confirmed is not None and doc.paid_in_cash and doc.document.quality is not Quality.RED:
                return None  # a cash receipt the owner confirmed: their answer is the evidence (§19, §55)
            if doc.document.quality is not Quality.GREEN:
                return "The document's details no longer agree."
            if self.o.staged.is_staged(doc) and not self.o.staged.settled(doc):
                return "The payments and the invoice no longer add up."
            return None
        rec = repo.transactions[item.subject_id]
        if rec.proof_evidence_ids:
            return None
        cash = self.o.cash.problem(rec)  # cash paid into the bank: its till reports (backoffice.cashbook)
        if cash is not None:
            return cash or None
        if rec.claim_ids:  # pays back expense claims (backoffice.staff)
            return self.o.staff.reimbursement_problem(rec)
        if not rec.document_ids:
            if self.o.staged.settled_without_document(rec) is not None:
                return None  # a deposit given back, proven by both bank lines (checklist X8)
            if self.o.chargebacks.settled(rec) is not None:
                return None  # a disputed card payment, proven by what it is linked to (I7)
            return "The payment has lost its document."
        docs = [repo.documents.get(d) for d in rec.document_ids]
        # A hold on bank details that appeared after this payment was proven blocks only the new account.
        if any(d is None or (d.on_hold and not d.late_bank_hold) or d.document.quality is not Quality.GREEN
               for d in docs):
            return "The document for this payment no longer checks out."
        if any(d is not None and d.supporting for d in docs):
            return "A document that is not an invoice cannot close a payment."
        settlement = next((s for s in repo.settlements.values() if s.document_id in rec.document_ids), None)
        if settlement is not None:
            report = settlement.report
            if not settlement.settled or settlement.transaction_id != rec.id:
                return "The payout has lost its payout report."
            if not report.adds_up:
                return "The payout report no longer adds up."
            if report.net != rec.tx.amount or report.currency != rec.tx.currency.strip().upper():
                return "The payout report no longer matches what arrived in the bank."
        # One document, or one invoice with the credit notes netted against it.
        present = [d for d in docs if d is not None]
        credits = [d for d in present if d.credit_for and d.credit_for in rec.document_ids]
        primary = [d for d in present if d not in credits]
        parts = [d for d in present if rec.id in d.part_paid]
        if parts:
            # A part of invoices paid in parts: the parts of this payment add up to it, and no invoice is paid
            # beyond its total (checklist X8, I2, I5).
            staged = self.o.staged
            given_back = repo.deposits[rec.id].returned if rec.id in repo.deposits else _ZERO
            explained = sum((d.part_paid[rec.id] for d in parts), _ZERO) + given_back
            if len(parts) != len(present) or explained != abs(rec.tx.amount) \
                    or any(rec.tx.currency != d.document.currency or (staged.balance(d) or _ZERO) < 0 for d in parts):
                return "The payments and the invoice no longer add up."
        elif len(primary) == 1 and not credits and self.o.is_supplier_refund(rec, primary[0]):
            # A refund of a credit note, maybe in parts: never more than the credit note; less only when the
            # owner said it is part of it, or the parts add up to it exactly.
            total, back = abs(primary[0].document.gross_amount or _ZERO), self.o.refunded(primary[0])
            if back > total or (back < total and not any(r.refund_answer_ev for r in self.o.refunds_of(primary[0]))):
                return "The refunds and their credit note no longer agree."
        elif len(primary) == 1:
            owed = (primary[0].document.gross_amount or _ZERO) - sum(
                (abs(c.document.gross_amount or _ZERO) for c in credits), _ZERO)
            # What the bank paid in the document's own currency: a euro amount is never compared as if it
            # were dollars (the bank's conversion line gives the original amount, checklist P7).
            if owed != self.o.paid_in_document_currency(rec, primary[0]):
                return "The amounts of the payment and its document no longer agree."
        if rec.tx.entity_id is None:
            return "It is no longer clear which company this belongs to."
        if self.o.company_mismatch(rec, present) is not None:
            return "The invoice names another of your companies."
        return None


class CostCenterAgent(_Agent):
    """Which job, property, vehicle, outlet, event, course or client each cost is for (cost centers).

    Runs after entity assignment and matching, so a payment is decided together
    with its invoices. A company without cost centers is never touched: no
    question, no audit entry, nothing stored. Allocation never moves an item
    along the golden path: closure still needs its evidence (§3).
    """

    name = "cost_center"
    ALLOCATABLE = frozenset({EvidenceExpectation.INVOICE, EvidenceExpectation.RECEIPT,
                             EvidenceExpectation.SALES_INVOICE, EvidenceExpectation.REFUND_OR_CREDIT_NOTE})

    # ----------------------------------------------------------------- queries

    def centers(self, company_id: str | None = None, *, active_only: bool = True) -> list[CostCenter]:
        return sorted((c for c in self.repo.cost_centers.values()
                       if (c.active or not active_only) and (company_id is None or c.company_id == company_id)),
                      key=lambda c: (c.label.casefold(), c.id))

    def using(self) -> set[str]:
        return {c.company_id for c in self.repo.cost_centers.values() if c.active}

    def open_question(self, tx_id: str) -> NeedsYouRecord | None:
        return next((n for n in self.repo.needs.values()
                     if n.kind == "cost_center" and n.subject_id == tx_id and n.status == "open"), None)

    def history(self) -> dict[str, dict[str, dict[str, int]]]:
        """{company: {supplier key: {cost center id or GENERAL: count}}} from decided payments (not from history)."""
        resolver = self.repo.resolver()
        entries: dict[str, list[tuple[str | None, str]]] = {}
        for rec in sorted(self.repo.transactions.values(), key=lambda r: r.id):
            allocation = rec.tx.cost_allocation
            if allocation is None or rec.private or allocation.method is AllocationMethod.HISTORY:
                continue
            if allocation.general:
                target = GENERAL
            elif len(allocation.shares) == 1:
                target = allocation.shares[0].cost_center_id
            else:
                continue
            entries.setdefault(rec.company_id, []).append((resolver.resolve_transaction(rec.tx).key, target))
        return {company: history_counts(pairs) for company, pairs in entries.items()}

    # ----------------------------------------------------------------- facts

    def details(self, record: DocumentRecord) -> InvoiceDetails | None:
        """References, lines and VAT per rate of the document's e-invoice, when it has one."""
        for evidence_id in record.evidence_ids:
            evidence = self.repo.evidence(evidence_id)
            if evidence.format in (EvidenceFormat.UBL, EvidenceFormat.XML):
                found = read_invoice_details(self.repo.registry.open(self.repo.tenant_id, evidence_id))
                if found is not None:
                    return found
            outcome = self.repo.reads.get(evidence_id)
            for xml in getattr(outcome, "embedded_xml", ()) or ():
                found = read_invoice_details(xml)
                if found is not None:
                    return found
        return None

    def _text_of(self, record: DocumentRecord) -> str:
        words: list[str] = []
        for evidence_id in record.evidence_ids:
            evidence = self.repo.evidence(evidence_id)
            if evidence.format in (EvidenceFormat.TEXT, EvidenceFormat.QR, EvidenceFormat.HTML):
                data = self.repo.registry.open(self.repo.tenant_id, evidence_id)[:40000]
                words.append(data.decode("utf-8", errors="ignore"))
            outcome = self.repo.reads.get(evidence_id)
            if outcome is not None and getattr(outcome, "text", ""):
                words.append(outcome.text[:40000])
        return "\n".join(words)

    def document_texts(self, record: DocumentRecord, details: InvoiceDetails | None) -> list[tuple[str, str]]:
        doc = record.document
        kind = _DOC_LABELS.get(doc.doc_type, "Document").lower()
        head = " ".join(p for p in (doc.supplier_name, doc.invoice_number, doc.payment_reference,
                                    doc.customer_tax_id) if p)
        texts = [(f"the {kind}", head)]
        body = self._text_of(record)
        if body:
            texts.append((f"the {kind}", body))
        if details is not None and details.references:
            texts.append((f"the {kind}", "\n".join(details.references)))
        if record.message_text.strip():
            texts.append(("the email", record.message_text[:8000]))
        return texts

    def vat_parts(self, record: DocumentRecord, details: InvoiceDetails | None) -> tuple[VatPart, ...]:
        """The document's total by VAT rate: the e-invoice's subtotals, the fiscal QR, or net plus VAT."""
        doc = record.document
        gross = abs(doc.gross_amount) if doc.gross_amount is not None else None
        if gross is None:
            return ()

        def fits(parts: Sequence[VatPart]) -> bool:
            return bool(parts) and sum((p.gross for p in parts), _ZERO) == gross

        if details is not None and fits(details.vat_parts):
            return tuple(details.vat_parts)
        qr = _qr_vat_parts(self._text_of(record))
        if fits(qr):
            return qr
        if doc.net_amount is not None and doc.vat_amount is not None:
            single = (VatPart(rate=None, net=abs(doc.net_amount), vat=abs(doc.vat_amount)),)
            if fits(single):
                return single
        return ()

    def document_facts(self, record: DocumentRecord, company_id: str) -> CostCenterFacts | None:
        doc = record.document
        if doc.gross_amount is None or doc.gross_amount == 0:
            return None
        details = self.details(record)
        match = self.repo.resolver().resolve_document(doc)
        return CostCenterFacts(
            tenant_id=self.repo.tenant_id, company_id=company_id, subject_type="document", subject_id=record.id,
            total=abs(doc.gross_amount), currency=doc.currency, counterparty_key=match.key,
            counterparty_label=display_name(doc.supplier_name), supplier_tax_id=doc.supplier_tax_id,
            texts=tuple(self.document_texts(record, details)),
            emails=tuple(e for e in (record.sender, *record.recipients) if e),
            lines=doc.lines or (details.lines if details is not None else ()),
            vat_parts=self.vat_parts(record, details), on=doc.issue_date,
            evidence_ids=tuple(record.evidence_ids),
            customer_tax_ids=(doc.customer_tax_id,) if doc.customer_tax_id else ())

    def payment_facts(self, rec: TxRecord) -> CostCenterFacts:
        tx = rec.tx
        repo = self.repo
        docs = [repo.documents[d] for d in rec.document_ids if d in repo.documents]
        match = repo.resolver().resolve_transaction(tx)
        account = repo.accounts.get(tx.account_id)
        texts = [("the bank line", " ".join(p for p in (tx.counterparty, tx.description, tx.reference or "") if p))]
        texts += self.o.leases.texts_for(rec)  # a leasing payment: the asset and plate in its contract (X24)
        emails: list[str] = []
        evidence = [rec.evidence_id]
        lines: tuple[Any, ...] = ()
        parts: tuple[VatPart, ...] = ()
        for record in docs:
            details = self.details(record)
            texts += self.document_texts(record, details)
            emails += [e for e in (record.sender, *record.recipients) if e]
            evidence += record.evidence_ids
            if len(docs) == 1 and record.document.gross_amount is not None and \
                    abs(record.document.gross_amount) == abs(tx.amount):
                lines = record.document.lines or (details.lines if details is not None else ())
                parts = self.vat_parts(record, details)
        return CostCenterFacts(
            tenant_id=repo.tenant_id, company_id=rec.company_id, subject_type="transaction", subject_id=rec.id,
            total=abs(tx.amount), currency=tx.currency, counterparty_key=match.key,
            counterparty_label=self.o.merchant_name(tx), card_last4=tx.card_last4, account_id=tx.account_id,
            account_label=account.label if account is not None else None,
            supplier_tax_id=next((d.document.supplier_tax_id for d in docs if d.document.supplier_tax_id), None),
            texts=tuple(texts), emails=tuple(dict.fromkeys(emails)), lines=lines, vat_parts=parts,
            on=tx.booked_on, evidence_ids=tuple(dict.fromkeys(evidence)),
            customer_tax_ids=tuple(dict.fromkeys(d.document.customer_tax_id for d in docs
                                                 if d.document.customer_tax_id)))

    def decide(self, facts: CostCenterFacts, history: Mapping[str, Mapping[str, int]] | None = None
               ) -> CostCenterDecision:
        accountant_ids = self.repo.accountant_ids_for(facts.company_id)
        return decide_cost_center(centers=self.centers(facts.company_id), facts=facts, rulebook=self.repo.rulebook,
                                  accountant_ids=accountant_ids, history=history, today=self.repo.today())

    # ----------------------------------------------------------------- applying

    def apply_payment(self, rec: TxRecord, allocation: CostAllocation) -> None:
        rec.tx = rec.tx.model_copy(update={"cost_allocation": allocation})
        self._log_allocation(rec.id, allocation)

    def apply_document(self, record: DocumentRecord, allocation: CostAllocation) -> None:
        record.document = record.document.model_copy(update={"cost_allocation": allocation})
        self._log_allocation(record.id, allocation)

    def _log_allocation(self, subject_id: str, allocation: CostAllocation) -> None:
        self.log("allocate_cost_center", subject_id=subject_id, evidence_ids=list(allocation.evidence_ids),
                 values={"general": allocation.general, "total": allocation.total,
                         "shares": [[s.cost_center_id, s.amount] for s in allocation.shares],
                         **({"recharge": [s.cost_center_id for s in allocation.shares if s.recharge]}
                            if allocation.recharge_method else {})},
                 validations=[*allocation.why, *allocation.recharge_why],
                 response={"method": allocation.method.value, "quality": allocation.quality.value})

    def scaled(self, allocation: CostAllocation, total: Decimal, parts: Sequence[VatPart] = (),
               evidence_ids: Sequence[str] = ()) -> CostAllocation:
        """The same allocation for another amount (an invoice paid by one payment, or the other way round)."""
        evidence = tuple(dict.fromkeys((*allocation.evidence_ids, *evidence_ids)))
        if allocation.general:
            return allocation.model_copy(update={"total": total, "evidence_ids": evidence})
        if total == allocation.total and (not parts or all(s.parts for s in allocation.shares)):
            return allocation.model_copy(update={"evidence_ids": evidence})
        weights = [(s.cost_center_id, s.amount) for s in allocation.shares]
        recharged = {s.cost_center_id for s in allocation.shares if s.recharge}
        shares = tuple(s.model_copy(update={"recharge": s.cost_center_id in recharged})
                       for s in split_by_weights(total, weights, parts))
        return allocation.model_copy(update={"total": total, "shares": shares, "evidence_ids": evidence})

    def _new_evidence(self, rec: TxRecord) -> bool:
        """Invoices were matched to the payment after it was decided: their evidence is not behind the decision."""
        allocation = rec.tx.cost_allocation
        if allocation is None:
            return False
        known = set(allocation.evidence_ids)
        return any(e not in known for d in rec.document_ids if d in self.repo.documents
                   for e in self.repo.documents[d].evidence_ids)

    def carry_to_documents(self, rec: TxRecord, *, overwrite: bool = False, by_owner: bool = False) -> int:
        """A decided payment's invoices get the same cost centers.

        By default only an invoice with nothing better (none, or a likely one
        from history) takes it; ``overwrite`` replaces what the system decided
        for the invoice; ``by_owner`` (the owner chose for the payment) replaces
        anything, the owner's earlier choice for the invoice included.
        """
        allocation = rec.tx.cost_allocation
        if allocation is None:
            return 0
        moved = 0
        for doc_id in rec.document_ids:
            record = self.repo.documents.get(doc_id)
            if record is None or record.document.gross_amount is None or record.document.gross_amount == 0:
                continue
            current = record.document.cost_allocation
            if current is not None and not by_owner:
                if current.method is AllocationMethod.OWNER:
                    continue
                if not overwrite and current.method is not AllocationMethod.HISTORY:
                    continue
            total = abs(record.document.gross_amount)
            parts = self.vat_parts(record, self.details(record))
            try:
                carried = self.scaled(allocation, total, parts, record.evidence_ids)
            except SplitError:
                continue
            if current == carried:
                continue
            self.apply_document(record, carried)
            moved += 1
        return moved

    def _carried(self, rec: TxRecord) -> CostAllocation | None:
        """The payment's invoices' own allocations, combined, when they cover exactly this payment."""
        docs = [self.repo.documents[d] for d in rec.document_ids if d in self.repo.documents]
        allocations = [d.document.cost_allocation for d in docs]
        if not docs or any(a is None for a in allocations):
            return None
        decided = [a for a in allocations if a is not None]
        total = abs(rec.tx.amount)
        if sum((a.total for a in decided), _ZERO) != total:
            return None
        why = tuple(dict.fromkeys(w for a in decided for w in a.why))
        evidence = tuple(dict.fromkeys((rec.evidence_id, *[e for d in docs for e in d.evidence_ids])))
        quality = Quality.AMBER if any(a.quality is not Quality.GREEN for a in decided) else Quality.GREEN
        if all(a.general for a in decided):
            return CostAllocation(total=total, currency=rec.tx.currency, general=True, method=AllocationMethod.INVOICE,
                                  quality=quality, why=why, evidence_ids=evidence)
        if any(a.general for a in decided):
            return None
        if len(decided) == 1:
            shares = decided[0].shares
        else:
            amounts: dict[str, Decimal] = {}
            for a in decided:
                for share in a.shares:
                    amounts[share.cost_center_id] = amounts.get(share.cost_center_id, _ZERO) + share.amount
            recharged = {s.cost_center_id for a in decided for s in a.shares if s.recharge}
            shares = tuple(AllocationShare(cost_center_id=cid, amount=amount, recharge=cid in recharged)
                           for cid, amount in amounts.items())
        method = next((a.recharge_method for a in decided if a.recharge_method), None)
        recharge_why = tuple(dict.fromkeys(w for a in decided for w in a.recharge_why))
        return CostAllocation(total=total, currency=rec.tx.currency, shares=shares, method=AllocationMethod.INVOICE,
                              quality=quality, why=why, evidence_ids=evidence, recharge_method=method,
                              recharge_why=recharge_why)

    # ----------------------------------------------------------------- the pass

    def _ready_to_ask(self, rec: TxRecord) -> bool:
        """Ask once the invoice had its chance to name the cost center (matched, not needed, or late)."""
        if rec.decision is None:
            return False
        if rec.document_ids or not rec.decision.requires_document or self.repo.items[rec.item_id].is_done:
            return True
        return (self.repo.today() - rec.tx.booked_on).days >= CHASE_AFTER_DAYS

    def allocate(self, now: datetime) -> int:
        """Decide every open payment and document of the companies that keep cost centers."""
        repo = self.repo
        using = self.using()
        for n in repo.needs.values():  # a company that stopped keeping cost centers is not asked any more
            if n.kind == "cost_center" and n.status == "open" and n.company_id not in using:
                n.status, n.resolution = "resolved", "cost_center_off"
        if not using:
            return 0
        history = self.history()
        recharges = self.recharge_history()
        moved = 0
        for rec in sorted(repo.transactions.values(), key=lambda r: r.id):
            if rec.private or rec.tx.entity_id is None or rec.company_id not in using:
                continue
            open_q = self.open_question(rec.id)
            current = rec.tx.cost_allocation
            if current is not None and (current.method is AllocationMethod.OWNER or not self._new_evidence(rec)):
                if open_q is not None:
                    open_q.status, open_q.resolution = "resolved", "cost_center_evidence"
                moved += self.carry_to_documents(rec)
                continue
            if current is None and (rec.decision is None or rec.decision.expectation not in self.ALLOCATABLE):
                continue
            carried = self._carried(rec)
            owner_docs = carried is not None and all(
                (repo.documents[d].document.cost_allocation or carried).method is AllocationMethod.OWNER
                for d in rec.document_ids if d in repo.documents)
            decision = None if owner_docs else self.decide(self.payment_facts(rec), history.get(rec.company_id))
            allocation = carried if owner_docs else (decision.allocation if decision is not None else None)
            if allocation is None and carried is not None and len(rec.document_ids) > 1:
                allocation = carried  # several invoices, each already decided on its own
            if current is not None and allocation is None:
                # Its invoice arrived and disagrees with how it was decided: withdrawn, and asked (never kept by habit).
                rec.tx = rec.tx.model_copy(update={"cost_allocation": None})
                self.log("withdraw_cost_center", subject_id=rec.id, evidence_ids=[rec.evidence_id],
                         validations=list(decision.why) if decision is not None else [],
                         response={"was": current.method.value})
                for doc_id in rec.document_ids:
                    record = repo.documents.get(doc_id)
                    if record is not None and record.document.cost_allocation is not None and \
                            record.document.cost_allocation.method is not AllocationMethod.OWNER:
                        record.document = record.document.model_copy(update={"cost_allocation": None})
                moved += 1
            if allocation is not None:
                if rec.tx.amount < 0:  # only money out can be a cost the client pays back
                    allocation = self.with_recharge(self.payment_facts(rec), allocation,
                                                    recharges.get(rec.company_id))
                if allocation == current:
                    continue
                self.apply_payment(rec, allocation)
                self.carry_to_documents(rec, overwrite=current is not None)
                if open_q is not None:
                    open_q.status = "resolved"
                    open_q.resolution = ("cost_center_rule" if allocation.method in
                                         (AllocationMethod.RULE, AllocationMethod.LEARNED_SPLIT)
                                         else "cost_center_evidence")
                moved += 1
                continue
            if decision is None or decision.question is None or not self._ready_to_ask(rec):
                continue
            if open_q is None:
                who = self.o.merchant_name(rec.tx)
                needs_id = _unique_id(repo.needs, f"nd_{_slug(who.split()[0])}_{int(abs(rec.tx.amount))}_which")
                repo.needs[needs_id] = NeedsYouRecord(
                    id=needs_id, kind="cost_center", subject_type="transaction", subject_id=rec.id,
                    item_id=rec.item_id, company_id=rec.company_id, created_at=now, question=decision.question,
                    why=decision.why)
                self.log("ask_owner", subject_id=rec.id, evidence_ids=[rec.evidence_id],
                         validations=list(decision.why), response={"needs_you": needs_id})
            elif open_q.question is not None and \
                    [o.id for o in open_q.question.options] != [o.id for o in decision.question.options]:
                open_q.question, open_q.why = decision.question, decision.why  # the list of cost centers changed
        for record in sorted(repo.documents.values(), key=lambda d: d.id):
            if record.matched_tx_ids or record.on_hold or record.document.cost_allocation is not None:
                continue
            if record.document.quality is Quality.RED or record.id in repo.statements:
                continue  # a supplier's statement is never a cost of its own
            company = repo.item_company(repo.items[record.item_id])
            if company not in using:
                continue
            facts = self.document_facts(record, company)
            if facts is None:
                continue
            found = self.decide(facts, history.get(company))
            if found.allocation is not None:
                purchase = not record.sales and record.document.doc_type is not DocumentType.CREDIT_NOTE
                self.apply_document(record, self.with_recharge(facts, found.allocation, recharges.get(company))
                                    if purchase else found.allocation)
                moved += 1
        moved += self._recharges(now, using)
        return moved

    # ----------------------------------------------------------------- recharged to the client

    def recharge_history(self) -> dict[str, tuple[dict[tuple[str, str], tuple[int, int]], set[str]]]:
        """{company: ({(supplier key, cost center id): (recharged, not recharged)}, cost centers with any recharged)}.

        Only payments whose recharge was decided (not by history itself) count; general costs never do.
        """
        resolver = self.repo.resolver()
        out: dict[str, tuple[dict[tuple[str, str], tuple[int, int]], set[str]]] = {}
        for rec in sorted(self.repo.transactions.values(), key=lambda r: r.id):
            allocation = rec.tx.cost_allocation
            if allocation is None or rec.private or allocation.general or rec.tx.amount >= 0:
                continue
            counts, recharging = out.setdefault(rec.company_id, ({}, set()))
            recharging.update(s.cost_center_id for s in allocation.shares if s.recharge)
            if allocation.recharge_method == "history" or (allocation.recharge_method is None
                                                           and allocation.method is AllocationMethod.HISTORY):
                continue  # likely, not proven: never evidence for the next one
            key = match_key(resolver.resolve_transaction(rec.tx).key) or ""
            for share in allocation.shares:
                yes, no = counts.get((key, share.cost_center_id), (0, 0))
                counts[(key, share.cost_center_id)] = (yes + 1, no) if share.recharge else (yes, no + 1)
        return out

    def with_recharge(self, facts: CostCenterFacts, allocation: CostAllocation,
                      known: tuple[dict[tuple[str, str], tuple[int, int]], set[str]] | None = None) -> CostAllocation:
        """The allocation with each share marked as the client's to pay back, or not (never over the owner)."""
        if allocation.general or allocation.recharge_method == "owner":
            return allocation
        decision = self.recharge_decision(facts, allocation, known)
        return self.marked(allocation, decision.recharge, decision.method, decision.why)

    def recharge_decision(self, facts: CostCenterFacts, allocation: CostAllocation,
                          known: tuple[dict[tuple[str, str], tuple[int, int]], set[str]] | None = None) -> Any:
        counts, recharging = known if known is not None else ({}, set())
        centers = {c.id: c for c in self.centers(facts.company_id, active_only=False)}
        accountant_ids = self.repo.accountant_ids_for(facts.company_id)
        return decide_recharge(allocation=allocation, centers=centers, facts=facts, rulebook=self.repo.rulebook,
                               accountant_ids=accountant_ids, history=counts, recharging=recharging,
                               own_tax_ids=self.repo.own_tax_ids())

    @staticmethod
    def marked(allocation: CostAllocation, recharge: Sequence[str], method: str | None,
               why: Sequence[str]) -> CostAllocation:
        """The same allocation with ``recharge`` shares marked as the client's to pay back, and why."""
        shares = tuple(s.model_copy(update={"recharge": s.cost_center_id in recharge}) for s in allocation.shares)
        return allocation.model_copy(update={"shares": shares, "recharge_method": method,
                                             "recharge_why": tuple(why)})

    def open_recharge_question(self, tx_id: str, *, answered: bool = False) -> NeedsYouRecord | None:
        wanted = ("open", "answered") if answered else ("open",)
        return next((n for n in self.repo.needs.values()
                     if n.kind == "recharge" and n.subject_id == tx_id and n.status in wanted), None)

    def _recharges(self, now: datetime, using: set[str]) -> int:
        """Payments on a client whose recharge is not decided yet: decide it now, or ask once when it
        genuinely cannot be told (the client has had costs paid back before, and nothing says)."""
        repo = self.repo
        known = self.recharge_history()
        moved = 0
        for rec in sorted(repo.transactions.values(), key=lambda r: r.id):
            allocation = rec.tx.cost_allocation
            if allocation is None or allocation.general or rec.private or rec.tx.amount >= 0 or \
                    rec.company_id not in using or allocation.recharge_method is not None:
                continue
            asked = self.open_recharge_question(rec.id, answered=True)
            facts = self.payment_facts(rec)
            decision = self.recharge_decision(facts, allocation, known.get(rec.company_id))
            if decision.method is not None:
                self.apply_payment(rec, self.marked(allocation, decision.recharge, decision.method, decision.why))
                self.carry_to_documents(rec, overwrite=True)
                if asked is not None and asked.status == "open":
                    asked.status, asked.resolution = "resolved", "recharge_evidence"
                moved += 1
                continue
            if decision.ask is None or asked is not None or not self._ready_to_ask(rec):
                continue
            center = repo.cost_centers[decision.ask]
            who = self.o.merchant_name(rec.tx)
            needs_id = _unique_id(repo.needs, f"nd_{_slug(who.split()[0])}_{int(abs(rec.tx.amount))}_recharge")
            question = cost_center_question(self.centers(rec.company_id), facts, (), repo.today())
            repo.needs[needs_id] = NeedsYouRecord(
                id=needs_id, kind="recharge", subject_type="transaction", subject_id=rec.id, item_id=rec.item_id,
                company_id=rec.company_id, created_at=now, question=question,
                prompt=f"Does {center.label} pay this back?",
                options=(CheckOption(id="recharge", label=f"Yes, {center.label} pays it back",
                                     values={"cost_center_id": center.id}),
                         CheckOption(id="own", label="No, it is our own cost", values={"cost_center_id": center.id})),
                why=(f"It is on {center.label}, and some of their costs are paid back by them.",
                     "Nothing on the payment or its invoice says whether this one is."))
            self.log("ask_owner", subject_id=rec.id, evidence_ids=[rec.evidence_id],
                     values={"cost_center": center.id}, response={"needs_you": needs_id})
        return moved

    # ----------------------------------------------------------------- the owner's choice

    def owner_allocation(self, company_id: str, facts: CostCenterFacts, *, cost_center_id: str | None = None,
                         general: bool = False, split: Any = None, evidence_id: str | None = None
                         ) -> CostAllocation:
        """What the owner chose, checked: their own company's cost center, general costs, or an exact split.

        Raises :class:`SplitError` (a ValueError) with a plain message when it cannot be used.
        """
        centers = {c.id: c for c in self.centers(company_id)}
        noun = noun_for(list(centers.values())) if centers else "job"
        evidence = tuple(dict.fromkeys((*facts.evidence_ids, *((evidence_id,) if evidence_id else ()))))
        parts = facts.vat_parts if sum((p.gross for p in facts.vat_parts), _ZERO) == facts.total else ()
        if split is not None:
            entries = _split_entries(split, centers, noun)
            percents = [p for _, _, p, _ in entries]
            amounts = [a for _, a, _, _ in entries]
            if all(p is not None for p in percents) and all(a is None for a in amounts):
                shares = split_by_percent(facts.total, [(cid, p) for cid, _, p, _ in entries if p is not None], parts)
            elif all(a is not None for a in amounts) and all(p is None for p in percents):
                shares = split_by_amounts(facts.total, [(cid, a, r) for cid, a, _, r in entries if a is not None],
                                          parts, currency=facts.currency)
            else:
                raise SplitError("Give either amounts or percentages for every share, not both.")
            words = [f"{centers[s.cost_center_id].label} {format_money(s.amount, facts.currency)}" for s in shares]
            return CostAllocation(total=facts.total, currency=facts.currency, shares=shares,
                                  method=AllocationMethod.OWNER, why=(f"You split it: {_join_words(words)}.",),
                                  evidence_ids=evidence)
        if general:
            return CostAllocation(total=facts.total, currency=facts.currency, general=True,
                                  method=AllocationMethod.OWNER, why=("You said this is general costs.",),
                                  evidence_ids=evidence)
        center = centers.get(cost_center_id or "")
        if center is None:
            raise SplitError(f"That isn't one of this company's {_plural_noun(noun)}.")
        share = AllocationShare(cost_center_id=center.id, amount=facts.total, parts=parts)
        return CostAllocation(total=facts.total, currency=facts.currency, shares=(share,),
                              method=AllocationMethod.OWNER, why=(f"You said this is for {center.label}.",),
                              evidence_ids=evidence)


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
        self.settlement = SettlementAgent(self)
        self.obligations = ObligationAgent(self)
        self.missing = MissingEvidenceAgent(self)
        self.accountant = AccountantAgent(self)
        self.closure = ClosureAgent(self)
        self.auditor = AuditorAgent(self)
        self.cost_centers = CostCenterAgent(self)
        self.statements = StatementAgent(self)
        self.staged = StagedPaymentsAgent(self)
        from backoffice.staff import StaffAgent  # imported here: it builds on this module's agents

        self.staff = StaffAgent(self)
        self.payroll = PayrollAgent(self)
        from backoffice.chargebacks import ChargebackAgent  # disputed card payments on the bank statement (I7)

        self.chargebacks = ChargebackAgent(self)
        # Leasing, cash and memberships build on this module's agents, like the staff agent.
        from backoffice.cashbook import CashAgent
        from backoffice.leasing import LeaseAgent
        from backoffice.members import MembershipAgent

        self.leases = LeaseAgent(self)  # leasing and renting contracts (X24)
        self.cash = CashAgent(self)  # till reports, cash paid into the bank, the cash box (X4, X5)
        self.members = MembershipAgent(self)  # memberships, tuition and fees, and their receipts (X12)
        from backoffice.captures import CaptureAgent  # phone photos: pages together, retakes, copies (§11)
        from backoffice.package_delivery import PackageAgent  # the monthly package (§27 Day 0 / Day +1)

        self.packages = PackageAgent(self)
        self.captures = CaptureAgent(self)
        from backoffice.line_prices import LinePrices  # prices on invoice lines, read from the documents (X20)

        self.line_prices = LinePrices(self)
        self._activity_seq = 0
        # What sends the emails the back office writes itself (backoffice.mailer): the demo's simulated
        # outbox, or None. With None they wait in ``repo.outbox``; the production server sends each one
        # from an event of its own (server/runtime.py) and never inside another change.
        self.transport: Any = None

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
                 amount: Decimal | None = None, currency: str = "EUR", evidence_ids: Sequence[str] = (),
                 tag: str = "") -> None:
        self._activity_seq += 1
        self.repo.activity.append(ActivityEntry(
            id=f"a{self._activity_seq:04d}", at=at, kind=kind, text=text, company_id=company_id, amount=amount,
            currency=currency, evidence_ids=tuple(evidence_ids), tag=tag))

    # ----------------------------------------------------------------- the emails we write (§22, §25, §28)

    def write_email(self, kind: str, subject_id: str, company_id: str | None, to: str, subject: str, body: str,
                    at: datetime, *, headers: Sequence[tuple[str, str]] = (),
                    files: Sequence[tuple[str, str, bytes]] = (), cc: Sequence[str] = ()) -> OutgoingMessage:
        """Put one email in the outbox. It is written, not sent: :meth:`deliver` sends it."""
        message = OutgoingMessage(id=f"mail_{len(self.repo.outbox) + 1:04d}", kind=kind, subject_id=subject_id,
                                  company_id=company_id, to=to, subject=subject, body=body, written_at=at,
                                  headers=tuple(headers), files=tuple(files),
                                  attached=tuple(name for name, _, _ in files), cc=tuple(cc))
        self.repo.outbox[message.id] = message
        values: dict[str, Any] = {"id": message.id, "kind": kind, "to": to, "subject": subject}
        if cc:
            values["cc"] = list(cc)
        if files:
            values["files"] = [{"name": name, "sha256": sha256_hex(data), "size": len(data)}
                               for name, _, data in files]
        self.log("mailer", "write_email", subject_id=subject_id, values=values)
        return message

    def waiting_messages(self) -> list[OutgoingMessage]:
        return [m for m in self.repo.outbox.values() if not m.sent]

    def held_back(self, message: OutgoingMessage) -> bool:
        """Written on the owner's standing permission, which is switched off now: it is not sent (§25)."""
        action = GATED_MESSAGES.get(message.kind)
        return action is not None and not message.sent and not self.repo.policy.allows(action, message.company_id)

    def sendable_messages(self) -> list[OutgoingMessage]:
        """Waiting emails that may go out now (none held back by a permission switched off since)."""
        return [m for m in self.waiting_messages() if not self.held_back(m)]

    def deliver(self, at: datetime | None = None) -> list[str]:
        """Send every waiting email through ``self.transport``. Only what it accepted counts as sent."""
        if self.transport is None:
            return []
        now = at or self.repo.clock.now()
        sent = []
        for message in self.sendable_messages():
            try:
                self._transmit(message, self.transport)
            except Exception as exc:  # it stays waiting; the owner is told (never "sent")
                message.failures += 1
                self.log("mailer", "send_failed", subject_id=message.id, response={"error": type(exc).__name__})
                continue
            self._sent(message, now, self.transport)
            sent.append(message.id)
        return sent

    def send_waiting(self, message_id: str, transport: Any, at: datetime | None = None) -> bool:
        """Send one waiting email now (the production server, one event per email).

        A transport that refuses raises: the caller's change is then void and the email stays waiting.
        """
        message = self.repo.outbox.get(message_id)
        if message is None or message.sent or self.held_back(message):
            return False
        self._transmit(message, transport)
        self._sent(message, at or self.repo.clock.now(), transport)
        self.run(at)
        return True

    @staticmethod
    def _transmit(message: OutgoingMessage, transport: Any) -> None:
        transport.send([message.to, *message.cc], message.subject, message.body, list(message.files),
                       headers=dict(message.headers))

    def _sent(self, message: OutgoingMessage, at: datetime, transport: Any) -> None:
        message.status = "sent"
        message.sent_at = at
        message.files = ()  # the transport has them now; the audit log keeps their names and hashes
        self.log("mailer", "sent", subject_id=message.id, values={"kind": message.kind, "to": message.to},
                 response={"simulated": is_simulated(transport)})
        if message.kind == "supplier_request" and message.subject_id in self.repo.chases:
            self.missing.sent(self.repo.chases[message.subject_id], at)
        elif message.kind == "link_request" and message.subject_id in self.repo.broken_links:
            self.missing.sent_link_request(self.repo.broken_links[message.subject_id], at)
        elif message.kind == "expected_invoice_request" and message.subject_id in self.repo.expected_invoices:
            self.missing.sent_expected(self.repo.expected_invoices[message.subject_id], at)
        elif message.kind == "accountant_answer" and message.subject_id in self.repo.accountant_questions:
            self.accountant.sent(self.repo.accountant_questions[message.subject_id], at)
        elif message.kind == "payslip_request":
            asked = [t for t, m in self.repo.payslip_requests.items() if m == message.id]
            self.activity(at, "checked", f"Asked your accountant for {count_phrase(len(asked), 'payslip')}.",
                          message.company_id,
                          evidence_ids=[self.repo.transactions[t].evidence_id for t in asked
                                        if t in self.repo.transactions])
        elif message.kind in ("statement_request", "statement_correction") and \
                message.subject_id in self.repo.statements:
            self.statements.sent(message, at)
        elif message.kind in self.staff.MESSAGE_KINDS:
            self.staff.sent(message, at)
        elif message.kind == "accountant_package":
            self.packages.sent(message, at)
        elif message.kind == "correction_request" and message.subject_id in self.repo.documents:
            record = self.repo.documents[message.subject_id]
            who = display_name(record.document.supplier_name)
            item = self.repo.items[record.item_id]
            self.repo.closure_log.append(ClosureActivity(
                kind=ClosureKind.SUPPLIER_CHASED, at=at, entity_id=message.company_id or self._holder_for_document(record),
                subject_id=record.supplier_id or record.id, period=self.repo.item_month(item)))
            self.activity(at, "chased", f"Asked {who} for a corrected invoice.", message.company_id,
                          evidence_ids=record.evidence_ids)

    def _announce_waiting(self, at: datetime) -> None:
        """Tell the owner, once, about each email that is written but not sent (never counted as done)."""
        for message in self.waiting_messages():
            if message.announced:
                continue
            message.announced = True
            text = "Wrote an email. It is waiting to be sent."
            evidence: list[str] = []
            if message.kind == "supplier_request" and (chase := self.repo.chases.get(message.subject_id)):
                rec = self.repo.transactions[chase.tx_id]
                who = self.merchant_name(rec.tx)
                what = ("the credit note for the {} refund" if rec.tx.amount > 0 else
                        "the invoice for the {} payment").format(format_money(abs(rec.tx.amount), rec.tx.currency))
                text = f"Wrote to {who} asking for {what}. It is waiting to be sent."
                if (link := self.repo.broken_links.get(chase.link_id or "")) is not None:
                    text = link.waiting_line
                evidence = [rec.evidence_id]
            elif message.kind == "link_request" and (link := self.repo.broken_links.get(message.subject_id)):
                text = link.waiting_line
                evidence = [link.email_evidence_id] if link.email_evidence_id else []
            elif message.kind == "expected_invoice_request" and (
                    expected := self.repo.expected_invoices.get(message.subject_id)):
                text = (f"Wrote to {expected.supplier_name} asking for its usual invoice for {expected.period.name}. "
                        "It is waiting to be sent.")
            elif message.kind == "payslip_request":
                text = "Wrote to your accountant asking for the missing payslips. It is waiting to be sent."
            elif message.kind == "accountant_answer":
                text = "Wrote an answer to your accountant. It is waiting to be sent."
                q = self.repo.accountant_questions.get(message.subject_id)
                evidence = list(q.answer_evidence_ids) if q else []
            elif message.kind == "correction_request" and message.subject_id in self.repo.documents:
                record = self.repo.documents[message.subject_id]
                text = (f"Wrote to {display_name(record.document.supplier_name)} asking for a corrected invoice. "
                        "It is waiting to be sent.")
                evidence = list(record.evidence_ids)
            elif message.kind in ("statement_request", "statement_correction") and \
                    message.subject_id in self.repo.documents:
                what, who = self.statements.describe(message)
                text = f"Wrote to {who} asking for {what}. It is waiting to be sent."
                evidence = list(self.repo.documents[message.subject_id].evidence_ids)
            elif message.kind in self.staff.MESSAGE_KINDS:
                text, evidence = self.staff.waiting_line(message)
            elif message.kind == "accountant_package" and (package := self.packages.by_outbox(message.id)):
                text = f"{package.month.name} is ready for your accountant. It is waiting to be sent."
            self.activity(at, "waiting", text, message.company_id, evidence_ids=evidence)

    def evidence_text(self, evidence_id: str) -> str:
        """The readable text of one piece of evidence (text files, e-invoices, what the reader read)."""
        try:
            evidence = self.repo.evidence(evidence_id)
        except Exception:
            return ""
        if evidence.format in _READABLE_FILES:
            outcome = self.repo.reads.get(evidence_id)
            return (outcome.text or outcome.reading_text or "") if outcome is not None else ""
        if evidence.format not in (EvidenceFormat.TEXT, EvidenceFormat.UBL, EvidenceFormat.XML, EvidenceFormat.QR):
            return ""
        try:
            return self.repo.registry.open(self.repo.tenant_id, evidence_id).decode("utf-8")
        except (UnicodeDecodeError, ObjectNotFound, IntegrityError):
            return ""

    # ----------------------------------------------------------------- arrivals

    def ingest_file(self, data: bytes, *, filename: str | None = None, content_type: str | None = None,
                    source_kind: SourceKind = SourceKind.UPLOAD, at: datetime | None = None,
                    origin: str = "upload", run: bool = True) -> IngestReport:
        """One file arrives. ``run=False``: read it but let the caller say more about it before the agents
        run (an employee's receipt paid with their own money is an expense claim, never the company's)."""
        at = self.repo.clock.advance_to(at) if at else self.repo.clock.now()
        if (filename or "").lower().endswith(".csv") or (content_type or "").startswith("text/csv"):
            rows = _parse_bank_csv(data)
            if rows is not None:
                return self.ingest_bank(rows, at=at)
        outcome = self.discovery.file(data, filename=filename, content_type=content_type,
                                      source_kind=source_kind, at=at)
        report = self._process_outcome(outcome, at=at, origin=origin)
        if run:
            self.run(at)
        return report

    def share(self, payload: SharePayload, at: datetime | None = None) -> IngestReport:
        at = self.repo.clock.advance_to(at) if at else self.repo.clock.now()
        outcome = self.discovery.share(payload)
        report = self._process_outcome(outcome, at=at, origin="share")
        report.pending_links = []
        followed: list[LinkRecord] = []
        for url in outcome.pending_links:
            link = self.retrieval.follow(url, supplier=self._link_supplier(url), at=at, context={"shared": True},
                                         source_kind=SourceKind.MOBILE_SHARE, shared=True)
            followed.append(link)
            self._read_link(link, at=at, report=report)
        message = _shared_link_message(followed)
        if message:
            report.message = message
        self.run(at)
        return report

    def follow_waiting(self, urls: Sequence[str], at: datetime | None = None) -> IngestReport:
        """Follow links that were waiting (never opened, or the site did not answer), once more (§9).

        The production server opens them before recording the event (server/links.py); here they are
        read through that recording like any link, with the email they came in as their context.
        """
        at = self.repo.clock.advance_to(at) if at else self.repo.clock.now()
        report = IngestReport(route="link", message="Got it.")
        for url in dict.fromkeys(urls):
            if url not in self.repo.pending_links:
                continue  # settled meanwhile
            seen = self.repo.links_seen.get(url)
            email = self._email_context(seen.email_evidence_id) if seen and seen.email_evidence_id else None
            shared = bool(seen and seen.shared)
            supplier = email.supplier if email is not None else self._link_supplier(url)
            if shared:
                context: dict[str, Any] = {"shared": True}
            elif seen is not None and seen.email_evidence_id:
                context = {"email_evidence_id": seen.email_evidence_id}
            else:
                context = {}
            link = self.retrieval.follow(url, supplier=supplier, at=at, context=context,
                                         source_kind=SourceKind.MOBILE_SHARE if shared else SourceKind.EMAIL,
                                         email_evidence_id=seen.email_evidence_id if seen else None, shared=shared)
            if link.status == "waiting":
                report.pending_links.append(url)
                continue
            self._read_link(link, at=at, report=report, email=email)
        self.run(at)
        return report

    def _link_supplier(self, url: str) -> Supplier | None:
        from urllib.parse import urlsplit

        try:
            host = urlsplit(url).hostname
        except ValueError:
            return None
        return self.repo.supplier_for_domain(host) if host else None

    def supplier_company(self, supplier_id: str | None) -> str | None:
        """The company a supplier's invoices usually go to: the only one it has served, else None."""
        if not supplier_id:
            return None
        companies = {d.document.entity_id for d in self.repo.documents.values()
                     if d.supplier_id == supplier_id and d.document.entity_id}
        supplier = self.repo.suppliers.get(supplier_id)
        if supplier is not None:
            resolver = self.repo.resolver()
            companies |= {r.tx.entity_id for r in self.repo.transactions.values()
                          if r.tx.entity_id and r.tx.amount < 0 and not r.private
                          and (m := resolver.resolve_transaction(r.tx)).supplier is not None
                          and m.supplier.id == supplier_id}
        if len(companies) == 1:
            return next(iter(companies))
        if not companies and len(self.repo.companies) == 1:
            return next(iter(self.repo.companies))
        return None

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
            pages = self.captures.add_page(evidence, at)  # one page of a multi-page scan (D2): read together
            if pages is None:
                self._process_evidence(evidence, at=at, origin="scan", report=report)
            elif pages:
                report.evidence_ids = list(pages)
                self._process_pages(pages, at=at, origin="scan", report=report)
            else:
                # Read now, while this page's reading is at hand (on the server it is recorded with this upload's
                # event); it becomes part of the document once the other pages are here.
                self._parts_for(evidence.id)
                report.message = "Got it. I'll read it once the other pages of this scan are here."
        self.run(at)
        return receipt, report

    def _process_pages(self, evidence_ids: Sequence[str], *, at: datetime, origin: str, report: IngestReport) -> None:
        """Every page of one scan, read together as one document (D2): the pages' readings join, each page is
        its evidence. A letter or payslip over several pages is read the same way, and so is a leasing contract, a
        till report or a receipts list (their own agents, as for one page)."""
        parts = [p for e in evidence_ids for p in self._parts_for(e)]
        used = bool(parts) and (
            self._read_books_parts(parts, at=at, origin=origin, report=report)
            or self.payroll.accept(parts, at=at, origin=origin, report=report)
            or bool((letter := _letter_text(parts)) and self._read_letter(letter, evidence_ids[0], at=at,
                                                                          report=report))
            or self._document_from_parts(parts, at=at, origin=origin, retrieved=False, report=report) is not None
            or bool(report.needs_ids))
        if not used:
            report.stored_only = True
            if not self.captures.unreadable(evidence_ids, at=at, report=report):
                report.message = "Got it. I stored the pages, but I couldn't find invoice details in them."

    def ingest_bank(self, rows: Sequence[BankRow], *, at: datetime | None = None) -> IngestReport:
        at = self.repo.clock.advance_to(at) if at else self.repo.clock.now()
        created = self.discovery.bank_rows(rows, at)
        self.reconciliation.classify(created)
        self._note_payment_methods(created, at)
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
        if self._read_books_file(evidence, at=at, origin=origin, report=report):
            return  # a leasing contract, a till report or a receipts list (text or CSV)
        if self.settlement.accept(evidence, at=at, origin=origin, report=report):
            return  # a payout report (CSV / JSON): read by the settlement agent
        parts = self._parts_for(evidence.id)
        if parts and self._read_books_parts(parts, at=at, origin=origin, report=report):
            return  # the same, from a PDF's text or a photo
        if not parts:
            report.stored_only = True
            report.message = self._unread_message(evidence)
            self.discovery.log("stored_for_reading", subject_id=evidence.id, evidence_ids=[evidence.id],
                               values={"format": evidence.format.value})
            self.captures.unreadable([evidence.id], at=at, report=report)  # a poor photo: one task to retake it
            return
        if self.payroll.accept(parts, at=at, origin=origin, report=report):
            return  # a payslip: the evidence a salary needs (J3), never a letter or an invoice
        letter = _letter_text(parts)
        if letter and self._read_letter(letter, evidence.id, at=at, report=report):
            return
        asked = len(report.needs_ids)
        record = self._document_from_parts(parts, at=at, origin=origin, retrieved=False, report=report)
        if record is None and len(report.needs_ids) == asked and any(p.kind == "read" for p in parts):
            report.message = "Got it. I stored it, but I couldn't find invoice details in it."
            self.captures.unreadable([evidence.id], at=at, report=report)

    _BOOK_FORMATS = frozenset({EvidenceFormat.TEXT, EvidenceFormat.CSV})

    def _read_books(self, text: str, evidence_ids: list[str], *, at: datetime, origin: str, report: IngestReport,
                    sender: str | None = None, message_text: str = "") -> bool:
        """A leasing contract (X24), a till report (X5) or a receipts list (X12), each read by its own agent.
        True when one of them took it; anything else is read as usual."""
        if not text.strip():
            return False
        for agent in (self.leases, self.cash, self.members):
            if agent.accept_text(text, evidence_ids, at=at, origin=origin, report=report, sender=sender,
                                 message_text=message_text):
                return True
        return False

    def _read_books_file(self, evidence: Evidence, *, at: datetime, origin: str, report: IngestReport,
                         sender: str | None = None, message_text: str = "") -> bool:
        if evidence.format not in self._BOOK_FORMATS:
            return False
        try:
            text = self.repo.registry.open(self.repo.tenant_id, evidence.id).decode("utf-8")
        except (UnicodeDecodeError, ObjectNotFound, IntegrityError):
            return False
        return self._read_books(text, [evidence.id], at=at, origin=origin, report=report, sender=sender,
                                message_text=message_text)

    def _read_books_parts(self, parts: Sequence[_Part], *, at: datetime, origin: str, report: IngestReport,
                          sender: str | None = None, message_text: str = "") -> bool:
        read = [p for p in parts if p.kind == "read" and (p.text or p.reading_text)]
        if not read:
            return False
        text = "\n".join(p.text or p.reading_text for p in read)
        return self._read_books(text, list(dict.fromkeys(p.evidence_id for p in read)), at=at, origin=origin,
                                report=report, sender=sender, message_text=message_text)

    def _read_letter(self, text: str, evidence_id: str, *, at: datetime, report: IngestReport,
                     sender: str = "") -> bool:
        """A letter or message about an administrative obligation (§24): proof that one was done, a new one,
        or one that names none of your companies (one question). True when the text was taken as such, so
        it is not also read as an invoice; a fiscal document (QR code) always goes on to be read."""
        received = at.astimezone(TZ).date()
        fiscal = bool(_split_qr(text)[0])
        confirmed = self.obligations.confirm_from(text, evidence_id, received_on=received, sender=sender)
        if confirmed is not None:
            report.obligation_ids.append(confirmed.obligation.id)
            if confirmed.done:
                report.confirmed_ids.append(confirmed.obligation.id)
            else:
                self.activity(at, "collected", f"Read a letter about “{confirmed.title}”. It is not enough to close "
                              "it yet.", confirmed.obligation.entity_id, evidence_ids=[evidence_id])
            return not fiscal
        found = self.obligations.detect(text, evidence_id, received_on=received, sender=sender)
        if found is None or fiscal:
            return False
        if isinstance(found, PendingObligation):  # asked which company it is for (said once, in the Activity feed)
            report.message = ("Got it. This letter does not say which of your companies it is for. "
                              "I asked you in Needs you." if found.status == "open" else
                              "Got it. You told me this letter is not for your companies.")
            return True
        report.obligation_ids.append(found.obligation.id)
        self.activity(at, "collected", self.obligations.arrived_line(found), found.obligation.entity_id,
                      amount=found.obligation.amount, evidence_ids=[evidence_id])
        return True

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
        """The readable parts of one piece of evidence: text/XML directly; PDFs and photos through the reader;
        an HTML page (a receipt behind a link) by its schema.org data and its visible text."""
        evidence = self.repo.evidence(evidence_id)
        if evidence.format in _READABLE_FILES:
            return self._read_file(evidence)
        if evidence.format is EvidenceFormat.HTML:
            return self._html_parts(evidence_id)
        part = self._part_for(evidence_id)
        return [part] if part is not None else []

    def _html_parts(self, evidence_id: str) -> list[_Part]:
        try:
            data = self.repo.registry.open(self.repo.tenant_id, evidence_id)
        except (ObjectNotFound, IntegrityError):
            return []
        parts = [_Part(evidence_id, "html", data=data)] if _html_structured(data, evidence_id) else []
        text = html_to_text(data.decode("utf-8", errors="replace"))
        if text.strip():
            parts.append(_Part(evidence_id, "text", text=text))
        return parts

    def _read_file(self, evidence: Evidence) -> list[_Part]:
        """Stage 0 and the OCR chain for a PDF or photo (§13-17); nothing when no reader is configured."""
        repo = self.repo
        outcome = repo.reads.get(evidence.id)  # read before (e.g. a scan's first page, read when it arrived)
        if outcome is None and repo.reader is None:
            return []
        if outcome is None:
            from backoffice.reading import ReadRequest  # server only: the browser demo has no reader

            from backoffice.sensitivity import classify

            data = repo.registry.open(repo.tenant_id, evidence.id)
            hint: dict[str, str] = {}  # Stage 0 tells the OCR reading whether this is your own sales invoice
            request = ReadRequest(
                tenant_id=repo.tenant_id, evidence_id=evidence.id, data=data, mime_type=evidence.mime_type,
                stage0_fields=lambda text, method: self.documents.stage0_fields(text, evidence.id, method, hint),
                extractor=self.documents.text_extractor(hint),
                quality_hints=tuple(self.captures.capture_of(evidence).get("quality") or ()),  # the phone's (§11)
                # A sensitive document never goes to an external AI, even when external AI is on (§52, §53).
                sensitive=lambda text: classify(text, evidence.filename) is not None,
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
        email = self._email_facts(parsed, message_id)
        sender, text, body_part, recipients = email.sender, email.text, email.body, email.recipients
        self.packages.acknowledged(parsed, message_id, at)  # a reply in a monthly package's thread confirms it
        accountant = self.repo.accountant_by_email(sender)
        if accountant is not None:
            questions = self.accountant.receive(body_part.text if body_part else "", message_id, at, accountant,
                                                subject=parsed.subject, message_id=parsed.thread.message_id)
            report.question_ids += [q.id for q in questions]
            return
        # An employee answering my request for a card receipt (backoffice.staff): matched by its thread.
        reply = self.staff.reply_to(parsed, message_id, at)
        first_document = len(report.document_ids)
        groups: list[list[_Part]] = []
        hint = f"{parsed.sender_domain or ''} {parsed.subject}"  # names the provider when the report does not
        for f in result.files:
            if self._read_books_file(f.evidence, at=at, origin=origin, report=report, sender=sender,
                                     message_text=text):
                continue  # a leasing contract, a till report or a receipts list
            if self.settlement.accept(f.evidence, at=at, origin=origin, report=report, hint=hint):
                continue
            file_parts = self._parts_for(f.evidence.id)
            if file_parts and self._read_books_parts(file_parts, at=at, origin=origin, report=report, sender=sender,
                                                     message_text=text):
                continue
            if file_parts:
                groups.append(file_parts)
            else:
                report.stored_only = True
        supplier = email.supplier
        made = 0  # documents this email gave from its files and links
        for url in email_invoice_links(parsed):
            link = self.retrieval.follow(url, supplier=supplier, at=at, context={"email_evidence_id": message_id},
                                         source_kind=SourceKind.EMAIL, email_evidence_id=message_id)
            made += self._read_link(link, at=at, report=report, email=email)
        writer = _sender_line(parsed.sender)
        for file_parts in groups:
            if self.payroll.accept(file_parts, at=at, origin=origin, report=report, sender=sender, message_text=text,
                                   recipients=recipients):
                continue  # a payslip (J3)
            letter = _letter_text(file_parts)
            if letter and self._read_letter(letter, file_parts[0].evidence_id, at=at, report=report, sender=writer):
                continue
            made += self._document_from_parts(file_parts, at=at, origin=origin, retrieved=False, report=report,
                                              sender=sender, message_text=text, body=body_part,
                                              recipients=recipients) is not None
        letter = False
        if not result.files and not parsed.invoice_links and not parsed.bulk and body_part is not None:
            # A message with nothing attached: its own words may be the letter (a bank asking for
            # documents, an insurer's renewal notice, a confirmation that something was done).
            letter = self._read_letter(text, message_id, at=at, report=report, sender=writer)
        if not letter and not made and not _has_attachments(result):
            # No file and no link gave a document: the invoice may be written in the email itself (B4).
            self._body_invoice(parsed, email, at=at, origin=origin, report=report)
        for nested in result.attached_emails:
            self._process_email(nested, at=at, origin=origin, report=report)
        if reply is not None:
            self.staff.replied(reply, report.document_ids[first_document:], at)

    def _email_facts(self, parsed: Any, message_id: str) -> _EmailFacts:
        """Who sent an email, its readable words (the HTML body turned into text when it has no plain one)."""
        sender = parsed.sender.address if parsed.sender else None
        words = parsed.text_body if parsed.text_body.strip() else html_to_text(parsed.html_body)
        body = _Part(message_id, "email_body", text=words) if words.strip() else None
        recipients = tuple(dict.fromkeys(a.address.lower() for a in (*parsed.to, *parsed.cc) if a.address))
        return _EmailFacts(message_id=message_id, sender=sender, text=f"{parsed.subject}\n{words}", body=body,
                           recipients=recipients, supplier=self.repo.supplier_for_domain(parsed.sender_domain),
                           parsed=parsed)

    def _email_context(self, evidence_id: str) -> _EmailFacts | None:
        """The facts of an email on file (for a link from it followed later)."""
        try:
            parsed = parse_eml(self.repo.registry.open(self.repo.tenant_id, evidence_id))
        except (EmailParseError, ObjectNotFound, IntegrityError, KeyError, ValueError):
            return None
        return self._email_facts(parsed, evidence_id)

    def _read_link(self, link: LinkRecord, *, at: datetime, report: IngestReport,
                   email: _EmailFacts | None = None) -> int:
        """Read what a followed link gave (§9 step 10): each file, or the page itself. Returns documents made."""
        if link.status == "waiting":
            report.pending_links.append(link.url)
            return 0
        if link.status != "retrieved":
            return 0
        made = 0
        for evidence_id in link.evidence_ids:
            report.evidence_ids.append(evidence_id)
            parts = self._parts_for(evidence_id)
            if not parts:
                report.stored_only = True
                continue
            made += self._document_from_parts(
                parts, at=at, origin="link", retrieved=True, report=report,
                sender=email.sender if email else None, message_text=email.text if email else "",
                body=email.body if email else None, recipients=email.recipients if email else (),
                shared_link=link.shared) is not None
        return made

    def _body_invoice(self, parsed: Any, email: _EmailFacts, *, at: datetime, origin: str,
                      report: IngestReport) -> DocumentRecord | None:
        """An invoice or e-receipt written in the email itself, with nothing attached (B4).

        Only a complete one becomes a document, with the email as its evidence: its supplier, number,
        date and total, read by the same text readers as any document (Portuguese fiscal fields, the
        international labels) and, when the HTML carries it, schema.org data (JSON-LD or microdata).
        A body that merely mentions an invoice does not.
        """
        if email.body is None:
            return None
        parts: list[_Part] = []
        if parsed.html_body:
            html = parsed.html_body.encode("utf-8", errors="replace")
            if _html_structured(html, email.message_id):
                parts.append(_Part(email.message_id, "html", data=html))
        who = email.supplier.name if email.supplier is not None else _sender_name(parsed)
        parts.append(replace(email.body, supplier_name=who))
        extracted = self.documents.read(parts)
        if extracted is None or not _complete_invoice(extracted, structured=len(parts) > 1, bulk=parsed.bulk,
                                                      known_supplier=email.supplier is not None):
            return None
        self.documents.log("body_invoice", subject_id=email.message_id, evidence_ids=[email.message_id],
                           values={"parsers": list(dict.fromkeys(extracted.parsers))})
        return self._document_from_parts(parts, at=at, origin=origin, retrieved=False, report=report,
                                         sender=email.sender, message_text=email.text, recipients=email.recipients,
                                         known_supplier=email.supplier)

    # ----------------------------------------------------------------- documents

    def _document_from_parts(self, parts: list[_Part], *, at: datetime, origin: str, retrieved: bool,
                             report: IngestReport, sender: str | None = None, message_text: str = "",
                             body: _Part | None = None, recipients: tuple[str, ...] = (),
                             shared_link: bool = False, known_supplier: Supplier | None = None,
                             owner_says_separate: bool = False) -> DocumentRecord | None:
        """One document from what was read. ``owner_says_separate``: the owner answered that this unnumbered
        document is not the look-alike on file (backoffice.captures), so it is recorded on its own."""
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
        # Who issued it and from which country (checklist P7): a document from abroad is checked by its
        # own country's rules; None, or a Portuguese issuer, keeps the Portuguese ones.
        profile = extracted.issuer
        assessment = self.verification.assess(extracted.observations, extracted.doc_type,
                                              evidence_ids=extracted.evidence_ids, issuer=profile,
                                              country=extracted.home)
        values, quality, reasons = _settled_values(assessment), assessment.quality, assessment.reasons
        # The business's own sales invoice (§20 "money in"): issued by one of its companies.
        issuer = extracted.issuer_tax_id or next(
            (t for t in self.repo.own_tax_ids() if same_tax_id(t, _text(values.get("supplier_tax_id")))), None)
        sales = issuer is not None
        supplier_tax_id = _text(values.get("supplier_tax_id"))
        if sales:
            supplier = None
        elif profile is not None and profile.is_foreign and supplier_tax_id:
            # A foreign VAT number keeps its country ("IE9692928F"): the digits alone could be anyone's.
            supplier_tax_id = qualified_tax_id(supplier_tax_id, profile.country)
            supplier = self.repo.supplier_for_tax_id(supplier_tax_id)
        else:
            supplier = self.repo.supplier_for_tax_id(values.get("supplier_tax_id"))
        if supplier is None and not sales and not supplier_tax_id and known_supplier is not None:
            supplier = known_supplier  # names no tax number: the known supplier whose address sent it
        existing = self._duplicate_of(values, extracted.doc_type, supplier.id if supplier else None)
        if existing is not None:
            return self._merge_duplicate(existing, extracted, report, sender=sender, message_text=message_text,
                                         at=at)
        copy = None if owner_says_separate or extracted.statement is not None else self.captures.near_copy(
            values, extracted.doc_type, supplier.id if supplier else None, extracted.text, sales)
        if copy is not None and not set(extracted.evidence_ids) <= set(copy.evidence_ids):
            # No number read, but the same supplier, date and total as an invoice on file (checklist G3): the
            # same document only when the rules prove it; otherwise one plain question, never a second expense.
            proven = self.captures.proven_copy(extracted.text, copy, home=extracted.home)  # its country's rule
            if proven is not None:
                self.documents.log("same_document_proven", subject_id=copy.id,
                                   evidence_ids=[*copy.evidence_ids, *extracted.evidence_ids],
                                   response={"rule": f"same {proven}"})
                return self._merge_duplicate(copy, extracted, report, sender=sender, message_text=message_text, at=at)
            self.captures.ask_same(copy, parts, extracted.evidence_ids, at=at, origin=origin, retrieved=retrieved,
                                   options={"sender": sender, "message_text": message_text, "body": body,
                                            "recipients": recipients, "shared_link": shared_link,
                                            "known_supplier": known_supplier}, report=report)
            return None
        doc_id = "doc_" + extracted.evidence_ids[0][3:19]
        if extracted.statement is not None and doc_id in self.repo.documents:
            return self._merge_duplicate(self.repo.documents[doc_id], extracted, report)  # the same statement again
        customer = values.get("customer_tax_id")
        if extracted.buyer_is_final_consumer and customer and str(customer) == "999999990":
            customer = None  # sold to a final consumer: addressed to nobody in particular
        issuer_company = self.repo.company_for_tax_id(issuer)
        if sales and issuer_company is not None:
            name = self.repo.legal_names.get(issuer_company) or self.repo.company_name(issuer_company) or "Company"
        else:
            name = supplier.name if supplier else display_name(extracted.supplier_name, fallback="Supplier")
        document = Document(
            id=doc_id, tenant_id=self.repo.tenant_id, evidence_ids=extracted.evidence_ids,
            doc_type=extracted.doc_type, supplier_name=name,
            supplier_tax_id=supplier_tax_id, customer_tax_id=_text(customer),
            invoice_number=_text(values.get("invoice_number")), issue_date=_date(values.get("issue_date")),
            due_date=_date(values.get("due_date")), currency=_text(values.get("currency")) or "EUR",
            net_amount=_dec(values.get("net_amount")), vat_amount=_dec(values.get("vat_amount")),
            gross_amount=_dec(values.get("gross_amount")), iban=_text(values.get("iban")),
            payment_reference=_text(values.get("payment_reference")), quality=quality,
            lines=_invoice_lines(parts),
        )
        item = TrackedItem(id="item_" + doc_id, tenant_id=self.repo.tenant_id, subject_type="document",
                           subject_id=doc_id)
        self.repo.items[item.id] = item
        record = DocumentRecord(
            document=document, evidence_ids=extracted.evidence_ids, origin=origin, received_at=at, item_id=item.id,
            observations=extracted.observations, reasons=reasons, sender=sender, message_text=message_text,
            supplier_id=supplier.id if supplier else None, retrieved=retrieved, checks=assessment.verified_fields,
            sales=sales, text=extracted.text,
            paid_in_cash=extracted.paid_in_cash and not sales and document.doc_type in _CASH_DOCUMENTS,
            referenced_number=extracted.referenced_number if document.doc_type is DocumentType.CREDIT_NOTE else None,
            recipients=recipients, issuer=profile, billing_name=extracted.billing_name,
            billing_address=extracted.billing_address, country=extracted.home,
        )
        self.repo.documents[doc_id] = record
        quality = self._buyer_checked(record)  # addressed to a tax number none of your companies has: never GREEN
        self._classify_sensitive(record, extracted.text, message_text, self._filename(extracted.evidence_ids))
        evidence = extracted.evidence_ids
        self.advance(item, Stage.ACQUIRED, evidence, agent="discovery", note="Document received.")
        self.advance(item, Stage.UNDERSTOOD, evidence, agent="document", note="Details read.")
        if quality is Quality.RED and not record.supporting:
            # A pro-forma or delivery note never closes anything, so its readings are never asked about.
            self.advance(item, Stage.CONFLICT, evidence, agent="verification", note=" ".join(reasons))
        if sales:
            # Our own invoice to a customer: nothing is paid out on it, so the supplier checks do not apply.
            record.fraud = None
            entity_id = issuer_company
        else:
            # Fraud runs on every supplier document, before anything can be matched or paid (§26).
            record.fraud = self.fraud.check(record)
            entity_id = self.entity.assign_document(record)
        if entity_id:
            record.document = record.document.model_copy(update={"entity_id": entity_id})
        # An advance invoice, the deposits a final invoice takes off, the part held back (checklist X8).
        self.staged.read(record)
        if record.fraud is not None and record.fraud.hard_stop:
            self._hold(record, at)
        if extracted.statement is not None and not sales:
            self.statements.register(record, extracted.statement)
            document = record.document
        company = self.repo.item_company(item)
        who = display_name(document.supplier_name)
        kind = _DOC_LABELS.get(document.doc_type, "document").lower()
        source = {"email": "your email", "scan": "your phone", "share": "something you shared"}.get(
            origin, "your upload")
        number = f" {document.invoice_number}" if document.invoice_number else ""
        if record.supporting:
            self.activity(at, "collected", f"Collected the {who} {kind} from {source}. It is not an invoice, so I keep "
                          "it only as supporting evidence.", company, amount=document.gross_amount,
                          currency=document.currency, evidence_ids=evidence)
        elif sales:
            self.activity(at, "collected", f"Collected {self.repo.company_name(issuer_company) or who}'s sales "
                          f"{kind}{number} from {source}.", company, amount=document.gross_amount,
                          currency=document.currency, evidence_ids=evidence)
        elif retrieved:
            where = "the link you shared" if shared_link else "a link in your email"
            self.activity(at, "recovered", f"Recovered the {who} invoice from {where}.", company,
                          amount=document.gross_amount, currency=document.currency, evidence_ids=evidence)
        else:
            self.activity(at, "collected", f"Collected the {who} {kind} from {source}.", company,
                          amount=document.gross_amount, currency=document.currency, evidence_ids=evidence)
        unusual = record.fraud.of_kind(SignalKind.UNUSUAL_CURRENCY) if record.fraud is not None else ()
        if unusual and not record.on_hold:
            # Surfaced as a check, not a hold (checklist Q6): the owner reads it; nothing is blocked.
            self.activity(at, "checked", f"Checked the {who} invoice. {unusual[0].owner_line}", company,
                          evidence_ids=evidence)
        report.document_ids.append(doc_id)
        if record.on_hold and record.fraud is not None:
            report.message = f"Got it. I put the {who} payment on hold: {record.fraud.owner_message}"
        elif record.id in self.repo.statements:
            lines = count_phrase(len(extracted.statement.lines), "line") if extracted.statement else "its lines"
            report.message = (f"Got it. This is {who}'s account statement. It is never booked: I check its {lines} "
                              "against your invoices and payments.")
        elif record.supporting:
            report.message = (f"Got it. This is a {kind}, not an invoice. I keep it with the payment as supporting "
                              "evidence and still wait for the invoice.")
        elif quality is Quality.RED:
            needs = self._ask_about_conflict(record, at)
            if needs is not None:
                report.message = f"Got it. I need one answer from you: {needs.prompt}"
        return record

    def _duplicate_of(self, values: Mapping[str, Any], doc_type: DocumentType,
                      supplier_id: str | None = None) -> DocumentRecord | None:
        """The same document again: same supplier, same number and the same kind of document.

        The supplier is its tax number; when one of the two names none (an invoice written in an email's
        body from a known supplier), it is that known supplier (``supplier_id``).
        A different kind is never a copy (verification.duplicates): a credit note that carries
        its invoice's number, or a pro-forma numbered like the invoice, is a document of its own.
        """
        number = _text(values.get("invoice_number"))
        tax_id = _text(values.get("supplier_tax_id"))
        if not number or not (tax_id or supplier_id):
            return None
        for rec in self.repo.documents.values():
            doc = rec.document
            if not (doc.invoice_number and _same_number(doc.invoice_number, number)
                    and same_document_kind(doc.doc_type, doc_type)):
                continue
            if tax_id and doc.supplier_tax_id:
                if same_tax_id(doc.supplier_tax_id, tax_id):
                    return rec
            elif supplier_id and rec.supplier_id == supplier_id:
                return rec
        return None

    def _merge_duplicate(self, record: DocumentRecord, extracted: _Extracted, report: IngestReport, *,
                         sender: str | None = None, message_text: str = "",
                         at: datetime | None = None) -> DocumentRecord:
        """The same invoice again (another copy or channel): its observations join, nothing is duplicated.

        Bank details are never slipped in silently (checklist Q4): a copy whose bank details differ from the
        ones on file is a conflict (one plain question); a copy that adds bank details the first copy lacked
        puts them on the invoice and through every fraud check, exactly like a first copy's. An account the
        supplier was never paid into is held for the owner's verification by phone (§25, §26).
        """
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
                                              evidence_ids=record.evidence_ids, owner=record.owner_values,
                                              issuer=record.issuer, country=record.country)
        # What the first copy did not say and the new one does (an invoice first read from an email's words,
        # then its PDF): the gaps are filled from the checked values; nothing already known is replaced.
        settled = _settled_values(assessment)
        gaps = {name: value for name, read in _GAP_FIELDS
                if getattr(record.document, name) is None and (value := read(settled.get(name))) is not None}
        if "supplier_tax_id" in gaps and record.issuer is not None and record.issuer.is_foreign:
            gaps["supplier_tax_id"] = qualified_tax_id(gaps["supplier_tax_id"], record.issuer.country)
        added_iban = None
        if record.document.iban is None and not record.sales:
            added_iban = _text(settled.get("iban")) or next(
                (_text(o.value) for o in extracted.observations.get("iban", []) if _text(o.value)), None)
            if added_iban is not None and assessment.fields.get("iban") is not None and \
                    assessment.fields["iban"].quality is Quality.RED:
                added_iban = None  # two different accounts on the copies: the conflict question below asks
        if added_iban is not None:
            gaps["iban"] = normalize_iban(added_iban) if is_valid_iban(added_iban) else added_iban
        record.document = record.document.model_copy(update={"quality": assessment.quality,
                                                             "evidence_ids": record.evidence_ids, **gaps})
        record.reasons = assessment.reasons
        record.checks = assessment.verified_fields
        self._buyer_checked(record)
        self.documents.log("merge_duplicate", subject_id=record.id, evidence_ids=new,
                           values={"bank_details_added": mask_iban(gaps["iban"])} if "iban" in gaps else None)
        report.document_ids.append(record.id)
        if added_iban is not None:
            self._check_new_bank_details(record, at or self.repo.clock.now(), new, report, sender=sender,
                                         message_text=message_text)
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

    def _check_new_bank_details(self, record: DocumentRecord, at: datetime, evidence_ids: Sequence[str],
                                report: IngestReport | None = None, *, sender: str | None = None,
                                message_text: str = "") -> bool:
        """Bank details that joined a document after it was first read (a later copy, the owner's choice between
        two copies) go through the same fraud checks as a first copy's (§26, checklist Q4). A new beneficiary is
        held for the owner's verification; nothing is ever trusted here. Returns True when it was held."""
        if record.sales:
            return False
        supplier = self.repo.suppliers.get(record.supplier_id or "")
        own = [i for e in self.repo.entities for i in e.own_ibans]
        untrusted = new_beneficiary_ibans(supplier, [record.document.iban], own_ibans=own)
        record.fraud = self.fraud.check(record, later_copy=True, sender=sender, message_text=message_text)
        self.fraud.log("bank_details_added", subject_id=record.id, evidence_ids=list(evidence_ids),
                       values={"iban": mask_iban(record.document.iban or ""), "new_beneficiary": bool(untrusted)},
                       response={"hard_stop": record.fraud.hard_stop})
        if not record.fraud.hard_stop or record.on_hold:
            return False
        item = self.repo.items[record.item_id]
        # The payment already made and proven before these bank details appeared stays proven: only the new
        # account is blocked (the auditor keeps the closed payment closed).
        record.late_bank_hold = item.stage is Stage.CLOSED
        self._hold(record, at)
        if report is not None:
            who = display_name(record.document.supplier_name)
            report.message = f"Got it. I put the {who} payment on hold: {record.fraud.owner_message}"
        return True

    def _hold(self, record: DocumentRecord, at: datetime) -> None:
        repo = self.repo
        record.on_hold = True
        record.hold_released = False  # held again (new bank details after an earlier release): blocked until verified
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
        new_account = bool(record.fraud and record.fraud.of_kind(SignalKind.NEW_PAYMENT_RECIPIENT))
        if self._iban_changed(record):
            line = f"Put the {who} payment on hold. The bank details on the invoice changed."
        elif new_account:
            line = f"Put the {who} payment on hold. The invoice asks to be paid into an account you have not paid before."
        else:
            line = f"Put the {who} payment on hold. Something on the invoice does not look right."
        self.activity(at, "protected", line, company, evidence_ids=record.evidence_ids)

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
                                              owner=record.owner_values, issuer=record.issuer, country=record.country)
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
        if record.issuer is not None and record.issuer.is_foreign and update["supplier_tax_id"]:
            update["supplier_tax_id"] = qualified_tax_id(update["supplier_tax_id"], record.issuer.country)
        for name in ("net_amount", "vat_amount", "gross_amount"):
            update[name] = _dec(values.get(name))
        for name in ("issue_date", "due_date"):
            update[name] = _date(values.get(name))
        update["currency"] = _text(values.get("currency")) or record.document.currency
        record.document = record.document.model_copy(update=update)
        record.reasons = assessment.reasons
        record.checks = assessment.verified_fields
        self._buyer_checked(record)

    def addressed_elsewhere(self, record: DocumentRecord) -> str | None:
        """The buyer's tax number a purchase document shows when it is none of the business's companies' (checklist
        F6); None when it names one of them, names no buyer, or is the business's own sale. Our own numbers are
        compared with their country, so the same digits from another country are not taken for us."""
        tax = record.document.customer_tax_id
        if record.sales or not tax:
            return None
        ours = any(same_tax_id(qualified_tax_id(e.tax_id, e.country), tax) for e in self.repo.entities if e.tax_id)
        return None if ours else tax

    def _buyer_checked(self, record: DocumentRecord) -> Quality:
        """A document addressed to another company is never verified for yours (§3, F6): at most AMBER, with the
        reason in plain words. Its fraud check holds its payment; nothing closes on it. Returns its quality."""
        other = self.addressed_elsewhere(record)
        if other is None or record.document.quality is not Quality.GREEN:
            return record.document.quality
        record.document = record.document.model_copy(update={"quality": Quality.AMBER})
        record.reasons = (f"It is addressed to another company (tax number {' '.join(other.split())}), not one of "
                          "yours.",)
        return Quality.AMBER

    # ----------------------------------------------------------------- which company, large purchases, cash

    def support(self, rec: TxRecord, docs: Sequence[DocumentRecord]) -> None:
        """Keep documents with a payment as supporting evidence: never its proof, never booked."""
        for d in docs:
            if d.id not in rec.supporting_document_ids:
                rec.supporting_document_ids.append(d.id)
            if rec.id not in d.supports_tx_ids:
                d.supports_tx_ids.append(rec.id)

    def document_company(self, record: DocumentRecord) -> str | None:
        """The company a document itself names: the issuer of your own sales invoice, the buyer of a purchase."""
        if record.document.doc_type is DocumentType.CREDIT_NOTE and record.credit_for:
            return None  # judged through the invoice it corrects
        tax = record.document.supplier_tax_id if record.sales else record.document.customer_tax_id
        return self.repo.company_for_tax_id(tax)

    def company_mismatch(self, rec: TxRecord, docs: Sequence[DocumentRecord]) -> str | None:
        """The company the matched documents name, when it is one of yours but not the one whose account
        paid (or received) the money, and the owner has not decided yet; else None (§51)."""
        if rec.company_answer_ev or rec.tx.entity_id is None:
            return None
        named = sorted({c for d in docs if (c := self.document_company(d)) is not None and c != rec.company_id})
        return named[0] if named else None

    def ask_which_company(self, rec: TxRecord, docs: Sequence[DocumentRecord], named: str) -> int:
        """One plain question: which of two of your companies carries this payment and its invoice (§37)."""
        repo = self.repo
        if any(n.subject_id == rec.id and n.status == "open" and n.kind == "company" for n in repo.needs.values()):
            return 0
        now = repo.clock.now()
        payer, payer_name = rec.company_id, repo.company_name(rec.company_id) or "One of your companies"
        named_name = repo.company_name(named) or "another of your companies"
        sales = any(d.sales for d in docs)
        who = self.merchant_name(rec.tx)
        amount = format_money(abs(rec.tx.amount), rec.tx.currency)
        account = repo.accounts.get(rec.tx.account_id)
        if sales:
            prompt = f"{payer_name} received a payment for an invoice {named_name} issued. Which company should carry it?"
            why = [f"{named_name} issued the invoice.",
                   f"The {amount} arrived in {payer_name}'s account{f' ({account.label})' if account else ''}."]
            first = f"{named_name} (received by {payer_name})"
        else:
            prompt = f"{payer_name} paid an invoice addressed to {named_name}. Which company should carry it?"
            why = [f"The {who} invoice shows {named_name}'s tax number as the buyer.",
                   f"The {amount} left {payer_name}'s account{f' ({account.label})' if account else ''}."]
            first = f"{named_name} (paid by {payer_name})"
        why.append("Until you answer, I won't close this payment.")
        options = (
            CheckOption(id=f"company:{named}", label=first, values={"company": named, "named": named, "payer": payer}),
            CheckOption(id=f"company:{payer}", label=payer_name,
                        values={"company": payer, "named": named, "payer": payer}),
        )
        needs_id = _unique_id(repo.needs, f"nd_{_slug(who.split()[0])}_company")
        item = repo.items[rec.item_id]
        repo.needs[needs_id] = NeedsYouRecord(
            id=needs_id, kind="company", subject_type="transaction", subject_id=rec.id, item_id=rec.item_id,
            company_id=payer, created_at=now, why=tuple(why), prompt=prompt, options=options)
        self.entity.log("ask_owner", subject_id=rec.id,
                        evidence_ids=[rec.evidence_id, *(e for d in docs for e in d.evidence_ids)],
                        values={"paid_by": payer, "invoice_names": named}, response={"needs_you": needs_id})
        self.activity(now, "checked", f"The {who} invoice is addressed to {named_name}, but {payer_name} "
                      f"{'received' if sales else 'paid'} the money. I asked you which company carries it.", payer,
                      evidence_ids=[rec.evidence_id])
        return self.advance(item, Stage.NEEDS_OWNER, [rec.evidence_id, *(e for d in docs for e in d.evidence_ids)],
                            agent="entity", note="Which of your companies carries this.")

    def large_purchase_hold(self, docs: Sequence[DocumentRecord], company_id: str | None) -> str | None:
        """Why a large first purchase from a new supplier cannot close yet, in plain words; None when it can.

        At or above the high-value threshold (backoffice.purchases), from a supplier with no earlier
        documents or payments, closing needs the buyer's tax number to be the company's and the
        totals to add up (amount before VAT + VAT = total).
        """
        repo = self.repo
        company = repo.companies.get(company_id or "")
        for record in docs:
            doc = record.document
            if record.sales or not is_high_value(doc) or self._supplier_has_history(record):
                continue
            buyer_ok = company is not None and same_tax_id(doc.customer_tax_id, company.tax_id)
            parts_known = None not in (doc.net_amount, doc.vat_amount, doc.gross_amount)
            sums_ok = parts_known and check_sum(doc.net_amount, doc.vat_amount, doc.gross_amount).ok  # type: ignore[arg-type]
            if buyer_ok and sums_ok:
                continue
            problems = []
            if not buyer_ok:
                name = company.name if company is not None else "your company"
                problems.append(f"it does not show {name}'s tax number as the buyer")
            if not parts_known:
                problems.append("I can't see its amount before VAT and its VAT")
            elif not sums_ok:
                problems.append("its amount before VAT and its VAT do not add up to the total")
            who = display_name(doc.supplier_name)
            amount = format_money(abs(doc.gross_amount or _ZERO), doc.currency)
            return (f"I'm holding the {amount} {who} invoice: it is the first from this supplier and a large amount, "
                    f"and {join(problems)}.")
        return None

    def _supplier_has_history(self, record: DocumentRecord) -> bool:
        """Earlier documents from the same supplier (same tax number), or earlier payments to it.

        A pro-forma or quote for the same purchase is not history: it is the same new supplier.
        """
        repo = self.repo
        doc = record.document

        def earlier(other: DocumentRecord) -> bool:
            if other.document.issue_date and doc.issue_date:
                return other.document.issue_date < doc.issue_date
            return other.received_at < record.received_at

        if doc.supplier_tax_id and any(
                d.id != record.id and not d.supporting and d.credit_for != record.id and earlier(d)
                and same_tax_id(d.document.supplier_tax_id, doc.supplier_tax_id) for d in repo.documents.values()):
            return True
        if record.supplier_id is None:
            return False
        resolver = repo.resolver()
        paid = [*repo.history_transactions, *(r.tx for r in repo.transactions.values()
                                              if r.id not in record.matched_tx_ids)]
        since = doc.issue_date or record.received_at.astimezone(TZ).date()
        for tx in paid:
            found = resolver.resolve_transaction(tx).supplier
            if found is not None and found.id == record.supplier_id and tx.booked_on < since:
                return True
        return False

    def note_hold(self, subject: TxRecord | DocumentRecord, reason: str, company_id: str | None,
                  evidence: Sequence[str]) -> None:
        """Keep the plain reason something waits; said once in the activity feed."""
        if subject.hold_reason == reason:
            return
        subject.hold_reason = reason
        self.log("closure", "hold_large_purchase", subject_id=subject.id, evidence_ids=evidence,
                 response={"reason": reason})
        self.activity(self.repo.clock.now(), "checked", reason, company_id, evidence_ids=evidence)

    def ask_about_cash(self, record: DocumentRecord) -> int:
        """One plain question for a cash receipt that cannot close on its own: which company, and is it right."""
        repo = self.repo
        if any(n.subject_id == record.id and n.status == "open" for n in repo.needs.values()):
            return 0
        now = repo.clock.now()
        doc = record.document
        who = display_name(doc.supplier_name)
        amount = format_money(doc.gross_amount, doc.currency) if doc.gross_amount is not None else "a"
        when = f" on {day_month(doc.issue_date, repo.today())}" if doc.issue_date else ""
        verified = doc.quality is Quality.GREEN
        shown: dict[str, Any] = {}
        if not verified:  # what the owner sees, and so confirms, by answering yes
            for name in ("gross_amount", "issue_date", "currency"):
                value = getattr(doc, name)
                if value is not None:
                    shown[name] = value
        yes = "" if verified else "Yes, "
        companies = sorted(repo.companies.values(), key=lambda e: (e.id != doc.entity_id, e.name))
        options = [CheckOption(id=f"company:{e.id}", label=f"{yes}{e.name}" if yes else e.name,
                               values={"company": e.id, **shown}) for e in companies]
        options.append(CheckOption(id="personal", label="It's personal, not a company cost" if verified
                                   else "Yes, but it's personal, not a company cost"))
        if not verified:
            options.append(CheckOption(id="wrong", label="No, the details are wrong. I'll send a clearer photo."))
        if verified:
            prompt = f"Which company paid the {amount} cash purchase at {who}{when}?"
        else:
            prompt = f"I read a {amount} cash payment at {who}{when}. Is that right, and which company paid it?"
        why = ["The receipt says it was paid in cash, so there is no bank payment to match."]
        if doc.entity_id is None:
            why.append("It does not show which of your companies bought it.")
        if not verified:
            why.append("I could only read it from one source, so I need you to confirm it.")
        why.append("Until you answer, I won't count it.")
        company = doc.entity_id or next(iter(repo.companies), "")
        needs_id = _unique_id(repo.needs, f"nd_{_slug(who.split()[0])}_cash")
        repo.needs[needs_id] = NeedsYouRecord(
            id=needs_id, kind="cash", subject_type="document", subject_id=record.id, item_id=record.item_id,
            company_id=company, created_at=now, why=tuple(why), prompt=prompt, options=tuple(options))
        self.verification.log("ask_owner", subject_id=record.id, evidence_ids=record.evidence_ids,
                              values={"options": [o.label for o in options]}, response={"needs_you": needs_id})
        return self.advance(repo.items[record.item_id], Stage.NEEDS_OWNER, record.evidence_ids, agent="closure",
                            note="A cash receipt to confirm.")

    def _iban_changed(self, record: DocumentRecord) -> bool:
        return bool(record.fraud and record.fraud.of_kind(SignalKind.CHANGED_IBAN))

    def _holder_for_document(self, record: DocumentRecord) -> str:
        supplier = record.supplier_id
        for rec in sorted(self.repo.transactions.values(), key=lambda r: r.tx.booked_on, reverse=True):
            for d in rec.document_ids:
                if self.repo.documents[d].supplier_id == supplier and supplier:
                    return rec.company_id
        return next(iter(self.repo.companies))

    # ----------------------------------------------------------------- obligations (§24)

    def ask_about_obligation_payment(self, ob: ObligationRecord, rec: TxRecord) -> None:
        """A payment of the amount a letter asks for, to someone the bank line does not identify: one plain
        question, never a guess (§3, §37). The payment's own evidence path is not touched."""
        repo = self.repo
        now = repo.clock.now()
        amount = format_money(abs(rec.tx.amount), rec.tx.currency)
        who = self.merchant_name(rec.tx)
        when = day_month(rec.tx.booked_on, repo.today())
        letter = self.obligations.payment_letter(ob)
        received = day_month(ob.received_on, repo.today()) if ob.received_on else None
        of = f" of {received}" if received else ""
        due = format_money(ob.obligation.amount, rec.tx.currency) if ob.obligation.amount is not None else amount
        if rec.tx.amount > 0:  # a grant announced by a letter: money in
            who = display_name(rec.tx.counterparty)
            prompt = f"Is the {amount} from {who} on {when} the grant payment in the {letter}{of}?"
            why = (f"The letter says {due} will be paid by {day_month(ob.obligation.due_on, repo.today())}.",
                   f"This money is {amount}.",
                   "The bank line does not show that it came from whoever wrote the letter.",
                   "Until you answer, I won't count the grant as received.")
            options = (CheckOption(id="yes", label="Yes, it is that grant", values={"obligation": ob.obligation.id}),
                       CheckOption(id="no", label="No, it is for something else",
                                   values={"obligation": ob.obligation.id}))
        else:
            prompt = f"Does the {amount} payment to {who} on {when} pay the {letter}{of}?"
            why = (f"The letter asks for {due} by {day_month(ob.obligation.due_on, repo.today())}.",
                   f"This payment is {amount}, made after the letter arrived.",
                   "The bank line does not show that it went to whoever wrote the letter.",
                   "Until you answer, I won't count the letter as paid.")
            options = (CheckOption(id="yes", label="Yes, it pays that letter",
                                   values={"obligation": ob.obligation.id}),
                       CheckOption(id="no", label="No, it is for something else",
                                   values={"obligation": ob.obligation.id}))
        needs_id = _unique_id(repo.needs, f"nd_{_slug(who.split()[0])}_letter")
        repo.needs[needs_id] = NeedsYouRecord(
            id=needs_id, kind="obligation", subject_type="transaction", subject_id=rec.id, item_id=rec.item_id,
            company_id=rec.company_id, created_at=now, why=why, prompt=prompt, options=options)
        self.obligations.log("ask_owner", subject_id=ob.obligation.id, evidence_ids=[ob.evidence_id, rec.evidence_id],
                             values={"payment": rec.id}, response={"needs_you": needs_id})

    def _answer_obligation(self, needs: NeedsYouRecord, option_id: str, answer_ev: str, now: datetime) -> AnswerOutcome:
        """The owner said whether a payment pays a letter. "Yes": the payment and the answer are the proof."""
        repo = self.repo
        option = next((o for o in needs.options if o.id == option_id), None)
        if option is None:
            raise ValueError("not one of the options")
        ob = repo.obligations[option.values["obligation"]]
        rec = repo.transactions[needs.subject_id]
        needs.status, needs.answer, needs.answered_at = "answered", option.id, now
        amount = format_money(abs(rec.tx.amount), rec.tx.currency)
        letter = self.obligations.payment_letter(ob)
        if option.id == "no":
            ob.declined_tx_ids.append(rec.id)
            self.obligations.log("payment_not_for_letter", subject_id=ob.obligation.id,
                                 evidence_ids=[rec.evidence_id, answer_ev], actor=OWNER_ACTOR)
            self.activity(now, "answered", f"You said the {amount} payment does not pay the {letter}.",
                          rec.company_id, evidence_ids=[answer_ev])
            return AnswerOutcome(ok=True, message=f"Done. I'll keep watching for the payment that pays the {letter}.")
        result = satisfy(ob.obligation, [ObligationAgent._payment_fact(rec)])
        if not result.satisfied or result.quality is not Quality.GREEN:  # the letter changed meanwhile
            raise ValueError("That payment no longer fits the letter.")
        if ob.obligation.kind is ObligationKind.GRANT_PAYMENT:
            self.obligations.grant_received(ob, [rec], result, answer_ev=answer_ev)
            ob.confirmed_by = (answer_ev,)
            self.activity(now, "answered", f"You confirmed the {amount} is the grant in the {letter}.",
                          rec.company_id, amount=abs(rec.tx.amount), currency=rec.tx.currency,
                          evidence_ids=[rec.evidence_id, answer_ev])
            return AnswerOutcome(ok=True, message="Done. The grant is recorded as received.")
        self.obligations._done(ob, result, how=f"Paid on {day_month(rec.tx.booked_on)}, as you confirmed.",
                               extra=(answer_ev,))
        ob.confirmed_by = (answer_ev,)
        self.activity(now, "answered", f"You confirmed the {amount} payment pays the {letter}.", rec.company_id,
                      amount=abs(rec.tx.amount), currency=rec.tx.currency, evidence_ids=[rec.evidence_id, answer_ev])
        return AnswerOutcome(ok=True, message=f"Done. The {letter} is paid.")

    def _answer_obligation_company(self, needs: NeedsYouRecord, option_id: str, answer_ev: str,
                                   now: datetime) -> AnswerOutcome:
        """The owner said which company a letter is for (or that it is not theirs). Their answer is evidence."""
        repo = self.repo
        option = next((o for o in needs.options if o.id == option_id), None)
        if option is None:
            raise ValueError("not one of the options")
        pending = repo.pending_obligations[needs.subject_id]
        needs.status, needs.answer, needs.answered_at = "answered", option.id, now
        title = pending.finding.title
        if option.id == "none":
            pending.status = "declined"
            self.obligations.log("letter_not_ours", subject_id=pending.id, evidence_ids=[pending.evidence_id, answer_ev],
                                 actor=OWNER_ACTOR)
            self.activity(now, "answered", f"You said the letter about “{title}” is not for your companies.", None,
                          evidence_ids=[answer_ev])
            return AnswerOutcome(ok=True, message="Done. I won't track it.")
        company = option.values["company"]
        pending.status = "recorded"
        ob = self.obligations.record(pending.finding, pending.evidence_id, received_on=pending.received_on,
                                     sender=pending.sender, text=pending.text, entity_id=company)
        ob.reasons = (*ob.reasons, f"You said it is for {repo.company_name(company)}")
        self.obligations.log("letter_company", subject_id=ob.obligation.id,
                             evidence_ids=[pending.evidence_id, answer_ev], actor=OWNER_ACTOR,
                             values={"entity_id": company})
        name = repo.company_name(company) or "that company"
        self.activity(now, "answered", f"You said the letter about “{title}” is for {name}.", company,
                      amount=ob.obligation.amount, evidence_ids=[answer_ev])
        return AnswerOutcome(ok=True, message=f"Done. I added it to {name}'s deadlines.")

    def confirm_obligation(self, obligation_id: str, outcome: str, *, valid_until: date | None = None,
                           data: bytes | None = None, filename: str | None = None,
                           content_type: str | None = None) -> AnswerOutcome:
        """The owner's explicit confirmation that an obligation was done, stored as evidence (§3, §24, §55).

        ``outcome``: "sent" (a request was answered), "filed", "renewed" (with ``valid_until``) or
        "not_renewing". A file (the reply, the receipt, the renewed policy) is stored with it. What is
        to be paid is never closed this way: only its payment proves it.
        """
        repo = self.repo
        ob = repo.obligations.get(obligation_id)
        if ob is None:
            raise KeyError(obligation_id)
        if ob.done:
            raise PermissionError("already done")
        if ob.payable:
            raise PermissionError("I close this one when I see the payment in your bank.")
        wanted = {"sent": ProofKind.REPLY, "filed": ProofKind.SUBMISSION, "renewed": ProofKind.RENEWAL,
                  "not_renewing": ProofKind.DECISION}.get(outcome)
        allowed = {ProofKind.RENEWAL: (ProofKind.RENEWAL, ProofKind.DECISION)}.get(ob.proof, (ob.proof,))
        if wanted is None or wanted not in allowed:
            raise ValueError("That is not how this one gets done.")
        now = repo.clock.now()
        today = now.astimezone(TZ).date()
        if wanted is ProofKind.RENEWAL and (valid_until is None or valid_until <= ob.obligation.due_on):
            raise ValueError("Until when does the renewal run? It must run past "
                             f"{day_month(ob.obligation.due_on, today)}.")
        body = json.dumps({"obligation_id": ob.obligation.id, "outcome": outcome, "answered_by": repo.owner.email,
                           "answered_at": now.isoformat(), "valid_until": valid_until.isoformat() if valid_until else None},
                          sort_keys=True).encode()
        reg = repo.registry.register(body, tenant_id=repo.tenant_id, source_kind=SourceKind.UPLOAD,
                                     format=EvidenceFormat.JSON, mime_type="application/json", retrieved_at=now,
                                     metadata={"kind": "owner_confirmation"})
        evidence = [reg.evidence.id]
        if data:  # the reply, the filing receipt or the renewed policy, kept as it came (§55)
            filed = self.discovery.file(bytes(data), filename=filename, content_type=content_type,
                                        source_kind=SourceKind.UPLOAD, at=now)
            evidence += [r.evidence.id for r in filed.registrations if r.evidence.id not in evidence]
        self.log("owner", "confirm_obligation", subject_id=ob.obligation.id, evidence_ids=evidence, actor=OWNER_ACTOR,
                 values={"outcome": outcome})
        self.owner_time(now, entity_id=ob.obligation.entity_id, step="confirm_obligation")
        fact = EvidenceFact(evidence_id=reg.evidence.id, kind=wanted, on=today, quality=Quality.GREEN,
                            reference=ob.reference, valid_until=valid_until)
        result = satisfy(ob.obligation, [fact])
        self.obligations.log("satisfy", subject_id=ob.obligation.id, evidence_ids=[ob.evidence_id, *evidence],
                             response={"satisfied": result.satisfied, "quality": result.quality.value},
                             validations=list(result.reasons))
        if not result.satisfied or result.quality is not Quality.GREEN:
            raise ValueError(" ".join(result.reasons) or "That does not close it.")
        self.obligations._done(ob, result, how=_confirmed_how(wanted, today, valid_until, by_owner=True),
                               extra=evidence[1:])
        ob.confirmed_by = tuple(evidence)
        self.run(now)
        return AnswerOutcome(ok=True, message=f"Done. {ob.title} is closed with your confirmation.")

    def expected_invoice_not_coming(self, expected_id: str) -> AnswerOutcome:
        """The owner says a usual invoice will not come (the service ended, nothing was billed that period).
        Their statement is the evidence (§3, §55); the rhythm counts that period as covered."""
        repo = self.repo
        record = repo.expected_invoices.get(expected_id)
        if record is None:
            raise KeyError(expected_id)
        if record.status != "missing":
            raise PermissionError("already settled")
        now = repo.clock.now()
        body = json.dumps({"expected_invoice_id": record.id, "outcome": "not_coming", "answered_by": repo.owner.email,
                           "answered_at": now.isoformat()}, sort_keys=True).encode()
        reg = repo.registry.register(body, tenant_id=repo.tenant_id, source_kind=SourceKind.UPLOAD,
                                     format=EvidenceFormat.JSON, mime_type="application/json", retrieved_at=now,
                                     metadata={"kind": "owner_answer"})
        self.log("owner", "expected_invoice_not_coming", subject_id=record.id, evidence_ids=[reg.evidence.id],
                 actor=OWNER_ACTOR)
        self.owner_time(now, entity_id=record.company_id, step="expected_invoice_not_coming")
        record.status = "not_coming"
        self.advance(repo.items[record.item_id], Stage.NOT_REQUIRED, [reg.evidence.id], agent="missing_evidence",
                     actor=f"{OWNER_ACTOR}:{repo.owner.email}", quality=Quality.GREEN,
                     note="You said this invoice is not coming.")
        month = record.period.name
        self.activity(now, "answered", f"You said the {record.supplier_name} invoice for {month} is not coming.",
                      record.company_id, evidence_ids=[reg.evidence.id])
        self.run(now)
        return AnswerOutcome(ok=True, message=f"Done. I won't wait for the {record.supplier_name} invoice for {month}.")

    # ----------------------------------------------------------------- the pipeline

    def run(self, at: datetime | None = None) -> RunReport:
        """Run every agent in the fixed order until nothing moves (at most ``MAX_PASSES``)."""
        now = self.repo.clock.advance_to(at) if at else self.repo.clock.now()
        report = RunReport()
        for pending in self.captures.stale(now) if self.repo.captures else ():  # scans missing pages for a day
            self._process_pages(pending.evidence_ids(), at=now, origin="scan",
                                report=IngestReport(route="upload", message="Got it."))
        self.staff.learn(now)  # cardholders the bank's card details name (employee cards)
        self.obligations.calendar(now)  # deadlines a company's country sets by the calendar (Spain's modelo 303)
        for _ in range(self.MAX_PASSES):
            report.passes += 1
            moved = self._entities(now)
            moved += self._link_credit_notes(now)
            self.reconciliation.classify([r for r in self.repo.transactions.values() if r.decision is None])
            moved += self.payroll.reclassify()  # paid to an employee's account: a salary (J3)
            moved += self.reconciliation.customer_refunds()  # money back to a customer: your own credit note
            moved += self.staff.settle(now)  # a transfer paying an employee back their approved expense claims
            moved += self.settlement.settle(now)  # payouts first: their reports and commission invoices
            moved += self.chargebacks.link(now)  # disputed card payments: the sale taken back, the money won back
            moved += self.staged.settle(now)  # deposits, advance and final invoices, parts held back (X8)
            moved += self.payroll.prove()  # salaries: only by that month's payslip for that employee (J3)
            moved += self.leases.settle(now)  # each leasing payment to its line of the plan and its invoice (X24)
            moved += self.cash.settle(now)  # till reports: card part to card payouts, cash to cash deposits (X5)
            moved += self.members.settle(now)  # memberships and fees to their receipts, debits that came back (X12)
            matches = self.reconciliation.match()
            for m in matches:
                self._reverify_with_bank(m)
            moved += self.staff.link_receipts()  # a receipt an employee sent for a payment on their card
            moved += self._match_customer_refunds()
            moved += self._settle_partial_refunds(now)
            proven = self.obligations.prove()
            moved += self.closure.progress()
            moved += len(matches) + len(proven)
            report.transitions += moved
            if not moved:
                break
        self.cost_centers.allocate(now)  # which job, property, vehicle ...: nothing at all without cost centers
        self._ask_about_refund_amounts(now)  # a refund that does not match its credit note: one question
        self.staged.ask(now)  # a deposit, a part payment or a deposit given back that is not proven: one question
        self.chargebacks.ask(now)  # a disputed card payment whose sale (or money taken back) is not found
        report.expected = self.missing.check_recurring(now)  # usual invoices that are overdue (§23)
        self.statements.review(now)  # suppliers' account statements: nothing at all without one
        linked = self.missing.chase_broken_links(now)  # invoice links that no longer work: nothing without one
        report.chased = [*(r for b in linked if (r := self.repo.broken_links[b].tx_id)), *self.missing.chase_all(now)]
        self.missing.chase_expected(now)
        self.staff.follow_up(now)  # receipts asked from cardholders, reminders, expense claims to approve
        self.payroll.request_missing(now)  # missing payslips, from whoever runs payroll (J3)
        self.accountant.answer_all(now)
        report.sent = self.deliver(now)
        self._announce_waiting(now)
        report.reopened = self.auditor.recheck()
        # Invoices in another currency closed on the bank's conversion line: their exchange difference, for the
        # accountant (I11). Nothing at all without a reference-rate source (the browser demo has none).
        record_exchange_differences(self, now)
        report.closed_months = self.closure.record_closures(now)
        if self.packages.deliver_due(now):  # each month's package for the accountant, on its working day (§27)
            report.sent += self.deliver(now)
            self._announce_waiting(now)
        self._record_recovered(now)
        self._onboarding_progress(now)
        return report

    def _entities(self, now: datetime) -> int:
        """Which company each payment is for (§51). During a new business's first run, questions wait for the
        whole history, then only the most valuable few are asked (§5, backoffice.onboarding)."""
        moved = 0
        ob = self.repo.onboarding
        for rec in sorted(self.repo.transactions.values(), key=lambda r: r.id):
            if ob.learning:
                ob.historical.add(rec.id)  # the history the first run learns from (and its coverage, §5)
            if rec.tx.entity_id is not None or rec.private or self.repo.items[rec.item_id].is_done:
                ob.deferred.discard(rec.id)
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
                ob.deferred.discard(rec.id)
                moved += 1
            elif result.private and result.quality is Quality.GREEN:
                rec.private = True
                if open_q is not None:
                    open_q.status = "resolved"
                    open_q.resolution = "rule"
                ob.deferred.discard(rec.id)
                self.advance(self.repo.items[rec.item_id], Stage.NOT_REQUIRED, [rec.evidence_id], agent="entity",
                             quality=Quality.GREEN, note="Personal, by your rule.")
                moved += 1
            elif result.question is not None and open_q is None:
                if ob.holds(rec.id):
                    continue  # the first run is still learning, or this one waits its turn (§5)
                self._ask_entity(rec, result, now)
        if ob.learning and self._learning_done(now):
            moved += self._finish_learning(now)
        elif ob.deferred and not ob.learning:
            moved += self._release_deferred(now)
        return moved

    def _ask_entity(self, rec: TxRecord, result: EntityAssignment, now: datetime) -> NeedsYouRecord:
        """One "which company?" question in Needs You (§37)."""
        assert result.question is not None
        amount = abs(rec.tx.amount)
        who = self.merchant_name(rec.tx)
        needs_id = _unique_id(self.repo.needs, f"nd_{_slug(who.split()[0])}_{int(amount)}")
        needs = NeedsYouRecord(
            id=needs_id, kind="choice", subject_type="transaction", subject_id=rec.id, item_id=rec.item_id,
            company_id=rec.holder_id, created_at=now, question=result.question, why=self._entity_why(rec, result))
        self.repo.needs[needs_id] = needs
        self.entity.log("ask_owner", subject_id=rec.id, evidence_ids=[rec.evidence_id],
                        response={"needs_you": needs_id})
        return needs

    # ----------------------------------------------------------------- the first run (§5, §6, §58, §59)

    def _learning_done(self, now: datetime) -> bool:
        """The history is in: no connection is still reading its first 90 days (or it took too long)."""
        ob = self.repo.onboarding
        if not self.repo.transactions:
            return False
        waiting = {c for c in ob.waiting_for if c in self.repo.connectors}
        return not waiting or (ob.started_at is not None and now - ob.started_at > ob_.LEARNING_MAX)

    def _waiting_for_owner(self, rec: TxRecord) -> bool:
        return rec.tx.entity_id is None and not rec.private and not self.repo.items[rec.item_id].is_done \
            and rec.assignment is not None and rec.assignment.question is not None

    def _first_run_candidates(self, tx_ids: set[str]) -> list[Any]:
        recs = [r for r in self.repo.transactions.values() if r.id in tx_ids and self._waiting_for_owner(r)]
        return candidates_from_assignments([r.tx for r in recs], {r.id: r.assignment for r in recs
                                                                  if r.assignment is not None})

    def _finish_learning(self, now: datetime) -> int:
        """The history is in: ask only the most valuable few questions, one per series; defer the rest (§5)."""
        repo = self.repo
        ob = repo.onboarding
        ob.learning = False
        # Never more than the cap open at once, even when a later connection's history comes in.
        still_open = sum(1 for n in repo.needs.values() if n.status == "open" and n.id in {*ob.asked, *ob.released})
        room = max(0, ob.question_limit - still_open)
        chosen = select_questions(self._first_run_candidates(ob.historical), limit=room) if room else []
        chosen_ids = {c.question.facts.subject_id for c in chosen}
        moved = 0
        for rec in sorted(repo.transactions.values(), key=lambda r: r.id):
            if rec.id not in ob.historical or not self._waiting_for_owner(rec):
                continue
            if any(n.subject_id == rec.id and n.status == "open" for n in repo.needs.values()):
                continue
            if rec.id in chosen_ids:
                ob.asked.append(self._ask_entity(rec, rec.assignment, now).id)  # type: ignore[arg-type]
                moved += 1
            else:
                ob.deferred.add(rec.id)
        history = [r for r in repo.transactions.values() if r.id in ob.historical]
        coverage = compute_coverage(coverage_items([r.tx for r in history],
                                                   {r.id: r.assignment for r in history if r.assignment is not None}))
        self.milestone(ob_.LEARNING_FINISHED, now)
        self.log("onboarding", "first_run_questions", evidence_ids=[r.evidence_id for r in history][:50],
                 values={"payments": len(history), "asked": len(ob.asked), "deferred": len(ob.deferred),
                         "limit": ob.question_limit},
                 response={"coverage": coverage.percent, "headline": coverage.headline})
        if history:
            line = f"Learned how your business works from {count_phrase(len(history), 'payment')}. {coverage.headline}"
            self.activity(now, "learned", f"{line} {confirm_line(len(ob.asked))}")
        return moved

    def _release_deferred(self, now: datetime) -> int:
        """A deferred question is asked later, one series at a time and at most one a day, and only when
        nothing from the first run is waiting for the owner: Needs You is never flooded (§5, §30)."""
        repo = self.repo
        ob = repo.onboarding
        today = now.astimezone(TZ).date()
        if ob.released_on == today:
            return 0
        waiting = {*ob.asked, *ob.released}
        if any(n.status == "open" and n.id in waiting for n in repo.needs.values()):
            return 0
        chosen = select_questions(self._first_run_candidates(set(ob.deferred)), limit=1)
        if not chosen:
            return 0
        rec = repo.transactions[chosen[0].question.facts.subject_id]
        if rec.assignment is None or any(n.subject_id == rec.id and n.status == "open" for n in repo.needs.values()):
            return 0
        ob.deferred.discard(rec.id)
        ob.released.append(self._ask_entity(rec, rec.assignment, now).id)
        ob.released_on = today
        return 1

    def begin_onboarding(self, at: datetime | None = None, *, question_limit: int | None = None) -> None:
        """A new business signed up (§4, §58): its first run learns before it asks, and its onboarding is timed."""
        ob = self.repo.onboarding
        now = at or self.repo.clock.now()
        if ob.started_at is not None:
            return
        ob.started_at = now
        ob.learning = True
        if question_limit is not None:
            ob.question_limit = question_limit
        self.milestone(ob_.ACCOUNT_CREATED, now)

    def milestone(self, name: str, at: datetime | None = None) -> bool:
        """Record the first time an onboarding milestone was reached, with its time, as an audit event."""
        ob = self.repo.onboarding
        if ob.started_at is None or name in ob.milestones:
            return False
        when = max(at or self.repo.clock.now(), ob.started_at)
        ob.milestones[name] = when
        self.log("onboarding", "milestone", subject_id=name, values={"milestone": name, "at": when.isoformat()},
                 response=ob_.MILESTONE_WORDS.get(name))
        return True

    def connection_reading(self, connector_id: str) -> None:
        """A connection started reading its first 90 days: its history is learned from before anything in it is
        asked (§5, §6). The first run waits for it; so does a connection added later, with the same cap."""
        ob = self.repo.onboarding
        if ob.started_at is None:
            return
        ob.waiting_for.add(connector_id)
        ob.learning = True

    def connection_read(self, connector_id: str) -> None:
        """A connection finished its first read (or went away)."""
        self.repo.onboarding.waiting_for.discard(connector_id)

    def owner_time(self, at: datetime, *, kind: InteractionKind = InteractionKind.ANSWER, entity_id: str | None = None,
                   seconds: int = ANSWER_SECONDS, step: str = "") -> None:
        """One span of the owner's active time (§59). While onboarding is open it counts as onboarding time."""
        ob = self.repo.onboarding
        onboarding = ob.open_at(at)
        self.repo.interactions.append(OwnerInteraction(
            at=at, active_seconds=seconds, kind=InteractionKind.ONBOARDING if onboarding else kind,
            entity_id=entity_id))
        if onboarding:
            ob.estimated_spans += 1
            self.log("onboarding", "owner_time", subject_id=step or None, values={"step": step, "seconds": seconds})

    def setup_step(self, step: str, *, entity_id: str | None = None) -> None:
        """An owner set-up step during onboarding (§4): one span of their time. Nothing outside onboarding."""
        now = self.repo.clock.now()
        if self.repo.onboarding.open_at(now):
            self.owner_time(now, entity_id=entity_id, step=step)

    def _onboarding_progress(self, now: datetime) -> None:
        """Milestones the state now shows (§58), and the end of onboarding (§59)."""
        repo = self.repo
        ob = repo.onboarding
        if ob.started_at is None:
            return
        m = ob.milestones
        if ob_.HISTORICAL_SCAN_COMPLETE not in m and ob_.EMAIL_CONNECTED in m and ob_.BANK_CONNECTED in m \
                and not {c for c in ob.waiting_for if c in repo.connectors}:
            self.milestone(ob_.HISTORICAL_SCAN_COMPLETE, now)
        if ob_.FIRST_DOCUMENT_FOUND not in m and repo.documents:
            first = min(d.received_at for d in repo.documents.values())
            self.milestone(ob_.FIRST_DOCUMENT_FOUND, first)
        if ob_.FIRST_AUTO_MATCH not in m:
            matched = [t.at for rec in repo.transactions.values()
                       if not owner_touched(item := repo.items[rec.item_id])
                       for t in item.history if t.to_stage is Stage.MATCHED]
            if matched:
                self.milestone(ob_.FIRST_AUTO_MATCH, min(matched))
        if ob.finished_at is None and not ob.learning:
            first_run_open = any(n.status == "open" and n.id in ob.asked for n in repo.needs.values())
            if not first_run_open or not ob.open_at(now):
                ob.finished_at = now
                self.milestone(ob_.ONBOARDING_FINISHED, now)
        elif ob.finished_at is None and not ob.open_at(now):
            ob.finished_at = now
            self.milestone(ob_.ONBOARDING_FINISHED, now)

    def activation(self) -> Any:
        """§58 activation and time to first value (closure.ActivationReport), or None when this business never
        went through onboarding (set up by hand, like the demo): nothing to measure."""
        signals = self.repo.onboarding.activation_signals()
        return None if signals is None else evaluate_activation(signals)

    def _link_credit_notes(self, now: datetime) -> int:
        """A credit note that names the invoice it corrects is linked to that invoice (§20 credit notes).

        The invoice must be on file, from the same supplier (same tax number), with that number.
        The link is what lets the payment be matched to the invoice net of the credit.
        """
        repo = self.repo
        linked = 0
        invoices = sorted((d for d in repo.documents.values() if d.document.doc_type in PURCHASE_INVOICE_TYPES
                           and d.document.invoice_number), key=lambda d: d.id)
        for note in sorted(repo.documents.values(), key=lambda d: d.id):
            if note.document.doc_type is not DocumentType.CREDIT_NOTE or note.credit_for or not note.referenced_number:
                continue
            original = next((d for d in invoices if d.document.supplier_tax_id
                             and same_tax_id(d.document.supplier_tax_id, note.document.supplier_tax_id)
                             and _same_number(d.document.invoice_number or "", note.referenced_number)), None)
            if original is None:
                continue
            note.credit_for = original.id
            who = display_name(note.document.supplier_name)
            number = f" {note.document.invoice_number}" if note.document.invoice_number else ""
            self.documents.log("link_credit_note", subject_id=note.id, evidence_ids=[*note.evidence_ids,
                                                                                      *original.evidence_ids],
                               values={"credit_note": note.id, "invoice": original.id,
                                       "referenced_number": note.referenced_number})
            self.activity(now, "checked", f"Linked the {who} credit note{number} to invoice "
                          f"{original.document.invoice_number}.", self.repo.item_company(repo.items[note.item_id]),
                          amount=note.document.gross_amount, currency=note.document.currency,
                          evidence_ids=note.evidence_ids)
            linked += 1
        return linked

    def credits_for(self, document_id: str) -> list[DocumentRecord]:
        """Credit notes linked to this invoice."""
        return sorted((d for d in self.repo.documents.values() if d.credit_for == document_id), key=lambda d: d.id)

    # ----------------------------------------------------------------- refunds (§20): invoice -> credit note -> refund

    @staticmethod
    def is_supplier_refund(rec: TxRecord, doc: DocumentRecord) -> bool:
        """Money back from a supplier against its credit note (not a credit note netted inside a payment)."""
        return rec.tx.amount > 0 and doc.document.doc_type is DocumentType.CREDIT_NOTE and not doc.sales

    def refunds_of(self, note: DocumentRecord) -> list[TxRecord]:
        """The refunds matched to a supplier's credit note, oldest first."""
        found = (self.repo.transactions[t] for t in note.matched_tx_ids if t in self.repo.transactions)
        return sorted((r for r in found if r.tx.amount > 0), key=lambda r: (r.tx.booked_on, r.id))

    def refunded(self, note: DocumentRecord) -> Decimal:
        """What of a supplier's credit note came back. A refund matched to several credit notes at once (their
        totals add up to it exactly) refunds each of them in full."""
        total = abs(note.document.gross_amount or _ZERO)
        back = _ZERO
        for r in self.refunds_of(note):
            together = any(d != note.id and d in self.repo.documents for d in r.document_ids)
            back += total if together else abs(r.tx.amount)
        return back

    def credit_left(self, note: DocumentRecord) -> Decimal:
        """What of a supplier's credit note has not come back to the bank yet."""
        return abs(note.document.gross_amount or _ZERO) - self.refunded(note)

    def customer_payments(self) -> list[TxRecord]:
        """Money in that paid one of your own sales invoices, oldest first (who your customers are, §20)."""
        repo = self.repo
        return [r for r in sorted(repo.transactions.values(), key=lambda r: (r.tx.booked_on, r.id))
                if r.tx.amount > 0 and any(repo.documents[d].sales for d in r.document_ids if d in repo.documents)]

    def customer_of(self, tx: Transaction, payments: Sequence[TxRecord] | None = None) -> tuple[str, bool] | None:
        """(name, same bank account) when this counterparty paid one of your own sales invoices (§20).

        ``payments``: :meth:`customer_payments`, when the caller asks about many payments at once.
        """
        key = counterparty_key(tx.counterparty)
        iban = normalize_iban(tx.counterparty_iban) if tx.counterparty_iban else None
        by_name = None
        for r in (self.customer_payments() if payments is None else payments):
            if r.id == tx.id:
                continue
            if iban and r.tx.counterparty_iban and normalize_iban(r.tx.counterparty_iban) == iban:
                return display_name(r.tx.counterparty), True
            if key and counterparty_key(r.tx.counterparty) == key and by_name is None:
                by_name = (display_name(r.tx.counterparty), False)
        return by_name

    def refund_chain_lines(self, txs: Sequence[TxRecord], docs: Sequence[DocumentRecord]
                           ) -> tuple[tuple[str, ...], str | None]:
        """For a refund matched to a credit note: "Why?" lines naming the invoice it corrects and how that
        invoice was paid, and a headline. Nothing for a credit note netted inside a payment."""
        notes = [d for d in docs if d.document.doc_type is DocumentType.CREDIT_NOTE]
        if len(txs) != 1 or not notes:
            return (), None
        rec = txs[0]
        customer = rec.tx.amount < 0 and all(n.sales for n in notes)
        if not (customer or all(self.is_supplier_refund(rec, n) for n in notes)):
            return (), None
        lines: list[str] = []
        headline = None
        for note in notes:
            invoice = self.repo.documents.get(note.credit_for or "")
            number = note.document.invoice_number or "on file"
            if invoice is None:
                continue
            original = invoice.document.invoice_number or "on file"
            lines.append(f"Credit note {number}: corrects invoice {original}")
            paid = [self.repo.transactions[t] for t in invoice.matched_tx_ids if t in self.repo.transactions]
            if paid:
                p = paid[0]
                verb = "paid" if p.tx.amount < 0 else "paid by the customer"
                lines.append(f"Invoice {original}: {verb} {format_money(abs(p.tx.amount), p.tx.currency)} "
                             f"on {day_month(p.tx.booked_on)}")
            headline = headline or (
                f"Refund to your customer for credit note {number}, which corrects invoice {original}." if customer
                else f"Refund for credit note {number}, which corrects invoice {original}.")
        return tuple(lines), headline

    def refund_chain(self, *, tx_id: str | None = None, document_id: str | None = None) -> list[dict[str, Any]]:
        """The steps invoice -> its payment -> credit note -> refund around one refund, credit note or invoice
        (§20, §54), oldest first. Each: step, id, label, date, amount, currency, evidence ids. Empty without a
        credit note."""
        repo = self.repo
        notes: list[DocumentRecord] = []
        if tx_id is not None and tx_id in repo.transactions:
            notes = [repo.documents[d] for d in repo.transactions[tx_id].document_ids
                     if d in repo.documents and repo.documents[d].document.doc_type is DocumentType.CREDIT_NOTE]
        elif document_id is not None and document_id in repo.documents:
            doc = repo.documents[document_id]
            notes = [doc] if doc.document.doc_type is DocumentType.CREDIT_NOTE else self.credits_for(doc.id)
        steps: list[dict[str, Any]] = []
        seen: set[str] = set()

        def add(step: str, subject_id: str, label: str, day: date | None, amount: Decimal | None, currency: str,
                evidence: Sequence[str]) -> None:
            if subject_id not in seen:
                seen.add(subject_id)
                steps.append({"step": step, "id": subject_id, "label": label, "date": day, "amount": amount,
                              "currency": currency, "evidenceIds": list(evidence)})

        for note in notes:
            invoice = repo.documents.get(note.credit_for or "")
            if invoice is not None:
                inv = invoice.document
                add("invoice", invoice.id, invoice.label.split(" · ")[0], inv.issue_date,
                    abs(inv.gross_amount) if inv.gross_amount is not None else None, inv.currency, invoice.evidence_ids)
                for t in invoice.matched_tx_ids:
                    p = repo.transactions.get(t)
                    if p is None:
                        continue
                    money, day = format_money(abs(p.tx.amount), p.tx.currency), day_month(p.tx.booked_on)
                    label = (f"Paid {money} to {self.merchant_name(p.tx)} on {day}" if p.tx.amount < 0
                             else f"{display_name(p.tx.counterparty)} paid {money} on {day}")
                    add("payment", p.id, label, p.tx.booked_on, abs(p.tx.amount), p.tx.currency, [p.evidence_id])
            corrects = (f", which corrects invoice {invoice.document.invoice_number}"
                        if invoice is not None and invoice.document.invoice_number else "")
            nd = note.document
            add("credit_note", note.id, f"{note.label.split(' · ')[0]}{corrects}", nd.issue_date,
                abs(nd.gross_amount) if nd.gross_amount is not None else None, nd.currency, note.evidence_ids)
            for t in note.matched_tx_ids:
                r = repo.transactions.get(t)
                if r is None or (r.tx.amount < 0 and not note.sales):
                    continue  # a credit note netted inside a payment: that payment is the invoice's
                money, day = format_money(abs(r.tx.amount), r.tx.currency), day_month(r.tx.booked_on)
                label = (f"Refund of {money} received on {day}" if r.tx.amount > 0
                         else f"Refund of {money} paid to {display_name(r.tx.counterparty)} on {day}")
                add("refund", r.id, label, r.tx.booked_on, abs(r.tx.amount), r.tx.currency, [r.evidence_id])
        return steps

    def _match_customer_refunds(self) -> int:
        """Money back to a customer, matched to your own credit note only on exact evidence: the same amount,
        and the refund goes to the bank account the customer paid the corrected invoice from (§20)."""
        repo = self.repo
        refunds = [r for r in sorted(repo.transactions.values(), key=lambda r: r.id)
                   if r.tx.amount < 0 and not r.document_ids and not r.private and r.tx.counterparty_iban
                   and r.decision is not None and r.decision.rule == "customer_refund"
                   and not repo.items[r.item_id].is_done
                   and repo.items[r.item_id].stage not in (Stage.NEEDS_OWNER, Stage.CONFLICT)]
        if not refunds:
            return 0
        notes = [d for d in sorted(repo.documents.values(), key=lambda d: d.id)
                 if d.sales and d.document.doc_type is DocumentType.CREDIT_NOTE and not d.matched_tx_ids
                 and not d.on_hold and d.document.quality is Quality.GREEN and d.credit_for in repo.documents
                 and not repo.items[d.item_id].is_done]
        fits: dict[str, list[DocumentRecord]] = {}
        for rec in refunds:
            iban = normalize_iban(rec.tx.counterparty_iban or "")
            for note in notes:
                doc = note.document
                if doc.entity_id not in (None, rec.company_id) or doc.currency != rec.tx.currency \
                        or abs(doc.gross_amount or _ZERO) != abs(rec.tx.amount):
                    continue
                payers = [repo.transactions[t] for t in repo.documents[note.credit_for or ""].matched_tx_ids
                          if t in repo.transactions]
                if any(p.tx.counterparty_iban and normalize_iban(p.tx.counterparty_iban) == iban for p in payers):
                    fits.setdefault(rec.id, []).append(note)
        moved = 0
        for rec_id, found in sorted(fits.items()):
            if len(found) != 1 or sum(1 for other in fits.values() if found[0] in other) != 1:
                continue  # more than one way to pair them: never on a guess
            note, rec = found[0], repo.transactions[rec_id]
            invoice = repo.documents[note.credit_for or ""]
            chain, headline = self.refund_chain_lines([rec], [note])
            original = invoice.document.invoice_number or "on file"
            rec.document_ids = [note.id]
            rec.likely_document_ids = []
            rec.match_why = (f"Credit note total: {format_money(abs(note.document.gross_amount or _ZERO))}",
                             f"Money paid out: {format_money(abs(rec.tx.amount), rec.tx.currency)}",
                             f"Bank account: the one the customer paid invoice {original} from", *chain)
            rec.match_headline = headline or "Matched to your credit note."
            note.matched_tx_ids = [rec.id]
            self.reconciliation.log("match", subject_id=rec.id, evidence_ids=[rec.evidence_id, *note.evidence_ids],
                                    values={"transactions": [rec.id], "documents": [note.id]},
                                    validations=list(rec.match_why),
                                    response={"quality": Quality.GREEN.value, "kind": "customer_refund"})
            moved += 1
        return moved

    def _open_refunds(self) -> list[TxRecord]:
        """Money back from a supplier with no credit note matched yet, nothing asked about it yet."""
        repo = self.repo
        return sorted((r for r in repo.transactions.values()
                       if r.tx.amount > 0 and not r.document_ids and not r.private and r.tx.entity_id is not None
                       and r.decision is not None and r.decision.expectation is EvidenceExpectation.REFUND_OR_CREDIT_NOTE
                       and not repo.items[r.item_id].is_done
                       and repo.items[r.item_id].stage not in (Stage.NEEDS_OWNER, Stage.CONFLICT)),
                      key=lambda r: (r.tx.booked_on, r.id))

    def _open_credit_notes(self, rec: TxRecord, supplier: Supplier) -> list[DocumentRecord]:
        """That supplier's credit notes for that company with money still to come back."""
        repo = self.repo
        return sorted((d for d in repo.documents.values()
                       if d.document.doc_type is DocumentType.CREDIT_NOTE and not d.sales and not d.on_hold
                       and d.supplier_id == supplier.id and d.document.gross_amount is not None
                       and d.document.currency == rec.tx.currency and d.document.entity_id in (None, rec.company_id)
                       and d.id not in rec.not_for_document_ids and not repo.items[d.item_id].is_done
                       and repo.items[d.item_id].stage not in (Stage.NEEDS_OWNER, Stage.CONFLICT)
                       and not any(t in repo.transactions and repo.transactions[t].tx.amount < 0
                                   for t in d.matched_tx_ids)
                       and self.credit_left(d) > 0),
                      key=lambda d: (d.document.issue_date or date.min, d.id))

    def _settle_partial_refunds(self, now: datetime) -> int:
        """The rest of a credit note refunded in parts: a refund equal to exactly what is still to come,
        from the same supplier for the same company, is matched to it (and only when no other fits)."""
        repo = self.repo
        refunds = self._open_refunds()
        if not refunds:
            return 0
        resolver = repo.resolver()
        fits: dict[str, list[DocumentRecord]] = {}
        for rec in refunds:
            supplier = resolver.resolve_transaction(rec.tx).supplier
            if supplier is None:
                continue
            found = [n for n in self._open_credit_notes(rec, supplier)
                     if self.refunds_of(n) and self.credit_left(n) == rec.tx.amount]
            if found:
                fits[rec.id] = found
        moved = 0
        for rec_id, found in sorted(fits.items()):
            if len(found) != 1 or sum(1 for other in fits.values() if found[0] in other) != 1:
                continue  # more than one way to pair them: never on a guess
            note, rec = found[0], repo.transactions[rec_id]
            earlier = self.refunds_of(note)
            note.matched_tx_ids.append(rec.id)
            rec.document_ids = [note.id]
            number = note.document.invoice_number or "on file"
            chain, _ = self.refund_chain_lines([rec], [note])
            before = ", ".join(f"{format_money(r.tx.amount, r.tx.currency)} on {day_month(r.tx.booked_on)}"
                               for r in earlier)
            rec.match_why = (f"Credit note total: {format_money(abs(note.document.gross_amount or _ZERO))}",
                             f"Money received: {format_money(rec.tx.amount, rec.tx.currency)}",
                             f"Earlier refunds: {before}", "Together: the credit note in full", *chain)
            rec.match_headline = f"The rest of credit note {number}."
            rec.likely_document_ids = []
            self.reconciliation.log("match", subject_id=rec.id, evidence_ids=[rec.evidence_id, *note.evidence_ids],
                                    values={"transactions": [rec.id], "documents": [note.id]},
                                    validations=list(rec.match_why),
                                    response={"quality": Quality.GREEN.value, "kind": "refund_in_parts"})
            moved += 1
        return moved

    def _ask_about_refund_amounts(self, now: datetime) -> None:
        """Money back that does not match the supplier's credit note: one plain question, never a close (§19)."""
        repo = self.repo
        resolver = repo.resolver()
        for rec in self._open_refunds():
            if any(n.subject_id == rec.id and n.status == "open" for n in repo.needs.values()):
                continue
            supplier = resolver.resolve_transaction(rec.tx).supplier
            if supplier is None:
                continue
            notes = self._open_credit_notes(rec, supplier)
            if any(n.id in rec.likely_document_ids and self.credit_left(n) == rec.tx.amount for n in notes):
                continue  # the right amount, still being confirmed
            notes = [n for n in notes if self.credit_left(n) != rec.tx.amount][:3]
            if notes:
                self._ask_refund(rec, notes, supplier, now)

    def _ask_refund(self, rec: TxRecord, notes: Sequence[DocumentRecord], supplier: Supplier, now: datetime) -> None:
        repo = self.repo
        who = display_name(supplier.name)
        amount = format_money(rec.tx.amount, rec.tx.currency)
        when = day_month(rec.tx.booked_on, repo.today())

        def number(n: DocumentRecord) -> str:
            return n.document.invoice_number or "on file"

        def left(n: DocumentRecord) -> str:
            return format_money(self.credit_left(n), n.document.currency)

        smaller = [n for n in notes if rec.tx.amount < self.credit_left(n)]
        if len(notes) == 1:
            ask = "Is this refund part of it?" if smaller else "What is this refund for?"
            prompt = f"{who} refunded {amount} on {when}, but its credit note {number(notes[0])} is for {left(notes[0])}. {ask}"
        else:
            prompt = f"{who} refunded {amount} on {when}, but none of its credit notes is for that amount. What is it for?"
        options = [CheckOption(id=f"part:{n.id}", label=f"Part of credit note {number(n)}. The rest is still to come.",
                               values={"note": n.id}) for n in smaller]
        chase = f" Ask {who} for its credit note." if supplier.contact_email else " I'll look for its own credit note."
        options.append(CheckOption(id="other", label=f"Something else.{chase}",
                                   values={"notes": ",".join(n.id for n in notes)}))
        why = []
        for n in notes:
            back = self.refunded(n)
            came = f", and {format_money(back, n.document.currency)} of it came back already" if back else ""
            why.append(f"Credit note {number(n)} is for "
                       f"{format_money(abs(n.document.gross_amount or _ZERO), n.document.currency)}{came}.")
        account = repo.accounts.get(rec.tx.account_id)
        company = repo.company_name(rec.company_id) or "your company"
        why += [f"{amount} arrived in {company}'s account{f' ({account.label})' if account else ''} on {when}.",
                "The amounts are not the same, so I won't match them on a guess.",
                "Until you answer, I won't close this refund."]
        needs_id = _unique_id(repo.needs, f"nd_{_slug(who.split()[0])}_refund")
        repo.needs[needs_id] = NeedsYouRecord(
            id=needs_id, kind="refund", subject_type="transaction", subject_id=rec.id, item_id=rec.item_id,
            company_id=rec.company_id, created_at=now, why=tuple(why), prompt=prompt, options=tuple(options))
        evidence = [rec.evidence_id, *(e for n in notes for e in n.evidence_ids)]
        self.reconciliation.log("ask_owner", subject_id=rec.id, evidence_ids=evidence,
                                values={"credit_notes": [n.id for n in notes], "refund": rec.tx.amount},
                                response={"needs_you": needs_id})
        self.activity(now, "checked", f"{who} refunded {amount}, but its credit note is for a different amount. "
                      "I asked you about it.", rec.company_id, amount=rec.tx.amount, currency=rec.tx.currency,
                      evidence_ids=evidence)
        self.advance(repo.items[rec.item_id], Stage.NEEDS_OWNER, evidence, agent="reconciliation",
                     note="A refund that does not match its credit note.")

    def _answer_refund(self, needs: NeedsYouRecord, option_id: str, answer_ev: str, now: datetime) -> AnswerOutcome:
        """The owner said whether a refund is part of a credit note for more, or for something else (§19, §37)."""
        repo = self.repo
        option = next((o for o in needs.options if o.id == option_id), None)
        if option is None:
            raise ValueError("not one of the options")
        rec = repo.transactions[needs.subject_id]
        item = repo.items[rec.item_id]
        owner = f"{OWNER_ACTOR}:{repo.owner.email}"
        who = self.merchant_name(rec.tx)
        amount = format_money(rec.tx.amount, rec.tx.currency)
        if option.id == "other":
            needs.status, needs.answer, needs.answered_at = "answered", option.id, now
            for note_id in str(option.values.get("notes", "")).split(","):
                if note_id and note_id not in rec.not_for_document_ids:
                    rec.not_for_document_ids.append(note_id)
            self.advance(item, Stage.UNDERSTOOD, [rec.evidence_id, answer_ev], agent="reconciliation", actor=owner,
                         note="Not for that credit note, as you said.")
            self.activity(now, "answered", f"You said the {amount} refund from {who} is for something else.",
                          rec.company_id, evidence_ids=[answer_ev])
            return AnswerOutcome(ok=True, message="Done. I'm looking for its own credit note.")
        note = repo.documents[option.values["note"]]
        if rec.tx.amount >= self.credit_left(note):
            raise ValueError("That credit note has no more than this left to refund.")
        needs.status, needs.answer, needs.answered_at = "answered", option.id, now
        rec.refund_answer_ev = answer_ev
        note.matched_tx_ids.append(rec.id)
        rec.document_ids = [note.id]
        rec.likely_document_ids = []
        number = note.document.invoice_number or "on file"
        still = format_money(self.credit_left(note), note.document.currency)
        chain, _ = self.refund_chain_lines([rec], [note])
        rec.match_why = (f"Credit note total: {format_money(abs(note.document.gross_amount or _ZERO))}",
                         f"Money received: {amount}", "You said: it is part of this credit note",
                         f"Still to come: {still}", *chain)
        rec.match_headline = f"Part of credit note {number}. {still} of it is still to come."
        note.hold_reason = f"{still} of the {display_name(note.document.supplier_name)} credit note {number} is still to come."
        self.reconciliation.log("match", subject_id=rec.id, evidence_ids=[rec.evidence_id, *note.evidence_ids, answer_ev],
                                values={"transactions": [rec.id], "documents": [note.id]}, actor=OWNER_ACTOR,
                                validations=list(rec.match_why), response={"kind": "refund_in_parts"})
        self.advance(item, Stage.UNDERSTOOD, [rec.evidence_id, *note.evidence_ids, answer_ev], agent="reconciliation",
                     actor=owner, note=f"Part of credit note {number}, as you said.")
        self.activity(now, "answered", f"You said the {amount} refund from {who} is part of credit note {number}.",
                      rec.company_id, amount=rec.tx.amount, currency=rec.tx.currency, evidence_ids=[answer_ev])
        return AnswerOutcome(ok=True, message=f"Done. I counted it as part of credit note {number}. {still} is still to come.")

    # ----------------------------------------------------------------- how a supplier is usually paid (L4)

    def method_labels(self) -> dict[str, str]:
        """Plain names of the cards and accounts ("card:4817" -> "card •••• 4817")."""
        out: dict[str, str] = {}
        for a in sorted(self.repo.accounts.values(), key=lambda a: a.id):
            if a.card_last4:
                out.setdefault(f"card:{a.card_last4}", a.label)
            out[f"account:{a.id}"] = a.label
        return out

    def payment_keys(self) -> Any:
        """A function giving the key a counterparty's payments are grouped by: the known supplier, else its
        cleaned name (one resolver, cached per name)."""
        resolver = self.repo.resolver()
        cache: dict[str, str | None] = {}

        def key(name: str | None) -> str | None:
            if not name:
                return None
            if name not in cache:
                cache[name] = resolver.resolve(name).key or counterparty_key(name)
            return cache[name]

        return key

    def payment_groups(self, key: Any) -> dict[str, list[Transaction]]:
        """Every payment out (the imported history included), grouped by counterparty key."""
        groups: dict[str, list[Transaction]] = {}
        for t in (*self.repo.history_transactions, *(r.tx for r in self.repo.transactions.values())):
            if t.amount < 0 and (k := key(t.counterparty)):
                groups.setdefault(k, []).append(t)
        return groups

    @staticmethod
    def payment_series(key: str, payments: Sequence[Transaction], *, before: date | None = None,
                       exclude: str | None = None) -> RecurringSeries | None:
        """The rhythm of payments to one counterparty, with the card or account it is usually paid from (L4)."""
        txs = [t for t in payments if t.id != exclude and (before is None or t.booked_on < before)]
        if len(txs) < 2:
            return None
        occurrences = [Occurrence(on=t.booked_on, amount=abs(t.amount), currency=t.currency, label=t.counterparty,
                                  ref=t.id, method=method_of(t)) for t in txs]
        return learn_series(key, occurrences, basis=Basis.PAYMENTS)

    def _note_payment_methods(self, created: Sequence[TxRecord], at: datetime) -> None:
        """A payment of a supplier from another card or account than usual is noted in plain words (L4): a
        note on the payment and one line in Activity, never a hold. Not during a first run's history import."""
        outgoing = [r for r in created if r.tx.amount < 0]
        if not outgoing:
            return
        labels = self.method_labels()
        key_of = self.payment_keys()
        groups = self.payment_groups(key_of)
        for rec in sorted(outgoing, key=lambda r: (r.tx.booked_on, r.id)):
            key = key_of(rec.tx.counterparty)
            if not key:
                continue
            series = self.payment_series(key, groups.get(key, []), before=rec.tx.booked_on, exclude=rec.id)
            note = payment_method_note(series, rec.tx, labels) if series is not None else None
            if note is None or note in rec.notes:
                continue
            rec.notes.append(note)
            self.log("reconciliation", "payment_method_differs", subject_id=rec.id, evidence_ids=[rec.evidence_id],
                     values={"usual": labels.get(series.usual_method or "", "") if series else "",
                             "this_time": labels.get(method_of(rec.tx), "")})
            if not self.repo.onboarding.learning:
                who = self.merchant_name(rec.tx)
                self.activity(at, "checked", f"{who}: {note}", rec.holder_id, amount=abs(rec.tx.amount),
                              currency=rec.tx.currency, evidence_ids=[rec.evidence_id])

    # ----------------------------------------------------------------- evidence needs learned from answers (J7)

    def learned_evidence(self, rec: TxRecord) -> str | None:
        """The stored answer or rule behind a learned expectation of this payment, if any."""
        key = self.repo.resolver().resolve_transaction(rec.tx).key
        found = _LearnedExpectations(self.repo).lookup(rec.tx, key)
        return found.evidence_id if found is not None else None

    def learn_evidence_need(self, tx_id: str, need: str, *, by: str = "owner", company_id: str | None = None,
                            evidence_id: str | None = None) -> tuple[LearnedExpectation, int]:
        """"This never has an invoice" / "This always needs one", from the owner or the accountant (J7).

        It is remembered for the counterparty (its IBAN when the bank shows one, else the supplier or bank
        name) and used for this payment and every later one; open payments of that counterparty are decided
        again now. ``company_id`` limits an accountant's answer to one company. Returns what was learned and
        how many payments it changed.
        """
        repo = self.repo
        rec = repo.transactions[tx_id]
        if need not in ("none", "invoice", "receipt"):
            raise ValueError("need is none, invoice or receipt")
        expectation = {"none": EvidenceExpectation.BANK_EVIDENCE_SUFFICES, "invoice": EvidenceExpectation.INVOICE,
                       "receipt": EvidenceExpectation.RECEIPT}[need]
        who = self.merchant_name(rec.tx)
        reasons = {
            ("owner", "none"): f"You told me {who} never sends an invoice. The bank record is enough.",
            ("owner", "invoice"): f"You told me {who} always sends an invoice.",
            ("owner", "receipt"): f"You told me a receipt is enough for {who}.",
            ("accountant", "none"): f"Your accountant said {who} needs no invoice. The bank record is enough.",
            ("accountant", "invoice"): f"Your accountant said {who} always needs an invoice.",
            ("accountant", "receipt"): f"Your accountant said a receipt is enough for {who}.",
        }
        now = repo.clock.now()
        if evidence_id is None:
            body = json.dumps({"kind": "evidence_need", "transaction_id": tx_id, "need": need, "by": by,
                               "answered_at": now.isoformat(), "answered_by": repo.owner.email if by == "owner"
                               else by}, sort_keys=True).encode()
            evidence_id = repo.registry.register(body, tenant_id=repo.tenant_id, source_kind=SourceKind.UPLOAD,
                                                 format=EvidenceFormat.JSON, mime_type="application/json",
                                                 retrieved_at=now, metadata={"kind": "owner_answer"}).evidence.id
        learned = LearnedExpectation(expectation, reasons[(by, need)], evidence_id=evidence_id, taught_by=by)
        store = repo.expectation_overrides
        if company_id is not None:
            store = repo.company_expectation_overrides.setdefault(company_id, InMemoryExpectationOverrides())
        match = repo.resolver().resolve_transaction(rec.tx)
        if rec.tx.counterparty_iban:
            store.remember_iban(rec.tx.counterparty_iban, learned)
        if match.supplier is not None:
            store.remember_supplier(match.key, learned)
        try:
            store.remember_descriptor(rec.tx.counterparty, learned)
        except ValueError:
            pass  # nothing in the bank name to remember: the IBAN or supplier carries it
        key_of = self.payment_keys()
        mine, iban = key_of(rec.tx.counterparty), normalize_iban(rec.tx.counterparty_iban or "")
        changed = self.redecide(lambda r: key_of(r.tx.counterparty) == mine
                                or (bool(iban) and normalize_iban(r.tx.counterparty_iban or "") == iban))
        actor = f"{OWNER_ACTOR}:{repo.owner.email}" if by == "owner" else f"accountant:{by}"
        self.log("reconciliation", "learn_expectation", subject_id=tx_id, evidence_ids=[evidence_id], actor=actor,
                 values={"need": need, "by": by, "company": company_id or ""}, response={"changed": changed})
        if by == "owner":
            self.owner_time(now, entity_id=rec.company_id, step="evidence_need")
        self.activity(now, "learned", f"Learned: {learned.reason} I will remember this.", company_id or rec.company_id,
                      evidence_ids=[evidence_id])
        self.run(now)
        return learned, changed

    def redecide(self, which: Any) -> int:
        """Decide again what evidence open payments need (after something was learned); returns how many changed."""
        engine = self.reconciliation.engine()
        changed = 0
        for rec in sorted(self.repo.transactions.values(), key=lambda r: r.id):
            if rec.decision is None or rec.document_ids or rec.private or self.repo.items[rec.item_id].is_done:
                continue
            if not which(rec):
                continue
            decision = engine.classify(rec.tx)
            if decision == rec.decision:
                continue
            rec.decision = decision
            self.reconciliation.log("expect", subject_id=rec.id, evidence_ids=[rec.evidence_id],
                                    values={"expectation": decision.expectation.value, "rule": decision.rule},
                                    response={"quality": decision.quality.value, "reason": decision.reason})
            changed += 1
        return changed

    # ----------------------------------------------------------------- due dates (F8)

    def usable_due_date(self, record: DocumentRecord) -> date | None:
        """The due date an invoice states, when it can be used: a real date, not before the invoice's own date,
        and not disputed between sources (F8)."""
        due = record.document.due_date
        if due is None:
            return None
        check = record.checks.get(CriticalField.DUE_DATE.value)
        if check is not None and (check.quality is Quality.RED or check.value is None):
            return None
        issued = record.document.issue_date
        if issued is not None and due < issued:
            return None
        return due

    def invoice_due(self, record: DocumentRecord, today: date | None = None) -> dict[str, Any] | None:
        """An unpaid purchase invoice with a usable due date: when it is due and a plain line about it (F8).

        None for anything already paid (matched), on hold (the hold says it), a sale, a supporting document,
        a cash receipt or a credit note. ``overdue`` once the due date has passed without its payment.
        """
        doc = record.document
        if record.sales or record.supporting or record.paid_in_cash or record.on_hold or record.matched_tx_ids \
                or record.supports_tx_ids or doc.doc_type not in _PAYABLE_LATER or record.payslip is not None:
            return None
        if self.repo.items[record.item_id].is_done:
            return None
        due = self.usable_due_date(record)
        if due is None:
            return None
        today = today or self.repo.today()
        who = display_name(doc.supplier_name)
        amount = format_money(doc.gross_amount, doc.currency) if doc.gross_amount is not None else None
        what = f"The {who} invoice" + (f" of {amount}" if amount else "")
        days = (due - today).days
        if days < 0:
            line = f"{what} was due on {day_month(due, today)}. I haven't seen its payment yet."
        elif days == 0:
            line = f"{what} is due today. I haven't seen its payment yet."
        else:
            line = f"{what} is due on {day_month(due, today)}."
        if days < 0 and (likely := self.likely_payment(record, due)) is not None:
            paid_with = self.method_labels().get(method_of(likely.tx), "")
            line = (f"{what} was due on {day_month(due, today)}. A payment of "
                    f"{format_money(abs(likely.tx.amount), likely.tx.currency)} on "
                    f"{day_month(likely.tx.booked_on, today)}{' from ' + paid_with if paid_with else ''} may be it: "
                    "I'm checking.")
        return {"due": due, "overdue": days < 0, "days": days, "line": line, "document_id": record.id}

    def likely_payment(self, record: DocumentRecord, around: date) -> TxRecord | None:
        """An unmatched payment of this invoice's supplier, at its amount, near ``around``: looked for on the
        supplier's usual card or account first (L4). Never a match: only words for the owner."""
        doc = record.document
        if record.supplier_id is None or doc.gross_amount is None:
            return None
        key_of = self.payment_keys()
        series = self.payment_series(record.supplier_id, self.payment_groups(key_of).get(record.supplier_id, []))
        if series is None:
            return None
        open_txs = [r.tx for r in self.repo.transactions.values() if not r.document_ids and not r.private
                    and r.tx.amount == -abs(doc.gross_amount)]
        found, _ = find_payment(series, open_txs, around=around, key=key_of)
        return self.repo.transactions.get(found.id) if found is not None else None

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
        if self.staged.is_staged(self.repo.documents[match.document_ids[0]]):
            return  # a part of an invoice paid in parts: the bank never shows its whole total
        self.reverify_pair(self.repo.transactions[match.transaction_ids[0]],
                           self.repo.documents[match.document_ids[0]])

    def reverify_pair(self, rec: TxRecord, doc: DocumentRecord) -> None:
        """One payment and its one document: the bank's booked amount confirms the document's total (§19)."""
        evidence = [*doc.evidence_ids, rec.evidence_id]
        # Paid net of a linked credit note: the payment plus the credit is what the invoice's total must show.
        credit = sum((abs(c.document.gross_amount or _ZERO) for c in self.credits_for(doc.id)
                      if c.id in rec.document_ids), _ZERO)
        charge = self.bank_charge(rec, doc)
        if doc.is_foreign:
            # From abroad: the bank, in the document's own currency, is the independent source (checklist P7).
            if charge is not None and credit:
                charge = replace(charge, amount=charge.amount + credit)
            assessment = self.verification.assess(
                doc.observations, doc.document.doc_type, subject_id=doc.id, evidence_ids=evidence,
                owner=doc.owner_values, issuer=doc.issuer, bank=charge, country=doc.country)
        else:
            same = rec.tx.currency.strip().upper() == doc.document.currency.strip().upper()
            # Another currency without the bank's conversion line: stated, never compared as if equal.
            amount, currency = ((abs(rec.tx.amount) + credit, None) if same else
                                (charge.amount + credit, charge.currency) if charge is not None else
                                (abs(rec.tx.amount), rec.tx.currency))
            assessment = self.verification.assess(
                doc.observations, doc.document.doc_type, bank_amount=amount, subject_id=doc.id,
                evidence_ids=evidence, owner=doc.owner_values, bank_currency=currency,
                country=doc.country)  # its company's VAT rates (§49), as when it was first read
        doc.document = doc.document.model_copy(update={"quality": assessment.quality})
        doc.reasons = assessment.reasons
        doc.checks = assessment.verified_fields
        self._buyer_checked(doc)
        if doc.document.entity_id is None and rec.tx.entity_id:
            doc.document = doc.document.model_copy(update={"entity_id": rec.tx.entity_id})

    def bank_charge(self, rec: TxRecord, doc: DocumentRecord) -> BankCharge | None:
        """What the bank says was paid, in the document's currency: the booked amount, or the original
        amount on the bank's conversion line ("USD 125,00 TAXA 0,9215"). None when it says neither."""
        tx = rec.tx
        currency = (doc.document.currency or "EUR").strip().upper()
        if tx.currency.strip().upper() == currency:
            return BankCharge(abs(tx.amount), currency, tx.booked_on, rec.evidence_id)
        fx = fx_from_text(f"{tx.counterparty} {tx.description}", tx.currency)
        if fx is not None and fx.original_currency == currency:
            return BankCharge(fx.original_amount, currency, tx.booked_on, rec.evidence_id, converted=True)
        return None

    def paid_in_document_currency(self, rec: TxRecord, doc: DocumentRecord) -> Decimal | None:
        charge = self.bank_charge(rec, doc)
        return charge.amount if charge is not None else None

    def bank_confirmations(self, txs: Sequence[TxRecord], docs: Sequence[DocumentRecord]) -> dict[str, str]:
        """Documents from abroad that exactly one payment confirms: {document id: that payment's id}.

        Such a document has no fiscal QR code, so no second source of its own, and reconciliation
        would never match it GREEN. When the bank alone proves it (amount, currency, date, with the
        rules that hold anywhere, checklist P7) it is offered to reconciliation as confirmed, and only
        a GREEN 1-to-1 match with that one payment keeps it (see ``ReconciliationAgent.match``).
        """
        out: dict[str, str] = {}
        for record in sorted(docs, key=lambda d: d.id):
            doc = record.document
            if not record.is_foreign or doc.quality is not Quality.AMBER or doc.gross_amount is None:
                continue
            outgoing = doc.doc_type is not DocumentType.CREDIT_NOTE
            agreeing = []
            for rec in sorted(txs, key=lambda r: r.id):
                if (rec.tx.amount < 0) != outgoing:
                    continue
                charge = self.bank_charge(rec, record)
                if charge is None or charge.amount != abs(doc.gross_amount):
                    continue
                if doc.issue_date is not None and not (
                        -PAYMENT_LEAD_DAYS <= (charge.booked_on - doc.issue_date).days <= PAYMENT_LAG_DAYS):
                    continue
                assessment = self.verification.assess(record.observations, doc.doc_type, owner=record.owner_values,
                                                      issuer=record.issuer, bank=charge, log=False,
                                                      country=record.country)
                if assessment.quality is Quality.GREEN:
                    agreeing.append(rec.id)
            if len(agreeing) == 1:
                out[record.id] = agreeing[0]
        return out

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

    def answer(self, needs_id: str, option_id: str, *, remember: bool = False, split: Any = None) -> AnswerOutcome:
        """The owner's one tap. ``split``: for "which job is this for?", the amounts or percentages of a split."""
        repo = self.repo
        needs = repo.needs.get(needs_id)
        if needs is None:
            raise KeyError(needs_id)
        if needs.status != "open":
            raise PermissionError("already answered")
        now = repo.clock.now()
        chosen = None
        if needs.kind == "cost_center":
            # Checked before anything is recorded: a split that doesn't add up changes nothing.
            chosen = self._cost_center_choice(needs, option_id, split)
        elif needs.kind in ("statement", "recharge", "chargeback", *_STAGED_QUESTIONS, *self.staff.NEEDS_KINDS,
                            *self.book_questions(), *self.captures.NEEDS_KINDS) and \
                option_id not in {o.id for o in needs.options}:
            raise ValueError("not one of the options")
        answer_ev = self._record_answer(needs, option_id, now, split=split if needs.kind == "cost_center" else None)
        self.owner_time(now, kind=InteractionKind.APPROVAL if needs.kind == "approval" else InteractionKind.ANSWER,
                        entity_id=needs.company_id or None, step=f"answer:{needs.kind}")
        if needs.kind == "approval":
            outcome = self._answer_approval(needs, option_id, answer_ev, now)
        elif needs.kind == "check":
            outcome = self._answer_check(needs, option_id, answer_ev, now)
        elif needs.kind == "company":
            outcome = self._answer_company(needs, option_id, answer_ev, now)
        elif needs.kind == "cash":
            outcome = self._answer_cash(needs, option_id, answer_ev, now)
        elif needs.kind == "obligation":
            outcome = self._answer_obligation(needs, option_id, answer_ev, now)
        elif needs.kind == "obligation_company":
            outcome = self._answer_obligation_company(needs, option_id, answer_ev, now)
        elif needs.kind == "refund":
            outcome = self._answer_refund(needs, option_id, answer_ev, now)
        elif needs.kind == "cost_center":
            assert chosen is not None
            outcome = self._answer_cost_center(needs, option_id, chosen, remember, answer_ev, now)
        elif needs.kind == "statement":
            outcome = self.statements.answer(needs, option_id, answer_ev, now)
        elif needs.kind == "recharge":
            outcome = self._answer_recharge(needs, option_id, remember, answer_ev, now)
        elif needs.kind in _STAGED_QUESTIONS:
            outcome = self.staged.answer(needs, option_id, answer_ev, now)
        elif needs.kind in self.staff.NEEDS_KINDS:
            outcome = self.staff.answer(needs, option_id, answer_ev, now)
        elif needs.kind == "chargeback":
            outcome = self.chargebacks.answer(needs, option_id, answer_ev, now)
        elif needs.kind in self.book_questions():
            outcome = self._book_agent(needs.kind).answer(needs, option_id, answer_ev, now)
        elif needs.kind in self.captures.NEEDS_KINDS:  # a photo to take again; is this copy the same invoice?
            outcome = self.captures.answer(needs, option_id, answer_ev, now)
        else:
            outcome = self._answer_choice(needs, option_id, remember, answer_ev, now)
        self.run(now)
        return outcome

    def book_questions(self) -> tuple[str, ...]:
        """Question kinds of the leasing, cash and membership agents."""
        return (*self.leases.NEEDS_KINDS, *self.cash.NEEDS_KINDS, *self.members.NEEDS_KINDS)

    def _book_agent(self, kind: str) -> Any:
        return next(a for a in (self.leases, self.cash, self.members) if kind in a.NEEDS_KINDS)

    def matched_elsewhere(self, rec: TxRecord) -> bool:
        """A payment its own agent matches (never by amount alone in the general matching): a leasing payment of
        a plan, cash paid into the bank, a direct debit that came back and the payment it returns."""
        return (self.leases.kept_from_matching(rec) or self.members.kept_from_matching(rec)
                or (rec.decision is not None and rec.decision.rule == "cash_deposit"))

    def _record_answer(self, needs: NeedsYouRecord, option_id: str, now: datetime, *, split: Any = None) -> str:
        record = {"needs_id": needs.id, "option_id": option_id, "answered_by": self.repo.owner.email,
                  "answered_at": now.isoformat(), "subject_id": needs.subject_id}
        if split is not None:
            record["split"] = split  # the split itself is the owner's evidence (§55)
        body = json.dumps(record, sort_keys=True, default=str).encode()
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

    # ----------------------------------------------------------------- which job (cost centers)

    def _cost_center_choice(self, needs: NeedsYouRecord, option_id: str, split: Any) -> CostAllocation:
        """What the owner's answer means, checked before anything is recorded (a bad split changes nothing)."""
        question = needs.question
        assert question is not None
        rec = self.repo.transactions[needs.subject_id]
        facts = self.cost_centers.payment_facts(rec)
        if option_id == SPLIT_OPTION:
            if split is None:
                raise SplitError("Tell me how to split it: an amount or a percentage for each one.")
            return self.cost_centers.owner_allocation(rec.company_id, facts, split=split)
        try:
            option = question.option(option_id)
        except KeyError:
            raise ValueError("not one of the options") from None
        if option.kind is OptionKind.GENERAL:
            return self.cost_centers.owner_allocation(rec.company_id, facts, general=True)
        if option.kind is OptionKind.COST_CENTER:
            return self.cost_centers.owner_allocation(rec.company_id, facts, cost_center_id=option.cost_center_id)
        raise ValueError("not one of the options")

    def _allocation_words(self, allocation: CostAllocation) -> str:
        labels = {c.id: c.label for c in self.repo.cost_centers.values()}
        if allocation.general:
            return "in general costs"
        if allocation.is_split:
            words = [f"{labels.get(s.cost_center_id, 'another one')} {format_money(s.amount, allocation.currency)}"
                     + (" (to recharge to them)" if s.recharge else "") for s in allocation.shares]
            return "split: " + _join_words(words)
        tail = ", to recharge to them" if allocation.shares[0].recharge else ""
        return f"on {labels.get(allocation.shares[0].cost_center_id, 'it')}{tail}"

    def _learn_cost_center_rule(self, question: Question, option_id: str, allocation: CostAllocation,
                                answer_ev: str, now: datetime) -> str | None:
        split = percents_of(allocation.shares, allocation.total) if option_id == SPLIT_OPTION else ()
        names = {c.id: c.label for c in self.repo.cost_centers.values()}
        proposal = suggest_cost_center_rule(question, option_id, answered_by=OWNER_ACTOR, answered_at=now,
                                            split=split, names=names)
        if proposal is None:
            return None
        rule = proposal.rule.model_copy(update={"id": "rule_" + hashlib.sha256(
            f"{question.id}:{option_id}:{split}".encode()).hexdigest()[:12]})
        self.repo.rulebook.add(rule, reason="one-tap answer")
        label = f"{proposal.label[0].lower()}{proposal.label[1:]}"
        self.activity(now, "learned", f"Learned: {label}. I will not ask again.", None, evidence_ids=[answer_ev])
        self.cost_centers.log("learn_rule", subject_id=rule.id, evidence_ids=[answer_ev], actor=OWNER_ACTOR,
                              values={"label": proposal.label})
        return proposal.label

    def _answer_cost_center(self, needs: NeedsYouRecord, option_id: str, chosen: CostAllocation, remember: bool,
                            answer_ev: str, now: datetime) -> AnswerOutcome:
        repo = self.repo
        question = needs.question
        assert question is not None
        rec = repo.transactions[needs.subject_id]
        allocation = chosen.model_copy(update={"evidence_ids": tuple(dict.fromkeys((*chosen.evidence_ids, answer_ev)))})
        self.cost_centers.apply_payment(rec, allocation)
        self.cost_centers.carry_to_documents(rec, by_owner=True)
        needs.status, needs.answer, needs.answered_at = "answered", option_id, now
        who = self.merchant_name(rec.tx)
        noun = noun_for(self.cost_centers.centers(rec.company_id)) or "job"
        before = {n.id for n in repo.needs.values() if n.kind == "cost_center" and n.status == "open"}
        learned = self._learn_cost_center_rule(question, option_id, allocation, answer_ev, now) if remember else None
        self.activity(now, "answered", f"You told me which {noun} the {who} payment is for.", rec.company_id,
                      amount=abs(rec.tx.amount), currency=rec.tx.currency, evidence_ids=[answer_ev])
        if learned:
            self.cost_centers.allocate(now)
        resolved = tuple(sorted(n.id for n in repo.needs.values() if n.id in before and n.status == "resolved"))
        return AnswerOutcome(ok=True, message=f"Done. The {who} payment is now {self._allocation_words(allocation)}.",
                             learned=learned, resolved_ids=resolved)

    # ----------------------------------------------------------------- sensitive documents (checklist X32)

    def _filename(self, evidence_ids: Sequence[str]) -> str:
        for evidence_id in evidence_ids:
            try:
                name = self.repo.evidence(evidence_id).filename
            except Exception:  # noqa: BLE001 - a missing name is no name
                continue
            if name:
                return str(name)
        return ""

    def _classify_sensitive(self, record: DocumentRecord, *texts: str) -> None:
        """Mark a new document sensitive when its own words (or its kind: a payslip) say so (§52)."""
        from backoffice.sensitivity import classify

        category = classify(*texts, doc_type=record.document.doc_type)
        if category is None:
            return
        record.sensitive, record.sensitive_by = category, "wording"
        self.log("document", "mark_sensitive", subject_id=record.id, evidence_ids=record.evidence_ids,
                 values={"category": category, "by": "wording"})

    def mark_sensitive(self, document_id: str, sensitive: bool) -> DocumentRecord:
        """The owner marks a document sensitive, or not (§52). KeyError for an unknown document."""
        record = self.repo.documents[document_id]
        before = record.sensitive
        record.sensitive = ("owner" if before is None else before) if sensitive else None
        record.sensitive_by = "owner"
        self.log("owner", "mark_sensitive", subject_id=document_id, evidence_ids=record.evidence_ids,
                 values={"sensitive": bool(sensitive), "category": record.sensitive or "", "before": before or ""},
                 actor=OWNER_ACTOR)
        return record

    def sensitive_documents(self) -> dict[str, DocumentRecord]:
        return {d.id: d for d in self.repo.documents.values() if d.sensitive}

    def record_access(self, document_ids: Sequence[str], *, who: str, role: str, at: datetime,
                      how: str = "opened the original") -> list[AccessEntry]:
        """Log reads of sensitive documents' originals (who, when, which document), for the owner (§52)."""
        entries = []
        for document_id in dict.fromkeys(document_ids):
            record = self.repo.documents.get(document_id)
            if record is None or not record.sensitive:
                continue
            entry = AccessEntry(at=at, document_id=document_id, who=who, role=role, how=how)
            self.repo.document_access.append(entry)
            self.log("access", "document_opened", subject_id=document_id, evidence_ids=record.evidence_ids,
                     values={"who": who, "role": role, "how": how})
            entries.append(entry)
        return entries

    def allocate_by_owner(self, subject_id: str, *, cost_center_id: str | None = None, general: bool = False,
                          split: Any = None, remember: bool = False, recharge: bool | None = None,
                          answered_by: str | None = None) -> AnswerOutcome:
        """The owner puts one payment or document on a cost center, on general costs, or splits it.

        ``recharge``: True when the client it is for pays it back (a reimbursable or pass-through cost),
        False when it is the business's own; None leaves that to the evidence (and one question when it
        genuinely cannot be told). Raises KeyError (unknown subject) or :class:`SplitError` (plain message)
        before anything is recorded.
        """
        repo = self.repo
        agent = self.cost_centers
        rec = repo.transactions.get(subject_id)
        record = repo.documents.get(subject_id) if rec is None else None
        if rec is None and record is None:
            raise KeyError(subject_id)
        if rec is not None:
            if rec.private or rec.tx.entity_id is None:
                raise SplitError("First tell me which company this payment belongs to.")
            company: str | None = rec.company_id
            facts: CostCenterFacts | None = agent.payment_facts(rec)
            thing, who = "payment", self.merchant_name(rec.tx)
        else:
            assert record is not None
            company = repo.item_company(repo.items[record.item_id])
            if company is None:
                raise SplitError("First tell me which company this document belongs to.")
            facts = agent.document_facts(record, company)
            thing = _DOC_LABELS.get(record.document.doc_type, "Document").lower()
            who = display_name(record.document.supplier_name)
        if facts is None:
            raise SplitError("This document has no total I can use.")
        centers = agent.centers(company)
        if not centers:
            raise SplitError("Add a job, property, vehicle or client for this company first.")
        allocation = agent.owner_allocation(company, facts, cost_center_id=cost_center_id, general=general,
                                            split=split)
        outgoing = rec.tx.amount < 0 if rec is not None else (
            not record.sales and record.document.doc_type is not DocumentType.CREDIT_NOTE)  # type: ignore[union-attr]
        if recharge is not None and allocation.general:
            raise SplitError("General costs are your own: only a cost on a client can be paid back by them.")
        if recharge is not None and not outgoing:
            raise SplitError("Only a cost you paid can be paid back by a client.")
        option_id = SPLIT_OPTION if split is not None else ("general" if general else f"cc:{cost_center_id}")
        now = repo.clock.now()
        said = {"subject_id": subject_id, "option_id": option_id}
        if recharge is not None:
            said["recharge"] = recharge  # the owner's own word that the client pays it back (§55)
        body = json.dumps({"kind": "cost_center", **said,
                           "split": split, "answered_by": answered_by or repo.owner.email,
                           "answered_at": now.isoformat()},
                          sort_keys=True, default=str).encode()
        reg = repo.registry.register(body, tenant_id=repo.tenant_id, source_kind=SourceKind.UPLOAD,
                                     format=EvidenceFormat.JSON, mime_type="application/json", retrieved_at=now,
                                     metadata={"kind": "owner_answer"})
        answer_ev = reg.evidence.id
        self.log("owner", "allocate_cost_center", subject_id=subject_id, evidence_ids=[answer_ev],
                 actor=f"manager:{answered_by}" if answered_by else OWNER_ACTOR, values={"option_id": option_id})
        allocation = allocation.model_copy(update={"evidence_ids": (*allocation.evidence_ids, answer_ev)})
        if recharge is not None:
            labels = join_and([c.label for c in centers if c.id in allocation.cost_center_ids])
            allocation = agent.marked(allocation, allocation.cost_center_ids if recharge else (), "owner",
                                      (f"You said {labels} pays this back." if recharge else
                                       "You said this is your own cost.",))
        elif outgoing:
            allocation = agent.with_recharge(facts, allocation, agent.recharge_history().get(company or ""))
        payments = [rec] if rec is not None else [
            repo.transactions[t] for t in record.matched_tx_ids  # type: ignore[union-attr]
            if t in repo.transactions and len(record.matched_tx_ids) == 1]  # type: ignore[union-attr]
        if rec is not None:
            agent.apply_payment(rec, allocation)
            agent.carry_to_documents(rec, by_owner=True)
        else:
            agent.apply_document(record, allocation)  # type: ignore[arg-type]
            for paid in payments:
                same = agent.scaled(allocation, abs(paid.tx.amount), evidence_ids=[paid.evidence_id])
                agent.apply_payment(paid, same)
        for paid in payments:
            open_q = agent.open_question(paid.id)
            if open_q is not None:
                open_q.status, open_q.answer, open_q.answered_at = "answered", option_id, now
        self.owner_time(now, entity_id=company, step="allocate")
        learned = None
        if remember:
            question = cost_center_question(centers, facts, (), repo.today())
            learned = self._learn_cost_center_rule(question, option_id, allocation, answer_ev, now)
        noun = noun_for(centers)
        self.activity(now, "answered", f"You told me which {noun} the {who} {thing} is for.", company,
                      amount=facts.total, currency=facts.currency, evidence_ids=[answer_ev])
        self.run(now)
        return AnswerOutcome(ok=True, message=f"Done. The {who} {thing} is now {self._allocation_words(allocation)}.",
                             learned=learned)

    def _answer_recharge(self, needs: NeedsYouRecord, option_id: str, remember: bool, answer_ev: str,
                         now: datetime) -> AnswerOutcome:
        """The owner said whether the client pays this cost back. Their answer is the evidence (§55)."""
        repo = self.repo
        agent = self.cost_centers
        rec = repo.transactions[needs.subject_id]
        allocation = rec.tx.cost_allocation
        yes = option_id == "recharge"
        needs.status, needs.answer, needs.answered_at = "answered", option_id, now
        who = self.merchant_name(rec.tx)
        cid = str(needs.options[0].values.get("cost_center_id", "")) if needs.options else ""
        center = repo.cost_centers.get(cid)
        if allocation is None or allocation.general or center is None or cid not in allocation.cost_center_ids:
            return AnswerOutcome(ok=True, message=f"Done. The {who} payment is not on a client any more, so there "
                                                  "is nothing to pay back.")
        why = (f"You said {center.label} pays this back.",) if yes else ("You said this is your own cost.",)
        keep = [s.cost_center_id for s in allocation.shares if s.recharge and s.cost_center_id != cid]
        updated = agent.marked(allocation, [*keep, *([cid] if yes else [])], "owner", why)
        updated = updated.model_copy(update={"evidence_ids": tuple(dict.fromkeys((*updated.evidence_ids, answer_ev)))})
        agent.apply_payment(rec, updated)
        agent.carry_to_documents(rec, by_owner=True)
        learned = None
        if remember and needs.question is not None:
            proposal = recharge_rule(needs.question, yes, answered_by=OWNER_ACTOR, answered_at=now)
            if proposal is not None:
                rule = proposal.rule.model_copy(update={"id": "rule_" + hashlib.sha256(
                    f"{needs.id}:{option_id}".encode()).hexdigest()[:12]})
                repo.rulebook.add(rule, reason="one-tap answer")
                learned = proposal.label
                self.activity(now, "learned", f"Learned: {learned[0].lower()}{learned[1:]}. I will not ask again.",
                              None, evidence_ids=[answer_ev])
                agent.log("learn_rule", subject_id=rule.id, evidence_ids=[answer_ev], actor=OWNER_ACTOR,
                          values={"label": proposal.label})
        self.activity(now, "answered", f"You told me whether {center.label} pays back the {who} payment.",
                      rec.company_id, amount=abs(rec.tx.amount), currency=rec.tx.currency, evidence_ids=[answer_ev])
        message = (f"Done. The {who} payment is on {center.label}, to recharge to them." if yes else
                   f"Done. The {who} payment is your own cost.")
        return AnswerOutcome(ok=True, message=message, learned=learned)

    def _answer_approval(self, needs: NeedsYouRecord, option_id: str, answer_ev: str, now: datetime) -> AnswerOutcome:
        repo = self.repo
        record = repo.documents[needs.subject_id]
        item = repo.items[record.item_id]
        who = display_name(record.document.supplier_name)
        if option_id == "keep_blocked":
            needs.status = "answered"
            needs.answer = option_id
            needs.answered_at = now
            if record.late_bank_hold and self._resumes_closed(record):
                # Already paid and proven before the new bank details appeared: the invoice stays closed, the new
                # account stays blocked (the hold remains: nothing is ever paid into it).
                self.advance(item, Stage.CLOSED, [*record.evidence_ids, answer_ev], agent="fraud",
                             actor=f"{OWNER_ACTOR}:{repo.owner.email}", quality=Quality.GREEN,
                             note="Kept blocked by the owner. The invoice was already paid; its new bank details "
                                  "are not used.")
            else:
                self.advance(item, Stage.CONFLICT, [*record.evidence_ids, answer_ev], agent="fraud",
                             actor=f"{OWNER_ACTOR}:{repo.owner.email}", note="Kept blocked by the owner.")
            self.activity(now, "protected", f"Kept the {who} payment blocked.", needs.company_id,
                          evidence_ids=[answer_ev])
            request, reason = self._request_correction(record, needs.company_id, answer_ev, now)
            if request is None:
                return AnswerOutcome(ok=True, message=f"Done. It stays blocked. {reason}")
            self.deliver(now)
            if request.sent:
                return AnswerOutcome(ok=True, message=f"Done. It stays blocked. I asked {who} for a corrected invoice "
                                                      f"at {request.to}.")
            return AnswerOutcome(ok=True, message=f"Done. It stays blocked. I wrote to {who} at {request.to} asking "
                                                  "for a corrected invoice. It is waiting to be sent.")
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
        if record.late_bank_hold and self._resumes_closed(record):
            # Paid and proven before its new bank details appeared: it resumes closed.
            record.late_bank_hold = False
            self.advance(item, Stage.CLOSED, [*record.evidence_ids, answer_ev], agent="fraud",
                         actor=f"{OWNER_ACTOR}:{repo.owner.email}", quality=Quality.GREEN,
                         note="New bank details confirmed by the owner by phone. The invoice was already paid.")
            self.activity(now, "protected", f"You confirmed {who}'s new bank details by phone. This invoice was "
                          "already paid.", needs.company_id, evidence_ids=[answer_ev])
            return AnswerOutcome(ok=True, message=f"Done. {who}'s new account is confirmed. This invoice was already "
                                                  "paid.")
        record.late_bank_hold = False
        self.advance(item, Stage.UNDERSTOOD, [*record.evidence_ids, answer_ev], agent="fraud",
                     actor=f"{OWNER_ACTOR}:{repo.owner.email}", note="New bank details confirmed by the owner by phone.")
        if self.addressed_elsewhere(record) is not None:
            # The call verified the account, not who the invoice is for: it is never counted or paid for you (F6).
            self.activity(now, "protected", f"You confirmed {who}'s new bank details by phone. The invoice is "
                          "addressed to another company, so it stays open until a corrected one arrives.",
                          needs.company_id, evidence_ids=[answer_ev])
            return AnswerOutcome(ok=True, message="Done. The account is confirmed. The invoice is addressed to another "
                                                  "company, so I won't count or pay it until a corrected one arrives.")
        self.activity(now, "protected", f"You confirmed {who}'s new bank details by phone. The payment can go ahead.",
                      needs.company_id, evidence_ids=[answer_ev])
        return AnswerOutcome(ok=True, message="Done. The payment will go to the new account.")

    def _resumes_closed(self, record: DocumentRecord) -> bool:
        """An invoice that was closed on its evidence before new bank details put it on hold, and still checks out."""
        item = self.repo.items[record.item_id]
        return record.document.quality is Quality.GREEN and any(t.to_stage is Stage.CLOSED for t in item.history)

    def _request_correction(self, record: DocumentRecord, company_id: str, answer_ev: str,
                            now: datetime) -> tuple[OutgoingMessage | None, str]:
        """The owner kept an invoice blocked: ask the supplier for a corrected one (§26).

        Written to the supplier's address on file, never to the sender of the held invoice. The owner's tap
        is the approval (it is not routine). Returns the email, or None and why none was written.
        """
        repo = self.repo
        who = display_name(record.document.supplier_name)
        supplier = repo.suppliers.get(record.supplier_id or "")
        company = repo.companies.get(company_id)
        doc = record.document
        if supplier is None or not supplier.contact_email:
            return None, f"I don't have an email address for {who}, so please ask them for a corrected invoice yourself."
        if company is None or doc.gross_amount is None or doc.gross_amount == 0:
            return None, f"I can't write to {who} about this invoice, so please ask them for a corrected one yourself."
        approval = Approval(tenant_id=repo.tenant_id, action=ActionKind.SUPPLIER_INVOICE_REQUEST, subject_id=record.id,
                            level=Requirement.OWNER, approved_by=f"{OWNER_ACTOR}:{repo.owner.email}", approved_at=now,
                            entity_id=company.id)
        decision = authorize(ActionKind.SUPPLIER_INVOICE_REQUEST, repo.policy, ActionContext(
            tenant_id=repo.tenant_id, entity_id=company.id, subject_id=record.id, unusual=True, approval=approval))
        self.log("fraud", "authorize_correction_request", subject_id=record.id, evidence_ids=[answer_ev],
                 actor=OWNER_ACTOR, response={"allowed": decision.allowed_now, "reason": decision.reason_plain})
        if not decision.allowed_now:
            return None, f"Please ask {who} for a corrected invoice yourself."
        try:
            facts = ChaseFacts(
                supplier_name=display_name(supplier.name), supplier_email=supplier.contact_email,
                amount=abs(doc.gross_amount), currency=doc.currency, paid_on=doc.issue_date or now.astimezone(TZ).date(),
                company_name=company.name.strip(), company_tax_id=company.tax_id, company_country=company.country,
                invoice_number=clean_invoice_number(doc.invoice_number),
                language=choose_language(supplier, supplier.contact_email))
            message = compose_correction_request(facts, token=thread_token(repo.tenant_id, record.id),
                                                 today=now.astimezone(TZ).date(), message_id_domain=MESSAGE_ID_DOMAIN)
        except ValueError:
            return None, f"I can't write to {who} about this invoice, so please ask them for a corrected one yourself."
        out = self.write_email("correction_request", record.id, company.id, message.to, message.subject, message.body,
                               now, headers=(("Message-ID", message.message_id),))
        return out, ""

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
        settlement = repo.settlements.get(record.id)
        if settlement is not None and not option.values:
            # A payout report that does not add up, or does not match the bank: set aside, never used.
            settlement.status = "set_aside"
            label = settlement.report.provider.label
            self.settlement.log("set_aside", subject_id=record.id, evidence_ids=[*record.evidence_ids, answer_ev],
                                actor=OWNER_ACTOR, response={"reason": "the owner is getting a corrected report"})
            self.activity(now, "answered", f"You set the payout report from {label} aside until a corrected one "
                          "arrives.", needs.company_id, evidence_ids=[answer_ev])
            return AnswerOutcome(ok=True, message=f"Done. I set it aside. When {label} sends a corrected report, "
                                                  "I will read it.")
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
        iban_before = normalize_iban(record.document.iban) if record.document.iban else None
        assessment = self.verification.assess(record.observations, record.document.doc_type, subject_id=record.id,
                                              evidence_ids=[*record.evidence_ids, answer_ev],
                                              owner=record.owner_values, issuer=record.issuer, country=record.country)
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
        chosen = f"Done. I will use {option.label.split(', as ')[0]} for the {who} invoice."
        iban_after = normalize_iban(record.document.iban) if record.document.iban else None
        if iban_after is not None and iban_after != iban_before and \
                self._check_new_bank_details(record, now, [answer_ev]):
            # Choosing which copy is right is not verifying a new beneficiary: that is a call to a number on file.
            return AnswerOutcome(ok=True, message=f"{chosen} Its bank account is one you have not paid before, so "
                                                  "the payment is on hold until you confirm it by phone.")
        return AnswerOutcome(ok=True, message=chosen)

    def _answer_company(self, needs: NeedsYouRecord, option_id: str, answer_ev: str, now: datetime) -> AnswerOutcome:
        """The owner said which of their companies carries a payment whose invoice names another (§51).

        The answer is evidence. When the company that carries it is not the one whose account paid,
        it is recorded as an inter-company payment for the accountant.
        """
        repo = self.repo
        option = next((o for o in needs.options if o.id == option_id), None)
        if option is None:
            raise ValueError("not one of the options")
        rec = repo.transactions[needs.subject_id]
        item = repo.items[rec.item_id]
        chosen, named, payer = option.values["company"], option.values["named"], option.values["payer"]
        needs.status = "answered"
        needs.answer = option.id
        needs.answered_at = now
        rec.tx = rec.tx.model_copy(update={"entity_id": chosen})
        rec.company_answer_ev = answer_ev
        rec.company_note = (payer, chosen, named)
        for d in rec.document_ids:
            doc = repo.documents[d]
            doc.document = doc.document.model_copy(update={"entity_id": chosen})
        who = self.merchant_name(rec.tx)
        chosen_name = repo.company_name(chosen) or "that company"
        payer_name = repo.company_name(payer) or "the other company"
        if chosen != payer:
            detail = f" {payer_name} paid it on {chosen_name}'s behalf."
        elif chosen != named:
            detail = f" It stays with {chosen_name} although the invoice names {repo.company_name(named)}."
        else:
            detail = ""
        self.log("entity", "company_answer", subject_id=rec.id, evidence_ids=[rec.evidence_id, answer_ev],
                 actor=OWNER_ACTOR, values={"carried_by": chosen, "paid_by": payer, "invoice_names": named})
        self.advance(item, Stage.UNDERSTOOD, [rec.evidence_id, answer_ev], agent="entity",
                     actor=f"{OWNER_ACTOR}:{repo.owner.email}", note=f"{chosen_name} carries it.{detail}")
        self.activity(now, "answered", f"You said {chosen_name} carries the {who} invoice.{detail}", chosen,
                      amount=abs(rec.tx.amount), currency=rec.tx.currency, evidence_ids=[answer_ev])
        return AnswerOutcome(ok=True, message=f"Done. {chosen_name} carries it.{detail}")

    def _answer_cash(self, needs: NeedsYouRecord, option_id: str, answer_ev: str, now: datetime) -> AnswerOutcome:
        """The owner confirmed a cash receipt (which company; the reading is right) or set it aside (§11, §37)."""
        repo = self.repo
        option = next((o for o in needs.options if o.id == option_id), None)
        if option is None:
            raise ValueError("not one of the options")
        record = repo.documents[needs.subject_id]
        item = repo.items[record.item_id]
        owner = f"{OWNER_ACTOR}:{repo.owner.email}"
        who = display_name(record.document.supplier_name)
        needs.status = "answered"
        needs.answer = option.id
        needs.answered_at = now
        if option.id in ("personal", "wrong"):
            personal = option.id == "personal"
            note = "Personal, as you said." if personal else "Set aside: you said the details are wrong."
            self.advance(item, Stage.NOT_REQUIRED, [*record.evidence_ids, answer_ev], agent="closure", actor=owner,
                         quality=Quality.GREEN, note=note)
            self.activity(now, "answered", f"You set the {who} cash receipt aside." if not personal else
                          f"You said the {who} cash purchase is personal.", None, evidence_ids=[answer_ev])
            message = ("Done. It is set aside as personal." if personal else
                       "Done. I set it aside. Send a clearer photo and I will read it again.")
            return AnswerOutcome(ok=True, message=message)
        company = option.values["company"]
        for name, value in option.values.items():
            if name == "company":
                continue
            record.owner_values[name] = FieldObservation(
                value=value, source=answer_ev, method=ExtractionMethod.HUMAN, confidence=1.0,
                location="owner answer: " + option.label)
        if record.owner_values:
            assessment = self.verification.assess(record.observations, record.document.doc_type, subject_id=record.id,
                                                  evidence_ids=[*record.evidence_ids, answer_ev],
                                                  owner=record.owner_values, issuer=record.issuer, country=record.country)
            self._apply_assessment(record, assessment)
        record.document = record.document.model_copy(update={"entity_id": company})
        record.owner_confirmed = answer_ev
        name = repo.company_name(company) or "that company"
        self.advance(item, Stage.UNDERSTOOD, [*record.evidence_ids, answer_ev], agent="closure", actor=owner,
                     note=f"A cash purchase for {name}, as you said.")
        self.activity(now, "answered", f"You said the {who} cash purchase is for {name}.", company,
                      amount=record.document.gross_amount, currency=record.document.currency, evidence_ids=[answer_ev])
        return AnswerOutcome(ok=True, message=f"Done. I counted it as a cash purchase for {name}.")

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

    def rule_author(self, company_id: str | None) -> AccountantProfile:
        """Whose rule this is: the company's accountant, else the business's (§28).

        Raises PermissionError when no accountant is connected, and ValueError when
        companies have different accountants and none was named.
        """
        repo = self.repo
        if company_id is not None:
            if company_id not in repo.companies:
                raise ValueError("I don't know that client.")
            author = repo.accountant_for(company_id)
            if author is None:
                raise PermissionError("no accountant is connected")
            return author
        if repo.accountant is not None:
            return repo.accountant
        found = repo.accountants()
        if not found:
            raise PermissionError("no accountant is connected")
        if len(found) > 1:
            raise ValueError("Choose which client this rule is for.")
        return found[0]

    def accountant_rule(self, text: str, scope: str = "client", company_id: str | None = None) -> tuple[Rule, int]:
        """An accountant's rule (§28). With ``company_id`` and the "client" scope it covers that company
        only; "all" covers every company this accountant looks after (and their other clients)."""
        repo = self.repo
        author = self.rule_author(company_id)
        for pattern, need in _EXPECTATION_RULES:
            wanted = pattern.match(text)
            if wanted is not None:
                return self._accountant_expectation_rule(text, wanted.group("who").strip(), need, scope, company_id,
                                                         author)
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
        entity_ids = (company_id,) if company_id is not None and not all_clients else ()
        now = repo.clock.now()
        seed = f"{text}|{scope}" if company_id is None else f"{text}|{scope}|{company_id}|{author.id}"
        rule = Rule(
            id="rule_" + hashlib.sha256(seed.encode()).hexdigest()[:12],
            author=RuleAuthor.ACCOUNTANT, author_id=author.id, scope=rule_scope,
            tenant_id=None if all_clients else repo.tenant_id, entity_ids=entity_ids,
            match=RuleMatch(counterparty_key=supplier.id if supplier else key),
            outcome=RuleOutcome(category=what[:1].upper() + what[1:]), created_at=now,
            label=f"Treat all {display_name(supplier.name) if supplier else who} costs as {what}",
        )
        repo.rulebook.add(rule, reason="accountant rule")
        affected = 0
        resolver = repo.resolver()
        for rec in repo.transactions.values():
            accountant_ids = repo.accountant_ids_for(rec.company_id)
            if not rule.covers(rec.company_id) or author.id not in accountant_ids:
                continue  # another company's payment: this rule is not used there
            subject = RuleSubject.from_transaction(rec.tx, key=resolver.resolve_transaction(rec.tx).key,
                                                   entity_id=rec.company_id)
            decision = repo.rulebook.evaluate(subject, tenant_id=repo.tenant_id, accountant_ids=accountant_ids)
            if decision.get(RuleField.CATEGORY) is not None and decision.category == rule.outcome.category:
                affected += 1
        self.log("accountant", "accountant_rule", subject_id=rule.id, actor=f"accountant:{author.id}",
                 values={"text": text, "scope": rule_scope.value, "companies": list(entity_ids)},
                 response={"affected": affected})
        self.activity(now, "learned", f"Your accountant taught me: {rule.label}.", company_id)
        return rule, affected

    def _accountant_expectation_rule(self, text: str, who: str, need: str, scope: str, company_id: str | None,
                                     author: AccountantProfile) -> tuple[Rule, int]:
        """"Vodafone never has an invoice" / "Adobe always needs an invoice": what evidence that counterparty's
        payments need, learned for every payment from now on (§21, J7). Kept as the accountant's rule too, so
        their rules list shows it."""
        from backoffice.learning import Expectation

        repo = self.repo
        key = counterparty_key(who)
        supplier = repo.resolver().resolve(who).supplier
        if key is None and supplier is None:
            raise ValueError("I could not tell which supplier the rule is about.")
        all_clients = scope in ("all", "all_clients", "all_clients_of_accountant")
        limited = company_id if company_id is not None and not all_clients else None
        now = repo.clock.now()
        name = display_name(supplier.name) if supplier else display_name(who)
        label = f"{name} needs no invoice" if need == "none" else f"{name} always needs an invoice"
        body = json.dumps({"kind": "accountant_rule", "text": text, "accountant": author.id, "company": limited,
                           "at": now.isoformat()}, sort_keys=True).encode()
        evidence_id = repo.registry.register(body, tenant_id=repo.tenant_id, source_kind=SourceKind.ACCOUNTANT,
                                             format=EvidenceFormat.JSON, mime_type="application/json",
                                             retrieved_at=now, metadata={"kind": "accountant_rule"}).evidence.id
        reason = (f"Your accountant said {name} needs no invoice. The bank record is enough." if need == "none" else
                  f"Your accountant said {name} always needs an invoice.")
        expectation = EvidenceExpectation.BANK_EVIDENCE_SUFFICES if need == "none" else EvidenceExpectation.INVOICE
        learned = LearnedExpectation(expectation, reason, evidence_id=evidence_id, taught_by="accountant")
        store = repo.expectation_overrides if limited is None else repo.company_expectation_overrides.setdefault(
            limited, InMemoryExpectationOverrides())
        if supplier is not None:
            store.remember_supplier(supplier.id, learned)
        try:
            store.remember_descriptor(who, learned)
        except ValueError:
            pass
        seed = f"{text}|{scope}" if company_id is None else f"{text}|{scope}|{company_id}|{author.id}"
        rule_scope = RuleScope.ALL_CLIENTS_OF_ACCOUNTANT if all_clients else RuleScope.CLIENT
        rule = Rule(
            id="rule_" + hashlib.sha256(seed.encode()).hexdigest()[:12],
            author=RuleAuthor.ACCOUNTANT, author_id=author.id, scope=rule_scope,
            tenant_id=None if all_clients else repo.tenant_id, entity_ids=(limited,) if limited else (),
            match=RuleMatch(counterparty_key=supplier.id if supplier else key),
            outcome=RuleOutcome(expectation=Expectation.NONE if need == "none" else Expectation.INVOICE),
            created_at=now, label=label,
        )
        repo.rulebook.add(rule, reason="accountant rule")
        key_of = self.payment_keys()
        wanted = supplier.id if supplier is not None else key_of(who)

        def covered(r: TxRecord) -> bool:
            return key_of(r.tx.counterparty) == wanted and (limited is None or r.company_id == limited)

        affected = sum(1 for r in repo.transactions.values() if covered(r))
        changed = self.redecide(covered)
        self.log("accountant", "accountant_rule", subject_id=rule.id, evidence_ids=[evidence_id],
                 actor=f"accountant:{author.id}",
                 values={"text": text, "scope": rule_scope.value, "companies": [limited] if limited else [],
                         "need": need}, response={"affected": affected, "changed": changed})
        self.activity(now, "learned", f"Your accountant taught me: {label}.", company_id, evidence_ids=[evidence_id])
        self.run(now)
        return rule, affected

    def accountant_rules(self, company_id: str) -> list[Rule]:
        """Active accountant rules used for ``company_id``'s payments (its accountant's, for it)."""
        repo = self.repo
        ids = set(repo.accountant_ids_for(company_id))
        return [r for r in repo.rulebook.applicable(repo.tenant_id, ids)
                if r.author is RuleAuthor.ACCOUNTANT and r.covers(company_id)]

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


def _company_packs() -> tuple[CompanyPack, ...]:
    """Every pack a company can run on (Portugal, Spain): their fiscal QR codes are recognised on any document,
    so a code from another country names the issuer's country and is never read as text."""
    return tuple(company_pack(c) for c in company_countries())


def _split_qr(text: str) -> tuple[list[tuple[str, str]], str]:
    """Fiscal QR payloads found in a text (decoded QR codes) with their country, and the text without them."""
    packs = _company_packs()
    payloads: list[tuple[str, str]] = []
    rest: list[str] = []
    for line in (text or "").splitlines():
        found = next(((p.country_code, payload) for p in packs if (payload := p.find_fiscal_qr(line))), None)
        if found is not None:
            payloads.append(found)
        else:
            rest.append(line)
    return payloads, "\n".join(rest)


def _fiscal_qr(country: str, payload: str, evidence_id: str) -> FiscalQRResult | None:
    """A fiscal QR payload read by its own country's pack; None when it cannot be read (never guessed, §19)."""
    try:
        return company_pack(country).parse_fiscal_qr(payload, evidence_id)
    except (FiscalQRError, CountryPackError):
        return None


def _names_own_tax_id(text: str, tax_id: str) -> bool:
    """'NIF: 516 123 459', 'PT516123459', 'CIF: B-1234567-4', 'ESB12345674' all name that tax number."""
    compact = re.sub(r"[^0-9A-Za-z]", "", tax_id or "").upper()
    if len(compact) < 8:
        return False
    body = r"[ .\-]?".join(re.escape(c) for c in compact)
    return re.search(rf"(?<![0-9A-Za-z.]){body}(?![0-9A-Za-z]|[.]\d)", text or "", re.I) is not None or \
        re.search(rf"(?<![0-9A-Za-z])[A-Z]{{2}}[ \-]?{body}(?![0-9A-Za-z]|[.]\d)", text or "", re.I) is not None


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


# What a document calls itself, on folded (lower-case, accent-free) text; the most specific name first,
# so "Fatura pró-forma" is a pro-forma and "Fatura-recibo" an invoice-receipt, not an invoice (§50).
_FATURA = r"(?:fatura|factura)"
_KIND_NAMES: tuple[tuple[DocumentType, str], ...] = (
    (DocumentType.PRO_FORMA, rf"{_FATURA}\s*[-:]?\s*pro[\s-]*forma|pro[\s-]*forma(?:\s+invoice)?"),
    (DocumentType.QUOTE, r"orcamento|quotation|quote"),
    (DocumentType.DELIVERY_NOTE, r"guia\s+de\s+(?:remessa|transporte)|delivery\s+note|transport\s+document"),
    (DocumentType.ORDER_CONFIRMATION,
     r"confirmacao\s+(?:de|da)\s+encomenda|nota\s+de\s+encomenda|order\s+confirmation|purchase\s+order"),
    (DocumentType.SUPPLIER_STATEMENT,
     r"extrato\s+(?:de\s+)?conta[\s-]+corrente|statement\s+of\s+account|supplier\s+statement"),
    (DocumentType.CREDIT_NOTE, r"nota\s+de\s+credito|credit\s+note"),
    (DocumentType.DEBIT_NOTE, r"nota\s+de\s+debito|debit\s+note"),
    (DocumentType.INVOICE_RECEIPT, rf"{_FATURA}[\s-]+recibo|invoice[\s-]+receipt"),
    (DocumentType.SIMPLIFIED_INVOICE, rf"{_FATURA}\s+simplificada|simplified\s+invoice"),
    (DocumentType.INVOICE, rf"{_FATURA}|(?:tax\s+)?invoice"),
    (DocumentType.RECEIPT, r"recibo|receipt|talao(?:\s+de\s+venda)?"),
)
_KIND_TITLES = tuple((kind, re.compile(rf"(?:{words})(?![a-z])")) for kind, words in _KIND_NAMES)
# Without a title line, only names that invoices do not use when they merely refer to another document
# ("conforme orçamento", "V/ guia de remessa", "purchase order: PO-12" are common on real invoices).
_KIND_ANYWHERE = tuple(
    (kind, re.compile(rf"(?<![a-z])(?:{words})(?![a-z])")) for kind, words in (
        (DocumentType.PRO_FORMA, rf"{_FATURA}\s*[-:]?\s*pro[\s-]*forma|pro[\s-]*forma\s+invoice|proforma\s+invoice"),
        (DocumentType.SUPPLIER_STATEMENT, r"extrato\s+(?:de\s+)?conta[\s-]+corrente|statement\s+of\s+account"),
        (DocumentType.CREDIT_NOTE, r"nota\s+de\s+credito|credit\s+note"),
        (DocumentType.DEBIT_NOTE, r"nota\s+de\s+debito|debit\s+note"),
        (DocumentType.INVOICE_RECEIPT, rf"{_FATURA}[\s-]+recibo"),
        (DocumentType.SIMPLIFIED_INVOICE, rf"{_FATURA}\s+simplificada"),
        (DocumentType.INVOICE, rf"{_FATURA}|invoice"),
    )
)


# Spanish names of a credit note ("Factura rectificativa" would otherwise read as an invoice): checked first
# on a document from abroad (checklist P7).
_FOREIGN_CREDIT_NOTE = re.compile(r"(?<![a-z])(?:factura\s+rectificativa|nota\s+de\s+abono)(?![a-z])")


def _doc_kind(text: str, pack: CompanyPack, *, foreign: bool = False) -> DocumentType | None:
    """The kind a document's own words give it: a domestic one in its country's own names first
    ("Factura rectificativa"), then the core's Portuguese and English names."""
    if not foreign:
        named = pack.document_kind(text)
        if named is not None:
            return named
    return _text_doc_type(text, foreign=foreign)


def _text_doc_type(text: str, *, foreign: bool = False) -> DocumentType | None:
    """The kind a document's own title gives it, or None when its text never names one.

    The title is the first line that starts with a document name ("Fatura n.º FT 2026/183",
    "Guia de remessa GR 2026/33", "Orçamento"). Without one, only names that are not
    mere references to other documents count. A document from abroad (``foreign``) is
    also a credit note when it uses the Spanish names for one.
    """
    from backoffice.learning import fold

    if foreign and _FOREIGN_CREDIT_NOTE.search(fold(text or "")):
        return DocumentType.CREDIT_NOTE
    for raw in (text or "").splitlines():
        line = fold(raw)
        for kind, pattern in _KIND_TITLES:
            if pattern.match(line):
                return kind
    folded = fold(text or "")
    for kind, pattern in _KIND_ANYWHERE:
        if pattern.search(folded):
            return kind
    return None


# The invoice a credit note corrects: "referente à fatura FT 2026/183", "Ref. FT A/123",
# "Documento de origem: FT 2026/183", "Invoice reference: FT 2026/183".
_INVOICE_REFERENCE = re.compile(
    r"(?<![a-z])(?:referente|relativ[ao]|respeitante|correspondente|retifica|rectifica|anula|corrige)"
    r"\s+(?:(?:[àa]o?|da|do|de)\s+)?"
    r"(?:(?:fatura|factura|invoice)(?:[\s-]+recibo|\s+simplificada)?\s+)?(?:(?:n\.?\s*[ºo°]\.?|nr\.?|no\.)\s*)?"
    r"(?P<a>(?:[a-z]{1,4}\s+)?[^\s/,;:()]{1,40}/\d{1,12})(?![\d/])"
    r"|(?<![a-z])(?:ref(?:er[êe]ncia|\.)?|documento\s+de\s+origem|doc\.?\s+(?:de\s+)?origem|invoice\s+reference"
    r"|original\s+invoice)\s*[:.]?\s*(?:(?:fatura|factura|invoice)\s+)?(?:(?:n\.?\s*[ºo°]\.?|nr\.?|no\.)\s*)?"
    r"(?P<b>(?:FT|FR|FS|ND|VD)\s+[^\s/,;:()]{1,40}/\d{1,12})(?![\d/])",
    re.IGNORECASE,
)


def _referenced_invoice(text: str) -> str | None:
    """The invoice number a credit note says it corrects, as printed; None when it names none."""
    for m in _INVOICE_REFERENCE.finditer(text or ""):
        found = m.group("a") or m.group("b")
        if found and any(ch.isdigit() for ch in found):
            return " ".join(found.split())
    return None


# "Pago em numerário", "Dinheiro 20,00", "Paid in cash" — unless the same document also names
# a card or bank payment (then it does not say cash alone, §19). "Cash & Carry" is a shop name.
_CASH_WORDS = re.compile(
    r"(?<![a-z])(?:numerario|(?:pago|pagamento|paga)\s+em\s+dinheiro|dinheiro|paid\s+(?:in\s+)?cash"
    r"|cash\s+payment|payment\s*(?:method)?\s*:?\s*cash|cash)(?![a-z])")
_NOT_CASH_WORDS = re.compile(
    r"(?<![a-z])(?:cartao|card|multibanco|visa|mastercard|maestro|amex|american\s+express|mb\s*way|transferencia"
    r"|bank\s+transfer|debito\s+direto|direct\s+debit|paypal|apple\s+pay|google\s+pay|tpa)(?![a-z])")
_CASH_AND_CARRY = re.compile(r"cash\s*(?:&|and|e|n)\s*carry|cash\s*back|petty\s+cash")
_TEXT_KEPT = 20_000  # characters of a document's text kept for wording checks
# An accountant's rule about the evidence a counterparty's payments need (J7): "Vodafone never has an invoice".
_INVOICE_WORD = r"(?:an?\s+)?invoices?"
_EXPECTATION_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^\s*no\s+invoices?\s+(?:is\s+|are\s+)?(?:needed|required)\s+for\s+(?P<who>.+?)\s*\.?\s*$", re.I),
     "none"),
    (re.compile(r"^\s*(?P<who>.+?)\s+(?:payments?\s+|subscriptions?\s+|costs?\s+)?(?:never\s+(?:has|have|needs?|sends?|"
                r"comes?\s+with)\s+" + _INVOICE_WORD + r"|(?:does|do)\s+not\s+need\s+" + _INVOICE_WORD +
                r"|(?:doesn't|don't)\s+need\s+" + _INVOICE_WORD + r"|needs?\s+no\s+invoices?)\s*\.?\s*$", re.I), "none"),
    (re.compile(r"^\s*(?:always\s+)?(?:require|ask\s+for)\s+" + _INVOICE_WORD + r"\s+for\s+(?P<who>.+?)\s*\.?\s*$",
                re.I), "invoice"),
    (re.compile(r"^\s*(?P<who>.+?)\s+(?:payments?\s+|subscriptions?\s+|costs?\s+)?always\s+(?:has|have|needs?|"
                r"requires?|sends?)\s+" + _INVOICE_WORD + r"\s*\.?\s*$", re.I), "invoice"),
)


def _says_paid_in_cash(text: str) -> bool:
    from backoffice.learning import fold

    folded = _CASH_AND_CARRY.sub(" ", fold(text or ""))
    return bool(_CASH_WORDS.search(folded)) and not _NOT_CASH_WORDS.search(folded)


_TITLE_WORDS = ("nif", "fatura", "invoice", "data", "atcud")
_FOREIGN_TITLE_WORDS = (*_TITLE_WORDS, "tax invoice", "factura", "receipt", "recibo", "bill to", "page", "vat ")


def _first_line(text: str, *, foreign: bool = False, pack: CompanyPack | None = None) -> str | None:
    """The first line that is not a title or a label: the supplier's name. A domestic document skips its
    own country's title words (``pack``), one from abroad the international ones."""
    words = _FOREIGN_TITLE_WORDS if foreign else (tuple(pack.title_words) if pack is not None else _TITLE_WORDS)
    for line in (text or "").splitlines():
        line = line.strip()
        if line and not line.lower().startswith(words):
            return line
    return None


def _foreign_rules(issuer: IssuerProfile, on: date) -> ForeignRules:
    """What the issuer's country allows on ``on``, for :func:`backoffice.verification.assess_foreign`."""
    country = issuer.country or ""
    name = COUNTRY_NAMES.get(country, country)
    number = issuer.tax_number
    valid = number is not None and number.valid and number.country == country
    if number is not None and number.kind == "ein":
        check = "A valid US tax number: it has the right format."
    elif number is not None and number.checksum:
        check = f"A valid VAT number from {name}: its check digits are right."
    else:
        check = f"A valid VAT number from {name}: it has the right format."
    return ForeignRules(
        country=country, country_name=name, rates=foreign_vat_rates(country, on), zero_vat=issuer.zero_vat_reason,
        vat_number=number.printed if valid and number is not None else None, vat_number_check=check,
        vat_number_required=issuer.needs_vat_number, stated_rates=issuer.stated_rates,
    )


_READABLE_FILES = frozenset({EvidenceFormat.PDF, EvidenceFormat.IMAGE, EvidenceFormat.SCREENSHOT})
# A statement's own fields are its supplier, its customer and its date: the numbers and amounts printed on it
# belong to the documents on its lines (a statement is never a copy of one of them, never an amount to pay).
_NOT_A_STATEMENT_FIELD = ("invoice_number", "gross_amount", "net_amount", "vat_amount", "due_date",
                          "payment_reference", "iban")


def _statement_issuer(text: str) -> str | None:
    """The first line of a statement that is not its title, a header row or a number (the supplier's name)."""
    from backoffice.learning import fold

    for raw in (text or "").splitlines():
        cells = [c.strip() for c in re.split(r"[;,\t|]", raw) if c.strip()]
        line = " ".join(cells)
        folded = fold(line)
        if not line or re.match(r"(?:nif|nipc|vat|data|date|periodo|period|cliente|customer)\b", folded):
            continue
        name = re.sub(r"(?i)\b(?:extrato\s+(?:de\s+)?conta[\s-]+corrente|statement\s+of\s+account|account\s+"
                      r"statement|supplier\s+statement)\b[\s:-]*", "", line).strip(" -:")
        name = re.split(r"(?i)\s+(?:NIF|NIPC|VAT)\b", name)[0].strip(" -:,")
        if name and re.search(r"[A-Za-z]", name) and not re.search(r"\d{2}[/.-]\d{2}", name):
            return name[:120]
    return None
# The field a "which value is right?" question leads with, most useful first.
_CONFLICT_ORDER = ("gross_amount", "vat_amount", "net_amount", "supplier_tax_id", "invoice_number", "issue_date",
                   "iban", "currency", "customer_tax_id", "due_date", "payment_reference")
_OPINION_FLOOR = 0.4  # below this confidence a reading neither supports nor contradicts (verification policy)
_AS_PRINTED = frozenset({"invoice_number", "supplier_tax_id", "customer_tax_id", "payment_reference"})


def _invoice_lines(parts: Sequence[_Part]) -> tuple[Any, ...]:
    """The lines of the first structured e-invoice among ``parts`` (empty for text and scans)."""
    for part in parts:
        if part.kind == "ubl" and part.data:
            details = read_invoice_details(part.data)
            if details is not None and details.lines:
                return details.lines
    return ()


def _qr_vat_parts(text: str) -> tuple[VatPart, ...]:
    """A fiscal QR's amounts by VAT rate, as its country's pack reads them (rates in percent; exempt at 0%,
    non-taxable without a rate). Empty when the code gives no breakdown (Spain's give only the total)."""
    if not text:
        return ()
    payloads, _ = _split_qr(text)
    for country, payload in payloads:
        found = _fiscal_qr(country, payload, "")
        if found is None:
            continue
        return tuple(found.vat_parts)
    return ()


def _decimal_value(value: Any, what: str) -> Decimal:
    if isinstance(value, bool):
        raise SplitError(f"That {what} is not a number.")
    try:
        number = Decimal(repr(value)) if isinstance(value, float) else Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise SplitError(f"That {what} is not a number.") from None
    if not number.is_finite():
        raise SplitError(f"That {what} is not a number.")
    return number


def _plural_noun(noun: str) -> str:
    from backoffice.learning.cost_centers import plural

    if noun == "one":
        return "jobs, properties and the like"
    return " or ".join(plural(w) for w in noun.split(" or "))


def _split_entries(split: Any, centers: Mapping[str, CostCenter], noun: str
                   ) -> list[tuple[str, Decimal | None, Decimal | None, Decimal | None]]:
    """The owner's split as (cost center id, amount, percent, VAT rate); checked for shape, not yet for totals."""
    if not isinstance(split, (list, tuple)) or len(split) < 2:
        raise SplitError("A split needs at least two parts.")
    if len(split) > 200:
        raise SplitError("That is too many parts for one split.")
    out: list[tuple[str, Decimal | None, Decimal | None, Decimal | None]] = []
    for entry in split:
        if not isinstance(entry, Mapping):
            raise SplitError("I couldn't read that split.")
        cid = entry.get("costCenterId") or entry.get("cost_center_id") or entry.get("id")
        if not isinstance(cid, str) or cid not in centers:
            raise SplitError(f"One of those isn't one of this company's {_plural_noun(noun)}.")
        amount, percent = entry.get("amount"), entry.get("percent")
        rate = entry.get("vatRate", entry.get("vat_rate"))
        out.append((cid,
                    to_cents(amount) if amount not in (None, "") else None,
                    _decimal_value(percent, "percentage") if percent not in (None, "") else None,
                    _decimal_value(rate, "VAT rate") if rate not in (None, "") else None))
    keys = [(cid, rate) for cid, _, _, rate in out]
    if len(set(keys)) != len(keys):
        raise SplitError("Each one can appear only once in a split.")
    return out


def _join_words(words: Sequence[str]) -> str:
    if len(words) <= 1:
        return "".join(words)
    return ", ".join(words[:-1]) + f" and {words[-1]}"


def _sender_line(sender: Any) -> str:
    """'Millennium BCP <avisos@millenniumbcp.pt>' (the name helps recognise who wrote a letter)."""
    if sender is None or not getattr(sender, "address", ""):
        return ""
    name = " ".join((getattr(sender, "name", "") or "").replace("<", " ").replace(">", " ").split())
    return f"{name} <{sender.address}>" if name else sender.address


def _letter_text(parts: Sequence[_Part]) -> str:
    """The readable text of a file, for spotting letters with a deadline (§24)."""
    for part in parts:
        if part.kind == "text":
            return part.text
        if part.kind == "read":
            own = part.text if part.method is not ExtractionMethod.QR else ""
            return own or part.reading_text
    return ""


# --------------------------------------------------------------------------- emails, bodies and links (§8, §9)


@dataclass(frozen=True)
class _EmailFacts:
    """What the agents need from one email: who sent it, its words (HTML turned into text when there is no
    plain body), who it went to, and the supplier its sender's domain belongs to."""

    message_id: str
    sender: str | None
    text: str  # subject and words, for letters and fraud wording checks
    body: _Part | None
    recipients: tuple[str, ...]
    supplier: Supplier | None
    parsed: Any


def _has_attachments(result: EmailIngestResult) -> bool:
    """A file sent with the email (an inline logo is not), or an email attached to it."""
    return bool(result.attached_emails) or any(
        not (f.inline and f.evidence.format in (EvidenceFormat.IMAGE, EvidenceFormat.SCREENSHOT))
        for f in result.files)


def _html_structured(html: bytes, source: str) -> tuple[Any, ...]:
    """schema.org Invoice / Order data in an HTML body or page (JSON-LD or microdata); () when there is none."""
    try:
        return tuple(r for r in extract_html_structured(html, source=source) if r.fields)
    except (StructuredDataError, ValueError, RecursionError):
        return ()


def _complete_invoice(extracted: _Extracted, *, structured: bool, bulk: bool, known_supplier: bool) -> bool:
    """An email body is a document only when it carries the whole invoice (B4): who issued it, its number,
    a date and its total. A body that only mentions an invoice ("attached", "available online") is not."""
    obs = extracted.observations

    def has(f: CriticalField) -> bool:
        return bool(obs.get(f.value))

    if extracted.statement is not None or extracted.doc_type in SUPPORTING_DOCUMENT_TYPES:
        return False
    if not (has(CriticalField.INVOICE_NUMBER) and has(CriticalField.GROSS_AMOUNT)
            and (has(CriticalField.ISSUE_DATE) or has(CriticalField.DUE_DATE))):
        return False
    fiscal = extracted.qr is not None
    if bulk and not (structured or fiscal):
        return False  # a newsletter quoting prices is not an invoice
    return fiscal or has(CriticalField.SUPPLIER_TAX_ID) or known_supplier or (
        structured and bool(extracted.supplier_name))


def _sender_name(parsed: Any) -> str | None:
    """'Vodafone Business' from 'Vodafone Business <faturacao@vodafone.pt>', else the domain's name."""
    from backoffice.evidence.domains import display_name_for_host

    sender = parsed.sender
    name = " ".join((getattr(sender, "name", "") or "").replace("<", " ").replace(">", " ").split())
    if name and "@" not in name:
        return name
    domain = parsed.sender_domain
    return display_name_for_host(domain) if domain else None


def _possessive(name: str) -> str:
    return f"{name}'" if name.endswith("s") else f"{name}'s"


def _shared_link_message(followed: Sequence[LinkRecord]) -> str | None:
    """The one thing the owner needs to hear about the links they shared, if anything (§12, §69)."""
    for status, reason in (("sign_in", LinkOutcome.MFA_REQUIRED.value), ("sign_in", None), ("blocked", None)):
        for link in followed:
            if link.status == status and (reason is None or link.reason == reason) and link.message:
                return link.message
    if any(link.status == "broken" for link in followed):
        return "Got it. This link no longer works. I'm looking for the invoice another way."
    return None


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


_NUMBER_NOISE = re.compile(r"[\s/_-]+")


@lru_cache(maxsize=65536)
def _number_key(number: str) -> str:
    """A document number without spaces, slashes, dashes or underscores, upper case (pure: cached)."""
    return _NUMBER_NOISE.sub("", number).upper()


def _same_number(a: str, b: str) -> bool:
    return _number_key(a) == _number_key(b)


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


# Fields a later copy of the same document may fill when the first copy left them empty (never the IBAN:
# bank details are checked by the fraud agent when a document is first read, never slipped in afterwards).
_GAP_FIELDS: tuple[tuple[str, Any], ...] = (
    ("supplier_tax_id", _text), ("issue_date", _date), ("due_date", _date),
    ("net_amount", _dec), ("vat_amount", _dec), ("gross_amount", _dec),
)


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


def _reply_to(message_id: str | None) -> str | None:
    """A received Message-ID as an In-Reply-To header value ('<id@host>'), or None if it is not one plain id."""
    bare = (message_id or "").strip().strip("<>")
    return f"<{bare}>" if re.fullmatch(r"[^<>\s@]{1,200}@[^<>\s@]{1,100}", bare) else None


def _amount_in(text: str) -> Decimal | None:
    """The first money amount written in a question (backoffice.accountant_questions.amounts_in)."""
    found = amounts_in(text)
    return found[0] if found else None


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
    """A bank export: date,amount,counterparty,description,account[,card,iban,reference,kind,cardholder]."""
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
            reference=row.get("reference") or None, cardholder=row.get("cardholder") or None,
        ))
    return rows


def local_datetime(day: date, hour: int = 9, minute: int = 0) -> datetime:
    """A time on ``day`` in the tenant's local time."""
    return datetime.combine(day, time(hour, minute), tzinfo=TZ)
