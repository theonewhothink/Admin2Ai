"""Stage 0 structured extraction (§13, §18, §19, §52)."""

import sys
import types
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from backoffice.domain.models import (
    METHOD_RANK,
    DocumentType,
    ExtractionMethod,
    FieldObservation,
)
from backoffice.domain.models import CriticalField as F
from backoffice.extraction import (
    FieldPath,
    JsonFieldMapper,
    LabelledFieldExtractor,
    MissingDependencyError,
    PdfContent,
    QRHook,
    StructuredDataError,
    StructuredFormatError,
    UnsafeXMLError,
    XMLSyntaxError,
    ZXingDecoder,
    extract_from_pdf,
    extract_html_structured,
    extract_structured,
    fiscal_qr_handler,
    parse_einvoice,
    parse_epc_qr,
    parse_xml,
    read_pdf,
)
from backoffice.extraction.jsonmap import resolve_path

FIXTURES = Path(__file__).parent / "fixtures" / "ocr"


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def values(result, field):
    return [o.value for o in result.fields.get(field, ())]


# --------------------------------------------------------------------------- safe XML

BILLION_LAUGHS = b"""<?xml version="1.0"?>
<!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;">]>
<Invoice>&lol2;</Invoice>"""

XXE = b"""<?xml version="1.0"?>
<!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>
<Invoice>&xxe;</Invoice>"""


@pytest.mark.parametrize("payload", [BILLION_LAUGHS, XXE, b"<!DOCTYPE html><Invoice/>"])
def test_xml_with_dtd_or_entities_is_refused(payload):
    with pytest.raises(UnsafeXMLError) as info:
        parse_xml(payload)
    assert info.value.code == "xml_doctype"


def test_xml_limits_and_syntax_errors():
    with pytest.raises(UnsafeXMLError) as too_big:
        parse_xml(b"<a/>", max_bytes=3)
    assert too_big.value.code == "xml_too_large"
    with pytest.raises(UnsafeXMLError):
        parse_xml(b"<a>" * 10 + b"</a>" * 10, max_depth=5)
    with pytest.raises(XMLSyntaxError):
        parse_xml(b"<a><b></a>")
    with pytest.raises(XMLSyntaxError):
        parse_xml(b"<a>&undefined;</a>")


def test_xml_namespaces_become_clark_notation():
    root = parse_xml('<x:a xmlns:x="urn:x" x:k="v"><b>é</b></x:a>')
    assert root.tag == "{urn:x}a"
    assert root.get("{urn:x}k") == "v"
    assert root.find("b").text == "é"


# --------------------------------------------------------------------------- UBL / CII


def test_ubl_invoice_all_critical_fields_with_locations():
    result = parse_einvoice(fixture("einvoice/ubl_invoice.xml"), source="ev_1")
    assert result.kind == "ubl_invoice"
    assert result.doc_type is DocumentType.INVOICE
    assert values(result, F.INVOICE_NUMBER) == ["INV-2026-0042"]
    assert values(result, F.ISSUE_DATE) == [date(2026, 9, 10)]
    assert values(result, F.DUE_DATE) == [date(2026, 10, 10)]
    assert values(result, F.SUPPLIER_TAX_ID) == ["PT508602939"]
    assert values(result, F.CUSTOMER_TAX_ID) == ["PT516123459"]
    assert values(result, F.GROSS_AMOUNT) == [Decimal("1230.00")]
    assert values(result, F.NET_AMOUNT) == [Decimal("1000.00")]
    assert values(result, F.VAT_AMOUNT) == [Decimal("230.00")]
    assert values(result, F.CURRENCY) == ["EUR"]
    assert values(result, F.IBAN) == ["PT50000201231234567890154"]
    assert values(result, F.PAYMENT_REFERENCE) == ["INV-2026-0042"]
    assert result.extras["supplier_name"] == "Lisbon Software Lda"
    assert result.extras["amount_payable"] == "1230.00"
    gross = result.fields[F.GROSS_AMOUNT][0]
    assert gross.method is ExtractionMethod.STRUCTURED_XML
    assert gross.source == "ev_1"
    assert gross.location == "/Invoice/cac:LegalMonetaryTotal/cbc:TaxInclusiveAmount"
    assert METHOD_RANK[gross.method] > METHOD_RANK[ExtractionMethod.OCR]
    assert isinstance(gross.value, Decimal)


def test_ubl_credit_note_keeps_positive_amounts_and_type():
    result = parse_einvoice(fixture("einvoice/ubl_credit_note.xml"), source="ev_2")
    assert result.kind == "ubl_credit_note"
    assert result.doc_type is DocumentType.CREDIT_NOTE
    assert values(result, F.GROSS_AMOUNT) == [Decimal("123.00")]
    assert values(result, F.DUE_DATE) == [date(2026, 10, 20)]


def test_cii_invoice():
    result = parse_einvoice(fixture("einvoice/cii_invoice.xml"), source="ev_3")
    assert result.kind == "cii"
    assert values(result, F.INVOICE_NUMBER) == ["RE-2026-118"]
    assert values(result, F.ISSUE_DATE) == [date(2026, 9, 15)]
    assert values(result, F.DUE_DATE) == [date(2026, 10, 15)]
    assert values(result, F.SUPPLIER_TAX_ID) == ["DE123456789"]  # the VA id, not the FC number
    assert result.extras["supplier_fiscal_number"] == "201/000/12345"
    assert values(result, F.CUSTOMER_TAX_ID) == ["PT516123459"]
    assert values(result, F.NET_AMOUNT) == [Decimal("200.00")]
    assert values(result, F.VAT_AMOUNT) == [Decimal("38.00")]
    assert values(result, F.GROSS_AMOUNT) == [Decimal("238.00")]
    assert values(result, F.IBAN) == ["DE89370400440532013000"]
    assert values(result, F.PAYMENT_REFERENCE) == ["RE-2026-118"]


UBL_HEAD = (
    '<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2" '
    'xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2" '
    'xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">'
)


def ubl(body: str) -> str:
    return UBL_HEAD + body + "</Invoice>"


def test_ubl_inconsistencies_surface_instead_of_being_resolved():
    doc = ubl(
        "<cbc:ID>X-1</cbc:ID><cbc:IssueDate>2026-02-30</cbc:IssueDate><cbc:InvoiceTypeCode>999</cbc:InvoiceTypeCode>"
        "<cbc:DocumentCurrencyCode>EUR</cbc:DocumentCurrencyCode>"
        "<cac:PaymentMeans><cac:PayeeFinancialAccount><cbc:ID>12345678</cbc:ID></cac:PayeeFinancialAccount></cac:PaymentMeans>"
        '<cac:TaxTotal><cbc:TaxAmount currencyID="EUR">23.00</cbc:TaxAmount></cac:TaxTotal>'
        '<cac:TaxTotal><cbc:TaxAmount currencyID="USD">25.00</cbc:TaxAmount></cac:TaxTotal>'
        '<cac:LegalMonetaryTotal><cbc:TaxInclusiveAmount currencyID="USD">123.00</cbc:TaxInclusiveAmount>'
        '<cbc:TaxExclusiveAmount currencyID="EUR">1OO.00</cbc:TaxExclusiveAmount></cac:LegalMonetaryTotal>'
    )
    result = parse_einvoice(doc, source="ev")
    assert values(result, F.ISSUE_DATE) == []
    assert values(result, F.VAT_AMOUNT) == [Decimal("23.00")]  # the tax-currency total is ignored
    assert values(result, F.NET_AMOUNT) == []
    assert values(result, F.IBAN) == []
    # The gross amount's currency disagrees with the document currency: both are reported.
    assert sorted(values(result, F.CURRENCY)) == ["EUR", "USD"]
    assert result.doc_type is DocumentType.INVOICE
    for code in (
        "invalid_date:IssueDate",
        "tax_total_in_other_currency",
        "amount_currency_mismatch",
        "payee_account_not_iban",
        "unknown_type_code:999",
    ):
        assert code in result.notes
    assert any(n.startswith("invalid_amount:") for n in result.notes)


def test_ubl_non_vat_tax_scheme_is_not_a_vat_number():
    doc = ubl(
        "<cbc:ID>X</cbc:ID><cac:AccountingSupplierParty><cac:Party><cac:PartyTaxScheme>"
        "<cbc:CompanyID>12345</cbc:CompanyID><cac:TaxScheme><cbc:ID>GST</cbc:ID></cac:TaxScheme>"
        "</cac:PartyTaxScheme></cac:Party></cac:AccountingSupplierParty>"
    )
    result = parse_einvoice(doc, source="ev")
    assert values(result, F.SUPPLIER_TAX_ID) == []
    assert "supplier_non_vat_tax_scheme" in result.notes


def test_ubl_inside_peppol_envelope():
    wrapped = (
        '<StandardBusinessDocument xmlns="http://www.unece.org/cefact/namespaces/StandardBusinessDocumentHeader">'
        "<StandardBusinessDocumentHeader><HeaderVersion>1.0</HeaderVersion></StandardBusinessDocumentHeader>"
        + ubl("<cbc:ID>WRAPPED-1</cbc:ID>")
        + "</StandardBusinessDocument>"
    )
    assert values(parse_einvoice(wrapped, source="ev"), F.INVOICE_NUMBER) == ["WRAPPED-1"]


def test_other_xml_vocabularies_are_not_einvoices():
    with pytest.raises(StructuredFormatError):
        parse_einvoice(b"<rss><channel/></rss>", source="ev")
    with pytest.raises(UnsafeXMLError):
        parse_einvoice(XXE, source="ev")


# --------------------------------------------------------------------------- HTML


def test_jsonld_invoice():
    [result] = extract_html_structured(fixture("html/invoice_jsonld.html"), source="ev_h")
    assert result.kind == "html_jsonld_invoice"
    assert values(result, F.INVOICE_NUMBER) == ["CLOUD-88213"]
    assert values(result, F.GROSS_AMOUNT) == [Decimal("39.00")]
    assert values(result, F.CURRENCY) == ["EUR"]
    assert values(result, F.DUE_DATE) == [date(2026, 10, 1)]
    assert values(result, F.SUPPLIER_TAX_ID) == ["IE1234567T"]
    assert values(result, F.CUSTOMER_TAX_ID) == ["PT516123459"]
    assert result.extras["supplier_name"] == "CloudHost Europe"
    observation = result.fields[F.GROSS_AMOUNT][0]
    assert observation.method is ExtractionMethod.HTML_STRUCTURED
    assert observation.location == "json-ld[0].@graph[0].totalPaymentDue"


def test_microdata_order_with_nested_invoice():
    order, invoice = extract_html_structured(fixture("html/order_microdata.html"), source="ev_m")
    assert order.kind == "html_microdata_order"
    assert values(order, F.GROSS_AMOUNT) == [Decimal("24.90")]
    assert order.fields[F.GROSS_AMOUNT][0].confidence < invoice.fields[F.INVOICE_NUMBER][0].confidence
    assert values(order, F.SUPPLIER_TAX_ID) == ["PT509123457"]
    assert F.INVOICE_NUMBER not in order.fields  # an order number is not an invoice number
    assert order.extras["order_number"] == "112-4417"
    assert order.extras["supplier_name"] == "Papelaria Lusa"
    assert invoice.kind == "html_microdata_invoice"
    assert values(invoice, F.INVOICE_NUMBER) == ["FT PL/901"]
    assert values(invoice, F.DUE_DATE) == [date(2026, 9, 30)]


def test_html_edge_cases():
    html = (
        '<script type="application/ld+json">{not json</script>'
        '<script type="application/ld+json">[{"@type": ["schema:Invoice"], '
        '"schema:totalPaymentDue": {"@type": "MonetaryAmount", "value": "12,5O", "currency": "$"}}]</script>'
        '<span itemprop="price">5.00</span>'  # itemprop outside any item: ignored
    )
    broken, invoice = extract_html_structured(html, source="ev")
    assert broken.notes == ("jsonld_unreadable",)
    assert invoice.fields == {}
    assert any(n.startswith("unreadable_amount:") for n in invoice.notes)
    assert any(n.startswith("unreadable_currency:") for n in invoice.notes)
    assert extract_html_structured("<p>No markup here</p>", source="ev") == ()


# --------------------------------------------------------------------------- JSON API


PAYLOAD = b"""{"data": {"number": "INV-9", "total": 48360, "currency": "eur", "created": 1789718400,
  "vat": 90.43, "lines": [{"ref": "RF18539007547034"}], "due": "18/10/2026", "alt_number": "INV-10",
  "supplier": {"name": "Vendor"}}}"""


def test_json_mapper_key_paths_minor_units_and_dates():
    mapper = JsonFieldMapper(
        {
            F.INVOICE_NUMBER: "data.number",
            F.GROSS_AMOUNT: FieldPath("$.data.total", minor_units=True),
            F.VAT_AMOUNT: "data.vat",
            F.CURRENCY: "data.currency",
            F.ISSUE_DATE: FieldPath("data.created", date_format="unix"),
            F.DUE_DATE: FieldPath("data.due", date_format="%d/%m/%Y"),
            F.PAYMENT_REFERENCE: "data.lines[0].ref",
            F.IBAN: "data.missing.path",
        },
        extras={"supplier_name": "data.supplier.name"},
    )
    result = mapper.extract(PAYLOAD, source="ev_api")
    assert values(result, F.INVOICE_NUMBER) == ["INV-9"]
    assert values(result, F.GROSS_AMOUNT) == [Decimal("483.60")]
    assert values(result, F.VAT_AMOUNT) == [Decimal("90.43")]
    assert type(values(result, F.VAT_AMOUNT)[0]) is Decimal  # parsed without float
    assert values(result, F.CURRENCY) == ["EUR"]
    assert values(result, F.ISSUE_DATE) == [date(2026, 9, 18)]
    assert values(result, F.DUE_DATE) == [date(2026, 10, 18)]
    assert values(result, F.PAYMENT_REFERENCE) == ["RF18539007547034"]
    assert F.IBAN not in result.fields
    assert result.extras == {"supplier_name": "Vendor"}
    assert result.fields[F.GROSS_AMOUNT][0].location == "$.data.total"
    assert result.fields[F.GROSS_AMOUNT][0].method is ExtractionMethod.API


def test_json_mapper_reports_every_path_and_unreadable_values():
    mapper = JsonFieldMapper(
        {
            F.INVOICE_NUMBER: ["data.number", "data.alt_number"],
            F.GROSS_AMOUNT: FieldPath("data.vat", minor_units=True),
        }
    )
    result = mapper.extract(PAYLOAD, source="ev")
    assert values(result, F.INVOICE_NUMBER) == ["INV-9", "INV-10"]  # disagreement kept for §19
    assert values(result, F.GROSS_AMOUNT) == []
    assert "unreadable:gross_amount:data.vat" in result.notes


def test_json_mapper_input_validation():
    with pytest.raises(StructuredDataError):
        JsonFieldMapper({F.INVOICE_NUMBER: "a"}).extract(b"{broken", source="ev")
    with pytest.raises(ValueError):
        FieldPath("a..b")
    with pytest.raises(ValueError):
        FieldPath("$")
    with pytest.raises(ValueError):
        JsonFieldMapper({F.INVOICE_NUMBER: "a"}, extras={"name": "a..b"})
    assert resolve_path({"a": [{"b": 1}]}, "a[0].b") == 1
    assert resolve_path({"a": [{"b": 1}]}, "a[3].b") is None
    assert resolve_path({"a": 1}, "a.b") is None
    python_payload = {"total": 12.5, "ts": "not-a-number"}
    result = JsonFieldMapper(
        {F.GROSS_AMOUNT: "total", F.ISSUE_DATE: FieldPath("ts", date_format="unix")}
    ).extract(python_payload, source="ev")
    assert values(result, F.GROSS_AMOUNT) == [Decimal("12.5")]
    assert "unreadable:issue_date:ts" in result.notes


# --------------------------------------------------------------------------- PDF


class _FakePage:
    def __init__(self, text):
        self._text = text

    def extract_text(self):
        return self._text


def fake_pypdf(texts, *, attachments=None, metadata=None, encrypted=False):
    module = types.ModuleType("pypdf")

    class PdfReader:
        def __init__(self, stream):
            assert stream.read().startswith(b"%PDF")
            self.pages = [_FakePage(t) for t in texts]
            self.attachments = attachments or {}
            self.metadata = metadata or {}
            self.is_encrypted = encrypted

        def decrypt(self, password):
            return 0

    module.PdfReader = PdfReader
    return module


def test_pdf_reader_is_optional(monkeypatch):
    monkeypatch.setitem(sys.modules, "pypdf", None)
    with pytest.raises(MissingDependencyError) as info:
        read_pdf(b"%PDF-1.7")
    assert "pip install pypdf" in str(info.value)
    [result] = extract_structured(b"%PDF-1.7 ...", source="ev")
    assert result.notes == ("pdf_reader_unavailable",)


def test_pdf_text_and_embedded_xml(monkeypatch):
    text = "Invoice number: FT 1/2\nTotal: 10,00 EUR\n" + "padding " * 5
    monkeypatch.setitem(
        sys.modules,
        "pypdf",
        fake_pypdf(
            [text], attachments={"factur-x.xml": [fixture("einvoice/cii_invoice.xml")], "logo.png": [b"x"]}
        ),
    )
    results = extract_structured(b"%PDF-1.7", source="ev_pdf", text_extractor=LabelledFieldExtractor())
    embedded, text_result = results
    assert embedded.kind == "pdf_attachment_cii"
    assert "embedded_file:factur-x.xml" in embedded.notes
    assert values(embedded, F.GROSS_AMOUNT) == [Decimal("238.00")]
    assert text_result.kind == "pdf_text"
    assert values(text_result, F.GROSS_AMOUNT) == [Decimal("10.00")]
    assert text_result.fields[F.GROSS_AMOUNT][0].method is ExtractionMethod.EMBEDDED_TEXT


def test_pdf_text_layer_made_by_ocr_is_downgraded(monkeypatch):
    content = PdfContent(
        page_texts=("Total: 10,00 EUR\nand a few more words here",), metadata={"Producer": "OCRmyPDF 16"}
    )
    [result] = extract_from_pdf(content, source="ev", text_extractor=LabelledFieldExtractor())
    assert result.notes == ("text_layer_from_ocr",)
    assert result.fields[F.GROSS_AMOUNT][0].method is ExtractionMethod.OCR


def test_pdf_without_text_layer_and_broken_files(monkeypatch):
    monkeypatch.setitem(sys.modules, "pypdf", fake_pypdf(["", "  "]))
    assert extract_from_pdf(b"%PDF-1.7", source="ev", text_extractor=LabelledFieldExtractor()) == ()
    monkeypatch.setitem(sys.modules, "pypdf", fake_pypdf(["x"], encrypted=True))
    [result] = extract_structured(b"%PDF-1.7", source="ev")
    assert result.notes == ("pdf_encrypted",)
    monkeypatch.setitem(sys.modules, "pypdf", fake_pypdf(["x"], attachments={"bad.xml": [b"<rss/>"]}))
    [skipped] = extract_from_pdf(b"%PDF-1.7", source="ev")
    assert skipped.notes == ("attachment_skipped:not_einvoice",)


# --------------------------------------------------------------------------- QR

EPC = "BCD\n002\n1\nSCT\nBPOTPTPL\nHazel Tree Lda\nPT50000201231234567890154\nEUR483.60\n\nRF18539007547034\n"


def test_epc_qr_payment_code():
    result = parse_epc_qr(EPC, "ev_qr")
    assert values(result, F.IBAN) == ["PT50000201231234567890154"]
    assert values(result, F.CURRENCY) == ["EUR"]
    assert values(result, F.PAYMENT_REFERENCE) == ["RF18539007547034"]
    assert F.GROSS_AMOUNT not in result.fields  # an amount to pay is not the invoice total
    assert result.extras["amount_payable"] == "483.60"
    assert result.extras["beneficiary_name"] == "Hazel Tree Lda"
    assert result.fields[F.IBAN][0].method is ExtractionMethod.QR


def test_epc_qr_rejections():
    assert parse_epc_qr("A:123*B:456", "ev") is None
    with pytest.raises(ValueError):
        parse_epc_qr("BCD\n009\n1\nSCT", "ev")
    with pytest.raises(ValueError):
        parse_epc_qr(EPC.replace("EUR483.60", "USD483.60"), "ev")
    bad_iban = parse_epc_qr(EPC.replace("PT500002", "PT510002"), "ev")
    assert F.IBAN not in bad_iban.fields
    assert "epc_qr_invalid_iban" in bad_iban.notes


class _FiscalQR:
    """Shape of a country pack's FiscalQRResult."""

    def __init__(self, observations, consistent=True, usable=True):
        self.observations = observations
        self.consistent = consistent
        self.usable = usable
        self.doc_type = DocumentType.INVOICE
        self.country = "PT"
        self.notes = ("w1",)


class _Named(FieldObservation):
    field: F


def fake_pt_parse(payload, evidence_id):
    if not payload.startswith("A:"):
        return None
    if "O:" not in payload:
        raise ValueError("fiscal_qr_malformed")
    gross = payload.split("O:")[1].split("*")[0]
    return _FiscalQR(
        [
            _Named(
                value=Decimal(gross),
                source=evidence_id,
                method=ExtractionMethod.QR,
                confidence=0.95,
                field=F.GROSS_AMOUNT,
            )
        ],
        consistent=gross == "483.60",
    )


def test_qr_hook_with_country_handler_and_malformed_payloads():
    hook = QRHook()
    hook.register(fiscal_qr_handler(fake_pt_parse), first=True)
    results = hook.extract(["A:1*O:483.60", "A:1*O:438.60", "A:broken", EPC, "unknown", "", EPC], source="ev")
    pt_ok, pt_bad, malformed, epc = results
    assert pt_ok.kind == "fiscal_qr_pt"
    assert pt_ok.doc_type is DocumentType.INVOICE
    assert values(pt_ok, F.GROSS_AMOUNT) == [Decimal("483.60")]
    assert "fiscal_qr_inconsistent" not in pt_ok.notes
    # An inconsistent code keeps its observation so the conflict surfaces (§19).
    assert values(pt_bad, F.GROSS_AMOUNT) == [Decimal("438.60")]
    assert "fiscal_qr_inconsistent" in pt_bad.notes
    assert malformed.fields == {} and malformed.notes == ("qr_unreadable:fiscal_qr_malformed",)
    assert epc.kind == "epc_qr"


def test_zxing_decoder_is_optional_and_filters_formats(monkeypatch):
    monkeypatch.setitem(sys.modules, "zxingcpp", None)
    with pytest.raises(MissingDependencyError):
        ZXingDecoder().decode(b"img")

    class Result:
        def __init__(self, text, fmt, valid=True):
            self.text, self.format, self.valid = text, fmt, valid

    zxing = types.ModuleType("zxingcpp")
    zxing.read_barcodes = lambda img: [
        Result("A:1*B:2", "BarcodeFormat.QRCode"),
        Result("5601234567890", "BarcodeFormat.EAN13"),
        Result("junk", "BarcodeFormat.QRCode", valid=False),
    ]

    class Image:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    pil = types.ModuleType("PIL")
    pil_image = types.ModuleType("PIL.Image")
    pil_image.open = lambda stream: Image()
    pil.Image = pil_image
    monkeypatch.setitem(sys.modules, "zxingcpp", zxing)
    monkeypatch.setitem(sys.modules, "PIL", pil)
    monkeypatch.setitem(sys.modules, "PIL.Image", pil_image)
    assert [c.text for c in ZXingDecoder().decode(b"img")] == ["A:1*B:2"]
    assert len(ZXingDecoder(qr_only=False).decode(b"img")) == 2


# --------------------------------------------------------------------------- dispatcher


def test_extract_structured_dispatches_on_content():
    [xml] = extract_structured(fixture("einvoice/ubl_invoice.xml"), source="ev", mime_type="application/pdf")
    assert xml.kind == "ubl_invoice"
    [rss] = extract_structured(b"<rss/>", source="ev")
    assert rss.fields == {} and rss.notes == ("not_einvoice",)
    [broken] = extract_structured(b"<a><b></a>", source="ev")
    assert broken.notes == ("xml_syntax",)
    assert len(extract_structured(fixture("html/order_microdata.html"), source="ev")) == 2
    assert extract_structured(b'{"a": 1}', source="ev") == ()
    mapper = JsonFieldMapper({F.INVOICE_NUMBER: "a"})
    [api] = extract_structured(b'{"a": 1}', source="ev", json_mapper=mapper)
    assert values(api, F.INVOICE_NUMBER) == ["1"]
    assert extract_structured(b"\x89PNG\r\n\x1a\n....", source="ev", qr_payloads=[EPC])[0].kind == "epc_qr"
    with pytest.raises(UnsafeXMLError):
        extract_structured(XXE, source="ev")
