"""Builds the photographed and scanned invoice-table fixtures (§11, §13, QA E8). Run from backend/:

    python tests/fixtures/photos/make_tables.py

Same camera and fonts as ``make_photos.py`` (Pillow, numpy and reportlab, only to regenerate; the tests
read the committed files). Every company, tax number and amount is fictional; tax numbers pass the
Portuguese check digit and every fiscal QR payload is checked with the Portugal pack's own parser. Output is
deterministic (fixed seeds).

Files:

* ``ft-mt2026-0518.jpg``: an A4 invoice from a paint and timber shop with an eight-column table (code,
  description, quantity, unit, unit price, discount, VAT rate, line total; numbers right-aligned), two VAT
  rates, descriptions that hold numbers of their own ("Tinta aquosa branca 15 L"), a quantity with a
  thousands separator ("1.200"), and its fiscal QR code; photographed 2.5 degrees rotated, in perspective.
* ``inv-bt-20417-scan.pdf``: an English invoice (dot decimals, comma thousands, the unit price before the
  quantity, a description on two lines) scanned to a PDF with no text layer.
* ``ft-ld2026-0093.jpg``: an invoice whose lines do not add up to its net: a delivery charge is printed
  under the table, not as a line. Its lines must be refused.
* ``*.txt``: what is printed on each page, line by line.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[2] / "src"))

from backoffice.countries.pt.nif import validate_nif  # noqa: E402
from make_photos import HAZEL_NIF, draw_qr, font, photograph, qr_payload, rotated_corners  # noqa: E402

TINTAS_NIF = "513204113"
TINTAS_QR = qr_payload(
    A=TINTAS_NIF, B=HAZEL_NIF, C="PT", D="FT", E="N", F="20260915", G="FT MT2026/518", H="MTQ4K7P2-518",
    I1="PT", I3="24.00", I4="1.44", I7="439.96", I8="101.19", N="102.63", O="566.59", Q="t9Xa", R="2311",
)
TINTAS_HEADER = [
    ("Madeiras & Tintas do Douro, Lda.", "title"),
    ("Rua de Costa Cabral 912, 4200-225 Porto", "small"),
    (f"NIF: {TINTAS_NIF}   Tel. 225 018 377", "small"),
    ("", "gap"),
    ("FATURA", "heading"),
    ("Fatura n.º FT MT2026/518", "body"),
    ("ATCUD: MTQ4K7P2-518", "body"),
    ("Data de emissão: 15/09/2026", "body"),
    ("Cliente: Hazel Tree Interiores, Lda.", "body"),
    (f"NIF: {HAZEL_NIF}", "body"),
]
# (label, x, align): "l" draws from x, "r" ends at x.
TINTAS_COLUMNS = [("Código", 80, "l"), ("Descrição", 205, "l"), ("Qtd.", 640, "r"), ("Un.", 670, "l"),
                  ("Preço unit.", 880, "r"), ("Desc.", 975, "r"), ("IVA", 1060, "r"), ("Total", 1165, "r")]
TINTAS_ITEMS = [
    ("TIN15", "Tinta aquosa branca 15 L", "4", "un", "62,50", "0%", "23%", "250,00"),
    ("PIN01", "Pincel plano 50 mm", "10", "un", "3,45", "0%", "23%", "34,50"),
    ("MAD22", "Ripa de pinho 22x45 mm", "36", "m", "2,15", "10%", "23%", "69,66"),
    ("COL05", "Cola de madeira 5 kg", "2", "un", "18,90", "0%", "23%", "37,80"),
    ("PAR01", "Parafusos inox 4x40", "1.200", "un", "0,04", "0%", "23%", "48,00"),
    ("LIV03", "Livro: Guia de acabamentos", "1", "un", "24,00", "0%", "6%", "24,00"),
]
TINTAS_TOTALS = [
    "Base tributável (6%): 24,00",
    "IVA 6%: 1,44",
    "Base tributável (23%): 439,96",
    "IVA 23%: 101,19",
    "Total: 566,59 €",
]

LIXA_NIF = "509418775"
LIXA_QR = qr_payload(
    A=LIXA_NIF, B=HAZEL_NIF, C="PT", D="FT", E="N", F="20260922", G="FT LD2026/93", H="LDQ2M8R5-93",
    I1="PT", I7="86.70", I8="19.94", N="19.94", O="106.64", Q="p3Lz", R="1876",
)
LIXA_HEADER = [
    ("Lixas & Drogaria Bonfim, Lda.", "title"),
    ("Rua do Bonfim 233, 4300-069 Porto", "small"),
    (f"NIF: {LIXA_NIF}", "small"),
    ("", "gap"),
    ("FATURA", "heading"),
    ("Fatura n.º FT LD2026/93", "body"),
    ("ATCUD: LDQ2M8R5-93", "body"),
    ("Data de emissão: 22/09/2026", "body"),
    ("Cliente: Hazel Tree Interiores, Lda.", "body"),
    (f"NIF: {HAZEL_NIF}", "body"),
]
LIXA_COLUMNS = [("Descrição", 110, "l"), ("Qtd.", 700, "r"), ("Preço", 880, "r"), ("IVA", 1000, "r"),
                ("Valor", 1150, "r")]
LIXA_ITEMS = [
    ("Lixa grão 120 (cx. 50)", "3", "12,90", "23%", "38,70"),
    ("Massa de reparação 1 kg", "4", "8,25", "23%", "33,00"),
]
LIXA_TOTALS = [
    "Portes de envio: 15,00",
    "Base tributável (23%): 86,70",
    "IVA 23%: 19,94",
    "Total: 106,64 €",
]

TIMBER_COLUMNS = [("Item", 110, "l"), ("Unit price", 760, "r"), ("Qty", 860, "r"), ("VAT", 960, "r"),
                  ("Amount", 1150, "r")]
TIMBER_ITEMS = [
    ("Oak veneer sheet 2500x1250 mm", "86.40", "12", "0%", "1,036.80"),
    ("(A grade, crown cut)", "", "", "", ""),
    ("Edge banding oak 22 mm (50 m)", "23.75", "8", "0%", "190.00"),
    ("Hardwax oil 2.5 L", "41.20", "3", "0%", "123.60"),
]
TIMBER_HEADER = [
    ("Baltic Timber Ltd", "title"),
    ("14 Dock Road, Hull HU1 2AB, United Kingdom", "small"),
    ("VAT number: GB287461903", "small"),
    ("", "gap"),
    ("INVOICE", "heading"),
    ("Invoice number: BT-20417", "body"),
    ("Invoice date: 18 September 2026", "body"),
    ("Billed to: Hazel Tree Interiores, Lda.", "body"),
    (f"VAT number: PT{HAZEL_NIF}", "body"),
]
TIMBER_TOTALS = [
    "Subtotal: €1,350.40",
    "VAT (0%): €0.00",
    "Total due: €1,350.40",
]


def invoice_with_table(header, columns, items, totals, qr: str | None):  # type: ignore[no-untyped-def]
    """An A4 page at about 150 dpi (1240 x 1754): header lines, a shaded table header, rows, totals, QR."""
    from PIL import Image, ImageDraw

    page = Image.new("RGB", (1240, 1754), (252, 251, 247))
    draw = ImageDraw.Draw(page)
    ink = (22, 22, 26)
    styles = {"title": font(44, bold=True), "small": font(24), "heading": font(36, bold=True), "body": font(28)}
    y = 100
    for text, style in header:
        if style == "gap":
            y += 30
            continue
        draw.text((90, y), text, fill=ink, font=styles[style])
        y += {"title": 64, "small": 36, "heading": 56, "body": 42}[style]
    y += 40
    head, cell = font(23, bold=True), font(24)

    def put(x: int, align: str, text: str, face, top: int) -> None:  # type: ignore[no-untyped-def]
        left = x if align == "l" else x - draw.textlength(text, font=face)
        draw.text((left, top), text, fill=ink, font=face)

    draw.rectangle([70, y - 10, 1180, y + 36], fill=(232, 234, 230))
    for label, x, align in columns:
        put(x, align, label, head, y)
    y += 60
    for row in items:
        for (_, x, align), text in zip(columns, row):
            if text:
                put(x, align, text, cell, y)
        y += 44
    draw.line([70, y + 4, 1180, y + 4], fill=(150, 150, 150), width=2)
    y += 34
    for i, text in enumerate(totals):
        face = font(32, bold=True) if i == len(totals) - 1 else font(28)
        put(1165, "r", text, face, y)
        y += 48
    if qr:
        side = draw_qr(draw, qr, 90, 1360, 6)
        draw.text((90 + side + 40, 1370), "Processado por programa certificado", fill=(90, 90, 90), font=font(20))
    return page


def scan_to_pdf(page, *, degrees: float, seed: int) -> bytes:  # type: ignore[no-untyped-def]
    """``page`` through an office scanner: slightly crooked, grey, a little noise, JPEG inside a PDF with no
    text layer (Pillow writes the image alone)."""
    import numpy as np
    from PIL import Image

    rng = np.random.default_rng(seed)
    grey = page.convert("L").rotate(degrees, resample=Image.Resampling.BICUBIC, expand=False, fillcolor=250)
    pixels = np.asarray(grey).astype(float) * 0.97 + rng.normal(0, 5, grey.size[::-1])
    scanned = Image.fromarray(np.clip(pixels, 0, 255).astype("uint8"))
    jpeg = io.BytesIO()
    scanned.save(jpeg, format="JPEG", quality=82)
    out = io.BytesIO()
    Image.open(io.BytesIO(jpeg.getvalue())).save(out, format="PDF", resolution=150.0,
                                                 title="Scan", creator="Office scanner", producer="Scanner")
    return out.getvalue()


def main() -> None:
    for nif in (TINTAS_NIF, LIXA_NIF, HAZEL_NIF):
        assert validate_nif(nif).valid, nif

    tintas = invoice_with_table(TINTAS_HEADER, TINTAS_COLUMNS, TINTAS_ITEMS, TINTAS_TOTALS, TINTAS_QR)
    (HERE / "ft-mt2026-0518.jpg").write_bytes(photograph(
        tintas, canvas=(1500, 2000), corners=rotated_corners(750, 1000, 1240 * 1.06, 1754 * 1.06, 2.5), seed=41))

    lixa = invoice_with_table(LIXA_HEADER, LIXA_COLUMNS, LIXA_ITEMS, LIXA_TOTALS, LIXA_QR)
    (HERE / "ft-ld2026-0093.jpg").write_bytes(photograph(
        lixa, canvas=(1500, 2000), corners=[(160, 130), (1360, 170), (1390, 1850), (130, 1870)], seed=43))

    timber = invoice_with_table(TIMBER_HEADER, TIMBER_COLUMNS, TIMBER_ITEMS, TIMBER_TOTALS, None)
    (HERE / "inv-bt-20417-scan.pdf").write_bytes(scan_to_pdf(timber, degrees=0.8, seed=47))

    def printed(header, columns, items, totals) -> list[str]:  # type: ignore[no-untyped-def]
        rows = ["  ".join(label for label, _, _ in columns)]
        rows += ["  ".join(cell for cell in row if cell) for row in items]
        return [t for t, s in header if s != "gap"] + rows + totals

    for name, lines in {
        "ft-mt2026-0518.txt": printed(TINTAS_HEADER, TINTAS_COLUMNS, TINTAS_ITEMS, TINTAS_TOTALS),
        "ft-ld2026-0093.txt": printed(LIXA_HEADER, LIXA_COLUMNS, LIXA_ITEMS, LIXA_TOTALS),
        "inv-bt-20417-scan.txt": printed(TIMBER_HEADER, TIMBER_COLUMNS, TIMBER_ITEMS, TIMBER_TOTALS),
    }.items():
        (HERE / name).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("written: ft-mt2026-0518.jpg, ft-ld2026-0093.jpg, inv-bt-20417-scan.pdf and their .txt")


if __name__ == "__main__":
    main()
