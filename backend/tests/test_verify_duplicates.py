"""Duplicate detection (§25) and the §26 "duplicate with a different IBAN" signal."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from backoffice.domain.models import Document, DocumentType, Quality
from backoffice.verification import (
    DocumentFingerprint,
    DuplicateIndex,
    DuplicateKind,
    DuplicateSignal,
    find_duplicates,
)
from backoffice.verification.duplicates import supplier_name_key

T0 = datetime(2026, 9, 18, 9, 0, tzinfo=timezone.utc)
IBAN_A = "PT50000201231234567890154"
IBAN_B = "GB82WEST12345698765432"


def fp(doc_id, **overrides):
    base = dict(
        document_id=doc_id,
        tenant_id="t1",
        sha256s=frozenset({f"hash-{doc_id}"}),
        evidence_ids=(f"ev-{doc_id}",),
        doc_type=DocumentType.INVOICE,
        supplier_tax_id="503504564",
        supplier_name="Vodafone Portugal, S.A.",
        invoice_number="FT 2026/183",
        issue_date=date(2026, 9, 18),
        gross_amount=D("117.20"),
        currency="EUR",
        iban=IBAN_A,
    )
    base.update(overrides)
    return DocumentFingerprint(**base)


def test_same_file_is_an_exact_duplicate_safe_to_merge():
    first = fp("d1", received_at=T0)
    again = fp("d2", sha256s={"HASH-D1 "}, received_at=T0 + timedelta(hours=1), invoice_number=None)
    (verdict,) = find_duplicates(again, [first])
    assert verdict.kind is DuplicateKind.EXACT and verdict.auto_merge_safe
    assert verdict.merge.keep_id == "d1" and verdict.merge.merge_id == "d2"  # first received kept
    assert verdict.merge.link_evidence_ids == ("ev-d2",)  # evidence is linked, never deleted
    assert verdict.reasons == ("This is the same file as one I already have.",)


def test_same_supplier_and_number_across_formats():
    emailed = fp("d1")
    photo = fp("d2", supplier_tax_id="PT 503 504 564", invoice_number="ft2026/183")
    (verdict,) = find_duplicates(photo, [emailed])
    assert verdict.kind is DuplicateKind.SAME_NUMBER and verdict.auto_merge_safe and not verdict.signals
    assert verdict.reasons == ("It has the same supplier and invoice number as one I already have.",)


def test_duplicate_with_a_different_iban_is_a_fraud_signal_and_never_merged():
    original = fp("d1")
    resent = fp("d2", iban=IBAN_B)
    (verdict,) = find_duplicates(resent, [original])
    assert verdict.kind is DuplicateKind.SAME_NUMBER
    assert verdict.signals == (DuplicateSignal.DIFFERENT_IBAN,) and verdict.fraud_signal
    assert verdict.merge is None and not verdict.auto_merge_safe
    assert "The bank details are different from the other copy." in verdict.reasons


def test_same_number_with_a_different_total_or_date():
    (verdict,) = find_duplicates(fp("d2", gross_amount=D("171.20"), issue_date=date(2026, 9, 19)), [fp("d1")])
    assert verdict.signals == (DuplicateSignal.DIFFERENT_AMOUNT, DuplicateSignal.DIFFERENT_DATE)
    assert verdict.merge is None and not verdict.auto_merge_safe and not verdict.fraud_signal


def test_near_duplicate_asks_before_merging():
    receipt = fp("d2", invoice_number="FR 2026/77", issue_date=date(2026, 9, 20))
    (verdict,) = find_duplicates(receipt, [fp("d1")])
    assert verdict.kind is DuplicateKind.NEAR and not verdict.auto_merge_safe
    assert verdict.merge is not None  # a suggestion, to be confirmed
    assert verdict.reasons == (
        "Same supplier and total as one I already have, 2 days apart, but with a different number.",
        "It may be a separate purchase, so I'll ask before merging.",
    )
    same_day = find_duplicates(fp("d3", invoice_number=None), [fp("d1")])[0]
    assert same_day.kind is DuplicateKind.NEAR and "on the same day" in same_day.reasons[0]


def test_near_duplicate_with_a_different_iban_is_flagged():
    (verdict,) = find_duplicates(fp("d2", invoice_number="FT 2026/184", iban=IBAN_B), [fp("d1")])
    assert verdict.kind is DuplicateKind.NEAR and verdict.fraud_signal and verdict.merge is None


def test_near_window_is_configurable():
    later = fp("d2", invoice_number="FT 2026/190", issue_date=date(2026, 9, 23))
    assert find_duplicates(later, [fp("d1")]) == ()
    assert find_duplicates(later, [fp("d1")], near_days=7)[0].kind is DuplicateKind.NEAR
    with pytest.raises(ValueError):
        DuplicateIndex(near_days=-1)


@pytest.mark.parametrize(
    "other",
    [
        dict(doc_type=DocumentType.CREDIT_NOTE, invoice_number="NC 2026/5"),  # a refund, not a copy
        dict(supplier_tax_id="999999990", supplier_name="Someone Else"),
        dict(invoice_number="FT 2026/184", gross_amount=D("117.21")),
        dict(invoice_number="FT 2026/184", currency="USD"),
        dict(invoice_number="FT 2026/184", issue_date=None),
        dict(tenant_id="t2", sha256s={"hash-d1"}),  # nothing crosses tenants
    ],
)
def test_not_duplicates(other):
    assert find_duplicates(fp("d2", **other), [fp("d1")]) == ()


def test_other_kind_is_a_wildcard_but_a_credit_note_with_the_same_number_is_not_a_copy():
    assert (
        find_duplicates(fp("d2", doc_type=DocumentType.OTHER), [fp("d1")])[0].kind
        is DuplicateKind.SAME_NUMBER
    )
    assert find_duplicates(fp("d2", doc_type=DocumentType.CREDIT_NOTE), [fp("d1")]) == ()


def test_copies_filed_under_two_companies_are_not_merged_automatically():
    (verdict,) = find_duplicates(fp("d2", entity_id="ent_b"), [fp("d1", entity_id="ent_a")])
    assert not verdict.auto_merge_safe
    assert "The two copies are filed under different companies." in verdict.reasons


def test_supplier_name_fallback_is_never_automatic():
    a = fp("d1", supplier_tax_id=None, supplier_name="Café Central, Lda.")
    b = fp("d2", supplier_tax_id=None, supplier_name="CAFE CENTRAL LDA")
    (verdict,) = find_duplicates(b, [a])
    assert verdict.kind is DuplicateKind.SAME_NUMBER and not verdict.auto_merge_safe
    assert verdict.reasons[-1] == "I'll ask before merging."
    # two different tax ids are two suppliers, whatever the names say
    assert find_duplicates(fp("d3", supplier_tax_id="500000000"), [fp("d1")]) == ()


def test_keep_the_best_evidence_then_the_first_received():
    early_amber = fp("d1", quality=Quality.AMBER, received_at=T0)
    late_green = fp("d2", quality=Quality.GREEN, received_at=T0 + timedelta(days=1), sha256s={"hash-d1"})
    (verdict,) = find_duplicates(late_green, [early_amber])
    assert verdict.merge.keep_id == "d2"


def test_index_orders_verdicts_and_handles_updates():
    index = DuplicateIndex([fp("d1"), fp("d2", invoice_number="FT 2026/200", issue_date=date(2026, 9, 17))])
    assert len(index) == 2
    candidate = fp("d3", sha256s={"hash-d2"}, invoice_number=None)
    verdicts = index.check(candidate)
    assert [(v.duplicate_of, v.kind) for v in verdicts] == [
        ("d2", DuplicateKind.EXACT),
        ("d1", DuplicateKind.NEAR),
    ]
    assert [v.duplicate_of for v in index.check(fp("d1"))] == ["d2"]  # never a duplicate of itself
    index.add(fp("d2", invoice_number="FT 2026/200", gross_amount=D("5.00"), sha256s={"other"}))
    assert [v.duplicate_of for v in index.check(candidate)] == ["d1"]
    index.remove("d1")
    index.remove("unknown")
    assert index.check(candidate) == () and len(index) == 1


def test_fingerprint_from_a_domain_document():
    doc = Document(
        tenant_id="t1",
        evidence_ids=["ev_1"],
        doc_type=DocumentType.INVOICE,
        supplier_tax_id="PT503504564",
        invoice_number="FT 2026/183",
        issue_date=date(2026, 9, 18),
        gross_amount=D("117.2"),
        iban="pt50 0002 0123 1234 5678 9015 4",
    )
    print_ = DocumentFingerprint.from_document(doc, sha256s=["ABC"], received_at=T0)
    assert print_.supplier_tax_id == "503504564" and print_.invoice_number == "FT2026/183"
    assert print_.gross_amount == D("117.20") and print_.iban == IBAN_A and print_.sha256s == {"abc"}
    assert find_duplicates(print_, [fp("d1")])[0].kind is DuplicateKind.SAME_NUMBER


def test_fingerprint_rejects_float_money_naive_times_and_ignores_invalid_ibans():
    with pytest.raises(TypeError):
        fp("d1", gross_amount=117.2)
    with pytest.raises(ValueError):
        fp("d1", received_at=datetime(2026, 9, 18))
    assert fp("d1", iban="PT50000201231234567890155").iban is None  # bad check digits: unknown, not different


def test_supplier_name_key():
    assert supplier_name_key("Vodafone Portugal, S.A.") == supplier_name_key("VODAFONE PORTUGAL SA")
    assert supplier_name_key("Acme Ltd") == supplier_name_key("acme") == "acme"
    assert supplier_name_key("Ações & Cia, Lda") == "acoes and cia"
    assert supplier_name_key("") is None and supplier_name_key("S.A.") is None
