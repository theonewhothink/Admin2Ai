"""MissingInvoiceWorkflow: find, ask, wait, remind, escalate, close (§22, §45).

One run per transaction that lacks evidence. The workflow is a thin,
deterministic interpreter: :func:`~.decisions.decide_chase` picks the next
step from immutable state, the step runs as an activity or a durable wait,
and the result is folded back with an ``apply_*`` function.

Typical life (defaults from :class:`~.contracts.ChasePolicy`):

    search -> nothing verified -> authorized? -> ask supplier
      -> wait up to 6 days (thread checked every 12 h, or a document signal)
      -> reminder (at most ``max_reminders``) -> ...
      -> still nothing: a Needs-You card for the owner (no push, §42),
         while the thread is still checked daily for 30 days for a late reply
    any time: evidence arrives -> verify -> match -> GREEN -> close.

Nothing closes on AMBER (§57); a RED document goes to the owner (§19).
One bad outside input never ends the chase: a document the pipeline cannot
read stays unconfirmed, a thread that can no longer be read shows no reply,
an address that bounces means no usable contact. A refund (money in) is
chased for its credit note. Long chases continue-as-new, carrying their state.
Cancelling the execution itself (e.g. from the Temporal UI) tidies up like
the ``cancel`` signal before the run ends as cancelled.

Signals: ``document_received``, ``owner_answered``, ``cancel``.
Queries: ``current_status`` (plain language), ``progress`` (structured).
Workflow code never reads the wall clock; it uses ``workflow.now()``.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from datetime import datetime
from typing import Any

from temporalio import workflow
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from . import text
    from .activities import (
        BackofficeActivities,
        activity_options,
        execute_or,
        is_permanent_failure,
    )
    from .contracts import (
        ITEM_RESOLVED_SIGNAL,
        AuthorizationRequest,
        CancelRequest,
        DocumentArrival,
        EvidenceBatch,
        GatedAction,
        ItemResolved,
        MissingInvoiceInput,
        MissingInvoiceProgress,
        MissingInvoiceResult,
        NeedsYouItem,
        OwnerAnswer,
        OwnerItemResolution,
        ReconcileRequest,
        SearchRequest,
        SupplierMessageReceipt,
        SupplierRequest,
        ThreadCheckRequest,
        ThreadCheckResult,
        VerifyRequest,
    )
    from .decisions import (
        ChaseState,
        CheckAuthorization,
        CheckEvidence,
        CheckThread,
        Escalate,
        EscalationReason,
        EvidenceVerdict,
        Finish,
        NotifyParent,
        RecordOutcome,
        ResolveOwnerItem,
        Search,
        SendMessage,
        Wait,
        apply_authorization,
        apply_cancel,
        apply_document_arrival,
        apply_escalation,
        apply_evidence_verdict,
        apply_item_resolved,
        apply_message,
        apply_outcome_recorded,
        apply_owner_answer,
        apply_parent_notified,
        apply_search,
        apply_thread_check,
        chase_phase,
        chase_result,
        decide_chase,
        judge_evidence,
        ledger_outcome,
        needs_match,
        new_chase_state,
        unreadable_verdict,
    )

__all__ = ["MissingInvoiceWorkflow"]


@workflow.defn(name="MissingInvoiceWorkflow")
class MissingInvoiceWorkflow:
    """Durable chase for one transaction's missing invoice (§22)."""

    @workflow.init
    def __init__(self, inp: MissingInvoiceInput) -> None:
        self._input = inp
        self._state: ChaseState = (
            ChaseState.model_validate(inp.carry) if inp.carry else new_chase_state(inp.policy)
        )

    # ---------- run

    @workflow.run
    async def run(self, inp: MissingInvoiceInput) -> MissingInvoiceResult:
        try:
            return await self._drive(inp)
        except asyncio.CancelledError:
            # Stopped by an operator: finish like a cancel signal (tidy the owner
            # card, record the outcome, tell the parent), then stop as cancelled.
            stop = CancelRequest(requested_by="system", reason="stopped by an operator")
            self._state = apply_cancel(self._state, stop)
            await self._drive(inp)
            raise

    async def _drive(self, inp: MissingInvoiceInput) -> MissingInvoiceResult:
        while True:
            step = decide_chase(
                self._state,
                inp.policy,
                workflow.now(),
                report_to=inp.report_to_workflow_id,
            )
            if isinstance(step, Finish):
                summary = text.chase_summary(step.outcome.status, inp.transaction)
                return chase_result(self._state, inp.transaction, summary)
            if isinstance(step, Wait) and workflow.info().is_continue_as_new_suggested():
                await self._continue_as_new(inp)
            await self._perform(step)

    # ---------- signals

    @workflow.signal
    def document_received(self, arrival: DocumentArrival) -> None:
        """Evidence that may be the invoice arrived through another channel."""
        self._state = apply_document_arrival(self._state, arrival)

    @workflow.signal
    def owner_answered(self, answer: OwnerAnswer) -> None:
        """The owner answered the Needs-You card (or acted on their own)."""
        self._state = apply_owner_answer(self._state, answer, self._input.policy)

    @workflow.signal
    def cancel(self, request: CancelRequest) -> None:
        """Stop chasing (e.g. the payment turned out not to need an invoice)."""
        self._state = apply_cancel(self._state, request)

    # ---------- queries

    @workflow.query
    def current_status(self) -> str:
        return text.chase_status_text(self._state, self._input.transaction, self._input.policy)

    @workflow.query
    def progress(self) -> MissingInvoiceProgress:
        state = self._state
        return MissingInvoiceProgress(
            phase=chase_phase(state, self._input.policy).value,
            status_text=self.current_status(),
            messages_sent=state.messages_sent,
            needs_owner=state.escalation is not None,
            finished=state.final is not None,
            last_message_at=state.last_message_at,
        )

    # ---------- steps

    async def _act(self, fn: Callable[..., Any], arg: Any) -> Any:
        return await workflow.execute_activity_method(fn, arg, **activity_options(fn.__name__))

    async def _perform(self, step: object) -> None:
        tx = self._input.transaction
        match step:
            case Search():
                request = SearchRequest(
                    transaction=tx, exclude_evidence_ids=self._state.seen_evidence
                )
                outcome = await self._act(BackofficeActivities.search_for_evidence, request)
                self._state = apply_search(self._state, outcome, workflow.now())
            case CheckEvidence(batch=batch):
                verdict = await self._check_evidence(batch)
                self._state = apply_evidence_verdict(self._state, batch, verdict)
            case CheckAuthorization(message_number=number):
                decision = await self._act(
                    BackofficeActivities.is_action_authorized,
                    AuthorizationRequest(
                        tenant_id=tx.tenant_id,
                        entity_id=tx.entity_id,
                        action=GatedAction.SUPPLIER_INVOICE_REQUEST,
                        subject_id=tx.transaction_id,
                    ),
                )
                self._state = apply_authorization(self._state, number, decision)
            case SendMessage(kind=kind, message_number=number):
                now = workflow.now()
                request = SupplierRequest(
                    transaction=tx,
                    kind=kind,
                    message_number=number,
                    thread_id=self._state.thread_id,
                    default_body=text.supplier_request_body(tx, kind, now.date()),
                    idempotency_key=f"{workflow.info().workflow_id}:message:{number}",
                )
                # An address that can never receive mail means no usable contact.
                receipt = await execute_or(
                    BackofficeActivities.send_supplier_request,
                    request,
                    SupplierMessageReceipt(sent=False, reason="could not be delivered"),
                )
                self._state = apply_message(self._state, receipt, workflow.now())
            case CheckThread():
                assert self._state.thread_id and self._state.first_message_at
                # A thread that can no longer be read simply shows no reply;
                # reminders, other channels and the owner still work.
                result = await execute_or(
                    BackofficeActivities.check_thread_for_reply,
                    ThreadCheckRequest(
                        transaction=tx,
                        thread_id=self._state.thread_id,
                        since=self._state.first_message_at,
                    ),
                    ThreadCheckResult(replied=False),
                )
                self._state = apply_thread_check(self._state, result, workflow.now())
            case Escalate(reason=reason):
                await self._escalate(reason)
            case ResolveOwnerItem(pending=pending):
                await self._act(
                    BackofficeActivities.resolve_owner_item,
                    OwnerItemResolution(
                        tenant_id=tx.tenant_id,
                        item_id=pending.item_id,
                        message=text.resolution_message(pending.resolution, tx),
                    ),
                )
                self._state = apply_item_resolved(self._state, pending.item_id)
            case RecordOutcome():
                await self._act(
                    BackofficeActivities.record_item_outcome,
                    ledger_outcome(self._state, tx),
                )
                self._state = apply_outcome_recorded(self._state)
            case NotifyParent(workflow_id=parent_id, status=status):
                await self._notify_parent(
                    parent_id, ItemResolved(subject_id=tx.transaction_id, status=status)
                )
            case Wait(until=until):
                await self._wait(until)
            case _:
                raise TypeError(f"unknown step {step!r}")

    async def _check_evidence(self, batch: EvidenceBatch) -> EvidenceVerdict:
        """Understand -> Verify -> Match (§3). A document the pipeline can never
        read is unconfirmed, not a reason to abandon the chase."""
        tx = self._input.transaction
        try:
            verification = await self._act(
                BackofficeActivities.ingest_and_verify,
                VerifyRequest(transaction=tx, evidence_ids=batch.evidence_ids),
            )
            match_outcome = None
            if needs_match(verification):
                match_outcome = await self._act(
                    BackofficeActivities.reconcile_transaction,
                    ReconcileRequest(
                        transaction=tx,
                        document_id=verification.document_id,
                        evidence_ids=verification.evidence_ids,
                    ),
                )
        except ActivityError as err:
            if not is_permanent_failure(err):
                raise
            workflow.logger.warning("Evidence could not be checked; kept as unconfirmed.")
            return unreadable_verdict(batch)
        return judge_evidence(verification, match_outcome)

    async def _continue_as_new(self, inp: MissingInvoiceInput) -> None:
        """Restart with fresh history, carrying the chase state (§45; raises)."""
        await workflow.wait_condition(workflow.all_handlers_finished)
        carry = self._state.model_dump(mode="json")
        workflow.continue_as_new(inp.model_copy(update={"carry": carry}))

    async def _escalate(self, reason: EscalationReason) -> None:
        tx, state = self._input.transaction, self._state
        card = text.chase_card(reason, tx, state, workflow.now().date())
        evidence = {
            EscalationReason.CONFLICT: state.conflict_evidence,
            EscalationReason.UNCONFIRMED: state.unconfirmed,
        }.get(reason, state.message_evidence_ids)
        item = NeedsYouItem(
            tenant_id=tx.tenant_id,
            kind=card.kind,
            headline=card.headline,
            detail=card.detail,
            why=card.why,
            options=card.options,
            subject_id=tx.transaction_id,
            evidence_ids=evidence,
            dedupe_key=f"{workflow.info().workflow_id}:needs-you:{state.escalations + 1}",
            notify=False,  # §42: a missing invoice waits in Needs You, no push
        )
        ref = await self._act(BackofficeActivities.notify_owner, item)
        self._state = apply_escalation(self._state, reason, ref, workflow.now())

    async def _notify_parent(self, parent_id: str, message: ItemResolved) -> None:
        try:
            handle = workflow.get_external_workflow_handle(parent_id)
            await handle.signal(ITEM_RESOLVED_SIGNAL, message)
        except Exception:  # parent already finished: the ledger still has the outcome
            workflow.logger.info("Parent workflow could not be told; outcome is recorded.")
        self._state = apply_parent_notified(self._state)

    async def _wait(self, until: datetime | None) -> None:
        seen = self._state.inbox
        timeout = None if until is None else until - workflow.now()
        with contextlib.suppress(TimeoutError):  # woken by the timer or a signal
            await workflow.wait_condition(lambda: self._state.inbox != seen, timeout=timeout)
