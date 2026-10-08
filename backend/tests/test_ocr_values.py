"""Value parsing, normalization, field plumbing and the labelled extractor (§18, §19)."""

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from backoffice.domain.models import CriticalField as F
from backoffice.domain.models import DocumentType, ExtractionMethod, FieldObservation
from backoffice.extraction import (
    LabelledFieldExtractor,
    Stage0Result,
    best,
    combine,
    comparison_key,
    from_named_observations,
    group_named,
    iban_is_valid,
    normalize_currency,
    normalize_tax_id,
    parse_amount,
    parse_date,
    ranked,
    typed_value,
)
from backoffice.extraction.values import find_currency


def obs(value, method=ExtractionMethod.OCR, source="ev", confidence=0.5):
    return FieldObservation(value=value, source=source, method=method, confidence=confidence)


# --------------------------------------------------------------------------- amounts


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1.492,30", "1492.30"),
        ("1,492.30", "1492.30"),
        ("1 492,30", "1492.30"),
        ("1\u00a0492,30", "1492.30"),
        ("1'492.30", "1492.30"),
        ("483,60", "483.60"),
        ("483.6", "483.6"),
        ("€ 483,60", "483.60"),
        ("483.60 EUR", "483.60"),
        ("usd 10.00", "10.00"),
        ("US$ 10.00", "10.00"),
        ("(12.50)", "-12.50"),
        ("12.50-", "-12.50"),
        ("- € 5,00", "-5.00"),
        ("1.234.567", "1234567"),
        ("1,234,567.89", "1234567.89"),
        ("0.500", "0.500"),
        ("1.2345", "1.2345"),
        ("42", "42"),
    ],
)
def test_parse_amount_accepts_common_formats(raw, expected):
    assert parse_amount(raw) == Decimal(expected)


@pytest.mark.parametrize(
    "raw",
    [
        "1.492", "1,492", "VAT 90.43", "abc", "1.23.4", "12.", "", "1,2,3.4.5", "EUR", "0.12.345",
        # A lost decimal comma is not thousands grouping: never read "483 60" as 48360.
        "483 60", "1 49,30", "12 3456",
    ],
)  # fmt: skip
def test_parse_amount_refuses_ambiguous_or_garbage(raw):
    assert parse_amount(raw) is None


def test_parse_amount_types_never_go_through_binary_float():
    assert parse_amount(0.1) == Decimal("0.1")
    assert parse_amount(Decimal("483.60")) == Decimal("483.60")
    assert parse_amount(7) == Decimal(7)
    assert parse_amount(True) is None
    assert parse_amount(Decimal("NaN")) is None
    assert parse_amount(float("inf")) is None
    assert parse_amount(None) is None


def test_negative_zero_is_zero():
    assert str(parse_amount("-0.00")) == "0.00"


# --------------------------------------------------------------------------- dates


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-09-18", date(2026, 9, 18)),
        ("20260918", date(2026, 9, 18)),
        ("2026-09-18T10:00:00+01:00", date(2026, 9, 18)),
        ("2026-09-18Z", date(2026, 9, 18)),
        ("18/09/2026", date(2026, 9, 18)),
        ("18.09.2026", date(2026, 9, 18)),
        ("09/18/2026", date(2026, 9, 18)),
        ("18 Sep 2026", date(2026, 9, 18)),
        ("18th September 2026", date(2026, 9, 18)),
        ("18 de setembro de 2026", date(2026, 9, 18)),
        ("1 de março de 2026", date(2026, 3, 1)),
        ("18 de septiembre de 2026", date(2026, 9, 18)),
        ("September 18, 2026", date(2026, 9, 18)),
    ],
)
def test_parse_date_formats(raw, expected):
    assert parse_date(raw) == expected


def test_parse_date_never_guesses_day_order():
    assert parse_date("05/09/2026") is None
    assert parse_date("05/09/2026", day_first=True) == date(2026, 9, 5)
    assert parse_date("05/09/2026", day_first=False) == date(2026, 5, 9)
    assert parse_date("05/05/2026") == date(2026, 5, 5)


def test_parse_date_rejects_impossible_and_foreign_values():
    assert parse_date("31/02/2026", day_first=True) is None
    assert parse_date("18 Brumaire 2026") is None
    assert parse_date("next Tuesday") is None
    assert parse_date(20260918) is None


def test_parse_date_accepts_date_objects():
    assert parse_date(datetime(2026, 9, 18, 23, 0, tzinfo=timezone.utc)) == date(2026, 9, 18)
    assert parse_date(date(2026, 9, 18)) == date(2026, 9, 18)


# --------------------------------------------------------------------------- identifiers


def test_tax_ids_compare_without_country_prefix_or_separators():
    assert normalize_tax_id("PT 509 123 457") == normalize_tax_id("509123457") == "509123457"
    assert normalize_tax_id("ESB12345678") == "B12345678"
    assert normalize_tax_id("ATU12345678") == "U12345678"
    # Swiss UIDs keep their "CHE" prefix; short ids keep theirs too.
    assert normalize_tax_id("CHE-123.456.789") == "CHE123456789"
    assert normalize_tax_id("PT12") == "PT12"
    assert normalize_tax_id("") is None
    assert normalize_tax_id(None) is None


def test_iban_checksum():
    assert iban_is_valid("PT50 0002 0123 1234 5678 9015 4")
    assert iban_is_valid("GB82WEST12345698765432")
    assert not iban_is_valid("PT50000201231234567890155")
    assert not iban_is_valid("not an iban")
    assert not iban_is_valid("12345678901234567")


def test_currency_normalization_only_unambiguous():
    assert normalize_currency("eur") == "EUR"
    assert normalize_currency("€") == "EUR"
    assert normalize_currency("£") == "GBP"
    assert normalize_currency("$") is None  # USD, CAD, AUD...
    assert normalize_currency("euro") is None
    assert find_currency("Total: 483.60 EUR") == "EUR"
    assert find_currency("Total VAT: 90.43") is None
    assert find_currency("€10 or £9") is None  # two currencies: no choice


def test_comparison_keys_make_equivalent_readings_collide():
    assert comparison_key(F.GROSS_AMOUNT, "483,60") == comparison_key(F.GROSS_AMOUNT, Decimal("483.6"))
    assert comparison_key(F.ISSUE_DATE, "2026-09-18") == comparison_key(F.ISSUE_DATE, date(2026, 9, 18))
    assert comparison_key(F.INVOICE_NUMBER, "FT 2026/183") == comparison_key(F.INVOICE_NUMBER, "ft2026/183")
    assert comparison_key(F.IBAN, "PT50 0002") == comparison_key(F.IBAN, "pt500002")


def test_unreadable_values_never_match_real_ones():
    garbage = comparison_key(F.GROSS_AMOUNT, "4B3,6O")
    assert garbage.startswith("raw:")
    assert garbage != comparison_key(F.GROSS_AMOUNT, "483.60")


def test_typed_value_per_field():
    assert typed_value(F.NET_AMOUNT, "393,17") == Decimal("393.17")
    assert typed_value(F.DUE_DATE, "2026-10-18") == date(2026, 10, 18)
    assert typed_value(F.CURRENCY, "gbp") == "GBP"
    assert typed_value(F.PAYMENT_REFERENCE, " RF18 5390 ") == "RF185390"


# --------------------------------------------------------------------------- fields


def test_ranked_orders_by_method_rank_then_confidence():
    ocr = obs("1", ExtractionMethod.OCR, confidence=0.99)
    xml = obs("1", ExtractionMethod.STRUCTURED_XML, confidence=0.5)
    qr_low = obs("1", ExtractionMethod.QR, confidence=0.4)
    qr_high = obs("1", ExtractionMethod.QR, confidence=0.9)
    assert ranked([ocr, qr_low, xml, qr_high]) == (xml, qr_high, qr_low, ocr)
    assert best([ocr, xml]) is xml
    assert best([]) is None


def test_group_named_accepts_named_objects_and_pairs():
    class Named(FieldObservation):
        field: F

    named = Named(
        value="483.60", source="ev", method=ExtractionMethod.QR, confidence=0.9, field=F.GROSS_AMOUNT
    )
    grouped = group_named([named, (F.CURRENCY, obs("EUR")), ("issue_date", obs("2026-09-18"))])
    assert set(grouped) == {F.GROSS_AMOUNT, F.CURRENCY, F.ISSUE_DATE}
    with pytest.raises(TypeError):
        group_named([(F.CURRENCY, "EUR")])


def test_from_named_observations_adapts_a_country_extractor():
    calls = []

    def country_extract(text, source, *, method=ExtractionMethod.OCR):
        calls.append((text, source, method))
        return [(F.GROSS_AMOUNT, FieldObservation(value="1", source=source, method=method, confidence=0.4))]

    extractor = from_named_observations(country_extract)
    found = extractor("Total 1", "ev@engine", ExtractionMethod.VLM)
    assert calls == [("Total 1", "ev@engine", ExtractionMethod.VLM)]
    assert found[F.GROSS_AMOUNT][0].method is ExtractionMethod.VLM


def test_stage0_result_coverage_and_combine():
    xml = Stage0Result.build(
        "ubl_invoice",
        "ev",
        [(F.GROSS_AMOUNT, obs("10", ExtractionMethod.STRUCTURED_XML)), (F.CURRENCY, obs("EUR"))],
        doc_type=DocumentType.INVOICE,
        notes=["a", "a", "b"],
    )
    qr = Stage0Result.build("epc_qr", "ev", [(F.GROSS_AMOUNT, obs("10", ExtractionMethod.QR))])
    assert xml.notes == ("a", "b")
    assert xml.covers({F.GROSS_AMOUNT, F.CURRENCY})
    assert xml.missing({F.GROSS_AMOUNT, F.ISSUE_DATE}) == {F.ISSUE_DATE}
    merged = combine([qr, xml])
    assert [o.method for o in merged[F.GROSS_AMOUNT]] == [
        ExtractionMethod.STRUCTURED_XML,
        ExtractionMethod.QR,
    ]
    with pytest.raises(TypeError):
        xml.fields[F.IBAN] = ()  # read-only view


# --------------------------------------------------------------------------- labelled extractor

INVOICE_TEXT = """ACME Lda
Invoice No.: FT 2026/183
Invoice date: 18/09/2026
Due date: 2026-10-18
VAT number: PT 509 123 457
Subtotal: 393,17
Total VAT: 90,43
Total (incl. VAT): 483,60 €
IBAN: PT50 0002 0123 1234 5678 9015 4
Date of delivery: 17/09/2026
Totally unrelated line 99.99
"""


def test_labelled_extractor_reads_labelled_values_only():
    found = LabelledFieldExtractor()(INVOICE_TEXT, "ev@fake", ExtractionMethod.OCR)
    values = {f: [o.value for o in found[f]] for f in found}
    assert values[F.INVOICE_NUMBER] == ["FT 2026/183"]
    assert values[F.ISSUE_DATE] == [date(2026, 9, 18)]
    assert values[F.DUE_DATE] == [date(2026, 10, 18)]
    assert values[F.SUPPLIER_TAX_ID] == ["PT 509 123 457"]
    assert values[F.NET_AMOUNT] == [Decimal("393.17")]
    assert values[F.VAT_AMOUNT] == [Decimal("90.43")]
    assert values[F.GROSS_AMOUNT] == [Decimal("483.60")]
    assert values[F.CURRENCY] == ["EUR"]
    assert values[F.IBAN] == ["PT50 0002 0123 1234 5678 9015 4"]
    assert all(
        o.source == "ev@fake" and o.method is ExtractionMethod.OCR for obs_ in found.values() for o in obs_
    )


def test_labelled_extractor_reports_every_distinct_value():
    found = LabelledFieldExtractor()("Total: 10.00\nTotal: 10,00\nTotal: 12.00", "ev", ExtractionMethod.OCR)
    assert [o.value for o in found[F.GROSS_AMOUNT]] == [Decimal("10.00"), Decimal("12.00")]


def test_labelled_extractor_rejects_invalid_values():
    text = "IBAN: PT50 0002 0123 1234 5678 9015 5\nSupplier VAT: 12\nTotal: 1.492\nCurrency: euros"
    assert LabelledFieldExtractor()(text, "ev", ExtractionMethod.OCR) == {}


def test_labelled_extractor_custom_labels_and_validation():
    extractor = LabelledFieldExtractor({F.GROSS_AMOUNT: ["valor a pagar"]}, confidence=0.3, day_first=None)
    found = extractor("Valor a pagar: 12,30", "ev", ExtractionMethod.OCR)
    assert found[F.GROSS_AMOUNT][0].value == Decimal("12.30")
    assert found[F.GROSS_AMOUNT][0].confidence == 0.3
    with pytest.raises(ValueError):
        LabelledFieldExtractor(confidence=1.5)
