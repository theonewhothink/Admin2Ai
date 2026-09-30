"""Verification Agent logic: deterministic, evidence-first (§18, §19, §26, §56-57).

Quality levels: GREEN = verified (independent sources agree), AMBER = likely
(not enough for autonomous closure), RED = conflict (a person must look).
AMBER is never promoted to GREEN to improve statistics, and a disagreement is
reported with every value, never settled by picking one.

Public API
----------

Fields::

    verify_field(name, observations, *, policy=, hints=, currency=) -> VerifiedField
    assess_field(...) -> FieldAssessment          # + supporting / conflicting / possible values
    order_key(observation)                        # the deterministic strongest-first order

Documents::

    verify_document(observations_by_field, allowed_rates=(), bank_amount=None, **options)
        -> (dict[str, VerifiedField], Quality)
    assess_document(...) -> DocumentAssessment    # + sum/rate checks, missing, conflicts, reasons
        options: doc_type=, required=, requirements=, bank_currency=, bank_source=,
                 breakdowns=[TaxBreakdown], other_charges=, hints=, policy=
    required_fields(doc_type) / DEFAULT_REQUIREMENTS, bank_observation(amount), group_by_field(named)
    regraded(assessment, fields) -> DocumentAssessment   # quality and summary after fields are judged again

Documents from abroad (checklist P7)::

    assess_foreign(observations_by_field, ForeignRules(...), *, doc_type=, bank=BankCharge(...))
        -> DocumentAssessment   # rules that hold anywhere: bank-confirmed total, net + VAT = total,
                                # a VAT rate valid in the issuer's country, the issuer's VAT number

Arithmetic::

    check_sum(net, vat, gross, *, other_charges=, lines=) -> SumCheck
    check_rate(net, vat, allowed_rates, *, allow_mixed=, stated_rate=) -> RateCheck
    check_tax_lines(lines, allowed_rates) -> tuple[RateCheck, ...]
    derive_observations(observations_by_field, *, breakdowns=, other_charges=) -> ARITHMETIC observations
    derive_gross(net_obs, vat_obs) -> FieldObservation | None

Normalization::

    normalize_value(field, value, hints) -> Normalized (candidates: 0 unreadable, 1 clear, 2+ ambiguous)
    normalize_amount / _currency / _date / _tax_id / _iban / _invoice_number / _payment_reference
    NormalizeHints(locale="pt-PT" | decimal_separator= | day_first=)
    currency_mark(amount_text) -> CurrencyMark | None   # "483,60 €" -> EUR
    fails_check_digits(field, value) -> bool           # complete IBAN / RF with wrong check digits

Duplicates (§25, §26)::

    DocumentFingerprint.from_document(doc, sha256s=...)
    DuplicateIndex(known).check(candidate) / find_duplicates(candidate, known) -> tuple[DuplicateVerdict, ...]

Altered documents (§26), signals only::

    detect_tampering(metadata=, issue_date=, observations_by_field=) -> tuple[TamperSignal, ...]
    metadata_signals(pdf_info, issue_date), observation_signals(observations_by_field)

Allowed VAT rates are injected (e.g. a country pack's ``vat_rates(on)``);
nothing here knows a country's rates.
"""

from .arithmetic import (
    RateCheck,
    RateFit,
    SumCheck,
    TaxBreakdown,
    TaxLine,
    allowed_rate_values,
    check_rate,
    check_sum,
    check_tax_lines,
    derive_gross,
    derive_observations,
    sum_tolerance,
)
from .document import (
    BANK_CONFIDENCE,
    DEFAULT_REQUIREMENTS,
    DocumentAssessment,
    assess_document,
    bank_observation,
    group_by_field,
    regraded,
    required_fields,
    verify_document,
)
from .foreign import (
    PAYMENT_LAG_DAYS,
    PAYMENT_LEAD_DAYS,
    BankCharge,
    ForeignRules,
    assess_foreign,
    confirm_foreign,
    foreign_requirements,
)
from .duplicates import (
    DEFAULT_NEAR_DAYS,
    DocumentFingerprint,
    DuplicateIndex,
    DuplicateKind,
    DuplicateSignal,
    DuplicateVerdict,
    MergeSuggestion,
    find_duplicates,
)
from .fields import (
    DEFAULT_POLICY,
    FieldAssessment,
    VerificationPolicy,
    assess_field,
    channel_token,
    independent,
    is_high_rank,
    lineage,
    order_key,
    verify_field,
)
from .normalize import (
    CurrencyMark,
    Normalized,
    NormalizeHints,
    NormNote,
    comparison_key,
    currency_mark,
    fails_check_digits,
    iban_is_valid,
    invoice_number_key,
    normalize_amount,
    normalize_currency,
    normalize_date,
    normalize_iban,
    normalize_invoice_number,
    normalize_payment_reference,
    normalize_tax_id,
    normalize_value,
    split_tax_id,
)
from .tamper import (
    PdfMetadata,
    SignalStrength,
    TamperKind,
    TamperSignal,
    detect_tampering,
    metadata_signals,
    observation_signals,
    parse_pdf_date,
)

__all__ = [
    "BANK_CONFIDENCE",
    "PAYMENT_LAG_DAYS",
    "PAYMENT_LEAD_DAYS",
    "BankCharge",
    "ForeignRules",
    "assess_foreign",
    "confirm_foreign",
    "foreign_requirements",
    "regraded",
    "CurrencyMark",
    "DEFAULT_NEAR_DAYS",
    "DEFAULT_POLICY",
    "DEFAULT_REQUIREMENTS",
    "DocumentAssessment",
    "DocumentFingerprint",
    "DuplicateIndex",
    "DuplicateKind",
    "DuplicateSignal",
    "DuplicateVerdict",
    "FieldAssessment",
    "MergeSuggestion",
    "NormNote",
    "NormalizeHints",
    "Normalized",
    "PdfMetadata",
    "RateCheck",
    "RateFit",
    "SignalStrength",
    "SumCheck",
    "TamperKind",
    "TamperSignal",
    "TaxBreakdown",
    "TaxLine",
    "VerificationPolicy",
    "allowed_rate_values",
    "assess_document",
    "assess_field",
    "bank_observation",
    "channel_token",
    "check_rate",
    "check_sum",
    "check_tax_lines",
    "comparison_key",
    "currency_mark",
    "derive_gross",
    "derive_observations",
    "detect_tampering",
    "fails_check_digits",
    "find_duplicates",
    "group_by_field",
    "iban_is_valid",
    "independent",
    "invoice_number_key",
    "is_high_rank",
    "lineage",
    "metadata_signals",
    "normalize_amount",
    "normalize_currency",
    "normalize_date",
    "normalize_iban",
    "normalize_invoice_number",
    "normalize_payment_reference",
    "normalize_tax_id",
    "normalize_value",
    "observation_signals",
    "order_key",
    "parse_pdf_date",
    "required_fields",
    "split_tax_id",
    "sum_tolerance",
    "verify_document",
    "verify_field",
]
