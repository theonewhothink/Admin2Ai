"""Portuguese fiscal document types and vocabulary (§50).

Document type codes are the SAF-T (PT) 1.04_01 codes, also used in field D of
the AT invoice QR code. Source: Portaria 302/2016 (SAF-T PT 1.04_01 structure)
and Portaria 195/2020 (QR code). verified_as_of: 2026-09-27.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from backoffice.countries.base import DocumentFamily, NativeDocumentType, Term
from backoffice.domain.models import DocumentType

from ._text import fold_label

_I = DocumentFamily.INVOICE
_P = DocumentFamily.PAYMENT
_M = DocumentFamily.MOVEMENT
_W = DocumentFamily.WORKING
_T = DocumentType


def _t(
    code: str,
    native: str,
    english: str,
    doc_type: DocumentType,
    family: DocumentFamily,
    *,
    fiscal: bool = False,
    paid: bool = False,
    legacy: bool = False,
) -> NativeDocumentType:
    return NativeDocumentType(code, native, english, doc_type, family, fiscal, paid, legacy)


_TYPES: tuple[NativeDocumentType, ...] = (
    # Sales invoices (SAF-T InvoiceType)
    _t("FT", "Fatura", "Invoice", _T.INVOICE, _I, fiscal=True),
    _t("FR", "Fatura-recibo", "Invoice-receipt", _T.INVOICE_RECEIPT, _I, fiscal=True, paid=True),
    _t("FS", "Fatura simplificada", "Simplified invoice", _T.SIMPLIFIED_INVOICE, _I, fiscal=True),
    _t("NC", "Nota de crédito", "Credit note", _T.CREDIT_NOTE, _I, fiscal=True),
    _t("ND", "Nota de débito", "Debit note", _T.DEBIT_NOTE, _I, fiscal=True),
    # Insurance-sector invoice types. RP/RE are the sector's invoice and
    # reversal; the co-insurance/reinsurance allocations stay OTHER.
    _t("RP", "Prémio ou recibo de prémio", "Insurance premium", _T.INVOICE, _I, fiscal=True),
    _t("RE", "Estorno ou recibo de estorno", "Insurance refund", _T.CREDIT_NOTE, _I, fiscal=True),
    _t("CS", "Imputação a co-seguradoras", "Co-insurance allocation", _T.OTHER, _I),
    _t("LD", "Imputação a co-seguradora líder", "Lead co-insurer allocation", _T.OTHER, _I),
    _t("RA", "Resseguro aceite", "Accepted reinsurance", _T.OTHER, _I),
    # Pre-2013 invoice types, still seen on historical documents.
    _t("VD", "Venda a dinheiro", "Cash sale", _T.INVOICE_RECEIPT, _I, fiscal=True, paid=True,
       legacy=True),
    _t("TV", "Talão de venda", "Sales slip", _T.SIMPLIFIED_INVOICE, _I, fiscal=True, legacy=True),
    _t("TD", "Talão de devolução", "Return slip", _T.CREDIT_NOTE, _I, fiscal=True, legacy=True),
    _t("AA", "Alienação de ativos", "Asset sale", _T.INVOICE, _I, fiscal=True, legacy=True),
    _t("DA", "Devolução de ativos", "Asset return", _T.CREDIT_NOTE, _I, fiscal=True, legacy=True),
    # Receipts (SAF-T PaymentType)
    _t("RC", "Recibo (IVA de caixa)", "Receipt (cash-accounting VAT)", _T.RECEIPT, _P, paid=True),
    _t("RG", "Recibo", "Receipt", _T.RECEIPT, _P, paid=True),
    # Movement of goods (SAF-T MovementType): not invoices.
    _t("GR", "Guia de remessa", "Delivery note", _T.OTHER, _M),
    _t("GT", "Guia de transporte", "Transport document", _T.OTHER, _M),
    _t("GA", "Guia de movimentação de ativos próprios", "Own-asset transfer note", _T.OTHER, _M),
    _t("GC", "Guia de consignação", "Consignment note", _T.OTHER, _M),
    _t("GD", "Guia ou nota de devolução", "Return note", _T.OTHER, _M),
    # Working documents (SAF-T WorkType): not invoices, never proof of a sale.
    _t("CM", "Consulta de mesa", "Table check", _T.OTHER, _W),
    _t("CC", "Crédito de consignação", "Consignment credit", _T.OTHER, _W),
    _t("FC", "Fatura de consignação", "Consignment invoice", _T.OTHER, _W),
    _t("FO", "Folha de obra", "Work sheet", _T.OTHER, _W),
    _t("NE", "Nota de encomenda", "Purchase order", _T.OTHER, _W),
    _t("OU", "Outros", "Other", _T.OTHER, _W),
    _t("OR", "Orçamento", "Quote", _T.OTHER, _W),
    _t("PF", "Pró-forma", "Pro-forma invoice", _T.OTHER, _W),
    _t("DC", "Documento de conferência", "Conference document", _T.OTHER, _W, legacy=True),
)

PT_DOCUMENT_TYPES: Mapping[str, NativeDocumentType] = MappingProxyType(
    {t.code: t for t in _TYPES}
)

# SAF-T status codes per family (InvoiceStatus, PaymentStatus, MovementStatus,
# WorkStatus). "A" = cancelled everywhere.
DOCUMENT_STATUSES: Mapping[DocumentFamily, Mapping[str, str]] = MappingProxyType(
    {
        _I: MappingProxyType(
            {"N": "normal", "S": "self-billed", "A": "cancelled", "R": "summary", "F": "invoiced"}
        ),
        _P: MappingProxyType({"N": "normal", "A": "cancelled"}),
        _M: MappingProxyType(
            {"N": "normal", "T": "third-party", "A": "cancelled", "F": "invoiced", "R": "summary"}
        ),
        _W: MappingProxyType({"N": "normal", "A": "cancelled", "F": "invoiced"}),
    }
)
CANCELLED_STATUS = "A"

# Preferred native code when exporting a core document type. RECEIPT is left
# out on purpose: RC and RG are not interchangeable.
_PREFERRED_CODE: Mapping[DocumentType, str] = MappingProxyType(
    {
        _T.INVOICE: "FT",
        _T.INVOICE_RECEIPT: "FR",
        _T.SIMPLIFIED_INVOICE: "FS",
        _T.CREDIT_NOTE: "NC",
        _T.DEBIT_NOTE: "ND",
    }
)


def get_document_type(code: str) -> NativeDocumentType | None:
    return PT_DOCUMENT_TYPES.get((code or "").strip().upper())


def map_document_type(code: str) -> DocumentType | None:
    """SAF-T / QR code -> core DocumentType; None for unknown codes."""
    entry = get_document_type(code)
    return entry.doc_type if entry else None


def preferred_code(doc_type: DocumentType) -> str | None:
    """Native code for a core type, when there is exactly one sensible choice."""
    return _PREFERRED_CODE.get(doc_type)


def status_label(code: str, status: str) -> str | None:
    """Plain meaning of a status for a document code (e.g. "A" -> "cancelled")."""
    entry = get_document_type(code)
    if entry is None:
        return None
    return DOCUMENT_STATUSES[entry.family].get(status)


# --------------------------------------------------------------------------- #
# Vocabulary. ``english`` is plain language for the owner (§36): no jargon.
# --------------------------------------------------------------------------- #

PT_TERMS: tuple[Term, ...] = (
    Term("Fatura", "invoice", "invoice", ("Factura", "Fatura n.º")),
    Term("Fatura-Recibo", "invoice_receipt", "invoice and receipt", ("Fatura Recibo", "Factura-Recibo")),
    Term("Fatura Simplificada", "simplified_invoice", "simplified invoice", ("Factura Simplificada",)),
    Term("Recibo", "receipt", "receipt"),
    Term("Nota de Crédito", "credit_note", "credit note (refund)", ("Nota de Credito",)),
    Term("Nota de Débito", "debit_note", "extra charge note", ("Nota de Debito",)),
    Term("Guia de Remessa", "delivery_note", "delivery note"),
    Term("Guia de Transporte", "transport_document", "transport document"),
    Term("Orçamento", "quote", "quote"),
    Term("Pró-forma", "proforma", "pro-forma (not an invoice)", ("Fatura Pró-forma", "Proforma")),
    Term("Base tributável", "net_amount", "amount before VAT",
         ("Base de incidência", "Incidência", "Valor tributável", "Total sem IVA", "Total s/ IVA")),
    Term("IVA", "vat_amount", "VAT", ("Total IVA", "Total de IVA", "Valor IVA", "Imposto sobre o Valor Acrescentado")),
    Term("Taxa de IVA", "vat_rate", "VAT rate", ("Taxa",)),
    Term("Isento", "vat_exempt", "no VAT charged", ("Isenta", "Isento de IVA")),
    Term("Total", "gross_amount", "total",
         ("Total a pagar", "Total do documento", "Total c/ IVA", "Total com IVA", "Valor total")),
    Term("Retenção na fonte", "withholding", "tax withheld", ("Retenção IRS", "Retenção na fonte IRS", "IRS retido")),
    Term("Imposto do Selo", "stamp_duty", "stamp duty"),
    Term("Contribuinte", "tax_id", "tax number", ("N.º Contribuinte", "Nº Contribuinte", "NIF/NIPC")),
    Term("NIF", "tax_id", "tax number", ("Número de Identificação Fiscal",)),
    Term("NIPC", "tax_id", "company tax number", ("Número de Identificação de Pessoa Coletiva",)),
    Term("Consumidor final", "final_consumer", "customer without a tax number"),
    Term("Fornecedor", "supplier", "supplier", ("Emitente", "Prestador")),
    Term("Cliente", "customer", "customer", ("Adquirente",)),
    Term("Data de emissão", "issue_date", "date issued", ("Data do documento", "Data da fatura")),
    Term("Data de vencimento", "due_date", "due date", ("Vencimento", "Data limite de pagamento")),
    Term("Referência Multibanco", "payment_reference", "Multibanco payment reference", ("Referência MB", "Ref. MB")),
    Term("Entidade", "payment_entity", "Multibanco entity"),
    Term("Montante", "amount", "amount", ("Valor",)),
    Term("IBAN", "iban", "bank account (IBAN)", ("NIB",)),
    Term("ATCUD", "atcud", "document code"),
    Term("Série", "series", "document series"),
    Term("Desconto", "discount", "discount"),
    Term("Morada", "address", "address"),
    Term("Anulado", "cancelled", "cancelled", ("Anulada", "Documento anulado")),
)

_TERM_INDEX: Mapping[str, Term] = MappingProxyType(
    {fold_label(label): term for term in PT_TERMS for label in (term.native, *term.aliases)}
)


def lookup_term(label: str) -> Term | None:
    """Find a term by its Portuguese label, ignoring case, accents and spacing."""
    if not isinstance(label, str):
        return None
    return _TERM_INDEX.get(fold_label(label))
