"""Regressions found in adversarial review of the verification module.

Each test pins one defect that used to slip through: a guess where the spec
demands a conflict (§19), a GREEN that no evidence earned (§57), wrong
arithmetic, or owner-facing text that echoed raw data (§36, §70).
"""

import itertools
import re
from datetime import date, datetime, timezone
from decimal import Decimal as D

import pytest

from backoffice.domain.models import CriticalField as F
from backoffice.domain.models import DocumentType
from backoffice.domain.models import ExtractionMethod as M
from backoffice.domain.models import FieldObservation, Quality
from backoffice.verification import (
    DocumentFingerprint,
    DuplicateKind,
    NormalizeHints,
    TaxBreakdown,
    TaxLine,
    assess_document,
    assess_field,
    bank_observation,
    check_sum,
    currency_mark,
    derive_gross,
    derive_observations,
    detect_tampering,
    fails_check_digits,
    find_duplicates,
    metadata_signals,
    normalize_date,
    normalize_tax_id,
    normalize_value,
    observation_signals,
)

PT = NormalizeHints(locale="pt-PT")
PT_RATES = (D("0.06"), D("0.13"), D("0.23"))
EV = "ev_1"
IBAN_A = "PT50000201231234567890154"
IBAN_SAME_ENDING = "PT33003300004567890120154"  # a different account, same last four digits


def see(value, method=M.OCR, source="ev_1@pp-ocrv6", confidence=0.6, location=None):
    return FieldObservation(
        value=value, source=source, method=method, confidence=confidence, location=location
    )


def text(value, confidence=0.7):
    return see(value, M.EMBEDDED_TEXT, EV, confidence)


def qr(value, confidence=0.98):
    return see(value, M.QR, EV, confidence)


def xml(value, confidence=0.98):
    return see(value, M.STRUCTURED_XML, "ev_2", confidence)


_LEAKS = re.compile(r"ev_\d|@|pp-ocr|\bocr\b|arithmetic|<|>|script", re.I)


def assert_plain(lines):
    for line in lines:
        assert not _LEAKS.search(line), line


# --------------------------------------------------------------------------- determinism


def test_tied_readings_give_the_same_answer_in_any_order():
    """Two engines finishing in a different order must not change the result or its wording."""
    tied = [see("483,60", source="ev_1@engine-a"), see("488,60", source="ev_1@engine-b")]
    results = {
        (r.quality, r.reasons, r.conflicting_values)
        for r in (assess_field(F.GROSS_AMOUNT, list(p)) for p in itertools.permutations(tied))
    }
    assert len(results) == 1
    forms = [xml("FT 2026/183"), see("FT2026/183", M.API, "supplier-api", 0.98)]
    values = {assess_field(F.INVOICE_NUMBER, list(p)).value for p in itertools.permutations(forms)}
    assert len(values) == 1


def test_tamper_details_do_not_depend_on_observation_order():
    observations = [text("483,60"), see("438,60", source="ev_1@a"), see("448,60", source="ev_1@b")]
    details = {
        observation_signals({F.GROSS_AMOUNT: list(p)})[0].detail for p in itertools.permutations(observations)
    }
    assert len(details) == 1


# --------------------------------------------------------------------------- currency on amounts (§19)


def test_same_number_in_two_currencies_is_a_conflict_not_a_match():
    result = assess_field(F.GROSS_AMOUNT, [text("$483.60"), qr("€483.60")])
    assert result.quality is Quality.RED and result.value is None
    assert "currency" in result.reasons[0] and "€" in result.reasons[0] and "$" in result.reasons[0]
    assert_plain(result.reasons)


def test_compatible_or_missing_currency_marks_still_agree():
    assert assess_field(F.GROSS_AMOUNT, [text("US$ 483.60"), qr("$483.60")]).quality is Quality.GREEN
    assert assess_field(F.GROSS_AMOUNT, [text("483,60 €"), qr(D("483.60"))]).quality is Quality.GREEN
    assert assess_field(F.GROSS_AMOUNT, [text("EUR 483,60"), qr("€ 483.60")]).quality is Quality.GREEN


def test_an_unclear_reading_in_another_currency_blocks_green():
    unclear = see("£483.60", source="ev_1@other", confidence=0.2)
    result = assess_field(F.GROSS_AMOUNT, [text("€483.60"), qr(D("483.60")), unclear])
    assert result.quality is Quality.AMBER and result.value == D("483.60")


def test_currency_printed_next_to_amounts_is_checked_against_the_document_currency():
    observations = {
        F.GROSS_AMOUNT: [see("$483.60"), qr(D("483.60"))],
        F.NET_AMOUNT: [see("393,17"), qr(D("393.17"))],
        F.VAT_AMOUNT: [see("90,43"), qr(D("90.43"))],
        F.CURRENCY: [text("EUR")],
    }
    result = assess_document(observations, PT_RATES, doc_type=DocumentType.RECEIPT, hints=PT)
    assert result.quality is Quality.RED and "currency" in result.conflicts
    # and a matching mark is one more independent witness of the currency
    observations[F.GROSS_AMOUNT] = [see("483,60 €"), qr(D("483.60"))]
    agreed = assess_document(observations, PT_RATES, doc_type=DocumentType.RECEIPT, hints=PT)
    assert agreed.fields["currency"].quality is Quality.GREEN


def test_a_sum_never_counts_as_a_second_witness_of_the_currency():
    own_sum = see("€483.60", M.ARITHMETIC, EV, 0.95, location="qr:arithmetic(net+vat+M)")
    observations = {F.GROSS_AMOUNT: [qr("€483.60"), own_sum]}
    result = assess_document(observations, required=[F.CURRENCY])
    assert result.fields["currency"].quality is Quality.AMBER  # the QR code alone printed the €


# --------------------------------------------------------------------------- invalid values (§19)


def test_an_invalid_account_number_printed_by_the_document_is_a_conflict():
    result = assess_field(
        F.IBAN, [xml(IBAN_A), text("PT50000201231234567890155"), see(IBAN_A, M.BANK, "bank", 0.99)]
    )
    assert result.quality is Quality.RED and result.value is None
    assert IBAN_A in result.conflicting_values
    assert "isn't valid" in result.reasons[0]
    assert "PT5000020123" not in " ".join(result.reasons)  # no full bank details in passing text
    assert_plain(result.reasons)


def test_a_lone_invalid_value_is_named_as_invalid_not_unreadable():
    result = assess_field(F.IBAN, [text("PT50000201231234567890155")])
    assert result.quality is Quality.AMBER and result.value is None
    assert result.reasons == ("What the document text shows for the bank details isn't valid.",)


def test_a_scan_that_misreads_check_digits_is_still_just_unreadable():
    result = assess_field(
        F.IBAN, [xml(IBAN_A), see("PT50000201231234567890155"), see(IBAN_A, M.BANK, "bank", 0.99)]
    )
    assert result.quality is Quality.GREEN
    assert "I couldn't read the bank details from the scan." in result.reasons


def test_a_truncated_account_number_is_a_partial_read_not_a_different_account():
    wrapped = text("PT50 0002 0123 1234 5678 90")  # the text layer broke the line; 21 of 25 characters
    result = assess_field(F.IBAN, [xml(IBAN_A), wrapped, see(IBAN_A, M.BANK, "bank", 0.99)])
    assert result.quality is Quality.GREEN
    assert "I couldn't read the bank details from the document text." in result.reasons


def test_a_payment_reference_with_bad_check_digits_is_a_conflict():
    result = assess_field(F.PAYMENT_REFERENCE, [xml("RF18 5390 0754 7034"), text("RF18 5390 0754 7035")])
    assert result.quality is Quality.RED and "RF18539007547034" in result.conflicting_values


def test_iban_conflict_shows_enough_digits_to_tell_accounts_apart():
    result = assess_field(F.IBAN, [xml(IBAN_A), text(IBAN_SAME_ENDING)])
    assert result.quality is Quality.RED
    reason = result.reasons[0]
    assert "ending 0154 (" not in reason  # would read as the same account twice
    assert IBAN_A not in reason and IBAN_SAME_ENDING not in reason
    endings = re.findall(r"ending (\w+)", reason)
    assert len(endings) == 2 and endings[0] != endings[1] and all(len(e) <= 8 for e in endings)


# --------------------------------------------------------------------------- arithmetic


def test_tax_breakdown_totals_respect_the_sign_of_each_line():
    lines = (TaxLine(D("100.00"), D("23.00"), D("0.23")), TaxLine(D("-10.00"), D("-0.60"), D("0.06")))
    breakdown = TaxBreakdown(lines=lines, source=EV, method=M.QR, confidence=0.98)
    assert breakdown.total_net == D("90.00") and breakdown.total_vat == D("22.40")
    credit = TaxBreakdown(
        lines=(TaxLine(D("-100.00"), D("-23.00")), TaxLine(D("-10.00"), D("-0.60"))),
        source=EV, method=M.QR, confidence=0.98,
    )  # fmt: skip
    assert credit.total_net == D("110.00") and credit.total_vat == D("23.60")  # magnitudes


def test_other_charges_can_be_deductions():
    assert check_sum(D("100.00"), D("23.00"), D("118.00"), other_charges=D("-5.00")).ok
    assert not check_sum(D("100.00"), D("23.00"), D("128.00"), other_charges=D("-5.00")).ok
    explained = check_sum(D("100.00"), D("23.00"), D("120.00"), other_charges=D("-5.00")).explain("EUR")
    assert "−€5.00" not in explained and "€5.00 taken off" in explained
    derived = derive_observations({F.NET_AMOUNT: [qr("100.00")], F.VAT_AMOUNT: [qr("23.00")]},
                                  other_charges=D("-5.00"))  # fmt: skip
    assert derived[F.GROSS_AMOUNT][0].value == D("118.00")
    assert derive_gross(qr("100.00"), qr("23.00"), other_charges=D("-5.00")).value == D("118.00")
    observations = {
        F.GROSS_AMOUNT: [see("118,00"), qr(D("118.00"))],
        F.NET_AMOUNT: [see("100,00"), qr(D("100.00"))],
        F.VAT_AMOUNT: [see("23,00"), qr(D("23.00"))],
    }
    result = assess_document(observations, PT_RATES, doc_type=DocumentType.RECEIPT, other_charges=D("-5.00"),
                             required=[F.GROSS_AMOUNT], hints=PT)  # fmt: skip
    assert result.sum_check.ok and result.quality is Quality.GREEN


# --------------------------------------------------------------------------- duplicates


def _fp(doc_id, **overrides):
    base = dict(
        document_id=doc_id, tenant_id="t1", sha256s=frozenset({"one-pdf"}), evidence_ids=("ev_pdf",),
        doc_type=DocumentType.INVOICE, supplier_tax_id="503504564", invoice_number="FT 2026/183",
        issue_date=date(2026, 9, 18), gross_amount=D("117.20"), currency="EUR",
    )  # fmt: skip
    base.update(overrides)
    return DocumentFingerprint(**base)


def test_one_file_holding_two_invoices_is_not_an_exact_duplicate():
    first = _fp("d1")
    second = _fp("d2", invoice_number="FT 2026/184", gross_amount=D("45.00"))
    assert find_duplicates(second, [first]) == ()


def test_two_invoices_in_one_file_with_the_same_total_are_only_a_near_duplicate():
    first = _fp("d1")
    second = _fp("d2", invoice_number="FT 2026/184")
    (verdict,) = find_duplicates(second, [first])
    assert verdict.kind is DuplicateKind.NEAR and not verdict.auto_merge_safe


def test_same_file_with_the_same_details_is_still_exact():
    (verdict,) = find_duplicates(_fp("d2", invoice_number=None), [_fp("d1")])
    assert verdict.kind is DuplicateKind.EXACT and verdict.auto_merge_safe


def test_same_file_from_two_suppliers_is_not_exact():
    assert find_duplicates(_fp("d2", supplier_tax_id="500000000"), [_fp("d1")]) == ()


def test_a_single_hash_given_as_text_is_one_hash_not_its_characters():
    a = _fp("d1", sha256s="ab12", evidence_ids="ev_a", invoice_number=None)
    b = _fp("d2", sha256s="cd21", evidence_ids="ev_b", invoice_number=None, gross_amount=D("9.99"))
    assert a.sha256s == frozenset({"ab12"}) and a.evidence_ids == ("ev_a",)
    assert find_duplicates(b, [a]) == ()  # sharing the characters "1" and "2" is not sharing a file


def test_issue_dates_given_as_datetimes_are_compared_as_days():
    a = _fp(
        "d1",
        sha256s={"x"},
        invoice_number="A-1",
        issue_date=datetime(2026, 9, 18, 23, 0, tzinfo=timezone.utc),
    )
    b = _fp("d2", sha256s={"y"}, invoice_number="A-2", issue_date=date(2026, 9, 19))
    (verdict,) = find_duplicates(b, [a])
    assert verdict.kind is DuplicateKind.NEAR and a.issue_date == date(2026, 9, 18)


# --------------------------------------------------------------------------- tamper


def test_editing_tool_detail_never_echoes_raw_metadata():
    raw = "Adobe Photoshop 25.0 <script>alert(1)</script> ev_123 " + "x" * 5000
    (signal,) = metadata_signals({"Producer": raw})
    assert signal.detail == "The file was saved with Photoshop, a program often used to edit documents."
    assert_plain([signal.detail])
    assert len(signal.values[0]) <= 200  # the fraud engine gets a bounded copy of the raw name


def test_issue_date_may_be_a_datetime():
    meta = {"CreationDate": "D:20260918100000Z", "ModDate": "D:20260925160000Z"}
    signals = metadata_signals(meta, datetime(2026, 9, 18, 9, 0, tzinfo=timezone.utc))
    assert [s.kind.value for s in signals] == ["edited_after_issue"]
    assert detect_tampering(metadata=meta, issue_date=datetime(2026, 9, 18, tzinfo=timezone.utc))


# --------------------------------------------------------------------------- normalization


def test_dotted_dates_are_only_resolved_by_their_shape_or_a_hint():
    assert normalize_date("03.04.2026").ambiguous  # no locale: either reading is possible
    assert normalize_date("03.04.2026", day_first=True).value == date(2026, 4, 3)
    assert normalize_date("03.04.2026", day_first=False).value == date(2026, 3, 4)
    assert normalize_value(F.ISSUE_DATE, "03.04.2026", NormalizeHints(locale="de-DE")).value == date(
        2026, 4, 3
    )
    assert normalize_date("18.09.2026").value == date(2026, 9, 18)  # only one reading is a real date


@pytest.mark.parametrize(
    "raw",
    ["NIF: 503 504 564", "NIF 503504564", "NIPC 503504564", "Contribuinte: 503504564", "VAT No. PT 503 504 564",
     "VAT: PT503504564", "USt-IdNr. PT503504564"],
)  # fmt: skip
def test_tax_id_labels_are_not_part_of_the_number(raw):
    assert normalize_tax_id(raw).value == "503504564"


def test_labels_never_eat_a_real_prefix():
    assert normalize_tax_id("NO 999 999 999 MVA").value == "999999999"
    assert normalize_tax_id("CIF B12345678").value == "B12345678"
    assert normalize_tax_id("NIE X1234567L").value == "X1234567L"


@pytest.mark.parametrize(
    ("raw", "written", "code"),
    [("€ 1.492,30", "€", "EUR"), ("1 492,30 EUR", "EUR", "EUR"), ("(US$ 12.00)", "US$", "USD"),
     ("-£12.00", "£", "GBP"), ("12,00 zł", "zł", "PLN")],
)  # fmt: skip
def test_currency_mark_reads_the_currency_printed_with_an_amount(raw, written, code):
    mark = currency_mark(raw)
    assert mark is not None and mark.written == written and code in mark.codes


@pytest.mark.parametrize("raw", ["1.492,30", D("12.00"), 12, "Total: 12", "not money", None])
def test_currency_mark_is_absent_without_a_printed_currency(raw):
    assert currency_mark(raw) is None


def test_only_complete_values_fail_their_check_digits():
    assert fails_check_digits(F.IBAN, "PT50 0002 0123 1234 5678 9015 5")
    assert not fails_check_digits(F.IBAN, "PT50 0002 0123 1234 5678 9015 4")  # valid
    assert not fails_check_digits(F.IBAN, "PT50 0002 0123 1234 5678 90")  # truncated: a partial read
    assert fails_check_digits("payment_reference", "RF18 5390 0754 7035")
    assert not fails_check_digits(F.PAYMENT_REFERENCE, "123 456 789")  # no check digits to fail
    assert not fails_check_digits(F.INVOICE_NUMBER, "PT50000201231234567890155")
    assert not fails_check_digits(F.IBAN, None)


def test_long_text_values_are_shortened_in_owner_text():
    long_number = "FT " + "9" * 300
    result = assess_field(F.INVOICE_NUMBER, [xml(long_number), text("FT 2026/183")])
    assert result.quality is Quality.RED
    assert len(result.reasons[0]) < 250
    assert (
        result.conflicting_values and long_number in result.conflicting_values
    )  # full value kept for review


def test_bank_observation_joins_only_as_a_magnitude_and_keeps_the_trail():
    obs = bank_observation(D("-0.00"))
    assert obs.value == D("0.00") and obs.method is M.BANK
