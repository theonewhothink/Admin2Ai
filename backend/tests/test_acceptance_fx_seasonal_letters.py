"""Acceptance: exchange differences, chargebacks, seasons, security deposits, tourist tax and grant letters.

1. FX (checklist I11; cases 5, 23, 24, 25, 32, 36, 49). An invoice in dollars or pounds closes against the
   euro charge on the bank's conversion line (checklist P7). What it was worth on its own date differs from
   what the bank charged: that exchange difference is recorded for the accountant, from a reference rate on
   the invoice date given by an injectable source (a fixed table here; the demo has none and never guesses).
   Spending and income answers count foreign payments at the euros actually charged and say so; a payment
   with no euro amount (a dollar account) is listed apart, in its own currency, money in included.
2. Chargebacks on the bank statement (I7): a card payment a customer disputed, taken back, is linked to the
   sale in its payout (the payout report lists it), else one plain question; money won back links both.
   Neither is a cost nor income.
3. Seasonal and irregular recurring patterns (X29; cases 31, 39): monthly-in-season, quarterly and yearly
   rhythms with their usual amounts; "missing" never fires out of season and does in season.
4. Security deposits (X9; case 33): held for the customer, never revenue; the return closes it; a part kept
   becomes income only with an invoice for it or the owner's confirmation.
5. Tourist tax and grant letters (X26, X30; cases 18, 40): obligations with who does them, the deadline, the
   proof they need, done only by that proof; a grant received is a grant, never a sale.

Every scenario runs through the live orchestrator and service. Companies, people, tax numbers, IBANs and
reference rates are fictional.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest
from test_acceptance_deposits_milestones import C_NIF, MARIA_NIF
from test_acceptance_engine_fixes import NORTE_NIF, qr_document
from test_acceptance_foreign_documents import AWS_INVOICE, UK_STUDIO, card, owner_texts, pay, tenant, upload
from test_acceptance_settlements import NOW, SIBS_REPORT, bank, business

from backoffice.chargebacks import chargeback_words
from backoffice.closure import VerificationCondition
from backoffice.closure.obligations import detect_obligation, grant_agency, is_grant_text, mentions_tourist_tax
from backoffice.demo import build_demo
from backoffice.demo import evidence as E
from backoffice.domain.lifecycle import Stage
from backoffice.domain.models import (
    LegalEntity,
    ObligationKind,
    Quality,
    Supplier,
    Transaction,
    TransactionKind,
)
from backoffice.language import find_jargon, find_off_tone
from backoffice.learning.recurrence import Basis, Cadence, Occurrence, check_overdue, learn_series, next_expected
from backoffice.orchestrator import Account, BankRow, Orchestrator, local_datetime
from backoffice.reconciliation import DatedFxRates, ExpectedEvidenceEngine
from backoffice.service import BackOfficeService
from backoffice.spending import Ledger

D = Decimal
K = TransactionKind
SEPT = (date(2026, 9, 1), date(2026, 9, 30))
USD_IBAN = "GB33BUKB20201555555555"  # Hazel Tree's US dollar account at a fictional bank
# ECB-style reference rates (units of the foreign currency for one euro), fictional values.
REFERENCE = DatedFxRates({
    ("EUR", "USD", date(2026, 9, 2)): D("1.1028"),  # 1 USD = 0.9068 EUR: USD 125.00 = €113.35
    ("EUR", "GBP", date(2026, 9, 17)): D("0.8480"),  # 1 GBP = 1.1792 EUR: £240.00 = €283.02
})


def plain(*texts: str) -> None:
    for text in texts:
        assert not find_jargon(text), (text, find_jargon(text))
        assert not find_off_tone(text), text
        for internal in ("tx_", "doc_", "ev_", "nd_", "obl_"):
            assert internal not in text, text


def stage(o: Orchestrator, record) -> Stage:  # type: ignore[no-untyped-def]
    return o.repo.items[record.item_id].stage


def golden_rule(o: Orchestrator) -> None:
    """Every transition carries stored evidence, a closed item is GREEN, the audit chain holds, the auditor
    reopens nothing, and every owner screen reads."""
    for item in o.repo.items.values():
        for t in item.history:
            assert t.evidence_ids and t.actor, item.id
            for ev in t.evidence_ids:
                o.repo.evidence(ev)
        if item.stage is Stage.CLOSED:
            assert item.quality is Quality.GREEN, item.id
    assert o.repo.audit.verify(o.repo.tenant_id).ok
    assert o.auditor.recheck() == []
    svc = BackOfficeService(o)
    for path in ("/api/home", "/api/needs-you", "/api/pipeline", "/api/activity", "/api/obligations"):
        status, body = svc.dispatch("GET", path, None)
        assert status == 200, (path, body)
    for n in svc.needs_you()["items"]:
        plain(n.get("question", ""), *n.get("why", []), *(o["label"] for o in n.get("options", [])))
    plain(*(d["note"] for d in svc.home()["dueSoon"]), *(a["text"] for a in svc.activity()["items"]))
    from backoffice.internal import operations, overview

    assert overview([svc]) and operations([svc])["activity"]


# =========================================================================== 1. FX differences and foreign totals


def abroad(rates: DatedFxRates | None = REFERENCE) -> Orchestrator:
    """Hazel Tree (Lisbon) with its suppliers abroad, a US dollar account, and a reference-rate source."""
    o = tenant()
    o.repo.fx_rates = rates
    o.repo.add_account(Account(id="wise-usd", bank="Wise", holder_id="hazel-tree", iban=USD_IBAN))
    return o


def usd_row(bank_id: str, day: date, amount: str, who: str, description: str) -> BankRow:
    kind = K.TRANSFER_IN if D(amount) > 0 else K.TRANSFER_OUT
    return BankRow(bank_id=bank_id, account_id="wise-usd", booked_on=day, amount=D(amount), counterparty=who,
                   description=description, kind=kind, currency="USD")


def test_the_exchange_difference_is_recorded_for_the_accountant_from_the_rate_on_the_invoice_date() -> None:
    o = abroad()
    doc = upload(o, AWS_INVOICE, date(2026, 9, 2), "aws.txt")
    tx = pay(o, card("c-0903", date(2026, 9, 3), "-115.19", "AWS EMEA", "COMPRA AWS EMEA USD 125,00 TAXA 0,9215"))
    assert tx.document_ids == [doc.id] and stage(o, doc) is Stage.CLOSED and stage(o, tx) is Stage.CLOSED
    found = o.repo.fx_differences[doc.id]
    assert (found.currency, found.foreign_amount, found.invoice_date) == ("USD", D("125.00"), date(2026, 9, 2))
    assert found.at_invoice_date == D("113.35") and found.booked == D("115.19") and found.difference == D("1.84")
    assert found.bank_rate == D("0.9215") and found.direction == "out"
    assert found.line == "Exchange difference: €1.84 more than on the invoice date"
    # recorded once, with its evidence, in the audit trail
    entries = [r for r in o.repo.audit_store.records(o.repo.tenant_id) if r.action == "exchange_difference"]
    assert len(entries) == 1 and set(doc.evidence_ids) | {tx.evidence_id} <= set(entries[0].evidence_ids)
    o.run(local_datetime(date(2026, 10, 2), 9, 30))
    assert len([r for r in o.repo.audit_store.records(o.repo.tenant_id) if r.action == "exchange_difference"]) == 1

    # the pound invoice paid at a better rate than on its date: less than on the invoice date
    studio = upload(o, UK_STUDIO, date(2026, 9, 17), "studio.txt")
    pay(o, card("c-0917", date(2026, 9, 17), "-281.21", "NORTHLIGHT STUDIO",
                "COMPRA NORTHLIGHT STUDIO GBP 240,00 TAXA 1,1717"))
    assert o.repo.fx_differences[studio.id].line == "Exchange difference: €1.81 less than on the invoice date"

    # the accountant's view: a plain line each, with the figures behind it; the owner reads none of it
    svc = BackOfficeService(o)
    flags = {f["id"]: f for f in svc.accountant_client("hazel-tree")["taxFlags"]}
    aws = flags[f"t_fx_{doc.id}"]
    assert aws["title"] == "Exchange difference: €1.84 more than on the invoice date"
    assert aws["detail"] == ("AWS invoice 1432987654 for USD 125.00: €113.35 at the reference rate on 2 September "
                             "2026 (1 USD = 0.9068 EUR); €115.19 charged on 3 September 2026 at 0.9215 EUR.")
    assert flags[f"t_fx_{studio.id}"]["title"] == "Exchange difference: €1.81 less than on the invoice date"
    for path in ("/api/home", "/api/needs-you", "/api/activity", "/api/months/hazel-tree/2026-09",
                 f"/api/transactions/{tx.id}", f"/api/documents/{doc.id}"):
        status, body = svc.dispatch("GET", path, None)
        assert status == 200, path
        assert not any("xchange difference" in t or "reference rate" in t for t in owner_texts(body)), path
    golden_rule(o)


def test_without_a_reference_rate_no_exchange_difference_is_ever_guessed() -> None:
    o = abroad(rates=None)  # the browser demo: no source at all
    doc = upload(o, AWS_INVOICE, date(2026, 9, 2), "aws.txt")
    pay(o, card("c-0903", date(2026, 9, 3), "-115.19", "AWS EMEA", "COMPRA AWS EMEA USD 125,00 TAXA 0,9215"))
    assert stage(o, doc) is Stage.CLOSED and o.repo.fx_differences == {}
    assert not [f for f in BackOfficeService(o).accountant_client("hazel-tree")["taxFlags"]
                if f["id"].startswith("t_fx_")]
    # a source without a rate for that currency or day: nothing either
    other = abroad(rates=DatedFxRates({("EUR", "GBP", date(2026, 9, 2)): D("0.85")}))
    aws = upload(other, AWS_INVOICE, date(2026, 9, 2), "aws.txt")
    pay(other, card("c-0903", date(2026, 9, 3), "-115.19", "AWS EMEA", "COMPRA AWS EMEA USD 125,00 TAXA 0,9215"))
    assert stage(other, aws) is Stage.CLOSED and other.repo.fx_differences == {}
    # without the bank's conversion line the invoice never closes, so there is nothing to compare
    third = abroad()
    open_doc = upload(third, AWS_INVOICE, date(2026, 9, 2), "aws.txt")
    pay(third, card("c-0903", date(2026, 9, 3), "-115.19", "AWS EMEA"))
    assert stage(third, open_doc) is not Stage.CLOSED and third.repo.fx_differences == {}
    # and the demo has no source and records nothing
    demo = build_demo()
    assert demo.repo.fx_rates is None and demo.repo.fx_differences == {}


def test_reference_rates_come_from_a_fixed_table_looking_back_over_weekends() -> None:
    table = DatedFxRates({("EUR", "USD", date(2026, 9, 4)): D("1.1000")})  # a Friday
    assert table.rate("EUR", "USD", date(2026, 9, 6)) == D("1.1000")  # Sunday: Friday's rate
    assert table.rate("USD", "EUR", date(2026, 9, 5)) == D(1) / D("1.1000")  # the inverse is derived
    assert table.rate("EUR", "USD", date(2026, 9, 3)) is None  # nothing before the table starts
    assert table.rate("EUR", "USD", date(2026, 9, 20)) is None  # too far from the last rate: never stretched
    assert table.rate("EUR", "EUR", date(2026, 9, 20)) == D(1)
    with pytest.raises(ValueError):
        DatedFxRates({("EUR", "USD", date(2026, 9, 4)): 1.1})  # type: ignore[dict-item]


def test_spending_counts_foreign_card_payments_at_the_euros_charged_and_lists_the_rest_apart() -> None:
    o = abroad()
    upload(o, AWS_INVOICE, date(2026, 9, 2), "aws.txt")
    upload(o, UK_STUDIO, date(2026, 9, 17), "studio.txt")
    o.ingest_bank([
        card("c-0903", date(2026, 9, 3), "-115.19", "AWS EMEA", "COMPRA AWS EMEA USD 125,00 TAXA 0,9215"),
        card("c-0917", date(2026, 9, 17), "-281.21", "NORTHLIGHT STUDIO",
             "COMPRA NORTHLIGHT STUDIO GBP 240,00 TAXA 1,1717"),
        card("c-0922", date(2026, 9, 22), "-198.00", "HOTEL MIRADOR GRANADA"),
        usd_row("w-0915", date(2026, 9, 15), "-49.00", "GITHUB INC", "GITHUB TEAM PLAN"),
    ], at=local_datetime(date(2026, 9, 30), 23, 0))
    svc = BackOfficeService(o)
    money = Ledger(svc).money(*SEPT, direction="out", company_ids=["hazel-tree"])
    assert money.total == D("594.40")  # 115.19 + 281.21 + 198.00, the euros actually charged
    assert sorted((x.merchant, x.original_currency, x.original_amount) for x in money.converted) == [
        ("AWS", "USD", D("125.00")), ("Northlight Studio", "GBP", D("240.00"))]
    (foreign,) = money.other_currency_lines
    assert (foreign.amount, foreign.currency) == (D("49.00"), "USD") and money.other_currency == 1
    answer = svc.ask("How much did Hazel Tree spend in September?")["answer"]
    assert answer.startswith("Hazel Tree spent €594.40 in September")
    assert ("2 payments were in other currencies: I counted what your bank charged in euros, AWS $125.00 "
            "(€115.19) and Northlight Studio £240.00 (€281.21).") in answer
    assert "Not in the euro total: $49.00 to Github on 15 September, paid in US dollars with no euro amount." in \
        answer
    plain(answer)
    # the model's facts say the same
    status, body = svc.dispatch("POST", "/api/chat/tool", {"name": "spending_summary", "input": {
        "date_from": "2026-09-01", "date_to": "2026-09-30", "direction": "out", "company_id": "hazel-tree"}})
    assert status == 200, body
    facts = body["result"]
    assert facts["total_eur"] == 594.40
    assert {(x["merchant"], x["original_currency"]) for x in facts["converted_at_the_euros_the_bank_booked"]} == {
        ("AWS", "USD"), ("Northlight Studio", "GBP")}
    assert facts["other_currency_payments_not_in_the_total"] == [{
        "date": "2026-09-15", "merchant": "Github", "amount": 49.0, "currency": "USD", "direction": "out",
        "company": "Hazel Tree", "evidence_id": foreign.evidence_id}]
    # one payment converted: said in one sentence
    one = Ledger(svc).money(date(2026, 9, 1), date(2026, 9, 10), direction="out", company_ids=["hazel-tree"])
    assert [x.merchant for x in one.converted] == ["AWS"]
    early = svc.ask("How much did Hazel Tree spend from 1 to 10 September?")["answer"]
    assert "The AWS payment on 3 September was $125.00: I counted the €115.19 your bank charged in euros." in early


def test_money_received_in_dollars_into_a_dollar_account_is_reported_in_dollars() -> None:
    o = abroad()
    o.ingest_bank([
        usd_row("w-0914", date(2026, 9, 14), "2000.00", "ACME CORP", "INVOICE HT-US-7 PAYMENT"),
        BankRow(bank_id="m-0918", account_id="mbcp-ht", booked_on=date(2026, 9, 18), amount=D("1230.00"),
                counterparty="ATELIER LUME LDA", description="TRF PROJETO", kind=K.TRANSFER_IN),
    ], at=local_datetime(date(2026, 9, 30), 23, 0))
    svc = BackOfficeService(o)
    money = Ledger(svc).money(*SEPT, direction="in", company_ids=["hazel-tree"])
    assert money.total == D("1230.00")  # the euro total never mixes in dollars
    (dollars,) = money.other_currency_lines
    assert (dollars.amount, dollars.currency, dollars.direction) == (D("2000.00"), "USD", "in")
    answer = svc.ask("How much did Hazel Tree receive in September?")["answer"]
    assert answer.startswith("€1,230.00 came in for Hazel Tree in September")
    assert (f"Also received in US dollars, not in the euro total: $2,000.00 from {dollars.merchant} on 14 "
            "September.") in answer
    plain(answer)
    card_notes = svc.chat({"message": "How much did Hazel Tree receive in September?"})["cards"][0]["notes"]
    assert any("$2,000.00" in n for n in card_notes)


# =========================================================================== 2. chargebacks on the bank statement


def sibs_payout(svc: BackOfficeService, *, with_report: bool = True) -> tuple[str, str | None]:
    """The card terminal's 18 September batch, paid out on the 19th (and SIBS's report for it)."""
    (payout,) = bank(svc, (date(2026, 9, 19), "1394.00", "TPA 1234567", "LIQ TPA LOTE L0918-4471"))
    if not with_report:
        return payout, None
    mail = E.email(sender="SIBS Extratos <extratos@sibs.pt>", to="rita@casaazul.pt", subject="Liquidações TPA",
                   at=NOW, text="Segue em anexo o extrato de liquidações.\n", message_id="<liq-0918@sibs.pt>",
                   attachments=(("liquidacoes_0918.csv", "text/csv", SIBS_REPORT.encode("utf-8")),))
    report = svc.orchestrator.ingest_file(mail, filename="liquidacoes.eml", content_type="message/rfc822")
    return payout, report.document_ids[0]


def closing(svc: BackOfficeService, tx_id: str):  # type: ignore[no-untyped-def]
    item = svc.repo.items[svc.repo.transactions[tx_id].item_id]
    assert item.stage is Stage.CLOSED and item.quality is Quality.GREEN
    return item.history[-1]


def test_a_chargeback_on_the_bank_statement_is_linked_to_its_sale_and_is_neither_a_cost_nor_income() -> None:
    svc = business()
    repo = svc.repo
    payout, report_id = sibs_payout(svc)
    assert report_id is not None and repo.settlements[report_id].settled
    sales_before = Ledger(svc).money(*SEPT, direction="in").total
    (taken,) = bank(svc, (date(2026, 9, 26), "-45.00", "SIBS", "CHARGEBACK TPA 1234567 COMPRA 17/09/2026"))
    rec = repo.transactions[taken]
    assert rec.decision.rule == "chargeback" and not rec.decision.requires_document
    cb = repo.chargebacks[taken]
    assert (cb.direction, cb.status, cb.provider, cb.payout_tx_id, cb.report_document_id, cb.sale_on_report) == (
        "out", "linked", "SIBS", payout, report_id, date(2026, 9, 17))
    # closed on what proves it: its own bank line, the payout's and the payout report that lists the sale
    step = closing(svc, taken)
    assert {rec.evidence_id, repo.transactions[payout].evidence_id, *repo.documents[report_id].evidence_ids} <= set(
        step.evidence_ids)
    assert step.note == rec.match_headline == ("A disputed card payment taken back: the sale of 17 September, paid "
                                               "out on 19 September.")
    assert rec.match_why == ("Taken back: €45.00 on 26 September", "Sale: €45.00 on 17 September",
                             "Paid out in the €1,394.00 payout of 19 September",
                             "Payout report: it lists the sale, of the same amount")
    assert taken not in repo.chases and not rec.document_ids  # nothing to chase: nobody sends an invoice for it
    # neither a cost nor income, and said
    spent = Ledger(svc).money(*SEPT, direction="out")
    assert taken not in {x.id for x in spent.lines} and [x.id for x in spent.disputed] == [taken]
    assert Ledger(svc).money(*SEPT, direction="in").total == sales_before
    answer = svc.ask("How much did we spend in September?")["answer"]
    assert "I left out the €45.00 SIBS took back on 26 September for a disputed card payment: it is not a cost." in \
        answer
    matched = {m["id"]: m for m in svc.month(next(iter(repo.companies)), "2026-09")["matched"]}
    assert matched[f"m_{taken}"]["description"] == "Disputed card payment taken back"
    detail = svc.transaction(taken)
    assert detail["disputed"] == {"status": "linked", "text": rec.match_headline}
    plain(answer, *rec.match_why, rec.match_headline, *(a.text for a in repo.activity))
    golden_rule(svc.orchestrator)


def test_a_chargeback_won_back_is_linked_to_the_money_taken_back_and_is_not_new_income() -> None:
    svc = business()
    repo = svc.repo
    payout, _ = sibs_payout(svc)
    (taken,) = bank(svc, (date(2026, 9, 26), "-45.00", "SIBS", "CHARGEBACK TPA 1234567 COMPRA 17/09/2026"))
    income_before = Ledger(svc).money(*SEPT, direction="in").total
    (won,) = bank(svc, (date(2026, 9, 30), "45.00", "SIBS", "ESTORNO CHARGEBACK TPA 1234567"))
    rec = repo.transactions[won]
    assert rec.decision.rule == "chargeback_won"  # not a payout of new sales, not a customer's payment
    assert repo.chargebacks[won].reverses == taken and repo.chargebacks[taken].won_back_by == won
    step = closing(svc, won)
    assert {rec.evidence_id, repo.transactions[taken].evidence_id} <= set(step.evidence_ids)
    assert step.note == "A disputed card payment won back: the €45.00 taken back on 26 September."
    assert "Won back: €45.00 on 30 September" in repo.transactions[taken].match_why  # both are linked
    money = Ledger(svc).money(*SEPT, direction="in")
    assert money.total == income_before and [x.id for x in money.disputed] == [won]
    answer = svc.ask("How much came in in September?")["answer"]
    assert ("I left out the €45.00 that came back on 30 September for a disputed card payment you won: it is not new "
            "income.") in answer
    plain(answer, step.note)
    golden_rule(svc.orchestrator)


def test_a_chargeback_whose_sale_cannot_be_found_is_one_plain_question() -> None:
    svc = business()
    repo = svc.repo
    payout, _ = sibs_payout(svc, with_report=False)  # the report never came: the sale cannot be found
    (taken,) = bank(svc, (date(2026, 9, 26), "-45.00", "SIBS", "DISPUTA VENDA TPA 1234567"))
    rec = repo.transactions[taken]
    assert repo.chargebacks[taken].status == "open" and stage(svc.orchestrator, rec) is Stage.NEEDS_OWNER
    (item,) = [i for i in svc.needs_you()["items"] if i["id"].endswith("_disputed")]
    assert item["question"] == ("SIBS took €45.00 back on 26 September for a disputed card payment. Which payout "
                                "was that sale in?")
    assert [o["label"] for o in item["options"]] == ["The €1,394.00 payout of 19 September",
                                                     "A card sale, but not in these payouts",
                                                     "No, it is something else"]
    assert "I can't find that sale in your payout reports, so I won't link it on a guess." in item["why"]
    assert svc.orchestrator.missing.plan(rec) == ("SIBS took €45.00 back on 26 September for a disputed card "
                                                  "payment. I asked you which sale it takes back.")
    plain(item["question"], *item["why"], *(o["label"] for o in item["options"]))
    golden_rule(svc.orchestrator)  # every screen reads with the question open
    out = svc.answer(item["id"], f"payout:{payout}")
    assert out["message"] == ("Done. I linked the €45.00 taken back to the payout of 19 September. It is neither "
                              "income nor a cost.")
    step = closing(svc, taken)
    assert rec.evidence_id in step.evidence_ids and repo.transactions[payout].evidence_id in step.evidence_ids
    assert repo.chargebacks[taken].answer_ev in step.evidence_ids  # the owner's answer is part of the proof

    # something else after all: an ordinary payment again, looking for its invoice
    (other,) = bank(svc, (date(2026, 9, 27), "-30.00", "SIBS", "CONTESTACAO REF 99812"))
    (item,) = [i for i in svc.needs_you()["items"] if "€30.00" in i["question"]]
    assert item["question"] == ("SIBS took €30.00 back on 27 September for a disputed card payment. Which payout was "
                                "that sale in?")
    svc.answer(item["id"], "other")
    other_rec = repo.transactions[other]
    assert repo.chargebacks[other].status == "not_chargeback" and other_rec.decision.requires_document
    assert svc.orchestrator.missing.plan(other_rec).startswith("I'm looking for the document for the €30.00 payment")
    golden_rule(svc.orchestrator)


def test_money_taken_back_then_won_back_links_both_even_when_the_sale_was_never_found() -> None:
    svc = business()
    repo = svc.repo
    sibs_payout(svc, with_report=False)
    (taken,) = bank(svc, (date(2026, 9, 26), "-20.00", "SIBS", "DISPUTA VENDA 555"))
    (asked,) = [n for n in repo.open_needs() if n.subject_id == taken]  # the sale is not found: asked
    (won,) = bank(svc, (date(2026, 9, 29), "20.00", "SIBS", "DISPUTA GANHA VENDA 555"))
    # the money came back: both are linked, both close on each other's bank line, the question is settled
    assert repo.chargebacks[won].reverses == taken and repo.chargebacks[taken].status == "won_back"
    assert asked.status == "resolved" and not [n for n in repo.open_needs() if n.subject_id == taken]
    back, out = closing(svc, won), closing(svc, taken)
    assert {repo.transactions[taken].evidence_id, repo.transactions[won].evidence_id} <= set(back.evidence_ids)
    assert {repo.transactions[taken].evidence_id, repo.transactions[won].evidence_id} <= set(out.evidence_ids)
    assert out.note == "A disputed card payment taken back, then won back on 29 September."
    assert Ledger(svc).money(*SEPT, direction="in").total == 0 and Ledger(svc).money(*SEPT).total == 0
    golden_rule(svc.orchestrator)


def _line(amount: str, who: str, description: str, *, kind: TransactionKind = K.TRANSFER_OUT,
          card_last4: str | None = None) -> Transaction:
    return Transaction(tenant_id="t", account_id="a", booked_on=date(2026, 9, 26), amount=D(amount), counterparty=who,
                       description=description, kind=kind, card_last4=card_last4)


def test_chargeback_wording_is_read_but_fees_and_purchase_refunds_are_not_chargebacks() -> None:
    words = chargeback_words(_line("-45.00", "SIBS", "CHARGEBACK TPA 1234567 COMPRA 17/09/2026"))
    assert words is not None and not words.won and words.sale_on == date(2026, 9, 17)
    assert words.provider is not None and words.provider.key == "sibs"
    for description in ("DISPUTA VENDA 004512", "CONTESTACAO CARTAO REF 99812", "ESTORNO VENDA TPA 1234567",
                        "REVERSAL POS SETTLEMENT 7781", "Chargeback dp_3Q4"):
        assert chargeback_words(_line("-20.00", "REDUNIQ", description)) is not None, description
    won = chargeback_words(_line("45.00", "SIBS", "DISPUTA GANHA TPA 1234567", kind=K.TRANSFER_IN))
    assert won is not None and won.won
    # the bank's fee for a dispute is a cost; a refund of a purchase is money back from a supplier
    assert chargeback_words(_line("-15.00", "MILLENNIUM BCP", "COMISSAO CHARGEBACK")) is None
    assert chargeback_words(_line("25.00", "ZARA", "ESTORNO COMPRA ZARA", kind=K.CARD, card_last4="5530")) is None
    assert chargeback_words(_line("25.00", "AMAZON", "DISPUTA COMPRA AMAZON", kind=K.CARD, card_last4="5530")) is None
    assert chargeback_words(_line("-80.00", "J SILVA", "ESTORNO TRF 2026-09-01")) is None  # no card sale in sight
    # a card refund keeps its own evidence need: the supplier's credit note
    refund = _line("25.00", "ZARA", "ESTORNO COMPRA ZARA", kind=K.CARD, card_last4="5530")
    assert ExpectedEvidenceEngine().classify(refund).rule == "refund"
    assert find_jargon("A chargeback of €45.00") == ["chargeback"]  # the owner reads "a disputed card payment"


# =========================================================================== 3. seasonal and irregular rhythms


def occ(day: date, amount: str) -> Occurrence:
    return Occurrence(on=day, amount=D(amount))


def test_seasonal_quarterly_and_yearly_rhythms_are_learned_with_their_usual_amounts() -> None:
    # a pool service billing from May to September, two summers running
    summers = [occ(date(2025, m, 12), "120.00") for m in range(5, 10)] + \
        [occ(date(2026, m, 12), "130.00") for m in range(5, 10)]
    pool = learn_series("piscinas", [*summers, occ(date(2026, 1, 20), "45.00")], basis=Basis.INVOICES,
                        name="Piscinas Azul")  # plus a one-off winter repair: an extra, not the rhythm
    assert pool is not None and pool.cadence is Cadence.MONTHLY and pool.season == (5, 6, 7, 8, 9)
    assert pool.trusted and pool.off_cycle == 1 and pool.rhythm == "every month from May to September"
    assert pool.typical_amount == D("130.00")  # the in-season price, after it went up
    assert pool.price_change is not None
    assert pool.price_change.message == "Piscinas Azul went from €120.00 to €130.00."
    assert next_expected(pool).expected == date(2027, 5, 12)
    for quiet in (date(2026, 10, 20), date(2026, 12, 31), date(2027, 3, 1), date(2027, 5, 12)):
        assert check_overdue(pool, quiet) is None, quiet  # never out of season, never before the usual day
    notice = check_overdue(pool, date(2027, 5, 20))
    assert notice is not None and notice.missed_periods == 1 and not notice.likely_ended
    assert notice.message == ("Piscinas Azul normally issues an invoice by the 12th, every month from May to "
                              "September. Today is the 20th. Invoice missing.")
    # three season months without it: it may have stopped (the winter months never count)
    assert check_overdue(pool, date(2027, 8, 20)).likely_ended  # type: ignore[union-attr]

    # a season across the new year (ski equipment hire, December to March)
    winters = [occ(date(y, m, 5), "80.00") for y, m in ((2024, 12), (2025, 1), (2025, 2), (2025, 3), (2025, 12),
                                                         (2026, 1), (2026, 2), (2026, 3))]
    ski = learn_series("ski", winters, basis=Basis.PAYMENTS, name="Neve Aluguer")
    assert ski is not None and ski.season == (12, 1, 2, 3) and ski.typical_amount == D("80.00")
    assert check_overdue(ski, date(2026, 7, 1)) is None and check_overdue(ski, date(2026, 11, 30)) is None
    late = check_overdue(ski, date(2026, 12, 20))
    assert late is not None and late.message == ("Neve Aluguer is normally paid by the 5th, every month from December "
                                                 "to March. Today is the 20th. Payment missing.")

    # quarterly and yearly, with their usual amounts
    quarters = [occ(date(2025, 10, 15), "450.00"), occ(date(2026, 1, 15), "450.00"), occ(date(2026, 4, 14), "450.00"),
                occ(date(2026, 7, 15), "450.00")]
    insurer = learn_series("fidelidade", quarters, basis=Basis.INVOICES, name="Fidelidade")
    assert insurer is not None and insurer.cadence is Cadence.QUARTERLY and insurer.typical_amount == D("450.00")
    assert insurer.season == () and insurer.rhythm == "every 3 months"
    assert check_overdue(insurer, date(2026, 9, 30)) is None
    quarter = check_overdue(insurer, date(2026, 11, 3))
    assert quarter is not None and "every 3 months" in quarter.message and quarter.message.endswith("Invoice missing.")
    years = [occ(date(2024, 3, 10), "12.00"), occ(date(2025, 3, 12), "12.00"), occ(date(2026, 3, 11), "14.00")]
    domain = learn_series("dominio", years, basis=Basis.PAYMENTS, name="Domínio PT")
    assert domain is not None and domain.cadence is Cadence.ANNUAL and domain.typical_amount == D("14.00")
    assert domain.price_change is not None
    assert domain.price_change.message == "Domínio PT went from €12.00 to €14.00."
    assert check_overdue(domain, date(2026, 12, 1)) is None and check_overdue(domain, date(2027, 4, 20)) is not None

    # an occasional skipped month is not a season
    skip = [occ(date(2026, m, 3), "50.00") for m in (1, 2, 4, 5, 6, 7, 8)]
    monthly = learn_series("norte", skip, basis=Basis.INVOICES)
    assert monthly is not None and monthly.season == () and monthly.missed_in_history == 1


def seasonal_invoice(o: Orchestrator, day: date, n: int):  # type: ignore[no-untyped-def]
    data = qr_document("FT", f"FT P/{n}", f"PSCN1234-{n}", "61.50", "50.00", "11.50", day.isoformat(), title="Fatura",
                       extra=("Manutenção da piscina",))
    report = o.ingest_file(data, filename=f"piscina{n}.txt", content_type="text/plain")
    return o.repo.documents[report.document_ids[0]]


def read_mail(o: Orchestrator, until: datetime) -> None:
    gmail = o.repo.connectors["gmail"]
    gmail.covered_until = gmail.last_synced_at = until


def test_a_seasonal_supplier_is_never_missing_out_of_season_and_is_missing_in_season() -> None:
    o = build_demo()
    repo = o.repo
    repo.add_supplier(Supplier(id="sup-norte", tenant_id=repo.tenant_id, name="Papelaria Norte",
                               aliases=["PAPELARIA NORTE"], tax_id=NORTE_NIF, countries=["PT"]))
    # two summers of pool maintenance invoices, read newest first as a mailbox import delivers them
    days = [date(year, month, 12) for year in (2026, 2025) for month in range(9, 4, -1)]
    for n, day in enumerate(days, start=1):
        seasonal_invoice(o, day, n)

    def ours() -> list:  # type: ignore[type-arg]
        return [e for e in repo.expected_invoices.values() if e.series_key == "sup-norte"]

    for day in (date(2026, 10, 20), date(2027, 1, 15), date(2027, 4, 30)):
        read_mail(o, local_datetime(day, 8, 0))
        o.run(local_datetime(day, 9, 0))
        assert not ours(), day  # quiet from October to April: never missing out of season
    read_mail(o, local_datetime(date(2027, 5, 20), 8, 0))
    o.run(local_datetime(date(2027, 5, 20), 9, 0))
    (expected,) = ours()
    assert expected.series.season == (5, 6, 7, 8, 9) and expected.series.typical_amount == D("61.50")
    assert expected.expected_on == date(2027, 5, 12) and expected.company_id == "hazel-tree"
    assert expected.notice == ("Papelaria Norte normally issues an invoice by the 12th, every month from May to "
                               "September. Today is the 20th. Invoice missing.")
    plain(expected.notice)
    # the May invoice arrives: closed with it as the evidence
    arrived = seasonal_invoice(o, date(2027, 5, 21), 99)
    assert expected.status == "received" and expected.document_id == arrived.id


# =========================================================================== 4. security deposits


JOAO_IBAN = "PT50003500009876543210987"


def pay_c(o: Orchestrator, bank_id: str, day: date, amount: str, who: str, description: str,
          iban: str | None = JOAO_IBAN):  # type: ignore[no-untyped-def]
    kind = K.TRANSFER_IN if D(amount) > 0 else K.TRANSFER_OUT
    o.ingest_bank([BankRow(bank_id=bank_id, account_id="mbcp-cc", booked_on=day, amount=D(amount), counterparty=who,
                           description=description, kind=kind, counterparty_iban=iban)])
    return next(r for r in o.repo.transactions.values() if r.tx.description == description)


def income(o: Orchestrator):  # type: ignore[no-untyped-def]
    return Ledger(BackOfficeService(o)).money(*SEPT, direction="in", company_ids=["company-c"])


def test_a_security_deposit_is_held_for_the_customer_never_revenue_and_its_return_closes_it() -> None:
    o = build_demo()
    repo = o.repo
    svc = BackOfficeService(o)
    before = income(o).total
    deposit = pay_c(o, "cc-0901", date(2026, 9, 1), "300.00", "JOANA MARTINS", "TRF CAUCAO VIATURA AA-12-BB")
    held = repo.deposits[deposit.id]
    assert held.security and held.status == "held" and held.why == "The bank line says it is a security deposit."
    assert stage(o, deposit) is Stage.UNDERSTOOD and not deposit.document_ids  # open, never closed as a sale
    assert not [n for n in repo.open_needs() if n.subject_id == deposit.id]  # nothing to ask: it waits to go back
    plan = ("Joana Martins paid a €300.00 security deposit on 1 September. I hold it for them, not as income: it "
            "closes when it goes back to them.")
    assert o.missing.plan(deposit) == plan
    assert plan in [r["text"] for r in svc.month("company-c", "2026-09")["remaining"]]
    detail = svc.transaction(deposit.id)
    assert detail["expects"] == ("A security deposit held for Joana Martins. It closes when it goes back to them: no "
                                 "invoice needed.")  # never "your own invoice covers it"
    assert detail["deposit"] == {
        "status": "held", "text": "Security deposit of €300.00 from Joana Martins on 1 September, held for them. It "
                                  "is theirs until it goes back or you keep part of it."}
    money = income(o)
    assert money.total == before and [x.amount for x in money.held_for] == [D("300.00")]
    answer = svc.ask("How much did Company C receive in September?")["answer"]
    assert "I left out the €300.00 security deposit from Joana Martins: it is held for them, not income." in answer
    assert repo.activity[-2].text == ("Recorded a €300.00 security deposit from Joana Martins. I hold it for them: "
                                      "it is not income.")
    # the customer's money: never chased for an invoice, never offered to one as a deposit for the work
    assert deposit.id not in repo.chases and not deposit.likely_document_ids

    back = pay_c(o, "cc-0915", date(2026, 9, 15), "-300.00", "JOANA MARTINS", "DEVOLUCAO CAUCAO AA-12-BB")
    assert back.decision.rule == "deposit_refund" and back.deposit_refund_of == deposit.id
    assert held.status == "refunded" and stage(o, back) is Stage.CLOSED and stage(o, deposit) is Stage.CLOSED
    assert repo.items[deposit.item_id].history[-1].note == "Security deposit given back in full on 15 September."
    assert repo.items[back.item_id].history[-1].note == ("Refund of the €300.00 security deposit Joana Martins paid "
                                                         "on 1 September.")
    spent = svc.ask("How much did Company C spend in September?")["answer"]
    assert "I left out the €300.00 paid back to Joana Martins: it gave back their deposit." in spent
    assert income(o).total == before
    matched = {m["id"]: m for m in svc.month("company-c", "2026-09")["matched"]}
    assert matched[f"m_{back.id}"]["description"] == "Security deposit given back"
    plain(answer, spent, o.missing.plan(deposit), *back.match_why)
    golden_rule(o)


def test_part_of_a_security_deposit_kept_is_income_only_with_the_owners_confirmation() -> None:
    o = build_demo()
    repo = o.repo
    svc = BackOfficeService(o)
    before = income(o).total
    deposit = pay_c(o, "cc-0901", date(2026, 9, 1), "300.00", "JOANA MARTINS", "TRF CAUCAO VIATURA AA-12-BB")
    back = pay_c(o, "cc-0915", date(2026, 9, 15), "-250.00", "JOANA MARTINS", "DEVOLUCAO CAUCAO AA-12-BB")
    held = repo.deposits[deposit.id]
    assert back.deposit_refund_of == deposit.id and held.returned == D("250.00") and held.available == D("50.00")
    assert stage(o, back) is Stage.CLOSED and stage(o, deposit) is Stage.NEEDS_OWNER
    (item,) = [i for i in svc.needs_you()["items"] if i["id"].endswith("_deposit_kept")]
    assert item["question"] == ("You gave back €250.00 of the €300.00 security deposit Joana Martins paid on 1 "
                                "September. Did you keep the other €50.00?")
    assert [o_["label"] for o_ in item["options"]] == ["Yes, I kept €50.00 for damage or another charge",
                                                       "No, it will go back to them later"]
    plain(item["question"], *item["why"], *(o_["label"] for o_ in item["options"]))
    assert income(o).total == before  # nothing kept is income before the owner says so
    golden_rule(o)  # every screen reads with the question open
    out = svc.answer(item["id"], "kept")
    assert out["message"] == "Done. I counted the €50.00 you kept as income."
    assert held.status == "kept" and held.kept == D("50.00") and held.available == 0
    step = repo.items[deposit.item_id].history[-1]
    assert stage(o, deposit) is Stage.CLOSED and held.kept_answer in step.evidence_ids
    assert back.evidence_id in step.evidence_ids
    assert step.note == ("Security deposit settled: €250.00 given back on 15 September, €50.00 kept, as you "
                         "confirmed.")
    money = income(o)
    assert money.total - before == D("50.00") and money.held_for == []
    answer = svc.ask("How much did Company C receive in September?")["answer"]
    assert "That includes €50.00 kept from a security deposit." in answer
    assert svc.transaction(deposit.id)["deposit"]["text"] == (
        "Security deposit of €300.00 from Joana Martins on 1 September. €250.00 went back to them; €50.00 was kept, "
        "as you confirmed.")
    plain(answer, out["message"])
    golden_rule(o)


def test_part_of_a_security_deposit_kept_is_income_with_your_invoice_for_it() -> None:
    o = build_demo()
    repo = o.repo
    deposit = pay_c(o, "cc-0901", date(2026, 9, 1), "300.00", "JOANA MARTINS", "TRF CAUCAO VIATURA AA-12-BB")
    pay_c(o, "cc-0915", date(2026, 9, 15), "-250.00", "JOANA MARTINS", "DEVOLUCAO CAUCAO AA-12-BB")
    report = o.ingest_file(qr_document(
        "FT", "FT CC2026/77", "CCQ7K2MP-77", "50.00", "40.65", "9.35", "2026-09-16", title="Fatura", issuer=C_NIF,
        issuer_name="Company C Studio, Unipessoal Lda.", buyer=MARIA_NIF, buyer_line="Cliente: Joana Martins",
        extra=("Reparação de risco na porta - viatura AA-12-BB",)), filename="FT_CC2026_77.txt",
        content_type="text/plain")
    invoice = repo.documents[report.document_ids[0]]
    held = repo.deposits[deposit.id]
    assert held.status == "applied" and held.applied_to == invoice.id and invoice.part_paid == {deposit.id: D("50.00")}
    assert stage(o, deposit) is Stage.CLOSED and stage(o, invoice) is Stage.CLOSED
    assert deposit.match_headline == "The €50.00 kept from the security deposit pays invoice FT CC2026/77."
    assert not [n for n in repo.open_needs() if n.subject_id == deposit.id]  # the invoice is the evidence: no question
    money = income(o)
    kept = [x for x in money.lines if x.description == "Kept from a security deposit"]
    assert [x.amount for x in kept] == [D("50.00")] and money.held_for == []
    assert BackOfficeService(o).transaction(deposit.id)["deposit"]["text"] == (
        "Security deposit of €300.00 from Joana Martins on 1 September. €250.00 went back to them; €50.00 was kept "
        "for invoice FT CC2026/77.")
    golden_rule(o)


# =========================================================================== 5. tourist tax and grant letters


HAZEL = "Hazel Tree Interiores, Lda., NIF 516 123 459"
TOURIST_TAX_LETTER = (f"Câmara Municipal de Lisboa\nTaxa Municipal Turística - setembro de 2026\n{HAZEL}\n"
                      "Dormidas declaradas: 171\nValor a pagar: 342,00 €\nReferência de pagamento: 123 456 789\n"
                      "Pagamento até 15/10/2026.\n")
TOURIST_DECLARATION = (f"Câmara Municipal do Porto\n{HAZEL}: deve submeter a declaração mensal da taxa turística "
                       "de outubro até 15/11/2026, no portal do município.\n")
GRANT_DOCUMENTS = ("IFAP - Instituto de Financiamento da Agricultura e Pescas\n"
                   f"Beneficiário: {HAZEL}\nCandidatura PEPAC n.º 2026/000123.\nDeve submeter os documentos em falta "
                   "(comprovativo de situação regularizada perante a Segurança Social) até 30/10/2026.\n")
GRANT_RECEIVED = ("IFAP\nConfirmamos a receção dos documentos da candidatura PEPAC n.º 2026/000123. "
                  "Hazel Tree Interiores, Lda.\n")
GRANT_APPROVED = (f"IFAP\n{HAZEL}\nInformamos que foi aprovado o pagamento de 4.500,00 € relativo à candidatura "
                  "PEPAC n.º 2026/000123, a efetuar por transferência até 31/10/2026.\n")


def letter(o: Orchestrator, text: str, day: date, name: str):  # type: ignore[no-untyped-def]
    return o.ingest_file(text.encode("utf-8"), filename=name, content_type="text/plain",
                         at=local_datetime(day, 10, 0))


def only(o: Orchestrator, kind: ObligationKind):  # type: ignore[no-untyped-def]
    (found,) = [ob for ob in o.repo.obligations.values() if ob.obligation.kind is kind]
    return found


def hazel_row(bank_id: str, day: date, amount: str, who: str, description: str, reference: str | None = None,
              ) -> BankRow:
    kind = K.TRANSFER_IN if D(amount) > 0 else K.TRANSFER_OUT
    return BankRow(bank_id=bank_id, account_id="mbcp-ht", booked_on=day, amount=D(amount), counterparty=who,
                   description=description, kind=kind, reference=reference)


def test_tourist_tax_payment_and_declaration_are_obligations_done_only_by_their_proof() -> None:
    o = build_demo()
    repo = o.repo
    svc = BackOfficeService(o)
    report = letter(o, TOURIST_TAX_LETTER, date(2026, 10, 5), "taxa_turistica.txt")
    ob = only(o, ObligationKind.TOURIST_TAX)
    ob_ = ob.obligation
    assert report.obligation_ids == [ob_.id] and not report.document_ids  # a letter, not an invoice
    assert (ob.title, ob_.entity_id, ob_.due_on, ob_.amount, ob_.responsible) == (
        "Tourist tax payment", "hazel-tree", date(2026, 10, 15), D("342.00"), "owner")
    assert ob.issuer == "municipality" and ob.reference == "123456789" and "From the municipality" in ob.reasons
    assert ob_.required_evidence == "Proof of a payment of €342.00 with reference 123456789 by 15 October 2026."
    assert ob_.consequence == "Paying late may lead to a fine and interest."
    assert repo.activity[-1].text == "Read a letter about the tourist tax to pay."
    (due,) = [d for d in svc.home()["dueSoon"] if d["title"] == "Tourist tax payment"]
    assert due["note"] == "€342.00, reference 123456789. I will check the payment when it goes out."
    # a tax payment of the same amount to the tax office does not pay the municipality
    o.ingest_bank([hazel_row("ht-1006", date(2026, 10, 6), "-342.00", "AUTORIDADE TRIBUTARIA", "PAG ESTADO IVA",
                             "123456789")], at=local_datetime(date(2026, 10, 6), 18, 0))
    assert not ob.done
    o.ingest_bank([hazel_row("ht-1009", date(2026, 10, 9), "-342.00", "MUNICIPIO DE LISBOA",
                             "PAGAMENTO TAXA TURISTICA SET 2026", "123456789")],
                  at=local_datetime(date(2026, 10, 9), 18, 0))
    paid = next(r for r in repo.transactions.values() if r.tx.description == "PAGAMENTO TAXA TURISTICA SET 2026")
    assert paid.decision.rule == "tourist_tax"
    assert ob.done and ob.satisfied_by == (paid.evidence_id,) and ob.how == "Paid on 9 October."
    step = repo.items[paid.item_id].history[-1]
    assert stage(o, paid) is Stage.CLOSED and ob.evidence_id in step.evidence_ids
    assert step.note == "The payment matches the tourist tax letter's amount and reference."
    line = next(x for x in Ledger(svc).lines if x.id == paid.id)
    assert (line.kind, line.merchant, line.category) == ("tax", "Tourist tax", "tax")
    matched = {m["id"]: m for m in svc.month("hazel-tree", "2026-10")["matched"]}
    assert (matched[f"m_{paid.id}"]["supplier"], matched[f"m_{paid.id}"]["description"]) == ("Municipality",
                                                                                             "Tourist tax")
    assert not [d for d in svc.home()["dueSoon"] if d["title"] == "Tourist tax payment"]

    # the monthly declaration: submitted, as the municipality confirms (or the owner says)
    letter(o, TOURIST_DECLARATION, date(2026, 11, 2), "declaracao.txt")
    declaration = only(o, ObligationKind.TOURIST_TAX_DECLARATION)
    assert declaration.title == "Tourist tax declaration" and declaration.obligation.due_on == date(2026, 11, 15)
    assert declaration.obligation.required_evidence == "Proof that it was submitted by 15 November 2026."
    assert o.obligations.next_step(declaration) == ("I close it when the municipality confirms the declaration, or "
                                                    "when you tell me it is submitted.")
    listed = {i["id"]: i for i in svc.obligations()["items"]}[declaration.obligation.id]
    assert [c["id"] for c in listed["confirmOptions"]] == ["filed"] and listed["responsible"] == "You"
    letter(o, "Câmara Municipal do Porto\nHazel Tree Interiores, Lda., NIF 516123459: declaração da taxa turística "
              "submetida com sucesso.\n", date(2026, 11, 10), "confirmacao.txt")
    assert declaration.done and declaration.how == "The filing receipt arrived on 10 November."
    plain(due["note"], o.obligations.next_step(declaration), *ob.reasons)
    golden_rule(o)


def test_grant_letters_are_obligations_and_the_grant_received_is_a_grant_not_a_sale() -> None:
    o = build_demo()
    repo = o.repo
    svc = BackOfficeService(o)
    letter(o, GRANT_DOCUMENTS, date(2026, 10, 5), "ifap_documentos.txt")
    docs = only(o, ObligationKind.GRANT_DOCUMENTS)
    assert (docs.title, docs.obligation.due_on, docs.obligation.responsible, docs.issuer, docs.agency) == (
        "Documents for your grant", date(2026, 10, 30), "owner", "grant_agency", "IFAP")
    assert docs.reasons[0] == "From IFAP"  # never read as a Social Security letter, although it names it
    assert docs.obligation.required_evidence == "Proof that the documents were sent by 30 October 2026."
    assert docs.obligation.consequence == "The grant may be delayed or cancelled if the documents are late."
    assert repo.activity[-1].text == "Read a letter about your grant: documents to send."
    assert o.obligations.next_step(docs) == ("When you have sent the documents, forward me the agency's confirmation "
                                             "or tell me they are sent.")
    letter(o, GRANT_RECEIVED, date(2026, 10, 12), "ifap_rececao.txt")
    assert docs.done and docs.how == "They confirmed on 12 October that they received it."

    letter(o, GRANT_APPROVED, date(2026, 10, 14), "ifap_pagamento.txt")
    grant = only(o, ObligationKind.GRANT_PAYMENT)
    assert (grant.title, grant.obligation.amount, grant.obligation.due_on) == (
        "Grant payment to receive", D("4500.00"), date(2026, 10, 31))
    assert grant.obligation.required_evidence == "The grant payment of €4,500.00 arriving in your bank."
    (due,) = [d for d in svc.home()["dueSoon"] if d["title"] == "Grant payment to receive"]
    assert due["note"] == "€4,500.00 · I will check that the money arrives in your bank."
    status, body = svc.dispatch("POST", f"/api/obligations/{grant.obligation.id}/done", {"outcome": "sent"})
    assert status == 409 and not grant.done  # the owner's word never stands for money received

    o.ingest_bank([hazel_row("ht-1020", date(2026, 10, 20), "4500.00", "IFAP IP",
                             "TRF IFAP PAGAMENTO PEPAC 2026/000123")], at=local_datetime(date(2026, 10, 20), 18, 0))
    rec = next(r for r in repo.transactions.values() if r.tx.description == "TRF IFAP PAGAMENTO PEPAC 2026/000123")
    assert rec.decision.rule == "grant" and not rec.document_ids
    assert grant.done and grant.how == "Received on 20 October." and grant.satisfied_by == (rec.evidence_id,)
    step = repo.items[rec.item_id].history[-1]
    assert stage(o, rec) is Stage.CLOSED and grant.evidence_id in step.evidence_ids
    assert step.note == "The grant letter's amount matches the money received."
    assert rec.id not in repo.chases and not [m for m in repo.outbox.values() if m.subject_id == rec.id]
    # a grant, never a sale
    line = next(x for x in Ledger(svc).lines if x.id == rec.id)
    assert (line.kind, line.merchant) == ("grant", "IFAP")
    answer = svc.ask("How much did Hazel Tree receive in October?")["answer"]
    assert answer.startswith("€4,500.00 came in for Hazel Tree in October") and \
        "It is a grant or subsidy (IFAP), not a sale." in answer
    matched = {m["id"]: m for m in svc.month("hazel-tree", "2026-10")["matched"]}
    assert (matched[f"m_{rec.id}"]["supplier"], matched[f"m_{rec.id}"]["description"]) == ("IFAP", "Grant received")
    assert svc.ask("How much did Hazel Tree receive in October?")["answer"] == answer
    plain(answer, due["note"], *docs.reasons, *grant.reasons)
    golden_rule(o)


def test_a_grant_paid_before_its_letter_waits_for_the_letter_and_is_never_a_sale() -> None:
    o = build_demo()
    repo = o.repo
    svc = BackOfficeService(o)
    o.ingest_bank([hazel_row("ht-1008", date(2026, 10, 8), "2000.00", "AGENCIA DESENVOLVIMENTO COESAO",
                             "PORTUGAL 2030 PAGAMENTO APOIO")], at=local_datetime(date(2026, 10, 8), 18, 0))
    rec = next(r for r in repo.transactions.values() if r.tx.description == "PORTUGAL 2030 PAGAMENTO APOIO")
    assert rec.decision.rule == "grant" and stage(o, rec) is not Stage.CLOSED
    assert o.missing.plan(rec).endswith("is a grant or subsidy, not a sale. Forward me the letter about it (the "
                                        "approval or the payment notice) and I will close it.")
    o.run(local_datetime(date(2026, 10, 20), 9, 0))
    assert rec.id not in repo.chases  # nobody sends an invoice for a grant: nothing is chased
    assert next(x for x in Ledger(svc).lines if x.id == rec.id).kind == "grant"
    # the payment notice, written after the money was sent: it proves it
    letter(o, f"Portugal 2030\n{HAZEL}\nInformamos que foi efetuado o pagamento de 2.000,00 € relativo ao apoio "
              "aprovado, por transferência em 07/10/2026.\n", date(2026, 10, 21), "pt2030.txt")
    grant = only(o, ObligationKind.GRANT_PAYMENT)
    assert grant.done and grant.how == "Received on 8 October." and stage(o, rec) is Stage.CLOSED
    golden_rule(o)


def test_grant_and_tourist_tax_words_are_read_and_payroll_allowances_are_never_grants() -> None:
    for text in (GRANT_DOCUMENTS, GRANT_APPROVED, "Subsídio à contratação: candidatura aprovada.",
                 "Your grant application: please send the missing documents.", "Apoio financeiro PRR"):
        assert is_grant_text(text), text
    for text in ("Recibo de vencimento. Subsídio de férias: 800,00 €.", "Contacte o apoio ao cliente: apoio@x.pt",
                 "We grant you access to the portal.", "Fatura n.º FT 1/2 - Apoio técnico",
                 "Crédito ao investimento: o seu financiamento foi aprovado. Prestação até 30/10/2026."):
        assert not is_grant_text(text), text
    assert grant_agency("Transferência IFAP PEPAC") == "IFAP" and grant_agency("Fatura EDP") is None
    assert mentions_tourist_tax("City tax collected for September") and not mentions_tourist_tax("IVA setembro")
    entity = LegalEntity(id="hazel-tree", tenant_id="t", name="Hazel Tree", country="PT", tax_id=E.HAZEL_NIF)
    payslip = detect_obligation("Hazel Tree NIF 516123459. Subsídio de férias: 800,00 €. Pagamento até "
                                "30/10/2026.", tenant_id="t", received_on=date(2026, 10, 5), entities=[entity])
    assert payslip is None or payslip.kind not in (ObligationKind.GRANT_DOCUMENTS, ObligationKind.GRANT_PAYMENT)
    found = detect_obligation(TOURIST_TAX_LETTER, tenant_id="t", received_on=date(2026, 10, 5), entities=[entity])
    assert found is not None and VerificationCondition.parse(found.obligation.verification_condition).amount == \
        D("342.00")
    # a bank line from a payroll allowance or an audit firm is never a grant
    engine = ExpectedEvidenceEngine()
    for who, description in (("SEG SOCIAL", "SUBSIDIO DE FERIAS"), ("GRANT THORNTON", "TRF HONORARIOS")):
        tx = Transaction(tenant_id="t", account_id="a", booked_on=date(2026, 10, 1), amount=D("800.00"),
                         counterparty=who, description=description, kind=K.TRANSFER_IN)
        assert engine.classify(tx).rule != "grant", who


# =========================================================================== the demo


def test_the_demo_is_unchanged_by_disputes_seasons_security_deposits_and_letters() -> None:
    o = build_demo()
    repo = o.repo
    assert repo.chargebacks == {} and repo.fx_differences == {} and repo.fx_rates is None
    assert not any(d.security for d in repo.deposits.values())
    assert sorted(ob.obligation.kind.value for ob in repo.obligations.values()) == ["tax_deadline", "tax_deadline"]
    rules = {r.decision.rule for r in repo.transactions.values() if r.decision is not None}
    assert not rules & {"chargeback", "chargeback_won", "grant", "tourist_tax"}
    actions = {"record_chargeback", "link_chargeback", "exchange_difference", "grant_received",
               "security_deposit_kept"}
    assert not [r for r in repo.audit_store.records(repo.tenant_id) if r.action in actions]
    svc = BackOfficeService(o)
    for question in ("How much came in in September?", "How much did we spend in September?"):
        answer = svc.ask(question)["answer"]
        assert "currenc" not in answer and "disputed" not in answer and "security deposit" not in answer
    assert not [f for c in repo.companies for f in svc.accountant_client(c)["taxFlags"] if f["id"].startswith("t_fx_")]
