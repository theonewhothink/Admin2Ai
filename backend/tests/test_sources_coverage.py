"""Sources as the proof the owner asked for: "we see all your card payments, and we found a matching invoice for
each one" (backoffice.source_coverage), and "Something missing?" understanding what the owner types
(backoffice.source_intake). Everything here is computed from the demo business's evidence."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from backoffice.demo import evidence as E
from backoffice.domain.models import TransactionKind
from backoffice.language import find_jargon, find_off_tone
from backoffice.orchestrator import (
    Account,
    BankRow,
    ConnectorState,
    Orchestrator,
    OwnerProfile,
    Repository,
    local_datetime,
)
from backoffice.service import BackOfficeService
from backoffice.source_intake import ASK_CLAUDE

STATE_KEYS = ("proven", "notNeeded", "looking", "needsYou", "personal")


@pytest.fixture()
def svc() -> BackOfficeService:
    return BackOfficeService.demo()


def sources(svc: BackOfficeService) -> dict[str, Any]:
    status, body = svc.dispatch("GET", "/api/sources", None)
    assert status == 200, body
    return body


def items(body: dict[str, Any], group: str) -> dict[str, dict[str, Any]]:
    return {i["id"]: i for g in body["groups"] if g["id"] == group for i in g["items"]}


def payments(svc: BackOfficeService, source_id: str) -> dict[str, Any]:
    status, body = svc.dispatch("GET", f"/api/sources/{source_id}/payments", None)
    assert status == 200, body
    return body


def understand(svc: BackOfficeService, text: str) -> dict[str, Any]:
    status, body = svc.dispatch("POST", "/api/sources/understand", {"text": text})
    assert status == 200, body
    return body


# --------------------------------------------------------------------------- the summary


def test_the_summary_says_what_is_read_and_how_every_payment_stands(svc: BackOfficeService) -> None:
    summary = sources(svc)["summary"]
    assert summary["text"] == "I read 1 mailbox, 3 bank accounts and 4 cards for your 3 companies."
    assert summary["coverage"] == ("I checked all 14 payments since 1 September: 7 have their invoice or proof, "
                                   "5 need none, 1 I'm still looking for and 1 needs your answer.")
    assert summary["tone"] == "attention"  # EDP's invoice is still missing and IKEA waits for Laura
    assert summary["counts"] == {"payments": 14, "proven": 7, "notNeeded": 5, "looking": 1, "needsYou": 1,
                                 "personal": 0}
    assert summary["counts"]["payments"] == len(svc.repo.transactions)


def test_a_connection_that_stopped_is_said_first_and_never_looks_green(svc: BackOfficeService) -> None:
    svc.mark_connection_stale("gmail")  # last read 18 hours 48 minutes before 09:30
    body = sources(svc)
    assert body["summary"]["coverage"].startswith("Gmail has not been read since 14:42 yesterday. I checked all 14 ")
    assert body["summary"]["tone"] == "attention"
    gmail = items(body, "email")["gmail"]
    assert gmail["status"] == "stale"
    assert gmail["coverage"]["text"] == "Not read since 14:42 yesterday · 6 invoices and receipts found"


def test_everything_proven_or_not_needed_is_green_and_said_so() -> None:
    now = local_datetime(date(2026, 10, 2), 9, 30)
    repo = Repository(tenant_id="t-calm", owner=OwnerProfile("Ana", "Ana Costa", "ana@costa.pt"), now=now)
    repo.add_company(id="costa", name="Costa Lda", legal_name="Costa, Lda.", tax_id=E.HAZEL_NIF,
                     ibans=[E.HAZEL_IBAN])
    repo.add_account(Account(id="acct", bank="Millennium BCP", holder_id="costa", iban=E.HAZEL_IBAN))
    repo.add_connector(ConnectorState(
        id="bank", name="Millennium BCP", kind="bank", account="Costa Lda", company_ids=("costa",), healthy=True,
        covered_from=local_datetime(date(2026, 7, 1), 0, 0), covered_until=now, last_synced_at=now))
    orchestrator = Orchestrator(repo)
    orchestrator.ingest_bank([BankRow(bank_id="b1", account_id="acct", booked_on=date(2026, 9, 30),
                                      amount=Decimal("-6.24"), counterparty="MILLENNIUM BCP",
                                      description="COMISSAO MANUTENCAO CONTA", kind=TransactionKind.FEE)],
                             at=now)
    orchestrator.run(now)
    svc = BackOfficeService(orchestrator)
    body = sources(svc)
    assert body["summary"] == {
        "text": "I read 1 bank account for Costa Lda.",
        "coverage": "I checked your one payment since 30 September: it needs no invoice.",
        "tone": "good",
        "counts": {"payments": 1, "proven": 0, "notNeeded": 1, "looking": 0, "needsYou": 0, "personal": 0}}
    assert items(body, "banks")["acct"]["coverage"]["text"] == "1 payment since 30 September: it needs no invoice."


def test_a_card_not_linked_to_its_bank_is_said_and_keeps_the_summary_amber(svc: BackOfficeService) -> None:
    status, _ = svc.dispatch("POST", "/api/sources", {"kind": "card", "last4": "9911", "bank": "Revolut",
                                                      "companyId": "hazel-tree"})
    assert status == 200
    body = sources(svc)
    card = next(i for i in items(body, "cards").values() if i["name"] == "Card •••• 9911")
    assert card["status"] == "not_connected"
    assert card["coverage"]["text"] == "No payments since 1 September yet."
    assert "Card •••• 9911 is not linked to its bank yet, so I can't see its payments." in body["summary"]["coverage"]
    assert body["summary"]["tone"] == "attention"


# --------------------------------------------------------------------------- each source's line


def test_every_bank_account_and_card_accounts_for_each_of_its_payments(svc: BackOfficeService) -> None:
    body = sources(svc)
    accounts = {**items(body, "banks"), **items(body, "cards")}
    assert set(accounts) == {"mbcp-ht", "mbcp-cc", "cgd-b", "card-5530", "card-7702", "card-2291", "card-4817"}
    total = 0
    for sid, item in accounts.items():
        counts = item["coverage"]["counts"]
        assert sum(counts[k] for k in STATE_KEYS) == counts["payments"], sid
        listed = payments(svc, sid)
        assert len(listed["items"]) == counts["payments"], sid
        assert [r["state"] for r in listed["items"]].count("proven") == counts["proven"], sid
        assert listed["coverage"] == item["coverage"]
        total += counts["payments"]
    assert total == len(svc.repo.transactions) == 14  # each payment is counted once, on its own account or card


def test_the_lines_are_exact(svc: BackOfficeService) -> None:
    body = sources(svc)
    lines = {i["id"]: i["coverage"]["text"] for g in body["groups"] for i in g["items"] if "coverage" in i}
    assert lines == {
        "gmail": "Read since 1 June · 6 invoices and receipts found · last read 09:12",
        "mbcp-ht": "3 of 6 payments since 1 September have their invoice or proof; 2 need none and I'm looking "
                   "for 1.",
        "mbcp-cc": "2 payments since 1 September: none needs an invoice.",
        "cgd-b": "1 of 2 payments since 1 September has its invoice; 1 needs none.",
        "card-5530": "1 payment since 1 September: it has its invoice.",
        "card-7702": "1 payment since 1 September: it has its invoice.",
        "card-2291": "1 payment since 1 September: it has its invoice.",
        "card-4817": "1 payment since 1 September: it needs your answer.",
        "accountant": "2 questions from them: 1 answered, 1 open · last in touch 18:20 yesterday",
    }
    # Banks and cards say which connection reads them, for Reconnect.
    assert items(body, "cards")["card-7702"]["connectionId"] == "cgd"
    assert items(body, "banks")["mbcp-ht"]["connectionId"] == "millennium"


def test_a_card_payment_listed_under_the_bank_account_is_the_cards(svc: BackOfficeService) -> None:
    # Open banking lists card payments under the account, with the card's last 4 digits: they are the card's.
    svc.orchestrator.ingest_bank([BankRow(bank_id="ob-1", account_id="mbcp-ht", booked_on=date(2026, 10, 1),
                                          amount=Decimal("-12.50"), counterparty="UBER *TRIP",
                                          description="COMPRA CARTAO", kind=TransactionKind.CARD,
                                          card_last4="5530")])
    card = payments(svc, "card-5530")
    assert [r["merchant"] for r in card["items"]] == ["Uber", "Uber"]
    assert card["coverage"]["counts"]["payments"] == 2
    assert all(r["merchant"] != "Uber" for r in payments(svc, "mbcp-ht")["items"])
    body = sources(svc)
    accounts = {**items(body, "banks"), **items(body, "cards")}
    assert sum(i["coverage"]["counts"]["payments"] for i in accounts.values()) == len(svc.repo.transactions) == 15


def test_one_mailbox_of_several_counts_the_mail_sent_to_it(svc: BackOfficeService) -> None:
    svc.dispatch("POST", "/api/sources", {"kind": "email", "provider": "microsoft", "address": "office@hazeltree.pt"})
    mail = {i["name"]: i["coverage"]["text"] for i in items(sources(svc), "email").values()}
    assert mail["laura@hazeltree.pt"] == "Read since 1 June · 6 invoices and receipts found · last read 09:12"
    # Adobe's invoice was sent to jorge@companyc.pt, which is neither mailbox: it counts for both.
    assert mail["office@hazeltree.pt"].split(" · ")[1] == "1 invoice found"


# --------------------------------------------------------------------------- one card's payments


def test_a_card_or_account_lists_its_payments_newest_first_with_their_state(svc: BackOfficeService) -> None:
    rows = payments(svc, "mbcp-ht")["items"]
    assert [r["date"] for r in rows] == sorted((r["date"] for r in rows), reverse=True)
    said = {r["merchant"]: (r["state"], r["stateText"]) for r in rows}
    assert said == {
        "Millennium BCP": ("not_needed", "No invoice needed: bank charge"),
        "Company C Studio": ("not_needed", "No invoice needed: money moved between your own accounts"),
        "Autoridade Tributaria": ("proven", "Tax letter found"),
        "EDP": ("looking", "Looking for the invoice since 22 September"),
        "Vodafone": ("proven", "Invoice found"),
        "Marta Gonçalves": ("proven", "Invoice-receipt found"),
    }
    for r in rows:  # each opens the payment's own page
        assert r["href"] == r["detailHref"] == f"/payments/detail?id={r['id']}"
        assert r["direction"] == "out" and r["currency"] == "EUR" and r["companyName"] == "Hazel Tree"
    vodafone = next(r for r in rows if r["merchant"] == "Vodafone")
    assert vodafone["amount"] == 92.4
    assert payments(svc, "card-5530")["items"][0]["stateText"] == "Receipt found"
    money_in = payments(svc, "mbcp-cc")["items"][-1]
    assert money_in["direction"] == "in" and money_in["amount"] == 500


def test_a_payment_waiting_for_the_owner_links_to_its_question(svc: BackOfficeService) -> None:
    [ikea] = payments(svc, "card-4817")["items"]
    assert (ikea["merchant"], ikea["state"], ikea["stateText"]) == ("IKEA", "needs_you", "Needs your answer")
    assert ikea["href"] == "/needs-you#nd_ikea_418"
    assert ikea["detailHref"] == f"/payments/detail?id={ikea['id']}"
    status, _ = svc.dispatch("POST", "/api/needs-you/nd_ikea_418/answer", {"option_id": "personal"})
    assert status == 200
    [ikea] = payments(svc, "card-4817")["items"]
    assert (ikea["state"], ikea["stateText"], ikea["companyName"]) == (
        "personal", "Personal: you said it is not for your companies.", "")
    summary = sources(svc)["summary"]
    assert summary["coverage"].endswith("1 I'm still looking for and 1 is personal.")
    assert summary["counts"]["personal"] == 1 and summary["counts"]["needsYou"] == 0


def test_only_bank_accounts_and_cards_have_payments(svc: BackOfficeService) -> None:
    for sid in ("gmail", "accountant", "sup-edp", "nothing-here"):
        status, body = svc.dispatch("GET", f"/api/sources/{sid}/payments", None)
        assert status == 404 and body["message"] == "I can't find that bank account or card."


# --------------------------------------------------------------------------- the companies


def test_each_company_with_its_tax_number_and_the_sources_that_feed_it(svc: BackOfficeService) -> None:
    companies = {c["id"]: c for c in sources(svc)["companies"]}
    hazel = companies["hazel-tree"]
    assert (hazel["name"], hazel["taxIdLabel"], hazel["taxId"]) == ("Hazel Tree", "NIF", E.HAZEL_NIF)
    assert [s["name"] for s in hazel["sources"]] == ["laura@hazeltree.pt", "Millennium BCP •••• 0265",
                                                     "Card •••• 5530"]
    assert [s["kind"] for s in hazel["sources"]] == ["email", "bank", "card"]
    assert [s["name"] for s in companies["company-c"]["sources"]] == [
        "laura@hazeltree.pt", "Millennium BCP •••• 3382", "Card •••• 2291", "Card •••• 4817"]
    assert [s["name"] for s in companies["company-b"]["sources"]] == [
        "laura@hazeltree.pt", "Caixa Geral de Depósitos •••• 3007", "Card •••• 7702"]


def test_a_company_in_another_country_uses_its_packs_name_for_the_tax_number(svc: BackOfficeService) -> None:
    status, _ = svc.dispatch("POST", "/api/onboarding/company", {"name": "Hazel Madrid", "taxId": "B12345674",
                                                                  "country": "ES"})
    assert status == 200
    madrid = next(c for c in sources(svc)["companies"] if c["name"] == "Hazel Madrid")
    assert madrid["taxIdLabel"] == "NIF or CIF"


# --------------------------------------------------------------------------- "Something missing?"


def test_an_email_address_is_a_mailbox_with_its_provider(svc: BackOfficeService) -> None:
    assert understand(svc, "ana@gmail.com") == {
        "kind": "email", "fields": {"address": "ana@gmail.com", "provider": "google"},
        "message": "ana@gmail.com is a Gmail address. Sign in with Google once and I read it."}
    assert understand(svc, "rui@hotmail.com")["fields"]["provider"] == "microsoft"
    # hazeltree.pt is on Google: laura@hazeltree.pt is read through Gmail.
    assert understand(svc, "Please read invoices@hazeltree.pt too")["fields"] == {
        "address": "invoices@hazeltree.pt", "provider": "google"}
    unknown = understand(svc, "contas@costa.pt")
    assert unknown["fields"] == {"address": "contas@costa.pt"}
    assert unknown["message"] == "Is contas@costa.pt with Google, Microsoft or another provider? Choose it and tap Add."
    assert understand(svc, "LAURA@hazeltree.pt") == {
        "kind": "email", "fields": {"address": "laura@hazeltree.pt"},
        "message": "laura@hazeltree.pt is already connected.", "already": True}


def test_an_iban_is_a_bank_account_named_by_its_bank_code(svc: BackOfficeService) -> None:
    assert understand(svc, "PT76 0007 0000 0012 3456 7892 3") == {
        "kind": "bank", "fields": {"iban": "PT76000700000012345678923", "bank": "Novo Banco"},
        "message": "PT76 •••• 8923 is a Novo Banco account. Check the company and tap Add."}
    assert understand(svc, "PT50 0002 0123 1234 5678 9015 4")["message"] == \
        "Which bank is PT50 •••• 0154 with? Fill it in and tap Add."
    assert understand(svc, "PT50 0033 0000 4532 8817 1026 4") == {
        "kind": "bank", "fields": {"iban": "PT50003300004532881710264"},
        "message": "That IBAN doesn't add up. Check the digits."}
    connected = understand(svc, E.HAZEL_IBAN)
    assert connected["already"] is True and connected["message"] == "Millennium BCP •••• 0265 is already connected."


def test_a_card_and_its_last_four_digits(svc: BackOfficeService) -> None:
    assert understand(svc, "my Revolut card ending 4821") == {
        "kind": "card", "fields": {"last4": "4821", "bank": "Revolut"},
        "message": "Card •••• 4821 from Revolut. Check the company and tap Add."}
    whole = understand(svc, "4111 1111 1111 1111")  # a whole card number: only its last 4 digits are kept
    assert whole["fields"] == {"last4": "1111"}
    assert "4111" not in str(whole) and whole["message"].endswith("I only keep the last 4 digits.")
    assert understand(svc, "card 5530")["already"] is True


def test_a_bank_by_its_name(svc: BackOfficeService) -> None:
    assert understand(svc, "Novo Banco") == {
        "kind": "bank", "fields": {"bank": "Novo Banco"},
        "message": "Add your Novo Banco account: check the details and tap Add."}
    assert understand(svc, "bank leumi")["fields"] == {"bank": "Bank Leumi"}
    assert understand(svc, "millennium")["message"].startswith("I already read Millennium BCP.")


def test_cloud_storage_by_its_link_or_its_name(svc: BackOfficeService) -> None:
    link = "https://drive.google.com/drive/folders/1AbCdEfGhIjK"
    assert understand(svc, link) == {
        "kind": "files", "fields": {"provider": "google", "address": "laura@hazeltree.pt", "folder": link},
        "message": "Google Drive: sign in once and I search it for missing invoices."}
    assert understand(svc, "our SharePoint")["fields"]["provider"] == "microsoft"
    assert understand(svc, "https://contoso.sharepoint.com/sites/finance")["kind"] == "files"


def test_accounting_software_by_its_name_or_address(svc: BackOfficeService) -> None:
    assert understand(svc, "TOConline")["fields"] == {"provider": "toconline"}
    assert understand(svc, "we use moloni")["fields"] == {"provider": "moloni"}
    assert understand(svc, "https://hazeltree.app.invoicexpress.com/")["fields"] == {
        "provider": "invoicexpress", "account": "hazeltree"}


def test_a_suppliers_website_by_its_address_or_its_name(svc: BackOfficeService) -> None:
    assert understand(svc, "the EDP website") == {
        "kind": "portal", "fields": {"supplier": "EDP"},
        "message": "EDP's website: add your sign-in there and I fetch your invoices."}
    assert understand(svc, "https://www.vodafone.pt/login")["fields"] == {"supplier": "Vodafone"}
    assert understand(svc, "worten.pt")["fields"] == {"supplier": "Worten"}


def test_anything_else_goes_to_the_chat_and_nothing_empty_is_accepted(svc: BackOfficeService) -> None:
    assert understand(svc, "my lawyer's invoices") == {
        "kind": "ask", "fields": {"text": "my lawyer's invoices"}, "message": ASK_CLAUDE}
    assert ASK_CLAUDE == "I'll ask Claude to help with that."
    for body in ({"text": "  "}, {}, {"text": 7}):
        status, reply = svc.dispatch("POST", "/api/sources/understand", body)
        assert status == 400 and reply["message"] == "Tell me what I'm not reading yet."


def test_understanding_only_reads(svc: BackOfficeService) -> None:
    from backoffice.server.events import state_digest

    before = state_digest(svc)
    for text in ("ana@gmail.com", "PT76 0007 0000 0012 3456 7892 3", "my Revolut card ending 4821", "Moloni",
                 "the EDP website", "my lawyer"):
        understand(svc, text)
    svc.dispatch("GET", "/api/sources/card-4817/payments", None)
    assert state_digest(svc) == before


# --------------------------------------------------------------------------- plain words


def test_every_new_line_is_plain_and_calm(svc: BackOfficeService) -> None:
    svc.mark_connection_stale("gmail")
    body = sources(svc)
    texts = [body["summary"]["text"], body["summary"]["coverage"]]
    texts += [i["coverage"]["text"] for g in body["groups"] for i in g["items"] if "coverage" in i]
    for sid in ("mbcp-ht", "mbcp-cc", "cgd-b", "card-5530", "card-4817"):
        texts += [r["stateText"] for r in payments(svc, sid)["items"]]
    for said in ("ana@gmail.com", "contas@costa.pt", "PT50 0002 0123 1234 5678 9015 4", "PT50 0033 0000 4532 8817 1026 4",
                 "my Revolut card ending 4821", "4111 1111 1111 1111", "my Revolut card", "Novo Banco", "OneDrive",
                 "TOConline", "the EDP website", "www.worten.pt", "my lawyer", "my bank account"):
        texts.append(understand(svc, said)["message"])
    for text in texts:
        assert find_jargon(text) == [], text
        assert find_off_tone(text) == [], text
