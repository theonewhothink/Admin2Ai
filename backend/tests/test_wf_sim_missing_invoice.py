"""MissingInvoiceWorkflow run in the real Temporal SDK runtime, offline (§22, §45).

The workflow code executes inside Temporal's Replayer with time skipping (see
``backoffice.workflows.testing``); every scenario ends by replaying its full
history in the sandboxed runner, so non-determinism would fail the test.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from temporalio.api.common.v1 import ActivityType
from temporalio.workflow import NondeterminismError

from backoffice.domain.models import Quality
from backoffice.workflows import (
    CancelRequest,
    ChasePolicy,
    DocumentArrival,
    GatedAction,
    ItemStatus,
    MissingInvoiceInput,
    MissingInvoiceWorkflow,
    OwnerAnswer,
    OwnerAnswerKind,
    TransactionRef,
    missing_invoice_workflow_id,
)
from backoffice.workflows.contracts import (
    ItemResolved,
    MatchOutcome,
    NeedsYouKind,
    SearchOutcome,
    ThreadCheckResult,
    VerificationOutcome,
)
from backoffice.workflows.services import PermanentServiceError
from backoffice.workflows.testing import ScriptedServices, SimulationError, WorkflowSimulator
from backoffice.workflows.text import find_forbidden

T0 = datetime(2026, 9, 20, 9, 0, tzinfo=timezone.utc)
SIX_DAYS = timedelta(days=6)
TX = TransactionRef(
    tenant_id="t1",
    transaction_id="tx-117",
    booked_on=date(2026, 9, 18),
    amount=Decimal("-117.20"),
    counterparty="VODAFONE PORTUGAL",
    supplier_name="Vodafone",
    invoice_number_hint="FT 2026/183",
)
WF_ID = missing_invoice_workflow_id("t1", "tx-117")


def run(coro):
    return asyncio.run(coro)


def green(svc: ScriptedServices, evidence_id: str, doc: str = "doc-1") -> None:
    """Script ``evidence_id`` as a verified invoice that matches the payment."""
    svc.verifications[evidence_id] = VerificationOutcome(
        quality=Quality.GREEN, document_id=doc, evidence_ids=(evidence_id,)
    )
    svc.matches[doc] = MatchOutcome(matched=True, quality=Quality.GREEN, evidence_ids=("ev-bank",))


async def started(svc: ScriptedServices, **policy) -> WorkflowSimulator:
    sim = WorkflowSimulator.for_services(svc.services(), start_time=T0)
    inp = MissingInvoiceInput(transaction=TX, policy=ChasePolicy(**policy))
    await sim.start(MissingInvoiceWorkflow.run, inp, id=WF_ID)
    return sim


def status(sim: WorkflowSimulator) -> str:
    text = sim.query(MissingInvoiceWorkflow.current_status)
    assert find_forbidden(text) == [], text
    return text


# ---------- the headline scenario


def test_chase_wait_six_days_remind_then_reply_signal_closes():
    async def scenario():
        svc = ScriptedServices()
        green(svc, "ev-reply")
        sim = await started(svc)

        # Search first, then ask, only because it is authorized (§22, §25).
        assert svc.names()[:3] == [
            "search_for_evidence",
            "is_action_authorized",
            "send_supplier_request",
        ]
        first = svc.sent_messages[0]
        assert first.default_body == (
            "Hello, could you please resend invoice FT 2026/183 relating to the "
            "€117.20 payment dated 18 September? Thank you."
        )
        assert first.idempotency_key == f"{WF_ID}:message:1"
        assert status(sim) == (
            "I asked Vodafone for the invoice on 20 September. Waiting for their reply."
        )

        # Nothing more is sent before the six days are up.
        await sim.advance(SIX_DAYS - timedelta(minutes=1))
        assert len(svc.sent_messages) == 1
        # ...but the thread is watched every 12 hours meanwhile.
        checks = sim.calls("check_thread_for_reply")
        assert [c.at - T0 for c in checks] == [timedelta(hours=12 * i) for i in range(1, 12)]

        await sim.advance(timedelta(minutes=1))
        assert len(svc.sent_messages) == 2
        reminder = svc.sent_messages[1]
        assert reminder.kind.value == "reminder"
        assert reminder.thread_id == "thread-1"  # same thread
        assert reminder.default_body.startswith("Hello, just following up:")
        reminder_call = sim.calls("send_supplier_request")[1]
        assert reminder_call.at == T0 + SIX_DAYS
        # A fresh search and a fresh permission check come right before it.
        assert svc.names()[-4:] == [
            "check_thread_for_reply",
            "search_for_evidence",
            "is_action_authorized",
            "send_supplier_request",
        ]
        assert status(sim) == "I reminded Vodafone on 26 September. Waiting for their reply."

        # The supplier replies a day later; ingestion signals the document.
        await sim.advance(timedelta(days=1))
        await sim.signal(
            MissingInvoiceWorkflow.document_received, DocumentArrival(evidence_ids=("ev-reply",))
        )
        assert sim.completed
        result = sim.result()
        assert result.status is ItemStatus.CLOSED
        assert result.evidence_ids == ("ev-reply", "ev-bank")
        assert result.document_id == "doc-1"
        assert result.messages_sent == 2
        assert result.escalations == 0
        assert result.summary == (
            "Done. The invoice for the €117.20 payment to Vodafone on 18 September "
            "is here and checks out."
        )
        [outcome] = svc.outcomes
        assert outcome.status is ItemStatus.CLOSED and outcome.quality is Quality.GREEN
        assert svc.owner_items == []  # the owner did nothing (§22)
        assert status(sim).startswith("Done.")
        await sim.verify_replay(sandboxed=True)

    run(scenario())


def test_waits_use_timers_not_polling_loops():
    async def scenario():
        svc = ScriptedServices()
        sim = await started(svc, thread_check_interval=timedelta(days=2))
        await sim.advance(SIX_DAYS)
        durations = [t.duration for t in sim.timers]
        assert durations[:3] == [timedelta(days=2)] * 3
        assert all(d > timedelta(0) for d in durations)

    run(scenario())


# ---------- evidence found other ways


def test_verified_invoice_found_by_search_closes_without_contacting_supplier():
    async def scenario():
        svc = ScriptedServices()
        svc.search_results = [SearchOutcome(evidence_ids=("ev-mail-att",))]
        green(svc, "ev-mail-att")
        sim = await started(svc)
        assert sim.completed
        assert sim.result().status is ItemStatus.CLOSED
        assert "send_supplier_request" not in svc.names()
        assert "is_action_authorized" not in svc.names()
        await sim.verify_replay()

    run(scenario())


def test_reply_found_by_thread_check_closes_without_any_signal():
    async def scenario():
        svc = ScriptedServices()
        svc.thread_replies = [
            ThreadCheckResult(replied=False),
            ThreadCheckResult(replied=True, evidence_ids=("ev-attachment",)),
        ]
        green(svc, "ev-attachment")
        sim = await started(svc)
        await sim.advance(timedelta(days=1))
        assert sim.completed
        assert sim.result().evidence_ids[0] == "ev-attachment"
        assert sim.calls("record_item_outcome")[0].at == T0 + timedelta(hours=24)
        await sim.verify_replay()

    run(scenario())


@pytest.mark.parametrize(
    ("document", "match"),
    [
        (Quality.AMBER, Quality.GREEN),  # likely document, perfect match
        (Quality.GREEN, Quality.AMBER),  # verified document, likely match
    ],
)
def test_amber_is_never_closed_and_is_shown_as_unconfirmed(document, match):
    async def scenario():
        svc = ScriptedServices()
        svc.authorized[GatedAction.SUPPLIER_INVOICE_REQUEST] = False
        svc.search_results = [SearchOutcome(evidence_ids=("ev-blurry",))]
        svc.verifications["ev-blurry"] = VerificationOutcome(
            quality=document, document_id="doc-a", evidence_ids=("ev-blurry",)
        )
        svc.matches["doc-a"] = MatchOutcome(matched=True, quality=match, evidence_ids=("ev-bank",))
        sim = await started(svc)
        assert not sim.completed
        assert svc.outcomes == []  # AMBER never closes (§57)
        [card] = svc.owner_items
        assert card.kind is NeedsYouKind.UNCONFIRMED_DOCUMENT
        assert card.evidence_ids == ("ev-blurry",)
        # Only a GREEN document is worth matching (Verify -> Match, §3).
        assert ("reconcile_transaction" in svc.names()) is (document is Quality.GREEN)
        assert status(sim) == (
            "I still need one thing. I found a document, but I can't confirm "
            "it's the invoice for this payment."
        )
        await sim.verify_replay()

    run(scenario())


def test_conflicting_document_goes_to_the_owner_and_is_never_guessed():
    async def scenario():
        svc = ScriptedServices()
        svc.search_results = [SearchOutcome(evidence_ids=("ev-qr-mismatch",))]
        svc.verifications["ev-qr-mismatch"] = VerificationOutcome(
            quality=Quality.RED,
            document_id="doc-x",
            evidence_ids=("ev-qr-mismatch",),
            reasons=("QR total 438.60 disagrees with 483.60",),
        )
        sim = await started(svc)
        [card] = svc.owner_items
        assert card.kind is NeedsYouKind.CONFLICT
        assert card.headline == "The invoice doesn't agree with the payment."
        assert card.evidence_ids == ("ev-qr-mismatch",)
        assert card.notify is False  # §42: stays in Needs You, no push
        assert svc.sent_messages == []
        assert svc.outcomes == []
        # It waits for the owner; time passing changes nothing.
        await sim.advance(timedelta(days=30))
        assert not sim.completed and len(svc.owner_items) == 1
        await sim.verify_replay()

    run(scenario())


# ---------- permission and escalation


def test_not_allowed_to_ask_escalates_then_keep_chasing_answer_allows_it():
    async def scenario():
        svc = ScriptedServices()
        svc.authorized[GatedAction.SUPPLIER_INVOICE_REQUEST] = False
        sim = await started(svc)
        assert svc.sent_messages == []
        [card] = svc.owner_items
        assert card.kind is NeedsYouKind.MISSING_DOCUMENT
        assert card.headline == "We can't find the invoice for this payment."
        assert "you haven't allowed me to contact suppliers" in card.detail
        assert [o.key for o in card.options] == [k.value for k in OwnerAnswerKind]

        await sim.advance(timedelta(hours=2))
        await sim.signal(
            MissingInvoiceWorkflow.owner_answered,
            OwnerAnswer(
                kind=OwnerAnswerKind.KEEP_CHASING,
                answered_by="owner-1",
                answer_evidence_id="ev-ans-1",
            ),
        )
        assert [r.message for r in svc.resolved_items] == ["OK. I'll keep asking."]
        assert len(svc.sent_messages) == 1  # the owner's answer is the permission
        assert svc.names().count("is_action_authorized") == 1
        await sim.verify_replay()

    run(scenario())


def test_no_reply_after_reminders_escalates_and_a_late_reply_still_closes():
    async def scenario():
        svc = ScriptedServices()
        sim = await started(svc, max_reminders=1)
        await sim.advance(2 * SIX_DAYS)
        assert len(svc.sent_messages) == 2
        [card] = svc.owner_items
        assert card.kind is NeedsYouKind.MISSING_DOCUMENT
        assert card.detail == (
            "€117.20 to Vodafone on 18 September. I asked Vodafone twice and didn't get it."
        )
        assert card.evidence_ids == ("ev-mail-1", "ev-mail-2")  # what we sent
        assert sim.calls("notify_owner")[0].at == T0 + 2 * SIX_DAYS
        # No third message, ever.
        green(svc, "ev-late")
        await sim.advance(timedelta(days=2))
        assert len(svc.sent_messages) == 2
        # The supplier finally answers; the daily late-reply check picks it up.
        svc.thread_replies = [ThreadCheckResult(replied=True, evidence_ids=("ev-late",))]
        await sim.advance(timedelta(days=1))
        assert sim.completed
        assert sim.calls("record_item_outcome")[0].at == T0 + 2 * SIX_DAYS + timedelta(days=3)
        assert sim.result().status is ItemStatus.CLOSED
        assert [r.message for r in svc.resolved_items] == ["Done. I found the invoice."]
        await sim.verify_replay()

    run(scenario())


def test_late_reply_listening_stops_after_the_window():
    async def scenario():
        svc = ScriptedServices()
        sim = await started(svc, max_reminders=0, late_reply_window=timedelta(days=2))
        await sim.advance(timedelta(days=30))
        checks = sim.calls("check_thread_for_reply")
        assert checks[-1].at == T0 + SIX_DAYS + timedelta(days=2)
        assert sim.pending_timers() == []  # nothing left but the owner
        assert not sim.completed

    run(scenario())


def test_missing_supplier_contact_is_asked_of_the_owner():
    async def scenario():
        svc = ScriptedServices()
        svc.supplier_contact = False
        sim = await started(svc)
        [card] = svc.owner_items
        assert card.kind is NeedsYouKind.NO_SUPPLIER_CONTACT
        assert card.detail.endswith("I don't have an email address for Vodafone.")
        assert card.options[2].label == "I added Vodafone's email address"
        assert not sim.completed

    run(scenario())


# ---------- owner answers


def test_owner_says_no_invoice_needed_ends_not_required_with_their_answer_as_evidence():
    async def scenario():
        svc = ScriptedServices()
        svc.authorized[GatedAction.SUPPLIER_INVOICE_REQUEST] = False
        sim = await started(svc)
        await sim.signal(
            MissingInvoiceWorkflow.owner_answered,
            OwnerAnswer(
                kind=OwnerAnswerKind.NO_DOCUMENT_NEEDED,
                answered_by="owner-1",
                answer_evidence_id="ev-answer-personal",
            ),
        )
        result = sim.result()
        assert result.status is ItemStatus.NOT_REQUIRED
        assert result.evidence_ids == ("ev-answer-personal",)
        [outcome] = svc.outcomes
        assert outcome.actor == "owner-1" and outcome.quality is None
        assert [r.message for r in svc.resolved_items] == ["Done."]
        await sim.verify_replay()

    run(scenario())


def test_owner_provided_document_is_verified_together_with_their_answer():
    async def scenario():
        svc = ScriptedServices()
        svc.authorized[GatedAction.SUPPLIER_INVOICE_REQUEST] = False
        sim = await started(svc)
        green(svc, "ev-scan")
        await sim.signal(
            MissingInvoiceWorkflow.owner_answered,
            OwnerAnswer(
                kind=OwnerAnswerKind.DOCUMENT_PROVIDED,
                answered_by="owner-1",
                answer_evidence_id="ev-answer-2",
                evidence_ids=("ev-scan",),
            ),
        )
        verify = sim.calls("ingest_and_verify")[-1].arg
        assert verify.evidence_ids == ("ev-scan", "ev-answer-2")
        assert sim.result().status is ItemStatus.CLOSED
        assert [r.message for r in svc.resolved_items] == ["Thanks. I'm checking it now."]
        await sim.verify_replay()

    run(scenario())


def test_owner_provided_document_that_does_not_verify_comes_back_to_them():
    async def scenario():
        svc = ScriptedServices()
        svc.authorized[GatedAction.SUPPLIER_INVOICE_REQUEST] = False
        sim = await started(svc)
        await sim.signal(
            MissingInvoiceWorkflow.owner_answered,
            OwnerAnswer(
                kind=OwnerAnswerKind.DOCUMENT_PROVIDED,
                answered_by="owner-1",
                answer_evidence_id="ev-answer-3",
                evidence_ids=("ev-receipt-photo",),
            ),
        )
        assert not sim.completed
        kinds = [c.kind for c in svc.owner_items]
        assert kinds == [NeedsYouKind.MISSING_DOCUMENT, NeedsYouKind.UNCONFIRMED_DOCUMENT]
        await sim.verify_replay()

    run(scenario())


def test_owner_will_handle_stops_chasing_and_closes_when_the_invoice_arrives():
    async def scenario():
        svc = ScriptedServices()
        sim = await started(svc, max_reminders=0)
        await sim.advance(SIX_DAYS)
        assert len(svc.owner_items) == 1
        await sim.signal(
            MissingInvoiceWorkflow.owner_answered,
            OwnerAnswer(
                kind=OwnerAnswerKind.OWNER_WILL_HANDLE,
                answered_by="owner-1",
                answer_evidence_id="ev-a",
            ),
        )
        assert status(sim) == (
            "You said you'll handle this. I'll close it when the invoice arrives."
        )
        await sim.advance(timedelta(days=20))
        assert len(svc.sent_messages) == 1
        green(svc, "ev-owner-upload")
        await sim.signal(
            MissingInvoiceWorkflow.document_received,
            DocumentArrival(evidence_ids=("ev-owner-upload",), channel="upload"),
        )
        assert sim.result().status is ItemStatus.CLOSED
        await sim.verify_replay()

    run(scenario())


def test_cancel_stops_immediately_and_tidies_the_owner_card():
    async def scenario():
        svc = ScriptedServices()
        svc.authorized[GatedAction.SUPPLIER_INVOICE_REQUEST] = False
        sim = await started(svc)
        await sim.signal(
            MissingInvoiceWorkflow.cancel,
            CancelRequest(requested_by="expected-evidence-engine", reason="internal transfer"),
        )
        result = sim.result()
        assert result.status is ItemStatus.CANCELLED
        assert result.evidence_ids == ()
        assert [r.message for r in svc.resolved_items] == ["No longer needed."]
        assert svc.outcomes[0].status is ItemStatus.CANCELLED
        await sim.verify_replay()

    run(scenario())


# ---------- month-close integration


def test_holds_supplier_contact_until_chase_window_and_searches_again_first():
    async def scenario():
        svc = ScriptedServices()
        hold = T0 + timedelta(days=2)
        sim = await started(svc, chase_not_before=hold)
        assert svc.sent_messages == []
        assert status(sim) == (
            "I'm still looking for the invoice for the €117.20 payment to Vodafone "
            "on 18 September. If it doesn't turn up, I'll ask Vodafone on 22 September."
        )
        # It arrives meanwhile and is found by the second search: no email at all.
        svc.search_results = [SearchOutcome(evidence_ids=("ev-arrived",))]
        green(svc, "ev-arrived")
        await sim.advance(timedelta(days=2))
        assert sim.calls("search_for_evidence")[-1].at == hold
        assert sim.result().status is ItemStatus.CLOSED
        assert svc.sent_messages == []
        await sim.verify_replay()

    run(scenario())


def test_reports_its_outcome_to_the_parent_workflow():
    async def scenario():
        svc = ScriptedServices()
        svc.search_results = [SearchOutcome(evidence_ids=("ev-1",))]
        green(svc, "ev-1")
        sim = WorkflowSimulator.for_services(svc.services(), start_time=T0)
        inp = MissingInvoiceInput(transaction=TX, report_to_workflow_id="month-close-1")
        await sim.start(MissingInvoiceWorkflow.run, inp, id=WF_ID)
        [sent] = sim.external_signals
        assert (sent.workflow_id, sent.signal_name) == ("month-close-1", "item_resolved")
        from backoffice.workflows import DATA_CONVERTER

        payload = DATA_CONVERTER.payload_converter.from_payloads(
            list(sent.payloads), [ItemResolved]
        )[0]
        assert payload == ItemResolved(subject_id="tx-117", status=ItemStatus.CLOSED)
        await sim.verify_replay()

    run(scenario())


# ---------- failure and determinism


def test_permanent_service_failure_fails_the_run_instead_of_retrying_forever():
    class BrokenSearch(ScriptedServices):
        async def search_for_evidence(self, request):
            raise PermanentServiceError("unknown tenant")

    async def scenario():
        svc = BrokenSearch()
        sim = WorkflowSimulator.for_services(svc.services(), start_time=T0)
        await sim.start(MissingInvoiceWorkflow.run, MissingInvoiceInput(transaction=TX), id=WF_ID)
        assert sim.completed
        with pytest.raises(SimulationError, match="workflow failed"):
            sim.result()
        assert svc.owner_items == []  # never a raw error in front of the owner

    run(scenario())


def test_replay_check_catches_a_history_that_does_not_match_the_code():
    async def scenario():
        svc = ScriptedServices()
        sim = await started(svc)
        await sim.verify_replay()  # genuine history replays cleanly
        history = sim.histories()[0]
        for event in history.events:
            if event.HasField("activity_task_scheduled_event_attributes"):
                event.activity_task_scheduled_event_attributes.activity_type.CopyFrom(
                    ActivityType(name="something_else")
                )
                break
        from temporalio.worker import Replayer

        from backoffice.workflows import DATA_CONVERTER, WORKFLOWS

        replayer = Replayer(workflows=list(WORKFLOWS), data_converter=DATA_CONVERTER)
        with pytest.raises(NondeterminismError):
            await replayer.replay_workflow(history)

    run(scenario())


def test_every_status_along_the_way_is_plain_language():
    async def scenario():
        svc = ScriptedServices()
        seen: list[str] = []
        sim = await started(svc, max_reminders=1)
        for _ in range(5):
            seen.append(status(sim))
            await sim.advance(timedelta(days=3))
        assert len(set(seen)) >= 3
        for text in seen + [c.headline + " " + c.detail + " " + c.why for c in svc.owner_items]:
            assert find_forbidden(text) == [], text
            assert "tx-117" not in text and "t1" not in text.split()

    run(scenario())


def test_operator_cancellation_tidies_up_before_stopping():
    async def scenario():
        svc = ScriptedServices()
        sim = await started(svc, max_reminders=0)
        await sim.advance(SIX_DAYS)
        assert len(svc.owner_items) == 1
        await sim.request_cancel()
        assert sim.completed and sim.cancelled
        with pytest.raises(SimulationError, match="cancelled"):
            sim.result()
        assert [r.message for r in svc.resolved_items] == ["No longer needed."]
        [outcome] = svc.outcomes
        assert outcome.status is ItemStatus.CANCELLED and outcome.actor == "system"
        await sim.verify_replay()

    run(scenario())
