"""Field-level verification (§18, §19, §57): GREEN needs independent agreement."""

import itertools
import re
from decimal import Decimal as D

import pytest

from backoffice.domain.models import CriticalField as F
from backoffice.domain.models import ExtractionMethod as M
from backoffice.domain.models import FieldObservation, Quality, VerifiedField
from backoffice.verification import (
    NormalizeHints,
    VerificationPolicy,
    assess_field,
    bank_observation,
    channel_token,
    derive_gross,
    independent,
    is_high_rank,
    lineage,
    verify_field,
)
from backoffice.verification.fields import derived_location

PP = "ev_1@pp-ocrv6-medium"
VL = "ev_1@paddleocr-vl"
PAID = "ev_1@commercial"
EV = "ev_1"


def see(value, method=M.OCR, source=PP, confidence=0.6, location=None):
    return FieldObservation(
        value=value, source=source, method=method, confidence=confidence, location=location
    )


def qr(value, confidence=0.98):
    return see(value, M.QR, EV, confidence)


# Owner-facing text must not leak engine names, evidence ids or method codes (§36, §70).
_LEAKS = re.compile(r"ev_\d|@|pp-ocr|paddle|commercial|\bocr\b|\bvlm\b|arithmetic|structured|iban", re.I)


def assert_plain(reasons):
    for reason in reasons:
        assert not _LEAKS.search(reason), reason


# --------------------------------------------------------------------------- §19


def test_section_19_ocr_qr_arithmetic_and_bank_agree_green():
    arithmetic = derive_gross(see("393,17"), see("90,43"))
    observations = [see("483,60"), qr(D("483.60")), arithmetic, bank_observation(D("-483.60"))]
    field = verify_field(F.GROSS_AMOUNT, observations, currency="EUR")
    assert isinstance(field, VerifiedField)
    assert field.quality is Quality.GREEN and field.value == D("483.60")
    assert field.name == "gross_amount" and len(field.observations) == 4
    assert "Confirmed by" in field.reasons[0] and "€" not in field.reasons[0]
    assert_plain(field.reasons)


def test_section_19_qr_disagrees_red_conflict_with_both_values():
    arithmetic = derive_gross(see("393,17"), see("90,43"))
    observations = [see("483,60"), qr(D("438.60")), arithmetic, bank_observation(D("483.60"))]
    result = assess_field(F.GROSS_AMOUNT, observations, currency="EUR")
    assert result.quality is Quality.RED
    assert result.value is None  # never pick one
    assert set(result.conflicting_values) == {D("483.60"), D("438.60")}
    assert "€483.60" in result.reasons[0] and "€438.60" in result.reasons[0]
    assert "the QR code" in result.reasons[0]
    assert result.verified.quality is Quality.RED and result.verified.value is None
    assert_plain(result.reasons)


# --------------------------------------------------------------------------- independence


def test_single_source_is_amber_with_its_value():
    result = assess_field(F.GROSS_AMOUNT, [qr("483.60")])
    assert result.quality is Quality.AMBER and result.value == D("483.60")
    assert result.reasons == ("Only the QR code shows this so far.",)


def test_the_same_engine_twice_is_not_independent():
    two_scans = [see("483,60", source="ev_1@pp-ocrv6"), see("483.60", source="ev_2@pp-ocrv6")]
    result = assess_field(F.GROSS_AMOUNT, two_scans)
    assert result.quality is Quality.AMBER
    assert result.reasons == ("Only the scan shows this so far.",)


def test_two_different_engines_agree_but_readings_alone_are_not_enough():
    result = assess_field(
        F.GROSS_AMOUNT, [see("483,60"), see("483.60", M.VLM, VL), see("483,6", source=PAID)]
    )
    assert result.quality is Quality.AMBER and result.value == D("483.60")
    assert result.reasons == ("The scans agree, but I still need a more reliable source.",)


def test_embedded_text_and_a_scan_are_independent_green():
    text = see("483,60", M.EMBEDDED_TEXT, EV)
    assert assess_field(F.GROSS_AMOUNT, [text, see("483.60")]).quality is Quality.GREEN


def test_structured_sources_confirm_each_other():
    xml = see(D("483.6"), M.STRUCTURED_XML, EV, 0.98)
    assert assess_field(F.GROSS_AMOUNT, [xml, qr("483.60")]).quality is Quality.GREEN


def test_the_same_structured_source_twice_is_one_source():
    first, second = qr("483.60"), see("483.60", M.QR, "ev_2", 0.98)  # the invoice received twice
    assert assess_field(F.GROSS_AMOUNT, [first, second]).quality is Quality.AMBER


def test_a_qr_codes_own_subtotals_never_confirm_the_qr_code():
    own_sum = see(D("483.60"), M.ARITHMETIC, EV, 0.95, location="qr:arithmetic(net+vat+M)")
    result = assess_field(F.GROSS_AMOUNT, [qr("483.60"), own_sum])
    assert result.quality is Quality.AMBER
    assert result.reasons == ("Only the QR code shows this so far.",)


def test_arithmetic_over_scanned_numbers_is_still_scan_grade():
    other_engine_sum = derive_gross(see("393,17", source=PAID), see("90,43", source=PAID))
    assert not is_high_rank(other_engine_sum)
    result = assess_field(F.GROSS_AMOUNT, [see("483,60"), other_engine_sum])
    assert result.quality is Quality.AMBER
    assert "more reliable source" in result.reasons[0]


def test_arithmetic_over_the_same_scan_is_not_independent_of_it():
    same_engine_sum = derive_gross(see("393,17"), see("90,43"))
    assert not independent(see("483,60"), same_engine_sum)
    assert assess_field(F.GROSS_AMOUNT, [see("483,60"), same_engine_sum]).quality is Quality.AMBER


def test_arithmetic_over_structured_numbers_confirms_a_scan():
    xml_sum = derive_gross(
        see("393.17", M.STRUCTURED_XML, EV, 0.98), see("90.43", M.STRUCTURED_XML, EV, 0.98)
    )
    assert is_high_rank(xml_sum)
    assert assess_field(F.GROSS_AMOUNT, [see("483,60"), xml_sum]).quality is Quality.GREEN


def test_foreign_arithmetic_without_lineage_is_its_own_high_rank_channel():
    calc = see(D("483.60"), M.ARITHMETIC, "calc", 0.9)
    assert lineage(calc) == {"arithmetic"} and is_high_rank(calc)
    assert assess_field(F.GROSS_AMOUNT, [see("483,60"), calc]).quality is Quality.GREEN


def test_a_person_confirming_a_scan_is_green():
    person = see(D("483.60"), M.HUMAN, "owner", 1.0)
    result = assess_field(F.GROSS_AMOUNT, [see("483,60"), person])
    assert result.quality is Quality.GREEN and "a person's check" in result.reasons[0]


def test_channels_and_lineage():
    assert channel_token(M.OCR, "ev_9@PP-OCRv6") == "ocr:pp-ocrv6"
    assert channel_token(M.VLM, "paddle-vl") == "vlm:paddle-vl"
    assert channel_token(M.QR, "ev_9") == "qr"
    derived = see(D("1"), M.ARITHMETIC, EV, location=derived_location("x+y", ["qr", "ocr:pp"]))
    assert lineage(derived) == {"qr", "ocr:pp"}
    assert not is_high_rank(derived)  # one input was a reading
    assert lineage(see(D("1"), M.ARITHMETIC, EV, location="arithmetic:no lineage")) == {"arithmetic"}
    assert lineage(see(D("1"), M.ARITHMETIC, "ev_1@pp", location="ocr:line 4")) == {"ocr:pp"}


# --------------------------------------------------------------------------- normalization, confidence


def test_values_are_compared_after_normalization():
    observations = [see("€ 483,60"), qr(D("483.6")), see("483.60 EUR", M.EMBEDDED_TEXT, EV)]
    assert assess_field(F.GROSS_AMOUNT, observations).quality is Quality.GREEN
    number = assess_field(F.INVOICE_NUMBER, [qr("FT 2026/183"), see("ft2026/183")])
    assert number.quality is Quality.GREEN and number.value == "FT 2026/183"  # strongest source's form
    tax = assess_field(F.SUPPLIER_TAX_ID, [qr("503504564"), see("PT 503 504 564")])
    assert tax.quality is Quality.GREEN and tax.value == "503504564"


def test_confident_disagreement_is_red_even_between_readings():
    result = assess_field(F.GROSS_AMOUNT, [see("483,60"), see("488,60", M.VLM, VL)], currency="EUR")
    assert result.quality is Quality.RED and result.value is None
    # strongest method first (VLM outranks OCR), so the order never depends on input order
    assert (
        result.reasons[0]
        == "The sources disagree on the total: €488.60 (the first scan) vs €483.60 (the second scan)."
    )


def test_one_engine_is_just_the_scan_even_across_pages():
    result = assess_field(F.GROSS_AMOUNT, [see("483,60"), see("488,60", source="ev_2@pp-ocrv6-medium")])
    assert result.quality is Quality.RED
    assert "(the scan) vs" in result.reasons[0]


def test_unclear_dissent_blocks_green_but_is_not_a_conflict():
    unclear = see("438,60", source=PAID, confidence=0.2)
    result = assess_field(F.GROSS_AMOUNT, [qr("483.60"), see("483,60"), unclear], currency="EUR")
    assert result.quality is Quality.AMBER and result.value == D("483.60")
    assert (
        result.reasons[0]
        == "An unclear reading from the second scan showed €438.60, so I can't confirm this yet."
    )


def test_only_unclear_readings():
    agree = assess_field(F.GROSS_AMOUNT, [see("483,60", confidence=0.1), see("483.6", M.VLM, VL, 0.2)])
    assert agree.quality is Quality.AMBER and agree.value == D("483.60")
    assert agree.reasons == ("I only have an unclear reading of the total so far.",)
    differ = assess_field(F.GROSS_AMOUNT, [see("483,60", confidence=0.1), see("488,60", M.VLM, VL, 0.2)])
    assert differ.quality is Quality.AMBER and differ.value is None


def test_policy_threshold_decides_who_takes_part():
    observations = [qr("483.60"), see("438,60")]
    assert assess_field(F.GROSS_AMOUNT, observations).quality is Quality.RED
    strict = VerificationPolicy(min_confidence=0.9)
    assert assess_field(F.GROSS_AMOUNT, observations, policy=strict).quality is Quality.AMBER
    with pytest.raises(ValueError):
        VerificationPolicy(min_confidence=0)


# --------------------------------------------------------------------------- ambiguity, unreadable, missing


def test_ambiguous_reading_is_compatible_but_does_not_confirm():
    result = assess_field(F.ISSUE_DATE, [qr("20260403"), see("03/04/2026")])
    assert result.quality is Quality.AMBER and str(result.value) == "2026-04-03"
    resolved = assess_field(
        F.ISSUE_DATE, [qr("20260403"), see("03/04/2026")], hints=NormalizeHints(locale="pt-PT")
    )
    assert resolved.quality is Quality.GREEN


def test_ambiguous_only_lists_every_reading_and_picks_none():
    result = assess_field(F.ISSUE_DATE, [see("03/04/2026"), see("03-04-2026", M.EMBEDDED_TEXT, EV)])
    assert result.quality is Quality.AMBER and result.value is None
    assert {str(v) for v in result.possible_values} == {"2026-04-03", "2026-03-04"}
    assert "3 April 2026" in result.reasons[0] and "4 March 2026" in result.reasons[0]


def test_ambiguous_reading_that_fits_nothing_is_a_conflict():
    result = assess_field(F.ISSUE_DATE, [qr("20260501"), see("03/04/2026")])
    assert result.quality is Quality.RED
    assert "1 May 2026" in result.reasons[0] and " or " in result.reasons[0]


def test_unreadable_values_are_noted_not_counted():
    bad_checksum = see("PT50 0002 0123 1234 5678 9015 5")
    good = see("PT50000201231234567890154", M.STRUCTURED_XML, EV, 0.98)
    result = assess_field(F.IBAN, [bad_checksum, good])
    assert result.quality is Quality.AMBER and result.value == "PT50000201231234567890154"
    assert "I couldn't read the bank details from the scan." in result.reasons
    assert len(result.observations) == 2  # the full trail is kept
    only_bad = assess_field(F.IBAN, [bad_checksum])
    assert only_bad.value is None and only_bad.reasons == ("I couldn't read the bank details from the scan.",)
    assert_plain(result.reasons)


def test_missing_field():
    result = assess_field("invoice_number", [])
    assert result.quality is Quality.AMBER and result.value is None
    assert result.reasons == ("I haven't found the invoice number yet.",)


def test_iban_conflicts_are_shown_without_full_account_numbers():
    xml = see("PT50000201231234567890154", M.STRUCTURED_XML, EV, 0.98)
    other = see("GB82WEST12345698765432", M.EMBEDDED_TEXT, EV, 0.7)
    result = assess_field(F.IBAN, [xml, other])
    assert result.quality is Quality.RED
    assert "ending 0154" in result.reasons[0] and "PT50000201231234567890154" not in result.reasons[0]


# --------------------------------------------------------------------------- determinism, demotion


def test_result_does_not_depend_on_input_order():
    observations = [
        see("483,60"),
        qr(D("483.60")),
        bank_observation(D("483.60")),
        see("483,6", M.VLM, VL, 0.3),
    ]
    results = {
        (r.quality, r.value, r.reasons)
        for r in (assess_field(F.GROSS_AMOUNT, list(p)) for p in itertools.permutations(observations))
    }
    assert len(results) == 1


def test_demote_never_promotes_and_red_drops_the_value():
    green = assess_field(F.GROSS_AMOUNT, [qr("483.60"), see("483,60")])
    amber = green.demote(Quality.AMBER, "VAT rate check failed.")
    assert amber.quality is Quality.AMBER and amber.value == D("483.60")
    assert amber.demote(Quality.GREEN, "x").quality is Quality.AMBER  # never back up (§57)
    red = amber.demote(Quality.RED, "The amounts don't add up.")
    assert red.quality is Quality.RED and red.value is None
    assert red.reasons[-1] == "The amounts don't add up."


def test_the_confidence_threshold_is_inclusive():
    at_threshold = see("438,60", confidence=0.4)  # a weak label or an inconsistent fiscal QR code
    assert assess_field(F.GROSS_AMOUNT, [qr("483.60"), at_threshold]).quality is Quality.RED
    just_below = see("438,60", confidence=0.39)
    assert assess_field(F.GROSS_AMOUNT, [qr("483.60"), just_below]).quality is Quality.AMBER
