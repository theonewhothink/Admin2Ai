"""Acceptance: supplier statements, owner statements, and costs recharged to clients (checklist X19, X25, X10).

1. Supplier statements (X19; cases 2, 3, 34, 46): a supplier's account statement (extrato de conta
   corrente) is read line by line (Portuguese and English text, CSV) and checked against the business's
   own invoices, credit notes and bank payments from that supplier. Documents it lists that the business
   never received are missing documents, asked for from the supplier (counted as asked only once a
   transport accepted the email); the business's own documents it does not list are shown for the
   accountant; an amount that differs is one plain question, never a silent change; the closing balance is
   checked against the invoices not paid yet. The statement itself is never booked and never duplicates
   an invoice.
2. Owner statements (X25; cases 17, 20): for a property, apartment or house, a period statement: rent and
   other money received, costs with their evidence, the management fee set on it, and what is due to the
   owner. A statement with anything still open says so and is not final.
3. Reimbursable costs and client money (X10; cases 15, 21, 23, 26, 32): a cost on a client can be the
   client's to pay back (by the owner's word, by rule, by the cost center's setting, or from the invoice
   itself). It is left out of the business's own costs and reported separately; money the client pays back
   is matched to it; a travel agency's client money is not revenue. One plain question only when it
   genuinely cannot be told.
"""

from __future__ import annotations

import base64
import csv
import io
import zipfile
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import pytest

from backoffice.closure import ActivityKind as ClosureKind
from backoffice.countries.pt.nif import validate_nif
from backoffice.demo import build_demo
from backoffice.demo import evidence as E
from backoffice.domain.lifecycle import Stage
from backoffice.domain.models import AllocationMethod, DocumentType, Supplier, TransactionKind
from backoffice.language import find_jargon, find_off_tone
from backoffice.learning import RuleField, RuleOutcome
from backoffice.mailer import SimulatedOutbox
from backoffice.orchestrator import TZ, BankRow, Orchestrator
from backoffice.service import BackOfficeService
from backoffice.spending import Ledger
from backoffice.supplier_statements import (
    CREDIT_NOTE,
    INVOICE,
    PAYMENT,
    OurDocument,
    OurPayment,
    check_statement,
    read_statement,
)

NORTE_NIF = "509123457"  # Papelaria Norte, a supplier of Hazel Tree (fictional, valid check digit)
SEPT = "2026-09"


def plain(*texts: str) -> None:
    for text in texts:
        assert not find_jargon(text), (text, find_jargon(text))
        assert not find_off_tone(text), text


def ok(result: tuple[int, dict[str, Any]], status: int = 200) -> dict[str, Any]:
    code, body = result
    assert code == status, body
    return body


def get(svc: BackOfficeService, path: str, status: int = 200) -> dict[str, Any]:
    return ok(svc.dispatch("GET", path, None), status)


def post(svc: BackOfficeService, path: str, body: dict[str, Any], status: int = 200) -> dict[str, Any]:
    return ok(svc.dispatch("POST", path, body), status)


def ask(svc: BackOfficeService, question: str) -> dict[str, Any]:
    return post(svc, "/api/ask", {"question": question})


# --------------------------------------------------------------------------- evidence builders


def _pt(value: str) -> str:
    whole, cents = value.split(".")
    groups = []
    while len(whole) > 3:
        groups.insert(0, whole[-3:])
        whole = whole[:-3]
    return ".".join([whole, *groups]) + "," + cents


def norte_document(code: str, number: str, total: str, net: str, vat: str, day: str, *, title: str = "Fatura",
                   extra: tuple[str, ...] = ()) -> bytes:
    """A Papelaria Norte document for Hazel Tree: its text layer and its AT fiscal QR code."""
    seq = number.rsplit("/", 1)[1]
    payload = E.qr_payload(A=NORTE_NIF, B=E.HAZEL_NIF, C="PT", D=code, E="N", F=day.replace("-", ""), G=number,
                           H=f"NRTQ7K2M-{seq}", I1="PT", I7=net, I8=vat, N=vat, O=total, Q="ab12", R="1234")
    issued = f"{day[8:10]}/{day[5:7]}/{day[0:4]}"
    return E._invoice_text(
        ["Papelaria Norte, Lda.", f"NIF: {NORTE_NIF}", f"{title} n.º {number}", f"ATCUD: NRTQ7K2M-{seq}",
         f"Data de emissão: {issued}"],
        ["Cliente: Hazel Tree Interiores, Lda.", f"NIF: {E.HAZEL_NIF}", *extra, f"Base tributável (23%): {_pt(net)}",
         f"IVA 23%: {_pt(vat)}", f"Total: {_pt(total)} €"],
        payload,
    )


def norte_statement(*rows: str, closing: str | None = None, period: str = "01/09/2026 a 30/09/2026") -> bytes:
    """Papelaria Norte's account statement for Hazel Tree, as its PDF's text layer shows it."""
    head = ["Papelaria Norte, Lda.", f"NIF: {NORTE_NIF}", "Extrato de conta corrente",
            "Cliente: Hazel Tree Interiores, Lda.", f"NIF: {E.HAZEL_NIF}", f"Período: {period}",
            "Data        Documento     Descrição             Débito     Crédito     Saldo",
            "01/09/2026                Saldo anterior                                0,00"]
    tail = [f"Saldo final: {closing} €"] if closing is not None else []
    return "\n".join([*head, *rows, *tail, ""]).encode()


def bank(bank_id: str, day: date, amount: str, who: str = "PAPELARIA NORTE", description: str = "TRF PAPELARIA NORTE",
         account: str = "mbcp-ht") -> BankRow:
    kind = TransactionKind.TRANSFER_OUT if Decimal(amount) < 0 else TransactionKind.TRANSFER_IN
    return BankRow(bank_id=bank_id, account_id=account, booked_on=day, amount=Decimal(amount), counterparty=who,
                   description=description, kind=kind)


def upload(o: Orchestrator, data: bytes, name: str = "document.txt", content_type: str = "text/plain"):  # type: ignore[no-untyped-def]
    return o.ingest_file(data, filename=name, content_type=content_type)


def stage(o: Orchestrator, record: Any) -> Stage:
    return o.repo.items[record.item_id].stage


def doc_by_number(o: Orchestrator, number: str) -> Any:
    return next(d for d in o.repo.documents.values() if d.document.invoice_number == number)


@pytest.fixture
def norte() -> Orchestrator:
    """The demo tenant (Hazel Tree ...) with one more supplier, Papelaria Norte, and its September documents:
    FT A/101 (€246.00, paid on 8 September), FT A/102 (€123.00, not paid yet) and credit note NC A/12
    (€24.60) for FT A/102."""
    o = build_demo()
    o.repo.add_supplier(Supplier(id="sup-norte", tenant_id=o.repo.tenant_id, name="Papelaria Norte",
                                 aliases=["PAPELARIA NORTE"], tax_id=NORTE_NIF, countries=["PT"],
                                 contact_email="faturas@papelarianorte.pt", email_domains=["papelarianorte.pt"]))
    upload(o, norte_document("FT", "FT A/101", "246.00", "200.00", "46.00", "2026-09-03"), "ft101.txt")
    o.ingest_bank([bank("norte-0908", date(2026, 9, 8), "-246.00")])
    upload(o, norte_document("FT", "FT A/102", "123.00", "100.00", "23.00", "2026-09-10"), "ft102.txt")
    upload(o, norte_document("NC", "NC A/12", "24.60", "20.00", "4.60", "2026-09-12", title="Nota de crédito",
                             extra=("Referente à fatura FT A/102",)), "nc12.txt")
    return o


SEPTEMBER_ROWS = (
    "03/09/2026  FT A/101      Fatura                246,00                  246,00",
    "09/09/2026  RE 2026/55    Recibo                             246,00      0,00",
    "10/09/2026  FT A/102      Fatura                130,00                  130,00",
    "12/09/2026  NC A/12       Nota de crédito                     24,60    105,40",
    "15/09/2026  FT A/103      Fatura                 80,00                  185,40",
)


# --------------------------------------------------------------------------- 1. reading statements


def test_statement_lines_are_read_from_portuguese_english_and_csv_layouts() -> None:
    pt = read_statement(norte_statement(*SEPTEMBER_ROWS, closing="185,40").decode())
    assert pt is not None and pt.layout == "text" and pt.adds_up
    assert [(x.number, x.kind) for x in pt.lines] == [("FT A/101", INVOICE), ("RE 2026/55", PAYMENT),
                                                      ("FT A/102", INVOICE), ("NC A/12", CREDIT_NOTE),
                                                      ("FT A/103", INVOICE)]
    assert [x.debit for x in pt.lines] == [Decimal("246.00"), 0, Decimal("130.00"), 0, Decimal("80.00")]
    assert [x.credit for x in pt.lines] == [0, Decimal("246.00"), 0, Decimal("24.60"), 0]
    assert pt.lines[3].balance == Decimal("105.40") and pt.lines[0].on == date(2026, 9, 3)
    assert (pt.opening, pt.closing) == (Decimal("0.00"), Decimal("185.40"))
    assert (pt.start, pt.end) == (date(2026, 9, 1), date(2026, 9, 30))

    en = read_statement("\n".join([
        "Vodafone Portugal", "Statement of account", "Customer: Hazel Tree Interiores, Lda.",
        "Period: 01/09/2026 to 30/09/2026",
        "Date        Reference      Description       Debit      Credit     Balance",
        "Opening balance                                                     0.00",
        "05/09/2026  INV-88001      Invoice           92.40                  92.40",
        "18/09/2026  PMT-5501       Payment                      92.40       0.00",
        "24/09/2026  INV-88340      Invoice           1,092.40               1,092.40",
        "Closing balance                                                     1,092.40"]))
    assert en is not None and en.adds_up and en.closing == Decimal("1092.40")
    assert [(x.number, x.kind, x.amount) for x in en.lines] == [
        ("INV-88001", INVOICE, Decimal("92.40")), ("PMT-5501", PAYMENT, Decimal("92.40")),
        ("INV-88340", INVOICE, Decimal("1092.40"))]

    exported = read_statement("\n".join([
        f"Extrato de conta corrente;Papelaria Norte, Lda.;NIF {NORTE_NIF}",
        "Data;Documento;Descrição;Débito;Crédito;Saldo",
        "01/09/2026;;Saldo anterior;;;0,00",
        "03/09/2026;FT A/101;Fatura;246,00;;246,00",
        "09/09/2026;RE 2026/55;Recibo;;246,00;0,00",
        "15/09/2026;FT A/103;Fatura;80,00;;80,00"]))
    assert exported is not None and exported.layout == "csv" and exported.adds_up
    assert [(x.number, x.kind, x.effect) for x in exported.lines] == [
        ("FT A/101", INVOICE, Decimal("246.00")), ("RE 2026/55", PAYMENT, Decimal("-246.00")),
        ("FT A/103", INVOICE, Decimal("80.00"))]
    assert exported.closing == Decimal("80.00")

    # Kept "the other way round" (invoices as credits, the balance negative): turned the right way.
    inverted = read_statement("Extrato de conta corrente\nData;Documento;Descrição;Débito;Crédito;Saldo\n"
                              "03/09/2026;FT A/101;Fatura;;246,00;-246,00\n"
                              "09/09/2026;RE 2026/55;Pagamento;246,00;;0,00\n")
    assert inverted is not None and [x.effect for x in inverted.lines] == [Decimal("246.00"), Decimal("-246.00")]
    assert inverted.closing == 0 and str(inverted.closing) == "0.00"

    # No opening balance printed: it follows from the first line's balance.
    carried = read_statement("Statement of account\n03/09/2026  INV-1  Invoice  246.00  1,246.00\n"
                             "09/09/2026  PMT-2  Payment  1,000.00  246.00\n")
    assert carried is not None and carried.adds_up and (carried.opening, carried.closing) == (1000, 246)
    assert [x.effect for x in carried.lines] == [Decimal("246.00"), Decimal("-1000.00")]

    # A statement whose running balance does not follow is read, but reported as not adding up.
    broken = read_statement(norte_statement("03/09/2026  FT A/101      Fatura     246,00      250,00").decode())
    assert broken is not None and not broken.adds_up
    assert broken.problems == ("The balance on line 1 does not follow from the line before.",)
    plain(*broken.problems)

    # An invoice is never taken for a statement, and a bank export is never a supplier's statement.
    assert read_statement(norte_document("FT", "FT A/101", "246.00", "200.00", "46.00", "2026-09-03").decode()) is None
    assert read_statement("Data;Descrição;Débito;Crédito;Saldo\n03/09/2026;COMPRA;10,00;;90,00\n") is None


def test_statement_is_checked_line_by_line_and_never_booked_or_duplicated(norte: Orchestrator) -> None:
    o, repo = norte, norte.repo
    ft101, ft102, nc12 = (doc_by_number(o, n) for n in ("FT A/101", "FT A/102", "NC A/12"))
    paid = next(r for r in repo.transactions.values() if r.tx.description == "TRF PAPELARIA NORTE")
    assert paid.document_ids == [ft101.id] and stage(o, paid) is Stage.CLOSED
    assert nc12.credit_for == ft102.id
    documents_before = {d.id for d in repo.documents.values()}
    ledger_before = [(x.id, x.amount, x.kind) for x in Ledger(BackOfficeService(o)).lines]

    report = upload(o, norte_statement(*SEPTEMBER_ROWS, closing="185,40"), "extrato_norte_setembro.txt")
    assert report.message == ("Got it. This is Papelaria Norte's account statement. It is never booked: I check its "
                              "5 lines against your invoices and payments.")
    statement = repo.documents[report.document_ids[0]]
    assert statement.document.doc_type is DocumentType.SUPPLIER_STATEMENT and statement.supporting
    assert statement.document.invoice_number is None and statement.document.gross_amount is None
    assert stage(o, statement) is Stage.NOT_REQUIRED
    assert statement.matched_tx_ids == [] and statement.supports_tx_ids == []
    assert {d.id for d in repo.documents.values()} == documents_before | {statement.id}  # nothing merged, no copies
    assert ft101.evidence_ids == [ft101.evidence_ids[0]] and paid.document_ids == [ft101.id]
    assert stage(o, paid) is Stage.CLOSED and statement.document.cost_allocation is None

    check = repo.statements[statement.id].check
    assert check is not None
    assert [(r.line.number, r.status) for r in check.rows] == [
        ("FT A/101", "matched"), ("RE 2026/55", "matched"), ("FT A/102", "differs"), ("NC A/12", "matched"),
        ("FT A/103", "missing")]
    rows = {r.line.number: r for r in check.rows}
    assert rows["FT A/101"].document_id == ft101.id and rows["NC A/12"].document_id == nc12.id
    assert rows["RE 2026/55"].transaction_id == paid.id  # booked by the supplier a day after it left the bank
    assert rows["FT A/102"].document_id == ft102.id and rows["FT A/102"].our_amount == Decimal("123.00")
    # What you owe by your own records: FT A/102 less its credit note.
    assert check.our_open == Decimal("98.40") and check.closing == Decimal("185.40")
    assert check.explained == "The difference of €87.00 is the documents I don't have and the amount difference."
    assert not check.complete

    # Never booked: not in the spending ledger, and supporting evidence for the accountant.
    assert [(x.id, x.amount, x.kind) for x in Ledger(BackOfficeService(o)).lines] == ledger_before
    svc = BackOfficeService(o)
    listed = next(d for d in svc.documents_list({"company": "hazel-tree"})["items"] if d["id"] == statement.id)
    assert listed["booking"] == "supporting" and listed["type"] == "supplier statement" and listed["amount"] is None
    exported = svc.documents_export({"company": "hazel-tree"})
    with zipfile.ZipFile(io.BytesIO(base64.b64decode(exported["data"]))) as z:
        ledger_rows = list(csv.DictReader(io.StringIO(z.read("ledger.csv").decode("utf-8-sig")), delimiter=";"))
    row = next(r for r in ledger_rows if r["type"] == "supplier statement")
    assert row["booking"] == "supporting" and row["file"].startswith("documents/supporting/")
    assert o.auditor.recheck() == []

    # The same statement again is the same document.
    again = upload(o, norte_statement(*SEPTEMBER_ROWS, closing="185,40"), "extrato_norte_setembro.txt")
    assert again.document_ids == [statement.id] and len(repo.statements) == 1


def test_documents_on_the_statement_we_do_not_have_are_missing_and_asked_for_once(norte: Orchestrator) -> None:
    o, repo = norte, norte.repo
    statement = repo.documents[upload(o, norte_statement(*SEPTEMBER_ROWS, closing="185,40")).document_ids[0]]
    sr = repo.statements[statement.id]
    assert sr.company_id == "hazel-tree" and sr.supplier_id == "sup-norte"
    # One email for the one document it lists that never arrived; the demo's transport accepted it.
    message = repo.outbox[sr.request_id]
    assert message.kind == "statement_request" and message.to == "faturas@papelarianorte.pt" and message.sent
    assert message.subject.startswith("Extrato de conta corrente: documentos em falta (Ref. ")
    assert "- Fatura FT A/103 de 15 de setembro, 80,00 €" in message.body
    assert "FT A/102" not in message.body and "NIF 516123459" in message.body
    assert sr.requested == ("invoice:FTA103",)
    accepted = [m for m in o.transport.accepted if m.to == ("faturas@papelarianorte.pt",)]
    assert len(accepted) == 1 and accepted[0].subject == message.subject
    chased = [a.text for a in repo.activity if a.kind == "chased" and "Papelaria" in a.text]
    assert chased == ["Asked Papelaria Norte for invoice FT A/103, which its statement lists but I don't have."]
    detected = [a for a in repo.closure_log if a.kind is ClosureKind.MISSING_DOCUMENT_DETECTED
                and (a.subject_id or "").startswith(statement.id)]
    assert [a.subject_id for a in detected] == [f"{statement.id}:invoice:FTA103"]
    assert any(a.kind is ClosureKind.SUPPLIER_CHASED and a.subject_id == "sup-norte" for a in repo.closure_log)
    plain(*chased)

    # Asked once: later runs and a second look never write again.
    o.run()
    o.run()
    assert [m.id for m in repo.outbox.values() if m.kind == "statement_request"] == [sr.request_id]

    # The invoice arrives: its line matches, and the missing document counts as retrieved.
    upload(o, norte_document("FT", "FT A/103", "80.00", "65.04", "14.96", "2026-09-15"), "ft103.txt")
    check = sr.check
    assert check is not None and [r.status for r in check.rows] == ["matched", "matched", "differs", "matched",
                                                                      "matched"]
    assert any(a.kind is ClosureKind.MISSING_DOCUMENT_RETRIEVED and a.subject_id == f"{statement.id}:invoice:FTA103"
               for a in repo.closure_log)
    assert check.our_open == Decimal("178.40") and check.difference == Decimal("7.00")
    assert check.explained == "The difference of €7.00 is the amount difference on the statement."


def test_a_statement_request_counts_as_asked_only_when_a_transport_accepts_it(norte: Orchestrator) -> None:
    o, repo = norte, norte.repo
    o.transport = None  # nothing can send email
    svc = BackOfficeService(o)
    chased_before = svc.month("hazel-tree", SEPT)["stats"]["suppliersChased"]  # type: ignore[index]
    statement = repo.documents[upload(o, norte_statement(*SEPTEMBER_ROWS, closing="185,40")).document_ids[0]]
    sr = repo.statements[statement.id]
    message = repo.outbox[sr.request_id]
    assert message.status == "waiting" and not message.sent
    texts = [a.text for a in repo.activity if "Papelaria" in a.text]
    assert "Wrote to Papelaria Norte asking for invoice FT A/103, which its statement lists but I don't have. It is " \
           "waiting to be sent." in texts
    assert not any(a.startswith("Asked Papelaria") for a in texts)
    assert not any(a.kind is ClosureKind.SUPPLIER_CHASED and a.subject_id == "sup-norte" for a in repo.closure_log)
    assert svc.month("hazel-tree", SEPT)["stats"]["suppliersChased"] == chased_before  # type: ignore[index]
    detail = get(svc, f"/api/documents/{statement.id}")["statement"]
    assert detail["request"] == {"status": "waiting", "to": "faturas@papelarianorte.pt",
                                 "text": "I wrote to Papelaria Norte asking for it. It is waiting to be sent."}
    assert "I asked" not in detail["summary"]
    assert sr.request_id in svc.waiting_messages()

    # Sent now: only now is the supplier asked.
    transport = SimulatedOutbox()
    assert o.send_waiting(sr.request_id, transport)
    assert message.sent and len(transport.accepted) == 1
    assert any(a.kind is ClosureKind.SUPPLIER_CHASED and a.subject_id == "sup-norte" for a in repo.closure_log)
    assert svc.month("hazel-tree", SEPT)["stats"]["suppliersChased"] == chased_before + 1  # type: ignore[index]
    detail = get(svc, f"/api/documents/{statement.id}")["statement"]
    assert detail["request"]["status"] == "sent" and detail["request"]["text"] == "I asked Papelaria Norte for it."
    plain(detail["request"]["text"], *texts)


def test_an_amount_difference_is_one_plain_question_never_a_silent_change(norte: Orchestrator) -> None:
    o, repo = norte, norte.repo
    ft102 = doc_by_number(o, "FT A/102")
    statement = repo.documents[upload(o, norte_statement(*SEPTEMBER_ROWS, closing="185,40")).document_ids[0]]
    sr = repo.statements[statement.id]
    asked = [n for n in repo.open_needs() if n.kind == "statement"]
    assert len(asked) == 1 and asked[0].id == sr.needs_id and asked[0].subject_id == statement.id
    need = asked[0]
    assert need.prompt == ("Papelaria Norte's statement shows invoice FT A/102 as €130.00, but the invoice says "
                           "€123.00. Which is right?")
    assert [(x.id, x.label) for x in need.options] == [("document", "The invoice: €123.00"),
                                                      ("statement", "The statement: €130.00")]
    assert need.why == ("Invoice FT A/102: €130.00 on the statement, €123.00 on the invoice you have.",
                        "I never change an amount without you. If the statement is right, I will ask them for a "
                        "corrected document.")
    svc = BackOfficeService(o)
    item = next(i for i in get(svc, "/api/needs-you")["items"] if i["id"] == need.id)
    assert item["kind"] == "choice" and item["merchant"] == "Papelaria Norte" and item["amount"] == 130
    assert item["question"] == need.prompt and item["date"] == "2026-09-30"
    attention = svc._attention_answer()
    assert need.prompt in attention["answer"]
    assert {"label": "Papelaria Norte · statement to check", "id": f"needs:{need.id}"} in attention["evidence"]
    fixes = {f["id"]: f for f in svc.internal("overview")["fixes"]}
    fix = fixes[f"{repo.tenant_id}:{need.id}"]
    assert fix["label"] == "Papelaria Norte €130.00: waiting for the owner" and fix["detail"] == need.prompt
    assert post(svc, f"/api/needs-you/{need.id}/answer", {"option_id": "nope"}, status=400)["message"] == \
        "Please pick one of the options."
    assert ft102.document.gross_amount == Decimal("123.00")  # nothing changed while it waits

    out = post(svc, f"/api/needs-you/{need.id}/answer", {"option_id": "document"})
    assert out["message"] == ("Done. The invoice stays as it is. I noted the difference on Papelaria Norte's statement "
                              "for your accountant.")
    assert ft102.document.gross_amount == Decimal("123.00") and sr.answer == "document"
    assert not [n for n in repo.open_needs() if n.kind == "statement"]
    o.run()
    assert not [n for n in repo.open_needs() if n.kind == "statement"]  # answered once, never asked again
    row = next(r for r in get(svc, f"/api/documents/{statement.id}")["statement"]["lines"] if r["number"] == "FT A/102")
    assert row["status"] == "differs"
    assert row["text"] == ("Invoice FT A/102: €130.00 here, €123.00 on the invoice you have. You said the invoice "
                           "is right.")
    plain(need.prompt, *need.why, *(x.label for x in need.options), out["message"], row["text"])


def test_when_the_statement_is_right_the_supplier_is_asked_for_a_corrected_document(norte: Orchestrator) -> None:
    o, repo = norte, norte.repo
    ft102 = doc_by_number(o, "FT A/102")
    statement = repo.documents[upload(o, norte_statement(*SEPTEMBER_ROWS, closing="185,40")).document_ids[0]]
    sr = repo.statements[statement.id]
    svc = BackOfficeService(o)
    out = post(svc, f"/api/needs-you/{sr.needs_id}/answer", {"option_id": "statement"})
    assert out["message"] == ("Done. Nothing changes until the corrected document arrives. I will ask Papelaria Norte "
                              "for it.")
    assert ft102.document.gross_amount == Decimal("123.00")  # never a silent change
    correction = repo.outbox[sr.correction_id]
    assert correction.kind == "statement_correction" and correction.sent
    assert correction.subject.startswith("Extrato de conta corrente: documentos com valores diferentes (Ref. ")
    assert "- Fatura FT A/102 de 10 de setembro: no extrato 130,00 €, na fatura que recebemos 123,00 €" in \
        correction.body
    assert any(a.text == "Asked Papelaria Norte for corrected documents where its statement differs."
               for a in repo.activity)
    detail = get(svc, f"/api/documents/{statement.id}")["statement"]
    assert detail["correction"] == {"status": "sent", "to": "faturas@papelarianorte.pt"}
    plain(out["message"])


def test_our_documents_missing_from_the_statement_and_the_closing_balance_are_shown_for_the_accountant(
        norte: Orchestrator) -> None:
    o, repo = norte, norte.repo
    upload(o, norte_document("FT", "FT A/104", "61.50", "50.00", "11.50", "2026-09-25"), "ft104.txt")
    # A statement that agrees with everything but does not list FT A/104.
    statement = repo.documents[upload(o, norte_statement(
        "03/09/2026  FT A/101      Fatura                246,00                  246,00",
        "09/09/2026  RE 2026/55    Recibo                             246,00      0,00",
        "10/09/2026  FT A/102      Fatura                123,00                  123,00",
        "12/09/2026  NC A/12       Nota de crédito                     24,60     98,40",
        closing="98,40")).document_ids[0]]
    check = repo.statements[statement.id].check
    assert check is not None
    assert [r.status for r in check.rows] == ["matched"] * 4 and not check.missing and not check.differences
    ft104 = doc_by_number(o, "FT A/104")
    assert [d.id for d in check.not_on_statement] == [ft104.id]
    assert check.our_open == Decimal("159.90") and check.closing == Decimal("98.40") and not check.balance_agrees
    assert not [n for n in repo.open_needs() if n.kind == "statement"]  # nothing to ask: shown for the accountant
    svc = BackOfficeService(o)
    view = get(svc, f"/api/documents/{statement.id}")["statement"]
    assert view["notOnStatementText"] == ("Not on Papelaria Norte's statement, although it covers their dates: "
                                          "Invoice FT A/104 · €61.50.")
    assert [d["id"] for d in view["notOnStatement"]] == [ft104.id]
    assert view["notOnStatement"][0]["evidence"][0]["id"] == ft104.evidence_ids[0]
    assert view["balance"] == {"statement": 98.4, "ours": 159.9, "difference": -61.5, "agrees": False,
                               "text": "It says you owe €98.40; the invoices you have not paid yet come to €159.90."}
    assert view["request"] is None and view["complete"] is False
    anomalies = {a["id"]: a for a in get(svc, "/api/accountant/clients/hazel-tree")["anomalies"]}
    flagged = anomalies[f"an_statement_{statement.id}"]
    assert flagged["title"] == "Papelaria Norte's statement differs from the records"
    assert flagged["detail"].endswith(view["notOnStatementText"]) and flagged["tone"] == "attention"
    plain(view["notOnStatementText"], view["balance"]["text"], view["summary"], flagged["title"], flagged["detail"])

    # A statement that matches every line and the balance is complete: nothing to do.
    o2 = build_demo()
    o2.repo.add_supplier(repo.suppliers["sup-norte"])
    upload(o2, norte_document("FT", "FT A/101", "246.00", "200.00", "46.00", "2026-09-03"), "ft101.txt")
    o2.ingest_bank([bank("norte-0908", date(2026, 9, 8), "-246.00")])
    upload(o2, norte_document("FT", "FT A/102", "123.00", "100.00", "23.00", "2026-09-10"), "ft102.txt")
    done = o2.repo.documents[upload(o2, norte_statement(
        "03/09/2026  FT A/101      Fatura                246,00                  246,00",
        "09/09/2026  RE 2026/55    Recibo                             246,00      0,00",
        "10/09/2026  FT A/102      Fatura                123,00                  123,00",
        closing="123,00")).document_ids[0]]
    check2 = o2.repo.statements[done.id].check
    assert check2 is not None and check2.complete
    view2 = get(BackOfficeService(o2), f"/api/documents/{done.id}")["statement"]
    assert view2["complete"] is True and view2["balance"]["agrees"] is True
    assert view2["summary"] == ("Papelaria Norte's statement matches your records: 3 lines, all found, and the "
                                "balance of €123.00 is what you have not paid yet.")
    assert view2["balance"]["text"] == "The balance of €123.00 matches the invoices you have not paid yet."
    assert not [m for m in o2.repo.outbox.values() if m.kind.startswith("statement")]
    assert not [n for n in o2.repo.open_needs() if n.kind == "statement"]


def test_a_paid_document_missing_from_the_statement_is_asked_for_with_its_payment() -> None:
    o = build_demo()
    o.repo.add_supplier(Supplier(id="sup-norte", tenant_id=o.repo.tenant_id, name="Papelaria Norte",
                                 aliases=["PAPELARIA NORTE"], tax_id=NORTE_NIF, countries=["PT"],
                                 contact_email="faturas@papelarianorte.pt"))
    rows = ("29/09/2026  FT A/105      Fatura                 80,00                   80,00",
            "30/09/2026  RE 2026/61    Recibo                              80,00      0,00")
    # Paid two days ago, without its invoice: not missing yet (invoices follow payments by a day or two).
    o.ingest_bank([bank("norte-0930", date(2026, 9, 30), "-80.00")])
    rec = next(r for r in o.repo.transactions.values() if r.tx.description == "TRF PAPELARIA NORTE")
    sr = o.repo.statements[upload(o, norte_statement(*rows, closing="0,00")).document_ids[0]]
    assert sr.check is not None and [r.status for r in sr.check.rows] == ["missing", "matched"]
    assert sr.check.rows[1].transaction_id == rec.id and sr.check.balance_agrees
    # Already paid from the bank: it is asked for with that payment, never twice.
    assert not sr.request_id and not [m for m in o.repo.outbox.values() if m.kind == "statement_request"]
    assert rec.id not in o.repo.chases
    o.run(datetime(2026, 10, 5, 9, 0, tzinfo=TZ))
    chase = o.repo.chases[rec.id]
    assert chase.message.subject.startswith("Fatura FT A/105 (Ref. ")
    assert "Poderiam, por favor, reenviar a fatura FT A/105 referente ao pagamento de 80,00 € de 30 de setembro?" \
        in chase.message.body
    assert not [m for m in o.repo.outbox.values() if m.kind == "statement_request"]

    # Nothing paid for it: the statement's own request asks for it.
    o2 = build_demo()
    o2.repo.add_supplier(o.repo.suppliers["sup-norte"])
    sr2 = o2.repo.statements[upload(o2, norte_statement(rows[0], closing="80,00")).document_ids[0]]
    assert sr2.request_id and o2.repo.outbox[sr2.request_id].kind == "statement_request"


def test_the_statement_check_is_in_the_document_detail_and_in_ask(norte: Orchestrator) -> None:
    o, repo = norte, norte.repo
    statement = repo.documents[upload(o, norte_statement(*SEPTEMBER_ROWS, closing="185,40")).document_ids[0]]
    svc = BackOfficeService(o)
    detail = get(svc, f"/api/documents/{statement.id}")
    assert detail["type"] == "supplier statement" and detail["booking"] == "supporting"
    assert detail["evidence"] == [{"label": "Papelaria Norte · Account statement", "id": statement.evidence_ids[0]}]
    view = detail["statement"]
    assert view["booked"] is False and view["supplier"] == "Papelaria Norte" and view["supplierKnown"] is True
    assert (view["from"], view["to"], view["opening"], view["closing"], view["addsUp"]) == (
        "2026-09-01", "2026-09-30", 0, 185.4, True)
    assert [(r["number"], r["status"]) for r in view["lines"]] == [
        ("FT A/101", "matched"), ("RE 2026/55", "matched"), ("FT A/102", "differs"), ("NC A/12", "matched"),
        ("FT A/103", "missing")]
    assert [r["text"] for r in view["lines"]] == [
        "Invoice FT A/101 · €246.00: you have it.",
        "Payment RE 2026/55 of €246.00: in your bank on 8 September.",
        "Invoice FT A/102: €130.00 here, €123.00 on the invoice you have.",
        "Credit note NC A/12 · €24.60: you have it.",
        "Invoice FT A/103 · €80.00: you don't have it."]
    ft101 = doc_by_number(o, "FT A/101")
    assert view["lines"][0]["document"] == {"id": ft101.id, "label": ft101.label}
    assert view["lines"][0]["evidence"][0]["id"] == ft101.evidence_ids[0]
    assert view["lines"][1]["payment"]["label"].startswith("Transfer 8 September · €246.00")
    assert [r["number"] for r in view["missing"]] == ["FT A/103"]
    assert [r["number"] for r in view["differences"]] == ["FT A/102"]
    assert view["needsYouId"] == repo.statements[statement.id].needs_id
    assert view["summary"] == (
        "Papelaria Norte's statement: 3 of its 5 lines match your records. It lists a document I don't have: "
        "invoice FT A/103 of 15 September (€80.00). It shows invoice FT A/102 as €130.00, but the invoice says "
        "€123.00. It says you owe €185.40; the invoices you have not paid yet come to €98.40. The difference of "
        "€87.00 is the documents I don't have and the amount difference. I asked Papelaria Norte for it.")
    plain(view["summary"], view["note"], *(r["text"] for r in view["lines"]), view["balance"]["text"])

    # An invoice's detail: what it is matched to, no statement.
    invoice_detail = get(svc, f"/api/documents/{ft101.id}")
    assert "statement" not in invoice_detail and invoice_detail["status"] == "matched"
    assert [p["id"] for p in invoice_detail["payments"]] == [
        next(r for r in repo.transactions.values() if r.tx.description == "TRF PAPELARIA NORTE").evidence_id]
    assert get(svc, "/api/documents/doc_nope", status=404)["message"] == "I can't find that document."

    # Ask: "Does our ... statement match?"
    reply = ask(svc, "Does our Papelaria Norte statement match?")
    assert reply["answer"] == view["summary"]
    ids = [e["id"] for e in reply["evidence"]]
    assert ids[0] == statement.evidence_ids[0] and doc_by_number(o, "FT A/102").evidence_ids[0] in ids
    assert f"needs:{view['needsYouId']}" in ids
    assert ask(svc, "Does the Vodafone statement match?")["answer"] == \
        "I don't have an account statement from Vodafone."
    assert "statement" not in ask(svc, "Show me the bank statement for September").get("answer", "")[:30]
    plain(reply["answer"])


def test_csv_and_english_statements_without_tax_numbers_are_still_read() -> None:
    o = build_demo()
    o.transport = None
    o.repo.add_supplier(Supplier(id="sup-norte", tenant_id=o.repo.tenant_id, name="Papelaria Norte",
                                 aliases=["PAPELARIA NORTE"], tax_id=NORTE_NIF, countries=["PT"],
                                 contact_email="faturas@papelarianorte.pt"))
    exported = "\n".join([
        f"Extrato de conta corrente;Papelaria Norte, Lda.;NIF {NORTE_NIF}",
        f"Cliente;Hazel Tree Interiores, Lda.;NIF {E.HAZEL_NIF}",
        "Data;Documento;Descrição;Débito;Crédito;Saldo",
        "01/09/2026;;Saldo anterior;;;0,00",
        "15/09/2026;FT A/103;Fatura;80,00;;80,00", ""]).encode()
    report = o.ingest_file(exported, filename="extrato_norte.csv", content_type="text/csv")
    assert report.transaction_ids == [] and len(report.document_ids) == 1
    sr = o.repo.statements[report.document_ids[0]]
    assert sr.statement.layout == "csv" and sr.supplier_id == "sup-norte" and sr.company_id == "hazel-tree"
    assert sr.check is not None and [r.status for r in sr.check.rows] == ["missing"]
    assert o.repo.outbox[sr.request_id].status == "waiting"

    english = "\n".join([
        "Papelaria Norte", "Statement of account", "Customer: Hazel Tree",
        "Date        Reference   Description   Debit    Credit   Balance",
        "Opening balance                                          0.00",
        "05/09/2026  INV-5501    Invoice       49.20             49.20",
        "Closing balance                                          49.20", ""]).encode()
    report = upload(o, english, "statement.txt")
    record = o.repo.documents[report.document_ids[0]]
    assert record.document.doc_type is DocumentType.SUPPLIER_STATEMENT and record.supplier_id == "sup-norte"
    sr = o.repo.statements[record.id]
    assert sr.company_id == "hazel-tree"  # named on it
    assert sr.check is not None and sr.check.missing[0].line.number == "INV-5501"
    detail = get(BackOfficeService(o), f"/api/documents/{record.id}")["statement"]
    assert detail["summary"].startswith("Papelaria Norte's statement: its one line does not match your records.")
    assert detail["request"]["text"] == "I wrote to Papelaria Norte asking for it. It is waiting to be sent."
    plain(detail["summary"])


def test_check_statement_matches_by_amount_and_reports_payments_not_in_the_bank() -> None:
    statement = read_statement(norte_statement(
        "03/09/2026  FA-77         Fatura                100,00                  100,00",
        "20/09/2026  RE 2026/70    Recibo                             100,00      0,00",
        "22/09/2026  RE 2026/71    Pagamento                           50,00    -50,00").decode())
    assert statement is not None
    ours = [OurDocument(id="d1", kind=INVOICE, number=None, amount=Decimal("100.00"), on=date(2026, 9, 5), paid=True,
                        label="Receipt · €100.00")]
    paid = [OurPayment(id="t1", amount=Decimal("100.00"), on=date(2026, 9, 25), evidence_id="ev1")]
    check = check_statement(statement, ours, paid, supplier="Papelaria Norte")
    assert [(r.status, r.by_amount) for r in check.rows] == [("matched", True), ("matched", False),
                                                            ("not_found", False)]
    assert check.rows[1].transaction_id == "t1"
    from backoffice.supplier_statements import summary

    text = summary(check)
    assert "I can't find payment RE 2026/71 of 22 September (€50.00) in your bank." in text
    plain(text)


# --------------------------------------------------------------------------- 2. owner statements


COMPANY = "516123459"


def nif(prefix8: str) -> str:
    total = sum(int(d) * w for d, w in zip(prefix8, range(9, 1, -1), strict=True))
    check = 11 - total % 11
    number = prefix8 + str(0 if check >= 10 else check)
    assert validate_nif(number).valid, number
    return number


LIMPA = nif("50777777")
EDP = nif("50300000")
TENANT = nif("23456789")
META = nif("98000013")
IKEA = nif("50123456")
HOTEL = nif("50555555")
MICROSOFT = nif("98000012")


class Business:
    """One owner, one company with a bank account, a card, and a few suppliers (a new tenant)."""

    def __init__(self) -> None:
        self.svc = BackOfficeService.new_tenant("t-rebill", owner_name="Rita Alves", owner_email="rita@gestao.pt",
                                                now=datetime(2026, 10, 2, 9, 0, tzinfo=TZ))
        self.a = self.svc.add_company("Gestão Alves", COMPANY)["company"]["id"]
        self.bank = post(self.svc, "/api/sources", {"kind": "bank", "bank": "Millennium BCP", "companyId": self.a,
                                                    "iban": "PT50000201231234567890154"})["id"]
        self.card = post(self.svc, "/api/sources", {"kind": "card", "bank": "Millennium BCP", "companyId": self.a,
                                                    "last4": "4817"})["id"]
        for name, tax_id in (("Limpa Tudo", LIMPA), ("EDP Comercial", EDP), ("Meta Platforms", META),
                             ("IKEA", IKEA), ("Hotel Infante", HOTEL), ("Microsoft", MICROSOFT)):
            post(self.svc, "/api/sources", {"kind": "supplier", "name": name, "taxId": tax_id})

    @property
    def repo(self) -> Any:
        return self.svc.repo

    def center(self, name: str, kind: str, **body: Any) -> str:
        return post(self.svc, f"/api/companies/{self.a}/cost-centers", {"name": name, "kind": kind, **body})[
            "costCenter"]["id"]

    def pay(self, day: date, amount: str, counterparty: str, description: str = "", *, card: bool = False) -> str:
        incoming = Decimal(amount) > 0
        kind = (TransactionKind.CARD if card else TransactionKind.TRANSFER_IN if incoming
                else TransactionKind.TRANSFER_OUT)
        row = BankRow(bank_id=f"b-{counterparty}-{day}-{amount}", account_id=self.card if card else self.bank,
                      booked_on=day, amount=Decimal(amount), counterparty=counterparty, description=description,
                      kind=kind, card_last4="4817" if card else None)
        return self.svc.orchestrator.ingest_bank([row]).transaction_ids[0]

    def upload(self, name: str, data: bytes, content_type: str = "text/plain") -> dict[str, Any]:
        return self.svc.upload_evidence(name, content_type, data)

    def tx(self, tx_id: str) -> Any:
        return self.repo.transactions[tx_id]

    def allocation(self, tx_id: str) -> Any:
        return self.repo.transactions[tx_id].tx.cost_allocation


def invoice(supplier: str, supplier_nif: str, number: str, day: date, net: str, vat: str, *body: str,
            customer: str = COMPANY, code: str = "FT") -> bytes:
    gross = Decimal(net) + Decimal(vat)
    seq = number.rsplit("/", 1)[-1]
    qr = E.qr_payload(A=supplier_nif, B=customer, C="PT", D=code, E="N", F=day.strftime("%Y%m%d"), G=number,
                      H=f"CSDF7T5H-{seq}", I1="PT", I7=net, I8=vat, N=vat, O=f"{gross:.2f}", Q="e1Dk", R="1422")
    title = {"FT": "Fatura", "FR": "Fatura-recibo"}[code]
    lines = [supplier, f"NIF: {supplier_nif}", f"{title} n.º {number}", f"ATCUD: CSDF7T5H-{seq}",
             f"Data de emissão: {day:%d/%m/%Y}", "Cliente: Gestão Alves, Lda.", f"NIF: {customer}", *body,
             f"Base tributável (23%): {net.replace('.', ',')}", f"IVA 23%: {vat.replace('.', ',')}",
             f"Total: {str(gross).replace('.', ',')} €", f"Código QR: {qr}", ""]
    return "\n".join(lines).encode()


def rent_receipt(number: str, day: date, net: str, vat: str) -> bytes:
    """The business's own invoice-receipt for a stay in the apartment (its sales document for the money received)."""
    seq = number.rsplit("/", 1)[-1]
    total = f"{Decimal(net) + Decimal(vat):.2f}"
    qr = E.qr_payload(A=COMPANY, B=TENANT, C="PT", D="FR", E="N", F=day.strftime("%Y%m%d"), G=number,
                      H=f"RNTQ7T5H-{seq}", I1="PT", I7=net, I8=vat, N=vat, O=total, Q="e1Dk", R="1422")
    lines = ["Gestão Alves, Lda.", f"NIF: {COMPANY}", f"Fatura-recibo n.º {number}", f"ATCUD: RNTQ7T5H-{seq}",
             f"Data de emissão: {day:%d/%m/%Y}", "Cliente: John Smith", f"NIF: {TENANT}",
             "Alojamento de setembro - Apartamento 2B (APT-2B)", f"Base tributável (23%): {net.replace('.', ',')}",
             f"IVA 23%: {vat.replace('.', ',')}", f"Total: {total.replace('.', ',')} €", f"Código QR: {qr}", ""]
    return "\n".join(lines).encode()


def test_owner_statement_for_a_property_shows_rent_costs_fee_and_what_is_due() -> None:
    biz = Business()
    apt = biz.center("2B", "Apartment", owner="Maria Costa", managementFee={"percent": "10"},
                     identifiers={"addresses": ["Rua do Almada 50"], "references": ["APT-2B"]})
    card = get(biz.svc, f"/api/cost-centers/{apt}")
    assert card["owner"] == "Maria Costa" and card["managementFee"] == {"percent": 10, "monthly": None}
    assert card["isProperty"] is True and card["recharge"] is False
    rent = biz.pay(date(2026, 9, 1), "1200.00", "JOHN SMITH", "RENDA SETEMBRO APT-2B FR GA2026/9")
    biz.upload("recibo.txt", rent_receipt("FR GA2026/9", date(2026, 9, 1), "975.61", "224.39"))
    clean = biz.pay(date(2026, 9, 10), "-123.00", "LIMPA TUDO", "LIMPEZA APT-2B")
    biz.upload("limpa.txt", invoice("Limpa Tudo, Lda.", LIMPA, "FT LT2026/9", date(2026, 9, 10), "100.00", "23.00",
                                    "Limpeza do apartamento APT-2B"))
    edp = biz.pay(date(2026, 9, 20), "-61.50", "EDP COMERCIAL", "DD EDP APT-2B")
    assert biz.repo.items[biz.tx(rent).item_id].stage is Stage.CLOSED
    assert biz.repo.items[biz.tx(clean).item_id].stage is Stage.CLOSED

    st = get(biz.svc, f"/api/cost-centers/{apt}/statement?month=2026-09")
    assert st["title"] == "Owner statement · Apartment 2B · September" and st["ownerStatement"] is True
    assert st["owner"] == "Maria Costa" and st["period"] == {"from": "2026-09-01", "to": "2026-09-30",
                                                            "label": "September"}
    assert [(m["id"], m["amount"], m["kind"], m["label"]) for m in st["moneyIn"]] == [(rent, 1200, "rent", "Rent")]
    assert [(c["id"], c["amount"]) for c in st["costs"]] == [(clean, 123), (edp, 61.5)]
    clean_row = st["costs"][0]
    assert [e["id"] for e in clean_row["evidence"]] == [
        biz.tx(clean).evidence_id, biz.repo.documents[biz.tx(clean).document_ids[0]].evidence_ids[0]]
    assert st["received"] == 1200 and st["spent"] == 184.5
    assert st["managementFee"] == {"amount": 120, "label": "Management fee: 10% of €1,200.00 received",
                                   "percent": 10, "monthly": None}
    assert st["net"] == 895.5 and st["netDueToOwner"] == 895.5
    # The EDP payment still waits for its invoice: the statement says so and is not final.
    assert st["final"] is False and st["status"] == "not final"
    assert [o["id"] for o in st["openItems"]] == [edp]
    assert st["summary"] == ("Apartment 2B in September: €1,200.00 received, €184.50 in costs, €120.00 management "
                             "fee. Maria Costa is due €895.50. Not final: 1 thing is still open.")
    plain(st["summary"], *(o["text"] for o in st["openItems"]), st["managementFee"]["label"])

    biz.upload("edp.txt", invoice("EDP Comercial", EDP, "FT EDP2026/77", date(2026, 9, 20), "50.00", "11.50",
                                  "Local de consumo: Rua do Almada 50, 2B"))
    st = get(biz.svc, f"/api/cost-centers/{apt}/statement?month=2026-09")
    assert st["final"] is True and st["status"] == "final" and st["openItems"] == []
    assert st["summary"] == ("Apartment 2B in September: €1,200.00 received, €184.50 in costs, €120.00 management "
                             "fee. Maria Costa is due €895.50. Final: every amount is proven.")
    assert {e["id"] for e in st["evidence"]} >= {biz.tx(t).evidence_id for t in (rent, clean, edp)}
    plain(st["summary"])


def test_an_owner_statement_with_open_items_says_so_and_is_not_final() -> None:
    biz = Business()
    apt = biz.center("3C", "Apartment", owner="Rui Neves", managementFee={"monthly": "50.00"},
                     identifiers={"references": ["APT-3C"]})
    biz.center("4D", "Apartment", identifiers={"references": ["APT-4D"]})
    biz.pay(date(2026, 9, 2), "900.00", "ANA PIRES", "RENDA APT-3C")
    unknown = biz.pay(date(2026, 9, 12), "-45.00", "DROGARIA CENTRAL", "MATERIAL")  # which apartment? not said
    st = get(biz.svc, f"/api/cost-centers/{apt}/statement?month=2026-09")
    assert st["managementFee"]["amount"] == 50 and st["managementFee"]["label"] == \
        "Management fee: €50.00 a month for 1 month"
    assert st["net"] == 850 and st["final"] is False
    texts = [o["text"] for o in st["openItems"]]
    assert any("DROGARIA" in t.upper() and "which apartment it is for" in t for t in texts)
    assert any(o["id"] != unknown and "900.00" in o["text"] for o in st["openItems"])  # the rent's own receipt
    assert st["summary"].endswith(f"Not final: {len(texts)} things are still open.")
    # A two-month period counts the monthly fee twice; a negative result says who owes whom.
    two = get(biz.svc, f"/api/cost-centers/{apt}/statement?from=2026-08-01&to=2026-09-30")
    assert two["managementFee"]["amount"] == 100 and two["period"]["label"] == "1 August to 30 September"
    empty = get(biz.svc, f"/api/cost-centers/{apt}/statement?month=2026-07")
    assert empty["received"] == 0 and empty["net"] == -50
    assert "The costs are more than the money received: Rui Neves owes €50.00." in empty["summary"]
    plain(*texts, two["summary"], empty["summary"])


def test_owner_statement_route_periods_and_errors() -> None:
    biz = Business()
    apt = biz.center("5E", "Apartment")
    job = biz.center("Obra Norte", "Job")
    default = get(biz.svc, f"/api/cost-centers/{apt}/statement")
    assert default["period"] == {"from": "2026-09-01", "to": "2026-09-30", "label": "September"}  # the month closing
    assert default["summary"] == ("Apartment 5E in September: €0.00 received, €0.00 in costs. The owner is due €0.00. "
                                  "Final: every amount is proven.")
    other = get(biz.svc, f"/api/cost-centers/{job}/statement?month=2026-09")
    assert other["ownerStatement"] is False and other["netDueToOwner"] is None
    assert other["title"] == "Statement · Job Obra Norte · September"
    assert other["summary"].startswith("Job Obra Norte in September: €0.00 received, €0.00 in costs. What is left:")
    assert get(biz.svc, f"/api/cost-centers/{apt}/statement?month=2026-13", status=400)["message"] == \
        "Use a month like 2026-09."
    assert get(biz.svc, "/api/cost-centers/cc-nope/statement", status=404)["message"] == "I can't find that one."
    for bad, message in (({"managementFee": {"percent": "120"}}, "A fee is more than 0% and at most 100%."),
                         ({"managementFee": {"percent": "abc"}}, "That percent is not a number."),
                         ({"managementFee": "10%"}, "Give the fee as a percent of what comes in, an amount a month, "
                                                    "or both."),
                         ({"recharge": "yes"}, "Say true or false."), ({"owner": 7}, "Write the owner's name.")):
        assert post(biz.svc, f"/api/cost-centers/{apt}", bad, status=400)["message"] == message
        plain(message)
    changed = post(biz.svc, f"/api/cost-centers/{apt}", {"owner": "Luísa Reis", "managementFee": {"percent": "8.5",
                                                                                                "monthly": "20"}})
    assert changed["costCenter"]["owner"] == "Luísa Reis"
    assert changed["costCenter"]["managementFee"] == {"percent": 8.5, "monthly": 20}
    cleared = post(biz.svc, f"/api/cost-centers/{apt}", {"managementFee": None, "owner": None})
    assert cleared["costCenter"]["managementFee"] is None and cleared["costCenter"]["owner"] is None
    routes = [p.pattern for v, p, _ in biz.svc._routes() if v == "GET"]
    assert "/api/cost-centers/([^/]+)/statement" in routes and "/api/documents/([^/]+)" in routes


# --------------------------------------------------------------------------- 3. costs recharged to clients


def test_media_bought_for_a_client_is_inferred_from_the_invoice_and_left_out_of_own_costs() -> None:
    biz = Business()
    lume = biz.center("Atelier Lume", "Client")
    media = biz.pay(date(2026, 9, 10), "-2400.00", "META PLATFORMS", "META ADS")
    biz.upload("meta.txt", invoice("Meta Platforms Ireland", META, "FT MT2026/88", date(2026, 9, 10), "1951.22",
                                   "448.78", "Campanha de setembro - cliente final: Atelier Lume"))
    allocation = biz.allocation(media)
    assert biz.tx(media).document_ids and allocation.cost_center_ids == (lume,)
    assert [s.recharge for s in allocation.shares] == [True] and allocation.recharge_method == "evidence"
    assert allocation.recharge_why == ("The invoice names Client Atelier Lume as the client it was bought for.",)
    doc = biz.repo.documents[biz.tx(media).document_ids[0]].document
    assert doc.cost_allocation is not None and doc.cost_allocation.shares[0].recharge is True
    software = biz.pay(date(2026, 9, 15), "-24.60", "MICROSOFT*365", "LICENCA")  # the agency's own cost
    back = biz.pay(date(2026, 9, 25), "1800.00", "ATELIER LUME LDA", "REEMBOLSO MEDIA ATELIER LUME")
    fee = biz.pay(date(2026, 9, 26), "1230.00", "ATELIER LUME LDA", "FEE SETEMBRO ATELIER LUME")
    assert biz.allocation(back).shares[0].recharge is False  # money in is never itself "to recharge"

    spent = ask(biz.svc, "How much did we spend in September?")["answer"]
    assert spent.startswith("You spent €24.60 in September: Microsoft on 15 September.")
    assert "I left out €2,400.00 bought for Client Atelier Lume, to recharge to them; €1,800.00 of it is already " \
           "paid back." in spent
    income = ask(biz.svc, "How much came in in September?")["answer"]
    assert income.startswith("€1,230.00 came in in September:")
    assert "I left out €1,800.00 that Client Atelier Lume paid back for costs you bought for them." in income
    ledger = Ledger(biz.svc)
    kinds = {x.id: x.kind for x in ledger.lines}
    assert kinds[media] == "recharge" and kinds[back] == "paid_back" and kinds[fee] == "income"
    assert kinds[software] == "cost"
    view = get(biz.svc, f"/api/cost-centers/{lume}?month=2026-09")
    assert view["recharged"]["toRecharge"] == 2400 and view["recharged"]["paidBack"] == 1800
    assert view["recharged"]["outstanding"] == 600
    assert view["recharged"]["text"] == ("€2,400.00 bought for Client Atelier Lume in September, to recharge to them. "
                                         "€1,800.00 is already paid back; €600.00 is still to come.")
    row = next(p for p in view["payments"] if p["id"] == media)
    assert row["toRecharge"] is True and row["why"][-1] == allocation.recharge_why[0]
    assert not [n for n in biz.repo.open_needs() if n.kind == "recharge"]
    on_client = ask(biz.svc, "How much did we spend on Atelier Lume in September?")["answer"]
    assert on_client.startswith("You spent €2,400.00 on Client Atelier Lume in September: one payment. All of it is "
                                "theirs to pay back.")
    plain(spent, income, view["recharged"]["text"], *row["why"], on_client)


def test_the_owner_marks_a_cost_to_recharge_and_an_unclear_one_is_one_question_with_always() -> None:
    biz = Business()
    casa = biz.center("Casa Moreira", "Client")
    first = biz.pay(date(2026, 9, 3), "-418.00", "IKEA ALFRAGIDE", card=True)
    out = post(biz.svc, "/api/cost-centers/allocate", {"subjectId": first, "costCenterId": casa, "recharge": True})
    assert out["message"] == "Done. The IKEA payment is now on Client Casa Moreira, to recharge to them."
    allocation = biz.allocation(first)
    assert allocation.recharge_method == "owner" and allocation.shares[0].recharge is True
    assert allocation.recharge_why == ("You said Client Casa Moreira pays this back.",)
    refused = post(biz.svc, "/api/cost-centers/allocate", {"subjectId": first, "general": True, "recharge": True},
                   status=400)
    assert refused["message"] == "General costs are your own: only a cost on a client can be paid back by them."

    # The same shop again, for the same client: it could be either, and nothing says. One question.
    second = biz.pay(date(2026, 9, 5), "-60.00", "IKEA ALFRAGIDE", card=True)
    post(biz.svc, "/api/cost-centers/allocate", {"subjectId": second, "costCenterId": casa})
    asked = [n for n in biz.repo.open_needs() if n.kind == "recharge"]
    assert len(asked) == 1 and asked[0].subject_id == second
    item = next(i for i in get(biz.svc, "/api/needs-you")["items"] if i["id"] == asked[0].id)
    assert item["question"] == "Does Client Casa Moreira pay this back?"
    assert [(o["id"], o["label"]) for o in item["options"]] == [("recharge", "Yes, Client Casa Moreira pays it back"),
                                                               ("own", "No, it is our own cost")]
    assert item["why"] == ["It is on Client Casa Moreira, and some of their costs are paid back by them.",
                           "Nothing on the payment or its invoice says whether this one is."]
    assert item["remember"]["overrides"] == {
        "recharge": "Always recharge IKEA paid with card •••• 4817 to the client it is for",
        "own": "Always keep IKEA paid with card •••• 4817 as your own cost"}
    answered = post(biz.svc, f"/api/needs-you/{asked[0].id}/answer", {"option_id": "recharge", "remember": True})
    assert answered["message"] == "Done. The IKEA payment is on Client Casa Moreira, to recharge to them."
    assert answered["learned"] == "Always recharge IKEA paid with card •••• 4817 to the client it is for"
    assert biz.allocation(second).shares[0].recharge is True and biz.allocation(second).recharge_method == "owner"
    rule = next(r for r in biz.repo.rulebook.rules if r.outcome.recharge is not None)
    assert rule.outcome.fields() == frozenset({RuleField.RECHARGE}) and rule.outcome.recharge is True

    # Next time the rule decides it: no question.
    third = biz.pay(date(2026, 9, 12), "-99.00", "IKEA ALFRAGIDE", card=True)
    post(biz.svc, "/api/cost-centers/allocate", {"subjectId": third, "costCenterId": casa})
    assert biz.allocation(third).recharge_method == "rule" and biz.allocation(third).shares[0].recharge is True
    assert biz.allocation(third).recharge_why == ("You told us: Always recharge IKEA paid with card •••• 4817 to the "
                                                  "client it is for.",)
    assert not [n for n in biz.repo.open_needs() if n.kind == "recharge"]
    # The owner can also say it is their own cost.
    fourth = biz.pay(date(2026, 9, 14), "-15.00", "IKEA ALFRAGIDE", card=True)
    post(biz.svc, "/api/cost-centers/allocate", {"subjectId": fourth, "costCenterId": casa, "recharge": False})
    assert biz.allocation(fourth).shares[0].recharge is False and biz.allocation(fourth).recharge_method == "owner"
    spent = ask(biz.svc, "How much did we spend in September?")["answer"]
    assert spent.startswith("You spent €15.00 in September: IKEA on 14 September.")
    assert "I left out €577.00 bought for Client Casa Moreira, to recharge to them." in spent
    plain(item["question"], *item["why"], answered["message"], answered["learned"], spent, out["message"])

    # A "keep as my own" rule narrows cleanly and is never mistaken for "nothing decided".
    own = RuleOutcome(recharge=False)
    assert own.fields() == frozenset({RuleField.RECHARGE}) and own.without({RuleField.RECHARGE}) is None
    both = RuleOutcome(recharge=False, category="Office")
    assert both.without({RuleField.CATEGORY}) == RuleOutcome(recharge=False)


def test_a_client_that_never_had_costs_paid_back_is_never_asked() -> None:
    biz = Business()
    casa = biz.center("Casa Moreira", "Client")
    for day, amount in ((3, "-418.00"), (5, "-60.00"), (9, "-12.00")):
        tx = biz.pay(date(2026, 9, day), amount, "IKEA ALFRAGIDE", card=True)
        post(biz.svc, "/api/cost-centers/allocate", {"subjectId": tx, "costCenterId": casa})
        assert biz.allocation(tx).shares[0].recharge is False and biz.allocation(tx).recharge_method is None
    assert not [n for n in biz.repo.open_needs() if n.kind == "recharge"]
    assert get(biz.svc, f"/api/cost-centers/{casa}")["recharged"] is None
    assert ask(biz.svc, "How much did we spend in September?")["answer"].startswith(
        "You spent €490.00 in September")


def test_client_money_held_by_a_travel_agency_is_not_revenue() -> None:
    biz = Business()
    silva = biz.center("Família Silva", "Client", recharge=True, identifiers={"keywords": ["SILVA ACORES"]})
    assert get(biz.svc, f"/api/cost-centers/{silva}")["recharged"]["text"] == \
        "Nothing received from or paid for Client Família Silva. Their money is never your revenue."
    money_in = biz.pay(date(2026, 9, 2), "3000.00", "JOAO SILVA", "VIAGEM SILVA ACORES")
    hotel = biz.pay(date(2026, 9, 8), "-2700.00", "HOTEL INFANTE", "RESERVA SILVA ACORES")
    allocation = biz.allocation(hotel)
    assert allocation.cost_center_ids == (silva,) and allocation.shares[0].recharge is True
    assert allocation.recharge_method == "setting"
    assert allocation.recharge_why == ("Costs on Client Família Silva are theirs to pay back, as you set.",)
    view = get(biz.svc, f"/api/cost-centers/{silva}?month=2026-09")["recharged"]
    assert view["clientMoney"] is True and view["received"] == 3000 and view["toRecharge"] == 2700
    assert view["held"] == 300 and [r["id"] for r in view["receipts"]] == [money_in]
    assert view["text"] == ("€3,000.00 received from Client Família Silva in September is client money, not revenue. "
                            "€2,700.00 was paid out for them. €300.00 of their money is still held for them.")
    income = ask(biz.svc, "How much came in in September?")["answer"]
    assert income.startswith("Nothing came in from customers in September, and no refunds.")
    assert ("I left out €3,000.00 of client money from Client Família Silva: it is theirs, not revenue. €300.00 of "
            "their money is still held for them.") in income
    spent = ask(biz.svc, "How much did we spend in September?")["answer"]
    assert spent.startswith("You had no costs of your own in September.")
    assert "I left out €2,700.00 paid for Client Família Silva out of their own money: it is not your cost." in spent
    assert Ledger(biz.svc).money(date(2026, 9, 1), date(2026, 9, 30), direction="in").total == 0
    plain(view["text"], income, spent)


def test_a_reseller_line_naming_the_end_customer_is_a_pass_through_cost() -> None:
    biz = Business()
    customers = [nif(f"50913{n:03d}") for n in range(2)]
    centers = [biz.center(f"Acme {i + 1}", "Client", identifiers={"taxIds": [t]}) for i, t in enumerate(customers)]
    net = [("100.00", customers[0]), ("40.00", customers[1])]
    body = "".join(
        f"""<cac:InvoiceLine><cbc:ID>{i}</cbc:ID><cbc:Note>Cliente final NIF {t} · licencas</cbc:Note>
        <cbc:InvoicedQuantity unitCode="C62">1</cbc:InvoicedQuantity>
        <cbc:LineExtensionAmount currencyID="EUR">{a}</cbc:LineExtensionAmount>
        <cac:Item><cbc:Name>Microsoft 365</cbc:Name><cac:ClassifiedTaxCategory><cbc:ID>S</cbc:ID>
        <cbc:Percent>23</cbc:Percent><cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme></cac:ClassifiedTaxCategory>
        </cac:Item><cac:Price><cbc:PriceAmount currencyID="EUR">{a}</cbc:PriceAmount></cac:Price></cac:InvoiceLine>"""
        for i, (a, t) in enumerate(net, start=1))
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
         xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
         xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">
  <cbc:CustomizationID>urn:cen.eu:en16931:2017</cbc:CustomizationID><cbc:ID>FT MS2026/950</cbc:ID>
  <cbc:IssueDate>2026-09-05</cbc:IssueDate><cbc:InvoiceTypeCode>380</cbc:InvoiceTypeCode>
  <cbc:DocumentCurrencyCode>EUR</cbc:DocumentCurrencyCode>
  <cac:AccountingSupplierParty><cac:Party><cac:PartyName><cbc:Name>Microsoft Ireland</cbc:Name></cac:PartyName>
    <cac:PartyTaxScheme><cbc:CompanyID>PT{MICROSOFT}</cbc:CompanyID><cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme>
    </cac:PartyTaxScheme></cac:Party></cac:AccountingSupplierParty>
  <cac:AccountingCustomerParty><cac:Party><cac:PartyTaxScheme><cbc:CompanyID>PT{COMPANY}</cbc:CompanyID>
    <cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme></cac:PartyTaxScheme></cac:Party></cac:AccountingCustomerParty>
  <cac:TaxTotal><cbc:TaxAmount currencyID="EUR">32.20</cbc:TaxAmount>
    <cac:TaxSubtotal><cbc:TaxableAmount currencyID="EUR">140.00</cbc:TaxableAmount>
      <cbc:TaxAmount currencyID="EUR">32.20</cbc:TaxAmount>
      <cac:TaxCategory><cbc:ID>S</cbc:ID><cbc:Percent>23</cbc:Percent>
      <cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme></cac:TaxCategory></cac:TaxSubtotal></cac:TaxTotal>
  <cac:LegalMonetaryTotal><cbc:LineExtensionAmount currencyID="EUR">140.00</cbc:LineExtensionAmount>
    <cbc:TaxExclusiveAmount currencyID="EUR">140.00</cbc:TaxExclusiveAmount>
    <cbc:TaxInclusiveAmount currencyID="EUR">172.20</cbc:TaxInclusiveAmount>
    <cbc:PayableAmount currencyID="EUR">172.20</cbc:PayableAmount></cac:LegalMonetaryTotal>
  {body}
</Invoice>
""".encode()
    payment = biz.pay(date(2026, 9, 6), "-172.20", "MICROSOFT*365")
    biz.upload("ms.xml", xml, "application/xml")
    biz.upload("ms.txt", invoice("Microsoft Ireland Operations Ltd", MICROSOFT, "FT MS2026/950", date(2026, 9, 5),
                                 "140.00", "32.20"))
    allocation = biz.allocation(payment)
    assert allocation is not None and allocation.method is AllocationMethod.LINES
    assert allocation.cost_center_ids == tuple(centers) and [s.recharge for s in allocation.shares] == [True, True]
    assert allocation.recharge_why == ("Line 1 of the invoice names Client Acme 1 as the end customer.",
                                       "Line 2 of the invoice names Client Acme 2 as the end customer.")
    spent = ask(biz.svc, "How much did we spend in September?")["answer"]
    assert "I left out €123.00 bought for Client Acme 1, to recharge to them." in spent
    assert "I left out €49.20 bought for Client Acme 2, to recharge to them." in spent
    plain(spent, *allocation.recharge_why)


def test_a_disbursement_on_behalf_of_a_client_is_theirs_to_pay_back() -> None:
    biz = Business()
    matter = biz.center("Processo Ribeiro", "Client", identifiers={"references": ["PROC-2026-17"]})
    fee = biz.pay(date(2026, 9, 14), "-73.80", "LIMPA TUDO", "CERTIDAO PROC-2026-17")
    biz.upload("certidao.txt", invoice("Limpa Tudo, Lda.", LIMPA, "FT LT2026/41", date(2026, 9, 14), "60.00", "13.80",
                                       "Certidão pedida por conta do cliente - Proc. PROC-2026-17"))
    allocation = biz.allocation(fee)
    assert allocation.cost_center_ids == (matter,) and allocation.shares[0].recharge is True
    assert allocation.recharge_method == "evidence"
    # Paid back with a transfer of exactly what is outstanding: matched to it, never counted as fees.
    back = biz.pay(date(2026, 9, 28), "73.80", "CARLOS RIBEIRO", "PROC-2026-17")
    assert Ledger(biz.svc).lines and {x.id: x.kind for x in Ledger(biz.svc).lines}[back] == "paid_back"
    view = get(biz.svc, f"/api/cost-centers/{matter}")["recharged"]
    assert view["paidBack"] == 73.8 and view["outstanding"] == 0
    assert view["text"] == "€73.80 bought for Client Processo Ribeiro, to recharge to them. All of it is paid back."
    plain(view["text"])


# --------------------------------------------------------------------------- the demo and the words


def test_the_demo_is_unchanged_by_statements_and_recharges() -> None:
    svc = BackOfficeService.demo()
    repo = svc.repo
    assert repo.statements == {} and repo.cost_centers == {}
    assert not [m for m in repo.outbox.values() if m.kind.startswith("statement")]
    assert {n.kind for n in repo.needs.values()} <= {"choice", "approval", "check", "company", "cash", "cost_center"}
    assert not [x for x in Ledger(svc).lines if x.kind in ("recharge", "paid_back", "client_money")]
    assert all("statement" not in d for d in svc.documents_list()["items"] if d["type"] != "supplier statement")
