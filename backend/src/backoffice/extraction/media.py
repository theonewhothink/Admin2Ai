"""Media-type sniffing from content, never from file names (§7, §13).

Evidence arrives from email, shares and portals with unreliable names and
headers, so the bytes decide what a file is.
"""

from __future__ import annotations

__all__ = [
    "IMAGE_MIME_TYPES",
    "MIME_BMP",
    "MIME_GIF",
    "MIME_HTML",
    "MIME_JPEG",
    "MIME_JSON",
    "MIME_PDF",
    "MIME_PNG",
    "MIME_TEXT",
    "MIME_TIFF",
    "MIME_WEBP",
    "MIME_XML",
    "OCR_INPUT_MIME_TYPES",
    "sniff_mime",
    "sniff_text_format",
]

MIME_PDF = "application/pdf"
MIME_PNG = "image/png"
MIME_JPEG = "image/jpeg"
MIME_TIFF = "image/tiff"
MIME_GIF = "image/gif"
MIME_BMP = "image/bmp"
MIME_WEBP = "image/webp"
MIME_XML = "application/xml"
MIME_HTML = "text/html"
MIME_JSON = "application/json"
MIME_TEXT = "text/plain"

IMAGE_MIME_TYPES: frozenset[str] = frozenset(
    {MIME_PNG, MIME_JPEG, MIME_TIFF, MIME_GIF, MIME_BMP, MIME_WEBP}
)
# What a real OCR engine may receive: page images and whole PDFs.
OCR_INPUT_MIME_TYPES: frozenset[str] = IMAGE_MIME_TYPES | {MIME_PDF}

_BOMS = (b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff")


def sniff_mime(data: bytes) -> str | None:
    """MIME type of a binary document from its magic bytes, or None."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return MIME_PNG
    if data.startswith(b"\xff\xd8\xff"):
        return MIME_JPEG
    if data.startswith((b"II*\x00", b"MM\x00*")):
        return MIME_TIFF
    if data.startswith((b"GIF87a", b"GIF89a")):
        return MIME_GIF
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return MIME_WEBP
    # The PDF header may follow a little junk (allowed by most readers).
    if b"%PDF-" in data[:1024]:
        return MIME_PDF
    if data.startswith(b"BM") and len(data) > 26:
        return MIME_BMP
    return None


def sniff_text_format(data: bytes) -> str | None:
    """MIME type of a text document (XML, HTML or JSON), or None."""
    head = data[:2048]
    for bom in _BOMS:
        if head.startswith(bom):
            head = head[len(bom) :]
            break
    text = head.decode("utf-8", errors="ignore").lstrip().lower()
    if text.startswith(("<!doctype html", "<html")) or "<html" in text[:512]:
        return MIME_HTML
    if text.startswith("<"):
        return MIME_XML
    if text.startswith(("{", "[")):
        return MIME_JSON
    return None
