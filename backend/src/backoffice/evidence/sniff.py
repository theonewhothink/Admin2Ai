"""Content sniffing: what an evidence file really is (§7, §13 Stage 0).

Bytes are trusted over declared types. A ``.pdf`` that is really an HTML page
is recorded as HTML, and the mismatch is kept as a signal (§26 altered or
suspicious documents) rather than silently corrected.
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from dataclasses import dataclass

from backoffice.domain.models import EvidenceFormat

__all__ = ["MAX_ZIP_DIRECTORY_BYTES", "Sniffed", "is_textual", "sniff", "zip_directory_shape"]

_SCAN = 4096  # bytes inspected for markers

_UBL_NS = re.compile(rb"urn:oasis:names:specification:ubl:schema:xsd:(Invoice|CreditNote)-2")
_SAFT_NS = re.compile(rb"urn:OECD:StandardAuditFile-Tax", re.IGNORECASE)
_HTML_START = re.compile(
    rb"<(!doctype\s+html|html|head|body|meta|title|table|div|form|p|a|span|h[1-6]|br|img|input|label|script|style)\b",
    re.IGNORECASE,
)
_HTML_DECLARED = frozenset({"text/html", "application/xhtml+xml"})
_XML_ROOT = re.compile(rb"<[A-Za-z_][\w.:-]*[\s>/]")
_EML_HEADER = re.compile(
    rb"^(Return-Path|Received|From|To|Subject|Date|Message-ID|MIME-Version|Delivered-To"
    rb"|X-[A-Za-z0-9-]+|DKIM-Signature|ARC-[A-Za-z-]+|Authentication-Results|Reply-To"
    rb"|Content-Type|In-Reply-To|References):[ \t]",
    re.IGNORECASE | re.MULTILINE,
)


@dataclass(frozen=True)
class Sniffed:
    """Result of sniffing. ``format`` is ``None`` for unsupported content."""

    format: EvidenceFormat | None
    mime_type: str
    declared_mime_type: str | None = None

    @property
    def mismatch(self) -> bool:
        """True when the declared type disagrees with the bytes (a fraud signal)."""
        declared = _base_type(self.declared_mime_type)
        if declared is None or declared in _GENERIC_DECLARED:
            return False
        return declared != self.mime_type and not _compatible(declared, self.mime_type)


_GENERIC_DECLARED = frozenset(
    {"application/octet-stream", "binary/octet-stream", "application/binary", "application/unknown"}
)
# Declared types that legitimately describe the sniffed type.
_COMPATIBLE = {
    "application/xml": {"text/xml"},
    "text/xml": {"application/xml"},
    "image/jpg": {"image/jpeg"},
    "image/pjpeg": {"image/jpeg"},
    "application/x-pdf": {"application/pdf"},
    "application/x-zip-compressed": {"application/zip"},
    "application/zip": {"application/x-zip-compressed"},
    "text/plain": {"text/csv", "message/rfc822"},
    "application/vnd.ms-excel": {"text/csv"},
    "text/comma-separated-values": {"text/csv"},
    "application/csv": {"text/csv"},
    "application/json": {"text/plain"},
}


def _compatible(declared: str, sniffed: str) -> bool:
    if sniffed in _COMPATIBLE.get(declared, set()):
        return True
    # "application/xml" style declarations for UBL/SAF-T documents.
    return declared.endswith("+xml") and sniffed == "application/xml"


def _base_type(value: str | None) -> str | None:
    if not value:
        return None
    return value.split(";", 1)[0].strip().lower() or None


def _strip_bom(head: bytes) -> bytes:
    for bom in (b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff"):
        if head.startswith(bom):
            return head[len(bom) :]
    return head


def _image_type(head: bytes) -> str | None:
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head.startswith((b"II*\x00", b"MM\x00*")):
        return "image/tiff"
    if head[4:8] == b"ftyp" and head[8:12] in (b"heic", b"heix", b"mif1", b"msf1", b"heim", b"heis"):
        return "image/heic"
    return None


_OPENDOCUMENT = b"application/vnd.oasis.opendocument."
_EOCD = b"PK\x05\x06"
_EOCD64_LOCATOR = b"PK\x06\x07"
_EOCD64 = b"PK\x06\x06"
_EOCD_SEARCH = 22 + 65_535  # the end record plus the longest possible archive comment
MAX_ZIP_DIRECTORY_BYTES = 4 * 1024 * 1024  # larger directories are not parsed at all


def zip_directory_shape(data: bytes) -> tuple[int, int] | None:
    """``(entries, directory bytes)`` from the end-of-central-directory record.

    Read without parsing the directory, so a hostile archive with millions of
    entries can be refused before :mod:`zipfile` builds an object per entry.
    ZIP64 records are followed. ``None`` when no end record is found.
    """
    end = data.rfind(_EOCD, max(0, len(data) - _EOCD_SEARCH))
    if end < 0 or len(data) < end + 22:
        return None
    entries = int.from_bytes(data[end + 10 : end + 12], "little")
    size = int.from_bytes(data[end + 12 : end + 16], "little")
    if entries != 0xFFFF and size != 0xFFFFFFFF:
        return entries, size
    locator = end - 20
    if locator < 0 or data[locator : locator + 4] != _EOCD64_LOCATOR:
        return entries, size  # ZIP64 markers without a ZIP64 record: treat as huge
    offset = int.from_bytes(data[locator + 8 : locator + 16], "little")
    if data[offset : offset + 4] != _EOCD64 or len(data) < offset + 48:
        return entries, size
    return (int.from_bytes(data[offset + 32 : offset + 40], "little"),
            int.from_bytes(data[offset + 40 : offset + 48], "little"))


def _opendocument_type(zf: zipfile.ZipFile) -> str | None:
    """The ``mimetype`` entry of an OpenDocument file (tiny and stored first by the spec)."""
    try:
        info = zf.getinfo("mimetype")
    except KeyError:
        return None
    if info.file_size > 200:
        return None
    try:
        with zf.open(info) as fh:
            value = fh.read(200).strip()
    except Exception:  # noqa: BLE001 - any decoder error means "not an OpenDocument file"
        return None
    return value.decode("ascii", "replace") if value.startswith(_OPENDOCUMENT) else None


def _zip_kind(data: bytes) -> Sniffed:
    """ZIP container: plain archive, an Office Open XML spreadsheet or another document."""
    shape = zip_directory_shape(data)
    if shape is None or shape[1] > MAX_ZIP_DIRECTORY_BYTES:
        return Sniffed(EvidenceFormat.ZIP, "application/zip")  # not opened: expansion decides safely
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = set(zf.namelist()[:2000])
            opendocument = _opendocument_type(zf)
    except Exception:  # noqa: BLE001 - sniffing never raises; expansion reports what is wrong
        return Sniffed(EvidenceFormat.ZIP, "application/zip")
    if opendocument:
        # .ods/.odt: one document, not an archive to expand; not a format we read yet.
        return Sniffed(None, opendocument)
    if "[Content_Types].xml" in names and any(n.startswith("xl/") for n in names):
        return Sniffed(
            EvidenceFormat.XLSX,
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    if "[Content_Types].xml" in names:
        # Word / PowerPoint documents are not an evidence format we read.
        return Sniffed(None, "application/vnd.openxmlformats-officedocument")
    return Sniffed(EvidenceFormat.ZIP, "application/zip")


def _markup_kind(text_head: bytes, declared: str | None) -> Sniffed | None:
    stripped = text_head.lstrip()
    if _HTML_START.match(stripped) or re.match(rb"<!--.*?-->\s*<html", stripped, re.S | re.I):
        return Sniffed(EvidenceFormat.HTML, "text/html")
    if declared in _HTML_DECLARED and stripped.startswith(b"<") and not stripped.startswith(b"<?xml"):
        return Sniffed(EvidenceFormat.HTML, "text/html")  # an HTML fragment served as HTML
    if stripped.startswith(b"<?xml") or _XML_ROOT.match(stripped):
        if _UBL_NS.search(text_head):
            return Sniffed(EvidenceFormat.UBL, "application/xml")
        if _SAFT_NS.search(text_head):
            return Sniffed(EvidenceFormat.SAFT, "application/xml")
        if re.search(rb"<html\b", text_head, re.I):
            return Sniffed(EvidenceFormat.HTML, "text/html")
        return Sniffed(EvidenceFormat.XML, "application/xml")
    return None


def is_textual(data: bytes) -> bool:
    """UTF-8 (or Latin-1-clean) text without binary control bytes."""
    sample = data[:_SCAN]
    if b"\x00" in sample:
        return False
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError as exc:
        # A multi-byte sequence cut at the sample boundary is still text.
        if exc.start < len(sample) - 4:
            return False
    controls = sum(1 for b in sample if b < 32 and b not in (9, 10, 12, 13))
    return controls <= len(sample) // 100


def _looks_like_csv(data: bytes) -> bool:
    lines = [ln for ln in data[:_SCAN].splitlines() if ln.strip()][:6]
    if len(lines) < 2:
        return False
    for sep in (b";", b",", b"\t"):
        counts = {ln.count(sep) for ln in lines[:-1]}  # last line may be truncated
        if len(counts) == 1 and next(iter(counts)) >= 1:
            return True
    return False


def _json_kind(data: bytes) -> bool:
    if len(data) > 32 * 1024 * 1024:
        return False
    try:
        json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError):
        return False
    return True


def sniff(
    data: bytes,
    *,
    declared_type: str | None = None,
    filename: str | None = None,
) -> Sniffed:
    """Identify ``data`` from its bytes; declared type and filename only break ties.

    Returns ``Sniffed(format=None, ...)`` for content we do not ingest (the
    original stays preserved inside its parent, e.g. the .eml).
    """
    declared = _base_type(declared_type)
    result = _sniff_bytes(data, declared, (filename or "").lower())
    return Sniffed(result.format, result.mime_type, declared)


def _sniff_bytes(data: bytes, declared: str | None, name: str) -> Sniffed:
    if not data:
        return Sniffed(None, "application/x-empty")
    head = data[:_SCAN]
    # Exact magic numbers first: a ZIP holding a stored PDF also contains "%PDF-".
    if head.startswith(b"%PDF-"):
        return Sniffed(EvidenceFormat.PDF, "application/pdf")
    image = _image_type(head)
    if image:
        return Sniffed(EvidenceFormat.IMAGE, image)
    if head.startswith((b"PK\x03\x04", b"PK\x05\x06")):
        return _zip_kind(data)
    # PDF readers accept junk before the header within the first 1024 bytes.
    if b"%PDF-" in head[:1024]:
        return Sniffed(EvidenceFormat.PDF, "application/pdf")
    if not is_textual(data):
        return Sniffed(None, declared or "application/octet-stream")
    return _sniff_text(data, _strip_bom(head), declared, name)


def _sniff_text(data: bytes, head: bytes, declared: str | None, name: str) -> Sniffed:
    markup = _markup_kind(head, declared)
    if markup:
        return markup
    if declared == "message/rfc822" or name.endswith(".eml") or _is_eml(head):
        return Sniffed(EvidenceFormat.EML, "message/rfc822")
    stripped = head.lstrip()
    if stripped[:1] in (b"{", b"[") and _json_kind(data):
        return Sniffed(EvidenceFormat.JSON, "application/json")
    if declared in ("text/csv", "application/csv") or name.endswith(".csv") or _looks_like_csv(data):
        return Sniffed(EvidenceFormat.CSV, "text/csv")
    return Sniffed(EvidenceFormat.TEXT, "text/plain")


def _is_eml(head: bytes) -> bool:
    """At least three RFC 5322 header lines before the first blank line."""
    header_block = re.split(rb"\r?\n\r?\n", head, maxsplit=1)[0]
    return len(_EML_HEADER.findall(header_block)) >= 3
