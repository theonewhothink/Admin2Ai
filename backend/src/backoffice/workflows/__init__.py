"""Durable orchestration with Temporal (§22, §25, §27, §45-46).

A deterministic orchestrator drives the agents through long waits: request an
invoice, wait six days, remind, escalate; hold a payment until a verified
person approves it; run the month-end schedule until the month is closed with
evidence. Business rules live in pure functions (:mod:`.decisions`); the
Temporal workflows only interpret them; every side effect is an activity that
calls an injected service.

Public API
----------
Workflows (start them with a client built by :func:`connect_client`)
    ``MissingInvoiceWorkflow``  input ``MissingInvoiceInput``  -> ``MissingInvoiceResult``
        signals: ``document_received(DocumentArrival)``, ``owner_answered(OwnerAnswer)``,
        ``cancel(CancelRequest)``; queries: ``current_status() -> str``, ``progress()``.
        Workflow id: ``missing_invoice_workflow_id(tenant_id, transaction_id)``.
    ``HardApprovalWorkflow``  input ``ApprovalInput`` -> ``ApprovalResult``
        signals: ``approve(ApprovalSignal)``, ``reject(ApprovalSignal)``,
        ``withdraw(WithdrawRequest)``; queries: ``current_status()``, ``progress()``.
    ``MonthCloseWorkflow``  input ``MonthCloseInput`` -> ``MonthCloseResult``
        signals: ``accountant_query(AccountantQuery)``,
        ``delivery_confirmed(DeliveryConfirmation)``, ``send_package(SendPackageRequest)``,
        ``item_resolved(ItemResolved)``; queries: ``current_status()``, ``progress()``.
        Package card options map to signals: ``send_for_me`` -> ``send_package``;
        ``sent_myself`` / ``they_have_it`` -> ``delivery_confirmed`` with the stored
        answer as evidence. An accountant question closes only when
        ``handle_accountant_query`` answers it with evidence; after the owner
        answers its card, signal ``item_resolved`` so the month asks again.

Wiring
    ``WorkflowServices(evidence=, documents=, reconciler=, policy=, suppliers=,
    owner=, approvals=, ledger=, month_close=)`` — one implementation per
    Protocol in :mod:`.services`; raise ``PermanentServiceError`` for
    failures retrying cannot fix.
    ``build_worker(client, task_queue, services, **worker_kwargs) -> Worker``
    ``connect_client(WorkerSettings) -> Client`` (uses ``DATA_CONVERTER``)
    ``WorkerSettings.from_env(os.environ)``; CLI: ``python -m backoffice.workflows.worker``

Contracts: every input/result/signal type is exported from :mod:`.contracts`.
Offline testing without a Temporal server: :mod:`backoffice.workflows.testing`.

Exports are resolved lazily so that importing a workflow module inside the
Temporal sandbox does not import the worker or client machinery.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

_MODULE_EXPORTS: dict[str, tuple[str, ...]] = {
    "missing_invoice": ("MissingInvoiceWorkflow",),
    "approval": ("HardApprovalWorkflow",),
    "month_close": ("MonthCloseWorkflow",),
    "activities": ("ACTIVITY_NAMES", "BackofficeActivities"),
    "services": (
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
    ),
    "worker": (
        "DATA_CONVERTER",
        "DEFAULT_TASK_QUEUE",
        "WORKFLOWS",
        "ConfigError",
        "WorkerSettings",
        "build_worker",
        "connect_client",
        "run_worker",
        "worker_options",
    ),
    "decisions": ("missing_invoice_workflow_id", "step_schedule"),
    "contracts": (
        "DEFAULT_DAY_OFFSETS",
        "AccountantQuery",
        "ActorKind",
        "ApprovalCategory",
        "ApprovalDecisionKind",
        "ApprovalInput",
        "ApprovalResult",
        "ApprovalSignal",
        "ApprovalStatus",
        "CancelRequest",
        "ChasePolicy",
        "DeliveryConfirmation",
        "DocumentArrival",
        "GatedAction",
        "ItemResolved",
        "ItemStatus",
        "MissingInvoiceInput",
        "MissingInvoiceResult",
        "MonthCloseInput",
        "MonthCloseResult",
        "MonthStep",
        "NeedsYouItem",
        "OwnerAnswer",
        "OwnerAnswerKind",
        "Period",
        "SendPackageRequest",
        "TransactionRef",
        "WithdrawRequest",
    ),
}
_EXPORTS: dict[str, str] = {
    name: module for module, names in _MODULE_EXPORTS.items() for name in names
}

__all__ = [
    "ACTIVITY_NAMES",
    "AccountantQuery",
    "ActorKind",
    "ApprovalCategory",
    "ApprovalDecisionKind",
    "ApprovalDesk",
    "ApprovalInput",
    "ApprovalResult",
    "ApprovalSignal",
    "ApprovalStatus",
    "BackofficeActivities",
    "CancelRequest",
    "ChasePolicy",
    "ConfigError",
    "DATA_CONVERTER",
    "DEFAULT_DAY_OFFSETS",
    "DEFAULT_TASK_QUEUE",
    "DeliveryConfirmation",
    "DocumentArrival",
    "DocumentPipeline",
    "EvidenceFinder",
    "GatedAction",
    "HardApprovalWorkflow",
    "ItemLedger",
    "ItemResolved",
    "ItemStatus",
    "MissingInvoiceInput",
    "MissingInvoiceResult",
    "MissingInvoiceWorkflow",
    "MonthCloseDesk",
    "MonthCloseInput",
    "MonthCloseResult",
    "MonthCloseWorkflow",
    "MonthStep",
    "NeedsYouItem",
    "OwnerAnswer",
    "OwnerAnswerKind",
    "OwnerInbox",
    "Period",
    "PermanentServiceError",
    "PolicyGate",
    "Reconciler",
    "SendPackageRequest",
    "SupplierMailbox",
    "TransactionRef",
    "WORKFLOWS",
    "WithdrawRequest",
    "WorkerSettings",
    "WorkflowServices",
    "build_worker",
    "connect_client",
    "missing_invoice_workflow_id",
    "run_worker",
    "step_schedule",
    "worker_options",
]  # literal so tools see the re-exports; kept equal to _EXPORTS by a test


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(f"{__name__}.{module}"), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_EXPORTS))


if TYPE_CHECKING:  # pragma: no cover - static analysers only
    from .activities import ACTIVITY_NAMES, BackofficeActivities
    from .approval import HardApprovalWorkflow
    from .contracts import (
        DEFAULT_DAY_OFFSETS,
        AccountantQuery,
        ActorKind,
        ApprovalCategory,
        ApprovalDecisionKind,
        ApprovalInput,
        ApprovalResult,
        ApprovalSignal,
        ApprovalStatus,
        CancelRequest,
        ChasePolicy,
        DeliveryConfirmation,
        DocumentArrival,
        GatedAction,
        ItemResolved,
        ItemStatus,
        MissingInvoiceInput,
        MissingInvoiceResult,
        MonthCloseInput,
        MonthCloseResult,
        MonthStep,
        NeedsYouItem,
        OwnerAnswer,
        OwnerAnswerKind,
        Period,
        SendPackageRequest,
        TransactionRef,
        WithdrawRequest,
    )
    from .decisions import missing_invoice_workflow_id, step_schedule
    from .missing_invoice import MissingInvoiceWorkflow
    from .month_close import MonthCloseWorkflow
    from .services import (
        ApprovalDesk,
        DocumentPipeline,
        EvidenceFinder,
        ItemLedger,
        MonthCloseDesk,
        OwnerInbox,
        PermanentServiceError,
        PolicyGate,
        Reconciler,
        SupplierMailbox,
        WorkflowServices,
    )
    from .worker import (
        DATA_CONVERTER,
        DEFAULT_TASK_QUEUE,
        WORKFLOWS,
        ConfigError,
        WorkerSettings,
        build_worker,
        connect_client,
        run_worker,
        worker_options,
    )
