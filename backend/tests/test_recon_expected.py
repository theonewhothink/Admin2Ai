"""Expected-evidence engine (§21): no useless chasing."""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

import pytest

from backoffice.domain.models import (
    DocumentType,
    LegalEntity,
    Quality,
    Supplier,
    Transaction,
    TransactionKind,
)
from backoffice.reconciliation import (
    ACCEPTED_DOCUMENT_TYPES,
    BankMetadata,
    EvidenceExpectation,
    EvidenceProvider,
    ExpectationOverrides,
    ExpectedEvidenceEngine,
    InMemoryExpectationOverrides,
    LearnedExpectation,
    SupplierResolver,
)

T = "tenant"
E = EvidenceExpectation
HAZEL = LegalEntity(
    id="ent_hazel",
    tenant_id=T,
    name="Hazel Tree Lda",
    country="PT",
    tax_id="PT999999990",
    own_ibans=["PT50 0000 0000 0000 0000 0000 1", "PT50000000000000000000002"],
)
OAK = LegalEntity(
    id="ent_oak",
    tenant_id=T,
    name="Oak Holdings SA",
    country="PT",
    tax_id="PT888888880",
    own_ibans=["PT50000000000000000000009"],
)
VODAFONE = Supplier(
    id="sup_voda",
    tenant_id=T,
    name="Vodafone Portugal",
    aliases=["Vodafone"],
    known_ibans=["PT50000201231234567890154"],
)
UBER = Supplier(id="sup_uber", tenant_id=T, name="Uber B.V.", aliases=["UBER"])
_counter = iter(range(10_000))


def tx(
    amount: str,
    counterparty: str,
    kind: TransactionKind = TransactionKind.TRANSFER_OUT,
    **kw,
) -> Transaction:
    kw.setdefault("id", f"tx_{next(_counter)}")
    kw.setdefault("entity_id", "ent_hazel")
    return Transaction(
        tenant_id=T,
        account_id="acc",
        booked_on=date(2026, 9, 10),
        amount=Decimal(amount),
        counterparty=counterparty,
        kind=kind,
        **kw,
    )


def engine(**kw) -> ExpectedEvidenceEngine:
    kw.setdefault("entities", [HAZEL, OAK])
    kw.setdefault("suppliers", SupplierResolver([VODAFONE, UBER]))
    return ExpectedEvidenceEngine(**kw)


_BANNED = re.compile(
    r"reconcil|entit(?:y|ies)|exception|workflow|\bAPI\b|!|\b(?:tx|ent|sup)_\w+", re.I
)


# --- suppliers and receipts


def test_known_supplier_debit_expects_an_invoice() -> None:
    decision = engine().classify(
        tx("-83.21", "VODAFONE PT*1234", TransactionKind.DIRECT_DEBIT)
    )
    assert decision.expectation is E.INVOICE and decision.quality is Quality.GREEN
    assert (
        decision.reason == "Vodafone Portugal should send an invoice for this payment."
    )
    assert decision.requires_document and decision.provider is EvidenceProvider.SUPPLIER


def test_unknown_transfer_out_expects_an_invoice_but_is_only_likely() -> None:
    decision = engine().classify(tx("-400.00", "JOAO CARPINTEIRO"))
    assert decision.expectation is E.INVOICE and decision.quality is Quality.AMBER


def test_card_purchase_in_a_shop_needs_a_receipt() -> None:
    decision = engine().classify(
        tx(
            "-12.40",
            "COMPRA 4817 PASTELARIA BELEM LISBOA",
            TransactionKind.CARD,
            card_last4="4817",
        )
    )
    assert decision.expectation is E.RECEIPT and decision.quality is Quality.AMBER
    assert decision.provider is EvidenceProvider.OWNER


def test_declared_card_present_is_certain() -> None:
    purchase = tx(
        "-12.40", "SOME SHOP", TransactionKind.CARD, card_last4="4817", id="tx_pos"
    )
    decision = engine(
        bank_metadata={"tx_pos": BankMetadata(card_present=True)}
    ).classify(purchase)
    assert decision.expectation is E.RECEIPT and decision.quality is Quality.GREEN


def test_online_card_purchases_expect_an_invoice() -> None:
    for descriptor in ("NOTION.SO HELP.NOTION.SO", "PAYPAL *SOMESAAS"):
        decision = engine().classify(
            tx("-10.00", descriptor, TransactionKind.CARD, card_last4="4817")
        )
        assert decision.expectation is E.INVOICE, descriptor
    online = tx(
        "-10.00", "SHOP", TransactionKind.CARD, card_last4="4817", id="tx_online"
    )
    decision = engine(
        bank_metadata={"tx_online": BankMetadata(card_present=False)}
    ).classify(online)
    assert decision.expectation is E.INVOICE


def test_known_supplier_paid_by_card_expects_an_invoice() -> None:
    decision = engine().classify(
        tx("-14.20", "UBER *TRIP", TransactionKind.CARD, card_last4="4817")
    )
    assert decision.expectation is E.INVOICE


# --- money in


def test_refund_from_a_supplier_expects_a_credit_note() -> None:
    decision = engine().classify(
        tx("20.00", "VODAFONE PORTUGAL", TransactionKind.TRANSFER_IN)
    )
    assert decision.expectation is E.REFUND_OR_CREDIT_NOTE
    assert (
        decision.reason == "Money back from Vodafone Portugal. A credit note covers it."
    )


def test_card_refund_expects_a_credit_note() -> None:
    decision = engine().classify(
        tx("15.00", "ZARA LISBOA", TransactionKind.CARD, card_last4="4817")
    )
    assert decision.expectation is E.REFUND_OR_CREDIT_NOTE


def test_customer_payment_expects_our_sales_invoice() -> None:
    decision = engine().classify(
        tx("1230.00", "CLIENTE XPTO LDA", TransactionKind.TRANSFER_IN)
    )
    assert (
        decision.expectation is E.SALES_INVOICE
        and decision.provider is EvidenceProvider.OWN_RECORDS
    )


# --- no document needed


def test_transfer_between_own_accounts_needs_nothing() -> None:
    decision = engine().classify(
        tx("-500.00", "HAZEL TREE", counterparty_iban="pt50 0000 0000 0000 0000 0000 1")
    )
    assert (
        decision.expectation is E.NONE_INTERNAL_TRANSFER
        and decision.quality is Quality.GREEN
    )
    assert (
        not decision.requires_document and decision.provider is EvidenceProvider.NOBODY
    )
    assert decision.accepted_document_types == frozenset()


def test_transfer_to_another_own_company_is_explained_as_such() -> None:
    decision = engine().classify(
        tx("-500.00", "OAK HOLDINGS", counterparty_iban="PT50000000000000000000009")
    )
    assert decision.expectation is E.NONE_INTERNAL_TRANSFER
    assert decision.reason == "Money moved between your companies. No document needed."


def test_bank_flagged_internal_transfer() -> None:
    decision = engine().classify(tx("-500.00", "SAVINGS", TransactionKind.INTERNAL))
    assert (
        decision.expectation is E.NONE_INTERNAL_TRANSFER
        and decision.rule == "bank_marked_internal"
    )


def test_own_company_name_without_iban_is_only_likely_internal() -> None:
    decision = engine().classify(tx("-500.00", "HAZEL TREE LDA"))
    assert (
        decision.expectation is E.NONE_INTERNAL_TRANSFER
        and decision.quality is Quality.AMBER
    )


@pytest.mark.parametrize(
    ("counterparty", "kind", "amount"),
    [
        ("COMISSAO MANUTENCAO CONTA", TransactionKind.TRANSFER_OUT, "-5.20"),
        ("IMPOSTO SELO S/ COMISSAO", TransactionKind.TRANSFER_OUT, "-0.21"),
        ("MONTHLY FEE", TransactionKind.TRANSFER_OUT, "-9.00"),
        ("JUROS CREDORES", TransactionKind.TRANSFER_IN, "0.12"),
    ],
)
def test_bank_charges_and_interest_need_only_the_bank_statement(
    counterparty: str, kind: TransactionKind, amount: str
) -> None:
    decision = engine().classify(tx(amount, counterparty, kind))
    assert (
        decision.expectation is E.BANK_EVIDENCE_SUFFICES
        and decision.quality is Quality.AMBER
    )
    assert not decision.requires_document


def test_fee_kind_from_the_bank_is_certain() -> None:
    decision = engine().classify(tx("-3.00", "", TransactionKind.FEE))
    assert (
        decision.expectation is E.BANK_EVIDENCE_SUFFICES
        and decision.quality is Quality.GREEN
    )
    assert decision.reason == "Bank charge. Your bank statement is enough."


def test_commission_paid_to_someone_else_is_not_a_bank_fee() -> None:
    decision = engine().classify(
        tx(
            "-250.00",
            "COMISSAO AGENTE SILVA",
            counterparty_iban="PT50123412341234123412341",
        )
    )
    assert decision.expectation is E.INVOICE


def test_zero_amount_needs_nothing() -> None:
    decision = engine().classify(
        tx("0.00", "CARD CHECK", TransactionKind.CARD, card_last4="4817")
    )
    assert (
        decision.expectation is E.BANK_EVIDENCE_SUFFICES
        and decision.rule == "zero_amount"
    )


# --- tax, payroll, loans, cards


@pytest.mark.parametrize(
    "counterparty",
    [
        "AUTORIDADE TRIBUTARIA E ADUANEIRA",
        "Autoridade Tributária",
        "AT",
        "PAG ESTADO IVA 2026/08",
        "PAGAMENTO AO ESTADO",
        "SEG SOCIAL CONTRIBUICOES",
        "Segurança Social",
        "HMRC VAT",
        "AGENCIA TRIBUTARIA",
    ],
)
def test_tax_authorities_expect_a_notice_or_proof(counterparty: str) -> None:
    decision = engine().classify(tx("-1500.00", counterparty))
    assert decision.expectation is E.TAX_NOTICE_OR_PROOF, counterparty
    assert decision.reason == "Tax payment. I need the tax notice or payment proof."
    assert decision.provider is EvidenceProvider.GOVERNMENT


def test_tax_refund_is_recognised() -> None:
    decision = engine().classify(
        tx("320.00", "AUTORIDADE TRIBUTARIA", TransactionKind.TRANSFER_IN)
    )
    assert decision.expectation is E.TAX_NOTICE_OR_PROOF and decision.reason.startswith(
        "Tax refund"
    )


@pytest.mark.parametrize(
    "counterparty", ["IVA CONSULTORES LDA", "AT THE CORNER CAFE", "PATRICIA SANTOS"]
)
def test_words_that_merely_look_like_tax_are_not_tax(counterparty: str) -> None:
    assert (
        engine().classify(tx("-50.00", counterparty)).expectation
        is not E.TAX_NOTICE_OR_PROOF
    )


def test_payroll_by_employee_iban_is_certain() -> None:
    decision = engine(employee_ibans=["PT50111122223333444455556"]).classify(
        tx("-1200.00", "MARIA COSTA", counterparty_iban="PT50111122223333444455556")
    )
    assert decision.expectation is E.PAYROLL and decision.quality is Quality.GREEN


def test_payroll_by_wording_is_likely() -> None:
    decision = engine().classify(
        tx("-1200.00", "MARIA COSTA", description="VENCIMENTO SETEMBRO")
    )
    assert decision.expectation is E.PAYROLL and decision.quality is Quality.AMBER


def test_loan_repayment_expects_the_loan_statement() -> None:
    decision = engine().classify(tx("-640.00", "PRESTACAO EMPRESTIMO 000123"))
    assert (
        decision.expectation is E.LOAN_STATEMENT
        and decision.provider is EvidenceProvider.BANK
    )


def test_provision_of_services_is_not_a_loan() -> None:
    decision = engine().classify(tx("-640.00", "PRESTACAO DE SERVICOS LDA"))
    assert decision.expectation is E.INVOICE


def test_card_repayment_expects_the_card_statement() -> None:
    by_words = engine().classify(tx("-845.10", "LIQUIDACAO CARTAO CREDITO"))
    assert (
        by_words.expectation is E.CARD_STATEMENT and by_words.quality is Quality.AMBER
    )
    declared = tx("-845.10", "DEBITO", id="tx_settle")
    decision = engine(
        bank_metadata={"tx_settle": BankMetadata(settles_card_last4="4817")}
    ).classify(declared)
    assert (
        decision.expectation is E.CARD_STATEMENT and decision.quality is Quality.GREEN
    )


# --- learned overrides


def test_learned_overrides_win_over_rules() -> None:
    overrides = InMemoryExpectationOverrides()
    overrides.remember_supplier(
        "sup_voda", LearnedExpectation(E.BANK_EVIDENCE_SUFFICES)
    )
    overrides.remember_iban(
        "pt50 9999 0000 0000 0000 0000 1",
        LearnedExpectation(E.PAYROLL, "You told me this is Ana's salary."),
    )
    key = overrides.remember_descriptor(
        "RENDA ESCRITORIO 09", LearnedExpectation(E.INVOICE)
    )
    assert key == "renda escritorio"
    eng = engine(overrides=overrides)
    voda = eng.classify(tx("-83.21", "VODAFONE PT*1"))
    assert (
        voda.expectation is E.BANK_EVIDENCE_SUFFICES
        and voda.rule == "learned"
        and voda.quality is Quality.GREEN
    )
    assert voda.reason == "You told me the bank statement is enough for these."
    ana = eng.classify(
        tx("-900.00", "ANA", counterparty_iban="PT50999900000000000000001")
    )
    assert (
        ana.expectation is E.PAYROLL
        and ana.reason == "You told me this is Ana's salary."
    )
    rent = eng.classify(tx("-1500.00", "RENDA ESCRITORIO 10"))
    assert rent.expectation is E.INVOICE and rent.rule == "learned"


def test_overrides_are_a_protocol() -> None:
    class Always:
        def lookup(
            self, transaction: Transaction, supplier_key: str
        ) -> LearnedExpectation | None:
            return LearnedExpectation(E.RECEIPT)

    assert isinstance(Always(), ExpectationOverrides)
    assert (
        engine(overrides=Always()).classify(tx("-1.00", "X")).expectation is E.RECEIPT
    )


def test_uninformative_descriptor_cannot_be_learned() -> None:
    with pytest.raises(ValueError):
        InMemoryExpectationOverrides().remember_descriptor(
            "SEPA DD 0001", LearnedExpectation(E.INVOICE)
        )


# --- whole-engine properties


def test_classify_all_is_ordered_and_keyed_by_id() -> None:
    txs = [tx("-1", "B", id="tx_b"), tx("-1", "A", id="tx_a")]
    assert list(engine().classify_all(txs)) == ["tx_a", "tx_b"]


def test_every_expectation_has_a_provider_and_document_types() -> None:
    for expectation in E:
        assert expectation in ACCEPTED_DOCUMENT_TYPES
    assert DocumentType.CREDIT_NOTE in ACCEPTED_DOCUMENT_TYPES[E.REFUND_OR_CREDIT_NOTE]
    assert DocumentType.TAX_NOTICE in ACCEPTED_DOCUMENT_TYPES[E.TAX_NOTICE_OR_PROOF]


def test_reasons_are_plain_language() -> None:
    samples = [
        tx("-83.21", "VODAFONE"),
        tx("-500", "X", counterparty_iban="PT50000000000000000000002"),
        tx("-5", "COMISSAO"),
        tx("-1500", "HMRC"),
        tx("-1200", "SALARIO X"),
        tx("-640", "EMPRESTIMO"),
        tx("20", "UBER"),
        tx("100", "CUSTOMER"),
        tx("-12", "SHOP", TransactionKind.CARD, card_last4="1"),
        tx("-845", "LIQUIDACAO CARTAO"),
        tx("0", "X"),
        tx("-1", "PAYPAL *X", TransactionKind.CARD, card_last4="1"),
    ]
    seen = set()
    for decision in engine().classify_all(samples).values():
        seen.add(decision.expectation)
        assert decision.reason.endswith(".") and not _BANNED.search(decision.reason), (
            decision.reason
        )
    assert seen == set(E)


# --- rule-order regressions


def test_state_stamp_duty_is_tax_not_a_bank_fee() -> None:
    decision = engine().classify(tx("-120.00", "PAGAMENTO AO ESTADO IMPOSTO DO SELO"))
    assert decision.expectation is E.TAX_NOTICE_OR_PROOF


def test_bank_fee_with_vat_wording_stays_a_bank_fee() -> None:
    decision = engine().classify(tx("-6.15", "COMISSAO MANUTENCAO IMPOSTO IVA"))
    assert decision.expectation is E.BANK_EVIDENCE_SUFFICES


def test_due_date_wording_on_a_supplier_debit_is_not_payroll() -> None:
    debit = tx(
        "-45.10",
        "EDP COMERCIAL",
        TransactionKind.DIRECT_DEBIT,
        description="FATURA 99 VENCIMENTO 30/09",
    )
    assert engine().classify(debit).expectation is E.INVOICE
    known = tx("-83.21", "VODAFONE", description="VENCIMENTO 25/09")
    assert engine().classify(known).expectation is E.INVOICE


def test_default_card_kind_without_card_number_is_not_a_card_purchase() -> None:
    # Transaction.kind defaults to CARD; a transfer in with an IBAN is a customer payment.
    payment = tx(
        "900.00",
        "CLIENTE XPTO",
        TransactionKind.CARD,
        counterparty_iban="PT50123412341234123412341",
    )
    assert engine().classify(payment).expectation is E.SALES_INVOICE
