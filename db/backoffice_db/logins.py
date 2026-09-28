"""Login users for the services (§52 least privilege).

Migrations create NOLOGIN group roles only; passwords never live in a
migration. At deploy time the platform creates (or re-keys) each service's
login and makes it a member of exactly the group it needs::

    APP_DB_PASSWORD=... python -m backoffice_db ensure-login backoffice_api --member-of backoffice_app

The password is read from an environment variable (injected from the secrets
vault), never from the command line.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from .executor import SqlExecutor
from .tenancy import GROUP_ROLES

__all__ = ["MIN_PASSWORD_LENGTH", "ensure_login", "ensure_login_sql"]

MIN_PASSWORD_LENGTH = 16
_ROLE = re.compile(r"[a-z_][a-z0-9_]{0,62}")


def ensure_login_sql(user: str, password: str, member_of: Sequence[str]) -> str:
    """Script creating or updating ``user`` as a plain login in ``member_of``."""
    if not _ROLE.fullmatch(user) or user in GROUP_ROLES or user.startswith("pg_"):
        raise ValueError("login name must be a plain lower-case identifier, not a group role")
    groups = list(dict.fromkeys(member_of))
    if not groups or any(g not in GROUP_ROLES for g in groups):
        raise ValueError(f"member_of must name group roles from {GROUP_ROLES}")
    if len(password) < MIN_PASSWORD_LENGTH or "\x00" in password:
        raise ValueError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    literal = "'" + password.replace("'", "''") + "'"
    return (
        "SET LOCAL standard_conforming_strings = on;\n"
        "DO $$ BEGIN\n"
        f"    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{user}') THEN\n"
        f"        CREATE ROLE {user} NOLOGIN;\n"
        "    END IF;\n"
        "END $$;\n"
        f"ALTER ROLE {user} WITH LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE "
        f"NOREPLICATION PASSWORD {literal};\n"
        f"GRANT {', '.join(groups)} TO {user};\n"
    )


def ensure_login(executor: SqlExecutor, user: str, password: str, member_of: Sequence[str]) -> None:
    """Create or re-key ``user`` (idempotent). Must run as a role with CREATEROLE."""
    executor.execute(ensure_login_sql(user, password, member_of))
