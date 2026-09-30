"""AT fiscal QR code parsing (§13 Stage 0, §18, §19)."""

from datetime import date
from decimal import Decimal

import pytest

from backoffice.countries.base import FiscalQRError, NamedObservation, group_by_field
from backoffice.countries.pt import (
    PTQRCode,
    QRCodeError,
    looks_like_pt_qr,
    parse_qr,
    qr_to_observations,
)
from backoffice.countries.pt.qr import FIELD_ORDER
from backoffice.domain.models import CriticalField, DocumentType, ExtractionMethod, FieldObservation

# The example published in the AT technical specification (Portaria 195/2020).
AT_EXAMPLE = (
    "A:123456789*B:999999990*C:PT*D:FT*E:N*F:20191231*G:FT AB2019/0035*H:CSDF7T5H-0035*"
    "I1:PT*I2:12000.00*I3:15000.00*I4:900.00*I5:50000.00*I6:6500.00*I7:80000.00*I8:18400.00*"
    "J1:PT-AC*J2:10000.00*J3:25000.56*J4:1000.02*J5:75000.00*J6:6750.00*J7:100000.00*J8:18000.00*"
    "K1:PT-MA*K2:5000.00*K3:12500.00*K4:625.00*K5:25000.00*K6:3000.00*K7:40000.00*K8:8800.00*"
    "L:100.00*M:25.00*N:64000.02*O:513600.58*P:100.00*Q:kLp0*R:9999*"
    "S:TB;PT00000000000000000000000;513500.58"
)

# §19: net 393.17 + VAT 90.43 (23%) = 483.60.
SECTION_19 = {
    "A": "509123457", "B": "516123459", "C": "PT", "D": "FT", "E": "N", "F": "20260918",
    "G": "FT 2026/183", "H": "CSDF7T5H-183", "I1": "PT", "I7": "393.17", "I8": "90.43",
    "N": "90.43", "O": "483.60", "Q": "kLp0", "R": "2345",
}


def build(**changes: str | None) -> str:
    """A QR payload from SECTION_19 with fields changed (None removes a field)."""
    fields = {**SECTION_19, **changes}
    return "*".join(f"{k}:{fields[k]}" for k in FIELD_ORDER if fields.get(k) is not None)


def issue_codes(payload: str) -> set[str]:
    with pytest.raises(QRCodeError) as info:
        parse_qr(payload)
    return {issue.code for issue in info.value.issues}


def by_field(observations: list[NamedObservation]) -> dict[CriticalField, list[FieldObservation]]:
    return group_by_field(observations)


# --------------------------------------------------------------------------- #
# §19 example and conflict
# --------------------------------------------------------------------------- #


def test_section_19_example_values_and_arithmetic():
    code = parse_qr(build())
    assert code.net_total == Decimal("393.17")
    assert code.vat_total == Decimal("90.43")
    assert code.gross_total == Decimal("483.60")
    assert code.net_total + code.vat_total == code.gross_total
    assert code.is_consistent
    assert code.checks.warnings == ()  # 23% on the mainland is plausible
    assert code.doc_type == DocumentType.INVOICE
    assert code.issue_date == date(2026, 9, 18)
    assert code.invoice_number == "FT 2026/183"
    assert code.atcud is not None and code.atcud.sequence == 183


def test_section_19_observations():
    observations = qr_to_observations(parse_qr(build()), "ev_qr")
    assert all(isinstance(o, FieldObservation) for o in observations)
    assert all(o.source == "ev_qr" for o in observations)
    grouped = by_field(observations)
    assert [o.value for o in grouped[CriticalField.GROSS_AMOUNT]] == [Decimal("483.60")] * 2
    assert [o.method for o in grouped[CriticalField.GROSS_AMOUNT]] == [
        ExtractionMethod.QR, ExtractionMethod.ARITHMETIC]
    assert grouped[CriticalField.NET_AMOUNT][0].value == Decimal("393.17")
    assert grouped[CriticalField.VAT_AMOUNT][0].value == Decimal("90.43")
    assert grouped[CriticalField.SUPPLIER_TAX_ID][0].value == "509123457"
    assert grouped[CriticalField.CUSTOMER_TAX_ID][0].value == "516123459"
    assert grouped[CriticalField.ISSUE_DATE][0].value == date(2026, 9, 18)
    assert grouped[CriticalField.INVOICE_NUMBER][0].value == "FT 2026/183"
    assert grouped[CriticalField.GROSS_AMOUNT][0].location == "qr:O"
    assert grouped[CriticalField.NET_AMOUNT][0].location == "qr:I7"
    assert all(isinstance(o.value, (Decimal, str, date)) for o in observations)
    assert not any(isinstance(o.value, float) for o in observations)


def test_section_19_conflict_is_surfaced_not_resolved():
    """QR says 438.60 while its own parts add up to 483.60: report both, never pick."""
    code = parse_qr(build(O="438.60"))
    assert not code.is_consistent
    assert code.checks.gross_total_ok is False
    assert code.checks.gross_total_expected == Decimal("483.60")
    assert "gross_total_mismatch" in {w.code for w in code.checks.warnings}

    gross = by_field(qr_to_observations(code, "ev_qr"))[CriticalField.GROSS_AMOUNT]
    values = {o.method: o.value for o in gross}
    assert values == {ExtractionMethod.QR: Decimal("438.60"),
                      ExtractionMethod.ARITHMETIC: Decimal("483.60")}
    qr_obs = next(o for o in gross if o.method == ExtractionMethod.QR)
    assert qr_obs.confidence < 0.5


def test_require_consistent_rejects_the_conflict():
    with pytest.raises(QRCodeError) as info:
        parse_qr(build(O="438.60"), require_consistent=True)
    assert {i.code for i in info.value.issues} == {"gross_total_mismatch"}


def test_tax_total_mismatch():
    code = parse_qr(build(N="90.00", O="483.17"))
    assert code.checks.tax_total_ok is False
    assert not code.is_consistent


def test_rounding_tolerance():
    within = parse_qr(build(O="483.61"))
    assert within.is_consistent
    # No arithmetic observation that would disagree by a cent with field O.
    gross = by_field(qr_to_observations(within, "ev"))[CriticalField.GROSS_AMOUNT]
    assert [(o.method, o.value) for o in gross] == [(ExtractionMethod.QR, Decimal("483.61"))]
    assert not parse_qr(build(O="483.63")).is_consistent
    assert not parse_qr(build(O="483.63"), tolerance=Decimal("0.00")).is_consistent
    assert parse_qr(build(O="483.63"), tolerance=Decimal("0.05")).is_consistent


# --------------------------------------------------------------------------- #
# Official AT example
# --------------------------------------------------------------------------- #


def test_official_at_example():
    code = parse_qr(AT_EXAMPLE)
    assert [b.region for b in code.tax_blocks] == ["PT", "PT-AC", "PT-MA"]
    assert code.vat_total == Decimal("63975.02")
    assert code.stamp_duty == Decimal("25.00")
    assert code.tax_total == code.vat_total + code.stamp_duty  # N includes stamp duty
    assert code.net_total == Decimal("449600.56")  # all bases + L
    assert code.gross_total == Decimal("513600.58")
    assert code.withholding == Decimal("100.00")
    assert code.amount_payable == Decimal("513500.58")  # matches the S example
    assert code.other_info == "TB;PT00000000000000000000000;513500.58"
    assert code.buyer_is_final_consumer
    assert code.is_consistent
    # 2019 regional rates (Azores 18%, Madeira 5%) are all plausible.
    assert code.checks.warnings == ()


def test_official_example_observations_include_final_consumer_as_is():
    grouped = by_field(qr_to_observations(parse_qr(AT_EXAMPLE), "ev"))
    assert grouped[CriticalField.CUSTOMER_TAX_ID][0].value == "999999990"
    assert grouped[CriticalField.GROSS_AMOUNT][1].value == Decimal("513600.58")


# --------------------------------------------------------------------------- #
# Structure and formats
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("payload", ["", "   ", "\ufeff"])
def test_empty_payload(payload):
    assert issue_codes(payload) == {"empty"}


def test_non_text_payload():
    with pytest.raises(QRCodeError):
        parse_qr(None)  # type: ignore[arg-type]


def test_surrounding_whitespace_and_bom_are_tolerated():
    assert parse_qr("\ufeff" + build() + "\r\n").gross_total == Decimal("483.60")


@pytest.mark.parametrize("missing", ["A", "B", "C", "D", "E", "F", "G", "H", "I1", "N", "O", "Q", "R"])
def test_mandatory_fields(missing):
    assert "missing_field" in issue_codes(build(**{missing: None}))


def test_unknown_duplicate_and_syntax_errors():
    assert "unknown_field" in issue_codes(build() + "*Z:1")
    assert "duplicate_field" in issue_codes(build() + "*R:1")
    assert "syntax" in issue_codes(build().replace("*E:N", "*EN"))
    assert "syntax" in issue_codes(build() + "*")


def test_field_order_is_enforced():
    payload = build().replace("A:509123457*B:516123459", "B:516123459*A:509123457")
    assert "order" in issue_codes(payload)


def test_empty_field_must_be_omitted():
    assert "empty_field" in issue_codes(build() + "*S:")


def test_lengths():
    assert "too_long" in issue_codes(build(G="FT " + "A" * 60 + "/1"))
    assert "too_long" in issue_codes(build(R="12345"))


@pytest.mark.parametrize("amount", ["483,60", "483.6", "483", "-483.60", "1,483.60", "4.8360e2", " 483.60"])
def test_amount_format_is_strict(amount):
    assert "amount_format" in issue_codes(build(O=amount))


def test_non_ascii_digits_are_rejected():
    assert "amount_format" in issue_codes(build(O="٤٨٣.٦٠"))


def test_issuer_must_be_a_valid_nif():
    assert "issuer_nif" in issue_codes(build(A="509123450"))
    assert "issuer_nif" in issue_codes(build(A="PT5091234"))
    assert "issuer_nif" in issue_codes(build(A="999999990"))  # placeholder cannot issue


def test_portuguese_buyer_needs_a_valid_nif_but_foreign_buyer_does_not():
    assert "buyer_nif" in issue_codes(build(B="516123450"))
    code = parse_qr(build(B="ESB12345678", C="ES"))
    assert code.buyer_tax_id == "ESB12345678" and code.buyer_country == "ES"
    assert parse_qr(build(B="999999990", C="Desconhecido")).buyer_country == "Desconhecido"


def test_buyer_country_format():
    assert "country_format" in issue_codes(build(C="Portugal"))
    assert "country_format" in issue_codes(build(C="pt"))


def test_document_fields():
    assert "doc_type" in issue_codes(build(D="XX"))
    assert "doc_type" in issue_codes(build(D="ft"))
    assert "status" in issue_codes(build(E="Z"))
    assert "date_format" in issue_codes(build(F="20260230"))
    assert "date_format" in issue_codes(build(F="2026-9-1"))
    assert "document_number" in issue_codes(build(G="FT2026/183"))
    assert "atcud_format" in issue_codes(build(H="SHORT-1"))
    assert "hash_format" in issue_codes(build(Q="ab"))
    assert "certificate_format" in issue_codes(build(R="12a"))


def test_all_problems_are_reported_together():
    codes = issue_codes(build(A="509123450", O="483,60", F="20261340"))
    assert {"issuer_nif", "amount_format", "date_format"} <= codes


def test_error_is_a_fiscal_qr_error_with_plain_owner_message():
    with pytest.raises(FiscalQRError) as info:
        parse_qr(build(O="x"))
    message = info.value.owner_message
    assert "QR code" in message and "O:" not in message and "Error" not in message


# --------------------------------------------------------------------------- #
# Tax blocks
# --------------------------------------------------------------------------- #


def test_block_amounts_need_their_fiscal_space():
    payload = build().replace("*N:", "*J3:10.00*N:")  # J3 without J1
    assert "block_without_space" in issue_codes(payload)


def test_vat_without_base():
    assert "vat_without_base" in issue_codes(build(I7=None))


def test_duplicate_fiscal_space():
    payload = build().replace("*N:", "*J1:PT*J7:1.00*J8:0.23*N:")
    assert "duplicate_space" in issue_codes(payload)


def test_invalid_fiscal_space():
    assert "space_format" in issue_codes(build(I1="PT-XX"))
    payload = build().replace("*N:", "*J1:0*N:")
    assert "space_format" in issue_codes(payload)


def test_no_tax_detail_document():
    """I1:0 = no tax breakdown (e.g. a receipt): no net/VAT is invented."""
    code = parse_qr(build(D="RG", G="RG 2026/9", H="CSDF7T5H-9", I1="0", I7=None, I8=None,
                          N="0.00", O="500.00"))
    assert not code.has_tax_detail
    assert code.checks.gross_total_ok is None and code.is_consistent
    assert code.doc_type == DocumentType.RECEIPT
    grouped = by_field(qr_to_observations(code, "ev"))
    assert CriticalField.NET_AMOUNT not in grouped and CriticalField.VAT_AMOUNT not in grouped
    assert [o.method for o in grouped[CriticalField.GROSS_AMOUNT]] == [ExtractionMethod.QR]


def test_no_tax_detail_cannot_carry_amounts():
    assert "no_tax_with_detail" in issue_codes(build(I1="0"))


def test_non_taxable_only_document():
    code = parse_qr(build(I7=None, I8=None, L="100.00", N="0.00", O="100.00"))
    assert code.net_total == Decimal("100.00") and code.vat_total == Decimal("0.00")
    assert code.is_consistent
    grouped = by_field(qr_to_observations(code, "ev"))
    assert grouped[CriticalField.NET_AMOUNT][0].location == "qr:L"
    assert grouped[CriticalField.VAT_AMOUNT][0].value == Decimal("0.00")


def test_base_without_vat_counts_as_zero_vat():
    code = parse_qr(build(I7="0.02", I8=None, N="0.00", O="0.02"))
    assert code.vat_total == Decimal("0.00") and code.is_consistent


def test_stamp_duty_is_part_of_n_but_not_vat():
    code = parse_qr(build(M="10.00", N="100.43", O="493.60"))
    assert code.is_consistent
    assert code.vat_total == Decimal("90.43")
    grouped = by_field(qr_to_observations(code, "ev"))
    assert grouped[CriticalField.VAT_AMOUNT][0].value == Decimal("90.43")
    assert grouped[CriticalField.GROSS_AMOUNT][1].value == Decimal("493.60")


def test_withholding_gives_amount_payable():
    code = parse_qr(build(P="45.00"))
    assert code.gross_total == Decimal("483.60")
    assert code.amount_payable == Decimal("438.60")


def test_mixed_rates_single_region():
    code = parse_qr(build(I3="100.00", I4="6.00", I5="100.00", I6="13.00", N="109.43", O="702.60"))
    assert code.is_consistent and code.checks.warnings == ()


# --------------------------------------------------------------------------- #
# Remarks (never change values)
# --------------------------------------------------------------------------- #


def remarks(code: PTQRCode) -> set[str]:
    return {w.code for w in code.checks.warnings}


def test_implausible_rate_is_a_remark_only():
    code = parse_qr(build(I8="51.11", N="51.11", O="444.28"))  # 13% declared as normal rate
    assert code.is_consistent
    assert "rate_implausible" in remarks(code)


def test_regional_rates_by_date():
    madeira_4 = build(I1="PT-MA", F="20241001", I3="100.00", I4="4.00", I7=None, I8=None,
                      N="4.00", O="104.00")
    assert remarks(parse_qr(madeira_4)) == set()
    madeira_5_too_late = madeira_4.replace("I4:4.00", "I4:5.00").replace("N:4.00", "N:5.00").replace(
        "O:104.00", "O:105.00")
    assert "rate_implausible" in remarks(parse_qr(madeira_5_too_late))
    azores_16 = build(I1="PT-AC", I8="62.91", N="62.91", O="456.08")
    assert remarks(parse_qr(azores_16)) == set()


def test_foreign_fiscal_space_has_no_rate_check():
    code = parse_qr(build(I1="ES", I8="82.57", N="82.57", O="475.74"))  # 21% Spanish VAT
    assert remarks(code) == set() and code.is_consistent


def test_dates_before_rate_table_are_unknown_not_wrong():
    code = parse_qr(build(F="20090115", H="0"))
    assert "rate_unknown" in remarks(code)
    assert "rate_implausible" not in remarks(code)


def test_missing_atcud_after_it_became_mandatory():
    assert "atcud_missing" in remarks(parse_qr(build(H="0")))
    old = parse_qr(build(H="0", F="20201103"))
    assert old.atcud is None and "atcud_missing" not in remarks(old)


def test_atcud_sequence_must_match_document_number():
    code = parse_qr(build(H="CSDF7T5H-184"))
    assert code.checks.atcud_matches_number is False and not code.is_consistent


def test_cancelled_and_credit_note():
    cancelled = parse_qr(build(E="A"))
    assert cancelled.is_cancelled and "cancelled" in remarks(cancelled)
    credit = parse_qr(build(D="NC", G="NC 2026/4", H="CSDF7T5H-4"))
    assert credit.doc_type == DocumentType.CREDIT_NOTE
    assert credit.gross_total == Decimal("483.60")  # sign comes from the document type


def test_status_family_remark():
    assert "status_family" in remarks(parse_qr(build(E="T")))


def test_hash_placeholder_is_a_remark():
    assert "hash_missing" in remarks(parse_qr(build(Q="0")))


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #


def test_looks_like_pt_qr():
    assert looks_like_pt_qr(build())
    assert looks_like_pt_qr("  " + AT_EXAMPLE)
    assert not looks_like_pt_qr("https://example.com/invoice/123")
    assert not looks_like_pt_qr("A:only")
    assert not looks_like_pt_qr(None)  # type: ignore[arg-type]


def test_arithmetic_observation_can_be_disabled():
    observations = qr_to_observations(parse_qr(build()), "ev", include_arithmetic=False)
    assert {o.method for o in observations} == {ExtractionMethod.QR}
