"""Temporal activities: thin, typed wrappers around injected services (§45-46).

Each activity delegates to exactly one method of a service Protocol from
:mod:`.services`; implementations are injected when the worker is built, so
the workflows stay deterministic and the engine stays testable.

Error mapping:

* :class:`~.services.PermanentServiceError` -> non-retryable
  ``ApplicationError(type="PermanentServiceError")``;
* a service returning the wrong type or breaking a contract ->
  non-retryable ``ApplicationError(type="ContractViolation")``;
* anything else -> retried by Temporal with :data:`DEFAULT_RETRY` (durably,
  for as long as it takes, §45).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any, TypeVar

from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

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
from .services import PermanentServiceError, WorkflowServices

__all__ = [
    "ACTIVITY_NAMES",
    "ACTIVITY_TIMEOUTS",
    "CONTRACT_VIOLATION",
    "DEFAULT_RETRY",
    "PERMANENT_FAILURE",
    "BackofficeActivities",
    "activity_options",
    "execute_or",
    "is_permanent_failure",
]

PERMANENT_FAILURE = "PermanentServiceError"
CONTRACT_VIOLATION = "ContractViolation"

DEFAULT_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=10),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(hours=1),
    maximum_attempts=0,  # unlimited: a durable wait outlives an outage (§45)
    non_retryable_error_types=[PERMANENT_FAILURE, CONTRACT_VIOLATION],
)

# start_to_close per activity. Generous where OCR or portal navigation runs.
ACTIVITY_TIMEOUTS: dict[str, timedelta] = {
    "search_for_evidence": timedelta(minutes=30),
    "ingest_and_verify": timedelta(minutes=30),
    "reconcile_transaction": timedelta(minutes=5),
    "is_action_authorized": timedelta(minutes=1),
    "send_supplier_request": timedelta(minutes=5),
    "check_thread_for_reply": timedelta(minutes=5),
    "notify_owner": timedelta(minutes=1),
    "resolve_owner_item": timedelta(minutes=1),
    "request_approval": timedelta(minutes=1),
    "verify_approver": timedelta(minutes=1),
    "record_approval_event": timedelta(minutes=1),
    "record_item_outcome": timedelta(minutes=1),
    "run_completeness_audit": timedelta(minutes=30),
    "prepare_accountant_package": timedelta(minutes=30),
    "deliver_accountant_package": timedelta(minutes=10),
    "confirm_delivery": timedelta(minutes=5),
    "handle_accountant_query": timedelta(minutes=10),
    "evaluate_closure": timedelta(minutes=10),
}
ACTIVITY_NAMES: tuple[str, ...] = tuple(ACTIVITY_TIMEOUTS)


def activity_options(name: str) -> dict[str, Any]:
    """Keyword arguments for ``workflow.execute_activity_method``."""
    return {"start_to_close_timeout": ACTIVITY_TIMEOUTS[name], "retry_policy": DEFAULT_RETRY}


async def execute_or(fn: Callable[..., Awaitable[T]], arg: Any, fallback: T) -> T:
    """Run activity ``fn`` from workflow code; a permanent service failure
    yields ``fallback`` instead of failing the workflow.

    For steps where one bad outside input (a vanished thread, a bounced
    address, a garbage sign-in assertion) must not end a workflow that waits
    for days (§45). The fallback must be the safe reading: not sent, not
    verified, not confirmed. Transient failures still retry durably.
    """
    try:
        return await workflow.execute_activity_method(fn, arg, **activity_options(fn.__name__))
    except ActivityError as err:
        if not is_permanent_failure(err):
            raise
        workflow.logger.warning("%s failed permanently; using the safe fallback", fn.__name__)
        return fallback


def is_permanent_failure(err: BaseException) -> bool:
    """True for an activity that failed with :class:`PermanentServiceError`.

    Workflows use it to degrade gracefully where the golden rule allows it
    (unreadable evidence is simply not evidence); contract violations and
    cancellations are never matched and still surface.
    """
    cause = err.cause if isinstance(err, ActivityError) else None
    return isinstance(cause, ApplicationError) and cause.type == PERMANENT_FAILURE


T = TypeVar("T")


async def _call(awaitable: Awaitable[Any], expected: type[T] | None) -> T:
    try:
        result = await awaitable
    except PermanentServiceError as err:
        raise ApplicationError(
            str(err) or "permanent service failure",
            type=PERMANENT_FAILURE,
            non_retryable=True,
        ) from err
    if expected is not None and not isinstance(result, expected):
        raise ApplicationError(
            f"service returned {type(result).__name__}, expected {expected.__name__}",
            type=CONTRACT_VIOLATION,
            non_retryable=True,
        )
    return result


def _violation(message: str) -> ApplicationError:
    return ApplicationError(message, type=CONTRACT_VIOLATION, non_retryable=True)


class BackofficeActivities:
    """All workflow activities, bound to one set of injected services.

    Method names equal activity names (see :data:`ACTIVITY_NAMES`).
    """

    def __init__(self, services: WorkflowServices) -> None:
        if not isinstance(services, WorkflowServices):
            raise TypeError("services must be a WorkflowServices")
        self._s = services

    def definitions(self) -> list[Callable[..., Any]]:
        """Bound activity callables to register on a worker."""
        return [getattr(self, name) for name in ACTIVITY_NAMES]

    # ---------- missing invoice (§22)

    @activity.defn(name="search_for_evidence")
    async def search_for_evidence(self, request: SearchRequest) -> SearchOutcome:
        return await _call(self._s.evidence.search_for_evidence(request), SearchOutcome)

    @activity.defn(name="ingest_and_verify")
    async def ingest_and_verify(self, request: VerifyRequest) -> VerificationOutcome:
        return await _call(self._s.documents.ingest_and_verify(request), VerificationOutcome)

    @activity.defn(name="reconcile_transaction")
    async def reconcile_transaction(self, request: ReconcileRequest) -> MatchOutcome:
        return await _call(self._s.reconciler.reconcile_transaction(request), MatchOutcome)

    @activity.defn(name="is_action_authorized")
    async def is_action_authorized(self, request: AuthorizationRequest) -> AuthorizationDecision:
        return await _call(self._s.policy.is_action_authorized(request), AuthorizationDecision)

    @activity.defn(name="send_supplier_request")
    async def send_supplier_request(self, request: SupplierRequest) -> SupplierMessageReceipt:
        receipt = await _call(
            self._s.suppliers.send_supplier_request(request), SupplierMessageReceipt
        )
        if receipt.sent and not receipt.message_evidence_id:
            raise _violation("a sent supplier message must be stored as evidence")
        return receipt

    @activity.defn(name="check_thread_for_reply")
    async def check_thread_for_reply(self, request: ThreadCheckRequest) -> ThreadCheckResult:
        return await _call(self._s.suppliers.check_thread_for_reply(request), ThreadCheckResult)

    # ---------- owner (§35, §42)

    @activity.defn(name="notify_owner")
    async def notify_owner(self, item: NeedsYouItem) -> OwnerItemRef:
        return await _call(self._s.owner.notify_owner(item), OwnerItemRef)

    @activity.defn(name="resolve_owner_item")
    async def resolve_owner_item(self, resolution: OwnerItemResolution) -> None:
        await _call(self._s.owner.resolve_owner_item(resolution), None)

    @activity.defn(name="record_item_outcome")
    async def record_item_outcome(self, outcome: ItemOutcome) -> None:
        await _call(self._s.ledger.record_item_outcome(outcome), None)

    # ---------- hard approval (§25)

    @activity.defn(name="request_approval")
    async def request_approval(self, request: ApprovalRequest) -> OwnerItemRef:
        return await _call(self._s.approvals.request_approval(request), OwnerItemRef)

    @activity.defn(name="verify_approver")
    async def verify_approver(self, request: ApproverCheckRequest) -> ApproverCheck:
        return await _call(self._s.approvals.verify_approver(request), ApproverCheck)

    @activity.defn(name="record_approval_event")
    async def record_approval_event(self, event: ApprovalAuditEvent) -> None:
        await _call(self._s.approvals.record_approval_event(event), None)

    # ---------- month close (§27)

    @activity.defn(name="run_completeness_audit")
    async def run_completeness_audit(self, request: CompletenessRequest) -> CompletenessReport:
        report = await _call(
            self._s.month_close.run_completeness_audit(request), CompletenessReport
        )
        if any(g.tenant_id != request.tenant_id for g in report.gaps):
            raise _violation("the audit returned another tenant's transactions")
        if any(g.entity_id not in (None, request.entity_id) for g in report.gaps):
            # §51: one account, many companies, different rules. Chasing
            # another company's payment under this month would mix them up.
            raise _violation("the audit returned another company's transactions")
        return report

    @activity.defn(name="prepare_accountant_package")
    async def prepare_accountant_package(self, request: PackageRequest) -> PackageRef:
        return await _call(self._s.month_close.prepare_accountant_package(request), PackageRef)

    @activity.defn(name="deliver_accountant_package")
    async def deliver_accountant_package(self, request: DeliveryRequest) -> DeliveryReceipt:
        receipt = await _call(
            self._s.month_close.deliver_accountant_package(request), DeliveryReceipt
        )
        if receipt.delivered and not receipt.delivery_evidence_id:
            raise _violation("a delivery must be backed by evidence")
        return receipt

    @activity.defn(name="confirm_delivery")
    async def confirm_delivery(self, request: DeliveryCheckRequest) -> DeliveryConfirmation:
        return await _call(self._s.month_close.confirm_delivery(request), DeliveryConfirmation)

    @activity.defn(name="handle_accountant_query")
    async def handle_accountant_query(self, request: QueryHandlingRequest) -> QueryResolution:
        resolution = await _call(
            self._s.month_close.handle_accountant_query(request), QueryResolution
        )
        if resolution.query_id != request.query.query_id:
            raise _violation("the answer is for a different question")
        return resolution

    @activity.defn(name="evaluate_closure")
    async def evaluate_closure(self, request: ClosureRequest) -> ClosureVerdict:
        return await _call(self._s.month_close.evaluate_closure(request), ClosureVerdict)
