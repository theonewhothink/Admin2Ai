"""Tenant isolation contract between the application and PostgreSQL (§52).

Every tenant table has row-level security ENABLED and FORCED with the policy

    tenant_id = current_setting('app.tenant_id', true)

so a session sees and writes only the rows of the tenant it declared, and a
session that declared nothing sees nothing. The application connects as a
login user that is a member of :data:`APP_ROLE` (never a superuser, never the
table owner, never ``BYPASSRLS``) and starts EVERY transaction with::

    cursor.execute(SCOPE_SQL, scope_params(tenant_id, user_id))

``set_config(..., true)`` is transaction-local (``SET LOCAL``): when the
transaction ends the setting is gone, so a pooled connection cannot carry one
tenant's scope into the next request.
"""

from __future__ import annotations

import re

__all__ = [
    "APP_ROLE",
    "EVIDENCE_ADMIN_ROLE",
    "GROUP_ROLES",
    "READONLY_ROLE",
    "SCHEDULER_ROLE",
    "SCOPE_SQL",
    "TENANT_SETTING",
    "USER_SETTING",
    "scope_params",
    "scope_sql",
    "validate_id",
    "validate_tenant_id",
]

TENANT_SETTING = "app.tenant_id"
USER_SETTING = "app.user_id"

APP_ROLE = "backoffice_app"  # api and worker: DML under RLS
READONLY_ROLE = "backoffice_readonly"  # support and analytics: SELECT under RLS
EVIDENCE_ADMIN_ROLE = "backoffice_evidence_admin"  # hard-approved evidence deletion (§25)
SCHEDULER_ROLE = "backoffice_scheduler"  # may list tenant ids for periodic fan-out
GROUP_ROLES = (APP_ROLE, READONLY_ROLE, EVIDENCE_ADMIN_ROLE, SCHEDULER_ROLE)

# Same alphabets as the tenant_key and bo_id domains in 0001_core.sql.
_TENANT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


def scope_sql(placeholder: str = "%s") -> str:
    """The statement that scopes one transaction to a tenant (and signed-in user)."""
    if placeholder not in ("%s", "?", "$1"):
        raise ValueError("placeholder must be %s, ? or $1")
    second = "$2" if placeholder == "$1" else placeholder
    return (
        f"SELECT set_config('{TENANT_SETTING}', {placeholder}, true), "
        f"set_config('{USER_SETTING}', {second}, true)"
    )


SCOPE_SQL = scope_sql()


def validate_tenant_id(value: object) -> str:
    """``value`` if it is a well-formed tenant id, else ``ValueError``."""
    if not isinstance(value, str) or not _TENANT_ID.fullmatch(value):
        raise ValueError("tenant id must be 1-128 letters, digits, '_', '.' or '-'")
    return value


def validate_id(value: object) -> str:
    """``value`` if it is a well-formed record id (models.new_id style), else ``ValueError``."""
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError("id must be 1-128 letters, digits, '_', '.', ':' or '-'")
    return value


def scope_params(tenant_id: str, user_id: str | None = None) -> tuple[str, str]:
    """Parameters for :data:`SCOPE_SQL`. Without a user, the user setting is cleared."""
    return validate_tenant_id(tenant_id), "" if user_id is None else validate_id(user_id)
