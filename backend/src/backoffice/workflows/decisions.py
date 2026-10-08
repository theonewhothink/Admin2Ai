"""Pure decision logic behind the durable workflows.

No Temporal imports, no clocks, no randomness: every function receives
``now`` explicitly and returns new immutable state, so the same rules run
unchanged inside a workflow (deterministic replay, §45) and in plain unit
tests. The workflow classes are thin interpreters: ask :func:`decide_chase`
what to do, execute it as an activity/timer, fold the result back in with an
``apply_*`` function.

Rules enforced here:

* §3   nothing closes without GREEN evidence (:func:`judge_evidence`);
* §19  disagreement is a CONFLICT for a human, never a guess;
* §22  search first, ask the supplier only if permitted, follow up, escalate;
* §25  outbound requests re-check authorization right before sending;
        hard approvals come only from a verified human (:func:`refusal_before_check`);
* §27  month-end steps run in order at their day offsets; the month closes
        only with a complete package whose delivery is confirmed for *that*
        package, and with every accountant question answered with evidence;
* §57  AMBER is never promoted to GREEN.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum

from pydantic import AwareDatetime

from backoffice.domain.models import Quality

from .contracts import (
    MONTH_STEPS,
    AccountantQuery,
    ActorKind,
    ApprovalDecisionKind,
    ApprovalInput,
    ApprovalRecord,
    ApprovalSignal,
    ApprovalStatus,
    ApproverCheck,
    AuthorizationDecision,
    CancelRequest,
    ChasePolicy,
    ClosureVerdict,
    CompletenessReport,
    Contract,
    DeliveryConfirmation,
    DeliveryReceipt,
    DocumentArrival,
    EvidenceBatch,
    EvidenceOrigin,
    ItemOutcome,
    ItemResolved,
    ItemStatus,
    MatchOutcome,
    MissingInvoiceInput,
    MissingInvoiceResult,
    MonthCard,
    MonthCloseInput,
    MonthCloseState,
    MonthStep,
    OpenCard,
    OwnerAnswer,
    OwnerAnswerKind,
    OwnerItemRef,
    PackageRef,
    QueryResolution,
    SearchOutcome,
    SendPackageRequest,
    StepRecord,
    SupplierMessageKind,
    SupplierMessageReceipt,
    ThreadCheckResult,
    TransactionRef,
    VerificationOutcome,
)

__all__ = [
    "AGENT_ACTOR",
    "CLOCK_TOLERANCE",
    "ChasePhase",
    "ChaseState",
    "ChaseStep",
    "CheckAuthorization",
    "CheckEvidence",
    "CheckThread",
    "Escalate",
    "EscalationReason",
    "EvidenceVerdict",
    "FinalOutcome",
    "Finish",
    "NotifyParent",
    "PendingResolution",
    "RecordOutcome",
    "ResolveOwnerItem",
    "Resolution",
    "UNREADABLE_REASON",
    "Search",
    "SendMessage",
    "VerdictKind",
    "Wait",
    "apply_authorization",
    "apply_cancel",
    "apply_document_arrival",
    "apply_escalation",
    "apply_evidence_verdict",
    "apply_item_resolved",
    "apply_message",
    "apply_outcome_recorded",
    "apply_owner_answer",
    "apply_parent_notified",
    "apply_search",
    "apply_thread_check",
    "chase_phase",
    "chase_result",
    "decide_chase",
    "judge_evidence",
    "ledger_outcome",
    "needs_match",
    "new_chase_state",
    "reply_deadline",
    "unreadable_verdict",
    # approval
    "approval_record",
    "approval_rollover",
    "approval_status_for",
    "refusal_after_check",
    "refusal_before_check",
    "reminder_delay",
    # month close
    "DELIVERY_GRACE",
    "CardResolution",
    "apply_audit",
    "apply_card_resolved",
    "apply_children_started",
    "apply_delivery",
    "apply_delivery_confirmation",
    "apply_month_item_resolved",
    "apply_owner_item_raised",
    "apply_package",
    "apply_query_handled",
    "apply_query_received",
    "apply_send_attempted",
    "apply_send_request",
    "apply_verdict",
    "card_key",
    "card_resolution",
    "cards_to_resolve",
    "child_input",
    "delivery_check_due",
    "delivery_confirmed",
    "delivery_evidence_ids",
    "delivery_overdue",
    "delivery_sent",
    "gaps_to_start",
    "is_more_complete",
    "missing_invoice_workflow_id",
    "month_can_close",
    "month_rollover",
    "next_month_step",
    "open_query_ids",
    "package_complete",
    "package_refresh_due",
    "package_version",
    "record_step",
    "sleep_needed",
    "step_schedule",
]

# Timers fire at, not before, their deadline; allow for sub-second clock noise
# so a deadline that is "reached" does not trigger one more tiny timer.
CLOCK_TOLERANCE = timedelta(seconds=1)

AGENT_ACTOR = "missing-evidence-agent"  # §46 Missing Evidence agent


def _dedupe(ids: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(ids))


# ========== evidence gate (§3, §19, §57)


class VerdictKind(str, Enum):
    CLOSE = "close"
    CONFLICT = "conflict"
    UNCONFIRMED = "unconfirmed"


class EvidenceVerdict(Contract):
    kind: VerdictKind
    evidence_ids: tuple[str, ...] = ()
    document_id: str | None = None
    reasons: tuple[str, ...] = ()


def needs_match(verification: VerificationOutcome) -> bool:
    """Only a GREEN, identified document is worth matching (Verify -> Match, §3)."""
    return verification.quality is Quality.GREEN and bool(verification.document_id)


def judge_evidence(
    verification: VerificationOutcome, match: MatchOutcome | None
) -> EvidenceVerdict:
    """Decide whether evidence closes the item.

    CLOSE needs a GREEN document *and* a GREEN match *and* evidence ids.
    Any RED is a CONFLICT (§19). Everything else, including AMBER, stays
    UNCONFIRMED: it is never rounded up to GREEN (§57).
    """
    reasons = verification.reasons + (match.reasons if match else ())
    if verification.quality is Quality.RED or (match and match.quality is Quality.RED):
        ids = verification.evidence_ids + (match.evidence_ids if match else ())
        return EvidenceVerdict(
            kind=VerdictKind.CONFLICT,
            evidence_ids=_dedupe(ids),
            document_id=verification.document_id,
            reasons=reasons,
        )
    closes = (
        needs_match(verification)
        and match is not None
        and match.matched
        and match.quality is Quality.GREEN
    )
    ids = _dedupe(verification.evidence_ids + (match.evidence_ids if match else ()))
    if closes and ids:
        return EvidenceVerdict(
            kind=VerdictKind.CLOSE,
            evidence_ids=ids,
            document_id=verification.document_id,
            reasons=reasons,
        )
    return EvidenceVerdict(
        kind=VerdictKind.UNCONFIRMED,
        evidence_ids=ids,
        document_id=verification.document_id,
        reasons=reasons,
    )


UNREADABLE_REASON = "the document could not be read"


def unreadable_verdict(batch: EvidenceBatch) -> EvidenceVerdict:
    """Evidence the pipeline permanently failed to read is unconfirmed, never
    closing and never fatal: the chase goes on and the owner can see it (§3, §48)."""
    return EvidenceVerdict(
        kind=VerdictKind.UNCONFIRMED,
        evidence_ids=batch.evidence_ids,
        reasons=(UNREADABLE_REASON,),
    )


# ========== missing-invoice machine (§22)


class EscalationReason(str, Enum):
    NO_REPLY = "no_reply"
    NOT_ALLOWED_TO_ASK = "not_allowed_to_ask"
    NO_SUPPLIER_CONTACT = "no_supplier_contact"
    UNCONFIRMED = "unconfirmed"
    CONFLICT = "conflict"


class Resolution(str, Enum):
    """Why an owner card is being closed (rendered by :mod:`.text`)."""

    FOUND = "found"
    NOT_NEEDED = "not_needed"
    CHECKING_OWNER_DOCUMENT = "checking_owner_document"
    KEEP_CHASING = "keep_chasing"
    OWNER_WILL_HANDLE = "owner_will_handle"
    SUPERSEDED = "superseded"
    CANCELLED = "cancelled"


class ChasePhase(str, Enum):
    SEARCHING = "searching"
    WAITING_TO_ASK = "waiting_to_ask"
    WAITING_FOR_SUPPLIER = "waiting_for_supplier"
    NEEDS_OWNER = "needs_owner"
    OWNER_HANDLING = "owner_handling"
    FINISHED = "finished"


class PendingResolution(Contract):
    item_id: str
    resolution: Resolution


class FinalOutcome(Contract):
    status: ItemStatus
    actor: str
    evidence_ids: tuple[str, ...] = ()
    document_id: str | None = None
    quality: Quality | None = None
    note: str = ""
    superseded_evidence_ids: tuple[str, ...] = ()


class ChaseState(Contract):
    """Everything the missing-invoice workflow knows. Immutable; see ``apply_*``."""

    inbox: int = 0  # bumped by every signal; wakes waits
    search_count: int = 0
    searched_at: AwareDatetime | None = None
    seen_evidence: tuple[str, ...] = ()
    pending: tuple[EvidenceBatch, ...] = ()
    unconfirmed: tuple[str, ...] = ()
    conflict_evidence: tuple[str, ...] = ()
    conflict_acknowledged: tuple[str, ...] = ()
    conflict_reasons: tuple[str, ...] = ()
    authorized: bool | None = None
    authorization_for: int = 0  # message number the last check covered
    owner_allowed_chasing: bool = False
    messages_sent: int = 0
    message_budget: int = 1
    message_evidence_ids: tuple[str, ...] = ()
    thread_id: str | None = None
    first_message_at: AwareDatetime | None = None
    last_message_at: AwareDatetime | None = None
    last_thread_check_at: AwareDatetime | None = None
    supplier_replied: bool = False
    no_contact: bool = False
    escalation: EscalationReason | None = None
    escalated_at: AwareDatetime | None = None
    escalations: int = 0
    owner_item_id: str | None = None
    to_resolve: tuple[PendingResolution, ...] = ()
    owner_handling: bool = False
    final: FinalOutcome | None = None
    outcome_recorded: bool = False
    parent_notified: bool = False


def new_chase_state(policy: ChasePolicy) -> ChaseState:
    """Initial state: one request plus ``max_reminders`` follow-ups."""
    return ChaseState(message_budget=1 + policy.max_reminders)


# ---------- steps


@dataclass(frozen=True)
class Search:
    pass


@dataclass(frozen=True)
class CheckEvidence:
    batch: EvidenceBatch


@dataclass(frozen=True)
class CheckAuthorization:
    message_number: int


@dataclass(frozen=True)
class SendMessage:
    kind: SupplierMessageKind
    message_number: int


@dataclass(frozen=True)
class CheckThread:
    pass


@dataclass(frozen=True)
class Escalate:
    reason: EscalationReason


@dataclass(frozen=True)
class ResolveOwnerItem:
    pending: PendingResolution


@dataclass(frozen=True)
class RecordOutcome:
    outcome: FinalOutcome


@dataclass(frozen=True)
class NotifyParent:
    workflow_id: str
    status: ItemStatus


@dataclass(frozen=True)
class Wait:
    """Sleep until ``until`` (None = until a signal arrives)."""

    until: datetime | None


@dataclass(frozen=True)
class Finish:
    outcome: FinalOutcome


ChaseStep = (
    Search
    | CheckEvidence
    | CheckAuthorization
    | SendMessage
    | CheckThread
    | Escalate
    | ResolveOwnerItem
    | RecordOutcome
    | NotifyParent
    | Wait
    | Finish
)


# ---------- decide


def decide_chase(
    state: ChaseState,
    policy: ChasePolicy,
    now: datetime,
    *,
    report_to: str | None = None,
) -> ChaseStep:
    """The single next thing the missing-invoice workflow should do (§22).

    Order: tidy owner cards, finish, check new evidence, search, surface
    conflicts, respect the owner, poll late replies, chase.
    """
    if state.to_resolve:
        return ResolveOwnerItem(state.to_resolve[0])
    if state.final is not None:
        return _finishing(state, report_to)
    if state.pending:
        return CheckEvidence(state.pending[0])
    if state.search_count == 0:
        return Search()
    if _unescalated_conflict(state):
        return Escalate(EscalationReason.CONFLICT)
    if state.owner_handling:
        return Wait(None)
    if state.escalation is not None:
        return _while_escalated(state, policy, now)
    return _chase(state, policy, now)


def _finishing(state: ChaseState, report_to: str | None) -> ChaseStep:
    assert state.final is not None
    if not state.outcome_recorded:
        return RecordOutcome(state.final)
    if report_to and not state.parent_notified:
        return NotifyParent(report_to, state.final.status)
    return Finish(state.final)


def _unescalated_conflict(state: ChaseState) -> bool:
    return bool(set(state.conflict_evidence) - set(state.conflict_acknowledged))


def reply_deadline(state: ChaseState, policy: ChasePolicy) -> datetime | None:
    """When the supplier's reply window for the last message ends."""
    if state.last_message_at is None:
        return None
    return state.last_message_at + policy.reply_wait


def _last_look(state: ChaseState) -> datetime | None:
    looks = [t for t in (state.last_thread_check_at, state.last_message_at) if t]
    return max(looks) if looks else None


def _poll_thread_until(
    state: ChaseState, interval: timedelta, now: datetime, window_end: datetime
) -> ChaseStep | None:
    """Thread polling every ``interval`` inside a window; None once it is covered."""
    if state.thread_id is None:
        return Wait(window_end) if now + CLOCK_TOLERANCE < window_end else None
    last = _last_look(state)
    assert last is not None  # a thread exists only after a message was sent
    if last + CLOCK_TOLERANCE >= window_end:
        return None
    next_check = min(last + interval, window_end)
    if now + CLOCK_TOLERANCE >= next_check:
        return CheckThread()
    return Wait(next_check)


def _while_escalated(state: ChaseState, policy: ChasePolicy, now: datetime) -> ChaseStep:
    """Waiting for the owner; keep listening for a late supplier reply."""
    deadline = reply_deadline(state, policy)
    if deadline is None or state.thread_id is None:
        return Wait(None)
    step = _poll_thread_until(
        state, policy.late_reply_check_interval, now, deadline + policy.late_reply_window
    )
    return step if step is not None else Wait(None)


def _reason(state: ChaseState, otherwise: EscalationReason) -> EscalationReason:
    """A candidate we could not confirm is what the owner needs to see first:
    the card then carries that document (e.g. their own upload), §35."""
    return EscalationReason.UNCONFIRMED if state.unconfirmed else otherwise


def _chase(state: ChaseState, policy: ChasePolicy, now: datetime) -> ChaseStep:
    if state.no_contact:
        return Escalate(_reason(state, EscalationReason.NO_SUPPLIER_CONTACT))
    number = state.messages_sent + 1
    if state.messages_sent == 0:
        hold = policy.chase_not_before
        if hold is not None:
            if now + CLOCK_TOLERANCE < hold:
                return Wait(hold)
            if state.searched_at is None or state.searched_at + CLOCK_TOLERANCE < hold:
                return Search()  # look again before bothering the supplier
        kind = SupplierMessageKind.REQUEST
    else:
        deadline = reply_deadline(state, policy)
        assert deadline is not None
        step = _poll_thread_until(state, policy.thread_check_interval, now, deadline)
        if step is not None:
            return step
        if state.messages_sent >= state.message_budget:
            return Escalate(_reason(state, EscalationReason.NO_REPLY))
        assert state.last_message_at is not None
        if state.searched_at is None or state.searched_at <= state.last_message_at:
            return Search()  # it may have arrived some other way since we asked
        kind = SupplierMessageKind.REMINDER
    if not state.owner_allowed_chasing:
        if state.authorization_for != number:
            return CheckAuthorization(number)
        if not state.authorized:
            return Escalate(_reason(state, EscalationReason.NOT_ALLOWED_TO_ASK))
    return SendMessage(kind, number)


# ---------- apply: activity results


def _fresh(state: ChaseState, ids: tuple[str, ...]) -> tuple[str, ...]:
    seen = set(state.seen_evidence)
    return tuple(i for i in _dedupe(ids) if i not in seen)


def _with_batch(state: ChaseState, ids: tuple[str, ...], origin: EvidenceOrigin) -> ChaseState:
    fresh = _fresh(state, ids)
    if not fresh:
        return state
    return state.model_copy(
        update={
            "seen_evidence": state.seen_evidence + fresh,
            "pending": state.pending + (EvidenceBatch(evidence_ids=fresh, origin=origin),),
        }
    )


def _resolve_current(state: ChaseState, resolution: Resolution) -> dict[str, object]:
    """Updates that retire the open owner card, if any."""
    updates: dict[str, object] = {"owner_item_id": None, "escalation": None}
    if state.owner_item_id:
        updates["to_resolve"] = state.to_resolve + (
            PendingResolution(item_id=state.owner_item_id, resolution=resolution),
        )
    return updates


def apply_search(state: ChaseState, outcome: SearchOutcome, now: datetime) -> ChaseState:
    state = _with_batch(state, outcome.evidence_ids, EvidenceOrigin.SEARCH)
    return state.model_copy(update={"search_count": state.search_count + 1, "searched_at": now})


def apply_evidence_verdict(
    state: ChaseState, batch: EvidenceBatch, verdict: EvidenceVerdict
) -> ChaseState:
    """Fold a verification/match verdict for ``batch`` into the state."""
    pending = list(state.pending)
    if batch in pending:
        pending.remove(batch)
    state = state.model_copy(update={"pending": tuple(pending)})
    if state.final is not None:  # cancelled or answered meanwhile: that wins
        return state
    if verdict.kind is VerdictKind.CLOSE:
        final = FinalOutcome(
            status=ItemStatus.CLOSED,
            actor=AGENT_ACTOR,
            evidence_ids=verdict.evidence_ids,
            document_id=verdict.document_id,
            quality=Quality.GREEN,
            note="; ".join(verdict.reasons),
            superseded_evidence_ids=state.conflict_evidence,
        )
        return state.model_copy(
            update={"final": final, **_resolve_current(state, Resolution.FOUND)}
        )
    if verdict.kind is VerdictKind.CONFLICT:
        updates = {
            "conflict_evidence": _dedupe(
                state.conflict_evidence + batch.evidence_ids + verdict.evidence_ids
            ),
            "conflict_reasons": _dedupe(state.conflict_reasons + verdict.reasons),
            # A new conflict always replaces the open card with a fresh one.
            **_resolve_current(state, Resolution.SUPERSEDED),
        }
        return state.model_copy(update=updates)
    updates = {"unconfirmed": _dedupe(state.unconfirmed + batch.evidence_ids)}
    if state.escalation in _NOTHING_FOUND:
        # The open card says nothing was found; show the owner this candidate
        # instead (a conflict card stays: it matters more).
        updates.update(_resolve_current(state, Resolution.SUPERSEDED))
    return state.model_copy(update=updates)


# Escalations whose card tells the owner that nothing usable was found.
_NOTHING_FOUND = frozenset(
    {
        EscalationReason.NO_REPLY,
        EscalationReason.NOT_ALLOWED_TO_ASK,
        EscalationReason.NO_SUPPLIER_CONTACT,
    }
)


def apply_authorization(
    state: ChaseState, message_number: int, decision: AuthorizationDecision
) -> ChaseState:
    return state.model_copy(
        update={"authorized": decision.authorized, "authorization_for": message_number}
    )


def apply_message(state: ChaseState, receipt: SupplierMessageReceipt, now: datetime) -> ChaseState:
    if not receipt.sent:
        return state.model_copy(update={"no_contact": True})
    evidence = (receipt.message_evidence_id,) if receipt.message_evidence_id else ()
    return state.model_copy(
        update={
            "messages_sent": state.messages_sent + 1,
            "thread_id": receipt.thread_id or state.thread_id,
            "first_message_at": state.first_message_at or now,
            "last_message_at": now,
            "no_contact": False,
            "message_evidence_ids": state.message_evidence_ids + evidence,
        }
    )


def apply_thread_check(state: ChaseState, result: ThreadCheckResult, now: datetime) -> ChaseState:
    state = _with_batch(state, result.evidence_ids, EvidenceOrigin.SUPPLIER_REPLY)
    return state.model_copy(
        update={
            "last_thread_check_at": now,
            "supplier_replied": state.supplier_replied or result.replied,
        }
    )


def _resolution_for_final(final: FinalOutcome) -> Resolution:
    return {
        ItemStatus.CLOSED: Resolution.FOUND,
        ItemStatus.NOT_REQUIRED: Resolution.NOT_NEEDED,
        ItemStatus.CANCELLED: Resolution.CANCELLED,
    }[final.status]


def apply_escalation(
    state: ChaseState, reason: EscalationReason, item: OwnerItemRef, now: datetime
) -> ChaseState:
    if state.final is not None:  # finished while the card was being created
        pending = PendingResolution(
            item_id=item.item_id, resolution=_resolution_for_final(state.final)
        )
        return state.model_copy(update={"to_resolve": state.to_resolve + (pending,)})
    acknowledged = (
        state.conflict_evidence
        if reason is EscalationReason.CONFLICT
        else state.conflict_acknowledged
    )
    return state.model_copy(
        update={
            "escalation": reason,
            "owner_item_id": item.item_id,
            "escalations": state.escalations + 1,
            "escalated_at": now,
            "conflict_acknowledged": acknowledged,
        }
    )


def apply_item_resolved(state: ChaseState, item_id: str) -> ChaseState:
    rest = tuple(r for r in state.to_resolve if r.item_id != item_id)
    return state.model_copy(update={"to_resolve": rest})


def apply_outcome_recorded(state: ChaseState) -> ChaseState:
    return state.model_copy(update={"outcome_recorded": True})


def apply_parent_notified(state: ChaseState) -> ChaseState:
    return state.model_copy(update={"parent_notified": True})


# ---------- apply: signals


def _bump(state: ChaseState) -> ChaseState:
    return state.model_copy(update={"inbox": state.inbox + 1})


def apply_document_arrival(state: ChaseState, arrival: DocumentArrival) -> ChaseState:
    state = _bump(state)
    if state.final is not None:
        return state
    return _with_batch(state, arrival.evidence_ids, EvidenceOrigin.PIPELINE)


_ANSWER_RESOLUTION: dict[OwnerAnswerKind, Resolution] = {
    OwnerAnswerKind.DOCUMENT_PROVIDED: Resolution.CHECKING_OWNER_DOCUMENT,
    OwnerAnswerKind.NO_DOCUMENT_NEEDED: Resolution.NOT_NEEDED,
    OwnerAnswerKind.KEEP_CHASING: Resolution.KEEP_CHASING,
    OwnerAnswerKind.OWNER_WILL_HANDLE: Resolution.OWNER_WILL_HANDLE,
}


def apply_owner_answer(state: ChaseState, answer: OwnerAnswer, policy: ChasePolicy) -> ChaseState:
    """The owner decided (§35). Their answer is evidence (§54)."""
    state = _bump(state)
    if state.final is not None:
        return state
    updates: dict[str, object] = {
        **_resolve_current(state, _ANSWER_RESOLUTION[answer.kind]),
        "conflict_acknowledged": state.conflict_evidence,
    }
    if answer.kind is OwnerAnswerKind.DOCUMENT_PROVIDED:
        # Always verify what the owner points at, together with their answer.
        ids = _dedupe(answer.evidence_ids + (answer.answer_evidence_id,))
        batch = EvidenceBatch(evidence_ids=ids, origin=EvidenceOrigin.OWNER)
        updates["pending"] = state.pending + (batch,)
        updates["seen_evidence"] = _dedupe(state.seen_evidence + ids)
    elif answer.kind is OwnerAnswerKind.NO_DOCUMENT_NEEDED:
        updates["final"] = FinalOutcome(
            status=ItemStatus.NOT_REQUIRED,
            actor=answer.answered_by,
            evidence_ids=(answer.answer_evidence_id,),
            note="The owner said no invoice is needed.",
        )
    elif answer.kind is OwnerAnswerKind.KEEP_CHASING:
        updates.update(
            owner_allowed_chasing=True,
            owner_handling=False,
            no_contact=False,
            message_budget=state.messages_sent + 1 + policy.max_reminders,
        )
    else:
        updates["owner_handling"] = True
    return state.model_copy(update=updates)


def apply_cancel(state: ChaseState, request: CancelRequest) -> ChaseState:
    state = _bump(state)
    if state.final is not None:
        return state
    final = FinalOutcome(
        status=ItemStatus.CANCELLED, actor=request.requested_by, note=request.reason
    )
    return state.model_copy(
        update={"final": final, **_resolve_current(state, Resolution.CANCELLED)}
    )


# ---------- views


def chase_phase(state: ChaseState, policy: ChasePolicy) -> ChasePhase:
    if state.final is not None:
        return ChasePhase.FINISHED
    if state.escalation is not None or _unescalated_conflict(state):
        return ChasePhase.NEEDS_OWNER
    if state.owner_handling:
        return ChasePhase.OWNER_HANDLING
    if state.messages_sent > 0:
        return ChasePhase.WAITING_FOR_SUPPLIER
    if policy.chase_not_before is not None and state.search_count > 0:
        return ChasePhase.WAITING_TO_ASK
    return ChasePhase.SEARCHING


def ledger_outcome(state: ChaseState, tx: TransactionRef) -> ItemOutcome:
    """The lifecycle record to persist; validation re-checks the golden rule."""
    final = state.final
    if final is None:
        raise ValueError("the item has not finished")
    return ItemOutcome(
        tenant_id=tx.tenant_id,
        subject_id=tx.transaction_id,
        status=final.status,
        quality=final.quality,
        evidence_ids=final.evidence_ids,
        document_id=final.document_id,
        actor=final.actor,
        note=final.note,
        superseded_evidence_ids=final.superseded_evidence_ids,
    )


def chase_result(state: ChaseState, tx: TransactionRef, summary: str) -> MissingInvoiceResult:
    final = state.final
    if final is None:
        raise ValueError("the item has not finished")
    return MissingInvoiceResult(
        status=final.status,
        transaction_id=tx.transaction_id,
        evidence_ids=final.evidence_ids,
        document_id=final.document_id,
        messages_sent=state.messages_sent,
        escalations=state.escalations,
        superseded_evidence_ids=final.superseded_evidence_ids,
        summary=summary,
    )


# ========== hard approval (§25, §26)


def refusal_before_check(
    signal: ApprovalSignal,
    inp: ApprovalInput,
    sent_as: ApprovalDecisionKind | None = None,
) -> str | None:
    """Why a decision cannot even be considered, or None. Internal audit text.

    Only a person may decide; the requester may not approve their own request;
    AI agents never approve (§25, §26). ``sent_as`` is the signal it came in
    on; an 'approve' signal carrying a rejection (or vice versa) is refused.
    """
    if sent_as is not None and sent_as is not signal.decision:
        return "the decision does not match how it was sent"
    if signal.actor_kind is not ActorKind.HUMAN:
        return "only a person can decide a hard approval"
    actor = signal.actor_id.strip()
    if not actor:
        return "the decision does not say who made it"
    if not signal.assertion_id.strip():
        return "no proof of a fresh sign-in"
    if actor == inp.requested_by.strip():
        return "the requester cannot decide their own request"
    if inp.eligible_approvers and actor not in inp.eligible_approvers:
        return "this person may not decide this request"
    return None


def refusal_after_check(check: ApproverCheck) -> str | None:
    if not check.verified:
        return check.reason or "sign-in could not be confirmed"
    if not (check.auth_method or "").strip():
        return "sign-in method unknown"
    return None


def reminder_delay(reminders_sent: int, first: timedelta, cap: timedelta) -> timedelta:
    """Gentle backoff between 'still needs approval' reminders: 1d, 2d, 4d, then cap."""
    if reminders_sent < 0:
        raise ValueError("reminders_sent cannot be negative")
    delay = first
    for _ in range(reminders_sent):
        if delay >= cap:
            break
        delay *= 2
    return min(delay, cap)


def approval_record(
    signal: ApprovalSignal, check: ApproverCheck, request_id: str, now: datetime
) -> ApprovalRecord:
    """Who decided and when; ``now`` must be ``workflow.now()`` (deterministic)."""
    return ApprovalRecord(
        request_id=request_id,
        decision=signal.decision,
        actor_id=signal.actor_id.strip(),
        actor_display_name=check.display_name,
        auth_method=(check.auth_method or "").strip(),
        decided_at=now,
        note=signal.note,
    )


def approval_status_for(decision: ApprovalDecisionKind) -> ApprovalStatus:
    return (
        ApprovalStatus.APPROVED
        if decision is ApprovalDecisionKind.APPROVE
        else ApprovalStatus.REJECTED
    )


def approval_rollover(reminders_this_run: int, per_run: int, suggested: bool) -> bool:
    """Continue-as-new to keep history bounded while waiting indefinitely."""
    return suggested or reminders_this_run >= per_run


# ========== month close (§27)


def step_schedule(inp: MonthCloseInput) -> dict[MonthStep, datetime]:
    """UTC run time of every step; CLOSURE runs right after the last step."""
    out: dict[MonthStep, datetime] = {}
    for step in MONTH_STEPS:
        key = MonthStep.ACCOUNTANT_QUERIES if step is MonthStep.CLOSURE else step
        day = inp.day_zero + timedelta(days=inp.day_offsets[key])
        out[step] = datetime.combine(day, inp.run_at, tzinfo=timezone.utc)
    return out


def next_month_step(state: MonthCloseState) -> MonthStep | None:
    done = {r.step for r in state.completed}
    return next((s for s in MONTH_STEPS if s not in done), None)


def sleep_needed(now: datetime, target: datetime) -> timedelta | None:
    """How long to sleep before ``target``; None when it is already due."""
    return target - now if now + CLOCK_TOLERANCE < target else None


def record_step(
    state: MonthCloseState, step: MonthStep, scheduled_for: datetime, ran_at: datetime
) -> MonthCloseState:
    if any(r.step is step for r in state.completed):
        raise ValueError(f"{step.value} already ran")
    record = StepRecord(step=step, scheduled_for=scheduled_for, ran_at=ran_at)
    return state.model_copy(update={"completed": state.completed + (record,)})


def missing_invoice_workflow_id(tenant_id: str, transaction_id: str) -> str:
    """Deterministic id: one chase per transaction, never two in parallel."""
    return f"missing-invoice:{tenant_id}:{transaction_id}"


def gaps_to_start(
    gaps: tuple[TransactionRef, ...], already: tuple[str, ...]
) -> tuple[TransactionRef, ...]:
    """Gaps without a chase yet, first occurrence wins, order kept."""
    skip = set(already)
    out: list[TransactionRef] = []
    for gap in gaps:
        if gap.transaction_id not in skip:
            skip.add(gap.transaction_id)
            out.append(gap)
    return tuple(out)


def child_input(
    gap: TransactionRef,
    policy: ChasePolicy,
    chase_not_before: datetime | None,
    report_to: str,
) -> MissingInvoiceInput:
    return MissingInvoiceInput(
        transaction=gap,
        policy=policy.model_copy(update={"chase_not_before": chase_not_before}),
        report_to_workflow_id=report_to,
    )


def apply_audit(state: MonthCloseState, report: CompletenessReport) -> MonthCloseState:
    return state.model_copy(update={"audit": report, "gaps": report.gaps})


def apply_children_started(
    state: MonthCloseState, started: tuple[str, ...], already_running: tuple[str, ...]
) -> MonthCloseState:
    return state.model_copy(
        update={
            "chased_ids": _dedupe(state.chased_ids + started + already_running),
            "already_running_ids": _dedupe(state.already_running_ids + already_running),
        }
    )


def _known_query_ids(state: MonthCloseState) -> set[str]:
    asked = state.pending_queries + state.waiting_queries
    return {q.query_id for q in asked} | {r.query_id for r in state.handled_queries}


def apply_query_received(state: MonthCloseState, query: AccountantQuery) -> MonthCloseState:
    if query.query_id in _known_query_ids(state):
        return state
    return state.model_copy(update={"pending_queries": state.pending_queries + (query,)})


def apply_query_handled(state: MonthCloseState, resolution: QueryResolution) -> MonthCloseState:
    """Only an answer backed by evidence settles a question (§3, §28); any other
    outcome leaves it waiting, to be tried again at the next closure check."""
    qid = resolution.query_id
    asked = next(
        (q for q in state.pending_queries + state.waiting_queries if q.query_id == qid), None
    )
    if asked is None:
        return state
    pending = tuple(q for q in state.pending_queries if q.query_id != qid)
    waiting = tuple(q for q in state.waiting_queries if q.query_id != qid)
    if resolution.resolved:
        return state.model_copy(
            update={
                "pending_queries": pending,
                "waiting_queries": waiting,
                "handled_queries": state.handled_queries + (resolution,),
            }
        )
    return state.model_copy(
        update={"pending_queries": pending, "waiting_queries": waiting + (asked,)}
    )


def apply_month_item_resolved(state: MonthCloseState, item: ItemResolved) -> MonthCloseState:
    """A chase finished. Recorded for the status line; it settles nothing by
    itself: the closure verdict and evidence do (§3)."""
    ids = _dedupe(state.resolved_subject_ids + (item.subject_id,))
    return state.model_copy(update={"resolved_subject_ids": ids})


def open_query_ids(state: MonthCloseState) -> tuple[str, ...]:
    """Accountant questions not yet answered with evidence."""
    return _dedupe(tuple(q.query_id for q in state.pending_queries + state.waiting_queries))


# ---------- the accountant package (§27-28)

DELIVERY_GRACE = timedelta(days=1)  # §27: package on Day 0, confirmed by Day +1


def package_version(state: MonthCloseState) -> int:
    """1 for the first package, plus one for each more complete one after it."""
    return len(state.earlier_packages) + 1


def is_more_complete(current: PackageRef | None, new: PackageRef) -> bool:
    """A package replaces the current one only when fewer items are missing,
    so the accountant is never sent the same gaps twice."""
    return current is None or new.missing_items < current.missing_items


def apply_package(state: MonthCloseState, package: PackageRef) -> MonthCloseState:
    """Adopt the first package, or a more complete one; its delivery starts afresh."""
    current = state.package
    if not is_more_complete(current, package):
        return state
    earlier = state.earlier_packages + ((current,) if current else ())
    return state.model_copy(
        update={
            "package": package,
            "earlier_packages": earlier,
            "delivery": None,
            "delivered_at": None,
            "delivery_confirmation": None,
            "send_request": None,
        }
    )


def package_complete(state: MonthCloseState) -> bool:
    return state.package is not None and state.package.missing_items == 0


def _verdict_clear(verdict: ClosureVerdict) -> bool:
    return verdict.closed and verdict.open_items == 0 and verdict.summary.unresolved_issues == 0


def package_refresh_due(verdict: ClosureVerdict, state: MonthCloseState) -> bool:
    """Everything is resolved, but the accountant's package still has gaps:
    a more complete package must reach them before the month closes (§27)."""
    return _verdict_clear(verdict) and state.package is not None and not package_complete(state)


def delivery_sent(state: MonthCloseState) -> bool:
    return bool(state.delivery and state.delivery.delivered)


def delivery_confirmed(state: MonthCloseState) -> bool:
    c = state.delivery_confirmation
    return bool(c and c.confirmed and c.evidence_id)


def delivery_check_due(state: MonthCloseState) -> bool:
    """Sent but not yet confirmed: worth asking the delivery service (again)."""
    return delivery_sent(state) and not delivery_confirmed(state)


def delivery_overdue(state: MonthCloseState, now: datetime) -> bool:
    """Still unconfirmed a day after sending: time to ask the owner (§27)."""
    sent_at = state.delivered_at
    return (
        delivery_check_due(state)
        and sent_at is not None
        and now + CLOCK_TOLERANCE >= sent_at + DELIVERY_GRACE
    )


def apply_delivery(
    state: MonthCloseState, receipt: DeliveryReceipt, now: datetime | None = None
) -> MonthCloseState:
    """Record a delivery attempt of the current package (``now`` = workflow time)."""
    updates: dict[str, object] = {"delivery": receipt}
    if receipt.delivered:
        updates["delivered_at"] = now
        updates["send_request"] = None
    if receipt.confirmed and receipt.confirmation_evidence_id:
        updates["delivery_confirmation"] = DeliveryConfirmation(
            confirmed=True,
            evidence_id=receipt.confirmation_evidence_id,
            package_id=state.package.package_id if state.package else None,
        )
    return state.model_copy(update=updates)


def apply_delivery_confirmation(
    state: MonthCloseState, confirmation: DeliveryConfirmation
) -> MonthCloseState:
    """Only an evidenced confirmation of *this* package counts (§3): one that
    arrives before any package exists, or names another package, is ignored,
    and a 'not yet' never erases a confirmation."""
    package = state.package
    if not confirmation.confirmed or delivery_confirmed(state) or package is None:
        return state
    if confirmation.package_id not in (None, package.package_id):
        return state
    pinned = confirmation.model_copy(update={"package_id": package.package_id})
    return state.model_copy(update={"delivery_confirmation": pinned})


def apply_send_request(state: MonthCloseState, request: SendPackageRequest) -> MonthCloseState:
    """The owner's "Send it for me" is kept only while there is an unsent package."""
    package = state.package
    if package is None or delivery_sent(state) or delivery_confirmed(state):
        return state
    if request.package_id not in (None, package.package_id):
        return state
    return state.model_copy(update={"send_request": request})


def apply_send_attempted(state: MonthCloseState) -> MonthCloseState:
    return state.model_copy(update={"send_request": None, "send_attempts": state.send_attempts + 1})


def delivery_evidence_ids(state: MonthCloseState) -> tuple[str, ...]:
    """Evidence that the *current* package reached the accountant."""
    ids: list[str] = []
    if state.delivery and state.delivery.delivery_evidence_id:
        ids.append(state.delivery.delivery_evidence_id)
    if state.delivery_confirmation and state.delivery_confirmation.evidence_id:
        ids.append(state.delivery_confirmation.evidence_id)
    return _dedupe(tuple(ids))


# ---------- Needs-You cards raised by the month close (§35, §69)


class CardResolution(str, Enum):
    """Why a month-close card is taken down (rendered by :mod:`.text`)."""

    SENT = "sent"
    RECEIVED = "received"
    ANSWERED = "answered"
    SUPERSEDED = "superseded"
    MONTH_CLOSED = "month_closed"


def card_key(
    card: MonthCard, *, version: int = 0, attempt: int = 0, query_id: str | None = None
) -> str:
    """Dedupe key: one card per package version / owner attempt / question."""
    if card is MonthCard.ACCOUNTANT_QUESTION:
        return f"{card.value}:{query_id}"
    if card is MonthCard.PACKAGE_READY:
        return f"{card.value}:{version}:{attempt}"
    return f"{card.value}:{version}"


def apply_owner_item_raised(
    state: MonthCloseState, dedupe_key: str, card: OpenCard | None = None
) -> MonthCloseState:
    updates: dict[str, object] = {"owner_items": _dedupe(state.owner_items + (dedupe_key,))}
    if card is not None:
        updates["open_cards"] = state.open_cards + (card,)
    return state.model_copy(update=updates)


def card_resolution(
    card: OpenCard, state: MonthCloseState, *, closing: bool = False
) -> CardResolution | None:
    """Whether an open card's situation has resolved, and how."""
    if card.card is MonthCard.ACCOUNTANT_QUESTION:
        if card.query_id in {r.query_id for r in state.handled_queries}:
            return CardResolution.ANSWERED
    elif card.version < package_version(state):
        return CardResolution.SUPERSEDED
    elif delivery_confirmed(state):
        return CardResolution.RECEIVED
    elif card.card is MonthCard.PACKAGE_READY:
        if delivery_sent(state):
            return CardResolution.SENT
        if card.attempt < state.send_attempts:  # the owner acted; a newer card follows
            return CardResolution.SUPERSEDED
    return CardResolution.MONTH_CLOSED if closing else None


def cards_to_resolve(
    state: MonthCloseState, *, closing: bool = False
) -> tuple[tuple[OpenCard, CardResolution], ...]:
    pairs = ((c, card_resolution(c, state, closing=closing)) for c in state.open_cards)
    return tuple((c, r) for c, r in pairs if r is not None)


def apply_card_resolved(state: MonthCloseState, item_id: str) -> MonthCloseState:
    rest = tuple(c for c in state.open_cards if c.item_id != item_id)
    return state.model_copy(update={"open_cards": rest})


# ---------- closure


def apply_verdict(state: MonthCloseState, verdict: ClosureVerdict) -> MonthCloseState:
    return state.model_copy(
        update={"last_verdict": verdict, "closure_checks": state.closure_checks + 1}
    )


def month_can_close(verdict: ClosureVerdict, state: MonthCloseState) -> bool:
    """MONTH CLOSED only with evidence (§27, §3): the closure verdict, a
    complete package whose delivery is confirmed, and every accountant
    question answered with evidence."""
    return (
        _verdict_clear(verdict)
        and package_complete(state)
        and delivery_confirmed(state)
        and not open_query_ids(state)
    )


def month_rollover(checks_this_run: int, per_run: int, suggested: bool) -> bool:
    return suggested or checks_this_run >= per_run
