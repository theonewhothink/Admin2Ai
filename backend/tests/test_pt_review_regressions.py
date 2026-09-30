"""Regression tests for defects found in the adversarial review of the Portugal pack.

Each test names the defect it pins down. They were written before the fixes.
"""

import csv
import io
import time
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from test_pt_saft import audit_file, invoice_xml

import backoffice.countries.pt.qr as pt_qr
from backoffice.countries import CountryPackError, get_pack
from backoffice.countries.base import VATBucket
from backoffice.countries.pt import (
    ATCUDError,
    MultibancoError,
    QRCodeError,
    RateCheck,
    RateDataUnavailable,
    export_ledger_csv,
    extract_text_fields,
    is_plausible_rate,
    is_valid_iban,
    ledger_row_from_document,
    parse_atcud,
    parse_multibanco,
    parse_qr,
    parse_saft_sales_invoices,
    rate_for,
    saft_invoice_observations,
    validate_nif,
)
from backoffice.countries.pt.qr import FIELD_ORDER
from backoffice.countries.pt.vat import check_rate
from backoffice.domain.models import (
    CriticalField,
    Document,
    DocumentType,
    ExtractionMethod,
    Quality,
)

D = Decimal

QR_FIELDS = {
    "A": "509123457", "B": "516123459", "C": "PT", "D": "FT", "E": "N", "F": "20260918",
    "G": "FT 2026/183", "H": "CSDF7T5H-183", "I1": "PT", "I7": "393.17", "I8": "90.43",
    "N": "90.43", "O": "483.60", "Q": "kLp0", "R": "2345",
}


def qr(**changes: str | None) -> str:
    fields = {**QR_FIELDS, **changes}
    return "*".join(f"{k}:{fields[k]}" for k in FIELD_ORDER if fields.get(k) is not None)


# --------------------------------------------------------------------------- #
# VAT table (regulatory facts) and determinism
# --------------------------------------------------------------------------- #


def test_azores_reduced_and_intermediate_4_9_only_from_2015_07_01():
    """Lei 63-A/2015: Azores 5%->4% and 10%->9% took effect on 2015-07-01, not 2015-01-01.

    The first half of 2015 is not covered by the table, so it must answer
    UNKNOWN instead of wrongly accepting 4% (or rejecting the real 5%).
    """
    spring_2015 = date(2015, 3, 1)
    assert check_rate(D("100.00"), D("4.00"), "PT-AC", spring_2015) == RateCheck.UNKNOWN
    assert check_rate(D("100.00"), D("5.00"), "PT-AC", spring_2015) == RateCheck.UNKNOWN
    assert rate_for("PT-AC", VATBucket.REDUCED, date(2015, 7, 1)) == D("0.04")
    assert rate_for("PT-AC", VATBucket.INTERMEDIATE, date(2015, 7, 1)) == D("0.09")
    assert rate_for("PT-AC", VATBucket.NORMAL, date(2015, 7, 1)) == D("0.18")


def _clock_says_2019(monkeypatch):
    fake = lambda: datetime(2019, 6, 1, tzinfo=UTC)  # noqa: E731
    monkeypatch.setattr("backoffice.countries.pt.vat.utcnow", fake, raising=False)
    monkeypatch.setattr("backoffice.countries.pt.pack.utcnow", fake, raising=False)


def test_undated_rate_check_does_not_depend_on_the_wall_clock(monkeypatch):
    """Without a date the check uses the rates currently in the table, never the clock."""
    _clock_says_2019(monkeypatch)
    assert is_plausible_rate(D("100.00"), D("16.00"), "PT-AC")  # 16% since 2021-07-01
    assert not is_plausible_rate(D("100.00"), D("18.00"), "PT-AC")
    pack = get_pack("PT")
    assert pack.is_plausible_vat(D("100.00"), D("16.00"), region="PT-AC") is True


def test_pack_answers_unknown_for_a_region_it_does_not_cover():
    """The protocol returns bool | None; a foreign region must not raise."""
    pack = get_pack("PT")
    day = date(2026, 9, 18)
    assert pack.is_plausible_vat(D("100.00"), D("21.00"), on=day, region="ES") is None
    assert pack.is_plausible_vat(D("100.00"), D("21.00"), on=day, region="nonsense") is None
    assert pack.vat_rates(day, "ES") == ()


# --------------------------------------------------------------------------- #
# Error types carry owner-safe messages
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "action",
    [
        lambda: parse_atcud("nope"),
        lambda: parse_multibanco("12", "34"),
        lambda: is_plausible_rate(D("100.00"), D("20.00"), "PT", date(2001, 1, 1)),
    ],
)
def test_module_errors_are_country_pack_errors_with_plain_messages(action):
    with pytest.raises(CountryPackError) as info:
        action()
    message = info.value.owner_message
    assert message and "Error" not in message and "ATCUD" not in message
    # Still the ValueError / LookupError callers already catch.
    assert isinstance(info.value, (ValueError, LookupError))


def test_error_classes_keep_their_standard_bases():
    assert issubclass(ATCUDError, ValueError) and issubclass(ATCUDError, CountryPackError)
    assert issubclass(MultibancoError, ValueError) and issubclass(MultibancoError, CountryPackError)
    assert issubclass(RateDataUnavailable, LookupError)
    assert issubclass(RateDataUnavailable, CountryPackError)


# --------------------------------------------------------------------------- #
# ATCUD / NIF / IBAN
# --------------------------------------------------------------------------- #


def test_atcud_label_is_only_stripped_when_it_is_a_label():
    """A validation code that happens to begin with the letters ATCUD must survive."""
    assert parse_atcud("ATCUDBCD-5").validation_code == "ATCUDBCD"
    assert parse_atcud("ATCUD: ATCUDBCD-5").validation_code == "ATCUDBCD"
    assert parse_atcud("atcud CSDF7T5H-35").validation_code == "CSDF7T5H"


def test_nif_without_any_digit_is_a_character_problem():
    check = validate_nif("...")
    assert not check.valid and check.problem == "characters"
    assert "0 digits" not in check.message


def test_iban_with_non_ascii_digits_is_rejected():
    ascii_iban = "PT50000201231234567890154"
    assert is_valid_iban(ascii_iban)
    assert not is_valid_iban(ascii_iban.replace("0", "٠"))  # Arabic-Indic zero


# --------------------------------------------------------------------------- #
# Fiscal QR
# --------------------------------------------------------------------------- #


def test_oversized_qr_payload_is_rejected_up_front():
    payload = qr(S="y" * (pt_qr.MAX_PAYLOAD_CHARS + 10))
    with pytest.raises(QRCodeError) as info:
        parse_qr(payload)
    assert [i.code for i in info.value.issues] == ["payload_too_long"]


@pytest.mark.parametrize(
    ("doc", "number", "atcud", "kind"),
    [("PF", "PF 2026/7", "CSDF7T5H-7", DocumentType.PRO_FORMA), ("OR", "OR 2026/7", "CSDF7T5H-7", DocumentType.QUOTE),
     ("GT", "GT 2026/7", "CSDF7T5H-7", DocumentType.DELIVERY_NOTE)],
)
def test_non_fiscal_documents_are_not_usable_as_payment_evidence(doc, number, atcud, kind):
    """A pro-forma, quote or transport document QR cannot support a payment: each is its own
    supporting-only kind."""
    result = get_pack("PT").parse_fiscal_qr(qr(D=doc, G=number, H=atcud), "ev")
    assert result is not None and result.doc_type == kind
    assert result.usable is False


def test_invoice_receipt_and_receipt_qr_stay_usable():
    pack = get_pack("PT")
    assert pack.parse_fiscal_qr(qr(D="FR", G="FR 2026/183"), "ev").usable
    receipt = qr(D="RG", G="RG 2026/9", H="CSDF7T5H-9", I1="0", I7=None, I8=None, N="0.00",
                 O="500.00")
    assert pack.parse_fiscal_qr(receipt, "ev").usable


# --------------------------------------------------------------------------- #
# Text fields
# --------------------------------------------------------------------------- #


def test_repeated_labels_do_not_blow_up_quadratically():
    """Untrusted text (e-mail attachments) must not stall extraction."""
    started = time.perf_counter()
    result = extract_text_fields("iva " * 60000, "ev")
    assert time.perf_counter() - started < 5.0
    assert result.observations == ()


def test_label_overlap_resolution_still_prefers_the_longest_label():
    result = extract_text_fields("Total c/ IVA: 483,60 €  IVA: 90,43", "ev")
    values = {o.field: o.value for o in result.observations}
    assert values == {CriticalField.GROSS_AMOUNT: D("483.60"), CriticalField.VAT_AMOUNT: D("90.43")}


def test_v_slash_contribuinte_is_the_customer_and_n_slash_the_supplier():
    """Portuguese invoices write "V/ Contribuinte" (yours) and "N/ Contribuinte" (ours)."""
    for text in (
        "N/ Contribuinte: 509123457\nV/ Contribuinte: 516123459\n",
        "N/NIF 509 123 457    V/ N.º Contribuinte: 516123459\n",
    ):
        values = {o.field: o.value for o in extract_text_fields(text, "ev").observations}
        assert values[CriticalField.CUSTOMER_TAX_ID] == "516123459", text
        assert values[CriticalField.SUPPLIER_TAX_ID] == "509123457", text
    # "s/n" (no street number) in an address is not a possessive marker.
    address = extract_text_fields("Rua das Flores s/n NIF 509123457", "ev")
    assert address.unassigned_tax_ids == ("509123457",)


# --------------------------------------------------------------------------- #
# SAF-T
# --------------------------------------------------------------------------- #

IVA_LINE = (
    "<Line><CreditAmount>1000.00</CreditAmount><Tax><TaxType>IVA</TaxType>"
    "<TaxCountryRegion>PT</TaxCountryRegion><TaxCode>NOR</TaxCode>"
    "<TaxPercentage>23</TaxPercentage></Tax></Line>"
)


def test_percentage_stamp_duty_is_not_counted_as_vat():
    """Stamp duty charged as a percentage (e.g. 4% on a credit) is part of TaxPayable, not VAT."""
    lines = IVA_LINE + (
        "<Line><CreditAmount>500.00</CreditAmount><Tax><TaxType>IS</TaxType>"
        "<TaxCountryRegion>PT</TaxCountryRegion><TaxCode>17.2.1</TaxCode>"
        "<TaxPercentage>4</TaxPercentage></Tax></Line>"
    )
    xml = audit_file(invoice_xml(lines=lines, totals=("250.00", "1500.00", "1750.00")),
                     credit="1500.00")
    (inv,) = parse_saft_sales_invoices(xml).invoices
    assert inv.stamp_duty == D("20.00")
    assert inv.vat_total == D("230.00")


def test_saft_observations_surface_an_arithmetic_conflict():
    """GrossTotal that disagrees with NetTotal + TaxPayable must not be reported as verified-grade."""
    result = parse_saft_sales_invoices(audit_file(invoice_xml(totals=("90.43", "393.17", "438.60"))))
    (inv,) = result.invoices
    observations = saft_invoice_observations(inv, result.header, "ev_saft")
    gross = [o for o in observations if o.field == CriticalField.GROSS_AMOUNT]
    by_method = {o.method: o for o in gross}
    assert by_method[ExtractionMethod.STRUCTURED_XML].value == D("438.60")
    assert by_method[ExtractionMethod.STRUCTURED_XML].confidence <= 0.4
    arithmetic = by_method[ExtractionMethod.ARITHMETIC]
    assert arithmetic.value == D("483.60") and arithmetic.source == "ev_saft"
    # Same channel as the file itself: it must never count as independent confirmation.
    assert arithmetic.location.startswith("structured_xml:")


def test_consistent_saft_invoice_keeps_full_confidence_and_no_arithmetic_noise():
    result = parse_saft_sales_invoices(audit_file(invoice_xml()))
    observations = saft_invoice_observations(result.invoices[0], result.header, "ev")
    assert {o.method for o in observations} == {ExtractionMethod.STRUCTURED_XML}
    assert min(o.confidence for o in observations) > 0.9


def test_cancelled_saft_invoice_yields_no_observations_by_default():
    xml = audit_file(invoice_xml(status="A"), credit="0.00")
    result = parse_saft_sales_invoices(xml)
    (inv,) = result.invoices
    assert saft_invoice_observations(inv, result.header, "ev") == []
    assert saft_invoice_observations(inv, result.header, "ev", include_cancelled=True)


def test_duplicate_invoice_numbers_are_a_file_problem():
    xml = audit_file(invoice_xml() + invoice_xml(), credit="786.34")
    result = parse_saft_sales_invoices(xml)
    assert any("FT 2026/183" in p and "more than once" in p for p in result.problems)


def test_atcud_that_does_not_belong_to_the_invoice_is_a_problem():
    (inv,) = parse_saft_sales_invoices(audit_file(invoice_xml(atcud="CSDF7T5H-184"))).invoices
    assert any("ATCUD" in p for p in inv.problems)
    (bad,) = parse_saft_sales_invoices(audit_file(invoice_xml(atcud="short-1"))).invoices
    assert any("ATCUD" in p for p in bad.problems)
    (none,) = parse_saft_sales_invoices(audit_file(invoice_xml(atcud="0"))).invoices
    assert none.problems == ()


def test_unknown_invoice_type_is_a_problem():
    (inv,) = parse_saft_sales_invoices(audit_file(invoice_xml(doc_type="XX"))).invoices
    assert inv.doc_type == DocumentType.OTHER
    assert any("InvoiceType" in p for p in inv.problems)


# --------------------------------------------------------------------------- #
# Ledger export
# --------------------------------------------------------------------------- #


def _doc(**overrides) -> Document:
    base = dict(tenant_id="t", evidence_ids=["ev_1"], doc_type=DocumentType.INVOICE,
                supplier_name="Fornecedora", supplier_tax_id="509123457",
                invoice_number="FT 2026/183", issue_date=date(2026, 9, 18),
                net_amount=D("393.17"), vat_amount=D("90.43"), gross_amount=D("483.60"),
                quality=Quality.GREEN)
    base.update(overrides)
    return Document(**base)


@pytest.mark.parametrize("period", ["../../etc/passwd", "2026/09", "a b", "", "x" * 80])
def test_ledger_period_cannot_inject_a_path(period):
    with pytest.raises(ValueError):
        export_ledger_csv([], period=period)


def test_ledger_period_accepts_plain_labels():
    assert export_ledger_csv([], period="2026-09").filename == "ledger-2026-09-subset-not-saft.csv"


def test_credit_note_with_already_negative_amounts_stays_negative():
    doc = _doc(doc_type=DocumentType.CREDIT_NOTE, invoice_number="NC 2026/4",
               net_amount=D("-393.17"), vat_amount=D("-90.43"), gross_amount=D("-483.60"))
    row = ledger_row_from_document(doc)
    assert (row.net, row.vat, row.gross) == (D("-393.17"), D("-90.43"), D("-483.60"))
    positive = ledger_row_from_document(_doc(doc_type=DocumentType.CREDIT_NOTE,
                                             invoice_number="NC 2026/4"))
    assert positive.gross == D("-483.60")


def test_ledger_order_is_deterministic_for_equal_date_and_number():
    a = ledger_row_from_document(_doc(supplier_tax_id="509123457", evidence_ids=["ev_a"]))
    b = ledger_row_from_document(_doc(supplier_tax_id="516123459", evidence_ids=["ev_b"]))
    first = export_ledger_csv([a, b]).csv
    second = export_ledger_csv([b, a]).csv
    assert first == second
    rows = list(csv.reader(io.StringIO(first), delimiter=";"))
    assert [r[4] for r in rows[1:]] == ["509123457", "516123459"]


def test_stamp_duty_with_a_fixed_amount_is_not_also_counted_by_percentage():
    lines = IVA_LINE + (
        "<Line><CreditAmount>500.00</CreditAmount><Tax><TaxType>IS</TaxType>"
        "<TaxCountryRegion>PT</TaxCountryRegion><TaxCode>17.2.1</TaxCode>"
        "<TaxPercentage>4</TaxPercentage><TaxAmount>20.00</TaxAmount></Tax></Line>"
    )
    xml = audit_file(invoice_xml(lines=lines, totals=("250.00", "1500.00", "1750.00")),
                     credit="1500.00")
    (inv,) = parse_saft_sales_invoices(xml).invoices
    assert inv.stamp_duty == D("20.00") and inv.vat_total == D("230.00")
    assert inv.problems == ()


def test_stamp_duty_alone_is_not_a_tax_breakdown():
    """I1:0 with only M: O cannot be rebuilt from parts, so no false conflict and no net 0.00."""
    code = parse_qr(qr(D="RG", G="RG 2026/9", H="CSDF7T5H-9", I1="0", I7=None, I8=None,
                       M="10.00", N="10.00", O="510.00"))
    assert code.is_consistent and code.checks.gross_total_ok is None
    from backoffice.countries.pt import qr_to_observations

    fields = {o.field for o in qr_to_observations(code, "ev")}
    assert CriticalField.NET_AMOUNT not in fields and CriticalField.VAT_AMOUNT not in fields
    # With a real breakdown the stamp duty still counts towards O.
    detailed = parse_qr(qr(M="10.00", N="100.43", O="493.60"))
    assert detailed.is_consistent and detailed.checks.gross_total_expected == D("493.60")


def test_amount_to_pay_is_not_the_gross_total():
    """With tax withheld, "Total a pagar" = gross - withholding: a separate concept."""
    from backoffice.countries.pt import lookup_term

    for label in ("Total a pagar", "Valor a pagar", "A pagar"):
        assert lookup_term(label).concept == "amount_payable", label
    assert lookup_term("Total").concept == "gross_amount"
    assert lookup_term("Total do documento").concept == "gross_amount"


def test_default_saft_size_limit_is_bounded_for_memory():
    from backoffice.countries.pt.saft import DEFAULT_MAX_BYTES

    assert DEFAULT_MAX_BYTES <= 64 * 1024 * 1024


def test_saft_vat_that_contradicts_the_line_rates_is_a_conflict():
    """NetTotal 393.17 at 23% is 90.43 VAT; TaxPayable 90.00 (with a matching gross) is not."""
    result = parse_saft_sales_invoices(audit_file(invoice_xml(totals=("90.00", "393.17", "483.17"))))
    (inv,) = result.invoices
    assert inv.lines_vat == D("90.43")
    assert any("TaxPayable" in p for p in inv.problems)
    vat = [o for o in saft_invoice_observations(inv, result.header, "ev")
           if o.field == CriticalField.VAT_AMOUNT]
    by_method = {o.method: o for o in vat}
    assert by_method[ExtractionMethod.STRUCTURED_XML].value == D("90.00")
    assert by_method[ExtractionMethod.STRUCTURED_XML].confidence <= 0.4
    assert by_method[ExtractionMethod.ARITHMETIC].value == D("90.43")


def test_saft_per_line_rounding_is_not_a_vat_conflict():
    """Three lines of 0.10 at 23%: per-line VAT 0.02 x 3 = 0.06; on the total it is 0.07."""
    line = ("<Line><CreditAmount>0.10</CreditAmount><Tax><TaxType>IVA</TaxType>"
            "<TaxCountryRegion>PT</TaxCountryRegion><TaxCode>NOR</TaxCode>"
            "<TaxPercentage>23</TaxPercentage></Tax></Line>")
    xml = audit_file(invoice_xml(lines=line * 3, totals=("0.06", "0.30", "0.36")), credit="0.30")
    (inv,) = parse_saft_sales_invoices(xml).invoices
    assert inv.problems == ()
