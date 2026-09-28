"""Pure decision logic behind the workflows: no Temporal, no clock (§3, §19, §22, §25, §27, §57)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

import pytest

from backoffice.domain.models import Quality
from backoffice.workflows import decisions as d
from backoffice.workflows.contracts import (
    AccountantQuery,
    ActorKind,
    ApprovalCategory,
    ApprovalDecisionKind,
    ApprovalInput,
    ApprovalSignal,
    ApprovalStatus,
    ApproverCheck,
    AuthorizationDecision,
    CancelRequest,
    ChasePolicy,
    ClosureVerdict,
    CompletenessReport,
    DeliveryConfirmation,
    DeliveryReceipt,
    DocumentArrival,
    EvidenceBatch,
    EvidenceOrigin,
    ItemResolved,
    ItemStatus,
    MatchOutcome,
    MonthCloseInput,
    MonthCloseState,
    MonthStep,
    MonthSummary,
    OwnerAnswer,
    OwnerAnswerKind,
    OwnerItemRef,
    PackageRef,
    Period,
    QueryResolution,
    SearchOutcome,
    SupplierMessageKind,
    SupplierMessageReceipt,
    ThreadCheckResult,
    TransactionRef,
    VerificationOutcome,
)

T0 = datetime(2026, 9, 20, 9, 0, tzinfo=timezone.utc)
POLICY = ChasePolicy()
TX = TransactionRef(
    tenant_id="t1",
    transaction_id="tx-1",
    booked_on=date(2026, 9, 18),
    amount=Decimal("-117.20"),
    counterparty="VODAFONE",
)


def v(
    quality: Quality, doc: str | None = "doc-1", ids=("ev-1",), reasons=()
) -> VerificationOutcome:
    return VerificationOutcome(quality=quality, document_id=doc, evidence_ids=ids, reasons=reasons)


def m(quality: Quality, matched: bool = True, ids=("ev-bank",)) -> MatchOutcome:
    return MatchOutcome(matched=matched, quality=quality, evidence_ids=ids)


# ========== evidence gate


class TestJudgeEvidence:
    def test_green_document_and_green_match_close_with_all_evidence(self):
        verdict = d.judge_evidence(v(Quality.GREEN), m(Quality.GREEN))
        assert verdict.kind is d.VerdictKind.CLOSE
        assert verdict.evidence_ids == ("ev-1", "ev-bank")
        assert verdict.document_id == "doc-1"

    @pytest.mark.parametrize(
        ("doc_q", "match"),
        [
            (Quality.AMBER, None),
            (Quality.AMBER, m(Quality.GREEN)),
            (Quality.GREEN, m(Quality.AMBER)),
            (Quality.GREEN, m(Quality.GREEN, matched=False)),
            (Quality.GREEN, None),
        ],
    )
    def test_anything_short_of_green_stays_unconfirmed(self, doc_q, match):
        assert d.judge_evidence(v(doc_q), match).kind is d.VerdictKind.UNCONFIRMED

    @pytest.mark.parametrize(
        ("doc_q", "match_q"),
        [(Quality.RED, None), (Quality.GREEN, Quality.RED), (Quality.RED, Quality.GREEN)],
    )
    def test_any_red_is_a_conflict_never_a_guess(self, doc_q, match_q):
        match = m(match_q) if match_q else None
        verdict = d.judge_evidence(v(doc_q, reasons=("QR says 438.60",)), match)
        assert verdict.kind is d.VerdictKind.CONFLICT
        assert "QR says 438.60" in verdict.reasons

    def test_green_without_any_evidence_cannot_close(self):
        verdict = d.judge_evidence(v(Quality.GREEN, ids=()), m(Quality.GREEN, ids=()))
        assert verdict.kind is d.VerdictKind.UNCONFIRMED

    def test_only_identified_green_documents_are_matched(self):
        assert d.needs_match(v(Quality.GREEN))
        assert not d.needs_match(v(Quality.GREEN, doc=None))
        assert not d.needs_match(v(Quality.AMBER))


# ========== chase machine: single decisions


def fresh() -> d.ChaseState:
    return d.new_chase_state(POLICY)


def searched(state: d.ChaseState | None = None, at: datetime = T0) -> d.ChaseState:
    return d.apply_search(state or fresh(), SearchOutcome(), at)


def asked(state: d.ChaseState, at: datetime, thread: str | None = "th-1") -> d.ChaseState:
    receipt = SupplierMessageReceipt(
        sent=True, thread_id=thread, message_evidence_id=f"ev-m{state.messages_sent + 1}"
    )
    return d.apply_message(state, receipt, at)


def allowed(state: d.ChaseState, number: int, ok: bool = True) -> d.ChaseState:
    return d.apply_authorization(state, number, AuthorizationDecision(authorized=ok))


class TestDecideChase:
    def test_starts_by_searching(self):
        assert d.decide_chase(fresh(), POLICY, T0) == d.Search()

    def test_budget_is_one_request_plus_reminders(self):
        assert fresh().message_budget == 3
        assert d.new_chase_state(ChasePolicy(max_reminders=0)).message_budget == 1

    def test_checks_permission_right_before_the_first_request(self):
        state = searched()
        assert d.decide_chase(state, POLICY, T0) == d.CheckAuthorization(1)
        state = allowed(state, 1)
        assert d.decide_chase(state, POLICY, T0) == d.SendMessage(SupplierMessageKind.REQUEST, 1)

    def test_not_allowed_escalates_instead_of_emailing(self):
        state = allowed(searched(), 1, ok=False)
        assert d.decide_chase(state, POLICY, T0) == d.Escalate(
            d.EscalationReason.NOT_ALLOWED_TO_ASK
        )

    def test_waits_for_reply_polling_the_thread(self):
        state = asked(allowed(searched(), 1), T0)
        step = d.decide_chase(state, POLICY, T0)
        assert step == d.Wait(T0 + timedelta(hours=12))
        assert d.decide_chase(state, POLICY, T0 + timedelta(hours=12)) == d.CheckThread()

    def test_without_a_thread_it_simply_waits_out_the_reply_window(self):
        state = asked(allowed(searched(), 1), T0, thread=None)
        assert d.decide_chase(state, POLICY, T0 + timedelta(hours=13)) == d.Wait(
            T0 + timedelta(days=6)
        )

    def test_at_the_deadline_it_checks_once_more_then_searches_then_asks_permission(self):
        state = asked(allowed(searched(), 1), T0)
        deadline = T0 + timedelta(days=6)
        state = state.model_copy(update={"last_thread_check_at": deadline - timedelta(hours=1)})
        assert d.decide_chase(state, POLICY, deadline) == d.CheckThread()
        state = d.apply_thread_check(state, ThreadCheckResult(replied=False), deadline)
        assert d.decide_chase(state, POLICY, deadline) == d.Search()
        state = searched(state, deadline)
        assert d.decide_chase(state, POLICY, deadline) == d.CheckAuthorization(2)
        state = allowed(state, 2)
        assert d.decide_chase(state, POLICY, deadline) == d.SendMessage(
            SupplierMessageKind.REMINDER, 2
        )

    def test_permission_withdrawn_before_a_reminder_stops_the_reminder(self):
        state = asked(allowed(searched(), 1), T0)
        deadline = T0 + timedelta(days=6)
        state = d.apply_thread_check(state, ThreadCheckResult(replied=False), deadline)
        state = allowed(searched(state, deadline), 2, ok=False)
        assert d.decide_chase(state, POLICY, deadline) == d.Escalate(
            d.EscalationReason.NOT_ALLOWED_TO_ASK
        )

    def test_escalates_when_the_budget_is_spent(self):
        policy = ChasePolicy(max_reminders=0)
        state = asked(
            allowed(d.apply_search(d.new_chase_state(policy), SearchOutcome(), T0), 1), T0
        )
        deadline = T0 + timedelta(days=6)
        state = d.apply_thread_check(state, ThreadCheckResult(replied=False), deadline)
        assert d.decide_chase(state, policy, deadline) == d.Escalate(d.EscalationReason.NO_REPLY)

    def test_unconfirmed_candidates_change_the_escalation_reason(self):
        state = allowed(searched(), 1, ok=False).model_copy(update={"unconfirmed": ("ev-9",)})
        assert d.decide_chase(state, POLICY, T0) == d.Escalate(d.EscalationReason.UNCONFIRMED)

    def test_pending_evidence_is_checked_before_anything_else(self):
        state = d.apply_document_arrival(fresh(), DocumentArrival(evidence_ids=("ev-x",)))
        step = d.decide_chase(state, POLICY, T0)
        assert step == d.CheckEvidence(
            EvidenceBatch(evidence_ids=("ev-x",), origin=EvidenceOrigin.PIPELINE)
        )

    def test_hold_until_chase_window_then_search_again(self):
        hold = T0 + timedelta(days=2)
        policy = ChasePolicy(chase_not_before=hold)
        state = d.apply_search(d.new_chase_state(policy), SearchOutcome(), T0)
        assert d.decide_chase(state, policy, T0) == d.Wait(hold)
        assert d.decide_chase(state, policy, hold) == d.Search()
        state = d.apply_search(state, SearchOutcome(), hold)
        assert d.decide_chase(state, policy, hold) == d.CheckAuthorization(1)

    def test_a_deadline_within_clock_noise_counts_as_reached(self):
        hold = T0 + timedelta(days=2)
        policy = ChasePolicy(chase_not_before=hold)
        state = d.apply_search(d.new_chase_state(policy), SearchOutcome(), T0)
        assert d.decide_chase(state, policy, hold - timedelta(milliseconds=300)) == d.Search()

    def test_no_contact_escalates(self):
        state = allowed(searched(), 1)
        state = d.apply_message(state, SupplierMessageReceipt(sent=False, reason="no email"), T0)
        assert state.messages_sent == 0
        assert d.decide_chase(state, POLICY, T0) == d.Escalate(
            d.EscalationReason.NO_SUPPLIER_CONTACT
        )

    def test_while_escalated_it_keeps_listening_for_a_late_reply_then_stops(self):
        policy = ChasePolicy(
            max_reminders=0,
            late_reply_window=timedelta(days=1),
            late_reply_check_interval=timedelta(hours=12),
        )
        state = asked(
            allowed(d.apply_search(d.new_chase_state(policy), SearchOutcome(), T0), 1), T0
        )
        end = T0 + timedelta(days=6)
        state = d.apply_thread_check(state, ThreadCheckResult(replied=False), end)
        state = d.apply_escalation(
            state, d.EscalationReason.NO_REPLY, OwnerItemRef(item_id="c1"), end
        )
        assert d.decide_chase(state, policy, end) == d.Wait(end + timedelta(hours=12))
        state = d.apply_thread_check(
            state, ThreadCheckResult(replied=False), end + timedelta(hours=12)
        )
        state = d.apply_thread_check(
            state, ThreadCheckResult(replied=False), end + timedelta(days=1)
        )
        assert d.decide_chase(state, policy, end + timedelta(days=1)) == d.Wait(None)

    def test_late_replies_are_checked_daily_by_default(self):
        policy = ChasePolicy(max_reminders=0)
        state = asked(
            allowed(d.apply_search(d.new_chase_state(policy), SearchOutcome(), T0), 1), T0
        )
        end = T0 + timedelta(days=6)
        state = d.apply_thread_check(state, ThreadCheckResult(replied=False), end)
        state = d.apply_escalation(
            state, d.EscalationReason.NO_REPLY, OwnerItemRef(item_id="c"), end
        )
        assert d.decide_chase(state, policy, end) == d.Wait(end + timedelta(days=1))

    def test_owner_handling_waits_without_timers(self):
        state = searched().model_copy(update={"owner_handling": True})
        assert d.decide_chase(state, POLICY, T0) == d.Wait(None)

    def test_finishing_order_resolve_card_record_notify_parent_finish(self):
        state = searched().model_copy(
            update={"owner_item_id": "c1", "escalation": d.EscalationReason.NO_REPLY}
        )
        state = d.apply_cancel(state, CancelRequest(requested_by="system"))
        steps = []
        for _ in range(4):
            step = d.decide_chase(state, POLICY, T0, report_to="parent-1")
            steps.append(type(step).__name__)
            if isinstance(step, d.ResolveOwnerItem):
                state = d.apply_item_resolved(state, step.pending.item_id)
            elif isinstance(step, d.RecordOutcome):
                state = d.apply_outcome_recorded(state)
            elif isinstance(step, d.NotifyParent):
                state = d.apply_parent_notified(state)
        assert steps == ["ResolveOwnerItem", "RecordOutcome", "NotifyParent", "Finish"]

    def test_never_asks_to_wait_for_a_moment_already_past(self):
        # Walk a long chase and check every Wait is strictly in the future.
        state, now = fresh(), T0
        for _ in range(400):
            step = d.decide_chase(state, POLICY, now)
            if isinstance(step, d.Wait):
                if step.until is None:
                    break
                assert step.until > now + d.CLOCK_TOLERANCE
                now = step.until
            else:
                state = _apply_quiet_world(state, step, now)
        assert state.escalation is d.EscalationReason.NO_REPLY


def _apply_quiet_world(state: d.ChaseState, step, now: datetime) -> d.ChaseState:
    """Nothing is ever found, the supplier never answers, everything is allowed."""
    if isinstance(step, d.Search):
        return d.apply_search(state, SearchOutcome(), now)
    if isinstance(step, d.CheckAuthorization):
        return allowed(state, step.message_number)
    if isinstance(step, d.SendMessage):
        return asked(state, now)
    if isinstance(step, d.CheckThread):
        return d.apply_thread_check(state, ThreadCheckResult(replied=False), now)
    if isinstance(step, d.Escalate):
        return d.apply_escalation(state, step.reason, OwnerItemRef(item_id="c"), now)
    raise AssertionError(step)


# ========== chase machine: signals and results


class TestChaseSignals:
    def test_evidence_seen_before_is_not_checked_twice(self):
        state = d.apply_search(fresh(), SearchOutcome(evidence_ids=("ev-1", "ev-1")), T0)
        assert state.pending[0].evidence_ids == ("ev-1",)
        state = d.apply_document_arrival(state, DocumentArrival(evidence_ids=("ev-1",)))
        assert len(state.pending) == 1
        assert state.inbox == 1  # a signal always wakes the workflow

    def test_close_verdict_retires_the_open_card_and_records_superseded_conflicts(self):
        state = fresh().model_copy(
            update={
                "owner_item_id": "c1",
                "escalation": d.EscalationReason.CONFLICT,
                "conflict_evidence": ("ev-bad",),
            }
        )
        batch = EvidenceBatch(evidence_ids=("ev-good",), origin=EvidenceOrigin.SUPPLIER_REPLY)
        verdict = d.judge_evidence(v(Quality.GREEN, ids=("ev-good",)), m(Quality.GREEN))
        state = d.apply_evidence_verdict(
            state.model_copy(update={"pending": (batch,)}), batch, verdict
        )
        assert state.final.status is ItemStatus.CLOSED
        assert state.final.superseded_evidence_ids == ("ev-bad",)
        assert state.to_resolve == (
            d.PendingResolution(item_id="c1", resolution=d.Resolution.FOUND),
        )
        assert state.pending == ()

    def test_a_new_conflict_replaces_the_open_card(self):
        state = searched().model_copy(
            update={"owner_item_id": "c1", "escalation": d.EscalationReason.NO_REPLY}
        )
        batch = EvidenceBatch(evidence_ids=("ev-r",), origin=EvidenceOrigin.SUPPLIER_REPLY)
        state = d.apply_evidence_verdict(state, batch, d.judge_evidence(v(Quality.RED), None))
        assert state.to_resolve[0].resolution is d.Resolution.SUPERSEDED
        assert d.decide_chase(state, POLICY, T0) == d.ResolveOwnerItem(state.to_resolve[0])
        state = d.apply_item_resolved(state, "c1")
        assert d.decide_chase(state, POLICY, T0) == d.Escalate(d.EscalationReason.CONFLICT)

    def test_an_acknowledged_conflict_is_not_raised_again(self):
        state = searched().model_copy(update={"conflict_evidence": ("ev-bad",)})
        state = d.apply_escalation(
            state, d.EscalationReason.CONFLICT, OwnerItemRef(item_id="c1"), T0
        )
        answer = OwnerAnswer(
            kind=OwnerAnswerKind.KEEP_CHASING, answered_by="o", answer_evidence_id="ev-a"
        )
        state = d.apply_item_resolved(d.apply_owner_answer(state, answer, POLICY), "c1")
        assert d.decide_chase(state, POLICY, T0) == d.SendMessage(SupplierMessageKind.REQUEST, 1)

    def test_signals_after_the_end_change_nothing(self):
        state = d.apply_cancel(fresh(), CancelRequest(requested_by="system"))
        final = state.final
        state = d.apply_document_arrival(state, DocumentArrival(evidence_ids=("ev-late",)))
        answer = OwnerAnswer(
            kind=OwnerAnswerKind.NO_DOCUMENT_NEEDED, answered_by="o", answer_evidence_id="ev-a"
        )
        state = d.apply_owner_answer(state, answer, POLICY)
        assert state.final == final and state.pending == ()

    def test_card_created_while_finishing_is_tidied_up(self):
        state = d.apply_cancel(fresh(), CancelRequest(requested_by="system"))
        state = d.apply_escalation(
            state, d.EscalationReason.NO_REPLY, OwnerItemRef(item_id="c9"), T0
        )
        assert state.to_resolve == (
            d.PendingResolution(item_id="c9", resolution=d.Resolution.CANCELLED),
        )
        assert state.escalation is None

    def test_keep_chasing_grants_a_new_round(self):
        state = asked(allowed(searched(), 1), T0).model_copy(update={"no_contact": True})
        answer = OwnerAnswer(
            kind=OwnerAnswerKind.KEEP_CHASING, answered_by="o", answer_evidence_id="ev-a"
        )
        state = d.apply_owner_answer(state, answer, POLICY)
        assert state.owner_allowed_chasing and not state.no_contact
        assert state.message_budget == 1 + 1 + POLICY.max_reminders

    def test_no_document_needed_is_backed_by_the_answer(self):
        answer = OwnerAnswer(
            kind=OwnerAnswerKind.NO_DOCUMENT_NEEDED,
            answered_by="owner-1",
            answer_evidence_id="ev-a",
        )
        state = d.apply_owner_answer(fresh(), answer, POLICY)
        outcome = d.ledger_outcome(state, TX)
        assert outcome.status is ItemStatus.NOT_REQUIRED
        assert outcome.evidence_ids == ("ev-a",) and outcome.actor == "owner-1"

    def test_ledger_outcome_and_result_need_a_finished_item(self):
        with pytest.raises(ValueError):
            d.ledger_outcome(fresh(), TX)
        with pytest.raises(ValueError):
            d.chase_result(fresh(), TX, "x")

    def test_phases(self):
        assert d.chase_phase(fresh(), POLICY) is d.ChasePhase.SEARCHING
        held = ChasePolicy(chase_not_before=T0 + timedelta(days=1))
        assert d.chase_phase(searched(), held) is d.ChasePhase.WAITING_TO_ASK
        assert d.chase_phase(asked(searched(), T0), POLICY) is d.ChasePhase.WAITING_FOR_SUPPLIER
        conflict = searched().model_copy(update={"conflict_evidence": ("x",)})
        assert d.chase_phase(conflict, POLICY) is d.ChasePhase.NEEDS_OWNER


# ========== pure end-to-end driver


def drive(
    world: dict[str, Callable],
    *,
    policy: ChasePolicy = POLICY,
    signals: list[tuple[timedelta, Callable[[d.ChaseState], d.ChaseState]]] = (),
    horizon: timedelta = timedelta(days=90),
) -> tuple[d.ChaseState, list[tuple[timedelta, str]]]:
    """The MissingInvoiceWorkflow loop as a pure function (no Temporal at all)."""
    state, now, log = d.new_chase_state(policy), T0, []
    pending = sorted(signals, key=lambda s: s[0])
    while now - T0 <= horizon:
        step = d.decide_chase(state, policy, now)
        if isinstance(step, d.Finish):
            return state, log
        if isinstance(step, d.Wait):
            wake = step.until
            if pending and (wake is None or T0 + pending[0][0] <= wake):
                offset, apply = pending.pop(0)
                now = max(now, T0 + offset)
                state = apply(state)
                continue
            if wake is None:
                break
            now = wake
            continue
        log.append((now - T0, type(step).__name__))
        state = world[type(step).__name__](state, step, now)
    return state, log


def quiet_world(**overrides) -> dict[str, Callable]:
    world = {
        "Search": lambda s, st, now: d.apply_search(s, SearchOutcome(), now),
        "CheckAuthorization": lambda s, st, now: allowed(s, st.message_number),
        "SendMessage": lambda s, st, now: asked(s, now),
        "CheckThread": lambda s, st, now: d.apply_thread_check(
            s, ThreadCheckResult(replied=False), now
        ),
        "Escalate": lambda s, st, now: d.apply_escalation(
            s, st.reason, OwnerItemRef(item_id="card"), now
        ),
        "CheckEvidence": lambda s, st, now: d.apply_evidence_verdict(
            s,
            st.batch,
            d.judge_evidence(v(Quality.GREEN, ids=st.batch.evidence_ids), m(Quality.GREEN)),
        ),
        "ResolveOwnerItem": lambda s, st, now: d.apply_item_resolved(s, st.pending.item_id),
        "RecordOutcome": lambda s, st, now: d.apply_outcome_recorded(s),
    }
    world.update(overrides)
    return world


def test_pure_chase_six_days_reminder_then_reply_closes():
    reply = (
        timedelta(days=7),
        lambda s: d.apply_document_arrival(s, DocumentArrival(evidence_ids=("ev-reply",))),
    )
    state, log = drive(quiet_world(), signals=[reply])
    sends = [t for t, name in log if name == "SendMessage"]
    assert sends == [timedelta(0), timedelta(days=6)]
    assert state.final.status is ItemStatus.CLOSED
    assert state.final.evidence_ids == ("ev-reply", "ev-bank")
    assert log[-2:] == [(timedelta(days=7), "CheckEvidence"), (timedelta(days=7), "RecordOutcome")]


def test_pure_chase_exhausts_reminders_then_waits_for_the_owner():
    state, log = drive(quiet_world(), horizon=timedelta(days=60))
    sends = [t for t, name in log if name == "SendMessage"]
    assert sends == [timedelta(days=0), timedelta(days=6), timedelta(days=12)]
    assert [t for t, name in log if name == "Escalate"] == [timedelta(days=18)]
    assert state.final is None and state.escalation is d.EscalationReason.NO_REPLY


def test_pure_chase_owner_answer_ends_it():
    answer = OwnerAnswer(
        kind=OwnerAnswerKind.NO_DOCUMENT_NEEDED, answered_by="o", answer_evidence_id="ev-a"
    )
    signal = (timedelta(days=20), lambda s: d.apply_owner_answer(s, answer, POLICY))
    state, _ = drive(quiet_world(), signals=[signal])
    assert state.final.status is ItemStatus.NOT_REQUIRED


# ========== approval


def approval_input(**kw) -> ApprovalInput:
    fields = dict(
        tenant_id="t1",
        request_id="r1",
        category=ApprovalCategory.BANK_DETAIL_CHANGE,
        summary="Change Hazel Tree's bank details.",
        evidence_ids=("ev-letter",),
        requested_by="fraud-agent",
    )
    fields.update(kw)
    return ApprovalInput(**fields)


def signal(**kw) -> ApprovalSignal:
    fields = dict(
        decision=ApprovalDecisionKind.APPROVE,
        actor_id="ana",
        actor_kind=ActorKind.HUMAN,
        assertion_id="s1",
    )
    fields.update(kw)
    return ApprovalSignal(**fields)


class TestApprovalRules:
    def test_a_signed_in_person_passes_the_first_gate(self):
        assert d.refusal_before_check(signal(), approval_input()) is None

    @pytest.mark.parametrize(
        ("kw", "reason"),
        [
            ({"actor_kind": ActorKind.AGENT}, "only a person"),
            ({"actor_kind": ActorKind.SYSTEM}, "only a person"),
            ({"actor_id": "  "}, "does not say who"),
            ({"assertion_id": ""}, "no proof"),
            ({"actor_id": "fraud-agent"}, "requester cannot"),
        ],
    )
    def test_refusals(self, kw, reason):
        assert reason in d.refusal_before_check(signal(**kw), approval_input())

    def test_eligible_approvers_are_enforced(self):
        inp = approval_input(eligible_approvers=("rui",))
        assert "may not decide" in d.refusal_before_check(signal(), inp)

    def test_signal_name_must_match_the_decision(self):
        assert d.refusal_before_check(signal(), approval_input(), ApprovalDecisionKind.REJECT)
        assert (
            d.refusal_before_check(signal(), approval_input(), ApprovalDecisionKind.APPROVE) is None
        )

    def test_identity_check_must_confirm_with_a_method(self):
        assert d.refusal_after_check(ApproverCheck(verified=True, auth_method="passkey")) is None
        assert d.refusal_after_check(ApproverCheck(verified=False, reason="expired")) == "expired"
        assert (
            d.refusal_after_check(ApproverCheck(verified=False)) == "sign-in could not be confirmed"
        )
        assert (
            d.refusal_after_check(ApproverCheck(verified=True, auth_method=" "))
            == "sign-in method unknown"
        )

    def test_reminder_backoff_doubles_up_to_the_cap(self):
        one, week = timedelta(days=1), timedelta(days=7)
        assert [d.reminder_delay(n, one, week).days for n in range(6)] == [1, 2, 4, 7, 7, 7]
        assert d.reminder_delay(10_000, one, week) == week
        with pytest.raises(ValueError):
            d.reminder_delay(-1, one, week)

    def test_record_keeps_who_how_and_when(self):
        when = T0 + timedelta(hours=5)
        check = ApproverCheck(verified=True, display_name="Ana", auth_method="passkey")
        record = d.approval_record(signal(actor_id=" ana ", note="ok"), check, "r1", when)
        assert (record.actor_id, record.auth_method, record.decided_at, record.note) == (
            "ana",
            "passkey",
            when,
            "ok",
        )

    def test_status_and_rollover(self):
        assert d.approval_status_for(ApprovalDecisionKind.APPROVE) is ApprovalStatus.APPROVED
        assert d.approval_status_for(ApprovalDecisionKind.REJECT) is ApprovalStatus.REJECTED
        assert d.approval_rollover(5, 5, False) and d.approval_rollover(0, 5, True)
        assert not d.approval_rollover(4, 5, False)


# ========== month close

DAY0 = date(2026, 10, 5)
PACKAGE = PackageRef(package_id="pkg-1", evidence_id="ev-p", complete_items=5, missing_items=0)


def month_input(**kw) -> MonthCloseInput:
    return MonthCloseInput(
        tenant_id="t1", entity_id="e1", period=Period(year=2026, month=9), day_zero=DAY0, **kw
    )


class TestMonthRules:
    def test_schedule_follows_section_27(self):
        schedule = d.step_schedule(month_input())
        days = {step: (when.date() - DAY0).days for step, when in schedule.items()}
        assert days == {
            MonthStep.COMPLETENESS_AUDIT: -7,
            MonthStep.EVIDENCE_RETRIEVAL: -5,
            MonthStep.SUPPLIER_CHASES: -3,
            MonthStep.PACKAGE: 0,
            MonthStep.DELIVERY_CONFIRMATION: 1,
            MonthStep.ACCOUNTANT_QUERIES: 2,
            MonthStep.CLOSURE: 2,
        }
        assert all(w.time() == time(6, 0) and w.tzinfo is not None for w in schedule.values())

    def test_custom_run_time(self):
        schedule = d.step_schedule(month_input(run_at=time(22, 30)))
        assert schedule[MonthStep.PACKAGE] == datetime(2026, 10, 5, 22, 30, tzinfo=timezone.utc)

    def test_next_step_and_record_step(self):
        state = MonthCloseState()
        assert d.next_month_step(state) is MonthStep.COMPLETENESS_AUDIT
        state = d.record_step(state, MonthStep.COMPLETENESS_AUDIT, T0, T0)
        assert d.next_month_step(state) is MonthStep.EVIDENCE_RETRIEVAL
        with pytest.raises(ValueError):
            d.record_step(state, MonthStep.COMPLETENESS_AUDIT, T0, T0)

    def test_sleep_needed(self):
        assert d.sleep_needed(T0, T0 + timedelta(hours=1)) == timedelta(hours=1)
        assert d.sleep_needed(T0, T0) is None
        assert d.sleep_needed(T0, T0 + timedelta(milliseconds=500)) is None
        assert d.sleep_needed(T0 + timedelta(days=1), T0) is None

    def test_gaps_to_start_dedupes_and_keeps_order(self):
        a, b = TX, TX.model_copy(update={"transaction_id": "tx-2"})
        assert d.gaps_to_start((a, b, a), ()) == (a, b)
        assert d.gaps_to_start((a, b), ("tx-1",)) == (b,)

    def test_child_input_and_id(self):
        child = d.child_input(TX, POLICY, T0, "month-1")
        assert child.policy.chase_not_before == T0 and child.report_to_workflow_id == "month-1"
        assert POLICY.chase_not_before is None  # the shared policy is untouched
        assert d.missing_invoice_workflow_id("t1", "tx-1") == "missing-invoice:t1:tx-1"

    def test_children_bookkeeping(self):
        state = d.apply_children_started(MonthCloseState(), ("a",), ("b",))
        assert state.chased_ids == ("a", "b") and state.already_running_ids == ("b",)

    def test_audit_replaces_gaps(self):
        state = d.apply_audit(
            MonthCloseState(gaps=(TX,)),
            CompletenessReport(transactions_checked=1, documents_collected=1),
        )
        assert state.gaps == ()

    def test_delivery_confirmation_needs_evidence_and_is_never_erased(self):
        state = d.apply_delivery(
            MonthCloseState(package=PACKAGE),
            DeliveryReceipt(delivered=True, delivery_evidence_id="ev-d"),
            T0,
        )
        assert state.delivered_at == T0 and d.delivery_check_due(state)
        assert not d.delivery_confirmed(state)
        state = d.apply_delivery_confirmation(
            state, DeliveryConfirmation(confirmed=True, evidence_id="ev-c")
        )
        assert d.delivery_confirmed(state)
        state = d.apply_delivery_confirmation(state, DeliveryConfirmation(confirmed=False))
        assert d.delivery_confirmed(state)
        assert d.delivery_evidence_ids(state) == ("ev-d", "ev-c")

    def test_receipt_that_already_confirms_counts(self):
        receipt = DeliveryReceipt(
            delivered=True,
            delivery_evidence_id="ev-d",
            confirmed=True,
            confirmation_evidence_id="ev-r",
        )
        assert d.delivery_confirmed(d.apply_delivery(MonthCloseState(), receipt))
        unproven = DeliveryReceipt(delivered=True, delivery_evidence_id="ev-d", confirmed=True)
        assert not d.delivery_confirmed(d.apply_delivery(MonthCloseState(), unproven))

    def test_questions_are_tracked_until_answered_with_evidence(self):
        q = AccountantQuery(query_id="q1", text="?")
        state = d.apply_query_received(MonthCloseState(), q)
        assert d.apply_query_received(state, q) == state
        assert d.open_query_ids(state) == ("q1",)
        state = d.apply_query_handled(
            state, QueryResolution(query_id="q1", resolved=False, owner_question="?")
        )
        assert d.open_query_ids(state) == ("q1",)
        assert state.waiting_queries == (q,) and state.pending_queries == ()
        assert d.apply_query_received(state, q) == state  # already asked
        # A bare "resolved" signal is not evidence (§3): still open.
        state = d.apply_month_item_resolved(
            state, ItemResolved(subject_id="q1", status=ItemStatus.CLOSED)
        )
        assert d.open_query_ids(state) == ("q1",)
        state = d.apply_query_handled(
            state, QueryResolution(query_id="q1", resolved=True, evidence_ids=("ev-a",))
        )
        assert d.open_query_ids(state) == () and state.waiting_queries == ()
        assert [r.query_id for r in state.handled_queries] == ["q1"]
        # An answer for a question never asked changes nothing.
        stray = QueryResolution(query_id="q9", resolved=True, evidence_ids=("ev-b",))
        assert d.apply_query_handled(state, stray) == state

    @pytest.mark.parametrize(
        ("verdict", "confirmed", "open_question", "expected"),
        [
            (ClosureVerdict(closed=True, open_items=0), True, False, True),
            (ClosureVerdict(closed=False, open_items=0), True, False, False),
            (ClosureVerdict(closed=True, open_items=1), True, False, False),
            (
                ClosureVerdict(
                    closed=True, open_items=0, summary=MonthSummary(unresolved_issues=1)
                ),
                True,
                False,
                False,
            ),
            (ClosureVerdict(closed=True, open_items=0), False, False, False),
            (ClosureVerdict(closed=True, open_items=0), True, True, False),
        ],
    )
    def test_month_closes_only_with_evidence(self, verdict, confirmed, open_question, expected):
        state = MonthCloseState(package=PACKAGE)
        if confirmed:
            state = d.apply_delivery_confirmation(
                state, DeliveryConfirmation(confirmed=True, evidence_id="ev")
            )
        if open_question:
            state = d.apply_query_received(state, AccountantQuery(query_id="q", text="?"))
        assert d.month_can_close(verdict, state) is expected

    def test_verdicts_are_counted(self):
        state = d.apply_verdict(MonthCloseState(), ClosureVerdict(closed=False, open_items=2))
        assert state.closure_checks == 1 and state.last_verdict.open_items == 2
        assert d.month_rollover(30, 30, False) and d.month_rollover(1, 30, True)
        assert not d.month_rollover(29, 30, False)
