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

from pydantic import BaseModel, Field

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
    OTHER = "other"


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
