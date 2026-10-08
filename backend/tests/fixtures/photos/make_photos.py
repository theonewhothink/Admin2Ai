"""Builds the photographed-receipts golden fixtures (§11, §14, §56). Run from backend/:

    python tests/fixtures/photos/make_photos.py

Needs Pillow, numpy and reportlab (only to regenerate; the tests read the
committed files). Text is drawn with a common font (DejaVu Sans, or
reportlab's Bitstream Vera, the same design, when DejaVu is not installed),
then each page is "photographed": placed on a table, seen in perspective,
lit unevenly, with sensor noise and JPEG compression. Every company, tax
number and amount is fictional; tax numbers pass the Portuguese check digit
and every fiscal QR payload is checked with the Portugal pack's own parser
before anything is written. Output is deterministic (fixed seeds).

Files:

* ``ft-fba2026-1207.jpg``: an A4 invoice from a hardware shop with its fiscal QR
  code, photographed at an angle on a desk.
* ``fs-pb2026-0441.jpg``: a café's thermal till receipt (simplified invoice) with
  its fiscal QR code.
* ``fs-mr2026-0088.jpg``: a restaurant receipt photographed slightly rotated and a
  little out of focus, no QR code (an older till).
* ``fs-mr2026-0088-blurred.jpg``: the same receipt so blurred nobody could read
  it: the owner is asked to take it again.
* ``*.txt``: what is printed on each page, line by line (for the browser check
  and for reading the photos side by side).
"""

from __future__ import annotations

import io
import math
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2] / "src"))

from backoffice.countries.pt.nif import validate_nif  # noqa: E402
from backoffice.countries.pt.qr import FIELD_ORDER, parse_qr  # noqa: E402

HAZEL_NIF = "516123459"  # the demo's Hazel Tree Interiores, Lda.


def qr_payload(**fields: str) -> str:
    payload = "*".join(f"{k}:{fields[k]}" for k in FIELD_ORDER if fields.get(k) not in (None, ""))
    parse_qr(payload)  # raises when malformed
    return payload


# --------------------------------------------------------------------------- the documents

BOAVISTA_NIF = "514732288"
BOAVISTA_QR = qr_payload(
    A=BOAVISTA_NIF, B=HAZEL_NIF, C="PT", D="FT", E="N", F="20260923", G="FT FBA2026/1207", H="JFBA7K2Q-1207",
    I1="PT", I7="96.50", I8="22.20", N="22.20", O="118.70", Q="b7Qe", R="2093",
)
BOAVISTA_HEADER = [
    ("Ferragens Boavista, Lda.", "title"),
    ("Avenida da Boavista 1840, 4100-115 Porto", "small"),
    (f"NIF: {BOAVISTA_NIF}   Tel. 226 091 344", "small"),
    ("", "gap"),
    ("FATURA", "heading"),
    ("Fatura n.º FT FBA2026/1207", "body"),
    ("ATCUD: JFBA7K2Q-1207", "body"),
    ("Data de emissão: 23/09/2026", "body"),
    ("Data de vencimento: 23/10/2026", "body"),
    ("", "gap"),
    ("Cliente: Hazel Tree Interiores, Lda.", "body"),
    (f"NIF: {HAZEL_NIF}", "body"),
]
BOAVISTA_ITEMS = [
    ("Parafusos inox 4x40 (cx. 200)", "2", "12,50", "25,00"),
    ("Dobradiças latão 75 mm", "6", "4,25", "25,50"),
    ("Verniz mate 2,5 L", "1", "46,00", "46,00"),
]
BOAVISTA_TOTALS = [
    "Base tributável (23%): 96,50",
    "IVA 23%: 22,20",
    "Total: 118,70 €",
]

BOLHAO_NIF = "509882412"
BOLHAO_QR = qr_payload(
    A=BOLHAO_NIF, B=HAZEL_NIF, C="PT", D="FS", E="N", F="20260926", G="FS PB2026/441", H="KPB4M8XT-441",
    I1="PT", I5="7.43", I6="0.97", N="0.97", O="8.40", Q="Hq3z", R="1187",
)
BOLHAO_LINES = [
    "PASTELARIA DO BOLHÃO",
    "Rua Formosa 279, 4000-252 Porto",
    f"NIF: {BOLHAO_NIF}",
    "--------------------------------",
    "Fatura simplificada",
    "N.º FS PB2026/441",
    "ATCUD: KPB4M8XT-441",
    "Data: 26/09/2026  10:42",
    f"NIF cliente: {HAZEL_NIF}",
    "--------------------------------",
    "2 Galão            2,80",
    "2 Pastel de nata   2,60",
    "1 Tosta mista      3,00",
    "--------------------------------",
    "TOTAL           8,40 EUR",
    "Base 13%: 7,43",
    "IVA 13%: 0,97",
    "Pago com cartão  ****5530",
    "Obrigado pela visita!",
]

MARINHEIRO_NIF = "507311566"
MARINHEIRO_LINES = [
    "Restaurante O Marinheiro",
    "Rua do Ouro 52, 4150-551 Porto",
    f"NIF {MARINHEIRO_NIF}",
    "Fatura simplificada FS MR2026/88",
    "Data: 24/09/2026",
    f"Contribuinte: {HAZEL_NIF}",
    "Prato do dia x3      38,70",
    "Água 1,5L x2          4,20",
    "Cafés x3              2,70",
    "Total:  45,60 €",
    "IVA 13% incluído: 5,25",
    "Volte sempre!",
]


# --------------------------------------------------------------------------- drawing


def font(size: int, *, mono: bool = False, bold: bool = False):  # type: ignore[no-untyped-def]
    from PIL import ImageFont

    name = "DejaVuSansMono" if mono else "DejaVuSans"
    if bold:
        name += "-Bold"
    for folder in ("/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/dejavu", "/Library/Fonts"):
        path = Path(folder) / f"{name}.ttf"
        if path.is_file():
            return ImageFont.truetype(str(path), size)
    import reportlab  # Bitstream Vera: the font DejaVu was made from

    vera = {"DejaVuSans": "Vera", "DejaVuSans-Bold": "VeraBd", "DejaVuSansMono": "VeraMono",
            "DejaVuSansMono-Bold": "VeraMoBd"}[name]
    return ImageFont.truetype(str(Path(reportlab.__file__).parent / "fonts" / f"{vera}.ttf"), size)


def qr_matrix(text: str) -> list[list[bool]]:
    from reportlab.graphics.barcode.qrencoder import QRCode, QRErrorCorrectLevel

    code = QRCode(None, QRErrorCorrectLevel.M)
    code.addData(text)
    code.make()
    n = code.getModuleCount()
    return [[bool(code.isDark(r, c)) for c in range(n)] for r in range(n)]


def draw_qr(draw, payload: str, left: int, top: int, cell: int, ink=(18, 18, 18)) -> int:  # type: ignore[no-untyped-def]
    matrix = qr_matrix(payload)
    for r, row in enumerate(matrix):
        for c, dark in enumerate(row):
            if dark:
                draw.rectangle([left + c * cell, top + r * cell, left + (c + 1) * cell - 1, top + (r + 1) * cell - 1],
                               fill=ink)
    return len(matrix) * cell


def invoice_page():  # type: ignore[no-untyped-def]
    """The A4 invoice at about 150 dpi (1240 x 1754)."""
    from PIL import Image, ImageDraw

    page = Image.new("RGB", (1240, 1754), (252, 251, 247))
    draw = ImageDraw.Draw(page)
    ink = (22, 22, 26)
    styles = {"title": font(46, bold=True), "small": font(24), "heading": font(36, bold=True), "body": font(29)}
    y = 110
    for text, style in BOAVISTA_HEADER:
        if style == "gap":
            y += 30
            continue
        draw.text((110, y), text, fill=ink, font=styles[style])
        y += {"title": 66, "small": 36, "heading": 56, "body": 44}[style]
    y += 40
    head = font(25, bold=True)
    cell = font(26)
    columns = (110, 720, 840, 1010)
    draw.rectangle([100, y - 10, 1140, y + 38], fill=(232, 234, 230))
    for x, text in zip(columns, ("Descrição", "Qtd.", "Preço", "Valor")):
        draw.text((x, y), text, fill=ink, font=head)
    y += 62
    for row in BOAVISTA_ITEMS:
        for x, text in zip(columns, row):
            draw.text((x, y), text, fill=ink, font=cell)
        y += 46
    draw.line([100, y + 6, 1140, y + 6], fill=(150, 150, 150), width=2)
    y += 36
    totals = font(30)
    for i, text in enumerate(BOAVISTA_TOTALS):
        draw.text((640, y), text, fill=ink, font=font(34, bold=True) if i == len(BOAVISTA_TOTALS) - 1 else totals)
        y += 52
    side = draw_qr(draw, BOAVISTA_QR, 110, 1330, 6)
    draw.text((110 + side + 40, 1340), "Processado por programa certificado", fill=(90, 90, 90), font=font(20))
    draw.text((110 + side + 40, 1372), "n.º 2093/AT", fill=(90, 90, 90), font=font(20))
    draw.text((110 + side + 40, 1420), "Pagamento a 30 dias", fill=ink, font=font(22))
    return page


def thermal_receipt(lines: list[str], qr: str | None, *, width: int = 600):  # type: ignore[no-untyped-def]
    """A till receipt printed on 80 mm thermal paper (monospace, slightly grey ink)."""
    from PIL import Image, ImageDraw

    mono = font(28, mono=True)
    bold = font(32, mono=True, bold=True)
    height = 80 + 44 * len(lines) + (330 if qr else 60)
    paper = Image.new("RGB", (width, height), (248, 247, 242))
    draw = ImageDraw.Draw(paper)
    y = 50
    for i, line in enumerate(lines):
        face = bold if i == 0 or line.startswith(("TOTAL", "Total")) else mono
        w = draw.textlength(line, font=face)
        x = (width - w) / 2 if i < 3 or line.endswith("!") else 36
        draw.text((x, y), line, fill=(44, 44, 48), font=face)
        y += 44
    if qr:
        side = len(qr_matrix(qr)) * 5
        draw_qr(draw, qr, (width - side) // 2, y + 20, 5, ink=(40, 40, 44))
    return paper


# --------------------------------------------------------------------------- the camera


def _perspective_coefficients(dst: list[tuple[float, float]], src: list[tuple[float, float]]) -> list[float]:
    """PIL's 8 perspective coefficients mapping output points ``dst`` back to input points ``src``."""
    import numpy as np

    rows = []
    for (x, y), (u, v) in zip(dst, src):
        rows.append([x, y, 1, 0, 0, 0, -u * x, -u * y])
        rows.append([0, 0, 0, x, y, 1, -v * x, -v * y])
    a = np.array(rows, dtype=float)
    b = np.array([c for point in src for c in point], dtype=float)
    return list(np.linalg.solve(a, b))


def photograph(page, *, canvas: tuple[int, int], corners: list[tuple[float, float]], seed: int,  # type: ignore[no-untyped-def]
               blur: float = 0.0, noise: float = 6.0, light: float = 0.18, quality: int = 86) -> bytes:
    """``page`` lying on a desk, seen by a phone: corners land at ``corners`` (TL, TR, BR, BL) on a ``canvas``
    sized photo, light falls off across it, the sensor adds noise and the phone saves a JPEG."""
    import numpy as np
    from PIL import Image, ImageFilter

    rng = np.random.default_rng(seed)
    w, h = canvas
    yy, xx = np.mgrid[0:h, 0:w].astype(float)
    desk = np.empty((h, w, 3))
    for i, (base, slope) in enumerate(((112, 18), (92, 14), (70, 10))):  # warm wood, darker to one side
        desk[..., i] = base + slope * (xx / w) - 10 * (yy / h)
    desk += rng.normal(0, 7, (h, w, 1))
    photo = Image.fromarray(np.clip(desk, 0, 255).astype("uint8"))
    pw, ph = page.size
    coeffs = _perspective_coefficients(corners, [(0, 0), (pw, 0), (pw, ph), (0, ph)])
    warped = page.transform(canvas, Image.Transform.PERSPECTIVE, coeffs, Image.Resampling.BICUBIC)
    mask = Image.new("L", page.size, 255).transform(canvas, Image.Transform.PERSPECTIVE, coeffs,
                                                     Image.Resampling.BILINEAR)
    shadow = mask.filter(ImageFilter.GaussianBlur(18))
    dark = Image.new("RGB", canvas, (30, 24, 20))
    photo = Image.composite(dark, photo, shadow.point(lambda v: int(v * 0.45)))
    photo.paste(warped, (0, 0), mask)
    if blur:
        photo = photo.filter(ImageFilter.GaussianBlur(blur))
    pixels = np.asarray(photo).astype(float)
    cx, cy = rng.uniform(0.3, 0.7) * w, rng.uniform(0.2, 0.5) * h
    falloff = 1 - light * ((xx - cx) ** 2 + (yy - cy) ** 2) / (w ** 2 + h ** 2) * 2
    pixels = pixels * falloff[..., None] + rng.normal(0, noise, pixels.shape)
    out = Image.fromarray(np.clip(pixels, 0, 255).astype("uint8"))
    buffer = io.BytesIO()
    out.save(buffer, format="JPEG", quality=quality, optimize=True)
    return buffer.getvalue()


def rotated_corners(cx: float, cy: float, w: float, h: float, degrees: float) -> list[tuple[float, float]]:
    t = math.radians(degrees)
    out = []
    for dx, dy in ((-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2)):
        out.append((cx + dx * math.cos(t) - dy * math.sin(t), cy + dx * math.sin(t) + dy * math.cos(t)))
    return out


def main() -> None:
    for nif in (BOAVISTA_NIF, BOLHAO_NIF, MARINHEIRO_NIF, HAZEL_NIF):
        assert validate_nif(nif).valid, nif

    invoice = invoice_page()
    (HERE / "ft-fba2026-1207.jpg").write_bytes(photograph(
        invoice, canvas=(1500, 2000), corners=[(170, 140), (1350, 190), (1395, 1830), (120, 1860)], seed=7))

    cafe = thermal_receipt(BOLHAO_LINES, BOLHAO_QR)
    cw, ch = cafe.size
    scale = 1.5
    (HERE / "fs-pb2026-0441.jpg").write_bytes(photograph(
        cafe, canvas=(1200, int(ch * scale) + 200),
        corners=[(150, 90), (150 + cw * scale, 110), (150 + cw * scale + 10, 110 + ch * scale),
                 (140, 95 + ch * scale)], seed=11, light=0.22))

    marinheiro = thermal_receipt(MARINHEIRO_LINES, None, width=640)
    mw, mh = marinheiro.size
    corners = rotated_corners(600, 160 + mh * 0.75, mw * 1.5, mh * 1.5, 4.0)
    canvas = (1200, int(mh * 1.5) + 330)
    (HERE / "fs-mr2026-0088.jpg").write_bytes(photograph(marinheiro, canvas=canvas, corners=corners, seed=23,
                                                         blur=1.3, noise=7.0))
    (HERE / "fs-mr2026-0088-blurred.jpg").write_bytes(photograph(marinheiro, canvas=canvas, corners=corners,
                                                                 seed=29, blur=9.0, noise=7.0))

    printed = {
        "ft-fba2026-1207.txt": [t for t, s in BOAVISTA_HEADER if s != "gap"]
        + ["  ".join(r) for r in BOAVISTA_ITEMS] + BOAVISTA_TOTALS,
        "fs-pb2026-0441.txt": BOLHAO_LINES,
        "fs-mr2026-0088.txt": MARINHEIRO_LINES,
    }
    for name, lines in printed.items():
        (HERE / name).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("written:", ", ".join(sorted(p.name for p in HERE.iterdir() if p.suffix in (".jpg", ".txt"))))


if __name__ == "__main__":
    main()
