"""Migration files and runner, without a database (fakes stand in for psql)."""

from __future__ import annotations

import dataclasses
import re
import sys
from enum import Enum
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO / "db") not in sys.path:
    sys.path.insert(0, str(REPO / "db"))

from backoffice.domain.lifecycle import Stage  # noqa: E402
from backoffice.domain.models import (  # noqa: E402
    DocumentType,
    EvidenceFormat,
    ExtractionMethod,
    ObligationKind,
    Quality,
    SourceKind,
    TransactionKind,
)
from backoffice.reconciliation import MatchKind  # noqa: E402
from backoffice_db import (  # noqa: E402
    MIGRATIONS_DIR,
    Migration,
    MigrationBlocked,
    MigrationDrift,
    MigrationError,
    MigrationLayoutError,
    PsqlError,
    lint_sql,
    load_migrations,
    migrate,
    status,
)
from backoffice_db.migrations import bookkept_script, checksum_of, strip_sql  # noqa: E402

ALL_SQL = "\n".join(p.read_text() for p in sorted(MIGRATIONS_DIR.glob("*.sql")))


# --------------------------------------------------------------------------- the real files


def test_real_migrations_load_in_order_and_pass_lint() -> None:
    migrations = load_migrations()
    assert [m.version for m in migrations] == [f"{i:04d}" for i in range(1, len(migrations) + 1)]
    assert migrations[0].name == "core"
    assert all(len(m.checksum) == 64 for m in migrations)


def test_only_the_embeddings_migration_needs_pgvector() -> None:
    needs = {m.filename: m.requires_extensions for m in load_migrations() if m.requires_extensions}
    assert needs == {"0005_evidence_embeddings.sql": ("vector",)}


def _domain_values(name: str) -> set[str]:
    """The values a domain allows after every migration: its CREATE DOMAIN, or the constraint a
    later migration replaced it with (ALTER DOMAIN ... ADD CONSTRAINT ... CHECK), the last one winning."""
    found = list(re.finditer(
        rf"(?:CREATE DOMAIN {name} AS text|ALTER DOMAIN {name} ADD CONSTRAINT \w+)\s+CHECK \(VALUE IN "
        rf"\((?P<body>.*?)\)\);", ALL_SQL, re.S,
    ))
    assert found, f"domain {name} not found"
    return set(re.findall(r"'([a-z_]+)'", found[-1].group("body")))


@pytest.mark.parametrize(
    ("domain", "enum"),
    [
        ("source_kind", SourceKind),
        ("evidence_format", EvidenceFormat),
        ("quality_level", Quality),
        ("extraction_method", ExtractionMethod),
        ("document_type", DocumentType),
        ("transaction_kind", TransactionKind),
        ("obligation_kind", ObligationKind),
        ("item_stage", Stage),
        ("match_kind", MatchKind),
    ],
)
def test_sql_enumerations_mirror_the_domain_model(domain: str, enum: type[Enum]) -> None:
    assert _domain_values(domain) == {member.value for member in enum}


def test_quality_mapping_keeps_green_amber_red_names() -> None:
    # GREEN/AMBER/RED are stored by value; a rename in either place must fail here.
    assert (Quality.GREEN.value, Quality.AMBER.value, Quality.RED.value) == ("verified", "likely", "conflict")


def test_every_tenant_table_is_isolated_in_the_files() -> None:
    """Static twin of the live catalog check, for machines without PostgreSQL."""
    tables = re.findall(r"CREATE TABLE (\w+) \((.*?)\n\);", ALL_SQL, re.S)
    assert tables
    for name, body in tables:
        if re.search(r"^\s*tenant_id\s", body, re.M):
            isolated = f"CALL enable_tenant_isolation('{name}')" in ALL_SQL or (
                f"ALTER TABLE {name} FORCE ROW LEVEL SECURITY" in ALL_SQL
                and re.search(rf"CREATE POLICY \w+ ON {name}\b", ALL_SQL)
            )
            assert isolated, f"{name} has tenant_id but no row level security"


def test_money_columns_are_numeric_18_2() -> None:
    money = re.findall(r"^\s*(\w*amount|fee|difference|conversion_cost)\s+([a-z]+(?:\([0-9,]+\))?)", ALL_SQL, re.M)
    assert money
    assert {kind for _name, kind in money} == {"numeric(18,2)"}


def test_vector_table_is_marked_candidate_only() -> None:
    sql = (MIGRATIONS_DIR / "0005_evidence_embeddings.sql").read_text()
    assert "Candidate retrieval only" in sql and "Never authoritative evidence" in sql


# --------------------------------------------------------------------------- layout


def _write(directory: Path, files: dict[str, str]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (directory / name).write_text(text)
    return directory


def test_gap_in_numbering_is_refused(tmp_path: Path) -> None:
    d = _write(tmp_path, {"0001_a.sql": "SELECT 1;", "0003_c.sql": "SELECT 1;"})
    with pytest.raises(MigrationLayoutError, match="without gaps"):
        load_migrations(d)


def test_bad_names_and_duplicate_versions_are_all_reported(tmp_path: Path) -> None:
    d = _write(
        tmp_path,
        {"0001_a.sql": "SELECT 1;", "0001_b.sql": "SELECT 1;", "2_Bad-Name.sql": "SELECT 1;"},
    )
    with pytest.raises(MigrationLayoutError) as err:
        load_migrations(d)
    assert "used twice" in str(err.value) and "2_Bad-Name.sql" in str(err.value)


def test_missing_directory_is_a_layout_error(tmp_path: Path) -> None:
    with pytest.raises(MigrationLayoutError):
        load_migrations(tmp_path / "nope")


def test_checksum_ignores_line_endings() -> None:
    assert checksum_of("a\r\nb\r\n") == checksum_of("a\nb\n") != checksum_of("a\nb")


def test_requires_directive_is_read_from_the_header_only(tmp_path: Path) -> None:
    d = _write(
        tmp_path,
        {
            "0001_a.sql": "-- requires-extension: vector\n-- note\nSELECT 1;\n",
            "0002_b.sql": "SELECT 1;\n-- requires-extension: postgis\n",
        },
    )
    first, second = load_migrations(d)
    assert first.requires_extensions == ("vector",)
    assert second.requires_extensions == ()


# --------------------------------------------------------------------------- lint


@pytest.mark.parametrize(
    ("sql", "problem"),
    [
        ("BEGIN; CREATE TABLE t (a int); COMMIT;", "transaction control"),
        ("CREATE TABLE t (a int);\nrollback;", "transaction control"),
        ("CREATE INDEX CONCURRENTLY i ON t (a);", "CONCURRENTLY"),
        ("CREATE TABLE t (a double precision);", "floating point"),
        ("CREATE TABLE t (a real);", "floating point"),
        ("CREATE TABLE t (a money);", "money type"),
        ("CREATE TABLE t (a timestamp);", "timestamptz"),
        ("CREATE TABLE t (a timestamp without time zone);", "timestamptz"),
        ("\\i other.sql", "meta-commands"),
    ],
)
def test_lint_catches(sql: str, problem: str) -> None:
    assert any(problem in p for p in lint_sql(sql)), lint_sql(sql)


def test_lint_ignores_comments_strings_and_function_bodies() -> None:
    sql = """
    -- BEGIN; float money timestamp
    /* COMMIT; real */
    CREATE TABLE t (a timestamptz, b timestamp with time zone, c numeric(18,2), money_total numeric);
    COMMENT ON TABLE t IS 'never a float; BEGIN';
    CREATE FUNCTION f() RETURNS trigger LANGUAGE plpgsql AS $body$
    BEGIN
        RAISE EXCEPTION 'double precision';
    END
    $body$;
    DO $$ BEGIN NULL; END $$;
    SELECT CURRENT_TIMESTAMP, to_timestamp(0);
    """
    assert lint_sql(sql) == []


def test_strip_sql_handles_doubled_quotes_and_nested_dollar_tags() -> None:
    stripped = strip_sql("SELECT 'it''s; BEGIN', $a$ $$ COMMIT $$ $a$, \"we\"\"ird\";")
    assert "BEGIN" not in stripped and "COMMIT" not in stripped and "we" not in stripped
    assert stripped.startswith("SELECT")


def test_real_migrations_have_no_transaction_control_even_with_plpgsql() -> None:
    for migration in load_migrations():
        assert lint_sql(migration.sql) == [], migration.filename


# --------------------------------------------------------------------------- runner with a fake database


class FakeDatabase:
    """Just enough of PostgreSQL for the runner: bookkeeping and extensions."""

    def __init__(self, *, extensions: set[str] | None = None) -> None:
        self.rows: dict[str, tuple[str, str]] = {}  # version -> (name, checksum)
        self.scripts: list[str] = []
        self.extensions = extensions if extensions is not None else {"vector"}
        self.has_table = False
        self.fail_on: str | None = None
        self.race_on: str | None = None  # another runner applies this version first

    def execute(self, sql: str) -> None:
        if "CREATE TABLE IF NOT EXISTS public.schema_migrations" in sql:
            self.has_table = True
            return
        match = re.search(r"VALUES \('(\d{4})', '([a-z0-9_]+)', '([0-9a-f]{64})'\)", sql)
        assert match, "migration scripts insert their bookkeeping row"
        version, name, checksum = match.groups()
        if self.race_on == version:
            self.rows[version] = (name, checksum)
            raise PsqlError(3, "ERROR:  23505: duplicate key value violates unique constraint")
        if self.fail_on == version:
            raise PsqlError(3, "ERROR:  42P07: relation already exists")
        self.scripts.append(sql)
        self.rows[version] = (name, checksum)

    def query(self, sql: str) -> list[dict[str, str | None]]:
        if "to_regclass" in sql:
            return [{"present": "t" if self.has_table else "f"}]
        if "FROM public.schema_migrations" in sql:
            return [
                {"version": v, "name": n, "checksum": c} for v, (n, c) in sorted(self.rows.items())
            ]
        if "pg_available_extensions" in sql:
            wanted = re.findall(r"'([a-z0-9_]+)'", sql)
            return [{"name": n} for n in wanted if n in self.extensions]
        raise AssertionError(f"unexpected query {sql}")


def _migrations(tmp_path: Path, count: int = 3, *, needs: dict[int, str] | None = None) -> tuple[Migration, ...]:
    files = {}
    for i in range(1, count + 1):
        header = f"-- requires-extension: {needs[i]}\n" if needs and i in needs else ""
        files[f"{i:04d}_step_{i}.sql"] = f"{header}CREATE TABLE t{i} (a int);\n"
    return load_migrations(_write(tmp_path, files))


def test_migrate_applies_pending_in_order_with_bookkeeping(tmp_path: Path) -> None:
    db = FakeDatabase()
    migrations = _migrations(tmp_path)
    applied = migrate(db, migrations)
    assert [m.version for m in applied] == ["0001", "0002", "0003"]
    first = db.scripts[0]
    assert first.index("pg_advisory_xact_lock") < first.index("INSERT INTO public.schema_migrations")
    assert first.index("INSERT INTO public.schema_migrations") < first.index("CREATE TABLE t1")
    assert migrate(db, migrations) == ()


def test_migrate_stops_at_target(tmp_path: Path) -> None:
    db = FakeDatabase()
    migrations = _migrations(tmp_path)
    assert [m.version for m in migrate(db, migrations, target="0002")] == ["0001", "0002"]
    assert status(db, migrations).pending == (migrations[2],)
    with pytest.raises(MigrationError, match="unknown target"):
        migrate(db, migrations, target="0009")


def test_edited_applied_migration_is_drift(tmp_path: Path) -> None:
    db = FakeDatabase()
    migrations = _migrations(tmp_path)
    migrate(db, migrations[:2])
    edited = (dataclasses.replace(migrations[0], checksum="0" * 64), *migrations[1:])
    current = status(db, edited)
    assert current.changed == ("0001",) and not current.ok
    with pytest.raises(MigrationDrift, match="edited"):
        migrate(db, edited)
    assert len(db.scripts) == 2  # nothing new ran


def test_database_ahead_of_checkout_is_drift(tmp_path: Path) -> None:
    db = FakeDatabase()
    migrations = _migrations(tmp_path)
    migrate(db, migrations)
    with pytest.raises(MigrationDrift, match="does not know: 0003"):
        migrate(db, migrations[:2])


def test_missing_extension_blocks_without_recording(tmp_path: Path) -> None:
    db = FakeDatabase(extensions=set())
    migrations = _migrations(tmp_path, needs={2: "vector"})
    with pytest.raises(MigrationBlocked) as err:
        migrate(db, migrations)
    assert err.value.migration.version == "0002"
    assert [m.version for m in err.value.applied] == ["0001"]
    assert "0002" not in db.rows and "0003" not in db.rows


def test_losing_a_race_to_another_runner_is_not_an_error(tmp_path: Path) -> None:
    db = FakeDatabase()
    db.has_table = True
    migrations = _migrations(tmp_path)
    db.race_on = "0002"
    applied = migrate(db, migrations)
    assert [m.version for m in applied] == ["0001", "0003"]
    assert set(db.rows) == {"0001", "0002", "0003"}


def test_other_database_errors_propagate(tmp_path: Path) -> None:
    db = FakeDatabase()
    db.fail_on = "0002"
    with pytest.raises(PsqlError) as err:
        migrate(db, _migrations(tmp_path))
    assert err.value.sqlstate == "42P07"
    assert set(db.rows) == {"0001"}


def test_bookkept_script_refuses_malformed_metadata(tmp_path: Path) -> None:
    good = _migrations(tmp_path, 1)[0]
    with pytest.raises(MigrationLayoutError):
        bookkept_script(dataclasses.replace(good, name="x'; DROP TABLE t; --"))
    with pytest.raises(MigrationLayoutError):
        bookkept_script(dataclasses.replace(good, checksum="nothex"))
