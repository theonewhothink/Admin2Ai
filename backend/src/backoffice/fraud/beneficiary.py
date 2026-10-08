"""Trusted bank details: the only way an IBAN joins a supplier profile (§25, §26).

A bank detail change is a HARD_APPROVAL action (§25) and AI may never approve
changed beneficiary information (§26). :func:`trust_iban` is therefore the
single gate for adding an IBAN to ``Supplier.known_ibans``, and it refuses
unless it is handed a recorded :class:`HardApproval` that:

* was given by a human (never an agent),
* was verified out of band (a call to a number already on file, in person,
  a signed letter, or the bank's own payee check) and says how,
* names this supplier and exactly this IBAN,
* points at the evidence of the approval (e.g. the call note), and
* is for an IBAN that passes the mod-97 check.

The fraud engine only reads profiles; nothing in it can produce an approval.
At onboarding, :func:`beneficiary_proposals` turns past payments into
questions for the owner — proposals, never trust.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from datetime import date, datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, field_validator

from backoffice.domain.models import Supplier, Transaction
from backoffice.learning.keys import display_name
from backoffice.learning.plain import count_phrase, day_month

from .iban import is_valid_iban, mask_iban, normalize_iban

__all__ = [
    "ApproverKind",
    "BeneficiaryChange",
    "BeneficiaryChangeRefused",
    "BeneficiaryProposal",
    "HardApproval",
    "VerificationChannel",
    "beneficiary_proposals",
    "new_beneficiary_ibans",
    "trust_iban",
]


class ApproverKind(str, Enum):
    HUMAN = "human"
    AGENT = "agent"


class VerificationChannel(str, Enum):
    PHONE_CALL_TO_KNOWN_NUMBER = "phone_call_to_known_number"  # a number on file, not one from the email
    IN_PERSON = "in_person"
    SIGNED_LETTER = "signed_letter"
    BANK_PAYEE_CHECK = "bank_payee_check"  # the bank confirmed the account holder's name


class HardApproval(BaseModel):
    """A human's recorded approval of one IBAN for one supplier."""

    model_config = ConfigDict(frozen=True)

    supplier_id: str
    iban: str
    approver_id: str
    approver_kind: ApproverKind
    approved_at: datetime
    verified_out_of_band: bool
    channel: VerificationChannel | None = None
    evidence_id: str  # the stored record of the approval (call note, letter scan)
    note: str = ""

    @field_validator("approved_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("approved_at must be timezone-aware")
        return value


class BeneficiaryChangeRefused(PermissionError):
    """Refused addition of bank details. ``reason`` is a stable internal code."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class BeneficiaryChange(BaseModel):
    """Audit record of a trusted IBAN being added (§55)."""

    model_config = ConfigDict(frozen=True)

    supplier_id: str
    iban: str
    previous_ibans: tuple[str, ...]
    approval: HardApproval


def _refusal(supplier: Supplier, iban: str, approval: HardApproval | None) -> str | None:
    if approval is None:
        return "no_approval"
    if approval.approver_kind is not ApproverKind.HUMAN:
        return "approver_not_human"
    if not approval.approver_id.strip():
        return "approver_unknown"
    if not approval.verified_out_of_band or approval.channel is None:
        return "not_verified_out_of_band"
    if not approval.evidence_id.strip():
        return "approval_not_recorded"
    if approval.supplier_id != supplier.id:
        return "approval_for_another_supplier"
    if not is_valid_iban(iban):
        return "invalid_iban"
    if normalize_iban(approval.iban) != normalize_iban(iban):
        return "approval_for_another_iban"
    return None


def new_beneficiary_ibans(
    supplier: Supplier | None, ibans: Iterable[str | None], *, own_ibans: Iterable[str] = ()
) -> tuple[str, ...]:
    """The bank accounts in ``ibans`` a payment to this supplier would go to that its profile does not trust.

    Every one of them is a new beneficiary: whatever brought it (the first copy of an invoice, a later copy that
    adds bank details, an answer choosing between two copies) it goes through the fraud checks and is held for
    the owner's out-of-band verification (:func:`trust_iban`); nothing here trusts it. The business's own
    accounts are never a supplier's beneficiary. Invalid numbers are returned as written (never trusted).
    Normalized, in order, without repeats.
    """
    trusted = {normalize_iban(i) for i in (supplier.known_ibans if supplier is not None else []) if i}
    own = {normalize_iban(i) for i in own_ibans if i}
    out: list[str] = []
    for raw in ibans:
        if not raw or not str(raw).strip():
            continue
        iban = normalize_iban(str(raw))
        if iban in own or iban in trusted or iban in out:
            continue
        out.append(iban)
    return tuple(out)


def trust_iban(
    supplier: Supplier, iban: str, approval: HardApproval | None
) -> tuple[Supplier, BeneficiaryChange | None]:
    """Return a copy of ``supplier`` that trusts ``iban``, plus the audit record.

    Raises :class:`BeneficiaryChangeRefused` unless ``approval`` is a valid,
    human, out-of-band hard approval for exactly this supplier and IBAN.
    Already-trusted IBANs are a no-op (no new record). The input is never mutated.
    """
    reason = _refusal(supplier, iban, approval)
    if reason is not None:
        raise BeneficiaryChangeRefused(reason)
    assert approval is not None  # _refusal checked it
    normalized = normalize_iban(iban)
    current = [normalize_iban(i) for i in supplier.known_ibans]
    if normalized in current:
        return supplier, None
    updated = supplier.model_copy(update={"known_ibans": [*supplier.known_ibans, normalized]})
    change = BeneficiaryChange(
        supplier_id=supplier.id, iban=normalized, previous_ibans=tuple(current), approval=approval
    )
    return updated, change


class BeneficiaryProposal(BaseModel):
    """'You have paid Vodafone at PT50 •••• 0154 6 times. Keep paying there?' — needs hard approval."""

    model_config = ConfigDict(frozen=True)

    supplier_id: str
    iban: str
    payments: int
    last_paid_on: date
    prompt: str


def beneficiary_proposals(
    supplier: Supplier, payments: Iterable[Transaction], *, today: date | None = None, min_payments: int = 2
) -> list[BeneficiaryProposal]:
    """Bank details the business already paid this supplier, as questions for the owner.

    ``payments`` must already be this supplier's outgoing payments. Only valid,
    not-yet-trusted IBANs paid at least ``min_payments`` times are proposed.
    """
    known = {normalize_iban(i) for i in supplier.known_ibans}
    seen: dict[str, list[date]] = defaultdict(list)
    for tx in payments:
        if tx.amount >= 0 or not tx.counterparty_iban or not is_valid_iban(tx.counterparty_iban):
            continue
        iban = normalize_iban(tx.counterparty_iban)
        if iban not in known:
            seen[iban].append(tx.booked_on)
    name = display_name(supplier.name)
    proposals = []
    for iban, dates in sorted(seen.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        if len(dates) < min_payments:
            continue
        prompt = (
            f"You have paid {name} at {mask_iban(iban)} {count_phrase(len(dates), 'time')}, "
            f"most recently on {day_month(max(dates), today)}. Keep paying there?"
        )
        proposals.append(
            BeneficiaryProposal(
                supplier_id=supplier.id, iban=iban, payments=len(dates), last_paid_on=max(dates), prompt=prompt
            )
        )
    return proposals
