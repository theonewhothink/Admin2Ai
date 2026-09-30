"""Builds the uploaded-documents golden fixtures (§56). Run from backend/:

    python tests/fixtures/documents/make_documents.py

Needs reportlab and Pillow (only to regenerate; the tests read the committed
files). Every company, tax number and amount is fictional; tax numbers pass
the Portuguese check digit, QR payloads follow the AT specification and are
checked with the Portugal pack's own parser before anything is written.

Files:

* ``edp-ft-558120.pdf``: born-digital invoice, text layer, the fiscal QR drawn
  as a real QR code and its payload printed under it ("Código QR: A:...").
  It is the demo's EDP September invoice, the one being chased.
* ``moderna-ft-pm2026-88.pdf``: born-digital invoice whose QR payload is only
  in the PDF metadata (Subject), not in the text.
* ``alianca-ft-ga2026-412.pdf``: the printed totals were altered: the text
  says 483,60 € while the fiscal QR says 438.00.
* ``central-fs-cc2026-3317-scan.pdf``: a scanned receipt, pixels only (no text layer).
* ``central-fs-cc2026-3317.jpg``: the same receipt photographed on a phone, with
  EXIF metadata (camera, time, GPS position) that must never leave (§53) and a
  real fiscal QR code on it.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2] / "src"))

from backoffice.countries.pt.qr import FIELD_ORDER, parse_qr  # noqa: E402

HAZEL_NIF = "516123459"
COMPANY_C_NIF = "517003210"


def qr_payload(**fields: str) -> str:
    payload = "*".join(f"{k}:{fields[k]}" for k in FIELD_ORDER if fields.get(k) not in (None, ""))
    parse_qr(payload)  # raises when malformed
    return payload


EDP_QR = qr_payload(
    A="501000100", B=HAZEL_NIF, C="PT", D="FT", E="N", F="20260918", G="FT EDP2026/558120", H="EDPQ7K2M-558120",
    I1="PT", I7="52.11", I8="11.99", N="11.99", O="64.10", Q="e1Dk", R="1422",
)
EDP_LINES = [
    "EDP Comercial - Comercialização de Energia, S.A.", "NIF: 501000100", "Fatura n.º FT EDP2026/558120",
    "ATCUD: EDPQ7K2M-558120", "Data de emissão: 18/09/2026", "Data de vencimento: 19/09/2026",
    "Cliente: Hazel Tree Interiores, Lda.", f"NIF: {HAZEL_NIF}", "Eletricidade - estúdio, agosto/setembro",
    "Base tributável (23%): 52,11", "IVA 23%: 11,99", "Total: 64,10 €",
]

MODERNA_QR = qr_payload(
    A="514400218", B=COMPANY_C_NIF, C="PT", D="FT", E="N", F="20260921", G="FT PM2026/88", H="PMQ4X8ZT-88",
    I1="PT", I7="40.00", I8="9.20", N="9.20", O="49.20", Q="m0Dr", R="2764",
)
MODERNA_LINES = [
    "Papelaria Moderna do Porto, Lda.", "Rua de Santa Catarina 212, 4000-447 Porto", "NIF: 514400218",
    "Fatura n.º FT PM2026/88", "ATCUD: PMQ4X8ZT-88", "Data de emissão: 21/09/2026",
    "Cliente: Company C Studio, Unipessoal Lda.", f"NIF: {COMPANY_C_NIF}", "Cadernos, papel de desenho e marcadores",
    "Base tributável (23%): 40,00", "IVA 23%: 9,20", "Total: 49,20 €",
]

# What the fiscal QR (and the tax authority) says: 356,10 + 81,90 = 438,00.
ALIANCA_QR = qr_payload(
    A="509123457", B=HAZEL_NIF, C="PT", D="FT", E="N", F="20260924", G="FT GA2026/412", H="GAQ9K3LW-412",
    I1="PT", I7="356.10", I8="81.90", N="81.90", O="438.00", Q="aL7c", R="1893",
)
# What the printed text says: 393,17 + 90,43 = 483,60.
ALIANCA_LINES = [
    "Gráfica Aliança, Lda.", "NIF: 509123457", "Fatura n.º FT GA2026/412", "ATCUD: GAQ9K3LW-412",
    "Data de emissão: 24/09/2026", "Cliente: Hazel Tree Interiores, Lda.", f"NIF: {HAZEL_NIF}",
    "Catálogos da coleção de outono (500 un.)", "Base tributável (23%): 393,17", "IVA 23%: 90,43",
    "Total: 483,60 €",
]

CENTRAL_QR = qr_payload(
    A="516722344", B=HAZEL_NIF, C="PT", D="FS", E="N", F="20260929", G="FS CC2026/3317", H="CCQ7M2KP-3317",
    I1="PT", I5="21.15", I6="2.75", N="2.75", O="23.90", Q="Kx2e", R="0987",
)
CENTRAL_LINES = [
    "Café Central da Baixa", "Rua Augusta 101, 1100-048 Lisboa", "NIF: 516722344",
    "Fatura simplificada n.º FS CC2026/3317", "ATCUD: CCQ7M2KP-3317", "Data de emissão: 29/09/2026",
    f"Cliente NIF: {HAZEL_NIF}", "Almoço de equipa (3 pessoas)", "Base tributável (13%): 21,15", "IVA 13%: 2,75",
    "Total: 23,90 €", "Tel. 213 400 912",
]


# --------------------------------------------------------------------------- QR matrix


def qr_matrix(text: str) -> list[list[bool]]:
    from reportlab.graphics.barcode.qrencoder import QRCode, QRErrorCorrectLevel

    code = QRCode(None, QRErrorCorrectLevel.M)
    code.addData(text)
    code.make()
    n = code.getModuleCount()
    return [[bool(code.isDark(r, c)) for c in range(n)] for r in range(n)]


# --------------------------------------------------------------------------- PDFs


def invoice_pdf(path: Path, lines: list[str], qr: str, *, qr_in_text: bool, qr_in_metadata: bool,
                title: str) -> None:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(str(path), pagesize=A4, invariant=1)
    c.setTitle(title)
    c.setAuthor("Faturação certificada n.º 0000/AT")
    if qr_in_metadata:
        c.setSubject(qr)
    width, height = A4
    y = height - 72
    for i, line in enumerate(lines):
        c.setFont("Helvetica-Bold" if i == 0 else "Helvetica", 13 if i == 0 else 10)
        c.drawString(56, y, line)
        y -= 18
    matrix = qr_matrix(qr)
    cell = 3.2
    top = 190
    c.setFillColorRGB(0, 0, 0)
    for r, row in enumerate(matrix):
        for col, dark in enumerate(row):
            if dark:
                c.rect(56 + col * cell, top - r * cell, cell, cell, stroke=0, fill=1)
    if qr_in_text:
        c.setFont("Helvetica", 5.2)
        c.drawString(56, 40, f"Código QR: {qr}")
    c.showPage()
    c.save()


def scan_pdf(path: Path, jpeg: bytes) -> None:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(str(path), pagesize=A4, invariant=1)
    c.setTitle("Digitalização")
    width, height = A4
    c.drawImage(ImageReader(io.BytesIO(jpeg)), 90, 120, width=width - 180, height=height - 240)
    c.showPage()
    c.save()


# --------------------------------------------------------------------------- photo


def receipt_photo(lines: list[str], qr: str) -> bytes:
    import reportlab
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", (900, 1150), (246, 244, 238))
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype(str(Path(reportlab.__file__).parent / "fonts" / "Vera.ttf"), 30)
    y = 60
    for line in lines:
        draw.text((60, y), line, fill=(20, 20, 20), font=font)
        y += 50
    matrix = qr_matrix(qr)
    cell = 7
    left, top = 60, y + 30
    border = 4 * cell
    draw.rectangle([left - border, top - border, left + len(matrix) * cell + border,
                    top + len(matrix) * cell + border], fill=(255, 255, 255))
    for r, row in enumerate(matrix):
        for col, dark in enumerate(row):
            if dark:
                draw.rectangle([left + col * cell, top + r * cell, left + (col + 1) * cell - 1,
                                top + (r + 1) * cell - 1], fill=(0, 0, 0))
    exif = Image.Exif()
    exif[0x010F] = "Fictional Phone Co."  # Make
    exif[0x0110] = "Model X"  # Model
    exif[0x0132] = "2026:09:29 13:42:10"  # DateTime
    gps = exif.get_ifd(0x8825)
    gps[1] = "N"
    gps[2] = (38.0, 42.0, 36.0)
    gps[3] = "W"
    gps[4] = (9.0, 8.0, 12.0)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=80, exif=exif.tobytes())
    return buffer.getvalue()


def main() -> None:
    invoice_pdf(HERE / "edp-ft-558120.pdf", EDP_LINES, EDP_QR, qr_in_text=True, qr_in_metadata=False,
                title="Fatura FT EDP2026/558120")
    invoice_pdf(HERE / "moderna-ft-pm2026-88.pdf", MODERNA_LINES, MODERNA_QR, qr_in_text=False,
                qr_in_metadata=True, title="Fatura FT PM2026/88")
    invoice_pdf(HERE / "alianca-ft-ga2026-412.pdf", ALIANCA_LINES, ALIANCA_QR, qr_in_text=True,
                qr_in_metadata=False, title="Fatura FT GA2026/412")
    photo = receipt_photo(CENTRAL_LINES, CENTRAL_QR)
    (HERE / "central-fs-cc2026-3317.jpg").write_bytes(photo)
    scan_pdf(HERE / "central-fs-cc2026-3317-scan.pdf", photo)
    (HERE / "central-fs-cc2026-3317.txt").write_text("\n".join(CENTRAL_LINES) + "\n", encoding="utf-8")
    print("written:", ", ".join(sorted(p.name for p in HERE.iterdir() if p.suffix in (".pdf", ".jpg", ".txt"))))


if __name__ == "__main__":
    main()
