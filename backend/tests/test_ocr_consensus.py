"""Field-level agreement and disagreement (§18, §19, §57)."""

from decimal import Decimal

import pytest

from backoffice.domain.models import CriticalField as F
from backoffice.domain.models import ExtractionMethod as M
from backoffice.domain.models import FieldObservation, Quality
from backoffice.ocr import (
    ConflictKind,
    ConsensusPolicy,
    FieldState,
    build_consensus,
    decide,
)


def see(value, method=M.OCR, source="ev_1@pp-ocrv6-medium", confidence=0.6):
    return FieldObservation(value=value, source=source, method=method, confidence=confidence)


PP = "ev_1@pp-ocrv6-medium"
VL = "ev_1@paddleocr-vl"
PAID = "ev_1@commercial"


def test_missing_single_and_agreed():
    assert decide(F.GROSS_AMOUNT, []).state is FieldState.MISSING
    assert decide(F.GROSS_AMOUNT, []).quality is None
    single = decide(F.GROSS_AMOUNT, [see("483,60")])
    assert single.state is FieldState.SINGLE and single.value == Decimal("483.60")
    agreed = decide(F.GROSS_AMOUNT, [see("483,60"), see(Decimal("483.6"), M.QR, "ev_1")])
    assert agreed.state is FieldState.AGREED and agreed.settled
    assert agreed.observations[0].method is M.QR  # strongest first
    assert agreed.quality is Quality.AMBER  # never GREEN here (§57)


def test_identifiers_are_reported_as_printed_but_compared_normalized():
    number = decide(F.INVOICE_NUMBER, [see("FT 2026/183", M.QR, "ev_1"), see("ft2026/183")])
    assert number.state is FieldState.AGREED and number.value == "FT 2026/183"
    tax_id = decide(F.SUPPLIER_TAX_ID, [see("PT509123457", M.STRUCTURED_XML, "ev_1"), see("509 123 457")])
    assert tax_id.state is FieldState.AGREED and tax_id.value == "PT509123457"
    iban = decide(F.IBAN, [see("pt50 0002 0123 1234 5678 9015 4")])
    assert iban.value == "PT50000201231234567890154"


def test_the_same_voter_twice_is_still_one_source():
    same = decide(F.GROSS_AMOUNT, [see("483.60"), see("483,60")])
    assert same.state is FieldState.SINGLE
    # XML and embedded text of one PDF are independent methods: two voters.
    two = decide(F.GROSS_AMOUNT, [see("1", M.STRUCTURED_XML, "ev"), see("1", M.EMBEDDED_TEXT, "ev")])
    assert two.state is FieldState.AGREED


def test_section_19_everything_agrees():
    result = decide(
        F.GROSS_AMOUNT,
        [
            see("483,60"),
            see("483.60", M.QR, "ev_1"),
            see("483.60", M.ARITHMETIC, "calc"),
            see("483.60", M.BANK, "tx_9"),
        ],
    )
    assert result.state is FieldState.AGREED and len(result.candidates[0].voters) == 4


def test_section_19_qr_disagreement_is_a_conflict_never_a_majority():
    # OCR, arithmetic and bank say 483.60; the QR says 438.60. Structured sources disagree.
    result = decide(
        F.GROSS_AMOUNT,
        [
            see("483,60"),
            see("438.60", M.QR, "ev_1"),
            see("483.60", M.ARITHMETIC, "calc"),
            see("483.60", M.BANK, "tx"),
        ],
    )
    assert result.state is FieldState.CONFLICT and result.conflict is ConflictKind.SOURCES
    assert result.quality is Quality.RED and not result.resolvable
    assert result.value is None
    assert {c.value for c in result.candidates} == {Decimal("483.60"), Decimal("438.60")}


def test_one_reading_against_an_anchor_may_still_be_a_misread():
    result = decide(F.GROSS_AMOUNT, [see("438.60", M.QR, "ev_1"), see("483.60", source=PP)])
    assert result.state is FieldState.CONFLICT and result.conflict is ConflictKind.READINGS
    assert result.resolvable  # a second reading agreeing with the QR could settle it


def test_readings_never_overrule_an_anchor():
    result = decide(
        F.GROSS_AMOUNT,
        [see("438.60", M.QR, "ev_1"), see("483.60", source=PP), see("483.60", M.VLM, VL)],
    )
    assert result.state is FieldState.CONFLICT and not result.resolvable


def test_reading_majority_confirming_the_anchor():
    result = decide(
        F.GROSS_AMOUNT,
        [see("483.60", M.QR, "ev_1"), see("488.60", source=PP), see("483.60", M.VLM, VL)],
    )
    assert result.state is FieldState.MAJORITY and result.value == Decimal("483.60")
    assert result.quality is Quality.AMBER
    dissent = [c for c in result.candidates if c.value != Decimal("483.60")]
    assert dissent and dissent[0].voters == (f"{PP}|ocr",)


def test_readings_only_majority_and_splits():
    majority = decide(
        F.GROSS_AMOUNT, [see("488.60", source=PP), see("483.60", M.VLM, VL), see("483,60", M.VLM, PAID)]
    )
    assert majority.state is FieldState.MAJORITY and majority.value == Decimal("483.60")
    split = decide(F.GROSS_AMOUNT, [see("488.60", source=PP), see("483.60", M.VLM, VL)])
    assert split.state is FieldState.CONFLICT and split.resolvable
    three_way = decide(F.GROSS_AMOUNT, [see("1", source=PP), see("2", M.VLM, VL), see("3", M.VLM, PAID)])
    assert three_way.state is FieldState.CONFLICT and not three_way.resolvable


def test_majority_can_be_switched_off():
    policy = ConsensusPolicy(allow_majority=False)
    result = decide(
        F.GROSS_AMOUNT,
        [see("488.60", source=PP), see("483.60", M.VLM, VL), see("483.60", M.VLM, PAID)],
        policy,
    )
    assert result.state is FieldState.CONFLICT and not result.resolvable
    with pytest.raises(ValueError):
        ConsensusPolicy(min_majority_votes=1)


def test_garbage_reading_disagrees_instead_of_matching():
    result = decide(F.GROSS_AMOUNT, [see("4B3,6O", source=PP), see("483.60", M.VLM, VL)])
    assert result.state is FieldState.CONFLICT
    assert "4B3,6O" in {c.value for c in result.candidates}


def test_build_consensus_required_missing_and_optional_conflicts():
    observations = {
        F.GROSS_AMOUNT: [see("483.60")],
        F.IBAN: [
            see("PT50000201231234567890154", M.STRUCTURED_XML, "ev"),
            see("GB82WEST12345698765432", M.QR, "ev"),
        ],
    }
    consensus = build_consensus(observations, required={F.GROSS_AMOUNT, F.INVOICE_NUMBER})
    assert consensus.missing == (F.INVOICE_NUMBER,)
    assert consensus.conflicts == (F.IBAN,)  # not required, still a conflict (§26)
    assert not consensus.settled and consensus.improvable
    assert set(consensus.fields) == {F.INVOICE_NUMBER, F.GROSS_AMOUNT, F.IBAN}
    only_hard = build_consensus({F.IBAN: observations[F.IBAN]}, required=())
    assert not only_hard.settled and not only_hard.improvable
    settled = build_consensus({F.GROSS_AMOUNT: [see("1")]}, required=[F.GROSS_AMOUNT])
    assert settled.settled and not settled.improvable
