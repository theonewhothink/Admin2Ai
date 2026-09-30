"""Spanish invoice types and document vocabulary (§50 for Spain).

Real Decreto 1619/2012 (Reglamento de facturación): a *factura completa* (ordinaria) names both
parties and their NIFs and supports the buyer's VAT deduction; a *factura simplificada* (the old
"ticket") may leave the buyer out; a *factura rectificativa* corrects an earlier one (a credit or
debit). The Verifactu codes (Orden HAC/1177/2024) are F1 (complete), F2 (simplified), F3 (complete
issued in place of simplified ones) and R1-R5 (corrective). Albaranes (delivery notes),
presupuestos (quotes) and facturas proforma are never tax invoices.

verified_as_of: 2026-09 (author knowledge; not re-checked online).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from types import MappingProxyType

from backoffice.countries.base import DocumentFamily, NativeDocumentType, Term
from backoffice.domain.models import DocumentType

from ._text import fold

__all__ = ["ES_DOCUMENT_TYPES", "ES_TERMS", "document_kind", "lookup_term", "map_document_type"]


def _t(code: str, native: str, english: str, doc_type: DocumentType, family: DocumentFamily, *,
       fiscal: bool, proves_payment: bool = False) -> tuple[str, NativeDocumentType]:
    return code, NativeDocumentType(code, native, english, doc_type, family, fiscal, proves_payment)


ES_DOCUMENT_TYPES: Mapping[str, NativeDocumentType] = MappingProxyType(dict([
    _t("F1", "Factura completa", "Invoice", DocumentType.INVOICE, DocumentFamily.INVOICE, fiscal=True),
    _t("F2", "Factura simplificada", "Simplified invoice", DocumentType.SIMPLIFIED_INVOICE,
       DocumentFamily.INVOICE, fiscal=True),
    _t("F3", "Factura emitida en sustitución de facturas simplificadas", "Invoice replacing simplified invoices",
       DocumentType.INVOICE, DocumentFamily.INVOICE, fiscal=True),
    *(_t(f"R{n}", "Factura rectificativa", "Corrective invoice (credit note)", DocumentType.CREDIT_NOTE,
         DocumentFamily.INVOICE, fiscal=True) for n in range(1, 6)),
    _t("RC", "Recibo", "Receipt", DocumentType.RECEIPT, DocumentFamily.PAYMENT, fiscal=False, proves_payment=True),
    _t("AL", "Albarán", "Delivery note", DocumentType.DELIVERY_NOTE, DocumentFamily.MOVEMENT, fiscal=False),
    _t("PR", "Presupuesto", "Quote", DocumentType.QUOTE, DocumentFamily.WORKING, fiscal=False),
    _t("PF", "Factura proforma", "Pro-forma invoice", DocumentType.PRO_FORMA, DocumentFamily.WORKING, fiscal=False),
]))  # fmt: skip

# Names by which a document calls itself, folded, most specific first.
_NAMES: tuple[tuple[str, str], ...] = (
    ("factura proforma", "PF"), ("factura pro forma", "PF"), ("factura pro-forma", "PF"), ("proforma", "PF"),
    ("factura rectificativa", "R1"), ("nota de abono", "R1"), ("factura de abono", "R1"), ("abono", "R1"),
    ("factura simplificada", "F2"), ("ticket", "F2"),
    ("factura completa", "F1"), ("factura ordinaria", "F1"), ("factura", "F1"),
    ("albaran", "AL"), ("presupuesto", "PR"), ("recibo", "RC"),
)  # fmt: skip
# Without a title line, only names that are never a mere reference to another document.
_ANYWHERE = frozenset({"PF", "R1", "F2"})
_TITLE = tuple((code, re.compile(rf"\s*{re.escape(name)}(?![a-z])")) for name, code in _NAMES)
_WORD = tuple((code, re.compile(rf"(?<![a-z]){re.escape(name)}(?![a-z])")) for name, code in _NAMES
              if code in _ANYWHERE and name != "abono")

ES_TERMS: tuple[Term, ...] = (
    Term("Factura nº", "invoice_number", "Invoice number", ("numero de factura", "n.º factura", "num. factura")),
    Term("Fecha de expedición", "issue_date", "Issue date", ("fecha", "fecha de emision", "fecha factura")),
    Term("Fecha de vencimiento", "due_date", "Due date", ("vencimiento",)),
    Term("Base imponible", "net_amount", "Amount before VAT", ("base",)),
    Term("IVA", "vat_amount", "VAT", ("cuota iva", "cuota de iva", "importe iva")),
    Term("Total", "gross_amount", "Total", ("importe total", "total factura", "total a pagar")),
    Term("NIF", "tax_id", "Tax number", ("cif", "nie", "n.i.f.", "c.i.f.")),
    Term("Recargo de equivalencia", "equivalence_surcharge", "Retailers' VAT surcharge", ()),
    Term("Retención IRPF", "withholding", "Income tax withheld", ("irpf", "retencion")),
)


def map_document_type(code: str) -> DocumentType | None:
    entry = ES_DOCUMENT_TYPES.get((code or "").strip().upper())
    return entry.doc_type if entry else None


def lookup_term(label: str) -> Term | None:
    key = fold(label).strip(" :.")
    for term in ES_TERMS:
        if key == fold(term.native) or key in (fold(a) for a in term.aliases):
            return term
    return None


def document_kind(text: str) -> DocumentType | None:
    """The kind a Spanish document's own title gives it ("Factura rectificativa nº R-2026/4"), or None.

    The title is the first line that starts with a document name; without one, only names that are
    never mere references to another document (a pro-forma, a corrective invoice, a simplified one).
    """
    for raw in (text or "").splitlines():
        line = fold(raw)
        for code, pattern in _TITLE:
            if pattern.match(line):
                return ES_DOCUMENT_TYPES[code].doc_type
    folded = fold(text or "")
    for code, pattern in _WORD:
        if pattern.search(folded):
            return ES_DOCUMENT_TYPES[code].doc_type
    return None
