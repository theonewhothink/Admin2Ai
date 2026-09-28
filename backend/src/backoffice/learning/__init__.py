"""Learning: how this business works (§5, §6, §23, §28, §37–38, §46 Entity Agent).

Public API
----------
Recurring expectations (§23) — :mod:`.recurrence`
    ``learn_from_transactions(txs) -> list[RecurringSeries]``
    ``learn_from_documents(docs, arrived_on=None) -> list[RecurringSeries]``
    ``learn_series(key, occurrences, basis=...) -> RecurringSeries | None``
    ``next_expected(series, arrivals=()) -> ExpectedWindow``
    ``check_overdue(series, today, arrivals=()) -> OverdueNotice | None``
    (one-off extras are tolerated as ``off_cycle`` and never cover a missing period)
    ``detect_price_change(amounts, currency, name) -> PriceChange | None``

Rules (§38 one-tap learning, §28 accountant rules) — :mod:`.rules`
    ``RuleBook.add(rule)``, ``RuleBook.evaluate(subject, tenant_id=, accountant_ids=) -> RuleDecision``
    ``RuleSubject.from_transaction(tx)`` / ``.from_document(doc)``
    ``suggest_rule_from_answer(question, answer) -> RuleProposal | None``

Entity Agent (§46, §51, §37) — :mod:`.entity`
    ``assign_entity(entities=, transaction=, document=, ownership=, rulebook=, history=) -> EntityAssignment``

First run (§5) — :mod:`.onboarding`
    ``candidates_from_assignments(transactions, assignments, series_keys=None) -> list[Candidate]``
    ``select_questions(candidates, limit=4) -> list[Candidate]``
    ``compute_coverage(items) -> Coverage`` ("We understand 96% of your business.")
    ``recurring_expense_question(series, tenant_id=, entities=) -> Question``

Shared building blocks, also used by the fraud and missing-evidence packages:
:mod:`.keys` (counterparty keys, ``match_key`` keeping resolved supplier ids
verbatim, ``qualified_tax_id`` giving a company's own tax number its country), :mod:`.plain` (owner-facing
formatting), :mod:`.stats` (median / MAD), :mod:`.questions` (Needs-You questions).
"""

from __future__ import annotations

from .entity import (
    ANOTHER_COMPANY,
    UNSURE_PROMPT,
    EntityAssignment,
    OwnershipBook,
    Vote,
    VoteKind,
    assign_entity,
    build_history,
)
from .keys import (
    counterparty_key,
    display_name,
    fold,
    is_canonical_key,
    match_key,
    normalize_tax_id,
    qualified_tax_id,
    same_tax_id,
    tax_id_country,
)
from .onboarding import (
    DEFAULT_QUESTION_LIMIT,
    MAX_QUESTION_LIMIT,
    Candidate,
    Coverage,
    CoverageItem,
    candidates_from_assignments,
    compute_coverage,
    confirm_line,
    coverage_items,
    recurring_expense_question,
    select_questions,
    uncertainty_for,
)
from .plain import card_mask, day_month, format_money, join_and, ordinal
from .questions import Answer, OptionKind, Question, QuestionKind, QuestionOption, SubjectFacts, describe_subject
from .recurrence import (
    CADENCE_SPECS,
    MAX_OFF_CYCLE_SHARE,
    Basis,
    Cadence,
    Direction,
    ExpectedWindow,
    Occurrence,
    OverdueNotice,
    PriceChange,
    RecurringSeries,
    check_overdue,
    detect_price_change,
    learn_from_documents,
    learn_from_transactions,
    learn_series,
    next_expected,
)
from .rules import (
    AUTHORITY,
    PERSONAL,
    Expectation,
    FieldDecision,
    Rule,
    RuleAuthor,
    RuleBook,
    RuleConflict,
    RuleDecision,
    RuleError,
    RuleEvent,
    RuleEventKind,
    RuleField,
    RuleMatch,
    RuleOutcome,
    RuleProposal,
    RuleScope,
    RuleSubject,
    suggest_rule_from_answer,
)
from .stats import mad, median, modified_z

__all__ = [
    "ANOTHER_COMPANY",
    "AUTHORITY",
    "CADENCE_SPECS",
    "DEFAULT_QUESTION_LIMIT",
    "MAX_OFF_CYCLE_SHARE",
    "MAX_QUESTION_LIMIT",
    "PERSONAL",
    "UNSURE_PROMPT",
    "Answer",
    "Basis",
    "Cadence",
    "Candidate",
    "Coverage",
    "CoverageItem",
    "Direction",
    "EntityAssignment",
    "Expectation",
    "ExpectedWindow",
    "FieldDecision",
    "Occurrence",
    "OptionKind",
    "OverdueNotice",
    "OwnershipBook",
    "PriceChange",
    "Question",
    "QuestionKind",
    "QuestionOption",
    "RecurringSeries",
    "Rule",
    "RuleAuthor",
    "RuleBook",
    "RuleConflict",
    "RuleDecision",
    "RuleError",
    "RuleEvent",
    "RuleEventKind",
    "RuleField",
    "RuleMatch",
    "RuleOutcome",
    "RuleProposal",
    "RuleScope",
    "RuleSubject",
    "SubjectFacts",
    "Vote",
    "VoteKind",
    "assign_entity",
    "build_history",
    "candidates_from_assignments",
    "card_mask",
    "check_overdue",
    "compute_coverage",
    "confirm_line",
    "counterparty_key",
    "coverage_items",
    "day_month",
    "describe_subject",
    "detect_price_change",
    "display_name",
    "fold",
    "format_money",
    "is_canonical_key",
    "join_and",
    "learn_from_documents",
    "learn_from_transactions",
    "learn_series",
    "mad",
    "match_key",
    "median",
    "modified_z",
    "next_expected",
    "normalize_tax_id",
    "ordinal",
    "qualified_tax_id",
    "recurring_expense_question",
    "same_tax_id",
    "select_questions",
    "suggest_rule_from_answer",
    "tax_id_country",
    "uncertainty_for",
]
