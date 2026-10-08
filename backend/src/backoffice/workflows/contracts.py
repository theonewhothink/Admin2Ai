"""Typed contracts for the durable workflows (§22, §25, §27, §45).

Every value that crosses a Temporal boundary (workflow input/result, signal,
query, activity request/response) is one of these immutable pydantic models.
They serialise through :data:`backoffice.workflows.DATA_CONVERTER` (pydantic
JSON), so money stays ``Decimal`` end to end and datetimes stay
timezone-aware.

Rules encoded here rather than trusted to callers:

* Money is ``Decimal``; floats are refused, never silently rounded.
* Closing an item requires evidence and GREEN quality (§3, §57).
* An owner answer is itself evidence and must reference its stored record (§54).
"""

from __future__ import annotations

import calendar
from datetime import date, time, timedelta
from decimal import Decimal
from enum import Enum
from typing import Annotated, Any

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from backoffice.domain.models import Quality, Supplier, Transaction

__all__ = [
    "ITEM_RESOLVED_SIGNAL",
    "DEFAULT_DAY_OFFSETS",
    "MONTH_NAMES",
    "MONTH_STEPS",
    "AccountantQuery",
    "ActorKind",
    "ApprovalAuditEvent",
    "ApprovalCarry",
    "ApprovalCategory",
    "ApprovalDecisionKind",
    "ApprovalInput",
    "ApprovalProgress",
    "ApprovalRecord",
    "ApprovalRequest",
    "ApprovalResult",
    "ApprovalSignal",
    "ApprovalStatus",
    "ApproverCheck",
    "ApproverCheckRequest",
    "AuthorizationDecision",
    "AuthorizationRequest",
    "CancelRequest",
    "ChasePolicy",
    "ClosureRequest",
    "ClosureVerdict",
    "CompletenessReport",
    "CompletenessRequest",
    "Contract",
    "DeliveryCheckRequest",
    "DeliveryConfirmation",
    "DeliveryReceipt",
    "DeliveryRequest",
    "DocumentArrival",
    "EvidenceBatch",
    "EvidenceOrigin",
    "GatedAction",
    "ItemOutcome",
    "ItemResolved",
    "ItemStatus",
    "MatchOutcome",
    "MissingInvoiceInput",
    "MissingInvoiceProgress",
    "MissingInvoiceResult",
    "MonthCloseInput",
    "MonthCloseProgress",
    "MonthCloseResult",
    "MonthCard",
    "MonthCloseState",
    "MonthStep",
    "MonthSummary",
    "NeedsYouItem",
    "NeedsYouKind",
    "OpenCard",
    "OwnerAnswer",
    "OwnerAnswerKind",
    "OwnerItemRef",
    "OwnerItemResolution",
    "OwnerOption",
    "PackageRef",
    "PackageRequest",
    "Period",
    "QueryHandlingRequest",
    "QueryResolution",
    "ReconcileRequest",
    "SearchOutcome",
    "SearchRequest",
    "SendPackageRequest",
    "StepRecord",
    "SupplierMessageKind",
    "SupplierMessageReceipt",
    "SupplierRequest",
    "ThreadCheckRequest",
    "ThreadCheckResult",
    "TransactionRef",
    "VerificationOutcome",
    "VerifyRequest",
    "WithdrawRequest",
]

MONTH_NAMES: tuple[str, ...] = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)  # fmt: skip; literal, not calendar.month_name, so output never depends on locale

# Signal name shared by MissingInvoiceWorkflow (sender) and MonthCloseWorkflow
# (receiver); a constant avoids an import cycle between the two modules.
ITEM_RESOLVED_SIGNAL = "item_resolved"

NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
EvidenceIds = tuple[NonBlank, ...]
CurrencyCode = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]


class Contract(BaseModel):
    """Immutable, strict-shaped base for every workflow contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")


def _refuse_float(value: Any) -> Any:
    if isinstance(value, float):
        raise ValueError("money must be Decimal, never float")
    return value


def _positive(value: timedelta, name: str) -> timedelta:
    if value <= timedelta(0):
        raise ValueError(f"{name} must be positive")
    return value


# ---------- shared


class GatedAction(str, Enum):
    """§25 'automatic if authorized' actions a workflow checks before acting.

    Values equal ``backoffice.policy.ActionKind`` values so the integration can
    map them one to one.
    """

    SUPPLIER_INVOICE_REQUEST = "supplier_invoice_request"
    DOCUMENT_DELIVERY = "document_delivery"


class TransactionRef(Contract):
    """The facts about one bank/card transaction a workflow needs (§20, §22).

    ``amount`` keeps the bank sign (negative = money out), like
    :class:`backoffice.domain.models.Transaction`.
    """

    tenant_id: NonBlank
    transaction_id: NonBlank
    entity_id: str | None = None
    booked_on: date
    amount: Decimal
    currency: CurrencyCode = "EUR"
    counterparty: NonBlank
    supplier_id: str | None = None
    supplier_name: str | None = None
    invoice_number_hint: str | None = None

    @field_validator("amount", mode="before")
    @classmethod
    def _amount_not_float(cls, v: Any) -> Any:
        return _refuse_float(v)

    @property
    def supplier_display(self) -> str:
        """Best human name for the supplier (normalised name, else bank text)."""
        return (self.supplier_name or "").strip() or self.counterparty

    @classmethod
    def from_transaction(
        cls,
        tx: Transaction,
        *,
        supplier: Supplier | None = None,
        invoice_number_hint: str | None = None,
    ) -> TransactionRef:
        return cls(
            tenant_id=tx.tenant_id,
            transaction_id=tx.id,
            entity_id=tx.entity_id,
            booked_on=tx.booked_on,
            amount=tx.amount,
            currency=tx.currency,
            counterparty=tx.counterparty,
            supplier_id=supplier.id if supplier else None,
            supplier_name=supplier.name if supplier else None,
            invoice_number_hint=invoice_number_hint,
        )


class EvidenceOrigin(str, Enum):
    SEARCH = "search"
    SUPPLIER_REPLY = "supplier_reply"
    PIPELINE = "pipeline"  # arrived through normal ingestion (signal)
    OWNER = "owner"


class EvidenceBatch(Contract):
    evidence_ids: tuple[NonBlank, ...] = Field(min_length=1)
    origin: EvidenceOrigin


class OwnerOption(Contract):
    """One choice on a Needs-You card: ``key`` is machine-readable, ``label`` plain."""

    key: NonBlank
    label: NonBlank


class NeedsYouKind(str, Enum):
    MISSING_DOCUMENT = "missing_document"
    UNCONFIRMED_DOCUMENT = "unconfirmed_document"
    CONFLICT = "conflict"
    NO_SUPPLIER_CONTACT = "no_supplier_contact"
    APPROVAL = "approval"
    PACKAGE_READY = "package_ready"
    DELIVERY_UNCONFIRMED = "delivery_unconfirmed"
    ACCOUNTANT_QUESTION = "accountant_question"


class NeedsYouItem(Contract):
    """A decision for the owner (§35): plain text, options, evidence, 'Why?'.

    ``subject_id`` and ``dedupe_key`` are internal and never displayed.
    ``notify`` is True only for §42 push-worthy events.
    """

    tenant_id: NonBlank
    kind: NeedsYouKind
    headline: NonBlank
    detail: str = ""
    why: str = ""
    options: tuple[OwnerOption, ...] = ()
    subject_id: NonBlank
    evidence_ids: EvidenceIds = ()
    dedupe_key: NonBlank
    notify: bool = False


class OwnerItemRef(Contract):
    item_id: NonBlank


class OwnerItemResolution(Contract):
    tenant_id: NonBlank
    item_id: NonBlank
    message: NonBlank  # plain language, e.g. "Done. I found the invoice."


class AuthorizationRequest(Contract):
    tenant_id: NonBlank
    entity_id: str | None = None
    action: GatedAction
    subject_id: NonBlank


class AuthorizationDecision(Contract):
    authorized: bool
    reason: str = ""


# ---------- missing invoice (§22)

DEFAULT_REPLY_WAIT = timedelta(days=6)  # §45: "request invoice -> wait 6 days"


class ChasePolicy(Contract):
    """How patiently a missing invoice is chased (§22, §45)."""

    reply_wait: timedelta = DEFAULT_REPLY_WAIT
    thread_check_interval: timedelta = timedelta(hours=12)
    max_reminders: int = Field(default=2, ge=0, le=10)
    # After escalating, keep listening for a late supplier reply, less often.
    late_reply_window: timedelta = timedelta(days=30)
    late_reply_check_interval: timedelta = timedelta(days=1)
    # §27: month close finds gaps on Day -5 but chases suppliers from Day -3.
    chase_not_before: AwareDatetime | None = None

    @field_validator("reply_wait", "thread_check_interval", "late_reply_check_interval")
    @classmethod
    def _must_be_positive(cls, v: timedelta, info: Any) -> timedelta:
        return _positive(v, info.field_name)

    @field_validator("late_reply_window")
    @classmethod
    def _not_negative(cls, v: timedelta) -> timedelta:
        if v < timedelta(0):
            raise ValueError("late_reply_window cannot be negative")
        return v


class MissingInvoiceInput(Contract):
    transaction: TransactionRef
    policy: ChasePolicy = ChasePolicy()
    # Workflow to tell (signal ITEM_RESOLVED_SIGNAL) when this item finishes.
    report_to_workflow_id: str | None = None
    # Internal: the chase state carried across continue-as-new (§45). Callers
    # leave it unset; it is JSON so this module need not import the state type.
    carry: dict[str, Any] | None = None


class DocumentArrival(Contract):
    """Signal: evidence that may be the missing invoice arrived (already stored)."""

    evidence_ids: tuple[NonBlank, ...] = Field(min_length=1)
    channel: str = "email"


class OwnerAnswerKind(str, Enum):
    DOCUMENT_PROVIDED = "document_provided"
    NO_DOCUMENT_NEEDED = "no_document_needed"
    KEEP_CHASING = "keep_chasing"
    OWNER_WILL_HANDLE = "owner_will_handle"


class OwnerAnswer(Contract):
    """Signal: the owner's answer to a Needs-You card.

    ``answer_evidence_id`` is the stored record of the answer: a human answer
    is evidence (§54, ``ExtractionMethod.HUMAN``) and is what lets an item end
    as NOT_REQUIRED.
    """

    kind: OwnerAnswerKind
    answered_by: NonBlank
    answer_evidence_id: NonBlank
    evidence_ids: EvidenceIds = ()

    @model_validator(mode="after")
    def _document_needs_evidence(self) -> OwnerAnswer:
        if self.kind is OwnerAnswerKind.DOCUMENT_PROVIDED and not self.evidence_ids:
            raise ValueError("a provided document must reference its evidence")
        return self


class CancelRequest(Contract):
    requested_by: NonBlank
    reason: str = ""


class SearchRequest(Contract):
    transaction: TransactionRef
    exclude_evidence_ids: tuple[str, ...] = ()


class SearchOutcome(Contract):
    """Result of the §22 search plan (email, history, files, portals, ...)."""

    evidence_ids: EvidenceIds = ()
    sources_checked: tuple[str, ...] = ()


class VerifyRequest(Contract):
    transaction: TransactionRef
    evidence_ids: tuple[NonBlank, ...] = Field(min_length=1)


class VerificationOutcome(Contract):
    """Field-level verification of a document (§18-19). RED means CONFLICT."""

    quality: Quality
    document_id: str | None = None
    evidence_ids: EvidenceIds = ()
    reasons: tuple[str, ...] = ()


class ReconcileRequest(Contract):
    transaction: TransactionRef
    document_id: NonBlank
    evidence_ids: EvidenceIds = ()


class MatchOutcome(Contract):
    matched: bool
    quality: Quality
    evidence_ids: EvidenceIds = ()
    reasons: tuple[str, ...] = ()


class SupplierMessageKind(str, Enum):
    REQUEST = "request"
    REMINDER = "reminder"


class SupplierRequest(Contract):
    """Ask a supplier for an invoice (§22).

    ``default_body`` is the English §22 wording; a mailer may localise it
    (e.g. Portuguese) but must keep the same facts. ``idempotency_key`` is
    stable across activity retries so a message is never sent twice.
    """

    transaction: TransactionRef
    kind: SupplierMessageKind
    message_number: int = Field(ge=1)
    thread_id: str | None = None
    default_body: NonBlank
    idempotency_key: NonBlank


class SupplierMessageReceipt(Contract):
    """``sent=False`` means no usable contact; the owner is asked instead."""

    sent: bool
    thread_id: str | None = None
    message_evidence_id: str | None = None
    sent_at: AwareDatetime | None = None
    reason: str = ""


class ThreadCheckRequest(Contract):
    transaction: TransactionRef
    thread_id: NonBlank
    since: AwareDatetime


class ThreadCheckResult(Contract):
    replied: bool
    evidence_ids: EvidenceIds = ()  # attachments/links found, already stored


class ItemStatus(str, Enum):
    CLOSED = "closed"
    NOT_REQUIRED = "not_required"
    CANCELLED = "cancelled"


class ItemOutcome(Contract):
    """Final state of a tracked item, persisted by the ledger (§3, §55)."""

    tenant_id: NonBlank
    subject_type: NonBlank = "transaction"
    subject_id: NonBlank
    status: ItemStatus
    quality: Quality | None = None
    evidence_ids: EvidenceIds = ()
    document_id: str | None = None
    actor: NonBlank
    note: str = ""
    superseded_evidence_ids: EvidenceIds = ()

    @model_validator(mode="after")
    def _golden_rule(self) -> ItemOutcome:
        if self.status is ItemStatus.CLOSED:
            if self.quality is not Quality.GREEN:
                raise ValueError("closure requires verified (GREEN) evidence")
            if not self.evidence_ids or not self.document_id:
                raise ValueError("closure requires evidence and a document")
        if self.status is ItemStatus.NOT_REQUIRED and not self.evidence_ids:
            raise ValueError("'not required' must be backed by evidence")
        return self


class ItemResolved(Contract):
    """Signal to a parent workflow: one of its items finished."""

    subject_id: NonBlank
    status: ItemStatus


class MissingInvoiceResult(Contract):
    status: ItemStatus
    transaction_id: NonBlank
    evidence_ids: EvidenceIds = ()
    document_id: str | None = None
    messages_sent: int = 0
    escalations: int = 0
    superseded_evidence_ids: EvidenceIds = ()
    summary: NonBlank


class MissingInvoiceProgress(Contract):
    """Structured status for the API; ``status_text`` is what the owner reads."""

    phase: str
    status_text: NonBlank
    messages_sent: int
    needs_owner: bool
    finished: bool
    last_message_at: AwareDatetime | None = None


# ---------- hard approval (§25)


class ApprovalCategory(str, Enum):
    """§25 hard-approval actions (values equal ``policy.ActionKind``)."""

    MONEY_MOVEMENT = "money_movement"
    TAX_FILING = "tax_filing"
    BANK_DETAIL_CHANGE = "bank_detail_change"
    LEGALLY_BINDING_ACCEPTANCE = "legally_binding_acceptance"
    ORIGINAL_EVIDENCE_DELETION = "original_evidence_deletion"


class ActorKind(str, Enum):
    HUMAN = "human"
    AGENT = "agent"
    SYSTEM = "system"


class ApprovalDecisionKind(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"


class ApprovalCarry(Contract):
    """State carried across continue-as-new."""

    requested_at: AwareDatetime
    last_prompted_at: AwareDatetime
    reminders_sent: int = 0
    refused_attempts: int = 0
    owner_item_id: str | None = None


class ApprovalInput(Contract):
    """A hard-approval request. ``summary`` is the plain sentence the owner sees."""

    tenant_id: NonBlank
    request_id: NonBlank
    category: ApprovalCategory
    summary: NonBlank
    amount: Decimal | None = None
    currency: CurrencyCode = "EUR"
    evidence_ids: tuple[NonBlank, ...] = Field(min_length=1)
    requested_by: NonBlank
    eligible_approvers: tuple[NonBlank, ...] = ()
    first_reminder_after: timedelta = timedelta(days=1)
    max_reminder_interval: timedelta = timedelta(days=7)
    reminders_per_run: int = Field(default=50, ge=1)
    carry: ApprovalCarry | None = None

    @field_validator("amount", mode="before")
    @classmethod
    def _amount_not_float(cls, v: Any) -> Any:
        return _refuse_float(v)

    @model_validator(mode="after")
    def _intervals(self) -> ApprovalInput:
        if self.category is ApprovalCategory.MONEY_MOVEMENT and not (
            self.amount is not None and self.amount > 0
        ):
            # The approver must see exactly how much money moves (§25).
            raise ValueError("a money movement needs a positive amount")
        _positive(self.first_reminder_after, "first_reminder_after")
        if self.max_reminder_interval < self.first_reminder_after:
            raise ValueError("max_reminder_interval must be >= first_reminder_after")
        return self


class ApprovalSignal(Contract):
    """Signal: a decision. Deliberately permissive in shape so that a bad
    attempt is *recorded and refused* rather than silently dropped.

    ``assertion_id`` references a server-side record of a fresh, strongly
    authenticated session (never a bearer credential); the identity service
    confirms it via the ``verify_approver`` activity.
    """

    decision: ApprovalDecisionKind
    actor_id: str = ""
    actor_kind: ActorKind = ActorKind.HUMAN
    assertion_id: str = ""
    note: str = ""


class WithdrawRequest(Contract):
    requested_by: NonBlank
    reason: str = ""


class ApprovalRequest(Contract):
    """Create (reminder_number 0) or re-surface (>0) the approval card."""

    tenant_id: NonBlank
    request_id: NonBlank
    category: ApprovalCategory
    headline: NonBlank
    summary: NonBlank
    amount: Decimal | None = None
    currency: CurrencyCode = "EUR"
    evidence_ids: tuple[NonBlank, ...] = Field(min_length=1)
    reminder_number: int = Field(default=0, ge=0)
    owner_item_id: str | None = None
    dedupe_key: NonBlank

    @field_validator("amount", mode="before")
    @classmethod
    def _amount_not_float(cls, v: Any) -> Any:
        return _refuse_float(v)


class ApproverCheckRequest(Contract):
    tenant_id: NonBlank
    request_id: NonBlank
    category: ApprovalCategory
    actor_id: NonBlank
    assertion_id: NonBlank


class ApproverCheck(Contract):
    verified: bool
    display_name: str | None = None
    auth_method: str | None = None
    reason: str = ""


class ApprovalRecord(Contract):
    """Who decided, when, and how they proved it (§25, §55)."""

    request_id: NonBlank
    decision: ApprovalDecisionKind
    actor_id: NonBlank
    actor_display_name: str | None = None
    auth_method: NonBlank
    decided_at: AwareDatetime
    note: str = ""


class ApprovalAuditEvent(Contract):
    """Every attempt, accepted or refused, goes to the audit log (§55)."""

    tenant_id: NonBlank
    request_id: NonBlank
    accepted: bool
    decision: ApprovalDecisionKind | None = None
    actor_id: str = ""
    actor_kind: ActorKind | None = None
    reason: str = ""
    at: AwareDatetime


class ApprovalStatus(str, Enum):
    APPROVED = "approved"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"


class ApprovalResult(Contract):
    status: ApprovalStatus
    request_id: NonBlank
    record: ApprovalRecord | None = None
    reminders_sent: int = 0
    refused_attempts: int = 0
    summary: NonBlank

    @model_validator(mode="after")
    def _decision_has_record(self) -> ApprovalResult:
        decided = self.status in (ApprovalStatus.APPROVED, ApprovalStatus.REJECTED)
        if decided and self.record is None:
            raise ValueError("a decision must carry who decided and when")
        if self.status is ApprovalStatus.APPROVED and (
            self.record is None or self.record.decision is not ApprovalDecisionKind.APPROVE
        ):
            raise ValueError("approved results need an approving record")
        return self


class ApprovalProgress(Contract):
    waiting: bool
    status_text: NonBlank
    reminders_sent: int
    refused_attempts: int
    requested_at: AwareDatetime | None = None


# ---------- month close (§27)


class MonthStep(str, Enum):
    COMPLETENESS_AUDIT = "completeness_audit"
    EVIDENCE_RETRIEVAL = "evidence_retrieval"
    SUPPLIER_CHASES = "supplier_chases"
    PACKAGE = "package"
    DELIVERY_CONFIRMATION = "delivery_confirmation"
    ACCOUNTANT_QUERIES = "accountant_queries"
    CLOSURE = "closure"


MONTH_STEPS: tuple[MonthStep, ...] = tuple(MonthStep)

# §27 day offsets relative to Day 0 (package day). CLOSURE follows the last step.
DEFAULT_DAY_OFFSETS: dict[MonthStep, int] = {
    MonthStep.COMPLETENESS_AUDIT: -7,
    MonthStep.EVIDENCE_RETRIEVAL: -5,
    MonthStep.SUPPLIER_CHASES: -3,
    MonthStep.PACKAGE: 0,
    MonthStep.DELIVERY_CONFIRMATION: 1,
    MonthStep.ACCOUNTANT_QUERIES: 2,
}


class Period(Contract):
    year: int = Field(ge=2000, le=2100)
    month: int = Field(ge=1, le=12)

    @property
    def first_day(self) -> date:
        return date(self.year, self.month, 1)

    @property
    def last_day(self) -> date:
        return date(self.year, self.month, calendar.monthrange(self.year, self.month)[1])

    @property
    def month_name(self) -> str:
        return MONTH_NAMES[self.month - 1]

    @property
    def key(self) -> str:
        return f"{self.year:04d}-{self.month:02d}"


class CompletenessRequest(Contract):
    tenant_id: NonBlank
    entity_id: NonBlank
    period: Period


class CompletenessReport(Contract):
    transactions_checked: int = Field(ge=0)
    documents_collected: int = Field(ge=0)
    gaps: tuple[TransactionRef, ...] = ()


class PackageRequest(Contract):
    tenant_id: NonBlank
    entity_id: NonBlank
    period: Period


class PackageRef(Contract):
    package_id: NonBlank
    evidence_id: NonBlank  # the package itself is stored as evidence
    complete_items: int = Field(ge=0)
    missing_items: int = Field(ge=0)


class DeliveryRequest(Contract):
    tenant_id: NonBlank
    entity_id: NonBlank
    period: Period
    package: PackageRef
    idempotency_key: NonBlank


class DeliveryReceipt(Contract):
    delivered: bool
    delivery_evidence_id: str | None = None
    confirmed: bool = False
    confirmation_evidence_id: str | None = None


class DeliveryCheckRequest(Contract):
    tenant_id: NonBlank
    package: PackageRef
    receipt: DeliveryReceipt | None = None


class DeliveryConfirmation(Contract):
    """Activity result or signal. A confirmation needs evidence (§3).

    ``package_id`` names the package it confirms; a confirmation for another
    package, or one that arrives before any package exists, is ignored.
    """

    confirmed: bool
    evidence_id: str | None = None
    confirmed_by: str = ""
    package_id: str | None = None

    @model_validator(mode="after")
    def _needs_evidence(self) -> DeliveryConfirmation:
        if self.confirmed and not (self.evidence_id or "").strip():
            raise ValueError("a delivery confirmation needs evidence")
        return self


class SendPackageRequest(Contract):
    """Signal: the owner tapped "Send it for me" on the package card.

    Their explicit request authorizes this one delivery (§25 "automatic if
    authorized"). ``answer_evidence_id`` is the stored record of the tap (§54).
    """

    requested_by: NonBlank
    answer_evidence_id: NonBlank
    package_id: str | None = None


class AccountantQuery(Contract):
    query_id: NonBlank
    text: NonBlank
    subject_id: str | None = None
    received_at: AwareDatetime | None = None


class QueryHandlingRequest(Contract):
    tenant_id: NonBlank
    entity_id: NonBlank
    period: Period
    query: AccountantQuery


class QueryResolution(Contract):
    query_id: NonBlank
    resolved: bool
    evidence_ids: EvidenceIds = ()
    owner_question: str | None = None  # plain language, when the owner must answer

    @model_validator(mode="after")
    def _resolved_needs_evidence(self) -> QueryResolution:
        if self.resolved and not self.evidence_ids:
            raise ValueError("a resolved question needs evidence")
        return self


class ClosureRequest(Contract):
    tenant_id: NonBlank
    entity_id: NonBlank
    period: Period
    package_id: str | None = None
    delivery_evidence_ids: EvidenceIds = ()
    open_query_ids: tuple[str, ...] = ()


class MonthSummary(Contract):
    """§2 north-star numbers."""

    transactions_checked: int = Field(default=0, ge=0)
    documents_collected: int = Field(default=0, ge=0)
    missing_documents_retrieved: int = Field(default=0, ge=0)
    suppliers_chased: int = Field(default=0, ge=0)
    accountant_questions_resolved: int = Field(default=0, ge=0)
    tax_obligations_verified: int = Field(default=0, ge=0)
    unresolved_issues: int = Field(default=0, ge=0)
    owner_minutes: int | None = Field(default=None, ge=0)


class ClosureVerdict(Contract):
    """The closure service's evidence-based verdict (source of truth, §3)."""

    closed: bool
    open_items: int = Field(ge=0)
    summary: MonthSummary = MonthSummary()
    evidence_ids: EvidenceIds = ()


class StepRecord(Contract):
    step: MonthStep
    scheduled_for: AwareDatetime
    ran_at: AwareDatetime


class MonthCard(str, Enum):
    """The Needs-You cards a month close can raise (§35)."""

    PACKAGE_READY = "package-ready"
    DELIVERY_UNCONFIRMED = "delivery-unconfirmed"
    ACCOUNTANT_QUESTION = "accountant-question"


class OpenCard(Contract):
    """A month-close card still in front of the owner, taken down once its
    situation resolves so the owner never acts on something already done (§69).

    ``version`` is the package version a package card is about; ``attempt``
    counts the owner's "Send it for me" requests; ``query_id`` names the
    accountant question.
    """

    card: MonthCard
    item_id: NonBlank
    key: NonBlank
    version: int = Field(default=0, ge=0)
    attempt: int = Field(default=0, ge=0)
    query_id: str | None = None


class MonthCloseState(Contract):
    """Everything a month close needs to resume after continue-as-new."""

    completed: tuple[StepRecord, ...] = ()
    audit: CompletenessReport | None = None
    gaps: tuple[TransactionRef, ...] = ()
    chased_ids: tuple[str, ...] = ()
    already_running_ids: tuple[str, ...] = ()
    resolved_subject_ids: tuple[str, ...] = ()
    # The package the accountant should have; earlier, less complete ones
    # were superseded by it (§27: the accountant must end up with everything).
    package: PackageRef | None = None
    earlier_packages: tuple[PackageRef, ...] = ()
    delivery: DeliveryReceipt | None = None
    delivered_at: AwareDatetime | None = None
    delivery_confirmation: DeliveryConfirmation | None = None
    send_request: SendPackageRequest | None = None  # owner's "Send it for me"
    send_attempts: int = 0
    owner_items: tuple[str, ...] = ()  # dedupe keys of Needs-You items raised
    open_cards: tuple[OpenCard, ...] = ()
    pending_queries: tuple[AccountantQuery, ...] = ()  # not yet handled
    waiting_queries: tuple[AccountantQuery, ...] = ()  # handled, no evidence yet
    handled_queries: tuple[QueryResolution, ...] = ()  # answered with evidence
    closure_checks: int = 0
    last_verdict: ClosureVerdict | None = None
    runs: int = 1


class MonthCloseInput(Contract):
    """Start a month close. ``day_zero`` is §27 Day 0 (package day); the spec
    leaves the date to the business/accountant, so the caller chooses it, but
    the package day must fall after the month ends. Steps run at ``run_at``
    UTC on their day.
    """

    tenant_id: NonBlank
    entity_id: NonBlank
    period: Period
    day_zero: date
    run_at: time = time(6, 0)
    day_offsets: dict[MonthStep, int] = Field(default_factory=lambda: dict(DEFAULT_DAY_OFFSETS))
    chase_policy: ChasePolicy = ChasePolicy()
    closure_recheck_interval: timedelta = timedelta(days=1)
    rechecks_per_run: int = Field(default=30, ge=1)
    state: MonthCloseState | None = None

    @model_validator(mode="after")
    def _schedule_is_ordered(self) -> MonthCloseInput:
        if self.run_at.tzinfo is not None:
            raise ValueError("run_at is a UTC wall-clock time; pass it without tzinfo")
        scheduled = [s for s in MONTH_STEPS if s is not MonthStep.CLOSURE]
        if set(self.day_offsets) != set(scheduled):
            raise ValueError("day_offsets must give a day for every scheduled step")
        days = [self.day_offsets[s] for s in scheduled]
        if days != sorted(days):
            raise ValueError("day_offsets must follow the §27 step order")
        if self.day_zero < self.period.first_day:
            raise ValueError("day_zero cannot be before the month starts")
        package_day = self.day_zero + timedelta(days=self.day_offsets[MonthStep.PACKAGE])
        if package_day <= self.period.last_day:
            # A package prepared while the month is still running misses the
            # last days' transactions: it could never be complete (§27).
            raise ValueError("the accountant package can only be prepared after the month ends")
        _positive(self.closure_recheck_interval, "closure_recheck_interval")
        return self


class MonthCloseResult(Contract):
    period: Period
    headline: NonBlank
    summary_line: NonBlank
    summary: MonthSummary
    steps: tuple[StepRecord, ...]
    package_id: str | None = None
    evidence_ids: EvidenceIds = ()
    chases_started: int = 0


class MonthCloseProgress(Contract):
    next_step: MonthStep | None
    next_step_at: AwareDatetime | None
    steps_done: tuple[MonthStep, ...]
    status_text: NonBlank
    open_items: int | None = None
