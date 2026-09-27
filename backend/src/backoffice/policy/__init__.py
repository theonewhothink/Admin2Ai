"""Policy layer: what the operator may do on its own (§25–26) and what may
leave our infrastructure (§52–53).

Public API
----------
Action levels (§25, §26):
    ActionKind, ActionLevel, Requirement, ACTION_LEVELS, OBSERVATION_ACTIONS
    TenantPolicy(tenant_id, grants).with_grant(action, granted_by=, entity_id=None)
    Grant, Approval, ActionContext, Decision, PolicyError
    authorize(action, policy, context) -> Decision
    effective_level(action, context) -> ActionLevel
    level_of(action), is_grantable(action)

Privacy (§53):
    redact(text, vault=None, kinds=ALL_KINDS) -> Redaction(text, vault, matches)
    Redaction.restore(external_text) / TokenVault.restore(text)
    find_pii(text, kinds=ALL_KINDS) -> list[PiiMatch]
    is_clean(text) -> bool, iban_is_valid(s), nib_is_valid(digits), luhn_is_valid(digits)
    PiiKind, PiiMatch, TokenVault, ALL_KINDS
"""

from .actions import (
    ACTION_LEVELS,
    OBSERVATION_ACTIONS,
    ActionContext,
    ActionKind,
    ActionLevel,
    Approval,
    Decision,
    Grant,
    PolicyError,
    Requirement,
    TenantPolicy,
    authorize,
    effective_level,
    is_grantable,
    level_of,
)
from .privacy import (
    ALL_KINDS,
    PiiKind,
    PiiMatch,
    Redaction,
    TokenVault,
    find_pii,
    iban_is_valid,
    is_clean,
    luhn_is_valid,
    nib_is_valid,
    redact,
)

__all__ = [
    "ACTION_LEVELS",
    "ALL_KINDS",
    "OBSERVATION_ACTIONS",
    "ActionContext",
    "ActionKind",
    "ActionLevel",
    "Approval",
    "Decision",
    "Grant",
    "PiiKind",
    "PiiMatch",
    "PolicyError",
    "Redaction",
    "Requirement",
    "TenantPolicy",
    "TokenVault",
    "authorize",
    "effective_level",
    "find_pii",
    "iban_is_valid",
    "is_clean",
    "is_grantable",
    "level_of",
    "luhn_is_valid",
    "nib_is_valid",
    "redact",
]
