"""Canonical values for critical fields (§18): no guessing, ambiguity is explicit."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from backoffice.domain.models import CriticalField as F
from backoffice.verification import (
    NormalizeHints,
    NormNote,
    comparison_key,
    iban_is_valid,
    invoice_number_key,
    normalize_amount,
    normalize_currency,
    normalize_date,
    normalize_iban,
    normalize_invoice_number,
    normalize_payment_reference,
    normalize_tax_id,
    normalize_value,
    split_tax_id,
)
from backoffice.verification.normalize import MONTHS, _MONTH_NAMES

# --------------------------------------------------------------------------- amounts


@pytest.mark.parametrize(
    "raw",
    [
        "1.492,30",
        "1,492.30",
        "€ 1 492,30",
        "1492.3",
        "1492,30",
        "EUR 1.492,30",
        "1.492,30 €",
        "1 492,30 EUR",
        "1 492,30",  # no-break space grouping
        "1 492,30",  # narrow no-break space
        "1'492.30",  # Swiss grouping
        "US$ 1,492.30",
        D("1492.3"),
        1492.3,  # read through repr, never binary arithmetic
    ],
)
def test_amount_formats_agree(raw):
    result = normalize_amount(raw)
    assert result.clear and result.value == D("1492.30")
    assert str(result.value) == "1492.30"  # quantized to cents


def test_integers_and_repeated_grouping():
    assert normalize_amount(1492).value == D("1492.00")
    assert normalize_amount("12.345.678").value == D("12345678.00")
    assert normalize_amount("1,234,567.89").value == D("1234567.89")
    assert normalize_amount("0,50").value == D("0.50")


def test_lone_separator_with_three_digits_is_ambiguous_without_a_hint():
    result = normalize_amount("1.492")
    assert result.ambiguous and result.note is NormNote.AMBIGUOUS and result.value is None
    assert result.candidates == (D("1492.00"), D("1.49"))


def test_hints_resolve_only_the_ambiguous_case():
    assert normalize_amount("1.492", decimal_separator=",").value == D("1492.00")
    assert normalize_amount("1.492", decimal_separator=".").value == D("1.49")
    pt, gb = NormalizeHints(locale="pt-PT"), NormalizeHints(locale="en_GB")
    assert normalize_value(F.GROSS_AMOUNT, "1.492", pt).value == D("1492.00")
    assert normalize_value(F.GROSS_AMOUNT, "1.492", gb).value == D("1.49")
    # the value's own shape wins over the hint
    assert normalize_amount("1,492.30", decimal_separator=",").value == D("1492.30")
    assert normalize_amount("1492.30", decimal_separator=",").value == D("1492.30")
    # a first group of four digits or a leading zero cannot be thousands
    assert normalize_amount("1492.300").value == D("1492.30")
    assert normalize_amount("0.492").value == D("0.49")


def test_signs_are_kept_by_amount_but_fields_compare_magnitudes():
    for raw in ("(1,492.30)", "1.492,30-", "−1.492,30", "-€1.492,30", "€-1.492,30"):
        assert normalize_amount(raw).value == D("-1492.30"), raw
    assert normalize_value(F.GROSS_AMOUNT, "-483,60").value == D("483.60")
    assert normalize_value("vat_amount", "(90.43)").value == D("90.43")


def test_sub_cent_digits_are_rounded_explicitly():
    result = normalize_amount(0.1 + 0.2)
    assert result.value == D("0.30") and result.note is NormNote.ROUNDED
    assert normalize_amount("12.345", decimal_separator=".").note is NormNote.ROUNDED


@pytest.mark.parametrize(
    "raw",
    [None, True, "", "abc", "Total: 12", "12abc", "1492.", "1.49.2", "12,34,567.89", "1,2,3",
     "(12", "--12", "EUR 12 USD", D("NaN"), float("inf"), [12], "14 92,30"],
)  # fmt: skip
def test_unreadable_amounts(raw):
    result = normalize_amount(raw)
    assert not result.readable and result.value is None


# --------------------------------------------------------------------------- currency


@pytest.mark.parametrize(
    ("raw", "code"),
    [("EUR", "EUR"), ("eur", "EUR"), ("€", "EUR"), ("Euros", "EUR"), (" Eur. ", "EUR"),
     ("£", "GBP"), ("₪", "ILS"), ("NIS", "ILS"), ("zł", "PLN"), ("R$", "BRL"), ("usd", "USD")],
)  # fmt: skip
def test_currency_codes_symbols_and_names(raw, code):
    assert normalize_currency(raw).value == code


def test_shared_symbols_stay_ambiguous_and_unknown_codes_are_refused():
    dollar = normalize_currency("$")
    assert dollar.ambiguous and "USD" in dollar.candidates and "CAD" in dollar.candidates
    assert normalize_currency("kr").ambiguous
    assert normalize_currency("XYZ").note is NormNote.UNKNOWN_CODE
    assert not normalize_currency("money").readable
    assert not normalize_currency(978).readable


# --------------------------------------------------------------------------- dates


@pytest.mark.parametrize(
    "raw",
    [
        "2026-09-18",
        "2026/9/18",
        "20260918",  # Portuguese fiscal QR field F
        "18/09/2026",
        "18-09-2026",
        "18.09.2026",
        "09/18/2026",  # only month-first is possible
        "18/09/26",
        "18 de setembro de 2026",
        "18 Setembro 2026",
        "18-Sep-2026",
        "18 sept. 2026",
        "18. September 2026",
        "September 18, 2026",
        "Sep 18th, 2026",
        "Friday, September 18, 2026",
        "18 septiembre 2026",
        "18 septembre 2026",
        "2026-09-18T23:30:00-05:00",
        "18/09/2026 14:30",
        date(2026, 9, 18),
        datetime(2026, 9, 18, 23, 30, tzinfo=timezone(timedelta(hours=-5))),  # its own calendar day
    ],
)
def test_date_formats(raw):
    assert normalize_date(raw).value == date(2026, 9, 18)


def test_day_month_swap_is_ambiguous_unless_a_hint_or_the_value_decides():
    result = normalize_date("03/04/2026")
    assert result.ambiguous and set(result.candidates) == {date(2026, 4, 3), date(2026, 3, 4)}
    assert normalize_date("03/04/2026", day_first=True).value == date(2026, 4, 3)
    assert normalize_date("03/04/2026", day_first=False).value == date(2026, 3, 4)
    assert normalize_value(F.ISSUE_DATE, "03/04/2026", NormalizeHints(locale="pt")).value == date(2026, 4, 3)
    assert normalize_value(F.DUE_DATE, "03/04/2026", NormalizeHints(locale="en-US")).value == date(2026, 3, 4)
    assert normalize_value(
        F.DUE_DATE, "03/04/2026", NormalizeHints(locale="en")
    ).ambiguous  # en alone: unknown
    # an impossible reading is dropped, whatever the hint
    assert normalize_date("09/18/2026", day_first=True).value == date(2026, 9, 18)
    # the same day either way is not ambiguous; dots promise nothing about the order
    assert normalize_date("05/05/2026").value == date(2026, 5, 5)
    assert normalize_date("03.04.2026").ambiguous


@pytest.mark.parametrize("raw", ["31/02/2026", "2026-13-01", "32 September 2026", "18 Foo 2026", "01/01/1850",
                                 "tomorrow", "", 20260918, None])  # fmt: skip
def test_unreadable_dates(raw):
    assert not normalize_date(raw).readable


def test_month_names_are_unique_across_languages():
    names = [n for group in _MONTH_NAMES.values() for n in group]
    assert len(names) == len(set(names)) == len(MONTHS)


# --------------------------------------------------------------------------- identifiers


@pytest.mark.parametrize(
    ("raw", "country", "number"),
    [
        ("PT 503 504 564", "PT", "503504564"),
        ("pt503504564", "PT", "503504564"),
        ("503.504.564", None, "503504564"),
        (503504564, None, "503504564"),
        ("ESB12345678", "ES", "B12345678"),
        ("B12345678", None, "B12345678"),
        ("ATU12345678", "AT", "U12345678"),
        ("EL 123456789", "GR", "123456789"),
        ("CHE-123.456.789 MWST", "CH", "123456789"),
        ("NO 999 999 999 MVA", "NO", "999999999"),
        ("IE1234567WA", "IE", "1234567WA"),
    ],
)
def test_tax_ids_lose_prefix_and_spacing(raw, country, number):
    assert split_tax_id(raw) == (country, number)
    assert normalize_tax_id(raw).value == number


@pytest.mark.parametrize("raw", ["", "PT", "ABC", "12", None, True, "NIF-only-letters"])
def test_unreadable_tax_ids(raw):
    assert not normalize_tax_id(raw).readable


def test_iban_is_compacted_and_mod97_checked():
    assert normalize_iban("pt50 0002 0123 1234 5678 9015 4").value == "PT50000201231234567890154"
    assert normalize_iban("IBAN: DE89 3704 0044 0532 0130 00").value == "DE89370400440532013000"
    assert normalize_iban("GB82-WEST-1234-5698-7654-32").value == "GB82WEST12345698765432"
    bad_digit = normalize_iban("GB82 WEST 1234 5698 7654 33")
    assert not bad_digit.readable and bad_digit.note is NormNote.INVALID
    wrong_length = normalize_iban("PT50 0002 0123 1234 5678 9015")  # 21 chars, PT needs 25
    assert wrong_length.note is NormNote.INVALID
    assert not normalize_iban("not an iban").readable
    assert iban_is_valid("PT50000201231234567890154") and not iban_is_valid("PT50000201231234567890155")


def test_invoice_numbers_keep_the_series_and_compare_without_spaces():
    assert normalize_invoice_number(" ft 2026 / 183 ").value == "FT 2026/183"
    assert normalize_invoice_number("FT–2026⁄183").value == "FT-2026/183"  # dash and slash variants
    assert normalize_invoice_number("#183").value == "183"
    assert normalize_invoice_number(183).value == "183"
    assert invoice_number_key("FT 2026/183") == invoice_number_key("FT2026/183") == "FT2026/183"
    assert comparison_key(F.INVOICE_NUMBER, "FT 2026/183") == comparison_key("invoice_number", "FT2026/183")
    assert comparison_key(F.INVOICE_NUMBER, "FT 2026/183") != comparison_key(F.INVOICE_NUMBER, "FT 2026/0183")
    assert not normalize_invoice_number("  / ").readable


def test_payment_references():
    assert normalize_payment_reference("123 456 789").value == "123456789"  # Multibanco
    assert normalize_payment_reference("rf18 5390 0754 7034").value == "RF18539007547034"
    assert normalize_payment_reference("RF18 5390 0754 7035").note is NormNote.INVALID
    assert not normalize_payment_reference("ref: é").readable


def test_unknown_fields_are_compared_as_plain_text():
    assert normalize_value("supplier_name", "  Vodafone   Portugal ").value == "Vodafone Portugal"
    assert comparison_key("supplier_name", "Vodafone Portugal") == comparison_key(
        "supplier_name", "VODAFONE PORTUGAL"
    )
    assert not normalize_value("supplier_name", None).readable


def test_hints_validate_the_separator():
    with pytest.raises(ValueError):
        NormalizeHints(decimal_separator=" ")
