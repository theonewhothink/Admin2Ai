"""Import purchase chains: one foreign purchase's documents and payments, linked by their references (X28).

Buying goods from abroad leaves a trail of separate pieces: the supplier's pro-forma and the deposit paid on
it, the supplier's final invoice (often in a foreign currency), the freight forwarder's invoice, the customs
declaration ("DAU", "declaração aduaneira", "customs entry", with its MRN) and the duties and import VAT paid
to customs. Each is kept and matched as usual; this module shows that they belong to one purchase::

    Part of order PO-2026-114: deposit, invoice, freight, customs, duties

Linking is by the references the pieces carry, never by amounts (§3: nothing is linked on a guess):

* the order number ("PO-2026-114", "Purchase order", "Encomenda n.º ..."),
* the customs declaration's MRN (Movement Reference Number, 18 characters: "26PT000000012345A7"),
* container numbers (ISO 6346: "MSCU 123456 5") and bill of lading numbers ("B/L MEDUSZ123456").

Two pieces are in the same chain when they share one of these; a payment joins through the reference its
bank line quotes, or through the document it was already matched to. A piece that shares nothing stays on its
own, whatever its amount. The accountant is shown the import VAT paid at customs (it is on the customs
declaration, not on any supplier's invoice), as a flag for them to decide on (§28); the owner is never asked.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from backoffice.domain.models import DocumentType

if TYPE_CHECKING:
    from backoffice.orchestrator import DocumentRecord, Repository, TxRecord

__all__ = ["ImportChain", "ImportPiece", "Reference", "find_chains", "import_vat", "references"]

ORDER, MRN, CONTAINER, BILL_OF_LADING = "order", "mrn", "container", "bill_of_lading"

_ORDER_LABELLED = re.compile(
    r"(?<![A-Za-z])(?:purchase\s+order|order|p\.?\s?o\.?|encomenda|pedido|nota\s+de\s+encomenda)(?![A-Za-z])"
    r"\s*(?:n\.?\s?[ºo°]\.?|no\.?|nr\.?|number|num\.?|#|ref\.?)?\s*[:.]?\s*(?P<v>[A-Z0-9][A-Z0-9/\-.]{3,24}[A-Z0-9])",
    re.IGNORECASE)
_ORDER_BARE = re.compile(r"\b(?P<v>PO[-/ ]?\d{2,4}(?:[-/]\d{1,6})+|PO[-/]?\d{4,})\b", re.IGNORECASE)
_MRN = re.compile(r"\b(?P<v>\d{2}[A-Z]{2}[A-Z0-9]{14})\b")
_CONTAINER = re.compile(r"\b(?P<v>[A-Z]{3}[UJZ]\s?\d{6}\s?\d)\b")
_BILL = re.compile(
    r"(?<![A-Za-z])(?:bill\s+of\s+lading|b\s?/\s?l|bl\s+no|conhecimento\s+de\s+embarque|awb|air\s+waybill)"
    r"\s*(?:\s*\(\s*b\s?/\s?l\s*\))?"  # "Conhecimento de embarque (B/L): ..."
    r"\s*(?:n\.?\s?[ºo°]\.?|no\.?|nr\.?|number|#)?[\s():.]*(?P<v>[A-Z0-9][A-Z0-9\-]{5,24})", re.IGNORECASE)

_CUSTOMS_PHRASES = ("declaracao aduaneira", "declaracao de importacao", "documento administrativo unico",
                    "customs entry", "customs declaration", "import declaration", "declaracion aduanera")
_FREIGHT_WORDS = ("freight", "frete", "transitario", "forwarder", "forwarding", "bill of lading",
                  "conhecimento de embarque", "shipping", "transporte maritimo", "transporte internacional",
                  "desalfandegamento", "customs clearance")
_DEPOSIT_WORDS = ("deposit", "sinal", "adiantamento", "advance payment", "down payment", "prepayment", "anticipo")
_VAT_LINE = re.compile(r"^(?:.*\b(?:iva|vat|import\s+vat)\b.*?)(?P<v>\d{1,3}(?:[.\s]\d{3})*,\d{2}|\d+,\d{2}|"
                       r"\d{1,3}(?:,\d{3})*\.\d{2}|\d+\.\d{2})\s*(?:€|eur)?\s*$", re.IGNORECASE)


def _fold(text: str) -> str:
    plain = unicodedata.normalize("NFKD", text.casefold())
    return "".join(c for c in plain if not unicodedata.combining(c))


@dataclass(frozen=True, order=True)
class Reference:
    """One reference a piece carries: its kind and its value, normalised (upper case, no spaces)."""

    kind: str
    value: str

    @property
    def words(self) -> str:
        return {ORDER: "order", MRN: "MRN", CONTAINER: "container", BILL_OF_LADING: "bill of lading"}[self.kind] + \
            f" {self.value}"


def _clean(value: str) -> str:
    return re.sub(r"\s+", "", value).upper().strip(".-/")


def references(text: str) -> frozenset[Reference]:
    """The order, MRN, container and bill of lading references in a text (a document, a bank line)."""
    if not text:
        return frozenset()
    found: set[Reference] = set()
    for pattern in (_ORDER_LABELLED, _ORDER_BARE):
        for m in pattern.finditer(text):
            value = _clean(m.group("v"))
            if len(value) >= 5 and any(c.isdigit() for c in value) and not _MRN.fullmatch(value) \
                    and not _looks_like_date(value):
                found.add(Reference(ORDER, value))
    for m in _MRN.finditer(text.upper()):
        found.add(Reference(MRN, m.group("v")))
    for m in _CONTAINER.finditer(text.upper()):
        found.add(Reference(CONTAINER, _clean(m.group("v"))))
    for m in _BILL.finditer(text):
        value = _clean(m.group("v"))
        if any(c.isdigit() for c in value):
            found.add(Reference(BILL_OF_LADING, value))
    return frozenset(found)


def _looks_like_date(value: str) -> bool:
    return bool(re.fullmatch(r"\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}", value))


def _amount(text: str) -> Decimal | None:
    raw = text.replace(" ", "")
    if "," in raw and (raw.rfind(",") > raw.rfind(".")):
        raw = raw.replace(".", "").replace(",", ".")
    else:
        raw = raw.replace(",", "")
    try:
        return Decimal(raw).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None


def import_vat(text: str) -> Decimal | None:
    """The import VAT a customs declaration states (its "IVA" / "VAT" line), when exactly one amount is given."""
    found = {v for line in (text or "").splitlines() if (m := _VAT_LINE.match(line.strip()))
             and (v := _amount(m.group("v"))) is not None and v > 0}
    return found.pop() if len(found) == 1 else None


# --------------------------------------------------------------------------- chains


@dataclass(frozen=True)
class ImportPiece:
    """One document or payment of an import purchase, with the role it plays in it."""

    kind: str  # "document" | "transaction"
    id: str
    role: str  # "pro-forma" | "deposit" | "invoice" | "freight" | "customs" | "duties" | "payment"
    label: str
    on: date | None
    amount: Decimal | None
    currency: str
    references: tuple[Reference, ...]
    evidence_ids: tuple[str, ...] = ()


_ROLE_ORDER = {"pro-forma": 0, "deposit": 1, "invoice": 2, "payment": 3, "freight": 4, "customs": 5, "duties": 6}


@dataclass
class ImportChain:
    """One import purchase: its pieces and the references that tie them together.

    ``links`` are the references at least two pieces share (what ties the chain); ``references`` every
    reference its pieces carry (an order number printed on one piece still names the purchase).
    """

    id: str
    pieces: list[ImportPiece] = field(default_factory=list)
    links: tuple[Reference, ...] = ()
    references: tuple[Reference, ...] = ()

    def _first(self, kind: str, among: Sequence[Reference] | None = None) -> str | None:
        found = sorted(r.value for r in (self.links if among is None else among) if r.kind == kind)
        return found[0] if found else None

    @property
    def order(self) -> str | None:
        return self._first(ORDER) or self._first(ORDER, self.references)

    @property
    def mrn(self) -> str | None:
        return self._first(MRN) or self._first(MRN, self.references)

    @property
    def name(self) -> str:
        """"order PO-2026-114", else the MRN, container or bill of lading that ties it."""
        if self.order:
            return f"order {self.order}"
        first = min(self.links or self.references,
                    key=lambda r: ({MRN: 0, CONTAINER: 1, BILL_OF_LADING: 2}.get(r.kind, 3), r.value))
        return {MRN: "import MRN", CONTAINER: "shipment", BILL_OF_LADING: "shipment"}.get(first.kind, "import") + \
            f" {first.value}"

    @property
    def roles(self) -> list[str]:
        return list(dict.fromkeys(p.role for p in sorted(self.pieces, key=_piece_order)))

    @property
    def line(self) -> str:
        """'Part of order PO-2026-114: deposit, invoice, freight, customs, duties'"""
        return f"Part of {self.name}: {', '.join(self.roles)}"

    def has(self, kind: str, subject_id: str) -> bool:
        return any(p.kind == kind and p.id == subject_id for p in self.pieces)


def _piece_order(p: ImportPiece) -> tuple[int, date, str]:
    return (_ROLE_ORDER.get(p.role, 9), p.on or date.min, p.id)


def _document_role(record: DocumentRecord, refs: Iterable[Reference]) -> str:
    """customs (a customs declaration), pro-forma, freight (a forwarder's invoice) or invoice."""
    folded = _fold(f"{record.document.supplier_name or ''}\n{record.text}")
    words = set(re.findall(r"[a-z]+", folded))
    kinds = {r.kind for r in refs}
    if any(p in folded for p in _CUSTOMS_PHRASES) or (MRN in kinds and words & {"dau", "mrn", "customs"}):
        return "customs"
    if record.document.doc_type is DocumentType.PRO_FORMA:
        return "pro-forma"
    if kinds & {CONTAINER, BILL_OF_LADING} and any(w in folded for w in _FREIGHT_WORDS):
        return "freight"
    return "invoice"


def _payment_role(rec: TxRecord, refs: Iterable[Reference], matched: Sequence[ImportPiece]) -> str:
    """duties (paid to customs, quoting the MRN), deposit, freight or payment."""
    text = _fold(f"{rec.tx.counterparty} {rec.tx.description} {rec.tx.reference or ''}")
    if any(r.kind == MRN for r in refs):
        return "duties"
    if any(w in text for w in _DEPOSIT_WORDS) or any(p.role == "pro-forma" for p in matched):
        return "deposit"
    if any(p.role == "freight" for p in matched):
        return "freight"
    return "payment"


_PAYMENT_LABEL = {"deposit": "Deposit paid", "duties": "Duties and VAT paid to customs", "freight": "Freight paid",
                  "payment": "Payment"}


def _document_label(record: DocumentRecord, role: str) -> str:
    return "Customs declaration" if role == "customs" else record.label.split(" · ")[0]


def find_chains(repo: Repository, *, company_id: str | None = None,
                text_of: Callable[[str], str] | None = None) -> list[ImportChain]:
    """Every import chain among the business's documents, letters and payments.

    Pieces are tied only by a reference they share (order, MRN, container, bill of lading); a payment also
    joins the document it was already matched to, and the letter (a customs declaration read as a tax to pay)
    it paid. A chain needs at least one shared reference and either two documents or a customs, duties,
    freight or deposit piece: a lone invoice and its payment are not an import chain. ``company_id`` keeps the
    chains that touch that company; ``text_of`` reads a letter's text by its evidence id.
    """
    documents: dict[str, tuple[DocumentRecord, frozenset[Reference]]] = {}
    for record in repo.documents.values():
        if not record.sales:
            refs = references(record.text)
            if refs:
                documents[record.id] = (record, refs)
    letters: dict[str, tuple[Any, frozenset[Reference], str]] = {}
    read = text_of or (lambda evidence_id: _stored_text(repo, evidence_id))
    for ob in repo.obligations.values():
        text = read(ob.evidence_id)
        refs = references(text)
        if refs and _is_customs_text(text, refs):
            letters[ob.obligation.id] = (ob, refs, text)
    if not documents and not letters:
        return []
    payments: dict[str, tuple[TxRecord, frozenset[Reference], list[str]]] = {}
    for rec in repo.transactions.values():
        if rec.private or rec.tx.amount >= 0:
            continue
        refs = references(" ".join(p for p in (rec.tx.description, rec.tx.reference or "") if p))
        matched = [d for d in (*rec.document_ids, *rec.supporting_document_ids) if d in documents]
        if refs or matched:
            payments[rec.id] = (rec, refs, matched)

    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    carriers: dict[Reference, list[str]] = {}  # reference -> the pieces that carry it
    carried: dict[str, frozenset[Reference]] = {}
    for doc_id, (_, refs) in sorted(documents.items()):
        carried[f"d:{doc_id}"] = refs
    for ob_id, (_, refs, _) in sorted(letters.items()):
        carried[f"o:{ob_id}"] = refs
    for tx_id, (_, refs, _) in sorted(payments.items()):
        carried[f"t:{tx_id}"] = refs
    for key, refs in sorted(carried.items()):
        find(key)
        for r in sorted(refs):
            carriers.setdefault(r, []).append(key)
    for keys in carriers.values():
        for other in keys[1:]:
            union(keys[0], other)
    for tx_id, (_, _, matched) in sorted(payments.items()):
        for d in matched:
            union(f"t:{tx_id}", f"d:{d}")
    groups: dict[str, list[str]] = {}
    for key in sorted(carried):
        groups.setdefault(find(key), []).append(key)

    chains: list[ImportChain] = []
    for root, keys in sorted(groups.items()):
        members = set(keys)
        links = tuple(sorted(r for r, holders in carriers.items() if len(members & set(holders)) >= 2))
        if len(keys) < 2 or not links:
            continue  # shares no reference with anything: stays on its own, whatever its amount
        pieces: list[ImportPiece] = []
        by_document: dict[str, ImportPiece] = {}
        for key in keys:
            if key.startswith("d:"):
                record, refs = documents[key[2:]]
                role = _document_role(record, refs)
                piece = ImportPiece("document", record.id, role, _document_label(record, role),
                                    record.document.issue_date, record.document.gross_amount,
                                    record.document.currency, tuple(sorted(refs)), tuple(record.evidence_ids))
                by_document[record.id] = piece
                pieces.append(piece)
            elif key.startswith("o:"):  # a customs declaration read as a tax to pay (§24)
                ob, refs, _ = letters[key[2:]]
                pieces.append(ImportPiece("obligation", ob.obligation.id, "customs", "Customs declaration",
                                          ob.received_on, ob.obligation.amount, "EUR",
                                          tuple(sorted(refs)), (ob.evidence_id,)))
        for key in keys:
            if key.startswith("t:"):
                rec, refs, matched = payments[key[2:]]
                role = _payment_role(rec, refs, [by_document[d] for d in matched if d in by_document])
                pieces.append(ImportPiece("transaction", rec.id, role, _PAYMENT_LABEL[role], rec.tx.booked_on,
                                          rec.tx.amount, rec.tx.currency, tuple(sorted(refs)), (rec.evidence_id,)))
        documents_in = sum(1 for p in pieces if p.kind == "document")
        if documents_in < 2 and not {p.role for p in pieces} & {"customs", "duties", "freight", "deposit"}:
            continue  # one invoice and its payment: an ordinary purchase, not an import chain
        chain = ImportChain(id="imp_" + root.split(":", 1)[1], pieces=sorted(pieces, key=_piece_order), links=links,
                            references=tuple(sorted({r for p in pieces for r in p.references})))
        if company_id is None or _touches(repo, chain, company_id):
            chains.append(chain)
    return chains


def _is_customs_text(text: str, refs: Iterable[Reference]) -> bool:
    folded = _fold(text)
    words = set(re.findall(r"[a-z]+", folded))
    return any(p in folded for p in _CUSTOMS_PHRASES) or (
        any(r.kind == MRN for r in refs) and bool(words & {"dau", "mrn", "customs", "alfandega", "aduaneira"}))


def _stored_text(repo: Repository, evidence_id: str) -> str:
    """A letter's text as stored (a text file, or what the reader read of a PDF or photo)."""
    outcome = repo.reads.get(evidence_id)
    if outcome is not None:
        return outcome.text or outcome.reading_text or ""
    try:
        return repo.registry.open(repo.tenant_id, evidence_id).decode("utf-8")
    except Exception:
        return ""


def _touches(repo: Repository, chain: ImportChain, company_id: str) -> bool:
    for p in chain.pieces:
        if p.kind == "transaction" and repo.transactions[p.id].company_id == company_id:
            return True
        if p.kind == "obligation" and repo.obligations[p.id].obligation.entity_id == company_id:
            return True
        if p.kind == "document":
            record = repo.documents[p.id]
            if (record.document.entity_id or repo.item_company(repo.items[record.item_id])) == company_id:
                return True
    return False


def chain_for(repo: Repository, kind: str, subject_id: str,
              text_of: Callable[[str], str] | None = None) -> ImportChain | None:
    """The import chain a document, letter or payment belongs to, if any."""
    return next((c for c in find_chains(repo, text_of=text_of) if c.has(kind, subject_id)), None)


def chain_view(chain: ImportChain, *, today: date | None = None) -> dict[str, Any]:
    """The chain as the document and payment details show it (plain words, each piece linked)."""
    from backoffice.learning import day_month, format_money

    return {
        "id": chain.id, "name": chain.name, "order": chain.order, "mrn": chain.mrn, "line": chain.line,
        "references": [r.words for r in chain.links],
        "pieces": [{"kind": p.kind, "id": p.id, "role": p.role, "label": p.label,
                    "date": p.on.isoformat() if p.on else None,
                    "amount": _json_amount(p.amount), "currency": p.currency,
                    "text": " · ".join(x for x in (p.label, day_month(p.on, today) if p.on else "",
                                                  format_money(abs(p.amount), p.currency)
                                                  if p.amount is not None else "") if x),
                    "linkedBy": [r.words for r in p.references if r in chain.links]}
                   for p in chain.pieces],
    }


def _json_amount(value: Decimal | None) -> int | float | None:
    if value is None:
        return None
    value = abs(value).quantize(Decimal("0.01"))
    return int(value) if value == value.to_integral_value() else float(value)


def customs_vat(repo: Repository, chain: ImportChain, text_of: Callable[[str], str] | None = None
                ) -> tuple[Decimal | None, ImportPiece | None]:
    """The import VAT the chain's customs declaration states, and that declaration's piece."""
    customs = next((p for p in chain.pieces if p.role == "customs"), None)
    if customs is None:
        return None, None
    if customs.kind == "document":
        text = repo.documents[customs.id].text
    else:
        read = text_of or (lambda evidence_id: _stored_text(repo, evidence_id))
        text = "\n".join(read(e) for e in customs.evidence_ids)
    return import_vat(text), customs
