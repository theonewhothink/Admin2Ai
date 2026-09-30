"""Page images redacted before they leave our infrastructure (§52-53).

Only uncertain evidence goes to an external engine, and "unnecessary bank
account numbers, addresses and sensitive unrelated content" are removed
first *whenever feasible*. What is feasible depends on what is installed:

* **Metadata** is always removed from JPEG and PNG images, in pure Python:
  EXIF (camera, time, GPS position), XMP, IPTC, comments and text chunks
  never leave. The pixels are untouched.
* **Personal details are masked** on page images when a local engine has
  already read the page with line boxes (``OCRHints.prior_pages``) and
  ``Pillow`` is installed: every line in which :func:`~backoffice.policy.privacy.find_pii`
  finds a bank account, IBAN, card number, e-mail address, phone number or
  postal address is painted over. Amounts, dates, tax numbers and invoice
  numbers are not personal details and stay readable.
* **PDFs** are rendered to page images with the optional ``pypdfium2``
  (plus ``Pillow``), so the same masking applies and no document metadata,
  attachment or hidden layer travels with them. Without a renderer the PDF
  is sent as it is and the step says so (``pdf_not_masked``).

Every choice is reported as a stable note on :class:`RedactedPages`, so the
audit log shows exactly what left and how it was reduced (§55).
"""

from __future__ import annotations

import io
import struct
from collections.abc import Callable
from dataclasses import dataclass, field

from backoffice.extraction._optional import MissingDependencyError, import_optional
from backoffice.extraction.media import MIME_JPEG, MIME_PDF, MIME_PNG
from backoffice.extraction.pdf import render_pdf_pages

from .base import OCRHints, OCRPage, PageImage

__all__ = [
    "RedactedPages",
    "VisionRedactor",
    "mask_personal_details",
    "rasterize_pdf",
    "strip_metadata",
    "vision_redactor",
]


@dataclass(frozen=True)
class RedactedPages:
    """What may leave, and stable notes on how it was reduced."""

    pages: tuple[PageImage, ...]
    notes: tuple[str, ...] = field(default=())


VisionRedactor = Callable[[tuple[PageImage, ...], OCRHints], RedactedPages]


# --------------------------------------------------------------------------- metadata

# JPEG segments that carry metadata rather than pixels: APP1 (EXIF / XMP),
# APP13 (Photoshop IPTC) and COM. APP0 (JFIF), APP2 (ICC colour profile) and
# APP14 (Adobe colour transform) stay: dropping them would change the colours.
_JPEG_DROP = frozenset({0xE1, 0xED, 0xFE})
# PNG chunks that carry text, EXIF or a timestamp.
_PNG_DROP = frozenset({b"tEXt", b"zTXt", b"iTXt", b"eXIf", b"tIME"})
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _strip_jpeg(data: bytes) -> bytes | None:
    if not data.startswith(b"\xff\xd8"):
        return None
    out = bytearray(b"\xff\xd8")
    i = 2
    while i + 4 <= len(data):
        if data[i] != 0xFF:
            return None  # not a well-formed segment boundary: leave the image alone
        marker = data[i + 1]
        if marker == 0xFF:  # fill byte
            i += 1
            continue
        if marker == 0xDA or marker == 0xD9:  # start of scan / end of image: pixels follow
            out += data[i:]
            return bytes(out)
        if 0xD0 <= marker <= 0xD7 or marker == 0x01:
            out += data[i : i + 2]
            i += 2
            continue
        (length,) = struct.unpack(">H", data[i + 2 : i + 4])
        end = i + 2 + length
        if length < 2 or end > len(data):
            return None
        if marker not in _JPEG_DROP:
            out += data[i:end]
        i = end
    return None


def _strip_png(data: bytes) -> bytes | None:
    if not data.startswith(_PNG_SIGNATURE):
        return None
    out = bytearray(_PNG_SIGNATURE)
    i = len(_PNG_SIGNATURE)
    while i + 12 <= len(data):
        (length,) = struct.unpack(">I", data[i : i + 4])
        kind = data[i + 4 : i + 8]
        end = i + 12 + length
        if end > len(data):
            return None
        if kind not in _PNG_DROP:
            out += data[i:end]
        i = end
        if kind == b"IEND":
            return bytes(out)
    return None


def strip_metadata(page: PageImage) -> PageImage:
    """The page without EXIF/XMP/IPTC/comments (JPEG) or text/EXIF/time chunks (PNG).

    Other formats, and files whose structure is not what it should be, are
    returned unchanged: this never guesses at bytes it does not understand.
    """
    stripper = {MIME_JPEG: _strip_jpeg, MIME_PNG: _strip_png}.get(page.mime_type)
    cleaned = stripper(page.data) if stripper else None
    if cleaned is None or cleaned == page.data:
        return page
    return PageImage(data=cleaned, mime_type=page.mime_type, number=page.number, width=page.width,
                     height=page.height, page_count=page.page_count)


# --------------------------------------------------------------------------- rendering


def rasterize_pdf(page: PageImage, *, max_pages: int, dpi: int = 150) -> tuple[PageImage, ...]:
    """PNG page images of a PDF (needs ``pypdfium2`` and ``Pillow``); raises MissingDependencyError."""
    return tuple(
        PageImage(data=png, mime_type=MIME_PNG, number=page.number + index, width=width, height=height)
        for index, (png, width, height) in enumerate(render_pdf_pages(page.data, max_pages=max_pages, dpi=dpi))
    )


# --------------------------------------------------------------------------- masking


def _sensitive_boxes(prior: OCRPage) -> list[tuple[float, float, float, float]]:
    from backoffice.policy.privacy import find_pii

    boxes = []
    for line in prior.lines:
        if line.bbox is not None and find_pii(line.text):
            b = line.bbox
            boxes.append((b.x0, b.y0, b.x1, b.y1))
    return boxes


def mask_personal_details(page: PageImage, prior: OCRPage | None) -> tuple[PageImage, int]:
    """``page`` with every line showing personal details painted over, and how many lines.

    Boxes are in the pixel space of the engine that read ``prior``; they are
    scaled to this image when the two sizes differ. Needs ``Pillow``
    (raises MissingDependencyError). Pages without boxed lines are returned
    unchanged with a count of 0.
    """
    if prior is None or page.mime_type not in (MIME_PNG, MIME_JPEG):
        return page, 0
    boxes = _sensitive_boxes(prior)
    if not boxes:
        return page, 0
    image_module = import_optional("PIL.Image", feature="Masking personal details", package="Pillow")
    draw_module = import_optional("PIL.ImageDraw", feature="Masking personal details", package="Pillow")
    with image_module.open(io.BytesIO(page.data)) as opened:
        image = opened.convert("RGB")
    sx = image.width / prior.width if prior.width else 1.0
    sy = image.height / prior.height if prior.height else 1.0
    draw = draw_module.Draw(image)
    pad = 2
    for x0, y0, x1, y1 in boxes:
        draw.rectangle([x0 * sx - pad, y0 * sy - pad, x1 * sx + pad, y1 * sy + pad], fill=(0, 0, 0))
    buffer = io.BytesIO()
    if page.mime_type == MIME_JPEG:
        image.save(buffer, format="JPEG", quality=90)
    else:
        image.save(buffer, format="PNG")
    masked = PageImage(data=buffer.getvalue(), mime_type=page.mime_type, number=page.number,
                       width=image.width, height=image.height)
    return masked, len(boxes)


# --------------------------------------------------------------------------- the redactor


def vision_redactor(*, max_pages: int = 10, dpi: int = 150, render_pdfs: bool = True) -> VisionRedactor:
    """The default redactor for an external vision engine (module docstring)."""

    def redact(pages: tuple[PageImage, ...], hints: OCRHints) -> RedactedPages:
        prior = {p.number: p for p in hints.prior_pages}
        notes: list[str] = []
        images: list[PageImage] = []
        for page in pages:
            if page.mime_type == MIME_PDF:
                if not render_pdfs:
                    notes.append("pdf_not_masked")
                    images.append(page)
                    continue
                try:
                    images.extend(rasterize_pdf(page, max_pages=max_pages, dpi=dpi))
                    notes.append("pdf_rendered")
                except MissingDependencyError:
                    notes.append("pdf_not_masked")
                    images.append(page)
                except Exception:  # a PDF the renderer cannot open is still sent whole, never guessed at
                    notes.append("pdf_not_masked")
                    images.append(page)
                continue
            images.append(page)
        out: list[PageImage] = []
        masked_lines = 0
        masking_missing = False
        for image in images:
            if image.mime_type == MIME_PDF:
                out.append(image)
                continue
            clean = strip_metadata(image)
            if clean is not image:
                notes.append("metadata_removed")
            try:
                clean, count = mask_personal_details(clean, prior.get(image.number))
                masked_lines += count
            except MissingDependencyError:
                masking_missing = True
            out.append(clean)
        if masked_lines:
            notes.append(f"masked_lines:{masked_lines}")
        elif not prior:
            notes.append("no_local_reading_to_mask_from")
        if masking_missing:
            notes.append("masking_unavailable")
        return RedactedPages(pages=tuple(out), notes=tuple(dict.fromkeys(notes)))

    return redact

