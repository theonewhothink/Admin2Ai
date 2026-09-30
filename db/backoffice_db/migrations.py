"""Numbered SQL migrations: discovery, lint, status and a safe runner (§44, §52).

Layout: ``db/migrations/NNNN_name.sql``, numbered from 0001 without gaps.
Each file is plain SQL, applied in ONE transaction together with its
bookkeeping row in ``schema_migrations`` (version, name, SHA-256 checksum), so
a migration is either fully applied and recorded or not at all.

Guarantees:

* **Drift is refused.** If an applied file was edited, or the database knows a
  version this checkout does not, nothing runs (:class:`MigrationDrift`).
* **Missing extensions stop the run.** A file that starts with
  ``-- requires-extension: vector`` is only applied when the extension is
  available; otherwise :class:`MigrationBlocked` names it. The migration is
  never recorded as applied without its objects.
* **Concurrent runners are safe.** Each transaction takes an advisory lock
  and inserts its bookkeeping row first; a runner that loses the race sees the
  version applied by the other and moves on.
* **Files stay portable.** The linter rejects transaction control, psql
  meta-commands, ``CONCURRENTLY`` (impossible inside a transaction), binary
  floating point and the ``money`` type (money is numeric, §money rules), and
  timestamps without time zone.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .executor import PsqlError, SqlExecutor

__all__ = [
    "MIGRATIONS_DIR",
    "AppliedMigration",
    "Migration",
    "MigrationBlocked",
    "MigrationDrift",
    "MigrationError",
    "MigrationLayoutError",
    "MigrationStatus",
    "lint_sql",
    "load_migrations",
    "migrate",
    "status",
]

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

_FILENAME = re.compile(r"^(?P<version>[0-9]{4})_(?P<name>[a-z0-9]+(?:_[a-z0-9]+)*)\.sql$")
_REQUIRES = re.compile(r"^--\s*requires-extension:\s*(?P<ext>[a-z_][a-z0-9_]*)\s*$", re.MULTILINE)
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
# Advisory lock id shared by every runner: 'backoffice.migrations' as a bigint.
_LOCK_KEY = int.from_bytes(hashlib.sha256(b"backoffice.migrations").digest()[:8], "big", signed=True)

# The api's readiness check (GET /readyz) compares what was applied with the
# files it ships; the application role may read (only read) the bookkeeping.
STATUS_GRANT = """DO $status_grant$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'backoffice_app') THEN
        GRANT SELECT ON public.schema_migrations TO backoffice_app;
    END IF;
END
$status_grant$;"""

# Run under the same lock: concurrent CREATE TABLE IF NOT EXISTS can still collide.
BOOKKEEPING_DDL = f"""
SELECT pg_advisory_xact_lock({_LOCK_KEY});
CREATE TABLE IF NOT EXISTS public.schema_migrations (
    version     char(4)     PRIMARY KEY CHECK (version ~ '^[0-9]{{4}}$'),
    name        text        NOT NULL,
    checksum    char(64)    NOT NULL CHECK (checksum ~ '^[0-9a-f]{{64}}$'),
    applied_at  timestamptz NOT NULL DEFAULT now(),
    applied_by  text        NOT NULL DEFAULT current_user
);
REVOKE ALL ON public.schema_migrations FROM PUBLIC;
{STATUS_GRANT}
"""


class MigrationError(Exception):
    """Base class. Messages are for engineers, never shown to business owners."""


class MigrationLayoutError(MigrationError):
    """Files are misnamed, misnumbered or fail the lint."""


class MigrationDrift(MigrationError):
    """The database and the migration files disagree about what was applied."""


class MigrationBlocked(MigrationError):
    """A migration needs a PostgreSQL extension the server does not offer."""

    def __init__(self, message: str, *, migration: Migration, applied: Sequence[Migration]) -> None:
        super().__init__(message)
        self.migration = migration
        self.applied = tuple(applied)


@dataclass(frozen=True)
class Migration:
    version: str
    name: str
    path: Path
    sql: str
    checksum: str  # SHA-256 of the file with line endings normalised to \n
    requires_extensions: tuple[str, ...] = ()

    @property
    def filename(self) -> str:
        return f"{self.version}_{self.name}.sql"


@dataclass(frozen=True)
class AppliedMigration:
    version: str
    name: str
    checksum: str


@dataclass(frozen=True)
class MigrationStatus:
    applied: tuple[AppliedMigration, ...]
    pending: tuple[Migration, ...]
    changed: tuple[str, ...]  # applied versions whose file no longer matches
    unknown: tuple[str, ...]  # applied versions with no file in this checkout

    @property
    def ok(self) -> bool:
        return not self.changed and not self.unknown


# --------------------------------------------------------------------------- loading and lint


def checksum_of(sql: str) -> str:
    """Checksum independent of the checkout's line endings."""
    normalised = sql.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


def load_migrations(directory: Path | None = None) -> tuple[Migration, ...]:
    """All migrations in order, or :class:`MigrationLayoutError` listing every problem."""
    root = MIGRATIONS_DIR if directory is None else directory
    if not root.is_dir():
        raise MigrationLayoutError(f"no migrations directory at {root}")
    problems: list[str] = []
    found: dict[str, Migration] = {}
    for path in sorted(root.glob("*.sql")):
        match = _FILENAME.fullmatch(path.name)
        if not match:
            problems.append(f"{path.name}: name must be NNNN_lower_snake.sql")
            continue
        version = match.group("version")
        if version in found:
            problems.append(f"{path.name}: version {version} is used twice")
            continue
        sql = path.read_text(encoding="utf-8")
        problems.extend(f"{path.name}: {p}" for p in lint_sql(sql))
        found[version] = Migration(
            version=version,
            name=match.group("name"),
            path=path,
            sql=sql,
            checksum=checksum_of(sql),
            requires_extensions=tuple(_REQUIRES.findall(_header(sql))),
        )
    expected = [f"{n:04d}" for n in range(1, len(found) + 1)]
    if sorted(found) != expected:
        problems.append(f"versions must run 0001..{len(found):04d} without gaps, got {sorted(found)}")
    if problems:
        raise MigrationLayoutError("; ".join(problems))
    return tuple(found[v] for v in expected)


def _header(sql: str) -> str:
    """The leading comment block, where directives live."""
    lines: list[str] = []
    for line in sql.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("--"):
            break
        lines.append(stripped)
    return "\n".join(lines)


_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"(?:^|;)\s*(?:BEGIN|COMMIT|ROLLBACK|END|START\s+TRANSACTION|SAVEPOINT|RELEASE)\b",
                   re.IGNORECASE),
        "transaction control is the runner's job",
    ),
    (re.compile(r"\bCONCURRENTLY\b", re.IGNORECASE), "CONCURRENTLY cannot run inside a transaction"),
    (
        re.compile(r"\b(?:real|float4|float8|double\s+precision|float)\b", re.IGNORECASE),
        "binary floating point is not allowed (money and confidences are numeric)",
    ),
    (re.compile(r"\bmoney\b", re.IGNORECASE), "the money type is locale-dependent; use numeric(18,2)"),
    (
        re.compile(r"\btimestamp\b(?!\s+with\s+time\s+zone)", re.IGNORECASE),
        "timestamps must be timestamptz",
    ),
)


def lint_sql(sql: str) -> list[str]:
    """Problems in one migration's text (comments, strings and bodies are ignored)."""
    code = strip_sql(sql)
    problems = [message for pattern, message in _RULES if pattern.search(code)]
    if re.search(r"(?m)^\s*\\", code):
        problems.append("psql meta-commands are not allowed")
    return problems


def strip_sql(sql: str) -> str:
    """``sql`` with comments, quoted strings, quoted identifiers and dollar-quoted
    bodies blanked out, so the linter only sees statement structure."""
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if sql.startswith("--", i):
            end = sql.find("\n", i)
            i = n if end < 0 else end
        elif sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            i = n if end < 0 else end + 2
            out.append(" ")
        elif ch in ("'", '"'):
            i = _skip_quoted(sql, i, ch)
            out.append(" ")
        elif ch == "$" and (tag := _dollar_tag(sql, i)):
            end = sql.find(tag, i + len(tag))
            i = n if end < 0 else end + len(tag)
            out.append(" ")
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _skip_quoted(sql: str, start: int, quote: str) -> int:
    i = start + 1
    while i < len(sql):
        if sql[i] == quote:
            if i + 1 < len(sql) and sql[i + 1] == quote:  # doubled quote escape
                i += 2
                continue
            return i + 1
        i += 1
    return len(sql)


_DOLLAR = re.compile(r"\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$")


def _dollar_tag(sql: str, i: int) -> str | None:
    if i > 0 and (sql[i - 1].isalnum() or sql[i - 1] == "_"):
        return None  # part of an identifier such as foo$1
    match = _DOLLAR.match(sql, i)
    return match.group(0) if match else None


# --------------------------------------------------------------------------- database side


def status(executor: SqlExecutor, migrations: Sequence[Migration]) -> MigrationStatus:
    """Compare the database's ``schema_migrations`` with the files."""
    exists = executor.query("SELECT to_regclass('public.schema_migrations') IS NOT NULL AS present")
    rows = []
    if exists and exists[0]["present"] == "t":
        rows = executor.query(
            "SELECT version, name, checksum FROM public.schema_migrations ORDER BY version"
        )
    applied = tuple(
        AppliedMigration(str(r["version"]), str(r["name"]), str(r["checksum"]).strip()) for r in rows
    )
    by_version = {m.version: m for m in migrations}
    changed = tuple(a.version for a in applied if a.version in by_version
                    and by_version[a.version].checksum != a.checksum)
    unknown = tuple(a.version for a in applied if a.version not in by_version)
    done = {a.version for a in applied}
    pending = tuple(m for m in migrations if m.version not in done)
    return MigrationStatus(applied=applied, pending=pending, changed=changed, unknown=unknown)


def migrate(
    executor: SqlExecutor,
    migrations: Sequence[Migration],
    *,
    target: str | None = None,
) -> tuple[Migration, ...]:
    """Apply pending migrations in order (up to ``target``); return those applied here."""
    if target is not None and target not in {m.version for m in migrations}:
        raise MigrationError(f"unknown target version {target!r}")
    executor.execute(BOOKKEEPING_DDL)
    current = status(executor, migrations)
    _refuse_drift(current)
    applied: list[Migration] = []
    for migration in current.pending:
        if target is not None and migration.version > target:
            break
        missing = _missing_extensions(executor, migration.requires_extensions)
        if missing:
            raise MigrationBlocked(
                f"{migration.filename} needs the PostgreSQL extension(s) {', '.join(missing)}, "
                "which this server does not offer",
                migration=migration,
                applied=applied,
            )
        if _apply(executor, migration, migrations):
            applied.append(migration)
    return tuple(applied)


def _refuse_drift(current: MigrationStatus) -> None:
    if current.changed:
        raise MigrationDrift(f"applied migrations were edited: {', '.join(current.changed)}")
    if current.unknown:
        raise MigrationDrift(
            f"the database has migrations this checkout does not know: {', '.join(current.unknown)}"
        )


def _apply(executor: SqlExecutor, migration: Migration, migrations: Sequence[Migration]) -> bool:
    """Apply one migration; False when a concurrent runner applied it first."""
    try:
        executor.execute(bookkept_script(migration))
    except PsqlError as err:
        if err.sqlstate != "23505":  # unique_violation on schema_migrations
            raise
        again = status(executor, migrations)
        _refuse_drift(again)
        if any(a.version == migration.version for a in again.applied):
            return False
        raise
    return True


def bookkept_script(migration: Migration) -> str:
    """The migration plus its bookkeeping, as one script for one transaction."""
    if not _FILENAME.fullmatch(migration.filename) or not _HEX64.fullmatch(migration.checksum):
        raise MigrationLayoutError(f"refusing to run malformed migration {migration.filename!r}")
    return (
        "SET LOCAL search_path = public;\n"
        "SET LOCAL client_min_messages = warning;\n"
        f"SELECT pg_advisory_xact_lock({_LOCK_KEY});\n"
        "INSERT INTO public.schema_migrations (version, name, checksum) "
        f"VALUES ('{migration.version}', '{migration.name}', '{migration.checksum}');\n"
        f"{STATUS_GRANT}\n"
        f"{migration.sql}\n"
    )


def _missing_extensions(executor: SqlExecutor, names: Iterable[str]) -> list[str]:
    wanted = sorted(set(names))
    if not wanted:
        return []
    listed = ", ".join(f"'{n}'" for n in wanted)  # names match [a-z_][a-z0-9_]*
    rows = executor.query(f"SELECT name FROM pg_available_extensions WHERE name IN ({listed})")
    available = {r["name"] for r in rows}
    return [n for n in wanted if n not in available]
