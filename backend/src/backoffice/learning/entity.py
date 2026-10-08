"""Entity Agent (§46, §51, §37, §38): which of the owner's companies does this belong to?

One human, many companies (§51). Each transaction or document is assigned to
one of the tenant's legal entities, to "Personal", or left for the owner with
a Needs-You question ("We aren't sure which company this belongs to.").

Facts ("votes") considered, strongest first:

* **Rule** — an explicit teaching from the owner or accountant (§38, §28),
  already resolved by :class:`~backoffice.learning.rules.RuleBook` precedence.
* **Invoice addressee** — the customer tax number printed on the invoice.
  Documentary evidence: a rule never silently overrides it.
* **Account / card ownership** — the account or card belongs to a company.
  A *personal* account or card is only a hint: owners connect personal cards
  precisely because some business costs end up there.
* **Billing name, billing address, receiving mailbox** (checklist H2, H3, H5) —
  the customer name printed on the invoice equals one company's legal or trade
  name; the billing address printed on it is one company's address; it was sent
  to a mailbox or alias that belongs to one company (set by the owner, a mailbox
  connected for one company only, or learned from earlier invoices). These are
  documentary *hints*: they never override a tax number, and a hint that
  disagrees with the tax number, the account or card, a rule or another hint
  becomes the one question, never a silent choice.
* **History** — how this counterparty was assigned before (>= 3 times, >= 80%).

Outcome quality (§57):

* GREEN — a rule nothing documentary contradicts, company-ownership facts
  that all agree with no weaker fact disagreeing, or two different hints
  (name, address, mailbox) that agree with nothing against them.
* AMBER — likely: only history agrees, a weak fact disagrees, a rule is
  contradicted by an ownership fact it did not match on, or the tenant has
  a single company and nothing points anywhere. A single hint on its own is
  AMBER and asked.
* RED — strong facts disagree, a hint disagrees with a stronger fact or with
  another hint, or saved answers disagree (§19): no assignment, the owner is
  asked.

When unsure (RED, nothing to go on, only a personal-card hint, one hint alone,
or an invoice addressed to another company) a question is produced with
options: each company, Personal, Another company.

A company card at a shop that is clearly personal for that company (a
streaming service, a supermarket for a business that does not sell food,
:mod:`.personal`), with no rule, no invoice naming the company and no history of
the company paying that merchant, is asked as "Was this Netflix payment for
Hazel Tree or personal?" (checklist X23) rather than booked on the card's word.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import Enum
from functools import cache

from pydantic import BaseModel, ConfigDict, Field

from backoffice.countries import LazyPattern, pack_alternatives, pack_words
from backoffice.domain.models import Document, LegalEntity, Quality, Transaction

from .keys import counterparty_key, display_name, fold, qualified_tax_id, same_tax_id
from .plain import card_mask
from .questions import (
    OptionKind,
    Question,
    QuestionKind,
    QuestionOption,
    SubjectFacts,
    describe_subject,
)
from .rules import PERSONAL, FieldDecision, RuleAuthor, RuleBook, RuleField, RuleSubject

__all__ = [
    "ANOTHER_COMPANY",
    "HISTORY_MIN_COUNT",
    "HISTORY_MIN_SHARE",
    "UNSURE_PROMPT",
    "Addressee",
    "CompanyDirectory",
    "EntityAssignment",
    "OwnershipBook",
    "Vote",
    "VoteKind",
    "assign_entity",
    "build_history",
    "same_address",
]

ANOTHER_COMPANY = "another_company"
UNSURE_PROMPT = "We aren't sure which company this belongs to."
HISTORY_MIN_COUNT = 3
HISTORY_MIN_SHARE = Decimal("0.8")


class OwnershipBook(BaseModel):
    """Who owns which account and card. Values are entity ids or ``PERSONAL``."""

    model_config = ConfigDict(frozen=True)

    accounts: dict[str, str] = Field(default_factory=dict)
    cards: dict[str, str] = Field(default_factory=dict)  # card last 4 digits -> owner
    account_labels: dict[str, str] = Field(default_factory=dict)  # "Millennium •••• 1234"


class CompanyDirectory(BaseModel):
    """What the owner's companies are known by besides their tax numbers (checklist H2, H3, H5).

    ``names``: each company's legal and trade names (the company's own ``name`` always counts too);
    ``addresses``: its postal addresses; ``mailboxes``: a mailbox or alias (lower case) -> the company
    whose mail it receives.
    """

    model_config = ConfigDict(frozen=True)

    names: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    addresses: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    mailboxes: dict[str, str] = Field(default_factory=dict)


@dataclass(frozen=True)
class Addressee:
    """Who a document is made out to besides the tax number, and where it arrived.

    ``name``: the customer (billing) name printed on it; ``address``: the billing address printed
    on it; ``mailboxes``: the addresses the email carrying it was sent to.
    """

    name: str | None = None
    address: str | None = None
    mailboxes: tuple[str, ...] = ()

    @property
    def empty(self) -> bool:
        return not (self.name or self.address or self.mailboxes)


class VoteKind(str, Enum):
    RULE = "rule"
    INVOICE_ADDRESSEE = "invoice_addressee"
    ACCOUNT = "account"
    CARD = "card"
    HISTORY = "history"
    BILLING_NAME = "billing_name"
    BILLING_ADDRESS = "billing_address"
    MAILBOX = "mailbox"


# Documentary hints: never override a tax number; a disagreement becomes the one question.
_HINTS = frozenset({VoteKind.BILLING_NAME, VoteKind.BILLING_ADDRESS, VoteKind.MAILBOX})


@dataclass(frozen=True)
class Vote:
    kind: VoteKind
    target: str  # entity id, PERSONAL or ANOTHER_COMPANY
    why: str
    strong: bool
    covers: frozenset[VoteKind] = frozenset()  # RULE only: facts the rule matched on


class EntityAssignment(BaseModel):
    model_config = ConfigDict(frozen=True)

    subject_type: str
    subject_id: str
    entity_id: str | None
    private: bool
    quality: Quality
    why: tuple[str, ...]
    question: Question | None = None
    rule_id: str | None = None

    @property
    def needs_owner(self) -> bool:
        return self.question is not None


def build_history(pairs: Iterable[tuple[str, str]]) -> dict[str, dict[str, int]]:
    """{counterparty key: {entity id or PERSONAL: count}} from past (key, target) pairs."""
    history: dict[str, dict[str, int]] = {}
    for key, target in pairs:
        bucket = history.setdefault(key, {})
        bucket[target] = bucket.get(target, 0) + 1
    return history


# --------------------------------------------------------------------------- votes


@dataclass(frozen=True)
class _Context:
    entities: Mapping[str, LegalEntity]
    names: Mapping[str, str]
    counterparty: str  # owner-facing counterparty name


def _target_name(target: str, ctx: _Context) -> str:
    return ctx.names.get(target, "another company")


def _rule_vote(decision: FieldDecision, ctx: _Context) -> Vote | None:
    target = decision.value
    if target != PERSONAL and target not in ctx.entities:
        return None  # the company was removed; a stale rule is not evidence
    covers = frozenset(
        kind
        for criterion, kind in (("card_last4", VoteKind.CARD), ("account_id", VoteKind.ACCOUNT))
        if criterion in decision.matched_on
    )
    if decision.author is RuleAuthor.ACCOUNTANT:
        why = "Your accountant set this as personal." if target == PERSONAL else (
            f"Your accountant set this to {_target_name(target, ctx)}."
        )
    elif decision.label:
        why = f"You told us: {decision.label}."
    else:
        why = "You told us this is personal." if target == PERSONAL else (
            f"You told us this belongs to {_target_name(target, ctx)}."
        )
    return Vote(VoteKind.RULE, target, why, strong=True, covers=covers)


def _addressee_vote(doc: Document, ctx: _Context) -> Vote | None:
    if not doc.customer_tax_id:
        return None
    for entity in ctx.entities.values():
        # The company's own number gets its country, so the same digits from
        # another country (ES509123456 vs our PT 509123456) never count as ours.
        if same_tax_id(qualified_tax_id(entity.tax_id, entity.country), doc.customer_tax_id):
            why = f"Invoice addressed to {ctx.names[entity.id]}"
            return Vote(VoteKind.INVOICE_ADDRESSEE, entity.id, why, strong=True)
    shown = " ".join(doc.customer_tax_id.split())  # extracted text: one clean line for people
    why = f"Invoice addressed to another company (tax number {shown})"
    return Vote(VoteKind.INVOICE_ADDRESSEE, ANOTHER_COMPANY, why, strong=True)


def _ownership_votes(tx: Transaction, ownership: OwnershipBook, ctx: _Context) -> list[Vote]:
    votes: list[Vote] = []
    card_owner = ownership.cards.get(tx.card_last4) if tx.card_last4 else None
    if tx.card_last4 and card_owner is not None and (card_owner == PERSONAL or card_owner in ctx.entities):
        card = f"card {card_mask(tx.card_last4)}"
        if card_owner == PERSONAL:
            votes.append(Vote(VoteKind.CARD, PERSONAL, f"Paid with a personal {card}", strong=False))
        else:
            votes.append(Vote(VoteKind.CARD, card_owner, f"Paid with {ctx.names[card_owner]}'s {card}", strong=True))
    account_owner = ownership.accounts.get(tx.account_id)
    if account_owner is not None and (account_owner == PERSONAL or account_owner in ctx.entities):
        verb = "Paid from" if tx.amount < 0 else "Received in"
        if account_owner == PERSONAL:
            votes.append(Vote(VoteKind.ACCOUNT, PERSONAL, f"{verb} a personal account", strong=False))
        else:
            owner = ctx.names[account_owner]
            votes.append(Vote(VoteKind.ACCOUNT, account_owner, f"{verb} {owner}'s account", strong=True))
    return votes


def _names_of(entity_id: str, ctx: _Context, directory: CompanyDirectory) -> set[str]:
    names = [ctx.entities[entity_id].name, *directory.names.get(entity_id, ())]
    return {k for n in names if (k := counterparty_key(n))}


def _billing_name_vote(name: str | None, ctx: _Context, directory: CompanyDirectory) -> Vote | None:
    """The customer name printed on the invoice is exactly one company's legal or trade name (H2)."""
    key = counterparty_key(name)
    if not key:
        return None
    hits = sorted(e for e in ctx.entities if key in _names_of(e, ctx, directory))
    if len(hits) != 1:
        return None  # nobody's, or two companies share it: not evidence of either
    shown = " ".join((name or "").split())
    return Vote(VoteKind.BILLING_NAME, hits[0], f"Made out to {shown}", strong=False)


# A postal code as a pack's country writes it ("entity.postal_code": Portugal's 1200-384, its parts as groups), and
# words that are never part of an address (with a pack's own: "entity.address_filler").
_POSTAL_CODE = LazyPattern(lambda: pack_alternatives("entity.postal_code"))
_ADDRESS_FILLER = frozenset({"n", "no", "nr", "num", "numero", "number", "de", "the", "of"})


@cache
def _address_filler() -> frozenset[str]:
    return _ADDRESS_FILLER | frozenset(pack_words("entity.address_filler"))


def _address_parts(text: str) -> tuple[str | None, frozenset[str]]:
    folded = fold(text)
    found = _POSTAL_CODE.search(folded)
    code = "-".join(g for g in found.groups() if g) if found else None
    rest = _POSTAL_CODE.sub(" ", folded)
    return code, frozenset(t for t in re.split(r"[^a-z0-9]+", rest) if t and t not in _address_filler())


def same_address(known: str, printed: str) -> bool:
    """``printed`` is the address ``known``: every word and number of ``known`` is in it (the house number
    included), and the postal codes agree when both show one. Never a fuzzy match."""
    known_code, known_words = _address_parts(known)
    printed_code, printed_words = _address_parts(printed)
    if len(known_words) < 3 or not any(w.isdigit() for w in known_words | ({known_code} if known_code else set())):
        return False  # too little to tell one address from another
    if known_code and printed_code and known_code != printed_code:
        return False
    return known_words <= printed_words


def _billing_address_vote(address: str | None, ctx: _Context, directory: CompanyDirectory) -> Vote | None:
    """The billing address printed on the invoice is one company's address (H3)."""
    if not address or not address.strip():
        return None
    hits = sorted(e for e in ctx.entities if any(same_address(a, address) for a in directory.addresses.get(e, ())))
    if len(hits) != 1:
        return None
    shown = " ".join(address.split())
    return Vote(VoteKind.BILLING_ADDRESS, hits[0], f"Billed to {ctx.names[hits[0]]}'s address ({shown})", strong=False)


def _mailbox_votes(mailboxes: Sequence[str], ctx: _Context, directory: CompanyDirectory) -> list[Vote]:
    """It was sent to a mailbox or alias that belongs to one company (H5): one vote per company named."""
    votes: dict[str, Vote] = {}
    for raw in mailboxes:
        address = (raw or "").strip().lower()
        target = directory.mailboxes.get(address)
        if target is None or target not in ctx.entities or target in votes:
            continue
        votes[target] = Vote(VoteKind.MAILBOX, target, f"Sent to {address}, {ctx.names[target]}'s mailbox",
                             strong=False)
    return [votes[t] for t in sorted(votes)]


def _history_vote(key: str | None, history: Mapping[str, Mapping[str, int]], ctx: _Context) -> Vote | None:
    counts = history.get(key or "", {})
    total = sum(counts.values())
    if total < HISTORY_MIN_COUNT:
        return None
    target = min(counts, key=lambda t: (-counts[t], t))
    if Decimal(counts[target]) / Decimal(total) < HISTORY_MIN_SHARE:
        return None
    if target != PERSONAL and target not in ctx.entities:
        return None
    why = f"{ctx.counterparty} is usually personal" if target == PERSONAL else (
        f"{ctx.counterparty} usually belongs to {ctx.names[target]}"
    )
    return Vote(VoteKind.HISTORY, target, why, strong=False)


# --------------------------------------------------------------------------- decision


@dataclass(frozen=True)
class _Resolution:
    target: str | None
    quality: Quality
    ask: bool


def _resolve(votes: Sequence[Vote], *, rule_conflict: bool, single_entity: str | None) -> _Resolution:
    if rule_conflict:
        return _Resolution(None, Quality.RED, ask=True)
    rule = next((v for v in votes if v.kind is VoteKind.RULE), None)
    facts = [v for v in votes if v.kind is not VoteKind.RULE]
    hints = [v for v in facts if v.kind in _HINTS]
    strong = [v for v in facts if v.strong]
    weak = [v for v in facts if not v.strong and v.kind not in _HINTS]
    if len({v.target for v in hints}) > 1:
        return _Resolution(None, Quality.RED, ask=True)  # the name, address or mailbox disagree
    if rule is not None:
        against = [v for v in strong if v.target != rule.target]
        if any(v.kind is VoteKind.INVOICE_ADDRESSEE for v in against) or any(v.target != rule.target for v in hints):
            return _Resolution(None, Quality.RED, ask=True)
        uncovered = [v for v in against if v.kind not in rule.covers]
        return _Resolution(rule.target, Quality.AMBER if uncovered else Quality.GREEN, ask=False)
    if strong:
        targets = {v.target for v in strong}
        if len(targets) > 1:
            return _Resolution(None, Quality.RED, ask=True)
        target = targets.pop()
        if any(v.target != target for v in hints):
            # A hint never overrides the tax number, the account or the card: the disagreement is asked.
            return _Resolution(None, Quality.RED, ask=True)
        if target == ANOTHER_COMPANY:
            return _Resolution(None, Quality.AMBER, ask=True)
        disagreeing = any(v.target != target for v in weak)
        return _Resolution(target, Quality.AMBER if disagreeing else Quality.GREEN, ask=False)
    if hints:
        target = hints[0].target
        confirmed = len({v.kind for v in hints}) >= 2 and not any(v.target != target for v in weak)
        # Two different documentary hints agree: enough. One alone is likely, and asked.
        return _Resolution(target, Quality.GREEN if confirmed else Quality.AMBER, ask=not confirmed)
    history = [v for v in weak if v.kind is VoteKind.HISTORY]
    if history and len({v.target for v in weak}) == 1:
        return _Resolution(history[0].target, Quality.AMBER, ask=False)
    if not weak and single_entity is not None:
        return _Resolution(single_entity, Quality.AMBER, ask=False)
    # Hints only (a personal card, split history): unsure, not a conflict between facts.
    return _Resolution(None, Quality.AMBER, ask=True)


# --------------------------------------------------------------------------- public API


def assign_entity(
    *,
    entities: Sequence[LegalEntity],
    transaction: Transaction | None = None,
    document: Document | None = None,
    ownership: OwnershipBook | None = None,
    rulebook: RuleBook | None = None,
    accountant_ids: Iterable[str] = (),
    history: Mapping[str, Mapping[str, int]] | None = None,
    key: str | None = None,
    today: date | None = None,
    addressee: Addressee | None = None,
    directory: CompanyDirectory | None = None,
    personal_signal: str | None = None,
) -> EntityAssignment:
    """Assign a transaction, a document, or a matched pair of both (§46 Entity Agent).

    ``key`` overrides the counterparty key (e.g. a resolved supplier id). ``addressee`` is the
    document's billing name and address and the mailboxes it was sent to, compared with what the
    companies are known by (``directory``). ``personal_signal``: the purchase is at a shop that is
    clearly personal for the card's company ("a streaming service", :func:`.personal.personal_signal`).
    """
    if transaction is None and document is None:
        raise ValueError("assign a transaction, a document, or both")
    tenant_id = transaction.tenant_id if transaction is not None else document.tenant_id  # type: ignore[union-attr]
    if document is not None and document.tenant_id != tenant_id:
        raise ValueError("transaction and document belong to different tenants")
    own = [e for e in entities if e.tenant_id == tenant_id]
    if not own:
        raise ValueError("the tenant has no companies")
    raw_name = transaction.counterparty if transaction is not None else document.supplier_name  # type: ignore[union-attr]
    series_key = key or counterparty_key(raw_name) or counterparty_key(document.supplier_name if document else None)
    ctx = _Context(
        entities={e.id: e for e in own},
        names={e.id: display_name(e.name, fallback=e.name) for e in own},
        counterparty=display_name(raw_name, fallback="This supplier"),
    )

    votes: list[Vote] = []
    rule_conflict = False
    rule_id: str | None = None
    if rulebook is not None:
        subject = _rule_subject(transaction, document, series_key)
        decision = rulebook.evaluate(subject, tenant_id=tenant_id, accountant_ids=accountant_ids)
        rule_conflict = decision.unresolved(RuleField.ENTITY) is not None
        entity_decision = decision.get(RuleField.ENTITY)
        vote = _rule_vote(entity_decision, ctx) if entity_decision else None
        if vote is not None:
            votes.append(vote)
            rule_id = entity_decision.rule_id  # type: ignore[union-attr]
    if document is not None and (vote := _addressee_vote(document, ctx)):
        votes.append(vote)
    if addressee is not None and not addressee.empty:
        known = directory or CompanyDirectory()
        for found in (_billing_name_vote(addressee.name, ctx, known),
                      _billing_address_vote(addressee.address, ctx, known)):
            if found is not None:
                votes.append(found)
        votes.extend(_mailbox_votes(addressee.mailboxes, ctx, known))
    if transaction is not None:
        votes.extend(_ownership_votes(transaction, ownership or OwnershipBook(), ctx))
    if history and (vote := _history_vote(series_key, history, ctx)):
        votes.append(vote)

    single = own[0].id if len(own) == 1 else None
    resolution = _resolve(votes, rule_conflict=rule_conflict, single_entity=single)
    why = [v.why for v in votes]
    if rule_conflict:
        why.insert(0, "Two saved answers disagree about this.")
    if resolution.target == single and not votes and single is not None:
        why.append("You have one company.")
    if resolution.ask and not why:
        why.append("Nothing on it shows which company it is for.")

    subject_type, subject_id = ("transaction", transaction.id) if transaction else ("document", document.id)  # type: ignore[union-attr]
    question = None
    personal = _personal_purchase(resolution, votes, transaction, personal_signal, series_key, history)
    if personal is not None:
        # A company card at a shop that is clearly personal, with nothing else saying it is the company's:
        # one plain question, never booked to the company on the card's word (checklist X23).
        company = personal
        shop = _WEB_SUFFIX.sub("", ctx.counterparty) or ctx.counterparty  # "Netflix.com" -> "Netflix"
        why += [f"{shop} is {personal_signal}, usually a personal cost",
                f"Nothing shows {ctx.names[company]} paying {shop} before"]
        facts = _facts(subject_type, subject_id, transaction, document, series_key, ctx, ownership)
        facts = facts.model_copy(update={"counterparty_label": shop})
        question = _personal_question(tenant_id, facts, ctx, own, company, why, today)
        resolution = _Resolution(None, Quality.AMBER, ask=True)
    elif resolution.ask:
        facts = _facts(subject_type, subject_id, transaction, document, series_key, ctx, ownership)
        question = _question(tenant_id, facts, ctx, own, why, today)
    target = resolution.target
    return EntityAssignment(
        subject_type=subject_type,
        subject_id=subject_id,
        entity_id=target if target not in (None, PERSONAL) else None,
        private=target == PERSONAL,
        quality=resolution.quality,
        why=tuple(why),
        question=question,
        rule_id=rule_id if resolution.target is not None and any(v.kind is VoteKind.RULE for v in votes) else None,
    )


def _rule_subject(tx: Transaction | None, doc: Document | None, key: str | None) -> RuleSubject:
    subject = RuleSubject.from_transaction(tx, key=key) if tx is not None else RuleSubject()
    if doc is not None:
        subject = subject.merged(RuleSubject.from_document(doc, key=key))
    return subject


def _facts(
    subject_type: str,
    subject_id: str,
    tx: Transaction | None,
    doc: Document | None,
    key: str | None,
    ctx: _Context,
    ownership: OwnershipBook | None,
) -> SubjectFacts:
    amount: Decimal | None = abs(tx.amount) if tx is not None else None
    if amount is None and doc is not None and doc.gross_amount is not None:
        amount = abs(doc.gross_amount)
    account_id = tx.account_id if tx is not None else None
    return SubjectFacts(
        subject_type=subject_type,
        subject_id=subject_id,
        counterparty_key=key,
        counterparty_label=ctx.counterparty,
        card_last4=tx.card_last4 if tx is not None else None,
        account_id=account_id,
        account_label=(ownership.account_labels.get(account_id) if ownership and account_id else None),
        supplier_tax_id=doc.supplier_tax_id if doc is not None else None,
        amount=amount,
        currency=tx.currency if tx is not None else doc.currency,  # type: ignore[union-attr]
        on=tx.booked_on if tx is not None else doc.issue_date,  # type: ignore[union-attr]
    )


_WEB_SUFFIX = re.compile(r"\.(?:com|net|org|pt|es|eu|io|co\.uk)$", re.IGNORECASE)


def _personal_purchase(
    resolution: _Resolution,
    votes: Sequence[Vote],
    transaction: Transaction | None,
    signal: str | None,
    key: str | None,
    history: Mapping[str, Mapping[str, int]] | None,
) -> str | None:
    """The card's company, when a purchase at a clearly personal shop would otherwise go to it on the card
    alone: no rule, no invoice naming a company, no billing hint and no history of that company paying it."""
    if not signal or transaction is None or transaction.amount >= 0 or resolution.target in (None, PERSONAL):
        return None
    if resolution.quality is not Quality.GREEN:
        return None
    if not any(v.kind is VoteKind.CARD and v.target == resolution.target for v in votes):
        return None  # only a company card is questioned: a transfer from an account is not a shop purchase
    if any(v.kind in (VoteKind.RULE, VoteKind.INVOICE_ADDRESSEE, *_HINTS) for v in votes):
        return None
    earlier = (history or {}).get(key or "", {})
    if any(target != PERSONAL and count > 0 for target, count in earlier.items()):
        return None  # the business has paid this merchant before: ordinary business history
    return resolution.target


def _personal_question(
    tenant_id: str,
    facts: SubjectFacts,
    ctx: _Context,
    entities: Sequence[LegalEntity],
    company: str,
    why: Sequence[str],
    today: date | None,
) -> Question:
    """'Was this Netflix payment for Hazel Tree or personal?' The card's company first, then Personal,
    then the owner's other companies. Answering can become a one-tap rule (§38)."""
    others = sorted((e for e in entities if e.id != company), key=lambda e: (ctx.names[e.id].casefold(), e.id))
    options = [QuestionOption(id=f"entity:{company}", label=ctx.names[company], kind=OptionKind.ENTITY,
                              entity_id=company),
               QuestionOption(id="personal", label="Personal", kind=OptionKind.PERSONAL)]
    options += [QuestionOption(id=f"entity:{e.id}", label=ctx.names[e.id], kind=OptionKind.ENTITY, entity_id=e.id)
                for e in others]
    return Question(
        tenant_id=tenant_id,
        kind=QuestionKind.WHICH_COMPANY,
        prompt=f"Was this {facts.counterparty_label} payment for {ctx.names[company]} or personal?",
        detail=describe_subject(facts, today),
        options=tuple(options),
        why=tuple(why),
        facts=facts,
        series_key=facts.counterparty_key,
    )


def _question(
    tenant_id: str,
    facts: SubjectFacts,
    ctx: _Context,
    entities: Sequence[LegalEntity],
    why: Sequence[str],
    today: date | None,
) -> Question:
    ordered = sorted(entities, key=lambda e: (ctx.names[e.id].casefold(), e.id))
    options = [
        QuestionOption(id=f"entity:{e.id}", label=ctx.names[e.id], kind=OptionKind.ENTITY, entity_id=e.id)
        for e in ordered
    ]
    options.append(QuestionOption(id="personal", label="Personal", kind=OptionKind.PERSONAL))
    options.append(QuestionOption(id="another_company", label="Another company", kind=OptionKind.ANOTHER_COMPANY))
    return Question(
        tenant_id=tenant_id,
        kind=QuestionKind.WHICH_COMPANY,
        prompt=UNSURE_PROMPT,
        detail=describe_subject(facts, today),
        options=tuple(options),
        why=tuple(why),
        facts=facts,
        series_key=facts.counterparty_key,
    )
