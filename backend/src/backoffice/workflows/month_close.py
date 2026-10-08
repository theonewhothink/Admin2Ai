"""MonthCloseWorkflow: the §27 month-end autopilot, ending in MONTH CLOSED.

Schedule (relative to Day 0, the package day chosen by the caller; it must
fall after the month ends), each step at ``run_at`` UTC, run in order; a late
start catches up in order:

    Day -7  completeness audit
    Day -5  missing-evidence retrieval: a child MissingInvoiceWorkflow per gap,
            told not to contact suppliers before Day -3
    Day -3  supplier chases: re-audit, new gaps are chased at once
    Day  0  accountant package prepared, delivered if authorized (§25)
    Day +1  delivery confirmed (with evidence) or the owner is asked
    Day +2  accountant questions handled
    Final   closure evaluation, re-checked until the month is closed with
            evidence: every item resolved, a complete package confirmed as
            received by the accountant, every question answered with evidence

While re-checking it also: asks again whether the package arrived; retries
questions that are still waiting for an answer; and, when late documents
completed the month after the package went out, sends a more complete
package (only ever when fewer items are missing, so the accountant never gets
the same gaps twice).

The owner's "Send it for me" on the package card arrives as the
``send_package`` signal; that explicit request authorizes the delivery (§25).
Month-end cards are taken down as soon as their situation resolves (§69).

Children are started with ``ParentClosePolicy.ABANDON`` and deterministic ids,
so they survive this workflow's continue-as-new and a transaction is never
chased twice. They signal ``item_resolved`` back when they finish.
Continue-as-new carries :class:`~.contracts.MonthCloseState`.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from datetime import datetime
from typing import Any

from temporalio import workflow
from temporalio.exceptions import WorkflowAlreadyStartedError

with workflow.unsafe.imports_passed_through():
    from . import text
    from .activities import BackofficeActivities, activity_options, execute_or
    from .contracts import (
        ITEM_RESOLVED_SIGNAL,
        AccountantQuery,
        AuthorizationRequest,
        ClosureRequest,
        CompletenessRequest,
        DeliveryCheckRequest,
        DeliveryConfirmation,
        DeliveryReceipt,
        DeliveryRequest,
        GatedAction,
        ItemResolved,
        MonthCard,
        MonthCloseInput,
        MonthCloseProgress,
        MonthCloseResult,
        MonthCloseState,
        MonthStep,
        NeedsYouItem,
        OpenCard,
        OwnerItemResolution,
        PackageRequest,
        QueryHandlingRequest,
        QueryResolution,
        SendPackageRequest,
        TransactionRef,
    )
    from .decisions import (
        apply_audit,
        apply_card_resolved,
        apply_children_started,
        apply_delivery,
        apply_delivery_confirmation,
        apply_month_item_resolved,
        apply_owner_item_raised,
        apply_package,
        apply_query_handled,
        apply_query_received,
        apply_send_attempted,
        apply_send_request,
        apply_verdict,
        card_key,
        cards_to_resolve,
        child_input,
        delivery_check_due,
        delivery_confirmed,
        delivery_evidence_ids,
        delivery_overdue,
        delivery_sent,
        gaps_to_start,
        is_more_complete,
        missing_invoice_workflow_id,
        month_can_close,
        month_rollover,
        next_month_step,
        open_query_ids,
        package_refresh_due,
        package_version,
        record_step,
        sleep_needed,
        step_schedule,
    )
    from .missing_invoice import MissingInvoiceWorkflow
    from .text import Card

__all__ = ["MonthCloseWorkflow"]


@workflow.defn(name="MonthCloseWorkflow")
class MonthCloseWorkflow:
    """Durable month-end orchestration for one company and one month (§27)."""

    @workflow.init
    def __init__(self, inp: MonthCloseInput) -> None:
        self._input = inp
        self._state: MonthCloseState = inp.state or MonthCloseState()
        self._schedule = step_schedule(inp)
        self._inbox = 0

    # ---------- run

    @workflow.run
    async def run(self, inp: MonthCloseInput) -> MonthCloseResult:
        while True:
            await self._owner_work()
            step = next_month_step(self._state)
            if step is None:  # defensive: CLOSURE returns below
                raise RuntimeError("month close has no step left")
            target = self._schedule[step]
            wait = sleep_needed(workflow.now(), target)
            if wait is not None:
                if workflow.info().is_continue_as_new_suggested():
                    await self._continue_as_new()
                # A durable timer to the next step; the owner's requests wake it.
                with contextlib.suppress(TimeoutError):
                    await workflow.wait_condition(self._owner_work_waiting, timeout=wait)
                continue
            if step is MonthStep.CLOSURE:
                return await self._close_month(target)
            await self._run_step(step)
            self._state = record_step(self._state, step, target, workflow.now())

    # ---------- signals

    @workflow.signal
    def accountant_query(self, query: AccountantQuery) -> None:
        """A question from the accountant about this month (§28)."""
        self._state = apply_query_received(self._state, query)
        self._inbox += 1

    @workflow.signal
    def delivery_confirmed(self, confirmation: DeliveryConfirmation) -> None:
        """The accountant (or owner) confirmed the package arrived, with evidence."""
        self._state = apply_delivery_confirmation(self._state, confirmation)
        self._inbox += 1

    @workflow.signal
    def send_package(self, request: SendPackageRequest) -> None:
        """The owner tapped "Send it for me" on the package card (§25, §35)."""
        self._state = apply_send_request(self._state, request)
        self._inbox += 1

    @workflow.signal(name=ITEM_RESOLVED_SIGNAL)
    def item_resolved(self, item: ItemResolved) -> None:
        """A chase finished or the owner answered something; re-check now.

        It settles nothing by itself: the closure verdict and evidence do.
        """
        self._state = apply_month_item_resolved(self._state, item)
        self._inbox += 1

    # ---------- queries

    @workflow.query
    def current_status(self) -> str:
        step = next_month_step(self._state)
        at = self._schedule[step] if step else None
        return text.month_status_text(self._state, self._input.period, step, at)

    @workflow.query
    def progress(self) -> MonthCloseProgress:
        step = next_month_step(self._state)
        verdict = self._state.last_verdict
        return MonthCloseProgress(
            next_step=step,
            next_step_at=self._schedule[step] if step else None,
            steps_done=tuple(r.step for r in self._state.completed),
            status_text=self.current_status(),
            open_items=verdict.open_items if verdict else None,
        )

    # ---------- steps

    async def _act(self, fn: Callable[..., Any], arg: Any) -> Any:
        return await workflow.execute_activity_method(fn, arg, **activity_options(fn.__name__))

    async def _run_step(self, step: MonthStep) -> None:
        if step is MonthStep.COMPLETENESS_AUDIT:
            await self._audit()
        elif step is MonthStep.EVIDENCE_RETRIEVAL:
            await self._audit()
            await self._start_chases(self._schedule[MonthStep.SUPPLIER_CHASES])
        elif step is MonthStep.SUPPLIER_CHASES:
            await self._audit()
            await self._start_chases(None)
        elif step is MonthStep.PACKAGE:
            await self._prepare_package()
        elif step is MonthStep.DELIVERY_CONFIRMATION:
            await self._check_delivery(ask_owner=True)
        elif step is MonthStep.ACCOUNTANT_QUERIES:
            await self._handle_new_queries()
        else:
            raise ValueError(f"unexpected step {step}")

    async def _audit(self) -> None:
        inp = self._input
        report = await self._act(
            BackofficeActivities.run_completeness_audit,
            CompletenessRequest(
                tenant_id=inp.tenant_id, entity_id=inp.entity_id, period=inp.period
            ),
        )
        self._state = apply_audit(self._state, report)

    async def _start_chases(self, chase_not_before: datetime | None) -> None:
        gaps = gaps_to_start(self._state.gaps, self._state.chased_ids)
        if not gaps:
            return
        results = await asyncio.gather(*(self._start_child(gap, chase_not_before) for gap in gaps))
        started = tuple(tx_id for tx_id, fresh in results if fresh)
        running = tuple(tx_id for tx_id, fresh in results if not fresh)
        self._state = apply_children_started(self._state, started, running)

    async def _start_child(
        self, gap: TransactionRef, chase_not_before: datetime | None
    ) -> tuple[str, bool]:
        """Start one chase; False when one is already running for it."""
        inp = self._input
        try:
            await workflow.start_child_workflow(
                MissingInvoiceWorkflow.run,
                child_input(gap, inp.chase_policy, chase_not_before, workflow.info().workflow_id),
                id=missing_invoice_workflow_id(gap.tenant_id, gap.transaction_id),
                parent_close_policy=workflow.ParentClosePolicy.ABANDON,
            )
        except WorkflowAlreadyStartedError:
            return gap.transaction_id, False
        return gap.transaction_id, True

    # ---------- the package (§27-28)

    async def _new_package(self) -> bool:
        """Prepare a package; adopt it if first or more complete. True if adopted."""
        inp = self._input
        package = await self._act(
            BackofficeActivities.prepare_accountant_package,
            PackageRequest(tenant_id=inp.tenant_id, entity_id=inp.entity_id, period=inp.period),
        )
        if not is_more_complete(self._state.package, package):
            return False
        self._state = apply_package(self._state, package)
        return True

    async def _prepare_package(self) -> None:
        await self._new_package()
        await self._deliver_package(owner_asked=False)

    async def _refresh_package(self) -> None:
        """Late documents completed the month: send the accountant the rest."""
        if await self._new_package():
            await self._deliver_package(owner_asked=False)

    async def _deliver_package(self, *, owner_asked: bool) -> None:
        """Send the current package if allowed (§25); otherwise ask the owner.

        ``owner_asked``: the owner tapped "Send it for me", which authorizes it.
        """
        inp, state = self._input, self._state
        package = state.package
        assert package is not None
        allowed = owner_asked or await self._delivery_authorized(package.package_id)
        version, attempt = package_version(state), state.send_attempts
        if allowed:
            receipt = await execute_or(
                BackofficeActivities.deliver_accountant_package,
                DeliveryRequest(
                    tenant_id=inp.tenant_id,
                    entity_id=inp.entity_id,
                    period=inp.period,
                    package=package,
                    idempotency_key=(
                        f"{workflow.info().workflow_id}:delivery:"
                        f"{package.package_id}:{version}:{attempt}"
                    ),
                ),
                DeliveryReceipt(delivered=False),  # e.g. the address bounced: ask the owner
            )
            self._state = apply_delivery(self._state, receipt, workflow.now())
            if receipt.delivered:
                return
        await self._raise_card(
            text.package_ready_card(inp.period, allowed=allowed),
            MonthCard.PACKAGE_READY,
            (package.evidence_id,),
            version=version,
            attempt=attempt,
        )

    async def _delivery_authorized(self, package_id: str) -> bool:
        decision = await self._act(
            BackofficeActivities.is_action_authorized,
            AuthorizationRequest(
                tenant_id=self._input.tenant_id,
                entity_id=self._input.entity_id,
                action=GatedAction.DOCUMENT_DELIVERY,
                subject_id=package_id,
            ),
        )
        return bool(decision.authorized)

    async def _check_delivery(self, *, ask_owner: bool) -> None:
        """Ask whether the sent package arrived; if not and ``ask_owner``, a card."""
        state = self._state
        if not delivery_check_due(state):
            return
        assert state.package is not None
        confirmation = await execute_or(
            BackofficeActivities.confirm_delivery,
            DeliveryCheckRequest(
                tenant_id=self._input.tenant_id, package=state.package, receipt=state.delivery
            ),
            DeliveryConfirmation(confirmed=False),
        )
        self._state = apply_delivery_confirmation(self._state, confirmation)
        if ask_owner and not delivery_confirmed(self._state):
            sent_on = state.delivered_at.date() if state.delivered_at else None
            await self._raise_card(
                text.delivery_unconfirmed_card(self._input.period, sent_on),
                MonthCard.DELIVERY_UNCONFIRMED,
                delivery_evidence_ids(self._state),
                version=package_version(self._state),
            )

    # ---------- accountant questions (§28)

    async def _handle_new_queries(self) -> None:
        while self._state.pending_queries:
            await self._handle_query(self._state.pending_queries[0])

    async def _retry_waiting_queries(self) -> None:
        for query in self._state.waiting_queries:  # a snapshot: each tried once
            await self._handle_query(query)

    async def _handle_query(self, query: AccountantQuery) -> None:
        inp = self._input
        # A question the service cannot handle goes to the owner in the
        # accountant's own words rather than failing the whole month.
        resolution = await execute_or(
            BackofficeActivities.handle_accountant_query,
            QueryHandlingRequest(
                tenant_id=inp.tenant_id, entity_id=inp.entity_id, period=inp.period, query=query
            ),
            QueryResolution(query_id=query.query_id, resolved=False, owner_question=query.text),
        )
        self._state = apply_query_handled(self._state, resolution)
        if not resolution.resolved and resolution.owner_question:
            await self._raise_card(
                text.accountant_question_card(resolution.owner_question),
                MonthCard.ACCOUNTANT_QUESTION,
                (),
                query_id=query.query_id,
            )

    # ---------- the owner (§35, §42, §69)

    def _owner_work_waiting(self) -> bool:
        return self._state.send_request is not None or bool(cards_to_resolve(self._state))

    async def _owner_work(self) -> None:
        """Act on "Send it for me", then take down cards that are settled."""
        if self._state.send_request is not None:
            self._state = apply_send_attempted(self._state)
            state = self._state
            if state.package is not None and not (
                delivery_sent(state) or delivery_confirmed(state)
            ):
                await self._deliver_package(owner_asked=True)
        await self._tidy_cards()

    async def _raise_card(
        self,
        card: Card,
        kind: MonthCard,
        evidence: tuple[str, ...],
        *,
        version: int = 0,
        attempt: int = 0,
        query_id: str | None = None,
    ) -> None:
        """Create a Needs-You card once per key (§35); quiet, no push (§42)."""
        key = card_key(kind, version=version, attempt=attempt, query_id=query_id)
        if key in self._state.owner_items:
            return
        inp = self._input
        ref = await self._act(
            BackofficeActivities.notify_owner,
            NeedsYouItem(
                tenant_id=inp.tenant_id,
                kind=card.kind,
                headline=card.headline,
                detail=card.detail,
                why=card.why,
                options=card.options,
                subject_id=f"{inp.entity_id}:{inp.period.key}",
                evidence_ids=evidence,
                dedupe_key=f"{workflow.info().workflow_id}:{key}",
                notify=False,
            ),
        )
        opened = OpenCard(
            card=kind,
            item_id=ref.item_id,
            key=key,
            version=version,
            attempt=attempt,
            query_id=query_id,
        )
        self._state = apply_owner_item_raised(self._state, key, opened)

    async def _tidy_cards(self, *, closing: bool = False) -> None:
        for card, resolution in cards_to_resolve(self._state, closing=closing):
            await self._act(
                BackofficeActivities.resolve_owner_item,
                OwnerItemResolution(
                    tenant_id=self._input.tenant_id,
                    item_id=card.item_id,
                    message=text.card_resolution_message(resolution),
                ),
            )
            self._state = apply_card_resolved(self._state, card.item_id)

    # ---------- closure

    async def _close_month(self, target: datetime) -> MonthCloseResult:
        inp = self._input
        checks_this_run = 0
        while True:
            await self._owner_work()
            await self._handle_new_queries()
            await self._retry_waiting_queries()
            await self._check_delivery(ask_owner=delivery_overdue(self._state, workflow.now()))
            await self._tidy_cards()
            verdict = await self._act(
                BackofficeActivities.evaluate_closure,
                ClosureRequest(
                    tenant_id=inp.tenant_id,
                    entity_id=inp.entity_id,
                    period=inp.period,
                    package_id=self._state.package.package_id if self._state.package else None,
                    delivery_evidence_ids=delivery_evidence_ids(self._state),
                    open_query_ids=open_query_ids(self._state),
                ),
            )
            self._state = apply_verdict(self._state, verdict)
            if month_can_close(verdict, self._state):
                self._state = record_step(self._state, MonthStep.CLOSURE, target, workflow.now())
                await self._tidy_cards(closing=True)
                return self._result()
            if package_refresh_due(verdict, self._state):
                await self._refresh_package()
            seen = self._inbox
            with contextlib.suppress(TimeoutError):  # re-check on a timer or a signal
                await workflow.wait_condition(
                    lambda seen=seen: self._inbox != seen or self._owner_work_waiting(),
                    timeout=inp.closure_recheck_interval,
                )
            checks_this_run += 1
            suggested = workflow.info().is_continue_as_new_suggested()
            if month_rollover(checks_this_run, inp.rechecks_per_run, suggested):
                await self._continue_as_new()

    def _result(self) -> MonthCloseResult:
        state, inp = self._state, self._input
        verdict = state.last_verdict
        assert verdict is not None
        evidence = verdict.evidence_ids + delivery_evidence_ids(state)
        if state.package:
            evidence += (state.package.evidence_id,)
        return MonthCloseResult(
            period=inp.period,
            headline=text.month_closed_headline(inp.period),
            summary_line=text.month_summary_line(verdict.summary),
            summary=verdict.summary,
            steps=state.completed,
            package_id=state.package.package_id if state.package else None,
            evidence_ids=tuple(dict.fromkeys(evidence)),
            chases_started=len(state.chased_ids) - len(state.already_running_ids),
        )

    async def _continue_as_new(self) -> None:
        """Restart with fresh history, carrying the state (never returns: raises)."""
        await workflow.wait_condition(workflow.all_handlers_finished)
        state = self._state.model_copy(update={"runs": self._state.runs + 1})
        workflow.continue_as_new(self._input.model_copy(update={"state": state}))
