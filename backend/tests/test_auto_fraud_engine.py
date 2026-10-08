"""Fraud engine (§26): hard stops, severities, plain owner messages, read-only guarantees."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from backoffice.domain.models import Document, DocumentType, LegalEntity, Supplier
from backoffice.fraud.engine import (
    AlteredDocumentHint,
    FraudAssessment,
    FraudCase,
    FraudConfig,
    FraudSignal,
    Severity,
    SignalKind,
    assess,
)

D = Decimal
KNOWN_IBAN = "PT50 0002 0123 1234 5678 9015 4"
NEW_IBAN = "GB82 WEST 1234 5698 7654 32"  # valid, but not Vodafone's
LT_IBAN = "LT12 1000 0111 0100 1000"  # valid published example
OUR_IBAN = "DE89 3704 0044 0532 0130 00"
HAZEL = LegalEntity(id="ent_hazel", tenant_id="t1", name="Hazel Tree, Lda.", country="PT",
                    tax_id="PT509123456", own_ibans=[OUR_IBAN])  # fmt: skip
VODAFONE = Supplier(id="sup_voda", tenant_id="t1", name="Vodafone Portugal - Comunicações Pessoais, S.A.",
                    tax_id="PT502544180", known_ibans=[KNOWN_IBAN], email_domains=["vodafone.pt"],
                    countries=["PT"])  # fmt: skip
_JARGON = re.compile(r"\bentit(?:y|ies)\b|\breconcil|\bexception|\berror\b|\b[a-z]{2,8}_[0-9a-f]{16}\b|!", re.I)


def invoice(n: int, amount: str = "92.40", iban: str | None = KNOWN_IBAN, number: str | None = None, **kw) -> Document:
    return Document(
        id=f"doc_{n}", tenant_id="t1", evidence_ids=[f"ev_{n}"], doc_type=kw.pop("doc_type", DocumentType.INVOICE),
        supplier_name="Vodafone Portugal", supplier_tax_id=kw.pop("supplier_tax_id", "PT502544180"),
        customer_tax_id=kw.pop("customer_tax_id", "509123456"), invoice_number=number or f"FT 2026/{n}",
        issue_date=kw.pop("issue_date", date(2026, 1 + n % 12, 20)), gross_amount=D(amount), iban=iban, **kw,
    )  # fmt: skip


HISTORY = [invoice(i, amount) for i, amount in enumerate(["92.40", "95.10", "91.00", "99.90", "92.40", "93.80"], 1)]


_DEFAULT = object()


def case(document: Document | None | object = _DEFAULT, **kw) -> FraudCase:
    doc = invoice(100) if document is _DEFAULT else document
    base = dict(entities=[HAZEL], supplier=VODAFONE, document=doc, history=HISTORY,
                sender="Vodafone <faturas@vodafone.pt>")  # fmt: skip
    base.update(kw)
    return FraudCase(**base)


def kinds(result: FraudAssessment) -> set[SignalKind]:
    return {s.kind for s in result.signals}


def assert_plain(result: FraudAssessment) -> None:
    for line in [result.owner_message or "", *(s.owner_line for s in result.signals), *result.passed]:
        assert not _JARGON.search(line), line
        assert "PT50000201" not in line and "GB82WEST" not in line  # never a full IBAN


# --------------------------------------------------------------------------- clean case


def test_clean_invoice_passes() -> None:
    result = assess(case())
    assert not result.hard_stop and result.owner_message is None
    assert result.signals == ()
    assert result.passed == (
        "Bank details match what Vodafone Portugal used before.",
        "Sent from Vodafone Portugal's usual email address.",
        "The amount is in line with Vodafone Portugal's usual invoices.",
        "The bank account is in Portugal, as usual.",
        "Addressed to Hazel Tree.",
    )
    assert result.beneficiary_ibans == ("PT50000201231234567890154",)


# --------------------------------------------------------------------------- bank details


def test_spec_changed_iban_message() -> None:
    supplier = VODAFONE.model_copy(update={"name": "Vodafone", "countries": ["PT", "GB"]})
    result = assess(case(invoice(100, iban=NEW_IBAN), supplier=supplier))
    assert result.hard_stop and result.needs_beneficiary_verification
    assert result.owner_message == "Vodafone changed the IBAN shown on its invoice. Payment blocked."
    [signal] = result.of_kind(SignalKind.CHANGED_IBAN)
    assert signal.severity is Severity.CRITICAL
    assert signal.facts["iban"] == "GB82 •••• 5432" and signal.facts["origin"] == "invoice"
    assert_plain(result)


def test_engine_never_changes_the_profile() -> None:
    before = VODAFONE.model_dump()
    assess(case(invoice(100, iban=NEW_IBAN)))
    assert VODAFONE.model_dump() == before
    assert VODAFONE.known_ibans == [KNOWN_IBAN]


def test_formatting_differences_are_not_a_change() -> None:
    result = assess(case(invoice(100, iban="pt50000201231234567890154")))
    assert SignalKind.CHANGED_IBAN not in kinds(result) and not result.hard_stop


def test_invalid_iban_is_critical() -> None:
    result = assess(case(invoice(100, iban="PT50 0002 0123 1234 5678 9015 5")))
    assert result.hard_stop
    assert result.signals[0].kind is SignalKind.INVALID_BANK_DETAILS
    assert result.owner_message == "The bank details on this invoice are not valid. Payment blocked."
    assert result.beneficiary_ibans == ()


def test_iban_in_email_body_is_checked() -> None:
    result = assess(case(invoice(100), message_text=f"Please pay to {NEW_IBAN} from now on."))
    changed = result.of_kind(SignalKind.CHANGED_IBAN)
    assert changed and changed[0].owner_line == "The email gives a new IBAN for Vodafone Portugal."
    assert result.hard_stop and changed[0].facts["origin"] == "email"


def test_payment_instruction_without_document() -> None:
    result = assess(case(document=None, payment_iban=NEW_IBAN, payment_amount=D("1240.00")))
    assert result.of_kind(SignalKind.CHANGED_IBAN)[0].owner_line == "This payment goes to a new IBAN for Vodafone Portugal."
    assert result.of_kind(SignalKind.UNUSUAL_AMOUNT)[0].owner_line == (
        "Vodafone Portugal usually charges around €93.10. This payment is €1,240.00."
    )
    with pytest.raises(TypeError):
        case(document=None, payment_amount=92.4)


def test_new_supplier_is_a_new_payment_recipient() -> None:
    newcomer = Supplier(id="sup_new", tenant_id="t1", name="Printing Co")
    result = assess(case(invoice(100), supplier=newcomer, history=[], sender=None))
    assert result.hard_stop and result.needs_beneficiary_verification
    [signal] = result.of_kind(SignalKind.NEW_PAYMENT_RECIPIENT)
    assert signal.severity is Severity.HIGH
    assert signal.owner_line == "Printing Co asks to be paid into an account you have not paid before."
    assert result.of_kind(SignalKind.NOT_ENOUGH_HISTORY)[0].severity is Severity.INFO


def test_our_own_iban_is_not_a_beneficiary() -> None:
    result = assess(case(invoice(100), message_text=f"Paid from {OUR_IBAN}."))
    assert result.beneficiary_ibans == ("PT50000201231234567890154",)
    assert not result.hard_stop


# --------------------------------------------------------------------------- sender


def test_lookalike_domain_is_critical() -> None:
    result = assess(case(sender="Vodafone <faturas@vodafone-pt.com>"))
    [signal] = result.of_kind(SignalKind.LOOKALIKE_DOMAIN)
    assert signal.severity is Severity.CRITICAL
    assert signal.owner_line == "The email came from an address made to look like vodafone.pt."
    assert result.owner_message == "The email came from an address made to look like vodafone.pt. Payment blocked."


def test_changed_and_free_mail_domains() -> None:
    changed = assess(case(sender="faturas@vodafone.com"))
    assert changed.of_kind(SignalKind.CHANGED_EMAIL_DOMAIN)[0].owner_line == (
        "The email came from vodafone.com, not Vodafone Portugal's usual address."
    )
    free = assess(case(sender="vodafone.faturas@gmail.com"))
    assert free.of_kind(SignalKind.CHANGED_EMAIL_DOMAIN)[0].owner_line == (
        "The email came from a personal address, not Vodafone Portugal's usual one."
    )
    assert changed.hard_stop and free.hard_stop


# --------------------------------------------------------------------------- amount


def test_unusual_high_amount_is_a_hard_stop() -> None:
    result = assess(case(invoice(100, amount="1240.00")))
    [signal] = result.of_kind(SignalKind.UNUSUAL_AMOUNT)
    assert signal.severity is Severity.HIGH
    assert signal.owner_line == "Vodafone Portugal usually charges around €93.10. This invoice is €1,240.00."
    assert result.owner_message == (
        "Vodafone Portugal usually charges around €93.10. This invoice is €1,240.00. Payment blocked."
    )


def test_normal_variation_and_small_increase_pass() -> None:
    assert not assess(case(invoice(100, amount="104.00"))).hard_stop
    fixed = [invoice(i, "39.00") for i in range(1, 7)]
    # A fixed-price subscription moving 5% is a price change, not fraud.
    assert not assess(case(invoice(100, amount="41.00"), history=fixed)).hard_stop
    # ... but tripling is.
    assert assess(case(invoice(100, amount="117.00"), history=fixed)).hard_stop


def test_unusually_low_amount_is_only_a_warning() -> None:
    result = assess(case(invoice(100, amount="10.00")))
    [signal] = result.of_kind(SignalKind.UNUSUAL_AMOUNT)
    assert signal.severity is Severity.WARNING and not result.hard_stop


def test_minimum_history_before_judging_amounts() -> None:
    result = assess(case(invoice(100, amount="5000"), history=HISTORY[:4]))
    assert not result.of_kind(SignalKind.UNUSUAL_AMOUNT)
    assert result.of_kind(SignalKind.NOT_ENOUGH_HISTORY)[0].facts == {"history": "4", "needed": "5"}
    relaxed = assess(case(invoice(100, amount="5000"), history=HISTORY[:4]), FraudConfig(min_history=3))
    assert relaxed.of_kind(SignalKind.UNUSUAL_AMOUNT)


def test_amount_history_ignores_other_currencies_credit_notes_and_itself() -> None:
    current = invoice(100, amount="1240.00")
    noisy = [*HISTORY[:4], invoice(7, "1240.00", currency="USD"), invoice(8, "1240.00", doc_type=DocumentType.CREDIT_NOTE), current]
    result = assess(case(current, history=noisy))
    assert result.of_kind(SignalKind.NOT_ENOUGH_HISTORY)


# --------------------------------------------------------------------------- duplicates


def test_duplicate_with_different_iban_is_critical() -> None:
    earlier = invoice(1, number="FT 2026/183")
    again = invoice(100, iban=NEW_IBAN, number="FT2026/183")
    result = assess(case(again, history=[earlier, *HISTORY[1:]]))
    [dup] = result.of_kind(SignalKind.DUPLICATE_DIFFERENT_IBAN)
    assert dup.severity is Severity.CRITICAL
    assert dup.owner_line == "Vodafone Portugal already sent invoice FT2026/183 with different bank details."
    assert result.owner_message is not None and result.owner_message.endswith("Payment blocked.")
    assert "I also found" in result.owner_message


def test_plain_duplicate_is_a_warning() -> None:
    earlier = invoice(1, number="FT 2026/183")
    result = assess(case(invoice(100, number="FT 2026/183"), history=[earlier, *HISTORY[1:]]))
    [dup] = result.of_kind(SignalKind.DUPLICATE_INVOICE)
    assert dup.severity is Severity.WARNING and not result.hard_stop
    assert dup.owner_line == "This looks like a copy of invoice FT 2026/183, which we already have."


def test_duplicate_without_numbers_uses_amount_and_date() -> None:
    earlier = invoice(1, number=None).model_copy(update={"invoice_number": None, "issue_date": date(2026, 9, 1)})
    now = invoice(100, iban=NEW_IBAN).model_copy(update={"invoice_number": None, "issue_date": date(2026, 9, 1),
                                                          "gross_amount": earlier.gross_amount})  # fmt: skip
    result = assess(case(now, history=[earlier]))
    assert result.of_kind(SignalKind.DUPLICATE_DIFFERENT_IBAN)[0].owner_line == (
        "Vodafone Portugal already sent this invoice with different bank details."
    )


# --------------------------------------------------------------------------- country and recipient


def test_unusual_iban_country() -> None:
    result = assess(case(invoice(100, iban=LT_IBAN)))
    [country] = result.of_kind(SignalKind.UNUSUAL_COUNTRY)
    assert country.owner_line == "This bank account is in Lithuania. Vodafone Portugal is normally paid in Portugal."
    assert result.of_kind(SignalKind.CHANGED_IBAN)


def test_unusual_supplier_tax_country() -> None:
    result = assess(case(invoice(100, supplier_tax_id="LT100001234567")))
    [country] = result.of_kind(SignalKind.UNUSUAL_COUNTRY)
    assert country.owner_line == "The invoice shows a tax number from Lithuania, not Portugal."


def test_country_needs_a_profile() -> None:
    bare = VODAFONE.model_copy(update={"countries": [], "known_ibans": []})
    result = assess(case(invoice(100, iban=LT_IBAN), supplier=bare))
    assert not result.of_kind(SignalKind.UNUSUAL_COUNTRY)


def test_invoice_recipient_mismatch() -> None:
    result = assess(case(invoice(100, customer_tax_id="PT 999 999 990")))
    [signal] = result.of_kind(SignalKind.RECIPIENT_MISMATCH)
    assert signal.severity is Severity.HIGH
    assert signal.owner_line == "This invoice is addressed to another company (tax number PT 999 999 990)."
    # Invoices without a customer tax number (simplified invoices) are not flagged.
    assert not assess(case(invoice(100, customer_tax_id=None))).of_kind(SignalKind.RECIPIENT_MISMATCH)


# --------------------------------------------------------------------------- wording and tampering


@pytest.mark.parametrize(
    ("text", "severity", "line"),
    [
        ("Informamos os novos dados bancários.", Severity.CRITICAL, "The email asks you to pay into new bank details."),
        ("Urgent and confidential: pay today.", Severity.HIGH, "The email pushes for an urgent, confidential payment."),
        ("Pagamento urgente, por favor.", Severity.WARNING, "The email pushes for urgent payment."),
        ("This matter is confidential.", Severity.WARNING, "The email asks to keep the payment confidential."),
    ],
)
def test_suspicious_language(text: str, severity: Severity, line: str) -> None:
    result = assess(case(message_text=text))
    [signal] = result.of_kind(SignalKind.SUSPICIOUS_INSTRUCTIONS)
    assert signal.severity is severity and signal.owner_line == line
    assert result.hard_stop == (severity in (Severity.HIGH, Severity.CRITICAL))


@dataclass(frozen=True)
class TamperLike:  # the verification layer's shape: enum-like kind and strength, plain detail
    kind: str
    strength: str
    detail: str


def test_altered_document_signals() -> None:
    strong = assess(case(altered=[AlteredDocumentHint("qr_mismatch", "strong", "QR total differs")]))
    assert strong.of_kind(SignalKind.ALTERED_DOCUMENT)[0].severity is Severity.CRITICAL
    one_weak = assess(case(altered=[TamperLike("editing_software", "weak", "Saved by an editor")]))
    assert one_weak.of_kind(SignalKind.ALTERED_DOCUMENT)[0].severity is Severity.WARNING and not one_weak.hard_stop
    two_weak = assess(case(altered=[TamperLike("editing_software", "WEAK", ""), TamperLike("edited_after_issue", "weak", "")]))
    assert two_weak.hard_stop
    assert two_weak.owner_message == "This document shows signs of being edited. Payment blocked."


# --------------------------------------------------------------------------- aggregation


def test_many_signals_summarized_calmly_most_severe_first() -> None:
    result = assess(case(invoice(100, iban=LT_IBAN, amount="1240", customer_tax_id="999999990"),
                         sender="x@vodafone-pt.com", message_text="New bank details. Urgent."))  # fmt: skip
    severities = [s.severity for s in result.signals]
    order = [Severity.CRITICAL, Severity.HIGH, Severity.WARNING, Severity.INFO]
    assert severities == sorted(severities, key=order.index)
    assert result.signals[0].kind is SignalKind.CHANGED_IBAN
    hard = [s for s in result.signals if s.hard_stop]
    assert result.owner_message == (
        f"Vodafone Portugal changed the IBAN shown on its invoice. I also found {len(hard) - 1} other warning signs. "
        "Payment blocked."
    )
    assert_plain(result)


def test_assessment_consistency_is_enforced() -> None:
    signal = FraudSignal(kind=SignalKind.CHANGED_IBAN, severity=Severity.CRITICAL, owner_line="x")
    with pytest.raises(ValidationError):
        FraudAssessment(hard_stop=False, signals=(signal,), owner_message=None)
    with pytest.raises(ValidationError):
        FraudAssessment(hard_stop=True, signals=(signal,), owner_message=None)


def test_unknown_supplier_uses_document_name() -> None:
    result = assess(FraudCase(entities=[HAZEL], document=invoice(100, iban=NEW_IBAN)))
    assert result.of_kind(SignalKind.NEW_PAYMENT_RECIPIENT)[0].owner_line.startswith("Vodafone Portugal asks")
