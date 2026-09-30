"""Cost Center Agent's reasoning (§46, §54, §37–38): which job, property, vehicle ... a cost is for.

Only companies that keep cost centers are ever considered; a company without
them is never asked anything. For one payment or document, strongest first,
each fact with the reason shown under "Why?":

1. **Rule** the owner taught with one tap ("Always put Leroy Merlin paid with
   card •••• 5530 on Job Rua das Flores"), or a learned split ("Always split
   EDP: Apartment 1A 40%, ..."). A rule never silently overrides the evidence:
   when the invoice itself names another cost center, the owner is asked.
2. **Identifiers on the evidence**: a project code or reference, a site or
   property address, a license plate, a booking or property name, a customer
   tax number (a reseller's line), an email alias, a keyword. An invoice whose
   lines name different cost centers is split by its lines, exactly per VAT
   rate; lines that do not add up to the invoice are asked about, not guessed.
3. **Card or account** that belongs to one cost center (a technician's card, a
   vehicle's fuel card).
4. **Supplier history**: every earlier payment to this supplier (at least
   three) went to the same cost center. Likely, not proven (AMBER), and shown
   as such.

When facts disagree, or none is strong enough, the result carries one plain
question ("Which job is this for?") with one-tap options, "General costs"
and, from the owner's answer, an "Always ..." rule (:func:`suggest_cost_center_rule`).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_DOWN, Decimal

from pydantic import BaseModel, ConfigDict

from backoffice.domain.cost_centers import (
    CostCenter,
    SplitError,
    split_by_lines,
    split_by_percent,
)
from backoffice.domain.models import (
    AllocationMethod,
    AllocationShare,
    CostAllocation,
    DocumentLine,
    Quality,
    VatPart,
)

from .keys import fold, match_key, same_tax_id
from .plain import card_mask, join_and
from .questions import OptionKind, Question, QuestionKind, QuestionOption, SubjectFacts, describe_subject
from .rules import (
    GENERAL,
    Rule,
    RuleAuthor,
    RuleBook,
    RuleField,
    RuleOutcome,
    RuleProposal,
    RuleScope,
    RuleSubject,
    _where,
)

__all__ = [
    "HISTORY_MIN_COUNT",
    "SPLIT_OPTION",
    "CostCenterDecision",
    "CostCenterFacts",
    "Hit",
    "RechargeDecision",
    "cost_center_question",
    "decide_cost_center",
    "decide_recharge",
    "find_hits",
    "history_counts",
    "noun_for",
    "percents_of",
    "plural",
    "recharge_rule",
    "suggest_cost_center_rule",
]

HISTORY_MIN_COUNT = 3
SPLIT_OPTION = "split"  # the answer "split it between several", sent with the amounts or percentages
_FILLER = frozenset({"n", "o", "no", "nr", "num", "numero", "nro"})


@dataclass(frozen=True)
class CostCenterFacts:
    """What one payment or document (or a payment with its invoices) says, for choosing its cost center."""

    tenant_id: str
    company_id: str
    subject_type: str  # "transaction" | "document"
    subject_id: str
    total: Decimal  # absolute amount
    currency: str = "EUR"
    counterparty_key: str | None = None  # the resolved supplier id when known, else the name key
    counterparty_label: str = "This supplier"
    card_last4: str | None = None
    account_id: str | None = None
    account_label: str | None = None
    supplier_tax_id: str | None = None
    texts: tuple[tuple[str, str], ...] = ()  # (where, in plain words, e.g. "the invoice"; the text)
    emails: tuple[str, ...] = ()  # addresses the document was sent from or to
    lines: tuple[DocumentLine, ...] = ()
    vat_parts: tuple[VatPart, ...] = ()  # the total by VAT rate, when known
    on: date | None = None
    evidence_ids: tuple[str, ...] = ()
    customer_tax_ids: tuple[str, ...] = ()  # who the invoices are addressed to (a client, not the business)


@dataclass(frozen=True)
class Hit:
    """One identifier of one cost center, found on the evidence."""

    cost_center_id: str
    kind: str  # address | plate | reference | tax_id | email | keyword | name
    value: str  # as the owner entered it
    where: str  # "the invoice", "the bank line", "line 3"


class CostCenterDecision(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    subject_type: str
    subject_id: str
    allocation: CostAllocation | None = None
    question: Question | None = None
    why: tuple[str, ...] = ()

    @property
    def needs_owner(self) -> bool:
        return self.question is not None


# --------------------------------------------------------------------------- words


def noun_for(centers: Sequence[CostCenter]) -> str:
    """The business's own word, lower-case: 'job', 'property', 'vehicle or outlet', or 'one'."""
    kinds = list(dict.fromkeys(c.kind for c in centers))
    if len(kinds) == 1:
        return kinds[0].lower()
    if len(kinds) == 2:
        return f"{kinds[0].lower()} or {kinds[1].lower()}"
    return "one"


def plural(word: str) -> str:
    """'Job' -> 'Jobs', 'Property' -> 'Properties', 'Course' -> 'Courses'."""
    if not word:
        return word
    lower = word.lower()
    if lower.endswith("y") and len(word) > 1 and lower[-2] not in "aeiou":
        return word[:-1] + ("IES" if word.isupper() else "ies")
    if lower.endswith(("s", "x", "ch", "sh", "z")):
        return word + "es"
    return word + "s"


def _general_label(centers: Sequence[CostCenter]) -> str:
    noun = noun_for(centers)
    return "General costs" if noun == "one" else f"General costs, not for one {noun}"


# --------------------------------------------------------------------------- identifiers


def _chunks(value: str) -> list[str]:
    return re.findall(r"[a-z]+|[0-9]+", fold(value))


def _code_pattern(value: str) -> re.Pattern[str] | None:
    """A code, plate, name or keyword: its letters and digits in order, separators optional."""
    chunks = _chunks(value)
    if sum(len(c) for c in chunks) < 3:
        return None  # too short to mean one thing
    body = r"[^a-z0-9]{0,2}".join(re.escape(c) for c in chunks)
    return re.compile(rf"(?<![a-z0-9]){body}(?![a-z0-9])")


def _tokens(text: str) -> list[str]:
    return [t for t in re.sub(r"[^a-z0-9]+", " ", fold(text)).split() if t not in _FILLER]


def _tax_pattern(value: str) -> re.Pattern[str] | None:
    compact = re.sub(r"[^0-9A-Za-z]", "", value).lower()
    body = re.sub(r"^[a-z]{2}(?=\d)", "", compact)
    if len(body) < 5:
        return None
    digits = r"[\s.]?".join(re.escape(ch) for ch in body)
    return re.compile(rf"(?<![a-z0-9])(?:[a-z]{{2}}\s?)?{digits}(?![0-9])")


def _name_counts(center: CostCenter) -> bool:
    """A cost center's own name is an identifier when it is specific enough to mean only it.

    Not when the owner gave its addresses: a job named after its street would
    otherwise match the same street on a supplier's letterhead.
    """
    if center.identifiers.addresses:
        return False
    chunks = _chunks(center.name)
    letters = sum(len(c) for c in chunks)
    return letters >= 5 and (len(chunks) >= 2 or any(c.isdigit() for c in chunks))


def find_hits(text: str, centers: Sequence[CostCenter], where: str) -> list[Hit]:
    """Every identifier of ``centers`` that appears in ``text`` (accents, case and separators ignored)."""
    if not text or not text.strip():
        return []
    folded = fold(text)
    tokens = f" {' '.join(_tokens(text))} "
    hits: list[Hit] = []
    for c in centers:
        ids = c.identifiers
        for address in ids.addresses:
            wanted = _tokens(address)
            if len(wanted) >= 2 and f" {' '.join(wanted)} " in tokens:
                hits.append(Hit(c.id, "address", address, where))
        for kind, values in (("reference", ids.references), ("plate", ids.plates), ("keyword", ids.keywords)):
            for value in values:
                pattern = _code_pattern(value)
                if pattern is not None and pattern.search(folded):
                    hits.append(Hit(c.id, kind, value, where))
        for tax_id in ids.tax_ids:
            pattern = _tax_pattern(tax_id)
            if pattern is not None and pattern.search(folded):
                hits.append(Hit(c.id, "tax_id", tax_id, where))
        for email in ids.emails:
            if re.search(rf"(?<![\w.+-]){re.escape(email)}(?![\w-])", folded):
                hits.append(Hit(c.id, "email", email, where))
        if _name_counts(c) and not any(h.cost_center_id == c.id for h in hits):
            pattern = _code_pattern(c.name)
            if pattern is not None and pattern.search(folded):
                hits.append(Hit(c.id, "name", c.name, where))
    return hits


def _email_hits(emails: Sequence[str], centers: Sequence[CostCenter]) -> list[Hit]:
    wanted = {e.lower() for e in emails}
    return [Hit(c.id, "email", e, "the email") for c in centers for e in c.identifiers.emails if e in wanted]


def _hit_line(hit: Hit, center: CostCenter) -> str:
    where = hit.where[:1].upper() + hit.where[1:]
    shows = {
        "address": f"shows the address {hit.value}",
        "plate": f"shows the plate {hit.value}",
        "reference": f"names {hit.value}",
        "tax_id": f"names the tax number {hit.value}",
        "keyword": f"mentions {hit.value}",
        "name": f"names {hit.value}",
    }
    if hit.kind == "email":
        verb = "came through" if hit.where == "the email" else "names"
        return f"{where} {verb} {hit.value}, which you use for {center.label}."
    return f"{where} {shows[hit.kind]}, which is {center.label}."


# --------------------------------------------------------------------------- allocations


def _parts_for(facts: CostCenterFacts) -> tuple[VatPart, ...]:
    parts = facts.vat_parts
    if parts and sum((p.gross for p in parts), Decimal(0)) == facts.total:
        return parts
    return ()


def _single(cid: str, facts: CostCenterFacts, method: AllocationMethod, why: Sequence[str], *,
            quality: Quality = Quality.GREEN, rule_id: str | None = None) -> CostAllocation:
    share = AllocationShare(cost_center_id=cid, amount=facts.total, parts=_parts_for(facts))
    return CostAllocation(total=facts.total, currency=facts.currency, shares=(share,), method=method,
                          quality=quality, why=tuple(why), rule_id=rule_id, evidence_ids=facts.evidence_ids)


def _general(facts: CostCenterFacts, method: AllocationMethod, why: Sequence[str], *,
             quality: Quality = Quality.GREEN, rule_id: str | None = None) -> CostAllocation:
    return CostAllocation(total=facts.total, currency=facts.currency, general=True, method=method, quality=quality,
                          why=tuple(why), rule_id=rule_id, evidence_ids=facts.evidence_ids)


def percents_of(shares: Sequence[AllocationShare], total: Decimal) -> tuple[tuple[str, Decimal], ...]:
    """The shares as percentages adding up to exactly 100 (to the hundredth; leftover to the largest)."""
    if total <= 0 or not shares:
        return ()
    if all(s.percent is not None for s in shares) and sum((s.percent for s in shares), Decimal(0)) == 100:  # type: ignore[misc]
        return tuple((s.cost_center_id, s.percent) for s in shares)  # type: ignore[misc]
    raw = [(s.amount * 100 / total).quantize(Decimal("0.01"), rounding=ROUND_DOWN) for s in shares]
    biggest = max(range(len(shares)), key=lambda i: (shares[i].amount, -i))
    raw[biggest] += Decimal(100) - sum(raw, Decimal(0))
    return tuple((s.cost_center_id, p) for s, p in zip(shares, raw, strict=True))


def _pct(value: Decimal) -> str:
    text = f"{value.normalize():f}" if value == value.to_integral_value() else f"{value:f}".rstrip("0").rstrip(".")
    return f"{text}%"


def _split_words(split: Sequence[tuple[str, Decimal]], names: Mapping[str, str]) -> str:
    return join_and([f"{names.get(cid, 'another one')} {_pct(p)}" for cid, p in split])


# --------------------------------------------------------------------------- the decision


def decide_cost_center(
    *,
    centers: Sequence[CostCenter],
    facts: CostCenterFacts,
    rulebook: RuleBook | None = None,
    accountant_ids: Iterable[str] = (),
    history: Mapping[str, Mapping[str, int]] | None = None,
    today: date | None = None,
) -> CostCenterDecision:
    """Choose the cost center(s) for one payment or document, or the one question to ask (module docstring)."""
    own = sorted((c for c in centers if c.active and c.company_id == facts.company_id
                  and c.tenant_id == facts.tenant_id), key=lambda c: (c.label.casefold(), c.id))
    base = {"subject_type": facts.subject_type, "subject_id": facts.subject_id}
    if not own or facts.total <= 0:
        return CostCenterDecision(**base)
    by_id = {c.id: c for c in own}
    names = {c.id: c.label for c in own}
    who = facts.counterparty_label

    header: list[Hit] = []
    for where, text in facts.texts:
        header += find_hits(text, own, where)
    header += _email_hits(facts.emails, own)
    by_line = {line.id: find_hits(line.text, own, f"line {line.id} of the invoice") for line in facts.lines}

    def hit_lines(hits: Iterable[Hit]) -> list[str]:
        seen: list[str] = []
        for h in hits:
            line = _hit_line(h, by_id[h.cost_center_id])
            if line not in seen:
                seen.append(line)
        return seen

    def ask(why: Sequence[str]) -> CostCenterDecision:
        lines = list(dict.fromkeys(why)) or [f"Nothing on it shows which {noun_for(own)} it is for."]
        return CostCenterDecision(**base, question=cost_center_question(own, facts, lines, today), why=tuple(lines))

    def done(allocation: CostAllocation) -> CostCenterDecision:
        return CostCenterDecision(**base, allocation=allocation, why=allocation.why)

    # 1. a rule the owner taught
    if rulebook is not None:
        subject = RuleSubject(counterparty_key=facts.counterparty_key, card_last4=facts.card_last4,
                              account_id=facts.account_id, amount=facts.total, supplier_tax_id=facts.supplier_tax_id)
        decision = rulebook.evaluate(subject, tenant_id=facts.tenant_id, accountant_ids=accountant_ids)
        if decision.unresolved(RuleField.COST_CENTER) is not None:
            return ask(["Two saved answers disagree about this.", *hit_lines(header)])
        taught = decision.get(RuleField.COST_CENTER)
        if taught is not None:
            value = taught.value
            reason = (f"You told us: {taught.label}." if taught.label else
                      "You told us this is general costs." if value == GENERAL else
                      f"You told us this is for {names.get(value, 'it')}." if isinstance(value, str) else
                      "You told us how to split it.")
            if taught.author is RuleAuthor.ACCOUNTANT:
                reason = f"Your accountant set this: {taught.label}." if taught.label else "Your accountant set this."
            usable = (value == GENERAL or (isinstance(value, str) and value in by_id)
                      or (isinstance(value, tuple) and all(cid in by_id for cid, _ in value)))
            if usable:
                targets = set() if value == GENERAL else ({value} if isinstance(value, str) else
                                                          {cid for cid, _ in value})
                against = [h for h in header if h.cost_center_id not in targets]
                against += [h for hs in by_line.values() for h in hs if h.cost_center_id not in targets]
                if against:
                    return ask([reason, *hit_lines(against), "They don't agree, so I'm asking you."])
                support = hit_lines(h for h in header if h.cost_center_id in targets)
                if value == GENERAL:
                    return done(_general(facts, AllocationMethod.RULE, [reason], rule_id=taught.rule_id))
                if isinstance(value, str):
                    return done(_single(value, facts, AllocationMethod.RULE, [reason, *support],
                                        rule_id=taught.rule_id))
                try:
                    shares = split_by_percent(facts.total, list(value), _parts_for(facts))
                except SplitError:
                    return ask([reason, "That split leaves one of them with nothing on this amount."])
                return done(CostAllocation(
                    total=facts.total, currency=facts.currency, shares=shares, method=AllocationMethod.LEARNED_SPLIT,
                    why=(reason, *support), rule_id=taught.rule_id, evidence_ids=facts.evidence_ids))

    # 2. identifiers on the evidence, line by line when the invoice has lines naming cost centers
    header_ids = list(dict.fromkeys(h.cost_center_id for h in header))
    if any(by_line.values()):
        assignment: dict[str, str] = {}
        unclear: list[str] = []
        for line in facts.lines:
            ids = list(dict.fromkeys(h.cost_center_id for h in by_line[line.id]))
            if len(ids) == 1:
                assignment[line.id] = ids[0]
            elif not ids and len(header_ids) == 1:
                assignment[line.id] = header_ids[0]
            else:
                unclear.append(line.id)
        line_hits = [h for hs in by_line.values() for h in hs]
        if unclear:
            what = ("Some invoice lines don't say which one they are for." if any(not by_line[i] for i in unclear)
                    else "One invoice line names more than one of them.")
            return ask([*hit_lines(header), *hit_lines(line_hits), what])
        targets = list(dict.fromkeys(assignment[line.id] for line in facts.lines))
        if [t for t in header_ids if t not in targets]:
            return ask([*hit_lines(header), *hit_lines(line_hits), "The invoice and its lines don't agree."])
        if len(targets) == 1:
            return done(_single(targets[0], facts, AllocationMethod.IDENTIFIER, [*hit_lines(header),
                                                                                 *hit_lines(line_hits)]))
        shares = split_by_lines(facts.lines, assignment, _parts_for(facts), facts.total)
        if shares is None:
            return ask([*hit_lines(line_hits),
                        "The invoice lines name different ones, but they don't add up to the total."])
        counts = {cid: sum(1 for v in assignment.values() if v == cid) for cid in targets}
        if all(n == 1 for n in counts.values()):
            summary = (f"Split by the {len(facts.lines)} invoice lines, one for each: "
                       f"{join_and([names[cid] for cid in targets])}.")
        else:
            summary = f"Split by the {len(facts.lines)} invoice lines: " + join_and(
                [f"{names[cid]} ({counts[cid]} line{'s' if counts[cid] != 1 else ''})" for cid in targets]) + "."
        return done(CostAllocation(total=facts.total, currency=facts.currency, shares=shares,
                                   method=AllocationMethod.LINES, why=(summary, *hit_lines(header)),
                                   evidence_ids=facts.evidence_ids))
    if len(header_ids) == 1:
        return done(_single(header_ids[0], facts, AllocationMethod.IDENTIFIER, hit_lines(header)))
    if len(header_ids) > 1:
        return ask([*hit_lines(header), f"It mentions {join_and([names[i] for i in header_ids])}."])

    # 3. a card or account used only for one cost center
    owners: list[tuple[str, str]] = []
    if facts.card_last4:
        owners += [(c.id, f"Paid with card {card_mask(facts.card_last4)}, which you use for {c.label}.")
                   for c in own if facts.card_last4 in c.identifiers.cards]
    if facts.account_id:
        account = facts.account_label or "an account"
        owners += [(c.id, f"Paid from {account}, which you use for {c.label}.")
                   for c in own if facts.account_id in c.identifiers.accounts]
    owner_ids = list(dict.fromkeys(cid for cid, _ in owners))
    if len(owner_ids) == 1:
        return done(_single(owner_ids[0], facts, AllocationMethod.CARD, [line for _, line in owners]))
    if len(owner_ids) > 1:
        return ask([line for _, line in owners])

    # 4. supplier history: consistent, at least three times
    key = match_key(facts.counterparty_key)
    counts = {t: n for t, n in (history or {}).get(key or "", {}).items() if t == GENERAL or t in by_id}
    earlier = sum(counts.values())
    if earlier >= HISTORY_MIN_COUNT and len(counts) == 1:
        target = next(iter(counts))
        if target == GENERAL:
            why = f"The last {earlier} {who} payments were all general costs."
            return done(_general(facts, AllocationMethod.HISTORY, [why], quality=Quality.AMBER))
        why = f"The last {earlier} {who} payments were all for {names[target]}."
        return done(_single(target, facts, AllocationMethod.HISTORY, [why], quality=Quality.AMBER))
    if counts:
        spread = join_and([names.get(t, "general costs") for t in sorted(counts, key=lambda t: (-counts[t], t))])
        return ask([f"Earlier {who} payments went to {spread}. That is not enough to be sure."])
    return ask([])


# --------------------------------------------------------------------------- the question and one-tap learning


def cost_center_question(centers: Sequence[CostCenter], facts: CostCenterFacts, why: Sequence[str],
                         today: date | None = None) -> Question:
    """'Which job is this for?' with each cost center, general costs, and the reasons."""
    ordered = sorted(centers, key=lambda c: (c.label.casefold(), c.id))
    options = [QuestionOption(id=f"cc:{c.id}", label=c.label, kind=OptionKind.COST_CENTER, cost_center_id=c.id)
               for c in ordered]
    options.append(QuestionOption(id="general", label=_general_label(ordered), kind=OptionKind.GENERAL))
    subject = SubjectFacts(
        subject_type=facts.subject_type, subject_id=facts.subject_id, counterparty_key=facts.counterparty_key,
        counterparty_label=facts.counterparty_label, card_last4=facts.card_last4, account_id=facts.account_id,
        account_label=facts.account_label, supplier_tax_id=facts.supplier_tax_id, amount=facts.total,
        currency=facts.currency, on=facts.on)
    return Question(tenant_id=facts.tenant_id, kind=QuestionKind.WHICH_COST_CENTER,
                    prompt=f"Which {noun_for(ordered)} is this for?", detail=describe_subject(subject, today),
                    options=tuple(options), why=tuple(why), facts=subject, series_key=facts.counterparty_key)


def suggest_cost_center_rule(
    question: Question,
    option_id: str,
    *,
    answered_by: str,
    answered_at: datetime,
    split: Sequence[tuple[str, Decimal]] = (),
    names: Mapping[str, str] | None = None,
) -> RuleProposal | None:
    """'☑ Always put Leroy Merlin paid with card •••• 5530 on Job Rua das Flores' (checked by default, §38).

    ``split`` (cost center id, percent), for the answer :data:`SPLIT_OPTION`,
    becomes a learned split. None when the question has no stable fact to
    match on.
    """
    where = _where(question)
    if where is None:
        return None
    match, subject_text = where
    labels = dict(names or {})
    labels.update({o.cost_center_id: o.label for o in question.options if o.cost_center_id})
    if option_id == SPLIT_OPTION:
        if len(split) < 2:
            return None
        outcome = RuleOutcome(cost_center_split=tuple((cid, Decimal(p)) for cid, p in split))
        label = f"Always split {subject_text}: {_split_words(split, labels)}"
    else:
        try:
            option = question.option(option_id)
        except KeyError:
            return None
        if option.kind is OptionKind.COST_CENTER:
            outcome = RuleOutcome(cost_center_id=option.cost_center_id)
            label = f"Always put {subject_text} on {option.label}"
        elif option.kind is OptionKind.GENERAL:
            outcome = RuleOutcome(cost_center_id=GENERAL)
            label = f"Always treat {subject_text} as general costs"
        else:
            return None
    rule = Rule(author=RuleAuthor.OWNER, author_id=answered_by, scope=RuleScope.TENANT, tenant_id=question.tenant_id,
                match=match, outcome=outcome, created_at=answered_at, label=label)
    return RuleProposal(label=label, checked=True, rule=rule)


def history_counts(entries: Iterable[tuple[str | None, str]]) -> dict[str, dict[str, int]]:
    """{counterparty key: {cost center id or GENERAL: count}} from past (key, target) pairs."""
    out: dict[str, dict[str, int]] = {}
    for key, target in entries:
        k = match_key(key)
        if not k:
            continue
        bucket = out.setdefault(k, {})
        bucket[target] = bucket.get(target, 0) + 1
    return out


# --------------------------------------------------------------------------- recharged to the client


# Words that say a cost is a client's, bought for them to pay back: an end customer named on the invoice,
# "on behalf of", a disbursement, a cost to re-invoice (folded text).
_FOR_THE_CLIENT = re.compile(
    r"(?<![a-z])(?:cliente\s+final|utilizador\s+final|end[\s-]+(?:customer|client|user)|"
    r"on\s+behalf\s+of|em\s+nome\s+(?:de|do|da)|por\s+conta\s+(?:de|do|da)|a\s+refaturar|para\s+refaturar|"
    r"refaturacao|re-?invoic\w*|rebill\w*|to\s+(?:be\s+)?recharged?|recharge\s+to|reembols\w*|reimburs\w*|"
    r"disbursements?|despesas?\s+(?:do|de)\s+cliente|client\s+(?:expense|disbursement)s?|pass[\s-]+through)"
    r"(?![a-z])")


@dataclass(frozen=True)
class RechargeDecision:
    """Which shares of an allocation are the client's to pay back, why, or the one cost center to ask about."""

    recharge: tuple[str, ...] = ()  # cost center ids whose share is recharged to the client
    method: str | None = None  # "rule" | "setting" | "evidence" | "history"; None: nothing decided
    why: tuple[str, ...] = ()
    ask: str | None = None  # a cost center it genuinely cannot be told for: one plain question
    rule_id: str | None = None


def decide_recharge(
    *,
    allocation: CostAllocation,
    centers: Mapping[str, CostCenter],
    facts: CostCenterFacts,
    rulebook: RuleBook | None = None,
    accountant_ids: Iterable[str] = (),
    history: Mapping[tuple[str, str], tuple[int, int]] | None = None,
    recharging: Iterable[str] = (),
    own_tax_ids: Iterable[str] = (),
) -> RechargeDecision:
    """Is a cost on a client's cost center the client's to pay back (a reimbursable cost, a disbursement,
    media bought for them, a pass-through licence), or the business's own? Strongest first:

    1. a rule the owner taught ("Always recharge IKEA paid with card •••• 4817 to the client it is for");
    2. the cost center's own setting (its costs are the client's: a travel agency's trip, a matter's
       disbursements);
    3. the evidence: the invoice is addressed to the client, a line names the client as the end customer,
       or the invoice says it is for the client ("on behalf of", "cliente final", "a refaturar");
    4. history: the last costs from this supplier on this client were all recharged (likely, not proven).

    Otherwise nothing is decided (the business's own cost), and only when the client has had costs
    recharged before (so it genuinely could be either) is the owner asked one plain question.
    ``history``: {(supplier key, cost center id): (recharged, not recharged)}; ``recharging``: cost
    centers with any cost recharged before.
    """
    targets = [s.cost_center_id for s in allocation.shares if s.cost_center_id in centers]
    if allocation.general or not targets:
        return RechargeDecision()
    labels = {cid: centers[cid].label for cid in targets}
    who = facts.counterparty_label
    if rulebook is not None:
        subject = RuleSubject(counterparty_key=facts.counterparty_key, card_last4=facts.card_last4,
                              account_id=facts.account_id, amount=facts.total, supplier_tax_id=facts.supplier_tax_id)
        decision = rulebook.evaluate(subject, tenant_id=facts.tenant_id, accountant_ids=accountant_ids)
        taught = decision.get(RuleField.RECHARGE)
        if taught is not None and decision.unresolved(RuleField.RECHARGE) is None:
            reason = (f"You told us: {taught.label}." if taught.label else
                      "You told us the client pays these back." if taught.value else
                      "You told us these are your own costs.")
            if taught.author is RuleAuthor.ACCOUNTANT:
                reason = f"Your accountant set this: {taught.label}." if taught.label else "Your accountant set this."
            return RechargeDecision(recharge=tuple(targets) if taught.value else (), method="rule", why=(reason,),
                                    rule_id=taught.rule_id)
    own = [t for t in own_tax_ids if t]
    why: dict[str, list[str]] = {}
    setting = [cid for cid in targets if centers[cid].recharge]
    for cid in setting:
        why.setdefault(cid, []).append(f"Costs on {labels[cid]} are theirs to pay back, as you set.")
    evidence: list[str] = []
    for cid in targets:
        center = centers[cid]
        ids = center.identifiers.tax_ids
        for tax_id in facts.customer_tax_ids:
            if any(same_tax_id(t, tax_id) for t in ids) and not any(same_tax_id(o, tax_id) for o in own):
                why.setdefault(cid, []).append(f"The invoice is addressed to {labels[cid]} (tax number {tax_id}), "
                                               "so it is theirs to pay back.")
                evidence.append(cid)
        for line in facts.lines:
            named = line.customer_tax_id and any(same_tax_id(t, line.customer_tax_id) for t in ids)
            if named or (_FOR_THE_CLIENT.search(fold(line.text)) and find_hits(line.text, [center], "the line")):
                why.setdefault(cid, []).append(f"Line {line.id} of the invoice names {labels[cid]} as the end "
                                               "customer.")
                evidence.append(cid)
        for where, text in facts.texts:
            if not text or not _FOR_THE_CLIENT.search(fold(text)) or not find_hits(text, [center], where):
                continue
            place = where[:1].upper() + where[1:]
            line_text = f"{place} names {labels[cid]} as the client it was bought for."
            if line_text not in why.get(cid, []):
                why.setdefault(cid, []).append(line_text)
            evidence.append(cid)
    decided = [cid for cid in targets if cid in setting or cid in evidence]
    if decided:
        lines = tuple(dict.fromkeys(w for cid in decided for w in why.get(cid, [])))
        return RechargeDecision(recharge=tuple(decided), method="evidence" if evidence else "setting", why=lines)
    key = match_key(facts.counterparty_key) or ""
    likely: list[str] = []
    notes: list[str] = []
    settled_own: set[str] = set()
    for cid in targets:
        yes, no = (history or {}).get((key, cid), (0, 0))
        if yes >= HISTORY_MIN_COUNT and no == 0:
            likely.append(cid)
            notes.append(f"The last {yes} {who} costs on {labels[cid]} were all paid back by them.")
        elif no >= HISTORY_MIN_COUNT and yes == 0:
            settled_own.add(cid)
    if likely:
        return RechargeDecision(recharge=tuple(likely), method="history", why=tuple(notes))
    before = set(recharging)
    unsure = [cid for cid in targets if cid in before and cid not in settled_own]
    return RechargeDecision(ask=unsure[0] if unsure else None)


def recharge_rule(question: Question, recharge: bool, *, answered_by: str,
                  answered_at: datetime) -> RuleProposal | None:
    """'☑ Always recharge IKEA paid with card •••• 4817 to the client it is for' (checked by default, §38)."""
    where = _where(question)
    if where is None:
        return None
    match, subject_text = where
    label = (f"Always recharge {subject_text} to the client it is for" if recharge else
             f"Always keep {subject_text} as your own cost")
    rule = Rule(author=RuleAuthor.OWNER, author_id=answered_by, scope=RuleScope.TENANT, tenant_id=question.tenant_id,
                match=match, outcome=RuleOutcome(recharge=recharge), created_at=answered_at, label=label)
    return RuleProposal(label=label, checked=True, rule=rule)
