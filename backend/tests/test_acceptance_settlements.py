"""Payout settlements end to end: card terminals and payment / sales platforms (§3, §19-22, §36, §39, §54).

A payout into the bank from SIBS, Stripe, Booking.com, Glovo, Uber Eats, PayPal, ...
is the provider's net settlement of many sales, never customer revenue by itself.
Its evidence is the provider's payout report: the report must add up to the cent,
its net must equal the bank payout, and only then does the payout close, with gross
sales counted as money in, the fees as a cost and the refunds reported. Anything
that does not add up is one plain question; a payout without its report stays open
and asks for it.

Every case runs through the live pipeline (bank rows, uploaded or emailed report
files, the orchestrator's agents, the ledger and the chat) on a fresh tenant.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal

import pytest

from backoffice.closure import ActivityKind, Month
from backoffice.demo import evidence as E
from backoffice.domain.lifecycle import Stage
from backoffice.domain.models import DocumentType, ExtractionMethod, Quality, TransactionKind as K
from backoffice.language import find_jargon, find_off_tone
from backoffice.orchestrator import TZ, Account, BankRow
from backoffice.reconciliation import (
    EvidenceExpectation,
    MatchKind,
    is_card_repayment,
    is_card_settlement,
    payout_provider,
)
from backoffice.service import BackOfficeService
from backoffice.settlements import parse_settlement_reports
from backoffice.spending import Ledger

NOW = datetime(2026, 10, 2, 9, 0, tzinfo=TZ)
SEPT = (date(2026, 9, 1), date(2026, 9, 30))
CASA_NIF = "516123459"
GLOVO_NIF = "514666633"  # a valid, made-up NIF for Glovo's Portuguese company in these tests
IBAN = "PT50003300004532881710265"


# --------------------------------------------------------------------------- the world


def business() -> BackOfficeService:
    """Casa Azul: one company, one bank account, nothing else yet."""
    svc = BackOfficeService.new_tenant("t-payouts", owner_name="Rita Sousa", owner_email="rita@casaazul.pt", now=NOW)
    svc.add_company("Casa Azul", CASA_NIF, "Casa Azul, Lda.")
    company = next(iter(svc.repo.companies))
    svc.repo.add_account(Account(id="bcp", bank="Millennium BCP", holder_id=company, iban=IBAN))
    svc.repo.add_account(Account(id="card-1234", bank="Millennium BCP", holder_id=company, card_last4="1234"))
    return svc


def bank(svc: BackOfficeService, *rows: tuple) -> list[str]:
    """Bank rows (day, amount, counterparty, description[, kind, reference, account]) -> transaction ids."""
    made = []
    for i, (day, amount, counterparty, description, *rest) in enumerate(rows):
        kind = rest[0] if rest else (K.TRANSFER_IN if Decimal(amount) > 0 else K.TRANSFER_OUT)
        reference = rest[1] if len(rest) > 1 else None
        account = rest[2] if len(rest) > 2 else "bcp"
        made.append(BankRow(bank_id=f"{counterparty}-{day}-{amount}-{i}", account_id=account, booked_on=day,
                            amount=Decimal(amount), counterparty=counterparty, description=description, kind=kind,
                            reference=reference))
    return svc.orchestrator.ingest_bank(made).transaction_ids


def upload(svc: BackOfficeService, data: str | bytes, filename: str, content_type: str = "text/csv"):
    raw = data.encode("utf-8") if isinstance(data, str) else data
    return svc.orchestrator.ingest_file(raw, filename=filename, content_type=content_type)


def stage(svc: BackOfficeService, subject_id: str) -> Stage:
    repo = svc.repo
    rec = repo.transactions.get(subject_id) or repo.documents[subject_id]
    return repo.items[rec.item_id].stage


def settlement_for(svc: BackOfficeService, tx_id: str):
    """The payout report that settled this payout (else the one it is about)."""
    mine = [s for s in svc.repo.settlements.values() if s.transaction_id == tx_id]
    return next((s for s in mine if s.settled), mine[0])


def money_in(svc: BackOfficeService):
    return Ledger(svc).money(*SEPT, direction="in")


def money_out(svc: BackOfficeService):
    return Ledger(svc).money(*SEPT, direction="out")


def chat(svc: BackOfficeService, text: str) -> str:
    status, body = svc.dispatch("POST", "/api/chat", {"message": text, "history": []})
    assert status == 200, body
    return body["reply"]


def tool(svc: BackOfficeService, name: str, **args):
    status, body = svc.dispatch("POST", "/api/chat/tool", {"name": name, "input": args})
    assert status == 200, body
    return body["result"]


def assert_plain(*texts: str) -> None:
    for text in texts:
        assert not find_jargon(text), (text, find_jargon(text))
        assert not find_off_tone(text), (text, find_off_tone(text))


def assert_closed_with(svc: BackOfficeService, tx_id: str, report_evidence: list[str]) -> None:
    """Closed GREEN, and the closing step carries the payout report as evidence (§3)."""
    repo = svc.repo
    item = repo.items[repo.transactions[tx_id].item_id]
    assert item.stage is Stage.CLOSED and item.quality is Quality.GREEN
    closing = item.history[-1]
    assert closing.to_stage is Stage.CLOSED
    assert set(report_evidence) <= set(closing.evidence_ids)
    assert repo.transactions[tx_id].evidence_id in closing.evidence_ids


# --------------------------------------------------------------------------- report files

SIBS_REPORT = """Nº Lote;Data Liquidação;Data Movimento;Terminal;Tipo;Montante Bruto;Comissão;Montante Líquido;Moeda
L0918-4471;18/09/2026;17/09/2026;1234567;Compra;45,00;0,36;44,64;EUR
L0918-4471;18/09/2026;17/09/2026;1234567;Compra;120,50;0,96;119,54;EUR
L0918-4471;18/09/2026;17/09/2026;1234567;Compra;9,90;0,08;9,82;EUR
L0918-4471;18/09/2026;17/09/2026;1234567;Compra;1.250,00;10,00;1.240,00;EUR
L0918-4471;18/09/2026;17/09/2026;1234567;Devolução;-20,00;0,00;-20,00;EUR
Total;;;;;1.405,40;11,40;1.394,00;
"""  # gross 1,425.40 - fees 11.40 - refund 20.00 = 1,394.00

STRIPE_REPORT = """balance_transaction_id,created_utc,available_on_utc,currency,gross,fee,net,reporting_category,source_id,description,automatic_payout_id,automatic_payout_effective_at
txn_3Q1,2026-09-14 10:02:11,2026-09-16 00:00:00,eur,600.00,8.70,591.30,charge,ch_3Q1,Order 1041,po_1QxYz8Ab2Cd3Ef4G,2026-09-18 00:00:00
txn_3Q2,2026-09-14 15:40:02,2026-09-16 00:00:00,eur,400.00,5.80,394.20,charge,ch_3Q2,Order 1042,po_1QxYz8Ab2Cd3Ef4G,2026-09-18 00:00:00
txn_3Q3,2026-09-15 09:12:45,2026-09-17 00:00:00,eur,-20.00,0.00,-20.00,refund,re_3Q3,Refund order 1041,po_1QxYz8Ab2Cd3Ef4G,2026-09-18 00:00:00
txn_3Q4,2026-09-15 11:00:00,2026-09-17 00:00:00,eur,-50.00,15.00,-65.00,dispute,dp_3Q4,Dispute order 1039,po_1QxYz8Ab2Cd3Ef4G,2026-09-18 00:00:00
"""  # sales 1,000.00 - fees 29.50 - refunds 20.00 - disputed 50.00 = 900.50

BOOKING_REPORT = """Type,Reservation number,Check-in,Checkout,Guest name,Reservation status,Currency,Payment status,Amount,Commission,Net,Payout date,Payout ID
Reservation,4012345678,2026-09-01,2026-09-03,Ana Lopes,OK,EUR,Paid online,200.00,-30.00,170.00,2026-09-10,BKG-PAYOUT-88112233
Reservation,4012345679,2026-09-04,2026-09-06,Jan de Vries,OK,EUR,Paid online,160.00,-24.00,136.00,2026-09-10,BKG-PAYOUT-88112233
"""  # 360.00 - commission 54.00 = 306.00

BOOKING_INVOICE_UBL = """<?xml version="1.0" encoding="UTF-8"?>
<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
         xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
         xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">
  <cbc:ID>1283554019</cbc:ID>
  <cbc:IssueDate>2026-10-01</cbc:IssueDate>
  <cbc:InvoiceTypeCode>380</cbc:InvoiceTypeCode>
  <cbc:DocumentCurrencyCode>EUR</cbc:DocumentCurrencyCode>
  <cac:AccountingSupplierParty><cac:Party>
    <cac:PartyName><cbc:Name>Booking.com B.V.</cbc:Name></cac:PartyName>
    <cac:PartyTaxScheme><cbc:CompanyID>NL805734958B01</cbc:CompanyID><cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme></cac:PartyTaxScheme>
  </cac:Party></cac:AccountingSupplierParty>
  <cac:AccountingCustomerParty><cac:Party>
    <cac:PartyTaxScheme><cbc:CompanyID>PT516123459</cbc:CompanyID><cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme></cac:PartyTaxScheme>
  </cac:Party></cac:AccountingCustomerParty>
  <cac:TaxTotal><cbc:TaxAmount currencyID="EUR">0.00</cbc:TaxAmount>
    <cac:TaxSubtotal><cbc:TaxableAmount currencyID="EUR">54.00</cbc:TaxableAmount><cbc:TaxAmount currencyID="EUR">0.00</cbc:TaxAmount></cac:TaxSubtotal>
  </cac:TaxTotal>
  <cac:LegalMonetaryTotal>
    <cbc:LineExtensionAmount currencyID="EUR">54.00</cbc:LineExtensionAmount>
    <cbc:TaxExclusiveAmount currencyID="EUR">54.00</cbc:TaxExclusiveAmount>
    <cbc:TaxInclusiveAmount currencyID="EUR">54.00</cbc:TaxInclusiveAmount>
    <cbc:PayableAmount currencyID="EUR">54.00</cbc:PayableAmount>
  </cac:LegalMonetaryTotal>
</Invoice>
"""

# Glovo, week 38: 8 orders. Commission 30% (VAT included), one promotion paid by the restaurant, one refund.
GLOVO_ORDERS = [("G38-1001", "50.00", "15.00", "0.00", "0.00"), ("G38-1002", "45.00", "13.50", "0.00", "0.00"),
                ("G38-1003", "60.00", "18.00", "5.00", "0.00"), ("G38-1004", "35.00", "10.50", "0.00", "0.00"),
                ("G38-1005", "70.00", "21.00", "0.00", "10.00"), ("G38-1006", "40.00", "12.00", "0.00", "0.00"),
                ("G38-1007", "55.00", "16.50", "0.00", "0.00"), ("G38-1008", "55.00", "16.50", "0.00", "0.00")]
GLOVO_REPORT = "Order code,Order date,Products price,Glovo commission,Promotion paid by partner,Refunds,Total to receive,Payout date\n" + \
    "".join(f"{code},2026-09-{14 + i % 7:02d},{price},{fee},{promo},{refund},"
            f"{Decimal(price) - Decimal(fee) - Decimal(promo) - Decimal(refund)},2026-09-21\n"
            for i, (code, price, fee, promo, refund) in enumerate(GLOVO_ORDERS))
# 410.00 - 123.00 - 10.00 refunded - 5.00 promotion = 272.00

GLOVO_QR = E.qr_payload(
    A=GLOVO_NIF, B=CASA_NIF, C="PT", D="FT", E="N", F="20260922", G="FT GLV2026/3801", H="GLV7Q2KX-3801",
    I1="PT", I7="100.00", I8="23.00", N="23.00", O="123.00", Q="Gl0v", R="4411",
)
GLOVO_INVOICE = "\n".join([
    "Glovoapp Portugal, Unipessoal Lda.", f"NIF: {GLOVO_NIF}", "Fatura n.º FT GLV2026/3801", "ATCUD: GLV7Q2KX-3801",
    "Data de emissão: 22/09/2026", "Cliente: Casa Azul, Lda.", f"NIF: {CASA_NIF}",
    "Comissões de serviço - semana 38", "Base tributável (23%): 100,00", "IVA 23%: 23,00", "Total: 123,00 €",
    f"Código QR: {GLOVO_QR}", ""])

UBER_EATS_ROWS = [(f"ue-{n:04d}", f"2026-09-{14 + n % 7:02d}", Decimal(20 + n)) for n in range(12)]
UBER_EATS_REPORT = ("Order ID,Order Date,Sales (incl. VAT),Refunds,Promotions,Uber service fee,Total payout,"
                    "Payout date,Payout reference ID\n" + "".join(
                        f"{oid},{day},{sales},0.00,0.00,-{(sales * Decimal('0.3')).quantize(Decimal('0.01'))},"
                        f"{sales - (sales * Decimal('0.3')).quantize(Decimal('0.01'))},2026-09-22,UE-2026-W38-7731\n"
                        for oid, day, sales in UBER_EATS_ROWS))


def uber_eats_totals() -> tuple[Decimal, Decimal]:
    gross = sum((s for _, _, s in UBER_EATS_ROWS), Decimal(0))
    fees = sum(((s * Decimal("0.3")).quantize(Decimal("0.01")) for _, _, s in UBER_EATS_ROWS), Decimal(0))
    return gross, fees


def neutral_summary(net: str, *, refunds: str = "20.00", payout_id: str = "po_1QzZz9Kk7Lm3Np5Q") -> str:
    return ("provider,payout_id,payout_date,currency,gross,fees,refunds,chargebacks,adjustments,net\n"
            f"Stripe,{payout_id},2026-09-18,EUR,1000.00,29.00,{refunds},0.00,0.00,{net}\n")


# --------------------------------------------------------------------------- card terminal (SIBS)


def test_card_terminal_batch_payout_closes_with_the_sibs_report() -> None:
    svc = business()
    repo = svc.repo
    # The bank names no acquirer: 'TPA' (card terminal) and the batch number only.
    (tx,) = bank(svc, (date(2026, 9, 19), "1394.00", "TPA 1234567", "LIQ TPA LOTE L0918-4471"))
    rec = repo.transactions[tx]
    assert rec.decision.expectation is EvidenceExpectation.PAYOUT_REPORT  # not "money from a customer"
    assert rec.decision.provider.value == "payment_provider"
    assert stage(svc, tx) is Stage.UNDERSTOOD
    assert money_in(svc).total == 0  # a net payout is never customer revenue on its own

    # SIBS emails the settlement report (no provider column: the sender names it).
    mail = E.email(sender="SIBS Extratos <extratos@sibs.pt>", to="rita@casaazul.pt", subject="Liquidações TPA",
                   at=NOW, text="Segue em anexo o extrato de liquidações.\n", message_id="<liq-0918@sibs.pt>",
                   attachments=(("liquidacoes_0918.csv", "text/csv", SIBS_REPORT.encode("utf-8")),))
    report = svc.orchestrator.ingest_file(mail, filename="liquidacoes.eml", content_type="message/rfc822")
    (doc_id,) = report.document_ids
    doc = repo.documents[doc_id]
    assert doc.document.doc_type is DocumentType.PAYOUT_REPORT and doc.document.supplier_name == "SIBS"
    settlement = repo.settlements[doc_id]
    found = settlement.report
    assert (found.gross_sales, found.fees, found.refunds, found.net) == (
        Decimal("1425.40"), Decimal("11.40"), Decimal("20.00"), Decimal("1394.00"))
    assert found.order_count == 4 and found.adds_up and found.payout_id == "L0918-4471"

    # Report adds up, net = bank amount, same batch reference: the payout closes with the report as evidence.
    assert settlement.status == "settled" and settlement.transaction_id == tx
    assert rec.document_ids == [doc_id] and doc.matched_tx_ids == [tx]
    assert_closed_with(svc, tx, doc.evidence_ids)
    assert stage(svc, doc_id) is Stage.CLOSED
    assert "Payout reference: the same on the report and the bank line" in rec.match_why
    assert "Sales: €1,425.40 from 4 card payments" in rec.match_why
    # Field provenance (§18): the stated net from the report, confirmed by the arithmetic and the bank.
    methods = {o.method for o in doc.observations["net_amount"]}
    assert {ExtractionMethod.API, ExtractionMethod.ARITHMETIC, ExtractionMethod.BANK} <= methods
    assert doc.checks["net_amount"].quality is Quality.GREEN
    assert any(o.location and "Montante" in str(o.location) for o in doc.observations["net_amount"])

    # Gross sales are the revenue; the terminal's fees are a cost; the net payout is not counted twice.
    sales = money_in(svc)
    assert sales.total == Decimal("1425.40") and [x.kind for x in sales.lines] == ["sales"]
    line = sales.lines[0]
    assert (line.fees, line.refunds, line.paid_out) == (Decimal("11.40"), Decimal("20.00"), Decimal("1394.00"))
    costs = money_out(svc)
    assert [(x.kind, x.amount, x.category) for x in costs.lines] == [("platform_fee", Decimal("11.40"),
                                                                     "platform_fees")]
    assert_plain(*rec.match_why, rec.match_headline, *(a.text for a in repo.activity))


# --------------------------------------------------------------------------- Stripe


def test_stripe_payout_with_fees_a_refund_and_a_dispute() -> None:
    svc = business()
    repo = svc.repo
    (tx,) = bank(svc, (date(2026, 9, 18), "900.50", "STRIPE PAYMENTS EUROPE", "STRIPE PAYOUT CASA AZUL"))
    upload(svc, STRIPE_REPORT, "payout_reconciliation_itemized_2026-09-18.csv")
    settlement = settlement_for(svc, tx)
    found = settlement.report
    assert found.provider.key == "stripe" and found.format == "stripe_itemized_csv"
    assert found.payout_id == "po_1QxYz8Ab2Cd3Ef4G" and found.payout_date == date(2026, 9, 18)
    assert (found.gross_sales, found.fees, found.refunds, found.chargebacks, found.net) == (
        Decimal("1000.00"), Decimal("29.50"), Decimal("20.00"), Decimal("50.00"), Decimal("900.50"))
    assert found.adds_up and not found.rows_that_do_not_add_up
    assert settlement.status == "settled"
    assert_closed_with(svc, tx, repo.documents[settlement.document_id].evidence_ids)
    rec = repo.transactions[tx]
    assert "Dates: same day" in rec.match_why and "Disputed card payments taken back: €50.00" in rec.match_why

    # The same payout again as the API's JSON: one report, one more piece of evidence, nothing counted twice.
    api = {"object": "list", "data": [
        {"object": "balance_transaction", "id": "txn_3Q1", "amount": 60000, "fee": 870, "net": 59130, "currency": "eur",
         "type": "charge", "reporting_category": "charge", "created": 1789380131, "source": "ch_3Q1"},
        {"object": "balance_transaction", "id": "txn_3Q2", "amount": 40000, "fee": 580, "net": 39420, "currency": "eur",
         "type": "charge", "reporting_category": "charge", "created": 1789400402, "source": "ch_3Q2"},
        {"object": "balance_transaction", "id": "txn_3Q3", "amount": -2000, "fee": 0, "net": -2000, "currency": "eur",
         "type": "refund", "reporting_category": "refund", "created": 1789463565, "source": "re_3Q3"},
        {"object": "balance_transaction", "id": "txn_3Q4", "amount": -5000, "fee": 1500, "net": -6500,
         "currency": "eur", "type": "adjustment", "reporting_category": "dispute", "created": 1789470000,
         "source": "dp_3Q4"},
        {"object": "balance_transaction", "id": "txn_3Q5", "amount": -90050, "fee": 0, "net": -90050,
         "currency": "eur", "type": "payout", "reporting_category": "payout", "created": 1789689600,
         "available_on": 1789689600, "source": "po_1QxYz8Ab2Cd3Ef4G"}]}
    again = upload(svc, json.dumps(api), "po_1QxYz8Ab2Cd3Ef4G.json", "application/json")
    assert again.already_known and again.document_ids == [settlement.document_id]
    assert len(repo.documents[settlement.document_id].evidence_ids) == 2
    assert len(repo.settlements) == 1
    sales = money_in(svc)
    assert sales.total == Decimal("1000.00")
    assert (sales.lines[0].fees, sales.lines[0].refunds, sales.lines[0].chargebacks) == (
        Decimal("29.50"), Decimal("20.00"), Decimal("50.00"))


# --------------------------------------------------------------------------- Booking.com + commission invoice


def test_booking_payout_net_of_commission_and_the_commission_invoice() -> None:
    svc = business()
    repo = svc.repo
    # The commission invoice arrives first: nothing to pay, it will be kept from the payouts.
    invoice = upload(svc, BOOKING_INVOICE_UBL, "booking_commission_2026-09.xml", "application/xml")
    (invoice_id,) = invoice.document_ids
    (tx,) = bank(svc, (date(2026, 9, 12), "306.00", "BOOKING.COM B.V.", "BOOKING.COM PAYOUT",
                       K.TRANSFER_IN, "BKG-PAYOUT-88112233"))
    assert repo.transactions[tx].decision.expectation is EvidenceExpectation.PAYOUT_REPORT
    upload(svc, BOOKING_REPORT, "Payout_statement_BKG-PAYOUT-88112233.csv")
    settlement = settlement_for(svc, tx)
    found = settlement.report
    assert found.provider.key == "booking" and found.order_count == 2
    assert (found.gross_sales, found.fees, found.net) == (Decimal("360.00"), Decimal("54.00"), Decimal("306.00"))
    assert_closed_with(svc, tx, repo.documents[settlement.document_id].evidence_ids)
    assert "Commission: €54.00" in repo.transactions[tx].match_why

    # The commission invoice matches the commission kept from the payout, not a bank payment.
    doc = repo.documents[invoice_id]
    assert settlement.commission_document_ids == [invoice_id]
    assert doc.matched_tx_ids == [tx] and doc.document.entity_id == repo.transactions[tx].company_id
    assert invoice_id not in repo.chases and not any(invoice_id in r.document_ids
                                                     for r in repo.transactions.values())
    total = doc.checks["gross_amount"]
    assert total.quality is Quality.GREEN  # the e-invoice and the payout report agree on €54.00
    assert any(o.method is ExtractionMethod.API and o.value == Decimal("54.00") for o in total.observations)
    # Its other details (a foreign e-invoice, one source) are still unconfirmed: never closed on a guess (§57).
    assert doc.document.quality is Quality.AMBER and stage(svc, invoice_id) is not Stage.CLOSED

    # Gross is revenue; the commission is Booking.com's cost, evidenced by the report and the invoice.
    assert money_in(svc).total == Decimal("360.00")
    (fee,) = money_out(svc).lines
    assert fee.merchant == "Booking.com" and fee.amount == Decimal("54.00")
    assert fee.document_ids == (settlement.document_id, invoice_id)
    assert any("Booking.com invoice to the commission taken from your payout" in a.text for a in repo.activity)


# --------------------------------------------------------------------------- delivery platforms


def test_delivery_platform_weekly_payouts_cover_many_orders() -> None:
    svc = business()
    repo = svc.repo
    glovo_tx, uber_tx = bank(
        svc,
        (date(2026, 9, 22), "272.00", "GLOVOAPP23 SL", "TRF GLOVO PARTNER PAYOUT W38"),
        (date(2026, 9, 23), str(sum((s - (s * Decimal("0.3")).quantize(Decimal("0.01"))
                                     for _, _, s in UBER_EATS_ROWS), Decimal(0))),
         "UBER PORTIER B.V.", "UBER EATS PAYOUT", K.TRANSFER_IN, "UE-2026-W38-7731"),
    )
    upload(svc, GLOVO_REPORT, "glovo_account_statement_w38.csv")
    upload(svc, UBER_EATS_REPORT, "uber_eats_payment_details_w38.csv")

    glovo = settlement_for(svc, glovo_tx).report
    assert glovo.provider.key == "glovo" and glovo.order_count == 8
    assert (glovo.gross_sales, glovo.fees, glovo.refunds, glovo.adjustments, glovo.net) == (
        Decimal("410.00"), Decimal("123.00"), Decimal("10.00"), Decimal("-5.00"), Decimal("272.00"))
    uber = settlement_for(svc, uber_tx).report
    gross, fees = uber_eats_totals()
    assert uber.provider.key == "uber_eats" and uber.order_count == 12
    assert (uber.gross_sales, uber.fees) == (gross, fees) and uber.payout_id == "UE-2026-W38-7731"
    for tx, s in ((glovo_tx, settlement_for(svc, glovo_tx)), (uber_tx, settlement_for(svc, uber_tx))):
        assert s.status == "settled"
        assert_closed_with(svc, tx, repo.documents[s.document_id].evidence_ids)

    # Glovo's Portuguese commission invoice (fiscal QR): matches the week's commission and closes with it.
    invoice = upload(svc, GLOVO_INVOICE, "FT_GLV2026_3801.txt", "text/plain")
    (invoice_id,) = invoice.document_ids
    doc = repo.documents[invoice_id]
    assert doc.document.gross_amount == Decimal("123.00") and doc.document.quality is Quality.GREEN
    assert doc.matched_tx_ids == [glovo_tx] and settlement_for(svc, glovo_tx).commission_document_ids == [invoice_id]
    item = repo.items[doc.item_id]
    assert item.stage is Stage.CLOSED
    assert {*doc.evidence_ids, repo.transactions[glovo_tx].evidence_id} <= set(item.history[-1].evidence_ids)

    ins = money_in(svc)
    assert ins.total == Decimal("410.00") + gross
    assert {x.merchant for x in ins.lines} == {"Sales through Glovo", "Sales through Uber Eats"}
    assert sum((x.amount for x in money_out(svc).lines), Decimal(0)) == Decimal("123.00") + fees
    # September for Casa Azul: every payout, report and invoice is closed with evidence.
    company = repo.transactions[glovo_tx].company_id
    items = repo.items_for(company, Month(2026, 9))
    assert items and all(i.stage is Stage.CLOSED for i in items)


# --------------------------------------------------------------------------- conflicts


def test_report_that_does_not_add_up_is_a_conflict_question_never_a_close() -> None:
    svc = business()
    repo = svc.repo
    (tx,) = bank(svc, (date(2026, 9, 18), "961.00", "STRIPE PAYMENTS EUROPE", "STRIPE PAYOUT"))
    # 1,000.00 - 29.00 - 20.00 = 951.00, but the report says 961.00 (which is also what the bank got).
    bad = upload(svc, neutral_summary("961.00"), "stripe_payout_summary.csv")
    (doc_id,) = bad.document_ids
    settlement = repo.settlements[doc_id]
    assert settlement.status == "does_not_add_up" and not settlement.report.adds_up
    assert repo.documents[doc_id].document.quality is Quality.RED and stage(svc, doc_id) is Stage.CONFLICT
    assert stage(svc, tx) is not Stage.CLOSED and not repo.transactions[tx].document_ids
    (needs,) = [n for n in repo.open_needs() if n.subject_id == doc_id]
    assert needs.kind == "check"
    assert needs.prompt == ("The payout report from Stripe does not add up: €1,000.00 in sales minus €29.00 in "
                            "fees and €20.00 in refunds is €951.00, but it says €961.00 was paid out. "
                            "What should I do?")
    assert bad.message == f"Got it. I need one answer from you: {needs.prompt}"
    assert money_in(svc).total == 0 and len(money_in(svc).waiting_payouts) == 1
    card = next(c for c in svc.needs_you()["items"] if c["id"] == needs.id)
    assert card["merchant"] == "Stripe" and card["amount"] == 961.0
    # The payout knows its report arrived and why it cannot be used (it does not ask for it again).
    assert settlement.transaction_id == tx and repo.transactions[tx].likely_document_ids == [doc_id]
    plan = svc.orchestrator.missing.plan(repo.transactions[tx])
    assert plan == ("The €961.00 payout from Stripe on 18 September came with a payout report that does not add "
                    "up. I asked you what to do.")
    assert plan in chat(svc, "what is missing?")
    assert "its report does not add up" in chat(svc, "how much came in in september?")
    assert_plain(needs.prompt, *needs.why, *(o.label for o in needs.options), plan)

    answer = svc.answer(needs.id, "neither")
    assert answer["message"] == "Done. I set it aside. When Stripe sends a corrected report, I will read it."
    assert settlement.status == "set_aside" and stage(svc, tx) is not Stage.CLOSED
    assert svc.orchestrator.missing.plan(repo.transactions[tx]).endswith("I'm waiting for a corrected one.")

    # The corrected report (refunds were €10.00) arrives: it settles the payout and replaces the bad one.
    upload(svc, neutral_summary("961.00", refunds="10.00"), "stripe_payout_summary_corrected.csv")
    good = settlement_for(svc, tx)
    assert good.status == "settled" and good.document_id != doc_id
    assert_closed_with(svc, tx, repo.documents[good.document_id].evidence_ids)
    assert settlement.status == "replaced" and stage(svc, doc_id) is Stage.NOT_REQUIRED
    assert money_in(svc).total == Decimal("1000.00")


def test_bank_amount_different_from_the_report_is_a_conflict_question() -> None:
    svc = business()
    repo = svc.repo
    upload(svc, neutral_summary("951.00", payout_id="po_1QaBc2De3Fg4Hi5J"), "stripe_payout.csv")
    (doc_id,) = repo.settlements
    assert repo.settlements[doc_id].status == "waiting"  # its payout has not reached the bank yet
    (tx,) = bank(svc, (date(2026, 9, 21), "941.00", "STRIPE PAYMENTS EUROPE", "STRIPE PAYOUT",
                       K.TRANSFER_IN, "po_1QaBc2De3Fg4Hi5J"))
    settlement = repo.settlements[doc_id]
    assert settlement.status == "conflict" and settlement.transaction_id == tx
    assert stage(svc, doc_id) is Stage.CONFLICT and stage(svc, tx) is not Stage.CLOSED
    assert not repo.transactions[tx].document_ids and repo.transactions[tx].likely_document_ids == [doc_id]
    (needs,) = repo.open_needs()
    assert needs.prompt == ("The payout report from Stripe says €951.00 was paid out on 18 September, but €941.00 "
                            "arrived in your bank on 21 September. What should I do?")
    assert "Difference: €10.00" in needs.why
    assert "Payout reference: the same on the report and the bank line" in needs.why
    plan = svc.orchestrator.missing.plan(repo.transactions[tx])
    assert plan == "The €941.00 payout from Stripe on 21 September does not match its payout report. I asked you " \
                   "about it."
    assert money_in(svc).total == 0
    assert "its report does not match what arrived in your bank" in chat(svc, "how much came in in september?")
    assert_plain(needs.prompt, *needs.why, plan)
    svc.answer(needs.id, "neither")
    assert settlement.status == "set_aside" and stage(svc, tx) is not Stage.CLOSED
    assert not repo.open_needs()


def test_two_payouts_that_fit_one_report_equally_are_never_a_silent_pick() -> None:
    svc = business()
    repo = svc.repo
    first, second = bank(svc, (date(2026, 9, 17), "951.00", "STRIPE PAYMENTS EUROPE", "STRIPE PAYOUT"),
                         (date(2026, 9, 19), "951.00", "STRIPE PAYMENTS EUROPE", "STRIPE PAYOUT"))
    upload(svc, neutral_summary("951.00"), "stripe_payout.csv")  # no reference on either bank line
    (settlement,) = repo.settlements.values()
    assert settlement.status == "waiting" and settlement.transaction_id is None
    for tx in (first, second):
        rec = repo.transactions[tx]
        assert not rec.document_ids and stage(svc, tx) is Stage.UNDERSTOOD
        assert rec.likely_document_ids == [settlement.document_id]
    assert svc.orchestrator.missing.plan(repo.transactions[first]) == (
        "The €951.00 payout from Stripe on 17 September and its payout report could be paired more than one way. "
        "I won't pair them on a guess.")
    assert money_in(svc).total == 0


# --------------------------------------------------------------------------- no report yet


def test_payout_without_its_report_stays_open_asks_for_it_and_is_not_customer_revenue() -> None:
    svc = business()
    repo = svc.repo
    (tx,) = bank(svc, (date(2026, 9, 25), "250.00", "PAYPAL EUROPE S.A R.L. ET CIE", "PAYPAL TRANSFER"))
    rec = repo.transactions[tx]
    assert rec.decision.expectation is EvidenceExpectation.PAYOUT_REPORT
    assert rec.decision.expectation is not EvidenceExpectation.SALES_INVOICE
    assert rec.decision.reason == "Payout of your sales from PayPal, after its fees. Its payout report covers it."
    svc.orchestrator.run(NOW)
    assert stage(svc, tx) is Stage.UNDERSTOOD and rec.missing_since == NOW.date()
    assert any(a.kind is ActivityKind.MISSING_DOCUMENT_DETECTED and a.subject_id == tx for a in repo.closure_log)
    assert tx not in repo.chases  # nobody to email: the owner downloads the report from PayPal
    plan = svc.orchestrator.missing.plan(rec)
    assert plan == ("The €250.00 payout from PayPal on 25 September needs its payout report, so I can count the "
                    "sales, fees and refunds behind it. Upload the report here or forward the email it came in.")
    month = svc.month(rec.company_id, "2026-09")
    assert month["status"] == "open" and any(r["text"] == plan for r in month["remaining"])

    ins = money_in(svc)
    assert ins.total == 0 and not ins.lines  # never counted as money from a customer
    (waiting,) = ins.waiting_payouts
    assert (waiting.provider, waiting.status) == ("PayPal", "report_missing")
    reply = chat(svc, "how much came in in september?")
    assert reply.startswith("I can't count any sales in September yet.")
    assert "Not counted yet: the €250.00 payout from PayPal on 25 September, report not received yet." in reply
    facts = tool(svc, "spending_summary", date_from="2026-09-01", date_to="2026-09-30", direction="in")
    assert facts["total_eur"] == 0
    assert facts["payouts_not_counted_yet"][0]["why"] == "payout report not received yet"
    assert_plain(plan, reply)

    # The PayPal activity download arrives: the withdrawal and the payments since the last one.
    paypal = ("Date,Time,TimeZone,Name,Type,Status,Currency,Gross,Fee,Net,From Email Address,Transaction ID,Balance\n"
              "20/09/2026,10:00:00,CEST,Ana,Express Checkout Payment,Completed,EUR,180.00,-5.19,174.81,ana@x.pt,"
              "7KX12345AB678901C,174.81\n"
              "21/09/2026,11:30:00,CEST,Rui,Express Checkout Payment,Completed,EUR,80.00,-2.63,77.37,rui@x.pt,"
              "8KX12345AB678901C,252.18\n"
              "22/09/2026,09:15:00,CEST,Ana,Payment Refund,Completed,EUR,-2.18,0.00,-2.18,ana@x.pt,"
              "9KX12345AB678901C,250.00\n"
              "23/09/2026,09:00:00,CEST,,General Withdrawal,Completed,EUR,-250.00,0.00,-250.00,,"
              "1LX12345AB678901C,0.00\n")
    upload(svc, paypal, "Download.CSV")
    found = settlement_for(svc, tx).report
    assert (found.gross_sales, found.fees, found.refunds, found.net) == (
        Decimal("260.00"), Decimal("7.82"), Decimal("2.18"), Decimal("250.00"))
    assert_closed_with(svc, tx, repo.documents[settlement_for(svc, tx).document_id].evidence_ids)
    assert money_in(svc).total == Decimal("260.00") and not money_in(svc).waiting_payouts


# --------------------------------------------------------------------------- not a sales payout


def test_credit_card_bill_payment_is_not_a_sales_payout() -> None:
    svc = business()
    repo = svc.repo
    card_bill, service_payment, card_refund, customer = bank(
        svc,
        (date(2026, 9, 10), "-845.00", "MILLENNIUM BCP", "LIQUIDACAO CARTAO CREDITO"),
        (date(2026, 9, 11), "-45.00", "SIBS", "PAG SERV SIBS 21234 ENT 10611"),
        (date(2026, 9, 12), "19.90", "STRIPE *SAAS TOOL", "DEVOLUCAO COMPRA", K.CARD, None, "card-1234"),
        (date(2026, 9, 13), "1200.00", "CLIENTE XPTO LDA", "TRF FATURA FT 2026/77"),
    )
    decisions = {tx: repo.transactions[tx].decision.expectation for tx in (card_bill, service_payment, card_refund,
                                                                           customer)}
    assert decisions[card_bill] is EvidenceExpectation.CARD_STATEMENT  # paying off a card, not a payout
    assert decisions[service_payment] is not EvidenceExpectation.PAYOUT_REPORT  # money out through SIBS
    assert decisions[card_refund] is EvidenceExpectation.REFUND_OR_CREDIT_NOTE  # money back on a card
    assert decisions[customer] is EvidenceExpectation.SALES_INVOICE  # a customer paying an invoice
    bill = repo.transactions[card_bill].tx
    assert is_card_repayment(bill) and is_card_settlement(bill) and payout_provider(bill) is None
    assert MatchKind.CARD_SETTLEMENT is not MatchKind.PAYOUT_SETTLEMENT
    assert not repo.settlements
    ledger = Ledger(svc)
    kinds = {x.id: x.kind for x in ledger.lines}
    assert kinds[card_bill] == "card_repayment" and "payout" not in kinds.values()
    # A SIBS report never settles a card bill (money out) even with the same amount.
    upload(svc, "Nº Lote;Data Liquidação;Tipo;Montante Bruto;Comissão;Montante Líquido\n"
                "L0910-0001;10/09/2026;Compra;851,80;6,80;845,00\n", "sibs_0910.csv")
    (settlement,) = repo.settlements.values()
    assert settlement.status == "waiting" and not repo.transactions[card_bill].document_ids


# --------------------------------------------------------------------------- the owner's answers


def test_chat_and_ledger_income_answers_use_gross_sales_fees_and_refunds() -> None:
    svc = business()
    stripe_tx, _, _ = bank(
        svc,
        (date(2026, 9, 18), "951.00", "STRIPE PAYMENTS EUROPE", "STRIPE PAYOUT"),
        (date(2026, 9, 24), "300.00", "CLIENTE XPTO LDA", "TRF FATURA FT 2026/81"),
        (date(2026, 9, 26), "120.00", "SUMUP PAYMENTS LIMITED", "SUMUP PAYOUT"),
    )
    upload(svc, neutral_summary("951.00"), "stripe_payout_summary.csv")
    assert settlement_for(svc, stripe_tx).status == "settled"

    ins = money_in(svc)
    assert ins.total == Decimal("1300.00")  # €300.00 from a customer + €1,000.00 gross sales through Stripe
    assert sorted((x.kind, x.amount) for x in ins.lines) == [("income", Decimal("300.00")),
                                                             ("sales", Decimal("1000.00"))]
    assert [(x.provider, x.amount) for x in ins.waiting_payouts] == [("SumUp", Decimal("120.00"))]
    assert Decimal("951.00") not in {x.amount for x in ins.lines}  # the net payout is never counted

    reply = chat(svc, "how much came in in september?")
    assert reply.startswith("€1,300.00 came in in September, across 2 payments")
    assert "That includes €1,000.00 in sales through Stripe." in reply
    assert "From that, €29.00 went in fees and €20.00 was refunded to customers, so €951.00 reached your bank." \
        in reply
    assert "Not counted yet: the €120.00 payout from SumUp on 26 September, report not received yet." in reply
    assert_plain(reply)

    facts = tool(svc, "spending_summary", date_from="2026-09-01", date_to="2026-09-30", direction="in")
    assert facts["total_eur"] == 1300.0
    assert facts["included"] == {"customer_payments": 300.0, "sales_through_card_terminals_and_platforms": 1000.0}
    sales = facts["sales_from_payout_reports"]
    assert (sales["gross_sales_eur"], sales["fees_and_commission_eur"], sales["refunded_to_customers_eur"],
            sales["paid_out_to_bank_eur"]) == (1000.0, 29.0, 20.0, 951.0)
    assert [p["provider"] for p in facts["payouts_not_counted_yet"]] == ["SumUp"]

    spent = chat(svc, "how much did we spend in september?")
    assert "€29.00" in spent and "payment and platform fees" in spent
    costs = tool(svc, "spending_summary", date_from="2026-09-01", date_to="2026-09-30")
    assert costs["included"]["payment_and_platform_fees"] == 29.0


def _owner_texts(value) -> list[str]:  # type: ignore[no-untyped-def]
    """Every string an owner may read in a response (ids, keys and codes excluded)."""
    if isinstance(value, dict):
        skip = {"id", "href", "companyId", "evidenceIds", "optionId", "currentMonth", "months", "month", "key", "at",
                "date", "due", "lastSyncedAt", "pendingItemIds", "evidence", "documents", "transactions", "tone",
                "kind", "status", "currency", "head", "taxId"}
        return [t for k, v in value.items() if k not in skip for t in _owner_texts(v)]
    if isinstance(value, list):
        return [t for v in value for t in _owner_texts(v)]
    return [value] if isinstance(value, str) else []


def test_every_screen_stays_plain_with_payouts_settled_disputed_and_waiting() -> None:
    svc = business()
    company = next(iter(svc.repo.companies))
    bank(svc, (date(2026, 9, 18), "900.50", "STRIPE PAYMENTS EUROPE", "STRIPE PAYOUT"),
         (date(2026, 9, 19), "1394.00", "TPA 1234567", "LIQ TPA LOTE L0918-4471"),
         (date(2026, 9, 25), "250.00", "PAYPAL EUROPE", "PAYPAL TRANSFER"))
    upload(svc, STRIPE_REPORT, "stripe_payouts.csv")
    upload(svc, SIBS_REPORT.replace("1.250,00;10,00;1.240,00", "1.250,00;10,00;1.250,00"), "sibs_0918.csv")
    assert {s.status for s in svc.repo.settlements.values()} == {"settled", "does_not_add_up"}
    texts: list[str] = []
    for path in ("/api/home", "/api/needs-you", "/api/activity", f"/api/months/{company}/2026-09", "/api/pipeline",
                 f"/api/companies/{company}"):
        status, body = svc.dispatch("GET", path, None)
        assert status == 200, (path, body)
        texts += _owner_texts(body)
    texts += [chat(svc, q) for q in ("how much came in in september?", "what did we spend in september?",
                                     "what is missing?")]
    assert any("Payout report of 18 September" in t for t in texts)
    assert any("does not add up" in t for t in texts)
    assert any("needs its payout report" in t for t in texts)
    assert_plain(*texts)


def test_payouts_replay_identically_in_the_production_api(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """The event-sourced API: live state equals the state rebuilt from the log; reads never change it."""
    from _server_support import bearer, harness, signup

    from backoffice.server.events import state_digest
    from backoffice.server.runtime import TenantManager

    h = harness(tmp_path)
    account = signup(h.client)
    headers, tenant = bearer(account["token"]), account["tenant"]["id"]

    def ok(res):  # type: ignore[no-untyped-def]
        assert res.status_code == 200, res.text
        return res.json()

    source = ok(h.client.post("/api/sources", json={"kind": "bank", "bank": "Millennium BCP",
                                                     "companyId": "padaria-lda", "iban": IBAN}, headers=headers))
    rows = ("date,amount,counterparty,account,description,kind\n"
            f"2026-09-19,1394.00,TPA 1234567,{source['id']},LIQ TPA LOTE L0918-4471,transfer_in\n"
            f"2026-09-18,961.00,STRIPE PAYMENTS EUROPE,{source['id']},STRIPE PAYOUT,transfer_in\n")
    ok(h.client.post("/api/evidence", files={"file": ("extrato.csv", rows.encode(), "text/csv")}, headers=headers))
    ok(h.client.post("/api/evidence", files={"file": ("sibs_0918.csv", SIBS_REPORT.encode(), "text/csv")},
                     headers=headers))
    ok(h.client.post("/api/evidence", files={"file": ("stripe.csv", neutral_summary("961.00").encode(),
                                                      "text/csv")}, headers=headers))
    needs = ok(h.client.get("/api/needs-you", headers=headers))["items"]
    (question,) = [n for n in needs if "does not add up" in n["question"]]
    ok(h.client.post(f"/api/needs-you/{question['id']}/answer", json={"optionId": "neither"}, headers=headers))
    for path in ("/api/home", "/api/activity", "/api/months/padaria-lda/2026-09", "/api/documents", "/api/pipeline"):
        ok(h.client.get(path, headers=headers))  # strict reads: a read that changed state would fail here
    ok(h.client.post("/api/chat", json={"message": "how much came in in september?"}, headers=headers))

    h.clock.step = h.clock.step * 0
    with h.manager.open(tenant) as rt:
        live = state_digest(rt.service)
        statuses = sorted(s.status for s in rt.service.repo.settlements.values())
    assert statuses == ["set_aside", "settled"]
    fresh = TenantManager(h.store, h.objects, now=h.clock, strict_reads=True)
    with fresh.open(tenant) as rt:
        assert state_digest(rt.service) == live
        assert sorted(s.status for s in rt.service.repo.settlements.values()) == statuses


# --------------------------------------------------------------------------- reading the layouts


def test_each_provider_layout_is_read_into_the_same_figures() -> None:
    def one(data: str, filename: str, hint: str = ""):
        (found,) = parse_settlement_reports(data.encode("utf-8"), source="ev_test", filename=filename, hint=hint)
        return found

    airbnb = one("Date,Arriving by date,Type,Confirmation code,Start date,Nights,Guest,Listing,Details,"
                 "Reference code,Currency,Amount,Paid out,Service fee,Gross earnings\n"
                 "09/12/2026,09/14/2026,Payout,,,,,,Transfer to Account ***1234,,EUR,,582.00,,\n"
                 "09/12/2026,,Reservation,HMABC12345,09/10/2026,3,Joana,Studio,,,EUR,388.00,,12.00,400.00\n"
                 "09/12/2026,,Reservation,HMABC12346,09/11/2026,2,Pedro,Studio,,,EUR,194.00,,6.00,200.00\n",
                 "airbnb_.csv")
    assert (airbnb.provider.key, airbnb.payout_date, airbnb.gross_sales, airbnb.fees, airbnb.net) == (
        "airbnb", date(2026, 9, 12), Decimal("600.00"), Decimal("18.00"), Decimal("582.00"))
    neutral_json = one(json.dumps({"provider": "Mollie", "payouts": [{
        "id": "st_2026_0918", "date": "2026-09-18", "currency": "EUR", "gross": "500.00", "fees": "7.25",
        "refunds": "0", "net": "492.75",
        "lines": [{"type": "sale", "reference": "tr_1", "gross": "300.00", "fee": "4.35", "net": "295.65"},
                  {"type": "sale", "reference": "tr_2", "gross": "200.00", "fee": "2.90", "net": "197.10"}]}]}),
        "settlement.json")
    assert (neutral_json.provider.key, neutral_json.order_count, neutral_json.net, neutral_json.adds_up) == (
        "mollie", 2, Decimal("492.75"), True)
    neutral_lines = one("provider,payout_id,payout_date,currency,type,reference,date,amount,fee\n"
                        "Adyen,PAY-9981,2026-09-18,EUR,sale,psp1,2026-09-15,80.00,1.20\n"
                        "Adyen,PAY-9981,2026-09-18,EUR,chargeback,psp0,2026-09-16,-25.00,\n"
                        "Adyen,PAY-9981,2026-09-18,EUR,payout,,,53.80,\n", "adyen.csv")
    assert (neutral_lines.provider.key, neutral_lines.chargebacks, neutral_lines.net, neutral_lines.adds_up) == (
        "adyen", Decimal("25.00"), Decimal("53.80"), True)
    # A rows-only report whose rows disagree with their own net is flagged, never trusted.
    broken = one("Order ID,Order Date,Sales (incl. VAT),Uber service fee,Total payout,Payout date\n"
                 "a1,2026-09-14,25.00,-7.50,17.50,2026-09-21\n"
                 "a2,2026-09-15,40.00,-12.00,30.00,2026-09-21\n", "uber_eats.csv")
    assert not broken.adds_up and len(broken.rows_that_do_not_add_up) == 1
    assert broken.mismatch_sentence() == ("€65.00 in sales minus €19.50 in commission is €45.50, but it says €47.50 "
                                          "was paid out.")
    # Not a payout report at all: nothing is read from it here.
    assert parse_settlement_reports(b"date,amount,counterparty,account\n2026-09-01,-10.00,X,bcp\n",
                                    source="ev_x") == []


@pytest.mark.parametrize("counterparty, description, key", [
    ("SIBS PAGAMENTOS", "TPA 1234567", "sibs"),
    ("REDUNIQ", "LIQUIDACAO VENDAS", "reduniq"),
    ("COMERCIA GLOBAL PAYMENTS", "LIQ", "comercia"),
    ("TPA 7654321", "LIQ TPA", "card_terminal"),
    ("STRIPE", "STRIPE PAYOUT", "stripe"),
    ("PAYPAL EUROPE", "PAYPAL TRANSFER", "paypal"),
    ("SHOPIFY INTERNATIONAL", "SHOPIFY PAYMENTS PAYOUT", "shopify"),
    ("AMAZON PAYMENTS EUROPE SCA", "SELLER PAYOUT", "amazon"),
    ("GLOVOAPP23 SL", "PAYOUT", "glovo"),
    ("UBER PORTIER B.V.", "UBER EATS", "uber_eats"),
    ("BOLT OPERATIONS", "BOLT FOOD PAYOUT", "bolt_food"),
    ("BOOKING.COM B.V.", "PAYOUT", "booking"),
    ("AIRBNB PAYMENTS LUXEMBOURG", "PAYOUT", "airbnb"),
    ("MOLLIE B.V.", "SETTLEMENT", "mollie"),
    ("SUMUP LIMITED", "PAYOUT", "sumup"),
    ("SQUARE EUROPE LTD", "PAYOUT", "square"),
    ("ADYEN N.V.", "PAYOUT", "adyen"),
])
def test_payout_providers_are_recognised_on_money_in_only(counterparty: str, description: str, key: str) -> None:
    svc = business()
    tx_in, tx_out = bank(svc, (date(2026, 9, 15), "100.00", counterparty, description),
                         (date(2026, 9, 16), "-100.00", counterparty, description))
    repo = svc.repo
    rec = repo.transactions[tx_in]
    assert payout_provider(rec.tx).key == key
    assert rec.decision.expectation is EvidenceExpectation.PAYOUT_REPORT
    assert payout_provider(repo.transactions[tx_out].tx) is None
    assert repo.transactions[tx_out].decision.expectation is not EvidenceExpectation.PAYOUT_REPORT
