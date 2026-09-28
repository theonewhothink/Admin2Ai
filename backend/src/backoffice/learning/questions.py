"""Needs-You questions and answers (§5, §36–38).

A question is a decision, not a form: a plain prompt ("We aren't sure which
company this belongs to."), a short detail line, a handful of one-tap options
and the "Why am I seeing this?" lines. :class:`SubjectFacts` keeps the facts
the question was built from, so an answer can become a one-tap rule (§38)
without re-reading the transaction.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backoffice.domain.models import new_id

from .plain import card_mask, day_month, format_money

__all__ = [
    "Answer",
    "OptionKind",
    "Question",
    "QuestionKind",
    "QuestionOption",
    "SubjectFacts",
    "describe_subject",
]


class QuestionKind(str, Enum):
    WHICH_COMPANY = "which_company"  # §37 "We aren't sure which company this belongs to."
    WHAT_IS_THIS = "what_is_this"  # §5 "This €92.40 Vodafone expense appears every month."
    MISSING_INVOICE = "missing_invoice"  # §37 "We can't find the invoice for this payment."
    CONFIRM_MATCH = "confirm_match"  # likely evidence found, one tap to confirm


class OptionKind(str, Enum):
    ENTITY = "entity"  # one of the tenant's companies
    PERSONAL = "personal"
    ANOTHER_COMPANY = "another_company"  # not one of the tenant's companies
    CATEGORY = "category"
    OTHER = "other"
    ACTION = "action"  # "Ask Vodafone for it", "I'll upload it", "Yes", "No"


class QuestionOption(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    label: str
    kind: OptionKind
    entity_id: str | None = None
    category: str | None = None
    action: str | None = None

    @model_validator(mode="after")
    def _consistent(self) -> QuestionOption:
        if self.kind is OptionKind.ENTITY and not self.entity_id:
            raise ValueError("an entity option needs entity_id")
        if self.kind is OptionKind.CATEGORY and not self.category:
            raise ValueError("a category option needs category")
        if self.kind is OptionKind.ACTION and not self.action:
            raise ValueError("an action option needs action")
        return self


class SubjectFacts(BaseModel):
    """What the question is about, kept as facts for rule suggestions and ranking."""

    model_config = ConfigDict(frozen=True)

    subject_type: str  # "transaction" | "document" | "series"
    subject_id: str
    counterparty_key: str | None = None
    counterparty_label: str | None = None
    card_last4: str | None = None
    account_id: str | None = None
    account_label: str | None = None
    supplier_tax_id: str | None = None
    amount: Decimal | None = None  # absolute value
    currency: str = "EUR"
    on: date | None = None

    @field_validator("amount", mode="before")
    @classmethod
    def _no_float(cls, value: object) -> object:
        if isinstance(value, float):
            raise ValueError("money must be Decimal, never float")
        return value

    @field_validator("amount")
    @classmethod
    def _absolute(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and value < 0:
            raise ValueError("amount is stored as an absolute value")
        return value


def describe_subject(facts: SubjectFacts, today: date | None = None) -> str:
    """Detail line: 'IKEA · €84.50 · 12 September · card •••• 4817'."""
    parts: list[str] = []
    if facts.counterparty_label:
        parts.append(facts.counterparty_label)
    if facts.amount is not None:
        parts.append(format_money(facts.amount, facts.currency))
    if facts.on is not None:
        parts.append(day_month(facts.on, today))
    if facts.card_last4:
        parts.append(f"card {card_mask(facts.card_last4)}")
    elif facts.account_label:
        parts.append(facts.account_label)
    return " · ".join(parts)


class Question(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=lambda: new_id("q"))
    tenant_id: str
    kind: QuestionKind
    prompt: str
    detail: str = ""
    options: tuple[QuestionOption, ...]
    why: tuple[str, ...] = ()
    facts: SubjectFacts
    series_key: str | None = None

    @model_validator(mode="after")
    def _options(self) -> Question:
        if len(self.options) < 2:
            raise ValueError("a question needs at least two options")
        ids = [o.id for o in self.options]
        if len(set(ids)) != len(ids):
            raise ValueError("option ids must be unique")
        return self

    def option(self, option_id: str) -> QuestionOption:
        for candidate in self.options:
            if candidate.id == option_id:
                return candidate
        raise KeyError(option_id)


class Answer(BaseModel):
    model_config = ConfigDict(frozen=True)

    question_id: str
    option_id: str
    answered_by: str
    answered_at: datetime

    @field_validator("answered_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("answered_at must be timezone-aware")
        return value
