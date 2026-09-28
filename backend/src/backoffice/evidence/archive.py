"""Safe ZIP expansion (§7: ZIP evidence, §52: untrusted input).

Archives are expanded in memory only; nothing is ever written to disk under a
name taken from the archive, and every member name is still checked for path
traversal because names travel on as filenames and provenance.

Zip-bomb defences do not trust the sizes declared in the archive: bytes are
counted while decompressing, against a per-member cap, a shared total budget
(which also defeats overlapping-entry bombs) and a compression-ratio cap.
The central directory itself is bounded (entry count and bytes, read from
the end record) before :mod:`zipfile` parses it.
Nested archives are expanded to a bounded depth and share the same budget.
Only plain ZIPs are nested archives: an .xlsx, .docx or .ods is also a ZIP
container but is one document, kept whole as a member. Rejected members are
reported, never silently dropped.
"""

from __future__ import annotations

import io
import lzma
import stat
import struct
import zipfile
import zlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import PurePosixPath
from typing import Any

from backoffice.domain.models import Evidence, EvidenceFormat, SourceKind

from .sniff import MAX_ZIP_DIRECTORY_BYTES, sniff, zip_directory_shape
from .store import EvidenceRegistry, Registration

__all__ = [
    "ArchiveExpansion",
    "ArchiveMember",
    "ArchiveSkip",
    "SkipReason",
    "ZipLimits",
    "expand_zip",
    "register_members",
    "safe_member_name",
]

_CHUNK = 64 * 1024
_JUNK_PREFIXES = ("__MACOSX/",)
_JUNK_NAMES = frozenset({".DS_Store", "Thumbs.db", "desktop.ini"})


class SkipReason(str, Enum):
    PATH_TRAVERSAL = "path_traversal"
    ENCRYPTED = "encrypted"
    TOO_LARGE = "too_large"
    RATIO = "compression_ratio"
    BUDGET = "total_budget"
    TOO_MANY = "too_many_members"
    SYMLINK = "symlink"
    NESTED_TOO_DEEP = "nested_too_deep"
    CORRUPT = "corrupt"
    UNSUPPORTED = "unsupported_compression"
    JUNK = "system_file"
    EMPTY = "empty"


@dataclass(frozen=True)
class ZipLimits:
    max_members: int = 500
    max_member_bytes: int = 50 * 1024 * 1024
    max_total_bytes: int = 200 * 1024 * 1024
    max_ratio: int = 100  # decompressed / compressed, checked above ratio_floor
    ratio_floor_bytes: int = 1024 * 1024  # small text files compress well legitimately
    max_depth: int = 2  # an archive inside an archive inside the original
    max_name_length: int = 255
    max_entries: int = 10_000  # directory entries (files, folders, junk) read at all
    max_directory_bytes: int = MAX_ZIP_DIRECTORY_BYTES


@dataclass(frozen=True)
class ArchiveMember:
    path: str  # safe display path, "outer.zip/inner/file.pdf" for nested members
    data: bytes = field(repr=False)
    size: int
    compressed_size: int
    depth: int

    @property
    def sha256(self) -> str:
        return Evidence.hash_bytes(self.data)

    @property
    def filename(self) -> str:
        return PurePosixPath(self.path).name


@dataclass(frozen=True)
class ArchiveSkip:
    name: str  # sanitised for display; never used as a filesystem path
    reason: SkipReason


@dataclass(frozen=True)
class ArchiveExpansion:
    members: tuple[ArchiveMember, ...]
    skipped: tuple[ArchiveSkip, ...]

    @property
    def complete(self) -> bool:
        """True when every real file in the archive was expanded."""
        return all(s.reason in (SkipReason.JUNK, SkipReason.EMPTY) for s in self.skipped)


def safe_member_name(raw: str, max_length: int = 255) -> str | None:
    """Normalised relative POSIX path, or ``None`` when the name is dangerous.

    Rejects absolute paths, drive letters, ``..`` segments, NUL and control
    characters, and over-long names. Backslashes count as separators.
    """
    if not raw or len(raw) > max_length * 4:
        return None
    if any(ord(c) < 32 or c == "\x7f" for c in raw):
        return None
    name = raw.replace("\\", "/")
    if name.startswith("/") or (len(name) > 1 and name[1] == ":"):
        return None
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        return None
    clean = "/".join(parts)
    if len(clean) > max_length or any(len(p.encode("utf-8")) > 255 for p in parts):
        return None
    return clean


def _display(raw: str) -> str:
    cleaned = "".join(c if c.isprintable() else "?" for c in raw)
    return cleaned[:200]


class _Budget:
    def __init__(self, limits: ZipLimits) -> None:
        self.limits = limits
        self.remaining = limits.max_total_bytes
        self.members = 0


class _MemberRejected(Exception):
    def __init__(self, reason: SkipReason) -> None:
        super().__init__(reason.value)
        self.reason = reason


def expand_zip(data: bytes, limits: ZipLimits | None = None) -> ArchiveExpansion:
    """Expand a ZIP archive (and nested ZIPs) into memory, safely."""
    limits = limits or ZipLimits()
    budget = _Budget(limits)
    members: list[ArchiveMember] = []
    skipped: list[ArchiveSkip] = []
    _expand(data, "", 1, budget, members, skipped)
    return ArchiveExpansion(tuple(members), tuple(skipped))


def _expand(
    data: bytes,
    prefix: str,
    depth: int,
    budget: _Budget,
    members: list[ArchiveMember],
    skipped: list[ArchiveSkip],
) -> None:
    label = prefix.rstrip("/") or "archive"
    shape = zip_directory_shape(data)
    limits = budget.limits
    if shape is not None and (shape[0] > limits.max_entries or shape[1] > limits.max_directory_bytes):
        skipped.append(ArchiveSkip(label, SkipReason.TOO_MANY))  # the original is still kept whole
        return
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except NotImplementedError:  # e.g. a "version needed to extract" we do not support
        skipped.append(ArchiveSkip(label, SkipReason.UNSUPPORTED))
        return
    except Exception:  # noqa: BLE001 - zipfile raises many types for hostile directories
        skipped.append(ArchiveSkip(label, SkipReason.CORRUPT))
        return
    with archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            _expand_member(archive, info, prefix, depth, budget, members, skipped)


def _expand_member(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    prefix: str,
    depth: int,
    budget: _Budget,
    members: list[ArchiveMember],
    skipped: list[ArchiveSkip],
) -> None:
    limits = budget.limits
    shown = prefix + _display(info.filename)
    name = safe_member_name(info.filename, limits.max_name_length)
    reason = _precheck(info, name, budget)
    if reason is not None:
        skipped.append(ArchiveSkip(shown, reason))
        return
    assert name is not None
    budget.members += 1
    try:
        payload = _read_bounded(archive, info, budget)
    except _MemberRejected as rejected:
        skipped.append(ArchiveSkip(shown, rejected.reason))
        return
    path = prefix + name
    if not payload:
        skipped.append(ArchiveSkip(path, SkipReason.EMPTY))
        return
    if _is_plain_archive(payload):
        if depth >= limits.max_depth:
            skipped.append(ArchiveSkip(path, SkipReason.NESTED_TOO_DEEP))
            return
        _expand(payload, path + "/", depth + 1, budget, members, skipped)
        return
    members.append(ArchiveMember(path, payload, len(payload), info.compress_size, depth))


def _is_plain_archive(payload: bytes) -> bool:
    """A ZIP to expand, not a ZIP-based document (Office Open XML, OpenDocument)."""
    return payload[:4] == b"PK\x03\x04" and sniff(payload).format is EvidenceFormat.ZIP


def _precheck(info: zipfile.ZipInfo, name: str | None, budget: _Budget) -> SkipReason | None:
    limits = budget.limits
    if name is None:
        return SkipReason.PATH_TRAVERSAL
    if name.startswith(_JUNK_PREFIXES) or PurePosixPath(name).name in _JUNK_NAMES:
        return SkipReason.JUNK
    if budget.members >= limits.max_members:
        return SkipReason.TOO_MANY
    mode = info.external_attr >> 16
    if mode and stat.S_ISLNK(mode):
        return SkipReason.SYMLINK
    if info.flag_bits & 0x1:
        return SkipReason.ENCRYPTED
    if info.file_size > limits.max_member_bytes:
        return SkipReason.TOO_LARGE  # declared size already too big: don't even start
    return None


def _read_bounded(archive: zipfile.ZipFile, info: zipfile.ZipInfo, budget: _Budget) -> bytes:
    """Decompress counting real bytes; the declared sizes are not trusted."""
    limits = budget.limits
    cap = min(limits.max_member_bytes, budget.remaining)
    out = bytearray()
    try:
        with archive.open(info) as stream:
            while chunk := stream.read(_CHUNK):
                out += chunk
                if len(out) > cap:
                    raise _MemberRejected(
                        SkipReason.TOO_LARGE if len(out) > limits.max_member_bytes else SkipReason.BUDGET
                    )
                _check_ratio(len(out), info.compress_size, limits)
    except NotImplementedError:
        raise _MemberRejected(SkipReason.UNSUPPORTED) from None
    except (zipfile.BadZipFile, zlib.error, lzma.LZMAError, EOFError, OSError, ValueError, struct.error):
        raise _MemberRejected(SkipReason.CORRUPT) from None
    finally:
        # Decompression work counts against the budget even when rejected.
        budget.remaining -= len(out)
    return bytes(out)


def _check_ratio(produced: int, compressed: int, limits: ZipLimits) -> None:
    if produced > limits.ratio_floor_bytes and produced > max(compressed, 1) * limits.max_ratio:
        raise _MemberRejected(SkipReason.RATIO)


def register_members(
    registry: EvidenceRegistry,
    expansion: ArchiveExpansion,
    archive: Registration,
    *,
    source_kind: SourceKind,
    at: datetime,
    context: Mapping[str, Any] | None = None,
    original_url: str | None = None,
) -> tuple[list[Registration], list[str]]:
    """Register every readable member of an expanded archive as evidence (§7).

    Each sighting points back at the archive's evidence (§55). Returns the
    registrations and internal codes for what was skipped (the archive
    original keeps those bytes).
    """
    tenant_id = archive.evidence.tenant_id
    skipped = [f"{s.name}:{s.reason.value}" for s in expansion.skipped]
    regs: list[Registration] = []
    for member in expansion.members:
        found = sniff(member.data, filename=member.filename)
        if found.format is None:
            skipped.append(f"{member.path}:unsupported")
            continue
        regs.append(registry.register(
            member.data, tenant_id=tenant_id, source_kind=source_kind, format=found.format,
            mime_type=found.mime_type, filename=member.filename, original_url=original_url, retrieved_at=at,
            context={**(context or {}), "archive_evidence_id": archive.evidence.id, "archive_member": member.path},
        ))
    return regs, skipped
