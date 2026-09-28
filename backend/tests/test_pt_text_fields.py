"""Candidate fields from Portuguese OCR / plain text (§13, §18, §19 never guess)."""

from datetime import date
from decimal import Decimal

import pytest

from backoffice.countries.pt import extract_text_fields, parse_pt_amount, parse_pt_date
from backoffice.domain.models import CriticalField as F
from backoffice.domain.models import ExtractionMethod

SUPPLIER = "509123457"
CUSTOMER = "516123459"
PHONE_LIKE_VALID_NIF = "212345672"  # passes the NIF check digit, looks like a Lisbon phone

INVOICE = f"""FORNECEDORA DE TESTE, S.A.
Av. da Liberdade 1, Lisboa
NIF: {SUPPLIER}   Tel: {PHONE_LIKE_VALID_NIF[:3]} {PHONE_LIKE_VALID_NIF[3:6]} {PHONE_LIKE_VALID_NIF[6:]}
Fatura n.º FT 2026/183
ATCUD: CSDF7T5H-183
Data de emissão: 18/09/2026
Data de vencimento: 18-10-2026
Cliente
Hazel Tree Lda
NIF: PT {CUSTOMER[:3]} {CUSTOMER[3:6]} {CUSTOMER[6:]}
Descrição            Qtd   Preço    Total
Serviço móvel         1   393,17   393,17
Base tributável (23%): 393,17
IVA 23%: 90,43
Total: 483,60 €
Pagamento por Multibanco
Entidade: 21800
Referência: 123 456 789
Montante: 483,60 €
IBAN PT50 0002 0123 1234 5678 9015 4
"""


def values(result):
    return {o.field: o.value for o in result.observations}


def test_full_invoice():
    result = extract_text_fields(INVOICE, "ev_ocr")
    assert values(result) == {
        F.GROSS_AMOUNT: Decimal("483.60"),
        F.NET_AMOUNT: Decimal("393.17"),
        F.VAT_AMOUNT: Decimal("90.43"),
        F.ISSUE_DATE: date(2026, 9, 18),
        F.DUE_DATE: date(2026, 10, 18),
        F.INVOICE_NUMBER: "FT 2026/183",
        F.SUPPLIER_TAX_ID: SUPPLIER,
        F.CUSTOMER_TAX_ID: CUSTOMER,
        F.IBAN: "PT50000201231234567890154",
        F.PAYMENT_REFERENCE: "21800 123456789",
    }
    assert result.ambiguous == {}
    assert str(result.atcud) == "CSDF7T5H-183"
    assert result.multibanco.amount == Decimal("483.60")
    for obs in result.observations:
        assert obs.method == ExtractionMethod.OCR
        assert obs.source == "ev_ocr"
        assert 0 < obs.confidence <= 0.7  # candidates stay below structured sources
        assert isinstance(obs.location, str) and obs.location.startswith("text:")
    assert result.get(F.GROSS_AMOUNT).location == "text:line 15"


def test_supplier_is_deduced_only_because_the_customer_is_labelled():
    result = extract_text_fields(INVOICE, "ev")
    supplier = result.get(F.SUPPLIER_TAX_ID)
    customer = result.get(F.CUSTOMER_TAX_ID)
    assert supplier.confidence < customer.confidence


# --------------------------------------------------------------------------- #
# Numbers and dates
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("text", "amount"),
    [("1.492,30", "1492.30"), ("1 492,30", "1492.30"),
     ("1\u00a0492,30", "1492.30"), ("1\u202f492,30", "1492.30"),
     ("1492,30", "1492.30"), ("483.60", "483.60"), ("-10,00", "-10.00"),
     ("0,05", "0.05"), ("12.345.678,90", "12345678.90")],
)
def test_parse_pt_amount(text, amount):
    expected = Decimal(amount) if amount else None
    assert parse_pt_amount(text) == expected


@pytest.mark.parametrize("text", ["1.492", "1,492.30", "23,00%", "1.49,30", "abc", "", "1.492.30"])
def test_parse_pt_amount_rejects_ambiguous_forms(text):
    assert parse_pt_amount(text) is None


def test_amounts_are_decimal_never_float():
    assert type(parse_pt_amount("1.492,30")) is Decimal


@pytest.mark.parametrize(
    ("text", "day"),
    [("18/09/2026", date(2026, 9, 18)), ("18-09-2026", date(2026, 9, 18)),
     ("18.09.2026", date(2026, 9, 18)), ("2026-09-18", date(2026, 9, 18)),
     ("1/9/2026", date(2026, 9, 1)), ("18 de setembro de 2026", date(2026, 9, 18)),
     ("18 set 2026", date(2026, 9, 18)), ("18-Set-2026", date(2026, 9, 18)),
     ("5 de Março de 2026", date(2026, 3, 5))],
)
def test_parse_pt_date(text, day):
    assert parse_pt_date(text) == day


@pytest.mark.parametrize("text", ["31/02/2026", "18/13/2026", "18/09/26", "18/09-2026", "setembro"])
def test_parse_pt_date_rejects_invalid(text):
    assert parse_pt_date(text) is None


# --------------------------------------------------------------------------- #
# Labels and ambiguity
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "line",
    ["Total 1.492,30 €", "TOTAL: € 1.492,30", "Total (EUR): 1.492,30", "Total EUR 1492,30",
     "Total c/ IVA 1.492,30", "Total do documento 1 492,30 €"],
)
def test_gross_total_labels(line):
    assert values(extract_text_fields(line, "ev")) == {F.GROSS_AMOUNT: Decimal("1492.30")}


def test_labels_inside_longer_labels_do_not_leak():
    result = extract_text_fields("Total c/ IVA 483,60\nTotal s/ IVA 393,17\nTotal IVA 90,43", "ev")
    assert values(result) == {
        F.GROSS_AMOUNT: Decimal("483.60"),
        F.NET_AMOUNT: Decimal("393.17"),
        F.VAT_AMOUNT: Decimal("90.43"),
    }


def test_several_labels_on_one_ocr_line():
    result = extract_text_fields("Base Tributável: 393,17 IVA: 90,43 Total: 483,60", "ev")
    assert values(result) == {
        F.NET_AMOUNT: Decimal("393.17"),
        F.VAT_AMOUNT: Decimal("90.43"),
        F.GROSS_AMOUNT: Decimal("483.60"),
    }


def test_disagreeing_values_are_ambiguous_not_guessed():
    result = extract_text_fields("Total 483,60 €\n...\nTotal 438,60 €", "ev")
    assert result.get(F.GROSS_AMOUNT) is None
    assert result.ambiguous[F.GROSS_AMOUNT] == ("483.60", "438.60")


def test_repeated_identical_values_are_fine():
    result = extract_text_fields("Total 483,60 €\nTotal 483,60 €", "ev")
    assert result.get(F.GROSS_AMOUNT).value == Decimal("483.60")


def test_explicit_label_beats_generic_label():
    result = extract_text_fields("Total do documento 100,00\nTotal 90,00", "ev")
    obs = result.get(F.GROSS_AMOUNT)
    assert obs.value == Decimal("100.00") and obs.confidence == 0.6


def test_line_with_two_amounts_after_a_label_is_skipped():
    result = extract_text_fields("IVA 23% 393,17 90,43", "ev")
    assert result.get(F.VAT_AMOUNT) is None


def test_label_followed_by_words_is_not_a_value():
    result = extract_text_fields("Total Ilíquido 100,00\nTotal descontos 0,00\nSubtotal 100,00", "ev")
    assert result.observations == ()


def test_value_on_the_next_line():
    result = extract_text_fields("Total\n1.492,30 €\nData de emissão\n18/09/2026", "ev")
    assert values(result) == {F.GROSS_AMOUNT: Decimal("1492.30"), F.ISSUE_DATE: date(2026, 9, 18)}
    assert result.get(F.GROSS_AMOUNT).location == "text:line 2"


def test_table_headers_do_not_pull_values_from_rows():
    result = extract_text_fields("Descrição  Qtd  Total\n1.492,30", "ev")
    assert result.get(F.GROSS_AMOUNT) is None


def test_withholding_blocks_payable_as_gross():
    text = "Total 1.230,00\nRetenção na fonte IRS 287,50\nTotal a pagar 942,50"
    result = extract_text_fields(text, "ev")
    assert result.get(F.GROSS_AMOUNT).value == Decimal("1230.00")
    assert result.withholding == Decimal("287.50")
    only_payable = extract_text_fields("Retenção IRS: 287,50\nValor a pagar: 942,50", "ev")
    assert only_payable.get(F.GROSS_AMOUNT) is None


def test_payable_without_withholding_is_a_weak_gross():
    obs = extract_text_fields("Total a pagar: 483,60 €", "ev").get(F.GROSS_AMOUNT)
    assert obs.value == Decimal("483.60") and obs.confidence == 0.4


def test_percentages_are_not_amounts():
    assert extract_text_fields("IVA 23,00%", "ev").get(F.VAT_AMOUNT) is None
    assert extract_text_fields("IVA (23%): 90,43", "ev").get(F.VAT_AMOUNT).value == Decimal("90.43")


def test_dates_need_the_right_label():
    result = extract_text_fields("Data de pagamento: 18/10/2026\nData: 18/09/2026", "ev")
    assert values(result) == {F.ISSUE_DATE: date(2026, 9, 18)}
    assert result.get(F.ISSUE_DATE).confidence == 0.5  # generic "Data"
    assert extract_text_fields("Data 01/09/2026 - 30/09/2026", "ev").observations == ()
    assert extract_text_fields("Data de emissão: 31/02/2026", "ev").observations == ()


# --------------------------------------------------------------------------- #
# Tax numbers
# --------------------------------------------------------------------------- #


def test_unlabelled_nine_digit_numbers_are_ignored():
    result = extract_text_fields(f"Tel: {PHONE_LIKE_VALID_NIF}\nReferência interna {SUPPLIER}", "ev")
    assert result.observations == () and result.unassigned_tax_ids == ()


def test_invalid_check_digit_is_ignored():
    result = extract_text_fields("Cliente NIF: 516123450", "ev")
    assert result.observations == ()


def test_role_from_words_on_the_line():
    text = f"NIF Fornecedor: {SUPPLIER}\nNIF do Cliente: {CUSTOMER}"
    assert values(extract_text_fields(text, "ev")) == {
        F.SUPPLIER_TAX_ID: SUPPLIER, F.CUSTOMER_TAX_ID: CUSTOMER}


def test_two_nifs_on_one_line_get_their_own_roles():
    text = f"Emitente NIF {SUPPLIER} | Adquirente NIF {CUSTOMER}"
    assert values(extract_text_fields(text, "ev")) == {
        F.SUPPLIER_TAX_ID: SUPPLIER, F.CUSTOMER_TAX_ID: CUSTOMER}


def test_without_roles_nifs_stay_unassigned():
    result = extract_text_fields(f"NIF: {SUPPLIER}\nContribuinte n.º {CUSTOMER}", "ev")
    assert result.observations == ()
    assert set(result.unassigned_tax_ids) == {SUPPLIER, CUSTOMER}


def test_known_customer_ids_resolve_roles():
    text = f"NIF: {SUPPLIER}\nContribuinte n.º {CUSTOMER}"
    result = extract_text_fields(text, "ev", known_customer_tax_ids=["PT " + CUSTOMER])
    assert values(result) == {F.CUSTOMER_TAX_ID: CUSTOMER, F.SUPPLIER_TAX_ID: SUPPLIER}
    assert result.get(F.CUSTOMER_TAX_ID).confidence == 0.7


def test_known_customer_labelled_as_supplier_is_a_contradiction():
    text = f"NIF Fornecedor: {CUSTOMER}"
    result = extract_text_fields(text, "ev", known_customer_tax_ids=[CUSTOMER])
    assert result.observations == () and result.unassigned_tax_ids == (CUSTOMER,)


def test_contradictory_labels_block_the_supplier_deduction():
    text = f"Cliente NIF {CUSTOMER}\nFornecedor NIF {SUPPLIER}\nCliente NIF {SUPPLIER}"
    result = extract_text_fields(text, "ev")
    assert values(result) == {F.CUSTOMER_TAX_ID: CUSTOMER}
    assert result.unassigned_tax_ids == (SUPPLIER,)


def test_single_nif_is_not_assumed_to_be_the_supplier():
    result = extract_text_fields(f"NIF {SUPPLIER}", "ev")
    assert result.get(F.SUPPLIER_TAX_ID) is None
    assert result.unassigned_tax_ids == (SUPPLIER,)


def test_two_customers_are_ambiguous():
    text = f"Cliente NIF {CUSTOMER}\nCliente NIF {SUPPLIER}"
    result = extract_text_fields(text, "ev")
    assert result.get(F.CUSTOMER_TAX_ID) is None
    assert set(result.ambiguous[F.CUSTOMER_TAX_ID]) == {CUSTOMER, SUPPLIER}


def test_final_consumer():
    result = extract_text_fields("Contribuinte: 999999990", "ev")
    assert result.observations == () and result.buyer_is_final_consumer
    assert extract_text_fields("Cliente: Consumidor Final", "ev").buyer_is_final_consumer


def test_vies_style_nif_without_label():
    result = extract_text_fields(f"Cliente PT{CUSTOMER}", "ev")
    assert result.get(F.CUSTOMER_TAX_ID).value == CUSTOMER


def test_nif_is_not_read_from_an_amount():
    assert extract_text_fields("NIF: 123 456 789,00", "ev").unassigned_tax_ids == ()


# --------------------------------------------------------------------------- #
# Document numbers, ATCUD
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("text", "number"),
    [("FT 2026/183", "FT 2026/183"), ("Fatura-Recibo FR A/123", "FR A/123"),
     ("Documento: FT AB2019/0035", "FT AB2019/0035"),
     ("Fatura n.º PA_2022 2022/00002", "PA_2022 2022/00002"), ("FS  001/12", "FS 001/12")],
)
def test_document_numbers(text, number):
    assert extract_text_fields(text, "ev").get(F.INVOICE_NUMBER).value == number


def test_upper_case_words_and_dates_are_not_document_numbers():
    text = "REFERENTE DA 09/2026\nFT 18/09/2026\nFT2026/183"
    assert extract_text_fields(text, "ev").get(F.INVOICE_NUMBER) is None


def test_atcud_singles_out_the_document_among_several_numbers():
    text = "Recibo RG 2026/12\nreferente à fatura FT 2026/183\nATCUD: ABCD2345-12"
    result = extract_text_fields(text, "ev")
    obs = result.get(F.INVOICE_NUMBER)
    assert obs.value == "RG 2026/12" and obs.confidence == 0.55
    assert result.document_numbers == ("RG 2026/12", "FT 2026/183")


def test_several_numbers_without_atcud_are_ambiguous():
    result = extract_text_fields("NC 2026/5 referente a FT 2026/183", "ev")
    assert result.get(F.INVOICE_NUMBER) is None
    assert result.ambiguous[F.INVOICE_NUMBER] == ("NC 2026/5", "FT 2026/183")


def test_atcud_with_en_dash_and_lower_case_label():
    assert str(extract_text_fields("atcud: CSDF7T5H\u2013183", "ev").atcud) == "CSDF7T5H-183"
    assert extract_text_fields("ATCUD: ABC-1", "ev").atcud is None


# --------------------------------------------------------------------------- #
# IBAN and Multibanco
# --------------------------------------------------------------------------- #


def test_iban_checksum_is_required():
    assert extract_text_fields("IBAN PT50 0002 0123 1234 5678 9015 5", "ev").get(F.IBAN) is None


def test_iban_followed_by_more_characters_is_not_trimmed_to_fit():
    assert extract_text_fields("IBAN PT500002012312345678901549", "ev").get(F.IBAN) is None


def test_two_ibans_are_ambiguous():
    text = "IBAN PT50 0002 0123 1234 5678 9015 4\nIBAN DE89 3704 0044 0532 0130 00"
    result = extract_text_fields(text, "ev")
    assert result.get(F.IBAN) is None
    assert result.ibans == ("PT50000201231234567890154", "DE89370400440532013000")


def test_two_ibans_on_one_line_are_both_found():
    text = "IBAN PT50 0002 0123 1234 5678 9015 4 ou DE89 3704 0044 0532 0130 00"
    result = extract_text_fields(text, "ev")
    assert result.ibans == ("PT50000201231234567890154", "DE89370400440532013000")
    assert result.get(F.IBAN) is None
    adjacent = extract_text_fields("PT50 0002 0123 1234 5678 9015 4 DE89 3704 0044 0532 0130 00", "ev")
    assert len(adjacent.ibans) == 2


def test_multibanco_needs_one_entity_and_one_reference():
    result = extract_text_fields("Entidade: 21800\nReferência: 123 456 789\nReferência: 987 654 321", "ev")
    assert result.get(F.PAYMENT_REFERENCE) is None and result.multibanco is None
    assert F.PAYMENT_REFERENCE in result.ambiguous
    customer_ref = extract_text_fields("Sua ref. 123456789\nEntidade: 21800", "ev")
    assert customer_ref.get(F.PAYMENT_REFERENCE) is None


def test_multibanco_ref_mb_label():
    result = extract_text_fields("Entidade 11249 Ref. MB 123 456 789", "ev")
    assert result.get(F.PAYMENT_REFERENCE).value == "11249 123456789"


# --------------------------------------------------------------------------- #
# Misc
# --------------------------------------------------------------------------- #


def test_method_is_configurable_for_embedded_pdf_text():
    result = extract_text_fields("Total 10,00", "ev", method=ExtractionMethod.EMBEDDED_TEXT)
    assert result.observations[0].method == ExtractionMethod.EMBEDDED_TEXT


def test_empty_and_windows_line_endings():
    assert extract_text_fields("", "ev").observations == ()
    assert extract_text_fields(None, "ev").observations == ()  # type: ignore[arg-type]
    crlf = extract_text_fields("Total\r\n483,60\r\n", "ev")
    assert crlf.get(F.GROSS_AMOUNT).value == Decimal("483.60")
