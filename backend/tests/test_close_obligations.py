"""Administrative obligations (§24): detection, verification conditions, satisfaction, due soon."""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

import pytest

from backoffice.closure import (
    DueItem,
    EvidenceFact,
    Issuer,
    ProofKind,
    VerificationCondition,
    detect_obligation,
    due_soon,
    normalize_reference,
    satisfy,
)
from backoffice.domain.models import LegalEntity, Obligation, ObligationKind, Quality, SourceKind

RECEIVED = date(2026, 9, 20)
HAZEL = LegalEntity(id="ent_hazel", tenant_id="t1", name="Hazel Tree, Lda.", country="PT", tax_id="PT509123456")
OAK = LegalEntity(id="ent_oak", tenant_id="t1", name="Oak Studio Unipessoal", country="PT", tax_id="PT516000111")

AT_LETTER = """Autoridade Tributária e Aduaneira
Lisboa, 15 de setembro de 2026
NIF: 509 123 456
Pagamento de IVA — período 2026/08.
Referência para pagamento: 123 456 789
Total a pagar: 1.234,56 €
Data limite de pagamento: 20/10/2026. O não pagamento dentro do prazo implica coima e juros de mora."""


def detect(text: str, **kw):
    kw.setdefault("tenant_id", "t1")
    kw.setdefault("received_on", RECEIVED)
    kw.setdefault("entities", [HAZEL, OAK])
    return detect_obligation(text, **kw)


_BANNED = re.compile(r"reconcil|\bentit(y|ies)\b|exception|workflow|\bAPI\b|error|!|[a-z]{2,8}_[0-9a-f]{16}", re.I)


# =========================================================================== detection


def test_portuguese_tax_letter_becomes_a_complete_obligation():
    f = detect(AT_LETTER)
    assert f is not None and f.complete
    assert f.kind is ObligationKind.TAX_DEADLINE
    assert f.issuer is Issuer.TAX_AUTHORITY
    assert f.title == "Tax payment"
    assert f.due_on == date(2026, 10, 20)
    assert f.amount == Decimal("1234.56")
    assert f.reference == "123456789"
    assert f.entity_id == HAZEL.id  # found by its NIF, written with spaces
    assert f.responsible == "owner"
    assert f.consequence == "The letter mentions a fine and interest."
    assert f.quality is Quality.AMBER  # words in a letter are never verified evidence
    assert f.reasons == ("From the tax office", "Due 20 October", "Amount €1,234.56", "Reference 123456789")
    ob = f.obligation
    assert isinstance(ob, Obligation)
    assert (ob.tenant_id, ob.entity_id, ob.kind, ob.due_on, ob.amount) == (
        "t1", HAZEL.id, ObligationKind.TAX_DEADLINE, date(2026, 10, 20), Decimal("1234.56"),
    )  # fmt: skip
    assert ob.verification_condition == (
        "v1;proof=payment;amount=1234.56;currency=EUR;reference=123456789;by=2026-10-20;since=2026-09-20"
    )  # proof must be newer than the letter unless it carries the reference
    assert ob.required_evidence == "Proof of a payment of €1,234.56 with reference 123456789 by 20 October 2026."
    assert ob.satisfied_by_evidence_ids == []


def test_social_security_payment_with_text_date():
    f = detect("Segurança Social - Contribuições. O valor a pagar de 842,10 € deve ser pago até 20 de outubro.",
               default_entity_id=OAK.id)  # fmt: skip
    assert f is not None
    assert f.kind is ObligationKind.TAX_DEADLINE
    assert f.title == "Social Security payment"
    assert f.due_on == date(2026, 10, 20)  # year inferred: the next 20 October
    assert f.amount == Decimal("842.10")
    assert f.entity_id == OAK.id
    assert f.consequence == "Paying late may lead to a fine and interest."


def test_english_kyc_request():
    f = detect(
        "As part of our KYC review please upload proof of address due by 5 October 2026, "
        "or your account may be suspended.",
        source_kind=SourceKind.BANK,
        default_entity_id=HAZEL.id,
    )
    assert f is not None
    assert f.kind is ObligationKind.KYC_REQUEST
    assert f.issuer is Issuer.BANK
    assert f.title == "Your bank needs updated details"
    assert f.due_on == date(2026, 10, 5)
    assert f.condition.proof is ProofKind.REPLY
    assert f.consequence == "The letter mentions suspension."
    assert f.reasons[0] == "From your bank"


def test_rent_with_space_thousands_and_ate_ao_dia():
    f = detect("Caro inquilino, a renda de novembro no valor de 1 200,00 € deve ser paga até ao dia 8/11/2026.",
               default_entity_id=HAZEL.id)  # fmt: skip
    assert f is not None
    assert f.kind is ObligationKind.RENT
    assert (f.due_on, f.amount) == (date(2026, 11, 8), Decimal("1200.00"))
    assert f.consequence == "Paying late may cost a late fee."


def test_insurance_renewal_that_renews_on_its_own():
    f = detect("A sua apólice de seguro multirriscos renova automaticamente a 01/11/2026. Prémio anual: 480,00 €.",
               default_entity_id=HAZEL.id)  # fmt: skip
    assert f is not None
    assert f.kind is ObligationKind.INSURANCE_RENEWAL
    assert f.due_on == date(2026, 11, 1)
    assert f.consequence == "It renews on its own unless you act."
    assert f.required_evidence == "The renewed policy, or your decision not to renew."
    assert f.condition.amount is None  # a renewal is proven by the new policy, not a payment


def test_licence_and_contract_renewals():
    lic = detect("A renovação do alvará deve ser pedida até 30/11/2026.", default_entity_id=HAZEL.id)
    assert lic is not None and lic.kind is ObligationKind.LICENSE_RENEWAL
    assert lic.title == "Licence renewal"
    con = detect("Your subscription contract renewal date: 1 December 2026.", default_entity_id=HAZEL.id)
    assert con is not None and con.kind is ObligationKind.CONTRACT_RENEWAL
    assert con.due_on == date(2026, 12, 1)


def test_filing_goes_to_the_accountant():
    f = detect("HMRC: Your VAT return for the period ending 30 September 2026 must be filed "
               "no later than 7 November 2026.", default_entity_id=HAZEL.id)  # fmt: skip
    assert f is not None
    assert f.kind is ObligationKind.FILING
    assert f.title == "Tax return"
    assert f.responsible == "accountant"
    assert f.due_on == date(2026, 11, 7)
    assert f.condition.proof is ProofKind.SUBMISSION


def test_government_request_with_relative_deadline():
    f = detect("Autoridade Tributária: Notificação. Deverá apresentar os documentos solicitados "
               "no prazo de 15 dias.", default_entity_id=HAZEL.id)  # fmt: skip
    assert f is not None
    assert f.kind is ObligationKind.GOVERNMENT_REQUEST
    assert f.title == "Request from the tax office"
    assert f.due_on == date(2026, 10, 5)  # 15 calendar days from arrival: never later than business days
    assert f.quality is Quality.AMBER
    assert any("please check the exact date" in r for r in f.reasons)


def test_debt_collection_and_generic_payment_deadline():
    debt = detect("FINAL NOTICE: outstanding debt of €310.00 must be paid by 12 October 2026 "
                  "or we will start legal action.", default_entity_id=HAZEL.id)  # fmt: skip
    assert debt is not None and debt.kind is ObligationKind.DEBT_COLLECTION
    assert debt.consequence == "The letter mentions legal action."
    generic = detect("Invoice 2026/77: amount due €99.90, due date 2026-10-10.", default_entity_id=HAZEL.id)
    assert generic is not None and generic.kind is ObligationKind.PAYMENT_DEADLINE
    assert (generic.due_on, generic.amount, generic.title) == (date(2026, 10, 10), Decimal("99.90"), "Payment due")


def test_contradicting_dates_are_a_conflict_not_a_guess():
    f = detect("Pagamento até 20/10/2026. Data limite: 25/10/2026. Total a pagar 100,00 €", default_entity_id=HAZEL.id)
    assert f is not None
    assert f.quality is Quality.RED
    assert f.due_on == date(2026, 10, 20)  # earliest kept so reminders are never late
    assert "The letter gives different dates: 20 October and 25 October." in f.reasons


def test_contradicting_amounts_are_a_conflict():
    f = detect("Data limite de pagamento: 20/10/2026. Total a pagar: 100,00 €. Valor a pagar: 110,00 €.",
               default_entity_id=HAZEL.id)  # fmt: skip
    assert f is not None and f.quality is Quality.RED
    assert "The letter gives different amounts: €100.00 and €110.00." in f.reasons


def test_the_strongly_anchored_amount_beats_other_amounts():
    f = detect("Imposto 400,00 € + juros 5,00 €. Total a pagar: 405,00 (IVA incluído 20,00 €). "
               "Data limite: 20/10/2026.", default_entity_id=HAZEL.id)  # fmt: skip
    assert f is not None and f.amount == Decimal("405.00")


def test_texts_that_are_not_obligations():
    assert detect("Our summer newsletter: 3 out of 5 customers love it. Renew your style today.") is None
    assert detect("Renove já o seu seguro com 10% de desconto.") is None  # no deadline signal
    assert detect("Lunch on Friday?") is None


def test_invoices_and_payslips_without_deadlines_are_not_obligations():
    assert detect("Fatura FT 2026/183. Total a pagar: 50,00 €. Obrigado.") is None
    assert detect("Recibo de vencimento de setembro de 2026: 1.200,00 €") is None  # "vencimento" = salary


def test_a_zero_padded_number_is_not_a_thousands_group():
    f = detect("Aviso: período 08 405,00 € — data limite de pagamento 20/10/2026.", default_entity_id=HAZEL.id)
    assert f is not None and f.amount == Decimal("405.00")


def test_deadline_word_without_a_date_asks_for_it():
    f = detect("Segurança Social: tem contribuições em dívida. Data limite de pagamento a indicar.")
    assert f is not None
    assert f.due_on is None and f.obligation is None and not f.complete
    assert f.missing == ("the due date", "which company this is for", "the amount")
    assert f.question == "I still need the due date, which company this is for and the amount."


def test_the_letters_own_date_is_not_a_deadline():
    f = detect("Lisboa, 15 de setembro de 2026. Pagamento de renda: prazo a combinar.", default_entity_id=HAZEL.id)
    assert f is not None and f.due_on is None


# --------------------------------------------------------------------------- company


def test_company_found_by_name_when_no_tax_id():
    f = detect("Oak Studio Unipessoal: renda em atraso, pagamento até 01/10/2026 de 900,00 €.")
    assert f is not None and f.entity_id == OAK.id


def test_two_companies_named_is_not_guessed():
    f = detect("Hazel Tree Lda and Oak Studio Unipessoal: rent due by 1 October 2026, €900.00.")
    assert f is not None
    assert f.entity_id is None and f.obligation is None
    assert "which company this is for" in f.missing


def test_tax_id_outranks_name_and_is_not_glued_to_neighbouring_numbers():
    f = detect("Oak Studio: Autoridade Tributária, NIF 509123456 20.10.2026 pagamento de 50,00 €, "
               "data limite 20/10/2026", default_entity_id=OAK.id)  # fmt: skip
    assert f is not None and f.entity_id == HAZEL.id


def test_a_longer_number_does_not_count_as_a_tax_id():
    f = detect("Contrato 15091234567: renda até 01/10/2026 de 900,00 €.", default_entity_id=OAK.id)
    assert f is not None and f.entity_id == OAK.id


# --------------------------------------------------------------------------- formats


@pytest.mark.parametrize(
    ("snippet", "expected"),
    [
        ("due by October 15, 2026", date(2026, 10, 15)),
        ("due by 15th October 2026", date(2026, 10, 15)),
        ("due by 2026-10-15", date(2026, 10, 15)),
        ("due by 15.10.2026", date(2026, 10, 15)),
        ("due by 15-10-26", date(2026, 10, 15)),
        ("due by 15 Oct 2026", date(2026, 10, 15)),
        ("due by 10/25/2026", date(2026, 10, 25)),  # only the month-first reading is a real date
    ],
)
def test_date_formats(snippet, expected):
    f = detect(f"Payment due: {snippet}. Amount due €10.00", default_entity_id=HAZEL.id)
    assert f is not None and f.due_on == expected


def test_year_is_inferred_forward_across_new_year():
    f = detect("Pagamento até 15 de janeiro. Valor a pagar: 10,00 €", received_on=date(2026, 12, 20),
               default_entity_id=HAZEL.id)  # fmt: skip
    assert f is not None and f.due_on == date(2027, 1, 15)


def test_abbreviated_months_need_a_year():
    f = detect("Pagamento até 3 out. Valor a pagar: 10,00 €", default_entity_id=HAZEL.id)
    assert f is not None and f.due_on is None


@pytest.mark.parametrize(
    ("snippet", "amount", "currency"),
    [
        ("€1,234.56", Decimal("1234.56"), "EUR"),
        ("1.234,56 €", Decimal("1234.56"), "EUR"),
        ("EUR 99", Decimal("99.00"), "EUR"),
        ("1 234,56 euros", Decimal("1234.56"), "EUR"),
        ("£45.10", Decimal("45.10"), "GBP"),
    ],
)
def test_amount_formats(snippet, amount, currency):
    f = detect(f"Amount due: {snippet}. Due by 1 November 2026.", default_entity_id=HAZEL.id)
    assert f is not None and (f.amount, f.currency) == (amount, currency)


def test_bare_amount_is_read_only_after_an_amount_word():
    f = detect("Total a pagar: 405,00. Data limite: 20/10/2026.", default_entity_id=HAZEL.id)
    assert f is not None and f.amount == Decimal("405.00")


def test_owner_facing_detection_text_is_plain():
    f = detect(AT_LETTER)
    assert f is not None
    for text in (f.title, f.consequence, f.required_evidence, *f.reasons):
        assert not _BANNED.search(text), text


# =========================================================================== verification condition


def test_condition_round_trip_and_normalisation():
    c = VerificationCondition(ProofKind.PAYMENT, Decimal("405"), "EUR", "123 456-789", date(2026, 10, 20))
    assert c.amount == Decimal("405.00")
    assert c.reference == "123456789"
    assert VerificationCondition.parse(c.encode()) == c
    assert c.describe(date(2026, 9, 1)) == "Proof of a payment of €405.00 with reference 123456789 by 20 October."


@pytest.mark.parametrize(
    "text",
    ["", "v2;proof=payment", "v1", "v1;proof=payment;proof=payment", "v1;proof=payment;colour=red",
     "v1;proof=nonsense", "v1;proof=payment;amount", "v1;proof=decision"],
)
def test_condition_parse_rejects_anything_else(text):
    with pytest.raises(ValueError):
        VerificationCondition.parse(text)


def test_condition_validates_values():
    with pytest.raises(TypeError):
        VerificationCondition(ProofKind.PAYMENT, 405.0)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        VerificationCondition(ProofKind.PAYMENT, Decimal("-1"))
    with pytest.raises(ValueError):
        VerificationCondition(ProofKind.PAYMENT, currency="euro")


def test_normalize_reference():
    assert normalize_reference(" rf18-5390 0754 ") == "RF1853900754"


# =========================================================================== satisfaction


def tax_obligation(**kw) -> Obligation:
    cond = VerificationCondition(ProofKind.PAYMENT, Decimal("405.00"), "EUR", "123456789", date(2026, 10, 20))
    base = dict(
        tenant_id="t1", entity_id=HAZEL.id, kind=ObligationKind.TAX_DEADLINE, title="Tax payment",
        due_on=date(2026, 10, 20), amount=Decimal("405.00"), verification_condition=cond.encode(),
    )  # fmt: skip
    base.update(kw)
    return Obligation(**base)


def payment(eid="ev_bank_1", amount="405.00", ref="123 456 789", on=date(2026, 10, 18), quality=Quality.GREEN, **kw):
    return EvidenceFact(eid, ProofKind.PAYMENT, on, quality, Decimal(amount), reference=ref, **kw)


def test_exact_verified_payment_satisfies_without_mutating_the_input():
    ob = tax_obligation()
    result = satisfy(ob, [payment(amount="-405.00")])  # sign is ignored: magnitude paid
    assert result.satisfied and result.quality is Quality.GREEN and not result.late
    assert result.evidence_ids == ("ev_bank_1",)
    assert result.obligation.satisfied_by_evidence_ids == ["ev_bank_1"]
    assert ob.satisfied_by_evidence_ids == []
    assert result.reasons == ("Paid on 18 October.",)


def test_likely_proof_never_closes_an_obligation():
    result = satisfy(tax_obligation(), [payment(quality=Quality.AMBER)])
    assert not result.satisfied
    assert result.quality is Quality.AMBER
    assert result.reasons == ("The proof isn't verified yet.",)
    assert result.obligation.satisfied_by_evidence_ids == []


def test_same_reference_different_amount_is_a_conflict():
    result = satisfy(tax_obligation(), [payment(amount="400.00")])
    assert not result.satisfied
    assert result.quality is Quality.RED
    assert result.reasons == ("The payment was €400.00 but €405.00 was due.",)


def test_another_payment_is_not_proof():
    result = satisfy(tax_obligation(), [payment(ref="999999999")])
    assert not result.satisfied and result.quality is Quality.AMBER
    assert result.reasons == ("I haven't seen proof yet.",)


def test_amount_without_reference_is_only_likely():
    result = satisfy(tax_obligation(), [payment(ref=None)])
    assert not result.satisfied and result.quality is Quality.AMBER
    assert result.reasons == ("The amount matches but the payment doesn't show the reference.",)


def test_reference_without_amount_is_only_likely():
    fact = EvidenceFact("ev_1", ProofKind.PAYMENT, date(2026, 10, 1), Quality.GREEN, reference="123456789")
    result = satisfy(tax_obligation(), [fact])
    assert not result.satisfied and result.reasons == ("The payment doesn't show the amount.",)


def test_wrong_currency_is_not_a_match():
    result = satisfy(tax_obligation(), [payment(ref=None, currency="GBP")])
    assert not result.satisfied


def test_late_payment_satisfies_but_is_flagged():
    result = satisfy(tax_obligation(), [payment(on=date(2026, 10, 22))])
    assert result.satisfied and result.late
    assert result.reasons == ("Paid on 22 October.", "That was 2 days after the deadline.")


def test_instalments_with_the_same_reference_add_up():
    parts = [payment("ev_a", "200.00", on=date(2026, 10, 2)), payment("ev_b", "205.00", on=date(2026, 10, 9))]
    result = satisfy(tax_obligation(), parts)
    assert result.satisfied
    assert result.evidence_ids == ("ev_a", "ev_b")
    assert result.reasons == ("Paid in 2 parts, the last on 9 October.",)


def test_first_matching_proof_wins_deterministically():
    facts = [payment("ev_late", on=date(2026, 10, 19)), payment("ev_early", on=date(2026, 10, 1))]
    assert satisfy(tax_obligation(), facts).evidence_ids == ("ev_early",)


def test_amount_only_condition_matches_on_amount():
    cond = VerificationCondition(ProofKind.PAYMENT, Decimal("900.00"), by=date(2026, 10, 1))
    ob = tax_obligation(kind=ObligationKind.RENT, verification_condition=cond.encode())
    assert satisfy(ob, [payment(amount="900.00", ref=None, on=date(2026, 9, 30))]).satisfied


def test_payment_condition_without_amount_or_reference_cannot_be_checked():
    cond = VerificationCondition(ProofKind.PAYMENT, by=date(2026, 10, 1))
    ob = tax_obligation(verification_condition=cond.encode())
    result = satisfy(ob, [payment()])
    assert not result.satisfied
    assert result.reasons == ("I need the amount or a reference to confirm this payment.",)


def test_renewals_decisions_and_replies():
    cond = VerificationCondition(ProofKind.RENEWAL, by=date(2026, 11, 1))
    ob = tax_obligation(kind=ObligationKind.INSURANCE_RENEWAL, amount=None, verification_condition=cond.encode())
    renewed = EvidenceFact("ev_policy", ProofKind.RENEWAL, date(2026, 10, 20), Quality.GREEN,
                           valid_until=date(2027, 11, 1))  # fmt: skip
    too_short = EvidenceFact("ev_short", ProofKind.RENEWAL, date(2026, 10, 20), Quality.GREEN,
                             valid_until=date(2026, 10, 31))  # fmt: skip
    no_end = EvidenceFact("ev_open", ProofKind.RENEWAL, date(2026, 10, 20), Quality.GREEN)
    decided = EvidenceFact("ev_owner", ProofKind.DECISION, date(2026, 10, 25), Quality.GREEN)
    assert satisfy(ob, [renewed]).satisfied
    assert satisfy(ob, [too_short]).reasons == ("The renewal ends before the current one.",)
    assert satisfy(ob, [no_end]).reasons == ("The renewal doesn't show how long it lasts.",)
    assert satisfy(ob, [decided]).satisfied
    # A decision never stands in for a payment.
    assert not satisfy(tax_obligation(), [decided]).satisfied

    reply_cond = VerificationCondition(ProofKind.REPLY, reference="PROC-2026-17", by=date(2026, 10, 5))
    request = tax_obligation(kind=ObligationKind.GOVERNMENT_REQUEST, verification_condition=reply_cond.encode())
    sent = EvidenceFact("ev_mail", ProofKind.REPLY, date(2026, 10, 1), Quality.GREEN, reference="proc 2026 17")
    other = EvidenceFact("ev_mail2", ProofKind.REPLY, date(2026, 10, 1), Quality.GREEN, reference="PROC-2025-1")
    assert satisfy(request, [sent]).satisfied
    assert satisfy(request, [sent]).reasons == ("Sent on 1 October.",)
    assert not satisfy(request, [other]).satisfied


def test_unreadable_condition_is_never_guessed():
    ob = tax_obligation(verification_condition="pay the tax office")
    result = satisfy(ob, [payment()])
    assert not result.satisfied
    assert result.reasons == ("I can't check this one automatically.",)


def test_earlier_proof_is_kept_when_more_arrives():
    ob = tax_obligation(satisfied_by_evidence_ids=["ev_old"])
    result = satisfy(ob, [payment("ev_new")])
    assert result.obligation.satisfied_by_evidence_ids == ["ev_new", "ev_old"]


def test_evidence_facts_validate_money_and_ids():
    with pytest.raises(TypeError):
        EvidenceFact("ev_1", ProofKind.PAYMENT, date(2026, 10, 1), Quality.GREEN, 405.0)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        EvidenceFact(" ", ProofKind.PAYMENT, date(2026, 10, 1), Quality.GREEN)


def test_detected_obligation_can_be_satisfied_end_to_end():
    f = detect(AT_LETTER)
    assert f is not None and f.obligation is not None
    proof = EvidenceFact("ev_bank", ProofKind.PAYMENT, date(2026, 10, 15), Quality.GREEN,
                         Decimal("-1234.56"), reference="123456789")  # fmt: skip
    assert satisfy(f.obligation, [proof]).satisfied


# =========================================================================== due soon


def make(title: str, due: date, *, entity: str = HAZEL.id, satisfied: bool = False, amount: str | None = None):
    return Obligation(
        tenant_id="t1", entity_id=entity, kind=ObligationKind.PAYMENT_DEADLINE, title=title, due_on=due,
        amount=Decimal(amount) if amount else None, satisfied_by_evidence_ids=["ev"] if satisfied else [],
    )  # fmt: skip


def test_due_soon_lists_open_items_soonest_first_including_late_ones():
    today = date(2026, 10, 1)
    obligations = [
        make("Rent payment", date(2026, 10, 4), amount="900.00"),
        make("Tax payment", date(2026, 9, 29)),
        make("Insurance renewal", date(2026, 10, 1)),
        make("Licence renewal", date(2026, 10, 2)),
        make("Paid already", date(2026, 10, 2), satisfied=True),
        make("Far away", date(2026, 12, 1)),
        make("Other company", date(2026, 10, 3), entity=OAK.id),
    ]
    items = due_soon(obligations, today, entity_id=HAZEL.id)
    assert [i.title for i in items] == ["Tax payment", "Insurance renewal", "Licence renewal", "Rent payment"]
    assert [i.line for i in items] == [
        "Tax payment · 2 days late",
        "Insurance renewal · due today",
        "Licence renewal · due tomorrow",
        "Rent payment €900.00 · due in 3 days",
    ]
    assert items[0].overdue and not items[1].overdue
    assert len(due_soon(obligations, today)) == 5
    assert due_soon(obligations, today, within_days=0)[-1].title == "Insurance renewal"


def test_due_item_one_day_late_and_validation():
    item = DueItem("obl_1", HAZEL.id, "Tax payment", date(2026, 9, 30), -1, None, "owner")
    assert item.when == "1 day late"
    with pytest.raises(ValueError):
        due_soon([], date(2026, 10, 1), within_days=-1)
