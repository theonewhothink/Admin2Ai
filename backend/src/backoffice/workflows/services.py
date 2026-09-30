"""Injectable service boundaries for the workflow activities (§45-46).

Workflows never talk to email, banks, storage or the database directly. Each
activity is a thin wrapper around one method of a Protocol below, and the
implementations are injected when the worker is built
(:func:`backoffice.workflows.worker.build_worker`). The engine stays pure and
testable; the integration step wires the real agents (§46: Retrieval,
Document, Verification, Reconciliation, Missing Evidence, Accountant, Closure,
Auditor) behind these Protocols.

Error contract for implementations:

* raise :class:`PermanentServiceError` when retrying cannot help (bad input,
  unknown tenant); the activity fails without retries;
* raise anything else for transient trouble; Temporal retries with backoff,
  durably, for as long as it takes (§45).

Where one bad outside input causes the permanent failure, the workflows take
the safe reading instead of failing: an unreadable document is unconfirmed,
an unreadable thread shows no reply, an undeliverable supplier address means
no contact, an unverifiable sign-in is a refused approval, an unhandleable
accountant question goes to the owner, and an undeliverable or unconfirmable
package is not delivered/confirmed. Everywhere else (search, audit, package
preparation, closure evaluation, owner inbox, ledger) a permanent failure
fails the run for an operator to fix, never silently.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Protocol, runtime_checkable

from .contracts import (
    ApprovalAuditEvent,
    ApprovalRequest,
    ApproverCheck,
    ApproverCheckRequest,
    AuthorizationDecision,
    AuthorizationRequest,
    ClosureRequest,
    ClosureVerdict,
    CompletenessReport,
    CompletenessRequest,
    DeliveryCheckRequest,
    DeliveryConfirmation,
    DeliveryReceipt,
    DeliveryRequest,
    ItemOutcome,
    MatchOutcome,
    NeedsYouItem,
    OwnerItemRef,
    OwnerItemResolution,
    PackageRef,
    PackageRequest,
    QueryHandlingRequest,
    QueryResolution,
    ReconcileRequest,
    SearchOutcome,
    SearchRequest,
    SupplierMessageReceipt,
    SupplierRequest,
    ThreadCheckRequest,
    ThreadCheckResult,
    VerificationOutcome,
    VerifyRequest,
)

__all__ = [
    "ApprovalDesk",
    "DocumentPipeline",
    "EvidenceFinder",
    "ItemLedger",
    "MonthCloseDesk",
    "OwnerInbox",
    "PermanentServiceError",
    "PolicyGate",
    "Reconciler",
    "SupplierMailbox",
    "WorkflowServices",
]


class PermanentServiceError(Exception):
    """A service failure that retrying cannot fix. Never shown to owners."""


@runtime_checkable
class EvidenceFinder(Protocol):
    """§22 search plan: current email, historical email, files, supplier
    portal, accounting platform, previous recurring sequence."""

    async def search_for_evidence(self, request: SearchRequest) -> SearchOutcome: ...


@runtime_checkable
class DocumentPipeline(Protocol):
    """Understand + Verify (§13-19): field-level verification, QR, conflicts."""

    async def ingest_and_verify(self, request: VerifyRequest) -> VerificationOutcome: ...


@runtime_checkable
class Reconciler(Protocol):
    """Match a verified document to its transaction (§20)."""

    async def reconcile_transaction(self, request: ReconcileRequest) -> MatchOutcome: ...


@runtime_checkable
class PolicyGate(Protocol):
    """§25 'automatic if authorized' check, asked right before acting."""

    async def is_action_authorized(
        self, request: AuthorizationRequest
    ) -> AuthorizationDecision: ...


@runtime_checkable
class SupplierMailbox(Protocol):
    """Sends supplier requests and watches their threads (§22).

    ``send_supplier_request`` must be idempotent on ``request.idempotency_key``.
    """

    async def send_supplier_request(self, request: SupplierRequest) -> SupplierMessageReceipt: ...

    async def check_thread_for_reply(self, request: ThreadCheckRequest) -> ThreadCheckResult: ...


@runtime_checkable
class OwnerInbox(Protocol):
    """Needs-You items (§35) and their resolution.

    ``notify_owner`` is idempotent on ``item.dedupe_key``; resolving an item
    twice is harmless (activities can be retried after an interruption).
    """

    async def notify_owner(self, item: NeedsYouItem) -> OwnerItemRef: ...

    async def resolve_owner_item(self, resolution: OwnerItemResolution) -> None: ...


@runtime_checkable
class ApprovalDesk(Protocol):
    """Hard approvals (§25): card + push, identity check, audit trail."""

    async def request_approval(self, request: ApprovalRequest) -> OwnerItemRef: ...

    async def verify_approver(self, request: ApproverCheckRequest) -> ApproverCheck: ...

    async def record_approval_event(self, event: ApprovalAuditEvent) -> None: ...


@runtime_checkable
class ItemLedger(Protocol):
    """Persists an item's final lifecycle transition (§3, §55).

    Idempotent per ``(tenant_id, subject_id, status)``: the same outcome may be
    delivered again if the workflow was interrupted while recording it.
    """

    async def record_item_outcome(self, outcome: ItemOutcome) -> None: ...


@runtime_checkable
class MonthCloseDesk(Protocol):
    """Month-end operations (§27-28)."""

    async def run_completeness_audit(self, request: CompletenessRequest) -> CompletenessReport: ...

    async def prepare_accountant_package(self, request: PackageRequest) -> PackageRef: ...

    async def deliver_accountant_package(self, request: DeliveryRequest) -> DeliveryReceipt: ...

    async def confirm_delivery(self, request: DeliveryCheckRequest) -> DeliveryConfirmation: ...

    async def handle_accountant_query(self, request: QueryHandlingRequest) -> QueryResolution: ...

    async def evaluate_closure(self, request: ClosureRequest) -> ClosureVerdict: ...


_EXPECTED: dict[str, type] = {
    "evidence": EvidenceFinder,
    "documents": DocumentPipeline,
    "reconciler": Reconciler,
    "policy": PolicyGate,
    "suppliers": SupplierMailbox,
    "owner": OwnerInbox,
    "approvals": ApprovalDesk,
    "ledger": ItemLedger,
    "month_close": MonthCloseDesk,
}


@dataclass(frozen=True)
class WorkflowServices:
    """Everything the activities need. Validated at construction so a wiring
    mistake fails at worker start-up, not days into a waiting workflow."""

    evidence: EvidenceFinder
    documents: DocumentPipeline
    reconciler: Reconciler
    policy: PolicyGate
    suppliers: SupplierMailbox
    owner: OwnerInbox
    approvals: ApprovalDesk
    ledger: ItemLedger
    month_close: MonthCloseDesk

    def __post_init__(self) -> None:
        for f in fields(self):
            protocol = _EXPECTED[f.name]
            if not isinstance(getattr(self, f.name), protocol):
                raise TypeError(f"services.{f.name} does not implement {protocol.__name__}")
