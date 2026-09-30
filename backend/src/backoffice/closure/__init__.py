"""Closure: month status, obligations, month-end autopilot, accountant package and metrics.

The product is closure (§2). Everything here is deterministic and depends only
on :mod:`backoffice.domain`; other modules feed it through small protocols.

Month status (§2, §3, §47–48, §57)
    ``compute_month_status(entity_id, month, items, *, now, connectors, decisions=(),
    obligations=(), activities=(), interactions=(), weights=None, tz=UTC) -> MonthStatus``
    ``render_closed_summary(status) -> str`` — "September is closed. 218 transactions
    checked · … · 0 unresolved issues. You spent 4 minutes." (raises ``MonthNotClosed``)
    Inputs: ``ConnectorCoverage`` (name, healthy, covered_from, covered_until
    [, last_synced_at]) and ``EvidenceDecision`` (transaction_id, quality,
    requires_document) protocols; ``Activity`` / ``OwnerInteraction`` log records
    (enum fields and ``period`` may be given as stored strings). ``items`` and
    ``decisions`` must already be scoped to the company and month.
    ``MonthStatus.weighted_done/weighted_total/done_share`` carry the exact figures
    behind ``percent_closed`` so combined views agree with each company.

Obligations (§24)
    ``detect_obligation(text, *, tenant_id, received_on, sender="", source_kind=None,
    entities=(), default_entity_id=None) -> ObligationFinding | None``
    ``satisfy(obligation, facts: Sequence[EvidenceFact]) -> Satisfaction``
    ``due_soon(obligations, today, *, within_days=14, entity_id=None) -> list[DueItem]``
    ``VerificationCondition.parse/encode/describe`` — the machine-checkable
    ``Obligation.verification_condition`` format, with ``since`` (proof must be
    newer than the letter unless it carries the reference) and ``disputed`` (the
    letter gave two amounts: nothing closes it until a person replaces the
    condition with a confirmed one).

Month-end autopilot (§27)
    ``plan_month_end(month, close_day) -> CloseSchedule``
    ``evaluate_schedule(schedule, progress: CloseProgress, today) -> ScheduleStatus``
    (``CloseProgress.package_sha256``: Day +1 counts only a confirmation of the
    latest package)

Accountant package (§27 Day 0/+1)
    ``PackageEntry.from_domain(item, *, transaction=None, documents=(), evidence=(), why=())``
    ``build_package(entity, month, entries, *, generated_at, questions=(),
    csv_format=STANDARD_CSV | PT_EXCEL, evidence_reader=None) -> AccountantPackage``
    ``StoredEvidenceReader(object_store)`` reads originals by ``Evidence.storage_key``.
    ``verify_package(data, *, max_file_bytes, max_total_bytes) -> list[str]`` (safe on
    untrusted zips); ``AccountantPackage.delivery() -> PackageDelivery`` with
    ``.deliver(...)`` / ``.confirm(...)``. Entries from another tenant or company are refused.

Accountant workspace (§28)
    ``build_home_table(clients: Sequence[ClientMonth]) -> list[ClientRow]``
    ``build_client_view(entity, status, *, entries, decisions, anomalies, tax_flags,
    questions, delivery) -> ClientView``

Metrics and Home (§35, §58–59, §60)
    ``evaluate_activation(ActivationSignals) -> ActivationReport``
    ``customer_success(month, *, items, statuses, activities, interactions,
    accountant_question_baseline=None, tz=UTC) -> CustomerSuccessReport``
    ``home_summary(statuses, names, *, today, obligations=(), risk_count=0) -> HomeSummary``
    ``render_business_audit(BusinessAuditFindings) -> BusinessAuditReport``

``to_jsonable(value)`` turns any output above into JSON-safe data (money as strings).
"""

from ._serialize import to_jsonable
from .accountant import (
    AccountantQuestion,
    Anomaly,
    ClientMonth,
    ClientRow,
    ClientView,
    EvidenceRow,
    ExportState,
    ExportStatus,
    MissingDocument,
    QuestionDirection,
    QuestionStatus,
    TaxFlag,
    build_client_view,
    build_home_table,
    client_row,
    needs_accountant,
)
from .activity import (
    Activity,
    ActivityKind,
    Actor,
    InteractionKind,
    OwnerInteraction,
    is_owner_actor,
    owner_touched,
)
from .autopilot import (
    STEP_OFFSETS,
    CloseProgress,
    CloseSchedule,
    ScheduleStatus,
    StepKind,
    StepPlan,
    StepState,
    StepStatus,
    evaluate_schedule,
    plan_month_end,
)
from .business_audit import (
    AUDIT_DAYS,
    BusinessAuditFindings,
    BusinessAuditReport,
    PriceIncrease,
    audit_window,
    render_business_audit,
)
from .metrics import (
    ActivationCondition,
    ActivationReport,
    ActivationSignals,
    CompanyLine,
    CustomerSuccessReport,
    HomeSummary,
    Metric,
    MetricResult,
    customer_success,
    evaluate_activation,
    home_summary,
)
from .month import (
    TAX_OBLIGATION_KINDS,
    Blocker,
    BlockerKind,
    ClosedSummaryText,
    CloseSummary,
    ConnectorCoverage,
    EvidenceDecision,
    ItemCounts,
    ItemState,
    MonthNotClosed,
    MonthState,
    MonthStatus,
    classify_item,
    closed_summary,
    compute_month_status,
    is_open_obligation,
    render_closed_summary,
)
from .obligations import (
    AUTO_RENEWS,
    DEFAULT_DUE_SOON_DAYS,
    RENEWAL_KINDS,
    ConfirmationFinding,
    DueItem,
    EvidenceFact,
    Issuer,
    ObligationFinding,
    ProofKind,
    Satisfaction,
    VerificationCondition,
    detect_confirmation,
    detect_obligation,
    due_soon,
    normalize_reference,
    proof_for,
    satisfy,
)
from .package import (
    MANIFEST_SCHEMA,
    PT_EXCEL,
    STANDARD_CSV,
    AccountantPackage,
    CsvFormat,
    DeliveryChannel,
    DeliveryError,
    DeliveryState,
    DocumentRef,
    EvidenceIntegrityError,
    EvidenceReader,
    ObjectStoreLike,
    OpenQuestion,
    PackageDelivery,
    PackageEntry,
    StoredEvidenceReader,
    build_package,
    verify_package,
)
from .period import Month

__all__ = [
    "AUDIT_DAYS",
    "AUTO_RENEWS",
    "DEFAULT_DUE_SOON_DAYS",
    "RENEWAL_KINDS",
    "MANIFEST_SCHEMA",
    "PT_EXCEL",
    "STANDARD_CSV",
    "STEP_OFFSETS",
    "TAX_OBLIGATION_KINDS",
    "AccountantPackage",
    "AccountantQuestion",
    "ActivationCondition",
    "ActivationReport",
    "ActivationSignals",
    "Activity",
    "ActivityKind",
    "Actor",
    "Anomaly",
    "Blocker",
    "BlockerKind",
    "BusinessAuditFindings",
    "BusinessAuditReport",
    "ClientMonth",
    "ClientRow",
    "ClientView",
    "CloseProgress",
    "CloseSchedule",
    "CloseSummary",
    "ClosedSummaryText",
    "CompanyLine",
    "ConfirmationFinding",
    "ConnectorCoverage",
    "CsvFormat",
    "CustomerSuccessReport",
    "DeliveryChannel",
    "DeliveryError",
    "DeliveryState",
    "DocumentRef",
    "DueItem",
    "EvidenceDecision",
    "EvidenceFact",
    "EvidenceIntegrityError",
    "EvidenceReader",
    "EvidenceRow",
    "ExportState",
    "ExportStatus",
    "HomeSummary",
    "InteractionKind",
    "Issuer",
    "ItemCounts",
    "ItemState",
    "Metric",
    "MetricResult",
    "MissingDocument",
    "Month",
    "MonthNotClosed",
    "MonthState",
    "MonthStatus",
    "ObjectStoreLike",
    "ObligationFinding",
    "OpenQuestion",
    "OwnerInteraction",
    "PackageDelivery",
    "PackageEntry",
    "PriceIncrease",
    "ProofKind",
    "QuestionDirection",
    "QuestionStatus",
    "Satisfaction",
    "ScheduleStatus",
    "StepKind",
    "StepPlan",
    "StepState",
    "StepStatus",
    "StoredEvidenceReader",
    "TaxFlag",
    "VerificationCondition",
    "audit_window",
    "build_client_view",
    "build_home_table",
    "build_package",
    "classify_item",
    "client_row",
    "closed_summary",
    "compute_month_status",
    "customer_success",
    "detect_confirmation",
    "detect_obligation",
    "due_soon",
    "evaluate_activation",
    "evaluate_schedule",
    "home_summary",
    "is_open_obligation",
    "is_owner_actor",
    "needs_accountant",
    "normalize_reference",
    "owner_touched",
    "plan_month_end",
    "proof_for",
    "render_business_audit",
    "render_closed_summary",
    "satisfy",
    "to_jsonable",
    "verify_package",
]
