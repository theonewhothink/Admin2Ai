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

A country's own words for the core's other readers and writers (till reports, fraud phrases, the owner's
chat, supplier emails ...) are in its pack too, asked for by concept (:mod:`backoffice.countries.wording`).
"""

from .base import (
    BankFeePolicy,
    BankWording,
    CompanyPack,
    CountryPack,
    CountryPackError,
    DocumentFamily,
    FiscalQRError,
    FiscalQRResult,
    LearnedProfile,
    NamedObservation,
    NativeDocumentType,
    PeriodicObligation,
    TaxIdCheck,
    TaxIdKind,
    TaxProfile,
    TaxSignal,
    Term,
    TextReading,
    UnknownCountryError,
    VATBucket,
    VATRate,
    available_countries,
    DEFAULT_COUNTRY,
    company_countries,
    company_pack,
    easter_sunday,
    get_pack,
    group_by_field,
    register_pack,
    tax_id_hint,
    unregister_pack,
)
from .wording import PACK_WORDS, LazyPattern, pack_alternatives, pack_text, pack_words, spliced

__all__ = [
    "DEFAULT_COUNTRY",
    "PACK_WORDS",
    "BankFeePolicy",
    "BankWording",
    "CompanyPack",
    "CountryPack",
    "CountryPackError",
    "DocumentFamily",
    "FiscalQRError",
    "FiscalQRResult",
    "LazyPattern",
    "LearnedProfile",
    "NamedObservation",
    "NativeDocumentType",
    "PeriodicObligation",
    "TaxIdCheck",
    "TaxIdKind",
    "TaxProfile",
    "TaxSignal",
    "Term",
    "TextReading",
    "UnknownCountryError",
    "VATBucket",
    "VATRate",
    "available_countries",
    "company_countries",
    "company_pack",
    "easter_sunday",
    "get_pack",
    "group_by_field",
    "pack_alternatives",
    "pack_text",
    "pack_words",
    "register_pack",
    "spliced",
    "tax_id_hint",
    "unregister_pack",
]
