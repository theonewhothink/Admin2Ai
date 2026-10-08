"""Self-tests for the offline simulator in ``backoffice.workflows.testing``."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from backoffice.workflows import (
    ChasePolicy,
    DocumentArrival,
    MissingInvoiceInput,
    MissingInvoiceWorkflow,
    TransactionRef,
)
from backoffice.workflows.testing import ScriptedServices, SimulationError, WorkflowSimulator

T0 = datetime(2026, 9, 20, 9, 0, tzinfo=timezone.utc)
TX = TransactionRef(
    tenant_id="t1",
    transaction_id="tx-1",
    booked_on=date(2026, 9, 18),
    amount=Decimal("-10.00"),
    counterparty="SHOP",
)


def run(coro):
    return asyncio.run(coro)


def sim_for(svc: ScriptedServices | None = None) -> WorkflowSimulator:
    return WorkflowSimulator.for_services((svc or ScriptedServices()).services(), start_time=T0)


def test_start_time_must_be_timezone_aware():
    with pytest.raises(ValueError):
        WorkflowSimulator(workflows=[], activities=[], start_time=datetime(2026, 1, 1))


def test_misuse_is_reported_clearly():
    async def scenario():
        sim = sim_for()
        with pytest.raises(SimulationError, match="no running workflow"):
            await sim.signal(MissingInvoiceWorkflow.cancel, None)
        with pytest.raises(SimulationError, match="unknown workflow"):
            await sim.start("NoSuchWorkflow", None, id="x")
        with pytest.raises(SimulationError, match="not started"):
            sim.query(MissingInvoiceWorkflow.current_status)
        await sim.start(MissingInvoiceWorkflow.run, MissingInvoiceInput(transaction=TX), id="w")
        with pytest.raises(SimulationError, match="already started"):
            await sim.start(MissingInvoiceWorkflow.run, MissingInvoiceInput(transaction=TX), id="w")
        with pytest.raises(ValueError):
            await sim.advance(timedelta(seconds=-1))
        with pytest.raises(SimulationError, match="has not completed"):
            sim.result()

    run(scenario())


def test_time_only_moves_when_asked_and_skips_to_timers():
    async def scenario():
        sim = sim_for()
        await sim.start(MissingInvoiceWorkflow.run, MissingInvoiceInput(transaction=TX), id="w")
        assert sim.now == T0
        timer = await sim.advance_to_next_timer()
        assert timer is not None and sim.now == timer.fire_at == T0 + timedelta(hours=12)
        await sim.advance(timedelta(minutes=5))
        assert sim.now == T0 + timedelta(hours=12, minutes=5)

    run(scenario())


def test_a_signal_during_a_wait_cancels_the_timer_in_history():
    async def scenario():
        sim = sim_for()
        await sim.start(MissingInvoiceWorkflow.run, MissingInvoiceInput(transaction=TX), id="w")
        await sim.signal(
            MissingInvoiceWorkflow.document_received, DocumentArrival(evidence_ids=("ev-x",))
        )
        assert sim.cancelled_timers  # the wait was interrupted, not left dangling
        await sim.verify_replay(sandboxed=True)
        await sim.verify_replay(sandboxed=False)

    run(scenario())


def test_histories_enable_id_and_type_determinism_checks():
    async def scenario():
        sim = sim_for()
        await sim.start(MissingInvoiceWorkflow.run, MissingInvoiceInput(transaction=TX), id="w")
        completed = [
            e
            for e in sim.histories()[0].events
            if e.HasField("workflow_task_completed_event_attributes")
        ]
        assert completed
        for event in completed:
            flags = event.workflow_task_completed_event_attributes.sdk_metadata.core_used_flags
            assert 1 in flags

    run(scenario())


def test_run_until_complete_refuses_to_wait_forever_for_a_signal():
    async def scenario():
        svc = ScriptedServices()
        sim = sim_for(svc)
        short = ChasePolicy(max_reminders=0, late_reply_window=timedelta(days=1))
        inp = MissingInvoiceInput(transaction=TX, policy=short)
        await sim.start(MissingInvoiceWorkflow.run, inp, id="w")
        with pytest.raises(SimulationError, match="waiting for a signal"):
            await sim.run_until_complete()
        assert len(svc.owner_items) == 1  # it did everything it could first

    run(scenario())


def test_activity_calls_are_recorded_with_simulated_time_and_results():
    async def scenario():
        sim = sim_for()
        await sim.start(MissingInvoiceWorkflow.run, MissingInvoiceInput(transaction=TX), id="w")
        first = sim.activity_calls[0]
        assert (first.name, first.at) == ("search_for_evidence", T0)
        assert first.arg.transaction == TX
        assert first.result.evidence_ids == ()
        assert sim.calls("send_supplier_request")[0].result.sent is True

    run(scenario())


def test_activities_that_keep_failing_surface_the_cause():
    class Down(ScriptedServices):
        async def search_for_evidence(self, request):
            raise ConnectionError("mail server down")

    async def scenario():
        sim = sim_for(Down())
        with pytest.raises(SimulationError, match="kept failing") as info:
            await sim.start(MissingInvoiceWorkflow.run, MissingInvoiceInput(transaction=TX), id="w")
        assert isinstance(info.value.__cause__, ConnectionError)

    run(scenario())
