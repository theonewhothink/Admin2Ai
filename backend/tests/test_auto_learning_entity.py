"""Entity Agent (§46, §51, §37): company assignment, quality, Why lines, Needs-You questions."""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from backoffice.domain.models import Document, LegalEntity, Quality, Transaction
from backoffice.learning.entity import (
    UNSURE_PROMPT,
    OwnershipBook,
    assign_entity,
    build_history,
)
from backoffice.learning.questions import Answer, OptionKind
from backoffice.learning.rules import (
    PERSONAL,
    Rule,
    RuleAuthor,
    RuleBook,
    RuleMatch,
    RuleOutcome,
    RuleScope,
    suggest_rule_from_answer,
)

D = Decimal
T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)
HAZEL = LegalEntity(id="ent_hazel", tenant_id="t1", name="Hazel Tree, Lda.", country="PT", tax_id="PT509123456")
OAK = LegalEntity(id="ent_oak", tenant_id="t1", name="Oak Studio Unipessoal Lda", country="PT", tax_id="516000000")
OTHER_TENANT = LegalEntity(id="ent_x", tenant_id="t2", name="Elsewhere", country="PT", tax_id="500000000")
OWNERSHIP = OwnershipBook(
    accounts={"acc_hazel": "ent_hazel", "acc_oak": "ent_oak", "acc_me": PERSONAL},
    cards={"1111": "ent_hazel", "2222": "ent_oak", "4817": PERSONAL},
    account_labels={"acc_me": "Millennium •••• 1234"},
)
_JARGON = re.compile(r"\bentit(?:y|ies)\b|\breconcil|\bexception|\b[a-z]{2,8}_[0-9a-f]{16}\b|\bent_\w+", re.I)


def tx(account: str = "acc_hazel", card: str | None = None, amount: str = "-84.50", counterparty: str = "IKEA ALFRAGIDE") -> Transaction:
    return Transaction(id="tx_1", tenant_id="t1", account_id=account, booked_on=date(2026, 9, 12),
                       amount=D(amount), counterparty=counterparty, card_last4=card)  # fmt: skip


def doc(customer: str | None) -> Document:
    return Document(id="doc_1", tenant_id="t1", evidence_ids=["ev"], supplier_name="IKEA Portugal",
                    customer_tax_id=customer, gross_amount=D("84.50"), issue_date=date(2026, 9, 11))  # fmt: skip


def assert_plain(lines) -> None:
    for line in lines:
        assert not _JARGON.search(line), line


def test_company_account_and_card_agree_green() -> None:
    result = assign_entity(entities=[HAZEL, OAK], transaction=tx(card="1111"), ownership=OWNERSHIP)
    assert result.entity_id == "ent_hazel" and not result.private
    assert result.quality is Quality.GREEN and not result.needs_owner
    assert result.why == ("Paid with Hazel Tree's card •••• 1111", "Paid from Hazel Tree's account")
    assert_plain(result.why)


def test_invoice_addressee_is_documentary_evidence() -> None:
    result = assign_entity(entities=[HAZEL, OAK], document=doc("509 123 456"))
    assert result.entity_id == "ent_hazel" and result.quality is Quality.GREEN
    assert result.why == ("Invoice addressed to Hazel Tree",)
    assert result.subject_type == "document"


def test_strong_facts_disagree_red_question() -> None:
    result = assign_entity(entities=[HAZEL, OAK], transaction=tx(account="acc_oak", card="2222"),
                           document=doc("PT509123456"), ownership=OWNERSHIP)  # fmt: skip
    assert result.entity_id is None and result.quality is Quality.RED
    q = result.question
    assert q is not None and q.prompt == UNSURE_PROMPT
    assert [o.label for o in q.options] == ["Hazel Tree", "Oak Studio", "Personal", "Another company"]
    assert [o.kind for o in q.options][-2:] == [OptionKind.PERSONAL, OptionKind.ANOTHER_COMPANY]
    assert "Invoice addressed to Hazel Tree" in q.why and "Paid from Oak Studio's account" in q.why
    assert q.detail == "IKEA Alfragide · €84.50 · 12 September · card •••• 2222"
    assert_plain([q.prompt, q.detail, *q.why, *(o.label for o in q.options)])


def test_personal_card_only_asks_and_answer_becomes_spec_rule() -> None:
    result = assign_entity(entities=[HAZEL, OAK], transaction=tx(account="acc_me", card="4817"), ownership=OWNERSHIP)
    assert result.entity_id is None and result.quality is Quality.AMBER and result.needs_owner
    assert result.why == ("Paid with a personal card •••• 4817", "Paid from a personal account")
    q = result.question
    assert q is not None
    proposal = suggest_rule_from_answer(
        q, Answer(question_id=q.id, option_id="entity:ent_hazel", answered_by="owner1", answered_at=T0)
    )
    assert proposal is not None
    assert proposal.label == "Always use Hazel Tree for IKEA Alfragide paid with card •••• 4817"
    book = RuleBook([proposal.rule])
    again = assign_entity(entities=[HAZEL, OAK], transaction=tx(account="acc_me", card="4817"),
                          ownership=OWNERSHIP, rulebook=book)  # fmt: skip
    # The rule matched on the card, so it overrides the card's owner: verified.
    assert again.entity_id == "ent_hazel" and again.quality is Quality.GREEN
    assert again.rule_id == proposal.rule.id
    assert again.why[0] == "You told us: Always use Hazel Tree for IKEA Alfragide paid with card •••• 4817."


def test_rule_not_covering_an_ownership_fact_is_amber() -> None:
    rule = Rule(author=RuleAuthor.OWNER, author_id="o", scope=RuleScope.TENANT, tenant_id="t1",
                match=RuleMatch(counterparty_key="ikea alfragide"), outcome=RuleOutcome(entity_id="ent_hazel"),
                created_at=T0)  # fmt: skip
    result = assign_entity(entities=[HAZEL, OAK], transaction=tx(account="acc_oak"), ownership=OWNERSHIP,
                           rulebook=RuleBook([rule]))  # fmt: skip
    assert result.entity_id == "ent_hazel" and result.quality is Quality.AMBER
    assert result.why == ("You told us this belongs to Hazel Tree.", "Paid from Oak Studio's account")


def test_rule_never_overrides_invoice_addressee() -> None:
    rule = Rule(author=RuleAuthor.ACCOUNTANT, author_id="acc1", scope=RuleScope.CLIENT, tenant_id="t1",
                match=RuleMatch(counterparty_key="ikea portugal"), outcome=RuleOutcome(entity_id="ent_oak"),
                created_at=T0)  # fmt: skip
    result = assign_entity(entities=[HAZEL, OAK], document=doc("509123456"), rulebook=RuleBook([rule]),
                           accountant_ids=["acc1"])  # fmt: skip
    assert result.entity_id is None and result.quality is Quality.RED and result.needs_owner
    assert "Your accountant set this to Oak Studio." in result.why


def test_conflicting_saved_answers_ask_the_owner() -> None:
    rules = [
        Rule(author=RuleAuthor.OWNER, author_id="o", scope=RuleScope.TENANT, tenant_id="t1",
             match=RuleMatch(counterparty_key="ikea alfragide", card_last4="1111"), outcome=RuleOutcome(entity_id="ent_hazel"),
             created_at=T0),
        Rule(author=RuleAuthor.OWNER, author_id="o", scope=RuleScope.TENANT, tenant_id="t1",
             match=RuleMatch(counterparty_key="ikea alfragide", account_id="acc_hazel"), outcome=RuleOutcome(entity_id="ent_oak"),
             created_at=T0),
    ]  # fmt: skip
    result = assign_entity(entities=[HAZEL, OAK], transaction=tx(card="1111"), ownership=OWNERSHIP, rulebook=RuleBook(rules))
    assert result.quality is Quality.RED and result.needs_owner
    assert result.why[0] == "Two saved answers disagree about this."


def test_invoice_to_another_company_asks() -> None:
    result = assign_entity(entities=[HAZEL, OAK], document=doc("PT999999990"))
    assert result.entity_id is None and result.needs_owner and result.quality is Quality.AMBER
    assert result.why == ("Invoice addressed to another company (tax number PT999999990)",)


def test_history_alone_is_amber_and_weak_disagreement_downgrades() -> None:
    history = build_history([("ikea alfragide", "ent_oak")] * 4 + [("ikea alfragide", "ent_hazel")])
    only_history = assign_entity(entities=[HAZEL, OAK], transaction=tx(account="acc_unknown"), history=history)
    assert only_history.entity_id == "ent_oak" and only_history.quality is Quality.AMBER
    assert only_history.why == ("IKEA Alfragide usually belongs to Oak Studio",)
    against = assign_entity(entities=[HAZEL, OAK], transaction=tx(), ownership=OWNERSHIP, history=history)
    assert against.entity_id == "ent_hazel" and against.quality is Quality.AMBER


def test_split_history_gives_no_vote() -> None:
    history = build_history([("ikea alfragide", "ent_oak")] * 3 + [("ikea alfragide", "ent_hazel")] * 2)
    result = assign_entity(entities=[HAZEL, OAK], transaction=tx(account="acc_unknown"), history=history)
    assert result.needs_owner and result.why == ("Nothing on it shows which company it is for.",)


def test_single_company_without_facts_is_amber_not_a_question() -> None:
    result = assign_entity(entities=[HAZEL, OTHER_TENANT], document=doc(None))
    assert result.entity_id == "ent_hazel" and result.quality is Quality.AMBER
    assert not result.needs_owner and result.why == ("You have one company.",)


def test_personal_rule_from_accountant_and_income_wording() -> None:
    rule = Rule(author=RuleAuthor.ACCOUNTANT, author_id="acc1", scope=RuleScope.ALL_CLIENTS_OF_ACCOUNTANT,
                match=RuleMatch(counterparty_key="netflix"), outcome=RuleOutcome(private=True), created_at=T0)  # fmt: skip
    result = assign_entity(entities=[HAZEL, OAK], transaction=tx(counterparty="NETFLIX.COM", account="acc_x"),
                           rulebook=RuleBook([rule]), accountant_ids=["acc1"], key="netflix")  # fmt: skip
    assert result.private and result.entity_id is None and result.quality is Quality.GREEN
    assert result.why == ("Your accountant set this as personal.",)
    income = assign_entity(entities=[HAZEL, OAK], transaction=tx(amount="500"), ownership=OWNERSHIP)
    assert income.why == ("Received in Hazel Tree's account",)


def test_input_validation() -> None:
    with pytest.raises(ValueError):
        assign_entity(entities=[HAZEL])
    with pytest.raises(ValueError):
        assign_entity(entities=[OTHER_TENANT], transaction=tx())
    other_doc = doc(None).model_copy(update={"tenant_id": "t2"})
    with pytest.raises(ValueError):
        assign_entity(entities=[HAZEL], transaction=tx(), document=other_doc)


def test_stale_rule_for_removed_company_is_ignored() -> None:
    rule = Rule(author=RuleAuthor.OWNER, author_id="o", scope=RuleScope.TENANT, tenant_id="t1",
                match=RuleMatch(counterparty_key="ikea alfragide"), outcome=RuleOutcome(entity_id="ent_gone"),
                created_at=T0)  # fmt: skip
    result = assign_entity(entities=[HAZEL, OAK], transaction=tx(), ownership=OWNERSHIP, rulebook=RuleBook([rule]))
    assert result.entity_id == "ent_hazel" and result.rule_id is None
