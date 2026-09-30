"""SpainPack: the Spanish implementation of CountryPack (§49-50), complete enough to run a company."""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from datetime import date
from decimal import Decimal

from backoffice.countries.base import (
    FiscalQRResult,
    NamedObservation,
    NativeDocumentType,
    PeriodicObligation,
    TaxIdCheck,
    TaxIdKind,
    Term,
    TextReading,
    VATRate,
)
from backoffice.domain.models import DocumentType, ExtractionMethod

from . import documents, holidays, nif, obligations, qr, text_fields
from . import vat as es_vat


class SpainPack:
    """Spain: NIF/NIE/CIF, IVA 21/10/4/0, factura completa / simplificada / rectificativa, Verifactu and
    TicketBAI codes, Spanish labels and letters, the quarterly VAT return (modelo 303)."""

    country_code = "ES"
    country_name = "Spain"
    currency = "EUR"
    language = "es"
    # Lines that start with these words are never the supplier's name.
    title_words = ("factura", "nif", "cif", "fecha", "ticket", "invoice", "numero", "nº", "n.º", "abono",
                   "albaran", "presupuesto", "recibo")

    @property
    def document_types(self) -> Mapping[str, NativeDocumentType]:
        return documents.ES_DOCUMENT_TYPES

    @property
    def terminology(self) -> Sequence[Term]:
        return documents.ES_TERMS

    def normalize_tax_id(self, raw: str) -> str | None:
        check = nif.validate_nif(raw)
        return check.normalized if check.valid else None

    def validate_tax_id(self, raw: str) -> TaxIdCheck:
        return nif.validate_nif(raw)

    def map_document_type(self, native_code: str) -> DocumentType | None:
        return documents.map_document_type(native_code)

    def lookup_term(self, label: str) -> Term | None:
        return documents.lookup_term(label)

    def vat_rates(self, on: date, region: str | None = None) -> tuple[VATRate, ...]:
        return es_vat.rates_on(on, region)

    def is_plausible_vat(self, net: Decimal, vat: Decimal, *, on: date | None = None,
                         region: str | None = None) -> bool | None:
        return es_vat.check_rate(net, vat, on=on, region=region)

    def parse_fiscal_qr(self, payload: str, evidence_id: str) -> FiscalQRResult | None:
        if not qr.looks_like_es_qr(payload):
            return None
        code = qr.parse_es_qr(payload)
        return FiscalQRResult(
            country=self.country_code, native_doc_type="", doc_type=DocumentType.INVOICE,
            observations=tuple(qr.qr_to_observations(code, evidence_id)), consistent=True, usable=True,
            notes=code.notes, payload=code, issuer_tax_id=code.issuer_nif, currency=code.currency,
        )

    def find_fiscal_qr(self, line: str) -> str | None:
        return qr.find_qr_url(line)

    def extract_text_fields(self, text: str, source: str, *, method: ExtractionMethod = ExtractionMethod.OCR,
                            known_customer_tax_ids: Collection[str] = ()) -> list[NamedObservation]:
        return list(self.read_text(text, source, method=method,
                                   known_customer_tax_ids=known_customer_tax_ids).observations)

    def read_text(self, text: str, source: str, *, method: ExtractionMethod = ExtractionMethod.OCR,
                  known_customer_tax_ids: Collection[str] = (),
                  known_supplier_tax_ids: Collection[str] = ()) -> TextReading:
        return text_fields.read_text(text, source, method=method, known_customer_tax_ids=known_customer_tax_ids,
                                     known_supplier_tax_ids=known_supplier_tax_ids)

    def document_kind(self, text: str) -> DocumentType | None:
        return documents.document_kind(text)

    def obligation_vocabulary(self) -> Mapping[str, tuple[str, ...]]:
        return obligations.VOCABULARY

    def periodic_obligations(self, company_id: str, today: date) -> tuple[PeriodicObligation, ...]:
        return obligations.quarterly_vat_return(company_id, today)

    def is_private_person(self, tax_id: str | None) -> bool:
        check = nif.validate_nif(tax_id)
        return check.valid and check.kind is TaxIdKind.PERSON

    def public_holidays(self, year: int) -> frozenset[date]:
        """Spain's national public holidays (regional and local ones are not included)."""
        return holidays.national_holidays(year)

    def document_code(self, text: str) -> tuple[str, str] | None:
        """None: a Spanish document prints no unique code apart from its number (Verifactu and TicketBAI
        codes name the number itself), so an unnumbered copy is never proven the same by a code."""
        return None
