"""Action levels and authorization (§25, §26)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from backoffice.language import find_jargon, find_off_tone
from backoffice.policy import (
    ACTION_LEVELS,
    OBSERVATION_ACTIONS,
    ActionContext,
    ActionKind as A,
    ActionLevel as L,
    Approval,
    Grant,
    PolicyError,
    Requirement as R,
    TenantPolicy,
    authorize,
    effective_level,
    is_grantable,
    level_of,
)

T0 = datetime(2026, 9, 27, 9, 0, tzinfo=timezone.utc)
HARD = [a for a, lvl in ACTION_LEVELS.items() if lvl is L.HARD_APPROVAL]
OWNER = [a for a, lvl in ACTION_LEVELS.items() if lvl is L.OWNER_APPROVAL]
AUTH = [a for a, lvl in ACTION_LEVELS.items() if lvl is L.AUTOMATIC_IF_AUTHORIZED]
AUTO = [a for a, lvl in ACTION_LEVELS.items() if lvl is L.FULLY_AUTOMATIC]


def ctx(**kw) -> ActionContext:
    return ActionContext(tenant_id="t1", **kw)


def policy(*grants: tuple[A, str | None]) -> TenantPolicy:
    p = TenantPolicy(tenant_id="t1")
    for action, entity in grants:
        p = p.with_grant(action, granted_by="owner:1", entity_id=entity, at=T0)
    return p


def approval(action: A, level: R = R.HARD, **kw) -> Approval:
    base = dict(
        tenant_id="t1",
        action=action,
        subject_id="pay_1",
        level=level,
        approved_by="owner:1",
        approved_at=T0,
    )
    base.update(kw)
    return Approval(**base)


# --------------------------------------------------------------------------- the §25 table


def test_levels_match_spec_section_25():
    assert set(ACTION_LEVELS) == set(A)
    assert {a.value for a in AUTO} == {
        "reading", "extraction", "retrieval", "classification", "reconciliation",
        "duplicate_merging", "organization", "searching", "reminder",
    }  # fmt: skip
    assert {a.value for a in AUTH} == {
        "supplier_invoice_request", "routine_accountant_response", "upload", "document_delivery",
    }  # fmt: skip
    assert {a.value for a in OWNER} == {
        "unusual_external_communication", "tax_interpretation_change", "contractual_change",
    }  # fmt: skip
    assert {a.value for a in HARD} == {
        "money_movement", "tax_filing", "bank_detail_change",
        "legally_binding_acceptance", "original_evidence_deletion",
    }  # fmt: skip
    assert all(level_of(a) is ACTION_LEVELS[a] for a in A)


def test_only_authorizable_actions_are_grantable():
    assert {a for a in A if is_grantable(a)} == set(AUTH)


# --------------------------------------------------------------------------- basic levels


@pytest.mark.parametrize("action", AUTO, ids=lambda a: a.value)
def test_fully_automatic_needs_nothing(action):
    d = authorize(action, policy(), ctx())
    assert d.allowed_now and d.requires is R.NONE and d.level is L.FULLY_AUTOMATIC


@pytest.mark.parametrize("action", AUTH, ids=lambda a: a.value)
def test_authorizable_needs_owner_until_granted(action):
    d = authorize(action, policy(), ctx(entity_id="ent_a"))
    assert not d.allowed_now and d.requires is R.OWNER
    assert "You can let me do this automatically." in d.reason_plain
    granted = authorize(action, policy((action, None)), ctx(entity_id="ent_a"))
    assert granted.allowed_now and granted.requires is R.NONE


def test_grant_for_one_company_does_not_cover_another():
    p = policy((A.SUPPLIER_INVOICE_REQUEST, "ent_a"))
    assert authorize(A.SUPPLIER_INVOICE_REQUEST, p, ctx(entity_id="ent_a")).allowed_now
    assert not authorize(
        A.SUPPLIER_INVOICE_REQUEST, p, ctx(entity_id="ent_b")
    ).allowed_now
    assert not authorize(A.SUPPLIER_INVOICE_REQUEST, p, ctx()).allowed_now


def test_grant_for_all_companies_covers_every_company():
    p = policy((A.DOCUMENT_DELIVERY, None))
    for entity in ("ent_a", "ent_b", None):
        assert authorize(A.DOCUMENT_DELIVERY, p, ctx(entity_id=entity)).allowed_now


def test_grant_for_one_action_does_not_cover_another():
    p = policy((A.UPLOAD, None))
    assert not authorize(A.DOCUMENT_DELIVERY, p, ctx()).allowed_now


@pytest.mark.parametrize("action", OWNER, ids=lambda a: a.value)
def test_owner_approval_actions_always_ask(action):
    d = authorize(action, policy(*[(a, None) for a in AUTH]), ctx(subject_id="msg_1"))
    assert not d.allowed_now and d.requires is R.OWNER
    assert d.reason_plain.startswith("I need your OK before I ")


@pytest.mark.parametrize("action", HARD, ids=lambda a: a.value)
def test_hard_approval_actions_always_ask(action):
    d = authorize(action, policy(*[(a, None) for a in AUTH]), ctx(subject_id="pay_1"))
    assert not d.allowed_now and d.requires is R.HARD and d.level is L.HARD_APPROVAL
    assert d.reason_plain.endswith("I need your explicit approval every time.")


# --------------------------------------------------------------------------- grants can't escalate


@pytest.mark.parametrize("action", HARD + OWNER + AUTO, ids=lambda a: a.value)
def test_non_authorizable_actions_cannot_be_granted(action):
    with pytest.raises(ValidationError, match="cannot be pre-granted"):
        TenantPolicy(tenant_id="t1").with_grant(action, granted_by="owner:1", at=T0)


def test_stored_policy_with_a_hard_grant_is_rejected_on_load():
    raw = {
        "tenant_id": "t1",
        "grants": [
            {
                "action": "money_movement",
                "entity_id": None,
                "granted_by": "x",
                "granted_at": T0.isoformat(),
            }
        ],
    }
    with pytest.raises(ValidationError, match="cannot be pre-granted"):
        TenantPolicy.model_validate(raw)


def test_grant_needs_timezone_aware_time():
    with pytest.raises(ValidationError, match="timezone-aware"):
        Grant(action=A.UPLOAD, granted_by="owner:1", granted_at=datetime(2026, 9, 1))


def test_policy_is_immutable_and_copies_on_change():
    p0 = TenantPolicy(tenant_id="t1")
    p1 = p0.with_grant(A.UPLOAD, granted_by="owner:1", at=T0)
    assert p0.grants == () and p1.allows(A.UPLOAD)
    with pytest.raises(ValidationError):
        p1.tenant_id = "t2"  # type: ignore[misc]


def test_with_grant_replaces_same_scope_and_without_grant_removes_exact_scope():
    p = policy((A.UPLOAD, None), (A.UPLOAD, None), (A.UPLOAD, "ent_a"))
    assert len(p.grants) == 2
    p = p.without_grant(A.UPLOAD)
    assert not p.allows(A.UPLOAD, "ent_b")
    assert p.allows(A.UPLOAD, "ent_a")
    assert not p.without_grant(A.UPLOAD, entity_id="ent_a").allows(A.UPLOAD, "ent_a")


def test_policy_round_trips_through_json():
    p = policy((A.UPLOAD, None), (A.SUPPLIER_INVOICE_REQUEST, "ent_a"))
    assert TenantPolicy.model_validate_json(p.model_dump_json()) == p


def test_other_tenants_context_is_refused():
    with pytest.raises(PolicyError):
        authorize(A.READING, policy(), ActionContext(tenant_id="t2"))


# --------------------------------------------------------------------------- per-instance approvals


def test_matching_hard_approval_allows_that_one_payment():
    d = authorize(
        A.MONEY_MOVEMENT,
        policy(),
        ctx(subject_id="pay_1", approval=approval(A.MONEY_MOVEMENT)),
    )
    assert (
        d.allowed_now
        and d.requires is R.NONE
        and d.reason_plain == "You approved this."
    )


def test_owner_level_approval_is_not_enough_for_hard_actions():
    a = approval(A.MONEY_MOVEMENT, R.OWNER)
    d = authorize(A.MONEY_MOVEMENT, policy(), ctx(subject_id="pay_1", approval=a))
    assert not d.allowed_now and d.requires is R.HARD


def test_hard_approval_also_satisfies_owner_level():
    a = approval(A.CONTRACTUAL_CHANGE, R.HARD)
    assert authorize(
        A.CONTRACTUAL_CHANGE, policy(), ctx(subject_id="pay_1", approval=a)
    ).allowed_now


@pytest.mark.parametrize(
    "context",
    [
        ctx(subject_id="pay_2", approval=approval(A.MONEY_MOVEMENT)),  # other payment
        ctx(approval=approval(A.MONEY_MOVEMENT)),  # no subject: can't be per-instance
        ctx(subject_id="pay_1", approval=approval(A.TAX_FILING)),  # other action
        ctx(
            subject_id="pay_1",
            entity_id="ent_b",
            approval=approval(A.MONEY_MOVEMENT, entity_id="ent_a"),
        ),
        ctx(subject_id="pay_1", approval=approval(A.MONEY_MOVEMENT, tenant_id="t2")),
    ],
    ids=[
        "other-subject",
        "no-subject",
        "other-action",
        "other-company",
        "other-tenant",
    ],
)
def test_approval_only_counts_for_its_exact_instance(context):
    d = authorize(A.MONEY_MOVEMENT, policy(), context)
    assert not d.allowed_now and d.requires is R.HARD


def test_approval_is_void_when_the_approved_facts_change():
    a = approval(A.MONEY_MOVEMENT, fingerprint="amount=117.20;iban=PT50...154")
    ok = ctx(
        subject_id="pay_1", fingerprint="amount=117.20;iban=PT50...154", approval=a
    )
    assert authorize(A.MONEY_MOVEMENT, policy(), ok).allowed_now
    changed = ctx(
        subject_id="pay_1", fingerprint="amount=171.20;iban=PT50...154", approval=a
    )
    d = authorize(A.MONEY_MOVEMENT, policy(), changed)
    assert not d.allowed_now
    assert (
        d.reason_plain
        == "Something changed since you approved. I need your approval again."
    )


def test_owner_approval_runs_one_ungranted_authorizable_action():
    a = approval(A.SUPPLIER_INVOICE_REQUEST, R.OWNER, subject_id="req_1")
    d = authorize(
        A.SUPPLIER_INVOICE_REQUEST, policy(), ctx(subject_id="req_1", approval=a)
    )
    assert d.allowed_now and d.reason_plain == "You approved this."
    assert not authorize(
        A.SUPPLIER_INVOICE_REQUEST, policy(), ctx(subject_id="req_2", approval=a)
    ).allowed_now


def test_approval_needs_timezone_aware_time():
    with pytest.raises(ValidationError, match="timezone-aware"):
        approval(A.MONEY_MOVEMENT, approved_at=datetime(2026, 9, 27, 9, 0))


def test_approval_level_none_is_invalid():
    with pytest.raises(ValidationError):
        approval(A.MONEY_MOVEMENT, R.NONE)


# --------------------------------------------------------------------------- §26 changed beneficiary


@pytest.mark.parametrize(
    "action", [a for a in A if a not in OBSERVATION_ACTIONS], ids=lambda a: a.value
)
def test_changed_beneficiary_forces_hard_approval(action):
    p = policy(*[(a, None) for a in AUTH])
    d = authorize(action, p, ctx(subject_id="pay_1", beneficiary_changed=True))
    assert not d.allowed_now and d.requires is R.HARD and d.level is L.HARD_APPROVAL
    assert d.reason_plain == "The bank details changed. I need your explicit approval."


@pytest.mark.parametrize("action", sorted(OBSERVATION_ACTIONS), ids=lambda a: a.value)
def test_observation_continues_during_a_fraud_hold(action):
    for context in (ctx(beneficiary_changed=True), ctx(fraud_hold=True)):
        assert authorize(action, policy(), context).allowed_now


def test_changed_beneficiary_needs_a_hard_approval_that_saw_the_warning():
    unaware = approval(A.MONEY_MOVEMENT)
    d = authorize(
        A.MONEY_MOVEMENT,
        policy(),
        ctx(subject_id="pay_1", beneficiary_changed=True, approval=unaware),
    )
    assert not d.allowed_now
    owner_only = approval(A.MONEY_MOVEMENT, R.OWNER, acknowledged_risk=True)
    d = authorize(
        A.MONEY_MOVEMENT,
        policy(),
        ctx(subject_id="pay_1", beneficiary_changed=True, approval=owner_only),
    )
    assert not d.allowed_now
    aware = approval(A.MONEY_MOVEMENT, acknowledged_risk=True)
    d = authorize(
        A.MONEY_MOVEMENT,
        policy(),
        ctx(subject_id="pay_1", beneficiary_changed=True, approval=aware),
    )
    assert d.allowed_now


def test_changed_beneficiary_escalates_even_granted_supplier_requests():
    p = policy((A.SUPPLIER_INVOICE_REQUEST, None))
    assert (
        effective_level(A.SUPPLIER_INVOICE_REQUEST, ctx(beneficiary_changed=True))
        is L.HARD_APPROVAL
    )
    assert not authorize(
        A.SUPPLIER_INVOICE_REQUEST, p, ctx(beneficiary_changed=True)
    ).allowed_now


def test_fraud_hold_forces_hard_approval():
    d = authorize(A.RECONCILIATION, policy(), ctx(fraud_hold=True))
    assert d.requires is R.HARD
    assert (
        d.reason_plain == "Something here looks unusual. I need your explicit approval."
    )


# --------------------------------------------------------------------------- unusual


def test_unusual_escalates_authorizable_to_owner_even_when_granted():
    p = policy((A.ROUTINE_ACCOUNTANT_RESPONSE, None))
    d = authorize(A.ROUTINE_ACCOUNTANT_RESPONSE, p, ctx(unusual=True))
    assert not d.allowed_now and d.requires is R.OWNER and d.level is L.OWNER_APPROVAL
    assert d.reason_plain.startswith("This is not routine.")


def test_unusual_never_lowers_or_changes_other_levels():
    assert effective_level(A.READING, ctx(unusual=True)) is L.FULLY_AUTOMATIC
    assert effective_level(A.MONEY_MOVEMENT, ctx(unusual=True)) is L.HARD_APPROVAL
    assert effective_level(A.CONTRACTUAL_CHANGE, ctx(unusual=True)) is L.OWNER_APPROVAL


# --------------------------------------------------------------------------- invariants


def _all_contexts():
    for flags in (
        {},
        {"unusual": True},
        {"beneficiary_changed": True},
        {"fraud_hold": True},
    ):
        yield ctx(subject_id="pay_1", entity_id="ent_a", **flags)
        yield ctx(
            subject_id="pay_1",
            entity_id="ent_a",
            approval=approval(A.UPLOAD, R.OWNER),
            **flags,
        )


@pytest.mark.parametrize("action", list(A), ids=lambda a: a.value)
def test_decisions_are_consistent_and_plain(action):
    for p in (policy(), policy(*[(a, None) for a in AUTH])):
        for context in _all_contexts():
            d = authorize(action, p, context)
            assert d.allowed_now == (d.requires is R.NONE)
            assert d.reason_plain and d.reason_plain.endswith(".")
            assert find_jargon(d.reason_plain) == [], d.reason_plain
            assert find_off_tone(d.reason_plain) == [], d.reason_plain
            if ACTION_LEVELS[action] is L.HARD_APPROVAL:
                assert (
                    not d.allowed_now
                )  # nothing here carries a matching hard approval
