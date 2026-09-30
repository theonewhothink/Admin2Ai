"""Spain country pack (§49-50): enough to run a Spanish company's back office.

Generic callers go through the registry::

    from backoffice.countries import company_pack
    pack = company_pack("ES")

Tax numbers (nif)
    validate_nif(raw) -> TaxIdCheck       DNI "12345678Z", NIE "X1234567L", CIF "B12345674"
VAT (vat)
    rates_on(day, region=None)            IVA 21 / 10 / 4 / 0 (dated; no IVA in the Canary Islands)
Documents and vocabulary (documents)
    map_document_type("F2") -> SIMPLIFIED_INVOICE;  document_kind(text) -> DocumentType | None
Text (text_fields)
    read_text(text, source, ...) -> TextReading    "Factura nº", "Fecha", "Base imponible", "IVA", "Total"
Invoice QR codes (qr)
    parse_es_qr(url) -> ESQRCode           Verifactu (AEAT) and TicketBAI URLs
Obligations (obligations)
    quarterly_vat_return(company, today)   the modelo 303 deadline;  VOCABULARY for Spanish letters
"""

from backoffice.countries.base import register_pack

from .documents import ES_DOCUMENT_TYPES, ES_TERMS, document_kind, lookup_term, map_document_type
from .nif import cif_control, dni_letter, entity_kind_hint, is_valid_nif, normalize_nif, validate_nif
from .obligations import VAT_RETURN_TITLE, quarterly_vat_return, vat_return_due
from .pack import SpainPack
from .qr import ESQRCode, ESQRError, find_qr_url, looks_like_es_qr, parse_es_qr, qr_to_observations
from .text_fields import ES_LABELS, read_text
from .vat import ES_VAT_RATES, rates_on

PACK = SpainPack()
register_pack(PACK)

__all__ = [
    "ES_DOCUMENT_TYPES",
    "ES_LABELS",
    "ES_TERMS",
    "ES_VAT_RATES",
    "PACK",
    "VAT_RETURN_TITLE",
    "ESQRCode",
    "ESQRError",
    "SpainPack",
    "cif_control",
    "dni_letter",
    "document_kind",
    "entity_kind_hint",
    "find_qr_url",
    "is_valid_nif",
    "looks_like_es_qr",
    "lookup_term",
    "map_document_type",
    "normalize_nif",
    "parse_es_qr",
    "qr_to_observations",
    "quarterly_vat_return",
    "rates_on",
    "read_text",
    "validate_nif",
    "vat_return_due",
]
