"""Country packs (§49-50): one global core, country specifics behind a protocol.

    from backoffice.countries import get_pack
    pack = get_pack("PT")                       # lazily loads the Portugal pack (also "ES": Spain)
    pack.validate_tax_id("PT 123 456 789")      # -> TaxIdCheck
    pack.parse_fiscal_qr(payload, evidence_id)  # -> FiscalQRResult | None
    pack.extract_text_fields(text, evidence_id) # -> list[NamedObservation]

Importing this package does not import any country.

A company runs on its own country's pack (``company_pack(country)``): Portugal and Spain.
Documents from another country than the company's (checklist P7) are handled by
:mod:`backoffice.countries.foreign`, which is not a pack: it says which country
issued a document, checks foreign VAT numbers, lists EU and UK VAT rates and
reads English and Spanish invoice labels, so nothing is read as Portuguese
merely because the company is.
"""

from .base import (
    CompanyPack,
    CountryPack,
    CountryPackError,
    DocumentFamily,
    FiscalQRError,
    FiscalQRResult,
    NamedObservation,
    NativeDocumentType,
    PeriodicObligation,
    TaxIdCheck,
    TaxIdKind,
    Term,
    TextReading,
    UnknownCountryError,
    VATBucket,
    VATRate,
    available_countries,
    company_countries,
    company_pack,
    get_pack,
    group_by_field,
    register_pack,
    unregister_pack,
)

__all__ = [
    "CompanyPack",
    "CountryPack",
    "CountryPackError",
    "DocumentFamily",
    "FiscalQRError",
    "FiscalQRResult",
    "NamedObservation",
    "NativeDocumentType",
    "PeriodicObligation",
    "TaxIdCheck",
    "TaxIdKind",
    "Term",
    "TextReading",
    "UnknownCountryError",
    "VATBucket",
    "VATRate",
    "available_countries",
    "company_countries",
    "company_pack",
    "get_pack",
    "group_by_field",
    "register_pack",
    "unregister_pack",
]
