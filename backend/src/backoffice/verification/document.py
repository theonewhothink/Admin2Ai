"""Document-level verification (§18, §19, §57).

A document is GREEN only when every *required* critical field is GREEN.
Any RED critical field makes it RED (a contradiction is never outvoted).
Everything else is AMBER. Required fields depend on the document type:
a simplified receipt needs fewer than a full invoice.

Before the fields are judged:

* a currency printed next to an amount ("483,60 €") joins the currency
  field as an observation from that same source, so "$" beside the total
  of a document that says EUR is a conflict, not a detail;
* the amount the bank actually charged joins the gross as an independent
  BANK observation (unless it is in another currency than the document);
* ARITHMETIC observations are derived from each source's own numbers and
  tax lines (:func:`~.arithmetic.derive_observations`).

After, the settled totals must add up (net + VAT = gross, else all three
turn RED: which one is wrong is not guessed) and the VAT must fit an
allowed rate (else the VAT cannot be GREEN).

The due date (checklist F8) is a date like the others: read as a real
calendar date, and compared across sources, so a structured source (an
e-invoice) and the printed text that disagree make it RED. It must also fall
on or after the document's own date: a due date before the issue date cannot
be right, so it is kept on the record but not used (AMBER, no value), never
turned into a question on its own.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal
from types import MappingProxyType
from typing import Any

from backoffice.domain.models import (
    CriticalField,
    DocumentType,
    ExtractionMethod,
    FieldObservation,
    Quality,
    VerifiedField,
)

from ._display import field_label, join
from .arithmetic import (
    ZERO,
    RateCheck,
    RateFit,
    SumCheck,
    TaxBreakdown,
    allowed_rate_values,
    check_rate,
    check_sum,
    check_tax_lines,
    derive_observations,
)
from .fields import DEFAULT_POLICY, FieldAssessment, VerificationPolicy, assess_field
from .normalize import (
    AMOUNT_FIELDS,
    NormalizeHints,
    as_critical_field,
    currency_mark,
    field_name,
    normalize_currency,
)

__all__ = [
    "BANK_CONFIDENCE",
    "DEFAULT_REQUIREMENTS",
    "DUE_BEFORE_ISSUE",
    "DocumentAssessment",
    "assess_document",
    "bank_observation",
    "group_by_field",
    "regraded",
    "required_fields",
    "verify_document",
]

F = CriticalField

# A booked bank amount is exact; it is still one source among others.
BANK_CONFIDENCE = 0.99

_INVOICE = frozenset({
    F.SUPPLIER_TAX_ID, F.INVOICE_NUMBER, F.ISSUE_DATE, F.CURRENCY,
    F.GROSS_AMOUNT, F.NET_AMOUNT, F.VAT_AMOUNT,
})  # fmt: skip
_TOTAL_ONLY = frozenset({F.ISSUE_DATE, F.CURRENCY, F.GROSS_AMOUNT})

# Fields that must be GREEN before a document may close autonomously.
# Product policy, not law: country packs may pass their own table.
DEFAULT_REQUIREMENTS: Mapping[DocumentType, frozenset[CriticalField]] = MappingProxyType({
    DocumentType.INVOICE: _INVOICE,
    DocumentType.INVOICE_RECEIPT: _INVOICE,
    DocumentType.CREDIT_NOTE: _INVOICE,
    DocumentType.DEBIT_NOTE: _INVOICE,
    DocumentType.SIMPLIFIED_INVOICE: frozenset(
        {F.SUPPLIER_TAX_ID, F.INVOICE_NUMBER, F.ISSUE_DATE, F.CURRENCY, F.GROSS_AMOUNT}
    ),
    DocumentType.RECEIPT: _TOTAL_ONLY,
    DocumentType.TAX_NOTICE: frozenset({F.GROSS_AMOUNT, F.DUE_DATE, F.CURRENCY, F.PAYMENT_REFERENCE}),
    DocumentType.STATEMENT: _TOTAL_ONLY,
    DocumentType.PAYROLL: _TOTAL_ONLY,
    DocumentType.LOAN_STATEMENT: _TOTAL_ONLY,
    DocumentType.CONTRACT: frozenset({F.ISSUE_DATE}),
    DocumentType.OTHER: _TOTAL_ONLY,
})  # fmt: skip

_FIELD_ORDER = {f.value: i for i, f in enumerate(CriticalField)}


def required_fields(
    doc_type: DocumentType,
    requirements: Mapping[DocumentType, Collection[CriticalField]] | None = None,
) -> frozenset[CriticalField]:
    """Required fields for ``doc_type``; unknown types fall back to OTHER."""
    table = requirements if requirements is not None else DEFAULT_REQUIREMENTS
    found = table.get(doc_type, table.get(DocumentType.OTHER, DEFAULT_REQUIREMENTS[DocumentType.OTHER]))
    return frozenset(found)


def bank_observation(amount: Decimal, *, source: str = "bank") -> FieldObservation:
    """The charged amount as a BANK observation of the gross (magnitude: money out is negative)."""
    if isinstance(amount, bool) or not isinstance(amount, (Decimal, int)):
        raise TypeError("the bank amount must be Decimal, never float")
    return FieldObservation(
        value=abs(Decimal(amount)),
        source=source,
        method=ExtractionMethod.BANK,
        confidence=BANK_CONFIDENCE,
        location="bank:charged amount",
    )


def group_by_field(observations: Iterable[Any]) -> dict[str, list[FieldObservation]]:
    """Group observations that carry a ``field`` attribute (e.g. a country pack's
    named observations) into the mapping :func:`verify_document` takes."""
    grouped: dict[str, list[FieldObservation]] = {}
    for obs in observations:
        grouped.setdefault(field_name(obs.field), []).append(obs)
    return grouped


# --------------------------------------------------------------------------- result


@dataclass(frozen=True)
class DocumentAssessment:
    quality: Quality
    doc_type: DocumentType
    fields: Mapping[str, FieldAssessment]
    required: frozenset[str]
    sum_check: SumCheck | None
    rate_checks: tuple[RateCheck, ...]
    reasons: tuple[str, ...]

    @property
    def verified_fields(self) -> dict[str, VerifiedField]:
        return {name: a.verified for name, a in self.fields.items()}

    @property
    def conflicts(self) -> tuple[str, ...]:
        return tuple(n for n, a in self.fields.items() if a.quality is Quality.RED)

    @property
    def missing(self) -> tuple[str, ...]:
        """Required fields with no likely value at all."""
        return tuple(
            n for n in self.fields
            if n in self.required and self.fields[n].value is None and self.fields[n].quality is not Quality.RED
        )  # fmt: skip

    def as_tuple(self) -> tuple[dict[str, VerifiedField], Quality]:
        return self.verified_fields, self.quality


# --------------------------------------------------------------------------- steps


def _by_name(
    observations_by_field: Mapping[Any, Iterable[FieldObservation]],
) -> dict[str, list[FieldObservation]]:
    grouped: dict[str, list[FieldObservation]] = {}
    for name, observations in observations_by_field.items():
        grouped.setdefault(field_name(name), []).extend(observations)
    return grouped


def _bank(
    amount: Decimal | FieldObservation | None,
    bank_currency: str | None,
    source: str,
    document_currency: FieldAssessment,
) -> tuple[dict[str, list[FieldObservation]], list[str]]:
    """BANK observations to add, and notes on why the bank charge was left out."""
    if amount is None:
        return {}, []
    obs = amount if isinstance(amount, FieldObservation) else bank_observation(amount, source=source)
    if obs.method is not ExtractionMethod.BANK:
        raise ValueError("the bank amount must be a BANK observation")
    code = _currency_code(bank_currency)
    if document_currency.quality is Quality.RED:
        return {}, ["The currency on this document is unclear, so I didn't compare the bank charge."]
    possible = (document_currency.value,) if document_currency.value else document_currency.possible_values
    if code and possible and code not in possible:
        shown = " or ".join(possible)
        return {}, [f"The bank charge is in {code} and the document in {shown}, so I didn't compare them."]
    added = {F.GROSS_AMOUNT.value: [obs]}
    if code:
        added[F.CURRENCY.value] = [
            FieldObservation(value=code, source=obs.source, method=ExtractionMethod.BANK,
                             confidence=obs.confidence, location="bank:account currency")
        ]  # fmt: skip
    return added, []


def _printed_currencies(observed: Mapping[str, list[FieldObservation]]) -> list[FieldObservation]:
    """Currencies printed with amounts, as currency observations from the same sources."""
    found = []
    for name in _ordered(observed):
        field = as_critical_field(name)
        if field not in AMOUNT_FIELDS:
            continue
        for obs in observed[name]:
            mark = currency_mark(obs.value)
            if mark is not None and obs.method is not ExtractionMethod.ARITHMETIC:  # a sum prints nothing
                found.append(
                    obs.model_copy(
                        update={"value": mark.written, "location": f"currency printed with {name}"}
                    )
                )
    return found


def _currency_code(currency: str | None) -> str | None:
    if currency is None:
        return None
    code = normalize_currency(currency).value
    if not isinstance(code, str):
        raise ValueError(f"not a currency: {currency!r}")
    return code


def _settled(assessment: FieldAssessment | None) -> Any:
    if assessment is None or assessment.quality is Quality.RED:
        return None
    return assessment.value


def _check_totals(
    fields: dict[str, FieldAssessment],
    breakdowns: tuple[TaxBreakdown, ...],
    other: Decimal,
    currency: str | None,
) -> SumCheck | None:
    """net + VAT = gross on the settled values; a mismatch turns all three RED."""
    names = (F.NET_AMOUNT.value, F.VAT_AMOUNT.value, F.GROSS_AMOUNT.value)
    values = [_settled(fields.get(n)) for n in names]
    if any(v is None for v in values):
        return None
    lines = max((len(b.lines) for b in breakdowns), default=1)
    check = check_sum(values[0], values[1], values[2], other_charges=other, lines=lines)
    if not check.ok:
        for name in names:
            fields[name] = fields[name].demote(Quality.RED, check.explain(currency))
    return check


def _check_rates(
    fields: dict[str, FieldAssessment], rates: tuple[Decimal, ...], breakdowns: tuple[TaxBreakdown, ...]
) -> tuple[RateCheck, ...]:
    """Line rates (or the totals' blended rate); a misfit keeps the VAT from GREEN."""
    if not rates:
        return ()
    checks = [c for b in breakdowns for c in check_tax_lines(b.lines, rates)]
    net, vat = _settled(fields.get(F.NET_AMOUNT.value)), _settled(fields.get(F.VAT_AMOUNT.value))
    if not breakdowns and net is not None and vat is not None:
        checks.append(check_rate(net, vat, rates, allow_mixed=True))
    bad = [c for c in checks if c.fit is RateFit.UNEXPECTED]
    name = F.VAT_AMOUNT.value
    for check in bad:
        if name in fields:
            fields[name] = fields[name].demote(Quality.AMBER, check.explain())
    return tuple(checks)


DUE_BEFORE_ISSUE = "The due date is before the document's date, so I don't use it."


def _check_due_date(fields: dict[str, FieldAssessment]) -> None:
    """A due date before the document's own date cannot be right: kept, not used (F8)."""
    due, issue = fields.get(F.DUE_DATE.value), fields.get(F.ISSUE_DATE.value)
    if due is None or issue is None:
        return
    on, issued = _settled(due), _settled(issue)
    if not (isinstance(on, date) and isinstance(issued, date)):
        return
    if isinstance(on, datetime) or isinstance(issued, datetime):
        on, issued = (on.date() if isinstance(on, datetime) else on), (
            issued.date() if isinstance(issued, datetime) else issued)
    if on < issued:
        fields[F.DUE_DATE.value] = replace(due.demote(Quality.AMBER, DUE_BEFORE_ISSUE), value=None)


def _quality(fields: Mapping[str, FieldAssessment], required: frozenset[str]) -> Quality:
    critical = [a for n, a in fields.items() if as_critical_field(n) is not None]
    if any(a.quality is Quality.RED for a in critical):
        return Quality.RED
    if all(fields[n].quality is Quality.GREEN for n in required):
        return Quality.GREEN
    return Quality.AMBER


def _summary(quality: Quality, fields: Mapping[str, FieldAssessment], required: frozenset[str]) -> list[str]:
    if quality is Quality.GREEN:
        return ["Everything I need on this document is confirmed."]
    if quality is Quality.RED:
        red = [field_label(n) for n, a in fields.items() if a.quality is Quality.RED and as_critical_field(n)]
        return [f"The sources disagree on {join(red)}. A person needs to check."]
    open_ = [n for n in fields if n in required and fields[n].quality is not Quality.GREEN]
    missing = [field_label(n) for n in open_ if fields[n].value is None]
    unconfirmed = [field_label(n) for n in open_ if fields[n].value is not None]
    lines = []
    if missing:
        lines.append(f"I still need {join(missing)}.")
    if unconfirmed:
        lines.append(f"I still need to confirm {join(unconfirmed)}.")
    return lines


def _ordered(names: Iterable[str]) -> list[str]:
    return sorted(set(names), key=lambda n: (_FIELD_ORDER.get(n, len(_FIELD_ORDER)), n))


# --------------------------------------------------------------------------- entry points


def assess_document(
    observations_by_field: Mapping[CriticalField | str, Iterable[FieldObservation]],
    allowed_rates: Iterable[object] = (),
    bank_amount: Decimal | FieldObservation | None = None,
    *,
    doc_type: DocumentType = DocumentType.INVOICE,
    required: Collection[CriticalField | str] | None = None,
    requirements: Mapping[DocumentType, Collection[CriticalField]] | None = None,
    bank_currency: str | None = None,
    bank_source: str = "bank",
    breakdowns: Iterable[TaxBreakdown] = (),
    other_charges: Decimal = ZERO,
    hints: NormalizeHints | None = None,
    policy: VerificationPolicy = DEFAULT_POLICY,
) -> DocumentAssessment:
    """Verify every field of one document and grade the document (module docstring).

    ``bank_amount`` must be the charge for *this* document (a 1-to-1 match
    from reconciliation); partial payments do not belong here. Pass
    ``bank_currency`` (the account's currency): without it the charge is
    assumed to be in the document's currency.
    ``required`` overrides the per-type table. Non-critical fields are
    verified too but never decide the document's quality.
    ``other_charges`` is signed: positive adds to the total, negative takes
    something off (see :mod:`.arithmetic`).
    """
    required_names = frozenset(
        field_name(f) for f in (required if required is not None else required_fields(doc_type, requirements))
    )
    if not required_names:
        raise ValueError("at least one field must be required: nothing closes without evidence")
    rates = allowed_rate_values(allowed_rates)
    tax_lines = tuple(breakdowns)
    observed = _by_name(observations_by_field)
    printed = _printed_currencies(observed)
    if printed:
        observed.setdefault(F.CURRENCY.value, []).extend(printed)
    currency = assess_field(F.CURRENCY, observed.get(F.CURRENCY.value, ()), policy=policy, hints=hints)
    bank, notes = _bank(bank_amount, bank_currency, bank_source, currency)
    for name, extra in bank.items():
        observed.setdefault(name, []).extend(extra)
    for field, derived in derive_observations(observed, breakdowns=tax_lines, other_charges=other_charges,
                                              hints=hints).items():  # fmt: skip
        observed.setdefault(field.value, []).extend(derived)
    shown_currency = _settled(currency)
    if shown_currency is None and bank:
        shown_currency = _currency_code(bank_currency)
    fields = {
        name: assess_field(name, observed.get(name, ()), policy=policy, hints=hints, currency=shown_currency)
        for name in _ordered([*observed, *required_names])
    }
    sum_check = _check_totals(fields, tax_lines, other_charges, shown_currency)
    rate_checks = _check_rates(fields, rates, tax_lines)
    _check_due_date(fields)
    quality = _quality(fields, required_names)
    return DocumentAssessment(
        quality=quality,
        doc_type=doc_type,
        fields=MappingProxyType(fields),
        required=required_names,
        sum_check=sum_check,
        rate_checks=rate_checks,
        reasons=(*_summary(quality, fields, required_names), *notes),
    )


def verify_document(
    observations_by_field: Mapping[CriticalField | str, Iterable[FieldObservation]],
    allowed_rates: Iterable[object] = (),
    bank_amount: Decimal | FieldObservation | None = None,
    **options: Any,
) -> tuple[dict[str, VerifiedField], Quality]:
    """``(fields, quality)`` for one document; options as in :func:`assess_document`."""
    return assess_document(observations_by_field, allowed_rates, bank_amount, **options).as_tuple()


def regraded(assessment: DocumentAssessment, fields: Mapping[str, FieldAssessment]) -> DocumentAssessment:
    """The same document with some fields judged again by a rule that holds for it (e.g. the rules for a
    document from abroad, :mod:`.foreign`): its quality and summary follow the fields; other notes stay."""
    old_summary = _summary(assessment.quality, assessment.fields, assessment.required)
    notes = [r for r in assessment.reasons if r not in old_summary]
    table = dict(fields)
    quality = _quality(table, assessment.required)
    return DocumentAssessment(
        quality=quality,
        doc_type=assessment.doc_type,
        fields=MappingProxyType(table),
        required=assessment.required,
        sum_check=assessment.sum_check,
        rate_checks=assessment.rate_checks,
        reasons=(*_summary(quality, table, assessment.required), *notes),
    )
