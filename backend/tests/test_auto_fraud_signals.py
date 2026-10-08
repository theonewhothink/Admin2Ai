"""Fraud building blocks (§26): IBAN mod-97, sender domains, suspicious wording."""

from __future__ import annotations

import pytest

from backoffice.fraud.domains import (
    DomainVerdict,
    check_sender_domain,
    email_domain,
    registrable_domain,
    skeleton,
)
from backoffice.fraud.iban import (
    find_ibans,
    iban_country,
    is_valid_iban,
    mask_iban,
    normalize_iban,
)
from backoffice.fraud.phrases import PhraseCategory, find_suspicious_phrases

# Published example IBANs (check digits valid).
GB_IBAN = "GB82 WEST 1234 5698 7654 32"
DE_IBAN = "DE89 3704 0044 0532 0130 00"
PT_IBAN = "PT50 0002 0123 1234 5678 9015 4"

# --------------------------------------------------------------------------- IBAN


def test_normalize_iban() -> None:
    assert normalize_iban(" iban: pt50 0002.0123-1234 5678 9015 4 ") == "PT50000201231234567890154"


@pytest.mark.parametrize("iban", [GB_IBAN, DE_IBAN, PT_IBAN, "gb82west12345698765432"])
def test_valid_ibans(iban: str) -> None:
    assert is_valid_iban(iban)


@pytest.mark.parametrize(
    "iban",
    [
        "GB82 WEST 1234 5698 7654 33",  # wrong check digits
        "PT50 0002 0123 1234 5678 9015",  # too short for Portugal
        "DE89 3704 0044 0532 0130 0000",  # too long for Germany
        "1234",
        "",
        None,
        "GB82 WEST 1234 5698 7654 3!",
    ],
)
def test_invalid_ibans(iban: str | None) -> None:
    assert not is_valid_iban(iban)


def test_iban_helpers() -> None:
    assert iban_country(PT_IBAN) == "PT"
    assert mask_iban(PT_IBAN) == "PT50 •••• 0154"


def test_find_ibans_in_text() -> None:
    text = f"Please pay to {GB_IBAN} please. Old one {DE_IBAN}. Bogus GB82 WEST 1234 5698 7654 33."
    assert find_ibans(text) == ["GB82WEST12345698765432", "DE89370400440532013000"]
    assert find_ibans("") == []


# --------------------------------------------------------------------------- domains


def test_email_and_registrable_domain() -> None:
    assert email_domain("Vodafone <Faturas@Mail.Vodafone.PT>") == "mail.vodafone.pt"
    assert email_domain("not an address") is None
    assert registrable_domain("mail.vodafone.pt") == "vodafone.pt"
    assert registrable_domain("faturas.empresa.com.pt") == "empresa.com.pt"
    assert registrable_domain("billing.shop.co.uk") == "shop.co.uk"


@pytest.mark.parametrize(
    ("sender", "verdict"),
    [
        ("faturas@vodafone.pt", DomainVerdict.KNOWN),
        ("no-reply@mail.vodafone.pt", DomainVerdict.KNOWN),
        ("billing@vodafone-pt.com", DomainVerdict.LOOKALIKE),  # brand embedded
        ("billing@vodafone.pt.billing-secure.com", DomainVerdict.LOOKALIKE),
        ("billing@vodaf0ne.pt", DomainVerdict.LOOKALIKE),  # digit for letter
        ("billing@vodafome.pt", DomainVerdict.LOOKALIKE),  # one typo
        ("billing@xn--vodafne-dpf.pt", DomainVerdict.LOOKALIKE),  # Greek omicron, punycode
        ("billing@vodafone.com", DomainVerdict.CHANGED),  # same name, other ending
        ("billing@other-telecom.pt", DomainVerdict.CHANGED),
        ("vodafone.billing@gmail.com", DomainVerdict.FREE_MAIL),
    ],
)
def test_sender_domain_verdicts(sender: str, verdict: DomainVerdict) -> None:
    check = check_sender_domain(sender, ["vodafone.pt"])
    assert check.verdict is verdict
    if verdict is DomainVerdict.LOOKALIKE:
        assert check.imitated == "vodafone.pt"


def test_sender_domain_without_profile_or_sender() -> None:
    assert check_sender_domain("a@b.pt", []).verdict is DomainVerdict.UNKNOWN
    assert check_sender_domain(None, ["vodafone.pt"]).verdict is DomainVerdict.UNKNOWN


def test_short_brands_are_not_fuzzy_matched() -> None:
    # "meo" is too short for typo/embedding checks: a different domain is just "changed".
    assert check_sender_domain("x@meu.pt", ["meo.pt"]).verdict is DomainVerdict.CHANGED


def test_skeleton_folds_lookalikes() -> None:
    assert skeleton("vodafοne") == skeleton("vodafone")  # Greek omicron
    assert skeleton("rnicrosoft") == skeleton("microsoft")
    assert skeleton("paypa1") == skeleton("paypal")


# --------------------------------------------------------------------------- wording


@pytest.mark.parametrize(
    "text",
    [
        "Please note our new bank details below.",
        "Our bank account has changed, please update your payment details.",
        "Informamos que temos novos dados bancários.",
        "ALTERAÇÃO DE IBAN a partir deste mês",
        "Alteramos o nosso IBAN.",
        "Por favor não utilize a conta antiga.",
        "Please do not pay into the old account.",
        "We have changed banks.",
        "Mudámos de banco.",
        "Segue o novo NIB para pagamento.",
        "Please use our new account number.",
    ],
)
def test_bank_change_language(text: str) -> None:
    categories = {h.category for h in find_suspicious_phrases(text)}
    assert PhraseCategory.BANK_CHANGE in categories


@pytest.mark.parametrize(
    "text",
    [
        "Criámos uma nova conta para si no portal de cliente.",
        "Please find the new details of your plan attached.",
        "A alteração da conta de acesso ao portal foi concluída.",
        "We moved bank holiday opening hours.",
        "Mudou de tarifário com sucesso.",
    ],
)
def test_ordinary_wording_is_not_a_bank_change(text: str) -> None:
    assert not any(h.category is PhraseCategory.BANK_CHANGE for h in find_suspicious_phrases(text))


def test_urgency_and_secrecy_language() -> None:
    hits = find_suspicious_phrases("URGENTE: pagamento imediato. Assunto confidencial, não ligue.")
    categories = {h.category for h in hits}
    assert categories == {PhraseCategory.URGENCY, PhraseCategory.SECRECY}
    assert find_suspicious_phrases("Please find attached your monthly invoice. Thank you.") == []
    # Repeated phrases are reported once.
    assert len(find_suspicious_phrases("urgent urgent urgent")) == 1
