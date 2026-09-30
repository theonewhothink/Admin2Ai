"""PortugalPack: the Portuguese implementation of CountryPack (§49-50)."""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping, Sequence
from datetime import date
from decimal import Decimal

from backoffice.countries.base import (
    FiscalQRResult,
    NamedObservation,
    NativeDocumentType,
    PeriodicObligation,
    TaxIdCheck,
    Term,
    VATRate,
)
from backoffice.domain.models import DocumentType, ExtractionMethod, VatPart

from . import documents, nif, qr, text_fields
from . import vat as pt_vat


_QR_START = re.compile(r"A:\d{9}\*B:")
_ZERO = Decimal(0)


class PortugalPack:
    """Portugal: NIF, IVA by region, SAF-T document types, AT fiscal QR."""

    country_code = "PT"
    country_name = "Portugal"
    currency = "EUR"
    language = "pt"
    # Lines that start with these words are never the supplier's name.
    title_words = ("nif", "fatura", "invoice", "data", "atcud")

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
            issuer_tax_id=code.issuer_nif,
            buyer_is_final_consumer=code.buyer_is_final_consumer,
            currency=code.currency,
            vat_parts=_vat_parts(code),
        )

    def find_fiscal_qr(self, line: str) -> str | None:
        """The AT fiscal QR payload in a decoded-QR text line ("A:509442013*B:..."), or None."""
        m = _QR_START.search(line or "")
        if m and qr.looks_like_pt_qr(line[m.start():].strip()):
            return line[m.start():].strip()
        return None

    def read_text(
        self,
        text: str,
        source: str,
        *,
        method: ExtractionMethod = ExtractionMethod.OCR,
        known_customer_tax_ids: Collection[str] = (),
        known_supplier_tax_ids: Collection[str] = (),
    ) -> text_fields.TextFieldsResult:
        """Portuguese labels, NIFs with their roles and the final consumer (a TextReading and more)."""
        return text_fields.extract_text_fields(text, source, method=method,
                                               known_customer_tax_ids=known_customer_tax_ids,
                                               known_supplier_tax_ids=known_supplier_tax_ids)

    def document_kind(self, text: str) -> DocumentType | None:
        """None: the core's own names (Portuguese and English) are Portugal's."""
        return None

    def obligation_vocabulary(self) -> Mapping[str, tuple[str, ...]]:
        """Nothing to add: the core reads Portuguese and English letters already."""
        return {}

    def periodic_obligations(self, company_id: str, today: date) -> tuple[PeriodicObligation, ...]:
        """None yet: Portuguese deadlines come from the letters and messages that announce them."""
        return ()

    def is_private_person(self, tax_id: str | None) -> bool:
        """A NIF starting with 1, 2 or 3 belongs to a private person (the check digits are not needed)."""
        return bool(tax_id) and str(tax_id)[:1] in "123"

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


def _vat_parts(code: qr.PTQRCode) -> tuple[VatPart, ...]:
    """The code's amounts by VAT rate (rates in percent; exempt at 0%, non-taxable without a rate)."""
    merged: dict[Decimal | None, tuple[Decimal, Decimal]] = {}

    def add(rate: Decimal | None, net: Decimal, vat: Decimal) -> None:
        old = merged.get(rate, (_ZERO, _ZERO))
        merged[rate] = (old[0] + net, old[1] + vat)

    for block in code.tax_blocks:
        if block.exempt_base:
            add(Decimal(0), block.exempt_base, _ZERO)
        for bucket, base, vat in block.rate_pairs():
            try:
                fraction = pt_vat.rate_for(block.region, bucket, code.issue_date)
            except (ValueError, KeyError):
                fraction = None
            pct = None
            if fraction is not None:
                pct = fraction * 100
                pct = pct.quantize(Decimal(1)) if pct == pct.to_integral_value() else pct.normalize()
            add(pct, base, vat)
    for extra in (code.non_taxable, code.stamp_duty):
        if extra:
            add(None, extra, _ZERO)
    return tuple(VatPart(rate=rate, net=net, vat=vat) for rate, (net, vat) in merged.items())


def _can_support_payment(code: qr.PTQRCode) -> bool:
    """Not cancelled, and a tax invoice or a receipt (§3: closure needs real evidence).

    Pro-formas, quotes, orders and transport documents also carry the AT QR
    code but never evidence a purchase or a payment.
    """
    entry = documents.get_document_type(code.doc_type_code)
    if code.is_cancelled or entry is None:
        return False
    return entry.fiscal_invoice or entry.proves_payment
