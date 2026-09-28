"""Duplicate detection (§25 duplicate merging, §26 duplicate invoice with a different IBAN).

Three strengths, strongest first:

* ``EXACT``: the same file bytes (a shared sha256), and nothing read from
  the two documents says they differ. One PDF, email or archive can hold
  several invoices: two documents from one file with different numbers,
  totals or suppliers are two documents, judged by the rules below.
* ``SAME_NUMBER``: same supplier and same invoice number.
* ``NEAR``: same supplier, same total and currency, issued within
  ``near_days`` of each other, but a different (or missing) number.
  Two coffees on the same day look like this, so a near duplicate is never
  merged without asking.

Documents of different kinds (an invoice and its credit note) are never
duplicates of each other. Nothing crosses tenants.

A verdict suggests which document to keep; merging links evidence and
never deletes an original (§55). Differences that matter to fraud review
(bank details, total, date) are returned as signals: deciding fraud is the
fraud engine's job (§26), and a copy whose bank details or total differ is
not offered for merging at all.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum

from backoffice.domain.models import Document, DocumentType, Quality

from .fields import severity
from .normalize import (
    CENT,
    invoice_number_key,
    normalize_currency,
    normalize_iban,
    normalize_invoice_number,
    normalize_tax_id,
)

__all__ = [
    "DEFAULT_NEAR_DAYS",
    "DocumentFingerprint",
    "DuplicateIndex",
    "DuplicateKind",
    "DuplicateSignal",
    "DuplicateVerdict",
    "MergeSuggestion",
    "find_duplicates",
    "supplier_name_key",
]

DEFAULT_NEAR_DAYS = 3

# Legal-form words dropped when comparing supplier names ("Acme, S.A." = "ACME SA").
_LEGAL_FORMS = frozenset(
    "sa lda ltda unipessoal ltd limited plc llc inc corp gmbh ag kg sl slu srl spa sas sarl bv nv".split()
)


def supplier_name_key(name: str | None) -> str | None:
    """Case-, accent-, punctuation- and legal-form-insensitive supplier name."""
    if not name:
        return None
    folded = unicodedata.normalize("NFKD", name.casefold())
    plain = "".join(c for c in folded if not unicodedata.combining(c))
    words = re.sub(r"[^\w\s]", "", plain.replace("&", " and ")).split()
    while words and words[-1] in _LEGAL_FORMS:
        words.pop()
    return " ".join(words) or None


def _as_collection(value: Iterable[str] | str) -> Iterable[str]:
    """One id given as plain text is one id, never its characters."""
    return (value,) if isinstance(value, str) else value


def _money(value: Decimal | int | None) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (Decimal, int)):
        raise TypeError("amounts must be Decimal, never float")
    return abs(Decimal(value)).quantize(CENT)


@dataclass(frozen=True)
class DocumentFingerprint:
    """What duplicate detection needs to know about one document.

    Values are normalized on construction: tax id without country prefix,
    invoice number without spaces, IBAN compact (an invalid IBAN counts as
    absent), amount as a magnitude in cents, sha256 lower-case.
    """

    document_id: str
    tenant_id: str
    sha256s: frozenset[str] = frozenset()
    evidence_ids: tuple[str, ...] = ()
    doc_type: DocumentType = DocumentType.OTHER
    supplier_tax_id: str | None = None
    supplier_name: str | None = None
    invoice_number: str | None = None
    issue_date: date | None = None
    gross_amount: Decimal | None = None
    currency: str | None = None
    iban: str | None = None
    entity_id: str | None = None
    quality: Quality = Quality.AMBER
    received_at: datetime | None = None

    def __post_init__(self) -> None:
        number = normalize_invoice_number(self.invoice_number).value if self.invoice_number else None
        hashes = _as_collection(self.sha256s)
        normalized = {
            "sha256s": frozenset(h.strip().lower() for h in hashes if h and h.strip()),
            "evidence_ids": tuple(_as_collection(self.evidence_ids)),
            "issue_date": self.issue_date.date()
            if isinstance(self.issue_date, datetime)
            else self.issue_date,
            "supplier_tax_id": normalize_tax_id(self.supplier_tax_id).value if self.supplier_tax_id else None,
            "supplier_name": supplier_name_key(self.supplier_name),
            "invoice_number": invoice_number_key(number) if number else None,
            "gross_amount": _money(self.gross_amount),
            "currency": normalize_currency(self.currency).value if self.currency else None,
            "iban": normalize_iban(self.iban).value if self.iban else None,
        }
        for name, value in normalized.items():
            object.__setattr__(self, name, value)
        if self.received_at is not None and self.received_at.utcoffset() is None:
            raise ValueError("received_at must be timezone-aware")

    @classmethod
    def from_document(
        cls, document: Document, *, sha256s: Iterable[str] = (), received_at: datetime | None = None
    ) -> DocumentFingerprint:
        return cls(
            document_id=document.id,
            tenant_id=document.tenant_id,
            sha256s=frozenset(sha256s),
            evidence_ids=tuple(document.evidence_ids),
            doc_type=document.doc_type,
            supplier_tax_id=document.supplier_tax_id,
            supplier_name=document.supplier_name,
            invoice_number=document.invoice_number,
            issue_date=document.issue_date,
            gross_amount=document.gross_amount,
            currency=document.currency,
            iban=document.iban,
            entity_id=document.entity_id,
            quality=document.quality,
            received_at=received_at,
        )


class DuplicateKind(str, Enum):
    EXACT = "exact"
    SAME_NUMBER = "same_number"
    NEAR = "near"


_KIND_ORDER = {DuplicateKind.EXACT: 0, DuplicateKind.SAME_NUMBER: 1, DuplicateKind.NEAR: 2}


class DuplicateSignal(str, Enum):
    """Differences between two copies, handed to the fraud engine (§26)."""

    DIFFERENT_IBAN = "different_iban"
    DIFFERENT_AMOUNT = "different_amount"
    DIFFERENT_DATE = "different_date"


@dataclass(frozen=True)
class MergeSuggestion:
    """Keep ``keep_id``; link ``link_evidence_ids`` to it; mark ``merge_id`` as its duplicate."""

    keep_id: str
    merge_id: str
    link_evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class DuplicateVerdict:
    document_id: str
    duplicate_of: str
    kind: DuplicateKind
    signals: tuple[DuplicateSignal, ...] = ()
    merge: MergeSuggestion | None = None
    auto_merge_safe: bool = False
    reasons: tuple[str, ...] = ()

    @property
    def fraud_signal(self) -> bool:
        """A duplicate whose bank details differ (§26); for the fraud engine to judge."""
        return DuplicateSignal.DIFFERENT_IBAN in self.signals


# --------------------------------------------------------------------------- matching


def _same_supplier(a: DocumentFingerprint, b: DocumentFingerprint) -> bool:
    if a.supplier_tax_id and b.supplier_tax_id:
        return a.supplier_tax_id == b.supplier_tax_id
    return bool(a.supplier_name and a.supplier_name == b.supplier_name)


def _same_kind(a: DocumentFingerprint, b: DocumentFingerprint) -> bool:
    return a.doc_type == b.doc_type or DocumentType.OTHER in (a.doc_type, b.doc_type)


def _differs(x: object, y: object) -> bool:
    return x is not None and y is not None and x != y


def _days_apart(a: DocumentFingerprint, b: DocumentFingerprint) -> int | None:
    if a.issue_date is None or b.issue_date is None:
        return None
    return abs((a.issue_date - b.issue_date).days)


def _distinct(a: DocumentFingerprint, b: DocumentFingerprint) -> bool:
    """Details that tell two documents apart even when they come from one file."""
    return (
        _differs(a.invoice_number, b.invoice_number)
        or _differs(a.supplier_tax_id, b.supplier_tax_id)
        or _differs(a.gross_amount, b.gross_amount)
    )


def _kind(a: DocumentFingerprint, b: DocumentFingerprint, near_days: int) -> DuplicateKind | None:
    if a.sha256s & b.sha256s and not _distinct(a, b):
        return DuplicateKind.EXACT
    if not (_same_kind(a, b) and _same_supplier(a, b)):
        return None
    if a.invoice_number and a.invoice_number == b.invoice_number:
        return DuplicateKind.SAME_NUMBER
    days = _days_apart(a, b)
    same_total = a.gross_amount is not None and a.gross_amount == b.gross_amount and a.currency == b.currency
    if same_total and days is not None and days <= near_days:
        return DuplicateKind.NEAR
    return None


def _signals(
    kind: DuplicateKind, a: DocumentFingerprint, b: DocumentFingerprint
) -> tuple[DuplicateSignal, ...]:
    if kind is DuplicateKind.EXACT:
        return ()  # same bytes: any difference is ours, not the document's
    found = []
    if _differs(a.iban, b.iban):
        found.append(DuplicateSignal.DIFFERENT_IBAN)
    if kind is DuplicateKind.SAME_NUMBER:
        if _differs(a.gross_amount, b.gross_amount) or _differs(a.currency, b.currency):
            found.append(DuplicateSignal.DIFFERENT_AMOUNT)
        if _differs(a.issue_date, b.issue_date):
            found.append(DuplicateSignal.DIFFERENT_DATE)
    return tuple(found)


def _other_company(a: DocumentFingerprint, b: DocumentFingerprint) -> bool:
    return _differs(a.entity_id, b.entity_id)


def _auto_safe(
    kind: DuplicateKind, a: DocumentFingerprint, b: DocumentFingerprint, signals: tuple[DuplicateSignal, ...]
) -> bool:
    if signals or _other_company(a, b):
        return False
    if kind is DuplicateKind.EXACT:
        return True
    if kind is DuplicateKind.SAME_NUMBER:
        identified = bool(a.supplier_tax_id and b.supplier_tax_id)
        same_total = a.gross_amount is not None and a.gross_amount == b.gross_amount
        return identified and same_total and a.currency == b.currency
    return False


_LATEST = datetime.max.replace(tzinfo=timezone.utc)


def _keep_first(fp: DocumentFingerprint) -> tuple[int, datetime, str]:
    """Best evidence first, then the copy received first, then a stable id order."""
    return severity(fp.quality), fp.received_at or _LATEST, fp.document_id


def _merge(a: DocumentFingerprint, b: DocumentFingerprint) -> MergeSuggestion:
    keep, merge = sorted((a, b), key=_keep_first)
    links = tuple(e for e in merge.evidence_ids if e not in keep.evidence_ids)
    return MergeSuggestion(keep_id=keep.document_id, merge_id=merge.document_id, link_evidence_ids=links)


_KIND_REASON = {
    DuplicateKind.EXACT: "This is the same file as one I already have.",
    DuplicateKind.SAME_NUMBER: "It has the same supplier and invoice number as one I already have.",
}
_SIGNAL_REASON = {
    DuplicateSignal.DIFFERENT_IBAN: "The bank details are different from the other copy.",
    DuplicateSignal.DIFFERENT_AMOUNT: "The total is different from the other copy.",
    DuplicateSignal.DIFFERENT_DATE: "The date is different from the other copy.",
}


def _reasons(
    kind: DuplicateKind,
    a: DocumentFingerprint,
    b: DocumentFingerprint,
    signals: tuple[DuplicateSignal, ...],
    safe: bool,
) -> tuple[str, ...]:
    if kind is DuplicateKind.NEAR:
        days = _days_apart(a, b) or 0
        when = "on the same day" if days == 0 else f"{days} day{'s' if days != 1 else ''} apart"
        lines = [f"Same supplier and total as one I already have, {when}, but with a different number."]
    else:
        lines = [_KIND_REASON[kind]]
    lines += [_SIGNAL_REASON[s] for s in signals]
    if _other_company(a, b):
        lines.append("The two copies are filed under different companies.")
    if kind is DuplicateKind.NEAR and not signals:
        lines.append("It may be a separate purchase, so I'll ask before merging.")
    elif not safe and not signals and not _other_company(a, b):
        lines.append("I'll ask before merging.")
    return tuple(lines)


def _verdict(a: DocumentFingerprint, b: DocumentFingerprint, near_days: int) -> DuplicateVerdict | None:
    kind = _kind(a, b, near_days)
    if kind is None:
        return None
    signals = _signals(kind, a, b)
    safe = _auto_safe(kind, a, b, signals)
    content_differs = {DuplicateSignal.DIFFERENT_IBAN, DuplicateSignal.DIFFERENT_AMOUNT} & set(signals)
    return DuplicateVerdict(
        document_id=a.document_id,
        duplicate_of=b.document_id,
        kind=kind,
        signals=signals,
        merge=None if content_differs else _merge(a, b),
        auto_merge_safe=safe,
        reasons=_reasons(kind, a, b, signals, safe),
    )


# --------------------------------------------------------------------------- index


# ("sha", tenant, hash) | ("number", tenant, number) | ("total", tenant, currency, amount)
_IndexKey = tuple[object, ...]


class DuplicateIndex:
    """Known documents, indexed by file hash, supplier number and total."""

    def __init__(
        self, fingerprints: Iterable[DocumentFingerprint] = (), *, near_days: int = DEFAULT_NEAR_DAYS
    ):
        if near_days < 0:
            raise ValueError("near_days cannot be negative")
        self._near_days = near_days
        self._docs: dict[str, DocumentFingerprint] = {}
        self._keys: dict[_IndexKey, set[str]] = {}
        for fp in fingerprints:
            self.add(fp)

    def __len__(self) -> int:
        return len(self._docs)

    @staticmethod
    def _index_keys(fp: DocumentFingerprint) -> list[_IndexKey]:
        keys: list[_IndexKey] = [("sha", fp.tenant_id, h) for h in fp.sha256s]
        if fp.invoice_number:
            keys.append(("number", fp.tenant_id, fp.invoice_number))
        if fp.gross_amount is not None:
            keys.append(("total", fp.tenant_id, fp.currency, fp.gross_amount))
        return keys

    def add(self, fp: DocumentFingerprint) -> None:
        """Add or replace (same ``document_id``) a document."""
        self.remove(fp.document_id)
        self._docs[fp.document_id] = fp
        for key in self._index_keys(fp):
            self._keys.setdefault(key, set()).add(fp.document_id)

    def remove(self, document_id: str) -> None:
        old = self._docs.pop(document_id, None)
        if old is None:
            return
        for key in self._index_keys(old):
            ids = self._keys.get(key, set())
            ids.discard(document_id)
            if not ids:
                self._keys.pop(key, None)

    def check(self, fp: DocumentFingerprint) -> tuple[DuplicateVerdict, ...]:
        """Verdicts for ``fp`` against every known document, strongest first."""
        ids = set().union(*(self._keys.get(k, set()) for k in self._index_keys(fp))) - {fp.document_id}
        verdicts = [v for i in sorted(ids) if (v := _verdict(fp, self._docs[i], self._near_days))]
        return tuple(sorted(verdicts, key=lambda v: (_KIND_ORDER[v.kind], v.duplicate_of)))


def find_duplicates(
    candidate: DocumentFingerprint,
    known: Iterable[DocumentFingerprint],
    *,
    near_days: int = DEFAULT_NEAR_DAYS,
) -> tuple[DuplicateVerdict, ...]:
    """One-off check of ``candidate`` against ``known`` documents."""
    return DuplicateIndex(known, near_days=near_days).check(candidate)
