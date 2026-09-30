"""Taught rules: one-tap learning (§38) and accountant rules (§28).

A :class:`Rule` says "when a transaction/document looks like *this*, the
answer is *that*". Owners teach rules by answering a question once ("☑ Always
use Hazel Tree for IKEA paid with card •••• 4817"); accountants teach rules
for one client or for all their authorized clients ("Treat all Adobe
subscriptions as Software").

Precedence (deterministic, per decided field)
---------------------------------------------
Rules are ranked separately for each field they decide, by this key, best first:

1. **Authority for the field.** Who owns the answer to this kind of question:

   * ``ENTITY`` (which company / personal), ``COST_CENTER`` (which job,
     property, vehicle ...; general costs; or a learned split) and
     ``RECHARGE`` (the client pays it back, or it is the business's own cost):
     the **owner** outranks the accountant. Only the owner knows which company,
     and which job, actually bought something, so an owner answer on this
     business beats an accountant rule, even a global one covering all their
     clients.
   * ``CATEGORY``, ``TAX_TREATMENT``, ``EXPECTATION``: the **accountant**
     outranks the owner. These are professional bookkeeping and tax judgements
     (§25 lists tax interpretation changes as owner-approval events; the
     accountant's rule is the interpretation), and the accountant decides
     which supporting evidence they need (§21).

2. **Scope.** A rule scoped to this business (owner rule, or accountant rule
   for this client) beats an accountant rule for all their clients.
3. **Match specificity.** More match criteria beat fewer ("IKEA + card 4817"
   beats "IKEA").
4. Rules tied on 1-3 that give the **same** value agree; the newest is cited.
   Tied rules that give **different** values are an unresolved conflict: the
   field is left undecided and the conflict is reported (§19, never guess).

Lower-ranked rules that disagree with the winner are reported as resolved
conflicts, so an accountant can see when an owner answer overrode their rule.

Every lifecycle change (created, superseded, deactivated) is appended to the
book's audit trail; rules are immutable and never edited in place (§55).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backoffice.domain.models import Document, Transaction, new_id

from .keys import counterparty_key, display_name, is_canonical_key, match_key, normalize_tax_id, same_tax_id
from .plain import card_mask
from .questions import Answer, OptionKind, Question

__all__ = [
    "AUTHORITY",
    "GENERAL",
    "PERSONAL",
    "Expectation",
    "FieldDecision",
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
    "suggest_rule_from_answer",
]

# Value of the ENTITY field when the answer is "Personal" (entity ids look like "ent_…").
PERSONAL = "personal"
# Value of the COST_CENTER field when the answer is "general costs, not one job" (cost center ids look like "cc-…").
GENERAL = "general"


class RuleError(ValueError):
    """A rule or answer that cannot be used (programming/config error, never owner-facing)."""


class RuleAuthor(str, Enum):
    OWNER = "owner"
    ACCOUNTANT = "accountant"


class RuleScope(str, Enum):
    TENANT = "tenant"  # owner rule for their own business
    CLIENT = "client"  # accountant rule for one client (tenant)
    ALL_CLIENTS_OF_ACCOUNTANT = "all_clients_of_accountant"


class Expectation(str, Enum):
    """What evidence a matching transaction should have (§21)."""

    INVOICE = "invoice"
    RECEIPT = "receipt"
    NONE = "none"
    TAX_NOTICE = "tax_notice"
    PAYROLL = "payroll"
    LOAN_STATEMENT = "loan_statement"
    BANK_EVIDENCE = "bank_evidence"


class RuleField(str, Enum):
    ENTITY = "entity"  # entity id, or PERSONAL
    CATEGORY = "category"
    TAX_TREATMENT = "tax_treatment"
    EXPECTATION = "expectation"
    # Which job, property, vehicle ... (cost center id), GENERAL, or a learned split
    # ((cost center id, percent), ...) adding up to 100.
    COST_CENTER = "cost_center"
    # Whether a cost on a client's cost center is recharged to that client (True) or the business's own (False).
    RECHARGE = "recharge"


AUTHORITY: dict[RuleField, tuple[RuleAuthor, ...]] = {
    RuleField.ENTITY: (RuleAuthor.OWNER, RuleAuthor.ACCOUNTANT),
    # Only the owner knows which job a purchase was for, like which company bought it.
    RuleField.COST_CENTER: (RuleAuthor.OWNER, RuleAuthor.ACCOUNTANT),
    # ... and whether the client pays it back.
    RuleField.RECHARGE: (RuleAuthor.OWNER, RuleAuthor.ACCOUNTANT),
    RuleField.CATEGORY: (RuleAuthor.ACCOUNTANT, RuleAuthor.OWNER),
    RuleField.TAX_TREATMENT: (RuleAuthor.ACCOUNTANT, RuleAuthor.OWNER),
    RuleField.EXPECTATION: (RuleAuthor.ACCOUNTANT, RuleAuthor.OWNER),
}
_SCOPE_RANK = {RuleScope.TENANT: 0, RuleScope.CLIENT: 0, RuleScope.ALL_CLIENTS_OF_ACCOUNTANT: 1}


# --------------------------------------------------------------------------- subject


@dataclass(frozen=True)
class RuleSubject:
    """The facts a rule can match on. ``amount`` is absolute."""

    counterparty_key: str | None = None
    card_last4: str | None = None
    account_id: str | None = None
    amount: Decimal | None = None
    supplier_tax_id: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.amount, float):
            raise TypeError("money must be Decimal, never float")

    @classmethod
    def from_transaction(cls, tx: Transaction, *, key: str | None = None) -> RuleSubject:
        return cls(
            counterparty_key=key or counterparty_key(tx.counterparty),
            card_last4=tx.card_last4,
            account_id=tx.account_id,
            amount=abs(tx.amount),
        )

    @classmethod
    def from_document(cls, doc: Document, *, key: str | None = None) -> RuleSubject:
        return cls(
            counterparty_key=key or counterparty_key(doc.supplier_name),
            amount=abs(doc.gross_amount) if doc.gross_amount is not None else None,
            supplier_tax_id=doc.supplier_tax_id,
        )

    def merged(self, other: RuleSubject) -> RuleSubject:
        """Facts of a matched transaction + document pair (own values win)."""
        return RuleSubject(
            counterparty_key=self.counterparty_key or other.counterparty_key,
            card_last4=self.card_last4 or other.card_last4,
            account_id=self.account_id or other.account_id,
            amount=self.amount if self.amount is not None else other.amount,
            supplier_tax_id=self.supplier_tax_id or other.supplier_tax_id,
        )


# --------------------------------------------------------------------------- rule model


class RuleMatch(BaseModel):
    """All set criteria must hold (AND). At least one criterion is required."""

    model_config = ConfigDict(frozen=True)

    counterparty_key: str | None = None
    card_last4: str | None = None
    account_id: str | None = None
    amount_min: Decimal | None = None
    amount_max: Decimal | None = None
    supplier_tax_id: str | None = None

    @field_validator("counterparty_key")
    @classmethod
    def _key(cls, value: str | None) -> str | None:
        if value is None:
            return None
        key = match_key(value)  # names are normalized; supplier ids are kept verbatim
        if key is None:
            raise ValueError("counterparty key is empty")
        return key

    @field_validator("card_last4")
    @classmethod
    def _card(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"\d{4}", value):
            raise ValueError("card_last4 must be exactly 4 digits")
        return value

    @field_validator("amount_min", "amount_max", mode="before")
    @classmethod
    def _no_float(cls, value: object) -> object:
        if isinstance(value, float):
            raise ValueError("money must be Decimal, never float")
        return value

    @model_validator(mode="after")
    def _criteria(self) -> RuleMatch:
        for bound in (self.amount_min, self.amount_max):
            if bound is not None and bound < 0:
                raise ValueError("amount bounds are absolute values")
        if self.amount_min is not None and self.amount_max is not None and self.amount_min > self.amount_max:
            raise ValueError("amount_min is above amount_max")
        if not self.criteria():
            raise ValueError("a rule must match on at least one fact")
        return self

    def criteria(self) -> tuple[str, ...]:
        """Names of the set criteria (an amount range counts once)."""
        names = [
            name
            for name in ("counterparty_key", "card_last4", "account_id", "supplier_tax_id")
            if getattr(self, name) is not None
        ]
        if self.amount_min is not None or self.amount_max is not None:
            names.append("amount")
        return tuple(names)

    def matches(self, subject: RuleSubject) -> bool:
        if self.counterparty_key is not None and match_key(subject.counterparty_key) != self.counterparty_key:
            return False
        if self.card_last4 is not None and subject.card_last4 != self.card_last4:
            return False
        if self.account_id is not None and subject.account_id != self.account_id:
            return False
        if self.supplier_tax_id is not None and not same_tax_id(self.supplier_tax_id, subject.supplier_tax_id):
            return False
        if self.amount_min is not None or self.amount_max is not None:
            if subject.amount is None:
                return False
            if self.amount_min is not None and subject.amount < self.amount_min:
                return False
            if self.amount_max is not None and subject.amount > self.amount_max:
                return False
        return True


class RuleOutcome(BaseModel):
    model_config = ConfigDict(frozen=True)

    entity_id: str | None = None
    private: bool = False
    category: str | None = None
    tax_treatment: str | None = None
    expectation: Expectation | None = None
    cost_center_id: str | None = None  # a cost center id, or GENERAL
    cost_center_split: tuple[tuple[str, Decimal], ...] = ()  # learned split: (cost center id, percent)
    recharge: bool | None = None  # the client pays it back (True), or it is the business's own cost (False)

    @field_validator("cost_center_split", mode="before")
    @classmethod
    def _split_money(cls, value: object) -> object:
        if isinstance(value, (list, tuple)):
            for entry in value:
                if isinstance(entry, (list, tuple)) and any(isinstance(v, float) for v in entry):
                    raise ValueError("percentages must be Decimal, never float")
        return value

    @model_validator(mode="after")
    def _consistent(self) -> RuleOutcome:
        if self.private and self.entity_id:
            raise ValueError("an outcome cannot be both personal and a company")
        if self.cost_center_id and self.cost_center_split:
            raise ValueError("an outcome cannot be one cost center and a split")
        if self.cost_center_split:
            ids = [cid for cid, _ in self.cost_center_split]
            if len(ids) < 2 or len(set(ids)) != len(ids) or GENERAL in ids:
                raise ValueError("a split names at least two different cost centers")
            if any(p <= 0 for _, p in self.cost_center_split):
                raise ValueError("every share of a split is more than zero")
            if sum((p for _, p in self.cost_center_split), Decimal(0)) != Decimal(100):
                raise ValueError("a split adds up to exactly 100%")
        if not self.fields():
            raise ValueError("a rule must decide at least one thing")
        return self

    def fields(self) -> frozenset[RuleField]:
        decided = set()
        if self.entity_id or self.private:
            decided.add(RuleField.ENTITY)
        if self.category:
            decided.add(RuleField.CATEGORY)
        if self.tax_treatment:
            decided.add(RuleField.TAX_TREATMENT)
        if self.expectation is not None:
            decided.add(RuleField.EXPECTATION)
        if self.cost_center_id or self.cost_center_split:
            decided.add(RuleField.COST_CENTER)
        if self.recharge is not None:
            decided.add(RuleField.RECHARGE)
        return frozenset(decided)

    def value(self, field: RuleField) -> Any:
        if field is RuleField.ENTITY:
            return PERSONAL if self.private else self.entity_id
        if field is RuleField.CATEGORY:
            return self.category
        if field is RuleField.TAX_TREATMENT:
            return self.tax_treatment
        if field is RuleField.COST_CENTER:
            return self.cost_center_split or self.cost_center_id
        if field is RuleField.RECHARGE:
            return self.recharge
        return self.expectation

    def without(self, fields: Iterable[RuleField]) -> RuleOutcome | None:
        """This outcome minus ``fields``; None when nothing would be left."""
        drop = set(fields)
        data = {
            "entity_id": None if RuleField.ENTITY in drop else self.entity_id,
            "private": False if RuleField.ENTITY in drop else self.private,
            "category": None if RuleField.CATEGORY in drop else self.category,
            "tax_treatment": None if RuleField.TAX_TREATMENT in drop else self.tax_treatment,
            "expectation": None if RuleField.EXPECTATION in drop else self.expectation,
            "cost_center_id": None if RuleField.COST_CENTER in drop else self.cost_center_id,
            "cost_center_split": () if RuleField.COST_CENTER in drop else self.cost_center_split,
            "recharge": None if RuleField.RECHARGE in drop else self.recharge,
        }
        if not any(v for k, v in data.items() if k != "recharge") and data["recharge"] is None:
            return None
        return RuleOutcome(**data)


class Rule(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=lambda: new_id("rule"))
    author: RuleAuthor
    author_id: str
    scope: RuleScope
    tenant_id: str | None = None  # required for TENANT and CLIENT scope
    match: RuleMatch
    outcome: RuleOutcome
    created_at: datetime
    label: str = ""  # plain words shown to people, e.g. the one-tap sentence (§38)
    active: bool = True

    @field_validator("created_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        return value

    @model_validator(mode="after")
    def _scope(self) -> Rule:
        if not self.author_id.strip():
            raise ValueError("a rule needs its author")
        if self.author is RuleAuthor.OWNER and self.scope is not RuleScope.TENANT:
            raise ValueError("owner rules apply to their own business only")
        if self.author is RuleAuthor.ACCOUNTANT and self.scope is RuleScope.TENANT:
            raise ValueError("accountant rules are scoped to a client or to all their clients")
        needs_tenant = self.scope is not RuleScope.ALL_CLIENTS_OF_ACCOUNTANT
        if needs_tenant and not self.tenant_id:
            raise ValueError(f"{self.scope.value} rules need a tenant")
        if not needs_tenant and self.tenant_id:
            raise ValueError("a rule for all clients cannot be tied to one tenant")
        return self

    def applies_to(self, tenant_id: str, accountant_ids: frozenset[str]) -> bool:
        """Whether this rule may be used for ``tenant_id`` right now."""
        if not self.active:
            return False
        if self.author is RuleAuthor.OWNER:
            return self.tenant_id == tenant_id
        if self.author_id not in accountant_ids:  # authorization withdrawn -> rule stops applying
            return False
        return self.scope is RuleScope.ALL_CLIENTS_OF_ACCOUNTANT or self.tenant_id == tenant_id

    def slot(self) -> tuple[Any, ...]:
        """Rules in the same slot say the same kind of thing about the same facts."""
        return (self.author, self.author_id, self.scope, self.tenant_id, self.match)

    def rank(self, field: RuleField) -> tuple[int, int, int]:
        """Lower is stronger. See the module docstring."""
        return (
            AUTHORITY[field].index(self.author),
            _SCOPE_RANK[self.scope],
            -len(self.match.criteria()),
        )


# --------------------------------------------------------------------------- decisions


class FieldDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    field: RuleField
    value: Any
    rule_id: str
    author: RuleAuthor
    matched_on: tuple[str, ...]  # criteria of the winning rule (e.g. ("counterparty_key", "card_last4"))
    label: str = ""


class RuleConflict(BaseModel):
    model_config = ConfigDict(frozen=True)

    field: RuleField
    resolved: bool
    winner_rule_id: str | None
    rule_ids: tuple[str, ...]  # every rule that disagreed, winner first when resolved
    reason: str  # internal explanation for accountants/auditors


class RuleDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    decisions: dict[RuleField, FieldDecision] = Field(default_factory=dict)
    conflicts: tuple[RuleConflict, ...] = ()

    def get(self, field: RuleField) -> FieldDecision | None:
        return self.decisions.get(field)

    def unresolved(self, field: RuleField) -> RuleConflict | None:
        for conflict in self.conflicts:
            if conflict.field is field and not conflict.resolved:
                return conflict
        return None

    @property
    def entity_id(self) -> str | None:
        decision = self.decisions.get(RuleField.ENTITY)
        return None if decision is None or decision.value == PERSONAL else decision.value

    @property
    def private(self) -> bool:
        decision = self.decisions.get(RuleField.ENTITY)
        return decision is not None and decision.value == PERSONAL

    @property
    def category(self) -> str | None:
        decision = self.decisions.get(RuleField.CATEGORY)
        return decision.value if decision else None

    @property
    def tax_treatment(self) -> str | None:
        decision = self.decisions.get(RuleField.TAX_TREATMENT)
        return decision.value if decision else None

    @property
    def expectation(self) -> Expectation | None:
        decision = self.decisions.get(RuleField.EXPECTATION)
        return decision.value if decision else None

    @property
    def cost_center(self) -> str | tuple[tuple[str, Decimal], ...] | None:
        """A cost center id, GENERAL, or a learned split ((cost center id, percent), ...)."""
        decision = self.decisions.get(RuleField.COST_CENTER)
        return decision.value if decision else None

    @property
    def recharge(self) -> bool | None:
        """True: the client pays it back; False: the business's own cost; None: no rule says."""
        decision = self.decisions.get(RuleField.RECHARGE)
        return decision.value if decision else None


def _newest(rules: Sequence[Rule]) -> Rule:
    return max(rules, key=lambda r: (r.created_at, r.id))


def _decide_field(field: RuleField, rules: Sequence[Rule]) -> tuple[FieldDecision | None, list[RuleConflict]]:
    ranked = sorted(rules, key=lambda r: (r.rank(field), r.id))
    top_rank = ranked[0].rank(field)
    top = [r for r in ranked if r.rank(field) == top_rank]
    top_values = {r.outcome.value(field) for r in top}
    if len(top_values) > 1:
        conflict = RuleConflict(
            field=field,
            resolved=False,
            winner_rule_id=None,
            rule_ids=tuple(r.id for r in top),
            reason="rules with equal authority, scope and specificity disagree",
        )
        return None, [conflict]
    winner = _newest(top)
    value = winner.outcome.value(field)
    overridden = [r for r in ranked if r.outcome.value(field) != value]
    conflicts = []
    if overridden:
        conflicts.append(
            RuleConflict(
                field=field,
                resolved=True,
                winner_rule_id=winner.id,
                rule_ids=(winner.id, *(r.id for r in overridden)),
                reason=_override_reason(field, winner, overridden[0]),
            )
        )
    decision = FieldDecision(
        field=field,
        value=value,
        rule_id=winner.id,
        author=winner.author,
        matched_on=winner.match.criteria(),
        label=winner.label,
    )
    return decision, conflicts


def _override_reason(field: RuleField, winner: Rule, loser: Rule) -> str:
    won, lost = winner.rank(field), loser.rank(field)
    if won[0] != lost[0]:
        return f"{winner.author.value} decides {field.value} over {loser.author.value}"
    if won[1] != lost[1]:
        return "a rule for this business beats a rule for all clients"
    return "a more specific rule beats a less specific one"


# --------------------------------------------------------------------------- the book


class RuleEventKind(str, Enum):
    CREATED = "created"
    SUPERSEDED = "superseded"
    DEACTIVATED = "deactivated"


class RuleEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    at: datetime
    kind: RuleEventKind
    rule_id: str
    actor: str
    reason: str = ""
    replaced_by: tuple[str, ...] = ()

    @field_validator("at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("audit times must be timezone-aware")
        return value


class RuleBook:
    """Active rules plus an append-only audit trail (§55). Evaluation is pure."""

    def __init__(self, rules: Iterable[Rule] = (), audit: Iterable[RuleEvent] = ()) -> None:
        self._rules: dict[str, Rule] = {}
        self._audit: list[RuleEvent] = list(audit)
        for rule in rules:
            if rule.id in self._rules:
                raise RuleError(f"duplicate rule id {rule.id}")
            self._rules[rule.id] = rule

    @property
    def rules(self) -> tuple[Rule, ...]:
        return tuple(sorted(self._rules.values(), key=lambda r: (r.created_at, r.id)))

    @property
    def audit(self) -> tuple[RuleEvent, ...]:
        return tuple(self._audit)

    def get(self, rule_id: str) -> Rule:
        return self._rules[rule_id]

    def add(self, rule: Rule, *, reason: str = "") -> tuple[Rule, ...]:
        """Store ``rule``; supersede older rules in the same slot for the same fields.

        An older rule that also decided other fields is replaced by a narrowed
        copy keeping only those fields, so no teaching is silently lost.
        Returns the narrowed copies created (usually none).
        """
        if rule.id in self._rules:
            raise RuleError(f"duplicate rule id {rule.id}")
        if not rule.active:
            raise RuleError("add an active rule")
        created: list[Rule] = []
        for old in list(self._rules.values()):
            overlap = old.outcome.fields() & rule.outcome.fields()
            if not old.active or old.slot() != rule.slot() or not overlap:
                continue
            replacements = [rule.id]
            remaining = old.outcome.without(overlap)
            if remaining is not None:
                narrowed = old.model_copy(update={"id": new_id("rule"), "outcome": remaining})
                self._rules[narrowed.id] = narrowed
                created.append(narrowed)
                replacements.append(narrowed.id)
                self._log(RuleEventKind.CREATED, narrowed, rule.author_id, rule.created_at,
                          "kept the rest of an older rule")  # fmt: skip
            self._rules[old.id] = old.model_copy(update={"active": False})
            self._log(RuleEventKind.SUPERSEDED, old, rule.author_id, rule.created_at,
                      reason or "replaced by a newer rule", tuple(replacements))  # fmt: skip
        self._rules[rule.id] = rule
        self._log(RuleEventKind.CREATED, rule, rule.author_id, rule.created_at, reason)
        return tuple(created)

    def deactivate(self, rule_id: str, *, actor: str, at: datetime, reason: str = "") -> Rule:
        rule = self._rules[rule_id]
        if not rule.active:
            return rule
        event = RuleEvent(at=at, kind=RuleEventKind.DEACTIVATED, rule_id=rule.id, actor=actor, reason=reason)
        inactive = rule.model_copy(update={"active": False})
        self._rules[rule_id] = inactive
        self._audit.append(event)
        return inactive

    def applicable(self, tenant_id: str, accountant_ids: Iterable[str] = ()) -> tuple[Rule, ...]:
        authorized = frozenset(accountant_ids)
        return tuple(r for r in self.rules if r.applies_to(tenant_id, authorized))

    def evaluate(
        self, subject: RuleSubject, *, tenant_id: str, accountant_ids: Iterable[str] = ()
    ) -> RuleDecision:
        """Per-field decision for ``subject`` with conflicts reported (see module docstring)."""
        matching = [r for r in self.applicable(tenant_id, accountant_ids) if r.match.matches(subject)]
        decisions: dict[RuleField, FieldDecision] = {}
        conflicts: list[RuleConflict] = []
        for field in RuleField:
            relevant = [r for r in matching if field in r.outcome.fields()]
            if not relevant:
                continue
            decision, found = _decide_field(field, relevant)
            conflicts.extend(found)
            if decision is not None:
                decisions[field] = decision
        return RuleDecision(decisions=decisions, conflicts=tuple(conflicts))

    def _log(
        self,
        kind: RuleEventKind,
        rule: Rule,
        actor: str,
        at: datetime,
        reason: str,
        replaced_by: tuple[str, ...] = (),
    ) -> None:
        self._audit.append(
            RuleEvent(at=at, kind=kind, rule_id=rule.id, actor=actor, reason=reason, replaced_by=replaced_by)
        )


# --------------------------------------------------------------------------- one-tap learning (§38)


class RuleProposal(BaseModel):
    """'☑ Always use Hazel Tree for IKEA paid with card •••• 4817' — checked by default."""

    model_config = ConfigDict(frozen=True)

    label: str
    checked: bool = True
    rule: Rule


def _where(question: Question) -> tuple[RuleMatch, str] | None:
    facts = question.facts
    # Never show a resolved id ("sup_0a1b…") to people: fall back to plain words.
    readable_key = None if is_canonical_key(facts.counterparty_key) else facts.counterparty_key
    who = facts.counterparty_label or display_name(readable_key, fallback="this supplier")
    if facts.counterparty_key and facts.card_last4:
        return (
            RuleMatch(counterparty_key=facts.counterparty_key, card_last4=facts.card_last4),
            f"{who} paid with card {card_mask(facts.card_last4)}",
        )
    if facts.counterparty_key and facts.account_id:
        suffix = f" paid from {facts.account_label}" if facts.account_label else ""
        return RuleMatch(counterparty_key=facts.counterparty_key, account_id=facts.account_id), f"{who}{suffix}"
    if facts.counterparty_key:
        return RuleMatch(counterparty_key=facts.counterparty_key), who
    if facts.supplier_tax_id and normalize_tax_id(facts.supplier_tax_id):
        return RuleMatch(supplier_tax_id=facts.supplier_tax_id), who or "this supplier"
    return None


def suggest_rule_from_answer(
    question: Question,
    answer: Answer,
    *,
    author: RuleAuthor = RuleAuthor.OWNER,
    scope: RuleScope | None = None,
) -> RuleProposal | None:
    """Turn an answer into a one-tap rule proposal (§38), checked by default.

    Returns None when the answer is a one-off that should not be generalized
    ("Another company", "Other", actions) or the question has no stable fact
    to match on.
    """
    if answer.question_id != question.id:
        raise RuleError("the answer belongs to a different question")
    try:
        option = question.option(answer.option_id)
    except KeyError as exc:
        raise RuleError("the answer is not one of the question's options") from exc
    where = _where(question)
    if where is None:
        return None
    match, subject_text = where
    if option.kind is OptionKind.ENTITY:
        outcome = RuleOutcome(entity_id=option.entity_id, category=option.category)
        label = f"Always use {option.label} for {subject_text}"
    elif option.kind is OptionKind.PERSONAL:
        outcome = RuleOutcome(private=True)
        label = f"Always treat {subject_text} as personal"
    elif option.kind is OptionKind.CATEGORY:
        outcome = RuleOutcome(category=option.category, entity_id=option.entity_id)
        label = f"Always treat {subject_text} as {option.label[:1].lower()}{option.label[1:]}"
    else:
        return None
    resolved_scope = scope or (RuleScope.TENANT if author is RuleAuthor.OWNER else RuleScope.CLIENT)
    rule = Rule(
        author=author,
        author_id=answer.answered_by,
        scope=resolved_scope,
        tenant_id=None if resolved_scope is RuleScope.ALL_CLIENTS_OF_ACCOUNTANT else question.tenant_id,
        match=match,
        outcome=outcome,
        created_at=answer.answered_at,
        label=label,
    )
    return RuleProposal(label=label, checked=True, rule=rule)
