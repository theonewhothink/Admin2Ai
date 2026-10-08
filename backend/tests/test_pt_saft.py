"""SAF-T (PT) sales invoices reader and subset ledger export (§13, §28, §50)."""

import csv
import io
from datetime import date
from decimal import Decimal

import pytest

from backoffice.countries.pt import (
    LEDGER_NOTICE,
    LedgerRow,
    SaftError,
    SaftSecurityError,
    export_ledger_csv,
    ledger_row_from_document,
    parse_saft_sales_invoices,
    saft_invoice_observations,
)
from backoffice.countries.pt.saft import parse_xml_safely
from backoffice.domain.models import (
    CriticalField,
    Document,
    DocumentType,
    ExtractionMethod,
    Quality,
)

NS = "urn:OECD:StandardAuditFile-Tax:PT_1.04_01"


def invoice_xml(
    number: str = "FT 2026/183",
    *,
    doc_type: str = "FT",
    status: str = "N",
    customer: str = "C1",
    lines: str | None = None,
    totals: tuple[str, str, str] = ("90.43", "393.17", "483.60"),
    extra: str = "",
    atcud: str = "CSDF7T5H-183",
) -> str:
    amount_tag = "DebitAmount" if doc_type == "NC" else "CreditAmount"
    lines = lines or (
        f"<Line><LineNumber>1</LineNumber><Quantity>1</Quantity><UnitPrice>393.17</UnitPrice>"
        f"<{amount_tag}>393.17</{amount_tag}><Tax><TaxType>IVA</TaxType>"
        "<TaxCountryRegion>PT</TaxCountryRegion><TaxCode>NOR</TaxCode>"
        "<TaxPercentage>23</TaxPercentage></Tax></Line>"
    )
    tax, net, gross = totals
    return (
        f"<Invoice><InvoiceNo>{number}</InvoiceNo><ATCUD>{atcud}</ATCUD>"
        f"<DocumentStatus><InvoiceStatus>{status}</InvoiceStatus></DocumentStatus>"
        f"<Hash>abc</Hash><InvoiceDate>2026-09-18</InvoiceDate><InvoiceType>{doc_type}</InvoiceType>"
        f"<CustomerID>{customer}</CustomerID>{lines}"
        f"<DocumentTotals><TaxPayable>{tax}</TaxPayable><NetTotal>{net}</NetTotal>"
        f"<GrossTotal>{gross}</GrossTotal></DocumentTotals>{extra}</Invoice>"
    )


def audit_file(invoices: str, *, entries: int | None = None, debit: str = "0.00",
               credit: str = "393.17", ns: str = f' xmlns="{NS}"', prefix: str = "") -> str:
    count = invoices.count("<Invoice>") if entries is None else entries
    body = (
        "<AuditFile{ns}><Header><AuditFileVersion>1.04_01</AuditFileVersion>"
        "<TaxRegistrationNumber>509123457</TaxRegistrationNumber>"
        "<CompanyName>Fornecedora de Teste, S.A.</CompanyName><FiscalYear>2026</FiscalYear>"
        "<StartDate>2026-09-01</StartDate><EndDate>2026-09-30</EndDate>"
        "<CurrencyCode>EUR</CurrencyCode><SoftwareCertificateNumber>2345</SoftwareCertificateNumber>"
        "</Header><MasterFiles>"
        "<Customer><CustomerID>C1</CustomerID><CustomerTaxID>516123459</CustomerTaxID>"
        "<CompanyName>Hazel Tree Lda</CompanyName><BillingAddress><Country>PT</Country>"
        "</BillingAddress></Customer>"
        "<Customer><CustomerID>CF</CustomerID><CustomerTaxID>999999990</CustomerTaxID>"
        "<CompanyName>Consumidor final</CompanyName><BillingAddress><Country>Desconhecido</Country>"
        "</BillingAddress></Customer></MasterFiles>"
        f"<SourceDocuments><SalesInvoices><NumberOfEntries>{count}</NumberOfEntries>"
        f"<TotalDebit>{debit}</TotalDebit><TotalCredit>{credit}</TotalCredit>{invoices}"
        "</SalesInvoices></SourceDocuments></AuditFile>"
    ).replace("{ns}", ns)
    if prefix:
        body = body.replace("</", f"</{prefix}:").replace("<", f"<{prefix}:").replace(
            f"<{prefix}:/", "</")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + body


def test_reads_invoices_and_header():
    result = parse_saft_sales_invoices(audit_file(invoice_xml()))
    assert result.problems == ()
    header = result.header
    assert header.company_tax_id == "509123457" and header.currency == "EUR"
    assert header.fiscal_year == 2026 and header.start_date == date(2026, 9, 1)
    (inv,) = result.invoices
    assert inv.invoice_no == "FT 2026/183" and inv.doc_type == DocumentType.INVOICE
    assert inv.invoice_date == date(2026, 9, 18)
    assert (inv.net_total, inv.vat_total, inv.gross_total) == (
        Decimal("393.17"), Decimal("90.43"), Decimal("483.60"))
    assert inv.customer_tax_id == "516123459" and inv.customer_name == "Hazel Tree Lda"
    assert inv.atcud is not None and inv.atcud.sequence == 183
    assert inv.problems == ()
    (base,) = inv.tax_bases
    assert (base.tax_type, base.region, base.code, base.percentage, base.base) == (
        "IVA", "PT", "NOR", Decimal("23"), Decimal("393.17"))


def test_document_dicts_build_core_documents():
    result = parse_saft_sales_invoices(audit_file(invoice_xml()))
    (fields,) = result.documents()
    doc = Document(tenant_id="t1", evidence_ids=["ev_saft"], **fields)
    assert doc.gross_amount == Decimal("483.60") and doc.supplier_tax_id == "509123457"
    assert doc.customer_tax_id == "516123459" and doc.invoice_number == "FT 2026/183"


def test_namespace_agnostic():
    plain = parse_saft_sales_invoices(audit_file(invoice_xml(), ns=""))
    prefixed_xml = audit_file(invoice_xml(), ns=f' xmlns:saft="{NS}"', prefix="saft")
    assert "<saft:Invoice>" in prefixed_xml
    prefixed = parse_saft_sales_invoices(prefixed_xml)
    default_ns = parse_saft_sales_invoices(audit_file(invoice_xml()))
    assert plain.invoices == prefixed.invoices == default_ns.invoices


def test_credit_note_lines_are_debits_and_amounts_stay_positive():
    xml = audit_file(invoice_xml("NC 2026/4", doc_type="NC", atcud="CSDF7T5H-4"),
                     debit="393.17", credit="0.00")
    result = parse_saft_sales_invoices(xml)
    (credit_note,) = result.invoices
    assert credit_note.doc_type == DocumentType.CREDIT_NOTE
    assert credit_note.tax_bases[0].base == Decimal("393.17")
    assert credit_note.problems == () and result.problems == ()
    doc = Document(tenant_id="t", evidence_ids=["e"], **result.documents()[0])
    assert doc.signed_gross == Decimal("-483.60")


def test_cancelled_documents_are_excluded_from_documents_and_control_totals():
    xml = audit_file(invoice_xml() + invoice_xml("FT 2026/184", status="A", atcud="CSDF7T5H-184"))
    result = parse_saft_sales_invoices(xml)
    assert result.problems == ()  # TotalCredit ignores the cancelled document
    assert len(result.invoices) == 2 and result.invoices[1].is_cancelled
    assert len(result.documents()) == 1
    assert len(result.documents(include_cancelled=True)) == 2


def test_consumer_invoice_keeps_the_placeholder_nif():
    (inv,) = parse_saft_sales_invoices(audit_file(invoice_xml(customer="CF"))).invoices
    assert inv.customer_tax_id == "999999990" and inv.customer_country == "Desconhecido"


def test_stamp_duty_withholding_and_foreign_currency():
    lines = (
        "<Line><CreditAmount>1000.00</CreditAmount><Tax><TaxType>IVA</TaxType>"
        "<TaxCountryRegion>PT</TaxCountryRegion><TaxCode>NOR</TaxCode>"
        "<TaxPercentage>23</TaxPercentage></Tax></Line>"
        "<Line><CreditAmount>0.00</CreditAmount><Tax><TaxType>IS</TaxType>"
        "<TaxCountryRegion>PT</TaxCountryRegion><TaxCode>1</TaxCode>"
        "<TaxAmount>4.00</TaxAmount></Tax></Line>"
    )
    extra = (
        "<WithholdingTax><WithholdingTaxType>IRS</WithholdingTaxType>"
        "<WithholdingTaxAmount>250.00</WithholdingTaxAmount></WithholdingTax>"
    )
    xml = audit_file(invoice_xml(lines=lines, totals=("234.00", "1000.00", "1234.00"), extra=extra),
                     credit="1000.00")
    (inv,) = parse_saft_sales_invoices(xml).invoices
    assert inv.stamp_duty == Decimal("4.00")
    assert inv.vat_total == Decimal("230.00")
    assert inv.withholding_total == Decimal("250.00")
    assert inv.problems == ()


def test_arithmetic_problems_are_reported_not_fixed():
    xml = audit_file(invoice_xml(totals=("90.43", "393.17", "438.60")))
    (inv,) = parse_saft_sales_invoices(xml).invoices
    assert inv.gross_total == Decimal("438.60")
    assert any("GrossTotal" in p for p in inv.problems)
    xml = audit_file(invoice_xml(totals=("90.43", "400.00", "490.43")))
    (inv,) = parse_saft_sales_invoices(xml).invoices
    assert any("sum of lines" in p for p in inv.problems)


def test_file_control_totals():
    result = parse_saft_sales_invoices(audit_file(invoice_xml(), entries=3, credit="999.00"))
    assert any("NumberOfEntries" in p for p in result.problems)
    assert any("TotalCredit" in p for p in result.problems)


def test_no_sales_invoices_section():
    xml = audit_file("").replace("<SourceDocuments><SalesInvoices>", "<SourceDocuments><X>").replace(
        "</SalesInvoices>", "</X>")
    assert parse_saft_sales_invoices(xml).invoices == ()


def test_legacy_encodings():
    xml = audit_file(invoice_xml()).replace('encoding="UTF-8"', 'encoding="windows-1252"').replace(
        "Hazel Tree Lda", "Açores Café Lda")
    (inv,) = parse_saft_sales_invoices(xml.encode("cp1252")).invoices
    assert inv.customer_name == "Açores Café Lda"
    # A str is always read as text, whatever the declaration says.
    (again,) = parse_saft_sales_invoices(xml).invoices
    assert again.customer_name == "Açores Café Lda"


# --------------------------------------------------------------------------- #
# Rejections
# --------------------------------------------------------------------------- #

BILLION_LAUGHS = """<?xml version="1.0"?>
<!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
<!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">]>
<AuditFile>&lol3;</AuditFile>"""

EXTERNAL_ENTITY = """<?xml version="1.0"?>
<!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>
<AuditFile><Header><CompanyName>&xxe;</CompanyName></Header></AuditFile>"""


@pytest.mark.parametrize("payload", [BILLION_LAUGHS, EXTERNAL_ENTITY,
                                     '<!DOCTYPE AuditFile SYSTEM "http://example.com/x.dtd"><AuditFile/>'])
def test_dtds_and_entities_are_refused(payload):
    with pytest.raises(SaftSecurityError) as info:
        parse_saft_sales_invoices(payload)
    assert "untouched" in info.value.owner_message
    with pytest.raises(SaftSecurityError):
        parse_saft_sales_invoices(payload.encode("utf-8"))


def test_size_limit():
    with pytest.raises(SaftError):
        parse_xml_safely("<a/>" + " " * 100, max_bytes=50)


@pytest.mark.parametrize(
    ("xml", "fragment"),
    [
        ("<AuditFile><Header>", "well-formed"),
        ("<Root/>", "AuditFile"),
        (audit_file(invoice_xml().replace("<InvoiceNo>FT 2026/183</InvoiceNo>", "")),
         "Invoice #1: InvoiceNo"),
        (audit_file(invoice_xml("FT2026/183")), "SAF-T pattern"),
        (audit_file(invoice_xml(totals=("NaN", "393.17", "483.60"))), "not a decimal"),
        (audit_file(invoice_xml(totals=("1e2", "393.17", "483.60"))), "not a decimal"),
        (audit_file(invoice_xml().replace("2026-09-18", "2026-02-30")), "not a real date"),
        (audit_file(invoice_xml().replace("2026-09-18", "18/09/2026")), "YYYY-MM-DD"),
        (audit_file(invoice_xml()).replace("<Header>", "<X>").replace("</Header>", "</X>"), "Header"),
    ],
)
def test_structural_problems_raise(xml, fragment):
    with pytest.raises(SaftError) as info:
        parse_saft_sales_invoices(xml)
    assert fragment in str(info.value)
    assert info.value.owner_message == "I couldn't read this accounting file."


# --------------------------------------------------------------------------- #
# Observations
# --------------------------------------------------------------------------- #


def test_structured_observations():
    result = parse_saft_sales_invoices(audit_file(invoice_xml()))
    obs = saft_invoice_observations(result.invoices[0], result.header, "ev_saft")
    by_field = {o.field: o for o in obs}
    assert by_field[CriticalField.GROSS_AMOUNT].value == Decimal("483.60")
    assert by_field[CriticalField.SUPPLIER_TAX_ID].value == "509123457"
    assert by_field[CriticalField.CUSTOMER_TAX_ID].value == "516123459"
    assert by_field[CriticalField.CURRENCY].value == "EUR"
    assert all(o.method == ExtractionMethod.STRUCTURED_XML and o.source == "ev_saft" for o in obs)


# --------------------------------------------------------------------------- #
# Ledger export
# --------------------------------------------------------------------------- #


def document(**overrides) -> Document:
    fields = dict(
        tenant_id="t", evidence_ids=["ev_1", "ev_2"], doc_type=DocumentType.INVOICE,
        supplier_name="Fornecedora de Teste, S.A.", supplier_tax_id="509123457",
        invoice_number="FT 2026/183", issue_date=date(2026, 9, 18), net_amount=Decimal("393.17"),
        vat_amount=Decimal("90.43"), gross_amount=Decimal("483.60"), quality=Quality.GREEN,
    )
    fields.update(overrides)
    return Document(**fields)


def parse_csv(text: str, delimiter: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text), delimiter=delimiter))


def test_ledger_pt_dialect():
    rows = [
        ledger_row_from_document(document(), atcud="CSDF7T5H-183"),
        ledger_row_from_document(document(doc_type=DocumentType.CREDIT_NOTE, invoice_number="NC 2026/4",
                                          issue_date=date(2026, 9, 2), quality=Quality.AMBER)),
    ]
    export = export_ledger_csv(rows, period="2026-09")
    table = parse_csv(export.csv, ";")
    assert table[0][:3] == ["Data", "Tipo", "Número"]
    assert table[1] == ["2026-09-02", "NC", "NC 2026/4", "", "509123457", "Fornecedora de Teste, S.A.",
                        "-393,17", "-90,43", "-483,60", "", "EUR", "por confirmar", "ev_1 ev_2"]
    assert table[2][1:4] == ["FT", "FT 2026/183", "CSDF7T5H-183"]
    assert table[2][8] == "483,60" and table[2][11] == "verificado"
    assert export.row_count == 2
    assert export.notice == LEDGER_NOTICE and "not a SAF-T" in export.notice
    assert "not-saft" in export.filename and "2026-09" in export.filename


def test_ledger_iso_dialect():
    export = export_ledger_csv([ledger_row_from_document(document(quality=Quality.RED))], dialect="iso")
    table = parse_csv(export.csv, ",")
    assert table[0][0] == "date"
    assert table[1][6:9] == ["393.17", "90.43", "483.60"] and table[1][11] == "conflict"


def test_ledger_neutralises_spreadsheet_formulas():
    row = ledger_row_from_document(document(supplier_name='=HYPERLINK("http://x","click")'))
    table = parse_csv(export_ledger_csv([row]).csv, ";")
    assert table[1][5].startswith("'=")


def test_ledger_code_from_number_or_type():
    no_prefix = ledger_row_from_document(document(invoice_number="2026/183",
                                                  doc_type=DocumentType.INVOICE_RECEIPT))
    assert no_prefix.doc_code == "FR"
    receipt = ledger_row_from_document(document(invoice_number=None, doc_type=DocumentType.RECEIPT))
    assert receipt.doc_code == ""


def test_ledger_rows_need_date_and_amount():
    with pytest.raises(ValueError):
        ledger_row_from_document(document(issue_date=None))
    with pytest.raises(ValueError):
        ledger_row_from_document(document(gross_amount=None))


def test_ledger_rejects_unknown_dialect_and_handles_empty():
    with pytest.raises(ValueError):
        export_ledger_csv([], dialect="xlsx")  # type: ignore[arg-type]
    empty = export_ledger_csv([])
    assert empty.row_count == 0 and len(parse_csv(empty.csv, ";")) == 1


def test_ledger_row_is_plain_data():
    row = LedgerRow(date(2026, 9, 1), "FT", "FT 1/1", "", "", "", None, None, Decimal("1"),
                    None, "EUR", Quality.AMBER, "")
    table = parse_csv(export_ledger_csv([row]).csv, ";")
    assert table[1][6] == "" and table[1][8] == "1,00"
