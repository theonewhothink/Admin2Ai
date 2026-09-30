"""Core data model.

The system starts from Evidence, not from Invoice:

    Source -> Evidence -> Document/Event -> Legal Entity
           -> Financial Transaction / Obligation -> Action -> Verification -> Closure
"""

from __future__ import annotations

import hashlib
import itertools
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

# Set only while an event-sourced tenant applies one event (backoffice.server):
# ids and "now" then come from the event itself, so replaying the event log
# rebuilds exactly the same state. Unset (the demo, tests, the browser) they
# are random ids and the wall clock, as before.
_ID_SOURCE: ContextVar[Callable[[str], str] | None] = ContextVar("backoffice_id_source", default=None)
_NOW_SOURCE: ContextVar[Callable[[], datetime] | None] = ContextVar("backoffice_now_source", default=None)


def new_id(prefix: str) -> str:
    source = _ID_SOURCE.get()
    if source is not None:
        return source(prefix)
    return f"{prefix}_{uuid4().hex[:16]}"


def utcnow() -> datetime:
    source = _NOW_SOURCE.get()
    if source is not None:
        return source().astimezone(timezone.utc)
    return datetime.now(timezone.utc)


@contextmanager
def deterministic(seed: str, now: Callable[[], datetime]) -> Iterator[None]:
    """Within this block, :func:`new_id` derives ids from ``seed`` and :func:`utcnow` reads ``now``.

    Ids are ``prefix_`` + 16 hex digits of SHA-256(seed, n) for the n-th id made
    in the block, so the same seed and the same sequence of calls give the same
    ids in any process.
    """
    counter = itertools.count()

    def make(prefix: str) -> str:
        digest = hashlib.sha256(f"{seed}\x1f{next(counter)}".encode()).hexdigest()
        return f"{prefix}_{digest[:16]}"

    id_token = _ID_SOURCE.set(make)
    now_token = _NOW_SOURCE.set(now)
    try:
        yield
    finally:
        _NOW_SOURCE.reset(now_token)
        _ID_SOURCE.reset(id_token)


class SourceKind(str, Enum):
    EMAIL = "email"
    BANK = "bank"
    CARD = "card"
    SUPPLIER_PORTAL = "supplier_portal"
    ACCOUNTING_SYSTEM = "accounting_system"
    CLOUD_STORAGE = "cloud_storage"
    UPLOAD = "upload"
    MOBILE_SCAN = "mobile_scan"
    MOBILE_SHARE = "mobile_share"
    GOVERNMENT = "government"
    ACCOUNTANT = "accountant"


class EvidenceFormat(str, Enum):
    PDF = "pdf"
    IMAGE = "image"
    SCREENSHOT = "screenshot"
    QR = "qr"
    EMAIL = "email"
    EML = "eml"
    HTML = "html"
    URL = "url"
    XML = "xml"
    UBL = "ubl"
    SAFT = "saft"
    JSON = "json"
    CSV = "csv"
    XLSX = "xlsx"
    ZIP = "zip"
    BANK_TRANSACTION = "bank_transaction"
    CARD_TRANSACTION = "card_transaction"
    GOVERNMENT_NOTICE = "government_notice"
    TEXT = "text"


class Quality(str, Enum):
    """Three quality levels. AMBER is never promoted to GREEN to improve statistics."""

    GREEN = "verified"
    AMBER = "likely"
    RED = "conflict"


class ExtractionMethod(str, Enum):
    STRUCTURED_XML = "structured_xml"
    EMBEDDED_TEXT = "embedded_text"
    QR = "qr"
    BARCODE = "barcode"
    API = "api"
    HTML_STRUCTURED = "html_structured"
    OCR = "ocr"
    VLM = "vlm"
    ARITHMETIC = "arithmetic"
    BANK = "bank"
    HUMAN = "human"


# Structured evidence outranks OCR.
METHOD_RANK: dict[ExtractionMethod, int] = {
    ExtractionMethod.HUMAN: 100,
    ExtractionMethod.STRUCTURED_XML: 90,
    ExtractionMethod.API: 90,
    ExtractionMethod.QR: 85,
    ExtractionMethod.BANK: 85,
    ExtractionMethod.EMBEDDED_TEXT: 80,
    ExtractionMethod.HTML_STRUCTURED: 75,
    ExtractionMethod.BARCODE: 75,
    ExtractionMethod.ARITHMETIC: 70,
    ExtractionMethod.VLM: 55,
    ExtractionMethod.OCR: 50,
}


class BoundingBox(BaseModel):
    page: int
    x0: float
    y0: float
    x1: float
    y1: float


class FieldObservation(BaseModel):
    """One observation of one field from one source."""

    value: Any
    source: str  # evidence id or engine name
    method: ExtractionMethod
    confidence: float = Field(ge=0.0, le=1.0)
    location: BoundingBox | str | None = None


class CriticalField(str, Enum):
    INVOICE_NUMBER = "invoice_number"
    SUPPLIER_TAX_ID = "supplier_tax_id"
    CUSTOMER_TAX_ID = "customer_tax_id"
    GROSS_AMOUNT = "gross_amount"
    NET_AMOUNT = "net_amount"
    VAT_AMOUNT = "vat_amount"
    CURRENCY = "currency"
    ISSUE_DATE = "issue_date"
    DUE_DATE = "due_date"
    IBAN = "iban"
    PAYMENT_REFERENCE = "payment_reference"


class VerifiedField(BaseModel):
    name: str
    value: Any
    quality: Quality
    observations: list[FieldObservation]
    reasons: list[str] = Field(default_factory=list)


class Evidence(BaseModel):
    """Immutable original. Never overwritten; derived data lives elsewhere."""

    id: str = Field(default_factory=lambda: new_id("ev"))
    tenant_id: str
    source_kind: SourceKind
    format: EvidenceFormat
    sha256: str
    storage_key: str | None = None
    original_url: str | None = None
    retrieved_at: datetime = Field(default_factory=utcnow)
    filename: str | None = None
    mime_type: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    model_config = {"frozen": True}

    @staticmethod
    def hash_bytes(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()


_CENT = Decimal("0.01")


def _money_in_cents(value: Decimal, name: str) -> Decimal:
    if isinstance(value, float) or not isinstance(value, Decimal):
        raise TypeError(f"{name} must be Decimal, never float")
    if not value.is_finite() or value != value.quantize(_CENT):
        raise ValueError(f"{name} must be a whole number of cents")
    return value


class DocumentLine(BaseModel):
    """One line of an invoice: what was bought, at which VAT rate, and for whom when the line says so."""

    model_config = {"frozen": True}

    id: str  # the line number as printed ("1", "2", ...)
    description: str = ""
    net_amount: Decimal | None = None
    vat_rate: Decimal | None = None  # percent, e.g. Decimal("23")
    quantity: Decimal | None = None
    reference: str | None = None  # project code, order line or cost reference printed on the line
    customer_tax_id: str | None = None  # the end customer named on a reseller line

    @property
    def text(self) -> str:
        return " ".join(p for p in (self.description, self.reference or "", self.customer_tax_id or "") if p)


class VatPart(BaseModel):
    """One VAT rate's share of an amount: net plus VAT. ``rate`` is None when the rate is not known."""

    model_config = {"frozen": True}

    rate: Decimal | None = None
    net: Decimal
    vat: Decimal = Decimal("0.00")

    @property
    def gross(self) -> Decimal:
        return self.net + self.vat


class AllocationMethod(str, Enum):
    """Why an amount sits on a cost center (strongest first). Every allocation also keeps its reasons."""

    OWNER = "owner"  # the owner said so (Needs You or a direct choice)
    RULE = "rule"  # a rule the owner taught ("always put ... on Job Rua das Flores")
    LEARNED_SPLIT = "learned_split"  # a split the owner taught ("always split EDP 40/30/30")
    IDENTIFIER = "identifier"  # a project code, address, plate, tax number ... found on the evidence
    LINES = "lines"  # the invoice lines name different cost centers
    INVOICE = "invoice"  # carried over between a payment and its matched invoice
    CARD = "card"  # paid with a card or account that belongs to one cost center
    HISTORY = "history"  # this supplier always went to the same cost center before (likely, not proven)


class AllocationShare(BaseModel):
    """The part of one amount that belongs to one cost center."""

    model_config = {"frozen": True}

    cost_center_id: str
    amount: Decimal  # positive, in cents
    parts: tuple[VatPart, ...] = ()  # the amount by VAT rate, when the rates are known
    percent: Decimal | None = None  # when the split was given in percent
    line_ids: tuple[str, ...] = ()  # invoice lines behind this share
    # Bought for the client this cost center is, to recharge to them (a reimbursable cost, a disbursement,
    # media bought for a client, a pass-through licence): recoverable from them, not the business's own cost.
    recharge: bool = False

    @model_validator(mode="after")
    def _exact(self) -> AllocationShare:
        _money_in_cents(self.amount, "amount")
        if self.amount <= 0:
            raise ValueError("a share must be more than zero")
        if self.parts and sum((p.gross for p in self.parts), Decimal(0)) != self.amount:
            raise ValueError("a share's VAT parts must add up to its amount")
        return self


class CostAllocation(BaseModel):
    """Which cost center(s) an amount belongs to: one, a split adding up exactly, or general costs.

    ``total`` is the absolute amount of the payment or document. The shares
    always add up to it to the cent; an allocation that does not is refused
    when it is built, never stored.
    """

    model_config = {"frozen": True}

    total: Decimal
    currency: str = "EUR"
    shares: tuple[AllocationShare, ...] = ()
    general: bool = False  # the company's general costs, not one cost center
    method: AllocationMethod
    quality: Quality = Quality.GREEN
    why: tuple[str, ...] = ()
    rule_id: str | None = None
    evidence_ids: tuple[str, ...] = ()
    # Why a share is (or is not) to recharge to the client: "owner" | "rule" | "setting" | "evidence" |
    # "history" (likely, not proven); None when nothing was decided about it (the business's own cost).
    recharge_method: str | None = None
    recharge_why: tuple[str, ...] = ()  # the reasons for it, shown under "Why?" after ``why``

    @model_validator(mode="after")
    def _adds_up(self) -> CostAllocation:
        _money_in_cents(self.total, "total")
        if self.total < 0:
            raise ValueError("the total is an absolute amount")
        if self.general and self.shares:
            raise ValueError("general costs have no cost center shares")
        if not self.general and not self.shares:
            raise ValueError("an allocation needs at least one share, or general costs")
        ids = [s.cost_center_id for s in self.shares]
        if len(set(ids)) != len(ids):
            raise ValueError("one share per cost center")
        if self.shares and sum((s.amount for s in self.shares), Decimal(0)) != self.total:
            raise ValueError("the shares must add up exactly to the total")
        return self

    @property
    def cost_center_ids(self) -> tuple[str, ...]:
        return tuple(s.cost_center_id for s in self.shares)

    @property
    def is_split(self) -> bool:
        return len(self.shares) > 1

    def amount_for(self, cost_center_id: str) -> Decimal:
        return next((s.amount for s in self.shares if s.cost_center_id == cost_center_id), Decimal(0))

    def recharge_for(self, cost_center_id: str) -> Decimal:
        """The part of this amount on ``cost_center_id`` that its client pays back (0 when none)."""
        return next((s.amount for s in self.shares if s.cost_center_id == cost_center_id and s.recharge), Decimal(0))

    @property
    def recharged(self) -> Decimal:
        """The part of this amount to recharge to clients (not the business's own cost)."""
        return sum((s.amount for s in self.shares if s.recharge), Decimal(0))


class DocumentType(str, Enum):
    INVOICE = "invoice"
    INVOICE_RECEIPT = "invoice_receipt"
    SIMPLIFIED_INVOICE = "simplified_invoice"
    RECEIPT = "receipt"
    CREDIT_NOTE = "credit_note"
    DEBIT_NOTE = "debit_note"
    STATEMENT = "statement"
    TAX_NOTICE = "tax_notice"
    PAYROLL = "payroll"
    LOAN_STATEMENT = "loan_statement"
    CONTRACT = "contract"
    # Documents that are not accounting documents (§3, §50): they never prove a purchase
    # or a payment. Kept as supporting evidence only.
    PRO_FORMA = "pro_forma"
    QUOTE = "quote"
    DELIVERY_NOTE = "delivery_note"  # guia de remessa / guia de transporte
    ORDER_CONFIRMATION = "order_confirmation"
    SUPPLIER_STATEMENT = "supplier_statement"  # extrato de conta corrente
    # A card terminal's or payment/sales platform's settlement statement: the sales, fees,
    # refunds and disputes behind one payout into the bank (backoffice.settlements).
    PAYOUT_REPORT = "payout_report"
    OTHER = "other"


# Supporting evidence only: attached to the supplier and the payment, never the invoice,
# never booked, never enough to close a payment (§3).
SUPPORTING_DOCUMENT_TYPES: frozenset[DocumentType] = frozenset({
    DocumentType.PRO_FORMA,
    DocumentType.QUOTE,
    DocumentType.DELIVERY_NOTE,
    DocumentType.ORDER_CONFIRMATION,
    DocumentType.SUPPLIER_STATEMENT,
})


class Document(BaseModel):
    id: str = Field(default_factory=lambda: new_id("doc"))
    tenant_id: str
    evidence_ids: list[str]
    doc_type: DocumentType = DocumentType.OTHER
    supplier_name: str | None = None
    supplier_tax_id: str | None = None
    customer_tax_id: str | None = None
    invoice_number: str | None = None
    issue_date: date | None = None
    due_date: date | None = None
    currency: str = "EUR"
    net_amount: Decimal | None = None
    vat_amount: Decimal | None = None
    gross_amount: Decimal | None = None
    iban: str | None = None
    payment_reference: str | None = None
    entity_id: str | None = None
    fields: dict[str, VerifiedField] = Field(default_factory=dict)
    quality: Quality = Quality.AMBER
    lines: tuple[DocumentLine, ...] = ()
    cost_allocation: CostAllocation | None = None  # which job, property, vehicle ... (cost centers)

    @property
    def signed_gross(self) -> Decimal | None:
        if self.gross_amount is None:
            return None
        return -self.gross_amount if self.doc_type == DocumentType.CREDIT_NOTE else self.gross_amount


class TransactionKind(str, Enum):
    CARD = "card"
    TRANSFER_OUT = "transfer_out"
    TRANSFER_IN = "transfer_in"
    DIRECT_DEBIT = "direct_debit"
    FEE = "fee"
    INTERNAL = "internal"


class Transaction(BaseModel):
    """Bank or card transaction. Negative amount = money out."""

    id: str = Field(default_factory=lambda: new_id("tx"))
    tenant_id: str
    account_id: str
    booked_on: date
    amount: Decimal
    currency: str = "EUR"
    counterparty: str
    description: str = ""
    kind: TransactionKind = TransactionKind.CARD
    card_last4: str | None = None
    counterparty_iban: str | None = None
    reference: str | None = None
    entity_id: str | None = None
    cost_allocation: CostAllocation | None = None  # which job, property, vehicle ... (cost centers)


class LegalEntity(BaseModel):
    id: str = Field(default_factory=lambda: new_id("ent"))
    tenant_id: str
    name: str
    country: str
    tax_id: str
    own_ibans: list[str] = Field(default_factory=list)


class Supplier(BaseModel):
    id: str = Field(default_factory=lambda: new_id("sup"))
    tenant_id: str
    name: str
    aliases: list[str] = Field(default_factory=list)
    tax_id: str | None = None
    known_ibans: list[str] = Field(default_factory=list)
    email_domains: list[str] = Field(default_factory=list)
    countries: list[str] = Field(default_factory=list)
    contact_email: str | None = None


class ObligationKind(str, Enum):
    TAX_DEADLINE = "tax_deadline"
    GOVERNMENT_REQUEST = "government_request"
    KYC_REQUEST = "kyc_request"
    LICENSE_RENEWAL = "license_renewal"
    INSURANCE_RENEWAL = "insurance_renewal"
    CONTRACT_RENEWAL = "contract_renewal"
    FILING = "filing"
    RENT = "rent"
    DEBT_COLLECTION = "debt_collection"
    BANK_REQUEST = "bank_request"
    PAYMENT_DEADLINE = "payment_deadline"
    VAT_RETURN = "vat_return"  # a periodic VAT return its country's calendar sets (Spain's modelo 303)


class Obligation(BaseModel):
    id: str = Field(default_factory=lambda: new_id("obl"))
    tenant_id: str
    entity_id: str
    kind: ObligationKind
    title: str
    due_on: date
    amount: Decimal | None = None
    responsible: str = "owner"
    consequence: str = ""
    required_evidence: str = ""
    verification_condition: str = ""
    satisfied_by_evidence_ids: list[str] = Field(default_factory=list)
