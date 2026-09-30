"""Regressions found in the adversarial review of the workflows module.

Each test pins one confirmed defect (it failed before the fix):

* refunds were described as "payments to" the supplier and chased for an
  "invoice" instead of a credit note (§20, §22, §36);
* a delivery confirmation that predates the package (or names another
  package) closed the month (§3, §27);
* an accountant question could be closed by ``item_resolved`` without any
  evidence (§3, §28);
* the "Send it for me" option on the package card had no path (§25, §35);
* a package delivered with missing items let the month close although the
  accountant never received the late documents (§3, §27);
* month-end cards stayed open after the situation resolved (§42, §69);
* a document the pipeline could not read failed the whole chase (§22, §48);
* a no-contact escalation hid the owner's own unverified upload (§35);
* bursts of refused approval attempts never continued-as-new (§45);
* a long chase never continued-as-new (§45);
* a package day inside the month, or another company's gaps, were accepted (§27, §51).
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from backoffice.domain.models import Quality
from backoffice.workflows import (
    AccountantQuery,
    ActorKind,
    ApprovalCategory,
    ApprovalDecisionKind,
    ApprovalInput,
    ApprovalSignal,
    ApprovalStatus,
    ChasePolicy,
    DeliveryConfirmation,
    DocumentArrival,
    GatedAction,
    HardApprovalWorkflow,
    ItemResolved,
    ItemStatus,
    MissingInvoiceInput,
    MissingInvoiceWorkflow,
    MonthCloseInput,
    MonthCloseWorkflow,
    MonthStep,
    OwnerAnswer,
    OwnerAnswerKind,
    Period,
    TransactionRef,
    missing_invoice_workflow_id,
    text,
)
from backoffice.workflows import decisions as d
from backoffice.workflows.activities import CONTRACT_VIOLATION, BackofficeActivities
from backoffice.workflows.contracts import (
    ApproverCheck,
    ClosureVerdict,
    CompletenessReport,
    CompletenessRequest,
    DeliveryReceipt,
    MatchOutcome,
    MonthCloseState,
    MonthSummary,
    NeedsYouKind,
    PackageRef,
    QueryResolution,
    SearchOutcome,
    SendPackageRequest,
    SupplierMessageKind,
    VerificationOutcome,
)
from backoffice.workflows.services import PermanentServiceError
from backoffice.workflows.testing import ScriptedServices, WorkflowSimulator
from backoffice.workflows.text import find_forbidden

T0 = datetime(2026, 9, 20, 9, 0, tzinfo=timezone.utc)
SEPTEMBER = Period(year=2026, month=9)
DAY0 = date(2026, 10, 5)
START = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
MONTH_ID = "month-close:t1:e1:2026-09"

PAYMENT = TransactionRef(
    tenant_id="t1",
    transaction_id="tx-117",
    booked_on=date(2026, 9, 18),
    amount=Decimal("-117.20"),
    counterparty="VODAFONE PORTUGAL",
    supplier_name="Vodafone",
)
REFUND = PAYMENT.model_copy(update={"transaction_id": "tx-25", "amount": Decimal("25.00")})


def run(coro):
    return asyncio.run(coro)


def at(offset: int, hours: int = 0) -> datetime:
    moment = datetime.combine(DAY0 + timedelta(days=offset), time(6, 0), tzinfo=timezone.utc)
    return moment + timedelta(hours=hours)


def green(svc: ScriptedServices, evidence_id: str, doc: str = "doc-1") -> None:
    svc.verifications[evidence_id] = VerificationOutcome(
        quality=Quality.GREEN, document_id=doc, evidence_ids=(evidence_id,)
    )
    svc.matches[doc] = MatchOutcome(matched=True, quality=Quality.GREEN, evidence_ids=("ev-bank",))


def closed_verdict() -> ClosureVerdict:
    return ClosureVerdict(
        closed=True,
        open_items=0,
        summary=MonthSummary(transactions_checked=10),
        evidence_ids=("ev-closure",),
    )


async def month(svc: ScriptedServices, **kw) -> WorkflowSimulator:
    sim = WorkflowSimulator.for_services(svc.services(), start_time=kw.pop("start", START))
    inp = MonthCloseInput(tenant_id="t1", entity_id="e1", period=SEPTEMBER, day_zero=DAY0, **kw)
    await sim.start(MonthCloseWorkflow.run, inp, id=MONTH_ID)
    return sim


def owner_texts(svc: ScriptedServices) -> list[str]:
    cards = [f"{c.headline} {c.detail} {c.why}" for c in svc.owner_items]
    return cards + [r.message for r in svc.resolved_items]


# ========== refunds need a credit note, not an invoice (§20, §22, §36)


class TestRefundWording:
    def test_refund_is_described_as_a_refund_from_the_supplier(self):
        assert text.payment_phrase(REFUND) == "the €25.00 refund from Vodafone on 18 September"
        assert text.payment_phrase(PAYMENT) == "the €117.20 payment to Vodafone on 18 September"

    def test_supplier_is_asked_for_the_credit_note(self):
        body = text.supplier_request_body(REFUND, SupplierMessageKind.REQUEST, date(2026, 9, 20))
        assert body == (
            "Hello, could you please send the credit note for the €25.00 refund "
            "dated 18 September? Thank you."
        )
        hinted = REFUND.model_copy(update={"invoice_number_hint": "NC 2026/12"})
        body = text.supplier_request_body(hinted, SupplierMessageKind.REMINDER, date(2026, 9, 26))
        assert body == (
            "Hello, just following up: could you please resend credit note NC 2026/12 "
            "relating to the €25.00 refund dated 18 September? Thank you."
        )

    def test_owner_cards_and_summaries_speak_of_the_credit_note(self):
        state = d.new_chase_state(ChasePolicy()).model_copy(update={"messages_sent": 1})
        card = text.chase_card(d.EscalationReason.NO_REPLY, REFUND, state, date(2026, 10, 2))
        assert card.headline == "We can't find the credit note for this refund."
        assert card.detail.startswith("€25.00 from Vodafone on 18 September.")
        assert [o.label for o in card.options][:2] == [
            "Upload the credit note",
            "No credit note needed",
        ]
        assert "credit note" in card.why and "invoice" not in card.why
        conflict = text.chase_card(d.EscalationReason.CONFLICT, REFUND, state, date(2026, 10, 2))
        assert conflict.headline == "The credit note doesn't agree with the refund."
        unconfirmed = text.chase_card(
            d.EscalationReason.UNCONFIRMED, REFUND, state, date(2026, 10, 2)
        )
        assert unconfirmed.headline == (
            "I found a document, but I can't confirm it's the credit note for this refund."
        )
        assert text.chase_summary(ItemStatus.CLOSED, REFUND) == (
            "Done. The credit note for the €25.00 refund from Vodafone on 18 September "
            "is here and checks out."
        )
        assert text.resolution_message(d.Resolution.FOUND, REFUND) == (
            "Done. I found the credit note."
        )
        for sentence in [card.headline, card.detail, card.why, conflict.headline]:
            assert "invoice" not in sentence and find_forbidden(sentence) == []

    def test_refund_chase_asks_for_a_credit_note_end_to_end(self):
        async def scenario():
            svc = ScriptedServices()
            sim = WorkflowSimulator.for_services(svc.services(), start_time=T0)
            await sim.start(
                MissingInvoiceWorkflow.run,
                MissingInvoiceInput(transaction=REFUND),
                id=missing_invoice_workflow_id("t1", "tx-25"),
            )
            [message] = svc.sent_messages
            assert "credit note" in message.default_body
            assert "invoice" not in message.default_body
            status = sim.query(MissingInvoiceWorkflow.current_status)
            assert status == (
                "I asked Vodafone for the credit note on 20 September. Waiting for their reply."
            )
            await sim.verify_replay()

        run(scenario())


# ========== delivery confirmation belongs to the package (§3, §27)


class TestDeliveryConfirmationBelongsToThePackage:
    def test_confirmation_before_any_package_is_ignored(self):
        early = d.apply_delivery_confirmation(
            MonthCloseState(), DeliveryConfirmation(confirmed=True, evidence_id="ev-old")
        )
        assert not d.delivery_confirmed(early)

    def test_confirmation_for_another_package_is_ignored(self):
        state = MonthCloseState(
            package=PackageRef(
                package_id="pkg-9", evidence_id="ev-p", complete_items=3, missing_items=0
            )
        )
        other = DeliveryConfirmation(confirmed=True, evidence_id="ev-x", package_id="pkg-8")
        assert not d.delivery_confirmed(d.apply_delivery_confirmation(state, other))
        mine = DeliveryConfirmation(confirmed=True, evidence_id="ev-y", package_id="pkg-9")
        assert d.delivery_confirmed(d.apply_delivery_confirmation(state, mine))

    def test_an_early_confirmation_signal_does_not_close_the_month(self):
        async def scenario():
            svc = ScriptedServices()  # confirm_delivery keeps answering "not yet"
            sim = await month(svc)
            await sim.signal(
                MonthCloseWorkflow.delivery_confirmed,
                DeliveryConfirmation(confirmed=True, evidence_id="ev-last-month"),
            )
            await sim.advance(at(4) - START)
            assert not sim.completed
            assert "confirm_delivery" in svc.names()
            kinds = [c.kind for c in svc.owner_items]
            assert NeedsYouKind.DELIVERY_UNCONFIRMED in kinds

        run(scenario())


# ========== accountant questions close only with evidence (§3, §28)


def test_an_accountant_question_is_not_closed_by_a_bare_item_resolved_signal():
    async def scenario():
        svc = ScriptedServices()
        svc.delivery_confirmations = [DeliveryConfirmation(confirmed=True, evidence_id="ev-ok")]
        svc.query_resolutions["q-7"] = QueryResolution(
            query_id="q-7", resolved=False, owner_question="Was the dinner a business meal?"
        )
        sim = await month(svc)
        await sim.signal(
            MonthCloseWorkflow.accountant_query,
            AccountantQuery(query_id="q-7", text="Nature of the 240 EUR expense?"),
        )
        await sim.advance(at(3) - START)
        await sim.signal(
            MonthCloseWorkflow.item_resolved,
            ItemResolved(subject_id="q-7", status=ItemStatus.CLOSED),
        )
        assert not sim.completed  # no evidence yet: still open
        # The owner answered in the app; the service can now answer with evidence.
        svc.query_resolutions["q-7"] = QueryResolution(
            query_id="q-7", resolved=True, evidence_ids=("ev-owner-answer",)
        )
        await sim.signal(
            MonthCloseWorkflow.item_resolved,
            ItemResolved(subject_id="q-7", status=ItemStatus.CLOSED),
        )
        assert sim.completed
        assert svc.names().count("handle_accountant_query") >= 3  # retried, not assumed
        [card] = [c for c in svc.owner_items if c.kind is NeedsYouKind.ACCOUNTANT_QUESTION]
        assert "Done." in [r.message for r in svc.resolved_items]
        assert sim.result().headline == "September is closed."
        assert card.detail == "Was the dinner a business meal?"
        await sim.verify_replay()

    run(scenario())


def test_an_unresolved_question_is_retried_on_the_next_check_without_any_signal():
    async def scenario():
        svc = ScriptedServices()
        svc.delivery_confirmations = [DeliveryConfirmation(confirmed=True, evidence_id="ev-ok")]
        svc.query_resolutions["q-8"] = QueryResolution(query_id="q-8", resolved=False)
        sim = await month(svc)
        await sim.signal(
            MonthCloseWorkflow.accountant_query, AccountantQuery(query_id="q-8", text="Why?")
        )
        await sim.advance(at(2) - START)
        assert not sim.completed
        svc.query_resolutions["q-8"] = QueryResolution(
            query_id="q-8", resolved=True, evidence_ids=("ev-found",)
        )
        await sim.advance(timedelta(days=1))
        assert sim.completed

    run(scenario())


# ========== "Send it for me" really sends it (§25, §35)


def test_owner_asks_us_to_send_the_package_and_it_is_sent():
    async def scenario():
        svc = ScriptedServices()
        svc.authorized[GatedAction.DOCUMENT_DELIVERY] = False
        svc.delivery_confirmations = [DeliveryConfirmation(confirmed=True, evidence_id="ev-rr")]
        sim = await month(svc)
        # Too early: there is no package yet, so nothing can be sent.
        await sim.signal(
            MonthCloseWorkflow.send_package,
            SendPackageRequest(requested_by="owner-1", answer_evidence_id="ev-tap-0"),
        )
        await sim.advance(at(0, hours=2) - START)
        assert "deliver_accountant_package" not in svc.names()
        [card] = svc.owner_items
        assert card.kind is NeedsYouKind.PACKAGE_READY
        assert [o.key for o in card.options][0] == "send_for_me"

        await sim.signal(
            MonthCloseWorkflow.send_package,
            SendPackageRequest(requested_by="owner-1", answer_evidence_id="ev-tap-1"),
        )
        [delivery] = sim.calls("deliver_accountant_package")
        assert delivery.at == at(0, hours=2)
        assert [r.message for r in svc.resolved_items] == ["Done. I sent it to your accountant."]
        result = await sim.run_until_complete()
        assert result.headline == "September is closed."
        assert "ev-delivery" in result.evidence_ids
        await sim.verify_replay()

    run(scenario())


def test_a_failed_send_the_owner_asked_for_is_reported_back_to_them():
    async def scenario():
        svc = ScriptedServices()
        svc.authorized[GatedAction.DOCUMENT_DELIVERY] = False
        svc.delivery = DeliveryReceipt(delivered=False)
        sim = await month(svc)
        await sim.advance(at(0, hours=1) - START)
        await sim.signal(
            MonthCloseWorkflow.send_package,
            SendPackageRequest(requested_by="owner-1", answer_evidence_id="ev-tap-1"),
        )
        details = [c.detail for c in svc.owner_items]
        assert details == [
            "I'm not allowed to send it for you yet.",
            "I couldn't send it to your accountant.",
        ]
        assert [r.message for r in svc.resolved_items] == ["No longer needed."]

    run(scenario())


# ========== the accountant must have everything (§3, §27)


def test_month_cannot_close_while_the_delivered_package_has_missing_items():
    state = MonthCloseState(
        package=PackageRef(package_id="p", evidence_id="ev-p", complete_items=9, missing_items=1)
    )
    state = d.apply_delivery_confirmation(
        state, DeliveryConfirmation(confirmed=True, evidence_id="ev-c")
    )
    assert not d.month_can_close(closed_verdict(), state)
    assert d.package_refresh_due(closed_verdict(), state)


def test_late_documents_are_sent_in_a_new_package_before_the_month_closes():
    async def scenario():
        svc = ScriptedServices()
        svc.package = PackageRef(
            package_id="pkg-1", evidence_id="ev-package-1", complete_items=9, missing_items=1
        )
        svc.delivery_confirmations = [
            DeliveryConfirmation(confirmed=True, evidence_id="ev-rr-1"),
            DeliveryConfirmation(confirmed=False),
            DeliveryConfirmation(confirmed=True, evidence_id="ev-rr-2"),
        ]
        sim = await month(svc)
        await sim.advance(at(2) - START)
        # Everything is resolved now, but the accountant got a package with a gap.
        svc.package = PackageRef(
            package_id="pkg-2", evidence_id="ev-package-2", complete_items=10, missing_items=0
        )
        result = await sim.run_until_complete()
        deliveries = sim.calls("deliver_accountant_package")
        assert [c.arg.package.package_id for c in deliveries] == ["pkg-1", "pkg-2"]
        assert deliveries[0].arg.idempotency_key != deliveries[1].arg.idempotency_key
        assert result.package_id == "pkg-2"
        assert {"ev-package-2", "ev-rr-2"} <= set(result.evidence_ids)
        assert "ev-rr-1" not in result.evidence_ids  # that receipt was for the old package
        await sim.verify_replay()

    run(scenario())


def test_a_package_that_is_no_more_complete_is_never_resent():
    async def scenario():
        svc = ScriptedServices()
        svc.package = PackageRef(
            package_id="pkg-1", evidence_id="ev-package-1", complete_items=9, missing_items=1
        )
        svc.delivery_confirmations = [DeliveryConfirmation(confirmed=True, evidence_id="ev-rr")]
        sim = await month(svc)
        await sim.advance(at(10) - START)
        assert not sim.completed
        assert len(sim.calls("deliver_accountant_package")) == 1  # the accountant is not spammed
        assert svc.names().count("prepare_accountant_package") > 2  # it keeps checking

    run(scenario())


# ========== month-end cards do not go stale (§42, §69)


def test_delivery_card_is_taken_down_when_the_accountant_confirms_later():
    async def scenario():
        svc = ScriptedServices()
        svc.delivery_confirmations = [
            DeliveryConfirmation(confirmed=False),
            DeliveryConfirmation(confirmed=True, evidence_id="ev-read-late"),
        ]
        sim = await month(svc)
        await sim.advance(at(1) - START)
        [card] = svc.owner_items
        assert card.kind is NeedsYouKind.DELIVERY_UNCONFIRMED
        result = await sim.run_until_complete()
        assert "ev-read-late" in result.evidence_ids  # found by a re-check, no signal
        assert [r.message for r in svc.resolved_items] == ["Done. Your accountant has it."]
        await sim.verify_replay()

    run(scenario())


def test_status_counts_only_documents_still_missing():
    gaps = (PAYMENT, REFUND)
    state = MonthCloseState(gaps=gaps, resolved_subject_ids=("tx-117",))
    assert text.month_status_text(state, SEPTEMBER, MonthStep.PACKAGE, None) == (
        "September: still looking for 1 missing document."
    )


# ========== a document the pipeline cannot read never kills the chase (§22, §48)


class UnreadablePipeline(ScriptedServices):
    async def ingest_and_verify(self, request):
        if "ev-broken" in request.evidence_ids:
            self._log("ingest_and_verify", request)
            raise PermanentServiceError("pdf parser: unexpected EOF at byte 1873")
        return await super().ingest_and_verify(request)


def test_unreadable_document_is_unconfirmed_and_the_chase_goes_on():
    async def scenario():
        svc = UnreadablePipeline()
        svc.authorized[GatedAction.SUPPLIER_INVOICE_REQUEST] = False
        svc.search_results = [SearchOutcome(evidence_ids=("ev-broken",))]
        sim = WorkflowSimulator.for_services(svc.services(), start_time=T0)
        await sim.start(
            MissingInvoiceWorkflow.run,
            MissingInvoiceInput(transaction=PAYMENT),
            id=missing_invoice_workflow_id("t1", "tx-117"),
        )
        assert not sim.completed  # the run did not fail
        [card] = svc.owner_items
        assert card.kind is NeedsYouKind.UNCONFIRMED_DOCUMENT
        assert card.evidence_ids == ("ev-broken",)
        for sentence in owner_texts(svc):
            assert "EOF" not in sentence and find_forbidden(sentence) == []
        green(svc, "ev-good")
        await sim.signal(
            MissingInvoiceWorkflow.document_received, DocumentArrival(evidence_ids=("ev-good",))
        )
        assert sim.result().status is ItemStatus.CLOSED
        await sim.verify_replay()

    run(scenario())


# ========== the owner's own unverified upload stays visible (§35)


def test_owner_upload_that_does_not_verify_is_shown_even_without_supplier_contact():
    async def scenario():
        svc = ScriptedServices()
        svc.supplier_contact = False
        sim = WorkflowSimulator.for_services(svc.services(), start_time=T0)
        await sim.start(
            MissingInvoiceWorkflow.run,
            MissingInvoiceInput(transaction=PAYMENT),
            id=missing_invoice_workflow_id("t1", "tx-117"),
        )
        assert svc.owner_items[0].kind is NeedsYouKind.NO_SUPPLIER_CONTACT
        await sim.signal(
            MissingInvoiceWorkflow.owner_answered,
            OwnerAnswer(
                kind=OwnerAnswerKind.DOCUMENT_PROVIDED,
                answered_by="owner-1",
                answer_evidence_id="ev-answer",
                evidence_ids=("ev-photo",),
            ),
        )
        second = svc.owner_items[1]
        assert second.kind is NeedsYouKind.UNCONFIRMED_DOCUMENT
        assert "ev-photo" in second.evidence_ids

    run(scenario())


# ========== history stays bounded (§45)


def test_a_flood_of_refused_approvals_continues_as_new_and_still_needs_a_person():
    async def scenario():
        svc = ScriptedServices()
        svc.approvers["sess-ana"] = ApproverCheck(
            verified=True, display_name="Ana Silva", auth_method="passkey"
        )
        sim = WorkflowSimulator.for_services(
            svc.services(), start_time=T0, suggest_continue_as_new_after=40
        )
        inp = ApprovalInput(
            tenant_id="t1", request_id="pay-1", category=ApprovalCategory.MONEY_MOVEMENT,
            summary="Pay €1,200.00 to Hazel Tree Lda.", amount=Decimal("1200.00"),
            evidence_ids=("ev-inv",), requested_by="payments-agent",
        )  # fmt: skip
        await sim.start(HardApprovalWorkflow.run, inp, id="approval:pay-1")
        bogus = ApprovalSignal(
            decision=ApprovalDecisionKind.APPROVE, actor_id="bot", actor_kind=ActorKind.AGENT
        )
        for _ in range(20):
            await sim.signal(HardApprovalWorkflow.approve, bogus)
        assert sim.run_count > 1 and not sim.completed
        await sim.signal(
            HardApprovalWorkflow.approve,
            ApprovalSignal(
                decision=ApprovalDecisionKind.APPROVE, actor_id="ana", assertion_id="sess-ana"
            ),
        )
        result = sim.result()
        assert result.status is ApprovalStatus.APPROVED
        assert result.refused_attempts == 20  # carried across runs
        assert len(svc.approval_requests) == 1  # one card, not one per run
        await sim.verify_replay()

    run(scenario())


def test_a_long_chase_continues_as_new_and_keeps_its_progress():
    async def scenario():
        svc = ScriptedServices()
        sim = WorkflowSimulator.for_services(
            svc.services(), start_time=T0, suggest_continue_as_new_after=60
        )
        await sim.start(
            MissingInvoiceWorkflow.run,
            MissingInvoiceInput(transaction=PAYMENT, policy=ChasePolicy(max_reminders=2)),
            id=missing_invoice_workflow_id("t1", "tx-117"),
        )
        await sim.advance(timedelta(days=19))
        assert sim.run_count > 1
        assert len(svc.sent_messages) == 3  # request + 2 reminders, never more
        assert [m.message_number for m in svc.sent_messages] == [1, 2, 3]
        assert len(svc.owner_items) == 1
        green(svc, "ev-late")
        await sim.signal(
            MissingInvoiceWorkflow.document_received, DocumentArrival(evidence_ids=("ev-late",))
        )
        result = sim.result()
        assert result.status is ItemStatus.CLOSED
        assert result.messages_sent == 3 and result.escalations == 1
        assert [r.message for r in svc.resolved_items] == ["Done. I found the invoice."]
        await sim.verify_replay()

    run(scenario())


# ========== inputs that cannot be right (§27, §51)


def test_package_day_must_be_after_the_month_ends():
    base = dict(tenant_id="t", entity_id="e", period=SEPTEMBER)
    with pytest.raises(ValidationError, match="after the month ends"):
        MonthCloseInput(**base, day_zero=date(2026, 9, 30))
    MonthCloseInput(**base, day_zero=date(2026, 10, 1))
    offsets = {
        MonthStep.COMPLETENESS_AUDIT: -9,
        MonthStep.EVIDENCE_RETRIEVAL: -8,
        MonthStep.SUPPLIER_CHASES: -7,
        MonthStep.PACKAGE: -2,
        MonthStep.DELIVERY_CONFIRMATION: 0,
        MonthStep.ACCOUNTANT_QUERIES: 1,
    }
    with pytest.raises(ValidationError, match="after the month ends"):
        MonthCloseInput(**base, day_zero=date(2026, 10, 1), day_offsets=offsets)


def test_an_audit_can_never_leak_another_companys_transactions():
    class OtherCompany(ScriptedServices):
        async def run_completeness_audit(self, request):
            gap = PAYMENT.model_copy(update={"entity_id": "e-other"})
            return CompletenessReport(transactions_checked=1, documents_collected=0, gaps=(gap,))

    acts = BackofficeActivities(OtherCompany().services())
    request = CompletenessRequest(tenant_id="t1", entity_id="e1", period=SEPTEMBER)
    with pytest.raises(ApplicationError) as err:
        asyncio.run(ActivityEnvironment().run(acts.run_completeness_audit, request))
    assert err.value.type == CONTRACT_VIOLATION and err.value.non_retryable


# ========== one bad outside input never fails a long-running workflow (§45, §48)


class BrokenOutsideWorld(ScriptedServices):
    """Permanent failures caused by one supplier, accountant or signal sender."""

    def __init__(self, *broken: str) -> None:
        super().__init__()
        self.broken = set(broken)

    async def check_thread_for_reply(self, request):
        if "thread" in self.broken:
            self._log("check_thread_for_reply", request)
            raise PermanentServiceError("thread 4411 not found")
        return await super().check_thread_for_reply(request)

    async def send_supplier_request(self, request):
        if "send" in self.broken:
            self._log("send_supplier_request", request)
            raise PermanentServiceError("550 mailbox unavailable")
        return await super().send_supplier_request(request)

    async def verify_approver(self, request):
        if request.assertion_id == "garbage":
            self._log("verify_approver", request)
            raise PermanentServiceError("assertion is not a valid JWT")
        return await super().verify_approver(request)

    async def handle_accountant_query(self, request):
        if request.query.query_id == "q-bad":
            self._log("handle_accountant_query", request)
            raise PermanentServiceError("cannot parse query")
        return await super().handle_accountant_query(request)

    async def confirm_delivery(self, request):
        if "confirm" in self.broken:
            self._log("confirm_delivery", request)
            raise PermanentServiceError("no such message")
        return await super().confirm_delivery(request)


async def chase(svc: ScriptedServices, **policy) -> WorkflowSimulator:
    sim = WorkflowSimulator.for_services(svc.services(), start_time=T0)
    await sim.start(
        MissingInvoiceWorkflow.run,
        MissingInvoiceInput(transaction=PAYMENT, policy=ChasePolicy(**policy)),
        id=missing_invoice_workflow_id("t1", "tx-117"),
    )
    return sim


def test_a_vanished_supplier_thread_does_not_stop_the_chase():
    async def scenario():
        svc = BrokenOutsideWorld("thread")
        sim = await chase(svc)
        await sim.advance(timedelta(days=6))
        assert not sim.completed
        assert len(svc.sent_messages) == 2  # the reminder still went out on day 6
        green(svc, "ev-reply")
        await sim.signal(
            MissingInvoiceWorkflow.document_received, DocumentArrival(evidence_ids=("ev-reply",))
        )
        assert sim.result().status is ItemStatus.CLOSED
        await sim.verify_replay()

    run(scenario())


def test_an_undeliverable_supplier_address_goes_to_the_owner():
    async def scenario():
        svc = BrokenOutsideWorld("send")
        sim = await chase(svc)
        assert not sim.completed
        [card] = svc.owner_items
        assert card.kind is NeedsYouKind.NO_SUPPLIER_CONTACT
        assert all("550" not in t for t in owner_texts(svc))

    run(scenario())


def test_a_garbage_approval_assertion_is_refused_not_fatal():
    async def scenario():
        svc = BrokenOutsideWorld()
        svc.approvers["sess-ana"] = ApproverCheck(
            verified=True, display_name="Ana Silva", auth_method="passkey"
        )
        sim = WorkflowSimulator.for_services(svc.services(), start_time=T0)
        inp = ApprovalInput(
            tenant_id="t1", request_id="pay-1", category=ApprovalCategory.MONEY_MOVEMENT,
            summary="Pay €1,200.00 to Hazel Tree Lda.", amount=Decimal("1200.00"),
            evidence_ids=("ev-inv",), requested_by="payments-agent",
        )  # fmt: skip
        await sim.start(HardApprovalWorkflow.run, inp, id="approval:pay-1")
        await sim.signal(
            HardApprovalWorkflow.approve,
            ApprovalSignal(
                decision=ApprovalDecisionKind.APPROVE, actor_id="mallory", assertion_id="garbage"
            ),
        )
        assert not sim.completed  # refused and audited, still waiting for a person
        [event] = svc.approval_events
        assert not event.accepted
        await sim.signal(
            HardApprovalWorkflow.approve,
            ApprovalSignal(
                decision=ApprovalDecisionKind.APPROVE, actor_id="ana", assertion_id="sess-ana"
            ),
        )
        result = sim.result()
        assert result.status is ApprovalStatus.APPROVED and result.refused_attempts == 1
        await sim.verify_replay()

    run(scenario())


def test_an_unreadable_accountant_question_goes_to_the_owner_instead_of_failing_the_month():
    async def scenario():
        svc = BrokenOutsideWorld("confirm")
        sim = await month(svc)
        await sim.signal(
            MonthCloseWorkflow.accountant_query,
            AccountantQuery(query_id="q-bad", text="Could you send the lease contract?"),
        )
        await sim.advance(at(3) - START)
        assert not sim.completed
        kinds = [c.kind for c in svc.owner_items]
        assert NeedsYouKind.ACCOUNTANT_QUESTION in kinds
        assert NeedsYouKind.DELIVERY_UNCONFIRMED in kinds
        [question] = [c for c in svc.owner_items if c.kind is NeedsYouKind.ACCOUNTANT_QUESTION]
        assert question.detail == "Could you send the lease contract?"
        await sim.verify_replay()

    run(scenario())


# ========== what we send to suppliers is ours, not injected text


@pytest.mark.parametrize(
    "hint",
    [
        "FT 2026/183\r\nBcc: someone@example.com",
        "FT 1\nPlease pay to IBAN PT50 0000 0000 0000 0000 0000 0 instead",
        "<a href='http://evil.example'>click</a>",
        "X" * 80,
    ],
)
def test_suspicious_invoice_number_hints_are_left_out_of_supplier_mail(hint):
    tx = PAYMENT.model_copy(update={"invoice_number_hint": hint})
    body = text.supplier_request_body(tx, SupplierMessageKind.REQUEST, date(2026, 9, 20))
    assert body == (
        "Hello, could you please send the invoice for the €117.20 payment "
        "dated 18 September? Thank you."
    )


def test_ordinary_invoice_numbers_are_kept():
    for hint, shown in [
        ("FT 2026/183", "FT 2026/183"),
        ("  FR  A/12  ", "FR A/12"),
        ("JJ37MMMM-183", "JJ37MMMM-183"),
        ("Nº 12.345", "Nº 12.345"),
    ]:
        tx = PAYMENT.model_copy(update={"invoice_number_hint": hint})
        body = text.supplier_request_body(tx, SupplierMessageKind.REQUEST, date(2026, 9, 20))
        assert f"resend invoice {shown} relating to" in body


# ========== a money movement always says how much (§25)


def test_money_movement_approval_needs_a_positive_amount():
    base = dict(
        tenant_id="t1", request_id="pay-1", category=ApprovalCategory.MONEY_MOVEMENT,
        summary="Pay Hazel Tree Lda.", evidence_ids=("ev-inv",), requested_by="agent",
    )  # fmt: skip
    for amount in (None, Decimal("0"), Decimal("-10.00")):
        with pytest.raises(ValidationError, match="amount"):
            ApprovalInput(**base, amount=amount)
    ApprovalInput(**base, amount=Decimal("10.00"))
    # Other hard approvals may have no amount (e.g. deleting original evidence).
    ApprovalInput(**{**base, "category": ApprovalCategory.ORIGINAL_EVIDENCE_DELETION})


# ========== the new month-close rules, as pure functions


def _pkg(pid: str, missing: int) -> PackageRef:
    return PackageRef(
        package_id=pid, evidence_id=f"ev-{pid}", complete_items=5, missing_items=missing
    )


class TestPackageRules:
    def test_only_a_more_complete_package_replaces_the_current_one(self):
        first = d.apply_package(MonthCloseState(), _pkg("p1", 2))
        assert d.package_version(first) == 1 and not d.package_complete(first)
        sent = d.apply_delivery(
            first, DeliveryReceipt(delivered=True, delivery_evidence_id="ev-d1"), T0
        )
        assert d.apply_package(sent, _pkg("p1b", 2)) == sent  # same gaps: never resent
        better = d.apply_package(sent, _pkg("p2", 0))
        assert d.package_version(better) == 2 and d.package_complete(better)
        assert better.earlier_packages == (_pkg("p1", 2),)
        assert better.delivery is None and better.delivered_at is None
        assert d.delivery_evidence_ids(better) == ()  # old receipts do not count

    def test_refresh_is_due_only_when_everything_else_is_clear(self):
        state = MonthCloseState(package=_pkg("p1", 1))
        assert d.package_refresh_due(closed_verdict(), state)
        assert not d.package_refresh_due(ClosureVerdict(closed=False, open_items=1), state)
        assert not d.package_refresh_due(closed_verdict(), MonthCloseState(package=_pkg("p", 0)))

    def test_delivery_is_overdue_a_day_after_sending(self):
        state = d.apply_delivery(
            MonthCloseState(package=_pkg("p1", 0)),
            DeliveryReceipt(delivered=True, delivery_evidence_id="ev-d"),
            T0,
        )
        assert not d.delivery_overdue(state, T0 + timedelta(hours=23))
        assert d.delivery_overdue(state, T0 + timedelta(days=1))
        confirmed = d.apply_delivery_confirmation(
            state, DeliveryConfirmation(confirmed=True, evidence_id="ev-c")
        )
        assert not d.delivery_overdue(confirmed, T0 + timedelta(days=3))
        assert confirmed.delivery_confirmation.package_id == "p1"  # pinned to the package


class TestSendRequests:
    REQUEST = SendPackageRequest(requested_by="owner-1", answer_evidence_id="ev-tap")

    def test_kept_only_while_there_is_an_unsent_package(self):
        assert d.apply_send_request(MonthCloseState(), self.REQUEST).send_request is None
        unsent = MonthCloseState(package=_pkg("p1", 0))
        assert d.apply_send_request(unsent, self.REQUEST).send_request == self.REQUEST
        other = self.REQUEST.model_copy(update={"package_id": "p0"})
        assert d.apply_send_request(unsent, other).send_request is None
        sent = d.apply_delivery(
            unsent, DeliveryReceipt(delivered=True, delivery_evidence_id="ev-d"), T0
        )
        assert d.apply_send_request(sent, self.REQUEST).send_request is None

    def test_an_attempt_clears_the_request_and_counts(self):
        state = d.apply_send_request(MonthCloseState(package=_pkg("p1", 0)), self.REQUEST)
        state = d.apply_send_attempted(state)
        assert state.send_request is None and state.send_attempts == 1


class TestMonthCards:
    def _card(self, kind, **kw):
        from backoffice.workflows.contracts import OpenCard

        key = d.card_key(kind, **kw)
        return OpenCard(card=kind, item_id=f"card-{key}", key=key, **kw)

    def test_keys_are_distinct_per_version_attempt_and_question(self):
        from backoffice.workflows.contracts import MonthCard

        keys = {
            d.card_key(MonthCard.PACKAGE_READY, version=1, attempt=0),
            d.card_key(MonthCard.PACKAGE_READY, version=1, attempt=1),
            d.card_key(MonthCard.PACKAGE_READY, version=2, attempt=0),
            d.card_key(MonthCard.DELIVERY_UNCONFIRMED, version=1),
            d.card_key(MonthCard.ACCOUNTANT_QUESTION, query_id="q1"),
        }
        assert len(keys) == 5

    def test_each_card_comes_down_when_its_situation_resolves(self):
        from backoffice.workflows.contracts import MonthCard

        ready = self._card(MonthCard.PACKAGE_READY, version=1)
        unconfirmed = self._card(MonthCard.DELIVERY_UNCONFIRMED, version=1)
        question = self._card(MonthCard.ACCOUNTANT_QUESTION, query_id="q1")
        state = MonthCloseState(package=_pkg("p1", 0), open_cards=(ready, unconfirmed, question))
        assert d.cards_to_resolve(state) == ()
        sent = d.apply_delivery(
            state, DeliveryReceipt(delivered=True, delivery_evidence_id="ev-d"), T0
        )
        assert d.cards_to_resolve(sent) == ((ready, d.CardResolution.SENT),)
        received = d.apply_delivery_confirmation(
            sent, DeliveryConfirmation(confirmed=True, evidence_id="ev-c")
        )
        assert dict(d.cards_to_resolve(received)) == {
            ready: d.CardResolution.RECEIVED,
            unconfirmed: d.CardResolution.RECEIVED,
        }
        answered = received.model_copy(
            update={
                "handled_queries": (
                    QueryResolution(query_id="q1", resolved=True, evidence_ids=("ev-a",)),
                )
            }
        )
        assert dict(d.cards_to_resolve(answered))[question] is d.CardResolution.ANSWERED

    def test_superseded_and_closing(self):
        from backoffice.workflows.contracts import MonthCard

        old = self._card(MonthCard.PACKAGE_READY, version=1)
        state = MonthCloseState(package=_pkg("p1", 1), open_cards=(old,))
        state = d.apply_package(state, _pkg("p2", 0))
        assert d.cards_to_resolve(state) == ((old, d.CardResolution.SUPERSEDED),)
        tapped = MonthCloseState(package=_pkg("p1", 0), open_cards=(old,), send_attempts=1)
        assert d.cards_to_resolve(tapped) == ((old, d.CardResolution.SUPERSEDED),)
        fresh = self._card(MonthCard.PACKAGE_READY, version=1, attempt=1)
        waiting = tapped.model_copy(update={"open_cards": (fresh,)})
        assert d.cards_to_resolve(waiting) == ()
        assert d.cards_to_resolve(waiting, closing=True) == (
            (fresh, d.CardResolution.MONTH_CLOSED),
        )
        assert d.apply_card_resolved(waiting, fresh.item_id).open_cards == ()

    def test_every_card_resolution_is_plain_language(self):
        for resolution in d.CardResolution:
            message = text.card_resolution_message(resolution)
            assert message and find_forbidden(message) == []


def test_every_refund_string_is_plain_and_never_says_invoice():
    state = d.new_chase_state(ChasePolicy()).model_copy(update={"messages_sent": 2})
    strings: list[str] = []
    for reason in d.EscalationReason:
        card = text.chase_card(reason, REFUND, state, date(2026, 10, 2))
        strings += [card.headline, card.detail, card.why, *(o.label for o in card.options)]
    strings += [text.resolution_message(r, REFUND) for r in d.Resolution]
    strings += [text.chase_summary(s, REFUND) for s in ItemStatus]
    for sentence in strings:
        assert find_forbidden(sentence) == [], sentence
        assert "invoice" not in sentence, sentence


def test_a_late_unconfirmed_document_replaces_the_cant_find_it_card():
    async def scenario():
        svc = ScriptedServices()
        sim = await chase(svc, max_reminders=0)
        await sim.advance(timedelta(days=6))
        [first] = svc.owner_items
        assert first.kind is NeedsYouKind.MISSING_DOCUMENT
        # The supplier's reply arrives but cannot be confirmed (AMBER).
        await sim.signal(
            MissingInvoiceWorkflow.document_received, DocumentArrival(evidence_ids=("ev-blurry",))
        )
        assert not sim.completed
        second = svc.owner_items[1]
        assert second.kind is NeedsYouKind.UNCONFIRMED_DOCUMENT
        assert second.evidence_ids == ("ev-blurry",)
        assert [r.message for r in svc.resolved_items] == [
            "I have a newer question about this payment."
        ]
        # Another blurry copy does not spam a third card.
        await sim.signal(
            MissingInvoiceWorkflow.document_received, DocumentArrival(evidence_ids=("ev-blurry-2",))
        )
        assert len(svc.owner_items) == 2
        await sim.verify_replay()

    run(scenario())
