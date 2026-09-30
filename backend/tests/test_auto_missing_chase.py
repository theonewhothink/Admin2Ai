"""Supplier chasing (§22): PT/EN requests from facts only, reminders, reply matching."""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

import pytest

from backoffice.domain.models import LegalEntity, Supplier, Transaction
from backoffice.missing.chase import (
    ChaseFacts,
    ChaseThread,
    InboundEmail,
    Language,
    MatchMethod,
    ReminderPolicy,
    ReminderStep,
    activity_line,
    choose_language,
    compose_reminder,
    compose_request,
    day_month_pt,
    format_money_pt,
    match_reply,
    next_reminder,
    thread_token,
)

TODAY = date(2026, 9, 27)
DOMAIN = "mail.example.invalid"
HAZEL = LegalEntity(id="ent_hazel", tenant_id="t1", name="Hazel Tree Lda", country="PT", tax_id="PT509123456")
VODAFONE = Supplier(id="sup_voda", tenant_id="t1", name="Vodafone Portugal, S.A.", contact_email="faturas@vodafone.pt",
                    countries=["PT"])  # fmt: skip
TX = Transaction(id="tx_0123456789abcdef", tenant_id="t1", account_id="acc", booked_on=date(2026, 9, 18),
                 amount=Decimal("-117.20"), counterparty="VODAFONE PORTUGAL")  # fmt: skip
_INTERNAL = re.compile(r"\b[a-z]{2,8}_[0-9a-f]{16}\b|tx_|ent_|sup_")


def facts(language: Language = Language.EN, number: str | None = "FT 2026/183") -> ChaseFacts:
    return ChaseFacts.build(TX, VODAFONE, HAZEL, invoice_number=number, language=language)


def flat(text: str) -> str:
    return " ".join(text.split())


# --------------------------------------------------------------------------- formatting


def test_portuguese_formatting() -> None:
    assert format_money_pt(Decimal("117.2")) == "117,20 €"
    assert format_money_pt(Decimal("1234.56")) == "1.234,56 €"
    assert format_money_pt(Decimal("5"), "CHF") == "5,00 CHF"
    assert day_month_pt(date(2026, 3, 18)) == "18 de março"
    assert day_month_pt(date(2025, 9, 18), TODAY) == "18 de setembro de 2025"


def test_thread_token_is_short_stable_and_one_way() -> None:
    token = thread_token("t1", TX.id)
    assert re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{6}", token)
    assert token == thread_token("t1", TX.id)
    assert token != thread_token("t1", "tx_other")


def test_language_choice() -> None:
    assert choose_language(VODAFONE) is Language.PT
    abroad = Supplier(tenant_id="t1", name="Adobe", countries=["IE"], contact_email="billing@adobe.com")
    assert choose_language(abroad, abroad.contact_email) is Language.EN
    assert choose_language(None, "faturas@empresa.pt") is Language.PT


# --------------------------------------------------------------------------- requests


def test_spec_english_request() -> None:
    msg = compose_request(facts(), token="7KQ2MX", today=TODAY, message_id_domain=DOMAIN)
    assert msg.to == "faturas@vodafone.pt"
    assert msg.subject == "Invoice FT 2026/183 (Ref. 7KQ2MX)"
    assert flat(msg.body).startswith(
        "Hello, Could you please resend invoice FT 2026/183 relating to the €117.20 payment dated 18 September? Thank you."
    )
    assert "Our details: Hazel Tree Lda, NIF 509123456." in msg.body
    assert msg.message_id == "<chase-7kq2mx-0-20260927@mail.example.invalid>"
    assert msg.in_reply_to is None and msg.reminder_number == 0
    assert not _INTERNAL.search(msg.subject + msg.body)


def test_portuguese_request_without_invoice_number() -> None:
    msg = compose_request(facts(Language.PT, number=None), token="7KQ2MX", today=TODAY, message_id_domain=DOMAIN)
    assert msg.subject == "Fatura do pagamento de 117,20 € de 18 de setembro (Ref. 7KQ2MX)"
    assert flat(msg.body).startswith(
        "Olá, Poderiam, por favor, enviar-nos a fatura referente ao pagamento de 117,20 € de 18 de setembro? "
        "Agradecemos desde já."
    )
    assert "Os nossos dados: Hazel Tree Lda, NIF 509123456." in msg.body


def test_portuguese_request_with_number_and_old_payment() -> None:
    msg = compose_request(facts(Language.PT), token="7KQ2MX", today=date(2027, 1, 5), message_id_domain=DOMAIN)
    assert msg.subject == "Fatura FT 2026/183 (Ref. 7KQ2MX)"
    assert "reenviar a fatura FT 2026/183 referente ao pagamento de 117,20 € de 18 de setembro de 2026?" in msg.body


def test_english_request_without_number_and_foreign_company() -> None:
    oak = LegalEntity(tenant_id="t1", name="Oak Ltd", country="GB", tax_id="GB123456789")
    f = ChaseFacts.build(TX, VODAFONE, oak, language=Language.EN)
    msg = compose_request(f, token="7KQ2MX", today=TODAY, message_id_domain=DOMAIN)
    assert msg.subject == "Invoice for the €117.20 payment of 18 September (Ref. 7KQ2MX)"
    assert "Could you please send the invoice for the €117.20 payment dated 18 September? Thank you." in msg.body
    assert "Our details: Oak Ltd, VAT number GB123456789." in msg.body


def test_internal_identifiers_are_refused() -> None:
    leaky = facts(number="tx_0123456789abcdef")
    with pytest.raises(ValueError):
        compose_request(leaky, token="7KQ2MX", today=TODAY, message_id_domain=DOMAIN)


def test_facts_validation() -> None:
    with pytest.raises(TypeError):
        ChaseFacts(supplier_name="V", supplier_email="a@b.pt", amount=1.5, currency="EUR",  # type: ignore[arg-type]
                   paid_on=TODAY, company_name="H", company_tax_id="1")  # fmt: skip
    with pytest.raises(ValueError):
        ChaseFacts(supplier_name="V", supplier_email="nobody", amount=Decimal("1"), currency="EUR",
                   paid_on=TODAY, company_name="H", company_tax_id="1")  # fmt: skip
    with pytest.raises(ValueError):
        ChaseFacts.build(TX, VODAFONE.model_copy(update={"contact_email": None}), HAZEL)


def test_activity_line_is_quiet_and_plain() -> None:
    assert activity_line(facts()) == "Asked Vodafone Portugal for the invoice for the €117.20 payment."


# --------------------------------------------------------------------------- reminders


def started(sent_on: date = date(2026, 9, 21)) -> ChaseThread:  # a Monday
    first = compose_request(facts(), token="7KQ2MX", today=sent_on, message_id_domain=DOMAIN)
    return ChaseThread.start(first, sent_on)


def test_reminder_cadence_and_escalation() -> None:
    policy = ReminderPolicy()
    thread = started()
    wait = next_reminder(thread, policy, date(2026, 9, 26))
    assert wait.step is ReminderStep.WAIT and wait.on == date(2026, 9, 28)  # 27th is a Sunday -> Monday
    due = next_reminder(thread, policy, date(2026, 9, 28))
    assert due.step is ReminderStep.SEND_REMINDER and due.reminder_number == 1

    r1 = compose_reminder(facts(), thread, today=date(2026, 9, 28), message_id_domain=DOMAIN)
    assert r1.subject == "Re: Invoice FT 2026/183 (Ref. 7KQ2MX)"
    assert r1.in_reply_to == thread.sent[0].message_id and r1.references == thread.message_ids
    assert flat(r1.body).startswith(
        "Hello, A quick reminder about invoice FT 2026/183 for the €117.20 payment dated 18 September."
    )
    thread = thread.with_sent(r1, date(2026, 9, 28))
    assert next_reminder(thread, policy, date(2026, 10, 1)).step is ReminderStep.WAIT
    assert next_reminder(thread, policy, date(2026, 10, 2)).reminder_number == 2

    r2 = compose_reminder(facts(), thread, today=date(2026, 10, 2), message_id_domain=DOMAIN)
    assert r2.subject == r1.subject and r2.references == (*thread.message_ids,)
    thread = thread.with_sent(r2, date(2026, 10, 2))
    assert thread.reminders_sent == 2
    assert next_reminder(thread, policy, date(2026, 10, 6)).step is ReminderStep.ESCALATE

    replied = thread.with_reply(date(2026, 10, 3))
    assert next_reminder(replied, policy, date(2026, 10, 30)).step is ReminderStep.DONE


def test_portuguese_reminder_and_guards() -> None:
    pt = facts(Language.PT, number=None)
    first = compose_request(pt, token="7KQ2MX", today=date(2026, 9, 21), message_id_domain=DOMAIN)
    thread = ChaseThread.start(first, date(2026, 9, 21))
    reminder = compose_reminder(pt, thread, today=date(2026, 9, 28), message_id_domain=DOMAIN)
    assert flat(reminder.body).startswith(
        "Olá, Relembramos o nosso pedido: a fatura referente ao pagamento de 117,20 € de 18 de setembro."
    )
    empty = ChaseThread(token="7KQ2MX", supplier_email="a@b.pt", subject="s")
    with pytest.raises(ValueError):
        next_reminder(empty, ReminderPolicy(), TODAY)
    with pytest.raises(ValueError):
        compose_reminder(pt, empty, today=TODAY, message_id_domain=DOMAIN)


def test_weekends_can_be_allowed() -> None:
    decision = next_reminder(started(), ReminderPolicy(skip_weekends=False), date(2026, 9, 27))
    assert decision.step is ReminderStep.SEND_REMINDER


# --------------------------------------------------------------------------- reply matching


def test_reply_matching_by_headers_then_subject() -> None:
    a = started()
    other_msg = compose_request(facts(), token="ABC123", today=TODAY, message_id_domain=DOMAIN)
    b = ChaseThread.start(other_msg, TODAY)
    threads = [a, b]

    by_header = match_reply(threads, InboundEmail(from_address="Faturas <faturas@vodafone.pt>",
                                                  in_reply_to=f" {a.sent[0].message_id.upper()} "))  # fmt: skip
    assert by_header is not None and by_header.thread is a
    assert by_header.method is MatchMethod.IN_REPLY_TO and by_header.sender_matches

    by_refs = match_reply(threads, InboundEmail(from_address="x@mail.vodafone.pt",
                                                references=f"<unrelated@x> {b.sent[0].message_id}"))  # fmt: skip
    assert by_refs is not None and by_refs.thread is b and by_refs.method is MatchMethod.REFERENCES

    by_subject = match_reply(threads, InboundEmail(from_address="someone@elsewhere.com",
                                                   subject="RE: Invoice FT 2026/183 (ref. 7kq2mx)"))  # fmt: skip
    assert by_subject is not None and by_subject.thread is a
    assert by_subject.method is MatchMethod.SUBJECT_REFERENCE and not by_subject.sender_matches

    assert match_reply(threads, InboundEmail(from_address="x@vodafone.pt", subject="Your invoice")) is None
    # "reference" is not "Ref.": no accidental token from the following word.
    assert match_reply(threads, InboundEmail(from_address="x@vodafone.pt", subject="Reference 7KQ2MX")) is None


def test_ambiguous_subject_reference_is_not_guessed() -> None:
    one = started()
    twin = one.model_copy(update={"supplier_email": "billing@meo.pt"})
    assert match_reply([one, twin], InboundEmail(from_address="x@other.com", subject="Ref. 7KQ2MX")) is None
    # The supplier's own domain disambiguates.
    match = match_reply([one, twin], InboundEmail(from_address="x@meo.pt", subject="Ref. 7KQ2MX"))
    assert match is not None and match.thread is twin
