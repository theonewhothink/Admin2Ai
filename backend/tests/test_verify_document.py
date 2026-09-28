"""Document-level verification (§18, §19, §57)."""

import re
from dataclasses import dataclass
from decimal import Decimal as D

import pytest

from backoffice.domain.models import CriticalField as F
from backoffice.domain.models import DocumentType
from backoffice.domain.models import ExtractionMethod as M
from backoffice.domain.models import FieldObservation, Quality, VerifiedField
from backoffice.verification import (
    DEFAULT_REQUIREMENTS,
    NormalizeHints,
    RateFit,
    TaxBreakdown,
    TaxLine,
    assess_document,
    bank_observation,
    group_by_field,
    required_fields,
    verify_document,
)

PT_RATES = (D("0.06"), D("0.13"), D("0.23"))
PT = NormalizeHints(locale="pt-PT")
OCR = "ev_1@pp-ocrv6-medium"
EV = "ev_1"


def see(value, method=M.OCR, source=OCR, confidence=0.6):
    return FieldObservation(value=value, source=source, method=method, confidence=confidence)


def qr(value):
    return see(value, M.QR, EV, 0.98)


def section_19(qr_gross="483.60"):
    """OCR and a Portuguese QR code for the §19 invoice (net 393.17 + VAT 90.43)."""
    return {
        F.GROSS_AMOUNT: [see("483,60"), qr(D(qr_gross))],
        F.NET_AMOUNT: [see("393,17"), qr(D("393.17"))],
        F.VAT_AMOUNT: [see("90,43"), qr(D("90.43"))],
        F.SUPPLIER_TAX_ID: [see("PT 503 504 564"), qr("503504564")],
        F.INVOICE_NUMBER: [see("FT 2026/183"), qr("FT 2026/183")],
        F.ISSUE_DATE: [see("18/09/2026"), qr("20260918")],
        F.CURRENCY: [see("€")],
    }


_LEAKS = re.compile(r"ev_\d|@|pp-ocr|\bocr\b|arithmetic|conflict|reconcil", re.I)


def assert_plain(lines):
    for line in lines:
        assert not _LEAKS.search(line), line


# --------------------------------------------------------------------------- §19


def test_section_19_ocr_qr_arithmetic_and_bank_green():
    fields, quality = verify_document(section_19(), PT_RATES, D("-483.60"), bank_currency="EUR", hints=PT)
    assert quality is Quality.GREEN
    assert all(isinstance(f, VerifiedField) for f in fields.values())
    gross = fields["gross_amount"]
    assert gross.quality is Quality.GREEN and gross.value == D("483.60")
    methods = {o.method for o in gross.observations}
    assert methods == {M.OCR, M.QR, M.ARITHMETIC, M.BANK}  # §19: four agreeing witnesses
    assert all(f.quality is Quality.GREEN for f in fields.values())


def test_section_19_qr_says_438_60_red_conflict():
    result = assess_document(section_19("438.60"), PT_RATES, D("-483.60"), bank_currency="EUR", hints=PT)
    assert result.quality is Quality.RED
    assert result.conflicts == ("gross_amount",)
    gross = result.fields["gross_amount"]
    assert gross.value is None and set(gross.conflicting_values) == {D("483.60"), D("438.60")}
    assert "€483.60" in gross.reasons[0] and "€438.60" in gross.reasons[0]
    assert result.reasons[0] == "The sources disagree on the total. A person needs to check."
    fields, quality = result.as_tuple()
    assert quality is Quality.RED and fields["gross_amount"].quality is Quality.RED
    assert_plain(result.reasons)


def test_without_the_bank_the_section_19_invoice_still_verifies_from_qr_and_ocr():
    observations = section_19()
    observations[F.CURRENCY].append(see("EUR", M.EMBEDDED_TEXT, EV, 0.6))
    assert assess_document(observations, PT_RATES, hints=PT).quality is Quality.GREEN


# --------------------------------------------------------------------------- required fields


def test_missing_required_fields_keep_the_document_amber():
    observations = section_19()
    del observations[F.INVOICE_NUMBER]
    result = assess_document(observations, PT_RATES, D("483.60"), bank_currency="EUR", hints=PT)
    assert result.quality is Quality.AMBER
    assert result.missing == ("invoice_number",)
    assert result.fields["invoice_number"].quality is Quality.AMBER
    assert result.reasons == ("I still need the invoice number.",)


def test_a_likely_value_is_not_enough():
    observations = section_19()
    observations[F.SUPPLIER_TAX_ID] = [qr("503504564")]  # one source only
    result = assess_document(observations, PT_RATES, D("483.60"), bank_currency="EUR", hints=PT)
    assert result.quality is Quality.AMBER and result.missing == ()
    assert result.reasons == ("I still need to confirm the supplier's VAT number.",)


def test_simplified_receipts_need_fewer_fields():
    receipt = {
        F.GROSS_AMOUNT: [see("12,50"), qr("12.50")],
        F.ISSUE_DATE: [see("18/09/2026"), qr("20260918")],
        F.CURRENCY: [see("€")],
    }
    result = assess_document(receipt, PT_RATES, D("12.50"), bank_currency="EUR", hints=PT,
                             doc_type=DocumentType.RECEIPT)  # fmt: skip
    assert result.quality is Quality.GREEN
    assert result.required == {"gross_amount", "issue_date", "currency"}
    invoice = assess_document(receipt, PT_RATES, D("12.50"), bank_currency="EUR", hints=PT)
    assert invoice.quality is Quality.AMBER  # a full invoice needs its supplier, number, net and VAT


def test_requirements_are_configurable():
    assert F.NET_AMOUNT in required_fields(DocumentType.INVOICE)
    assert F.NET_AMOUNT not in required_fields(DocumentType.SIMPLIFIED_INVOICE)
    assert required_fields(DocumentType.TAX_NOTICE) == DEFAULT_REQUIREMENTS[DocumentType.TAX_NOTICE]
    custom = {DocumentType.INVOICE: {F.GROSS_AMOUNT}}
    assert required_fields(DocumentType.RECEIPT, custom) == required_fields(DocumentType.OTHER)
    only_gross = {F.GROSS_AMOUNT: [see("12,50"), qr("12.50")]}
    assert assess_document(only_gross, requirements=custom).quality is Quality.GREEN
    assert assess_document(only_gross, required=[F.GROSS_AMOUNT]).quality is Quality.GREEN
    with pytest.raises(ValueError):
        assess_document(only_gross, required=[])


# --------------------------------------------------------------------------- bank


def test_bank_charge_in_another_currency_is_not_compared():
    observations = section_19()
    observations[F.CURRENCY] = [see("USD"), see("USD", M.EMBEDDED_TEXT, EV)]
    result = assess_document(observations, PT_RATES, D("412.87"), bank_currency="EUR", hints=PT)
    gross = result.fields["gross_amount"]
    assert all(o.method is not M.BANK for o in gross.observations)
    assert gross.quality is Quality.GREEN
    assert "The bank charge is in EUR and the document in USD, so I didn't compare them." in result.reasons


def test_bank_charge_is_not_compared_when_the_document_could_be_in_other_currencies():
    observations = section_19()
    observations[F.CURRENCY] = [see("$"), see("$", M.EMBEDDED_TEXT, EV)]
    result = assess_document(observations, PT_RATES, D("412.87"), bank_currency="EUR", hints=PT)
    assert all(o.method is not M.BANK for o in result.fields["gross_amount"].observations)
    assert result.fields["currency"].possible_values[0] == "USD"
    assert any(
        r.startswith("The bank charge is in EUR and the document in USD or CAD") for r in result.reasons
    )


def test_bank_charge_with_a_disputed_currency_is_not_compared():
    observations = section_19()
    observations[F.CURRENCY] = [see("USD"), see("EUR", M.EMBEDDED_TEXT, EV)]
    result = assess_document(observations, PT_RATES, D("483.60"), bank_currency="EUR", hints=PT)
    assert result.quality is Quality.RED and result.conflicts == ("currency",)
    assert all(o.method is not M.BANK for o in result.fields["gross_amount"].observations)


def test_bank_charge_that_differs_is_a_conflict():
    result = assess_document(section_19(), PT_RATES, D("-438.60"), bank_currency="EUR", hints=PT)
    assert result.quality is Quality.RED and result.conflicts == ("gross_amount",)


def test_bank_amount_inputs():
    assert bank_observation(D("-483.60")).value == D("483.60")
    assert bank_observation(D("483.60")).method is M.BANK
    with pytest.raises(TypeError):
        bank_observation(483.6)
    with pytest.raises(ValueError):
        assess_document(section_19(), PT_RATES, see(D("483.60")))  # not a BANK observation
    with pytest.raises(ValueError):
        assess_document(section_19(), PT_RATES, D("483.60"), bank_currency="money")
    custom = FieldObservation(value=D("483.60"), source="tx_1", method=M.BANK, confidence=0.99)
    assert (
        assess_document(section_19(), PT_RATES, custom, hints=PT).fields["gross_amount"].quality
        is Quality.GREEN
    )


# --------------------------------------------------------------------------- arithmetic


def test_totals_that_do_not_add_up_turn_all_three_red():
    # Each total is confirmed by two sources, no source states two of them,
    # yet net + VAT != gross. Which one is wrong is not guessed.
    observations = section_19()
    observations[F.NET_AMOUNT] = [see("393,17", M.EMBEDDED_TEXT, EV), see("393.17", M.STRUCTURED_XML, "ev_2")]
    observations[F.VAT_AMOUNT] = [see("90,43", M.VLM, "ev_1@vl"), see("90.43", M.API, "api")]
    observations[F.GROSS_AMOUNT] = [see("488,60"), qr(D("488.60"))]
    result = assess_document(observations, PT_RATES, hints=PT)
    assert result.quality is Quality.RED
    assert set(result.conflicts) == {"gross_amount", "net_amount", "vat_amount"}
    assert not result.sum_check.ok
    assert result.fields["net_amount"].reasons[-1] == (
        "The amounts don't add up: €393.17 + €90.43 VAT is €483.60, but the total shows €488.60."
    )


def test_a_source_whose_own_numbers_do_not_add_up_is_caught_by_arithmetic():
    observations = section_19()
    observations[F.NET_AMOUNT] = [see("393,17", M.EMBEDDED_TEXT, EV), see("393.17", M.STRUCTURED_XML, "ev_2")]
    observations[F.VAT_AMOUNT] = [see("90,43", M.VLM, "ev_1@vl"), qr(D("90.43"))]
    observations[F.GROSS_AMOUNT] = [see("488,60"), qr(D("488.60"))]
    result = assess_document(observations, PT_RATES, hints=PT)
    # the QR code's gross - VAT gives a net of 398.17 against the stated 393.17
    assert result.quality is Quality.RED and result.conflicts == ("net_amount",)
    assert D("398.17") in result.fields["net_amount"].conflicting_values


def test_a_vat_rate_that_fits_nothing_keeps_the_vat_from_green():
    observations = section_19()
    observations[F.VAT_AMOUNT] = [see("98,29"), qr(D("98.29"))]  # 25% of 393.17: above every PT rate
    observations[F.GROSS_AMOUNT] = [see("491,46"), qr(D("491.46"))]
    result = assess_document(observations, PT_RATES, D("491.46"), bank_currency="EUR", hints=PT)
    assert result.quality is Quality.AMBER
    assert result.fields["vat_amount"].quality is Quality.AMBER
    assert result.fields["gross_amount"].quality is Quality.GREEN
    assert result.rate_checks[-1].fit is RateFit.UNEXPECTED
    assert "The VAT doesn't match any expected rate." in result.fields["vat_amount"].reasons
    assert result.reasons == ("I still need to confirm the VAT.",)


def test_a_blend_of_allowed_rates_is_not_held_against_the_vat():
    observations = section_19()
    observations[F.VAT_AMOUNT] = [see("78,63"), qr(D("78.63"))]  # 20%: could be 23% and 6% lines
    observations[F.GROSS_AMOUNT] = [see("471,80"), qr(D("471.80"))]
    result = assess_document(observations, PT_RATES, D("471.80"), bank_currency="EUR", hints=PT)
    assert result.rate_checks[-1].fit is RateFit.MIXED
    assert result.quality is Quality.GREEN


def test_rate_check_is_skipped_without_rates_and_accepts_pack_rate_objects():
    @dataclass(frozen=True)
    class PackRate:
        rate: D

    result = assess_document(section_19(), [PackRate(r) for r in PT_RATES], D("483.60"), bank_currency="EUR",
                             hints=PT)  # fmt: skip
    assert result.quality is Quality.GREEN and result.rate_checks[0].rate == D("0.23")
    assert assess_document(section_19(), (), D("483.60"), bank_currency="EUR", hints=PT).rate_checks == ()


def test_tax_lines_are_checked_and_must_match_the_totals():
    lines = (TaxLine(D("300.00"), D("69.00"), D("0.23")), TaxLine(D("93.17"), D("21.43"), D("0.23")))
    breakdown = TaxBreakdown(lines=lines, source=EV, method=M.QR, confidence=0.98)
    good = assess_document(section_19(), PT_RATES, D("483.60"), bank_currency="EUR", hints=PT,
                           breakdowns=[breakdown])  # fmt: skip
    assert good.quality is Quality.GREEN
    assert [c.line for c in good.rate_checks] == [1, 2]
    short = TaxBreakdown(lines=lines[:1], source=EV, method=M.QR, confidence=0.98)
    bad = assess_document(section_19(), PT_RATES, D("483.60"), bank_currency="EUR", hints=PT,
                          breakdowns=[short])  # fmt: skip
    assert bad.quality is Quality.RED and set(bad.conflicts) == {"net_amount", "vat_amount", "gross_amount"}
    # stated totals and tax-line sums are never mixed: each conflict has exactly two values
    for name in bad.conflicts:
        assert len(bad.fields[name].conflicting_values) == 2, name
    assert D("369.00") in bad.fields["gross_amount"].conflicting_values  # 300.00 + 69.00 from the lines


def test_a_line_with_an_unexpected_rate_keeps_the_vat_amber():
    lines = (TaxLine(D("393.17"), D("90.43"), D("0.20")),)
    breakdown = TaxBreakdown(lines=lines, source=EV, method=M.QR, confidence=0.98)
    result = assess_document(section_19(), PT_RATES, D("483.60"), bank_currency="EUR", hints=PT,
                             breakdowns=[breakdown])  # fmt: skip
    assert result.quality is Quality.AMBER and result.fields["vat_amount"].quality is Quality.AMBER


# --------------------------------------------------------------------------- scope of RED


def test_any_red_critical_field_makes_the_document_red_even_if_not_required():
    observations = section_19()
    observations[F.DUE_DATE] = [see("18/10/2026"), qr("20261019")]
    result = assess_document(observations, PT_RATES, D("483.60"), bank_currency="EUR", hints=PT)
    assert result.quality is Quality.RED and result.conflicts == ("due_date",)


def test_free_text_fields_are_verified_but_do_not_decide():
    observations = section_19()
    observations["supplier_name"] = [see("Vodafone"), qr("NOS")]
    result = assess_document(observations, PT_RATES, D("483.60"), bank_currency="EUR", hints=PT)
    assert result.fields["supplier_name"].quality is Quality.RED
    assert result.quality is Quality.GREEN


def test_group_by_field_accepts_named_observations():
    @dataclass(frozen=True)
    class Named:
        field: F
        value: object
        source: str = EV
        method: M = M.QR
        confidence: float = 0.98

    grouped = group_by_field(
        [Named(F.GROSS_AMOUNT, "1"), Named(F.GROSS_AMOUNT, "2"), Named(F.CURRENCY, "EUR")]
    )
    assert {k: len(v) for k, v in grouped.items()} == {"gross_amount": 2, "currency": 1}


def test_field_order_and_plain_reasons():
    result = assess_document(section_19(), PT_RATES, D("483.60"), bank_currency="EUR", hints=PT)
    assert list(result.fields)[:3] == ["invoice_number", "supplier_tax_id", "gross_amount"]
    assert result.reasons == ("Everything I need on this document is confirmed.",)
    for field in result.fields.values():
        assert_plain(field.reasons)


@pytest.mark.parametrize("qr_gross", ["483.60", "438.60"])
def test_document_result_does_not_depend_on_observation_order(qr_gross):
    forward = assess_document(section_19(qr_gross), PT_RATES, D("483.60"), bank_currency="EUR", hints=PT)
    backward_obs = {k: list(reversed(v)) for k, v in reversed(list(section_19(qr_gross).items()))}
    backward = assess_document(backward_obs, PT_RATES, D("483.60"), bank_currency="EUR", hints=PT)
    assert forward.quality is backward.quality and forward.reasons == backward.reasons
    assert list(forward.fields) == list(backward.fields)
    for name, field in forward.fields.items():
        other = backward.fields[name]
        assert (field.quality, field.value, field.reasons) == (other.quality, other.value, other.reasons), (
            name
        )
