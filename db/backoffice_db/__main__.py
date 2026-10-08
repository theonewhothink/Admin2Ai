"""Command line: ``python -m backoffice_db {lint,status,migrate,check,ensure-login}``.

The database URL comes from ``--database-url``, else ``MIGRATION_DATABASE_URL``,
else ``DATABASE_URL``; a URL without a password uses ``PGPASSWORD`` (e.g.
injected from Secrets Manager). Migrations and logins must run as the schema
owner, never as the application's login. Output is for operators, never for
business owners.

Exit codes: 0 ok, 1 problems found (drift, lint, catalog), 2 configuration
error, 3 blocked by a missing PostgreSQL extension, 4 database error.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from .catalog import check_catalog
from .executor import ConnectionParams, PsqlError, PsqlExecutor
from .logins import ensure_login
from .migrations import (
    MIGRATIONS_DIR,
    Migration,
    MigrationBlocked,
    MigrationDrift,
    MigrationError,
    MigrationLayoutError,
    load_migrations,
    migrate,
    status,
)
from .tenancy import APP_ROLE, GROUP_ROLES

OK, PROBLEMS, CONFIG, BLOCKED, DATABASE = 0, 1, 2, 3, 4


def _parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--database-url", help="postgresql:// URL (default: $MIGRATION_DATABASE_URL, $DATABASE_URL)")
    common.add_argument("--dir", type=Path, default=MIGRATIONS_DIR, help="migrations directory")
    parser = argparse.ArgumentParser(prog="python -m backoffice_db", description="Back-office database tooling.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("lint", parents=[common], help="check migration files (no database)")
    commands.add_parser("status", parents=[common], help="applied / pending / drift")
    run_migrate = commands.add_parser("migrate", parents=[common], help="apply pending migrations")
    run_migrate.add_argument("--target", help="stop after this version (e.g. 0003)")
    commands.add_parser("check", parents=[common], help="verify schema invariants in the catalog")
    login = commands.add_parser("ensure-login", parents=[common], help="create or re-key a service login")
    login.add_argument("user", help="login name, e.g. backoffice_api")
    login.add_argument("--member-of", action="append", choices=GROUP_ROLES, help=f"group role (default {APP_ROLE})")
    login.add_argument("--password-env", default="APP_DB_PASSWORD", help="variable holding the password")
    return parser


def _database_url(args: argparse.Namespace, env: Mapping[str, str]) -> str:
    url = args.database_url or env.get("MIGRATION_DATABASE_URL") or env.get("DATABASE_URL")
    if not url:
        raise ValueError("no database URL: pass --database-url or set MIGRATION_DATABASE_URL")
    return url


def run(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    args = _parser().parse_args(argv)
    environment = os.environ if env is None else env
    try:
        migrations = load_migrations(args.dir)
        if args.command == "lint":
            print(f"{len(migrations)} migrations OK")
            return OK
        executor = PsqlExecutor(
            ConnectionParams.from_url(_database_url(args, environment)),
            base_env=environment,
        )
        return _dispatch(args, executor, migrations, environment)
    except MigrationLayoutError as err:
        print(f"migrations: {err}", file=sys.stderr)
        return PROBLEMS
    except MigrationDrift as err:
        print(f"drift: {err}", file=sys.stderr)
        return PROBLEMS
    except MigrationBlocked as err:
        print(f"blocked: {err}", file=sys.stderr)
        return BLOCKED
    except (ValueError, FileNotFoundError, MigrationError) as err:
        print(f"configuration: {err}", file=sys.stderr)
        return CONFIG
    except PsqlError as err:
        print(f"database: {err}", file=sys.stderr)
        return DATABASE


def _dispatch(
    args: argparse.Namespace,
    executor: PsqlExecutor,
    migrations: Sequence[Migration],
    env: Mapping[str, str],
) -> int:
    if args.command == "migrate":
        applied = migrate(executor, migrations, target=args.target)
        names = ", ".join(m.filename for m in applied) or "nothing to apply"
        print(f"applied: {names}")
        return OK
    if args.command == "status":
        return _print_status(executor, migrations)
    if args.command == "ensure-login":
        password = env.get(args.password_env, "")
        if not password:
            raise ValueError(f"set {args.password_env} to the login's password")
        ensure_login(executor, args.user, password, args.member_of or [APP_ROLE])
        print(f"login {args.user}: ready")
        return OK
    problems = check_catalog(executor)
    for problem in problems:
        print(f"catalog: {problem}", file=sys.stderr)
    if not problems:
        print("catalog OK")
    return PROBLEMS if problems else OK


def _print_status(executor: PsqlExecutor, migrations: Sequence[Migration]) -> int:
    current = status(executor, migrations)
    for a in current.applied:
        print(f"applied  {a.version}_{a.name}")
    for m in current.pending:
        print(f"pending  {m.filename}")
    for v in current.changed:
        print(f"CHANGED  {v} (file edited after it was applied)")
    for v in current.unknown:
        print(f"UNKNOWN  {v} (applied, but not in this checkout)")
    return OK if current.ok else PROBLEMS


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(run())
