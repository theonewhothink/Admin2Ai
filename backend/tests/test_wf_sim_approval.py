"""HardApprovalWorkflow in the real SDK runtime, offline (§25, §26, §42, §55).

The property that matters most: nothing but a verified human decision can
approve. Time, reminders, restarts (continue-as-new) and bogus signals never do.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from backoffice.workflows import (
    ActorKind,
    ApprovalCategory,
    ApprovalDecisionKind,
    ApprovalInput,
    ApprovalSignal,
    ApprovalStatus,
    HardApprovalWorkflow,
    WithdrawRequest,
)
from backoffice.workflows.contracts import ApproverCheck
from backoffice.workflows.testing import ScriptedServices, WorkflowSimulator
from backoffice.workflows.text import find_forbidden

T0 = datetime(2026, 9, 21, 8, 30, tzinfo=timezone.utc)
ANA = ApproverCheck(verified=True, display_name="Ana Silva", auth_method="passkey")


def run(coro):
    return asyncio.run(coro)


def request(**overrides) -> ApprovalInput:
    fields = dict(
        tenant_id="t1",
        request_id="pay-1200",
        category=ApprovalCategory.MONEY_MOVEMENT,
        summary="Pay €1,200.00 to Hazel Tree Lda.",
        amount=Decimal("1200.00"),
        evidence_ids=("ev-invoice-88",),
        requested_by="payments-agent",
    )
    fields.update(overrides)
    return ApprovalInput(**fields)


def decision(
    kind=ApprovalDecisionKind.APPROVE,
    actor="ana",
    actor_kind=ActorKind.HUMAN,
    assertion="sess-ana",
    note="",
):
    return ApprovalSignal(
        decision=kind, actor_id=actor, actor_kind=actor_kind, assertion_id=assertion, note=note
    )


async def started(svc: ScriptedServices, **overrides) -> WorkflowSimulator:
    svc.approvers.setdefault("sess-ana", ANA)
    sim = WorkflowSimulator.for_services(svc.services(), start_time=T0)
    await sim.start(HardApprovalWorkflow.run, request(**overrides), id="approval:pay-1200")
    return sim


def test_never_auto_approves_no_matter_how_long_it_waits():
    async def scenario():
        svc = ScriptedServices()
        sim = await started(svc, reminders_per_run=5)
        await sim.advance(timedelta(days=365))
        assert not sim.completed
        assert sim.run_count > 1  # history kept bounded with continue-as-new
        # Only asking and reminding happened: no verification, no audit, no decision.
        assert set(svc.names()) == {"request_approval"}
        headlines = [r.headline for r in svc.approval_requests]
        assert headlines[0] == "Approval needed."
        assert set(headlines[1:]) == {"This still needs your approval."}
        # Gentle backoff: 1, 2, 4 days, then weekly; counted across restarts.
        gaps = [
            b.reminder_number - a.reminder_number
            for a, b in zip(svc.approval_requests, svc.approval_requests[1:], strict=False)
        ]
        assert set(gaps) == {1}
        durations = [t.duration.days for t in sim.timers[:6]]
        assert durations == [1, 2, 4, 7, 7, 7]
        assert sim.query(HardApprovalWorkflow.current_status) == (
            "Waiting for your approval. Pay €1,200.00 to Hazel Tree Lda."
        )
        progress = sim.query(HardApprovalWorkflow.progress)
        assert progress.waiting and progress.requested_at == T0
        assert progress.reminders_sent == len(svc.approval_requests) - 1
        await sim.verify_replay()

    run(scenario())


def test_bogus_decisions_are_refused_audited_and_never_approve():
    async def scenario():
        svc = ScriptedServices()
        svc.approvers["sess-stale"] = ApproverCheck(verified=False, reason="sign-in expired")
        svc.approvers["sess-nomethod"] = ApproverCheck(verified=True, display_name="Rui")
        sim = await started(svc, eligible_approvers=("ana", "rui"))
        attempts = [
            decision(actor="agent-7", actor_kind=ActorKind.AGENT),  # AI never approves
            decision(actor="payments-agent"),  # requester approving itself
            decision(actor="mallory", assertion="sess-x"),  # not eligible
            decision(assertion=""),  # no proof of sign-in
            decision(assertion="sess-stale"),  # identity service says no
            decision(actor="rui", assertion="sess-nomethod"),  # unknown sign-in method
        ]
        for attempt in attempts:
            await sim.signal(HardApprovalWorkflow.approve, attempt)
        # An approve signal carrying a rejection (or vice versa) is refused too.
        await sim.signal(HardApprovalWorkflow.reject, decision(ApprovalDecisionKind.APPROVE))
        assert not sim.completed
        events = svc.approval_events
        assert len(events) == 7 and not any(e.accepted for e in events)
        assert [e.reason for e in events] == [
            "only a person can decide a hard approval",
            "the requester cannot decide their own request",
            "this person may not decide this request",
            "no proof of a fresh sign-in",
            "sign-in expired",
            "sign-in method unknown",
            "the decision does not match how it was sent",
        ]
        # The identity service is asked only when the attempt could be valid.
        assert svc.names().count("verify_approver") == 2  # sess-stale, sess-nomethod
        assert sim.query(HardApprovalWorkflow.progress).refused_attempts == 7
        await sim.verify_replay()

    run(scenario())


def test_verified_person_approves_and_who_and_when_are_recorded():
    async def scenario():
        svc = ScriptedServices()
        sim = await started(svc)
        await sim.advance(timedelta(days=2, hours=3))
        signed_at = sim.now
        await sim.signal(HardApprovalWorkflow.approve, decision(note="Checked with Hazel Tree"))
        result = sim.result()
        assert result.status is ApprovalStatus.APPROVED
        record = result.record
        assert record.actor_id == "ana"
        assert record.actor_display_name == "Ana Silva"
        assert record.auth_method == "passkey"
        assert record.decided_at == signed_at
        assert record.note == "Checked with Hazel Tree"
        assert result.summary == "Approved by Ana Silva on 23 September."
        assert result.reminders_sent == 1  # at +1 day; the next was due at +3 days
        accepted = [e for e in svc.approval_events if e.accepted]
        assert len(accepted) == 1 and accepted[0].decision is ApprovalDecisionKind.APPROVE
        [resolved] = svc.resolved_items
        assert resolved.message == "Approved by Ana Silva on 23 September."
        await sim.verify_replay()

    run(scenario())


def test_rejection_is_recorded_and_nothing_is_done():
    async def scenario():
        svc = ScriptedServices()
        sim = await started(svc)
        await sim.signal(HardApprovalWorkflow.reject, decision(ApprovalDecisionKind.REJECT))
        result = sim.result()
        assert result.status is ApprovalStatus.REJECTED
        assert result.summary == "Rejected by Ana Silva on 21 September. Nothing was done."
        assert find_forbidden(result.summary) == []
        await sim.verify_replay()

    run(scenario())


def test_first_valid_decision_wins_after_refused_ones():
    async def scenario():
        svc = ScriptedServices()
        sim = await started(svc)
        await sim.signal(HardApprovalWorkflow.approve, decision(actor_kind=ActorKind.SYSTEM))
        await sim.signal(HardApprovalWorkflow.reject, decision(ApprovalDecisionKind.REJECT))
        result = sim.result()
        assert result.status is ApprovalStatus.REJECTED
        assert result.refused_attempts == 1

    run(scenario())


def test_withdrawn_request_ends_without_approval():
    async def scenario():
        svc = ScriptedServices()
        sim = await started(svc)
        await sim.advance(timedelta(days=3))
        await sim.signal(
            HardApprovalWorkflow.withdraw,
            WithdrawRequest(requested_by="payments-agent", reason="invoice paid by card"),
        )
        result = sim.result()
        assert result.status is ApprovalStatus.WITHDRAWN
        assert result.record is None
        assert result.summary == "No longer needed. Nothing was done."
        assert svc.approval_events[-1].accepted is False
        assert svc.resolved_items[-1].message == "No longer needed. Nothing was done."
        await sim.verify_replay()

    run(scenario())


def test_decision_after_continue_as_new_is_still_verified_and_recorded():
    async def scenario():
        svc = ScriptedServices()
        sim = await started(svc, reminders_per_run=2)
        await sim.advance(timedelta(days=20))
        assert sim.run_count >= 2
        await sim.signal(HardApprovalWorkflow.approve, decision(assertion="sess-unknown"))
        assert not sim.completed
        await sim.signal(HardApprovalWorkflow.approve, decision())
        result = sim.result()
        assert result.status is ApprovalStatus.APPROVED
        assert result.refused_attempts == 1
        assert sim.query(HardApprovalWorkflow.progress).requested_at == T0
        # The approval card id survives restarts, so the owner sees one card.
        assert {r.owner_item_id for r in svc.approval_requests[1:]} == {"approval-card-1"}
        await sim.verify_replay()

    run(scenario())


def test_operator_cancellation_never_approves_and_takes_the_card_down():
    async def scenario():
        svc = ScriptedServices()
        sim = await started(svc)
        await sim.advance(timedelta(days=2))
        await sim.request_cancel()
        assert sim.cancelled
        assert not any(e.accepted for e in svc.approval_events)
        assert svc.resolved_items[-1].message == "No longer needed. Nothing was done."
        await sim.verify_replay()

    run(scenario())


def test_continues_as_new_when_the_server_suggests_it_and_still_waits():
    async def scenario():
        svc = ScriptedServices()
        svc.approvers.setdefault("sess-ana", ANA)
        sim = WorkflowSimulator.for_services(
            svc.services(), start_time=T0, suggest_continue_as_new_after=10
        )
        await sim.start(HardApprovalWorkflow.run, request(), id="approval:pay-1200")
        await sim.advance(timedelta(days=30))
        assert sim.run_count > 2 and not sim.completed
        await sim.signal(HardApprovalWorkflow.approve, decision())
        result = sim.result()
        assert result.status is ApprovalStatus.APPROVED
        assert result.reminders_sent == len(svc.approval_requests) - 1
        await sim.verify_replay()

    run(scenario())
