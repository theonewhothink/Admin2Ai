"""ATCUD and fiscal document numbers (§50)."""

from datetime import date

import pytest

from backoffice.countries.pt import (
    ATCUD_MANDATORY_FROM,
    ATCUDError,
    SeriesRegistration,
    is_valid_atcud,
    link_atcud,
    parse_atcud,
    parse_document_number,
)


def test_parse_atcud_keeps_printed_sequence():
    atcud = parse_atcud("CSDF7T5H-0035")
    assert atcud.validation_code == "CSDF7T5H"
    assert atcud.sequence == 35
    assert atcud.sequence_text == "0035"
    assert str(atcud) == "CSDF7T5H-0035"


@pytest.mark.parametrize("raw", ["ATCUD:CSDF7T5H-35", "ATCUD: CSDF7T5H-35", "  CSDF7T5H-35 "])
def test_parse_atcud_accepts_label_and_spaces(raw):
    assert parse_atcud(raw).sequence == 35


def test_longer_validation_codes_are_accepted():
    assert parse_atcud("ABCDEFGH23-7").validation_code == "ABCDEFGH23"


@pytest.mark.parametrize(
    "raw",
    ["ABC1234-1",  # 7-character code
     "CSDF7T5H35",  # no hyphen
     "csdf7t5h-35",  # lower case
     "CSDF7T5H-",  # no sequence
     "CSDF7T5H-3A",  # non-numeric sequence
     "CSDF-7T5H-35",
     "0",  # the "not applicable" placeholder is not an ATCUD
     "A" * 70 + "-1",  # longer than QR field H allows
     "",
     ],
)
def test_malformed_atcud_is_rejected(raw):
    with pytest.raises(ATCUDError):
        parse_atcud(raw)
    assert not is_valid_atcud(raw)


def test_non_text_atcud():
    with pytest.raises(ATCUDError):
        parse_atcud(None)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("raw", "prefix", "series", "number"),
    [("FT 2026/183", "FT", "2026", 183), ("FR A/123", "FR", "A", 123),
     ("FT AB2019/0035", "FT", "AB2019", 35), ("PA_2022 2022/00002", "PA_2022", "2022", 2)],
)
def test_document_numbers(raw, prefix, series, number):
    doc = parse_document_number(raw)
    assert (doc.prefix, doc.series, doc.number) == (prefix, series, number)
    assert str(doc) == raw


@pytest.mark.parametrize("raw", ["FT2026/183", "FT 2026-183", "FT 2026/", "FT 20 26/1", "2026/183", ""])
def test_malformed_document_numbers(raw):
    with pytest.raises(ATCUDError):
        parse_document_number(raw)


def test_link_matches_sequence_to_document_number():
    link = link_atcud(parse_atcud("CSDF7T5H-0035"), parse_document_number("FT AB2019/35"))
    assert link.ok and link.series is None


def test_link_flags_sequence_mismatch():
    link = link_atcud(parse_atcud("CSDF7T5H-36"), parse_document_number("FT AB2019/35"))
    assert not link.ok
    assert "does not match" in link.problems[0]


def test_link_against_registered_series():
    registry = [SeriesRegistration("CSDF7T5H", "2026", "FT", first_number=10)]
    atcud = parse_atcud("CSDF7T5H-12")
    ok = link_atcud(atcud, parse_document_number("FT 2026/12"), registry, doc_type="FT")
    assert ok.ok and ok.series is registry[0]

    wrong_series = link_atcud(atcud, parse_document_number("FT 2025/12"), registry)
    assert any("series" in p for p in wrong_series.problems)

    wrong_type = link_atcud(atcud, parse_document_number("FR 2026/12"), registry, doc_type="FR")
    assert any("document type" in p for p in wrong_type.problems)

    below = link_atcud(parse_atcud("CSDF7T5H-3"), parse_document_number("FT 2026/3"), registry)
    assert any("first number" in p for p in below.problems)


def test_link_unknown_code_only_matters_with_a_registry():
    atcud = parse_atcud("ZZZZ9999-5")
    doc = parse_document_number("FT 2026/5")
    assert link_atcud(atcud, doc).ok
    mapping = {"CSDF7T5H": SeriesRegistration("CSDF7T5H", "2026", "FT")}
    assert not link_atcud(atcud, doc, mapping).ok


def test_atcud_mandatory_date():
    assert date(2023, 1, 1) == ATCUD_MANDATORY_FROM
