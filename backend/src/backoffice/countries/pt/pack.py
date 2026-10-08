"""PortugalPack: the Portuguese implementation of CountryPack (§49-50)."""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping, Sequence
from typing import Any
from datetime import date
from decimal import Decimal

from backoffice.countries.base import (
    BankFeePolicy,
    BankWording,
    FiscalQRResult,
    LearnedProfile,
    NamedObservation,
    NativeDocumentType,
    PeriodicObligation,
    TaxIdCheck,
    TaxProfile,
    TaxSignal,
    Term,
    VATRate,
)
from backoffice.domain.models import DocumentType, ExtractionMethod, VatPart

from . import atcud, banking_words, documents, holidays, nif, qr, text_fields
from . import letters as pt_letters
from . import vocabulary as pt_vocabulary
from . import calendar as pt_calendar
from . import obligations as pt_obligations
from . import vat as pt_vat


_QR_START = re.compile(r"A:\d{9}\*B:")
_ATCUD = re.compile(r"\bATCUD\s*[:\-]?\s*([A-Z0-9]{8,}-\d+)\b", re.IGNORECASE)
_ZERO = Decimal(0)


class PortugalPack:
    """Portugal: NIF, IVA by region, SAF-T document types, AT fiscal QR."""

    country_code = "PT"
    country_name = "Portugal"
    currency = "EUR"
    language = "pt"
    # Lines that start with these words are never the supplier's name.
    title_words = ("nif", "fatura", "invoice", "data", "atcud")
    tax_id_hint = "NIF. It has 9 digits."

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
        """What Portuguese letters say (Autoridade Tributária, Segurança Social, "data limite de pagamento",
        "comprovativo de entrega"...), read with the core's English (backoffice.closure.obligations)."""
        return pt_obligations.VOCABULARY

    def vocabulary(self) -> Mapping[str, tuple[str, ...]]:
        """Portuguese words for the core's readers and writers, by concept (:mod:`.vocabulary`)."""
        return pt_vocabulary.VOCABULARY

    def supplier_letters(self) -> pt_letters.PortugueseLetters:
        """Requests to suppliers in Portuguese (:mod:`.letters`)."""
        return pt_letters.LETTERS

    def invoice_sites(self) -> tuple[Any, ...]:
        """Portuguese suppliers' invoice websites (:mod:`.invoice_sites`)."""
        from . import invoice_sites as pt_invoice_sites  # lazy: it imports the core's site description

        return pt_invoice_sites.SITES

    def bank_wording(self) -> BankWording:
        """How Portuguese bank statements word taxes, fees, salaries, loans, grants and the tourist tax."""
        return banking_words.BANK_WORDING

    def parse_amount(self, text: str) -> Decimal | None:
        """'1.492,30' -> Decimal('1492.30'); None when the text is not one amount."""
        return text_fields.parse_pt_amount(text)

    def fiscal_qr_payload(self, fields: Mapping[str, str]) -> str:
        """An AT fiscal QR payload with ``fields`` in the order the specification fixes (empty ones left out)."""
        return "*".join(f"{k}:{fields[k]}" for k in qr.FIELD_ORDER if fields.get(k) not in (None, ""))

    def periodic_obligations(self, company_id: str, today: date,
                             profile: TaxProfile | None = None) -> tuple[PeriodicObligation, ...]:
        """Portugal's statutory calendar (VAT, invoice report, salaries, Social Security, Modelo 22, IES, Modelo 10,
        advance payments) for what is known of the company (:mod:`backoffice.countries.pt.calendar`)."""
        return pt_calendar.obligations(company_id, today, profile)

    def learn_tax_profile(self, signals: Sequence[TaxSignal], today: date) -> LearnedProfile:
        """VAT rhythm, salaries and advance payments, from the company's own payments and payslips."""
        return pt_calendar.learn_profile(signals, today=today)

    def calendar_proof(self, text: str, calendar: str, period: str, *, payment: bool,
                       on: date | None = None) -> bool:
        return pt_calendar.calendar_proof(text, calendar, period, payment=payment, on=on)

    def bank_fee_policy(self) -> BankFeePolicy:
        """Bank fees, commissions, stamp duty and interest: the statement is enough in Portugal."""
        return banking_words.BANK_FEE_POLICY

    def is_private_person(self, tax_id: str | None) -> bool:
        """A NIF starting with 1, 2 or 3 belongs to a private person (the check digits are not needed)."""
        return bool(tax_id) and str(tax_id)[:1] in "123"

    def public_holidays(self, year: int) -> frozenset[date]:
        """Portugal's national public holidays (Código do Trabalho, art. 234)."""
        return holidays.national_holidays(year)

    def document_code(self, text: str) -> tuple[str, str] | None:
        """The ATCUD printed on a Portuguese document (its unique code), when one valid code is there."""
        found = set()
        for raw in _ATCUD.findall(text or ""):
            try:
                found.add(str(atcud.parse_atcud(raw.upper())))
            except atcud.ATCUDError:
                continue
        return ("ATCUD", found.pop()) if len(found) == 1 else None

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
