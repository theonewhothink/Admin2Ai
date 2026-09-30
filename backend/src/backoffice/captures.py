"""Photos from the phone: pages kept together, poor photos retaken, copies never counted twice (§11, §43).

Three things the mobile capture path needs on the server (checklist D2, E10, G3):

* **Multi-page scans stay one document.** The phone uploads each page of one scan separately with the
  same ``capture_id``, its ``page`` and the ``page_count`` (mobile/src/offline/uploader.ts). A page is
  stored at once (the phone may delete its copy) but read as a document only when every page is in:
  all pages are then read together, so the invoice is one document with every page as its evidence. A
  scan whose other pages never arrive is read with the pages it has after a day.
* **A poor photo is escalated, then retaken.** The phone's quality hints and the reader's own estimate
  route a poor image to the stronger engine (backoffice.reading). When even that cannot read it, the
  owner gets one plain task in Needs You ("This photo of a receipt is too blurred to read. Take it
  again?") instead of the photo being stored in silence.
* **A copy is one document.** A photographed invoice and the same invoice as a PDF by email become one
  document with both originals when their numbers match (Orchestrator._duplicate_of). A photo whose
  number could not be read, but whose supplier, date and total match an invoice already on file, is
  merged only when the rules of the company's own country prove it is the same document (its pack's
  unique document code: the same ATCUD for a Portuguese company; Spain has none, §49); otherwise it
  waits and the owner is asked once whether it is the same invoice: it never becomes a second expense
  on a guess (§3, verification.duplicates "near").
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from backoffice.domain.models import EvidenceFormat

if TYPE_CHECKING:  # the orchestrator imports this module
    from backoffice.orchestrator import (
        AnswerOutcome,
        DocumentRecord,
        IngestReport,
        NeedsYouRecord,
        Orchestrator,
        Repository,
    )

__all__ = ["CAPTURE_WAIT", "CaptureAgent", "PendingCapture", "PendingCopy", "document_code", "retake_prompt"]

CAPTURE_WAIT = timedelta(days=1)  # the other pages of a scan did not come: read what arrived

# One calm sentence per problem a new photo would fix (§11, §69), the worst first.
_RETAKE = (
    ("blurry", "This photo of a receipt is too blurred to read. Take it again?"),
    ("glare", "This photo of a receipt has a reflection I can't read through. Take it again away from direct "
              "light?"),
    ("too_dark", "This photo of a receipt is too dark to read. Take it again with more light?"),
    ("overexposed", "This photo of a receipt is too bright to read. Take it again away from direct light?"),
    ("low_resolution", "This photo of a receipt is too small to read. Take it again, a little closer?"),
)


def retake_prompt(flags: Sequence[str]) -> str | None:
    """The owner's task for an unreadable photo, from its worst problem; None when a new photo would not help."""
    return next((text for flag, text in _RETAKE if flag in flags), None)


def document_code(text: str, country: str) -> tuple[str, str] | None:
    """(name, value) of the unique document code ``text`` prints under the rules of ``country`` (the company's
    pack: a Portuguese document's ATCUD), when exactly one valid code is there; None otherwise."""
    from backoffice.countries import CountryPackError, company_pack

    try:
        return company_pack(country).document_code(text or "")
    except CountryPackError:
        return None


@dataclass
class PendingCapture:
    """Pages of one multi-page scan, stored as they arrive and read together once all are in."""

    capture_id: str
    page_count: int
    first_at: datetime
    pages: dict[int, str] = field(default_factory=dict)  # page number -> evidence id
    done: bool = False

    @property
    def complete(self) -> bool:
        return len(self.pages) >= self.page_count

    def evidence_ids(self) -> list[str]:
        return [self.pages[p] for p in sorted(self.pages)]


@dataclass
class PendingCopy:
    """A document read without its number that looks like one already on file: waiting for the owner."""

    needs_id: str
    existing_id: str  # the document it looks like
    parts: list[Any]  # what was read (orchestrator._Part), kept to make it a document if the owner says so
    at: datetime
    origin: str
    retrieved: bool
    options: dict[str, Any]  # the rest of what _document_from_parts was given (sender, message text, ...)
    evidence_ids: tuple[str, ...]
    status: str = "waiting"  # "waiting" | "merged" | "separate"


class CaptureAgent:
    """Multi-page scans, retake requests and copies waiting for the owner (module docstring)."""

    name = "capture"
    NEEDS_KINDS = ("retake", "same_document")

    def __init__(self, orchestrator: Orchestrator) -> None:
        self.o = orchestrator

    @property
    def repo(self) -> Repository:
        return self.o.repo

    def log(self, action: str, **kwargs: Any) -> None:
        self.o.log(self.name, action, **kwargs)

    # ----------------------------------------------------------------- pages of one scan (D2)

    @staticmethod
    def capture_of(evidence: Any) -> dict[str, Any]:
        """What the phone said about this capture (service.capture_fields), as stored with the evidence."""
        meta = evidence.metadata.get("capture") if isinstance(evidence.metadata, dict) else None
        return dict(meta) if isinstance(meta, dict) else {}

    def add_page(self, evidence: Any, at: datetime) -> list[str] | None:
        """Keep one page of a multi-page scan. None: not part of one (read it on its own). Otherwise the pages to
        read together now: every page once the scan is complete, else nothing yet."""
        meta = self.capture_of(evidence)
        cid, page, count = meta.get("id"), meta.get("page"), meta.get("pageCount")
        if not isinstance(cid, str) or not isinstance(page, int) or not isinstance(count, int) or count < 2:
            return None
        pending = self.repo.captures.get(cid)
        if pending is None:
            pending = self.repo.captures[cid] = PendingCapture(capture_id=cid, page_count=count, first_at=at)
        if pending.done:
            return None  # a page resent after the scan was read: read on its own (the same bytes are one evidence)
        pending.pages.setdefault(page, evidence.id)
        self.log("capture_page", subject_id=cid, evidence_ids=[evidence.id],
                 values={"page": page, "page_count": count, "received": len(pending.pages)})
        if not pending.complete:
            return []
        pending.done = True
        return pending.evidence_ids()

    def stale(self, now: datetime) -> list[PendingCapture]:
        """Scans whose other pages never came: read after a day with the pages that arrived."""
        out = []
        for pending in sorted(self.repo.captures.values(), key=lambda p: p.capture_id):
            if not pending.done and pending.pages and now - pending.first_at >= CAPTURE_WAIT:
                pending.done = True
                self.log("capture_incomplete", subject_id=pending.capture_id, evidence_ids=pending.evidence_ids(),
                         values={"received": len(pending.pages), "page_count": pending.page_count})
                out.append(pending)
        return out

    # ----------------------------------------------------------------- poor photos (E10)

    def unreadable(self, evidence_ids: Sequence[str], *, at: datetime, report: IngestReport) -> bool:
        """A photo nothing could be made of, even by the stronger engine: one plain task to take it again (§11).
        Returns True when the owner was asked."""
        repo = self.repo
        flags: list[str] = []
        for evidence_id in evidence_ids:
            try:
                evidence = repo.evidence(evidence_id)
            except Exception:
                continue
            outcome = repo.reads.get(evidence_id)
            if evidence.format is not EvidenceFormat.IMAGE or outcome is None:
                continue
            if outcome.found_anything and not outcome.needs_person:
                continue  # read, but not a document of ours: nothing a new photo would change
            flags += [f for f in outcome.retake_worthy if f not in flags]
        prompt = retake_prompt(flags)
        if prompt is None:
            return False
        first = evidence_ids[0]
        if any(n.kind == "retake" and n.subject_id == first for n in repo.needs.values()):
            return True
        from backoffice.orchestrator import CheckOption, NeedsYouRecord, _unique_id

        needs_id = _unique_id(repo.needs, "nd_retake_photo")
        company = next(iter(repo.companies)) if len(repo.companies) == 1 else ""
        repo.needs[needs_id] = NeedsYouRecord(
            id=needs_id, kind="retake", subject_type="evidence", subject_id=first, item_id="", company_id=company,
            created_at=at, prompt=prompt,
            options=(CheckOption(id="retake", label="I'll take it again"),
                     CheckOption(id="ignore", label="It isn't a receipt. Leave it.")),
            why=("I tried the stronger reader too, and I still can't read the total, the date or the supplier.",
                 "I kept the photo as it is."))
        self.log("ask_retake", subject_id=first, evidence_ids=list(evidence_ids),
                 values={"problems": flags}, response={"needs_you": needs_id})
        report.needs_ids.append(needs_id)
        report.message = f"Got it, but I can't read it. {prompt}"
        return True

    # ----------------------------------------------------------------- copies (G3)

    def near_copy(self, values: dict[str, Any], doc_type: Any, supplier_id: str | None, text: str,
                  sales: bool) -> DocumentRecord | None:
        """An invoice on file this unnumbered document may be a copy of: the same supplier, kind, issue date,
        currency and total. (Two coffees on the same day look like this too: never merged on this alone.)"""
        from backoffice.learning import same_tax_id
        from backoffice.orchestrator import _date, _dec, _text
        from backoffice.verification.duplicates import same_document_kind

        if _text(values.get("invoice_number")) or sales:
            return None
        tax_id = _text(values.get("supplier_tax_id"))
        total, day = _dec(values.get("gross_amount")), _date(values.get("issue_date"))
        currency = _text(values.get("currency")) or "EUR"
        if total is None or day is None or not (tax_id or supplier_id):
            return None
        found = []
        for rec in self.repo.documents.values():
            doc = rec.document
            if rec.sales or doc.gross_amount is None or doc.issue_date != day or doc.currency != currency:
                continue
            if abs(doc.gross_amount) != abs(total) or not same_document_kind(doc.doc_type, doc_type):
                continue
            if tax_id and doc.supplier_tax_id:
                if not same_tax_id(doc.supplier_tax_id, tax_id):
                    continue
            elif not (supplier_id and rec.supplier_id == supplier_id):
                continue
            found.append(rec)
        return min(found, key=lambda r: (r.received_at, r.id)) if found else None

    def proven_copy(self, text: str, existing: DocumentRecord, home: str | None = None) -> str | None:
        """The rule that proves it is the same document ("ATCUD": the same Portuguese unique document code on
        both), read with the pack of the company's own country; None when nothing proves it. ``home`` is the
        country of the company the new copy is for: a copy for a company in another country is never proven."""
        country = existing.country
        if home is not None and home != country:
            return None
        mine, theirs = document_code(text, country), document_code(existing.text, country)
        return mine[0] if mine is not None and mine == theirs else None

    def ask_same(self, existing: DocumentRecord, parts: list[Any], evidence_ids: Sequence[str], *, at: datetime,
                 origin: str, retrieved: bool, options: dict[str, Any], report: IngestReport) -> None:
        """One plain question: is this the invoice already on file? Until answered it is no second expense."""
        from backoffice.learning import day_month, display_name, format_money
        from backoffice.orchestrator import CheckOption, NeedsYouRecord, _unique_id

        repo = self.repo
        doc = existing.document
        who = display_name(doc.supplier_name)
        if any(p.status == "waiting" and set(p.evidence_ids) & set(evidence_ids) for p in repo.pending_copies.values()):
            report.message = f"Got it. I already asked you whether this is the {who} invoice you have."
            return
        number = f" {doc.invoice_number}" if doc.invoice_number else ""
        amount = format_money(abs(doc.gross_amount or Decimal("0")), doc.currency)
        when = day_month(doc.issue_date, repo.today()) if doc.issue_date else "the same day"
        needs_id = _unique_id(repo.needs, f"nd_{existing.id[4:14]}_copy")
        photo = any(repo.evidence(e).format is EvidenceFormat.IMAGE for e in evidence_ids
                    if self._known(e))
        what = "this photo" if photo else "this document"
        company = doc.entity_id or repo.item_company(repo.items[existing.item_id]) or ""
        repo.needs[needs_id] = NeedsYouRecord(
            id=needs_id, kind="same_document", subject_type="document", subject_id=existing.id,
            item_id=existing.item_id, company_id=company, created_at=at,
            prompt=f"Is {what} the same {who} invoice{number} ({amount}, {when})?",
            options=(CheckOption(id="same", label="Yes, it's the same invoice"),
                     CheckOption(id="different", label="No, it's a different purchase")),
            why=(f"I couldn't read the invoice number on {what}.",
                 f"The supplier, the date and the total are the same as {who} invoice{number}.",
                 "I won't count it twice, and I won't join them without your answer."))
        repo.pending_copies[needs_id] = PendingCopy(
            needs_id=needs_id, existing_id=existing.id, parts=list(parts), at=at, origin=origin, retrieved=retrieved,
            options=dict(options), evidence_ids=tuple(evidence_ids))
        self.log("ask_same_document", subject_id=existing.id, evidence_ids=[*existing.evidence_ids, *evidence_ids],
                 values={"supplier": who, "total": amount, "number_read": False}, response={"needs_you": needs_id})
        report.needs_ids.append(needs_id)
        report.message = (f"Got it. This looks like the {who} invoice{number} you already have. I asked you in Needs "
                          "you whether it is the same one.")

    def _known(self, evidence_id: str) -> bool:
        try:
            self.repo.evidence(evidence_id)
        except Exception:
            return False
        return True

    # ----------------------------------------------------------------- the owner's answers

    def answer(self, needs: NeedsYouRecord, option_id: str, answer_ev: str, now: datetime) -> AnswerOutcome:
        from backoffice.orchestrator import AnswerOutcome, IngestReport

        if option_id not in {o.id for o in needs.options}:
            raise ValueError("not one of the options")
        needs.status, needs.answer, needs.answered_at = "answered", option_id, now
        if needs.kind == "retake":
            self.log("retake_answered", subject_id=needs.subject_id, evidence_ids=[needs.subject_id, answer_ev],
                     values={"answer": option_id})
            return AnswerOutcome(True, "Done. Send me the new photo when you have it." if option_id == "retake" else
                                 "Done. I'll leave it as it is.")
        pending = self.repo.pending_copies[needs.id]
        existing = self.repo.documents[pending.existing_id]
        report = IngestReport(route="answer", message="")
        if option_id == "same":
            extracted = self.o.documents.read(pending.parts)
            if extracted is not None:
                self.o._merge_duplicate(existing, extracted, report)
            pending.status = "merged"
            self.log("same_document_confirmed", subject_id=existing.id,
                     evidence_ids=[*pending.evidence_ids, answer_ev], values={"by": "owner"})
            from backoffice.learning import display_name

            kind, number = existing.label.split(" · ")[0], existing.document.invoice_number or ""
            words = kind[: -len(number)].strip() if number and kind.endswith(number) else kind
            what = " ".join(p for p in (display_name(existing.document.supplier_name), words.lower(), number) if p)
            return AnswerOutcome(True, f"Done. I kept it with the {what}. It is one document, and every original is "
                                       "kept.")
        record = self.o._document_from_parts(pending.parts, at=now, origin=pending.origin, retrieved=pending.retrieved,
                                             report=report, owner_says_separate=True, **pending.options)
        pending.status = "separate"
        self.log("same_document_refused", subject_id=existing.id, evidence_ids=[*pending.evidence_ids, answer_ev],
                 values={"document": record.id if record is not None else None})
        return AnswerOutcome(True, "Done. I recorded it as a separate purchase.")
