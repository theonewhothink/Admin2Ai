"""PortugalPack: the Portuguese implementation of CountryPack (§49-50)."""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from datetime import date
from decimal import Decimal

from backoffice.countries.base import (
    FiscalQRResult,
    NamedObservation,
    NativeDocumentType,
    TaxIdCheck,
    Term,
    VATRate,
)
from backoffice.domain.models import DocumentType, ExtractionMethod, utcnow

from . import documents, nif, qr, text_fields
from . import vat as pt_vat


class PortugalPack:
    """Portugal: NIF, IVA by region, SAF-T document types, AT fiscal QR."""

    country_code = "PT"
    country_name = "Portugal"
    currency = "EUR"

    @property
    def document_types(self) -> Mapping[str, NativeDocumentType]:
        return documents.PT_DOCUMENT_TYPES

    @property
    def terminology(self) -> Sequence[Term]:
        return documents.PT_TERMS

    def normalize_tax_id(self, raw: str) -> str | None:
        return nif.normalize_nif(raw)

    def validate_tax_id(self, raw: str) -> TaxIdCheck:
        return nif.validate_nif(raw)

    def map_document_type(self, native_code: str) -> DocumentType | None:
        return documents.map_document_type(native_code)

    def lookup_term(self, label: str) -> Term | None:
        return documents.lookup_term(label)

    def vat_rates(self, on: date, region: str | None = None) -> tuple[VATRate, ...]:
        return pt_vat.rates_on(on, region)

    def is_plausible_vat(
        self,
        net: Decimal,
        vat: Decimal,
        *,
        on: date | None = None,
        region: str | None = None,
    ) -> bool | None:
        day = on or utcnow().date()
        result = pt_vat.check_rate(net, vat, region or pt_vat.PTRegion.MAINLAND, day)
        return None if result is pt_vat.RateCheck.UNKNOWN else result is pt_vat.RateCheck.PLAUSIBLE

    def parse_fiscal_qr(self, payload: str, evidence_id: str) -> FiscalQRResult | None:
        if not qr.looks_like_pt_qr(payload):
            return None
        code = qr.parse_qr(payload)
        return FiscalQRResult(
            country=self.country_code,
            native_doc_type=code.doc_type_code,
            doc_type=code.doc_type,
            observations=tuple(qr.qr_to_observations(code, evidence_id)),
            consistent=code.is_consistent,
            usable=not code.is_cancelled,
            notes=tuple(str(w) for w in code.checks.warnings),
            payload=code,
        )

    def extract_text_fields(
        self,
        text: str,
        source: str,
        *,
        method: ExtractionMethod = ExtractionMethod.OCR,
        known_customer_tax_ids: Collection[str] = (),
    ) -> list[NamedObservation]:
        result = text_fields.extract_text_fields(
            text, source, method=method, known_customer_tax_ids=known_customer_tax_ids
        )
        return list(result.observations)
