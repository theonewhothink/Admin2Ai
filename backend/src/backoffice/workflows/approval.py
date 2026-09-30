"""HardApprovalWorkflow: a decision only a verified person can make (§25-26).

Money movement, tax filing, bank-detail changes, legally binding acceptance
and deletion of original evidence always need hard approval. This workflow:

* raises the approval card (with a push notification, §42);
* waits **indefinitely** for an ``approve``/``reject`` signal;
* refuses any decision not made by a human, by the requester themselves, by
  someone outside ``eligible_approvers``, or whose fresh sign-in the identity
  service cannot confirm (``verify_approver`` activity) and audits the attempt;
* records who decided, how they proved it, and when (``workflow.now()`` at
  the moment the signal arrived);
* re-surfaces the card with "This still needs your approval." on a gentle
  backoff (1d, 2d, 4d, then weekly) and continues-as-new to bound history.

There is no code path that approves on a timer, on a count, or by default.
Cancelling the execution itself takes the card down and approves nothing.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime
from typing import Any

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from . import text
    from .activities import BackofficeActivities, activity_options, execute_or
    from .contracts import (
        ApprovalAuditEvent,
        ApprovalCarry,
        ApprovalDecisionKind,
        ApprovalInput,
        ApprovalProgress,
        ApprovalRecord,
        ApprovalRequest,
        ApprovalResult,
        ApprovalSignal,
        ApprovalStatus,
        ApproverCheck,
        ApproverCheckRequest,
        OwnerItemResolution,
        WithdrawRequest,
    )
    from .decisions import (
        approval_record,
        approval_rollover,
        approval_status_for,
        refusal_after_check,
        refusal_before_check,
        reminder_delay,
    )

__all__ = ["HardApprovalWorkflow"]


@workflow.defn(name="HardApprovalWorkflow")
class HardApprovalWorkflow:
    """Waits for a verified human decision; never approves by itself."""

    @workflow.init
    def __init__(self, inp: ApprovalInput) -> None:
        self._input = inp
        carry = inp.carry
        self._requested_at: datetime | None = carry.requested_at if carry else None
        self._last_prompted_at: datetime | None = carry.last_prompted_at if carry else None
        self._reminders = carry.reminders_sent if carry else 0
        self._refused = carry.refused_attempts if carry else 0
        self._item_id: str | None = carry.owner_item_id if carry else None
        self._inbox: list[tuple[ApprovalSignal, ApprovalDecisionKind, datetime]] = []
        self._withdrawn: WithdrawRequest | None = None
        self._status: ApprovalStatus | None = None
        self._record: ApprovalRecord | None = None

    # ---------- run

    @workflow.run
    async def run(self, inp: ApprovalInput) -> ApprovalResult:
        try:
            return await self._await_decision(inp)
        except asyncio.CancelledError:
            # Stopped by an operator: nothing was approved; take the card down.
            if self._status is None:
                await self._finish_withdrawn(
                    WithdrawRequest(requested_by="system", reason="stopped by an operator")
                )
            raise

    async def _await_decision(self, inp: ApprovalInput) -> ApprovalResult:
        if self._requested_at is None:
            self._requested_at = workflow.now()
            await self._prompt(reminder_number=0)
        reminders_this_run = 0
        while True:
            if self._withdrawn is not None:
                return await self._finish_withdrawn(self._withdrawn)
            if self._inbox:
                signal, sent_as, received_at = self._inbox.pop(0)
                result = await self._consider(signal, sent_as, received_at)
                if result is not None:
                    return result
                continue
            # Idle: nothing queued. Roll over here so that reminders *and*
            # bursts of refused attempts both keep the history bounded (§45).
            suggested = workflow.info().is_continue_as_new_suggested()
            if approval_rollover(reminders_this_run, inp.reminders_per_run, suggested):
                await workflow.wait_condition(workflow.all_handlers_finished)
                if not self._inbox and self._withdrawn is None:
                    workflow.continue_as_new(inp.model_copy(update={"carry": self._carry()}))
                continue
            if not await self._wait_for_decision():
                continue
            self._reminders += 1
            reminders_this_run += 1
            await self._prompt(reminder_number=self._reminders)

    # ---------- signals

    @workflow.signal
    def approve(self, signal: ApprovalSignal) -> None:
        self._receive(signal, ApprovalDecisionKind.APPROVE)

    @workflow.signal
    def reject(self, signal: ApprovalSignal) -> None:
        self._receive(signal, ApprovalDecisionKind.REJECT)

    @workflow.signal
    def withdraw(self, request: WithdrawRequest) -> None:
        """The request is no longer needed. Nothing will be done."""
        if self._withdrawn is None and self._status is None:
            self._withdrawn = request

    # ---------- queries

    @workflow.query
    def current_status(self) -> str:
        status = (
            ApprovalStatus.WITHDRAWN
            if (self._status is None and self._withdrawn is not None)
            else self._status
        )
        return text.approval_status_text(self._input.summary, status, self._record)

    @workflow.query
    def progress(self) -> ApprovalProgress:
        return ApprovalProgress(
            waiting=self._status is None and self._withdrawn is None,
            status_text=self.current_status(),
            reminders_sent=self._reminders,
            refused_attempts=self._refused,
            requested_at=self._requested_at,
        )

    # ---------- internals

    def _receive(self, signal: ApprovalSignal, sent_as: ApprovalDecisionKind) -> None:
        self._inbox.append((signal, sent_as, workflow.now()))

    async def _act(self, fn: Callable[..., Any], arg: Any) -> Any:
        return await workflow.execute_activity_method(fn, arg, **activity_options(fn.__name__))

    async def _prompt(self, reminder_number: int) -> None:
        inp = self._input
        request = ApprovalRequest(
            tenant_id=inp.tenant_id,
            request_id=inp.request_id,
            category=inp.category,
            headline=(
                text.APPROVAL_HEADLINE if reminder_number == 0 else text.APPROVAL_REMINDER_HEADLINE
            ),
            summary=inp.summary,
            amount=inp.amount,
            currency=inp.currency,
            evidence_ids=inp.evidence_ids,
            reminder_number=reminder_number,
            owner_item_id=self._item_id,
            dedupe_key=f"{workflow.info().workflow_id}:approval:{reminder_number}",
        )
        ref = await self._act(BackofficeActivities.request_approval, request)
        self._item_id = ref.item_id
        self._last_prompted_at = workflow.now()

    async def _wait_for_decision(self) -> bool:
        """Wait until the next reminder is due; True when it is (no decision)."""
        assert self._last_prompted_at is not None
        inp = self._input
        due = self._last_prompted_at + reminder_delay(
            self._reminders, inp.first_reminder_after, inp.max_reminder_interval
        )
        remaining = due - workflow.now()
        if remaining.total_seconds() <= 0:
            return True
        try:
            await workflow.wait_condition(
                lambda: bool(self._inbox) or self._withdrawn is not None,
                timeout=remaining,
            )
        except TimeoutError:
            return True
        return False

    async def _consider(
        self, signal: ApprovalSignal, sent_as: ApprovalDecisionKind, received_at: datetime
    ) -> ApprovalResult | None:
        inp = self._input
        refusal = refusal_before_check(signal, inp, sent_as)
        check = None
        if refusal is None:
            # Anyone able to signal controls the assertion: one the identity
            # service cannot process is a refusal, never a failed workflow.
            check = await execute_or(
                BackofficeActivities.verify_approver,
                ApproverCheckRequest(
                    tenant_id=inp.tenant_id,
                    request_id=inp.request_id,
                    category=inp.category,
                    actor_id=signal.actor_id.strip(),
                    assertion_id=signal.assertion_id.strip(),
                ),
                ApproverCheck(verified=False, reason="sign-in could not be checked"),
            )
            refusal = refusal_after_check(check)
        if refusal is not None or check is None:
            self._refused += 1
            await self._audit(signal, accepted=False, reason=refusal or "refused")
            return None
        record = approval_record(signal, check, inp.request_id, received_at)
        await self._audit(signal, accepted=True, reason="verified")
        self._status, self._record = approval_status_for(signal.decision), record
        return await self._finish(self._status, record)

    async def _audit(self, signal: ApprovalSignal, *, accepted: bool, reason: str) -> None:
        await self._act(
            BackofficeActivities.record_approval_event,
            ApprovalAuditEvent(
                tenant_id=self._input.tenant_id,
                request_id=self._input.request_id,
                accepted=accepted,
                decision=signal.decision,
                actor_id=signal.actor_id,
                actor_kind=signal.actor_kind,
                reason=reason,
                at=workflow.now(),
            ),
        )

    async def _finish(
        self, status: ApprovalStatus, record: ApprovalRecord | None
    ) -> ApprovalResult:
        summary = text.approval_summary(status, record)
        if self._item_id:
            await self._act(
                BackofficeActivities.resolve_owner_item,
                OwnerItemResolution(
                    tenant_id=self._input.tenant_id, item_id=self._item_id, message=summary
                ),
            )
        return ApprovalResult(
            status=status,
            request_id=self._input.request_id,
            record=record,
            reminders_sent=self._reminders,
            refused_attempts=self._refused,
            summary=summary,
        )

    async def _finish_withdrawn(self, request: WithdrawRequest) -> ApprovalResult:
        await self._act(
            BackofficeActivities.record_approval_event,
            ApprovalAuditEvent(
                tenant_id=self._input.tenant_id,
                request_id=self._input.request_id,
                accepted=False,
                actor_id=request.requested_by,
                reason=f"withdrawn: {request.reason}".strip(),
                at=workflow.now(),
            ),
        )
        self._status = ApprovalStatus.WITHDRAWN
        return await self._finish(ApprovalStatus.WITHDRAWN, None)

    def _carry(self) -> ApprovalCarry:
        assert self._requested_at is not None and self._last_prompted_at is not None
        return ApprovalCarry(
            requested_at=self._requested_at,
            last_prompted_at=self._last_prompted_at,
            reminders_sent=self._reminders,
            refused_attempts=self._refused,
            owner_item_id=self._item_id,
        )
