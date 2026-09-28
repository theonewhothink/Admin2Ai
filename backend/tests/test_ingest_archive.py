"""Content sniffing and safe ZIP expansion (§7, §13 Stage 0, §52)."""

from __future__ import annotations

import io
import stat
import zipfile

import pytest

from backoffice.domain.models import EvidenceFormat
from backoffice.evidence.archive import SkipReason, ZipLimits, expand_zip, safe_member_name
from backoffice.evidence.sniff import sniff

PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\n%%EOF\n"
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082"
)


def make_zip(entries, *, method=zipfile.ZIP_DEFLATED) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", method) as zf:
        for entry in entries:
            if isinstance(entry, zipfile.ZipInfo):
                zf.writestr(entry, b"x")
            else:
                name, data = entry
                zf.writestr(name, data)
    return buf.getvalue()


# --------------------------------------------------------------------------- sniffing


@pytest.mark.parametrize(
    ("data", "fmt", "mime"),
    [
        (PDF, EvidenceFormat.PDF, "application/pdf"),
        (b"\r\n\r\n%PDF-1.7 late header", EvidenceFormat.PDF, "application/pdf"),
        (PNG, EvidenceFormat.IMAGE, "image/png"),
        (b"\xff\xd8\xff\xe0" + b"\0" * 20, EvidenceFormat.IMAGE, "image/jpeg"),
        (b"\0\0\0\x18ftypheic" + b"\0" * 20, EvidenceFormat.IMAGE, "image/heic"),
        (b"RIFF\x10\0\0\0WEBPVP8 ", EvidenceFormat.IMAGE, "image/webp"),
        (b"<!DOCTYPE html><html><body>x</body></html>", EvidenceFormat.HTML, "text/html"),
        (b'<?xml version="1.0"?><Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"/>',
         EvidenceFormat.UBL, "application/xml"),
        (b'<?xml version="1.0"?><AuditFile xmlns="urn:OECD:StandardAuditFile-Tax:PT_1.04_01"/>',
         EvidenceFormat.SAFT, "application/xml"),
        (b"\xef\xbb\xbf<?xml version='1.0'?><root/>", EvidenceFormat.XML, "application/xml"),
        (b'{"total": "92.40"}', EvidenceFormat.JSON, "application/json"),
        (b"date;amount\n2026-09-01;92,40\n2026-09-02;10,00\n", EvidenceFormat.CSV, "text/csv"),
        (b"From: a@b.pt\r\nTo: c@d.pt\r\nSubject: hi\r\n\r\nbody", EvidenceFormat.EML, "message/rfc822"),
        (b"Paid Vodafone 92,40 EUR today", EvidenceFormat.TEXT, "text/plain"),
    ],
)
def test_sniff_formats(data, fmt, mime):
    found = sniff(data)
    assert (found.format, found.mime_type) == (fmt, mime)


def test_sniff_zip_versus_xlsx_versus_docx():
    assert sniff(make_zip([("a.pdf", PDF)])).format is EvidenceFormat.ZIP
    stored = make_zip([("a.pdf", PDF)], method=zipfile.ZIP_STORED)
    assert b"%PDF-" in stored[:1024] and sniff(stored).format is EvidenceFormat.ZIP
    xlsx = make_zip([("[Content_Types].xml", b"<x/>"), ("xl/workbook.xml", b"<x/>")])
    assert sniff(xlsx).format is EvidenceFormat.XLSX
    docx = make_zip([("[Content_Types].xml", b"<x/>"), ("word/document.xml", b"<x/>")])
    assert sniff(docx).format is None


def test_sniff_unknown_binary_and_empty():
    assert sniff(bytes(range(256)) * 4).format is None
    assert sniff(b"").format is None


def test_declared_type_mismatch_is_flagged_not_trusted():
    found = sniff(b"<html><body>login</body></html>", declared_type="application/pdf", filename="invoice.pdf")
    assert found.format is EvidenceFormat.HTML
    assert found.mismatch
    assert not sniff(PDF, declared_type="application/octet-stream").mismatch
    assert not sniff(PDF, declared_type="application/pdf; name=x.pdf").mismatch
    assert not sniff(b'<?xml version="1.0"?><r/>', declared_type="text/xml").mismatch


def test_csv_needs_declaration_or_structure():
    assert sniff(b"a,b\n", filename="export.csv").format is EvidenceFormat.CSV
    assert sniff(b"one line only").format is EvidenceFormat.TEXT


# --------------------------------------------------------------------------- names


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("faturas/FT-001.pdf", "faturas/FT-001.pdf"),
        ("./a/./b.pdf", "a/b.pdf"),
        ("dir\\file.pdf", "dir/file.pdf"),
        ("../../etc/passwd", None),
        ("a/../../b", None),
        ("/etc/passwd", None),
        ("C:\\Windows\\x.pdf", None),
        ("a\x00b.pdf", None),
        ("", None),
        ("a/" + "b" * 300, None),
    ],
)
def test_safe_member_name(raw, expected):
    assert safe_member_name(raw) == expected


# --------------------------------------------------------------------------- expansion


def test_expands_members_and_reports_dangerous_ones():
    data = make_zip([
        ("faturas/FT-001.pdf", PDF),
        ("../../evil.pdf", PDF),
        ("__MACOSX/faturas/._FT-001.pdf", b"junk"),
        ("empty.txt", b""),
        ("faturas/", b""),
    ])
    result = expand_zip(data)
    assert [m.path for m in result.members] == ["faturas/FT-001.pdf"]
    assert result.members[0].data == PDF and result.members[0].filename == "FT-001.pdf"
    reasons = {s.reason for s in result.skipped}
    assert reasons == {SkipReason.PATH_TRAVERSAL, SkipReason.JUNK, SkipReason.EMPTY}
    assert not result.complete


def _mark_encrypted(data: bytes, name: bytes) -> bytes:
    """zipfile cannot write encrypted entries: set the flag bit in the central directory."""
    out = bytearray(data)
    pos = out.find(b"PK\x01\x02")
    while pos != -1:
        name_len = int.from_bytes(out[pos + 28 : pos + 30], "little")
        if bytes(out[pos + 46 : pos + 46 + name_len]) == name:
            out[pos + 8] |= 0x1
        pos = out.find(b"PK\x01\x02", pos + 4)
    return bytes(out)


def test_symlink_and_encrypted_members_are_skipped():
    link = zipfile.ZipInfo("link.pdf")
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    data = _mark_encrypted(make_zip([link, ("locked.pdf", PDF + b"l"), ("ok.pdf", PDF)]), b"locked.pdf")
    result = expand_zip(data)
    assert [m.path for m in result.members] == ["ok.pdf"]
    assert {(s.name, s.reason) for s in result.skipped} == {
        ("link.pdf", SkipReason.SYMLINK), ("locked.pdf", SkipReason.ENCRYPTED)}


def test_zip_bomb_is_stopped_by_counting_real_bytes():
    bomb = make_zip([("bomb.txt", b"\0" * (5 * 1024 * 1024))])  # compresses ~1000x
    result = expand_zip(bomb, ZipLimits(max_ratio=100, ratio_floor_bytes=1024 * 1024))
    assert not result.members
    assert result.skipped[0].reason is SkipReason.RATIO


def test_member_and_total_size_caps():
    data = make_zip([(f"f{i}.bin", bytes(range(256)) * 16) for i in range(4)], method=zipfile.ZIP_STORED)
    too_big = expand_zip(data, ZipLimits(max_member_bytes=1024))
    assert {s.reason for s in too_big.skipped} == {SkipReason.TOO_LARGE}
    budget = expand_zip(data, ZipLimits(max_total_bytes=4096 * 2 + 10))
    assert len(budget.members) == 2
    assert {s.reason for s in budget.skipped} == {SkipReason.BUDGET}


def test_lying_header_sizes_do_not_bypass_limits():
    data = bytearray(make_zip([("big.bin", b"A" * 50_000)], method=zipfile.ZIP_STORED))
    # Rewrite the declared uncompressed size in the central directory to 10 bytes.
    cd = data.rfind(b"PK\x01\x02")
    data[cd + 24 : cd + 28] = (10).to_bytes(4, "little")
    result = expand_zip(bytes(data), ZipLimits(max_member_bytes=1000))
    assert not result.members
    assert result.skipped[0].reason in (SkipReason.TOO_LARGE, SkipReason.CORRUPT)


def test_member_count_limit():
    data = make_zip([(f"f{i}.pdf", PDF + bytes([i])) for i in range(5)])
    result = expand_zip(data, ZipLimits(max_members=3))
    assert len(result.members) == 3
    assert [s.reason for s in result.skipped] == [SkipReason.TOO_MANY] * 2


def test_nested_archives_expand_to_a_bounded_depth():
    inner = make_zip([("inner.pdf", PDF)])
    middle = make_zip([("inner.zip", inner), ("mid.pdf", PDF + b"m")])
    outer = make_zip([("middle.zip", middle)])
    result = expand_zip(outer, ZipLimits(max_depth=2))
    assert [m.path for m in result.members] == ["middle.zip/mid.pdf"]
    assert result.skipped[0].name == "middle.zip/inner.zip"
    assert result.skipped[0].reason is SkipReason.NESTED_TOO_DEEP
    deeper = expand_zip(outer, ZipLimits(max_depth=3))
    assert sorted(m.path for m in deeper.members) == ["middle.zip/inner.zip/inner.pdf", "middle.zip/mid.pdf"]
    assert deeper.members[0].depth == 3


def test_corrupt_archive_and_crc_errors():
    assert expand_zip(b"PK\x03\x04 not really").skipped[0].reason is SkipReason.CORRUPT
    data = bytearray(make_zip([("a.pdf", PDF)], method=zipfile.ZIP_STORED))
    idx = data.find(PDF)
    data[idx + 5] ^= 0xFF  # flip a byte inside the stored member
    result = expand_zip(bytes(data))
    assert not result.members and result.skipped[0].reason is SkipReason.CORRUPT
