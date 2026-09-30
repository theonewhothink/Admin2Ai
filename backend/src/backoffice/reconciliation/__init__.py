"""Reconciliation: find the evidence behind every transaction (§20, §21, §54, §57).

Public API
----------

Expected evidence (§21) — what should this transaction have?

    engine = ExpectedEvidenceEngine(entities=[company], suppliers=resolver)
    decisions = engine.classify_all(transactions)      # {tx_id: ExpectationDecision}
    decisions[tx.id].expectation, .reason, .quality, .requires_document, .provider

Reconciliation (§20) — which documents prove it?

    result = reconcile(
        transactions, documents, ReconciliationConfig(),
        suppliers=resolver, bank_metadata={tx_id: BankMetadata(...)},
        history=InMemoryPaymentHistory({...}), fx_rates=EcbFxRates(),
        own_tax_ids=[company.tax_id], expectations=decisions,
        document_balances={doc_id: still_to_pay},
    )
    result.matches                      # Match: kind, quality, allocations, headline, why
    result.unmatched_transaction_ids    # need evidence
    result.unmatched_document_ids, result.open_balances, result.no_document_needed
    result.likely_no_document           # excused on wording only (AMBER): confirm first

Only GREEN "no document needed" decisions skip matching; AMBER ones are still
offered every document and are excused only when nothing fits (§21, §57).

Suppliers — descriptor normalization and learned aliases:

    resolver = SupplierResolver(suppliers, memory=InMemoryAliasMemory())
    resolver.resolve("PAYPAL *ADOBE").key      # supplier id, or 'adobe' when unknown
    resolver.learn("PAYPAL *ADOBESYSTEM", supplier.id)

Payouts (§20, §21) — money in from a card terminal or a payment / sales platform
is a net settlement, never customer revenue by itself; it expects the provider's
payout report (``EvidenceExpectation.PAYOUT_REPORT``), which
:mod:`backoffice.settlements` parses and reconciles. ``is_card_repayment``
(older name ``is_card_settlement``) is the opposite: paying off a credit card.

    payout_provider(tx)                # PayoutProvider or None ('STRIPE PAYMENTS', 'TPA 1234 SIBS')

Scoring (§54) — explainable candidate scores:

    score = score_match([tx], [doc], MatchContext(suppliers=resolver))
    score.points, score.why, grade(score, [doc], ScoringConfig())

Sign convention: a transaction amount is negative for money out; a document's
*flow* (see :func:`document_flow`) is the money movement it calls for, so a
purchase invoice is negative and a credit note or own sales invoice positive
(a credit note is money back whatever sign its total was extracted with; payroll
records are money out even though the business issues them).

FX: an original amount the bank states only makes a match exact when the booked
money is accounted for too (the stated rate, or a reference rate within the FX
tolerance). Amounts read from statement text are exact only through the rate.
Currency codes are compared case-insensitively.
``document_balances`` and ``Allocation.amount`` use the same signed flow.

Everything owner-facing (``Match.headline``, ``Match.why``,
``ExpectationDecision.reason``) is plain language without ids or jargon.
Money is Decimal throughout. Results are deterministic.
"""

from .bank import (
    BankMetadata,
    FxDetails,
    fx_from_text,
    is_card_purchase,
    is_card_repayment,
    is_card_settlement,
)
from .engine import (
    Allocation,
    Alternative,
    Match,
    MatchKind,
    MatchTag,
    ReconciliationConfig,
    ReconciliationResult,
    reconcile,
)
from .expected import (
    ACCEPTED_DOCUMENT_TYPES,
    EvidenceExpectation,
    EvidenceProvider,
    ExpectationDecision,
    ExpectationOverrides,
    ExpectedEvidenceEngine,
    InMemoryExpectationOverrides,
    LearnedExpectation,
)
from .fx import DatedFxRates, EcbConfig, EcbFxRates, FxRateSource, FxRateUnavailable, StaticFxRates
from .payouts import (
    CARD_TERMINAL,
    PAYOUT_PROVIDERS,
    PayoutProvider,
    ProviderKind,
    compatible_providers,
    payout_provider,
    provider_by_key,
    provider_named,
)
from .history import (
    Cadence,
    InMemoryPaymentHistory,
    PaymentHistory,
    PaymentPattern,
    infer_cadence,
)
from .scoring import (
    CARD_LAST4_FIELD,
    AmountCheck,
    AmountStatus,
    CandidateScore,
    Factor,
    FactorKind,
    MatchContext,
    Outcome,
    ScoringConfig,
    ScoringWeights,
    SupplierRelation,
    document_flow,
    grade,
    score_match,
    supplier_relation,
)
from .suppliers import (
    AliasMemory,
    InMemoryAliasMemory,
    NormalizedDescriptor,
    ResolveMethod,
    ResolverConfig,
    SupplierMatch,
    SupplierResolver,
    key_similarity,
    normalize_descriptor,
)

__all__ = [
    "ACCEPTED_DOCUMENT_TYPES",
    "CARD_LAST4_FIELD",
    "CARD_TERMINAL",
    "PAYOUT_PROVIDERS",
    "PayoutProvider",
    "ProviderKind",
    "compatible_providers",
    "is_card_repayment",
    "payout_provider",
    "provider_by_key",
    "provider_named",
    "AliasMemory",
    "Allocation",
    "Alternative",
    "AmountCheck",
    "AmountStatus",
    "BankMetadata",
    "Cadence",
    "CandidateScore",
    "DatedFxRates",
    "EcbConfig",
    "EcbFxRates",
    "EvidenceExpectation",
    "EvidenceProvider",
    "ExpectationDecision",
    "ExpectationOverrides",
    "ExpectedEvidenceEngine",
    "Factor",
    "FactorKind",
    "FxDetails",
    "FxRateSource",
    "FxRateUnavailable",
    "InMemoryAliasMemory",
    "InMemoryExpectationOverrides",
    "InMemoryPaymentHistory",
    "LearnedExpectation",
    "Match",
    "MatchContext",
    "MatchKind",
    "MatchTag",
    "NormalizedDescriptor",
    "Outcome",
    "PaymentHistory",
    "PaymentPattern",
    "ReconciliationConfig",
    "ReconciliationResult",
    "ResolveMethod",
    "ResolverConfig",
    "ScoringConfig",
    "ScoringWeights",
    "StaticFxRates",
    "SupplierMatch",
    "SupplierRelation",
    "SupplierResolver",
    "document_flow",
    "fx_from_text",
    "grade",
    "infer_cadence",
    "is_card_purchase",
    "is_card_settlement",
    "key_similarity",
    "normalize_descriptor",
    "reconcile",
    "score_match",
    "supplier_relation",
]
