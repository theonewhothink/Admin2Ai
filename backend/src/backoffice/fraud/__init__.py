"""Fraud engine (§26) and the trusted-bank-details gate (§25).

Public API
----------
``assess(FraudCase(...), config=DEFAULT_CONFIG) -> FraudAssessment``
    Hard-stop checks for a supplier document and/or payment instruction:
    changed or invalid IBAN, new payment recipient, changed / lookalike sender
    domain, unusual amount (median/MAD), duplicate invoice with different IBAN,
    unusual country, invoice recipient mismatch, suspicious payment language
    (PT/EN), altered-document signals. ``FraudAssessment.owner_message`` is
    plain copy, e.g. "Vodafone changed the IBAN shown on its invoice. Payment blocked."

``trust_iban(supplier, iban, approval: HardApproval) -> (Supplier, BeneficiaryChange)``
    The only way an IBAN joins ``Supplier.known_ibans``: a human, out-of-band,
    recorded hard approval. The engine itself can never approve changed
    beneficiary information.

``beneficiary_proposals(supplier, payments) -> list[BeneficiaryProposal]``
    Onboarding questions built from past payments (proposals, never trust).

Helpers: :mod:`.iban` (normalize, mod-97, find in text, mask),
:mod:`.domains` (sender-domain verdicts), :mod:`.phrases` (suspicious wording).

Evasion resistance: every possible IBAN start in a text is examined (one IBAN
cannot hide the next) and non-breaking / zero-width separators are ignored;
wording checks drop invisible characters and read Cyrillic/Greek look-alike
letters as Latin; a supplier on a free-mail provider is recognised by its exact
address (``DomainVerdict.NEW_ADDRESS`` otherwise); the tenant's own tax
numbers are compared with their country, so the same digits from another
country are a recipient mismatch; a payment amount is judged even when an
invoice is attached; credit notes are never "unusual charges".
"""

from __future__ import annotations

from .beneficiary import (
    ApproverKind,
    BeneficiaryChange,
    BeneficiaryChangeRefused,
    BeneficiaryProposal,
    HardApproval,
    VerificationChannel,
    beneficiary_proposals,
    new_beneficiary_ibans,
    trust_iban,
)
from .domains import (
    CROSS_SCRIPT_LETTERS,
    FREE_MAIL_DOMAINS,
    DomainCheck,
    DomainVerdict,
    check_sender_domain,
    email_domain,
    registrable_domain,
)
from .engine import (
    DEFAULT_CONFIG,
    HARD_STOP_SEVERITIES,
    AlteredDocumentHint,
    AlteredSignal,
    FraudAssessment,
    FraudCase,
    FraudConfig,
    FraudSignal,
    Severity,
    SignalKind,
    assess,
)
from .iban import find_ibans, iban_country, is_valid_iban, mask_iban, normalize_iban
from .phrases import PhraseCategory, PhraseHit, find_suspicious_phrases

__all__ = [
    "CROSS_SCRIPT_LETTERS",
    "DEFAULT_CONFIG",
    "FREE_MAIL_DOMAINS",
    "HARD_STOP_SEVERITIES",
    "AlteredDocumentHint",
    "AlteredSignal",
    "ApproverKind",
    "BeneficiaryChange",
    "BeneficiaryChangeRefused",
    "BeneficiaryProposal",
    "DomainCheck",
    "DomainVerdict",
    "FraudAssessment",
    "FraudCase",
    "FraudConfig",
    "FraudSignal",
    "HardApproval",
    "PhraseCategory",
    "PhraseHit",
    "Severity",
    "SignalKind",
    "VerificationChannel",
    "assess",
    "beneficiary_proposals",
    "check_sender_domain",
    "email_domain",
    "find_ibans",
    "find_suspicious_phrases",
    "iban_country",
    "is_valid_iban",
    "mask_iban",
    "new_beneficiary_ibans",
    "normalize_iban",
    "registrable_domain",
    "trust_iban",
]
