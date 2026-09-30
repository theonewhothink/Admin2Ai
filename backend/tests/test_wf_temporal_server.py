"""The three key scenarios against a real Temporal server (§45).

Temporal's test servers are binaries fetched on first use from
temporal.download. Order of preference:

1. ``WorkflowEnvironment.start_time_skipping()`` (real 6-day waits, skipped),
   or an existing binary via ``TEMPORAL_TEST_SERVER_PATH``;
2. ``WorkflowEnvironment.start_local()`` (dev server; durations scaled down to
   seconds), or an existing CLI via ``TEMPORAL_CLI_PATH``;
3. otherwise the tests are skipped with the reason. The same scenarios always
   run offline in ``test_wf_sim_*.py`` (real SDK runtime, synthetic history).
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from datetime import date, timedelta
from decimal import Decimal

import pytest
from temporalio.client import WorkflowExecutionStatus
from temporalio.testing import WorkflowEnvironment

from backoffice.domain.models import Quality
from backoffice.workflows import (
    DATA_CONVERTER,
    ActorKind,
    ApprovalCategory,
    ApprovalDecisionKind,
    ApprovalInput,
    ApprovalSignal,
    ApprovalStatus,
    ChasePolicy,
    DeliveryConfirmation,
    DocumentArrival,
    HardApprovalWorkflow,
    ItemStatus,
    MissingInvoiceInput,
    MissingInvoiceWorkflow,
    MonthCloseInput,
    MonthCloseWorkflow,
    MonthStep,
    Period,
    TransactionRef,
    build_worker,
    missing_invoice_workflow_id,
)
from backoffice.workflows.contracts import ApproverCheck, MatchOutcome, VerificationOutcome
from backoffice.workflows.testing import ScriptedServices

TASK_QUEUE = "backoffice-test"
_PROBE: dict[str, object] = {}


async def _try_start(kind: str) -> WorkflowEnvironment:
    if kind == "time_skipping":
        return await WorkflowEnvironment.start_time_skipping(
            data_converter=DATA_CONVERTER,
            test_server_existing_path=os.environ.get("TEMPORAL_TEST_SERVER_PATH"),
        )
    return await WorkflowEnvironment.start_local(
        data_converter=DATA_CONVERTER,
        dev_server_existing_path=os.environ.get("TEMPORAL_CLI_PATH"),
    )


def _available_kind() -> str:
    """Probe once per session which environment can start; skip if none."""
    if "kind" not in _PROBE:
        errors: list[str] = []

        async def probe() -> str | None:
            for kind in ("time_skipping", "local"):
                try:
                    env = await _try_start(kind)
                except Exception as err:  # download blocked, no binary, ...
                    errors.append(f"{kind}: {err}")
                    continue
                await env.shutdown()
                return kind
            return None

        _PROBE["kind"] = asyncio.run(probe())
        _PROBE["errors"] = errors
    if _PROBE["kind"] is None:
        pytest.skip(
            "No Temporal server could be started here "
            f"({'; '.join(_PROBE['errors'])}). "  # type: ignore[arg-type]
            "Set TEMPORAL_TEST_SERVER_PATH or TEMPORAL_CLI_PATH to a local binary to run "
            "these. The same scenarios run offline in tests/test_wf_sim_*.py."
        )
    return str(_PROBE["kind"])


async def _eventually(predicate: Callable[[], bool], timeout: float = 20.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.05)


async def _pass_time(env: WorkflowEnvironment, delta: timedelta) -> None:
    if env.supports_time_skipping:
        await env.sleep(delta)
    else:
        await asyncio.sleep(delta.total_seconds())


def _scale(env: WorkflowEnvironment, real: timedelta, local: timedelta) -> timedelta:
    return real if env.supports_time_skipping else local


TX = TransactionRef(
    tenant_id="t1",
    transaction_id="tx-117",
    booked_on=date(2026, 9, 18),
    amount=Decimal("-117.20"),
    counterparty="VODAFONE",
    supplier_name="Vodafone",
)


def test_chase_wait_six_days_remind_then_reply_closes_on_a_real_server():
    kind = _available_kind()

    async def scenario():
        svc = ScriptedServices()
        svc.verifications["ev-reply"] = VerificationOutcome(
            quality=Quality.GREEN, document_id="doc-1", evidence_ids=("ev-reply",)
        )
        svc.matches["doc-1"] = MatchOutcome(
            matched=True, quality=Quality.GREEN, evidence_ids=("ev-bank",)
        )
        async with await _try_start(kind) as env:
            wait = _scale(env, timedelta(days=6), timedelta(seconds=3))
            policy = ChasePolicy(
                reply_wait=wait,
                thread_check_interval=_scale(env, timedelta(hours=12), timedelta(seconds=1)),
            )
            async with build_worker(env.client, TASK_QUEUE, svc.services()):
                handle = await env.client.start_workflow(
                    MissingInvoiceWorkflow.run,
                    MissingInvoiceInput(transaction=TX, policy=policy),
                    id=missing_invoice_workflow_id("t1", "tx-117"),
                    task_queue=TASK_QUEUE,
                )
                await _eventually(lambda: len(svc.sent_messages) == 1)
                await _pass_time(
                    env, wait + _scale(env, timedelta(minutes=1), timedelta(seconds=1))
                )
                await _eventually(lambda: len(svc.sent_messages) == 2)
                assert svc.sent_messages[1].kind.value == "reminder"
                status = await handle.query(MissingInvoiceWorkflow.current_status)
                assert status.startswith("I reminded Vodafone")
                await handle.signal(
                    MissingInvoiceWorkflow.document_received,
                    DocumentArrival(evidence_ids=("ev-reply",)),
                )
                result = await handle.result()
        assert result.status is ItemStatus.CLOSED
        assert result.messages_sent == 2
        assert svc.outcomes[0].quality is Quality.GREEN

    asyncio.run(scenario())


def test_approval_never_auto_approves_on_a_real_server():
    kind = _available_kind()

    async def scenario():
        svc = ScriptedServices()
        svc.approvers["sess-ana"] = ApproverCheck(
            verified=True, display_name="Ana", auth_method="passkey"
        )
        async with await _try_start(kind) as env:
            first = _scale(env, timedelta(days=1), timedelta(seconds=1))
            inp = ApprovalInput(
                tenant_id="t1", request_id="pay-1", category=ApprovalCategory.MONEY_MOVEMENT,
                summary="Pay €1,200.00 to Hazel Tree Lda.", amount=Decimal("1200.00"),
                evidence_ids=("ev-inv",), requested_by="payments-agent",
                first_reminder_after=first, max_reminder_interval=first * 2,
            )  # fmt: skip
            async with build_worker(env.client, TASK_QUEUE, svc.services()):
                handle = await env.client.start_workflow(
                    HardApprovalWorkflow.run, inp, id="approval:pay-1", task_queue=TASK_QUEUE
                )
                await _pass_time(env, _scale(env, timedelta(days=30), timedelta(seconds=5)))
                description = await handle.describe()
                assert description.status is WorkflowExecutionStatus.RUNNING
                assert len(svc.approval_requests) >= 3  # reminders, never a decision
                await handle.signal(
                    HardApprovalWorkflow.approve,
                    ApprovalSignal(
                        decision=ApprovalDecisionKind.APPROVE, actor_id="agent-1",
                        actor_kind=ActorKind.AGENT, assertion_id="x",
                    ),
                )  # fmt: skip
                await _eventually(lambda: len(svc.approval_events) == 1)
                assert (await handle.describe()).status is WorkflowExecutionStatus.RUNNING
                await handle.signal(
                    HardApprovalWorkflow.approve,
                    ApprovalSignal(
                        decision=ApprovalDecisionKind.APPROVE, actor_id="ana",
                        actor_kind=ActorKind.HUMAN, assertion_id="sess-ana",
                    ),
                )  # fmt: skip
                result = await handle.result()
        assert result.status is ApprovalStatus.APPROVED
        assert result.record.actor_id == "ana"
        assert result.refused_attempts == 1

    asyncio.run(scenario())


def test_month_close_runs_its_steps_in_order_on_a_real_server():
    kind = _available_kind()

    async def scenario():
        svc = ScriptedServices()
        svc.delivery_confirmations = [DeliveryConfirmation(confirmed=True, evidence_id="ev-ok")]
        async with await _try_start(kind) as env:
            now = await env.get_current_time()
            # Time-skipping: schedule in the future and let the server skip.
            # Local: schedule in the past so every step is due (catch-up path).
            offset = 8 if env.supports_time_skipping else -3
            day_zero = (now + timedelta(days=offset)).date()
            month_before = day_zero.replace(day=1) - timedelta(days=1)  # package after month end
            period = Period(year=month_before.year, month=month_before.month)
            inp = MonthCloseInput(tenant_id="t1", entity_id="e1", period=period, day_zero=day_zero)
            async with build_worker(env.client, TASK_QUEUE, svc.services()):
                handle = await env.client.start_workflow(
                    MonthCloseWorkflow.run, inp, id="month-close:t1:e1", task_queue=TASK_QUEUE
                )
                result = await handle.result()
        assert [r.step for r in result.steps] == list(MonthStep)
        ran = [r.ran_at for r in result.steps]
        assert ran == sorted(ran)
        assert result.headline.endswith("is closed.")
        audit_calls = [n for n in svc.names() if n == "run_completeness_audit"]
        assert len(audit_calls) == 3

    asyncio.run(scenario())
