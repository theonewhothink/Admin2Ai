"""Expected-evidence engine (§21): what evidence should this transaction have?

Not every debit needs an invoice. A Vodafone debit expects an invoice; a transfer
between the business's own accounts expects nothing; a tax payment expects the
tax notice or payment proof; a salary expects payroll records; a loan repayment
expects the loan statement; a bank fee is covered by the bank statement. Getting
this right prevents useless chasing (§22).

Rules run in a fixed order, first hit wins:

1. learned overrides (the owner or accountant said so once, §5, §40),
2. nothing moved (zero amount),
3. own accounts / own companies (IBAN, bank flag, or own company name),
4. credit-card repayments (money out paying off a card),
5. payouts (money in from a card terminal or a payment / sales platform: a net
   settlement whose evidence is the provider's payout report, never customer
   revenue on its own),
6. tax authorities named outright,
7. bank fees the bank itself flags,
8. loans (before fee wording: a loan instalment mentions interest, 'JUROS'),
9. bank fees and interest by wording,
10. other tax wording, then payroll,
11. money in: refunds from suppliers, otherwise customer payments,
12. money out: card purchases in a shop need a receipt, everything else an invoice.

Identity-based decisions (IBAN, bank flags, learned overrides, a resolved
supplier) are GREEN; wording-based ones are AMBER (§57): they steer where to
look, they never close anything.

The wording tables are bank-statement conventions, not legal facts. They were
compiled from public naming of the institutions and have NOT been verified
against live bank feeds (verified_as_of: never). Extend them as feeds are seen.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, runtime_checkable

from backoffice.domain.models import (
    DocumentType,
    LegalEntity,
    Quality,
    Transaction,
    TransactionKind,
)

from ._text import fold, normalize_iban, tokens
from .bank import (
    NO_METADATA,
    BankMetadata,
    is_card_purchase,
    is_card_repayment,
    phrase_in,
)
from .payouts import payout_provider
from .suppliers import SupplierMatch, SupplierResolver, normalize_descriptor

__all__ = [
    "ACCEPTED_DOCUMENT_TYPES",
    "BANK_FEE_PHRASES",
    "LOAN_PHRASES",
    "PAYROLL_WORDS",
    "TAX_AUTHORITY_PHRASES",
    "EvidenceExpectation",
    "EvidenceProvider",
    "ExpectationDecision",
    "ExpectationOverrides",
    "ExpectedEvidenceEngine",
    "InMemoryExpectationOverrides",
    "LearnedExpectation",
]


class EvidenceExpectation(str, Enum):
    INVOICE = "invoice"
    RECEIPT = "receipt"
    SALES_INVOICE = "sales_invoice"
    REFUND_OR_CREDIT_NOTE = "refund_or_credit_note"
    TAX_NOTICE_OR_PROOF = "tax_notice_or_proof"
    PAYROLL = "payroll"
    LOAN_STATEMENT = "loan_statement"
    CARD_STATEMENT = "card_statement"
    PAYOUT_REPORT = "payout_report"  # a provider's settlement statement for a payout (money in)
    BANK_EVIDENCE_SUFFICES = "bank_evidence_suffices"
    NONE_INTERNAL_TRANSFER = "none_internal_transfer"


class EvidenceProvider(str, Enum):
    """Where the missing-document autopilot (§22) should look or ask."""

    SUPPLIER = "supplier"
    OWNER = "owner"
    OWN_RECORDS = "own_records"
    GOVERNMENT = "government"
    PAYROLL = "payroll"
    BANK = "bank"
    PAYMENT_PROVIDER = "payment_provider"  # the card terminal or platform's payout report
    NOBODY = "nobody"


_PROVIDERS: dict[EvidenceExpectation, EvidenceProvider] = {
    EvidenceExpectation.INVOICE: EvidenceProvider.SUPPLIER,
    EvidenceExpectation.RECEIPT: EvidenceProvider.OWNER,
    EvidenceExpectation.SALES_INVOICE: EvidenceProvider.OWN_RECORDS,
    EvidenceExpectation.REFUND_OR_CREDIT_NOTE: EvidenceProvider.SUPPLIER,
    EvidenceExpectation.TAX_NOTICE_OR_PROOF: EvidenceProvider.GOVERNMENT,
    EvidenceExpectation.PAYROLL: EvidenceProvider.PAYROLL,
    EvidenceExpectation.LOAN_STATEMENT: EvidenceProvider.BANK,
    EvidenceExpectation.CARD_STATEMENT: EvidenceProvider.BANK,
    EvidenceExpectation.PAYOUT_REPORT: EvidenceProvider.PAYMENT_PROVIDER,
    EvidenceExpectation.BANK_EVIDENCE_SUFFICES: EvidenceProvider.NOBODY,
    EvidenceExpectation.NONE_INTERNAL_TRANSFER: EvidenceProvider.NOBODY,
}

_INVOICE_LIKE = frozenset(
    {DocumentType.INVOICE, DocumentType.INVOICE_RECEIPT, DocumentType.SIMPLIFIED_INVOICE,
     DocumentType.DEBIT_NOTE}
)  # fmt: skip

# Document types that satisfy each expectation (for matching and chasing).
ACCEPTED_DOCUMENT_TYPES: dict[EvidenceExpectation, frozenset[DocumentType]] = {
    EvidenceExpectation.INVOICE: _INVOICE_LIKE,
    EvidenceExpectation.RECEIPT: _INVOICE_LIKE | {DocumentType.RECEIPT},
    EvidenceExpectation.SALES_INVOICE: _INVOICE_LIKE | {DocumentType.RECEIPT},
    EvidenceExpectation.REFUND_OR_CREDIT_NOTE: frozenset({DocumentType.CREDIT_NOTE}),
    EvidenceExpectation.TAX_NOTICE_OR_PROOF: frozenset(
        {DocumentType.TAX_NOTICE, DocumentType.RECEIPT}
    ),
    EvidenceExpectation.PAYROLL: frozenset({DocumentType.PAYROLL}),
    EvidenceExpectation.LOAN_STATEMENT: frozenset(
        {DocumentType.LOAN_STATEMENT, DocumentType.STATEMENT}
    ),
    EvidenceExpectation.CARD_STATEMENT: frozenset({DocumentType.STATEMENT}),
    EvidenceExpectation.PAYOUT_REPORT: frozenset({DocumentType.PAYOUT_REPORT}),
    EvidenceExpectation.BANK_EVIDENCE_SUFFICES: frozenset(),
    EvidenceExpectation.NONE_INTERNAL_TRANSFER: frozenset(),
}

# --------------------------------------------------------------------------- wording tables
# Consecutive folded words (accents removed, uppercase). Unverified conventions.

TAX_AUTHORITY_PHRASES: tuple[str, ...] = (
    # Portugal
    "AUTORIDADE TRIBUTARIA",
    "AUTORIDADE TRIBUTARIA E ADUANEIRA",
    "PAGAMENTO AO ESTADO",
    "PAGAMENTOS AO ESTADO",
    "PAG AO ESTADO",
    "PAG ESTADO",
    "SEGURANCA SOCIAL",
    "SEG SOCIAL",
    "INSTITUTO DE GESTAO FINANCEIRA DA SEGURANCA SOCIAL",
    "IGFSS",
    # United Kingdom
    "HMRC",
    "HM REVENUE",
    # Spain
    "AGENCIA TRIBUTARIA",
    "AEAT",
    "TESORERIA GENERAL DE LA SEGURIDAD SOCIAL",
    "TGSS",
    # France / Italy / Germany / Ireland
    "DGFIP",
    "URSSAF",
    "AGENZIA DELLE ENTRATE",
    "DELEGA F24",
    "FINANZAMT",
    "REVENUE COMMISSIONERS",
)

# Portuguese tax abbreviations only count next to a word naming the state as payee
# ('PAG ESTADO IVA'), because on their own they collide with ordinary words.
TAX_WORDS: frozenset[str] = frozenset({"IVA", "IRC", "IRS", "IMI", "IUC", "IMT"})
STATE_WORDS: frozenset[str] = frozenset(
    {"ESTADO", "AT", "IMPOSTO", "IMPOSTOS", "FINANCAS", "TRIBUTARIA", "HMRC"}
)

BANK_FEE_PHRASES: tuple[str, ...] = (
    "COMISSAO",
    "COMISSOES",
    "COM MANUTENCAO",
    "MANUTENCAO DE CONTA",
    "MANUTENCAO CONTA",
    "DESPESAS DE MANUTENCAO",
    "IMPOSTO DO SELO",  # stamp duty the bank charges on its own fees and interest
    "IMPOSTO SELO",
    "IMP SELO",
    "JUROS",
    "BANK FEE",
    "BANK CHARGE",
    "BANK CHARGES",
    "SERVICE CHARGE",
    "ACCOUNT FEE",
    "MONTHLY FEE",
    "INTEREST",
    "OVERDRAFT",
    "COMISION",
    "COMISIONES",
    "INTERESES",
    "FRAIS BANCAIRES",
)

PAYROLL_WORDS: frozenset[str] = frozenset(
    {"SALARIO", "SALARIOS", "VENCIMENTO", "VENCIMENTOS", "ORDENADO", "ORDENADOS",
     "REMUNERACAO", "REMUNERACOES", "SALARY", "SALARIES", "PAYROLL", "WAGES", "NOMINA",
     "NOMINAS", "SUELDO", "GEHALT", "SALAIRE", "STIPENDIO"}
)  # fmt: skip

LOAN_PHRASES: tuple[str, ...] = (
    "PRESTACAO EMPRESTIMO",
    "PREST EMPRESTIMO",
    "AMORTIZACAO EMPRESTIMO",
    "EMPRESTIMO",
    "MUTUO",
    "LOAN REPAYMENT",
    "LOAN",
    "PRESTAMO",
    "CUOTA PRESTAMO",
)

# --------------------------------------------------------------------------- results


@dataclass(frozen=True)
class ExpectationDecision:
    """What evidence a transaction needs, and why, in plain words (§36, §54)."""

    transaction_id: str
    expectation: EvidenceExpectation
    reason: str
    quality: Quality
    rule: str  # developer-facing rule name, for the audit trail (§55)

    @property
    def requires_document(self) -> bool:
        return self.expectation not in (
            EvidenceExpectation.NONE_INTERNAL_TRANSFER,
            EvidenceExpectation.BANK_EVIDENCE_SUFFICES,
        )

    @property
    def provider(self) -> EvidenceProvider:
        return _PROVIDERS[self.expectation]

    @property
    def accepted_document_types(self) -> frozenset[DocumentType]:
        return ACCEPTED_DOCUMENT_TYPES[self.expectation]


@dataclass(frozen=True)
class LearnedExpectation:
    """An expectation the owner or accountant taught once ("I will remember this.").

    ``evidence_id`` is the stored answer or rule it came from (§54): a payment that needs no document
    because of it carries that evidence. ``taught_by`` is "owner" or "accountant".
    """

    expectation: EvidenceExpectation
    reason: str | None = None
    evidence_id: str | None = None
    taught_by: str = "owner"


@runtime_checkable
class ExpectationOverrides(Protocol):
    def lookup(
        self, transaction: Transaction, supplier_key: str
    ) -> LearnedExpectation | None: ...


@dataclass
class InMemoryExpectationOverrides:
    """Overrides keyed by counterparty IBAN, supplier key, or descriptor key."""

    by_iban: dict[str, LearnedExpectation] = field(default_factory=dict)
    by_supplier: dict[str, LearnedExpectation] = field(default_factory=dict)
    by_descriptor: dict[str, LearnedExpectation] = field(default_factory=dict)

    def remember_iban(self, iban: str, learned: LearnedExpectation) -> None:
        self.by_iban[normalize_iban(iban)] = learned

    def remember_supplier(self, supplier_key: str, learned: LearnedExpectation) -> None:
        self.by_supplier[supplier_key] = learned

    def remember_descriptor(self, descriptor: str, learned: LearnedExpectation) -> str:
        key = normalize_descriptor(descriptor).key
        if not key:
            raise ValueError("descriptor has nothing to remember")
        self.by_descriptor[key] = learned
        return key

    def lookup(
        self, transaction: Transaction, supplier_key: str
    ) -> LearnedExpectation | None:
        iban = normalize_iban(transaction.counterparty_iban)
        if iban and iban in self.by_iban:
            return self.by_iban[iban]
        if supplier_key and supplier_key in self.by_supplier:
            return self.by_supplier[supplier_key]
        key = normalize_descriptor(transaction.counterparty).key
        return self.by_descriptor.get(key) if key else None


# --------------------------------------------------------------------------- reasons

_LEARNED_REASON: dict[EvidenceExpectation, str] = {
    EvidenceExpectation.INVOICE: "You told me these payments need an invoice.",
    EvidenceExpectation.RECEIPT: "You told me a receipt is enough for these.",
    EvidenceExpectation.SALES_INVOICE: "You told me this money comes from customers.",
    EvidenceExpectation.REFUND_OR_CREDIT_NOTE: "You told me this is money back from a supplier.",
    EvidenceExpectation.TAX_NOTICE_OR_PROOF: "You told me these are tax payments.",
    EvidenceExpectation.PAYROLL: "You told me these are salaries.",
    EvidenceExpectation.LOAN_STATEMENT: "You told me these are loan payments.",
    EvidenceExpectation.CARD_STATEMENT: "You told me these pay off a card.",
    EvidenceExpectation.PAYOUT_REPORT: (
        "You told me these are payouts of your sales. Their payout report covers them."
    ),
    EvidenceExpectation.BANK_EVIDENCE_SUFFICES: (
        "You told me the bank statement is enough for these."
    ),
    EvidenceExpectation.NONE_INTERNAL_TRANSFER: "You told me this money stays in the business.",
}


# --------------------------------------------------------------------------- engine


@dataclass
class ExpectedEvidenceEngine:
    """Classifies transactions into :class:`EvidenceExpectation` (§21).

    ``entities`` supply the business's own IBANs and names; ``employee_ibans``
    marks salary transfers; ``bank_metadata`` adds declared facts (card present,
    card repayment). All inputs are injected; nothing is fetched.
    """

    entities: Sequence[LegalEntity] = ()
    suppliers: SupplierResolver = field(default_factory=SupplierResolver)
    overrides: ExpectationOverrides | None = None
    employee_ibans: Iterable[str] = ()
    bank_metadata: Mapping[str, BankMetadata] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._own_ibans: dict[str, str] = {}
        for entity in sorted(self.entities, key=lambda e: e.id):
            for iban in entity.own_ibans:
                self._own_ibans.setdefault(normalize_iban(iban), entity.id)
        self._own_names = tuple(
            sorted(
                {k for e in self.entities if (k := normalize_descriptor(e.name).key)}
            )
        )
        self._employees = frozenset(normalize_iban(i) for i in self.employee_ibans if i)

    def classify_all(
        self, transactions: Iterable[Transaction]
    ) -> dict[str, ExpectationDecision]:
        """Decisions keyed by transaction id, in booking order."""
        ordered = sorted(transactions, key=lambda t: (t.booked_on, t.id))
        return {t.id: self.classify(t) for t in ordered}

    def classify(self, tx: Transaction) -> ExpectationDecision:
        """What evidence ``tx`` should have, with a plain-language reason."""
        meta = self.bank_metadata.get(tx.id, NO_METADATA)
        supplier = self.suppliers.resolve_transaction(tx)
        text = f"{tx.counterparty} {tx.description}"
        tax = _tax_wording(tx, text)
        for rule in (
            lambda: self._learned(tx, supplier.key),
            lambda: self._nothing_moved(tx),
            lambda: self._internal(tx),
            lambda: self._card_repayment(tx, meta),
            # 'STRIPE PAYMENTS' / 'TPA 1234 SIBS' in: sales paid out net of fees, not a customer invoice.
            lambda: self._payout(tx),
            # 'PAGAMENTO AO ESTADO IMPOSTO DO SELO' is tax, not a bank charge.
            lambda: self._tax(tx, tax == "strong"),
            lambda: self._bank_fee(tx, text, worded=False),
            # 'PREST EMPRESTIMO CAPITAL E JUROS' is a loan instalment, not a charge.
            lambda: self._loan(tx, text),
            # 'IMPOSTO SELO S/ COMISSAO' is the bank's own charge.
            lambda: self._bank_fee(tx, text, worded=True),
            lambda: self._tax(tx, tax == "weak"),
            lambda: self._payroll(tx, text, supplier),
        ):
            decision = rule()
            if decision is not None:
                return decision
        if tx.amount > 0:
            return self._money_in(tx, supplier)
        return self._money_out(tx, supplier, meta)

    # ------------------------------------------------------------------ rules

    def _decide(self, tx, expectation, reason, quality, rule) -> ExpectationDecision:
        return ExpectationDecision(tx.id, expectation, reason, quality, rule)

    def _learned(
        self, tx: Transaction, supplier_key: str
    ) -> ExpectationDecision | None:
        if self.overrides is None:
            return None
        learned = self.overrides.lookup(tx, supplier_key)
        if learned is None:
            return None
        reason = learned.reason or _LEARNED_REASON[learned.expectation]
        return self._decide(tx, learned.expectation, reason, Quality.GREEN, "learned")

    def _nothing_moved(self, tx: Transaction) -> ExpectationDecision | None:
        if tx.amount != 0:
            return None
        return self._decide(
            tx, EvidenceExpectation.BANK_EVIDENCE_SUFFICES,
            "Nothing was charged. No document needed.", Quality.GREEN, "zero_amount",
        )  # fmt: skip

    def _internal(self, tx: Transaction) -> ExpectationDecision | None:
        expectation = EvidenceExpectation.NONE_INTERNAL_TRANSFER
        iban = normalize_iban(tx.counterparty_iban)
        if iban and iban in self._own_ibans:
            other = self._own_ibans[iban]
            if tx.entity_id and other != tx.entity_id:
                reason = "Money moved between your companies. No document needed."
                return self._decide(
                    tx, expectation, reason, Quality.GREEN, "own_company_iban"
                )
            reason = "Money moved between your own accounts. No document needed."
            return self._decide(tx, expectation, reason, Quality.GREEN, "own_iban")
        if tx.kind == TransactionKind.INTERNAL:
            reason = "Money moved between your own accounts. No document needed."
            return self._decide(
                tx, expectation, reason, Quality.GREEN, "bank_marked_internal"
            )
        name = normalize_descriptor(tx.counterparty).key
        if name and name in self._own_names and not is_card_purchase(tx):
            reason = (
                "This looks like a transfer to your own account. No document needed."
            )
            return self._decide(tx, expectation, reason, Quality.AMBER, "own_name")
        return None

    def _card_repayment(
        self, tx: Transaction, meta: BankMetadata
    ) -> ExpectationDecision | None:
        if not is_card_repayment(tx, meta):
            return None
        quality = Quality.GREEN if meta.settles_card_last4 else Quality.AMBER
        return self._decide(
            tx, EvidenceExpectation.CARD_STATEMENT,
            "This pays off a card. The card statement covers it.", quality, "card_repayment",
        )  # fmt: skip

    def _payout(self, tx: Transaction) -> ExpectationDecision | None:
        """Money in from a card terminal or a payment or sales platform (wording: AMBER)."""
        provider = payout_provider(tx)
        if provider is None:
            return None
        reason = (
            f"Payout of your sales from {provider.label}, after its {provider.fee_word}. "
            "Its payout report covers it."
        )
        return self._decide(
            tx, EvidenceExpectation.PAYOUT_REPORT, reason, Quality.AMBER, f"payout:{provider.key}"
        )

    def _bank_fee(
        self, tx: Transaction, text: str, *, worded: bool
    ) -> ExpectationDecision | None:
        """Declared by the bank (GREEN) or, when ``worded``, recognised by wording (AMBER)."""
        declared = tx.kind == TransactionKind.FEE
        if worded:
            if declared or not (
                not tx.counterparty_iban
                and not is_card_purchase(tx)
                and phrase_in(text, BANK_FEE_PHRASES)
            ):
                return None
        elif not declared:
            return None
        if tx.amount > 0:
            reason = "Interest from your bank. Your bank statement is enough."
        else:
            reason = "Bank charge. Your bank statement is enough."
        quality = Quality.GREEN if declared else Quality.AMBER
        return self._decide(
            tx, EvidenceExpectation.BANK_EVIDENCE_SUFFICES, reason, quality,
            "bank_fee" if declared else "bank_fee_wording",
        )  # fmt: skip

    def _tax(self, tx: Transaction, applies: bool) -> ExpectationDecision | None:
        if not applies:
            return None
        reason = (
            "Tax refund. The tax notice covers it."
            if tx.amount > 0
            else "Tax payment. I need the tax notice or payment proof."
        )
        return self._decide(
            tx,
            EvidenceExpectation.TAX_NOTICE_OR_PROOF,
            reason,
            Quality.AMBER,
            "tax_wording",
        )

    def _payroll(
        self, tx: Transaction, text: str, supplier: SupplierMatch
    ) -> ExpectationDecision | None:
        """Employee IBANs are certain. Wording only counts on a plain transfer to
        someone who is not a known supplier ('VENCIMENTO' also means 'due date')."""
        if tx.amount >= 0:
            return None
        iban = normalize_iban(tx.counterparty_iban)
        if iban and iban in self._employees:
            return self._decide(
                tx, EvidenceExpectation.PAYROLL,
                "Salary payment. Payroll records cover it.", Quality.GREEN, "employee_iban",
            )  # fmt: skip
        plain_transfer = (
            not is_card_purchase(tx) and tx.kind != TransactionKind.DIRECT_DEBIT
        )
        if (
            plain_transfer
            and not supplier.is_known
            and set(tokens(text)) & PAYROLL_WORDS
        ):
            reason = "This looks like a salary. Payroll records cover it."
            return self._decide(
                tx,
                EvidenceExpectation.PAYROLL,
                reason,
                Quality.AMBER,
                "payroll_wording",
            )
        return None

    def _loan(self, tx: Transaction, text: str) -> ExpectationDecision | None:
        if is_card_purchase(tx) or not phrase_in(text, LOAN_PHRASES):
            return None
        reason = (
            "Loan money received. The loan statement covers it."
            if tx.amount > 0
            else "Loan payment. The loan statement covers it."
        )
        return self._decide(
            tx,
            EvidenceExpectation.LOAN_STATEMENT,
            reason,
            Quality.AMBER,
            "loan_wording",
        )

    def _money_in(
        self, tx: Transaction, supplier: SupplierMatch
    ) -> ExpectationDecision:
        if is_card_purchase(tx) or supplier.is_known:
            name = supplier.display_name
            reason = (
                f"{_sentence(f'Money back from {name}')} A credit note covers it."
                if name
                else "Money back from a supplier. A credit note covers it."
            )
            quality = Quality.GREEN if supplier.is_strong else Quality.AMBER
            return self._decide(
                tx, EvidenceExpectation.REFUND_OR_CREDIT_NOTE, reason, quality, "refund"
            )
        return self._decide(
            tx, EvidenceExpectation.SALES_INVOICE,
            "Money from a customer. Your own invoice covers it.", Quality.AMBER, "customer_payment",
        )  # fmt: skip

    def _money_out(
        self, tx: Transaction, supplier: SupplierMatch, meta: BankMetadata
    ) -> ExpectationDecision:
        name = supplier.display_name
        invoice_reason = (
            f"{name} should send an invoice for this payment." if name
            else "Payments like this need an invoice."
        )  # fmt: skip
        if supplier.is_known:
            quality = Quality.GREEN if supplier.is_strong else Quality.AMBER
            return self._decide(
                tx,
                EvidenceExpectation.INVOICE,
                invoice_reason,
                quality,
                "known_supplier",
            )
        if is_card_purchase(tx) and _in_shop(meta, supplier):
            quality = Quality.GREEN if meta.card_present else Quality.AMBER
            return self._decide(
                tx, EvidenceExpectation.RECEIPT,
                "Card purchase in a shop. The receipt is enough.", quality, "card_in_shop",
            )  # fmt: skip
        return self._decide(
            tx,
            EvidenceExpectation.INVOICE,
            invoice_reason,
            Quality.AMBER,
            "payment_out",
        )


def _sentence(text: str) -> str:
    """End ``text`` with exactly one period ('..., S.A.' keeps its own)."""
    return text if text.endswith(".") else f"{text}."


def _tax_wording(tx: Transaction, text: str) -> str | None:
    """'strong' (an authority is named), 'weak' (tax word next to a state word), or None."""
    if phrase_in(text, TAX_AUTHORITY_PHRASES) or tokens(tx.counterparty) == ["AT"]:
        return "strong"
    words = set(tokens(fold(text)))
    if words & TAX_WORDS and words & STATE_WORDS:
        return "weak"
    return None


def _in_shop(meta: BankMetadata, supplier: SupplierMatch) -> bool:
    """In-shop unless declared online, or the descriptor shows an online merchant."""
    if meta.card_present is not None:
        return meta.card_present
    descriptor = supplier.descriptor
    return not (descriptor and (descriptor.domain or descriptor.processor))
