"""Checking a document from a supplier outside Portugal (checklist P7, case 49).

A Portuguese invoice is proved by its fiscal QR code against its text, with
Portuguese VAT rates. A foreign invoice has no fiscal QR code and follows
its own country's VAT. It can still be proved, by rules that hold anywhere:

* its total and currency are confirmed by an independent source: the bank's
  charge (the booked amount, or the original amount on the bank's
  conversion line when the card was charged in another currency) or a
  structured copy;
* net + VAT = total;
* the VAT is one rate valid in the issuer's country, or zero when the
  caller says zero is right (reverse charge; a supplier outside the EU);
  a rate the document prints must be that same rate;
* the supplier's VAT number passes its own country's format and, where
  they exist, check digits;
* the invoice number and date are printed in the document's own text (never
  only read from a scan), nothing contradicts them, and the payment date
  fits the invoice date.

Only when the amounts hold are the single-source fields (invoice number,
date, VAT number) accepted: the payment that matches the whole document is
what confirms them. Anything short of this stays AMBER with its reason
(§57: never promoted to look better). The caller supplies the country facts
(:class:`ForeignRules`); this module knows no country.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal

from backoffice.domain.models import CriticalField, DocumentType, ExtractionMethod, FieldObservation, Quality

from ._display import day, percent
from .arithmetic import ZERO, RateFit, check_rate
from .document import DocumentAssessment, assess_document, regraded, required_fields
from .fields import DEFAULT_POLICY, FieldAssessment, VerificationPolicy
from .normalize import comparison_key, normalize_value

__all__ = [
    "PAYMENT_LAG_DAYS",
    "PAYMENT_LEAD_DAYS",
    "BankCharge",
    "ForeignRules",
    "assess_foreign",
    "confirm_foreign",
    "foreign_requirements",
]

F = CriticalField

PAYMENT_LEAD_DAYS = 7  # a card is sometimes charged a few days before the invoice is issued
PAYMENT_LAG_DAYS = 31  # and an invoice is normally paid within a month

# Sources that are the document itself (not a scan's reading of it).
_DOCUMENT_METHODS = frozenset({
    ExtractionMethod.EMBEDDED_TEXT, ExtractionMethod.STRUCTURED_XML, ExtractionMethod.HTML_STRUCTURED,
    ExtractionMethod.API, ExtractionMethod.QR, ExtractionMethod.BARCODE, ExtractionMethod.HUMAN,
})  # fmt: skip


@dataclass(frozen=True)
class ForeignRules:
    """What the issuer's country allows, from the caller (``backoffice.countries.foreign``).

    ``rates`` are the positive VAT rates valid on the document's date;
    ``zero_vat`` says, in plain words, why no VAT may be right (None: it is
    not). ``vat_number`` is the issuer's number when it passed its country's
    checks, and ``vat_number_check`` the plain sentence saying so.
    """

    country: str
    country_name: str
    rates: tuple[Decimal, ...] = ()
    zero_vat: str | None = None
    vat_number: str | None = None
    vat_number_check: str = ""
    vat_number_required: bool = True
    stated_rates: tuple[Decimal, ...] = ()

    @property
    def allowed_rates(self) -> tuple[Decimal, ...]:
        extra = (ZERO,) if self.zero_vat else ()
        return tuple(sorted({*self.rates, *extra}))


@dataclass(frozen=True)
class BankCharge:
    """What the bank says was paid, in the document's currency.

    ``converted``: the amount is the original amount on the bank's
    conversion line (the card was charged in another currency).
    """

    amount: Decimal
    currency: str
    booked_on: date
    source: str = "bank"
    converted: bool = False

    def __post_init__(self) -> None:
        if isinstance(self.amount, float) or not isinstance(self.amount, Decimal):
            raise TypeError("money must be Decimal, never float")


def foreign_requirements(doc_type: DocumentType, rules: ForeignRules) -> frozenset[CriticalField]:
    """The usual fields for the document type; the supplier's VAT number only where one is due
    (EU and UK invoices) or printed."""
    required = required_fields(doc_type)
    if not rules.vat_number_required and rules.vat_number is None:
        required -= {F.SUPPLIER_TAX_ID}
    return required


def assess_foreign(
    observations_by_field: Mapping[CriticalField | str, Iterable[FieldObservation]],
    rules: ForeignRules,
    *,
    doc_type: DocumentType = DocumentType.INVOICE,
    bank: BankCharge | None = None,
    policy: VerificationPolicy = DEFAULT_POLICY,
) -> DocumentAssessment:
    """Verify a foreign document: the usual field rules with the issuer's VAT rates, then
    :func:`confirm_foreign`."""
    assessment = assess_document(
        observations_by_field, rules.allowed_rates, bank.amount if bank is not None else None,
        doc_type=doc_type, required=foreign_requirements(doc_type, rules),
        bank_currency=bank.currency if bank is not None else None,
        bank_source=bank.source if bank is not None else "bank", policy=policy,
    )  # fmt: skip
    return confirm_foreign(assessment, rules, bank, policy=policy)


def _green(fields: Mapping[str, FieldAssessment], field: CriticalField) -> bool:
    found = fields.get(field.value)
    return found is not None and found.quality is Quality.GREEN


def _agreeing(fa: FieldAssessment, policy: VerificationPolicy) -> tuple[FieldObservation, ...] | None:
    """The observations showing ``fa.value`` when every reading agrees with it; None otherwise."""
    if fa.value is None or fa.quality is Quality.RED:
        return None
    key = comparison_key(fa.name, fa.value)
    agreeing = []
    for obs in fa.observations:
        normalized = normalize_value(fa.name, obs.value)
        if not normalized.readable:
            continue
        keys = {comparison_key(fa.name, c) for c in normalized.candidates}
        if key not in keys:
            return None  # something reads it otherwise: never settle it by rule
        if len(keys) == 1 and obs.confidence >= policy.min_confidence:
            agreeing.append(obs)
    return tuple(agreeing) or None


def _printed(fa: FieldAssessment | None, policy: VerificationPolicy) -> tuple[FieldObservation, ...] | None:
    """Agreeing readings of which one is the document's own text or data (not a scan)."""
    if fa is None or fa.quality is not Quality.AMBER:
        return None
    agreeing = _agreeing(fa, policy)
    if not agreeing or not any(o.method in _DOCUMENT_METHODS for o in agreeing):
        return None
    return agreeing


def _confirm(fa: FieldAssessment, support: tuple[FieldObservation, ...], reason: str) -> FieldAssessment:
    return replace(fa, quality=Quality.GREEN, reasons=(reason,), supporting=support)


def _by_bank(fa: FieldAssessment) -> bool:
    return any(o.method is ExtractionMethod.BANK for o in fa.supporting)


def confirm_foreign(
    assessment: DocumentAssessment,
    rules: ForeignRules,
    bank: BankCharge | None = None,
    *,
    policy: VerificationPolicy = DEFAULT_POLICY,
) -> DocumentAssessment:
    """Accept what the rules in the module docstring prove; leave everything else as it was."""
    if assessment.quality is not Quality.AMBER:
        return assessment
    fields = dict(assessment.fields)
    if not (_green(fields, F.GROSS_AMOUNT) and _green(fields, F.CURRENCY)):
        return assessment
    if not _by_bank(fields[F.GROSS_AMOUNT.value]):
        total = "the confirmed total"
    elif bank is not None and bank.converted:
        total = "the amount on the bank's conversion line"
    else:
        total = "the total the bank charged"
    split = {F.NET_AMOUNT.value, F.VAT_AMOUNT.value}
    if split & assessment.required and not _confirm_split(fields, assessment, rules, total, policy):
        return regraded(assessment, fields)
    number = fields.get(F.INVOICE_NUMBER.value)
    support = _printed(number, policy)
    if number is not None and support:
        fields[number.name] = _confirm(number, support, "Printed on the invoice, and its amounts are confirmed.")
    issued = fields.get(F.ISSUE_DATE.value)
    support = _printed(issued, policy)
    if issued is not None and support and bank is not None and isinstance(issued.value, date):
        lag = (bank.booked_on - issued.value).days
        if -PAYMENT_LEAD_DAYS <= lag <= PAYMENT_LAG_DAYS:
            reason = f"Printed on the invoice, and the payment on {day(bank.booked_on)} fits this date."
            fields[issued.name] = _confirm(issued, support, reason)
    supplier = fields.get(F.SUPPLIER_TAX_ID.value)
    support = _printed(supplier, policy)
    if supplier is not None and support and rules.vat_number is not None:
        mine = comparison_key(supplier.name, normalize_value(supplier.name, rules.vat_number).value)
        if mine == comparison_key(supplier.name, supplier.value):
            fields[supplier.name] = _confirm(supplier, support, rules.vat_number_check)
    return regraded(assessment, fields)


def _confirm_split(
    fields: dict[str, FieldAssessment],
    assessment: DocumentAssessment,
    rules: ForeignRules,
    total: str,
    policy: VerificationPolicy,
) -> bool:
    """Net and VAT add up to the confirmed total at one rate valid for the issuer (or zero, when allowed)."""
    net, vat = fields.get(F.NET_AMOUNT.value), fields.get(F.VAT_AMOUNT.value)
    if net is None or vat is None or net.value is None or vat.value is None:
        return False
    if assessment.sum_check is None or not assessment.sum_check.ok:
        return False
    fit = check_rate(net.value, vat.value, rules.allowed_rates)
    if fit.fit is not RateFit.MATCHES or fit.rate is None:
        return False
    if any(stated != fit.rate for stated in rules.stated_rates):
        return False
    if fit.rate == ZERO:
        reason = f"It adds up to {total}, with no VAT: {rules.zero_vat}."
    else:
        reason = f"It adds up to {total}, with VAT at {percent(fit.rate)}, a rate used in {rules.country_name}."
    confirmed = {}
    for fa in (net, vat):
        if fa.quality is Quality.GREEN:
            continue
        support = _agreeing(fa, policy)
        if support is None:
            return False
        confirmed[fa.name] = _confirm(fa, support, reason)
    fields.update(confirmed)
    return True
