"""Acceptance: engine fixes found by the audit (§3 golden rule, §20–21, §26, §28, §37, §51, §57).

Each test replays the demo tenant (Hazel Tree, Company B, Company C) and then feeds it the
evidence of one scenario through the real orchestrator, the way a connector or the owner
would: fiscal-QR invoice text layers, bank rows and one-tap answers.

1. Non-accounting documents (pro-forma, quote, delivery note, order, supplier statement)
   are supporting evidence only: they never close a payment and are never booked.
2. A credit note is never merged into its invoice; linked to it, it is netted against it.
3. The business's own sales invoices are read the right way round and match money in.
4. An invoice addressed to one of your companies, paid from another's account, is a question.
5. A receipt paid in cash closes on its own evidence (or asks), and is counted as spending.
6. Large first purchases need more than a matching amount; capital assets are flagged.
"""

from __future__ import annotations

import base64
import csv
import io
import re
import zipfile
from datetime import date
from decimal import Decimal

import pytest

from backoffice.closure import Month
from backoffice.demo import build_demo
from backoffice.demo import evidence as E
from backoffice.domain.lifecycle import Stage
from backoffice.domain.models import (
    SUPPORTING_DOCUMENT_TYPES,
    CriticalField,
    DocumentType,
    Quality,
    SourceKind,
    Supplier,
    TransactionKind,
)
from backoffice.extraction import parse_einvoice
from backoffice.countries.pt import extract_text_fields
from backoffice.language import find_jargon, find_off_tone
from backoffice.orchestrator import BankRow, Orchestrator
from backoffice.purchases import CAPITAL_ASSET_FLAG
from backoffice.service import BackOfficeService
from backoffice.spending import Ledger

SEPT = Month(2026, 9)
NORTE_NIF = "509123457"  # Papelaria Norte, a supplier of Hazel Tree (fictional, valid check digit)
CUSTOMER_NIF = "512345678"  # Atelier Lume, a customer of Hazel Tree
CAFE_NIF = "508111226"
MAQUINARIA_NIF = "510999883"
FINAL_CONSUMER = "999999990"


# --------------------------------------------------------------------------- evidence builders


def _pt(value: str) -> str:
    whole, cents = value.split(".")
    groups = []
    while len(whole) > 3:
        groups.insert(0, whole[-3:])
        whole = whole[:-3]
    return ".".join([whole, *groups]) + "," + cents


def qr_document(code: str, number: str, atcud: str, total: str, net: str, vat: str, day: str, *, title: str,
                issuer: str = NORTE_NIF, issuer_name: str = "Papelaria Norte, Lda.", buyer: str = E.HAZEL_NIF,
                buyer_line: str = "Cliente: Hazel Tree Interiores, Lda.", extra: tuple[str, ...] = ()) -> bytes:
    """A Portuguese document's text layer plus its decoded AT fiscal QR code (as the demo's own files)."""
    payload = E.qr_payload(A=issuer, B=buyer, C="PT", D=code, E="N", F=day.replace("-", ""), G=number, H=atcud,
                           I1="PT", I7=net, I8=vat, N=vat, O=total, Q="ab12", R="1234")
    issued = f"{day[8:10]}/{day[5:7]}/{day[0:4]}"
    buyer_lines = [buyer_line] if buyer == FINAL_CONSUMER else [buyer_line, f"NIF: {buyer}"]
    return E._invoice_text(
        [issuer_name, f"NIF: {issuer}", f"{title} n.º {number}", f"ATCUD: {atcud}", f"Data de emissão: {issued}"],
        [*buyer_lines, *extra, f"Base tributável (23%): {_pt(net)}", f"IVA 23%: {_pt(vat)}",
         f"Total: {_pt(total)} €"],
        payload,
    )


def bank(bank_id: str, account: str, day: date, amount: str, who: str, description: str,
         kind: TransactionKind = TransactionKind.TRANSFER_OUT) -> BankRow:
    return BankRow(bank_id=bank_id, account_id=account, booked_on=day, amount=Decimal(amount), counterparty=who,
                   description=description, kind=kind)


def upload(o: Orchestrator, data: bytes, name: str = "document.txt", *, scan: bool = False):  # type: ignore[no-untyped-def]
    if scan:
        return o.ingest_file(data, filename=name, content_type="text/plain", source_kind=SourceKind.MOBILE_SCAN,
                             origin="scan")
    return o.ingest_file(data, filename=name, content_type="text/plain")


def tx_by(o: Orchestrator, description: str):  # type: ignore[no-untyped-def]
    return next(r for r in o.repo.transactions.values() if r.tx.description == description)


def stage(o: Orchestrator, record) -> Stage:  # type: ignore[no-untyped-def]
    return o.repo.items[record.item_id].stage


def plain(*texts: str) -> None:
    for text in texts:
        assert not find_jargon(text), (text, find_jargon(text))
        assert not find_off_tone(text), text


@pytest.fixture
def demo() -> Orchestrator:
    o = build_demo()
    o.repo.add_supplier(Supplier(id="sup-norte", tenant_id=o.repo.tenant_id, name="Papelaria Norte",
                                 aliases=["PAPELARIA NORTE"], tax_id=NORTE_NIF, countries=["PT"],
                                 contact_email="faturas@papelarianorte.pt"))
    return o


# --------------------------------------------------------------------------- 1. non-accounting documents


def edp_pro_forma(total: str = "64.10", net: str = "52.11", vat: str = "11.99", number: str = "PF EDP2026/77",
                  day: str = "2026-09-18") -> bytes:
    return qr_document("PF", number, f"EDPQ7K2M-{number.rsplit('/', 1)[1]}", total, net, vat, day,
                       title="Fatura Pró-forma", issuer=E.EDP_NIF,
                       issuer_name="EDP Comercial - Comercialização de Energia, S.A.",
                       extra=("Eletricidade - estimativa",))


def test_pro_forma_is_supporting_evidence_and_never_closes_the_payment(demo: Orchestrator) -> None:
    repo = demo.repo
    edp = tx_by(demo, "DD EDP COMERCIAL")
    report = upload(demo, edp_pro_forma())
    doc = repo.documents[report.document_ids[0]]
    assert doc.document.doc_type is DocumentType.PRO_FORMA and doc.supporting
    assert doc.document.quality is Quality.GREEN  # its readings agree, yet it proves nothing
    # kept with the payment and the supplier, never as its proof
    assert doc.supplier_id == "sup-edp"
    assert edp.supporting_document_ids == [doc.id] and doc.supports_tx_ids == [edp.id]
    assert edp.document_ids == [] and doc.matched_tx_ids == []
    assert stage(demo, edp) is Stage.UNDERSTOOD
    assert stage(demo, doc) is Stage.NOT_REQUIRED  # needs no closing of its own, never blocks the month
    assert edp.id in repo.chases  # the missing-invoice chase still applies
    assert not demo.month_status("hazel-tree", SEPT).closed
    assert "not an invoice" in report.message
    plain(report.message, *(a.text for a in repo.activity[-3:]))
    # the real invoice closes the payment; the pro-forma stays supporting evidence
    upload(demo, E.EDP_INVOICE, "Fatura_EDP.txt")
    assert stage(demo, edp) is Stage.CLOSED
    assert [repo.documents[d].document.doc_type for d in edp.document_ids] == [DocumentType.INVOICE]
    assert demo.auditor.recheck() == []


def test_a_payment_matched_only_to_a_pro_forma_stays_open_and_is_chased(demo: Orchestrator) -> None:
    repo = demo.repo
    demo.ingest_bank([bank("mbcp-0926-01", "mbcp-ht", date(2026, 9, 26), "-80.00", "EDP COMERCIAL",
                           "TRF EDP ADIANTAMENTO")])
    upload(demo, edp_pro_forma("80.00", "65.04", "14.96", "PF EDP2026/78", "2026-09-25"))
    rec = tx_by(demo, "TRF EDP ADIANTAMENTO")
    assert rec.document_ids == [] and rec.likely_document_ids == []
    assert len(rec.supporting_document_ids) == 1
    assert stage(demo, rec) is Stage.UNDERSTOOD
    assert rec.id in repo.chases  # asked EDP for the real invoice
    plan = demo.missing.plan(rec)
    assert "EDP" in plan and "invoice" in plan
    plain(plan)


def test_before_the_chase_the_plan_says_the_pro_forma_is_not_the_invoice(demo: Orchestrator) -> None:
    demo.ingest_bank([bank("mbcp-1001-01", "mbcp-ht", date(2026, 10, 1), "-80.00", "EDP COMERCIAL",
                           "TRF EDP ADIANTAMENTO")])
    upload(demo, edp_pro_forma("80.00", "65.04", "14.96", "PF EDP2026/79", "2026-09-30"))
    rec = tx_by(demo, "TRF EDP ADIANTAMENTO")
    assert rec.id not in demo.repo.chases  # a day old: not missing yet
    plan = demo.missing.plan(rec)
    assert plan == ("I have the pro-forma for the €80.00 payment to EDP on 1 October. It is not an invoice, so "
                    "I'm still looking for the invoice.")
    plain(plan)


@pytest.mark.parametrize(
    ("title", "kind"),
    [
        ("Fatura Pró-forma", DocumentType.PRO_FORMA),
        ("Pro-forma invoice", DocumentType.PRO_FORMA),
        ("Orçamento", DocumentType.QUOTE),
        ("Guia de remessa", DocumentType.DELIVERY_NOTE),
        ("Guia de transporte", DocumentType.DELIVERY_NOTE),
        ("Confirmação de encomenda", DocumentType.ORDER_CONFIRMATION),
        ("Extrato de conta corrente", DocumentType.SUPPLIER_STATEMENT),
    ],
)
def test_non_accounting_documents_are_their_own_kinds_and_close_nothing(demo: Orchestrator, title: str,
                                                                       kind: DocumentType) -> None:
    repo = demo.repo
    text = (f"Papelaria Norte, Lda.\nNIF: {NORTE_NIF}\n{title} n.º 2026/12\nData de emissão: 20/09/2026\n"
            f"Cliente: Hazel Tree Interiores, Lda.\nNIF: {E.HAZEL_NIF}\nTotal: 212,00 €\n").encode()
    demo.ingest_bank([bank("mbcp-0921-09", "mbcp-ht", date(2026, 9, 21), "-212.00", "PAPELARIA NORTE",
                           "TRF PAPELARIA NORTE")])
    doc = repo.documents[upload(demo, text).document_ids[0]]
    assert doc.document.doc_type is kind and kind in SUPPORTING_DOCUMENT_TYPES
    assert stage(demo, doc) is Stage.NOT_REQUIRED
    rec = tx_by(demo, "TRF PAPELARIA NORTE")
    assert rec.document_ids == [] and stage(demo, rec) is not Stage.CLOSED


@pytest.mark.parametrize(
    ("title", "kind"),
    [("Nota de débito", DocumentType.DEBIT_NOTE), ("Nota de crédito", DocumentType.CREDIT_NOTE),
     ("Fatura-recibo", DocumentType.INVOICE_RECEIPT), ("Fatura", DocumentType.INVOICE)],
)
def test_accounting_documents_keep_their_own_kind(demo: Orchestrator, title: str, kind: DocumentType) -> None:
    text = (f"Papelaria Norte, Lda.\nNIF: {NORTE_NIF}\n{title} n.º A 2026/7\nData de emissão: 20/09/2026\n"
            f"Cliente: Hazel Tree Interiores, Lda.\nNIF: {E.HAZEL_NIF}\nTotal: 41,00 €\n").encode()
    doc = demo.repo.documents[upload(demo, text).document_ids[0]]
    assert doc.document.doc_type is kind and not doc.supporting  # a debit note is never a receipt


def test_an_invoice_that_mentions_a_quote_or_delivery_note_is_still_an_invoice(demo: Orchestrator) -> None:
    text = qr_document("FT", "FT A/44", "ABCD1234-44", "246.00", "200.00", "46.00", "2026-09-19", title="Fatura",
                       extra=("Conforme orçamento n.º 2026/3", "V/ Guia de remessa GR 2026/33",
                              "Purchase order: PO-2026-118"))
    demo.ingest_bank([bank("mbcp-0922-09", "mbcp-ht", date(2026, 9, 22), "-246.00", "PAPELARIA NORTE",
                           "TRF PAPELARIA NORTE")])
    doc = demo.repo.documents[upload(demo, text).document_ids[0]]
    assert doc.document.doc_type is DocumentType.INVOICE
    assert stage(demo, tx_by(demo, "TRF PAPELARIA NORTE")) is Stage.CLOSED


def test_a_receipt_where_an_invoice_is_needed_is_kept_as_supporting_evidence(demo: Orchestrator) -> None:
    """ACCEPTED_DOCUMENT_TYPES (§21): a known supplier's payment needs its invoice; a receipt is not it."""
    repo = demo.repo
    edp = tx_by(demo, "DD EDP COMERCIAL")
    receipt = E._invoice_text(
        ["EDP Comercial - Comercialização de Energia, S.A.", f"NIF: {E.EDP_NIF}", "Recibo n.º RG EDP2026/9",
         "ATCUD: EDPQ7K2M-9", "Data de emissão: 19/09/2026"],
        ["Cliente: Hazel Tree Interiores, Lda.", f"NIF: {E.HAZEL_NIF}", "Total: 64,10 €"],
        E.qr_payload(A=E.EDP_NIF, B=E.HAZEL_NIF, C="PT", D="RG", E="N", F="20260919", G="RG EDP2026/9",
                     H="EDPQ7K2M-9", I1="0", N="0.00", O="64.10", Q="e1Dk", R="1422"))
    doc = repo.documents[upload(demo, receipt).document_ids[0]]
    assert doc.document.doc_type is DocumentType.RECEIPT
    assert edp.document_ids == [] and edp.supporting_document_ids == [doc.id]
    assert stage(demo, edp) is Stage.UNDERSTOOD
    upload(demo, E.EDP_INVOICE)
    assert stage(demo, edp) is Stage.CLOSED
    assert stage(demo, doc) is Stage.NOT_REQUIRED  # kept with the closed payment
    assert demo.auditor.recheck() == []


def test_accountant_export_marks_supporting_documents_as_not_booked(demo: Orchestrator) -> None:
    upload(demo, edp_pro_forma())
    svc = BackOfficeService(demo)
    listed = {d["type"]: d for d in svc.documents_list({"company": "hazel-tree"})["items"]}
    assert listed["pro forma"]["booking"] == "supporting"
    assert listed["pro forma"]["status"] == "supporting evidence, not an invoice"
    assert listed["invoice receipt"]["booking"] == "booked"
    exported = svc.documents_export({"company": "hazel-tree"})  # the accountant's download (§28)
    data, count = base64.b64decode(exported["data"]), exported["count"]
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        rows = list(csv.DictReader(io.StringIO(z.read("ledger.csv").decode("utf-8-sig")), delimiter=";"))
        manifest = z.read("manifest.json").decode()
        names = z.namelist()
    by_type = {r["type"]: r for r in rows}
    assert by_type["pro forma"]["booking"] == "supporting"
    assert by_type["pro forma"]["file"].startswith("documents/supporting/")
    assert all(r["booking"] == "booked" for r in rows if r["type"] != "pro forma")
    assert '"booking": "supporting"' in manifest and any(n.startswith("documents/supporting/") for n in names)
    assert count == len(rows)


# --------------------------------------------------------------------------- 2. credit notes


def test_a_credit_note_with_its_invoices_number_is_not_merged_or_a_conflict(demo: Orchestrator) -> None:
    repo = demo.repo

    def text(kind: str, total: str) -> bytes:
        return (f"Papelaria Norte, Lda.\nNIF: {NORTE_NIF}\n{kind} n.º A 2026/183\nData de emissão: 12/09/2026\n"
                f"Cliente: Hazel Tree Interiores, Lda.\nNIF: {E.HAZEL_NIF}\nReferente à fatura A 2026/183\n"
                f"Total: {total} €\n").encode()

    invoice = repo.documents[upload(demo, text("Fatura", "500,00"), "ft.txt").document_ids[0]]
    report = upload(demo, text("Nota de crédito", "123,00"), "nc.txt")
    note = repo.documents[report.document_ids[0]]
    assert note.id != invoice.id and not report.already_known
    assert note.document.doc_type is DocumentType.CREDIT_NOTE and invoice.document.doc_type is DocumentType.INVOICE
    assert invoice.evidence_ids == [invoice.evidence_ids[0]]  # nothing merged into the invoice
    for record in (invoice, note):
        assert record.document.quality is not Quality.RED and stage(demo, record) is not Stage.CONFLICT
    assert not [n for n in repo.open_needs() if n.subject_id in (invoice.id, note.id)]
    assert note.credit_for == invoice.id  # it says which invoice it corrects


@pytest.mark.parametrize("reference", ["Referente à fatura FT A/183", "Ref. FT A/183",
                                       "Documento de origem: FT A/183"])
def test_a_credit_note_is_netted_against_its_invoice(demo: Orchestrator, reference: str) -> None:
    repo = demo.repo
    invoice = repo.documents[upload(demo, qr_document(
        "FT", "FT A/183", "ABCD1234-183", "500.00", "406.50", "93.50", "2026-09-10", title="Fatura"),
        "ft.txt").document_ids[0]]
    note = repo.documents[upload(demo, qr_document(
        "NC", "NC A/12", "ABCD1234-12", "123.00", "100.00", "23.00", "2026-09-12", title="Nota de crédito",
        extra=(reference,)), "nc.txt").document_ids[0]]
    assert note.referenced_number == "FT A/183" and note.credit_for == invoice.id
    assert any("credit note NC A/12 to invoice FT A/183" in a.text for a in repo.activity)
    demo.ingest_bank([bank("mbcp-0920-09", "mbcp-ht", date(2026, 9, 20), "-377.00", "PAPELARIA NORTE",
                           "TRF PAPELARIA NORTE")])
    rec = tx_by(demo, "TRF PAPELARIA NORTE")
    assert rec.document_ids == [invoice.id, note.id]
    assert stage(demo, rec) is Stage.CLOSED
    assert stage(demo, invoice) is Stage.CLOSED and stage(demo, note) is Stage.CLOSED
    assert invoice.document.quality is Quality.GREEN  # 377.00 paid + 123.00 credited = the 500.00 total
    assert "Credit note NC A/12: €123.00 taken off" in rec.match_why
    assert demo.auditor.recheck() == []


def test_a_full_payment_closes_the_invoice_and_the_credit_note_waits_for_its_refund(demo: Orchestrator) -> None:
    repo = demo.repo
    invoice = repo.documents[upload(demo, qr_document(
        "FT", "FT A/183", "ABCD1234-183", "500.00", "406.50", "93.50", "2026-09-10", title="Fatura")).document_ids[0]]
    note = repo.documents[upload(demo, qr_document(
        "NC", "NC A/12", "ABCD1234-12", "123.00", "100.00", "23.00", "2026-09-12", title="Nota de crédito",
        extra=("Referente à fatura FT A/183",))).document_ids[0]]
    demo.ingest_bank([bank("mbcp-0920-09", "mbcp-ht", date(2026, 9, 20), "-500.00", "PAPELARIA NORTE",
                           "TRF PAPELARIA NORTE")])
    rec = tx_by(demo, "TRF PAPELARIA NORTE")
    assert rec.document_ids == [invoice.id] and stage(demo, rec) is Stage.CLOSED
    assert note.credit_for == invoice.id and stage(demo, note) is Stage.UNDERSTOOD
    demo.ingest_bank([bank("mbcp-0915-09", "mbcp-ht", date(2026, 9, 15), "123.00", "PAPELARIA NORTE",
                           "DEVOLUCAO PAPELARIA NORTE", TransactionKind.TRANSFER_IN)])
    refund = tx_by(demo, "DEVOLUCAO PAPELARIA NORTE")
    assert refund.document_ids == [note.id] and stage(demo, note) is Stage.CLOSED


def test_an_invoice_cancelled_in_full_by_its_credit_note_closes_without_a_payment(demo: Orchestrator) -> None:
    repo = demo.repo
    invoice = repo.documents[upload(demo, qr_document(
        "FT", "FT A/190", "ABCD1234-190", "246.00", "200.00", "46.00", "2026-09-10", title="Fatura")).document_ids[0]]
    note = repo.documents[upload(demo, qr_document(
        "NC", "NC A/13", "ABCD1234-13", "246.00", "200.00", "46.00", "2026-09-11", title="Nota de crédito",
        extra=("Anula a fatura FT A/190",))).document_ids[0]]
    assert note.credit_for == invoice.id
    assert stage(demo, invoice) is Stage.CLOSED and stage(demo, note) is Stage.CLOSED
    assert demo.auditor.recheck() == []


def test_ubl_credit_note_reads_the_invoice_it_corrects() -> None:
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<CreditNote xmlns="urn:oasis:names:specification:ubl:schema:xsd:CreditNote-2"
  xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
  xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">
  <cbc:ID>NC A/12</cbc:ID><cbc:IssueDate>2026-09-12</cbc:IssueDate>
  <cbc:DocumentCurrencyCode>EUR</cbc:DocumentCurrencyCode>
  <cac:BillingReference><cac:InvoiceDocumentReference><cbc:ID>FT A/183</cbc:ID></cac:InvoiceDocumentReference>
  </cac:BillingReference>
  <cac:AccountingSupplierParty><cac:Party><cac:PartyTaxScheme><cbc:CompanyID>PT{NORTE_NIF}</cbc:CompanyID>
  <cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme></cac:PartyTaxScheme></cac:Party></cac:AccountingSupplierParty>
  <cac:LegalMonetaryTotal><cbc:PayableAmount currencyID="EUR">123.00</cbc:PayableAmount></cac:LegalMonetaryTotal>
</CreditNote>""".encode()
    result = parse_einvoice(xml, source="ev_nc")
    assert result.doc_type is DocumentType.CREDIT_NOTE
    assert result.extras["invoice_reference"] == "FT A/183"


# --------------------------------------------------------------------------- 3. the business's own sales invoices


def hazel_sale(total: str = "1230.00", net: str = "1000.00", vat: str = "230.00") -> bytes:
    return qr_document("FT", "FT HT2026/31", "HTQ7K2MP-31", total, net, vat, "2026-09-14", title="Fatura",
                       issuer=E.HAZEL_NIF, issuer_name="Hazel Tree Interiores, Lda.", buyer=CUSTOMER_NIF,
                       buyer_line="Cliente: Atelier Lume, Lda.", extra=("Projeto de interiores - setembro",))


def test_own_sales_invoice_is_read_the_right_way_round(demo: Orchestrator) -> None:
    repo = demo.repo
    report = upload(demo, hazel_sale())
    doc = repo.documents[report.document_ids[0]]
    assert doc.sales
    assert doc.document.quality is Quality.GREEN  # no false QR conflict
    assert doc.document.supplier_tax_id == E.HAZEL_NIF and doc.document.customer_tax_id == CUSTOMER_NIF
    assert doc.document.entity_id == "hazel-tree"  # issued by Hazel Tree
    assert not doc.on_hold and stage(demo, doc) is Stage.UNDERSTOOD
    assert not [n for n in repo.open_needs() if n.subject_id == doc.id]
    assert report.message == "Got it."
    assert repo.activity[-1].text == "Collected Hazel Tree's sales invoice FT HT2026/31 from your upload."


def test_own_sales_invoice_matches_the_customer_payment(demo: Orchestrator) -> None:
    repo = demo.repo
    doc = repo.documents[upload(demo, hazel_sale()).document_ids[0]]
    demo.ingest_bank([bank("mbcp-0924-09", "mbcp-ht", date(2026, 9, 24), "1230.00", "ATELIER LUME LDA",
                           "TRF FT HT2026/31", TransactionKind.TRANSFER_IN)])
    rec = tx_by(demo, "TRF FT HT2026/31")
    assert rec.decision is not None and rec.decision.expectation.value == "sales_invoice"
    assert rec.document_ids == [doc.id]
    assert stage(demo, rec) is Stage.CLOSED and stage(demo, doc) is Stage.CLOSED
    assert "Money received: €1,230.00" in rec.match_why


def test_text_reader_takes_a_known_issuer_as_the_supplier() -> None:
    text = (f"Hazel Tree Interiores, Lda.\nNIF: {E.HAZEL_NIF}\nFatura n.º FT HT2026/31\n"
            f"Cliente: Atelier Lume, Lda.\nNIF: {CUSTOMER_NIF}\nTotal: 1.230,00 €\n")
    before = extract_text_fields(text, "ev", known_customer_tax_ids=[E.HAZEL_NIF])
    assert before.get(CriticalField.CUSTOMER_TAX_ID).value == E.HAZEL_NIF  # the old assumption, for purchases
    after = extract_text_fields(text, "ev", known_customer_tax_ids=[], known_supplier_tax_ids=[E.HAZEL_NIF])
    assert after.get(CriticalField.SUPPLIER_TAX_ID).value == E.HAZEL_NIF
    assert after.get(CriticalField.CUSTOMER_TAX_ID).value == CUSTOMER_NIF


# --------------------------------------------------------------------------- 4. the wrong company's account


def pay_hazel_invoice_from_company_b(o: Orchestrator):  # type: ignore[no-untyped-def]
    doc = o.repo.documents[upload(o, qr_document(
        "FT", "FT A/200", "ABCD1234-200", "246.00", "200.00", "46.00", "2026-09-10", title="Fatura")).document_ids[0]]
    o.ingest_bank([bank("cgd-0912-09", "cgd-b", date(2026, 9, 12), "-246.00", "PAPELARIA NORTE",
                        "TRF PAPELARIA NORTE")])
    return doc, tx_by(o, "TRF PAPELARIA NORTE")


def test_an_invoice_for_another_company_paid_from_the_wrong_account_asks_one_question(demo: Orchestrator) -> None:
    repo = demo.repo
    doc, rec = pay_hazel_invoice_from_company_b(demo)
    assert doc.document.entity_id == "hazel-tree" and rec.tx.entity_id == "company-b"
    assert rec.document_ids == [doc.id]  # the match itself is right...
    assert stage(demo, rec) is Stage.NEEDS_OWNER and stage(demo, doc) is not Stage.CLOSED  # ...but not closed
    (needs,) = [n for n in repo.open_needs() if n.kind == "company"]
    assert needs.prompt == "Company B paid an invoice addressed to Hazel Tree. Which company should carry it?"
    assert [o.id for o in needs.options] == ["company:hazel-tree", "company:company-b"]
    svc = BackOfficeService(demo)
    shown = next(i for i in svc.needs_you()["items"] if i["id"] == needs.id)
    assert shown["kind"] == "choice" and shown["question"] == needs.prompt and shown["amount"] == 246
    assert {o["label"] for o in shown["options"]} == {"Hazel Tree (paid by Company B)", "Company B"}
    plain(needs.prompt, *needs.why, *(o.label for o in needs.options))
    b = svc.month("company-b", "2026-09")
    assert b is not None and b["status"] == "open"
    assert any(r.get("href") == f"/needs-you#{needs.id}" for r in b["remaining"])
    assert demo.auditor.recheck() == []


def test_the_answer_closes_it_and_records_the_inter_company_payment(demo: Orchestrator) -> None:
    repo = demo.repo
    doc, rec = pay_hazel_invoice_from_company_b(demo)
    (needs,) = [n for n in repo.open_needs() if n.kind == "company"]
    svc = BackOfficeService(demo)
    status, out = svc.dispatch("POST", f"/api/needs-you/{needs.id}/answer", {"optionId": "company:hazel-tree"})
    assert status == 200 and out["message"] == "Done. Hazel Tree carries it. Company B paid it on Hazel Tree's behalf."
    assert stage(demo, rec) is Stage.CLOSED and stage(demo, doc) is Stage.CLOSED
    assert rec.tx.entity_id == "hazel-tree" and rec.company_note == ("company-b", "hazel-tree", "hazel-tree")
    assert rec.company_answer_ev in repo.items[rec.item_id].history[-1].evidence_ids  # the answer is evidence
    for company in ("hazel-tree", "company-b"):
        flags = svc.accountant_client(company)["taxFlags"]
        assert any(f["title"] == "Inter-company payment · €246.00" and "owes Company B" in f["detail"]
                   for f in flags), company
    assert demo.auditor.recheck() == []


def test_the_paying_company_can_keep_it(demo: Orchestrator) -> None:
    repo = demo.repo
    doc, rec = pay_hazel_invoice_from_company_b(demo)
    (needs,) = [n for n in repo.open_needs() if n.kind == "company"]
    outcome = demo.answer(needs.id, "company:company-b")
    assert outcome.ok and stage(demo, rec) is Stage.CLOSED
    assert rec.tx.entity_id == "company-b" and doc.document.entity_id == "company-b"
    flags = BackOfficeService(demo).accountant_client("company-b")["taxFlags"]
    assert any(f["title"] == "Invoice addressed to another company · €246.00" for f in flags)


def test_the_right_company_still_closes_without_a_question(demo: Orchestrator) -> None:
    doc = demo.repo.documents[upload(demo, qr_document(
        "FT", "FT A/201", "ABCD1234-201", "246.00", "200.00", "46.00", "2026-09-10", title="Fatura")).document_ids[0]]
    demo.ingest_bank([bank("mbcp-0912-09", "mbcp-ht", date(2026, 9, 12), "-246.00", "PAPELARIA NORTE",
                           "TRF PAPELARIA NORTE")])
    assert stage(demo, doc) is Stage.CLOSED
    assert not [n for n in demo.repo.open_needs() if n.kind == "company"]


# --------------------------------------------------------------------------- 5. cash purchases


def cafe_receipt(payment_line: str = "Pagamento: Numerário") -> bytes:
    return qr_document("FS", "FS C1/88", "CAFE1234-88", "12.30", "10.00", "2.30", "2026-09-17",
                       title="Fatura simplificada", issuer=CAFE_NIF, issuer_name="Café Central, Lda.",
                       extra=("2 x Almoço executivo", payment_line))


def test_a_verified_cash_receipt_closes_as_a_cash_expense(demo: Orchestrator) -> None:
    repo = demo.repo
    doc = repo.documents[upload(demo, cafe_receipt(), "cafe.txt", scan=True).document_ids[0]]
    assert doc.paid_in_cash and doc.document.quality is Quality.GREEN
    assert stage(demo, doc) is Stage.CLOSED  # no bank line to wait for
    assert doc.document.entity_id == "hazel-tree" and repo.item_month(repo.items[doc.item_id]) == SEPT
    assert repo.items[doc.item_id] in repo.items_for("hazel-tree", SEPT)
    assert repo.activity[-1].text == "Recorded the €12.30 cash purchase at Café Central for Hazel Tree."
    ledger = Ledger(BackOfficeService(demo))
    line = next(x for x in ledger.lines if x.id == doc.id)
    assert line.paid_in_cash and line.company_id == "hazel-tree" and line.amount == Decimal("12.30")
    assert ledger.payment_label(line) == "Café Central · 17 September · €12.30 · paid in cash"
    assert ledger.payment_dict(line)["paidWith"] == "cash" and ledger.payment_dict(line)["invoice"] == "matched"
    spent = ledger.money(date(2026, 9, 1), date(2026, 9, 30), company_ids=["hazel-tree"])
    assert line in spent.lines
    assert demo.auditor.recheck() == []


@pytest.mark.parametrize("paid", ["Pago em dinheiro", "Numerário 5,00 Troco 1,80", "Paid in cash"])
def test_an_unverified_cash_receipt_is_one_plain_question_not_a_blocker(demo: Orchestrator, paid: str) -> None:
    repo = demo.repo
    text = (f"Pastelaria Aurora\nNIF: 506777880\nTalão n.º 4471\nData: 18/09/2026\nConsumidor final\n"
            f"2 x Café 1,60\nTotal: 3,20 €\n{paid}\n").encode()
    doc = repo.documents[upload(demo, text, "talao.txt", scan=True).document_ids[0]]
    assert doc.paid_in_cash and doc.document.quality is not Quality.GREEN
    assert stage(demo, doc) is Stage.NEEDS_OWNER  # not stuck at "I'm still checking 1 document"
    (needs,) = [n for n in repo.open_needs() if n.kind == "cash"]
    assert needs.prompt == ("I read a €3.20 cash payment at Pastelaria Aurora on 18 September. Is that right, "
                            "and which company paid it?")
    plain(needs.prompt, *needs.why, *(o.label for o in needs.options))
    svc = BackOfficeService(demo)
    assert any(i["id"] == needs.id and i["kind"] == "choice" for i in svc.needs_you()["items"])
    outcome = demo.answer(needs.id, "company:company-c")
    assert outcome.message == "Done. I counted it as a cash purchase for Company C."
    assert stage(demo, doc) is Stage.CLOSED and doc.document.entity_id == "company-c"
    assert doc.owner_confirmed in repo.items[doc.item_id].history[-1].evidence_ids
    assert demo.auditor.recheck() == []
    line = next(x for x in Ledger(svc).lines if x.id == doc.id)
    assert line.paid_in_cash and line.company_id == "company-c"


def test_a_cash_receipt_set_aside_never_blocks_the_month(demo: Orchestrator) -> None:
    repo = demo.repo
    text = ("Pastelaria Aurora\nNIF: 506777880\nTalão n.º 4472\nData: 18/09/2026\nConsumidor final\n"
            "Total: 3,20 €\nPago em dinheiro\n").encode()
    doc = repo.documents[upload(demo, text, scan=True).document_ids[0]]
    (needs,) = [n for n in repo.open_needs() if n.kind == "cash"]
    assert demo.answer(needs.id, "wrong").ok
    assert repo.items[doc.item_id].is_done and repo.items[doc.item_id].quality is Quality.GREEN
    assert not any(x.id == doc.id for x in Ledger(BackOfficeService(demo)).lines)


@pytest.mark.parametrize("payment", ["Pago com cartão •••• 4817", "Recheio Cash & Carry · Multibanco"])
def test_card_receipts_are_not_read_as_cash(demo: Orchestrator, payment: str) -> None:
    doc = demo.repo.documents[upload(demo, cafe_receipt(payment), scan=True).document_ids[0]]
    assert not doc.paid_in_cash and stage(demo, doc) is Stage.UNDERSTOOD  # it waits for its card payment
    ikea = next(d for d in demo.repo.documents.values() if d.document.supplier_tax_id == E.IKEA_NIF)
    assert not ikea.paid_in_cash


# --------------------------------------------------------------------------- 6. large and capital purchases


def machine_invoice(buyer: str = E.HAZEL_NIF) -> bytes:
    line = "Cliente: Hazel Tree Interiores, Lda." if buyer != FINAL_CONSUMER else "Consumidor final"
    return qr_document("FT", "FT MN/51", "MAQN1234-51", "22140.00", "18000.00", "4140.00", "2026-09-16",
                       title="Fatura", issuer=MAQUINARIA_NIF, issuer_name="Maquinaria Norte, Lda.", buyer=buyer,
                       buyer_line=line, extra=("1 x Máquina de corte CNC MX-400",))


def pay_machine(o: Orchestrator):  # type: ignore[no-untyped-def]
    o.ingest_bank([bank("mbcp-0918-09", "mbcp-ht", date(2026, 9, 18), "-22140.00", "MAQUINARIA NORTE LDA",
                        "TRF MAQUINARIA NORTE")])
    return tx_by(o, "TRF MAQUINARIA NORTE")


def test_a_large_first_purchase_that_checks_out_closes_and_is_flagged_for_the_accountant(demo: Orchestrator) -> None:
    doc = demo.repo.documents[upload(demo, machine_invoice()).document_ids[0]]
    rec = pay_machine(demo)
    assert rec.document_ids == [doc.id] and stage(demo, rec) is Stage.CLOSED and rec.hold_reason == ""
    svc = BackOfficeService(demo)
    flags = svc.accountant_client("hazel-tree")["taxFlags"]
    (flag,) = [f for f in flags if f["title"].startswith(CAPITAL_ASSET_FLAG)]
    assert flag["title"] == ("Possible equipment purchase (capital asset) — accountant to confirm · "
                             "€18,000.00 before VAT")
    assert "Maquinaria Norte invoice FT MN/51" in flag["detail"]
    assert not any("capital" in text.lower() for text in [n.prompt for n in demo.repo.open_needs()])  # no owner work


def test_a_large_first_purchase_without_the_buyers_tax_number_stays_amber(demo: Orchestrator) -> None:
    doc = demo.repo.documents[upload(demo, machine_invoice(FINAL_CONSUMER)).document_ids[0]]
    assert doc.document.quality is Quality.GREEN  # its own readings agree...
    rec = pay_machine(demo)
    assert rec.document_ids == [doc.id]  # ...and it matches the payment exactly...
    item = demo.repo.items[rec.item_id]
    assert item.stage is Stage.UNDERSTOOD and item.quality is Quality.AMBER  # ...yet it does not close
    assert rec.hold_reason == ("I'm holding the €22,140.00 Maquinaria Norte invoice: it is the first from this "
                               "supplier and a large amount, and it does not show Hazel Tree's tax number as the "
                               "buyer.")
    plain(rec.hold_reason)
    month = BackOfficeService(demo).month("hazel-tree", "2026-09")
    assert month is not None and any(r["text"] == rec.hold_reason for r in month["remaining"])
    assert "I'm still looking for 1 document." in demo.month_status("hazel-tree", SEPT).reasons()  # EDP only
    assert demo.auditor.recheck() == []


def test_equipment_wording_above_the_threshold_is_flagged_but_routine_receipts_are_not(demo: Orchestrator) -> None:
    laptop = qr_document("FT", "FT A/77", "ABCD1234-77", "1845.00", "1500.00", "345.00", "2026-09-15",
                         title="Fatura", extra=("1 x Computador portátil 14 polegadas",))
    doc = demo.repo.documents[upload(demo, laptop).document_ids[0]]
    demo.ingest_bank([bank("mbcp-0916-09", "mbcp-ht", date(2026, 9, 16), "-1845.00", "PAPELARIA NORTE",
                           "TRF PAPELARIA NORTE")])
    assert stage(demo, doc) is Stage.CLOSED  # below the high-value threshold: the usual rule
    flags = BackOfficeService(demo).accountant_client("hazel-tree")["taxFlags"]
    assert [f["id"] for f in flags if f["title"].startswith(CAPITAL_ASSET_FLAG)] == [f"t_asset_{doc.id}"]
    # the €18.75 taxi receipt closes as before and is never flagged
    uber = next(r for r in demo.repo.transactions.values() if r.tx.amount == Decimal("-18.75"))
    assert stage(demo, uber) is Stage.CLOSED
    assert not any(uber.document_ids[0] in f["id"] for f in flags)


def test_a_large_purchase_from_a_supplier_with_history_is_not_held(demo: Orchestrator) -> None:
    upload(demo, qr_document("FT", "FT A/60", "ABCD1234-60", "123.00", "100.00", "23.00", "2026-09-02",
                             title="Fatura"))
    big = demo.repo.documents[upload(demo, qr_document(
        "FT", "FT A/61", "ABCD1234-61", "6150.00", "5000.00", "1150.00", "2026-09-20", title="Fatura",
        buyer=FINAL_CONSUMER, buyer_line="Consumidor final")).document_ids[0]]
    demo.ingest_bank([bank("mbcp-0921-19", "mbcp-ht", date(2026, 9, 21), "-6150.00", "PAPELARIA NORTE",
                           "TRF PAPELARIA NORTE")])
    assert stage(demo, big) is Stage.CLOSED


# --------------------------------------------------------------------------- owner-facing words


_INTERNAL = re.compile(r"\b(?:ev|tx|doc|item|obl|rule)_[0-9a-f]{6,}|Traceback|Exception|None\b|\bnull\b")
_NOT_SHOWN = {"id", "href", "companyId", "evidenceIds", "confirmOptionId", "optionId", "currentMonth", "months",
              "month", "key", "at", "date", "due", "lastSyncedAt", "pendingItemIds", "evidence", "documents",
              "transactions", "tone", "kind", "status", "currency", "head", "taxId"}


def _owner_texts(value) -> list[str]:  # type: ignore[no-untyped-def]
    if isinstance(value, dict):
        return [t for k, v in value.items() if k not in _NOT_SHOWN for t in _owner_texts(v)]
    if isinstance(value, list):
        return [t for v in value for t in _owner_texts(v)]
    return [value] if isinstance(value, str) else []


def test_everything_the_owner_reads_about_these_cases_is_plain_english(demo: Orchestrator) -> None:
    upload(demo, edp_pro_forma())
    pay_hazel_invoice_from_company_b(demo)
    upload(demo, ("Pastelaria Aurora\nNIF: 506777880\nTalão n.º 4471\nData: 18/09/2026\nConsumidor final\n"
                  "Total: 3,20 €\nPago em dinheiro\n").encode(), scan=True)
    upload(demo, machine_invoice(FINAL_CONSUMER))
    pay_machine(demo)
    upload(demo, hazel_sale())
    svc = BackOfficeService(demo)
    assert {n.kind for n in demo.repo.open_needs()} >= {"company", "cash"}
    for path in ("/api/home", "/api/needs-you", "/api/activity", "/api/months/hazel-tree/2026-09",
                 "/api/months/company-b/2026-09"):
        status, body = svc.dispatch("GET", path, None)
        assert status == 200, path
        for text in _owner_texts(body):
            assert not _INTERNAL.search(text), text
            plain(text)
    assert "which company" in svc.ask("What still needs my attention?")["answer"].lower()


# --------------------------------------------------------------------------- the demo is unchanged


def test_demo_outcomes_are_unchanged() -> None:
    o = build_demo()
    svc = BackOfficeService(o)
    assert [n.id for n in o.repo.open_needs()] == ["nd_ikea_418", "nd_vodafone_iban"]
    assert o.month_status("company-b", SEPT).closed and not o.month_status("hazel-tree", SEPT).closed
    assert all(not d.supporting and not d.sales and not d.paid_in_cash for d in o.repo.documents.values())
    assert svc.accountant_client("hazel-tree")["taxFlags"] == [
        f for f in svc.accountant_client("hazel-tree")["taxFlags"] if f["title"].startswith("Rent paid")]
    assert o.auditor.recheck() == []
