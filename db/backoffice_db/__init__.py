"""Database tooling for the AI Back-Office Operator (§44 PostgreSQL, §52 isolation).

The schema itself lives in ``db/migrations/NNNN_name.sql``; this package
applies it safely and states the contract the application must honour.

Migrations::

    from backoffice_db import PsqlExecutor, load_migrations, migrate
    applied = migrate(PsqlExecutor.from_url(url), load_migrations())

    # or: PYTHONPATH=db python -m backoffice_db migrate   (MIGRATION_DATABASE_URL)

Tenant scope, at the start of EVERY application transaction (§52)::

    from backoffice_db import SCOPE_SQL, scope_params
    cursor.execute(SCOPE_SQL, scope_params(tenant_id, user_id))

Money parameters for numeric(18,2) columns (refuses instead of rounding)::

    from backoffice_db import db_amount
    cursor.execute("... VALUES (%s)", (db_amount(document.gross_amount),))

Audit log: ``backoffice.audit.PostgresAuditStore(connect, table="audit_log")``
reads and writes the ``audit_log`` table of migration 0004.

Evidence deletion (§25 hard approval): insert an ``evidence_deletion_requests``
row, have an owner approve it (status, approved_by, approval_reference), then,
as a member of :data:`EVIDENCE_ADMIN_ROLE`, ``SELECT
execute_evidence_deletion(request_id, actor)``; it returns the object key to
purge from the evidence bucket.

Service logins (passwords from the environment, never from migrations)::

    ensure_login(executor, "backoffice_api", password, [APP_ROLE])
    # or: APP_DB_PASSWORD=... python -m backoffice_db ensure-login backoffice_api

Schema invariants (RLS everywhere, no floats, guarded history tables...)::

    problems = check_catalog(executor)    # or: python -m backoffice_db check
"""

from __future__ import annotations

from .catalog import APPEND_ONLY_TABLES, IDENTITY_TABLES, RLS_EXEMPT_TABLES, CatalogProblem, check_catalog
from .executor import ConnectionParams, PsqlError, PsqlExecutor, Row, SqlExecutor
from .logins import MIN_PASSWORD_LENGTH, ensure_login, ensure_login_sql
from .migrations import (
    MIGRATIONS_DIR,
    AppliedMigration,
    Migration,
    MigrationBlocked,
    MigrationDrift,
    MigrationError,
    MigrationLayoutError,
    MigrationStatus,
    lint_sql,
    load_migrations,
    migrate,
    status,
)
from .money import MAX_ABS_AMOUNT, db_amount, db_currency
from .tenancy import (
    API_KEY_SETTING,
    APP_ROLE,
    ERASURE_SETTING,
    EVIDENCE_ADMIN_ROLE,
    GROUP_ROLES,
    IDENTITY_SETTINGS,
    INVITE_SETTING,
    LOGIN_EMAIL_SETTING,
    RATE_SUBJECTS_SETTING,
    READONLY_ROLE,
    SCHEDULER_ROLE,
    SCOPE_SQL,
    SESSION_SETTING,
    SETTING_SQL,
    TENANT_SETTING,
    USER_SETTING,
    scope_params,
    scope_sql,
    setting_params,
    validate_id,
    validate_tenant_id,
)

__all__ = [
    "APPEND_ONLY_TABLES",
    "API_KEY_SETTING",
    "APP_ROLE",
    "AppliedMigration",
    "CatalogProblem",
    "ConnectionParams",
    "ERASURE_SETTING",
    "EVIDENCE_ADMIN_ROLE",
    "GROUP_ROLES",
    "IDENTITY_SETTINGS",
    "IDENTITY_TABLES",
    "INVITE_SETTING",
    "LOGIN_EMAIL_SETTING",
    "MAX_ABS_AMOUNT",
    "MIN_PASSWORD_LENGTH",
    "MIGRATIONS_DIR",
    "Migration",
    "MigrationBlocked",
    "MigrationDrift",
    "MigrationError",
    "MigrationLayoutError",
    "MigrationStatus",
    "PsqlError",
    "PsqlExecutor",
    "RATE_SUBJECTS_SETTING",
    "READONLY_ROLE",
    "RLS_EXEMPT_TABLES",
    "Row",
    "SCHEDULER_ROLE",
    "SCOPE_SQL",
    "SESSION_SETTING",
    "SETTING_SQL",
    "SqlExecutor",
    "TENANT_SETTING",
    "USER_SETTING",
    "check_catalog",
    "db_amount",
    "db_currency",
    "ensure_login",
    "ensure_login_sql",
    "lint_sql",
    "load_migrations",
    "migrate",
    "scope_params",
    "scope_sql",
    "setting_params",
    "status",
    "validate_id",
    "validate_tenant_id",
]
