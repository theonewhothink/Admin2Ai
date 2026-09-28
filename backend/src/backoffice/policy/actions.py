"""Action levels and the authorization gate (§25, §26).

Every action the operator can take has a fixed level:

* FULLY_AUTOMATIC: reading, extraction, retrieval, classification,
  reconciliation, duplicate merging, organization, searching, reminders.
* AUTOMATIC_IF_AUTHORIZED: supplier invoice requests, routine accountant
  responses, uploads, document delivery. Runs on its own only when the tenant
  granted it (for all companies or one company).
* OWNER_APPROVAL: unusual external communication, tax interpretation changes,
  contractual changes. Approved one instance at a time.
* HARD_APPROVAL: money movement, tax filing, bank detail change, legally
  binding acceptance, deletion of original evidence. Approved one instance at a
  time and can never be pre-granted.

A changed beneficiary or any other fraud hard-stop (§26) forces hard approval
for everything except pure observation (reading, extraction, retrieval,
searching), which must keep working so the evidence can be examined.

A hard approval is bound to the exact facts the approver saw: it counts only
when it carries a ``fingerprint`` equal to the action context's. An approval
without one never authorizes a hard-approval action.

Actions may be given as ``ActionKind`` members or their string values; an
unknown action raises :class:`PolicyError`.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator

from backoffice.domain.models import new_id, utcnow


class PolicyError(ValueError):
    """Programming or configuration error in the policy layer (never owner-facing)."""


class ActionLevel(str, Enum):
    FULLY_AUTOMATIC = "fully_automatic"
    AUTOMATIC_IF_AUTHORIZED = "automatic_if_authorized"
    OWNER_APPROVAL = "owner_approval"
    HARD_APPROVAL = "hard_approval"


class Requirement(str, Enum):
    """What is still needed before an action may run."""

    NONE = "none"
    OWNER = "owner"
    HARD = "hard"


class ActionKind(str, Enum):
    """Every action listed in §25, named after the spec wording."""

    # Fully automatic
    READING = "reading"
    EXTRACTION = "extraction"
    RETRIEVAL = "retrieval"
    CLASSIFICATION = "classification"
    RECONCILIATION = "reconciliation"
    DUPLICATE_MERGING = "duplicate_merging"
    ORGANIZATION = "organization"
    SEARCHING = "searching"
    REMINDER = "reminder"
    # Automatic if authorized
    SUPPLIER_INVOICE_REQUEST = "supplier_invoice_request"
    ROUTINE_ACCOUNTANT_RESPONSE = "routine_accountant_response"
    UPLOAD = "upload"
    DOCUMENT_DELIVERY = "document_delivery"
    # Owner approval
    UNUSUAL_EXTERNAL_COMMUNICATION = "unusual_external_communication"
    TAX_INTERPRETATION_CHANGE = "tax_interpretation_change"
    CONTRACTUAL_CHANGE = "contractual_change"
    # Hard approval (always)
    MONEY_MOVEMENT = "money_movement"
    TAX_FILING = "tax_filing"
    BANK_DETAIL_CHANGE = "bank_detail_change"
    LEGALLY_BINDING_ACCEPTANCE = "legally_binding_acceptance"
    ORIGINAL_EVIDENCE_DELETION = "original_evidence_deletion"


_A, _L = ActionKind, ActionLevel

ACTION_LEVELS: dict[ActionKind, ActionLevel] = {
    _A.READING: _L.FULLY_AUTOMATIC,
    _A.EXTRACTION: _L.FULLY_AUTOMATIC,
    _A.RETRIEVAL: _L.FULLY_AUTOMATIC,
    _A.CLASSIFICATION: _L.FULLY_AUTOMATIC,
    _A.RECONCILIATION: _L.FULLY_AUTOMATIC,
    _A.DUPLICATE_MERGING: _L.FULLY_AUTOMATIC,
    _A.ORGANIZATION: _L.FULLY_AUTOMATIC,
    _A.SEARCHING: _L.FULLY_AUTOMATIC,
    _A.REMINDER: _L.FULLY_AUTOMATIC,
    _A.SUPPLIER_INVOICE_REQUEST: _L.AUTOMATIC_IF_AUTHORIZED,
    _A.ROUTINE_ACCOUNTANT_RESPONSE: _L.AUTOMATIC_IF_AUTHORIZED,
    _A.UPLOAD: _L.AUTOMATIC_IF_AUTHORIZED,
    _A.DOCUMENT_DELIVERY: _L.AUTOMATIC_IF_AUTHORIZED,
    _A.UNUSUAL_EXTERNAL_COMMUNICATION: _L.OWNER_APPROVAL,
    _A.TAX_INTERPRETATION_CHANGE: _L.OWNER_APPROVAL,
    _A.CONTRACTUAL_CHANGE: _L.OWNER_APPROVAL,
    _A.MONEY_MOVEMENT: _L.HARD_APPROVAL,
    _A.TAX_FILING: _L.HARD_APPROVAL,
    _A.BANK_DETAIL_CHANGE: _L.HARD_APPROVAL,
    _A.LEGALLY_BINDING_ACCEPTANCE: _L.HARD_APPROVAL,
    _A.ORIGINAL_EVIDENCE_DELETION: _L.HARD_APPROVAL,
}

# Pure observation: changes nothing outside, needed to investigate a fraud flag.
OBSERVATION_ACTIONS: frozenset[ActionKind] = frozenset(
    {_A.READING, _A.EXTRACTION, _A.RETRIEVAL, _A.SEARCHING}
)

# Owner-facing verb phrases, completing "I need your OK before I ...".
_VERB: dict[ActionKind, str] = {
    _A.READING: "read your documents",
    _A.EXTRACTION: "read the details on a document",
    _A.RETRIEVAL: "fetch a document",
    _A.CLASSIFICATION: "sort this",
    _A.RECONCILIATION: "match this payment",
    _A.DUPLICATE_MERGING: "combine duplicate copies",
    _A.ORGANIZATION: "file this",
    _A.SEARCHING: "search your records",
    _A.REMINDER: "send a reminder",
    _A.SUPPLIER_INVOICE_REQUEST: "ask the supplier for the invoice",
    _A.ROUTINE_ACCOUNTANT_RESPONSE: "answer your accountant",
    _A.UPLOAD: "upload this document",
    _A.DOCUMENT_DELIVERY: "send documents to your accountant",
    _A.UNUSUAL_EXTERNAL_COMMUNICATION: "send this message",
    _A.TAX_INTERPRETATION_CHANGE: "change how this is taxed",
    _A.CONTRACTUAL_CHANGE: "change this contract",
    _A.MONEY_MOVEMENT: "move this money",
    _A.TAX_FILING: "submit this tax filing",
    _A.BANK_DETAIL_CHANGE: "change these bank details",
    _A.LEGALLY_BINDING_ACCEPTANCE: "accept these terms",
    _A.ORIGINAL_EVIDENCE_DELETION: "delete this original document",
}

# Why a hard-approval action always needs an explicit yes.
_HARD_WHY: dict[ActionKind, str] = {
    _A.MONEY_MOVEMENT: "This moves money.",
    _A.TAX_FILING: "This is a tax filing.",
    _A.BANK_DETAIL_CHANGE: "This changes bank details.",
    _A.LEGALLY_BINDING_ACCEPTANCE: "This is legally binding.",
    _A.ORIGINAL_EVIDENCE_DELETION: "This deletes an original document.",
}

_RANK = {Requirement.NONE: 0, Requirement.OWNER: 1, Requirement.HARD: 2}


def _as_action(action: ActionKind | str) -> ActionKind:
    try:
        return ActionKind(action)
    except ValueError:
        raise PolicyError(f"unknown action {action!r}") from None


def level_of(action: ActionKind | str) -> ActionLevel:
    """The fixed §25 level of an action."""
    return ACTION_LEVELS[_as_action(action)]


def is_grantable(action: ActionKind | str) -> bool:
    """Only AUTOMATIC_IF_AUTHORIZED actions can be pre-granted (§25)."""
    return level_of(action) is ActionLevel.AUTOMATIC_IF_AUTHORIZED


def _require_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetimes must be timezone-aware")
    return value


def _non_blank(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be blank")
    return value


# An identifier or name: "" or "  " would let unrelated records match each other.
NonBlank = Annotated[str, AfterValidator(_non_blank)]


class Grant(BaseModel):
    """Standing permission for one AUTOMATIC_IF_AUTHORIZED action.

    ``entity_id=None`` covers every company of the tenant (§51).
    Constructing a grant for any other level fails, including when a stored
    policy is loaded, so hard-approval actions can never be pre-granted.
    """

    model_config = ConfigDict(frozen=True)

    action: ActionKind
    entity_id: NonBlank | None = None
    granted_by: NonBlank
    granted_at: datetime

    @field_validator("action")
    @classmethod
    def _only_grantable(cls, action: ActionKind) -> ActionKind:
        if not is_grantable(action):
            raise ValueError(
                f"{action.value} is {ACTION_LEVELS[action].value} and cannot be pre-granted"
            )
        return action

    @field_validator("granted_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return _require_aware(value)

    def covers(self, action: ActionKind | str, entity_id: str | None) -> bool:
        return self.action is _as_action(action) and (
            self.entity_id is None or self.entity_id == entity_id
        )


class TenantPolicy(BaseModel):
    """A tenant's standing grants. Immutable; use ``with_grant`` / ``without_grant``."""

    model_config = ConfigDict(frozen=True)

    tenant_id: NonBlank
    grants: tuple[Grant, ...] = ()

    def allows(self, action: ActionKind | str, entity_id: str | None = None) -> bool:
        """True when a grant covers ``action`` for ``entity_id``."""
        return any(g.covers(action, entity_id) for g in self.grants)

    def with_grant(
        self,
        action: ActionKind | str,
        *,
        granted_by: str,
        entity_id: str | None = None,
        at: datetime | None = None,
    ) -> TenantPolicy:
        """Return a copy that grants ``action`` (replacing an identical-scope grant)."""
        action = _as_action(action)
        grant = Grant(
            action=action,
            entity_id=entity_id,
            granted_by=granted_by,
            granted_at=at or utcnow(),
        )
        kept = tuple(
            g for g in self.grants if (g.action, g.entity_id) != (action, entity_id)
        )
        return self.model_copy(update={"grants": kept + (grant,)})

    def without_grant(
        self, action: ActionKind | str, *, entity_id: str | None = None
    ) -> TenantPolicy:
        """Return a copy without the grant of exactly this scope."""
        action = _as_action(action)
        kept = tuple(
            g for g in self.grants if (g.action, g.entity_id) != (action, entity_id)
        )
        return self.model_copy(update={"grants": kept})


class Approval(BaseModel):
    """One human yes for one specific action instance.

    ``fingerprint`` binds the approval to the exact facts shown to the approver
    (amount, beneficiary, ...), so anything that changes afterwards voids it.
    Hard-approval actions require it. ``acknowledged_risk`` records that the
    approver saw the §26 warning. Hard approvals are issued only after step-up
    authentication (§52). ``id`` lets the audit record (§55) point at this
    decision and lets the executor use it once.
    """

    model_config = ConfigDict(frozen=True)

    id: NonBlank = Field(default_factory=lambda: new_id("apr"))
    tenant_id: NonBlank
    action: ActionKind
    subject_id: NonBlank
    level: Requirement
    approved_by: NonBlank
    approved_at: datetime
    entity_id: NonBlank | None = None
    fingerprint: NonBlank | None = None
    acknowledged_risk: bool = False

    @field_validator("level")
    @classmethod
    def _real_level(cls, level: Requirement) -> Requirement:
        if level is Requirement.NONE:
            raise ValueError("an approval must be OWNER or HARD")
        return level

    @field_validator("approved_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return _require_aware(value)


class ActionContext(BaseModel):
    """Facts about the specific action instance being authorized."""

    model_config = ConfigDict(frozen=True)

    tenant_id: NonBlank
    entity_id: NonBlank | None = None
    subject_id: NonBlank | None = None
    fingerprint: NonBlank | None = None  # digest of the facts an approver would see
    beneficiary_changed: bool = False  # §26: changed IBAN / new payment recipient
    fraud_hold: bool = False  # §26: any other hard-stop signal
    unusual: bool = False  # not routine: escalates authorizable actions to the owner
    approval: Approval | None = None

    @property
    def hard_stop(self) -> bool:
        return self.beneficiary_changed or self.fraud_hold


class Decision(BaseModel):
    """Outcome of :func:`authorize`. ``requires`` is what is still needed.

    ``approval_id`` names the human approval that allowed the action, if one
    did; record it with the action (§55).
    """

    model_config = ConfigDict(frozen=True)

    action: ActionKind
    level: ActionLevel
    allowed_now: bool
    requires: Requirement
    reason_plain: str
    approval_id: str | None = None


def effective_level(action: ActionKind | str, context: ActionContext) -> ActionLevel:
    """The level after §26 and 'unusual' escalation. Never lowers a level."""
    action = _as_action(action)
    base = ACTION_LEVELS[action]
    if context.hard_stop and action not in OBSERVATION_ACTIONS:
        return ActionLevel.HARD_APPROVAL
    if context.unusual and base is ActionLevel.AUTOMATIC_IF_AUTHORIZED:
        return ActionLevel.OWNER_APPROVAL
    return base


def authorize(
    action: ActionKind | str, policy: TenantPolicy, context: ActionContext
) -> Decision:
    """Decide whether ``action`` may run now (§25, §26).

    Raises :class:`PolicyError` when the context belongs to another tenant or
    the action is unknown.
    """
    action = _as_action(action)
    if context.tenant_id != policy.tenant_id:
        raise PolicyError("context tenant does not match policy tenant")
    level = effective_level(action, context)

    if level is ActionLevel.FULLY_AUTOMATIC:
        return _allow(action, level, "Routine work. No approval needed.")
    if level is ActionLevel.AUTOMATIC_IF_AUTHORIZED and policy.allows(
        action, context.entity_id
    ):
        return _allow(action, level, "You allowed me to do this automatically.")

    needed = (
        Requirement.HARD if level is ActionLevel.HARD_APPROVAL else Requirement.OWNER
    )
    approval = context.approval
    if approval is not None and _approval_matches(approval, action, context, needed):
        return _allow(action, level, "You approved this.", approval_id=approval.id)
    stale = (
        approval is not None
        and approval.action is action
        and approval.subject_id == context.subject_id
        and approval.fingerprint is not None
        and approval.fingerprint != context.fingerprint
    )
    reason = _blocked_reason(action, level, context, stale=stale)
    return Decision(
        action=action,
        level=level,
        allowed_now=False,
        requires=needed,
        reason_plain=reason,
    )


def _allow(
    action: ActionKind, level: ActionLevel, reason: str, *, approval_id: str | None = None
) -> Decision:
    return Decision(
        action=action,
        level=level,
        allowed_now=True,
        requires=Requirement.NONE,
        reason_plain=reason,
        approval_id=approval_id,
    )


def _approval_matches(
    approval: Approval, action: ActionKind, context: ActionContext, needed: Requirement
) -> bool:
    """An approval counts only for this exact instance, level and set of facts.

    A hard approval must be bound to the facts: both sides carry the same
    fingerprint (blank fingerprints are refused on construction).
    """
    return (
        approval.tenant_id == context.tenant_id
        and approval.action is action
        and context.subject_id is not None
        and approval.subject_id == context.subject_id
        and approval.entity_id == context.entity_id
        and approval.fingerprint == context.fingerprint
        and (needed is not Requirement.HARD or context.fingerprint is not None)
        and _RANK[approval.level] >= _RANK[needed]
        and (approval.acknowledged_risk or not context.hard_stop)
    )


def _blocked_reason(
    action: ActionKind, level: ActionLevel, context: ActionContext, *, stale: bool
) -> str:
    if context.beneficiary_changed and action not in OBSERVATION_ACTIONS:
        return "The bank details changed. I need your explicit approval."
    if context.fraud_hold and action not in OBSERVATION_ACTIONS:
        return "Something here looks unusual. I need your explicit approval."
    if stale:
        return "Something changed since you approved. I need your approval again."
    if level is ActionLevel.HARD_APPROVAL:
        return f"{_HARD_WHY[action]} I need your explicit approval every time."
    verb = _VERB[action]
    if context.unusual and ACTION_LEVELS[action] is ActionLevel.AUTOMATIC_IF_AUTHORIZED:
        return f"This is not routine. I need your OK before I {verb}."
    if ACTION_LEVELS[action] is ActionLevel.AUTOMATIC_IF_AUTHORIZED:
        return f"I need your OK before I {verb}. You can let me do this automatically."
    return f"I need your OK before I {verb}."
