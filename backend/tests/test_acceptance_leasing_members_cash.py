"""Leasing contracts, recurring customer payments, petty cash and cash sales (checklist X24, X12, X4, X5).

Cases 7, 9, 33, 35 (leasing), 10, 25, 45 (memberships, tuition, monthly fees), 19 (petty cash) and 2 (a
restaurant's cash and card takings). Every case runs through the live pipeline on a fresh tenant: bank rows,
uploaded or emailed files, the orchestrator's agents, the ledger, the chat, the month and the accountant's view.
The golden rule holds throughout: nothing closes without the evidence that proves it, and anything uncertain is
one plain question or one plain line, never a guess.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from backoffice.closure import Month
from backoffice.demo import evidence as E
from backoffice.domain.lifecycle import Stage
from backoffice.domain.models import (
    DocumentType,
    ExtractionMethod,
    Quality,
    Transaction,
)
from backoffice.domain.models import TransactionKind as K
from backoffice.language import find_jargon, find_off_tone
from backoffice.leases import read_lease
from backoffice.memberships import read_receipts_list, returned_debit
from backoffice.orchestrator import TZ, Account, BankRow
from backoffice.policy import ActionKind
from backoffice.reading import ReadOutcome
from backoffice.reading.stage0 import ReadStep, StepState
from backoffice.reconciliation import EvidenceExpectation
from backoffice.service import BackOfficeService
from backoffice.spending import Ledger
from backoffice.tills import read_till_reports

NOW = datetime(2026, 10, 2, 9, 0, tzinfo=TZ)
SEPT = (date(2026, 9, 1), date(2026, 9, 30))
IBAN = "PT50003300004532881710265"


# --------------------------------------------------------------------------- the world


def business(tenant: str, name: str, nif: str, owner_email: str, *, now: datetime = NOW) -> BackOfficeService:
    svc = BackOfficeService.new_tenant(tenant, owner_name="Rita Sousa", owner_email=owner_email, now=now)
    svc.add_company(name, nif, f"{name}, Lda.")
    company = next(iter(svc.repo.companies))
    svc.repo.add_account(Account(id="bcp", bank="Millennium BCP", holder_id=company, iban=IBAN))
    svc.repo.add_account(Account(id="card-1234", bank="Millennium BCP", holder_id=company, card_last4="1234"))
    return svc


def company_of(svc: BackOfficeService) -> str:
    return next(iter(svc.repo.companies))


def bank(svc: BackOfficeService, *rows: tuple, at: datetime | None = None) -> list[str]:
    """Bank rows (day, amount, counterparty, description[, kind, card, iban]) -> transaction ids."""
    made = []
    for i, (day, amount, counterparty, description, *rest) in enumerate(rows):
        kind = rest[0] if rest else (K.TRANSFER_IN if Decimal(amount) > 0 else K.TRANSFER_OUT)
        card = rest[1] if len(rest) > 1 else None
        iban = rest[2] if len(rest) > 2 else None
        made.append(BankRow(bank_id=f"{counterparty}-{day}-{amount}-{i}-{description}", account_id="bcp",
                            booked_on=day, amount=Decimal(amount), counterparty=counterparty, description=description,
                            kind=kind, card_last4=card, counterparty_iban=iban))
    return svc.orchestrator.ingest_bank(made, at=at).transaction_ids


def upload(svc: BackOfficeService, data: str | bytes, filename: str, content_type: str = "text/plain"):
    raw = data.encode("utf-8") if isinstance(data, str) else data
    return svc.orchestrator.ingest_file(raw, filename=filename, content_type=content_type)


def item(svc: BackOfficeService, subject_id: str):
    repo = svc.repo
    rec = repo.transactions.get(subject_id) or repo.documents[subject_id]
    return repo.items[rec.item_id]


def stage(svc: BackOfficeService, subject_id: str) -> Stage:
    return item(svc, subject_id).stage


def chat(svc: BackOfficeService, text: str) -> str:
    status, body = svc.dispatch("POST", "/api/chat", {"message": text, "history": []})
    assert status == 200, body
    return body["reply"]


def plain(*texts: str) -> None:
    for text in texts:
        assert not find_jargon(text), (text, find_jargon(text))
        assert not find_off_tone(text), (text, find_off_tone(text))


def money_in(svc: BackOfficeService):
    return Ledger(svc).money(*SEPT, direction="in")


def money_out(svc: BackOfficeService):
    return Ledger(svc).money(*SEPT, direction="out")


def _pt(value: str) -> str:
    whole, cents = value.split(".")
    groups = []
    while len(whole) > 3:
        groups.insert(0, whole[-3:])
        whole = whole[:-3]
    return ".".join([whole, *groups]) + "," + cents


def pt_invoice(*, issuer: str, issuer_name: str, buyer: str, buyer_name: str, code: str, number: str, atcud: str,
               day: str, net: str, vat: str, total: str, title: str = "Fatura", extra: tuple[str, ...] = ()) -> str:
    """A Portuguese invoice's text layer and its decoded fiscal QR code: verified (GREEN) when both agree."""
    payload = E.qr_payload(A=issuer, B=buyer, C="PT", D=code, E="N", F=day.replace("-", ""), G=number, H=atcud,
                           I1="PT", I7=net, I8=vat, N=vat, O=total, Q="ab12", R="1234")
    issued = f"{day[8:10]}/{day[5:7]}/{day[0:4]}"
    return E._invoice_text(
        [issuer_name, f"NIF: {issuer}", f"{title} n.º {number}", f"ATCUD: {atcud}", f"Data de emissão: {issued}"],
        [f"Cliente: {buyer_name}", f"NIF: {buyer}", *extra, f"Base tributável (23%): {_pt(net)}",
         f"IVA 23%: {_pt(vat)}", f"Total: {_pt(total)} €"],
        payload,
    ).decode("utf-8")


# =========================================================================== X5: cash sales (case 2, a restaurant)

CASA_NIF = "516123459"

SIBS_REPORT = """Nº Lote;Data Liquidação;Data Movimento;Terminal;Tipo;Montante Bruto;Comissão;Montante Líquido;Moeda
L0918-4471;18/09/2026;17/09/2026;1234567;Compra;45,00;0,36;44,64;EUR
L0918-4471;18/09/2026;17/09/2026;1234567;Compra;120,50;0,96;119,54;EUR
L0918-4471;18/09/2026;17/09/2026;1234567;Compra;9,90;0,08;9,82;EUR
L0918-4471;18/09/2026;17/09/2026;1234567;Compra;1.250,00;10,00;1.240,00;EUR
L0918-4471;18/09/2026;17/09/2026;1234567;Devolução;-20,00;0,00;-20,00;EUR
Total;;;;;1.405,40;11,40;1.394,00;
"""  # card sales on 17 September: 1,425.40 - 20.00 given back = 1,405.40 (1,394.00 paid out after fees)

Z_0918 = f"""Casa Azul, Lda.
NIF: {CASA_NIF}
RELATÓRIO Z N.º 0918
Data: 17/09/2026
Fundo de caixa: 100,00
Nº de talões: 52
Numerário: 225,00
Multibanco: 1.405,40
Total de vendas: 1.630,40
IVA 13%: 187,57
"""

Z_POS_EXPORT = """Data;Z;Numerário;Multibanco;Total
18/09/2026;0919;310,50;980,00;1.290,50
19/09/2026;0920;140,00;1.120,30;1.260,30
Total;;450,50;2.100,30;2.550,80
"""


def restaurant() -> BackOfficeService:
    return business("t-casa-azul", "Casa Azul", CASA_NIF, "rita@casaazul.pt")


def test_till_reports_are_read_as_cash_and_card_takings_per_day() -> None:
    (day,) = read_till_reports(Z_0918)
    assert (day.day, day.number, day.cash, day.card, day.total, day.tax_id) == (
        date(2026, 9, 17), "0918", Decimal("225.00"), Decimal("1405.40"), Decimal("1630.40"), CASA_NIF)
    assert day.adds_up  # the float and the VAT are not takings
    first, second = read_till_reports(Z_POS_EXPORT)  # the export's own total line is not a day
    assert (first.day, first.cash, first.card, first.total) == (date(2026, 9, 18), Decimal("310.50"),
                                                               Decimal("980.00"), Decimal("1290.50"))
    assert (second.number, second.location) == ("0920", "csv:row 3") and second.adds_up
    english = read_till_reports("Z REPORT #0412\nDate: 21/09/2026\nCash: 98.40\nCard: 311.25\nTotal: 409.65\n")
    assert english[0].cash == Decimal("98.40") and english[0].card == Decimal("311.25") and english[0].adds_up
    (closing,) = read_till_reports("FECHO DE CAIXA\nDia 22/09/2026\nDinheiro 80,00\nCartão 150,00\nMB Way 20,00\n"
                                   "Troco 5,00\nTotal 250,00\n")
    assert (closing.cash, closing.card, closing.other, closing.total) == (Decimal("80.00"), Decimal("150.00"),
                                                                          Decimal("20.00"), Decimal("250.00"))
    assert closing.adds_up and closing.split() == "€80.00 in cash, €150.00 by card and €20.00 paid other ways"
    assert read_till_reports(SIBS_REPORT) == []  # a card terminal's payout report is not a till report
    assert read_till_reports("Fatura n.º FT 1/2\nTotal: 12,30 €\n") == []


def test_a_restaurants_card_takings_match_the_card_terminal_and_the_cash_goes_to_the_bank_and_the_till() -> None:
    svc = restaurant()
    repo = svc.repo
    company = company_of(svc)
    # 17 September's till report, then the POS export for the 18th and 19th.
    report = upload(svc, Z_0918, "relatorio_z_0918.txt")
    (z17,) = report.document_ids
    assert report.message == ("Got it. This is your till report for 17 September: €225.00 in cash and €1,405.40 by "
                              "card. I will match the card part with your card terminal's payouts and the cash with "
                              "what you pay into the bank.")
    z18, _z19 = upload(svc, Z_POS_EXPORT, "fecho_caixa_semana38.csv", "text/csv").document_ids
    doc = repo.documents[z17]
    assert doc.book == "till" and doc.sales and doc.document.entity_id == company
    assert doc.document.quality is Quality.GREEN and doc.label == "Till report of 17 September · €1,630.40"
    # Until the card terminal's report proves the card part, the day stays open with one plain line.
    assert stage(svc, z17) is Stage.UNDERSTOOD
    assert doc.hold_reason == ("The till report of 17 September shows €1,405.40 paid by card. I'm waiting for your "
                               "card terminal's payout report for that day to confirm it.")

    # The card terminal pays out the 17th's card sales, and its report arrives: the card part is proven.
    (payout,) = bank(svc, (date(2026, 9, 19), "1394.00", "TPA 1234567", "LIQ TPA LOTE L0918-4471"))
    sibs = upload(svc, SIBS_REPORT, "liquidacoes_0918.csv", "text/csv").document_ids[0]
    assert repo.settlements[sibs].status == "settled"
    till = repo.till_days[z17]
    assert till.card_status == "matched" and till.card_documents == [sibs]
    closing = item(svc, z17).history[-1]
    assert closing.to_stage is Stage.CLOSED and item(svc, z17).quality is Quality.GREEN
    assert {*doc.evidence_ids, *repo.documents[sibs].evidence_ids, repo.transactions[payout].evidence_id} <= set(
        closing.evidence_ids)
    assert closing.note == "Its card takings match your card terminal's payout report; its cash went to the cash box."
    # The 18th and 19th have no card report yet: they wait, never closed on a guess.
    assert stage(svc, z18) is Stage.UNDERSTOOD and repo.till_days[z18].card_status == "waiting"

    # Cash paid into the bank on the 21st: it banks the 17th's cash and part of the 18th's, oldest first.
    (deposit,) = bank(svc, (date(2026, 9, 21), "450.00", "DEPOSITO NUMERARIO", "DEP NUMERARIO BALCAO 0123",
                            K.TRANSFER_IN))
    rec = repo.transactions[deposit]
    assert rec.decision.rule == "cash_deposit" and rec.decision.expectation is EvidenceExpectation.SALES_INVOICE
    assert rec.document_ids == [z17, z18] and stage(svc, deposit) is Stage.CLOSED
    assert rec.match_why == ("Paid into the bank: €450.00 in cash",
                             "Cash sales in the till reports of 17 September and 18 September: €535.50",
                             "Still in the till from those days: €85.50")
    assert repo.till_days[z17].banked == {deposit: Decimal("225.00")}
    assert repo.till_days[z18].banked == {deposit: Decimal("225.00")} and repo.till_days[z18].cash_left == \
        Decimal("85.50")
    assert stage(svc, z18) is Stage.UNDERSTOOD  # a deposit never closes a till report: its card part does
    assert deposit not in repo.chases and not repo.outbox  # nobody to ask: the till reports are the business's own
    assert svc.orchestrator.auditor.recheck() == []

    # Income: the till's cash sales and the card terminal's gross sales, never the deposit a second time.
    ins = money_in(svc)
    cash_sales = [x for x in ins.lines if x.kind == "till_cash"]
    assert sum((x.amount for x in cash_sales), Decimal(0)) == Decimal("675.50")
    assert ins.total == Decimal("675.50") + Decimal("1425.40")
    assert [x.id for x in ins.cash_banked] == [deposit] and deposit not in {x.id for x in ins.lines}
    reply = chat(svc, "how much came in in september?")
    assert "That includes €675.50 in cash sales from your till reports." in reply
    assert "I left out €450.00 of cash paid into the bank: it is the till's cash, counted from your till reports." \
        in reply
    plain(reply, *rec.match_why, doc.hold_reason, report.message)

    # The cash box at the end of September: the till's cash in, the bank deposit out, the rest still in the till.
    month = svc.month(company, "2026-09")
    (box,) = [n for n in month["notices"] if n["id"] == "n_cash_box_2026-09"]
    assert box == {"id": "n_cash_box_2026-09", "tone": "neutral",
                   "text": "At the end of September the cash box should hold €225.50 (€675.50 in cash sales came in; "
                           "€450.00 paid into the bank went out). Count it and tell me the amount, so any difference "
                           "is not lost."}
    view = svc.accountant_client(company)
    assert view["cashBox"]["shouldHold"] == 225.5 and view["cashBox"]["line"] == box["text"]
    rows = {r["date"]: r for r in view["cashBox"]["tillReports"]}
    assert rows["2026-09-17"] == {"id": z17, "date": "2026-09-17", "number": "0918", "cash": 225.0, "card": 1405.4,
                                  "other": 0.0, "total": 1630.4, "addsUp": True, "cardStatus": "matched",
                                  "cashInTheBank": 225.0, "cashInTheTill": 0.0, "status": "closed"}
    assert rows["2026-09-18"]["cashInTheTill"] == 85.5 and rows["2026-09-18"]["cardStatus"] == "waiting"
    plain(box["text"])


def test_cash_paid_in_without_its_till_reports_is_not_sales_and_says_so() -> None:
    svc = restaurant()
    repo = svc.repo
    (deposit,) = bank(svc, (date(2026, 9, 22), "300.00", "DEP. NUMERARIO", "DEPOSITO EM NUMERARIO ATM", K.TRANSFER_IN))
    svc.orchestrator.run(NOW)
    rec = repo.transactions[deposit]
    assert rec.decision.rule == "cash_deposit" and not rec.document_ids and stage(svc, deposit) is Stage.UNDERSTOOD
    plan = svc.orchestrator.missing.plan(rec)
    assert plan == ("The €300.00 of cash paid into the bank on 22 September needs the till reports it comes from. "
                    "Send me the till reports (Z reports) of the days before, and I will match them.")
    assert money_in(svc).total == 0
    assert ("Not counted yet: €300.00 of cash paid into the bank on 22 September. I don't have the till reports it "
            "comes from.") in chat(svc, "how much came in in september?")
    # A till report with less cash than the deposit does not explain it: still open, never a guess.
    upload(svc, "RELATÓRIO Z N.º 0921\nData: 21/09/2026\nNumerário: 180,00\nMultibanco: 0,00\nTotal: 180,00\n",
           "z0921.txt")
    assert not rec.document_ids and stage(svc, deposit) is Stage.UNDERSTOOD
    assert svc.orchestrator.missing.plan(rec) == (
        "The €300.00 of cash paid into the bank on 22 September is more than the €180.00 of till cash from the days "
        "before that is not in the bank yet. Send me the till reports that are missing, or tell me where the rest "
        "came from.")
    month = svc.month(company_of(svc), "2026-09")
    assert any(r["text"] == svc.orchestrator.missing.plan(rec) for r in month["remaining"])
    plain(plan, svc.orchestrator.missing.plan(rec))


def test_a_till_report_that_does_not_add_up_or_disagrees_with_the_card_terminal_is_never_used_on_a_guess() -> None:
    svc = restaurant()
    repo = svc.repo
    bad = upload(svc, "RELATÓRIO Z N.º 0917\nData: 16/09/2026\nNumerário: 200,00\nMultibanco: 500,00\n"
                      "Total: 750,00\n", "z0917.txt")
    (doc_id,) = bad.document_ids
    assert repo.documents[doc_id].document.quality is Quality.RED and stage(svc, doc_id) is Stage.CONFLICT
    (needs,) = [n for n in repo.open_needs() if n.kind == "till"]
    assert needs.prompt == ("The till report of 16 September does not add up: €200.00 in cash and €500.00 by card is "
                            "€700.00, but it says €750.00 in all. What should I do?")
    assert bad.message.endswith(f"I need one answer from you: {needs.prompt}")
    assert money_in(svc).total == 0  # never counted
    shown = next(i for i in svc.needs_you()["items"] if i["id"] == needs.id)
    assert shown["question"] == needs.prompt and shown["options"] == [
        {"id": "aside", "label": "Set it aside. I'll send a corrected one."}]
    plain(needs.prompt, *needs.why)
    outcome = svc.orchestrator.answer(needs.id, "aside")
    assert outcome.message == "Done. I set it aside. When you send the corrected till report, I will read it."
    assert repo.till_days[doc_id].status == "set_aside" and stage(svc, doc_id) is Stage.NOT_REQUIRED
    assert money_in(svc).total == 0

    # The card terminal shows other card sales for the day than the till: said plainly, the day never closes.
    (z17,) = upload(svc, Z_0918.replace("1.405,40", "1.415,40").replace("1.630,40", "1.640,40"), "z.txt").document_ids
    bank(svc, (date(2026, 9, 19), "1394.00", "TPA 1234567", "LIQ TPA LOTE L0918-4471"))
    upload(svc, SIBS_REPORT, "liquidacoes_0918.csv", "text/csv")
    till = repo.till_days[z17]
    assert till.card_status == "differs" and till.card_found == Decimal("1405.40")
    assert stage(svc, z17) is Stage.UNDERSTOOD
    line = repo.documents[z17].hold_reason
    assert line == ("The till report of 17 September says €1,415.40 was paid by card, but your card terminal's payout "
                    "report shows €1,405.40 of card sales that day. I won't close it until you or your accountant "
                    "check which is right.")
    assert any(r["text"] == line for r in svc.month(company_of(svc), "2026-09")["remaining"])
    plain(line)


# =========================================================================== X4: petty cash (case 19)

LIMA_NIF = "515888990"
CAFE_NIF = "508111226"
DROGARIA_NIF = "509123457"


def workshop() -> BackOfficeService:
    return business("t-oficina", "Oficina Lima", LIMA_NIF, "joao@oficinalima.pt")


def cash_receipt(issuer: str, issuer_name: str, number: str, atcud: str, day: str, net: str, vat: str,
                 total: str) -> str:
    return pt_invoice(issuer=issuer, issuer_name=issuer_name, buyer=LIMA_NIF, buyer_name="Oficina Lima, Lda.",
                      code="FS", number=number, atcud=atcud, day=day, net=net, vat=vat, total=total,
                      title="Fatura simplificada", extra=("Pagamento: Numerário",))


def test_cash_taken_out_funds_the_cash_box_its_receipts_draw_from_it_and_a_difference_is_never_absorbed() -> None:
    svc = workshop()
    repo = svc.repo
    company = company_of(svc)
    atm, float_ = bank(
        svc,
        (date(2026, 9, 3), "-200.00", "LEVANTAMENTO ATM", "LEVANTAMENTO ATM 0903 PORTO", K.CARD, "1234"),
        (date(2026, 9, 10), "-100.00", "JOAO LIMA", "TRF FUNDO DE MANEIO SETEMBRO", K.TRANSFER_OUT, None,
         "PT50003500001234567890123"),
    )
    for tx in (atm, float_):
        rec = repo.transactions[tx]
        assert rec.decision.expectation is EvidenceExpectation.NONE_INTERNAL_TRANSFER
        assert rec.decision.quality is Quality.GREEN and not rec.decision.requires_document
        assert stage(svc, tx) is Stage.NOT_REQUIRED  # not an expense, and no invoice is needed
    assert repo.transactions[atm].decision.reason == ("Cash taken out for your cash box. It is not a cost and needs no "
                                                      "invoice: the cash receipts show what it paid for.")
    assert repo.transactions[float_].decision.rule == "cash_box"
    # Cash receipts: paid from the box.
    upload(svc, cash_receipt(CAFE_NIF, "Café Central, Lda.", "FS C1/88", "CAFE1234-88", "2026-09-17", "10.00", "2.30",
                             "12.30"), "cafe.txt")
    upload(svc, cash_receipt(DROGARIA_NIF, "Drogaria Norte, Lda.", "FS D/412", "DROG1234-412", "2026-09-24", "34.07",
                             "7.83", "41.90"), "drogaria.txt")
    svc.orchestrator.run(NOW)
    assert not repo.chases and not repo.outbox

    # Spending: the receipts are the costs; the cash taken out is left out and said.
    out = money_out(svc)
    assert out.total == Decimal("54.20") and all(x.paid_in_cash for x in out.lines)
    assert {x.kind for x in out.left_out} == {"cash_withdrawal"}
    reply = chat(svc, "what did we spend in september?")
    assert "I left out €300.00 taken out in cash for your cash box: the cash receipts are the costs." in reply

    # The box at the end of September: €300.00 in, €54.20 out, €245.80 should be in it.
    period = svc.orchestrator.cash.period(company, Month(2026, 9))
    assert (period.came_in, period.went_out, period.closing) == (
        {"bank": Decimal("300.00")}, {"receipt": Decimal("54.20")}, Decimal("245.80"))
    month = svc.month(company, "2026-09")
    (box,) = [n for n in month["notices"] if n["id"] == "n_cash_box_2026-09"]
    assert box["text"] == ("At the end of September the cash box should hold €245.80 (€300.00 taken from the bank came "
                           "in; €54.20 in cash receipts went out). Count it and tell me the amount, so any difference "
                           "is not lost.")
    # Before the month is over, nothing is said: cash is still moving.
    assert svc.orchestrator.cash.month_line(company, Month(2026, 10)) is None

    # The owner counts €240.00: the €5.80 difference is one plain line, never a cost or money in.
    counted = svc.orchestrator.cash.count(company, Decimal("240.00"), date(2026, 9, 30), at=NOW)
    assert counted.difference == Decimal("-5.80") and counted.unexplained
    (box,) = [n for n in svc.month(company, "2026-09")["notices"] if n["id"] == "n_cash_box_2026-09"]
    assert box == {"id": "n_cash_box_2026-09", "tone": "attention",
                   "text": "Your count of €240.00 on 30 September is €5.80 less than the €245.80 the cash box should "
                           "hold. I have not counted the difference as a cost or as money in: it stays open until you "
                           "or your accountant explain it."}
    assert money_out(svc).total == Decimal("54.20") and money_in(svc).total == 0
    view = svc.accountant_client(company)["cashBox"]
    assert view["counted"]["amount"] == 240.0 and view["difference"] == -5.8 and view["unexplained"]
    assert repo.registry.get(repo.tenant_id, view["counted"]["evidenceId"]) is not None  # the count is evidence
    # October starts from what was counted.
    assert svc.orchestrator.cash.period(company, Month(2026, 10)).opening == Decimal("240.00")
    plain(box["text"], reply)


def test_more_cash_spent_than_taken_out_is_one_plain_line_and_a_surveyors_bill_is_not_a_withdrawal() -> None:
    svc = workshop()
    repo = svc.repo
    company = company_of(svc)
    atm, survey = bank(
        svc, (date(2026, 9, 3), "-20.00", "LEV MB", "LEV MB 1234 PORTO", K.CARD, "1234"),
        (date(2026, 9, 4), "-350.00", "GEOPONTO LDA", "LEVANTAMENTO TOPOGRAFICO OBRA", K.TRANSFER_OUT, None,
         "PT50001000001234567890154"))
    assert repo.transactions[atm].decision.rule == "cash_withdrawal"
    assert repo.transactions[survey].decision.expectation is EvidenceExpectation.INVOICE  # a bill to pay, not cash
    upload(svc, cash_receipt(CAFE_NIF, "Café Central, Lda.", "FS C1/88", "CAFE1234-88", "2026-09-17", "10.00", "2.30",
                             "12.30"), "cafe.txt")
    upload(svc, cash_receipt(DROGARIA_NIF, "Drogaria Norte, Lda.", "FS D/412", "DROG1234-412", "2026-09-24", "34.07",
                             "7.83", "41.90"), "drogaria.txt")
    line = svc.orchestrator.cash.month_line(company, Month(2026, 9))
    assert line == {"id": "n_cash_box_2026-09", "tone": "attention",
                    "text": "At the end of September €34.20 more cash went out of the cash box than came in (€20.00 "
                            "taken from the bank came in; €54.20 in cash receipts went out). Some cash came from "
                            "somewhere I can't see: tell me where, or send me what is missing."}
    plain(line["text"])


# =========================================================================== X24: leasing (cases 7, 9, 33, 35)

LIMA_ENTREGAS_NIF = "516234560"
BCP_LEASING_NIF = "503213560"  # a valid, made-up NIF for the leasing company in these tests

LEASE_CONTRACT = f"""Contrato de Locação Financeira Mobiliária
Contrato n.º 900123
Locador: BCP Leasing, S.A.
NIF: {BCP_LEASING_NIF}
Email: faturacao@bcpleasing.pt
Locatário: Lima Entregas, Lda.
NIF: {LIMA_ENTREGAS_NIF}
Bem locado: Renault Kangoo Van 1.5 dCi, Matrícula: 12-AB-34
Data da primeira renda: 05/03/2026
Prazo: 48 meses
Renda mensal (sem IVA): 284,55 €
IVA sobre a renda (23%): 65,45 €
Renda mensal com IVA: 350,00 €
Valor residual: 1.500,00 €
"""

HIRE_AGREEMENT = """VEHICLE HIRE AGREEMENT
Agreement number: NVH-44821
Owner: Northgate Vehicle Hire Ltd, VAT No GB 123 4567 89
Hirer: Lima Entregas, Lda.
Vehicle: Ford Transit Custom, Registration: AB12 CDE
First payment date: 10/04/2026
Term: 36 months
Monthly rental: £375.00 plus VAT at 20%
"""

NORTHGATE_STATEMENT = """Northgate Vehicle Hire Ltd
VAT No GB 123 4567 89
Statement of account
Hirer: Lima Entregas, Lda.
Agreement number: NVH-44821
Date Description Debit Credit Balance
10/08/2026 Monthly rental August £450.00 £450.00
11/08/2026 Direct debit received £450.00 £0.00
10/09/2026 Monthly rental September £450.00 £450.00
11/09/2026 Direct debit received £450.00 £0.00
Closing balance £0.00
"""


def leasing_invoice(number: str, atcud: str, day: str, rent: int, *, net: str = "284.55", vat: str = "65.45",
                    total: str = "350.00") -> str:
    return pt_invoice(issuer=BCP_LEASING_NIF, issuer_name="BCP Leasing, S.A.", buyer=LIMA_ENTREGAS_NIF,
                      buyer_name="Lima Entregas, Lda.", code="FT", number=number, atcud=atcud, day=day, net=net,
                      vat=vat, total=total, extra=(f"Renda n.º {rent} do contrato 900123",
                                                   "Renault Kangoo Van - Matrícula 12-AB-34"))


def courier(now: datetime = NOW) -> BackOfficeService:
    svc = business("t-lima-entregas", "Lima Entregas", LIMA_ENTREGAS_NIF, "rui@limaentregas.pt", now=now)
    svc.repo.policy = svc.repo.policy.with_grant(ActionKind.SUPPLIER_INVOICE_REQUEST, granted_by="rui@limaentregas.pt",
                                                 at=now)
    return svc


def test_a_leasing_or_renting_contract_is_read_into_its_payment_plan() -> None:
    lease = read_lease(LEASE_CONTRACT)
    assert lease is not None and lease.complete and lease.kind == "leasing" and lease.country == "PT"
    assert (lease.lessor, lease.lessor_tax_id, lease.lessor_email) == ("BCP Leasing, S.A.", BCP_LEASING_NIF,
                                                                       "faturacao@bcpleasing.pt")
    assert (lease.customer_tax_id, lease.number, lease.asset, lease.plate) == (
        LIMA_ENTREGAS_NIF, "900123", "Renault Kangoo Van 1.5 dCi", "12-AB-34")
    assert (lease.start, lease.term, lease.net, lease.vat, lease.gross, lease.residual) == (
        date(2026, 3, 5), 48, Decimal("284.55"), Decimal("65.45"), Decimal("350.00"), Decimal("1500.00"))
    plan = lease.schedule()
    assert len(plan) == 49 and plan[6].due == date(2026, 9, 5) and plan[6].number == 7
    assert plan[-1].residual and plan[-1].amount == Decimal("1500.00") and plan[-2].due == date(2030, 2, 5)
    hire = read_lease(HIRE_AGREEMENT)
    assert hire is not None and hire.kind == "renting" and hire.country == "GB" and hire.contract_suffices
    assert (hire.lessor, hire.lessor_tax_id, hire.plate, hire.start, hire.term) == (
        "Northgate Vehicle Hire Ltd", "GB123456789", "AB12 CDE", date(2026, 4, 10), 36)
    assert (hire.net, hire.vat, hire.gross, hire.currency) == (Decimal("375.00"), Decimal("75.00"), Decimal("450.00"),
                                                               "GBP")
    renting = read_lease("CONTRATO DE ALUGUER DE LONGA DURAÇÃO (ALD)\nLocador: Leaseplan Portugal, Lda.\n"
                         "Início: 01/06/2026\nDuração: 36 meses\nRenda mensal: 420,00 € + IVA\n")
    assert renting is not None and renting.kind == "renting" and renting.net == Decimal("420.00")
    assert renting.vat is None and not renting.complete and renting.missing == ("the monthly payment",)
    # The leasing company's monthly invoice says "leasing" too: it is an invoice, never a contract.
    assert read_lease(leasing_invoice("FT LSG2026/0907", "LSGQ7K2M-0907", "2026-09-01", 7)) is None
    broken = read_lease(LEASE_CONTRACT.replace("Renda mensal com IVA: 350,00 €", "Renda mensal com IVA: 355,00 €"))
    assert broken is not None and broken.problems and not broken.complete


def test_each_leasing_payment_matches_its_line_closes_on_the_monthly_invoice_and_a_missing_one_is_asked_for() -> None:
    svc = courier(datetime(2026, 9, 5, 9, 0, tzinfo=TZ))
    repo = svc.repo
    company = company_of(svc)
    report = upload(svc, LEASE_CONTRACT, "contrato_leasing_900123.txt")
    (contract_id,) = report.document_ids
    contract = repo.documents[contract_id]
    assert contract.document.doc_type is DocumentType.CONTRACT and contract.book == "lease"
    assert contract.label == "Leasing contract 900123" and stage(svc, contract_id) is Stage.NOT_REQUIRED
    assert report.message == ("Got it. This is your leasing contract with BCP Leasing for the Renault Kangoo Van 1.5 "
                              "dCi (12-AB-34): 48 monthly payments of €350.00, the first on 5 March. I will match each "
                              "payment to it and look for BCP Leasing's monthly invoice.")
    supplier = repo.suppliers[contract.supplier_id]
    assert (supplier.name, supplier.tax_id, supplier.contact_email) == ("BCP Leasing", BCP_LEASING_NIF,
                                                                         "faturacao@bcpleasing.pt")

    # September's direct debit is payment 7 of the plan; the monthly invoice closes it.
    (sept,) = bank(svc, (date(2026, 9, 5), "-350.00", "BCP LEASING SA", "DD LEASING CT 900123", K.DIRECT_DEBIT))
    rec = repo.transactions[sept]
    assert repo.lease_payments[sept] == (contract_id, 7)
    assert rec.decision.rule == "lease" and rec.decision.quality is Quality.GREEN
    assert rec.decision.reason == ("Leasing payment 7 of 48 for the Renault Kangoo Van 1.5 dCi (12-AB-34). BCP "
                                   "Leasing's monthly invoice covers it.")
    assert svc.orchestrator.missing.plan(rec) == (
        "The €350.00 payment to BCP Leasing on 5 September is payment 7 of 48 for the Renault Kangoo Van 1.5 dCi "
        "(12-AB-34). I'm waiting for BCP Leasing's invoice for it.")
    invoice = upload(svc, leasing_invoice("FT LSG2026/0907", "LSGQ7K2M-0907", "2026-09-01", 7), "FT_0907.txt")
    (invoice_id,) = invoice.document_ids
    assert repo.documents[invoice_id].document.quality is Quality.GREEN
    assert rec.document_ids == [invoice_id] and stage(svc, sept) is Stage.CLOSED
    assert stage(svc, invoice_id) is Stage.CLOSED
    assert rec.match_why == (("Leasing contract 900123: payment 7 of 48 for the Renault Kangoo Van 1.5 dCi "
                              "(12-AB-34), due 5 September"), "Monthly payment in the contract: €350.00",
                             "Invoice FT LSG2026/0907: €350.00", "Paid: €350.00, the same",
                             "The invoice names contract 900123")
    assert svc.orchestrator.auditor.recheck() == []

    # October's payment: no invoice. Days later it is asked for, like any missing invoice.
    (octo,) = bank(svc, (date(2026, 10, 5), "-350.00", "BCP LEASING SA", "DD LEASING CT 900123", K.DIRECT_DEBIT),
                   at=datetime(2026, 10, 5, 9, 0, tzinfo=TZ))
    assert repo.lease_payments[octo] == (contract_id, 8)
    svc.orchestrator.run(datetime(2026, 10, 9, 9, 0, tzinfo=TZ))
    chase = repo.chases[octo]
    assert chase.message.to == "faturacao@bcpleasing.pt" and "350,00 € de 5 de outubro" in chase.message.body
    assert svc.orchestrator.missing.plan(repo.transactions[octo]) == (
        "I wrote to BCP Leasing asking for the invoice for the €350.00 payment on 5 October. It is waiting to be sent.")
    # The invoice arrives: October closes too, on its own invoice (never September's).
    upload(svc, leasing_invoice("FT LSG2026/1008", "LSGQ7K2M-1008", "2026-10-01", 8), "FT_1008.txt")
    assert stage(svc, octo) is Stage.CLOSED and repo.transactions[octo].document_ids != rec.document_ids

    # The accountant sees the lease: the asset, the monthly payment and its VAT, what is paid and still to come.
    (lease,) = svc.accountant_client(company)["leases"]
    assert (lease["leasingCompany"], lease["asset"], lease["plate"], lease["contract"]) == (
        "BCP Leasing", "Renault Kangoo Van 1.5 dCi", "12-AB-34", "900123")
    assert (lease["monthly"], lease["monthlyBeforeVat"], lease["monthlyVat"], lease["residual"]) == (350.0, 284.55,
                                                                                                    65.45, 1500.0)
    assert (lease["paymentsDue"], lease["paymentsMatched"], lease["paymentsClosed"]) == (8, 2, 2)
    assert (lease["remainingPayments"], lease["remainingAmount"], lease["next"]) == (40, 15500.0, "2026-11-05")
    assert lease["line"] == ("Leasing with BCP Leasing for the Renault Kangoo Van 1.5 dCi (12-AB-34): 40 of 48 monthly "
                             "payments of €350.00 still to come, then €1,500.00 to keep it.")
    # A leasing payment is a cost, counted once (its invoice is not a second one).
    assert Ledger(svc).money(date(2026, 9, 1), date(2026, 10, 31), direction="out").total == Decimal("700.00")
    plain(report.message, rec.decision.reason, *rec.match_why, lease["line"], chase.line)


def test_a_leasing_payment_of_another_amount_is_one_plain_question() -> None:
    svc = courier()
    repo = svc.repo
    upload(svc, LEASE_CONTRACT, "contrato.txt")
    (sept, insurance) = bank(svc, (date(2026, 9, 5), "-362.00", "BCP LEASING SA", "DD LEASING CT 900123",
                                   K.DIRECT_DEBIT),
                             (date(2026, 9, 20), "-95.00", "BCP LEASING SA", "SEGURO VIATURA", K.DIRECT_DEBIT))
    (needs,) = [n for n in repo.open_needs() if n.kind == "lease"]
    assert needs.subject_id == sept and stage(svc, sept) is Stage.NEEDS_OWNER
    assert needs.prompt == ("The €362.00 payment to BCP Leasing on 5 September is not the €350.00 monthly payment in "
                            "your leasing contract for the Renault Kangoo Van 1.5 dCi (12-AB-34). What is it?")
    assert needs.why == (("Leasing contract 900123: payment 7 of 48 for the Renault Kangoo Van 1.5 dCi (12-AB-34), "
                          "due 5 September."), "Monthly payment in the contract: €350.00.", "Paid: €362.00.",
                         "Until you answer, I won't close this payment.")
    # Not on the plan's dates: an ordinary payment to the leasing company, never asked about.
    assert insurance not in repo.lease_payments and repo.transactions[insurance].decision.rule == "known_supplier"
    # An invoice of the new amount does not close it while the question is open.
    invoice = upload(svc, leasing_invoice("FT LSG2026/0907", "LSGQ7K2M-0907", "2026-09-01", 7, net="294.31",
                                          vat="67.69", total="362.00"), "FT_0907.txt").document_ids[0]
    assert stage(svc, sept) is Stage.NEEDS_OWNER and not repo.transactions[sept].document_ids
    shown = next(i for i in svc.needs_you()["items"] if i["id"] == needs.id)
    assert [o["label"] for o in shown["options"]] == ["This month's payment. The amount changed.",
                                                     "Something else, not the monthly payment."]
    plain(needs.prompt, *needs.why, *(o.label for o in needs.options))
    outcome = svc.orchestrator.answer(needs.id, "changed")
    assert outcome.message == ("Done. It is payment 7 of 48 for the Renault Kangoo Van 1.5 dCi (12-AB-34), at "
                               "€362.00. It closes when BCP Leasing's invoice arrives.")
    rec = repo.transactions[sept]
    assert rec.document_ids == [invoice] and stage(svc, sept) is Stage.CLOSED
    answer_ev = next(e for e in rec.extra_evidence_ids)
    assert answer_ev in item(svc, sept).history[-1].evidence_ids  # the owner's answer is part of its evidence
    assert "Monthly payment in the contract: €362.00" in rec.match_why

    # "Something else": it stays an ordinary payment that needs its own invoice.
    (octo,) = bank(svc, (date(2026, 10, 5), "-10.00", "BCP LEASING SA", "DD LEASING CT 900123 DESPESAS",
                         K.DIRECT_DEBIT), at=datetime(2026, 10, 5, 9, 0, tzinfo=TZ))
    (other,) = [n for n in repo.open_needs() if n.kind == "lease"]
    assert svc.orchestrator.answer(other.id, "other").message == ("Done. It stays an ordinary payment to BCP Leasing: "
                                                                  "I will look for its invoice.")
    assert octo not in repo.lease_payments and stage(svc, octo) is Stage.UNDERSTOOD
    assert not [n for n in repo.open_needs() if n.kind == "lease"]


class PdfTextLayer:
    """Stands in for the PDF reader: the contract's text layer."""

    external_ai = False

    def __init__(self, text: str) -> None:
        self.text = text

    def engines(self) -> tuple[str, ...]:
        return ()

    def read(self, request: object) -> ReadOutcome:
        return ReadOutcome(text=self.text, text_method=ExtractionMethod.EMBEDDED_TEXT, page_count=2,
                           steps=(ReadStep("pdf_text", StepState.DONE, "2 pages with text"),))


def test_a_leasing_contract_arriving_as_a_pdf_is_read_from_its_text_layer() -> None:
    svc = courier()
    repo = svc.repo
    repo.reader = PdfTextLayer(LEASE_CONTRACT)
    report = upload(svc, b"%PDF-1.4\n% leasing contract\n", "contrato_900123.pdf", "application/pdf")
    (contract_id,) = report.document_ids
    assert repo.leases[contract_id].contract.number == "900123" and repo.documents[contract_id].book == "lease"
    assert stage(svc, contract_id) is Stage.NOT_REQUIRED
    assert not [d for d in repo.documents.values() if d.document.doc_type is not DocumentType.CONTRACT]
    (sept,) = bank(svc, (date(2026, 9, 5), "-350.00", "BCP LEASING SA", "DD LEASING CT 900123", K.DIRECT_DEBIT))
    assert repo.lease_payments[sept] == (contract_id, 7)


def test_a_vehicle_lease_carries_its_vehicle_cost_center() -> None:
    svc = courier()
    repo = svc.repo
    company = company_of(svc)
    status, body = svc.dispatch("POST", f"/api/companies/{company}/cost-centers",
                                {"name": "Van 12-AB-34", "kind": "Vehicle", "identifiers": {"plates": ["12-AB-34"]}})
    assert status == 200, body
    van = body["costCenter"]["id"]
    upload(svc, LEASE_CONTRACT, "contrato.txt")
    # The bank line names no vehicle; the contract the payment belongs to does.
    (sept,) = bank(svc, (date(2026, 9, 5), "-350.00", "BCP LEASING SA", "DD 900123", K.DIRECT_DEBIT))
    allocation = repo.transactions[sept].tx.cost_allocation
    assert allocation is not None and allocation.cost_center_ids == (van,)
    assert allocation.why == ("The leasing contract shows the plate 12-AB-34, which is Vehicle Van 12-AB-34.",)
    plain(*allocation.why)


def test_where_the_contract_stands_as_the_tax_document_the_leasing_companys_statement_closes_the_payment() -> None:
    svc = courier()
    repo = svc.repo
    company = company_of(svc)
    repo.add_account(Account(id="gbp", bank="Wise", holder_id=company, iban="GB33BUKB20201555555555"))
    report = upload(svc, HIRE_AGREEMENT, "hire_agreement.txt")
    (agreement_id,) = report.document_ids
    assert repo.documents[agreement_id].label == "Renting contract NVH-44821"
    assert report.message == ("Got it. This is your renting contract with Northgate Vehicle Hire for the Ford Transit "
                              "Custom (AB12 CDE): 36 monthly payments of £450.00, the first on 10 April. I will match "
                              "each payment to it.")
    row = BankRow(bank_id="gbp-0911", account_id="gbp", booked_on=date(2026, 9, 11), amount=Decimal("-450.00"),
                  counterparty="NORTHGATE VEHICLE HIRE", description="DD NVH-44821", kind=K.DIRECT_DEBIT,
                  currency="GBP")
    (sept,) = svc.orchestrator.ingest_bank([row]).transaction_ids
    rec = repo.transactions[sept]
    assert repo.lease_payments[sept] == (agreement_id, 6)
    assert rec.decision.reason == ("Renting payment 6 of 36 for the Ford Transit Custom (AB12 CDE). The renting "
                                   "contract and Northgate Vehicle Hire's statement cover it.")
    assert svc.orchestrator.missing.plan(rec) == (
        "The £450.00 payment to Northgate Vehicle Hire on 11 September is payment 6 of 36 for the Ford Transit Custom "
        "(AB12 CDE). I need Northgate Vehicle Hire's statement showing it; with it, the renting contract covers it.")
    statement = upload(svc, NORTHGATE_STATEMENT, "northgate_statement.txt").document_ids[0]
    assert statement in repo.statements
    assert stage(svc, sept) is Stage.CLOSED
    closing = item(svc, sept).history[-1]
    assert {*repo.documents[agreement_id].evidence_ids, *repo.documents[statement].evidence_ids} <= set(
        closing.evidence_ids)
    assert closing.note == ("Payment 6 of 36 for the Ford Transit Custom (AB12 CDE): the renting contract sets it out "
                            "with its VAT, and Northgate Vehicle Hire's statement shows it paid on 11 September.")
    assert "Northgate Vehicle Hire's statement: £450.00 paid on 11 September" in rec.match_why
    assert svc.orchestrator.auditor.recheck() == []
    matched = svc.month(company, "2026-09")["matched"]
    assert any(m["description"] == "Leasing payment" and m["amount"] == 450.0 for m in matched)
    plain(closing.note, *rec.match_why, report.message)


# =========================================================================== X12: memberships, tuition, fees (10, 25, 45)

RITMO_NIF = "512777888"

RECEIPTS_SEPTEMBER = """Recibo;Data;Sócio;Pago por;NIF;Referente a;Valor;Forma de pagamento
R 2026/0901;05/09/2026;Ana Lopes;;;Setembro 2026;45,00;Débito direto
R 2026/0902;05/09/2026;Rui Costa;;;Setembro 2026;45,00;Débito direto
R 2026/0903;08/09/2026;Tiago Lopes;Maria Lopes;;Setembro 2026;60,00;Débito direto
R 2026/0904;10/09/2026;Beatriz Nunes;;;Setembro 2026;45,00;Numerário
"""


def gym() -> BackOfficeService:
    return business("t-ritmo", "Ginásio Ritmo", RITMO_NIF, "sara@ginasioritmo.pt")


def monthly_fees(svc: BackOfficeService, *members: tuple[str, str, int]) -> dict[tuple[str, int], str]:
    """Each member's direct debit in July, August and September -> {(payer, month): payment id}."""
    rows, keys = [], []
    for payer, amount, day in members:
        for month in (7, 8, 9):
            rows.append((date(2026, month, day), amount, payer.upper(), "COBRANCA SEPA MENSALIDADE", K.TRANSFER_IN))
            keys.append((payer, month))
    return dict(zip(keys, bank(svc, *rows), strict=True))


def test_receipts_lists_periods_and_returned_debits_are_read() -> None:
    rows = read_receipts_list(RECEIPTS_SEPTEMBER)
    assert [(r.number, r.member, r.payer, r.period, r.period_label, r.amount, r.cash) for r in rows] == [
        ("R 2026/0901", "Ana Lopes", None, "2026-09", "September 2026", Decimal("45.00"), False),
        ("R 2026/0902", "Rui Costa", None, "2026-09", "September 2026", Decimal("45.00"), False),
        ("R 2026/0903", "Tiago Lopes", "Maria Lopes", "2026-09", "September 2026", Decimal("60.00"), False),
        ("R 2026/0904", "Beatriz Nunes", None, "2026-09", "September 2026", Decimal("45.00"), True)]
    (term,) = read_receipts_list("Receipt no,Date,Student,Period,Amount\nT1-044,14/09/2026,Rui Costa,1st term,600.00\n")
    assert (term.period, term.period_label) == ("term:1:2026", "the 1st term")
    assert read_receipts_list(Z_POS_EXPORT) == [] and read_receipts_list(SIBS_REPORT) == []
    as_tx = Transaction(tenant_id="t", account_id="bcp", booked_on=date(2026, 9, 12), amount=Decimal("-45.00"),
                        counterparty="ANA LOPES", description="DEVOLUCAO COBRANCA SEPA MD0001", kind=K.TRANSFER_OUT)
    assert returned_debit(as_tx)
    assert returned_debit(as_tx.model_copy(update={"description": "RETURNED DIRECT DEBIT"}))
    assert not returned_debit(as_tx.model_copy(update={"description": "TRF ANA LOPES"}))


def test_monthly_fees_are_learned_matched_to_their_receipts_by_payer_and_period_and_a_missing_receipt_is_listed() -> None:
    svc = gym()
    repo = svc.repo
    company = company_of(svc)
    paid = monthly_fees(svc, ("Ana Lopes", "45.00", 5), ("Rui Costa", "45.00", 5), ("Maria Lopes", "60.00", 8))
    # Learned: three customers who pay the same amount every month.
    series = {s.name: s for s in svc.orchestrator.members.series(company)}
    assert {n: (s.amount, s.rhythm, s.trusted) for n, s in series.items()} == {
        "Ana Lopes": (Decimal("45.00"), "every month", True), "Rui Costa": (Decimal("45.00"), "every month", True),
        "Maria Lopes": (Decimal("60.00"), "every month", True)}
    ana_sept = repo.transactions[paid[("Ana Lopes", 9)]]
    assert svc.orchestrator.missing.plan(ana_sept) == (
        "Ana Lopes paid €45.00 on 5 September for September 2026 (Ana Lopes usually pays €45.00 every month). Your "
        "receipt for it is missing: issue it in your software and send me the receipts list. I won't ask Ana Lopes for "
        "it: the receipt is yours to issue.")
    assert not repo.chases and not repo.outbox  # a customer is never asked for the business's own receipt

    report = upload(svc, RECEIPTS_SEPTEMBER, "recibos_setembro.csv", "text/csv")
    assert report.message == "Got it. This is a list of 4 receipts (€195.00 in all). I will match each one with its payment."
    receipts = {r.row.member: r for r in repo.member_receipts.values()}
    # Each payment with its own person's receipt for its period: Ana's never goes to Rui's receipt of the same amount.
    for (payer, member) in (("Ana Lopes", "Ana Lopes"), ("Rui Costa", "Rui Costa"), ("Maria Lopes", "Tiago Lopes")):
        rec = repo.transactions[paid[(payer, 9)]]
        receipt = receipts[member]
        assert rec.document_ids == [receipt.document_id] and stage(svc, rec.id) is Stage.CLOSED, payer
        assert stage(svc, receipt.document_id) is Stage.CLOSED and receipt.status == "paid"
        assert repo.documents[receipt.document_id].document.quality is Quality.GREEN
    tiago = repo.transactions[paid[("Maria Lopes", 9)]]
    assert tiago.match_why == ("Receipt R 2026/0903: Tiago Lopes, September 2026, €60.00",
                               "Paid by: Maria Lopes (for Tiago Lopes)", "Amount: €60.00, the same",
                               "Usually: pays €60.00 every month")
    # Paid in cash at the desk: the receipt is the record; the cash goes to the cash box.
    beatriz = receipts["Beatriz Nunes"]
    assert beatriz.status == "paid" and stage(svc, beatriz.document_id) is Stage.CLOSED
    assert svc.orchestrator.cash.period(company, Month(2026, 9)).came_in == {"till": Decimal("45.00")}
    # July's and August's payments still miss their receipts: listed for the owner and the accountant.
    ana_aug = repo.transactions[paid[("Ana Lopes", 8)]]
    assert not ana_aug.document_ids and stage(svc, ana_aug.id) is Stage.UNDERSTOOD
    august = svc.month(company, "2026-08")
    assert svc.orchestrator.missing.plan(ana_aug) in [r["text"] for r in august["remaining"]]
    assert svc.orchestrator.missing.plan(ana_aug).startswith("Ana Lopes paid €45.00 on 5 August for August 2026")
    missing = chat(svc, "what is missing?")
    assert "6 payments in are waiting for your own receipts or till reports:" in missing
    assert svc.orchestrator.missing.plan(repo.transactions[paid[("Ana Lopes", 7)]]) in missing
    assert not repo.chases and not repo.outbox
    assert svc.orchestrator.auditor.recheck() == []

    # Money in for September: every fee once, the cash at the desk too.
    ins = money_in(svc)
    assert ins.total == Decimal("195.00")
    reply = chat(svc, "how much came in in september?")
    assert "That includes €195.00 in memberships and fees from 4 people who pay you every month." in reply
    view = svc.accountant_client(company)["memberships"]
    assert {p["name"]: p["rhythm"] for p in view["recurringPayers"]} == {"Ana Lopes": "every month",
                                                                         "Maria Lopes": "every month",
                                                                         "Rui Costa": "every month"}
    assert [r["status"] for r in view["receipts"]] == ["paid", "paid", "paid", "paid"]
    plain(reply, report.message, *tiago.match_why, svc.orchestrator.missing.plan(ana_aug))


def test_a_returned_direct_debit_reopens_its_period_until_the_member_pays_again() -> None:
    svc = gym()
    repo = svc.repo
    company = company_of(svc)
    paid = monthly_fees(svc, ("Ana Lopes", "45.00", 5))
    upload(svc, RECEIPTS_SEPTEMBER.splitlines()[0] + "\n" + RECEIPTS_SEPTEMBER.splitlines()[1] + "\n", "recibos.csv",
           "text/csv")
    (receipt,) = repo.member_receipts.values()
    first = paid[("Ana Lopes", 9)]
    assert stage(svc, first) is Stage.CLOSED and stage(svc, receipt.document_id) is Stage.CLOSED

    # Ana's bank returns the direct debit.
    (back,) = bank(svc, (date(2026, 9, 12), "-45.00", "ANA LOPES", "DEVOLUCAO COBRANCA SEPA MENSALIDADE",
                         K.TRANSFER_OUT))
    rec = repo.transactions[back]
    assert repo.payment_returns[back] == first and repo.returned_payments[first] == back
    assert rec.decision.rule == "returned_debit" and rec.decision.expectation is not \
        EvidenceExpectation.REFUND_OR_CREDIT_NOTE  # not a refund needing a credit note
    assert stage(svc, back) is Stage.NOT_REQUIRED
    assert {rec.evidence_id, repo.transactions[first].evidence_id} <= set(item(svc, back).history[-1].evidence_ids)
    # The period is open again: its receipt waits for Ana's next payment, with one plain line.
    assert receipt.status == "returned" and stage(svc, receipt.document_id) is Stage.NEEDS_OWNER
    line = ("Ana Lopes's €45.00 payment for September 2026 came back on 12 September. September 2026 is unpaid again: I "
            "will match Ana Lopes's next payment to receipt R 2026/0901.")
    september = svc.month(company, "2026-09")
    assert september["status"] == "open" and any(r["text"] == line for r in september["remaining"])
    assert any(a.text == "Ana Lopes's €45.00 payment for September 2026 came back on 12 September. September 2026 is "
               "unpaid again." and set(a.evidence_ids) == {rec.evidence_id, repo.transactions[first].evidence_id}
               for a in repo.activity)
    # Neither income nor a cost.
    ins, out = money_in(svc), money_out(svc)
    assert ins.total == 0 and out.total == 0
    assert {x.kind for x in ins.returned} == {"payment_returned"} and {x.kind for x in out.returned} == {
        "debit_returned"}
    reply = chat(svc, "how much came in in september?")
    assert "I left out €45.00 from Ana Lopes on 5 September: the payment came back on 12 September." in reply
    assert not repo.chases and not repo.outbox

    # Ana pays again by transfer: the period closes again, with the whole story as evidence.
    (again,) = bank(svc, (date(2026, 9, 20), "45.00", "ANA LOPES", "TRF MENSALIDADE SETEMBRO", K.TRANSFER_IN))
    paid_again = repo.transactions[again]
    assert paid_again.document_ids == [receipt.document_id] and stage(svc, again) is Stage.CLOSED
    assert receipt.status == "paid" and receipt.payments == [first, again]
    assert stage(svc, receipt.document_id) is Stage.CLOSED
    closing = item(svc, again).history[-1]
    assert {repo.transactions[first].evidence_id, rec.evidence_id} <= set(closing.evidence_ids)
    assert "Paid again after the direct debit of 5 September came back" in paid_again.match_why
    assert money_in(svc).total == Decimal("45.00")  # September's fee, once
    assert not any(r["text"] == line for r in svc.month(company, "2026-09").get("remaining", []))
    assert svc.orchestrator.auditor.recheck() == []
    plain(line, reply, *paid_again.match_why)


def test_tuition_paid_per_term_is_learned_and_matched_to_its_terms_receipt() -> None:
    svc = business("t-escola", "Escola Aurora", "508999111", "ines@escolaaurora.pt")
    repo = svc.repo
    company = company_of(svc)
    jan, apr, sept = bank(svc, (date(2026, 1, 12), "600.00", "RUI COSTA", "PROPINA 2 PERIODO", K.TRANSFER_IN),
                          (date(2026, 4, 13), "600.00", "RUI COSTA", "PROPINA 3 PERIODO", K.TRANSFER_IN),
                          (date(2026, 9, 14), "600.00", "RUI COSTA", "PROPINA 1 PERIODO", K.TRANSFER_IN))
    (series,) = svc.orchestrator.members.series(company)
    assert (series.name, series.amount, series.rhythm) == ("Rui Costa", Decimal("600.00"), "every term")
    upload(svc, "Receipt no,Date,Student,Period,Amount\nT1-044,14/09/2026,Rui Costa,1st term,600.00\n",
           "receipts_term1.csv", "text/csv")
    (receipt,) = repo.member_receipts.values()
    assert repo.transactions[sept].document_ids == [receipt.document_id] and stage(svc, sept) is Stage.CLOSED
    assert "Receipt T1-044: Rui Costa, the 1st term, €600.00" in repo.transactions[sept].match_why
    # The earlier terms' receipts are missing: said plainly, with the rhythm.
    assert svc.orchestrator.missing.plan(repo.transactions[apr]).startswith(
        "Rui Costa paid €600.00 on 13 April for April 2026 (Rui Costa usually pays €600.00 every term).")
    assert money_in(svc).total == Decimal("600.00")
    assert not repo.chases and jan not in repo.chases


def test_a_fee_paid_late_matches_the_receipt_of_its_own_period_and_is_said_for_that_period() -> None:
    svc = gym()
    repo = svc.repo
    # June and July on time; August's fee paid late, on 2 September, naming its month; September's on time.
    june, july, late, sept = bank(
        svc, (date(2026, 6, 5), "45.00", "RUI COSTA", "COBRANCA SEPA MENSALIDADE", K.TRANSFER_IN),
        (date(2026, 7, 5), "45.00", "RUI COSTA", "COBRANCA SEPA MENSALIDADE", K.TRANSFER_IN),
        (date(2026, 9, 2), "45.00", "RUI COSTA", "TRF MENSALIDADE AGOSTO", K.TRANSFER_IN),
        (date(2026, 9, 5), "45.00", "RUI COSTA", "COBRANCA SEPA MENSALIDADE", K.TRANSFER_IN))
    upload(svc, "Recibo;Data;Sócio;Referente a;Valor\nR 2026/0850;02/09/2026;Rui Costa;Agosto 2026;45,00\n"
                "R 2026/0902;05/09/2026;Rui Costa;Setembro 2026;45,00\n", "recibos.csv", "text/csv")
    receipts = {r.row.number: r for r in repo.member_receipts.values()}
    assert repo.transactions[late].document_ids == [receipts["R 2026/0850"].document_id]
    assert repo.transactions[sept].document_ids == [receipts["R 2026/0902"].document_id]
    # June's and July's payments have no receipts of their own: never given one of another period.
    assert not repo.transactions[june].document_ids and not repo.transactions[july].document_ids
    reply = chat(svc, "how much came in in september?")
    assert "That includes €90.00 in memberships and fees from one person" in reply
    assert "Of that, €45.00 pays for August 2026." in reply  # counted when it came in, said for its own month
    plain(reply)


def test_a_receipts_list_or_till_report_naming_none_of_two_companies_is_one_plain_question() -> None:
    svc = gym()
    repo = svc.repo
    gym_id = company_of(svc)
    svc.add_company("Ritmo Kids", "508999111", "Ritmo Kids, Lda.")
    paid = monthly_fees(svc, ("Ana Lopes", "45.00", 5))
    upload(svc, RECEIPTS_SEPTEMBER.splitlines()[0] + "\n" + RECEIPTS_SEPTEMBER.splitlines()[1] + "\n", "recibos.csv",
           "text/csv")
    (receipt,) = repo.member_receipts.values()
    (needs,) = [n for n in repo.open_needs() if n.kind == "member"]
    assert needs.prompt == "Which of your companies issued this list of receipts?"
    assert [o.id for o in needs.options] == [f"company:{gym_id}", "company:ritmo-kids"]
    assert receipt.company_id is None and not repo.transactions[paid[("Ana Lopes", 9)]].document_ids
    assert stage(svc, receipt.document_id) is Stage.NEEDS_OWNER
    plain(needs.prompt, *needs.why)
    assert svc.orchestrator.answer(needs.id, f"company:{gym_id}").message == "Done. The receipt is Ginásio Ritmo's."
    assert repo.transactions[paid[("Ana Lopes", 9)]].document_ids == [receipt.document_id]
    assert stage(svc, receipt.document_id) is Stage.CLOSED

    (z,) = upload(svc, "RELATÓRIO Z N.º 0101\nData: 17/09/2026\nNumerário: 60,00\nMultibanco: 0,00\nTotal: 60,00\n",
                  "z.txt").document_ids
    (till_q,) = [n for n in repo.open_needs() if n.kind == "till"]
    assert till_q.prompt == "Which of your companies is the till report of 17 September for?"
    assert stage(svc, z) is Stage.NEEDS_OWNER and money_in(svc).total == Decimal("45.00")  # not counted yet
    assert svc.orchestrator.answer(till_q.id, "company:ritmo-kids").message == "Done. I counted it for Ritmo Kids."
    assert repo.till_days[z].company_id == "ritmo-kids" and stage(svc, z) is Stage.CLOSED  # all cash: nothing to prove
    assert money_in(svc).total == Decimal("105.00")
