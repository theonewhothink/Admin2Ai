"""Explainable candidate scoring between transactions and documents (§20, §54, §57).

:func:`score_match` compares one or more bank transactions with one or more
documents and returns a :class:`CandidateScore`: integer points (0-100), the
factors behind them, and plain-language "Why?" lines such as::

    Invoice total: €83.21
    Bank charge: €83.21
    Dates: 1 day apart
    Supplier VAT: matches
    Card ending: matches
    Historical pattern: monthly

Sign convention (tenant's point of view): a transaction's amount is negative for
money out. A document's *flow* is the money movement it calls for: a purchase
invoice is money out (negative), a purchase credit note is money back
(positive), our own sales invoice is money in. Matching compares the two.

Scoring is deterministic and uses Decimal for money, integers for points.
:func:`grade` turns a score into GREEN / AMBER / RED (§57): GREEN only when
the score is high, the identity is confirmed, every document is GREEN and the
amounts reconcile exactly after declared fees/FX. Disagreement on bank details
is RED (§19). AMBER is never promoted to GREEN.
"""

from __future__ import annotations

import logging
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from typing import TypedDict

from backoffice.domain.models import Document, DocumentType, Quality, Transaction

from ._text import (
    compile_identifier,
    currency_code,
    days_phrase,
    fold,
    format_money,
    is_currency_code,
    normalize_iban,
    same_tax_id,
    squash,
)
from .bank import NO_METADATA, BankMetadata, FxDetails, fx_from_text
from .fx import FxRateSource, FxRateUnavailable, divide
from .history import PaymentHistory
from .suppliers import (
    STRONG_NAME_KINDS,
    ResolveMethod,
    SupplierMatch,
    SupplierResolver,
    key_similarity,
    normalize_descriptor,
)

__all__ = [
    "CARD_LAST4_FIELD",
    "AmountCheck",
    "AmountStatus",
    "CandidateScore",
    "Factor",
    "FactorKind",
    "MatchContext",
    "Outcome",
    "ScoringConfig",
    "ScoringWeights",
    "SupplierRelation",
    "check_amount",
    "document_card_last4",
    "document_flow",
    "document_label",
    "grade",
    "identifier_in",
    "is_own_document",
    "is_sales_document",
    "score_match",
    "supplier_relation",
]

logger = logging.getLogger(__name__)

# Documents may carry the paying card's last digits as a verified field.
CARD_LAST4_FIELD = "card_last4"
_ZERO = Decimal(0)
_CENT = Decimal("0.01")


# --------------------------------------------------------------------------- configuration


@dataclass(frozen=True)
class ScoringWeights:
    """Points per factor. Totals are clamped to 0-100."""

    amount_exact: int = 40
    amount_adjusted: int = 40  # exact after a declared fee or FX conversion (§20)
    amount_near: tuple[int, int, int] = (20, 12, 6)  # within 1/4, 1/2, all of tolerance
    amount_partial: int = 15
    currency_same: int = 5
    supplier_vat: int = 20
    supplier_same: int = 15
    supplier_similar: int = 8
    supplier_different_name: int = -15
    supplier_different: int = -40
    invoice_number: int = 25
    payment_reference: int = 25
    identifiers_cap: int = 30
    bank_details_match: int = 15
    bank_details_mismatch: int = -10
    card_match: int = 5
    card_mismatch: int = -5
    dates: tuple[tuple[int, int], ...] = ((3, 10), (7, 8), (14, 6), (31, 4))
    dates_in_window: int = 3
    due_date: int = 4
    history: int = 5
    # Card repayments: the bank and the card issuer are named differently, so the
    # supplier factor is replaced by the card itself.
    settlement_card: int = 20  # statement's card ending is the card being repaid
    settlement_purchases: int = 10  # the card's purchases add up to the repayment


@dataclass(frozen=True)
class ScoringConfig:
    """Tolerances, date windows and thresholds (all configurable, §20).

    * near amounts: within ``min(max(abs, ratio * amount), max)``.
    * dates: payment up to ``max_payment_lag_days`` after the document date and
      ``max_payment_lead_days`` before it (card charged, invoice emailed later);
      deposits may precede the invoice by ``deposit_window_days``.
    * FX: reference-rate matches and undeclared conversion costs are accepted
      within ``fx_tolerance_ratio``; declared-rate arithmetic within
      ``rounding_tolerance`` per converted transaction.
    """

    near_amount_abs: Decimal = Decimal("2.00")
    near_amount_ratio: Decimal = Decimal("0.01")
    near_amount_max: Decimal = Decimal("25.00")
    fx_tolerance_ratio: Decimal = Decimal("0.03")
    rounding_tolerance: Decimal = Decimal("0.01")
    max_payment_lag_days: int = 60
    max_payment_lead_days: int = 7
    deposit_window_days: int = 120
    due_date_window_days: int = 2
    high_threshold: int = 70
    plausible_threshold: int = 45
    partial_threshold: int = 45
    ambiguity_margin: int = 3
    similar_name_threshold: float = 0.86
    different_name_threshold: float = 0.5
    read_fx_from_text: bool = True
    weights: ScoringWeights = field(default_factory=ScoringWeights)

    def __post_init__(self) -> None:
        for name in ("near_amount_abs", "near_amount_ratio", "near_amount_max",
                     "fx_tolerance_ratio", "rounding_tolerance"):  # fmt: skip
            value = getattr(self, name)
            if not isinstance(value, Decimal) or value < 0:
                raise ValueError(f"{name} must be a non-negative Decimal")
        for name in ("max_payment_lag_days", "max_payment_lead_days", "deposit_window_days",
                     "due_date_window_days", "ambiguity_margin"):  # fmt: skip
            if getattr(self, name) < 0:
                raise ValueError(f"{name} cannot be negative")
        if not 0 <= self.plausible_threshold <= self.high_threshold <= 100:
            raise ValueError("thresholds must satisfy 0 <= plausible <= high <= 100")

    def near_tolerance(self, amount: Decimal) -> Decimal:
        scaled = abs(amount) * self.near_amount_ratio
        return min(max(self.near_amount_abs, scaled), self.near_amount_max)


# --------------------------------------------------------------------------- results


class FactorKind(str, Enum):
    AMOUNT = "amount"
    CURRENCY = "currency"
    DATES = "dates"
    DUE_DATE = "due_date"
    SUPPLIER = "supplier"
    INVOICE_NUMBER = "invoice_number"
    PAYMENT_REFERENCE = "payment_reference"
    BANK_DETAILS = "bank_details"
    CARD = "card"
    HISTORY = "history"


class Outcome(str, Enum):
    SUPPORTS = "supports"
    NEUTRAL = "neutral"
    CONTRADICTS = "contradicts"


@dataclass(frozen=True)
class Factor:
    """One scored reason. ``why`` holds owner-facing lines (§54), possibly none."""

    kind: FactorKind
    outcome: Outcome
    points: int
    why: tuple[str, ...] = ()


class AmountStatus(str, Enum):
    EXACT = "exact"
    ADJUSTED = "exact_after_declared_fees_or_fx"
    NEAR = "near"
    PARTIAL = "partial"
    MISMATCH = "mismatch"


@dataclass(frozen=True)
class AmountCheck:
    """How the money compares.

    ``document_total`` and ``covered`` are signed flows in the document currency;
    ``covered`` is what the transactions pay towards the documents. ``fee`` is
    the declared fee total and ``conversion_cost`` an undeclared or estimated FX
    cost (positive = the business is worse off), both in the bank currency.
    ``difference`` = covered - document_total, document currency.
    ``cost_estimated`` is True when the conversion cost was measured against a
    reference rate rather than the rate the bank stated.
    """

    status: AmountStatus
    document_total: Decimal
    document_currency: str
    bank_total: Decimal
    bank_currency: str
    covered: Decimal
    fee: Decimal = _ZERO
    conversion_cost: Decimal | None = None
    fx_rate: Decimal | None = None
    fx_declared: bool = False
    cost_estimated: bool = False

    @property
    def difference(self) -> Decimal:
        return self.covered - self.document_total

    @property
    def exact(self) -> bool:
        return self.status in (AmountStatus.EXACT, AmountStatus.ADJUSTED)

    @property
    def is_fx(self) -> bool:
        return self.document_currency != self.bank_currency


class _Amounts(TypedDict):
    """The four facts every :class:`AmountCheck` starts from."""

    document_total: Decimal
    document_currency: str
    bank_total: Decimal
    bank_currency: str


class SupplierRelation(str, Enum):
    SAME_VAT = "same_vat_number"
    SAME = "same"
    SIMILAR = "similar_name"
    UNKNOWN = "unknown"
    UNCLEAR = "unclear"  # conflicting supplier signals on one side
    DIFFERENT_NAME = "different_name"
    DIFFERENT = "different"


_RELATION_ORDER = [
    SupplierRelation.DIFFERENT,
    SupplierRelation.UNCLEAR,
    SupplierRelation.DIFFERENT_NAME,
    SupplierRelation.UNKNOWN,
    SupplierRelation.SIMILAR,
    SupplierRelation.SAME,
    SupplierRelation.SAME_VAT,
]


@dataclass(frozen=True)
class CandidateScore:
    """A scored candidate: which transactions, which documents, and why."""

    transaction_ids: tuple[str, ...]
    document_ids: tuple[str, ...]
    points: int
    factors: tuple[Factor, ...]
    amount: AmountCheck
    supplier: SupplierRelation
    identity_confirmed: bool
    identifier_found: bool
    conflict: bool
    viable: bool
    days_apart: int | None
    rejection: str | None = None  # developer-facing, never shown to owners

    @property
    def why(self) -> tuple[str, ...]:
        """Owner-facing provenance lines in a stable order (§54)."""
        return tuple(line for f in self.factors for line in f.why)

    @property
    def contradicted(self) -> bool:
        return any(f.outcome is Outcome.CONTRADICTS for f in self.factors)


# --------------------------------------------------------------------------- context


# Documents the business may issue itself that never ask for money in.
_NEVER_SALES = frozenset(
    {DocumentType.PAYROLL, DocumentType.TAX_NOTICE, DocumentType.STATEMENT,
     DocumentType.LOAN_STATEMENT}
)  # fmt: skip


def document_flow(doc: Document, own_tax_ids: Collection[str] = ()) -> Decimal | None:
    """Money movement a document calls for, from the tenant's side (None without a total).

    Purchase invoice -> negative, purchase credit note -> positive, own sales
    invoice -> positive, own sales credit note -> negative. A credit note is
    money back whatever sign its total was extracted with. Payroll records are
    money out even though the business issues them.
    """
    gross = doc.gross_amount
    if gross is None:
        return None
    signed = -abs(gross) if doc.doc_type is DocumentType.CREDIT_NOTE else gross
    return signed if is_sales_document(doc, own_tax_ids) else -signed


def is_own_document(doc: Document, own_tax_ids: Collection[str]) -> bool:
    """True when the tenant issued the document (its tax id is the supplier's)."""
    return bool(doc.supplier_tax_id) and any(
        same_tax_id(doc.supplier_tax_id, own) for own in own_tax_ids
    )


def is_sales_document(doc: Document, own_tax_ids: Collection[str]) -> bool:
    """True for the tenant's own invoices, receipts and notes (money in)."""
    return doc.doc_type not in _NEVER_SALES and is_own_document(doc, own_tax_ids)


class MatchContext:
    """Everything scoring needs besides the transaction and document themselves.

    Caches supplier resolutions and reference rates per run. Transaction and
    document ids must be unique within a run.
    """

    def __init__(
        self,
        *,
        suppliers: SupplierResolver | None = None,
        bank_metadata: Mapping[str, BankMetadata] | None = None,
        history: PaymentHistory | None = None,
        fx_rates: FxRateSource | None = None,
        own_tax_ids: Iterable[str] = (),
        config: ScoringConfig | None = None,
    ) -> None:
        self.suppliers = suppliers or SupplierResolver()
        self.bank_metadata = dict(bank_metadata or {})
        self.history = history
        self.fx_rates = fx_rates
        self.own_tax_ids = tuple(own_tax_ids)
        self.config = config or ScoringConfig()
        self._tx_suppliers: dict[str, SupplierMatch] = {}
        self._doc_suppliers: dict[str, SupplierMatch] = {}
        self._rates: dict[tuple[str, str, date], Decimal | None] = {}
        self._texts: dict[str, tuple[str, str]] = {}

    def metadata(self, tx: Transaction) -> BankMetadata:
        return self.bank_metadata.get(tx.id, NO_METADATA)

    def fx_details(self, tx: Transaction) -> FxDetails | None:
        """Declared FX, else an unambiguous foreign amount in the bank text."""
        meta = self.metadata(tx)
        if meta.fx is not None:
            return meta.fx
        if self.config.read_fx_from_text:
            return fx_from_text(f"{tx.counterparty} {tx.description}", tx.currency)
        return None

    def supplier_of(self, tx: Transaction) -> SupplierMatch:
        if tx.id not in self._tx_suppliers:
            self._tx_suppliers[tx.id] = self.suppliers.resolve_transaction(tx)
        return self._tx_suppliers[tx.id]

    def supplier_of_document(self, doc: Document) -> SupplierMatch:
        if doc.id not in self._doc_suppliers:
            self._doc_suppliers[doc.id] = self.suppliers.resolve_document(doc)
        return self._doc_suppliers[doc.id]

    def flow(self, doc: Document) -> Decimal | None:
        return document_flow(doc, self.own_tax_ids)

    def bank_text(self, tx: Transaction) -> tuple[str, str]:
        """(folded, squashed) text a payer may have typed references into."""
        if tx.id not in self._texts:
            folded = fold(
                f"{tx.counterparty} | {tx.description} | {tx.reference or ''}"
            )
            self._texts[tx.id] = (folded, squash(folded))
        return self._texts[tx.id]

    def reference_rate(self, base: str, quote: str, on: date) -> Decimal | None:
        """Reference rate (quote per base) or None; source failures are logged, not raised."""
        if self.fx_rates is None or not (
            is_currency_code(base) and is_currency_code(quote)
        ):
            return None
        base, quote = currency_code(base), currency_code(quote)
        key = (base, quote, on)
        if key not in self._rates:
            try:
                self._rates[key] = self.fx_rates.rate(base, quote, on)
            except FxRateUnavailable as exc:
                logger.warning(
                    "reference FX rate unavailable (%s -> %s on %s): %s",
                    base,
                    quote,
                    on,
                    exc,
                )
                self._rates[key] = None
        return self._rates[key]


# --------------------------------------------------------------------------- amounts


def _sign(value: Decimal) -> int:
    return (value > 0) - (value < 0)


def _money(amount: Decimal, currency: str) -> str:
    return format_money(abs(amount), currency)


def check_amount(
    transactions: Sequence[Transaction],
    document_total: Decimal,
    document_currency: str,
    ctx: MatchContext,
) -> AmountCheck:
    """Compare booked money with a document total (same currency or FX)."""
    bank_currency = currency_code(transactions[0].currency)
    document_currency = currency_code(document_currency)
    bank_total = sum((t.amount for t in transactions), _ZERO)
    bank_fee = sum((ctx.metadata(t).fee for t in transactions), _ZERO)
    base: _Amounts = {
        "document_total": document_total,
        "document_currency": document_currency,
        "bank_total": bank_total,
        "bank_currency": bank_currency,
    }
    if document_total == 0:
        return AmountCheck(
            status=AmountStatus.MISMATCH, covered=_ZERO, fee=bank_fee, **base
        )
    if bank_currency == document_currency:
        covered = bank_total + bank_fee
        status = _classify(covered, document_total, ctx.config, adjusted=bank_fee != 0)
        return AmountCheck(status=status, covered=covered, fee=bank_fee, **base)
    fx = [ctx.fx_details(t) for t in transactions]
    if all(f is not None and f.original_currency == document_currency for f in fx):
        return _declared_fx(transactions, [f for f in fx if f], bank_fee, ctx, base)
    latest = max(t.booked_on for t in transactions)
    rate = ctx.reference_rate(document_currency, bank_currency, latest)
    if rate is not None:
        return _reference_fx(rate, bank_fee, ctx.config, base)
    return AmountCheck(
        status=AmountStatus.MISMATCH, covered=_ZERO, fee=bank_fee, **base
    )


def _classify(
    covered: Decimal, total: Decimal, config: ScoringConfig, *, adjusted: bool
) -> AmountStatus:
    if _sign(covered) != _sign(total) or total == 0:
        return AmountStatus.MISMATCH
    diff = covered - total
    if diff == 0:
        return AmountStatus.ADJUSTED if adjusted else AmountStatus.EXACT
    if abs(diff) <= config.near_tolerance(total):
        return AmountStatus.NEAR
    if abs(covered) < abs(total):
        return AmountStatus.PARTIAL
    return AmountStatus.MISMATCH


def _declared_fx(
    transactions: Sequence[Transaction],
    fx: Sequence[FxDetails],
    bank_fee: Decimal,
    ctx: MatchContext,
    base: _Amounts,
) -> AmountCheck:
    """The bank stated the original amounts: compare those with the document (§20 FX).

    Equal original amounts are only *exact* when the booked money is accounted
    for too (§57): by the stated rate, or, for a structured bank field without a
    rate, by staying within the FX tolerance of a reference rate.
    """
    fees = bank_fee + sum((f.fee for f in fx), _ZERO)
    covered = sum(
        (
            _sign(t.amount) * f.original_amount
            for t, f in zip(transactions, fx, strict=True)
        ),
        _ZERO,
    )
    rates = [_oriented_rate(t, f, ctx) for t, f in zip(transactions, fx, strict=True)]
    implied = divide(abs(base["bank_total"] + fees), abs(covered)) if covered else None
    status = _classify(covered, base["document_total"], ctx.config, adjusted=True)
    cost: Decimal | None = None
    estimated = False
    if status is AmountStatus.ADJUSTED:
        status, cost, estimated = _verify_conversion(
            transactions, fx, rates, fees, ctx, base
        )
    return AmountCheck(
        status=status, covered=covered, fee=fees, conversion_cost=cost,
        fx_rate=_single_rate(rates) or implied, fx_declared=True,
        cost_estimated=estimated, **base,
    )  # fmt: skip


def _verify_conversion(
    transactions: Sequence[Transaction],
    fx: Sequence[FxDetails],
    rates: Sequence[Decimal | None],
    fees: Decimal,
    ctx: MatchContext,
    base: _Amounts,
) -> tuple[AmountStatus, Decimal | None, bool]:
    """(status, conversion cost, cost is an estimate) once the original amounts
    equal the document.

    * stated rates must explain the booked money within rounding, else NEAR
      (undeclared spread, within the FX tolerance) or MISMATCH;
    * without a rate, a reference rate may not be contradicted beyond the FX
      tolerance: a structured bank field then drops to NEAR, statement text
      (possibly typed by a payer) to MISMATCH;
    * statement text without a rate is never more than NEAR: nothing proved
      the booked amount.
    """
    config = ctx.config
    unexplained, converted = _rate_arithmetic(transactions, fx, rates, ctx)
    if converted and abs(unexplained) > config.rounding_tolerance * converted:
        limit = abs(base["bank_total"]) * config.fx_tolerance_ratio
        within = abs(unexplained) <= limit
        status = AmountStatus.NEAR if within else AmountStatus.MISMATCH
        return status, -unexplained, False
    if converted == len(transactions):
        return AmountStatus.ADJUSTED, None, False
    unproven = any(f.from_text and r is None for f, r in zip(fx, rates, strict=True))
    on = max(t.booked_on for t in transactions)
    reference = _reference_gap(ctx, base, fees, on)
    if reference is None:
        return (AmountStatus.NEAR if unproven else AmountStatus.ADJUSTED), None, False
    cost, expected = reference
    within = abs(cost) <= expected * config.fx_tolerance_ratio
    if unproven:
        return (AmountStatus.NEAR if within else AmountStatus.MISMATCH), cost, True
    if within:  # a normal spread: exact as declared, cost shown for information
        return AmountStatus.ADJUSTED, (cost if cost > 0 else None), True
    return AmountStatus.NEAR, cost, True


def _unexplained(
    tx: Transaction, f: FxDetails, rate: Decimal, ctx: MatchContext
) -> Decimal:
    """Booked amount minus (original * rate +/- fees), bank currency."""
    expected = (f.original_amount * rate).quantize(_CENT, rounding=ROUND_HALF_UP)
    fees = ctx.metadata(tx).fee + f.fee
    return tx.amount + fees - _sign(tx.amount) * expected


def _oriented_rate(tx: Transaction, f: FxDetails, ctx: MatchContext) -> Decimal | None:
    """The stated rate; a rate read from text is turned round ('1 EUR = 1.0852 USD')
    only when that inverse, and not the rate as written, explains the booked amount."""
    if f.rate is None or not f.from_text:
        return f.rate
    tolerance = ctx.config.rounding_tolerance
    if abs(_unexplained(tx, f, f.rate, ctx)) <= tolerance:
        return f.rate
    inverse = divide(Decimal(1), f.rate)
    return inverse if abs(_unexplained(tx, f, inverse, ctx)) <= tolerance else f.rate


def _rate_arithmetic(
    transactions: Sequence[Transaction],
    fx: Sequence[FxDetails],
    rates: Sequence[Decimal | None],
    ctx: MatchContext,
) -> tuple[Decimal, int]:
    """Unexplained booked money summed over transactions with a rate, and their count."""
    unexplained, converted = _ZERO, 0
    for tx, f, rate in zip(transactions, fx, rates, strict=True):
        if rate is None:
            continue
        converted += 1
        unexplained += _unexplained(tx, f, rate, ctx)
    return unexplained, converted


def _single_rate(rates: Sequence[Decimal | None]) -> Decimal | None:
    distinct = set(rates)
    return next(iter(distinct)) if len(distinct) == 1 and None not in distinct else None


def _reference_gap(
    ctx: MatchContext, base: _Amounts, fees: Decimal, on: date
) -> tuple[Decimal, Decimal] | None:
    """(conversion cost, reference conversion), bank currency.

    The cost is positive when the business is worse off than the reference
    rate: it paid more, or received less.
    """
    rate = ctx.reference_rate(base["document_currency"], base["bank_currency"], on)
    if rate is None:
        return None
    booked = base["bank_total"] + fees  # signed, before the declared fees
    expected = abs(base["document_total"]) * rate
    cost = _sign(booked) * expected - booked
    return cost.quantize(_CENT, rounding=ROUND_HALF_UP), expected


def _reference_fx(
    rate: Decimal, bank_fee: Decimal, config: ScoringConfig, base: _Amounts
) -> AmountCheck:
    """No declared conversion: at best a plausible match, with the cost estimated."""
    expected = base["document_total"] * rate
    covered_bank = base["bank_total"] + bank_fee
    gap = covered_bank - expected
    within = abs(gap) <= abs(expected) * config.fx_tolerance_ratio
    same_direction = _sign(covered_bank) == _sign(expected) != 0
    status = AmountStatus.NEAR if within and same_direction else AmountStatus.MISMATCH
    covered = base["document_total"] if status is AmountStatus.NEAR else _ZERO
    return AmountCheck(
        status=status, covered=covered, fee=bank_fee,
        conversion_cost=(-gap).quantize(_CENT, rounding=ROUND_HALF_UP),
        fx_rate=divide(abs(covered_bank), abs(base["document_total"])), fx_declared=False,
        cost_estimated=True, **base,
    )  # fmt: skip


# --------------------------------------------------------------------------- labels

_DOC_LABELS: dict[DocumentType, tuple[str, str]] = {
    DocumentType.INVOICE: ("Invoice", "invoices"),
    DocumentType.INVOICE_RECEIPT: ("Invoice", "invoices"),
    DocumentType.SIMPLIFIED_INVOICE: ("Invoice", "invoices"),
    DocumentType.DEBIT_NOTE: ("Invoice", "invoices"),
    DocumentType.CREDIT_NOTE: ("Credit note", "credit notes"),
    DocumentType.RECEIPT: ("Receipt", "receipts"),
    DocumentType.STATEMENT: ("Statement", "statements"),
    DocumentType.TAX_NOTICE: ("Tax notice", "tax notices"),
    DocumentType.PAYROLL: ("Payroll", "payroll records"),
    DocumentType.LOAN_STATEMENT: ("Loan statement", "loan statements"),
}
_DEFAULT_LABEL = ("Document", "documents")


def document_label(docs: Sequence[Document]) -> tuple[str, str]:
    """(singular label, plural noun) for owner-facing text."""
    labels = {_DOC_LABELS.get(d.doc_type, _DEFAULT_LABEL) for d in docs}
    return labels.pop() if len(labels) == 1 else _DEFAULT_LABEL


def _amount_lines(
    check: AmountCheck, docs: Sequence[Document], n_tx: int, full: Decimal
) -> list[str]:
    singular, plural = document_label(docs)
    if len(docs) == 1:
        lines = [f"{singular} total: {_money(full, check.document_currency)}"]
        if full != check.document_total:
            lines.append(
                f"Still to pay: {_money(check.document_total, check.document_currency)}"
            )
    else:
        head = plural.capitalize()
        total = _money(check.document_total, check.document_currency)
        lines = [f"{head} total: {total} ({len(docs)} {plural})"]
    bank_money = _money(check.bank_total, check.bank_currency)
    if n_tx > 1:
        bank = "Bank charges" if check.bank_total < 0 else "Money received"
        lines.append(f"{bank}: {bank_money} ({n_tx} payments)")
    else:
        bank = "Bank charge" if check.bank_total < 0 else "Money received"
        lines.append(f"{bank}: {bank_money}")
    lines.extend(_adjustment_lines(check))
    return lines


def _adjustment_lines(check: AmountCheck) -> list[str]:
    lines: list[str] = []
    if check.is_fx and check.fx_rate is not None:
        rate = check.fx_rate.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
        lines.append(
            f"Exchange rate: 1 {check.document_currency} = {rate} {check.bank_currency}"
        )
    if check.fee:
        lines.append(f"Bank fees: {_money(check.fee, check.bank_currency)}")
    if check.conversion_cost:
        about = "about " if check.cost_estimated else ""
        label = (
            "Conversion cost" if check.conversion_cost > 0 else "Conversion difference"
        )
        lines.append(
            f"{label}: {about}{_money(check.conversion_cost, check.bank_currency)}"
        )
    if check.status is AmountStatus.NEAR and check.difference:
        lines.append(f"Difference: {_money(check.difference, check.document_currency)}")
    if check.status is AmountStatus.PARTIAL:
        lines.append(
            f"Paid now: {_money(check.covered, check.document_currency)}"
            f" of {_money(check.document_total, check.document_currency)}"
        )
    return lines


# --------------------------------------------------------------------------- factors


def _amount_factor(
    check: AmountCheck, docs, n_tx: int, full: Decimal, config: ScoringConfig
) -> Factor:
    w = config.weights
    lines = tuple(_amount_lines(check, docs, n_tx, full))
    if check.status is AmountStatus.EXACT:
        return Factor(FactorKind.AMOUNT, Outcome.SUPPORTS, w.amount_exact, lines)
    if check.status is AmountStatus.ADJUSTED:
        return Factor(FactorKind.AMOUNT, Outcome.SUPPORTS, w.amount_adjusted, lines)
    if check.status is AmountStatus.PARTIAL:
        return Factor(FactorKind.AMOUNT, Outcome.NEUTRAL, w.amount_partial, lines)
    if check.status is AmountStatus.NEAR:
        return Factor(
            FactorKind.AMOUNT, Outcome.NEUTRAL, _near_points(check, config), lines
        )
    return Factor(FactorKind.AMOUNT, Outcome.CONTRADICTS, 0, lines)


def _near_points(check: AmountCheck, config: ScoringConfig) -> int:
    """Closer is better.

    A document-currency difference earns full points within a quarter of the
    near tolerance. An FX conversion gap is judged against the FX tolerance,
    which already allows for normal spreads: within half of it earns full points.
    """
    best, middle, low = config.weights.amount_near
    if check.difference == 0 and check.conversion_cost is not None:
        gap, tolerance = (
            abs(check.conversion_cost),
            abs(check.bank_total) * config.fx_tolerance_ratio,
        )
        return best if gap * 2 <= tolerance else middle
    gap, tolerance = abs(check.difference), config.near_tolerance(check.document_total)
    if gap * 4 <= tolerance:
        return best
    return middle if gap * 2 <= tolerance else low


def _currency_factor(check: AmountCheck, w: ScoringWeights) -> Factor:
    """Same currency, or bridged by a declared or reference rate (the amount
    factor already carries how exact that bridge is)."""
    if check.status is AmountStatus.MISMATCH:
        return Factor(FactorKind.CURRENCY, Outcome.NEUTRAL, 0)
    return Factor(FactorKind.CURRENCY, Outcome.SUPPORTS, w.currency_same)


def _document_day(doc: Document) -> date | None:
    return doc.issue_date or doc.due_date


def _date_factor(
    txs: Sequence[Transaction],
    docs: Sequence[Document],
    config: ScoringConfig,
    allow_deposit: bool,
) -> tuple[Factor, int | None, bool]:
    """(factor, worst days apart, inside the window)."""
    gaps = [
        (t.booked_on - day).days
        for t in txs
        for d in docs
        if (day := _document_day(d)) is not None
    ]
    if not gaps:
        return Factor(FactorKind.DATES, Outcome.NEUTRAL, 0), None, True
    lead = config.deposit_window_days if allow_deposit else config.max_payment_lead_days
    inside = all(-lead <= g <= config.max_payment_lag_days for g in gaps)
    worst = max(abs(g) for g in gaps)
    phrase = days_phrase(worst)
    line = f"Dates: up to {phrase}" if len(gaps) > 1 and worst else f"Dates: {phrase}"
    if not inside:
        return Factor(FactorKind.DATES, Outcome.CONTRADICTS, 0, (line,)), worst, False
    points = next(
        (p for limit, p in config.weights.dates if worst <= limit),
        config.weights.dates_in_window,
    )
    return Factor(FactorKind.DATES, Outcome.SUPPORTS, points, (line,)), worst, True


def _due_date_factor(txs, docs, config: ScoringConfig) -> Factor | None:
    dues = [d.due_date for d in docs]
    if not dues or any(d is None for d in dues) or len(txs) != 1:
        return None
    paid = txs[0].booked_on
    if all(
        abs((paid - due).days) <= config.due_date_window_days for due in dues if due
    ):
        return Factor(
            FactorKind.DUE_DATE,
            Outcome.SUPPORTS,
            config.weights.due_date,
            ("Due date: matches",),
        )
    return None


def _relation(
    tx_sm: SupplierMatch, doc_sm: SupplierMatch, doc: Document, config: ScoringConfig
) -> SupplierRelation:
    """How the payee of a transaction relates to the issuer of a document."""
    if ResolveMethod.CONFLICT in (tx_sm.method, doc_sm.method):
        return SupplierRelation.UNCLEAR
    if tx_sm.is_known and doc_sm.is_known:
        return _known_relation(tx_sm, doc_sm, doc)
    payee = tx_sm.supplier
    if (
        payee is not None
        and doc.supplier_tax_id
        and payee.tax_id
        and not same_tax_id(doc.supplier_tax_id, payee.tax_id)
    ):
        return (
            SupplierRelation.DIFFERENT
            if tx_sm.is_strong
            else SupplierRelation.DIFFERENT_NAME
        )
    return _name_relation(tx_sm, doc_sm, config)


def supplier_relation(
    tx: Transaction, doc: Document, ctx: MatchContext
) -> SupplierRelation:
    """How the payee of ``tx`` relates to the issuer of ``doc`` (cached resolutions).

    Documents the business issued itself (sales invoices, payroll) name the
    business, not the counterparty (customer, employee), so the relation is
    UNKNOWN.
    """
    if is_own_document(doc, ctx.own_tax_ids):
        return SupplierRelation.UNKNOWN
    return _relation(
        ctx.supplier_of(tx), ctx.supplier_of_document(doc), doc, ctx.config
    )


def _known_relation(
    tx_sm: SupplierMatch, doc_sm: SupplierMatch, doc: Document
) -> SupplierRelation:
    strong = tx_sm.is_strong and doc_sm.is_strong
    if tx_sm.key != doc_sm.key:
        return SupplierRelation.DIFFERENT if strong else SupplierRelation.DIFFERENT_NAME
    if not strong:
        return SupplierRelation.SIMILAR
    supplier = tx_sm.supplier
    if (
        supplier is not None
        and doc.supplier_tax_id
        and same_tax_id(doc.supplier_tax_id, supplier.tax_id)
    ):
        return SupplierRelation.SAME_VAT
    return SupplierRelation.SAME


def _name_relation(
    tx_sm: SupplierMatch, doc_sm: SupplierMatch, config: ScoringConfig
) -> SupplierRelation:
    a = tx_sm.name_key if tx_sm.supplier is None else _supplier_key_text(tx_sm)
    b = doc_sm.name_key if doc_sm.supplier is None else _supplier_key_text(doc_sm)
    if not a or not b:
        return SupplierRelation.UNKNOWN
    similarity, kind = key_similarity(a, b)
    if kind in STRONG_NAME_KINDS:
        return SupplierRelation.SAME
    if similarity >= config.similar_name_threshold:
        return SupplierRelation.SIMILAR
    if similarity <= config.different_name_threshold:
        return SupplierRelation.DIFFERENT_NAME
    return SupplierRelation.UNKNOWN


def _supplier_key_text(sm: SupplierMatch) -> str:
    assert sm.supplier is not None
    return normalize_descriptor(sm.supplier.name).key


_SUPPLIER_LINES = {
    SupplierRelation.SAME_VAT: "Supplier VAT: matches",
    SupplierRelation.SAME: "Supplier: matches",
    SupplierRelation.SIMILAR: "Supplier name: similar",
    SupplierRelation.UNCLEAR: "Supplier: unclear",
    SupplierRelation.DIFFERENT_NAME: "Supplier name: different",
    SupplierRelation.DIFFERENT: "Supplier: different",
}


def _supplier_factor(txs, docs, ctx: MatchContext) -> tuple[Factor, SupplierRelation]:
    relations = [supplier_relation(t, d, ctx) for t in txs for d in docs]
    worst = min(relations, key=_RELATION_ORDER.index)
    w = ctx.config.weights
    points = {
        SupplierRelation.SAME_VAT: w.supplier_vat,
        SupplierRelation.SAME: w.supplier_same,
        SupplierRelation.SIMILAR: w.supplier_similar,
        SupplierRelation.DIFFERENT_NAME: w.supplier_different_name,
        SupplierRelation.DIFFERENT: w.supplier_different,
    }.get(worst, 0)
    outcome = (
        Outcome.SUPPORTS
        if points > 0
        else Outcome.CONTRADICTS
        if points < 0
        else Outcome.NEUTRAL
    )
    line = _SUPPLIER_LINES.get(worst)
    return Factor(FactorKind.SUPPLIER, outcome, points, (line,) if line else ()), worst


def identifier_in(identifier: str | None, texts: Sequence[tuple[str, str]]) -> bool:
    """True when a strong identifier stands alone in any (folded, squashed) text."""
    if not identifier:
        return False
    pattern = compile_identifier(identifier)
    if pattern is None:
        return False
    core = squash(identifier)
    return any(
        core in squashed and pattern.search(folded) for folded, squashed in texts
    )


def _reference_hit(
    doc: Document, txs: Sequence[Transaction], texts: Sequence[tuple[str, str]]
) -> bool:
    if not doc.payment_reference:
        return False
    wanted = squash(doc.payment_reference)
    if any(t.reference and squash(t.reference) == wanted for t in txs):
        return True
    return identifier_in(doc.payment_reference, texts)


def _identifier_factors(txs, docs, ctx: MatchContext) -> list[Factor]:
    texts = [ctx.bank_text(t) for t in txs]
    w = ctx.config.weights
    numbered = [d for d in docs if d.invoice_number]
    number_hits = sum(1 for d in numbered if identifier_in(d.invoice_number, texts))
    referenced = [d for d in docs if d.payment_reference]
    ref_hits = sum(1 for d in referenced if _reference_hit(d, txs, texts))
    factors: list[Factor] = []
    if number_hits:
        points = w.invoice_number if number_hits == len(docs) else w.invoice_number // 2
        factors.append(
            Factor(
                FactorKind.INVOICE_NUMBER,
                Outcome.SUPPORTS,
                points,
                (_number_line(number_hits, len(docs)),),
            )
        )
    if ref_hits:
        points = (
            w.payment_reference if ref_hits == len(docs) else w.payment_reference // 2
        )
        used = sum(f.points for f in factors)
        points = max(0, min(points, w.identifiers_cap - used))
        factors.append(
            Factor(
                FactorKind.PAYMENT_REFERENCE,
                Outcome.SUPPORTS,
                points,
                ("Payment reference: matches",),
            )
        )
    return factors


def _number_line(hits: int, total: int) -> str:
    if total == 1:
        return "Invoice number: found in the bank details"
    if hits == total:
        return "Invoice numbers: all found in the bank details"
    return f"Invoice numbers: {hits} of {total} found in the bank details"


def _bank_details_factor(txs, docs, ctx: MatchContext) -> Factor | None:
    """Invoice IBAN versus where the money went (§19, §26).

    Disagreement is a conflict unless both accounts are known for the supplier.
    The business's own documents print its own IBAN (where the customer should
    pay), never the counterparty's, so they are not compared.
    """
    pairs = [
        (normalize_iban(t.counterparty_iban), normalize_iban(d.iban), t)
        for t in txs
        for d in docs
        if t.counterparty_iban and d.iban and not is_own_document(d, ctx.own_tax_ids)
    ]
    if not pairs:
        return None
    w = ctx.config.weights
    if all(paid == invoiced for paid, invoiced, _ in pairs):
        return Factor(
            FactorKind.BANK_DETAILS,
            Outcome.SUPPORTS,
            w.bank_details_match,
            ("Bank details: match",),
        )
    for paid, invoiced, tx in pairs:
        supplier = ctx.supplier_of(tx).supplier
        known = {normalize_iban(i) for i in supplier.known_ibans} if supplier else set()
        if paid != invoiced and not {paid, invoiced} <= known:
            return Factor(
                FactorKind.BANK_DETAILS,
                Outcome.CONTRADICTS,
                w.bank_details_mismatch,
                ("Bank details: do not match",),
            )
    return Factor(FactorKind.BANK_DETAILS, Outcome.NEUTRAL, 0)


def _card_factor(txs, docs, ctx: MatchContext) -> Factor | None:
    cards = {t.card_last4 for t in txs if t.card_last4}
    printed = {v for d in docs if (v := document_card_last4(d))}
    if not cards or not printed:
        return None
    w = ctx.config.weights
    if printed <= cards:
        return Factor(
            FactorKind.CARD, Outcome.SUPPORTS, w.card_match, ("Card ending: matches",)
        )
    return Factor(
        FactorKind.CARD,
        Outcome.CONTRADICTS,
        w.card_mismatch,
        ("Card ending: does not match",),
    )


def document_card_last4(doc: Document) -> str | None:
    """Last four card digits printed on a document (``fields['card_last4']``), if trusted."""
    field_ = doc.fields.get(CARD_LAST4_FIELD)
    if field_ is None or field_.quality is Quality.RED or field_.value is None:
        return None
    digits = "".join(ch for ch in str(field_.value) if ch.isdigit())
    return digits[-4:] if len(digits) >= 4 else None


def _history_factor(txs, docs, ctx: MatchContext) -> Factor | None:
    if ctx.history is None or len(txs) != 1:
        return None
    tx = txs[0]
    keys = [ctx.supplier_of(tx).key, *(ctx.supplier_of_document(d).key for d in docs)]
    for key in dict.fromkeys(k for k in keys if k):
        pattern = ctx.history.pattern_for(key)
        if (
            pattern is not None
            and pattern.cadence is not None
            and pattern.fits(tx.booked_on, tx.amount)
        ):
            if (
                tx.card_last4
                and pattern.card_last4
                and tx.card_last4 not in pattern.card_last4
            ):
                continue
            line = f"Historical pattern: {pattern.cadence.value}"
            return Factor(
                FactorKind.HISTORY,
                Outcome.SUPPORTS,
                ctx.config.weights.history,
                (line,),
            )
    return None


# --------------------------------------------------------------------------- scoring


def score_match(
    transactions: Sequence[Transaction],
    documents: Sequence[Document],
    context: MatchContext,
    *,
    document_amounts: Mapping[str, Decimal] | None = None,
    allow_deposit: bool = False,
) -> CandidateScore:
    """Score transactions against documents (1-1, 1-many, many-1).

    ``document_amounts`` overrides each document's outstanding flow (after
    earlier partial payments). ``allow_deposit`` lets payments precede the
    document date by up to ``deposit_window_days``.
    """
    txs = sorted(transactions, key=lambda t: (t.booked_on, t.id))
    docs = sorted(documents, key=lambda d: (_document_day(d) or date.max, d.id))
    ids = (tuple(t.id for t in txs), tuple(d.id for d in docs))
    problem = _structural_problem(txs, docs, context)
    flows = {d.id: _outstanding(d, context, document_amounts) for d in docs}
    full = sum((context.flow(d) or _ZERO for d in docs), _ZERO)
    total = sum(flows.values(), _ZERO)
    if problem is None and (
        total == 0 or _sign(total) != _sign(sum((t.amount for t in txs), _ZERO))
    ):
        problem = "direction"
    if problem is not None:
        return _rejected(ids, txs, docs, total, problem)
    return _score(ids, txs, docs, total, full, context, allow_deposit)


def _structural_problem(txs, docs, ctx: MatchContext) -> str | None:
    if not txs or not docs:
        return "empty"
    codes = [t.currency for t in txs] + [d.currency for d in docs]
    if not all(is_currency_code(code) for code in codes):
        return "unknown currency"
    tx_codes = {currency_code(t.currency) for t in txs}
    if len(tx_codes) != 1 or len({currency_code(d.currency) for d in docs}) != 1:
        return "mixed currencies"
    if any(ctx.flow(d) is None for d in docs):
        return "document without total"
    return None


def _outstanding(
    doc: Document, ctx: MatchContext, overrides: Mapping[str, Decimal] | None
) -> Decimal:
    if overrides is not None and doc.id in overrides:
        return overrides[doc.id]
    return ctx.flow(doc) or _ZERO


def _rejected(ids, txs, docs, total: Decimal, reason: str) -> CandidateScore:
    currency = currency_code(docs[0].currency) if docs else "EUR"
    bank_currency = currency_code(txs[0].currency) if txs else currency
    bank_total = sum((t.amount for t in txs), _ZERO)
    check = AmountCheck(
        status=AmountStatus.MISMATCH, document_total=total, document_currency=currency,
        bank_total=bank_total, bank_currency=bank_currency, covered=_ZERO,
    )  # fmt: skip
    return CandidateScore(
        transaction_ids=ids[0], document_ids=ids[1], points=0, factors=(), amount=check,
        supplier=SupplierRelation.UNKNOWN, identity_confirmed=False, identifier_found=False,
        conflict=False, viable=False, days_apart=None, rejection=reason,
    )  # fmt: skip


def _score(
    ids, txs, docs, total, full, ctx: MatchContext, allow_deposit: bool
) -> CandidateScore:
    config, w = ctx.config, ctx.config.weights
    check = check_amount(txs, total, docs[0].currency, ctx)
    date_factor, days, in_window = _date_factor(txs, docs, config, allow_deposit)
    supplier_factor, relation = _supplier_factor(txs, docs, ctx)
    identifiers = _identifier_factors(txs, docs, ctx)
    bank_details = _bank_details_factor(txs, docs, ctx)
    optional = [_due_date_factor(txs, docs, config), supplier_factor, *identifiers, bank_details,
                _card_factor(txs, docs, ctx), _history_factor(txs, docs, ctx)]  # fmt: skip
    factors = (
        _amount_factor(check, docs, len(txs), full, config),
        _currency_factor(check, w),
        date_factor,
        *(f for f in optional if f is not None),
    )
    found = bool(identifiers)
    iban_match = bank_details is not None and bank_details.outcome is Outcome.SUPPORTS
    conflict = (
        bank_details is not None and bank_details.outcome is Outcome.CONTRADICTS
    ) or any(d.quality is Quality.RED for d in docs)
    rejection = _viability(check, relation, in_window, found)
    return CandidateScore(
        transaction_ids=ids[0],
        document_ids=ids[1],
        points=max(0, min(100, sum(f.points for f in factors))),
        factors=factors,
        amount=check,
        supplier=relation,
        identity_confirmed=relation
        in (SupplierRelation.SAME, SupplierRelation.SAME_VAT)
        or found
        or iban_match,
        identifier_found=found,
        conflict=conflict,
        viable=rejection is None,
        days_apart=days,
        rejection=rejection,
    )


def _viability(
    check: AmountCheck, relation: SupplierRelation, in_window: bool, found: bool
) -> str | None:
    if check.status is AmountStatus.MISMATCH:
        return "amounts differ"
    if check.status is AmountStatus.PARTIAL and not found:
        return "partial payment without a reference"
    if relation is SupplierRelation.DIFFERENT:
        return "different supplier"
    if not in_window and not found:
        return "outside the date window"
    return None


def grade(
    score: CandidateScore,
    documents: Sequence[Document],
    config: ScoringConfig,
    *,
    ambiguous: bool = False,
    search_complete: bool = True,
) -> Quality:
    """GREEN / AMBER / RED for a chosen candidate (§3, §19, §57).

    RED: bank details disagree or a document is itself in conflict.
    GREEN: score >= high threshold, identity confirmed, exact amounts after
    declared fees/FX, every document GREEN, nothing contradicts, not ambiguous
    and the search was exhaustive. Everything else is AMBER.
    """
    if score.conflict:
        return Quality.RED
    if ambiguous or not search_complete or score.contradicted:
        return Quality.AMBER
    if score.supplier is SupplierRelation.UNCLEAR:
        return Quality.AMBER
    green = (
        score.viable
        and score.points >= config.high_threshold
        and score.identity_confirmed
        and score.amount.exact
        and all(d.quality is Quality.GREEN for d in documents)
    )
    return Quality.GREEN if green else Quality.AMBER
