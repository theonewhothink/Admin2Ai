"""Invoice tables read from photos and scans by where each word sits (§13, §18, QA E8).

The local engine (PP-OCRv6 through RapidOCR) returns a box for every run of text. The reader keeps them with
each printed line (``ReadOutcome.word_rows``, the photo's tilt undone), and the table's lines are read by
column: the heading row names the columns ("Código Descrição Qtd. Un. Preço unit. Desc. IVA Total"), every
word below goes under its heading, numbers are read in the document's own format, and a line is kept only when
its own numbers hold (quantity × unit price, less a discount, is its total). The lines are used only when, in
addition, they add up to the invoice's net (or gross) at each VAT rate: from its fiscal QR code, or the totals
it prints. Anything else leaves the invoice without lines: never guessed.

The photos and the scan in ``fixtures/photos`` (``make_tables.py``) are read by the real engine on this
machine. Those tests skip only when the engine is not installed; CI installs it and sets
``BACKOFFICE_REQUIRE_LOCAL_OCR=1``, so there a missing engine fails instead. The rules themselves are also
tested on located words directly, without the engine.
"""

from __future__ import annotations

import base64
import json
import os
from decimal import Decimal as D
from pathlib import Path

import pytest

from backoffice.domain.models import BoundingBox, Quality
from backoffice.line_prices import read_table_lines
from backoffice.ocr import OCRLine, OCRPage, OCRWord, local_ocr_available
from backoffice.reading import ReadOutcome, reader_from_env
from backoffice.reading.tables import Word, WordRow, word_rows
from backoffice.server.reads import decode_outcome, encode_outcome
from backoffice.service import BackOfficeService

PHOTOS = Path(__file__).parent / "fixtures" / "photos"


# --------------------------------------------------------------------------- the rules, on located words


def row(top: int, *words: tuple[str, int, int], page: int = 1) -> WordRow:
    return WordRow(page=page, top=top, bottom=top + 30, words=tuple(Word(t, x0, x1) for t, x0, x1 in words))


HEAD = row(100, ("Código", 80, 160), ("Descrição", 200, 320), ("Qtd.", 600, 650), ("Un. Preço unit.", 670, 880),
           ("Desc.", 910, 970), ("IVA", 1010, 1060), ("Total", 1110, 1170))


def priced(top: int, code: str, desc: str, qty: str, unit: str, price: str, disc: str, vat: str, total: str,
           desc_end: int = 480) -> WordRow:
    """A line laid out under HEAD: text from the left, numbers right-aligned to their heading."""
    return row(top, (code, 80, 150), (desc, 200, desc_end), (qty, 650 - 15 * len(qty), 650), (unit, 672, 700),
               (price, 880 - 15 * len(price), 880), (disc, 970 - 15 * len(disc), 970),
               (vat, 1060 - 15 * len(vat), 1060), (total, 1170 - 15 * len(total), 1170))


def test_columns_are_read_from_where_each_word_sits() -> None:
    rows = [
        row(40, ("Fatura n.º FT X/1", 80, 400)),
        HEAD,
        priced(150, "TIN15", "Tinta aquosa branca 15 L", "4", "un", "62,50", "0%", "23%", "250,00"),
        priced(195, "MAD22", "Ripa de pinho 22x45 mm", "36", "m", "2,15", "10%", "23%", "69,66"),
        # A long description runs on past its column's edge; its numbers are still its own.
        priced(240, "PAR01", "Parafusos inox 4x40 (caixa de 200)", "1.200", "un", "0,04", "0%", "23%", "48,00",
               desc_end=590),
        row(272, ("em aço inoxidável A2", 200, 420)),  # the same line's description, continued
        # "1 036,80" read as two runs of text: one amount in one column.
        row(320, ("LIV03", 80, 150), ("Livro: Guia", 200, 380), ("12", 620, 650), ("un", 672, 700),
            ("86,40", 805, 880), ("0%", 940, 970), ("6%", 1030, 1060), ("1", 1040 + 50, 1105), ("036,80", 1110, 1170)),
        row(400, ("Base tributável (23%): 367,66", 800, 1170)),
    ]
    lines = read_table_lines(rows, ",")
    assert [(x.code, x.description, x.quantity, x.unit, x.unit_price, x.net, x.vat_rate) for x in lines] == [
        ("TIN15", "Tinta aquosa branca 15 L", D("4"), "unit", D("62.50"), D("250.00"), D("23")),
        ("MAD22", "Ripa de pinho 22x45 mm", D("36"), "m", D("1.935"), D("69.66"), D("23")),
        ("PAR01", "Parafusos inox 4x40 (caixa de 200) em aço inoxidável A2", D("1200"), "unit", D("0.04"),
         D("48.00"), D("23")),
        ("LIV03", "Livro: Guia", D("12"), "unit", D("86.40"), D("1036.80"), D("6")),
    ]


def test_a_heading_the_engine_split_in_two_is_one_column_and_dot_decimals_are_read() -> None:
    rows = [
        row(100, ("Item", 100, 160), ("Unit", 640, 700), ("price", 710, 780), ("Qty", 840, 890),
            ("Amount", 1080, 1170)),
        row(150, ("Oak veneer sheet", 100, 330), ("86.40", 705, 780), ("12", 860, 890), ("1,036.80", 1050, 1170)),
        row(195, ("Hardwax oil 2.5 L", 100, 330), ("41.20", 705, 780), ("3", 875, 890), ("123.60", 1080, 1170)),
        row(260, ("Subtotal: €1,160.40", 900, 1170)),
    ]
    lines = read_table_lines(rows, ".")
    assert [(x.description, x.quantity, x.unit_price, x.net, x.vat_rate) for x in lines] == [
        ("Oak veneer sheet", D("12"), D("86.40"), D("1036.80"), None),
        ("Hardwax oil 2.5 L", D("3"), D("41.20"), D("123.60"), None)]


def test_a_row_whose_numbers_do_not_hold_refuses_the_whole_table() -> None:
    good = priced(150, "TIN15", "Tinta aquosa branca 15 L", "4", "un", "62,50", "0%", "23%", "250,00")
    wrong = priced(195, "PIN01", "Pincel plano 50 mm", "10", "un", "3,45", "0%", "23%", "35,40")  # 10 × 3,45 = 34,50
    assert read_table_lines([HEAD, good, wrong], ",") == ()
    missing = row(195, ("PIN01", 80, 150), ("Pincel plano 50 mm", 200, 480), ("10", 620, 650), ("un", 672, 700),
                  ("34,50", 1095, 1170))  # its unit price was not read
    assert read_table_lines([HEAD, good, missing], ",") == ()
    assert len(read_table_lines([HEAD, good], ",")) == 1


def test_without_a_priced_heading_there_is_no_table() -> None:
    lines = [priced(150, "TIN15", "Tinta aquosa branca 15 L", "4", "un", "62,50", "0%", "23%", "250,00")]
    assert read_table_lines(lines, ",") is None  # the text is read instead
    two_totals = row(100, ("Descrição", 200, 320), ("Qtd.", 600, 650), ("Preço", 750, 880), ("Total", 950, 1030),
                     ("Total", 1110, 1170))
    assert read_table_lines([two_totals, *lines], ",") is None  # which total is the line's would be a guess
    assert read_table_lines([], ",") is None


def test_word_rows_undo_the_photos_tilt_so_a_column_stays_a_column() -> None:
    import math

    tilt = math.radians(3.0)

    def word(text: str, x: float, y: float, width: float, height: float = 30) -> OCRWord:
        # A word photographed 3° clockwise: its centre moves down as it moves right; its box is axis-aligned.
        cx, cy = x * math.cos(tilt) - y * math.sin(tilt), x * math.sin(tilt) + y * math.cos(tilt)
        w = width * math.cos(tilt) + height * math.sin(tilt)
        h = width * math.sin(tilt) + height * math.cos(tilt)
        return OCRWord(text=text, bbox=BoundingBox(page=1, x0=cx - w / 2, y0=cy - h / 2, x1=cx + w / 2, y1=cy + h / 2))

    def line(*words: OCRWord) -> OCRLine:
        box = BoundingBox(page=1, x0=min(w.bbox.x0 for w in words), y0=min(w.bbox.y0 for w in words),
                          x1=max(w.bbox.x1 for w in words), y1=max(w.bbox.y1 for w in words))
        return OCRLine(text=" ".join(w.text for w in words), bbox=box, angle=3.0, words=words)

    page = OCRPage(number=1, lines=(
        line(word("Total", 1100, 100, 60)),
        line(word("250,00", 1090, 400, 80)),
        line(word("34,50", 1095, 900, 70)),
    ))
    rows = word_rows([page])
    right = [r.words[0].x1 for r in rows]
    assert max(right) - min(right) <= 2  # right-aligned under "Total", 800 px down the page: still one column
    tilted = [w.bbox.x1 for line in page.lines for w in line.words]
    assert max(tilted) - min(tilted) > 40  # as photographed, the same edge drifts by over 40 px
    assert [r.text for r in rows] == ["Total", "250,00", "34,50"]
    assert all(25 <= r.bottom - r.top <= 35 for r in rows)  # each word's own height, not its tilted box's


def test_located_words_are_recorded_with_the_reading_and_replayed_exactly() -> None:
    outcome = ReadOutcome(text="x", word_rows=(HEAD, row(150, ("TIN15", 80, 150))))
    assert decode_outcome(json.loads(json.dumps(encode_outcome(outcome)))) == outcome


# --------------------------------------------------------------------------- photos and scans, the real engine


@pytest.fixture(scope="module")
def read() -> dict:
    """Each fixture uploaded once to the demo business, read exactly as the server reads it."""
    if not local_ocr_available():
        if os.environ.get("BACKOFFICE_REQUIRE_LOCAL_OCR"):
            pytest.fail("the local OCR engine must run here (pip install -e '.[ocr-local,qr]')", pytrace=False)
        pytest.skip("the local OCR engine is not installed (the ocr-local extra)")
    svc = BackOfficeService.demo()
    svc.repo.reader = reader_from_env({})  # no sidecar: PP-OCRv6 in this process
    found = {"svc": svc}
    for name in ("ft-mt2026-0518.jpg", "inv-bt-20417-scan.pdf", "ft-ld2026-0093.jpg"):
        data = (PHOTOS / name).read_bytes()
        body = {"filename": name, "contentType": None, "dataBase64": base64.b64encode(data).decode()}
        status, out = svc.dispatch("POST", "/api/evidence", json.dumps(body))
        assert status == 200 and len(out["documents"]) == 1, out
        found[name] = svc.repo.documents[out["documents"][0]["id"]]
    return found


def test_a_photographed_invoice_table_is_read_by_column_and_adds_up_to_its_fiscal_qr(read) -> None:
    svc, record = read["svc"], read["ft-mt2026-0518.jpg"]
    found = svc.orchestrator.line_prices.invoice_lines(record)
    assert found is not None and found.source == "table"
    assert [(x.code, x.description, x.quantity, x.unit, x.unit_price, x.net, x.vat_rate) for x in found.lines] == [
        ("TIN15", "Tinta aquosa branca 15 L", D("4"), "unit", D("62.50"), D("250.00"), D("23")),
        ("PIN01", "Pincel plano 50 mm", D("10"), "unit", D("3.45"), D("34.50"), D("23")),
        ("MAD22", "Ripa de pinho 22x45 mm", D("36"), "m", D("1.935"), D("69.66"), D("23")),  # less 10%
        ("COL05", "Cola de madeira 5 kg", D("2"), "unit", D("18.90"), D("37.80"), D("23")),
        ("PAR01", "Parafusos inox 4x40", D("1200"), "unit", D("0.04"), D("48.00"), D("23")),  # "1.200"
        ("LIV03", "Livro: Guia de acabamentos", D("1"), "unit", D("24.00"), D("24.00"), D("6")),
    ]
    # Verified twice: every row holds on its own, and the lines add up, rate by rate, to the fiscal QR code.
    assert found.check == "The lines add up to the invoice's net: 6% €24.00, 23% €439.96."
    qr = svc.orchestrator.cost_centers.vat_parts(record, None)
    assert [(p.rate, p.net, p.vat) for p in qr] == [(D("6"), D("24.00"), D("1.44")), (D("23"), D("439.96"), D("101.19"))]
    assert record.checks["gross_amount"].quality is Quality.GREEN  # the QR code and the reading agree
    # The prices are compared per litre or per kilo when the description says the pack size.
    prices = {p.product: (p.basis, p.unit_price) for p in svc.orchestrator.line_prices.purchases()
              if p.document_id == record.id}
    assert prices["Tinta aquosa branca 15 L"] == ("l", D("250.00") / D("60"))
    assert prices["Cola de madeira 5 kg"] == ("kg", D("3.78"))


def test_a_scanned_english_invoice_table_is_read_with_its_own_number_format(read) -> None:
    svc, record = read["svc"], read["inv-bt-20417-scan.pdf"]
    assert svc.repo.reads[record.evidence_ids[0]].text == ""  # a scan: no text layer, only pixels
    found = svc.orchestrator.line_prices.invoice_lines(record)
    assert found is not None and found.source == "table"
    assert [(x.description, x.quantity, x.unit_price, x.net, x.vat_rate) for x in found.lines] == [
        ("Oak veneer sheet 2500x1250 mm (A grade, crown cut)", D("12"), D("86.40"), D("1036.80"), D("0")),
        ("Edge banding oak 22 mm (50 m)", D("8"), D("23.75"), D("190.00"), D("0")),
        ("Hardwax oil 2.5 L", D("3"), D("41.20"), D("123.60"), D("0")),
    ]
    assert found.check == "The lines add up to the invoice's net: €1,350.40."


def test_a_photographed_table_that_does_not_add_up_is_refused(read) -> None:
    """Each row holds, but a delivery charge printed under the table is part of the net: the lines are 71,70,
    the invoice's net 86,70. Its lines are not used at all, so no price is read from it."""
    svc, record = read["svc"], read["ft-ld2026-0093.jpg"]
    rows = svc.repo.reads[record.evidence_ids[0]].word_rows
    table = read_table_lines(rows, ",")
    assert [(x.description, x.net) for x in table] == [("Lixa grão 120 (cx. 50)", D("38.70")),
                                                      ("Massa de reparação 1 kg", D("33.00"))]
    assert record.document.net_amount == D("86.70") and record.document.quality is Quality.GREEN
    assert svc.orchestrator.line_prices.invoice_lines(record) is None
    assert not [p for p in svc.orchestrator.line_prices.purchases() if p.document_id == record.id]
