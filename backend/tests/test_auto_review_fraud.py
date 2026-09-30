"""Adversarial review of the fraud engine (§26): evasions and false alarms found in review.

Each test pins a defect that was reproduced before the fix.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from backoffice.domain.models import Document, DocumentType, LegalEntity, Supplier
from backoffice.fraud import (
    DomainVerdict,
    FraudCase,
    Severity,
    SignalKind,
    assess,
    check_sender_domain,
    find_ibans,
    find_suspicious_phrases,
    normalize_iban,
)
from backoffice.fraud.phrases import PhraseCategory
from backoffice.learning.keys import fold, qualified_tax_id

D = Decimal
KNOWN = "PT50 0002 0123 1234 5678 9015 4"
NEW = "GB82 WEST 1234 5698 7654 32"
HAZEL = LegalEntity(id="ent_hazel", tenant_id="t1", name="Hazel Tree, Lda.", country="PT", tax_id="509123456")
VODAFONE = Supplier(id="sup_voda", tenant_id="t1", name="Vodafone", known_ibans=[KNOWN],
                    email_domains=["vodafone.pt"], countries=["PT"])  # fmt: skip
PLUMBER = Supplier(id="sup_joao", tenant_id="t1", name="João Canalizador", known_ibans=[KNOWN],
                   email_domains=["gmail.com"], contact_email="joao.canal@gmail.com", countries=["PT"])  # fmt: skip


def invoice(n: int, amount: str = "92.40", **kw: object) -> Document:
    base: dict[str, object] = dict(
        id=f"doc_{n}", tenant_id="t1", evidence_ids=[f"ev_{n}"], supplier_name="Vodafone",
        invoice_number=f"FT 2026/{n}", issue_date=date(2026, 1 + n % 12, 20), gross_amount=D(amount), iban=KNOWN,
    )  # fmt: skip
    base.update(kw)
    return Document(**base)  # type: ignore[arg-type]


HISTORY = [invoice(i, a) for i, a in enumerate(["92.40", "95.10", "91.00", "99.90", "92.40", "93.80"], 1)]


# --------------------------------------------------------------------------- sender: free-mail suppliers


def test_free_mail_supplier_is_known_by_address_not_domain() -> None:
    """Defect: a supplier on gmail.com made *every* gmail.com sender 'usual'."""
    stranger = check_sender_domain("Joao <scammer.joao@gmail.com>", ["gmail.com"], known_addresses=["joao.canal@gmail.com"])
    assert stranger.verdict is DomainVerdict.NEW_ADDRESS
    usual = check_sender_domain("João <Joao.Canal@Gmail.com>", ["gmail.com"], known_addresses=["joao.canal@gmail.com"])
    assert usual.verdict is DomainVerdict.KNOWN
    # A known free-mail domain without any known address proves nothing either.
    assert check_sender_domain("x@gmail.com", ["gmail.com"]).verdict is DomainVerdict.NEW_ADDRESS
    # Business domains keep working at domain level.
    assert check_sender_domain("a@mail.vodafone.pt", ["vodafone.pt"]).verdict is DomainVerdict.KNOWN


def test_engine_blocks_new_address_on_free_mail_supplier() -> None:
    result = assess(FraudCase(entities=[HAZEL], supplier=PLUMBER, sender="Joao <scammer.joao@gmail.com>"))
    [signal] = result.of_kind(SignalKind.CHANGED_EMAIL_DOMAIN)
    assert signal.severity is Severity.HIGH and result.hard_stop
    assert signal.owner_line == "The email came from a personal address João Canalizador has not used before."
    assert not result.passed
    ok = assess(FraudCase(entities=[HAZEL], supplier=PLUMBER, sender="joao.canal@gmail.com"))
    assert not ok.hard_stop and ok.passed == ("Sent from João Canalizador's usual email address.",)


def test_known_address_listed_in_email_domains_is_accepted() -> None:
    supplier = PLUMBER.model_copy(update={"email_domains": ["joao.canal@gmail.com"], "contact_email": None})
    assert not assess(FraudCase(entities=[HAZEL], supplier=supplier, sender="joao.canal@gmail.com")).hard_stop
    assert assess(FraudCase(entities=[HAZEL], supplier=supplier, sender="other@gmail.com")).hard_stop


# --------------------------------------------------------------------------- tax numbers carry a country


def test_qualified_tax_id() -> None:
    assert qualified_tax_id("509123456", "PT") == "PT509123456"
    assert qualified_tax_id("PT 509 123 456", "PT") == "PT 509 123 456"  # already prefixed: untouched
    assert qualified_tax_id("123456789", "GR") == "EL123456789"  # Greek VAT prefix is EL
    assert qualified_tax_id("12-3456789", "US") == "12-3456789"  # no VAT prefix known: untouched
    assert qualified_tax_id(None, "PT") is None


def test_recipient_with_same_digits_but_other_country_is_a_mismatch() -> None:
    """Defect: 'ES509123456' matched our Portuguese NIF 509123456 because only digits were compared."""
    doc = invoice(100, customer_tax_id="ES509123456")
    result = assess(FraudCase(entities=[HAZEL], supplier=VODAFONE, document=doc, history=HISTORY))
    assert result.of_kind(SignalKind.RECIPIENT_MISMATCH) and result.hard_stop
    for ours in ("509123456", "PT509123456", "PT 509 123 456"):
        clean = assess(FraudCase(entities=[HAZEL], supplier=VODAFONE, document=invoice(100, customer_tax_id=ours),
                                 history=HISTORY))  # fmt: skip
        assert not clean.of_kind(SignalKind.RECIPIENT_MISMATCH), ours


# --------------------------------------------------------------------------- amounts


def test_large_credit_note_is_not_an_unusual_charge() -> None:
    """Defect: a €900 credit note (money coming back) was blocked as 'This invoice is €900.00'."""
    note = invoice(100, "900.00", doc_type=DocumentType.CREDIT_NOTE, invoice_number="NC 2026/1")
    result = assess(FraudCase(entities=[HAZEL], supplier=VODAFONE, document=note, history=HISTORY))
    assert not result.of_kind(SignalKind.UNUSUAL_AMOUNT)
    assert not result.hard_stop


def test_payment_amount_is_checked_even_with_a_document() -> None:
    """Defect: with a €92.40 invoice attached, a €9,240.00 payment instruction was never judged."""
    result = assess(FraudCase(entities=[HAZEL], supplier=VODAFONE, document=invoice(100), history=HISTORY,
                              payment_amount=D("9240.00")))  # fmt: skip
    [signal] = result.of_kind(SignalKind.UNUSUAL_AMOUNT)
    assert signal.severity is Severity.HIGH and result.hard_stop
    assert signal.owner_line == "Vodafone usually charges around €93.10. This payment is €9,240.00."
    # Paying exactly the invoice total adds nothing.
    same = assess(FraudCase(entities=[HAZEL], supplier=VODAFONE, document=invoice(100), history=HISTORY,
                            payment_amount=D("-92.40")))  # fmt: skip
    assert not same.hard_stop and not same.of_kind(SignalKind.UNUSUAL_AMOUNT)


def test_currency_case_does_not_hide_history() -> None:
    doc = invoice(100, "1240.00", currency="eur")
    result = assess(FraudCase(entities=[HAZEL], supplier=VODAFONE, document=doc, history=HISTORY))
    assert result.of_kind(SignalKind.UNUSUAL_AMOUNT)


# --------------------------------------------------------------------------- hidden bank details


def test_new_iban_right_after_the_old_one_is_found() -> None:
    """Defect: the scanner swallowed the start of the second IBAN, so the new one was invisible."""
    text = f"Old: {KNOWN} new {NEW} from now on."
    assert find_ibans(text) == [normalize_iban(KNOWN), normalize_iban(NEW)]
    result = assess(FraudCase(entities=[HAZEL], supplier=VODAFONE, message_text=text))
    assert result.of_kind(SignalKind.CHANGED_IBAN) and result.hard_stop


@pytest.mark.parametrize(
    "separator",
    [" ", " ", " ", "​", "⁠", "﻿", "\n"],
)
def test_ibans_split_by_unusual_spaces_are_found(separator: str) -> None:
    """Defect: non-breaking or zero-width spaces hid an IBAN from the email check."""
    text = "Please pay " + separator.join(["GB82", "WEST", "1234", "5698", "7654", "32"]) + " today"
    assert find_ibans(text) == ["GB82WEST12345698765432"]
    assert normalize_iban(separator.join(["GB82", "WEST12345698765432"])) == "GB82WEST12345698765432"


def test_unknown_length_country_is_found_when_followed_by_words() -> None:
    # Brazil is not in the length table; the trailing word must not break detection.
    iban = "BR15 0000 0000 0000 1093 2840 814P 2"
    assert find_ibans(f"Conta {iban} obrigado") == ["BR1500000000000010932840814P2"]


def test_no_iban_invented_from_ordinary_text() -> None:
    assert find_ibans("Order AB12 3456 7890 1234 shipped. Ref PT50 missing.") == []


def test_zero_width_characters_do_not_hide_suspicious_wording() -> None:
    """Defect: 'novos dados banc<ZWSP>ários' slipped past the phrase check."""
    assert fold("banc​arios­") == "bancarios"
    hits = find_suspicious_phrases("Informamos os novos dados banc​ários.")
    assert [h.category for h in hits] == [PhraseCategory.BANK_CHANGE]


def test_recipient_line_is_one_clean_line() -> None:
    doc = invoice(100, customer_tax_id=" PT 999\n999\t990 ")
    [signal] = assess(FraudCase(entities=[HAZEL], supplier=VODAFONE, document=doc, history=HISTORY)).of_kind(
        SignalKind.RECIPIENT_MISMATCH
    )
    assert signal.owner_line == "This invoice is addressed to another company (tax number PT 999 999 990)."


@pytest.mark.parametrize(
    "text",
    [
        "Please use our nеw bаnk details from today.",  # Cyrillic e and a
        "Informamos os nοvos dados bancários.",  # Greek omicron
    ],
)
def test_lookalike_letters_do_not_hide_bank_change_wording(text: str) -> None:
    """Defect: Cyrillic/Greek letters that look Latin slipped past the wording check."""
    assert [h.category for h in find_suspicious_phrases(text)] == [PhraseCategory.BANK_CHANGE]
