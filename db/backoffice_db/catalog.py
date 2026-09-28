"""Invariants of the migrated schema, read from the live catalog (§52, §55, §57).

The migrations are written to satisfy these rules; :func:`check_catalog`
proves it against a real database, so a later migration that forgets row-level
security, adds a float column or an unguarded history table fails CI:

* every table has row-level security enabled AND forced, with a policy that
  reads ``app.tenant_id`` (only the runner's ``schema_migrations`` is exempt);
* no column is binary floating point or ``money``; every money column is
  ``numeric(18,2)`` in a table that also has a ``char(3)`` currency column;
* history tables refuse UPDATE, DELETE and TRUNCATE (trigger), and the
  application role holds no UPDATE, DELETE or TRUNCATE privilege on them;
* the group roles exist and none is superuser, ``BYPASSRLS`` or a table owner;
* SECURITY DEFINER functions pin ``search_path`` and are not executable by PUBLIC;
* embedding tables say, in their comment, that they are never authoritative (§44).
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass

from .executor import SqlExecutor
from .tenancy import APP_ROLE, GROUP_ROLES

__all__ = ["APPEND_ONLY_TABLES", "RLS_EXEMPT_TABLES", "CatalogProblem", "check_catalog"]

# Tables whose rows are history: never edited, never removed (evidence only
# through an approved deletion request).
APPEND_ONLY_TABLES = (
    "evidence",
    "evidence_sightings",
    "evidence_deletion_requests",
    "field_observations",
    "match_factors",
    "tracked_item_transitions",
    "needs_you_answers",
    "audit_log",
)
RLS_EXEMPT_TABLES = frozenset({"schema_migrations"})
_MONEY_NAME = re.compile(r"^(?:amount|.+_amount|fee|difference|conversion_cost)$")

# pg_trigger.tgtype bits
_ROW, _BEFORE, _DELETE, _UPDATE, _TRUNCATE = 1, 2, 8, 16, 32


@dataclass(frozen=True)
class CatalogProblem:
    table: str
    rule: str
    detail: str

    def __str__(self) -> str:
        return f"{self.table}: {self.rule} ({self.detail})"


def check_catalog(executor: SqlExecutor, schema: str = "public") -> tuple[CatalogProblem, ...]:
    """Every violated invariant in ``schema``; empty when the schema is sound."""
    if not re.fullmatch(r"[a-z_][a-z0-9_]*", schema):
        raise ValueError("schema must be a plain lower-case identifier")
    problems: list[CatalogProblem] = []
    problems += _row_level_security(executor, schema)
    problems += _column_types(executor, schema)
    problems += _append_only(executor, schema)
    problems += _roles(executor, schema)
    problems += _definer_functions(executor, schema)
    problems += _embedding_comments(executor, schema)
    return tuple(problems)


def _row_level_security(executor: SqlExecutor, schema: str) -> list[CatalogProblem]:
    rows = executor.query(
        f"""
        SELECT c.relname AS table_name,
               c.relrowsecurity AS enabled,
               c.relforcerowsecurity AS forced,
               EXISTS (
                   SELECT 1 FROM pg_policy p
                   WHERE p.polrelid = c.oid
                     AND (coalesce(pg_get_expr(p.polqual, p.polrelid), '')
                          || coalesce(pg_get_expr(p.polwithcheck, p.polrelid), ''))
                         LIKE '%app.tenant_id%'
               ) AS tenant_policy
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = '{schema}' AND c.relkind IN ('r', 'p')
        ORDER BY 1
        """
    )
    problems = []
    for r in rows:
        name = str(r["table_name"])
        if name in RLS_EXEMPT_TABLES:
            continue
        if r["enabled"] != "t" or r["forced"] != "t":
            problems.append(CatalogProblem(name, "row level security", "must be enabled and forced"))
        if r["tenant_policy"] != "t":
            problems.append(CatalogProblem(name, "row level security", "no policy reads app.tenant_id"))
    return problems


def _column_types(executor: SqlExecutor, schema: str) -> list[CatalogProblem]:
    rows = executor.query(
        f"""
        SELECT c.relname AS table_name, a.attname AS column_name,
               bt.typname AS base_type,
               format_type(coalesce(nullif(t.typbasetype, 0), t.oid),
                           CASE WHEN t.typbasetype <> 0 THEN t.typtypmod ELSE a.atttypmod END)
                   AS full_type
        FROM pg_attribute a
        JOIN pg_class c ON c.oid = a.attrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        JOIN pg_type t ON t.oid = a.atttypid
        JOIN pg_type bt ON bt.oid = coalesce(nullif(t.typbasetype, 0), t.oid)
        WHERE n.nspname = '{schema}' AND c.relkind IN ('r', 'p') AND a.attnum > 0 AND NOT a.attisdropped
        ORDER BY 1, 2
        """
    )
    columns: dict[str, dict[str, tuple[str, str]]] = defaultdict(dict)
    for r in rows:
        columns[str(r["table_name"])][str(r["column_name"])] = (str(r["base_type"]), str(r["full_type"]))
    problems = []
    for table, cols in columns.items():
        has_currency = any(
            (name == "currency" or name.endswith("_currency")) and full == "character(3)"
            for name, (_base, full) in cols.items()
        )
        for name, (base, full) in cols.items():
            if base in ("float4", "float8", "money"):
                problems.append(CatalogProblem(table, "no float or money type", f"{name} is {full}"))
            if _MONEY_NAME.fullmatch(name):
                if full != "numeric(18,2)":
                    problems.append(CatalogProblem(table, "money is numeric(18,2)", f"{name} is {full}"))
                if not has_currency:
                    problems.append(CatalogProblem(table, "money has a currency", f"{name} has no char(3) currency"))
    return problems


def _append_only(executor: SqlExecutor, schema: str) -> list[CatalogProblem]:
    listed = ", ".join(f"'{t}'" for t in APPEND_ONLY_TABLES)
    rows = executor.query(
        f"""
        SELECT c.relname AS table_name,
               string_agg(tg.tgtype::text, ',') FILTER (WHERE tg.tgenabled <> 'D') AS types,
               has_table_privilege('{APP_ROLE}', c.oid, 'DELETE') AS app_delete,
               has_table_privilege('{APP_ROLE}', c.oid, 'TRUNCATE') AS app_truncate,
               has_any_column_privilege('{APP_ROLE}', c.oid, 'UPDATE') AS app_update
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        LEFT JOIN pg_trigger tg ON tg.tgrelid = c.oid AND NOT tg.tgisinternal
        WHERE n.nspname = '{schema}' AND c.relname IN ({listed})
        GROUP BY c.relname, c.oid
        """
    )
    seen = {str(r["table_name"]): r for r in rows}
    problems = []
    for table in APPEND_ONLY_TABLES:
        r = seen.get(table)
        if r is None:
            problems.append(CatalogProblem(table, "append-only", "table is missing"))
            continue
        types = [int(t) for t in (r["types"] or "").split(",") if t]
        row_before = [t for t in types if t & _BEFORE and t & _ROW]
        if not any(t & _UPDATE for t in row_before) or not any(t & _DELETE for t in row_before):
            problems.append(CatalogProblem(table, "append-only", "needs BEFORE UPDATE and DELETE row triggers"))
        if not any(t & _TRUNCATE and t & _BEFORE for t in types):
            problems.append(CatalogProblem(table, "append-only", "needs a BEFORE TRUNCATE trigger"))
        for privilege in ("delete", "truncate", "update"):
            if r[f"app_{privilege}"] == "t":
                problems.append(
                    CatalogProblem(table, "append-only", f"{APP_ROLE} must not have {privilege.upper()}")
                )
    return problems


def _roles(executor: SqlExecutor, schema: str) -> list[CatalogProblem]:
    listed = ", ".join(f"'{r}'" for r in GROUP_ROLES)
    rows = executor.query(
        f"""
        SELECT r.rolname,
               r.rolsuper OR r.rolbypassrls AS privileged,
               EXISTS (
                   SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                   WHERE n.nspname = '{schema}' AND c.relowner = r.oid
               ) AS owns_tables
        FROM pg_roles r WHERE r.rolname IN ({listed})
        """
    )
    found = {str(r["rolname"]): r for r in rows}
    problems = []
    for role in GROUP_ROLES:
        r = found.get(role)
        if r is None:
            problems.append(CatalogProblem(role, "roles", "role is missing"))
        elif r["privileged"] == "t":
            problems.append(CatalogProblem(role, "roles", "must not be superuser or bypass RLS"))
        elif r["owns_tables"] == "t":
            problems.append(CatalogProblem(role, "roles", "must not own tables (owners can skip policies)"))
    return problems


def _definer_functions(executor: SqlExecutor, schema: str) -> list[CatalogProblem]:
    rows = executor.query(
        f"""
        SELECT p.proname,
               coalesce(array_to_string(p.proconfig, ','), '') LIKE '%search_path=%' AS pinned,
               EXISTS (
                   SELECT 1 FROM aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) a
                   WHERE a.grantee = 0 AND a.privilege_type = 'EXECUTE'
               ) AS public_execute
        FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = '{schema}' AND p.prosecdef
        """
    )
    problems = []
    for r in rows:
        name = f"{r['proname']}()"
        if r["pinned"] != "t":
            problems.append(CatalogProblem(name, "security definer", "must SET search_path"))
        if r["public_execute"] == "t":
            problems.append(CatalogProblem(name, "security definer", "must not be executable by PUBLIC"))
    return problems


def _embedding_comments(executor: SqlExecutor, schema: str) -> list[CatalogProblem]:
    rows = executor.query(
        f"""
        SELECT DISTINCT c.relname AS table_name, coalesce(obj_description(c.oid, 'pg_class'), '') AS note
        FROM pg_attribute a
        JOIN pg_class c ON c.oid = a.attrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        JOIN pg_type t ON t.oid = a.atttypid
        WHERE n.nspname = '{schema}' AND c.relkind IN ('r', 'p') AND t.typname = 'vector'
          AND a.attnum > 0 AND NOT a.attisdropped
        """
    )
    return [
        CatalogProblem(str(r["table_name"]), "embeddings", "comment must say they are never authoritative")
        for r in rows
        if "never authoritative" not in str(r["note"]).lower()
    ]
