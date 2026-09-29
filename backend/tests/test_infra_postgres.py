"""The migrated schema on a real PostgreSQL 16.

Uses BACKOFFICE_TEST_DATABASE_URL (a superuser URL, e.g. the CI service
container) when set, else a throwaway local cluster (TemporaryPostgres). Skips
when neither is available. Application behaviour is exercised as the
``backoffice_app`` role (SET ROLE through PGOPTIONS), so row-level security and
grants apply exactly as they will for the api and worker.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import threading
import uuid
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO / "db") not in sys.path:
    sys.path.insert(0, str(REPO / "db"))

from backoffice_db import (  # noqa: E402
    SCOPE_SQL,
    MigrationBlocked,
    PsqlError,
    PsqlExecutor,
    check_catalog,
    load_migrations,
    migrate,
    status,
)
from backoffice_db.__main__ import run as cli  # noqa: E402
from backoffice_db.testing import PostgresUnavailable, TemporaryPostgres  # noqa: E402

GENESIS_PREFIX = "backoffice.audit.v1:"  # backoffice.audit's genesis rule, restated


# --------------------------------------------------------------------------- server fixtures


class Server:
    """Superuser access to a PostgreSQL server; one database per name."""

    def __init__(self, admin_url: str, psql: str) -> None:
        self.admin_url = admin_url
        self.psql = psql

    def url(self, dbname: str) -> str:
        parts = urlsplit(self.admin_url)
        return urlunsplit(parts._replace(path=f"/{dbname}"))

    def executor(self, dbname: str) -> PsqlExecutor:
        return PsqlExecutor.from_url(self.url(dbname), psql=self.psql, timeout=120)

    def create_database(self, prefix: str) -> str:
        name = f"{prefix}_{uuid.uuid4().hex[:10]}"
        self.executor(urlsplit(self.admin_url).path.lstrip("/") or "postgres").query(
            f"CREATE DATABASE {name}"
        )
        return name


@pytest.fixture(scope="module")
def server() -> Iterator[Server]:
    external = os.environ.get("BACKOFFICE_TEST_DATABASE_URL")
    if external:
        psql = shutil.which("psql")
        if not psql:
            pytest.skip("psql is not installed")
        yield Server(external, psql)
        return
    try:
        pg = TemporaryPostgres()
        pg.start()
    except PostgresUnavailable as err:
        pytest.skip(f"no PostgreSQL available: {err}")
    try:
        yield Server(pg.url("postgres"), str(pg.bindir / "psql"))
    finally:
        pg.stop()


@pytest.fixture(scope="module")
def db(server: Server) -> PsqlExecutor:
    """One migrated database shared by the behaviour tests (each uses its own tenants)."""
    executor = server.executor(server.create_database("bo_schema"))
    try:
        migrate(executor, load_migrations())
    except MigrationBlocked as err:  # pgvector missing: everything before it still applies
        assert err.migration.requires_extensions == ("vector",)
    return executor


@pytest.fixture(scope="module")
def has_vector(db: PsqlExecutor) -> bool:
    return bool(db.query("SELECT 1 AS x FROM pg_extension WHERE extname = 'vector'"))


def app(db: PsqlExecutor, tenant: str | None, user: str | None = None) -> PsqlExecutor:
    settings = {"role": "backoffice_app"}
    if tenant is not None:
        settings["app__tenant_id"] = tenant
    if user is not None:
        settings["app__user_id"] = user
    return db.with_settings(**settings)


def as_role(db: PsqlExecutor, role: str, tenant: str) -> PsqlExecutor:
    return db.with_settings(role=role, app__tenant_id=tenant)


def fails(executor: PsqlExecutor, sql: str, sqlstate: str) -> PsqlError:
    with pytest.raises(PsqlError) as err:
        executor.execute(sql)
    assert err.value.sqlstate == sqlstate, err.value.stderr
    return err.value


def count(executor: PsqlExecutor, sql: str) -> int:
    return int(executor.query(sql)[0]["n"] or 0)


SHA_A = hashlib.sha256(b"invoice A").hexdigest()
SHA_B = hashlib.sha256(b"invoice B").hexdigest()


@pytest.fixture()
def tenant(db: PsqlExecutor) -> dict[str, str]:
    """A fresh tenant with an owner, a company, a bank account and two originals."""
    t = f"t{uuid.uuid4().hex[:12]}"
    owner = f"usr_{uuid.uuid4().hex[:12]}"
    app(db, t, owner).execute(
        f"""
        INSERT INTO tenants (id, name, country) VALUES ('{t}', 'Padaria Lda', 'PT');
        INSERT INTO users (id, email) VALUES ('{owner}', '{owner}@example.pt');
        INSERT INTO memberships (tenant_id, user_id, role) VALUES ('{t}', '{owner}', 'owner');
        INSERT INTO legal_entities (tenant_id, id, name, country, tax_id)
            VALUES ('{t}', 'ent_1', 'Padaria Lda', 'PT', '509999990');
        INSERT INTO accounts (tenant_id, id, kind, name, entity_id)
            VALUES ('{t}', 'acc_1', 'bank', 'Main account', 'ent_1');
        INSERT INTO suppliers (tenant_id, id, name, tax_id) VALUES ('{t}', 'sup_1', 'Vodafone', '502544180');
        INSERT INTO evidence (tenant_id, id, source_kind, format, sha256, storage_key, retrieved_at)
            VALUES ('{t}', 'ev_a', 'email', 'pdf', '{SHA_A}', '{t}/sha256/{SHA_A[:2]}/{SHA_A}', now()),
                   ('{t}', 'ev_b', 'bank', 'bank_transaction', '{SHA_B}', NULL, now());
        INSERT INTO evidence_sightings (tenant_id, evidence_id, source_kind, seen_at)
            VALUES ('{t}', 'ev_a', 'email', now());
        """
    )
    return {"id": t, "owner": owner}


# --------------------------------------------------------------------------- migrations and catalog


def test_catalog_invariants_hold(db: PsqlExecutor) -> None:
    assert check_catalog(db) == ()


def test_catalog_check_catches_unsafe_schema_changes(server: Server) -> None:
    executor = server.executor(server.create_database("bo_unsafe"))
    migrate(executor, load_migrations(), target="0004")
    assert check_catalog(executor) == ()
    executor.execute(
        """
        CREATE TABLE sneaky (tenant_id text, total_amount double precision);
        GRANT UPDATE ON audit_log TO backoffice_app;
        ALTER TABLE match_factors DISABLE TRIGGER match_factors_append_only;
        CREATE FUNCTION leaky() RETURNS int LANGUAGE sql SECURITY DEFINER AS 'SELECT 1';
        """
    )
    found = {(p.table, p.rule) for p in check_catalog(executor)}
    assert {
        ("sneaky", "row level security"),
        ("sneaky", "no float or money type"),
        ("sneaky", "money has a currency"),
        ("audit_log", "append-only"),
        ("match_factors", "append-only"),
        ("leaky()", "security definer"),
    } <= found


def test_status_is_clean_and_rerun_is_a_no_op(db: PsqlExecutor, has_vector: bool) -> None:
    current = status(db, load_migrations())
    assert current.ok
    # Without pgvector the runner stops at 0005, so it and every later migration stay pending.
    expected_pending = () if has_vector else tuple(m.version for m in load_migrations() if m.version >= "0005")
    assert tuple(m.version for m in current.pending) == expected_pending
    if has_vector:
        assert migrate(db, load_migrations()) == ()


def test_cli_migrates_checks_and_reports(server: Server, capsys: pytest.CaptureFixture[str]) -> None:
    url = server.url(server.create_database("bo_cli"))
    env = {"MIGRATION_DATABASE_URL": url, "PATH": os.environ.get("PATH", "")}
    code = cli(["migrate", "--target", "0004"], env=env)
    assert code == 0
    assert cli(["check"], env=env) == 0
    assert cli(["status"], env=env) == 0
    out = capsys.readouterr().out
    assert "applied: 0001_core.sql" in out and "catalog OK" in out and "pending  0005" in out


def test_failed_migration_leaves_nothing_behind(server: Server, tmp_path: Path) -> None:
    (tmp_path / "0001_ok.sql").write_text("CREATE TABLE ok_table (a integer);\n")
    (tmp_path / "0002_broken.sql").write_text(
        "CREATE TABLE half_table (a integer);\nSELECT no_such_function();\n"
    )
    executor = server.executor(server.create_database("bo_fail"))
    with pytest.raises(PsqlError) as err:
        migrate(executor, load_migrations(tmp_path))
    assert err.value.sqlstate == "42883"
    assert [a.version for a in status(executor, load_migrations(tmp_path)).applied] == ["0001"]
    assert executor.query("SELECT to_regclass('half_table') AS t")[0]["t"] is None


def test_missing_extension_blocks_and_records_nothing(server: Server, tmp_path: Path) -> None:
    (tmp_path / "0001_needs.sql").write_text(
        "-- requires-extension: surely_not_installed_ext\nCREATE TABLE never_made (a integer);\n"
    )
    executor = server.executor(server.create_database("bo_block"))
    with pytest.raises(MigrationBlocked):
        migrate(executor, load_migrations(tmp_path))
    assert status(executor, load_migrations(tmp_path)).applied == ()


def test_concurrent_runners_apply_each_migration_once(server: Server) -> None:
    name = server.create_database("bo_race")
    migrations = load_migrations()
    results: list[object] = []

    def runner() -> None:
        try:
            results.append(migrate(server.executor(name), migrations, target="0004"))
        except Exception as exc:  # collected and asserted below
            results.append(exc)

    threads = [threading.Thread(target=runner) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not [r for r in results if isinstance(r, Exception)], results
    applied = [m.version for r in results for m in r]  # type: ignore[union-attr]
    assert sorted(applied) == ["0001", "0002", "0003", "0004"]
    rows = server.executor(name).query("SELECT count(*) AS n FROM schema_migrations")
    assert rows[0]["n"] == "4"


def test_ensure_login_creates_a_member_that_rls_applies_to(
    server: Server, db: PsqlExecutor, tenant: dict[str, str]
) -> None:
    user = f"svc_{uuid.uuid4().hex[:8]}"
    env = {
        "MIGRATION_DATABASE_URL": server.url(db.params.settings["dbname"]),
        "APP_DB_PASSWORD": "a-very-long-password-1234",
        "PATH": os.environ.get("PATH", ""),
    }
    assert cli(["ensure-login", user], env=env) == 0
    assert cli(["ensure-login", user], env={**env, "APP_DB_PASSWORD": "another-long-password-5678"}) == 0
    role = db.query(
        f"SELECT rolcanlogin, rolsuper, rolbypassrls, rolpassword IS NOT NULL AS has_password, "
        f"pg_has_role('{user}', 'backoffice_app', 'member') AS member FROM pg_authid WHERE rolname = '{user}'"
    )
    assert role == [{"rolcanlogin": "t", "rolsuper": "f", "rolbypassrls": "f", "has_password": "t", "member": "t"}]
    # Acting as that login (SET ROLE), row level security applies like for the app.
    as_login = db.with_settings(role=user, app__tenant_id=tenant["id"])
    assert count(as_login, "SELECT count(*) AS n FROM evidence") == 2
    assert count(db.with_settings(role=user), "SELECT count(*) AS n FROM evidence") == 0


# --------------------------------------------------------------------------- tenant isolation (§52)


def test_a_tenant_sees_only_its_own_rows(db: PsqlExecutor, tenant: dict[str, str]) -> None:
    other = f"t{uuid.uuid4().hex[:12]}"
    app(db, other).execute(f"INSERT INTO tenants (id, name, country) VALUES ('{other}', 'Other', 'ES')")
    assert count(app(db, tenant["id"]), "SELECT count(*) AS n FROM evidence") == 2
    assert count(app(db, other), "SELECT count(*) AS n FROM evidence") == 0
    assert count(app(db, other), "SELECT count(*) AS n FROM tenants") == 1


def test_no_tenant_scope_means_no_rows(db: PsqlExecutor, tenant: dict[str, str]) -> None:
    assert count(app(db, None), "SELECT count(*) AS n FROM evidence") == 0
    assert count(app(db, None), "SELECT count(*) AS n FROM tenants") == 0
    fails(
        app(db, None),
        f"INSERT INTO suppliers (tenant_id, id, name) VALUES ('{tenant['id']}', 'sup_x', 'X')",
        "42501",
    )


def test_writing_into_another_tenant_is_refused(db: PsqlExecutor, tenant: dict[str, str]) -> None:
    other = f"t{uuid.uuid4().hex[:12]}"
    app(db, other).execute(f"INSERT INTO tenants (id, name, country) VALUES ('{other}', 'Other', 'ES')")
    fails(
        app(db, tenant["id"]),
        f"INSERT INTO suppliers (tenant_id, id, name) VALUES ('{other}', 'sup_x', 'X')",
        "42501",
    )


def test_cross_tenant_references_are_impossible(db: PsqlExecutor, tenant: dict[str, str]) -> None:
    other = f"t{uuid.uuid4().hex[:12]}"
    app(db, other).execute(
        f"""
        INSERT INTO tenants (id, name, country) VALUES ('{other}', 'Other', 'ES');
        INSERT INTO suppliers (tenant_id, id, name) VALUES ('{other}', 'sup_other', 'Other supplier');
        """
    )
    fails(
        app(db, tenant["id"]),
        f"INSERT INTO documents (tenant_id, id, supplier_id) VALUES ('{tenant['id']}', 'doc_x', 'sup_other')",
        "23503",
    )


def test_scope_statement_is_transaction_local(db: PsqlExecutor, tenant: dict[str, str]) -> None:
    literal = SCOPE_SQL.replace("%s", f"'{tenant['id']}'", 1).replace("%s", "''", 1)
    app(db, None).execute(
        f"{literal};\nINSERT INTO suppliers (tenant_id, id, name) VALUES ('{tenant['id']}', 'sup_scoped', 'Scoped');"
    )
    assert count(app(db, tenant["id"]), "SELECT count(*) AS n FROM suppliers WHERE id = 'sup_scoped'") == 1
    assert count(app(db, None), "SELECT count(*) AS n FROM suppliers") == 0


def test_users_and_memberships_follow_the_signed_in_person(db: PsqlExecutor, tenant: dict[str, str]) -> None:
    owner = tenant["owner"]
    second = f"t{uuid.uuid4().hex[:12]}"
    app(db, second, owner).execute(
        f"""
        INSERT INTO tenants (id, name, country) VALUES ('{second}', 'Second Lda', 'PT');
        INSERT INTO memberships (tenant_id, user_id, role) VALUES ('{second}', '{owner}', 'owner');
        """
    )
    # Signed in, no tenant chosen yet: sees own memberships in both businesses.
    assert count(app(db, None, owner), "SELECT count(*) AS n FROM memberships") == 2
    # A stranger sees neither the person nor their memberships.
    stranger = app(db, f"t{uuid.uuid4().hex[:12]}", "usr_stranger")
    assert count(stranger, f"SELECT count(*) AS n FROM users WHERE id = '{owner}'") == 0
    assert count(stranger, "SELECT count(*) AS n FROM memberships") == 0


def test_scheduler_lists_tenants_but_reads_nothing_else(db: PsqlExecutor, tenant: dict[str, str]) -> None:
    scheduler = db.with_settings(role="backoffice_scheduler")
    ids = {r["id"] for r in scheduler.query("SELECT id FROM tenants")}
    assert tenant["id"] in ids
    with pytest.raises(PsqlError) as err:
        scheduler.query("SELECT count(*) AS n FROM evidence")
    assert err.value.sqlstate == "42501"


def test_tenant_ids_with_trailing_newline_are_refused_by_the_domain(db: PsqlExecutor) -> None:
    fails(db, "INSERT INTO tenants (id, name, country) VALUES (E'bad\\n', 'X', 'PT')", "23514")


# --------------------------------------------------------------------------- evidence is immutable (§7, §55)


def test_evidence_cannot_be_updated_or_deleted_even_by_the_owner_role(db: PsqlExecutor, tenant: dict[str, str]) -> None:
    t = tenant["id"]
    scoped = db.with_settings(app__tenant_id=t)  # superuser: privileges allow it, the trigger does not
    fails(scoped, f"UPDATE evidence SET filename = 'x.pdf' WHERE tenant_id = '{t}'", "23001")
    fails(scoped, f"DELETE FROM evidence WHERE tenant_id = '{t}'", "23001")
    fails(scoped, "TRUNCATE evidence CASCADE", "23001")
    fails(scoped, f"DELETE FROM evidence_sightings WHERE tenant_id = '{t}'", "23001")
    # And the application role has no such privileges at all.
    fails(app(db, t), f"UPDATE evidence SET filename = 'x.pdf' WHERE tenant_id = '{t}'", "42501")


def test_same_bytes_twice_is_one_evidence_per_tenant(db: PsqlExecutor, tenant: dict[str, str]) -> None:
    t = tenant["id"]
    fails(
        app(db, t),
        f"INSERT INTO evidence (tenant_id, id, source_kind, format, sha256, retrieved_at) "
        f"VALUES ('{t}', 'ev_dup', 'upload', 'pdf', '{SHA_A}', now())",
        "23505",
    )


def test_storage_key_must_end_with_the_hash(db: PsqlExecutor, tenant: dict[str, str]) -> None:
    t = tenant["id"]
    sha = hashlib.sha256(b"other").hexdigest()
    fails(
        app(db, t),
        f"INSERT INTO evidence (tenant_id, id, source_kind, format, sha256, storage_key, retrieved_at) "
        f"VALUES ('{t}', 'ev_k', 'upload', 'pdf', '{sha}', '{t}/sha256/00/{SHA_A}', now())",
        "23514",
    )


def _observe(t: str, field: str, value: str, source: str, method: str, evidence: str | None) -> str:
    ev = f"'{evidence}'" if evidence else "NULL"
    return (
        f"INSERT INTO field_observations (tenant_id, document_id, field_name, value, source, evidence_id, method, confidence) "
        f"VALUES ('{t}', 'doc_1', '{field}', '\"{value}\"', '{source}', {ev}, '{method}', 0.99);"
    )


def _verified_total(db: PsqlExecutor, t: str) -> None:
    """doc_1's gross amount verified by QR (ev_a) and embedded text (ev_a)."""
    app(db, t).execute(
        f"""
        INSERT INTO documents (tenant_id, id, doc_type, supplier_id, invoice_number, gross_amount, quality)
            VALUES ('{t}', 'doc_1', 'invoice', 'sup_1', 'FT 2026/183', 483.60, 'verified');
        INSERT INTO document_evidence (tenant_id, document_id, evidence_id, position) VALUES ('{t}', 'doc_1', 'ev_a', 1);
        {_observe(t, 'gross_amount', '483.60', 'ev_a', 'qr', 'ev_a')}
        {_observe(t, 'gross_amount', '483.60', 'ev_a', 'embedded_text', 'ev_a')}
        INSERT INTO verified_fields (tenant_id, document_id, name, value, quality)
            VALUES ('{t}', 'doc_1', 'gross_amount', '"483.60"', 'verified');
        INSERT INTO verified_field_observations (tenant_id, document_id, field_name, observation_id)
            SELECT '{t}', 'doc_1', 'gross_amount', id FROM field_observations WHERE tenant_id = '{t}';
        """
    )


def _request_deletion(db: PsqlExecutor, t: str, request: str = "del_1") -> None:
    app(db, t).execute(
        f"INSERT INTO evidence_deletion_requests (tenant_id, id, evidence_id, evidence_sha256, storage_key, reason, requested_by) "
        f"SELECT tenant_id, '{request}', id, sha256, storage_key, 'Owner asked to erase it', 'owner:{t}' "
        f"FROM evidence WHERE tenant_id = '{t}' AND id = 'ev_a'"
    )


def test_evidence_deletion_needs_an_owner_approval_and_leaves_a_record(db: PsqlExecutor, tenant: dict[str, str]) -> None:
    t, owner = tenant["id"], tenant["owner"]
    _verified_total(db, t)
    _request_deletion(db, t)
    admin = as_role(db, "backoffice_evidence_admin", t)

    # Executing before approval is refused.
    with pytest.raises(PsqlError) as err:
        admin.query("SELECT execute_evidence_deletion('del_1', 'worker') AS key")
    assert err.value.sqlstate == "23001"
    # Approval needs a reference, and an owner of this business.
    fails(admin, "UPDATE evidence_deletion_requests SET status = 'approved' WHERE id = 'del_1'", "23514")
    stranger = f"usr_{uuid.uuid4().hex[:8]}"
    app(db, t, stranger).execute(
        f"INSERT INTO users (id, email) VALUES ('{stranger}', '{stranger}@example.pt');"
        f"INSERT INTO memberships (tenant_id, user_id, role) VALUES ('{t}', '{stranger}', 'accountant')"
    )
    fails(
        admin,
        f"UPDATE evidence_deletion_requests SET status = 'approved', approved_by = '{stranger}', "
        f"approved_at = now(), approval_reference = 'appr_1' WHERE id = 'del_1'",
        "42501",
    )
    admin.execute(
        f"UPDATE evidence_deletion_requests SET status = 'approved', approved_by = '{owner}', "
        f"approved_at = now(), approval_reference = 'appr_1' WHERE id = 'del_1'"
    )

    key = admin.query("SELECT execute_evidence_deletion('del_1', 'worker:deletion') AS key")[0]["key"]
    assert key == f"{t}/sha256/{SHA_A[:2]}/{SHA_A}"

    reader = app(db, t)
    assert count(reader, "SELECT count(*) AS n FROM evidence WHERE id = 'ev_a'") == 0
    assert count(reader, "SELECT count(*) AS n FROM evidence_sightings") == 0
    assert count(reader, "SELECT count(*) AS n FROM field_observations") == 0
    assert count(reader, "SELECT count(*) AS n FROM evidence WHERE id = 'ev_b'") == 1
    field = reader.query("SELECT quality, reasons FROM verified_fields WHERE name = 'gross_amount'")[0]
    assert field["quality"] == "likely"  # lost its support: never stays GREEN (§57)
    assert "The original document was deleted." in str(field["reasons"])
    assert reader.query("SELECT quality FROM documents WHERE id = 'doc_1'")[0]["quality"] == "likely"
    request = reader.query("SELECT status, executed_by FROM evidence_deletion_requests WHERE id = 'del_1'")[0]
    assert request == {"status": "executed", "executed_by": "worker:deletion"}

    # The record is permanent; the object purge is noted exactly once.
    fails(db.with_settings(app__tenant_id=t), "DELETE FROM evidence_deletion_requests", "23001")
    admin.execute("UPDATE evidence_deletion_requests SET object_purged_at = now() WHERE id = 'del_1'")
    fails(admin, "UPDATE evidence_deletion_requests SET object_purged_at = now() WHERE id = 'del_1'", "23001")
    with pytest.raises(PsqlError):
        admin.query("SELECT execute_evidence_deletion('del_1', 'worker') AS key")


def test_the_app_cannot_run_deletions_or_approve_them(db: PsqlExecutor, tenant: dict[str, str]) -> None:
    t, owner = tenant["id"], tenant["owner"]
    _request_deletion(db, t, "del_2")
    fails(
        app(db, t),
        f"UPDATE evidence_deletion_requests SET status = 'approved', approved_by = '{owner}', "
        f"approved_at = now(), approval_reference = 'x' WHERE id = 'del_2'",
        "42501",
    )
    with pytest.raises(PsqlError) as err:
        app(db, t).query("SELECT execute_evidence_deletion('del_2', 'app') AS key")
    assert err.value.sqlstate == "42501"


def test_a_request_must_name_the_real_evidence(db: PsqlExecutor, tenant: dict[str, str]) -> None:
    t = tenant["id"]
    fails(
        app(db, t),
        f"INSERT INTO evidence_deletion_requests (tenant_id, id, evidence_id, evidence_sha256, reason, requested_by) "
        f"VALUES ('{t}', 'del_x', 'ev_a', '{SHA_B}', 'wrong hash', 'owner')",
        "23503",
    )


# --------------------------------------------------------------------------- verification floor (§18, §57)


def test_green_needs_two_distinct_observations(db: PsqlExecutor, tenant: dict[str, str]) -> None:
    t = tenant["id"]
    base = f"""
        INSERT INTO documents (tenant_id, id, doc_type, gross_amount) VALUES ('{t}', 'doc_1', 'invoice', 92.40);
        {_observe(t, 'gross_amount', '92.40', 'ev_a@pp-ocrv6', 'ocr', 'ev_a')}
    """
    link = f"""
        INSERT INTO verified_field_observations (tenant_id, document_id, field_name, observation_id)
            SELECT '{t}', 'doc_1', 'gross_amount', id FROM field_observations WHERE tenant_id = '{t}';
    """
    one_green = (
        f"{base} INSERT INTO verified_fields (tenant_id, document_id, name, value, quality) "
        f"VALUES ('{t}', 'doc_1', 'gross_amount', '\"92.40\"', 'verified'); {link}"
    )
    fails(app(db, t), one_green, "23514")  # checked at commit
    app(db, t).execute(one_green.replace("'verified'); ", "'likely'); "))  # AMBER on one source is fine
    # A second, different source makes GREEN possible.
    app(db, t).execute(
        f"""
        {_observe(t, 'gross_amount', '92.40', 'ev_a', 'qr', 'ev_a')}
        INSERT INTO verified_field_observations (tenant_id, document_id, field_name, observation_id)
            SELECT tenant_id, document_id, field_name, id FROM field_observations
            WHERE tenant_id = '{t}' AND method = 'qr';
        UPDATE verified_fields SET quality = 'verified' WHERE tenant_id = '{t}';
        """
    )
    # Unlinking the support later is refused while the field stays GREEN.
    fails(
        app(db, t),
        f"DELETE FROM verified_field_observations WHERE tenant_id = '{t}' AND observation_id = "
        f"(SELECT min(observation_id) FROM verified_field_observations WHERE tenant_id = '{t}')",
        "23514",
    )


def test_observations_are_facts(db: PsqlExecutor, tenant: dict[str, str]) -> None:
    t = tenant["id"]
    app(db, t).execute(
        f"INSERT INTO documents (tenant_id, id) VALUES ('{t}', 'doc_1'); "
        + _observe(t, "invoice_number", "FT 1", "ev_a", "embedded_text", "ev_a")
    )
    fails(db.with_settings(app__tenant_id=t), f"UPDATE field_observations SET confidence = 1 WHERE tenant_id = '{t}'", "23001")
    fails(app(db, t), f"UPDATE field_observations SET confidence = 1 WHERE tenant_id = '{t}'", "42501")


def test_numeric_18_2_rounds_silently_which_is_why_db_amount_refuses(db: PsqlExecutor, tenant: dict[str, str]) -> None:
    t = tenant["id"]
    app(db, t).execute(f"INSERT INTO documents (tenant_id, id, gross_amount) VALUES ('{t}', 'doc_r', 10.005)")
    assert app(db, t).query("SELECT gross_amount FROM documents WHERE id = 'doc_r'")[0]["gross_amount"] == "10.01"


# --------------------------------------------------------------------------- the golden rule (§3)


def _item(t: str, stage: str = "discovered") -> str:
    return (
        f"INSERT INTO tracked_items (tenant_id, id, subject_type, subject_id, stage, quality) "
        f"VALUES ('{t}', 'item_1', 'document', 'doc_1', '{stage}', 'likely');"
    )


def _transition(t: str, seq: int, frm: str | None, to: str, quality: str, evidence: str = "ARRAY['ev_a']") -> str:
    f = f"'{frm}'" if frm else "NULL"
    return (
        f"INSERT INTO tracked_item_transitions (tenant_id, item_id, seq, from_stage, to_stage, actor, evidence_ids, quality) "
        f"VALUES ('{t}', 'item_1', {seq}, {f}, '{to}', 'agent:verification', {evidence}, '{quality}');"
    )


def test_stage_changes_need_a_transition_with_evidence(db: PsqlExecutor, tenant: dict[str, str]) -> None:
    t = tenant["id"]
    a = app(db, t)
    a.execute(_item(t))
    fails(a, f"UPDATE tracked_items SET stage = 'acquired' WHERE tenant_id = '{t}'", "23514")
    fails(a, _transition(t, 1, "discovered", "acquired", "likely", "ARRAY[]::text[]")
          + f"UPDATE tracked_items SET stage = 'acquired' WHERE tenant_id = '{t}';", "23514")
    fails(a, _transition(t, 1, "discovered", "acquired", "likely", "ARRAY['  ']")
          + f"UPDATE tracked_items SET stage = 'acquired' WHERE tenant_id = '{t}';", "23514")
    a.execute(_transition(t, 1, "discovered", "acquired", "likely")
              + f"UPDATE tracked_items SET stage = 'acquired' WHERE tenant_id = '{t}';")
    assert a.query("SELECT stage FROM tracked_items")[0]["stage"] == "acquired"


def test_an_item_cannot_be_inserted_already_closed(db: PsqlExecutor, tenant: dict[str, str]) -> None:
    t = tenant["id"]
    fails(
        app(db, t),
        f"INSERT INTO tracked_items (tenant_id, id, subject_type, subject_id, stage, quality) "
        f"VALUES ('{t}', 'item_1', 'document', 'doc_1', 'closed', 'verified')",
        "23514",
    )


def test_closing_requires_green(db: PsqlExecutor, tenant: dict[str, str]) -> None:
    t = tenant["id"]
    a = app(db, t)
    a.execute(_item(t) + _transition(t, 1, "discovered", "acquired", "likely")
              + f"UPDATE tracked_items SET stage = 'acquired' WHERE tenant_id = '{t}';")
    # AMBER is never enough to close (§57), neither on the item nor on the transition.
    fails(a, f"UPDATE tracked_items SET stage = 'closed' WHERE tenant_id = '{t}'", "23514")
    fails(a, _transition(t, 2, "acquired", "closed", "likely"), "23514")
    legacy_null = _transition(t, 2, "acquired", "closed", "likely").replace("'likely');", "NULL);")
    fails(a, legacy_null, "23514")
    # A conflict is always RED.
    fails(a, _transition(t, 2, "acquired", "conflict", "likely"), "23514")
    a.execute(_transition(t, 2, "acquired", "conflict", "conflict")
              + f"UPDATE tracked_items SET stage = 'conflict', quality = 'conflict' WHERE tenant_id = '{t}';")


def test_history_is_continuous_and_append_only(db: PsqlExecutor, tenant: dict[str, str]) -> None:
    t = tenant["id"]
    a = app(db, t)
    a.execute(_item(t) + _transition(t, 1, "discovered", "acquired", "likely")
              + f"UPDATE tracked_items SET stage = 'acquired' WHERE tenant_id = '{t}';")
    fails(a, _transition(t, 3, "acquired", "understood", "likely"), "23514")  # gap
    fails(a, _transition(t, 2, "discovered", "understood", "likely"), "23514")  # wrong origin
    fails(db.with_settings(app__tenant_id=t), f"UPDATE tracked_item_transitions SET actor = 'x' WHERE tenant_id = '{t}'", "23001")
    fails(db.with_settings(app__tenant_id=t), f"DELETE FROM tracked_item_transitions WHERE tenant_id = '{t}'", "23001")


# --------------------------------------------------------------------------- audit chain (§52, §55)


def _record(t: str, seq: int, prev: str, action: str, at: str = "2026-09-01T10:00:00+00:00") -> tuple[str, str]:
    body = json.dumps(
        {"actor": "agent:closure", "action": action, "at": at, "subject_id": "item_1", "evidence_ids": ["ev_a"]},
        sort_keys=True, separators=(",", ":"),
    )
    digest = hashlib.sha256(f"{prev}\n{body}".encode()).hexdigest()
    sql = (
        f"INSERT INTO audit_log (tenant_id, seq, at, body, prev_hash, hash) "
        f"VALUES ('{t}', {seq}, '{at}', '{body}', '{prev}', '{digest}');"
    )
    return sql, digest


def test_audit_chain_is_linked_and_append_only(db: PsqlExecutor, tenant: dict[str, str]) -> None:
    t = tenant["id"]
    a = app(db, t)
    genesis = hashlib.sha256(f"{GENESIS_PREFIX}{t}".encode()).hexdigest()
    wrong_first, _ = _record(t, 1, "0" * 64, "extract")
    fails(a, wrong_first, "23000")
    first, h1 = _record(t, 1, genesis, "extract")
    a.execute(first)
    fails(a, _record(t, 3, h1, "match")[0], "23000")  # seq gap
    fails(a, _record(t, 2, genesis, "match")[0], "23000")  # fork from genesis
    fails(a, _record(t, 2, h1, "match", at="2026-08-01T00:00:00+00:00")[0], "23000")  # back in time
    second, _ = _record(t, 2, h1, "match")
    a.execute(second)
    row = a.query("SELECT actor, action, subject_id FROM audit_log WHERE seq = 2")[0]
    assert row == {"actor": "agent:closure", "action": "match", "subject_id": "item_1"}
    fails(db.with_settings(app__tenant_id=t), f"UPDATE audit_log SET body = body WHERE tenant_id = '{t}'", "23001")
    fails(db.with_settings(app__tenant_id=t), f"DELETE FROM audit_log WHERE tenant_id = '{t}'", "23001")
    fails(db.with_settings(app__tenant_id=t), "TRUNCATE audit_log", "23001")
    assert db.query(f"SELECT audit_genesis_hash('{t}') AS h")[0]["h"] == genesis


# --------------------------------------------------------------------------- rules, questions, suppliers


def test_accountant_rules_for_all_clients_follow_active_membership(db: PsqlExecutor, tenant: dict[str, str]) -> None:
    t = tenant["id"]
    accountant = f"usr_{uuid.uuid4().hex[:8]}"
    app(db, t, accountant).execute(
        f"INSERT INTO users (id, email) VALUES ('{accountant}', '{accountant}@contabil.pt');"
        f"INSERT INTO memberships (tenant_id, user_id, role) VALUES ('{t}', '{accountant}', 'accountant')"
    )
    app(db, None, accountant).execute(
        f"INSERT INTO rules (id, tenant_id, author, author_id, scope, match, outcome, label) "
        f"VALUES ('rule_{accountant}', NULL, 'accountant', '{accountant}', 'all_clients_of_accountant', "
        f"'{{\"counterparty_key\": \"adobe\"}}', '{{\"category\": \"software\"}}', 'Adobe is Software')"
    )
    visible = f"SELECT count(*) AS n FROM rules WHERE id = 'rule_{accountant}'"
    assert count(app(db, t), visible) == 1
    assert count(app(db, f"t{uuid.uuid4().hex[:12]}"), visible) == 0
    db.with_settings(app__tenant_id=t).execute(
        f"UPDATE memberships SET revoked_at = now() WHERE tenant_id = '{t}' AND user_id = '{accountant}'"
    )
    assert count(app(db, t), visible) == 0  # authorisation withdrawn -> rule stops applying


def test_rule_scope_matches_the_python_model(db: PsqlExecutor, tenant: dict[str, str]) -> None:
    t = tenant["id"]
    fails(
        app(db, t),
        f"INSERT INTO rules (id, tenant_id, author, author_id, scope, match, outcome) "
        f"VALUES ('rule_bad', '{t}', 'owner', 'usr_1', 'client', '{{}}', '{{}}')",
        "23514",
    )


def test_answers_must_pick_an_offered_option_and_never_change(db: PsqlExecutor, tenant: dict[str, str]) -> None:
    t, owner = tenant["id"], tenant["owner"]
    a = app(db, t)
    a.execute(
        f"INSERT INTO needs_you_questions (tenant_id, id, kind, prompt, options, facts, subject_type, subject_id) "
        f"VALUES ('{t}', 'q_1', 'what_is_this', 'This €92.40 Vodafone expense appears every month. Is it:', "
        f"'[{{\"id\": \"company\", \"label\": \"Company telecom\"}}, {{\"id\": \"personal\", \"label\": \"Personal\"}}]', "
        f"'{{}}', 'series', 'vodafone')"
    )
    fails(a, f"INSERT INTO needs_you_answers (tenant_id, question_id, option_id, answered_by) VALUES ('{t}', 'q_1', 'nope', '{owner}')", "23514")
    a.execute(f"INSERT INTO needs_you_answers (tenant_id, question_id, option_id, answered_by) VALUES ('{t}', 'q_1', 'company', '{owner}')")
    fails(db.with_settings(app__tenant_id=t), f"UPDATE needs_you_answers SET option_id = 'personal' WHERE tenant_id = '{t}'", "23001")
    fails(
        a,
        f"INSERT INTO needs_you_questions (tenant_id, id, kind, prompt, options, facts, subject_type, subject_id) "
        f"VALUES ('{t}', 'q_2', 'what_is_this', 'Is it?', '[{{\"label\": \"no id\"}}]', '{{}}', 'series', 'x')",
        "23514",
    )


def test_known_ibans_are_revoked_never_rewritten(db: PsqlExecutor, tenant: dict[str, str]) -> None:
    t = tenant["id"]
    a = app(db, t)
    a.execute(
        f"INSERT INTO supplier_known_ibans (tenant_id, supplier_id, iban, added_by, approval_reference) "
        f"VALUES ('{t}', 'sup_1', 'PT50000201231234567890154', 'owner', 'appr_9')"
    )
    fails(a, f"INSERT INTO supplier_known_ibans (tenant_id, supplier_id, iban, added_by) VALUES ('{t}', 'sup_1', 'pt50 0002', 'x')", "23514")
    fails(a, f"UPDATE supplier_known_ibans SET iban = 'PT50000201231234567890999' WHERE tenant_id = '{t}'", "42501")
    scoped = db.with_settings(app__tenant_id=t)
    fails(scoped, f"UPDATE supplier_known_ibans SET iban = 'PT50000201231234567890999' WHERE tenant_id = '{t}'", "23001")
    fails(scoped, f"DELETE FROM supplier_known_ibans WHERE tenant_id = '{t}'", "23001")
    a.execute(f"UPDATE supplier_known_ibans SET revoked_at = now(), revoked_by = 'owner' WHERE tenant_id = '{t}'")
    fails(a, f"UPDATE supplier_known_ibans SET revoked_at = now(), revoked_by = 'again' WHERE tenant_id = '{t}'", "23001")


def test_connector_state_keeps_gaps_and_coverage_consistent(db: PsqlExecutor, tenant: dict[str, str]) -> None:
    t = tenant["id"]
    a = app(db, t)
    a.execute(
        f"INSERT INTO connectors (tenant_id, id, kind, source_kind, account, coverage_start, coverage_end, known_gaps) "
        f"VALUES ('{t}', 'conn_1', 'gmail', 'email', 'ana@padaria.pt', '2026-06-01Z', '2026-09-27Z', "
        f"'{{[2026-09-10 14:42Z,2026-09-11 08:00Z)}}')"
    )
    fails(
        a,
        f"INSERT INTO connectors (tenant_id, id, kind, source_kind, account, coverage_start) "
        f"VALUES ('{t}', 'conn_2', 'imap', 'email', 'x@y.pt', '2026-06-01Z')",
        "23514",
    )
    gaps = a.query("SELECT known_gaps @> '2026-09-10 20:00Z'::timestamptz AS in_gap FROM connectors")[0]
    assert gaps["in_gap"] == "t"


# --------------------------------------------------------------------------- embeddings (§44)


def test_embeddings_are_tenant_scoped_and_leave_with_their_evidence(
    db: PsqlExecutor, tenant: dict[str, str], has_vector: bool
) -> None:
    if not has_vector:
        pytest.skip("pgvector is not installed on this server")
    t = tenant["id"]
    vec = "[" + ",".join(["0.001"] * 1024) + "]"
    app(db, t).execute(
        f"INSERT INTO evidence_embeddings (tenant_id, evidence_id, model, content_sha256, embedding) "
        f"VALUES ('{t}', 'ev_b', 'multilingual-e5-large@1', '{SHA_B}', '{vec}')"
    )
    near = f"SELECT evidence_id FROM evidence_embeddings ORDER BY embedding <=> '{vec}' LIMIT 1"
    assert app(db, t).query(near) == [{"evidence_id": "ev_b"}]
    assert app(db, f"t{uuid.uuid4().hex[:12]}").query(near) == []
    fails(
        app(db, t),
        f"INSERT INTO evidence_embeddings (tenant_id, evidence_id, model, content_sha256, embedding) "
        f"VALUES ('{t}', 'ev_b', 'm', '{SHA_B}', '[1,2,3]')",
        "22000",
    )
