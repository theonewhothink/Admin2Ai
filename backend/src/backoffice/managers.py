"""Outlet managers: one person runs one or more outlets of a company (checklist X37, case 48 franchise).

A ``manager`` membership (production, server/auth.py) is limited to some cost centers of one company,
usually a franchise's or a chain's outlets. Through :meth:`BackOfficeService.dispatch_manager` a manager:

* sees and answers the Needs You questions about their outlets' payments and documents (never a
  payment approval or changed bank details: those stay the owner's, §25);
* sees their outlets' documents (and opens their originals), payments and spending
  (``GET /api/manager/outlets``, ``GET /api/cost-centers/<id>[/statement]``);
* sends receipts for an outlet (``POST /api/manager/receipts``): each is put on that outlet.

Nothing else: no other outlet, no other company, no bank connection, no setting, no sensitive document
(§52). A payment is an outlet's when all of it is on that manager's outlets; a document when it is on
them, when every payment it proves is, or when the manager sent it for one of them.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import TYPE_CHECKING, Any

from backoffice.domain.cost_centers import SplitError
from backoffice.domain.models import SourceKind
from backoffice.sensitivity import mentions

if TYPE_CHECKING:  # pragma: no cover
    from backoffice.orchestrator import DocumentRecord, NeedsYouRecord, TxRecord
    from backoffice.service import BackOfficeService

__all__ = ["ManagerViews"]

MAX_UPLOAD = 25 * 1024 * 1024
# Needs You kinds a manager may answer: plain questions, never approvals of money or bank details (§25).
_OWNER_ONLY_KINDS = frozenset({"approval"})


class ManagerViews:
    def __init__(self, svc: BackOfficeService, cost_centers: Collection[str]) -> None:
        self.svc = svc
        self.repo = svc.repo
        self.o = svc.orchestrator
        centers = [self.repo.cost_centers[c] for c in sorted(set(cost_centers)) if c in self.repo.cost_centers]
        companies = {c.company_id for c in centers}
        # One company only: outlets that (by some mistake) span companies are none at all.
        self.centers = {c.id: c for c in centers} if len(companies) == 1 else {}
        self.company = companies.pop() if len(companies) == 1 else None

    @staticmethod
    def _error(status: int, message: str) -> Exception:
        from backoffice.service import ServiceError

        return ServiceError(status, message)

    # ----------------------------------------------------------------- what is theirs

    def tx_mine(self, rec: TxRecord | None) -> bool:
        if rec is None or rec.private or self.company is None or rec.company_id != self.company:
            return False
        allocation = rec.tx.cost_allocation
        return allocation is not None and not allocation.general and bool(allocation.shares) and all(
            s.cost_center_id in self.centers for s in allocation.shares)

    def doc_mine(self, record: DocumentRecord | None) -> bool:
        if record is None or record.sensitive or self.company is None:
            return False
        if record.uploaded_for in self.centers:
            return True
        if self.svc._document_company(record) != self.company:
            return False
        allocation = record.document.cost_allocation
        if allocation is not None:
            return not allocation.general and bool(allocation.shares) and all(
                s.cost_center_id in self.centers for s in allocation.shares)
        txs = [self.repo.transactions.get(t) for t in record.matched_tx_ids]
        return bool(txs) and all(self.tx_mine(t) for t in txs)

    def need_mine(self, n: NeedsYouRecord) -> bool:
        if n.status != "open" or n.kind in _OWNER_ONLY_KINDS:
            return False
        if n.subject_type == "transaction":
            rec = self.repo.transactions.get(n.subject_id)
            return self.tx_mine(rec) and not any(
                (d := self.repo.documents.get(i)) is not None and d.sensitive for i in rec.document_ids)  # type: ignore[union-attr]
        if n.subject_type == "document":
            return self.doc_mine(self.repo.documents.get(n.subject_id))
        return False

    def _hidden_ids(self) -> set[str]:
        """Ids of what a manager must never see named: other documents and payments, and their evidence."""
        hidden: set[str] = set()
        for d in self.repo.documents.values():
            if not self.doc_mine(d):
                hidden.add(d.id)
                hidden.update(d.evidence_ids)
        for r in self.repo.transactions.values():
            if not self.tx_mine(r):
                hidden.update((r.id, r.evidence_id))
        return hidden

    def _drop(self, value: Any, hidden: set[str]) -> Any:
        """``value`` without any list entry that names something hidden."""
        if isinstance(value, Mapping):
            return {k: self._drop(v, hidden) for k, v in value.items()}
        if isinstance(value, list):
            return [self._drop(v, hidden) for v in value if not (isinstance(v, Mapping) and mentions(v, hidden))]
        return value

    # ----------------------------------------------------------------- views

    def outlets(self) -> dict[str, Any]:
        """Their outlets: what each cost and brought in so far, with what is still open."""
        views = self.svc._cost_views()
        rows = []
        for center in sorted(self.centers.values(), key=lambda c: (c.label.casefold(), c.id)):
            t = views.totals(center)
            rows.append({**views.card(center), "companyName": self.repo.company_name(center.company_id),
                         "spent": _num(t.spent), "received": _num(t.received), "payments": len(t.payments),
                         "documents": len(t.documents), "openItems": len(t.open_items), "currency": "EUR"})
        return self._drop({"outlets": rows, "needsYou": len(self.needs_you()["items"])}, self._hidden_ids())

    def needs_you(self) -> dict[str, Any]:
        svc = self.svc
        items = []
        for n in svc._open_needs():
            if not self.need_mine(n):
                continue
            if n.kind == "check":
                items.append(svc._check(n))
            elif n.kind == "cost_center":
                items.append(svc._cost_center_item(n))
            elif n.kind == "choice":
                items.append(svc._choice(n))
            else:
                items.append(svc._question(n))
        return {"items": items}

    def answer(self, needs_id: str, option_id: str, remember: bool = False, split: Any = None) -> dict[str, Any]:
        n = self.repo.needs.get(needs_id)
        if n is None or not self.need_mine(n):
            raise self._error(404, "I can't find that question any more.")
        return self.svc.answer(needs_id, option_id, remember, split)

    def documents(self, body: Mapping[str, Any] | None = None) -> dict[str, Any]:
        b = body or {}
        items = self.svc.assistant.documents(query=str(b.get("q", "")), supplier=str(b.get("supplier", "")),
                                             date_from=self.svc._date_arg(b, "from"),
                                             date_to=self.svc._date_arg(b, "to"))
        mine = [i for i in items if self.doc_mine(self.repo.documents.get(i["id"]))]
        return {"items": mine, "total": len(mine)}

    def _document(self, document_id: str) -> DocumentRecord:
        record = self.repo.documents.get(document_id)
        if not self.doc_mine(record):
            raise self._error(404, "I can't find that document.")
        assert record is not None
        return record

    def document(self, document_id: str) -> dict[str, Any]:
        self._document(document_id)
        return self._drop(self.svc.document_detail(document_id), self._hidden_ids())

    def document_file(self, document_id: str) -> dict[str, Any]:
        self._document(document_id)
        return self.svc.document_download(document_id)

    def transaction(self, tx_id: str) -> dict[str, Any]:
        rec = self.repo.transactions.get(tx_id)
        if not self.tx_mine(rec):
            raise self._error(404, "I can't find that payment.")
        out = self.svc.transaction(tx_id) or {}
        return self._drop(out, self._hidden_ids())

    def _center(self, cost_center_id: str) -> Any:
        center = self.centers.get(cost_center_id)
        if center is None:
            raise self._error(404, "I can't find that one.")
        return center

    def cost_center(self, cost_center_id: str, body: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return self._drop(self.svc._cost_views().detail(self._center(cost_center_id), body), self._hidden_ids())

    def statement(self, cost_center_id: str, body: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return self._drop(self.svc._cost_views().statement(self._center(cost_center_id), body), self._hidden_ids())

    # ----------------------------------------------------------------- receipts for an outlet

    def receipt(self, body: Mapping[str, Any] | None, *, by: str = "") -> dict[str, Any]:
        """A receipt a manager sends for one of their outlets: read, then put on that outlet."""
        from backoffice.service import _b64

        b = body or {}
        chosen = str(b.get("costCenterId") or b.get("cost_center_id") or "")
        if not chosen and len(self.centers) == 1:
            chosen = next(iter(self.centers))
        if chosen not in self.centers:
            raise self._error(400, "Which of your outlets is this receipt for?")
        data = _b64(b.get("dataBase64") or b.get("data_base64"))
        if not data:
            raise self._error(400, "There was nothing to save.")
        if len(data) > MAX_UPLOAD:
            raise self._error(413, "This file is too large to send.")
        filename = b.get("filename") if isinstance(b.get("filename"), str) else None
        content_type = b.get("contentType") or b.get("content_type")
        content_type = content_type if isinstance(content_type, str) else None
        if (filename or "").lower().endswith(".csv") or (content_type or "").startswith("text/csv"):
            raise self._error(415, "Send a photo or a PDF of the receipt.")
        report = self.o.ingest_file(data, filename=filename, content_type=content_type,
                                    source_kind=SourceKind.MOBILE_SCAN, origin="scan")
        if report.route == "unsupported":
            raise self._error(415, report.message)
        center = self.centers[chosen]
        docs = [self.repo.documents[d] for d in dict.fromkeys(report.document_ids) if d in self.repo.documents]
        placed = False
        for record in docs:
            record.uploaded_for = chosen
            self.o.log("manager", "receipt_for_outlet", subject_id=record.id, evidence_ids=record.evidence_ids,
                       values={"cost_center": chosen, "by": by or "manager"}, actor=f"manager:{by}" if by else "manager")
            if record.sensitive:
                continue
            try:
                self.o.allocate_by_owner(record.id, cost_center_id=chosen, answered_by=by or "manager")
                placed = True
            except (SplitError, KeyError):
                continue  # which company it is for is not known yet: it stays theirs, and is placed when it is
        self.o.run()
        if not docs:
            return {"ok": True, "message": "Got it. I stored it, but I couldn't read it yet.", "documents": []}
        return {"ok": True, "message": f"Got it. I put it on {center.label}." if placed else
                f"Got it. It is on {center.label}'s list; I'm matching it with the payment.",
                "documents": [{"id": d.id, "label": d.label} for d in docs if self.doc_mine(d)]}


def _num(value: Any) -> int | float | None:
    from decimal import Decimal

    if value is None:
        return None
    value = Decimal(value).quantize(Decimal("0.01"))
    return int(value) if value == value.to_integral_value() else float(value)
