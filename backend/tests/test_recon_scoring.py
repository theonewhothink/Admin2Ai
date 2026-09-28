"""Explainable candidate scoring (§20, §54, §57)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from backoffice.domain.models import (
    Document,
    DocumentType,
    ExtractionMethod,
    FieldObservation,
    Quality,
    Supplier,
    Transaction,
    TransactionKind,
    VerifiedField,
)
from backoffice.reconciliation import (
    CARD_LAST4_FIELD,
    AmountStatus,
    BankMetadata,
    Cadence,
    FactorKind,
    FxDetails,
    InMemoryPaymentHistory,
    MatchContext,
    Outcome,
    PaymentPattern,
    ScoringConfig,
    StaticFxRates,
    SupplierRelation,
    SupplierResolver,
    document_flow,
    grade,
    score_match,
)

T = "tenant"
D = Decimal
VODAFONE = Supplier(
    id="sup_voda",
    tenant_id=T,
    name="Vodafone Portugal",
    aliases=["Vodafone"],
    tax_id="PT502544180",
    known_ibans=["PT50000201231234567890154", "PT50000201239999999999999"],
)
UBER = Supplier(id="sup_uber", tenant_id=T, name="Uber B.V.", aliases=["UBER"])
RESOLVER = SupplierResolver([VODAFONE, UBER])


def tx(
    amount: str,
    day: date = date(2026, 9, 25),
    counterparty: str = "VODAFONE PT*1234 LISBOA",
    **kw,
) -> Transaction:
    kw.setdefault("id", f"tx_{counterparty[:4]}_{amount}_{day.isoformat()}")
    return Transaction(
        tenant_id=T,
        account_id="acc",
        booked_on=day,
        amount=D(amount),
        counterparty=counterparty,
        **kw,
    )


def doc(
    gross: str, day: date = date(2026, 9, 24), name: str = "Vodafone Portugal", **kw
) -> Document:
    kw.setdefault("quality", Quality.GREEN)
    kw.setdefault("doc_type", DocumentType.INVOICE)
    kw.setdefault("id", f"doc_{name[:4]}_{gross}_{day.isoformat()}")
    return Document(
        tenant_id=T,
        evidence_ids=["ev"],
        supplier_name=name,
        issue_date=day,
        gross_amount=D(gross),
        **kw,
    )


def card_field(last4: str) -> dict[str, VerifiedField]:
    obs = FieldObservation(
        value=last4, source="ev", method=ExtractionMethod.EMBEDDED_TEXT, confidence=0.99
    )
    return {
        CARD_LAST4_FIELD: VerifiedField(
            name=CARD_LAST4_FIELD,
            value=last4,
            quality=Quality.GREEN,
            observations=[obs],
        )
    }


def ctx(**kw) -> MatchContext:
    kw.setdefault("suppliers", RESOLVER)
    return MatchContext(**kw)


# --- §54 example


def test_spec_example_why_lines() -> None:
    history = InMemoryPaymentHistory(
        {"sup_voda": PaymentPattern(cadence=Cadence.MONTHLY, usual_amount=D("83.00"))}
    )
    payment = tx("-83.21", kind=TransactionKind.CARD, card_last4="4817")
    invoice = doc("83.21", supplier_tax_id="502544180", fields=card_field("**** 4817"))
    score = score_match([payment], [invoice], ctx(history=history))
    assert score.why == (
        "Invoice total: €83.21",
        "Bank charge: €83.21",
        "Dates: 1 day apart",
        "Supplier VAT: matches",
        "Card ending: matches",
        "Historical pattern: monthly",
    )
    assert (
        score.viable
        and score.identity_confirmed
        and score.amount.status is AmountStatus.EXACT
    )
    assert score.points == 40 + 5 + 10 + 20 + 5 + 5
    assert grade(score, [invoice], ScoringConfig()) is Quality.GREEN


def test_factors_are_machine_readable_too() -> None:
    score = score_match([tx("-83.21")], [doc("83.21")], ctx())
    kinds = [f.kind for f in score.factors]
    assert kinds[:3] == [FactorKind.AMOUNT, FactorKind.CURRENCY, FactorKind.DATES]
    assert all(isinstance(f.points, int) for f in score.factors)


# --- amounts and direction


def test_document_flow_signs() -> None:
    assert document_flow(doc("100")) == D("-100")
    assert document_flow(doc("20", doc_type=DocumentType.CREDIT_NOTE)) == D("20")
    sale = doc("500", supplier_tax_id="PT999999990")
    assert document_flow(sale, own_tax_ids=["999999990"]) == D("500")
    no_total = Document(tenant_id=T, evidence_ids=["e"])
    assert document_flow(no_total) is None


def test_refund_matches_credit_note_but_not_invoice() -> None:
    refund = tx("20.00", counterparty="VODAFONE PORTUGAL")
    note = doc("20.00", doc_type=DocumentType.CREDIT_NOTE, id="cn")
    invoice = doc("20.00", id="inv")
    assert score_match([refund], [note], ctx()).viable
    wrong_way = score_match([refund], [invoice], ctx())
    assert not wrong_way.viable and wrong_way.rejection == "direction"


def test_customer_payment_matches_own_sales_invoice() -> None:
    payment = tx(
        "500.00", counterparty="CLIENTE XPTO LDA", kind=TransactionKind.TRANSFER_IN
    )
    sale = doc(
        "500.00",
        name="Our Company",
        supplier_tax_id="PT999999990",
        invoice_number="FT A/77",
    )
    score = score_match([payment], [sale], ctx(own_tax_ids=["999999990"]))
    assert score.viable and score.amount.status is AmountStatus.EXACT
    assert "Money received: €500.00" in score.why


def test_near_amount_reports_the_difference_and_is_never_green() -> None:
    score = score_match([tx("-1001.50")], [doc("1000.00")], ctx())
    assert score.amount.status is AmountStatus.NEAR
    assert score.amount.difference == D("-1.50")
    assert "Difference: €1.50" in score.why
    assert grade(score, [doc("1000.00")], ScoringConfig()) is Quality.AMBER


def test_declared_bank_fee_reconciles_exactly() -> None:
    payment = tx("-1001.50", id="fee_tx")
    meta = {"fee_tx": BankMetadata(fee=D("1.50"))}
    invoice = doc("1000.00", supplier_tax_id="502544180")
    score = score_match([payment], [invoice], ctx(bank_metadata=meta))
    assert score.amount.status is AmountStatus.ADJUSTED and score.amount.fee == D(
        "1.50"
    )
    assert "Bank fees: €1.50" in score.why
    assert grade(score, [invoice], ScoringConfig()) is Quality.GREEN


def test_received_net_of_fee_reconciles_exactly() -> None:
    payment = tx("998.50", id="in_tx", counterparty="CLIENTE XPTO")
    sale = doc("1000.00", name="Us", supplier_tax_id="999999990")
    score = score_match(
        [payment],
        [sale],
        ctx(
            bank_metadata={"in_tx": BankMetadata(fee=D("1.50"))},
            own_tax_ids=["999999990"],
        ),
    )
    assert score.amount.status is AmountStatus.ADJUSTED


def test_partial_payment_needs_a_reference_to_be_viable() -> None:
    bare = score_match([tx("-300.00")], [doc("1000.00")], ctx())
    assert bare.amount.status is AmountStatus.PARTIAL and not bare.viable
    quoted = score_match(
        [tx("-300.00", description="FT 2026/183")],
        [doc("1000.00", invoice_number="FT 2026/183")],
        ctx(),
    )
    assert quoted.viable and quoted.identifier_found
    assert "Paid now: €300.00 of €1,000.00" in quoted.why


def test_outstanding_amount_override_after_earlier_payments() -> None:
    invoice = doc("1000.00", id="inv")
    score = score_match(
        [tx("-700.00")], [invoice], ctx(), document_amounts={"inv": D("-700.00")}
    )
    assert score.amount.status is AmountStatus.EXACT
    assert score.why[:2] == ("Invoice total: €1,000.00", "Still to pay: €700.00")


# --- FX


def test_declared_fx_with_rate_and_fee_is_exact() -> None:
    payment = tx(
        "-92.45", id="fx_tx", counterparty="UBER *TRIP", kind=TransactionKind.CARD
    )
    meta = {
        "fx_tx": BankMetadata(
            fx=FxDetails(D("100.00"), "USD", rate=D("0.9150"), fee=D("0.95"))
        )
    }
    invoice = doc("100.00", name="Uber B.V.", currency="USD", day=date(2026, 9, 25))
    score = score_match([payment], [invoice], ctx(bank_metadata=meta))
    assert score.amount.status is AmountStatus.ADJUSTED and score.amount.fx_declared
    assert score.amount.fee == D("0.95")
    assert "Invoice total: $100.00" in score.why
    assert "Exchange rate: 1 USD = 0.9150 EUR" in score.why
    assert "Bank fees: €0.95" in score.why
    assert grade(score, [invoice], ScoringConfig()) is Quality.GREEN


def test_declared_fx_with_undeclared_spread_is_plausible_and_reports_the_cost() -> None:
    payment = tx("-92.60", id="fx_tx", counterparty="UBER *TRIP")
    meta = {"fx_tx": BankMetadata(fx=FxDetails(D("100.00"), "USD", rate=D("0.9150")))}
    invoice = doc("100.00", name="Uber B.V.", currency="USD", day=date(2026, 9, 25))
    score = score_match([payment], [invoice], ctx(bank_metadata=meta))
    assert score.amount.status is AmountStatus.NEAR
    assert score.amount.conversion_cost == D("1.10")
    assert "Conversion cost: €1.10" in score.why
    assert grade(score, [invoice], ScoringConfig()) is Quality.AMBER


def test_declared_original_amount_without_rate_is_exact() -> None:
    payment = tx("-93.00", id="fx_tx", counterparty="UBER *TRIP")
    meta = {"fx_tx": BankMetadata(fx=FxDetails(D("100.00"), "USD"))}
    invoice = doc("100.00", name="Uber B.V.", currency="USD", day=date(2026, 9, 25))
    score = score_match([payment], [invoice], ctx(bank_metadata=meta))
    assert score.amount.exact and score.amount.fx_rate == D("0.93")


def test_fx_amount_read_from_bank_text() -> None:
    payment = tx(
        "-92.45", counterparty="UBER *TRIP", description="USD 100,00 TAXA 0,9245"
    )
    invoice = doc("100.00", name="Uber B.V.", currency="USD", day=date(2026, 9, 25))
    score = score_match([payment], [invoice], ctx())
    assert score.amount.exact
    off = score_match(
        [payment], [invoice], ctx(config=ScoringConfig(read_fx_from_text=False))
    )
    assert not off.viable


def test_reference_rate_fx_is_near_with_estimated_cost() -> None:
    rates = StaticFxRates({("USD", "EUR"): D("0.9200")})
    payment = tx("-93.00", counterparty="UBER *TRIP")
    invoice = doc("100.00", name="Uber B.V.", currency="USD", day=date(2026, 9, 25))
    score = score_match([payment], [invoice], ctx(fx_rates=rates))
    assert score.amount.status is AmountStatus.NEAR and not score.amount.fx_declared
    assert score.amount.conversion_cost == D("1.00")
    assert "Conversion cost: about €1.00" in score.why
    too_far = score_match(
        [tx("-99.00", counterparty="UBER *TRIP")], [invoice], ctx(fx_rates=rates)
    )
    assert not too_far.viable


def test_foreign_currency_without_any_rate_cannot_match() -> None:
    score = score_match([tx("-92.00")], [doc("100.00", currency="USD")], ctx())
    assert not score.viable and score.amount.status is AmountStatus.MISMATCH


# --- identity signals


def test_invoice_number_must_stand_alone() -> None:
    invoice = doc("50.00", invoice_number="FT 2026/183", name="Someone Else")
    hit = score_match(
        [tx("-50.00", counterparty="X", description="PAG FT2026-183")], [invoice], ctx()
    )
    miss = score_match(
        [tx("-50.00", counterparty="X", description="PAG FT 2026/1834")],
        [invoice],
        ctx(),
    )
    assert (
        hit.identifier_found and "Invoice number: found in the bank details" in hit.why
    )
    assert not miss.identifier_found


def test_short_numbers_are_not_treated_as_references() -> None:
    invoice = doc("50.00", invoice_number="183")
    score = score_match([tx("-50.00", description="183")], [invoice], ctx())
    assert not score.identifier_found


def test_structured_payment_reference_matches() -> None:
    invoice = doc("50.00", payment_reference="123 456 789", name="Utility")
    score = score_match(
        [tx("-50.00", counterparty="PAG SERV", reference="123456789")], [invoice], ctx()
    )
    assert "Payment reference: matches" in score.why and score.identity_confirmed


def test_bank_details_match_supports() -> None:
    payment = tx(
        "-83.21", counterparty_iban="PT50000201231234567890154", counterparty="VODAFONE"
    )
    invoice = doc("83.21", iban="PT50 0002 0123 1234 5678 9015 4")
    score = score_match([payment], [invoice], ctx())
    assert "Bank details: match" in score.why and not score.conflict


def test_bank_details_disagreement_is_red() -> None:
    payment = tx(
        "-83.21", counterparty_iban="PT50000201231234567890154", counterparty="VODAFONE"
    )
    invoice = doc("83.21", iban="PT50111111111111111111111")
    score = score_match([payment], [invoice], ctx())
    assert score.conflict and "Bank details: do not match" in score.why
    assert grade(score, [invoice], ScoringConfig()) is Quality.RED


def test_two_known_accounts_of_the_same_supplier_are_not_a_conflict() -> None:
    payment = tx(
        "-83.21", counterparty_iban="PT50000201231234567890154", counterparty="VODAFONE"
    )
    invoice = doc("83.21", iban="PT50000201239999999999999")
    score = score_match([payment], [invoice], ctx())
    assert not score.conflict


def test_card_ending_disagreement_blocks_green() -> None:
    payment = tx("-83.21", kind=TransactionKind.CARD, card_last4="1111")
    invoice = doc("83.21", supplier_tax_id="502544180", fields=card_field("4817"))
    score = score_match([payment], [invoice], ctx())
    assert "Card ending: does not match" in score.why and score.contradicted
    assert grade(score, [invoice], ScoringConfig()) is Quality.AMBER


def test_different_known_suppliers_are_not_candidates() -> None:
    score = score_match(
        [tx("-14.20", counterparty="UBER *TRIP")], [doc("14.20")], ctx()
    )
    assert score.supplier is SupplierRelation.DIFFERENT and not score.viable


def test_clearly_different_names_are_penalised() -> None:
    score = score_match(
        [tx("-50.00", counterparty="PADARIA CENTRAL")],
        [doc("50.00", name="Oficina Mecanica Silva")],
        ctx(),
    )
    assert score.supplier is SupplierRelation.DIFFERENT_NAME
    assert score.points < ScoringConfig().plausible_threshold


def test_amount_and_date_alone_are_plausible_but_not_verified() -> None:
    payment = tx("-50.00", counterparty="", description="")
    invoice = doc("50.00", name="", day=date(2026, 9, 25))
    score = score_match([payment], [invoice], ctx())
    assert score.viable and not score.identity_confirmed
    assert (
        ScoringConfig().plausible_threshold
        <= score.points
        < ScoringConfig().high_threshold
    )
    assert grade(score, [invoice], ScoringConfig()) is Quality.AMBER


# --- dates


def test_date_window_is_configurable() -> None:
    late = tx("-83.21", day=date(2026, 12, 1))
    invoice = doc("83.21")
    assert not score_match([late], [invoice], ctx()).viable
    wide = ScoringConfig(max_payment_lag_days=90)
    assert score_match([late], [invoice], ctx(config=wide)).viable


def test_late_payment_quoting_the_invoice_is_viable_but_not_green() -> None:
    late = tx("-83.21", day=date(2026, 12, 1), description="FT 2026/183")
    invoice = doc("83.21", invoice_number="FT 2026/183", supplier_tax_id="502544180")
    score = score_match([late], [invoice], ctx())
    assert score.viable and score.contradicted
    assert grade(score, [invoice], ScoringConfig()) is Quality.AMBER


def test_due_date_supports_direct_debits() -> None:
    invoice = doc("83.21", day=date(2026, 9, 1), due_date=date(2026, 9, 25))
    score = score_match([tx("-83.21")], [invoice], ctx())
    assert "Due date: matches" in score.why


def test_deposit_window_only_when_allowed() -> None:
    early = tx("-300.00", day=date(2026, 8, 1), description="FT 2026/9")
    invoice = doc("1000.00", day=date(2026, 9, 24), invoice_number="FT 2026/9")
    strict = score_match([early], [invoice], ctx())
    relaxed = score_match([early], [invoice], ctx(), allow_deposit=True)
    assert any(
        f.kind is FactorKind.DATES and f.outcome is Outcome.CONTRADICTS
        for f in strict.factors
    )
    assert not relaxed.contradicted


# --- quality rules


def test_amber_document_never_yields_green_match() -> None:
    invoice = doc("83.21", supplier_tax_id="502544180", quality=Quality.AMBER)
    score = score_match([tx("-83.21")], [invoice], ctx())
    assert score.points >= ScoringConfig().high_threshold
    assert grade(score, [invoice], ScoringConfig()) is Quality.AMBER


def test_conflicting_document_makes_match_red() -> None:
    invoice = doc("83.21", supplier_tax_id="502544180", quality=Quality.RED)
    score = score_match([tx("-83.21")], [invoice], ctx())
    assert grade(score, [invoice], ScoringConfig()) is Quality.RED


def test_ambiguity_and_incomplete_search_cap_quality() -> None:
    invoice = doc("83.21", supplier_tax_id="502544180")
    score = score_match([tx("-83.21")], [invoice], ctx())
    assert grade(score, [invoice], ScoringConfig(), ambiguous=True) is Quality.AMBER
    assert (
        grade(score, [invoice], ScoringConfig(), search_complete=False) is Quality.AMBER
    )


# --- groups


def test_group_scoring_explains_several_invoices() -> None:
    a = doc("100.00", id="a", day=date(2026, 9, 1), invoice_number="FT 2026/101")
    b = doc("150.00", id="b", day=date(2026, 9, 2), invoice_number="FT 2026/102")
    payment = tx(
        "-250.00",
        day=date(2026, 9, 12),
        counterparty="VODAFONE",
        description="FT 2026/101 FT 2026/102",
    )
    score = score_match([payment], [a, b], ctx())
    assert score.amount.status is AmountStatus.EXACT
    assert "Invoices total: €250.00 (2 invoices)" in score.why
    assert "Dates: up to 11 days apart" in score.why
    assert "Invoice numbers: all found in the bank details" in score.why


def test_mixed_currencies_are_rejected() -> None:
    score = score_match(
        [tx("-1"), tx("-2", currency="USD", id="usd")], [doc("3")], ctx()
    )
    assert score.rejection == "mixed currencies"


# --- configuration


def test_config_rejects_float_money_and_bad_thresholds() -> None:
    with pytest.raises(ValueError):
        ScoringConfig(near_amount_abs=2.0)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        ScoringConfig(plausible_threshold=80, high_threshold=70)
    with pytest.raises(ValueError):
        ScoringConfig(max_payment_lag_days=-1)


def test_near_tolerance_scales_and_caps() -> None:
    cfg = ScoringConfig()
    assert cfg.near_tolerance(D("83.21")) == D("2.00")
    assert cfg.near_tolerance(D("-1000")) == D("10.00")
    assert cfg.near_tolerance(D("100000")) == D("25.00")


# --- supplier relations


def test_conflicting_supplier_signals_never_verify() -> None:
    # IBAN says Vodafone, descriptor says Uber: resolution is a conflict (§19).
    payment = tx(
        "-83.21",
        counterparty="UBER *TRIP",
        counterparty_iban="PT50000201231234567890154",
    )
    invoice = doc("83.21", supplier_tax_id="502544180", invoice_number="FT 9/1")
    score = score_match([payment], [invoice], ctx())
    assert (
        score.supplier is SupplierRelation.UNCLEAR and "Supplier: unclear" in score.why
    )
    assert grade(score, [invoice], ScoringConfig()) is Quality.AMBER


def test_document_tax_id_of_someone_else_rules_out_the_known_payee() -> None:
    invoice = doc("83.21", name="Unreadable Name", supplier_tax_id="PT999999990")
    score = score_match([tx("-83.21")], [invoice], ctx())
    assert score.supplier is SupplierRelation.DIFFERENT and not score.viable


def test_fuzzy_resolution_only_counts_as_similar() -> None:
    adobe = Supplier(
        id="sup_adobe", tenant_id=T, name="Adobe Systems Software Ireland Ltd"
    )
    context = ctx(suppliers=SupplierResolver([adobe]))
    payment = tx("-24.59", counterparty="PAYPAL *ADOBESYSTEM 99")
    invoice = doc("24.59", name="Adobe Systems Software Ireland Ltd")
    score = score_match([payment], [invoice], context)
    assert (
        score.supplier is SupplierRelation.SIMILAR
        and "Supplier name: similar" in score.why
    )
    assert grade(score, [invoice], ScoringConfig()) is Quality.AMBER


def test_unknown_suppliers_with_similar_names() -> None:
    payment = tx("-50.00", counterparty="PASTELARIA BELEMM")
    invoice = doc("50.00", name="Pastelaria Belem")
    assert score_match([payment], [invoice], ctx()).supplier is SupplierRelation.SIMILAR


def test_history_with_another_usual_card_does_not_support() -> None:
    pattern = PaymentPattern(cadence=Cadence.MONTHLY, card_last4=frozenset({"9999"}))
    history = InMemoryPaymentHistory({"sup_voda": pattern})
    payment = tx("-83.21", kind=TransactionKind.CARD, card_last4="4817")
    score = score_match([payment], [doc("83.21")], ctx(history=history))
    assert not any(line.startswith("Historical pattern") for line in score.why)


def test_some_invoice_numbers_found() -> None:
    a = doc("100.00", id="a", invoice_number="FT 2026/101")
    b = doc("150.00", id="b", invoice_number="FT 2026/102")
    payment = tx("-250.00", counterparty="VODAFONE", description="FT 2026/101")
    score = score_match([payment], [a, b], ctx())
    assert "Invoice numbers: 1 of 2 found in the bank details" in score.why
