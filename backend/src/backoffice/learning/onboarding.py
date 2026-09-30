"""First-run learning (§5): the few questions worth asking, and how much we already understand.

Question selection
------------------
Every candidate question is worth ``amount x occurrences x uncertainty``:
the typical amount of one occurrence, how often it happened in the imported
window, and how unsure we are (1 = no idea or conflicting facts, 0.5 = likely,
0 = verified). Candidates about the same series are deduplicated (one answer
teaches the whole series through a one-tap rule, §38), ranked by value, and
capped: 4 by default, never 10 or more (§5 "preferably <10").

Amounts in different currencies are compared as-is for ranking; no exchange
rate is applied here.

:func:`candidates_from_assignments` builds the candidates from Entity Agent
results: one per series (a counterparty's open questions), valued at the
median amount of those transactions x how many there are x the highest
uncertainty among them (1 when nothing was assigned at all). The most recent
transaction's question stands for the series.

Coverage ("We understand 96% of your business.")
------------------------------------------------
A transaction is *explained* when we can handle it without asking the owner:
it has an assignment (a company or personal) of GREEN or AMBER quality and no
open question. Everything else — RED, no assignment, or waiting on a
question — is *unexplained*.

* ``by_count``  = explained transactions / all transactions.
* ``by_volume`` = sum of |amount| of explained transactions / sum of |amount|
  of all transactions, over transactions in the base currency only (no FX).
* The headline percentage is the **lower** of the two, **rounded down**, and
  never 100 while anything is unexplained. We never overstate (§57).
* ``projected`` coverage treats the selected questions' series as answered,
  showing what the owner's few taps will achieve.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, field_validator

from backoffice.domain.models import LegalEntity, Quality, Transaction

from .entity import EntityAssignment
from .keys import display_name
from .plain import count_phrase, day_month, format_money
from .questions import OptionKind, Question, QuestionKind, QuestionOption, SubjectFacts
from .recurrence import Direction, RecurringSeries
from .stats import median

__all__ = [
    "DEFAULT_QUESTION_LIMIT",
    "MAX_QUESTION_LIMIT",
    "Candidate",
    "Coverage",
    "CoverageItem",
    "candidates_from_assignments",
    "compute_coverage",
    "confirm_line",
    "coverage_items",
    "recurring_expense_question",
    "select_questions",
    "uncertainty_for",
]

DEFAULT_QUESTION_LIMIT = 4
MAX_QUESTION_LIMIT = 9

_UNCERTAINTY = {Quality.RED: Decimal(1), Quality.AMBER: Decimal("0.5"), Quality.GREEN: Decimal(0)}


def uncertainty_for(quality: Quality | None) -> Decimal:
    """1 for no assignment or a conflict, 0.5 for likely, 0 for verified."""
    return Decimal(1) if quality is None else _UNCERTAINTY[quality]


class Candidate(BaseModel):
    model_config = ConfigDict(frozen=True)

    question: Question
    amount: Decimal  # typical absolute amount of one occurrence
    occurrences: int  # in the imported window
    uncertainty: Decimal  # 0..1
    series_key: str | None = None

    @field_validator("amount", mode="before")
    @classmethod
    def _no_float(cls, value: object) -> object:
        if isinstance(value, float):
            raise ValueError("money must be Decimal, never float")
        return value

    @field_validator("amount")
    @classmethod
    def _amount(cls, value: Decimal) -> Decimal:
        if value < 0:
            raise ValueError("amount is absolute")
        return value

    @field_validator("occurrences")
    @classmethod
    def _occurrences(cls, value: int) -> int:
        if value < 1:
            raise ValueError("a candidate happened at least once")
        return value

    @field_validator("uncertainty")
    @classmethod
    def _uncertainty(cls, value: Decimal) -> Decimal:
        if not Decimal(0) <= value <= Decimal(1):
            raise ValueError("uncertainty is between 0 and 1")
        return value

    @property
    def value(self) -> Decimal:
        return self.amount * self.occurrences * self.uncertainty

    @property
    def dedupe_key(self) -> str:
        return (
            self.series_key
            or self.question.series_key
            or self.question.facts.counterparty_key
            or f"question:{self.question.id}"
        )


def select_questions(candidates: Iterable[Candidate], *, limit: int = DEFAULT_QUESTION_LIMIT) -> list[Candidate]:
    """Highest-value questions, one per series, at most ``limit`` (1..9)."""
    if not 1 <= limit <= MAX_QUESTION_LIMIT:
        raise ValueError(f"ask between 1 and {MAX_QUESTION_LIMIT} questions")
    ranked = sorted(
        (c for c in candidates if c.value > 0),
        key=lambda c: (-c.value, c.dedupe_key, c.question.id),
    )
    chosen: list[Candidate] = []
    seen: set[str] = set()
    for candidate in ranked:
        if candidate.dedupe_key in seen:
            continue
        seen.add(candidate.dedupe_key)
        chosen.append(candidate)
        if len(chosen) == limit:
            break
    return chosen


def _uncertainty_of(assignment: EntityAssignment) -> Decimal:
    if assignment.entity_id is None and not assignment.private:
        return Decimal(1)  # nothing assigned: no idea, whatever the quality says
    return uncertainty_for(assignment.quality)


def candidates_from_assignments(
    transactions: Iterable[Transaction],
    assignments: Mapping[str, EntityAssignment],
    *,
    series_keys: Mapping[str, str] | None = None,
) -> list[Candidate]:
    """One candidate per series with open questions (definition in the module docstring).

    ``series_keys`` maps transaction id -> series key; by default the key the
    question was built from is used, and a transaction without one stands alone.
    """
    groups: dict[str, list[tuple[Transaction, EntityAssignment]]] = {}
    for tx in transactions:
        assignment = assignments.get(tx.id)
        if assignment is None or assignment.question is None:
            continue
        question = assignment.question
        key = (series_keys or {}).get(tx.id) or question.series_key or f"question:{question.id}"
        groups.setdefault(key, []).append((tx, assignment))
    candidates = []
    for key, members in sorted(groups.items()):
        latest_tx, latest = max(members, key=lambda m: (m[0].booked_on, m[0].id))
        assert latest.question is not None  # grouped only when a question exists
        candidates.append(
            Candidate(
                question=latest.question,
                amount=median([abs(tx.amount) for tx, _ in members]),
                occurrences=len(members),
                uncertainty=max(_uncertainty_of(a) for _, a in members),
                series_key=None if key.startswith("question:") else key,
            )
        )
    return candidates


# --------------------------------------------------------------------------- coverage


@dataclass(frozen=True)
class CoverageItem:
    amount: Decimal
    explained: bool
    currency: str = "EUR"
    series_key: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.amount, float):
            raise TypeError("money must be Decimal, never float")


class Coverage(BaseModel):
    model_config = ConfigDict(frozen=True)

    transactions: int
    explained_transactions: int
    currency: str
    volume: Decimal
    explained_volume: Decimal
    by_count: Decimal | None
    by_volume: Decimal | None
    percent: int | None
    headline: str


def coverage_items(
    transactions: Iterable[Transaction],
    assignments: Mapping[str, EntityAssignment],
    *,
    series_keys: Mapping[str, str] | None = None,
) -> list[CoverageItem]:
    """Coverage inputs from transactions and their Entity Agent results (by transaction id)."""
    items = []
    for tx in transactions:
        assignment = assignments.get(tx.id)
        explained = (
            assignment is not None
            and not assignment.needs_owner
            and assignment.quality is not Quality.RED
            and (assignment.entity_id is not None or assignment.private)
        )
        items.append(
            CoverageItem(
                amount=abs(tx.amount),
                explained=explained,
                currency=tx.currency,
                series_key=(series_keys or {}).get(tx.id),
            )
        )
    return items


def _share(part: Decimal, whole: Decimal) -> Decimal | None:
    return None if whole == 0 else part / whole


def compute_coverage(
    items: Sequence[CoverageItem],
    *,
    base_currency: str = "EUR",
    assume_answered: Iterable[str] = (),
) -> Coverage:
    """Share of the business we understand without questions (definition in the module docstring)."""
    answered = set(assume_answered)

    def explained(item: CoverageItem) -> bool:
        return item.explained or (item.series_key is not None and item.series_key in answered)

    total = len(items)
    done = sum(1 for i in items if explained(i))
    in_base = [i for i in items if i.currency.upper() == base_currency.upper()]
    volume = sum((abs(i.amount) for i in in_base), Decimal(0))
    explained_volume = sum((abs(i.amount) for i in in_base if explained(i)), Decimal(0))
    by_count = _share(Decimal(done), Decimal(total))
    by_volume = _share(explained_volume, volume)
    shares = [s for s in (by_count, by_volume) if s is not None]
    percent: int | None = None
    if shares:
        percent = math.floor(min(shares) * 100)
        if done < total:
            percent = min(percent, 99)
    headline = (
        "We are still learning how your business works."
        if percent is None
        else f"We understand {percent}% of your business."
    )
    return Coverage(
        transactions=total,
        explained_transactions=done,
        currency=base_currency.upper(),
        volume=volume,
        explained_volume=explained_volume,
        by_count=by_count,
        by_volume=by_volume,
        percent=percent,
        headline=headline,
    )


def confirm_line(questions: int) -> str:
    """'We need you to confirm 4 things.' / 'We need you to confirm one thing.'"""
    if questions == 0:
        return "Nothing needs you right now."
    return f"We need you to confirm {count_phrase(questions, 'thing')}."


# --------------------------------------------------------------------------- §5 recurring question


def recurring_expense_question(
    series: RecurringSeries,
    *,
    tenant_id: str,
    entities: Sequence[LegalEntity],
    category: str | None = None,
    category_label: str | None = None,
    today: date | None = None,
) -> Question:
    """'This €92.40 Vodafone expense appears every month.' → Company telecom / Personal / Other.

    ``today`` makes the "since" date carry its year when it is not this year.
    """
    own = sorted((e for e in entities if e.tenant_id == tenant_id), key=lambda e: (e.name.casefold(), e.id))
    if not own:
        raise ValueError("the tenant has no companies")
    if (category is None) != (category_label is None):
        raise ValueError("pass category and category_label together")
    options: list[QuestionOption] = []
    if len(own) == 1:
        entity = own[0]
        option_id = f"entity:{entity.id}"
        if category is not None:
            options.append(QuestionOption(id=option_id, label=f"Company {category_label}", kind=OptionKind.CATEGORY,
                                          category=category, entity_id=entity.id))  # fmt: skip
        else:
            options.append(QuestionOption(id=option_id, label="Company", kind=OptionKind.ENTITY, entity_id=entity.id))
    else:
        for entity in own:
            label = display_name(entity.name, fallback=entity.name)
            options.append(QuestionOption(id=f"entity:{entity.id}", label=label, kind=OptionKind.ENTITY,
                                          entity_id=entity.id, category=category))  # fmt: skip
    options.append(QuestionOption(id="personal", label="Personal", kind=OptionKind.PERSONAL))
    options.append(QuestionOption(id="other", label="Other", kind=OptionKind.OTHER))
    return Question(
        tenant_id=tenant_id,
        kind=QuestionKind.WHAT_IS_THIS,
        prompt=_recurring_prompt(series),
        options=tuple(options),
        why=(f"Seen {count_phrase(series.observations, 'time')} since {day_month(series.first_seen, today)}.",),
        facts=SubjectFacts(
            subject_type="series",
            subject_id=series.key,
            counterparty_key=series.key,
            counterparty_label=series.display_name,
            amount=series.typical_amount,
            currency=series.currency or "EUR",
            on=series.last_seen,
        ),
        series_key=series.key,
    )


def _recurring_prompt(series: RecurringSeries) -> str:
    rhythm = series.rhythm  # "every month", or "every month from May to September" for a seasonal one
    name = series.display_name
    if series.typical_amount is None:
        return f"{name} appears {rhythm}."
    amount = format_money(series.typical_amount, series.currency or "EUR")
    if series.direction is Direction.IN:
        return f"This {amount} payment from {name} arrives {rhythm}."
    return f"This {amount} {name} expense appears {rhythm}."
