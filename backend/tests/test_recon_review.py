"""Regression tests from the adversarial review of the reconciliation module.

Each test pins one defect that was found and fixed (§3, §19, §20, §21, §54, §57).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import httpx
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
    AmountStatus,
    BankMetadata,
    EcbConfig,
    EcbFxRates,
    EvidenceExpectation,
    ExpectedEvidenceEngine,
    FxDetails,
    MatchContext,
    MatchKind,
    MatchTag,
    StaticFxRates,
    SupplierResolver,
    document_flow,
    fx_from_text,
    normalize_descriptor,
    reconcile,
    score_match,
)
from backoffice.reconciliation.scoring import check_amount

T = "tenant"
D = Decimal
OWN_TAX_ID = "PT509999990"
VODAFONE = Supplier(
    id="sup_voda",
    tenant_id=T,
    name="Vodafone Portugal, S.A.",
    aliases=["Vodafone"],
    tax_id="PT502544180",
)
RESOLVER = SupplierResolver([VODAFONE])
USD_EUR = StaticFxRates({("USD", "EUR"): D("0.92")})


def tx(
    id: str, amount: str, day: date, counterparty: str = "VODAFONE", **kw
) -> Transaction:
    kw.setdefault("account_id", "acc_bank")
    return Transaction(
        id=id,
        tenant_id=T,
        booked_on=day,
        amount=D(amount),
        counterparty=counterparty,
        **kw,
    )


def doc(id: str, gross: str, day: date, name: str = "Vodafone", **kw) -> Document:
    kw.setdefault("quality", Quality.GREEN)
    kw.setdefault("doc_type", DocumentType.INVOICE)
    return Document(
        id=id,
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


def card_purchase(id: str, amount: str, day: date) -> Transaction:
    return tx(
        id, amount, day, "SHOP", account_id="acc_card",
        kind=TransactionKind.CARD, card_last4="4817",
    )  # fmt: skip


SEPT_12 = date(2026, 9, 12)
SEPT_10 = date(2026, 9, 10)


# --- credit notes stored with a negative total


def test_credit_note_with_negative_total_is_still_money_back() -> None:
    note = doc("cn", "-20.00", SEPT_10, doc_type=DocumentType.CREDIT_NOTE)
    assert document_flow(note) == D("20.00")
    assert document_flow(
        doc("cn2", "20.00", SEPT_10, doc_type=DocumentType.CREDIT_NOTE)
    ) == D("20.00")


def test_a_payment_out_never_matches_a_credit_note() -> None:
    note = doc(
        "cn",
        "-20.00",
        SEPT_10,
        doc_type=DocumentType.CREDIT_NOTE,
        supplier_tax_id="502544180",
    )
    debit = tx("t_out", "-20.00", SEPT_12)
    assert reconcile([debit], [note], suppliers=RESOLVER).matches == ()
    refund = tx("t_in", "20.00", SEPT_12)
    (match,) = reconcile([refund], [note], suppliers=RESOLVER).matches
    assert MatchTag.REFUND in match.tags and match.quality is Quality.GREEN


# --- FX: nothing is verified that was not checked


def test_text_fx_without_a_rate_never_verifies_the_booked_amount() -> None:
    absurd = tx("t1", "-5000.00", SEPT_12, description="COMPRA VODAFONE USD 100,00")
    invoice = doc("d1", "100.00", SEPT_12, currency="USD", supplier_tax_id="502544180")
    result = reconcile([absurd], [invoice], suppliers=RESOLVER)
    assert all(m.quality is not Quality.GREEN for m in result.matches)
    # With a reference rate the €5,000 debit is recognisably not a $100 purchase.
    checked = reconcile([absurd], [invoice], suppliers=RESOLVER, fx_rates=USD_EUR)
    assert checked.matches == () and checked.unmatched_transaction_ids == ("t1",)


def test_text_fx_without_a_rate_is_plausible_only() -> None:
    fair = tx("t1", "-92.40", SEPT_12, description="COMPRA VODAFONE USD 100,00")
    invoice = doc("d1", "100.00", SEPT_12, currency="USD", supplier_tax_id="502544180")
    (match,) = reconcile([fair], [invoice], suppliers=RESOLVER).matches
    assert match.quality is Quality.AMBER and MatchTag.FX in match.tags
    assert (
        match.headline
        == "The amount matches the invoice before currency conversion. Please confirm."
    )


def test_declared_amount_with_an_implausible_conversion_is_not_verified() -> None:
    absurd = tx("t1", "-5000.00", SEPT_12)
    invoice = doc("d1", "100.00", SEPT_12, currency="USD", supplier_tax_id="502544180")
    meta = {"t1": BankMetadata(fx=FxDetails(D("100.00"), "USD"))}
    (match,) = reconcile(
        [absurd], [invoice], suppliers=RESOLVER, bank_metadata=meta, fx_rates=USD_EUR
    ).matches
    assert match.quality is Quality.AMBER and MatchTag.NEAR_AMOUNT in match.tags
    assert match.conversion_cost == D("4908.00")
    # Measured against a reference rate: an estimate, and said so.
    assert "Conversion cost: about €4,908.00" in match.why
    assert (
        match.headline
        == "This payment includes about €4,908.00 in currency conversion costs."
    )
    # A normal spread on a declared amount stays exact.
    normal = tx("t2", "-92.50", SEPT_12)
    meta2 = {"t2": BankMetadata(fx=FxDetails(D("100.00"), "USD"))}
    (ok,) = reconcile(
        [normal], [invoice], suppliers=RESOLVER, bank_metadata=meta2, fx_rates=USD_EUR
    ).matches
    assert ok.quality is Quality.GREEN


def test_text_rate_quoted_the_other_way_round_still_reconciles() -> None:
    payment = tx("t1", "-92.15", SEPT_12, description="VODAFONE USD 100,00 RATE 1,0852")
    invoice = doc("d1", "100.00", SEPT_12, currency="USD", supplier_tax_id="502544180")
    score = score_match([payment], [invoice], MatchContext(suppliers=RESOLVER))
    assert score.amount.status is AmountStatus.ADJUSTED
    (match,) = reconcile([payment], [invoice], suppliers=RESOLVER).matches
    assert match.quality is Quality.GREEN


def test_fx_from_text_is_marked_as_read_from_text() -> None:
    parsed = fx_from_text("USD 100,00", "EUR")
    assert parsed is not None and parsed.from_text
    assert not FxDetails(D("1"), "USD").from_text


def test_zero_document_total_never_divides_by_zero() -> None:
    ctx = MatchContext(fx_rates=USD_EUR)
    check = check_amount([tx("t1", "-10.00", SEPT_12)], D("0"), "USD", ctx)
    assert check.status is AmountStatus.MISMATCH


# --- currency codes


def test_currency_codes_are_compared_case_insensitively() -> None:
    payment = tx("t1", "-83.21", SEPT_12, currency="eur")
    invoice = doc("d1", "83.21", SEPT_10, currency=" EUR ", supplier_tax_id="502544180")
    (match,) = reconcile([payment], [invoice], suppliers=RESOLVER).matches
    assert match.quality is Quality.GREEN and match.currency == "EUR"


# --- own sales documents


def test_our_own_iban_on_a_sales_invoice_is_not_a_conflict() -> None:
    sale = doc(
        "s1", "500.00", date(2026, 9, 5), "Hazel Tree Lda",
        supplier_tax_id=OWN_TAX_ID, invoice_number="FT 2026/77",
        iban="PT50000700000000000000001",
    )  # fmt: skip
    payment = tx(
        "t1", "500.00", date(2026, 9, 8), "CLIENTE XPTO",
        counterparty_iban="PT50003300000000000000002", description="FT 2026/77",
    )  # fmt: skip
    (match,) = reconcile([payment], [sale], own_tax_ids=[OWN_TAX_ID]).matches
    assert match.quality is Quality.GREEN
    assert not any("Bank details" in line for line in match.why)


def test_payroll_issued_by_the_business_is_money_out() -> None:
    payslips = doc(
        "ps", "1200.00", date(2026, 9, 28), "Hazel Tree Lda",
        doc_type=DocumentType.PAYROLL, supplier_tax_id=OWN_TAX_ID,
    )  # fmt: skip
    assert document_flow(payslips, [OWN_TAX_ID]) == D("-1200.00")
    salary = tx(
        "t1", "-1200.00", date(2026, 9, 28), "JOAO SILVA",
        kind=TransactionKind.TRANSFER_OUT, description="SALARIO SETEMBRO",
    )  # fmt: skip
    (match,) = reconcile([salary], [payslips], own_tax_ids=[OWN_TAX_ID]).matches
    assert match.document_ids == ("ps",)


# --- card repayments


def test_a_card_purchase_is_never_settled_twice() -> None:
    purchases = [
        card_purchase("p_jul", "-60.00", date(2026, 7, 10)),
        card_purchase("p_aug", "-60.00", date(2026, 8, 10)),
    ]
    repayments = [
        tx("r_aug", "-60.00", date(2026, 8, 1), "LIQUIDACAO CARTAO CREDITO"),
        tx("r_sep", "-60.00", date(2026, 9, 1), "LIQUIDACAO CARTAO CREDITO"),
    ]
    result = reconcile([*purchases, *repayments], [])
    settled = {m.transaction_ids[0]: m.settled_transaction_ids for m in result.matches}
    assert settled == {"r_aug": ("p_jul",), "r_sep": ("p_aug",)}
    assert all(not m.is_ambiguous for m in result.matches)


def test_one_purchase_cannot_back_two_equal_repayments() -> None:
    purchases = [card_purchase("p1", "-100.00", date(2026, 8, 5))]
    repayments = [
        tx("r1", "-100.00", date(2026, 9, 1), "LIQUIDACAO CARTAO CREDITO"),
        tx("r2", "-100.00", date(2026, 9, 3), "LIQUIDACAO CARTAO CREDITO"),
    ]
    result = reconcile([*purchases, *repayments], [])
    linked = [m.settled_transaction_ids for m in result.matches]
    assert linked == [("p1",)]
    assert "r2" in result.unmatched_transaction_ids


def test_card_repayment_needs_the_high_threshold_to_be_verified() -> None:
    purchases = [
        card_purchase("p1", "-100.00", date(2026, 8, 5)),
        card_purchase("p2", "-50.25", date(2026, 8, 20)),
    ]
    repayment = tx("rs", "-150.25", date(2026, 9, 20), "LIQUIDACAO CARTAO CREDITO")
    statement = doc(
        "st", "150.25", date(2026, 8, 28), "Banco", doc_type=DocumentType.STATEMENT
    )
    meta = {"rs": BankMetadata(settles_card_last4="4817")}
    (weak,) = reconcile(
        [*purchases, repayment], [statement], bank_metadata=meta
    ).matches
    assert weak.points < 70 and weak.quality is Quality.AMBER
    proven = doc(
        "st", "150.25", date(2026, 8, 28), "Banco",
        doc_type=DocumentType.STATEMENT, fields=card_field("4817"),
    )  # fmt: skip
    (strong,) = reconcile([*purchases, repayment], [proven], bank_metadata=meta).matches
    assert strong.kind is MatchKind.CARD_SETTLEMENT
    assert strong.points >= 70 and strong.quality is Quality.GREEN


# --- expectations


COMPANY = LegalEntity(
    id="ent_1", tenant_id=T, name="Hazel Tree Lda", country="PT", tax_id=OWN_TAX_ID
)


def test_a_likely_no_document_guess_still_takes_a_fitting_document() -> None:
    fee_like = tx("t1", "-12.30", SEPT_12, "COMISSAO ACME")
    decisions = ExpectedEvidenceEngine(entities=[COMPANY]).classify_all([fee_like])
    assert decisions["t1"].quality is Quality.AMBER
    assert not decisions["t1"].requires_document
    invoice = doc("d1", "12.30", SEPT_10, "ACME, Lda", invoice_number="FT 1/2026")
    result = reconcile([fee_like], [invoice], expectations=decisions)
    assert [m.document_ids for m in result.matches] == [("d1",)]
    assert result.no_document_needed == {} and result.likely_no_document == ()


def test_an_unmatched_likely_guess_is_not_chased_but_stays_unconfirmed() -> None:
    fee_like = tx("t1", "-5.20", SEPT_12, "COMISSAO MANUTENCAO CONTA")
    certain = tx("t2", "-3.00", SEPT_12, "", kind=TransactionKind.FEE)
    decisions = ExpectedEvidenceEngine().classify_all([fee_like, certain])
    result = reconcile([fee_like, certain], [], expectations=decisions)
    assert result.unmatched_transaction_ids == ()
    assert set(result.no_document_needed) == {"t1", "t2"}
    assert result.likely_no_document == ("t1",)


@pytest.mark.parametrize(
    ("counterparty", "description"),
    [
        ("PREST EMPRESTIMO 0123", "CAPITAL E JUROS"),
        ("LOAN REPAYMENT", "INTEREST AND PRINCIPAL"),
    ],
)
def test_loan_repayments_with_interest_wording_expect_the_loan_statement(
    counterparty: str, description: str
) -> None:
    payment = tx(
        "t1", "-640.00", SEPT_12, counterparty,
        description=description, kind=TransactionKind.DIRECT_DEBIT,
    )  # fmt: skip
    decision = ExpectedEvidenceEngine().classify(payment)
    assert decision.expectation is EvidenceExpectation.LOAN_STATEMENT
    assert decision.requires_document


def test_overdraft_interest_is_still_a_bank_charge() -> None:
    decision = ExpectedEvidenceEngine().classify(
        tx("t1", "-4.10", SEPT_12, "JUROS DESCOBERTO")
    )
    assert decision.expectation is EvidenceExpectation.BANK_EVIDENCE_SUFFICES


def test_supplier_names_ending_with_a_period_read_naturally() -> None:
    engine = ExpectedEvidenceEngine(suppliers=RESOLVER)
    refund = engine.classify(tx("t1", "10.00", SEPT_12))
    assert (
        refund.reason
        == "Money back from Vodafone Portugal, S.A. A credit note covers it."
    )
    debit = engine.classify(tx("t2", "-10.00", SEPT_12))
    assert (
        debit.reason
        == "Vodafone Portugal, S.A. should send an invoice for this payment."
    )
    assert ".." not in refund.reason + debit.reason


# --- descriptors


@pytest.mark.parametrize(
    ("descriptor", "key"),
    [
        ("PAYPAL EUROPE SARL", "paypal"),
        ("STRIPE PAYMENTS EUROPE LTD", "stripe"),
        ("PAYPAL *", "paypal"),
        ("PAYPAL *ADOBE", "adobe"),
        ("PAYPAL ADOBE", "adobe"),
        ("PayPal (Europe) S.à r.l. et Cie, S.C.A.", "paypal"),
        ("Dupont et Cie", "dupont"),
    ],
)
def test_a_processor_is_the_merchant_when_nothing_else_is_named(
    descriptor: str, key: str
) -> None:
    assert normalize_descriptor(descriptor).key == key


# --- ECB adapter


def test_ecb_refuses_non_currency_codes_without_any_request() -> None:
    calls: list[httpx.Request] = []

    def transport(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(404)

    rates = EcbFxRates(
        EcbConfig(base_url="https://ecb.test/service/data/EXR"),
        client=httpx.Client(transport=httpx.MockTransport(transport)),
    )
    for bad in ("../../admin", "US", "USD?x=1", "U$D"):
        with pytest.raises(ValueError):
            rates.rate(bad, "EUR", SEPT_12)
    assert calls == []
    assert rates.rate("usd", "usd", SEPT_12) == 1


def test_ecb_missing_observations_are_skipped_not_fatal() -> None:
    header = "KEY,FREQ,CURRENCY,CURRENCY_DENOM,EXR_TYPE,EXR_SUFFIX,TIME_PERIOD,OBS_VALUE,OBS_STATUS\n"
    body = header + (
        "EXR.D.USD.EUR.SP00.A,D,USD,EUR,SP00,A,2026-09-10,1.0850,A\n"
        "EXR.D.USD.EUR.SP00.A,D,USD,EUR,SP00,A,2026-09-11,NaN,A\n"
        "EXR.D.USD.EUR.SP00.A,D,USD,EUR,SP00,A,2026-09-12,Infinity,A\n"
    )
    rates = EcbFxRates(
        EcbConfig(base_url="https://ecb.test/service/data/EXR"),
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, text=body)
            )
        ),
    )
    assert rates.rate("EUR", "USD", SEPT_12) == D("1.0850")


def test_supplier_email_domains_may_be_written_as_addresses() -> None:
    hosting = Supplier(
        id="sup_h", tenant_id=T, name="Acme Hosting", email_domains=["@hostco.io"]
    )
    mail = Supplier(
        id="sup_m",
        tenant_id=T,
        name="Mailer Unlimited",
        email_domains=["billing@sendly.com"],
    )
    resolver = SupplierResolver([hosting, mail])
    assert resolver.resolve("HOSTCO.IO").key == "sup_h"
    assert resolver.resolve("WWW.SENDLY.COM 4411").key == "sup_m"


def test_a_cost_measured_against_the_stated_rate_is_not_called_an_estimate() -> None:
    payment = tx("t1", "-92.60", SEPT_12)
    meta = {"t1": BankMetadata(fx=FxDetails(D("100.00"), "USD", rate=D("0.9150")))}
    invoice = doc("d1", "100.00", SEPT_12, currency="USD", supplier_tax_id="502544180")
    (match,) = reconcile(
        [payment], [invoice], suppliers=RESOLVER, bank_metadata=meta
    ).matches
    assert "Conversion cost: €1.10" in match.why
    assert match.headline == "This payment includes €1.10 in currency conversion costs."


def test_money_received_short_after_conversion_is_a_cost() -> None:
    sale = doc(
        "s1", "100.00", SEPT_10, "Hazel Tree Lda", currency="USD",
        supplier_tax_id=OWN_TAX_ID, invoice_number="INV-2026-0042",
    )  # fmt: skip
    received = tx("t1", "91.00", SEPT_12, "GLOBEX CORP", description="INV-2026-0042")
    meta = {"t1": BankMetadata(fx=FxDetails(D("100.00"), "USD"))}
    (match,) = reconcile(
        [received],
        [sale],
        own_tax_ids=[OWN_TAX_ID],
        bank_metadata=meta,
        fx_rates=USD_EUR,
    ).matches
    assert match.conversion_cost == D("1.00")  # €92.00 expected, €91.00 received
    assert "Conversion cost: about €1.00" in match.why
