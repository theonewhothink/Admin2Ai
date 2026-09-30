"""Redaction before external AI (§53)."""

from __future__ import annotations

import re

import pytest

from backoffice.policy import (
    PiiKind as K,
    TokenVault,
    find_pii,
    iban_is_valid,
    is_clean,
    luhn_is_valid,
    nib_is_valid,
    redact,
)

PT_IBAN = "PT50 0002 0123 1234 5678 9015 4"
GB_IBAN = "GB82 WEST 1234 5698 7654 32"
DE_IBAN = "DE89 3704 0044 0532 0130 00"
VISA = "4111 1111 1111 1111"


def kinds_and_text(text: str) -> list[tuple[K, str]]:
    return [(m.kind, m.text) for m in find_pii(text)]


# --------------------------------------------------------------------------- checksums


@pytest.mark.parametrize("iban", [PT_IBAN, GB_IBAN, DE_IBAN, DE_IBAN.replace(" ", "")])
def test_known_ibans_are_valid(iban):
    assert iban_is_valid(iban)


@pytest.mark.parametrize(
    "iban", ["GB82 WEST 1234 5698 7654 33", "PT50", "XX00 ÄÖÜ1 2345 6789 01", ""]
)
def test_bad_ibans_are_invalid(iban):
    assert not iban_is_valid(iban)


def test_luhn():
    assert luhn_is_valid("4111111111111111")
    assert luhn_is_valid("5555555555554444")
    assert luhn_is_valid("378282246310005")
    assert not luhn_is_valid("4111111111111112")
    assert not luhn_is_valid("4111-1111")


# --------------------------------------------------------------------------- IBAN


@pytest.mark.parametrize("iban", [PT_IBAN, GB_IBAN, DE_IBAN, PT_IBAN.replace(" ", "")])
def test_iban_is_redacted_and_trailing_words_are_kept(iban):
    r = redact(f"Please pay to {iban} EUR by Friday.")
    assert r.text == "Please pay to [IBAN_1] EUR by Friday."
    assert r.matches[0].text == iban


def test_lowercase_iban_is_redacted():
    assert redact("iban: " + PT_IBAN.lower() + " ok").text == "iban: [IBAN_1] ok"


@pytest.mark.parametrize(
    "tail", ["EUR", "PARA", "para pagamento", "Obrigado", "A1 B2", "TOTAL 12"]
)
def test_iban_never_swallows_following_words(tail):
    for iban in (PT_IBAN, GB_IBAN, DE_IBAN):
        assert redact(f"{iban} {tail}").text == f"[IBAN_1] {tail}"


@pytest.mark.parametrize(
    "text",
    [
        "VAT GB123456789 NIF 509123456",
        "VAT GB123456789 SW1A 2AA FT 2026/183",
        "VAT GB123456789 qty 3 more",
        "VAT GB123456789 221B Baker Street",
    ],
)
def test_iban_fallback_never_joins_unrelated_words(text):
    assert K.IBAN not in {k for k, _ in kinds_and_text(text)}


def test_adjacent_ibans_are_both_found():
    text = f"{GB_IBAN} {PT_IBAN.lower()}"
    assert kinds_and_text(text) == [(K.IBAN, GB_IBAN), (K.IBAN, PT_IBAN.lower())]


@pytest.mark.parametrize("tail", ["TAKK", "FT 2026/183", "OK 12"])
def test_iban_from_a_country_outside_the_length_table_uses_the_checksum(tail):
    no_iban = "NO93 8601 1117 947"
    assert iban_is_valid(no_iban)
    assert redact(f"Konto {no_iban} {tail}").text == f"Konto [IBAN_1] {tail}"


def test_lowercase_iban_shaped_text_with_bad_checksum_is_kept():
    assert kinds_and_text("pt50 0002 0123 1234 5678 9015 8") == []


def test_iban_with_ocr_error_is_still_redacted_by_registry_length():
    damaged = "PT50 0002 0123 1234 5678 9015 8"  # checksum fails, length is PT's 25
    assert not iban_is_valid(damaged)
    assert redact(f"IBAN {damaged}").text == "IBAN [IBAN_1]"


def test_iban_shaped_codes_that_are_not_ibans_are_left_alone():
    for text in (
        "Ref FT2026A1234567890",
        "NIF PT509123456",
        "VAT GB123456789",
        "SKU ZZ12ABCDEFGHIJKLM",
    ):
        assert kinds_and_text(text) == [], text


# --------------------------------------------------------------------------- domestic accounts

PT_NIB = PT_IBAN.replace(" ", "")[4:]  # the 21 digits after "PT50"


def test_nib_checksum():
    assert nib_is_valid(PT_NIB)
    assert not nib_is_valid(PT_NIB[:-1] + "5")
    assert not nib_is_valid(PT_NIB[:-1])


@pytest.mark.parametrize(
    "nib",
    [
        PT_NIB,
        "0002 0123 1234 5678 9015 4",
        "0002.0123.12345678901.54",
        "0002 0123 12345678901 54",
    ],
)
def test_bare_nib_is_redacted(nib):
    assert redact(f"NIB {nib} (Banco)").text == "NIB [ACCOUNT_1] (Banco)"


def test_nib_after_other_numbers_is_still_found():
    assert kinds_and_text(f"Ref 12 {PT_NIB}") == [(K.BANK_ACCOUNT, PT_NIB)]


def test_21_digits_passing_the_nib_check_in_a_non_nib_grouping_are_kept():
    grouped = "1111 1111 1111 912 345 678"
    assert nib_is_valid(grouped.replace(" ", ""))  # a 1-in-97 coincidence
    assert (K.BANK_ACCOUNT, grouped) not in kinds_and_text(grouped)


def test_21_digit_numbers_failing_the_nib_check_are_kept():
    assert kinds_and_text("Ref " + PT_NIB[:-1] + "5") == []


def test_iban_takes_precedence_over_the_nib_inside_it():
    assert kinds_and_text(PT_IBAN) == [(K.IBAN, PT_IBAN)]


def test_uk_sort_code_and_account_number_are_redacted():
    r = redact("Sort code: 12-34-56, Account number: 12345678. A/C 87654321")
    assert (
        r.text == "Sort code: [ACCOUNT_1], Account number: [ACCOUNT_2]. A/C [ACCOUNT_3]"
    )


def test_eight_digit_numbers_without_an_account_label_are_kept():
    assert kinds_and_text("Order 12345678") == []


# --------------------------------------------------------------------------- cards


@pytest.mark.parametrize(
    "card", [VISA, "4111-1111-1111-1111", "4111111111111111", "3782 822463 10005"]
)
def test_luhn_valid_cards_are_redacted(card):
    assert redact(f"Paid with {card} today").text == "Paid with [CARD_1] today"


@pytest.mark.parametrize(
    "number", ["4111 111 1111 11111", "41 11 11 11 11 11 11 11", "411111 1111111111"]
)
def test_luhn_valid_digits_in_non_card_groupings_are_kept(number):
    assert kinds_and_text(f"Ref {number}") == []


def test_luhn_invalid_long_numbers_are_kept():
    assert kinds_and_text("Reference 4111 1111 1111 1112") == []


def test_card_is_isolated_from_neighbouring_numbers():
    text = f"Order 12 {VISA} 2027"
    assert kinds_and_text(text) == [(K.CARD, VISA)]


def test_many_cards_in_one_line_are_all_found():
    cards = [
        VISA,
        "5555 5555 5555 4444",
        VISA,
        "3782 822463 10005",
        "5555 5555 5555 4444",
    ]
    r = redact(" ".join(cards))
    assert r.text == "[CARD_1] [CARD_2] [CARD_1] [CARD_3] [CARD_2]"


def test_masked_card_endings_are_not_sensitive():
    assert kinds_and_text("Card •••• 4817") == []


# --------------------------------------------------------------------------- email


def test_emails_are_redacted():
    r = redact("Write to joao.silva+faturas@example.co.pt or ANA@Example.com.")
    assert r.text == "Write to [EMAIL_1] or [EMAIL_2]."


def test_same_email_in_different_case_gets_one_token():
    assert redact("a@b.pt and A@B.PT").text == "[EMAIL_1] and [EMAIL_1]"


# --------------------------------------------------------------------------- phones


@pytest.mark.parametrize(
    "text, phone",
    [
        ("call +351 912 345 678.", "+351 912 345 678"),
        ("call +44 20 7946 0958", "+44 20 7946 0958"),
        ("via 00351 912 345 678", "00351 912 345 678"),
        ("Tel: 213456789", "213456789"),
        ("Telemóvel 912345678", "912345678"),
        ("ligue 912 345 678", "912 345 678"),
        ("ligue 21 345 67 89", "21 345 67 89"),
        ("office 020 7946 0958", "020 7946 0958"),
        ("mobile 07700 900123", "07700 900123"),
        ("US office (555) 123-4567", "(555) 123-4567"),
    ],
)
def test_phone_numbers_are_found(text, phone):
    assert kinds_and_text(text) == [(K.PHONE, phone)]


@pytest.mark.parametrize(
    "text",
    [
        "NIF: 509 123 456",
        "NIF 234567890",
        "Contribuinte n.º 234 567 890",
        "VAT PT 509 123 456",
        "Invoice 2026-09-18 14:42 amount 1.492,30",
        "FT 2026/183 of 18.09.2026",
        "Reference 912345678",
        "Total 1 492 300.00 EUR",
        "Order 0012345678",
        "Date 01-09-2026 10:30",
        "Code +0123 4567 890",
        "Ref 0002 0123 1234 5678",
        "Contact 2026-09-18",
        "Tel. 18.09.2026",
    ],
)
def test_tax_ids_dates_amounts_and_references_are_not_phones(text):
    assert [k for k, _ in kinds_and_text(text)] == []


def test_plus_and_00_prefixes_share_a_token():
    r = redact("+351 912 345 678 or 00351 912 345 678")
    assert r.text == "[PHONE_1] or [PHONE_1]"


@pytest.mark.parametrize("word", ["or", "ou", "to", "at"])
def test_lowercase_words_before_a_number_are_not_country_prefixes(word):
    assert kinds_and_text(f"x {word} 912 345 678") == [(K.PHONE, "912 345 678")]


# --------------------------------------------------------------------------- addresses


@pytest.mark.parametrize(
    "text, address",
    [
        ("Rua Augusta, 123, 2º Esq", "Rua Augusta, 123, 2º Esq"),
        (
            "Morada: Avenida da Liberdade 245, 4º Dto",
            "Avenida da Liberdade 245, 4º Dto",
        ),
        ("Av. 25 de Abril, n.º 12", "Av. 25 de Abril, n.º 12"),
        ("Travessa do Carmo 3", "Travessa do Carmo 3"),
        ("1100-048 Lisboa", "1100-048 Lisboa"),
        ("4490-123 Póvoa de Varzim", "4490-123 Póvoa de Varzim"),
        ("1250-143 LISBOA", "1250-143 LISBOA"),
        ("Código Postal: 1100-048", "1100-048"),
        ("221B Baker Street", "221B Baker Street"),
        ("10 Downing Street", "10 Downing Street"),
        ("London SW1A 2AA", "SW1A 2AA"),
        ("Manchester M1 1AE", "M1 1AE"),
        ("New York, NY 10001", "NY 10001"),
        ("Vodafone New York, NY 10001-1234", "NY 10001-1234"),
    ],
)
def test_addresses_are_found(text, address):
    assert (K.ADDRESS, address) in kinds_and_text(text)


@pytest.mark.parametrize(
    "text",
    [
        "FT 2026-183 Lisboa",
        "ref 2026-183 Total",
        "Invoice 2026-183 IVA 23%",
        "Lisboa, quinta-feira 12 de setembro",
        "Total 1.492,30 EUR",
        "Orçamento OR 12345",
        "Version CA 90210",
    ],
)
def test_document_numbers_are_not_addresses(text):
    assert kinds_and_text(text) == []


def test_lowercase_words_are_not_document_series():
    assert kinds_and_text("send it here or 1100-048 Lisboa") == [
        (K.ADDRESS, "1100-048 Lisboa")
    ]


@pytest.mark.parametrize("after", ["Vodafone", "Total 1.492,30 EUR", "NIF 509123456"])
def test_postal_locality_never_swallows_the_next_word(after):
    assert kinds_and_text(f"1100-048 Lisboa {after}") == [
        (K.ADDRESS, "1100-048 Lisboa")
    ]


def test_postal_locality_stops_before_labels():
    text = "1250-143 LISBOA NIF 509123456"
    assert kinds_and_text(text) == [(K.ADDRESS, "1250-143 LISBOA")]


# --------------------------------------------------------------------------- the whole document


INVOICE = f"""Fatura FT 2026/183 · 18.09.2026
Fornecedor: Exemplo Lda, NIF 509123456
Rua Augusta, 123, 2º Esq
1100-048 Lisboa
Tel: 213 456 789 · faturas@exemplo.pt
Cliente NIF 234567890
Total 1.492,30 EUR (IVA 23%)
Pagamento por transferência: {PT_IBAN}
NIB alternativo: 0035 0268 00038229130 61
Pago com cartão {VISA}
"""


def test_invoice_is_redacted_but_extraction_facts_survive():
    r = redact(INVOICE)
    assert is_clean(r.text)
    for fact in (
        "FT 2026/183",
        "18.09.2026",
        "NIF 509123456",
        "NIF 234567890",
        "1.492,30",
        "IVA 23%",
    ):
        assert fact in r.text, fact
    for secret in (
        PT_IBAN,
        VISA,
        "faturas@exemplo.pt",
        "213 456 789",
        "Rua Augusta",
        "1100-048",
        "00038229130",
    ):
        assert secret not in r.text, secret
    assert {m.kind for m in r.matches} == set(K)


def test_restore_reverses_redaction_exactly():
    r = redact(INVOICE)
    assert r.restore(r.text) == INVOICE


def test_restore_maps_tokens_in_an_external_answer():
    r = redact(f"Pay {PT_IBAN} and email ana@example.pt")
    answer = '{"iban": "[IBAN_1]", "contact": "[EMAIL_1]", "other": "[IBAN_9]"}'
    restored = r.restore(answer)
    assert PT_IBAN in restored and "ana@example.pt" in restored
    assert "[IBAN_9]" in restored  # unknown tokens are never invented


# --------------------------------------------------------------------------- the token vault


def test_restore_uses_the_first_spelling_of_a_shared_value():
    spaced, compact = PT_IBAN, PT_IBAN.replace(" ", "")
    r = redact(f"{spaced} = {compact}")
    assert r.text == "[IBAN_1] = [IBAN_1]"
    assert r.restore(r.text) == f"{spaced} = {spaced}"


def test_same_value_gets_same_token_across_formats():
    r = redact(f"{PT_IBAN} / {PT_IBAN.replace(' ', '')} / {GB_IBAN}")
    assert r.text == "[IBAN_1] / [IBAN_1] / [IBAN_2]"


def test_shared_vault_keeps_tokens_consistent_across_texts():
    vault = TokenVault()
    a = redact(f"from {PT_IBAN}", vault=vault)
    b = redact(f"to {GB_IBAN}, again {PT_IBAN}", vault=vault)
    assert a.text == "from [IBAN_1]"
    assert b.text == "to [IBAN_2], again [IBAN_1]"
    assert len(vault) == 2


def test_tokens_never_collide_with_text_already_present():
    text = f"Literal [IBAN_1] then {PT_IBAN}"
    r = redact(text)
    # the literal is replaced too (see the review regressions below)
    assert r.text == "Literal [IBAN_2] then [IBAN_3]"
    assert r.restore(r.text) == text
    assert r.restore("[IBAN_2]") == "[IBAN_1]" and r.restore("[IBAN_3]") == PT_IBAN


def test_vault_view_is_read_only():
    r = redact(f"x {PT_IBAN}")
    view = r.vault.originals()
    assert dict(view) == {"[IBAN_1]": PT_IBAN}
    with pytest.raises(TypeError):
        view["[IBAN_1]"] = "tampered"  # type: ignore[index]


def test_kinds_filter_limits_redaction():
    r = redact(f"{PT_IBAN} ana@example.pt", kinds={K.EMAIL})
    assert r.text == f"{PT_IBAN} [EMAIL_1]"


def test_email_digits_are_not_reported_as_phone():
    assert kinds_and_text("contact 912345678@example.pt") == [
        (K.EMAIL, "912345678@example.pt")
    ]


@pytest.mark.parametrize(
    "text, phone",
    [
        ("qty 3 - (555) 123-4567", "(555) 123-4567"),
        ("+351 912 345 678\t14:42", "+351 912 345 678"),
        ("Tel: 912 345 678, obrigado", "912 345 678"),
        ("+44 20 7946 0958 14,50 EUR", "+44 20 7946 0958"),
        ("Tel 213 456 789. 2026-09-18", "213 456 789"),
        ("Tel 213 456 789 18.09.2026", "213 456 789"),
        ("on 2026-09-18, (555) 123-4567. 14:42", "(555) 123-4567"),
        ("call 415-555-2671 today", "415-555-2671"),
        ("18.09.2026 - (555) 123-4567; order 12", "(555) 123-4567"),
    ],
)
def test_phone_is_cut_out_of_surrounding_numbers(text, phone):
    assert kinds_and_text(text) == [(K.PHONE, phone)]


@pytest.mark.parametrize(
    "text",
    [
        "Invoice INV-00123 2026-09-18",
        "FT 2026/183 912 345",
        "PT 509 123 456 3782 822463",
        "NIF 509 123 456 789 1234",
        "Ref 123 456 7890",
    ],
)
def test_phone_never_starts_inside_a_code(text):
    assert kinds_and_text(text) == []


@pytest.mark.parametrize(
    "glued",
    [
        "+44 20 7946 0958 18.09.2026 ",
        "+351 912 345 678 2026-09-18 ",
        "912 345 678 18.09.2026 ",
        "18.09.2026 5555-5555-5555-4444",
        f"+44 20 7946 0958 {VISA}",
        f"{VISA} 912 345 678",
        f"+351 912 345 678 {PT_NIB}",
    ],
)
def test_values_glued_together_are_all_redacted(glued):
    r = redact(glued)
    assert is_clean(r.text)
    assert len(r.matches) == (
        1 if re.search(r"\d{4}-\d{2}-\d{2}|\d\d\.\d\d\.", glued) else 2
    )
    for date in ("18.09.2026", "2026-09-18"):
        assert (date in r.text) == (date in glued)
    assert r.restore(r.text) == glued


def test_text_without_pii_is_unchanged():
    text = "Vodafone invoice for September, total 92.40 EUR."
    r = redact(text)
    assert r.text == text and r.matches == () and is_clean(text)


def test_sensitive_values_stay_out_of_reprs():
    r = redact(f"Pay {PT_IBAN} from ana@example.pt")
    for shown in (repr(r), repr(r.matches), str(r.matches[0])):
        assert "PT50" not in shown and "ana@" not in shown


def test_long_numeric_tables_are_handled_quickly():
    import time

    table = " ".join(str(i % 10) for i in range(20000))
    started = time.perf_counter()
    redact(table)
    assert time.perf_counter() - started < 1.5  # linear; a quadratic scan takes far longer


SECRETS = [
    PT_IBAN,
    GB_IBAN,
    DE_IBAN.replace(" ", ""),
    VISA,
    "5555-5555-5555-4444",
    "ana.costa@example.pt",
    "+351 912 345 678",
    "+44 20 7946 0958",
    "Rua Augusta, 123, 2º Esq",
    "1100-048 Lisboa",
    "221B Baker Street",
    "0035 0268 00038229130 61",
]
FACTS = [
    "FT 2026/183",
    "18.09.2026",
    "NIF 509123456",
    "Total 1.492,30 EUR",
    "IVA 23%",
    "ATCUD:JJ37MMMM-1",
    "Vodafone",
    "ref 2026-183",
    "qty 3",
]


@pytest.mark.parametrize("seed", range(40))
def test_random_documents_round_trip_and_leak_nothing(seed):
    import random

    rng = random.Random(seed)
    secrets = rng.sample(SECRETS, k=rng.randint(1, 6))
    facts = rng.sample(FACTS, k=rng.randint(1, 5))
    parts = secrets + facts
    rng.shuffle(parts)
    text = "".join(p + rng.choice([" ", "\n", ", ", " | "]) for p in parts)
    r = redact(text)
    assert r.restore(r.text) == text
    assert is_clean(r.text)
    for secret in secrets:
        assert secret not in r.text
    for fact in facts:
        assert fact in r.text


# --------------------------------------------------------------------------- review regressions

NBSP, NNBSP, THIN, ZWSP, SOFT_HYPHEN = " ", " ", " ", "​", "­"


@pytest.mark.parametrize("sep", [NBSP, NNBSP, THIN, " "], ids=["nbsp", "narrow-nbsp", "thin", "figure"])
@pytest.mark.parametrize(
    "value, kind",
    [(PT_IBAN, K.IBAN), (DE_IBAN, K.IBAN), (VISA, K.CARD), ("+351 912 345 678", K.PHONE),
     ("0002 0123 1234 5678 9015 4", K.BANK_ACCOUNT)],
)  # fmt: skip
def test_values_spaced_with_unicode_spaces_are_redacted(sep, value, kind):
    """PDF text extraction emits no-break and thin spaces; they used to hide every value."""
    spaced = value.replace(" ", sep)
    r = redact(f"Pay: {spaced} today")
    assert [(m.kind, m.text) for m in r.matches] == [(kind, spaced)]
    assert r.text == f"Pay: [{kind.value}_1] today"
    assert r.restore(r.text) == f"Pay: {spaced} today"


@pytest.mark.parametrize("dash", ["‐", "‑", "‒", "–", "−"])
def test_cards_with_unicode_hyphens_are_redacted(dash):
    card = VISA.replace(" ", dash)
    assert kinds_and_text(f"Card {card}") == [(K.CARD, card)]


@pytest.mark.parametrize("invisible", [ZWSP, SOFT_HYPHEN, "‍", "﻿"])
def test_invisible_characters_inside_values_do_not_hide_them(invisible):
    iban = PT_IBAN.replace(" ", invisible)
    card = VISA.replace(" ", " " + invisible)
    r = redact(f"IBAN {iban} card {card}.")
    assert r.text == "IBAN [IBAN_1] card [CARD_1]."
    assert r.restore(r.text) == f"IBAN {iban} card {card}."


@pytest.mark.parametrize(
    "text, street, postal",
    [
        ("Morada: Rua de São Bento 12, 1200-820 Lisboa", "Rua de São Bento 12", "1200-820 Lisboa"),
        ("Rua Augusta 5, 1100-048 Lisboa", "Rua Augusta 5", "1100-048 Lisboa"),
        ("Av. da Liberdade 245 4º, 1250-143 Lisboa", "Av. da Liberdade 245 4º", "1250-143 Lisboa"),
    ],
)
def test_street_number_never_eats_the_postal_code(text, street, postal):
    """The floor pattern used to take '12' of '1200-820', leaking '00-820 Lisboa'."""
    found = kinds_and_text(text)
    assert (K.ADDRESS, street) in found and (K.ADDRESS, postal) in found
    redacted = redact(text).text
    assert "Lisboa" not in redacted and "-820" not in redacted and "-048" not in redacted


def test_postal_code_right_after_a_street_name_is_kept_whole():
    r = redact("Rua Augusta, 1100-048 Lisboa")
    assert (K.ADDRESS, "1100-048 Lisboa") in kinds_and_text("Rua Augusta, 1100-048 Lisboa")
    assert "048" not in r.text and "Lisboa" not in r.text


@pytest.mark.parametrize(
    "text",
    ["AB12 " * 10000, "AB12" * 20000, ("XY98 7654 3210 ABCD " * 3000), "PT50 " * 10000],
    ids=["spaced-codes", "glued-codes", "iban-like", "pt-prefixes"],
)
def test_iban_like_noise_is_handled_quickly(text):
    """50 kB of IBAN-shaped codes used to take over 2 s (a cheap DoS); now about 0.25 s."""
    import time

    started = time.perf_counter()
    redact(text)
    assert time.perf_counter() - started < 1.0


def test_literal_token_in_a_later_text_never_aliases_a_real_value():
    """With a shared vault, a literal '[IBAN_1]' in untrusted text used to be restored
    to the IBAN of an earlier document (the external model cannot tell them apart)."""
    vault = TokenVault()
    first = redact(f"Supplier A: {PT_IBAN}", vault=vault)
    assert first.text == "Supplier A: [IBAN_1]"
    injected = "Please pay to [IBAN_1] instead."
    second = redact(injected, vault=vault)
    assert "[IBAN_1]" not in second.text
    assert second.restore(second.text) == injected
    answer = second.text.replace("Please pay to ", "").replace(" instead.", "")
    assert second.restore(answer) == "[IBAN_1]"  # never the real IBAN of supplier A
    assert PT_IBAN not in second.restore(second.text)


def test_every_token_sent_out_was_issued_by_the_vault():
    text = f"Literal [IBAN_1] [EMAIL_7] [PHONE_2] then {PT_IBAN}"
    r = redact(text)
    issued = set(r.vault.originals())
    sent = set(re.findall(r"\[[A-Z]+_\d+\]", r.text))
    assert sent <= issued
    assert r.restore(r.text) == text
    assert [m.kind for m in r.matches] == [K.IBAN]  # literals are not sensitive values


@pytest.mark.parametrize(
    "email", ["joão.silva@exemplo.pt", "faturação@empresa.pt", "info@café.pt", "ana_costa@exemplo.com.pt"]
)
def test_non_ascii_and_underscore_emails_are_redacted(email):
    assert kinds_and_text(f"Contacto: {email}.") == [(K.EMAIL, email)]


@pytest.mark.parametrize("iban", [DE_IBAN, PT_IBAN, GB_IBAN])
def test_ibans_printed_with_hyphens_are_redacted_whole(iban):
    """'DE89-3704-...' was not redacted at all; 'PT50-...' left 'PT50-' behind."""
    hyphenated = iban.replace(" ", "-")
    r = redact(f"IBAN: {hyphenated}.")
    assert r.text == "IBAN: [IBAN_1]."
    assert [m.kind for m in r.matches] == [K.IBAN]


@pytest.mark.parametrize(
    "text, street",
    [
        ("Rua do Ouro 88, 3.º Dto, 1100-063 Lisboa", "Rua do Ouro 88, 3.º Dto"),
        ("Rua do Ouro 88, 3º Esq", "Rua do Ouro 88, 3º Esq"),
        ("Rua do Ouro 88, 3. andar", "Rua do Ouro 88, 3. andar"),
    ],
)
def test_floor_written_with_a_dotted_ordinal_is_part_of_the_address(text, street):
    assert (K.ADDRESS, street) in kinds_and_text(text)
