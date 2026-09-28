"""Portuguese NIF validation (§4 onboarding, §18 supplier/customer tax ids)."""

import pytest

from backoffice.countries.base import TaxIdKind
from backoffice.countries.pt import (
    FINAL_CONSUMER_NIF,
    entity_kind_hint,
    is_final_consumer,
    is_valid_nif,
    nif_check_digit,
    normalize_nif,
    validate_nif,
)

COMPANY = "509123457"
PERSON = "123456789"


@pytest.mark.parametrize(
    "raw",
    ["123456789", "PT 123 456 789", "pt123456789", "PT-123456789", "123.456.789",
     "123-456-789", "  123 456 789  ", "123\u00a0456\u00a0789"],
)
def test_normalize_strips_formatting(raw):
    assert normalize_nif(raw) == "123456789"


@pytest.mark.parametrize(
    "raw", ["12345678", "1234567890", "ABC", "", "NIF 123456789", "ES123456789", "12345678A",
            "١٢٣٤٥٦٧٨٩", None, 123456789],
)
def test_normalize_rejects_non_nif_shapes(raw):
    assert normalize_nif(raw) is None


def test_check_digit_mod11_including_remainder_zero_and_one():
    assert nif_check_digit("12345678") == 9
    assert nif_check_digit("10000001") == 0  # remainder 0
    assert nif_check_digit("50000000") == 0  # remainder 1
    assert nif_check_digit("50000006") == 9  # 11 - 2


@pytest.mark.parametrize("bad", ["1234567", "123456789", "1234567a"])
def test_check_digit_requires_eight_digits(bad):
    with pytest.raises(ValueError):
        nif_check_digit(bad)


@pytest.mark.parametrize(
    ("nif", "kind"),
    [
        ("123456789", TaxIdKind.PERSON),
        ("212345672", TaxIdKind.PERSON),
        ("300000014", TaxIdKind.PERSON),
        ("451234561", TaxIdKind.NON_RESIDENT),
        (COMPANY, TaxIdKind.COMPANY),
        ("600000001", TaxIdKind.PUBLIC_BODY),
        ("701234563", TaxIdKind.OTHER_ENTITY),
        ("801234565", TaxIdKind.SOLE_TRADER),
        ("901234567", TaxIdKind.OTHER_ENTITY),
        ("981234569", TaxIdKind.NON_RESIDENT),
        ("991234561", TaxIdKind.OTHER_ENTITY),
    ],
)
def test_valid_nifs_and_kind_hint(nif, kind):
    check = validate_nif(nif)
    assert check.valid and check.normalized == nif and check.problem is None
    assert check.kind == kind
    assert check.category
    assert entity_kind_hint(nif) == kind


def test_person_vs_company_hint():
    assert entity_kind_hint("PT " + PERSON).is_individual
    assert not entity_kind_hint(COMPANY).is_individual
    assert entity_kind_hint("not a nif") == TaxIdKind.UNKNOWN


@pytest.mark.parametrize("nif", ["401234568", "731234561", "001234560"])
def test_unassigned_prefixes_are_rejected_even_with_correct_check_digit(nif):
    check = validate_nif(nif)
    assert not check.valid and check.problem == "prefix"


def test_wrong_check_digit():
    check = validate_nif("123456780")
    assert not check.valid
    assert check.problem == "check_digit"
    assert check.normalized == "123456780"


@pytest.mark.parametrize(
    ("raw", "problem"),
    [("", "empty"), ("   ", "empty"), ("12345678", "length"), ("PT 1234 5678 90", "length"),
     ("12345678X", "characters")],
)
def test_shape_problems(raw, problem):
    check = validate_nif(raw)
    assert not check.valid and check.problem == problem


def test_owner_messages_are_plain_and_short():
    for raw in ["", "12345678", "123456780", "401234568", FINAL_CONSUMER_NIF, "12345678X"]:
        message = validate_nif(raw).message
        assert message and len(message) < 90
        assert "mod" not in message.lower() and "error" not in message.lower()
    assert validate_nif("12345678").message == "That NIF has 8 digits. It needs 9."
    assert validate_nif(PERSON).message == ""


def test_final_consumer_placeholder():
    assert is_valid_nif(FINAL_CONSUMER_NIF) is False
    check = validate_nif(FINAL_CONSUMER_NIF)
    assert check.problem == "placeholder" and check.kind == TaxIdKind.PLACEHOLDER
    allowed = validate_nif(FINAL_CONSUMER_NIF, allow_placeholder=True)
    assert allowed.valid and allowed.kind == TaxIdKind.PLACEHOLDER
    assert is_final_consumer("PT 999 999 990")
    assert entity_kind_hint(FINAL_CONSUMER_NIF) == TaxIdKind.PLACEHOLDER


def test_every_generated_nif_validates_and_any_other_last_digit_fails():
    for prefix in ["1", "2", "3", "45", "5", "6", "70", "71", "72", "74", "75", "77", "78",
                   "79", "8", "90", "91", "98", "99"]:
        first8 = (prefix + "2468013579")[:8]
        good = first8 + str(nif_check_digit(first8))
        assert is_valid_nif(good), good
        for digit in "0123456789":
            if digit != good[8]:
                assert not is_valid_nif(first8 + digit)
