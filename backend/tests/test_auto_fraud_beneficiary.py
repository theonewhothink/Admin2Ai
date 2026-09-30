"""Trusted bank details (§25, §26): only a human, out-of-band, recorded hard approval adds an IBAN."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from backoffice.domain.models import Document, Supplier, Transaction
from backoffice.fraud.beneficiary import (
    ApproverKind,
    BeneficiaryChangeRefused,
    HardApproval,
    VerificationChannel,
    beneficiary_proposals,
    trust_iban,
)
from backoffice.fraud.engine import FraudCase, SignalKind, assess

OLD = "PT50000201231234567890154"
NEW = "GB82 WEST 1234 5698 7654 32"
T0 = datetime(2026, 9, 20, 10, 0, tzinfo=timezone.utc)
SUPPLIER = Supplier(id="sup_voda", tenant_id="t1", name="Vodafone", known_ibans=[OLD], countries=["PT", "GB"])


def approval(**kw) -> HardApproval:
    base = dict(supplier_id="sup_voda", iban=NEW, approver_id="owner1", approver_kind=ApproverKind.HUMAN,
                approved_at=T0, verified_out_of_band=True, channel=VerificationChannel.PHONE_CALL_TO_KNOWN_NUMBER,
                evidence_id="ev_call_note")  # fmt: skip
    base.update(kw)
    return HardApproval(**base)


def test_verified_human_approval_adds_iban_without_mutating_input() -> None:
    updated, change = trust_iban(SUPPLIER, NEW, approval())
    assert updated.known_ibans == [OLD, "GB82WEST12345698765432"]
    assert SUPPLIER.known_ibans == [OLD]
    assert change is not None and change.previous_ibans == (OLD,) and change.approval.evidence_id == "ev_call_note"
    # The engine now accepts the approved IBAN.
    doc = Document(tenant_id="t1", evidence_ids=["e"], iban=NEW, gross_amount=Decimal("10"))
    assert not assess(FraudCase(entities=[], supplier=updated, document=doc)).of_kind(SignalKind.CHANGED_IBAN)


def test_already_trusted_is_a_no_op() -> None:
    same, change = trust_iban(SUPPLIER, "pt50 0002 0123 1234 5678 9015 4", approval(iban=OLD))
    assert same is SUPPLIER and change is None


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"approver_kind": ApproverKind.AGENT}, "approver_not_human"),
        ({"verified_out_of_band": False}, "not_verified_out_of_band"),
        ({"channel": None}, "not_verified_out_of_band"),
        ({"evidence_id": " "}, "approval_not_recorded"),
        ({"approver_id": ""}, "approver_unknown"),
        ({"supplier_id": "sup_other"}, "approval_for_another_supplier"),
        ({"iban": "DE89 3704 0044 0532 0130 00"}, "approval_for_another_iban"),
    ],
)
def test_refusals(kwargs: dict, reason: str) -> None:
    with pytest.raises(BeneficiaryChangeRefused) as exc:
        trust_iban(SUPPLIER, NEW, approval(**kwargs))
    assert exc.value.reason == reason
    assert SUPPLIER.known_ibans == [OLD]


def test_no_approval_and_invalid_iban_refused() -> None:
    with pytest.raises(BeneficiaryChangeRefused) as exc:
        trust_iban(SUPPLIER, NEW, None)
    assert exc.value.reason == "no_approval"
    with pytest.raises(BeneficiaryChangeRefused) as exc:
        trust_iban(SUPPLIER, "GB82 WEST 1234 5698 7654 33", approval(iban="GB82 WEST 1234 5698 7654 33"))
    assert exc.value.reason == "invalid_iban"


def test_approval_time_must_be_aware() -> None:
    with pytest.raises(ValidationError):
        approval(approved_at=datetime(2026, 9, 20))


def test_proposals_from_past_payments_are_questions_not_trust() -> None:
    bare = Supplier(id="sup_voda", tenant_id="t1", name="Vodafone Portugal, S.A.")
    payments = [
        Transaction(tenant_id="t1", account_id="a", booked_on=date(2026, m, 24), amount=Decimal("-92.40"),
                    counterparty="VODAFONE", counterparty_iban=OLD)
        for m in range(4, 10)
    ]  # fmt: skip
    payments.append(Transaction(tenant_id="t1", account_id="a", booked_on=date(2026, 9, 1), amount=Decimal("-5"),
                                counterparty="VODAFONE", counterparty_iban=NEW))  # fmt: skip (only once)
    payments.append(Transaction(tenant_id="t1", account_id="a", booked_on=date(2026, 9, 2), amount=Decimal("50"),
                                counterparty="VODAFONE", counterparty_iban=NEW))  # fmt: skip (refund, money in)
    [proposal] = beneficiary_proposals(bare, payments, today=date(2026, 9, 27))
    assert proposal.iban == OLD and proposal.payments == 6
    assert proposal.prompt == "You have paid Vodafone Portugal at PT50 •••• 0154 6 times, most recently on 24 September. Keep paying there?"
    assert bare.known_ibans == []
    assert beneficiary_proposals(SUPPLIER, payments) == []  # already trusted


def test_engine_cannot_receive_approvals() -> None:
    # The engine takes no approval input at all: FraudCase has no field that could carry one.
    fields = set(FraudCase.__dataclass_fields__)
    assert not {f for f in fields if "approv" in f or "trust" in f}
