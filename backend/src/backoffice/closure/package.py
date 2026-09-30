"""Accountant package (§27 Day 0 / Day +1, §28, §54–55).

One ZIP per company and month, built in memory and byte-for-byte
reproducible from the same inputs:

* ``manifest.json`` — company, period, every item with its status, quality,
  matched documents, "Why?" lines and evidence hashes, open questions, totals,
  and the SHA-256 of every other file in the package;
* ``ledger.csv`` — one row per item and matched document (UTF-8; the
  ``PT_EXCEL`` format uses ``;``, decimal commas and a BOM so Portuguese Excel
  opens it correctly). Each document's own currency has a column, and credit
  notes are negative so column sums are right; the manifest keeps the values as
  printed on the document, with its type;
* ``evidence_index.csv`` — every piece of evidence with its hash and where it is.
  Links are shared without passwords, query strings or fragments, which often
  hold sign-in tokens (§52);
* ``evidence/…`` — the untouched originals, when a reader is supplied. Their
  bytes are hashed again on the way in; an original that no longer matches
  its recorded hash is refused rather than shipped (§52, §55).

Delivery is tracked separately (:class:`PackageDelivery`): *delivered* is not
*confirmed*. Day +1 completes only when the accountant has confirmed receipt
of this exact package (by hash).
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import unicodedata
import zipfile
import zlib
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlsplit, urlunsplit

from backoffice.domain.lifecycle import TERMINAL, Stage, TrackedItem
from backoffice.domain.models import (
    SUPPORTING_DOCUMENT_TYPES,
    Document,
    DocumentType,
    Evidence,
    EvidenceFormat,
    LegalEntity,
    Quality,
    Transaction,
)

from ._serialize import to_jsonable
from ._text import cents, require_aware, require_money
from .period import Month

__all__ = [
    "MANIFEST_SCHEMA",
    "PT_EXCEL",
    "STANDARD_CSV",
    "AccountantPackage",
    "CsvFormat",
    "DeliveryChannel",
    "DeliveryError",
    "DeliveryState",
    "DocumentRef",
    "EvidenceIntegrityError",
    "EvidenceReader",
    "ObjectStoreLike",
    "OpenQuestion",
    "PackageDelivery",
    "PackageEntry",
    "StoredEvidenceReader",
    "build_package",
    "verify_package",
]

MANIFEST_SCHEMA = "backoffice.accountant-package/1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)


# =========================================================================== inputs


@runtime_checkable
class EvidenceReader(Protocol):
    """Reads an original's bytes; None when there is no stored file (e.g. a bank record)."""

    def read(self, evidence: Evidence) -> bytes | None: ...


@runtime_checkable
class ObjectStoreLike(Protocol):
    """The read side of an object store, e.g. the evidence module's S3 or local store."""

    def get(self, key: str) -> bytes: ...


class StoredEvidenceReader:
    """:class:`EvidenceReader` over an object store, keyed by ``Evidence.storage_key``.

    The integration layer passes the production store (S3-compatible, EU,
    versioned); tests pass an in-memory one. Store errors propagate: a package
    is never built with an original silently left out.
    """

    def __init__(self, store: ObjectStoreLike) -> None:
        self._store = store

    def read(self, evidence: Evidence) -> bytes | None:
        if not evidence.storage_key:
            return None
        return self._store.get(evidence.storage_key)


class EvidenceIntegrityError(ValueError):
    """An original's bytes no longer match the hash recorded when it was collected."""


@dataclass(frozen=True, slots=True)
class CsvFormat:
    delimiter: str = ","
    decimal_comma: bool = False
    bom: bool = False

    def __post_init__(self) -> None:
        if self.delimiter not in (",", ";", "\t"):
            raise ValueError("delimiter must be ',', ';' or a tab")
        if self.decimal_comma and self.delimiter == ",":
            raise ValueError("decimal commas need a ';' or tab delimiter")


STANDARD_CSV = CsvFormat()
PT_EXCEL = CsvFormat(delimiter=";", decimal_comma=True, bom=True)


@dataclass(frozen=True, slots=True)
class DocumentRef:
    """The parts of a matched document an accountant books from.

    ``supporting``: a pro-forma, quote, delivery note or order kept as supporting evidence. It is marked as such
    in the ledger ("document_role") and the manifest and is never booked (§3).
    """

    document_id: str
    doc_type: DocumentType
    quality: Quality
    currency: str = "EUR"
    supplier_name: str | None = None
    supplier_tax_id: str | None = None
    invoice_number: str | None = None
    issue_date: date | None = None
    net_amount: Decimal | None = None
    vat_amount: Decimal | None = None
    gross_amount: Decimal | None = None
    supporting: bool = False

    def __post_init__(self) -> None:
        for name in ("net_amount", "vat_amount", "gross_amount"):
            value = getattr(self, name)
            if value is not None:
                require_money(value, name)

    @property
    def role(self) -> str:
        return "supporting" if self.supporting else "booked"

    @classmethod
    def from_document(cls, doc: Document) -> DocumentRef:
        return cls(
            document_id=doc.id,
            doc_type=doc.doc_type,
            quality=doc.quality,
            currency=doc.currency,
            supplier_name=doc.supplier_name,
            supplier_tax_id=doc.supplier_tax_id,
            invoice_number=doc.invoice_number,
            issue_date=doc.issue_date,
            net_amount=doc.net_amount,
            vat_amount=doc.vat_amount,
            gross_amount=doc.gross_amount,
            supporting=doc.doc_type in SUPPORTING_DOCUMENT_TYPES,
        )


@dataclass(frozen=True, slots=True)
class PackageEntry:
    """One tracked item as the accountant sees it. ``amount`` is signed (negative = money out)."""

    item_id: str
    subject_type: str
    subject_id: str
    booked_on: date
    description: str
    stage: Stage
    quality: Quality
    amount: Decimal | None = None
    currency: str = "EUR"
    documents: tuple[DocumentRef, ...] = ()
    evidence: tuple[Evidence, ...] = ()
    why: tuple[str, ...] = ()
    category: str | None = None
    # Who the item belongs to, when known; build_package refuses anyone else's (§51–52).
    tenant_id: str | None = None
    entity_id: str | None = None

    def __post_init__(self) -> None:
        if self.amount is not None:
            require_money(self.amount, "amount")
        for ev in self.evidence:
            if not _SHA256.match(ev.sha256):
                raise ValueError(f"evidence {ev.id} has no valid sha256")

    @property
    def done(self) -> bool:
        """Closed or not required, with verified evidence (§3, §57)."""
        return self.stage in TERMINAL and self.quality is Quality.GREEN

    @property
    def status(self) -> str:
        if self.done:
            return "closed" if self.stage is Stage.CLOSED else "not_required"
        if self.stage is Stage.CONFLICT or self.quality is Quality.RED:
            return "conflict"
        if self.stage is Stage.NEEDS_OWNER:
            return "waiting_for_owner"
        return "open"

    @classmethod
    def from_domain(
        cls,
        item: TrackedItem,
        *,
        transaction: Transaction | None = None,
        documents: Sequence[Document] = (),
        evidence: Sequence[Evidence] = (),
        why: Sequence[str] = (),
        category: str | None = None,
        booked_on: date | None = None,
        description: str | None = None,
    ) -> PackageEntry:
        """Build from domain objects, checking they belong together (same item, same tenant)."""
        if transaction is not None and item.subject_type == "transaction" and item.subject_id != transaction.id:
            raise ValueError("transaction does not belong to this tracked item")
        tenants = {o.tenant_id for o in (transaction, *documents, *evidence) if o is not None}
        if tenants - {item.tenant_id}:
            raise ValueError("an entry cannot mix data from different tenants")
        if item.subject_type == "document" and documents and item.subject_id not in {d.id for d in documents}:
            raise ValueError("documents do not include this tracked item's document")
        day = booked_on or (transaction.booked_on if transaction else None) or _first_issue_date(documents)
        if day is None:
            raise ValueError("booked_on is required when there is no transaction or dated document")
        return cls(
            item_id=item.id,
            subject_type=item.subject_type,
            subject_id=item.subject_id,
            booked_on=day,
            description=description or _describe(transaction, documents),
            stage=item.stage,
            quality=item.quality,
            amount=transaction.amount if transaction else None,
            currency=transaction.currency if transaction else (documents[0].currency if documents else "EUR"),
            documents=tuple(DocumentRef.from_document(d) for d in documents),
            evidence=tuple(sorted(evidence, key=lambda e: e.id)),
            why=tuple(why),
            category=category,
            tenant_id=item.tenant_id,
            entity_id=_entity_of(transaction, documents),
        )


def _entity_of(transaction: Transaction | None, documents: Sequence[Document]) -> str | None:
    """The company the payment belongs to, else the one company all documents name."""
    if transaction is not None and transaction.entity_id:
        return transaction.entity_id
    named = {d.entity_id for d in documents if d.entity_id}
    return named.pop() if len(named) == 1 else None


def require_same_company(entries: Sequence[PackageEntry], entity: LegalEntity) -> None:
    """Refuse entries known to belong to another tenant or company (§51–52)."""
    for e in entries:
        if e.tenant_id not in (None, entity.tenant_id):
            raise ValueError(f"item {e.item_id} belongs to another tenant")
        if e.entity_id not in (None, entity.id):
            raise ValueError(f"item {e.item_id} belongs to another company")


def _first_issue_date(documents: Sequence[Document]) -> date | None:
    dates = [d.issue_date for d in documents if d.issue_date is not None]
    return min(dates) if dates else None


def _describe(transaction: Transaction | None, documents: Sequence[Document]) -> str:
    if transaction is not None:
        extra = transaction.description.strip()
        return f"{transaction.counterparty} — {extra}" if extra and extra != transaction.counterparty else transaction.counterparty
    for d in documents:
        if d.supplier_name:
            return f"{d.supplier_name} {d.invoice_number}".strip() if d.invoice_number else d.supplier_name
    return "Document"


@dataclass(frozen=True, slots=True)
class OpenQuestion:
    question_id: str
    text: str
    item_id: str | None = None
    asked_by: str = "accountant"
    asked_at: datetime | None = None


# =========================================================================== CSV


_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def _text_cell(value: str | None) -> str:
    """Neutralise spreadsheet formulas in text cells (CSV injection)."""
    if not value:
        return ""
    return f"'{value}" if value.startswith(_FORMULA_START) else value


def _money_cell(value: Decimal | None, fmt: CsvFormat) -> str:
    if value is None:
        return ""
    text = f"{cents(value):f}"
    return text.replace(".", ",") if fmt.decimal_comma else text


def _write_csv(header: Sequence[str], rows: Sequence[Sequence[str]], fmt: CsvFormat) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=fmt.delimiter, lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(header)
    writer.writerows(rows)
    data = buffer.getvalue().encode("utf-8")
    return (b"\xef\xbb\xbf" + data) if fmt.bom else data


_LEDGER_HEADER = (
    "date", "description", "amount", "currency", "status", "quality", "category",
    "supplier", "supplier_tax_id", "document_type", "document_number", "document_date",
    "net", "vat", "gross", "document_currency", "document_role", "why", "evidence_sha256", "item_id",
)  # fmt: skip


def _ledger_rows(entries: Sequence[PackageEntry], fmt: CsvFormat) -> list[list[str]]:
    """One row per matched document; the payment amount only on the first row so sums stay right."""
    rows = []
    for e in entries:
        head = [e.booked_on.isoformat(), _text_cell(e.description)]
        tail = [
            _text_cell(" | ".join(e.why)),
            " ".join(ev.sha256 for ev in e.evidence),
            e.item_id,
        ]
        docs: Sequence[DocumentRef | None] = e.documents or (None,)
        for i, d in enumerate(docs):
            amount = _money_cell(e.amount, fmt) if i == 0 else ""
            rows.append([
                *head, amount, e.currency, e.status, e.quality.value, _text_cell(e.category),
                *_document_cells(d, fmt), *tail,
            ])  # fmt: skip
    return rows


def _booked(value: Decimal | None, doc_type: DocumentType) -> Decimal | None:
    """A credit note reduces spend: its amounts are negative in the ledger, however printed."""
    if value is None or doc_type is not DocumentType.CREDIT_NOTE:
        return value
    return -abs(value)


def _document_cells(d: DocumentRef | None, fmt: CsvFormat) -> list[str]:
    if d is None:
        return [""] * 10
    return [
        _text_cell(d.supplier_name),
        _text_cell(d.supplier_tax_id),
        d.doc_type.value,
        _text_cell(d.invoice_number),
        d.issue_date.isoformat() if d.issue_date else "",
        _money_cell(_booked(d.net_amount, d.doc_type), fmt),
        _money_cell(_booked(d.vat_amount, d.doc_type), fmt),
        _money_cell(_booked(d.gross_amount, d.doc_type), fmt),
        d.currency,
        d.role,
    ]


# =========================================================================== evidence files


_FORMAT_EXT = {
    EvidenceFormat.PDF: ".pdf", EvidenceFormat.IMAGE: ".jpg", EvidenceFormat.SCREENSHOT: ".png",
    EvidenceFormat.EMAIL: ".eml", EvidenceFormat.EML: ".eml", EvidenceFormat.HTML: ".html",
    EvidenceFormat.XML: ".xml", EvidenceFormat.UBL: ".xml", EvidenceFormat.SAFT: ".xml",
    EvidenceFormat.JSON: ".json", EvidenceFormat.CSV: ".csv", EvidenceFormat.XLSX: ".xlsx",
    EvidenceFormat.ZIP: ".zip", EvidenceFormat.TEXT: ".txt",
}  # fmt: skip


def _safe_name(name: str, limit: int = 80) -> str:
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", ascii_name).strip("._")
    return cleaned[-limit:] if cleaned else ""


def _evidence_path(ev: Evidence) -> str:
    base = _safe_name(ev.filename or "")
    if not base:
        base = "original" + _FORMAT_EXT.get(ev.format, ".bin")
    return f"evidence/{ev.sha256[:16]}_{base}"


def _collect_evidence(
    entries: Sequence[PackageEntry], tenant_id: str
) -> tuple[dict[str, Evidence], dict[str, list[str]]]:
    """Unique evidence by id (sorted), with the items that use each piece. One tenant only (§52)."""
    by_id: dict[str, Evidence] = {}
    users: dict[str, list[str]] = defaultdict(list)
    for e in entries:
        for ev in e.evidence:
            if ev.tenant_id != tenant_id:
                raise ValueError(f"evidence {ev.id} belongs to another tenant")
            known = by_id.get(ev.id)
            if known is not None and known.sha256 != ev.sha256:
                raise EvidenceIntegrityError(f"evidence {ev.id} appears with two different hashes")
            by_id[ev.id] = ev
            users[ev.id].append(e.item_id)
    return dict(sorted(by_id.items())), {k: sorted(set(v)) for k, v in users.items()}


def _read_originals(
    evidence: dict[str, Evidence], reader: EvidenceReader | None
) -> dict[str, tuple[str, bytes]]:
    """sha256 -> (path, bytes); each distinct original once, re-hashed on the way in."""
    files: dict[str, tuple[str, bytes]] = {}
    if reader is None:
        return files
    for ev in evidence.values():
        if ev.sha256 in files:
            continue
        data = reader.read(ev)
        if data is None:
            continue  # nothing stored (e.g. a bank record): listed in the index only
        if hashlib.sha256(data).hexdigest() != ev.sha256:
            raise EvidenceIntegrityError(f"evidence {ev.id} no longer matches its recorded hash")
        files[ev.sha256] = (_evidence_path(ev), data)
    return files


def _public_url(url: str | None) -> str | None:
    """Where the original came from, without credentials, query or fragment (§52).

    Download links often carry sign-in tokens ("?token=…", "user:pass@"); the
    accountant gets the file itself, so only the plain web address is shared.
    Anything that is not an http(s) address is left out.
    """
    if not url:
        return None
    try:
        parts = urlsplit(url.strip())
        host = parts.hostname
        port = parts.port
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not host:
        return None
    netloc = f"[{host}]" if ":" in host else host
    if port is not None:
        netloc += f":{port}"
    return urlunsplit((parts.scheme.lower(), netloc, parts.path, "", ""))


def _index_rows(
    evidence: dict[str, Evidence], users: dict[str, list[str]], files: dict[str, tuple[str, bytes]]
) -> list[list[str]]:
    rows = []
    for ev in evidence.values():
        path = files[ev.sha256][0] if ev.sha256 in files else ""
        rows.append([
            ev.id, ev.sha256, _text_cell(ev.filename), ev.format.value, ev.source_kind.value,
            ev.retrieved_at.isoformat(), _text_cell(_public_url(ev.original_url)), path, " ".join(users[ev.id]),
        ])  # fmt: skip
    return rows


_INDEX_HEADER = (
    "evidence_id", "sha256", "filename", "format", "source", "retrieved_at", "original_url",
    "file_in_package", "item_ids",
)  # fmt: skip


# =========================================================================== manifest


def _entry_json(e: PackageEntry) -> dict[str, Any]:
    return {
        "item_id": e.item_id,
        "subject_type": e.subject_type,
        "subject_id": e.subject_id,
        "booked_on": e.booked_on,
        "description": e.description,
        "amount": None if e.amount is None else cents(e.amount),
        "currency": e.currency,
        "status": e.status,
        "quality": e.quality.value,
        "category": e.category,
        "documents": [
            {
                "document_id": d.document_id,
                "type": d.doc_type.value,
                "quality": d.quality.value,
                "supplier_name": d.supplier_name,
                "supplier_tax_id": d.supplier_tax_id,
                "invoice_number": d.invoice_number,
                "issue_date": d.issue_date,
                "currency": d.currency,
                "net": None if d.net_amount is None else cents(d.net_amount),
                "vat": None if d.vat_amount is None else cents(d.vat_amount),
                "gross": None if d.gross_amount is None else cents(d.gross_amount),
                "role": d.role,
            }
            for d in e.documents
        ],
        "why": list(e.why),
        "evidence": [{"id": ev.id, "sha256": ev.sha256} for ev in e.evidence],
    }


def _totals(entries: Sequence[PackageEntry]) -> dict[str, dict[str, Decimal]]:
    totals: dict[str, dict[str, Decimal]] = {}
    for e in entries:
        if e.amount is None:
            continue
        t = totals.setdefault(e.currency, {"money_in": Decimal("0.00"), "money_out": Decimal("0.00")})
        key = "money_in" if e.amount > 0 else "money_out"
        t[key] = cents(t[key] + abs(e.amount))
    return dict(sorted(totals.items()))


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# =========================================================================== package


@dataclass(frozen=True, slots=True)
class AccountantPackage:
    entity_id: str
    month: Month
    filename: str
    data: bytes
    sha256: str
    manifest: dict[str, Any]
    generated_at: datetime

    @property
    def complete(self) -> bool:
        return bool(self.manifest["complete"])

    def delivery(self) -> PackageDelivery:
        """A fresh delivery record for this exact package (state PREPARED)."""
        return PackageDelivery(
            entity_id=self.entity_id,
            month=self.month,
            package_sha256=self.sha256,
            prepared_at=self.generated_at,
        )


def _slug(text: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", ascii_text).strip("-") or "company"


def _zip_time(moment: datetime) -> tuple[int, int, int, int, int, int]:
    utc = moment.astimezone(timezone.utc)
    stamp = (utc.year, utc.month, utc.day, utc.hour, utc.minute, utc.second - utc.second % 2)
    return max(stamp, _ZIP_EPOCH)


def _zip(files: Sequence[tuple[str, bytes]], moment: datetime) -> bytes:
    buffer = io.BytesIO()
    stamp = _zip_time(moment)
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in files:
            info = zipfile.ZipInfo(name, date_time=stamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3  # ZipInfo records the host OS; fix it so bytes match everywhere
            info.external_attr = 0o644 << 16
            archive.writestr(info, data)
    return buffer.getvalue()


def build_package(
    entity: LegalEntity,
    month: Month,
    entries: Sequence[PackageEntry],
    *,
    generated_at: datetime,
    questions: Sequence[OpenQuestion] = (),
    csv_format: CsvFormat = STANDARD_CSV,
    evidence_reader: EvidenceReader | None = None,
    still_open: Sequence[str] = (),
) -> AccountantPackage:
    """Build the month's package for the accountant, entirely in memory (§27 Day 0).

    ``still_open``: plain lines on what is not settled yet, kept in the manifest as the package's honest note
    (a month sent before it closed, §3).
    """
    generated_at = require_aware(generated_at, "generated_at")
    ids = [e.item_id for e in entries]
    if len(ids) != len(set(ids)):
        raise ValueError("each tracked item may appear only once in a package")
    require_same_company(entries, entity)
    ordered = sorted(entries, key=lambda e: (e.booked_on, e.item_id))
    evidence, users = _collect_evidence(ordered, entity.tenant_id)
    originals = _read_originals(evidence, evidence_reader)

    ledger = _write_csv(_LEDGER_HEADER, _ledger_rows(ordered, csv_format), csv_format)
    index = _write_csv(_INDEX_HEADER, _index_rows(evidence, users, originals), csv_format)
    payload: list[tuple[str, bytes]] = [("ledger.csv", ledger), ("evidence_index.csv", index)]
    payload += sorted(originals.values())

    open_questions = sorted(questions, key=lambda q: q.question_id)
    manifest: dict[str, Any] = to_jsonable({
        "schema": MANIFEST_SCHEMA,
        "entity": {"id": entity.id, "name": entity.name, "tax_id": entity.tax_id, "country": entity.country},
        "period": {"month": str(month), "first_day": month.first_day, "last_day": month.last_day},
        "generated_at": generated_at,
        "complete": all(e.done for e in ordered) and not open_questions,
        "counts": {
            "items": len(ordered),
            "done": sum(1 for e in ordered if e.done),
            "open": sum(1 for e in ordered if not e.done),
            "evidence": len(evidence),
            "evidence_files": len(originals),
            "open_questions": len(open_questions),
        },
        "totals": _totals(ordered),
        "items": [_entry_json(e) for e in ordered],
        "open_questions": [
            {"id": q.question_id, "text": q.text, "item_id": q.item_id, "asked_by": q.asked_by, "asked_at": q.asked_at}
            for q in open_questions
        ],
        **({"still_open": list(still_open)} if still_open else {}),
        "evidence": [
            {
                "id": ev.id,
                "sha256": ev.sha256,
                "filename": ev.filename,
                "format": ev.format.value,
                "source": ev.source_kind.value,
                "retrieved_at": ev.retrieved_at,
                "original_url": _public_url(ev.original_url),
                "path": originals[ev.sha256][0] if ev.sha256 in originals else None,
                "item_ids": users[ev.id],
            }
            for ev in evidence.values()
        ],
        "files": {name: _sha(data) for name, data in payload},
    })
    manifest_bytes = (json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    data = _zip([("manifest.json", manifest_bytes), *payload], generated_at)
    return AccountantPackage(
        entity_id=entity.id,
        month=month,
        filename=f"{_slug(entity.name)}-{month}.zip",
        data=data,
        sha256=_sha(data),
        manifest=manifest,
        generated_at=generated_at,
    )


MAX_FILE_BYTES = 512 * 1024 * 1024  # one original; larger is refused, not inflated
MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
_MAX_MANIFEST_BYTES = 64 * 1024 * 1024
_CHUNK = 1024 * 1024
# RuntimeError also covers encrypted entries and NotImplementedError (unknown compression).
_UNREADABLE = (
    zipfile.BadZipFile, zipfile.LargeZipFile, zlib.error, EOFError, OSError, RuntimeError,
    ValueError, KeyError, AttributeError, TypeError,
)  # fmt: skip


def _hash_entry(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> str:
    """SHA-256 of one entry, streamed: never more than its declared size is inflated."""
    digest = hashlib.sha256()
    with archive.open(info) as stream:
        while chunk := stream.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _size_problems(infos: Sequence[zipfile.ZipInfo], max_file_bytes: int, max_total_bytes: int) -> list[str]:
    """Checked on declared sizes before anything is decompressed (zip bombs)."""
    too_big = [f"{i.filename} is too large to check" for i in infos if i.file_size > max_file_bytes]
    if too_big:
        return too_big
    if sum(i.file_size for i in infos) > max_total_bytes:
        return ["the package is too large to check"]
    return []


def verify_package(
    data: bytes, *, max_file_bytes: int = MAX_FILE_BYTES, max_total_bytes: int = MAX_TOTAL_BYTES
) -> list[str]:
    """Developer-facing integrity check: every listed file present once and matching its hash.

    Safe on untrusted input: sizes are checked before inflating, entries are
    hashed as streams, duplicate names are reported (an extractor may pick
    either copy), and any unreadable archive yields a problem, not an exception.
    """
    problems: list[str] = []
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
            dupes = sorted(n for n, c in Counter(i.filename for i in infos).items() if c > 1)
            if dupes:
                return [f"{name} appears more than once" for name in dupes]
            by_name = {i.filename: i for i in infos}
            if "manifest.json" not in by_name:
                return ["manifest.json is missing"]
            if by_name["manifest.json"].file_size > _MAX_MANIFEST_BYTES:
                return ["manifest.json is too large to check"]
            oversized = _size_problems(infos, max_file_bytes, max_total_bytes)
            if oversized:
                return oversized
            manifest = json.loads(archive.read("manifest.json"))
            if not isinstance(manifest, dict) or not isinstance(manifest.get("files", {}), dict):
                return ["manifest.json is not a package manifest"]
            listed: dict[str, str] = manifest.get("files", {})
            for name, digest in sorted(listed.items()):
                if name not in by_name:
                    problems.append(f"{name} is missing")
                elif _hash_entry(archive, by_name[name]) != digest:
                    problems.append(f"{name} does not match its hash")
            for name in sorted(set(by_name) - set(listed) - {"manifest.json"}):
                problems.append(f"{name} is not listed in the manifest")
            for ev in manifest.get("evidence", []):
                path = ev.get("path")
                if path and listed.get(path) != ev.get("sha256"):
                    problems.append(f"{path} is not the original it claims to be")
    except _UNREADABLE as exc:
        problems.append(f"unreadable package: {type(exc).__name__}")
    return problems


# =========================================================================== delivery (Day +1)


class DeliveryState(str, Enum):
    PREPARED = "prepared"
    DELIVERED = "delivered"
    CONFIRMED = "confirmed"  # the accountant confirmed they have it


class DeliveryChannel(str, Enum):
    EMAIL = "email"
    ACCOUNTING_SOFTWARE = "accounting_software"
    SHARED_FOLDER = "shared_folder"
    DOWNLOAD = "download"


class DeliveryError(ValueError):
    """An impossible delivery step (developer-facing)."""


_DELIVERY_LINES = {
    DeliveryState.PREPARED: "Ready for your accountant.",
    DeliveryState.DELIVERED: "Sent to your accountant.",
    DeliveryState.CONFIRMED: "Your accountant has it.",
}


def _evidence_id(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DeliveryError("every delivery step needs evidence")
    return value


@dataclass(frozen=True, slots=True)
class PackageDelivery:
    """Delivery of one exact package. Steps need evidence and move forward in time."""

    entity_id: str
    month: Month
    package_sha256: str
    prepared_at: datetime
    state: DeliveryState = DeliveryState.PREPARED
    channel: DeliveryChannel | None = None
    recipient: str | None = None
    delivered_at: datetime | None = None
    delivery_evidence_id: str | None = None
    confirmed_at: datetime | None = None
    confirmation_evidence_id: str | None = None
    confirmed_by: str | None = None

    def __post_init__(self) -> None:
        require_aware(self.prepared_at, "prepared_at")
        if not _SHA256.match(self.package_sha256):
            raise DeliveryError("package_sha256 must be a SHA-256 hex digest")

    @property
    def owner_line(self) -> str:
        return _DELIVERY_LINES[self.state]

    @property
    def confirmed(self) -> bool:
        return self.state is DeliveryState.CONFIRMED

    def deliver(
        self, *, at: datetime, channel: DeliveryChannel, recipient: str, evidence_id: str
    ) -> PackageDelivery:
        """Record sending (or re-sending). Refused once the accountant has confirmed."""
        at = require_aware(at, "at")
        if self.state is DeliveryState.CONFIRMED:
            raise DeliveryError("already confirmed by the accountant")
        if at < self.prepared_at or (self.delivered_at is not None and at < self.delivered_at):
            raise DeliveryError("delivery cannot go back in time")
        if not recipient.strip():
            raise DeliveryError("recipient is required")
        return replace(
            self,
            state=DeliveryState.DELIVERED,
            channel=channel,
            recipient=recipient,
            delivered_at=at,
            delivery_evidence_id=_evidence_id(evidence_id),
        )

    def confirm(self, *, at: datetime, evidence_id: str, by: str = "accountant") -> PackageDelivery:
        """Record the accountant's confirmation (reply, download receipt, software import)."""
        at = require_aware(at, "at")
        if self.state is not DeliveryState.DELIVERED or self.delivered_at is None:
            raise DeliveryError("only a delivered package can be confirmed")
        if at < self.delivered_at:
            raise DeliveryError("confirmation cannot come before delivery")
        return replace(
            self,
            state=DeliveryState.CONFIRMED,
            confirmed_at=at,
            confirmation_evidence_id=_evidence_id(evidence_id),
            confirmed_by=by,
        )
