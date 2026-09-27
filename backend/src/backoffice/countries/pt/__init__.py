"""Portugal country pack (§49-50).

Generic callers should go through the registry::

    from backoffice.countries import get_pack
    pack = get_pack("PT")

Portugal-specific callers can use the functions below directly.

Tax numbers (nif)
    validate_nif(raw, allow_placeholder=False) -> TaxIdCheck
    normalize_nif(raw) -> str | None            "PT 123 456 789" -> "123456789"
    entity_kind_hint(raw) -> TaxIdKind           person / company / ...
ATCUD and document numbers (atcud)
    parse_atcud(raw) -> ATCUD                    "CSDF7T5H-0035"
    parse_document_number(raw) -> DocumentNumber "FT 2026/183"
    link_atcud(atcud, document, registry=()) -> ATCUDLink
AT fiscal QR code (qr)
    parse_qr(payload) -> PTQRCode                raises QRCodeError when malformed
    qr_to_observations(code, evidence_id) -> list[NamedObservation]
VAT (vat)
    is_plausible_rate(net, vat, region="PT", on=None) -> bool
    check_rate(...) -> RateCheck;  rates_on(day, region) -> tuple[VATRate, ...]
Documents and vocabulary (documents)
    map_document_type(code) -> DocumentType | None;  lookup_term(label) -> Term | None
Text (text_fields)
    extract_text_fields(text, source, ...) -> TextFieldsResult
    parse_pt_amount("1.492,30") -> Decimal;  parse_pt_date("18/09/2026") -> date
Bank details (banking)
    is_valid_iban(raw), find_ibans(text), parse_multibanco(entity, reference)
SAF-T (saft)
    parse_saft_sales_invoices(xml) -> SaftSalesInvoices
    saft_invoice_observations(invoice, header, evidence_id)
    export_ledger_csv(rows) -> LedgerExport     subset CSV, explicitly not SAF-T
"""

from backoffice.countries.base import register_pack

from .atcud import (
    ATCUD,
    ATCUD_MANDATORY_FROM,
    ATCUDError,
    ATCUDLink,
    DocumentNumber,
    SeriesRegistration,
    is_valid_atcud,
    link_atcud,
    parse_atcud,
    parse_document_number,
)
from .banking import (
    MultibancoError,
    MultibancoReference,
    find_ibans,
    is_valid_iban,
    normalize_iban,
    parse_multibanco,
)
from .documents import (
    PT_DOCUMENT_TYPES,
    PT_TERMS,
    get_document_type,
    lookup_term,
    map_document_type,
    preferred_code,
    status_label,
)
from .nif import (
    FINAL_CONSUMER_NIF,
    entity_kind_hint,
    is_final_consumer,
    is_valid_nif,
    nif_check_digit,
    normalize_nif,
    validate_nif,
)
from .pack import PortugalPack
from .qr import (
    PTQRCode,
    QRChecks,
    QRCodeError,
    QRIssue,
    QRTaxBlock,
    looks_like_pt_qr,
    parse_qr,
    qr_to_observations,
)
from .saft import (
    LEDGER_NOTICE,
    LedgerExport,
    LedgerRow,
    SaftError,
    SaftHeader,
    SaftInvoice,
    SaftSalesInvoices,
    SaftSecurityError,
    export_ledger_csv,
    ledger_row_from_document,
    parse_saft_sales_invoices,
    saft_invoice_observations,
)
from .text_fields import TextFieldsResult, extract_text_fields, parse_pt_amount, parse_pt_date
from .vat import (
    PT_VAT_RATES,
    PTRegion,
    RateCheck,
    RateDataUnavailable,
    check_rate,
    is_plausible_rate,
    match_rate,
    rate_for,
    rates_on,
)

PACK = PortugalPack()
register_pack(PACK)

__all__ = [
    "ATCUD", "ATCUD_MANDATORY_FROM", "ATCUDError", "ATCUDLink", "DocumentNumber",
    "FINAL_CONSUMER_NIF", "LEDGER_NOTICE", "LedgerExport", "LedgerRow", "MultibancoError",
    "MultibancoReference", "PACK", "PTQRCode", "PTRegion", "PT_DOCUMENT_TYPES", "PT_TERMS",
    "PT_VAT_RATES", "PortugalPack", "QRChecks", "QRCodeError", "QRIssue", "QRTaxBlock",
    "RateCheck", "RateDataUnavailable", "SaftError", "SaftHeader", "SaftInvoice",
    "SaftSalesInvoices", "SaftSecurityError", "SeriesRegistration", "TextFieldsResult",
    "check_rate", "entity_kind_hint", "export_ledger_csv", "extract_text_fields", "find_ibans",
    "get_document_type", "is_final_consumer", "is_plausible_rate", "is_valid_atcud",
    "is_valid_iban", "is_valid_nif", "ledger_row_from_document", "link_atcud",
    "looks_like_pt_qr", "lookup_term", "map_document_type", "match_rate", "nif_check_digit",
    "normalize_iban", "normalize_nif", "parse_atcud", "parse_document_number",
    "parse_multibanco", "parse_pt_amount", "parse_pt_date", "parse_qr",
    "parse_saft_sales_invoices", "preferred_code", "qr_to_observations", "rate_for",
    "rates_on", "saft_invoice_observations", "status_label", "validate_nif",
]
