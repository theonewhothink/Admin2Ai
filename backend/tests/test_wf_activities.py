"""Activities are thin, typed wrappers around injected services (§45-46)."""

from __future__ import annotations

import asyncio
import inspect
from datetime import date
from decimal import Decimal

import pytest
from temporalio import activity
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from backoffice.domain.models import Quality
from backoffice.workflows import ACTIVITY_NAMES, BackofficeActivities, WorkflowServices
from backoffice.workflows.activities import (
    ACTIVITY_TIMEOUTS,
    CONTRACT_VIOLATION,
    DEFAULT_RETRY,
    PERMANENT_FAILURE,
    activity_options,
)
from backoffice.workflows.contracts import (
    AccountantQuery,
    CompletenessReport,
    CompletenessRequest,
    DeliveryReceipt,
    DeliveryRequest,
    PackageRef,
    Period,
    QueryHandlingRequest,
    QueryResolution,
    SearchOutcome,
    SearchRequest,
    SupplierMessageKind,
    SupplierMessageReceipt,
    SupplierRequest,
    TransactionRef,
    VerificationOutcome,
    VerifyRequest,
)
from backoffice.workflows.services import PermanentServiceError
from backoffice.workflows.testing import ScriptedServices

TX = TransactionRef(
    tenant_id="t1",
    transaction_id="tx-1",
    booked_on=date(2026, 9, 18),
    amount=Decimal("-117.20"),
    counterparty="VODAFONE",
)
SEPT = Period(year=2026, month=9)


def call(fn, arg):
    return asyncio.run(ActivityEnvironment().run(fn, arg))


def acts(svc: ScriptedServices) -> BackofficeActivities:
    return BackofficeActivities(svc.services())


def test_every_activity_is_registered_under_its_method_name():
    definitions = acts(ScriptedServices()).definitions()
    names = [activity._Definition.must_from_callable(fn).name for fn in definitions]
    assert names == list(ACTIVITY_NAMES)
    assert [fn.__name__ for fn in definitions] == list(ACTIVITY_NAMES)
    assert all(inspect.iscoroutinefunction(fn) for fn in definitions)


def test_the_activities_the_task_names_exist():
    for name in (
        "search_for_evidence",
        "send_supplier_request",
        "check_thread_for_reply",
        "ingest_and_verify",
        "reconcile_transaction",
        "notify_owner",
        "request_approval",
        "deliver_accountant_package",
    ):
        assert name in ACTIVITY_NAMES


def test_options_have_a_timeout_and_a_durable_retry_policy():
    for name in ACTIVITY_NAMES:
        options = activity_options(name)
        assert options["start_to_close_timeout"] == ACTIVITY_TIMEOUTS[name]
        assert options["retry_policy"] is DEFAULT_RETRY
    assert DEFAULT_RETRY.maximum_attempts == 0  # unlimited: outlive outages (§45)
    assert set(DEFAULT_RETRY.non_retryable_error_types) == {PERMANENT_FAILURE, CONTRACT_VIOLATION}
    with pytest.raises(KeyError):
        activity_options("unknown")


def test_activities_delegate_to_the_services_unchanged():
    svc = ScriptedServices()
    svc.search_results = [SearchOutcome(evidence_ids=("ev-1",))]
    svc.verifications["ev-1"] = VerificationOutcome(
        quality=Quality.GREEN, document_id="d", evidence_ids=("ev-1",)
    )
    a = acts(svc)
    request = SearchRequest(transaction=TX)
    assert call(a.search_for_evidence, request).evidence_ids == ("ev-1",)
    verified = call(a.ingest_and_verify, VerifyRequest(transaction=TX, evidence_ids=("ev-1",)))
    assert verified.quality is Quality.GREEN
    assert svc.calls[0] == ("search_for_evidence", request)


def test_permanent_failures_are_not_retried():
    class Broken(ScriptedServices):
        async def search_for_evidence(self, request):
            raise PermanentServiceError("tenant does not exist")

    with pytest.raises(ApplicationError) as info:
        call(acts(Broken()).search_for_evidence, SearchRequest(transaction=TX))
    assert info.value.non_retryable and info.value.type == PERMANENT_FAILURE


def test_transient_failures_propagate_for_temporal_to_retry():
    class Flaky(ScriptedServices):
        async def search_for_evidence(self, request):
            raise ConnectionError("mail server busy")

    with pytest.raises(ConnectionError):
        call(acts(Flaky()).search_for_evidence, SearchRequest(transaction=TX))


def test_a_service_returning_the_wrong_type_is_a_contract_violation():
    class Sloppy(ScriptedServices):
        async def search_for_evidence(self, request):
            return {"evidence_ids": ["ev-1"]}

    with pytest.raises(ApplicationError) as info:
        call(acts(Sloppy()).search_for_evidence, SearchRequest(transaction=TX))
    assert info.value.type == CONTRACT_VIOLATION and info.value.non_retryable


def _supplier_request() -> SupplierRequest:
    return SupplierRequest(
        transaction=TX, kind=SupplierMessageKind.REQUEST, message_number=1,
        default_body="Hello", idempotency_key="k",
    )  # fmt: skip


def test_a_sent_supplier_message_must_be_kept_as_evidence():
    class NoEvidence(ScriptedServices):
        async def send_supplier_request(self, request):
            return SupplierMessageReceipt(sent=True, thread_id="th")

    with pytest.raises(ApplicationError, match="stored as evidence"):
        call(acts(NoEvidence()).send_supplier_request, _supplier_request())
    receipt = call(acts(ScriptedServices()).send_supplier_request, _supplier_request())
    assert receipt.sent and receipt.message_evidence_id


def test_an_audit_can_never_leak_another_tenants_transactions():
    svc = ScriptedServices()
    svc.audits = [
        CompletenessReport(
            transactions_checked=1,
            documents_collected=0,
            gaps=(TX.model_copy(update={"tenant_id": "t2"}),),
        )
    ]
    request = CompletenessRequest(tenant_id="t1", entity_id="e1", period=SEPT)
    with pytest.raises(ApplicationError, match="another tenant"):
        call(acts(svc).run_completeness_audit, request)


def test_a_delivery_needs_evidence():
    svc = ScriptedServices()
    svc.delivery = DeliveryReceipt(delivered=True)
    request = DeliveryRequest(
        tenant_id="t1", entity_id="e1", period=SEPT, idempotency_key="k",
        package=PackageRef(package_id="p", evidence_id="ev-p", complete_items=1, missing_items=0),
    )  # fmt: skip
    with pytest.raises(ApplicationError, match="evidence"):
        call(acts(svc).deliver_accountant_package, request)


def test_an_answer_must_belong_to_the_question_asked():
    svc = ScriptedServices()
    svc.query_resolutions["q1"] = QueryResolution(query_id="q-other", resolved=False)
    request = QueryHandlingRequest(
        tenant_id="t1", entity_id="e1", period=SEPT, query=AccountantQuery(query_id="q1", text="?")
    )
    with pytest.raises(ApplicationError, match="different question"):
        call(acts(svc).handle_accountant_query, request)


def test_services_are_checked_when_wired_not_days_later():
    svc = ScriptedServices()
    wiring = dict(
        evidence=svc, documents=svc, reconciler=svc, policy=svc, suppliers=svc,
        owner=svc, approvals=svc, ledger=svc, month_close=svc,
    )  # fmt: skip
    WorkflowServices(**wiring)
    with pytest.raises(TypeError, match=r"services\.suppliers"):
        WorkflowServices(**{**wiring, "suppliers": object()})
    with pytest.raises(TypeError):
        BackofficeActivities(svc)  # type: ignore[arg-type]
