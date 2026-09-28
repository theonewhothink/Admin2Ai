"""Reconciliation engine (§20): every transaction tries to find its evidence.

:func:`reconcile` pairs bank/card transactions with documents and returns
:class:`Match` objects plus what is left unmatched. Supported shapes:

* 1 payment -> 1 document (invoice, receipt, tax notice, ...),
* 1 payment -> many documents of the same supplier (bounded subset sum;
  credit notes may offset invoices inside one payment),
* many payments -> 1 document (instalments, deposits, partial payments),
* refunds -> credit notes, card repayments -> card statements (and the card
  purchases they settle), FX with declared or reference rates, declared bank
  fees.

Phases run in a fixed order, strongest evidence first: card repayments, exact
1-1, exact 1-many, exact many-1, near 1-1, then reference-backed partial
payments. Every document and transaction carries a remaining amount, so nothing
is ever used twice beyond its value; a card purchase is linked to at most one
repayment.

Transactions that certainly need no document (a GREEN §21 decision) are not
matched. Likely ones (AMBER, e.g. fee wording) are matched like any other and
only excused when no document fits: a guess never hides real evidence.

Quality (§57): GREEN only when the score is high, the documents are GREEN, the
identity is confirmed and the amounts reconcile exactly after declared
fees/FX (card repayments included: they too need the high threshold). Two equally good candidates give AMBER with both listed (never a
silent pick). Bank details that disagree give RED (§19). Output order is
deterministic and independent of input order.
"""

from __future__ import annotations

import bisect
import hashlib
import logging
from collections import defaultdict
from functools import partial
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import TypeVar

from backoffice.domain.models import (
    Document,
    DocumentType,
    Quality,
    Transaction,
    TransactionKind,
)

from ._subset import find_subsets
from ._text import currency_code, format_money, is_currency_code, scale_to_int, squash
from .bank import BankMetadata, is_card_settlement
from .expected import EvidenceExpectation, ExpectationDecision
from .fx import FxRateSource, divide
from .history import PaymentHistory
from .scoring import (
    AmountStatus,
    CandidateScore,
    FactorKind,
    MatchContext,
    Outcome,
    ScoringConfig,
    SupplierRelation,
    document_card_last4,
    document_label,
    grade,
    identifier_in,
    is_sales_document,
    score_match,
    supplier_relation,
)
from .suppliers import SupplierResolver

__all__ = [
    "Allocation",
    "Alternative",
    "Match",
    "MatchKind",
    "MatchTag",
    "ReconciliationConfig",
    "ReconciliationResult",
    "reconcile",
]

logger = logging.getLogger(__name__)
_ZERO = Decimal(0)


# --------------------------------------------------------------------------- public types


class MatchKind(str, Enum):
    ONE_TO_ONE = "one_to_one"
    ONE_TO_MANY = "one_payment_many_documents"
    MANY_TO_ONE = "many_payments_one_document"
    PARTIAL = "partial_payment"
    CARD_SETTLEMENT = "card_settlement"


class MatchTag(str, Enum):
    REFUND = "refund"  # money back matched to a credit note
    CREDIT_NOTE_OFFSET = "credit_note_offset"  # a credit note netted inside one payment
    INSTALMENTS = "instalments"
    DEPOSIT = "deposit"  # a payment preceded the document
    FX = "fx"
    BANK_FEE = "bank_fee"
    NEAR_AMOUNT = "near_amount"
    AMBIGUOUS = "ambiguous"
    SEARCH_CAPPED = "search_capped"
    STATEMENT_MISSING = "statement_missing"


@dataclass(frozen=True)
class Allocation:
    """Part of a document settled by one transaction (signed flow, document currency)."""

    transaction_id: str
    document_id: str
    amount: Decimal


@dataclass(frozen=True)
class Alternative:
    """Another candidate that fitted (almost) as well; listed, never silently dropped."""

    transaction_ids: tuple[str, ...]
    document_ids: tuple[str, ...]
    points: int


@dataclass(frozen=True)
class Match:
    """A reconciliation decision with its evidence trail (§54, §55).

    ``headline`` and ``why`` are owner-facing plain language. ``fee`` and
    ``conversion_cost`` are in ``bank_currency``; ``difference`` (unexplained,
    near matches only) in the document ``currency``.
    """

    id: str
    kind: MatchKind
    quality: Quality
    points: int
    transaction_ids: tuple[str, ...]
    document_ids: tuple[str, ...]
    allocations: tuple[Allocation, ...]
    headline: str
    why: tuple[str, ...]
    tags: frozenset[MatchTag] = frozenset()
    alternatives: tuple[Alternative, ...] = ()
    currency: str = "EUR"
    bank_currency: str = "EUR"
    fee: Decimal = _ZERO
    conversion_cost: Decimal | None = None
    fx_rate: Decimal | None = None
    difference: Decimal = _ZERO
    settled_transaction_ids: tuple[str, ...] = ()
    score: CandidateScore | None = None

    @property
    def is_ambiguous(self) -> bool:
        return MatchTag.AMBIGUOUS in self.tags


@dataclass(frozen=True)
class ReconciliationConfig:
    """Engine bounds. The subset search is capped and says so when it is."""

    scoring: ScoringConfig = field(default_factory=ScoringConfig)
    max_subset_size: int = 6
    max_subset_pool: int = 20
    max_subset_solutions: int = 8
    max_search_nodes: int = 50_000
    settlement_lookback_days: int = 62

    def __post_init__(self) -> None:
        if self.max_subset_size < 2:
            raise ValueError("max_subset_size must be at least 2")
        if (
            min(self.max_subset_pool, self.max_subset_solutions, self.max_search_nodes)
            < 2
        ):
            raise ValueError("subset search bounds must be at least 2")
        if self.settlement_lookback_days < 1:
            raise ValueError("settlement_lookback_days must be positive")


@dataclass(frozen=True)
class ReconciliationResult:
    """Matches plus everything still open.

    * ``unmatched_transaction_ids``: need evidence (booking order).
    * ``unmatched_document_ids``: received no payment in this run.
    * ``open_balances``: documents partly paid (now or before) -> still to pay (signed flow).
    * ``no_document_needed``: transaction id -> plain reason (§21). Not chased.
    * ``likely_no_document``: the ids in ``no_document_needed`` that rest on
      wording alone (AMBER, §57). They were still offered every document, found
      none, and must not be closed without confirmation.
    * ``skipped``: id -> developer reason (zero amounts, no totals, already
      paid, unknown currency codes).
    * ``notes``: developer-facing log, e.g. capped searches. Never owner-facing.
    """

    matches: tuple[Match, ...]
    unmatched_transaction_ids: tuple[str, ...]
    unmatched_document_ids: tuple[str, ...]
    open_balances: Mapping[str, Decimal]
    no_document_needed: Mapping[str, str]
    skipped: Mapping[str, str]
    notes: tuple[str, ...]
    likely_no_document: tuple[str, ...] = ()

    def match_for_transaction(self, transaction_id: str) -> Match | None:
        return next(
            (m for m in self.matches if transaction_id in m.transaction_ids), None
        )

    def matches_for_document(self, document_id: str) -> tuple[Match, ...]:
        return tuple(m for m in self.matches if document_id in m.document_ids)

    def count(self, quality: Quality) -> int:
        return sum(1 for m in self.matches if m.quality is quality)


# --------------------------------------------------------------------------- entry point


def reconcile(
    transactions: Iterable[Transaction],
    documents: Iterable[Document],
    config: ReconciliationConfig | None = None,
    *,
    suppliers: SupplierResolver | None = None,
    bank_metadata: Mapping[str, BankMetadata] | None = None,
    history: PaymentHistory | None = None,
    fx_rates: FxRateSource | None = None,
    own_tax_ids: Iterable[str] = (),
    expectations: Mapping[str, ExpectationDecision] | None = None,
    document_balances: Mapping[str, Decimal] | None = None,
) -> ReconciliationResult:
    """Match transactions to documents (§20). Deterministic; ids must be unique.

    ``expectations`` (from :class:`~backoffice.reconciliation.expected.ExpectedEvidenceEngine`)
    lets transactions that certainly need no document (GREEN) skip matching;
    likely ones (AMBER) are still matched first. ``document_balances`` carries
    what is still to pay on documents partly paid in earlier runs.
    """
    config = config or ReconciliationConfig()
    context = MatchContext(
        suppliers=suppliers,
        bank_metadata=bank_metadata,
        history=history,
        fx_rates=fx_rates,
        own_tax_ids=own_tax_ids,
        config=config.scoring,
    )
    run = _Run(list(transactions), list(documents), config, context)
    run.load(expectations or {}, document_balances or {})
    return run.execute()


# --------------------------------------------------------------------------- the run

_EXACT = frozenset({AmountStatus.EXACT, AmountStatus.ADJUSTED})
_STATUS_RANK = {
    AmountStatus.EXACT: 0,
    AmountStatus.ADJUSTED: 1,
    AmountStatus.NEAR: 2,
    AmountStatus.PARTIAL: 3,
    AmountStatus.MISMATCH: 4,
}
_SAME_SUPPLIER = frozenset(
    {SupplierRelation.SAME_VAT, SupplierRelation.SAME, SupplierRelation.SIMILAR}
)
_Item = TypeVar("_Item", Transaction, Document)


def _tx_key(tx: Transaction) -> tuple[date, str]:
    return (tx.booked_on, tx.id)


def _doc_day(doc: Document) -> date | None:
    return doc.issue_date or doc.due_date


def _doc_key(doc: Document) -> tuple[date, str]:
    return (_doc_day(doc) or date.max, doc.id)


def _sign(value: Decimal) -> int:
    return (value > 0) - (value < 0)


def _check_unique(items: Sequence, what: str) -> None:
    seen: set[str] = set()
    for item in items:
        if item.id in seen:
            raise ValueError(f"duplicate {what} id {item.id!r}")
        seen.add(item.id)


class _AmountIndex:
    """Open documents sorted by absolute outstanding amount, per currency."""

    def __init__(
        self, entries: Iterable[tuple[Document, Decimal]], order: Mapping[str, int]
    ) -> None:
        buckets: dict[str, list[tuple[Decimal, int, Document]]] = defaultdict(list)
        for doc, remaining in entries:
            buckets[currency_code(doc.currency)].append(
                (abs(remaining), order[doc.id], doc)
            )
        self._amounts: dict[str, list[Decimal]] = {}
        self._docs: dict[str, list[Document]] = {}
        for currency, bucket in buckets.items():
            bucket.sort(key=lambda e: (e[0], e[1]))
            self._amounts[currency] = [e[0] for e in bucket]
            self._docs[currency] = [e[2] for e in bucket]

    def currencies(self) -> list[str]:
        return sorted(self._amounts)

    def around(
        self, currency: str, amount: Decimal, tolerance: Decimal
    ) -> list[Document]:
        amounts = self._amounts.get(currency_code(currency))
        if not amounts:
            return []
        lo = bisect.bisect_left(amounts, amount - tolerance)
        hi = bisect.bisect_right(amounts, amount + tolerance)
        return self._docs[currency][lo:hi]


class _Run:
    """One reconciliation pass with its ledger of remaining amounts."""

    def __init__(
        self,
        transactions: list[Transaction],
        documents: list[Document],
        config: ReconciliationConfig,
        context: MatchContext,
    ) -> None:
        _check_unique(transactions, "transaction")
        _check_unique(documents, "document")
        self.config = config
        self.scoring = config.scoring
        self.ctx = context
        self.all_txs = sorted(transactions, key=_tx_key)
        self.tx_order = {t.id: i for i, t in enumerate(self.all_txs)}
        self.docs = {d.id: d for d in sorted(documents, key=_doc_key)}
        self.doc_order = {doc_id: i for i, doc_id in enumerate(self.docs)}
        self.open_txs: dict[str, Transaction] = {}
        self.settlements: dict[str, Transaction] = {}
        self.full: dict[str, Decimal] = {}
        self.remaining: dict[str, Decimal] = {}
        self.touched: set[str] = set()
        # Ids taken by an ambiguous match or its rivals: later forced picks stay flagged.
        self.contested: set[str] = set()
        self.matches: list[Match] = []
        self.notes: list[str] = []
        self.skipped: dict[str, str] = {}
        self.no_document_needed: dict[str, str] = {}
        # AMBER "no document needed" guesses: matched first, excused only if nothing fits.
        self.likely_no_document: dict[str, str] = {}
        # Card purchases already linked to a repayment: a purchase is repaid once.
        self.settled_claimed: set[str] = set()

    # ------------------------------------------------------------------ loading

    def load(
        self,
        expectations: Mapping[str, ExpectationDecision],
        balances: Mapping[str, Decimal],
    ) -> None:
        for tx in self.all_txs:
            self._load_transaction(tx, expectations.get(tx.id))
        for doc in self.docs.values():
            self._load_document(doc, balances.get(doc.id))

    def _load_transaction(
        self, tx: Transaction, decision: ExpectationDecision | None
    ) -> None:
        if tx.amount == 0:
            self.skipped[tx.id] = "zero amount"
            return
        if not is_currency_code(tx.currency):
            self.skipped[tx.id] = "unknown currency"
            return
        if decision is not None and not decision.requires_document:
            if decision.quality is Quality.GREEN:
                self.no_document_needed[tx.id] = decision.reason
                return
            # A wording-based guess (AMBER, §57) must not hide real evidence.
            self.likely_no_document[tx.id] = decision.reason
        if is_card_settlement(tx, self.ctx.metadata(tx)) or (
            decision is not None
            and decision.expectation is EvidenceExpectation.CARD_STATEMENT
        ):
            self.settlements[tx.id] = tx
        else:
            self.open_txs[tx.id] = tx

    def _load_document(self, doc: Document, balance: Decimal | None) -> None:
        if not is_currency_code(doc.currency):
            self.skipped[doc.id] = "unknown currency"
            return
        flow = self.ctx.flow(doc)
        if flow is None or flow == 0:
            self.skipped[doc.id] = "no total" if flow is None else "zero total"
            return
        outstanding = flow if balance is None else balance
        if outstanding and (
            _sign(outstanding) != _sign(flow) or abs(outstanding) > abs(flow)
        ):
            raise ValueError(f"balance for document {doc.id!r} does not fit its total")
        if outstanding == 0:
            self.skipped[doc.id] = "already paid"
            return
        self.full[doc.id] = flow
        self.remaining[doc.id] = outstanding

    # ------------------------------------------------------------------ phases

    def execute(self) -> ReconciliationResult:
        self._card_settlements()
        self._one_to_one(exact=True)
        self._one_to_many()
        self._many_to_one()
        self._one_to_one(exact=False)
        self._partials()
        return self._result()

    def _one_to_one(self, *, exact: bool) -> None:
        """Global greedy 1-1 assignment, best evidence first, ties flagged (§20)."""
        index = _AmountIndex(
            ((d, self.remaining[d.id]) for d in self._open_docs()), self.doc_order
        )
        wanted = _EXACT if exact else frozenset({AmountStatus.NEAR})
        scored: list[CandidateScore] = []
        for tx in list(self.open_txs.values()):
            for doc in self._amount_candidates(tx, index, exact):
                score = self._score([tx], [doc])
                if (
                    score.viable
                    and score.amount.status in wanted
                    and score.points >= self.scoring.plausible_threshold
                ):
                    scored.append(score)
        by_tx: dict[str, list[CandidateScore]] = defaultdict(list)
        by_doc: dict[str, list[CandidateScore]] = defaultdict(list)
        for score in scored:
            by_tx[score.transaction_ids[0]].append(score)
            by_doc[score.document_ids[0]].append(score)
        for score in sorted(scored, key=self._rank):
            if self._available(score):
                pool = by_tx[score.transaction_ids[0]] + by_doc[score.document_ids[0]]
                self._record(MatchKind.ONE_TO_ONE, score, self._rivals(score, pool))

    def _one_to_many(self) -> None:
        """One payment for several documents of the same supplier (exact subset sum)."""
        for tx in list(self.open_txs.values()):
            for currency, target in self._targets(tx):
                pool, pool_capped = self._document_pool(tx, currency)
                if len(pool) < 2:
                    continue
                found, capped = self._search(
                    pool, [self.remaining[d.id] for d in pool], target,
                    partial(self._score_payment, tx), f"transaction {tx.id}",
                )  # fmt: skip
                if found:
                    best, rivals = self._best(found)
                    self._record(
                        MatchKind.ONE_TO_MANY,
                        best,
                        rivals,
                        capped=capped or pool_capped,
                    )
                    break

    def _many_to_one(self) -> None:
        """Several payments for one document: instalments, deposits (exact subset sum)."""
        for doc in self._open_docs():
            if doc.doc_type is DocumentType.STATEMENT or self.remaining[doc.id] == 0:
                continue
            pool, pool_capped = self._transaction_pool(doc)
            if len(pool) < 2:
                continue
            values = [self._contribution(t, doc) for t in pool]
            found, capped = self._search(
                pool, values, self.remaining[doc.id],
                partial(self._score_instalments, doc), f"document {doc.id}",
            )  # fmt: skip
            if found:
                best, rivals = self._best(found)
                self._record(
                    MatchKind.MANY_TO_ONE, best, rivals, capped=capped or pool_capped
                )

    def _partials(self) -> None:
        """Payments that quote a document's number but pay only part of it."""
        for tx in list(self.open_txs.values()):
            candidates: list[CandidateScore] = []
            for doc in self._open_docs():
                if not self._references(tx, doc):
                    continue
                score = self._score([tx], [doc], deposit=True)
                acceptable = (
                    score.amount.status is AmountStatus.PARTIAL or score.amount.exact
                )
                if (
                    score.viable
                    and acceptable
                    and score.points >= self.scoring.partial_threshold
                ):
                    candidates.append(score)
            if candidates:
                best, rivals = self._best(candidates)
                kind = (
                    MatchKind.PARTIAL
                    if best.amount.status is AmountStatus.PARTIAL
                    else MatchKind.ONE_TO_ONE
                )
                self._record(kind, best, rivals)

    # ------------------------------------------------------------------ card repayments

    def _card_settlements(self) -> None:
        """Card repayments: find the card statement and the purchases they settle."""
        for tx in list(self.settlements.values()):
            meta = self.ctx.metadata(tx)
            settled, settled_ambiguous = self._settled_purchases(tx, meta)
            statement = self._statement_for(tx, meta)
            if statement is not None:
                self._record_settlement(tx, meta, statement, settled, settled_ambiguous)
            elif settled:
                self._record_settlement_only(tx, settled, settled_ambiguous)

    def _statement_for(
        self, tx: Transaction, meta: BankMetadata
    ) -> tuple[CandidateScore, list[CandidateScore]] | None:
        candidates = []
        for doc in self._open_docs():
            if doc.doc_type is not DocumentType.STATEMENT:
                continue
            printed = document_card_last4(doc)
            if (
                printed
                and meta.settles_card_last4
                and printed != meta.settles_card_last4
            ):
                continue
            score = self._score([tx], [doc])
            # Supplier names are ignored here: the bank and the card issuer are named differently.
            if score.amount.exact and not _dates_contradicted(score):
                candidates.append(score)
        return self._best(candidates) if candidates else None

    def _settled_purchases(
        self, tx: Transaction, meta: BankMetadata
    ) -> tuple[tuple[str, ...], bool]:
        """Card purchases whose total equals the repayment: (ids, ambiguous)."""
        card = meta.settles_card_last4
        pool = [
            t for t in self.all_txs
            if t.kind is TransactionKind.CARD and t.card_last4 and t.id != tx.id
            and t.account_id != tx.account_id and t.booked_on <= tx.booked_on
            and (card is None or t.card_last4 == card)
            and t.id not in self.settled_claimed
        ]  # fmt: skip
        if meta.settlement_period is not None:
            start, end = meta.settlement_period
            chosen = [t for t in pool if start <= t.booked_on <= end]
            total = sum((t.amount for t in chosen), _ZERO)
            return (
                (tuple(t.id for t in chosen), False)
                if chosen and total == tx.amount
                else ((), False)
            )
        earliest = tx.booked_on - timedelta(days=self.config.settlement_lookback_days)
        solutions: list[tuple[str, ...]] = []
        for digits in sorted({t.card_last4 for t in pool if t.card_last4}):
            group = [
                t for t in pool if t.card_last4 == digits and t.booked_on >= earliest
            ]
            solutions.extend(_day_windows(group, tx.amount))
        if not solutions:
            return (), False
        return solutions[0], len(solutions) > 1

    def _record_settlement(
        self,
        tx: Transaction,
        meta: BankMetadata,
        found: tuple[CandidateScore, list[CandidateScore]],
        settled: tuple[str, ...],
        settled_ambiguous: bool,
    ) -> None:
        score, rivals = found
        doc = self.docs[score.document_ids[0]]
        card_match = (
            bool(meta.settles_card_last4)
            and document_card_last4(doc) == meta.settles_card_last4
        )
        extra = _settlement_lines(card_match, settled, settled_ambiguous)
        kept = [
            f for f in score.factors if f.kind is not FactorKind.SUPPLIER
        ]  # bank vs issuer names differ by nature
        why = tuple(line for f in kept for line in f.why) + extra
        w = self.scoring.weights
        points = max(
            0,
            min(
                100,
                sum(f.points for f in kept)
                + (w.settlement_card if card_match else 0)
                + (w.settlement_purchases if settled else 0),
            ),
        )
        identity = (
            card_match
            or (bool(settled) and not settled_ambiguous)
            or score.identifier_found
        )
        contradicted = any(f.outcome is Outcome.CONTRADICTS for f in kept)
        if score.conflict:
            quality = Quality.RED
        elif (
            identity
            and points >= self.scoring.high_threshold
            and not rivals
            and not settled_ambiguous
            and not contradicted
            and doc.quality is Quality.GREEN
        ):
            quality = Quality.GREEN
        else:
            quality = Quality.AMBER
        self._record(
            MatchKind.CARD_SETTLEMENT, score, rivals, quality=quality, points=points, why=why,
            settled=settled, ambiguous=settled_ambiguous,
        )  # fmt: skip

    def _record_settlement_only(
        self, tx: Transaction, settled: tuple[str, ...], ambiguous: bool
    ) -> None:
        why: tuple[str, ...] = (
            f"Card repayment: {format_money(abs(tx.amount), tx.currency)}",
            *_settlement_lines(False, settled, ambiguous),
        )
        tags = {MatchTag.STATEMENT_MISSING} | (
            {MatchTag.AMBIGUOUS} if ambiguous else set()
        )
        self.settlements.pop(tx.id, None)
        self.settled_claimed.update(settled)
        self.matches.append(
            Match(
                id=_match_id(MatchKind.CARD_SETTLEMENT, (tx.id,), settled),
                kind=MatchKind.CARD_SETTLEMENT,
                quality=Quality.AMBER,  # the statement itself is still missing (§3)
                points=0,
                transaction_ids=(tx.id,),
                document_ids=(),
                allocations=(),
                headline="Card repayment. I still need the card statement.",
                why=why,
                tags=frozenset(tags),
                currency=currency_code(tx.currency),
                bank_currency=currency_code(tx.currency),
                settled_transaction_ids=settled,
            )
        )

    # ------------------------------------------------------------------ candidates and pools

    def _open_docs(self) -> list[Document]:
        return [d for d in self.docs.values() if self.remaining.get(d.id, _ZERO) != 0]

    def _score(
        self,
        txs: Sequence[Transaction],
        docs: Sequence[Document],
        *,
        deposit: bool = False,
    ) -> CandidateScore:
        amounts = {d.id: self.remaining[d.id] for d in docs}
        return score_match(
            txs, docs, self.ctx, document_amounts=amounts, allow_deposit=deposit
        )

    def _score_payment(
        self, tx: Transaction, docs: Sequence[Document]
    ) -> CandidateScore:
        """One payment against several documents (subset search builder)."""
        return self._score([tx], docs)

    def _score_instalments(
        self, doc: Document, txs: Sequence[Transaction]
    ) -> CandidateScore:
        """Several payments, possibly before the document, against one document."""
        return self._score(txs, [doc], deposit=True)

    def _amount_candidates(
        self, tx: Transaction, index: _AmountIndex, exact: bool
    ) -> list[Document]:
        cfg = self.scoring
        tolerance = _ZERO if exact else cfg.near_amount_max
        paid = abs(tx.amount + self.ctx.metadata(tx).fee)
        bank_currency = currency_code(tx.currency)
        found = list(index.around(bank_currency, paid, tolerance))
        fx = self.ctx.fx_details(tx)
        if fx is not None and fx.original_currency != bank_currency:
            found += index.around(fx.original_currency, fx.original_amount, tolerance)
        if not exact:
            for currency in index.currencies():
                if currency == bank_currency or (
                    fx is not None and currency == fx.original_currency
                ):
                    continue
                rate = self.ctx.reference_rate(currency, bank_currency, tx.booked_on)
                if rate:
                    approx = divide(paid, rate)
                    found += index.around(
                        currency,
                        approx,
                        approx * cfg.fx_tolerance_ratio + cfg.near_amount_abs,
                    )
        unique = {d.id: d for d in found}
        return sorted(unique.values(), key=lambda d: self.doc_order[d.id])

    def _targets(self, tx: Transaction) -> list[tuple[str, Decimal]]:
        """(document currency, signed amount to cover) this payment can settle."""
        bank_currency = currency_code(tx.currency)
        targets = [(bank_currency, tx.amount + self.ctx.metadata(tx).fee)]
        fx = self.ctx.fx_details(tx)
        if fx is not None and fx.original_currency != bank_currency:
            targets.append(
                (fx.original_currency, _sign(tx.amount) * fx.original_amount)
            )
        return targets

    def _contribution(self, tx: Transaction, doc: Document) -> Decimal:
        """What ``tx`` pays towards ``doc``, in the document currency (signed flow)."""
        if currency_code(tx.currency) == currency_code(doc.currency):
            return tx.amount + self.ctx.metadata(tx).fee
        fx = self.ctx.fx_details(tx)
        if fx is None or fx.original_currency != currency_code(doc.currency):
            raise ValueError("transaction cannot be expressed in the document currency")
        return _sign(tx.amount) * fx.original_amount

    def _convertible(self, tx: Transaction, doc: Document) -> bool:
        if currency_code(tx.currency) == currency_code(doc.currency):
            return True
        fx = self.ctx.fx_details(tx)
        return fx is not None and fx.original_currency == currency_code(doc.currency)

    def _references(self, tx: Transaction, doc: Document) -> bool:
        texts = [self.ctx.bank_text(tx)]
        if identifier_in(doc.invoice_number, texts) or identifier_in(
            doc.payment_reference, texts
        ):
            return True
        return bool(
            doc.payment_reference
            and tx.reference
            and squash(doc.payment_reference) == squash(tx.reference)
        )

    def _document_pool(
        self, tx: Transaction, currency: str
    ) -> tuple[list[Document], bool]:
        lag, lead = (
            self.scoring.max_payment_lag_days,
            self.scoring.max_payment_lead_days,
        )
        pool: list[tuple[int, Document]] = []
        for doc in self._open_docs():
            if (
                currency_code(doc.currency) != currency
                or doc.doc_type is DocumentType.STATEMENT
            ):
                continue
            day = _doc_day(doc)
            gap = (tx.booked_on - day).days if day else None
            if not self._references(tx, doc):
                if gap is None or not -lead <= gap <= lag:
                    continue
                if supplier_relation(tx, doc, self.ctx) not in _SAME_SUPPLIER:
                    continue
            pool.append((abs(gap) if gap is not None else 0, doc))
        return self._cap_pool(pool, self.doc_order, f"transaction {tx.id}")

    def _transaction_pool(self, doc: Document) -> tuple[list[Transaction], bool]:
        day = _doc_day(doc)
        lag, early = self.scoring.max_payment_lag_days, self.scoring.deposit_window_days
        pool: list[tuple[int, Transaction]] = []
        for tx in self.open_txs.values():
            if not self._convertible(tx, doc):
                continue
            gap = (tx.booked_on - day).days if day else None
            if not self._references(tx, doc):
                if gap is None or not -early <= gap <= lag:
                    continue
                if supplier_relation(tx, doc, self.ctx) not in _SAME_SUPPLIER:
                    continue
            pool.append((abs(gap) if gap is not None else 0, tx))
        return self._cap_pool(pool, self.tx_order, f"document {doc.id}")

    def _cap_pool(
        self, pool: list[tuple[int, _Item]], order: Mapping[str, int], label: str
    ) -> tuple[list[_Item], bool]:
        """Keep the items closest in date; say so when some were dropped."""
        pool.sort(key=lambda entry: (entry[0], order[entry[1].id]))
        capped = len(pool) > self.config.max_subset_pool
        if capped:
            self._note(
                f"combination pool for {label} cut to {self.config.max_subset_pool} closest items"
            )
        kept = [item for _, item in pool[: self.config.max_subset_pool]]
        return sorted(kept, key=lambda item: order[item.id]), capped

    def _search(
        self,
        items: list,
        values: list[Decimal],
        target: Decimal,
        build: Callable[[list], CandidateScore],
        label: str,
    ) -> tuple[list[CandidateScore], bool]:
        """Exact subset sums over ``items``, scored; (viable candidates, search incomplete)."""
        nonzero = [
            (item, value)
            for item, value in zip(items, values, strict=True)
            if value != 0
        ]
        ints, _ = scale_to_int([v for _, v in nonzero] + [target])
        target_int = ints.pop()
        search = find_subsets(
            ints, target_int,
            min_size=2, max_size=self.config.max_subset_size,
            max_solutions=self.config.max_subset_solutions, node_limit=self.config.max_search_nodes,
        )  # fmt: skip
        if search.capped:
            self._note(f"subset search for {label} stopped after {search.nodes} steps")
        elif search.truncated:
            self._note(
                f"subset search for {label} stopped after {len(search.solutions)} combinations"
            )
        scores = [
            build([nonzero[i][0] for i in solution]) for solution in search.solutions
        ]
        viable = [
            s for s in scores
            if s.viable and s.amount.exact and s.points >= self.scoring.plausible_threshold
        ]  # fmt: skip
        return viable, not search.complete

    # ------------------------------------------------------------------ choosing

    def _rank(self, score: CandidateScore) -> tuple:
        days = score.days_apart if score.days_apart is not None else 10**6
        return (
            -score.points,
            _STATUS_RANK[score.amount.status],
            days,
            len(score.transaction_ids) + len(score.document_ids),
            tuple(self.tx_order[t] for t in score.transaction_ids),
            tuple(self.doc_order[d] for d in score.document_ids),
        )

    def _available(self, score: CandidateScore) -> bool:
        return all(
            t in self.open_txs or t in self.settlements for t in score.transaction_ids
        ) and all(self.remaining.get(d, _ZERO) != 0 for d in score.document_ids)

    def _rivals(
        self, best: CandidateScore, pool: Iterable[CandidateScore]
    ) -> list[CandidateScore]:
        """Candidates within the ambiguity margin that are still open, or that were
        taken by an earlier ambiguous choice (so a forced second pick stays flagged)."""
        margin = self.scoring.ambiguity_margin
        rivals = [
            s for s in pool
            if s is not best and s.points >= best.points - margin
            and (self._available(s) or self._entangled(s))
        ]  # fmt: skip
        return sorted(rivals, key=self._rank)

    def _entangled(self, score: CandidateScore) -> bool:
        return any(
            i in self.contested for i in (*score.transaction_ids, *score.document_ids)
        )

    def _best(
        self, scores: list[CandidateScore]
    ) -> tuple[CandidateScore, list[CandidateScore]]:
        ordered = sorted(scores, key=self._rank)
        return ordered[0], self._rivals(ordered[0], ordered[1:])

    # ------------------------------------------------------------------ recording

    def _record(
        self,
        kind: MatchKind,
        score: CandidateScore,
        rivals: Sequence[CandidateScore] = (),
        *,
        capped: bool = False,
        quality: Quality | None = None,
        points: int | None = None,
        why: tuple[str, ...] | None = None,
        settled: tuple[str, ...] = (),
        ambiguous: bool = False,
    ) -> None:
        txs = [self._transaction(t) for t in score.transaction_ids]
        docs = [self.docs[d] for d in score.document_ids]
        ambiguous = ambiguous or bool(rivals)
        if rivals:
            for item in (score, *rivals):
                self.contested.update(item.transaction_ids, item.document_ids)
        if quality is None:
            quality = grade(
                score,
                docs,
                self.scoring,
                ambiguous=ambiguous,
                search_complete=not capped,
            )
        allocations = self._allocations(kind, score, txs, docs)
        self.settled_claimed.update(settled)
        for tx in txs:
            self.open_txs.pop(tx.id, None)
            self.settlements.pop(tx.id, None)
        for allocation in allocations:
            self.remaining[allocation.document_id] -= allocation.amount
            self.touched.add(allocation.document_id)
        tags = _tags(kind, score, txs, docs, (ambiguous, capped), self.ctx.own_tax_ids)
        check = score.amount
        self.matches.append(
            Match(
                id=_match_id(kind, score.transaction_ids, score.document_ids),
                kind=kind,
                quality=quality,
                points=score.points if points is None else points,
                transaction_ids=score.transaction_ids,
                document_ids=score.document_ids,
                allocations=allocations,
                headline=_headline(
                    kind,
                    quality,
                    tags,
                    score,
                    docs,
                    rivals,
                    self._still_open(docs),
                    [self.docs[d] for r in rivals for d in r.document_ids],
                ),
                why=score.why if why is None else why,
                tags=tags,
                alternatives=tuple(
                    Alternative(r.transaction_ids, r.document_ids, r.points)
                    for r in rivals
                ),
                currency=check.document_currency,
                bank_currency=check.bank_currency,
                fee=check.fee,
                conversion_cost=check.conversion_cost,
                fx_rate=check.fx_rate if check.is_fx else None,
                difference=check.difference
                if check.status is AmountStatus.NEAR
                else _ZERO,
                settled_transaction_ids=settled,
                score=score,
            )
        )

    def _allocations(
        self, kind, score: CandidateScore, txs, docs
    ) -> tuple[Allocation, ...]:
        if kind is MatchKind.MANY_TO_ONE or score.amount.status is AmountStatus.PARTIAL:
            doc = docs[0]
            return tuple(
                Allocation(t.id, doc.id, self._contribution(t, doc)) for t in txs
            )
        tx = txs[0]  # one payment settles each document's outstanding amount
        return tuple(Allocation(tx.id, d.id, self.remaining[d.id]) for d in docs)

    def _still_open(self, docs: Sequence[Document]) -> Decimal:
        return sum((self.remaining[d.id] for d in docs), _ZERO)

    def _transaction(self, tx_id: str) -> Transaction:
        return self.all_txs[self.tx_order[tx_id]]

    def _note(self, message: str) -> None:
        logger.warning("reconciliation: %s", message)
        self.notes.append(message)

    # ------------------------------------------------------------------ result

    def _result(self) -> ReconciliationResult:
        left = sorted(
            [*self.open_txs, *self.settlements], key=self.tx_order.__getitem__
        )
        likely = tuple(t for t in left if t in self.likely_no_document)
        left = [t for t in left if t not in self.likely_no_document]
        excused = {**self.no_document_needed}
        excused.update((t, self.likely_no_document[t]) for t in likely)
        unmatched_docs = tuple(
            d
            for d in self.docs
            if self.remaining.get(d, _ZERO) != 0 and d not in self.touched
        )
        balances = {
            d: r for d, r in self.remaining.items() if r != 0 and r != self.full[d]
        }
        matches = sorted(self.matches, key=self._match_key)
        return ReconciliationResult(
            matches=tuple(matches),
            unmatched_transaction_ids=tuple(left),
            unmatched_document_ids=unmatched_docs,
            open_balances=MappingProxyType(
                dict(sorted(balances.items(), key=lambda kv: self.doc_order[kv[0]]))
            ),
            no_document_needed=MappingProxyType(
                dict(sorted(excused.items(), key=lambda kv: self.tx_order[kv[0]]))
            ),
            skipped=MappingProxyType(dict(self.skipped)),
            notes=tuple(self.notes),
            likely_no_document=likely,
        )

    def _match_key(self, match: Match) -> tuple:
        return (min(self.tx_order[t] for t in match.transaction_ids), match.id)


# --------------------------------------------------------------------------- helpers


def _match_id(kind: MatchKind, tx_ids: Sequence[str], doc_ids: Sequence[str]) -> str:
    digest = hashlib.sha256(
        f"{kind.value}|{','.join(tx_ids)}|{','.join(doc_ids)}".encode()
    ).hexdigest()
    return f"match_{digest[:16]}"


def _day_windows(
    group: Sequence[Transaction], target: Decimal
) -> list[tuple[str, ...]]:
    """Whole-day windows of card purchases whose total equals ``target``."""
    days: dict[date, list[Transaction]] = defaultdict(list)
    for tx in sorted(group, key=_tx_key):
        days[tx.booked_on].append(tx)
    ordered = sorted(days)
    totals = [sum((t.amount for t in days[d]), _ZERO) for d in ordered]
    windows: list[tuple[str, ...]] = []
    for i in range(len(ordered)):
        running = _ZERO
        for j in range(i, len(ordered)):
            running += totals[j]
            if running == target:
                windows.append(tuple(t.id for d in ordered[i : j + 1] for t in days[d]))
    return windows


def _dates_contradicted(score: CandidateScore) -> bool:
    return any(
        f.kind is FactorKind.DATES and f.outcome is Outcome.CONTRADICTS
        for f in score.factors
    )


def _settlement_lines(
    card_match: bool, settled: tuple[str, ...], ambiguous: bool
) -> tuple[str, ...]:
    lines: list[str] = []
    if card_match:
        lines.append("Card ending: matches")
    if settled and ambiguous:
        lines.append("Card purchases: more than one period adds up")
    elif settled:
        count = len(settled)
        noun = "purchase adds" if count == 1 else "purchases add"
        lines.append(f"Card purchases: {count} {noun} up exactly")
    return tuple(lines)


def _tags(
    kind: MatchKind,
    score: CandidateScore,
    txs: Sequence[Transaction],
    docs: Sequence[Document],
    flags: tuple[bool, bool],
    own_tax_ids: Sequence[str],
) -> frozenset[MatchTag]:
    """Machine-readable facts about a match. ``flags`` = (ambiguous, capped)."""
    ambiguous, capped = flags
    check = score.amount
    candidates = {
        MatchTag.FX: check.is_fx,
        MatchTag.BANK_FEE: bool(check.fee),
        MatchTag.NEAR_AMOUNT: check.status is AmountStatus.NEAR,
        MatchTag.AMBIGUOUS: ambiguous,
        MatchTag.SEARCH_CAPPED: capped,
        MatchTag.INSTALMENTS: kind is MatchKind.MANY_TO_ONE,
        MatchTag.DEPOSIT: _paid_before_issue(kind, txs, docs),
    }
    tags = {tag for tag, present in candidates.items() if present}
    return frozenset(tags | _credit_note_tags(txs, docs, own_tax_ids))


def _paid_before_issue(
    kind: MatchKind, txs: Sequence[Transaction], docs: Sequence[Document]
) -> bool:
    if kind not in (MatchKind.MANY_TO_ONE, MatchKind.PARTIAL):
        return False
    issued = docs[0].issue_date
    return issued is not None and any(t.booked_on < issued for t in txs)


def _credit_note_tags(
    txs: Sequence[Transaction], docs: Sequence[Document], own_tax_ids: Sequence[str]
) -> set[MatchTag]:
    notes = [d for d in docs if d.doc_type is DocumentType.CREDIT_NOTE]
    if not notes:
        return set()
    if len(docs) > 1:
        return {MatchTag.CREDIT_NOTE_OFFSET}
    purchase = not is_sales_document(notes[0], own_tax_ids)
    return {MatchTag.REFUND} if purchase and all(t.amount > 0 for t in txs) else set()


def _headline(
    kind,
    quality,
    tags,
    score: CandidateScore,
    docs,
    rivals,
    still_open: Decimal,
    rival_docs: Sequence[Document] = (),
) -> str:
    """One calm, plain sentence for the owner (§36, §69). No jargon, no ids."""
    singular, plural = document_label(docs)
    noun = singular.lower()
    if quality is Quality.RED:
        return _red_headline(score, noun)
    if MatchTag.AMBIGUOUS in tags:
        _, involved = document_label([*docs, *rival_docs])
        return _ambiguous_headline(kind, score, rivals, noun, involved)
    if kind is MatchKind.CARD_SETTLEMENT:
        return "Card repayment matched to the card statement."
    if kind is MatchKind.PARTIAL:
        still = format_money(abs(still_open), score.amount.document_currency)
        return f"Part payment. {still} is still open."
    if MatchTag.NEAR_AMOUNT in tags:
        return _near_headline(score, noun)
    return _shape_headline(
        kind, quality is Quality.GREEN, score, noun, plural, len(docs)
    )


def _red_headline(score: CandidateScore, noun: str) -> str:
    if any(
        f.kind is FactorKind.BANK_DETAILS and f.outcome is Outcome.CONTRADICTS
        for f in score.factors
    ):
        return f"The bank details on the {noun} don't match where the money went."
    return f"The {noun} has details that don't agree. Please check it."


def _shape_headline(
    kind: MatchKind,
    verified: bool,
    score: CandidateScore,
    noun: str,
    plural: str,
    count_docs: int,
) -> str:
    """'Matched to 3 invoices.' when verified, a gentle 'Please confirm.' otherwise."""
    count_tx = len(score.transaction_ids)
    if kind is MatchKind.ONE_TO_MANY:
        verified_text = f"Matched to {count_docs} {plural}."
        likely_text = f"This payment probably covers {count_docs} {plural}."
    elif kind is MatchKind.MANY_TO_ONE:
        verified_text = f"{count_tx} payments cover this {noun}."
        likely_text = f"{count_tx} payments probably cover this {noun}."
    else:
        verified_text = f"Matched to the {noun}."
        likely_text = f"This payment probably matches the {noun}."
    return verified_text if verified else f"{likely_text} Please confirm."


def _ambiguous_headline(
    kind, score: CandidateScore, rivals, noun: str, plural: str
) -> str:
    if kind is MatchKind.CARD_SETTLEMENT:
        if rivals:
            return "Card repayment. More than one card statement fits. Please confirm."
        return "Card repayment. Please confirm which purchases it covers."
    if kind in (MatchKind.ONE_TO_MANY, MatchKind.MANY_TO_ONE):
        return "More than one combination fits. Please confirm."
    same_payment = all(r.transaction_ids == score.transaction_ids for r in rivals)
    if same_payment and rivals:
        return f"This payment fits {len(rivals) + 1} {plural} equally well. Which one is it?"
    return f"More than one payment fits this {noun}. Please confirm."


def _near_headline(score: CandidateScore, noun: str) -> str:
    check = score.amount
    if check.difference:
        amount = format_money(abs(check.difference), check.document_currency)
        direction = "more" if abs(check.covered) > abs(check.document_total) else "less"
        return f"This payment is {amount} {direction} than the {noun}."
    if check.conversion_cost and check.conversion_cost > 0:
        cost = format_money(check.conversion_cost, check.bank_currency)
        about = "about " if check.cost_estimated else ""
        return f"This payment includes {about}{cost} in currency conversion costs."
    if check.fx_declared and check.conversion_cost is None:
        return (
            f"The amount matches the {noun} before currency conversion. Please confirm."
        )
    return f"This payment is close to the {noun} after currency conversion. Please confirm."
