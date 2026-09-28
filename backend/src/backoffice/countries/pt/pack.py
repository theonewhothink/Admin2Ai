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
from backoffice.domain.models import DocumentType, ExtractionMethod

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
        """Rates in force on ``on``; empty for a region Portugal does not tax."""
        region_key = _region(region) if region is not None else None
        if region is not None and region_key is None:
            return ()
        return pt_vat.rates_on(on, region_key)

    def is_plausible_vat(
        self,
        net: Decimal,
        vat: Decimal,
        *,
        on: date | None = None,
        region: str | None = None,
    ) -> bool | None:
        """None when the table does not cover the region or date.

        Without ``on`` the rates currently in the table are used (never the clock).
        """
        region_key = _region(region) if region is not None else pt_vat.PTRegion.MAINLAND
        if region_key is None:
            return None
        result = pt_vat.check_rate(net, vat, region_key, on)
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
            usable=_can_support_payment(code),
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


def _region(value: str) -> pt_vat.PTRegion | None:
    try:
        return pt_vat.PTRegion.parse(value)
    except ValueError:
        return None


def _can_support_payment(code: qr.PTQRCode) -> bool:
    """Not cancelled, and a tax invoice or a receipt (§3: closure needs real evidence).

    Pro-formas, quotes, orders and transport documents also carry the AT QR
    code but never evidence a purchase or a payment.
    """
    entry = documents.get_document_type(code.doc_type_code)
    if code.is_cancelled or entry is None:
        return False
    return entry.fiscal_invoice or entry.proves_payment
