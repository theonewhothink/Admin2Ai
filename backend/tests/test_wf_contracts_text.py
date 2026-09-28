"""Contracts (Decimal money, evidence rules) and owner-facing words (§3, §22, §36, §69-70)."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from backoffice.domain.models import Quality, Supplier, Transaction
from backoffice.workflows import DATA_CONVERTER, text
from backoffice.workflows import decisions as d
from backoffice.workflows.contracts import (
    ApprovalCategory,
    ApprovalDecisionKind,
    ApprovalInput,
    ApprovalRecord,
    ApprovalRequest,
    ApprovalResult,
    ApprovalStatus,
    ChasePolicy,
    ClosureVerdict,
    DeliveryConfirmation,
    DeliveryReceipt,
    ItemOutcome,
    ItemStatus,
    MissingInvoiceInput,
    MonthCloseInput,
    MonthCloseState,
    MonthStep,
    MonthSummary,
    NeedsYouKind,
    OwnerAnswer,
    OwnerAnswerKind,
    Period,
    QueryResolution,
    StepRecord,
    SupplierMessageKind,
    TransactionRef,
)

T0 = datetime(2026, 9, 20, 9, 0, tzinfo=timezone.utc)
TX = TransactionRef(
    tenant_id="t1",
    transaction_id="tx-1",
    booked_on=date(2026, 9, 18),
    amount=Decimal("-117.20"),
    counterparty="VODAFONE PT 0123",
    supplier_name="Vodafone",
    invoice_number_hint="FT 2026/183",
)


# ========== contracts


class TestMoney:
    def test_float_money_is_refused_everywhere(self):
        with pytest.raises(ValidationError, match="never float"):
            TransactionRef.model_validate({**TX.model_dump(), "amount": 117.2})
        with pytest.raises(ValidationError, match="never float"):
            ApprovalInput(
                tenant_id="t", request_id="r", category=ApprovalCategory.MONEY_MOVEMENT,
                summary="Pay", amount=1200.0, evidence_ids=("e",), requested_by="a",
            )  # fmt: skip
        with pytest.raises(ValidationError, match="never float"):
            ApprovalRequest(
                tenant_id="t", request_id="r", category=ApprovalCategory.MONEY_MOVEMENT,
                headline="h", summary="s", amount=1.5, evidence_ids=("e",), dedupe_key="k",
            )  # fmt: skip

    def test_decimal_survives_the_temporal_payload_round_trip_exactly(self):
        inp = MissingInvoiceInput(transaction=TX.model_copy(update={"amount": Decimal("-1492.30")}))
        payloads = DATA_CONVERTER.payload_converter.to_payloads([inp])
        assert b'"-1492.30"' in payloads[0].data  # a string, never a float
        [back] = DATA_CONVERTER.payload_converter.from_payloads(payloads, [MissingInvoiceInput])
        assert back == inp and isinstance(back.transaction.amount, Decimal)

    def test_datetimes_must_be_timezone_aware(self):
        with pytest.raises(ValidationError):
            ChasePolicy(chase_not_before=datetime(2026, 9, 20, 9, 0))

    def test_currency_code_shape(self):
        with pytest.raises(ValidationError):
            TX.model_validate({**TX.model_dump(), "currency": "eur"})


class TestContractRules:
    def test_closure_needs_green_evidence_and_a_document(self):
        base = dict(tenant_id="t", subject_id="s", status=ItemStatus.CLOSED, actor="a")
        ItemOutcome(**base, quality=Quality.GREEN, evidence_ids=("e",), document_id="d")
        for bad in (
            dict(quality=Quality.AMBER, evidence_ids=("e",), document_id="d"),
            dict(quality=Quality.GREEN, evidence_ids=(), document_id="d"),
            dict(quality=Quality.GREEN, evidence_ids=("e",), document_id=None),
        ):
            with pytest.raises(ValidationError, match="closure requires"):
                ItemOutcome(**base, **bad)

    def test_not_required_needs_evidence_but_cancelled_does_not(self):
        with pytest.raises(ValidationError):
            ItemOutcome(tenant_id="t", subject_id="s", status=ItemStatus.NOT_REQUIRED, actor="a")
        ItemOutcome(tenant_id="t", subject_id="s", status=ItemStatus.CANCELLED, actor="a")

    def test_owner_document_answer_must_point_at_evidence(self):
        with pytest.raises(ValidationError):
            OwnerAnswer(
                kind=OwnerAnswerKind.DOCUMENT_PROVIDED, answered_by="o", answer_evidence_id="a"
            )
        with pytest.raises(ValidationError):
            OwnerAnswer(kind=OwnerAnswerKind.KEEP_CHASING, answered_by="o", answer_evidence_id=" ")

    def test_confirmations_and_answers_need_evidence(self):
        with pytest.raises(ValidationError):
            DeliveryConfirmation(confirmed=True)
        with pytest.raises(ValidationError):
            QueryResolution(query_id="q", resolved=True)
        DeliveryConfirmation(confirmed=False)
        QueryResolution(query_id="q", resolved=False)

    def test_an_approval_result_always_says_who_decided(self):
        with pytest.raises(ValidationError):
            ApprovalResult(status=ApprovalStatus.APPROVED, request_id="r", summary="x")
        rejecting = ApprovalRecord(
            request_id="r",
            decision=ApprovalDecisionKind.REJECT,
            actor_id="a",
            auth_method="p",
            decided_at=T0,
        )
        with pytest.raises(ValidationError):
            ApprovalResult(
                status=ApprovalStatus.APPROVED, request_id="r", record=rejecting, summary="x"
            )
        ApprovalResult(status=ApprovalStatus.WITHDRAWN, request_id="r", summary="x")

    def test_chase_policy_bounds(self):
        with pytest.raises(ValidationError):
            ChasePolicy(reply_wait=timedelta(0))
        with pytest.raises(ValidationError):
            ChasePolicy(thread_check_interval=timedelta(seconds=-1))
        with pytest.raises(ValidationError):
            ChasePolicy(max_reminders=-1)
        with pytest.raises(ValidationError):
            ChasePolicy(late_reply_window=timedelta(days=-1))
        assert ChasePolicy().reply_wait == timedelta(days=6)  # §45

    def test_approval_reminder_intervals(self):
        base = dict(
            tenant_id="t", request_id="r", category=ApprovalCategory.TAX_FILING,
            summary="File the VAT return.", evidence_ids=("e",), requested_by="a",
        )  # fmt: skip
        with pytest.raises(ValidationError):
            ApprovalInput(**base, first_reminder_after=timedelta(0))
        with pytest.raises(ValidationError):
            ApprovalInput(
                **base,
                first_reminder_after=timedelta(days=2),
                max_reminder_interval=timedelta(days=1),
            )
        with pytest.raises(ValidationError):
            ApprovalInput(**{**base, "evidence_ids": ()})

    def test_month_input_validation(self):
        base = dict(
            tenant_id="t",
            entity_id="e",
            period=Period(year=2026, month=9),
            day_zero=date(2026, 10, 5),
        )
        MonthCloseInput(**base)
        with pytest.raises(ValidationError, match="order"):
            MonthCloseInput(
                **base, day_offsets={**MonthCloseInput(**base).day_offsets, MonthStep.PACKAGE: -6}
            )
        with pytest.raises(ValidationError, match="every scheduled step"):
            MonthCloseInput(**base, day_offsets={MonthStep.PACKAGE: 0})
        with pytest.raises(ValidationError, match="UTC"):
            MonthCloseInput(**base, run_at=time(6, 0, tzinfo=timezone.utc))
        with pytest.raises(ValidationError, match="before the month"):
            MonthCloseInput(**{**base, "day_zero": date(2026, 8, 31)})
        with pytest.raises(ValidationError):
            MonthCloseInput(**base, closure_recheck_interval=timedelta(0))

    def test_period_helpers(self):
        feb = Period(year=2028, month=2)
        assert (feb.first_day, feb.last_day, feb.month_name, feb.key) == (
            date(2028, 2, 1),
            date(2028, 2, 29),
            "February",
            "2028-02",
        )
        with pytest.raises(ValidationError):
            Period(year=2026, month=13)

    def test_transaction_ref_from_domain_transaction(self):
        tx = Transaction(
            tenant_id="t1", account_id="acc", booked_on=date(2026, 9, 18),
            amount=Decimal("-117.20"), counterparty="VODAFONE PT", entity_id="e1",
        )  # fmt: skip
        sup = Supplier(tenant_id="t1", name="Vodafone")
        ref = TransactionRef.from_transaction(tx, supplier=sup, invoice_number_hint="FT 1")
        assert (ref.transaction_id, ref.supplier_id, ref.supplier_display) == (
            tx.id,
            sup.id,
            "Vodafone",
        )
        assert TransactionRef.from_transaction(tx).supplier_display == "VODAFONE PT"

    def test_contracts_are_immutable_and_strict(self):
        with pytest.raises(ValidationError):
            TX.model_validate({**TX.model_dump(), "unexpected": 1})
        with pytest.raises(ValidationError):
            TX.amount = Decimal("1")  # type: ignore[misc]


# ========== text


class TestFormatting:
    @pytest.mark.parametrize(
        ("amount", "currency", "expected"),
        [
            (Decimal("1492.3"), "EUR", "€1,492.30"),
            (Decimal("117.205"), "EUR", "€117.21"),  # half up
            (Decimal("-12"), "GBP", "−£12.00"),
            (Decimal("1492.30"), "CHF", "CHF 1,492.30"),
            (0, "usd", "$0.00"),
        ],
    )
    def test_money(self, amount, currency, expected):
        assert text.format_money(amount, currency) == expected

    def test_money_refuses_floats_and_nonsense(self):
        with pytest.raises(TypeError):
            text.format_money(1.5)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            text.format_money(Decimal("NaN"))
        with pytest.raises(ValueError):
            text.format_money(Decimal("1"), "EURO")

    def test_days_show_the_year_only_when_it_differs(self):
        assert text.format_day(date(2026, 9, 18), date(2026, 12, 1)) == "18 September"
        assert text.format_day(date(2025, 12, 30), date(2026, 1, 4)) == "30 December 2025"
        assert text.format_day(T0) == "20 September 2026"

    def test_counting_words(self):
        assert [text.count_times(n) for n in (1, 2, 3)] == ["once", "twice", "3 times"]
        assert text.plural(1, "document") == "1 document"
        assert text.plural(2, "query", "queries") == "2 queries"
        assert [text.still_need(n) for n in (0, 1, 3)] == [
            "Done.",
            "I still need one thing.",
            "I still need 3 things.",
        ]
        with pytest.raises(ValueError):
            text.still_need(-1)


class TestSupplierMessage:
    def test_section_22_wording(self):
        assert text.supplier_request_body(TX, SupplierMessageKind.REQUEST, date(2026, 9, 20)) == (
            "Hello, could you please resend invoice FT 2026/183 relating to the €117.20 "
            "payment dated 18 September? Thank you."
        )

    def test_without_invoice_number_and_as_reminder(self):
        tx = TX.model_copy(update={"invoice_number_hint": " "})
        body = text.supplier_request_body(tx, SupplierMessageKind.REMINDER, date(2027, 1, 3))
        assert body == (
            "Hello, just following up: could you please send the invoice for the €117.20 "
            "payment dated 18 September 2026? Thank you."
        )


def _all_owner_strings() -> list[str]:
    state = d.new_chase_state(ChasePolicy()).model_copy(update={"messages_sent": 2})
    strings: list[str] = []
    for reason in d.EscalationReason:
        card = text.chase_card(reason, TX, state, date(2026, 10, 2))
        strings += [card.headline, card.detail, card.why, *(o.label for o in card.options)]
    strings += [text.resolution_message(r) for r in d.Resolution]
    strings += [text.chase_summary(s, TX) for s in ItemStatus]
    period = Period(year=2026, month=9)
    for card in (
        text.package_ready_card(period, allowed=False),
        text.package_ready_card(period, allowed=True),
        text.delivery_unconfirmed_card(period, date(2026, 10, 5)),
        text.delivery_unconfirmed_card(period, None),
        text.accountant_question_card("Was the dinner a business meal?"),
    ):
        strings += [card.headline, card.detail, card.why, *(o.label for o in card.options)]
    strings += [text.APPROVAL_HEADLINE, text.APPROVAL_REMINDER_HEADLINE]
    record = ApprovalRecord(
        request_id="r", decision=ApprovalDecisionKind.APPROVE, actor_id="ana",
        actor_display_name="Ana Silva", auth_method="passkey", decided_at=T0,
    )  # fmt: skip
    strings += [text.approval_summary(s, record) for s in ApprovalStatus]
    strings += [text.approval_status_text("Pay €1,200.00 to Hazel Tree Lda.", None)]
    strings += [text.month_summary_line(MonthSummary())]
    for step in [*MonthStep, None]:
        strings.append(text.month_status_text(MonthCloseState(), period, step, T0))
    return strings


class TestOwnerWords:
    def test_every_owner_string_is_plain_calm_and_free_of_ids(self):
        for sentence in _all_owner_strings():
            assert sentence.strip(), "empty owner text"
            assert text.find_forbidden(sentence) == [], sentence
            assert "tx-1" not in sentence and "t1" not in sentence.split()

    def test_the_linter_catches_what_it_should(self):
        assert "reconciliation" in text.find_forbidden("Reconciliation exception")
        assert "workflow" in text.find_forbidden("The workflow timed out")
        assert "timeout" in text.find_forbidden("Activity timeout")
        assert "internal ID" in text.find_forbidden("see tx_0123456789abcdef")
        assert "internal ID" in text.find_forbidden("missing-invoice:t1:tx-9")
        assert "raw error" in text.find_forbidden("ValueError: bad")
        assert "exclamation mark" in text.find_forbidden("Great job!")
        assert text.find_forbidden("Done. I found the invoice.") == []

    def test_chase_cards_use_the_spec_phrases(self):
        state = d.new_chase_state(ChasePolicy()).model_copy(update={"messages_sent": 1})
        missing = text.chase_card(d.EscalationReason.NO_REPLY, TX, state, date(2026, 10, 2))
        assert missing.kind is NeedsYouKind.MISSING_DOCUMENT
        assert missing.headline == "We can't find the invoice for this payment."  # §36
        assert (
            missing.detail
            == "€117.20 to Vodafone on 18 September. I asked Vodafone once and didn't get it."
        )
        conflict = text.chase_card(d.EscalationReason.CONFLICT, TX, state, date(2026, 10, 2))
        assert conflict.why == "When documents disagree, a person decides. I never guess."

    def test_month_texts(self):
        period = Period(year=2026, month=9)
        assert text.month_closed_headline(period) == "September is closed."
        line = text.month_summary_line(
            MonthSummary(
                transactions_checked=1, documents_collected=2, missing_documents_retrieved=1,
                suppliers_chased=1, accountant_questions_resolved=1, tax_obligations_verified=1,
                unresolved_issues=0,
            )
        )  # fmt: skip
        assert line.startswith("1 transaction checked · 2 documents collected · 1 missing document")
        assert "You spent" not in line
        waiting = MonthCloseState(last_verdict=ClosureVerdict(closed=False, open_items=2))
        assert text.month_status_text(waiting, period, MonthStep.CLOSURE, None) == (
            "September: I still need 3 things."  # 2 open + delivery not confirmed
        )
        delivered = MonthCloseState(
            delivery=DeliveryReceipt(delivered=True, delivery_evidence_id="e")
        )
        assert text.month_status_text(delivered, period, MonthStep.DELIVERY_CONFIRMATION, None) == (
            "September: sent to your accountant."
        )

    def test_status_text_for_a_finished_chase(self):
        final = d.FinalOutcome(status=ItemStatus.NOT_REQUIRED, actor="o", evidence_ids=("e",))
        state = d.new_chase_state(ChasePolicy()).model_copy(update={"final": final})
        assert text.chase_status_text(state, TX, ChasePolicy()) == (
            "Done. No invoice needed for the €117.20 payment to Vodafone on 18 September."
        )

    def test_step_records_are_ordered_data(self):
        record = StepRecord(step=MonthStep.PACKAGE, scheduled_for=T0, ran_at=T0)
        assert record.step is MonthStep.PACKAGE
