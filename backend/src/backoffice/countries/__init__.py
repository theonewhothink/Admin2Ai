"""Country packs (§49-50): one global core, country specifics behind a protocol.

    from backoffice.countries import get_pack
    pack = get_pack("PT")                       # lazily loads the Portugal pack
    pack.validate_tax_id("PT 123 456 789")      # -> TaxIdCheck
    pack.parse_fiscal_qr(payload, evidence_id)  # -> FiscalQRResult | None
    pack.extract_text_fields(text, evidence_id) # -> list[NamedObservation]

Importing this package does not import any country.

Documents from countries without a pack (checklist P7) are handled by
:mod:`backoffice.countries.foreign`, which is not a pack: it says which country
issued a document, checks foreign VAT numbers, lists EU and UK VAT rates and
reads English and Spanish invoice labels, so nothing is read as Portuguese
merely because the company is.
"""

from .base import (
    CountryPack,
    CountryPackError,
    DocumentFamily,
    FiscalQRError,
    FiscalQRResult,
    NamedObservation,
    NativeDocumentType,
    TaxIdCheck,
    TaxIdKind,
    Term,
    UnknownCountryError,
    VATBucket,
    VATRate,
    available_countries,
    get_pack,
    group_by_field,
    register_pack,
    unregister_pack,
)

__all__ = [
    "CountryPack",
    "CountryPackError",
    "DocumentFamily",
    "FiscalQRError",
    "FiscalQRResult",
    "NamedObservation",
    "NativeDocumentType",
    "TaxIdCheck",
    "TaxIdKind",
    "Term",
    "UnknownCountryError",
    "VATBucket",
    "VATRate",
    "available_countries",
    "get_pack",
    "group_by_field",
    "register_pack",
    "unregister_pack",
]
