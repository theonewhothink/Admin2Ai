"""MonthCloseWorkflow in the real SDK runtime, offline (§2, §27, §3).

Day 0 is 5 October 2026 here; steps run at 06:00 UTC on Day -7, -5, -3, 0,
+1, +2, then the closure evaluation.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

from backoffice.workflows import (
    AccountantQuery,
    ChasePolicy,
    DeliveryConfirmation,
    GatedAction,
    ItemResolved,
    ItemStatus,
    MonthCloseInput,
    MonthCloseWorkflow,
    MonthStep,
    Period,
    TransactionRef,
    missing_invoice_workflow_id,
)
from backoffice.workflows.contracts import (
    ClosureVerdict,
    CompletenessReport,
    DeliveryReceipt,
    MonthSummary,
    NeedsYouKind,
    QueryResolution,
)
from backoffice.workflows.testing import ScriptedServices, WorkflowSimulator
from backoffice.workflows.text import find_forbidden

DAY0 = date(2026, 10, 5)
START = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
SEPTEMBER = Period(year=2026, month=9)
WF_ID = "month-close:t1:e1:2026-09"


def at(offset: int) -> datetime:
    return datetime.combine(DAY0 + timedelta(days=offset), time(6, 0), tzinfo=timezone.utc)


def gap(tx_id: str, amount: str = "-39.00", who: str = "ADOBE") -> TransactionRef:
    return TransactionRef(
        tenant_id="t1",
        transaction_id=tx_id,
        booked_on=date(2026, 9, 12),
        amount=Decimal(amount),
        counterparty=who,
    )


def run(coro):
    return asyncio.run(coro)


def closed_verdict(**summary) -> ClosureVerdict:
    numbers = dict(
        transactions_checked=218,
        documents_collected=186,
        missing_documents_retrieved=14,
        suppliers_chased=7,
        accountant_questions_resolved=3,
        tax_obligations_verified=2,
        unresolved_issues=0,
        owner_minutes=4,
    )
    numbers.update(summary)
    return ClosureVerdict(
        closed=True, open_items=0, summary=MonthSummary(**numbers), evidence_ids=("ev-closure",)
    )


def open_verdict(n: int) -> ClosureVerdict:
    return ClosureVerdict(closed=False, open_items=n, summary=MonthSummary(unresolved_issues=n))


async def started(svc: ScriptedServices, start: datetime = START, **kw) -> WorkflowSimulator:
    sim = WorkflowSimulator.for_services(
        svc.services(), start_time=start, running_workflow_ids=kw.pop("running", ())
    )
    inp = MonthCloseInput(tenant_id="t1", entity_id="e1", period=SEPTEMBER, day_zero=DAY0, **kw)
    await sim.start(MonthCloseWorkflow.run, inp, id=WF_ID)
    return sim


def status(sim: WorkflowSimulator) -> str:
    text = sim.query(MonthCloseWorkflow.current_status)
    assert find_forbidden(text) == [], text
    return text


def test_month_close_runs_each_step_in_order_at_its_day_offset():
    async def scenario():
        svc = ScriptedServices()
        svc.audits = [
            CompletenessReport(
                transactions_checked=218, documents_collected=180, gaps=(gap("tx-a"),)
            )
        ]
        svc.delivery_confirmations = [
            DeliveryConfirmation(confirmed=True, evidence_id="ev-read-receipt")
        ]
        svc.verdicts = [closed_verdict()]
        sim = await started(svc)
        assert svc.calls == []  # nothing before Day -7
        assert status(sim) == "September: I'll check what's missing on 28 September."

        await sim.signal(
            MonthCloseWorkflow.accountant_query,
            AccountantQuery(query_id="q-1", text="Which company paid the Adobe bill?"),
        )
        result = await sim.run_until_complete()

        # Every step ran exactly when §27 says, in order.
        steps = [(r.step, r.ran_at) for r in result.steps]
        assert steps == [
            (MonthStep.COMPLETENESS_AUDIT, at(-7)),
            (MonthStep.EVIDENCE_RETRIEVAL, at(-5)),
            (MonthStep.SUPPLIER_CHASES, at(-3)),
            (MonthStep.PACKAGE, at(0)),
            (MonthStep.DELIVERY_CONFIRMATION, at(1)),
            (MonthStep.ACCOUNTANT_QUERIES, at(2)),
            (MonthStep.CLOSURE, at(2)),
        ]
        # The workflow slept (durable timers) from one step to the next.
        assert [t.fire_at for t in sim.timers] == [at(d) for d in (-7, -5, -3, 0, 1, 2)]
        # Activities in §27 order.
        assert [(c.name, c.at) for c in sim.activity_calls] == [
            ("run_completeness_audit", at(-7)),
            ("run_completeness_audit", at(-5)),
            ("run_completeness_audit", at(-3)),
            ("prepare_accountant_package", at(0)),
            ("is_action_authorized", at(0)),
            ("deliver_accountant_package", at(0)),
            ("confirm_delivery", at(1)),
            ("handle_accountant_query", at(2)),
            ("evaluate_closure", at(2)),
        ]
        # The gap got one chase, started on Day -5 and held until Day -3.
        [child] = sim.children
        assert child.workflow_type == "MissingInvoiceWorkflow"
        assert child.workflow_id == missing_invoice_workflow_id("t1", "tx-a")
        assert child.at == at(-5)
        assert child.arg.policy.chase_not_before == at(-3)
        assert child.arg.report_to_workflow_id == WF_ID
        # MONTH CLOSED, with the §2 summary.
        assert result.headline == "September is closed."
        assert result.summary_line == (
            "218 transactions checked · 186 documents collected · 14 missing documents "
            "retrieved automatically · 7 suppliers chased · 3 accountant questions resolved "
            "· 2 tax obligations verified · 0 unresolved issues. You spent 4 minutes."
        )
        assert set(result.evidence_ids) == {
            "ev-closure",
            "ev-delivery",
            "ev-read-receipt",
            "ev-package",
        }
        assert result.chases_started == 1
        assert status(sim) == "September is closed."
        await sim.verify_replay()

    run(scenario())


def test_late_start_catches_up_every_missed_step_in_order():
    async def scenario():
        svc = ScriptedServices()
        svc.delivery_confirmations = [DeliveryConfirmation(confirmed=True, evidence_id="ev-ok")]
        late = at(1) + timedelta(hours=3)
        sim = await started(svc, start=late)
        assert not sim.completed  # everything overdue ran; now it sleeps to Day +2
        assert [t.fire_at for t in sim.pending_timers()] == [at(2)]
        result = await sim.run_until_complete()
        assert [r.step for r in result.steps][:5] == [
            MonthStep.COMPLETENESS_AUDIT,
            MonthStep.EVIDENCE_RETRIEVAL,
            MonthStep.SUPPLIER_CHASES,
            MonthStep.PACKAGE,
            MonthStep.DELIVERY_CONFIRMATION,
        ]
        assert all(r.ran_at == late for r in result.steps[:5])
        assert result.steps[-1].ran_at == at(2)  # queries still wait for Day +2
        await sim.verify_replay()

    run(scenario())


def test_new_gap_found_on_day_minus_3_is_chased_at_once_and_old_gaps_are_not_restarted():
    async def scenario():
        svc = ScriptedServices()
        a, b = gap("tx-a"), gap("tx-b", "-92.40", "VODAFONE")
        svc.audits = [
            CompletenessReport(transactions_checked=10, documents_collected=9, gaps=(a,)),
            CompletenessReport(transactions_checked=11, documents_collected=10, gaps=(a,)),
            CompletenessReport(transactions_checked=12, documents_collected=10, gaps=(a, b)),
        ]
        sim = await started(svc)
        await sim.advance(at(-3) - START)
        assert [(c.workflow_id.split(":")[-1], c.at) for c in sim.children] == [
            ("tx-a", at(-5)),
            ("tx-b", at(-3)),
        ]
        assert sim.children[1].arg.policy.chase_not_before is None
        assert status(sim) == "September: still looking for 2 missing documents."

    run(scenario())


def test_a_chase_already_running_for_a_gap_is_not_duplicated():
    async def scenario():
        svc = ScriptedServices()
        svc.audits = [
            CompletenessReport(transactions_checked=5, documents_collected=4, gaps=(gap("tx-a"),))
        ]
        svc.delivery_confirmations = [DeliveryConfirmation(confirmed=True, evidence_id="ev-ok")]
        running = (missing_invoice_workflow_id("t1", "tx-a"),)
        sim = await started(svc, running=running)
        result = await sim.run_until_complete()
        [attempt] = sim.children
        assert attempt.already_running
        assert result.chases_started == 0
        await sim.verify_replay()

    run(scenario())


def test_month_stays_open_until_delivery_is_confirmed_with_evidence():
    async def scenario():
        svc = ScriptedServices()  # confirm_delivery answers "not yet"
        sim = await started(svc, closure_recheck_interval=timedelta(hours=6))
        await sim.advance(at(4) - START)
        assert not sim.completed
        cards = [c for c in svc.owner_items if c.kind is NeedsYouKind.DELIVERY_UNCONFIRMED]
        assert len(cards) == 1  # asked once, not every re-check
        assert (
            cards[0].headline
            == "I couldn't confirm your accountant received the September package."
        )
        assert cards[0].detail == "It was sent on 5 October. Could you check with them?"
        assert svc.names().count("evaluate_closure") > 3
        assert status(sim) == "September: I still need one thing."

        await sim.signal(
            MonthCloseWorkflow.delivery_confirmed,
            DeliveryConfirmation(
                confirmed=True, evidence_id="ev-owner-said-yes", confirmed_by="owner-1"
            ),
        )
        result = sim.result()
        assert result.headline == "September is closed."
        assert "ev-owner-said-yes" in result.evidence_ids
        await sim.verify_replay()

    run(scenario())


def test_open_items_keep_the_month_open_and_a_resolved_item_triggers_a_recheck_now():
    async def scenario():
        svc = ScriptedServices()
        svc.delivery_confirmations = [DeliveryConfirmation(confirmed=True, evidence_id="ev-ok")]
        svc.verdicts = [open_verdict(2), open_verdict(1), closed_verdict()]
        sim = await started(svc)
        await sim.advance(at(2) - START + timedelta(days=1))
        assert not sim.completed
        assert status(sim) == "September: I still need one thing."
        checks = sim.calls("evaluate_closure")
        assert [c.at for c in checks] == [at(2), at(3)]  # daily re-check
        # A chase finishing wakes the month up immediately.
        await sim.advance(timedelta(hours=1))
        await sim.signal(
            MonthCloseWorkflow.item_resolved,
            ItemResolved(subject_id="tx-a", status=ItemStatus.CLOSED),
        )
        assert sim.completed
        assert sim.calls("evaluate_closure")[-1].at == at(3) + timedelta(hours=1)
        await sim.verify_replay()

    run(scenario())


def test_delivery_not_authorized_asks_the_owner_and_waits_for_their_confirmation():
    async def scenario():
        svc = ScriptedServices()
        svc.authorized[GatedAction.DOCUMENT_DELIVERY] = False
        sim = await started(svc)
        await sim.advance(at(3) - START)
        assert "deliver_accountant_package" not in svc.names()
        assert "confirm_delivery" not in svc.names()
        [card] = svc.owner_items
        assert card.kind is NeedsYouKind.PACKAGE_READY
        assert card.detail == "I'm not allowed to send it for you yet."
        assert card.evidence_ids == ("ev-package",)
        await sim.signal(
            MonthCloseWorkflow.delivery_confirmed,
            DeliveryConfirmation(confirmed=True, evidence_id="ev-owner-forwarded"),
        )
        assert sim.result().headline == "September is closed."

    run(scenario())


def test_failed_delivery_is_explained_plainly():
    async def scenario():
        svc = ScriptedServices()
        svc.delivery = DeliveryReceipt(delivered=False)
        sim = await started(svc)
        await sim.advance(at(0) - START)
        [card] = svc.owner_items
        assert card.detail == "I couldn't send it to your accountant."

    run(scenario())


def test_unanswered_accountant_question_goes_to_the_owner_and_blocks_closing():
    async def scenario():
        svc = ScriptedServices()
        svc.delivery_confirmations = [DeliveryConfirmation(confirmed=True, evidence_id="ev-ok")]
        svc.query_resolutions["q-7"] = QueryResolution(
            query_id="q-7",
            resolved=False,
            owner_question="Was the €240.00 dinner on 9 September a business meal?",
        )
        sim = await started(svc)
        await sim.signal(
            MonthCloseWorkflow.accountant_query,
            AccountantQuery(
                query_id="q-7", text="Please confirm the nature of the 240 EUR expense."
            ),
        )
        await sim.advance(at(3) - START)
        assert not sim.completed
        [card] = [c for c in svc.owner_items if c.kind is NeedsYouKind.ACCOUNTANT_QUESTION]
        assert card.headline == "Your accountant has a question."
        assert card.detail == "Was the €240.00 dinner on 9 September a business meal?"
        closure_request = sim.calls("evaluate_closure")[-1].arg
        assert closure_request.open_query_ids == ("q-7",)
        # The owner answers through the app; the answer is stored as evidence,
        # so the question service can now resolve it. The signal wakes the
        # month, which asks again: only an evidenced answer closes it (§3).
        svc.query_resolutions["q-7"] = QueryResolution(
            query_id="q-7", resolved=True, evidence_ids=("ev-owner-answer",)
        )
        await sim.signal(
            MonthCloseWorkflow.item_resolved,
            ItemResolved(subject_id="q-7", status=ItemStatus.CLOSED),
        )
        assert sim.completed
        assert [r.message for r in svc.resolved_items] == ["Done."]  # card taken down
        await sim.verify_replay()

    run(scenario())


def test_duplicate_accountant_questions_are_handled_once():
    async def scenario():
        svc = ScriptedServices()
        svc.delivery_confirmations = [DeliveryConfirmation(confirmed=True, evidence_id="ev-ok")]
        sim = await started(svc)
        question = AccountantQuery(query_id="q-1", text="Receipt for the taxi?")
        await sim.signal(MonthCloseWorkflow.accountant_query, question)
        await sim.signal(MonthCloseWorkflow.accountant_query, question)
        await sim.run_until_complete()
        assert svc.names().count("handle_accountant_query") == 1

    run(scenario())


def test_long_wait_for_closure_continues_as_new_and_keeps_its_progress():
    async def scenario():
        svc = ScriptedServices()
        svc.delivery_confirmations = [DeliveryConfirmation(confirmed=True, evidence_id="ev-ok")]
        svc.verdicts = [open_verdict(1)] * 7 + [closed_verdict()]
        sim = await started(svc, rechecks_per_run=3)
        result = await sim.run_until_complete()
        assert sim.run_count == 3  # 3 + 3 re-checks, then closed in the third run
        assert [r.step for r in result.steps] == list(MonthStep)
        assert result.steps[0].ran_at == at(-7)  # carried across runs
        assert svc.names().count("run_completeness_audit") == 3  # no step re-ran
        assert svc.names().count("prepare_accountant_package") == 1
        await sim.verify_replay()

    run(scenario())


def test_progress_query_reports_the_next_step():
    async def scenario():
        svc = ScriptedServices()
        svc.audits = [CompletenessReport(transactions_checked=3, documents_collected=3)]
        sim = await started(svc)
        await sim.advance(at(-7) - START)
        progress = sim.query(MonthCloseWorkflow.progress)
        assert progress.steps_done == (MonthStep.COMPLETENESS_AUDIT,)
        assert progress.next_step is MonthStep.EVIDENCE_RETRIEVAL
        assert progress.next_step_at == at(-5)
        assert progress.status_text == "September: nothing missing so far."

    run(scenario())


def test_chase_policy_is_passed_to_the_children():
    async def scenario():
        svc = ScriptedServices()
        svc.audits = [
            CompletenessReport(transactions_checked=1, documents_collected=0, gaps=(gap("tx-z"),))
        ]
        policy = ChasePolicy(max_reminders=1, reply_wait=timedelta(days=4))
        sim = await started(svc, chase_policy=policy)
        await sim.advance(at(-5) - START)
        [child] = sim.children
        assert child.arg.policy.max_reminders == 1
        assert child.arg.policy.reply_wait == timedelta(days=4)

    run(scenario())


def test_continues_as_new_when_the_server_suggests_it_between_steps():
    async def scenario():
        svc = ScriptedServices()
        svc.audits = [
            CompletenessReport(transactions_checked=9, documents_collected=8, gaps=(gap("tx-a"),))
        ]
        svc.delivery_confirmations = [DeliveryConfirmation(confirmed=True, evidence_id="ev-ok")]
        sim = WorkflowSimulator.for_services(
            svc.services(), start_time=START, suggest_continue_as_new_after=25
        )
        inp = MonthCloseInput(tenant_id="t1", entity_id="e1", period=SEPTEMBER, day_zero=DAY0)
        await sim.start(MonthCloseWorkflow.run, inp, id=WF_ID)
        result = await sim.run_until_complete()
        assert sim.run_count > 1
        assert [r.step for r in result.steps] == list(MonthStep)
        assert [r.ran_at for r in result.steps][:6] == [at(d) for d in (-7, -5, -3, 0, 1, 2)]
        assert len(sim.children) == 1  # the gap was chased once, across runs
        assert svc.names().count("prepare_accountant_package") == 1
        await sim.verify_replay()

    run(scenario())
