"""Altered-document signals (§26): reported for the fraud engine, never decided here."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from backoffice.domain.models import CriticalField as F
from backoffice.domain.models import ExtractionMethod as M
from backoffice.domain.models import FieldObservation
from backoffice.verification import (
    PdfMetadata,
    SignalStrength,
    TamperKind,
    detect_tampering,
    metadata_signals,
    observation_signals,
    parse_pdf_date,
)

ISSUED = date(2026, 9, 18)
UTC = timezone.utc


def see(value, method=M.OCR, source="ev_1@pp-ocrv6", confidence=0.6, location=None):
    return FieldObservation(
        value=value, source=source, method=method, confidence=confidence, location=location
    )


def text(value, confidence=0.6):
    return see(value, M.EMBEDDED_TEXT, "ev_1", confidence)


def qr(value, confidence=0.98):
    return see(value, M.QR, "ev_1", confidence)


def kinds(signals):
    return [(s.kind, s.strength) for s in signals]


# --------------------------------------------------------------------------- PDF dates


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("D:20260918143000+01'00'", datetime(2026, 9, 18, 14, 30, tzinfo=timezone(timedelta(hours=1)))),
        ("D:20260918143000+01'00", datetime(2026, 9, 18, 14, 30, tzinfo=timezone(timedelta(hours=1)))),
        (
            "D:20260918143000-03'30'",
            datetime(2026, 9, 18, 14, 30, tzinfo=timezone(-timedelta(hours=3, minutes=30))),
        ),
        ("D:20260918143000Z", datetime(2026, 9, 18, 14, 30, tzinfo=UTC)),
        ("D:20260918143000Z00'00'", datetime(2026, 9, 18, 14, 30, tzinfo=UTC)),
        ("D:20260918", datetime(2026, 9, 18, tzinfo=UTC)),
        ("20260918143000", datetime(2026, 9, 18, 14, 30, tzinfo=UTC)),  # no offset: read as UTC
        ("2026-09-18T14:30:00+01:00", datetime(2026, 9, 18, 14, 30, tzinfo=timezone(timedelta(hours=1)))),
        ("2026-09-18T14:30:00Z", datetime(2026, 9, 18, 14, 30, tzinfo=UTC)),
        (datetime(2026, 9, 18, 14, 30), datetime(2026, 9, 18, 14, 30, tzinfo=UTC)),
    ],
)
def test_parse_pdf_date(raw, expected):
    parsed = parse_pdf_date(raw)
    assert parsed == expected and parsed.utcoffset() is not None


@pytest.mark.parametrize("raw", [None, "", "yesterday", "D:20261318", 20260918])
def test_unparseable_pdf_dates(raw):
    assert parse_pdf_date(raw) is None


# --------------------------------------------------------------------------- metadata


def test_clean_metadata_gives_no_signal():
    meta = {"/Producer": "PHC CS", "/CreationDate": "D:20260918100000Z", "/ModDate": "D:20260918100001Z"}
    assert metadata_signals(meta, ISSUED) == ()


def test_portal_rendering_later_is_not_an_edit():
    # Downloaded weeks later: created and modified together on download day.
    meta = {"CreationDate": "D:20261015090000Z", "ModDate": "D:20261015090002Z"}
    assert metadata_signals(meta, ISSUED) == ()


def test_changed_after_the_invoice_date():
    meta = {"CreationDate": "D:20260918100000Z", "ModDate": "D:20260925160000Z", "Producer": "Moloni"}
    (signal,) = metadata_signals(meta, ISSUED)
    assert (signal.kind, signal.strength) == (TamperKind.EDITED_AFTER_ISSUE, SignalStrength.WEAK)
    assert (
        signal.detail
        == "The file was changed on 25 September 2026, after the document date of 18 September 2026."
    )


def test_changed_after_issue_with_an_editing_tool_is_strong():
    meta = {"CreationDate": "D:20260918100000Z", "ModDate": "D:20260925160000Z", "Producer": "iLovePDF"}
    assert kinds(metadata_signals(meta, ISSUED)) == [
        (TamperKind.EDITING_SOFTWARE, SignalStrength.WEAK),
        (TamperKind.EDITED_AFTER_ISSUE, SignalStrength.STRONG),
    ]


def test_grace_period_and_unknown_creation():
    next_day = {"CreationDate": "D:20260918100000Z", "ModDate": "D:20260919235900Z"}
    assert metadata_signals(next_day, ISSUED) == ()  # within one day of grace
    no_creation = {"ModDate": "D:20260930120000Z", "Creator": "Adobe Photoshop 25.0"}
    assert kinds(metadata_signals(no_creation, ISSUED)) == [
        (TamperKind.EDITING_SOFTWARE, SignalStrength.WEAK),
        (TamperKind.EDITED_AFTER_ISSUE, SignalStrength.WEAK),  # can't tell an edit from a late render
    ]
    assert metadata_signals(no_creation) == metadata_signals(no_creation)[:1]  # no issue date: no date signal


def test_dates_that_contradict_each_other():
    meta = PdfMetadata(
        created_at=datetime(2026, 9, 18, 12, tzinfo=UTC), modified_at=datetime(2026, 9, 1, tzinfo=UTC)
    )
    assert kinds(metadata_signals(meta)) == [(TamperKind.METADATA_INCONSISTENT, SignalStrength.WEAK)]


def test_metadata_mapping_is_forgiving_about_keys():
    meta = PdfMetadata.from_mapping({"/producer": "  Sejda PDF  ", "MODDATE": "D:20260918Z", "creator": ""})
    assert meta.producer == "Sejda PDF" and meta.creator is None and meta.editing_tool == "Sejda PDF"
    assert meta.modified_at == datetime(2026, 9, 18, tzinfo=UTC)


# --------------------------------------------------------------------------- readings


def test_hidden_text_layer_disagrees_with_the_visible_page():
    second_engine = see("438.60", M.VLM, "ev_1@paddleocr-vl")
    signals = observation_signals(
        {F.GROSS_AMOUNT: [text("483,60"), see("438,60"), second_engine]}, currency="EUR"
    )
    (signal,) = signals
    assert (signal.kind, signal.strength) == (TamperKind.TEXT_LAYER_MISMATCH, SignalStrength.STRONG)
    assert signal.detail == (
        "For the total, the file's hidden text says €483.60, but the visible page shows €438.60."
    )
    assert signal.field == "gross_amount" and len(signal.observations) == 3


def test_one_engine_misreading_is_only_weak():
    (signal,) = observation_signals({F.GROSS_AMOUNT: [text("483,60"), see("438,60")]})
    assert (signal.kind, signal.strength) == (TamperKind.TEXT_LAYER_MISMATCH, SignalStrength.WEAK)
    # the same engine twice does not corroborate itself
    twice = [text("483,60"), see("438,60"), see("438,60", source="ev_2@pp-ocrv6")]
    assert observation_signals({F.GROSS_AMOUNT: twice})[0].strength is SignalStrength.WEAK


def test_unclear_readings_only_give_weak_signals():
    (signal,) = observation_signals({F.VAT_AMOUNT: [text("90,43", confidence=0.1), qr(D("98.43"))]})
    assert signal.kind is TamperKind.QR_MISMATCH and signal.strength is SignalStrength.WEAK


def test_text_layer_is_only_compared_on_amounts():
    assert observation_signals({F.INVOICE_NUMBER: [text("FT 2026/183"), see("FT 2026/188")]}) == ()


def test_qr_code_disagrees_with_the_printed_document():
    signals = observation_signals({F.SUPPLIER_TAX_ID: [qr("503504564"), text("PT 503 504 565")]})
    (signal,) = signals
    assert (signal.kind, signal.strength) == (TamperKind.QR_MISMATCH, SignalStrength.STRONG)
    assert signal.detail == (
        "For the supplier's VAT number, the QR code says 503504564, but the printed document shows 503504565."
    )
    scanned_only = observation_signals({F.SUPPLIER_TAX_ID: [qr("503504564"), see("PT 503 504 565")]})
    assert scanned_only[0].strength is SignalStrength.WEAK


def test_agreement_and_compatible_ambiguity_are_quiet():
    observations = {
        F.GROSS_AMOUNT: [qr(D("483.60")), text("483,60"), see("€ 483.60")],
        F.ISSUE_DATE: [qr("20260403"), see("03/04/2026")],  # could be 3 April: compatible
    }
    assert observation_signals(observations) == ()


def test_qr_code_that_contradicts_its_own_parts_is_weak():
    observations = {
        F.GROSS_AMOUNT: [qr(D("438.60"), 0.40)],
        F.NET_AMOUNT: [qr(D("393.17"), 0.40)],
        F.VAT_AMOUNT: [qr(D("90.43"), 0.40)],
    }
    signals = observation_signals(observations)
    assert kinds(signals) == [(TamperKind.QR_INCONSISTENT, SignalStrength.WEAK)]
    assert "483.60" in signals[0].detail and "438.60" in signals[0].detail


def test_country_pack_qr_arithmetic_is_recognised():
    pack_sum = see(D("483.60"), M.ARITHMETIC, "ev_1", 0.95, location="qr:arithmetic(net+vat+M)")
    signals = observation_signals({"gross_amount": [qr(D("438.60"), 0.40), pack_sum]})
    assert kinds(signals) == [(TamperKind.QR_INCONSISTENT, SignalStrength.WEAK)]


def test_detect_tampering_combines_everything():
    signals = detect_tampering(
        metadata={
            "CreationDate": "D:20260918100000Z",
            "ModDate": "D:20260930100000Z",
            "Producer": "PDFescape",
        },
        issue_date=ISSUED,
        observations_by_field={F.GROSS_AMOUNT: [qr(D("438.60")), text("483,60"), see("483,60")]},
    )
    assert {s.kind for s in signals} == {
        TamperKind.EDITING_SOFTWARE,
        TamperKind.EDITED_AFTER_ISSUE,
        TamperKind.QR_MISMATCH,
    }
    assert detect_tampering() == ()
