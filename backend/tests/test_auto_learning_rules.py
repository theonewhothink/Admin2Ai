"""Taught rules (§38, §28): matching, precedence, conflicts, audit, one-tap proposals."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from backoffice.domain.models import Document, Transaction
from backoffice.learning.questions import (
    Answer,
    OptionKind,
    Question,
    QuestionKind,
    QuestionOption,
    SubjectFacts,
)
from backoffice.learning.rules import (
    PERSONAL,
    Expectation,
    Rule,
    RuleAuthor,
    RuleBook,
    RuleError,
    RuleEventKind,
    RuleField,
    RuleMatch,
    RuleOutcome,
    RuleScope,
    RuleSubject,
    suggest_rule_from_answer,
)

T0 = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)
D = Decimal


def owner_rule(match: RuleMatch, outcome: RuleOutcome, *, at: datetime = T0, tenant: str = "t1", **kw) -> Rule:
    return Rule(author=RuleAuthor.OWNER, author_id="owner1", scope=RuleScope.TENANT, tenant_id=tenant,
                match=match, outcome=outcome, created_at=at, **kw)  # fmt: skip


def accountant_rule(
    match: RuleMatch, outcome: RuleOutcome, *, scope: RuleScope = RuleScope.CLIENT, at: datetime = T0,
    author_id: str = "acc1",
) -> Rule:  # fmt: skip
    tenant = None if scope is RuleScope.ALL_CLIENTS_OF_ACCOUNTANT else "t1"
    return Rule(author=RuleAuthor.ACCOUNTANT, author_id=author_id, scope=scope, tenant_id=tenant,
                match=match, outcome=outcome, created_at=at)  # fmt: skip


IKEA = RuleSubject(counterparty_key="ikea", card_last4="4817", account_id="acc1", amount=D("84.50"))


# --------------------------------------------------------------------------- model validation


def test_match_validation() -> None:
    with pytest.raises(ValidationError):
        RuleMatch()  # matches everything: refused
    with pytest.raises(ValidationError):
        RuleMatch(card_last4="48")
    with pytest.raises(ValidationError):
        RuleMatch(counterparty_key="ikea", amount_min=D("10"), amount_max=D("5"))
    with pytest.raises(ValidationError):
        RuleMatch(counterparty_key="ikea", amount_min=1.5)  # floats are never money
    assert RuleMatch(counterparty_key="IKEA ALFRAGIDE 1234").counterparty_key == "ikea alfragide"


def test_outcome_and_scope_validation() -> None:
    with pytest.raises(ValidationError):
        RuleOutcome()
    with pytest.raises(ValidationError):
        RuleOutcome(entity_id="ent_a", private=True)
    m, o = RuleMatch(counterparty_key="ikea"), RuleOutcome(category="furniture")
    with pytest.raises(ValidationError):
        Rule(author=RuleAuthor.OWNER, author_id="o", scope=RuleScope.CLIENT, tenant_id="t1", match=m, outcome=o, created_at=T0)
    with pytest.raises(ValidationError):
        Rule(author=RuleAuthor.ACCOUNTANT, author_id="a", scope=RuleScope.TENANT, tenant_id="t1", match=m, outcome=o, created_at=T0)
    with pytest.raises(ValidationError):
        Rule(author=RuleAuthor.ACCOUNTANT, author_id="a", scope=RuleScope.ALL_CLIENTS_OF_ACCOUNTANT, tenant_id="t1",
             match=m, outcome=o, created_at=T0)  # fmt: skip
    with pytest.raises(ValidationError):
        Rule(author=RuleAuthor.OWNER, author_id="o", scope=RuleScope.TENANT, tenant_id="t1", match=m, outcome=o,
             created_at=datetime(2026, 9, 1))  # fmt: skip


# --------------------------------------------------------------------------- matching


def test_all_criteria_must_match() -> None:
    match = RuleMatch(counterparty_key="ikea", card_last4="4817", amount_min=D("10"), amount_max=D("100"))
    assert match.matches(IKEA)
    assert not match.matches(RuleSubject(counterparty_key="ikea", card_last4="9999", amount=D("84.50")))
    assert not match.matches(RuleSubject(counterparty_key="ikea", card_last4="4817", amount=D("150")))
    assert not match.matches(RuleSubject(counterparty_key="ikea", card_last4="4817"))  # amount unknown
    assert RuleMatch(supplier_tax_id="PT509123456").matches(RuleSubject(supplier_tax_id="509 123 456"))


def test_subject_from_transaction_and_document() -> None:
    tx = Transaction(tenant_id="t1", account_id="acc1", booked_on=date(2026, 9, 12), amount=D("-84.50"),
                     counterparty="IKEA ALFRAGIDE", card_last4="4817")  # fmt: skip
    doc = Document(tenant_id="t1", evidence_ids=["e"], supplier_name="IKEA Portugal", supplier_tax_id="PT503",
                   gross_amount=D("84.50"))  # fmt: skip
    s = RuleSubject.from_transaction(tx).merged(RuleSubject.from_document(doc))
    assert s.counterparty_key == "ikea alfragide" and s.amount == D("84.50")
    assert s.card_last4 == "4817" and s.supplier_tax_id == "PT503"


# --------------------------------------------------------------------------- precedence


def test_more_specific_beats_less_specific() -> None:
    book = RuleBook([
        owner_rule(RuleMatch(counterparty_key="ikea"), RuleOutcome(entity_id="ent_oak")),
        owner_rule(RuleMatch(counterparty_key="ikea", card_last4="4817"), RuleOutcome(entity_id="ent_hazel")),
    ])  # fmt: skip
    decision = book.evaluate(IKEA, tenant_id="t1")
    assert decision.entity_id == "ent_hazel"
    assert decision.get(RuleField.ENTITY).matched_on == ("counterparty_key", "card_last4")  # type: ignore[union-attr]
    [conflict] = decision.conflicts
    assert conflict.resolved and conflict.reason == "a more specific rule beats a less specific one"


def test_owner_beats_accountant_for_entity_even_against_more_specific_rule() -> None:
    book = RuleBook([
        accountant_rule(RuleMatch(counterparty_key="ikea", card_last4="4817"), RuleOutcome(entity_id="ent_oak"),
                        scope=RuleScope.ALL_CLIENTS_OF_ACCOUNTANT),
        owner_rule(RuleMatch(counterparty_key="ikea"), RuleOutcome(entity_id="ent_hazel")),
    ])  # fmt: skip
    decision = book.evaluate(IKEA, tenant_id="t1", accountant_ids={"acc1"})
    assert decision.entity_id == "ent_hazel"
    assert decision.conflicts[0].reason == "owner decides entity over accountant"


def test_accountant_beats_owner_for_category_and_tax() -> None:
    book = RuleBook([
        owner_rule(RuleMatch(counterparty_key="adobe"), RuleOutcome(category="design", tax_treatment="none")),
        accountant_rule(RuleMatch(counterparty_key="adobe"), RuleOutcome(category="software", tax_treatment="vat_deductible"),
                        scope=RuleScope.ALL_CLIENTS_OF_ACCOUNTANT),
    ])  # fmt: skip
    decision = book.evaluate(RuleSubject(counterparty_key="adobe"), tenant_id="t1", accountant_ids={"acc1"})
    assert decision.category == "software"
    assert decision.tax_treatment == "vat_deductible"
    assert decision.get(RuleField.CATEGORY).author is RuleAuthor.ACCOUNTANT  # type: ignore[union-attr]


def test_client_rule_beats_all_clients_rule() -> None:
    book = RuleBook([
        accountant_rule(RuleMatch(counterparty_key="adobe"), RuleOutcome(category="software"),
                        scope=RuleScope.ALL_CLIENTS_OF_ACCOUNTANT),
        accountant_rule(RuleMatch(counterparty_key="adobe"), RuleOutcome(category="marketing")),
    ])  # fmt: skip
    decision = book.evaluate(RuleSubject(counterparty_key="adobe"), tenant_id="t1", accountant_ids={"acc1"})
    assert decision.category == "marketing"
    assert decision.conflicts[0].reason == "a rule for this business beats a rule for all clients"


def test_equal_rank_disagreement_is_unresolved_never_guessed() -> None:
    book = RuleBook([
        owner_rule(RuleMatch(counterparty_key="ikea", card_last4="4817"), RuleOutcome(entity_id="ent_hazel")),
        owner_rule(RuleMatch(counterparty_key="ikea", account_id="acc1"), RuleOutcome(entity_id="ent_oak")),
    ])  # fmt: skip
    decision = book.evaluate(IKEA, tenant_id="t1")
    assert decision.entity_id is None and RuleField.ENTITY not in decision.decisions
    conflict = decision.unresolved(RuleField.ENTITY)
    assert conflict is not None and conflict.winner_rule_id is None and len(conflict.rule_ids) == 2


def test_equal_rank_agreement_cites_newest() -> None:
    old = owner_rule(RuleMatch(counterparty_key="ikea", card_last4="4817"), RuleOutcome(entity_id="ent_hazel"))
    new = owner_rule(RuleMatch(counterparty_key="ikea", account_id="acc1"), RuleOutcome(entity_id="ent_hazel"),
                     at=T0 + timedelta(days=1))  # fmt: skip
    decision = RuleBook([old, new]).evaluate(IKEA, tenant_id="t1")
    assert decision.get(RuleField.ENTITY).rule_id == new.id  # type: ignore[union-attr]
    assert decision.conflicts == ()


def test_personal_and_expectation_fields() -> None:
    book = RuleBook([
        owner_rule(RuleMatch(counterparty_key="netflix"), RuleOutcome(private=True)),
        accountant_rule(RuleMatch(counterparty_key="netflix"), RuleOutcome(expectation=Expectation.NONE)),
    ])  # fmt: skip
    decision = book.evaluate(RuleSubject(counterparty_key="netflix"), tenant_id="t1", accountant_ids={"acc1"})
    assert decision.private and decision.entity_id is None
    assert decision.get(RuleField.ENTITY).value == PERSONAL  # type: ignore[union-attr]
    assert decision.expectation is Expectation.NONE


def test_rule_visibility_by_tenant_and_authorization() -> None:
    book = RuleBook([
        owner_rule(RuleMatch(counterparty_key="adobe"), RuleOutcome(category="a"), tenant="t2"),
        accountant_rule(RuleMatch(counterparty_key="adobe"), RuleOutcome(category="b"),
                        scope=RuleScope.ALL_CLIENTS_OF_ACCOUNTANT, author_id="acc9"),
    ])  # fmt: skip
    subject = RuleSubject(counterparty_key="adobe")
    assert book.evaluate(subject, tenant_id="t1").category is None  # other tenant's owner rule, unauthorized accountant
    assert book.evaluate(subject, tenant_id="t1", accountant_ids={"acc9"}).category == "b"
    assert book.evaluate(subject, tenant_id="t2").category == "a"


# --------------------------------------------------------------------------- lifecycle and audit


def test_add_supersedes_same_slot_and_keeps_other_fields() -> None:
    match = RuleMatch(counterparty_key="ikea", card_last4="4817")
    first = owner_rule(match, RuleOutcome(entity_id="ent_oak", category="furniture"))
    book = RuleBook()
    book.add(first)
    second = owner_rule(match, RuleOutcome(entity_id="ent_hazel"), at=T0 + timedelta(days=2))
    narrowed = book.add(second, reason="owner changed their answer")
    assert len(narrowed) == 1 and narrowed[0].outcome == RuleOutcome(category="furniture")
    assert not book.get(first.id).active
    decision = book.evaluate(IKEA, tenant_id="t1")
    assert decision.entity_id == "ent_hazel" and decision.category == "furniture"
    kinds = [e.kind for e in book.audit]
    assert kinds == [RuleEventKind.CREATED, RuleEventKind.CREATED, RuleEventKind.SUPERSEDED, RuleEventKind.CREATED]
    superseded = book.audit[2]
    assert superseded.rule_id == first.id and superseded.replaced_by == (second.id, narrowed[0].id)


def test_different_slots_coexist_and_deactivate_is_audited() -> None:
    book = RuleBook()
    a = owner_rule(RuleMatch(counterparty_key="ikea"), RuleOutcome(entity_id="ent_oak"))
    b = accountant_rule(RuleMatch(counterparty_key="ikea"), RuleOutcome(entity_id="ent_hazel"))
    book.add(a)
    book.add(b)
    assert all(r.active for r in book.rules)
    book.deactivate(a.id, actor="owner1", at=T0, reason="no longer true")
    assert book.evaluate(IKEA, tenant_id="t1", accountant_ids={"acc1"}).entity_id == "ent_hazel"
    assert book.audit[-1].kind is RuleEventKind.DEACTIVATED
    with pytest.raises(RuleError):
        book.add(a)  # ids are never reused


# --------------------------------------------------------------------------- one-tap proposals


def ikea_question(**facts) -> Question:
    base = dict(subject_type="transaction", subject_id="tx_1", counterparty_key="ikea", counterparty_label="IKEA",
                card_last4="4817", account_id="acc1", amount=D("84.50"), on=date(2026, 9, 12))  # fmt: skip
    base.update(facts)
    return Question(
        id="q1",
        tenant_id="t1",
        kind=QuestionKind.WHICH_COMPANY,
        prompt="We aren't sure which company this belongs to.",
        options=(
            QuestionOption(id="entity:ent_hazel", label="Hazel Tree", kind=OptionKind.ENTITY, entity_id="ent_hazel"),
            QuestionOption(id="personal", label="Personal", kind=OptionKind.PERSONAL),
            QuestionOption(id="another_company", label="Another company", kind=OptionKind.ANOTHER_COMPANY),
            QuestionOption(id="cat", label="Company telecom", kind=OptionKind.CATEGORY, category="telecom"),
        ),
        facts=SubjectFacts(**base),
    )


def answer(option_id: str, question_id: str = "q1") -> Answer:
    return Answer(question_id=question_id, option_id=option_id, answered_by="owner1", answered_at=T0)


def test_spec_one_tap_rule_proposal() -> None:
    proposal = suggest_rule_from_answer(ikea_question(), answer("entity:ent_hazel"))
    assert proposal is not None
    assert proposal.label == "Always use Hazel Tree for IKEA paid with card •••• 4817"
    assert proposal.checked  # default checked (§38)
    rule = proposal.rule
    assert rule.author is RuleAuthor.OWNER and rule.scope is RuleScope.TENANT and rule.tenant_id == "t1"
    assert rule.match == RuleMatch(counterparty_key="ikea", card_last4="4817")
    assert rule.outcome.entity_id == "ent_hazel"
    book = RuleBook()
    book.add(rule)
    assert book.evaluate(IKEA, tenant_id="t1").entity_id == "ent_hazel"


def test_proposals_for_personal_category_and_account() -> None:
    personal = suggest_rule_from_answer(ikea_question(), answer("personal"))
    assert personal is not None and personal.label == "Always treat IKEA paid with card •••• 4817 as personal"
    assert personal.rule.outcome.private
    category = suggest_rule_from_answer(ikea_question(card_last4=None, account_id=None), answer("cat"))
    assert category is not None and category.label == "Always treat IKEA as company telecom"
    by_account = suggest_rule_from_answer(
        ikea_question(card_last4=None, account_label="Millennium •••• 1234"), answer("entity:ent_hazel")
    )
    assert by_account is not None
    assert by_account.label == "Always use Hazel Tree for IKEA paid from Millennium •••• 1234"
    assert by_account.rule.match == RuleMatch(counterparty_key="ikea", account_id="acc1")


def test_one_off_answers_are_not_generalized() -> None:
    assert suggest_rule_from_answer(ikea_question(), answer("another_company")) is None
    assert suggest_rule_from_answer(ikea_question(counterparty_key=None, supplier_tax_id=None), answer("personal")) is None


def test_invalid_answers_rejected() -> None:
    with pytest.raises(RuleError):
        suggest_rule_from_answer(ikea_question(), answer("nope"))
    with pytest.raises(RuleError):
        suggest_rule_from_answer(ikea_question(), answer("personal", question_id="q2"))


def test_accountant_answer_becomes_client_rule() -> None:
    proposal = suggest_rule_from_answer(ikea_question(), answer("cat"), author=RuleAuthor.ACCOUNTANT)
    assert proposal is not None and proposal.rule.scope is RuleScope.CLIENT
    everywhere = suggest_rule_from_answer(
        ikea_question(), answer("cat"), author=RuleAuthor.ACCOUNTANT, scope=RuleScope.ALL_CLIENTS_OF_ACCOUNTANT
    )
    assert everywhere is not None and everywhere.rule.tenant_id is None
