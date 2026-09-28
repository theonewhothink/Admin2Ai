"""Reconciliation engine (§20): shapes, quality rules, ambiguity, determinism."""

from __future__ import annotations

import logging
import random
import re
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

import pytest

from backoffice.domain.models import (
    Document,
    DocumentType,
    ExtractionMethod,
    FieldObservation,
    LegalEntity,
    Quality,
    Supplier,
    Transaction,
    TransactionKind,
    VerifiedField,
)
from backoffice.reconciliation import (
    CARD_LAST4_FIELD,
    BankMetadata,
    ExpectedEvidenceEngine,
    FxDetails,
    MatchKind,
    MatchTag,
    ReconciliationConfig,
    StaticFxRates,
    SupplierResolver,
    document_flow,
    reconcile,
)

T = "tenant"
D = Decimal
SEPT = date(2026, 9, 1)
VODAFONE = Supplier(
    id="sup_voda",
    tenant_id=T,
    name="Vodafone Portugal",
    aliases=["Vodafone"],
    tax_id="PT502544180",
    known_ibans=["PT50000201231234567890154"],
)
ADOBE = Supplier(
    id="sup_adobe",
    tenant_id=T,
    name="Adobe Systems Software Ireland Ltd",
    aliases=["Adobe"],
)
RESOLVER = SupplierResolver([VODAFONE, ADOBE])


def tx(
    id: str, amount: str, day: int | date, counterparty: str = "ACME LDA", **kw
) -> Transaction:
    booked = day if isinstance(day, date) else SEPT + timedelta(days=day - 1)
    kw.setdefault("account_id", "acc_bank")
    return Transaction(
        id=id,
        tenant_id=T,
        booked_on=booked,
        amount=D(amount),
        counterparty=counterparty,
        **kw,
    )


def doc(
    id: str, gross: str, day: int | date, name: str = "ACME, Lda", **kw
) -> Document:
    issued = day if isinstance(day, date) else SEPT + timedelta(days=day - 1)
    kw.setdefault("quality", Quality.GREEN)
    kw.setdefault("doc_type", DocumentType.INVOICE)
    return Document(
        id=id,
        tenant_id=T,
        evidence_ids=["ev"],
        supplier_name=name,
        issue_date=issued,
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


_BANNED = re.compile(
    r"reconcil|entit(?:y|ies)|exception|\bOCR\b|workflow|queue|\bAPI\b|JSON|payload|ledger|accrual"
    r"|\bnull\b|\bNone\b|Traceback|Error\b|!|\b(?:tx|doc|sup|acc|match|ev)_\w+|confidence",
    re.IGNORECASE,
)


def assert_plain(text: str) -> None:
    assert text and text == text.strip()
    assert not _BANNED.search(text), text


# --- 1 -> 1


def test_one_to_one_verified_match_with_explanation() -> None:
    payment = tx(
        "tx_1",
        "-83.21",
        25,
        "VODAFONE PT*1234 LISBOA",
        kind=TransactionKind.DIRECT_DEBIT,
    )
    invoice = doc(
        "doc_1", "83.21", 24, "Vodafone Portugal", supplier_tax_id="502544180"
    )
    result = reconcile([payment], [invoice], suppliers=RESOLVER)
    (match,) = result.matches
    assert match.kind is MatchKind.ONE_TO_ONE and match.quality is Quality.GREEN
    assert match.headline == "Matched to the invoice."
    assert match.why == (
        "Invoice total: €83.21",
        "Bank charge: €83.21",
        "Dates: 1 day apart",
        "Supplier VAT: matches",
    )
    assert [(a.transaction_id, a.document_id, a.amount) for a in match.allocations] == [
        ("tx_1", "doc_1", D("-83.21"))
    ]
    assert (
        result.unmatched_transaction_ids == () and result.unmatched_document_ids == ()
    )
    assert result.match_for_transaction("tx_1") is match
    assert result.matches_for_document("doc_1") == (match,)
    assert result.count(Quality.GREEN) == 1


def test_amber_document_keeps_the_match_amber() -> None:
    payment = tx("tx_1", "-83.21", 25, "VODAFONE PT*1234")
    invoice = doc(
        "doc_1",
        "83.21",
        24,
        "Vodafone",
        supplier_tax_id="502544180",
        quality=Quality.AMBER,
    )
    (match,) = reconcile([payment], [invoice], suppliers=RESOLVER).matches
    assert match.quality is Quality.AMBER
    assert (
        match.headline == "This payment probably matches the invoice. Please confirm."
    )


def test_bank_details_disagreement_is_red_and_explained() -> None:
    payment = tx(
        "tx_1", "-83.21", 25, "VODAFONE", counterparty_iban="PT50000201231234567890154"
    )
    invoice = doc(
        "doc_1",
        "83.21",
        24,
        "Vodafone",
        supplier_tax_id="502544180",
        iban="PT50999999999999999999999",
    )
    (match,) = reconcile([payment], [invoice], suppliers=RESOLVER).matches
    assert match.quality is Quality.RED
    assert (
        match.headline
        == "The bank details on the invoice don't match where the money went."
    )


def test_refund_is_matched_to_the_credit_note() -> None:
    refund = tx("tx_r", "20.00", 12, "ACME LDA")
    note = doc("doc_cn", "20.00", 10, doc_type=DocumentType.CREDIT_NOTE)
    invoice = doc("doc_inv", "20.00", 10)
    result = reconcile([refund], [note, invoice])
    (match,) = result.matches
    assert match.document_ids == ("doc_cn",) and MatchTag.REFUND in match.tags
    assert match.why[:2] == ("Credit note total: €20.00", "Money received: €20.00")
    assert result.unmatched_document_ids == ("doc_inv",)


def test_customer_payment_matches_our_sales_invoice() -> None:
    payment = tx(
        "tx_in",
        "1230.00",
        20,
        "CLIENTE XPTO",
        kind=TransactionKind.TRANSFER_IN,
        description="FT A/77",
    )
    sale = doc(
        "doc_sale",
        "1230.00",
        5,
        "Our Company Lda",
        supplier_tax_id="PT999999990",
        invoice_number="FT A/77",
    )
    (match,) = reconcile([payment], [sale], own_tax_ids=["999999990"]).matches
    assert match.quality is Quality.GREEN
    assert "Invoice number: found in the bank details" in match.why


# --- ambiguity


def test_two_equally_good_invoices_give_amber_with_both_listed() -> None:
    payment = tx("tx_1", "-39.00", 15, "PAYPAL *ADOBE")
    first = doc("doc_a", "39.00", 14, "Adobe")
    second = doc("doc_b", "39.00", 16, "Adobe")
    result = reconcile([payment], [first, second], suppliers=RESOLVER)
    (match,) = result.matches
    assert match.quality is Quality.AMBER and match.is_ambiguous
    listed = {
        match.document_ids[0],
        *(d for alt in match.alternatives for d in alt.document_ids),
    }
    assert listed == {"doc_a", "doc_b"}
    assert (
        match.headline == "This payment fits 2 invoices equally well. Which one is it?"
    )
    assert len(result.unmatched_document_ids) == 1


def test_symmetric_pairs_are_both_flagged_and_nothing_is_used_twice() -> None:
    payments = [
        tx("tx_1", "-39.00", 15, "PAYPAL *ADOBE"),
        tx("tx_2", "-39.00", 15, "PAYPAL *ADOBE"),
    ]
    invoices = [doc("doc_a", "39.00", 14, "Adobe"), doc("doc_b", "39.00", 14, "Adobe")]
    result = reconcile(payments, invoices, suppliers=RESOLVER)
    assert len(result.matches) == 2
    assert all(m.quality is Quality.AMBER and m.is_ambiguous for m in result.matches)
    used = [d for m in result.matches for d in m.document_ids]
    assert sorted(used) == ["doc_a", "doc_b"]


def test_duplicate_payment_for_one_invoice_is_flagged() -> None:
    payments = [
        tx("tx_1", "-39.00", 15, "PAYPAL *ADOBE"),
        tx("tx_2", "-39.00", 15, "PAYPAL *ADOBE"),
    ]
    result = reconcile(
        payments, [doc("doc_a", "39.00", 14, "Adobe")], suppliers=RESOLVER
    )
    (match,) = result.matches
    assert (
        match.is_ambiguous
        and match.headline == "More than one payment fits this invoice. Please confirm."
    )
    assert len(result.unmatched_transaction_ids) == 1


def test_closer_date_wins_without_ambiguity_for_monthly_bills() -> None:
    payments = [
        tx("tx_aug", "-39.00", date(2026, 8, 5), "PAYPAL *ADOBE"),
        tx("tx_sep", "-39.00", 5, "PAYPAL *ADOBE"),
    ]
    invoices = [
        doc("doc_aug", "39.00", date(2026, 8, 4), "Adobe"),
        doc("doc_sep", "39.00", 4, "Adobe"),
    ]
    result = reconcile(payments, invoices, suppliers=RESOLVER)
    pairs = {m.transaction_ids[0]: m.document_ids[0] for m in result.matches}
    assert pairs == {"tx_aug": "doc_aug", "tx_sep": "doc_sep"}
    assert not any(m.is_ambiguous for m in result.matches)


# --- 1 -> many


def test_one_payment_for_invoices_minus_a_credit_note() -> None:
    payment = tx("tx_1", "-80.00", 12, "ACME LDA")
    invoice = doc("doc_inv", "100.00", 5)
    note = doc("doc_cn", "20.00", 6, doc_type=DocumentType.CREDIT_NOTE)
    other = doc("doc_other", "55.00", 7)
    result = reconcile([payment], [invoice, note, other])
    (match,) = result.matches
    assert (
        match.kind is MatchKind.ONE_TO_MANY
        and MatchTag.CREDIT_NOTE_OFFSET in match.tags
    )
    assert {a.document_id: a.amount for a in match.allocations} == {
        "doc_inv": D("-100.00"),
        "doc_cn": D("20.00"),
    }
    assert match.why[0] == "Documents total: €80.00 (2 documents)"
    assert result.unmatched_document_ids == ("doc_other",)


def test_one_payment_quoting_several_invoice_numbers() -> None:
    payment = tx("tx_1", "-250.00", 12, "TRF", description="FT 2026/101 FT 2026/102")
    a = doc("doc_a", "100.00", 1, "Some Supplier", invoice_number="FT 2026/101")
    b = doc("doc_b", "150.00", 2, "Some Supplier", invoice_number="FT 2026/102")
    (match,) = reconcile([payment], [a, b]).matches
    assert match.kind is MatchKind.ONE_TO_MANY and match.quality is Quality.GREEN
    assert match.headline == "Matched to 2 invoices."


def test_several_equal_combinations_are_ambiguous() -> None:
    payment = tx("tx_1", "-100.00", 12)
    docs = [doc(f"doc_{i}", "50.00", i) for i in range(1, 4)]
    (match,) = reconcile([payment], docs).matches
    assert match.kind is MatchKind.ONE_TO_MANY and match.quality is Quality.AMBER
    assert (
        match.alternatives
        and match.headline == "More than one combination fits. Please confirm."
    )


def test_other_suppliers_are_never_combined() -> None:
    payment = tx("tx_1", "-100.00", 12, "ACME LDA")
    docs = [
        doc("doc_a", "60.00", 5),
        doc("doc_b", "40.00", 5, "Totally Different Company"),
    ]
    result = reconcile([payment], docs)
    assert result.matches == ()


def test_exact_combination_beats_a_near_single_invoice() -> None:
    payment = tx("tx_1", "-100.50", 12)
    docs = [
        doc("doc_near", "100.00", 10),
        doc("doc_a", "60.00", 5),
        doc("doc_b", "40.50", 6),
    ]
    (match,) = reconcile([payment], docs).matches
    assert set(match.document_ids) == {"doc_a", "doc_b"}


def test_capped_search_is_logged_and_never_verified(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Only doc_00 + doc_01 add up to the payment; no single invoice does.
    payment = tx("tx_1", "-21.03", 25)
    docs = [
        doc(f"doc_{i:02d}", str(D(10 + i) + D("0.01") * (i + 1)), 20) for i in range(12)
    ]
    config = ReconciliationConfig(max_search_nodes=10)
    with caplog.at_level(logging.WARNING):
        result = reconcile([payment], docs, config)
    (match,) = result.matches
    assert set(match.document_ids) == {"doc_00", "doc_01"}
    assert MatchTag.SEARCH_CAPPED in match.tags and match.quality is Quality.AMBER
    assert any("stopped after" in note for note in result.notes)
    assert any("stopped after" in r.getMessage() for r in caplog.records)


def test_pool_cap_is_reported() -> None:
    payment = tx("tx_1", "-21.00", 25)
    docs = [doc(f"doc_{i:02d}", str(10 + i), 20 - i) for i in range(6)]
    result = reconcile([payment], docs, ReconciliationConfig(max_subset_pool=3))
    assert any("cut to 3" in note for note in result.notes)


# --- many -> 1


def test_instalments_with_a_deposit() -> None:
    invoice = doc("doc_1", "900.00", 10, invoice_number="FT 2026/55")
    payments = [
        tx(
            "tx_dep",
            "-300.00",
            date(2026, 8, 20),
            description="ADIANTAMENTO FT 2026/55",
        ),
        tx("tx_2", "-300.00", 15, description="FT 2026/55"),
        tx("tx_3", "-300.00", 30, description="FT 2026/55"),
    ]
    result = reconcile(payments, [invoice])
    (match,) = result.matches
    assert match.kind is MatchKind.MANY_TO_ONE
    assert {MatchTag.INSTALMENTS, MatchTag.DEPOSIT} <= match.tags
    assert (
        match.quality is Quality.GREEN
        and match.headline == "3 payments cover this invoice."
    )
    assert sum(a.amount for a in match.allocations) == D("-900.00")
    assert result.open_balances == {}


def test_partial_payment_keeps_the_rest_open() -> None:
    invoice = doc("doc_1", "1000.00", 10, invoice_number="FT 2026/183")
    payment = tx("tx_1", "-300.00", 15, description="PAG PARCIAL FT 2026/183")
    result = reconcile([payment], [invoice])
    (match,) = result.matches
    assert match.kind is MatchKind.PARTIAL and match.quality is Quality.AMBER
    assert match.headline == "Part payment. €700.00 is still open."
    assert dict(result.open_balances) == {"doc_1": D("-700.00")}
    assert result.unmatched_document_ids == ()


def test_balance_from_an_earlier_run_is_completed() -> None:
    invoice = doc(
        "doc_1",
        "1000.00",
        10,
        invoice_number="FT 2026/183",
        supplier_tax_id="PT502544180",
        name="Vodafone",
    )
    payment = tx("tx_1", "-700.00", 20, "VODAFONE", description="FT 2026/183")
    result = reconcile(
        [payment],
        [invoice],
        suppliers=RESOLVER,
        document_balances={"doc_1": D("-700.00")},
    )
    (match,) = result.matches
    assert match.kind is MatchKind.ONE_TO_ONE and match.quality is Quality.GREEN
    assert "Still to pay: €700.00" in match.why
    assert result.open_balances == {}


def test_untouched_partly_paid_document_is_reported_both_ways() -> None:
    invoice = doc("doc_1", "1000.00", 10)
    result = reconcile([], [invoice], document_balances={"doc_1": D("-400.00")})
    assert result.unmatched_document_ids == ("doc_1",)
    assert dict(result.open_balances) == {"doc_1": D("-400.00")}


@pytest.mark.parametrize("balance", ["400.00", "-1200.00"])
def test_impossible_balances_are_rejected(balance: str) -> None:
    with pytest.raises(ValueError):
        reconcile(
            [], [doc("doc_1", "1000.00", 10)], document_balances={"doc_1": D(balance)}
        )


# --- fees and FX


def test_undeclared_fee_is_near_and_explained() -> None:
    payment = tx("tx_1", "-1001.50", 15)
    (match,) = reconcile([payment], [doc("doc_1", "1000.00", 14)]).matches
    assert match.quality is Quality.AMBER and MatchTag.NEAR_AMOUNT in match.tags
    assert match.difference == D("-1.50")
    assert match.headline == "This payment is €1.50 more than the invoice."


def test_declared_fee_is_exact_and_reported() -> None:
    payment = tx("tx_1", "-1001.50", 15, "VODAFONE")
    invoice = doc("doc_1", "1000.00", 14, "Vodafone", supplier_tax_id="502544180")
    meta = {"tx_1": BankMetadata(fee=D("1.50"))}
    (match,) = reconcile(
        [payment], [invoice], suppliers=RESOLVER, bank_metadata=meta
    ).matches
    assert match.quality is Quality.GREEN and MatchTag.BANK_FEE in match.tags
    assert match.fee == D("1.50") and "Bank fees: €1.50" in match.why


def test_declared_fx_match_reports_rate_and_fee() -> None:
    payment = tx("tx_1", "-92.45", 25, "PAYPAL *ADOBE", kind=TransactionKind.CARD)
    invoice = doc("doc_1", "100.00", 25, "Adobe", currency="USD")
    meta = {
        "tx_1": BankMetadata(
            fx=FxDetails(D("100.00"), "USD", rate=D("0.9150"), fee=D("0.95"))
        )
    }
    (match,) = reconcile(
        [payment], [invoice], suppliers=RESOLVER, bank_metadata=meta
    ).matches
    assert match.quality is Quality.GREEN and MatchTag.FX in match.tags
    assert match.currency == "USD" and match.bank_currency == "EUR"
    assert match.fee == D("0.95") and match.fx_rate == D("0.9150")
    assert match.allocations[0].amount == D("-100.00")


def test_reference_rate_fx_match_is_plausible_only() -> None:
    payment = tx("tx_1", "-93.00", 25, "PAYPAL *ADOBE")
    invoice = doc("doc_1", "100.00", 25, "Adobe", currency="USD")
    rates = StaticFxRates({("USD", "EUR"): D("0.92")})
    (match,) = reconcile(
        [payment], [invoice], suppliers=RESOLVER, fx_rates=rates
    ).matches
    assert (
        match.quality is Quality.AMBER
        and {MatchTag.FX, MatchTag.NEAR_AMOUNT} <= match.tags
    )
    assert match.conversion_cost == D("1.00")
    assert (
        match.headline
        == "This payment includes about €1.00 in currency conversion costs."
    )


def test_instalments_paid_in_another_currency_use_declared_amounts() -> None:
    invoice = doc(
        "doc_1", "200.00", 10, "Adobe", currency="USD", invoice_number="INV-7781"
    )
    payments = [
        tx("tx_1", "-92.00", 12, "PAYPAL *ADOBE", description="INV-7781"),
        tx("tx_2", "-93.00", 20, "PAYPAL *ADOBE", description="INV-7781"),
    ]
    meta = {
        "tx_1": BankMetadata(fx=FxDetails(D("100.00"), "USD")),
        "tx_2": BankMetadata(fx=FxDetails(D("100.00"), "USD")),
    }
    (match,) = reconcile(
        payments, [invoice], suppliers=RESOLVER, bank_metadata=meta
    ).matches
    assert match.kind is MatchKind.MANY_TO_ONE
    assert [a.amount for a in match.allocations] == [D("-100.00"), D("-100.00")]


# --- card repayments


def card_purchase(id: str, amount: str, day: int | date) -> Transaction:
    return tx(
        id,
        amount,
        day,
        "SHOP",
        account_id="acc_card",
        kind=TransactionKind.CARD,
        card_last4="4817",
    )


def test_card_repayment_matches_statement_and_settled_purchases() -> None:
    purchases = [
        card_purchase("tx_p1", "-100.00", date(2026, 8, 5)),
        card_purchase("tx_p2", "-50.25", date(2026, 8, 20)),
    ]
    later = card_purchase("tx_p3", "-30.00", 2)
    repayment = tx("tx_settle", "-150.25", 1, "LIQUIDACAO CARTAO CREDITO")
    statement = doc(
        "doc_stmt",
        "150.25",
        date(2026, 8, 28),
        "Banco Exemplo",
        doc_type=DocumentType.STATEMENT,
        fields=card_field("4817"),
    )
    meta = {"tx_settle": BankMetadata(settles_card_last4="4817")}
    result = reconcile([*purchases, later, repayment], [statement], bank_metadata=meta)
    (match,) = result.matches
    assert match.kind is MatchKind.CARD_SETTLEMENT and match.quality is Quality.GREEN
    assert match.settled_transaction_ids == ("tx_p1", "tx_p2")
    assert (
        "Card ending: matches" in match.why
        and "Card purchases: 2 purchases add up exactly" in match.why
    )
    assert not any(line.startswith("Supplier") for line in match.why)
    # Purchases still need their own invoices.
    assert set(result.unmatched_transaction_ids) == {"tx_p1", "tx_p2", "tx_p3"}


def test_card_repayment_without_statement_is_amber() -> None:
    purchases = [
        card_purchase("tx_p1", "-100.00", date(2026, 8, 5)),
        card_purchase("tx_p2", "-50.25", date(2026, 8, 20)),
    ]
    repayment = tx("tx_settle", "-150.25", 1, "LIQUIDACAO CARTAO CREDITO")
    result = reconcile([*purchases, repayment], [])
    (match,) = result.matches
    assert match.quality is Quality.AMBER and MatchTag.STATEMENT_MISSING in match.tags
    assert match.headline == "Card repayment. I still need the card statement."
    assert (
        match.settled_transaction_ids == ("tx_p1", "tx_p2") and match.document_ids == ()
    )


def test_declared_settlement_period_is_used_directly() -> None:
    purchases = [
        card_purchase("tx_p1", "-100.00", date(2026, 8, 5)),
        card_purchase("tx_p2", "-50.25", date(2026, 8, 20)),
    ]
    repayment = tx("tx_settle", "-100.00", 1, "CARD")
    meta = {
        "tx_settle": BankMetadata(
            settles_card_last4="4817",
            settlement_period=(date(2026, 8, 1), date(2026, 8, 10)),
        )
    }
    (match,) = reconcile([*purchases, repayment], [], bank_metadata=meta).matches
    assert match.settled_transaction_ids == ("tx_p1",)


def test_unexplained_card_repayment_stays_open() -> None:
    repayment = tx("tx_settle", "-999.00", 1, "LIQUIDACAO CARTAO CREDITO")
    invoice = doc("doc_1", "999.00", 1)
    result = reconcile([repayment], [invoice])
    assert result.matches == () and result.unmatched_transaction_ids == ("tx_settle",)


# --- expectations and skips


def test_transactions_needing_no_document_are_not_matched() -> None:
    company = LegalEntity(
        id="ent_1",
        tenant_id=T,
        name="Hazel Tree Lda",
        country="PT",
        tax_id="PT999999990",
        own_ibans=["PT50000000000000000000001"],
    )
    transfer = tx(
        "tx_own",
        "-500.00",
        10,
        "HAZEL TREE",
        counterparty_iban="PT50000000000000000000001",
    )
    decisions = ExpectedEvidenceEngine(entities=[company]).classify_all([transfer])
    result = reconcile([transfer], [doc("doc_1", "500.00", 10)], expectations=decisions)
    assert result.matches == ()
    assert result.no_document_needed == {
        "tx_own": "Money moved between your own accounts. No document needed."
    }
    assert result.unmatched_transaction_ids == ()


def test_zero_amounts_missing_totals_and_paid_documents_are_skipped() -> None:
    zero = tx("tx_zero", "0.00", 5)
    no_total = Document(id="doc_nt", tenant_id=T, evidence_ids=["e"], supplier_name="X")
    paid = doc("doc_paid", "10.00", 5)
    result = reconcile([zero], [no_total, paid], document_balances={"doc_paid": D("0")})
    assert dict(result.skipped) == {
        "tx_zero": "zero amount",
        "doc_nt": "no total",
        "doc_paid": "already paid",
    }


def test_duplicate_ids_are_rejected() -> None:
    with pytest.raises(ValueError):
        reconcile([tx("tx_1", "-1", 1), tx("tx_1", "-2", 2)], [])
    with pytest.raises(ValueError):
        reconcile([], [doc("doc_1", "1", 1), doc("doc_1", "2", 2)])


def test_config_bounds_are_validated() -> None:
    with pytest.raises(ValueError):
        ReconciliationConfig(max_subset_size=1)
    with pytest.raises(ValueError):
        ReconciliationConfig(max_search_nodes=0)


# --- invariants


def _dataset(seed: int) -> tuple[list[Transaction], list[Document]]:
    rng = random.Random(seed)
    names = [
        "ACME LDA",
        "PAYPAL *ADOBE",
        "VODAFONE PT*1",
        "PADARIA CENTRAL",
        "OFICINA SILVA",
    ]
    doc_names = [
        "ACME, Lda",
        "Adobe",
        "Vodafone Portugal",
        "Padaria Central",
        "Oficina Silva",
    ]
    txs, docs = [], []
    for i in range(40):
        k = rng.randrange(len(names))
        amount = D(
            rng.choice(["10.00", "20.00", "30.00", "39.00", "50.00", "83.21", "100.00"])
        )
        day = rng.randrange(1, 28)
        docs.append(
            doc(
                f"doc_{i:03d}",
                str(amount),
                day,
                doc_names[k],
                doc_type=rng.choice(
                    [DocumentType.INVOICE] * 5 + [DocumentType.CREDIT_NOTE]
                ),
                invoice_number=f"FT 2026/{i + 100}" if rng.random() < 0.3 else None,
            )
        )
        if rng.random() < 0.8:
            sign = "-" if docs[-1].doc_type is DocumentType.INVOICE else ""
            txs.append(
                tx(
                    f"tx_{i:03d}",
                    f"{sign}{amount}",
                    min(28, day + rng.randrange(0, 4)),
                    names[k],
                )
            )
    return txs, docs


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_nothing_is_used_beyond_its_amount(seed: int) -> None:
    txs, docs = _dataset(seed)
    result = reconcile(txs, docs, suppliers=RESOLVER)
    seen_tx = [t for m in result.matches for t in m.transaction_ids]
    assert len(seen_tx) == len(set(seen_tx))
    assert set(seen_tx).isdisjoint(result.unmatched_transaction_ids)
    assert set(seen_tx) | set(result.unmatched_transaction_ids) == {t.id for t in txs}
    allocated: dict[str, Decimal] = defaultdict(Decimal)
    for m in result.matches:
        for a in m.allocations:
            allocated[a.document_id] += a.amount
    flows = {d.id: document_flow(d) for d in docs}
    for doc_id, total in allocated.items():
        assert abs(total) <= abs(flows[doc_id]) and (total > 0) == (flows[doc_id] > 0)


@pytest.mark.parametrize("seed", [4, 5])
def test_result_does_not_depend_on_input_order(seed: int) -> None:
    txs, docs = _dataset(seed)
    baseline = reconcile(txs, docs, suppliers=RESOLVER)
    rng = random.Random(seed)
    for _ in range(3):
        shuffled_txs, shuffled_docs = txs[:], docs[:]
        rng.shuffle(shuffled_txs)
        rng.shuffle(shuffled_docs)
        again = reconcile(
            shuffled_txs, shuffled_docs, suppliers=SupplierResolver([ADOBE, VODAFONE])
        )
        assert again == baseline


def test_owner_facing_text_is_plain_language() -> None:
    txs, docs = _dataset(7)
    extra_txs = [
        tx("tx_x1", "-1001.50", 15),
        tx("tx_x2", "-300.00", 15, description="FT 2026/999"),
        tx(
            "tx_x3",
            "-83.21",
            25,
            "VODAFONE",
            counterparty_iban="PT50000201231234567890154",
        ),
    ]
    extra_docs = [
        doc("doc_x1", "1000.00", 14),
        doc("doc_x2", "1000.00", 10, "Zeta", invoice_number="FT 2026/999"),
        doc("doc_x3", "83.21", 24, "Vodafone", iban="PT50999999999999999999999"),
    ]
    result = reconcile([*txs, *extra_txs], [*docs, *extra_docs], suppliers=RESOLVER)
    qualities = {m.quality for m in result.matches}
    assert qualities == {Quality.GREEN, Quality.AMBER, Quality.RED}
    for match in result.matches:
        assert_plain(match.headline)
        for line in match.why:
            assert_plain(line)


# --- headline regressions


def test_invoice_and_receipt_for_the_same_payment_are_called_documents() -> None:
    payment = tx("tx_1", "-14.20", 10, "PAYPAL *ADOBE")
    invoice = doc("doc_inv", "14.20", 10, "Adobe")
    receipt = doc("doc_rcpt", "14.20", 10, "Adobe", doc_type=DocumentType.RECEIPT)
    (match,) = reconcile([payment], [invoice, receipt], suppliers=RESOLVER).matches
    assert (
        match.headline == "This payment fits 2 documents equally well. Which one is it?"
    )


def test_two_fitting_card_statements_are_flagged() -> None:
    repayment = tx(
        "tx_settle",
        "-150.25",
        1,
        "LIQUIDACAO CARTAO CREDITO",
        kind=TransactionKind.TRANSFER_OUT,
    )
    first = doc(
        "doc_s1",
        "150.25",
        date(2026, 8, 28),
        "Banco Exemplo",
        doc_type=DocumentType.STATEMENT,
    )
    second = doc(
        "doc_s2",
        "150.25",
        date(2026, 8, 28),
        "Banco Exemplo",
        doc_type=DocumentType.STATEMENT,
    )
    (match,) = reconcile([repayment], [first, second]).matches
    assert match.quality is Quality.AMBER
    assert (
        match.headline
        == "Card repayment. More than one card statement fits. Please confirm."
    )


def test_card_purchases_adding_up_over_two_periods_are_ambiguous() -> None:
    purchases = [
        card_purchase("tx_p1", "-50.00", date(2026, 8, 5)),
        card_purchase("tx_p2", "-50.00", date(2026, 8, 20)),
    ]
    repayment = tx("tx_settle", "-50.00", 1, "LIQUIDACAO CARTAO CREDITO")
    (match,) = reconcile([*purchases, repayment], []).matches
    assert (
        match.is_ambiguous
        and "Card purchases: more than one period adds up" in match.why
    )


def test_card_statement_expectation_routes_to_card_repayment_matching() -> None:
    purchases = [card_purchase("tx_p1", "-100.00", date(2026, 8, 5))]
    repayment = tx(
        "tx_settle",
        "-100.00",
        1,
        "BANCO EXEMPLO DEBITO",
        kind=TransactionKind.DIRECT_DEBIT,
    )
    decisions = ExpectedEvidenceEngine(
        bank_metadata={"tx_settle": BankMetadata(settles_card_last4="4817")}
    ).classify_all([repayment])
    (match,) = reconcile([*purchases, repayment], [], expectations=decisions).matches
    assert (
        match.kind is MatchKind.CARD_SETTLEMENT
        and match.settled_transaction_ids == ("tx_p1",)
    )


# --- more branches


def test_document_in_conflict_gives_a_red_match_with_a_plain_headline() -> None:
    payment = tx("tx_1", "-83.21", 25, "VODAFONE")
    invoice = doc(
        "doc_1",
        "83.21",
        24,
        "Vodafone",
        supplier_tax_id="502544180",
        quality=Quality.RED,
    )
    (match,) = reconcile([payment], [invoice], suppliers=RESOLVER).matches
    assert match.quality is Quality.RED
    assert (
        match.headline == "The invoice has details that don't agree. Please check it."
    )


def test_instalments_without_references_stay_amber() -> None:
    invoice = doc("doc_1", "900.00", 10)
    payments = [tx("tx_1", "-450.00", 12), tx("tx_2", "-450.00", 40)]
    (match,) = reconcile(payments, [invoice]).matches
    assert match.kind is MatchKind.MANY_TO_ONE and match.quality is Quality.AMBER
    assert match.headline == "2 payments probably cover this invoice. Please confirm."


def test_many_equal_combinations_stop_early_and_say_so() -> None:
    payment = tx("tx_1", "-20.00", 12)
    docs = [doc(f"doc_{i}", "10.00", 5) for i in range(8)]
    result = reconcile([payment], docs, ReconciliationConfig(max_subset_solutions=3))
    (match,) = result.matches
    assert match.is_ambiguous and MatchTag.SEARCH_CAPPED in match.tags
    assert any("stopped after 3 combinations" in note for note in result.notes)


def test_short_structured_reference_links_a_partial_payment() -> None:
    invoice = doc("doc_1", "1000.00", 10, "Utility", payment_reference="12345")
    payment = tx("tx_1", "-250.00", 15, "PAG SERV", reference="12345")
    (match,) = reconcile([payment], [invoice]).matches
    assert match.kind is MatchKind.PARTIAL and "Payment reference: matches" in match.why


def test_declared_settlement_period_that_does_not_add_up_links_nothing() -> None:
    purchases = [card_purchase("tx_p1", "-100.00", date(2026, 8, 5))]
    repayment = tx("tx_settle", "-120.00", 1, "CARD")
    meta = {
        "tx_settle": BankMetadata(
            settles_card_last4="4817",
            settlement_period=(date(2026, 8, 1), date(2026, 8, 31)),
        )
    }
    result = reconcile([*purchases, repayment], [], bank_metadata=meta)
    assert result.matches == () and "tx_settle" in result.unmatched_transaction_ids


def test_statement_in_conflict_makes_the_card_repayment_red() -> None:
    repayment = tx(
        "tx_settle",
        "-150.25",
        1,
        "LIQUIDACAO CARTAO CREDITO",
        kind=TransactionKind.TRANSFER_OUT,
    )
    statement = doc(
        "doc_s",
        "150.25",
        date(2026, 8, 28),
        "Banco",
        doc_type=DocumentType.STATEMENT,
        quality=Quality.RED,
    )
    (match,) = reconcile([repayment], [statement]).matches
    assert match.quality is Quality.RED
