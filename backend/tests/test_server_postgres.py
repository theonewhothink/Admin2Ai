"""The production store on a real PostgreSQL 16: migration 0007, row-level security on the sign-in
and event tables, the append-only hash-chained event log, erasure, and the whole API rebuilt from it.

Uses BACKOFFICE_TEST_DATABASE_URL (a superuser URL, e.g. CI's service container) when set, else a
throwaway local cluster (TemporaryPostgres); skips when neither is available. The API connects as a
real login that is only a member of backoffice_app, so row-level security applies exactly as in
production. Without pgvector (0005) the migrations that do not need it still apply, in order.
"""

from __future__ import annotations

import os
import shutil
import sys
import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO / "db") not in sys.path:
    sys.path.insert(0, str(REPO / "db"))

pytest.importorskip("psycopg")

from _server_support import NIF_B, PASSWORD, bearer, build_business, harness, read_paths, signup  # noqa: E402
from backoffice_db import MigrationBlocked, PsqlError, PsqlExecutor, check_catalog, load_migrations, migrate  # noqa: E402
from backoffice_db.migrations import bookkept_script  # noqa: E402
from backoffice_db.testing import PostgresUnavailable, TemporaryPostgres  # noqa: E402

from backoffice.server.events import Event, state_digest  # noqa: E402
from backoffice.server.postgres import PostgresCredentialStore, PostgresStore  # noqa: E402
from backoffice.server.runtime import TenantManager  # noqa: E402
from backoffice.server.store import SeqConflict, Session, StoredEvent  # noqa: E402

APP_PASSWORD = "app-login-password-for-tests"


class Server:
    def __init__(self, admin_url: str, psql: str) -> None:
        self.admin_url = admin_url
        self.psql = psql

    def url(self, dbname: str, user: str | None = None, password: str | None = None) -> str:
        parts = urlsplit(self.admin_url)
        netloc = parts.netloc
        if user is not None:
            host = netloc.rsplit("@", 1)[-1]
            netloc = f"{user}:{quote(password or '')}@{host}" if password else f"{user}@{host}"
        return urlunsplit(parts._replace(path=f"/{dbname}", netloc=netloc))

    def executor(self, dbname: str) -> PsqlExecutor:
        return PsqlExecutor.from_url(self.url(dbname), psql=self.psql, timeout=120)


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


def migrate_all(executor: PsqlExecutor) -> None:
    migrations = load_migrations()
    try:
        migrate(executor, migrations)
    except MigrationBlocked as err:  # pgvector missing locally: everything else still applies, in order
        for m in migrations:
            if m.version > err.migration.version and not m.requires_extensions:
                executor.execute(bookkept_script(m))


@pytest.fixture(scope="module")
def database(server: Server) -> dict[str, object]:
    name = f"bo_server_{uuid.uuid4().hex[:10]}"
    admin = server.executor(urlsplit(server.admin_url).path.lstrip("/") or "postgres")
    admin.query(f"CREATE DATABASE {name}")
    db = server.executor(name)
    migrate_all(db)
    login = f"bo_api_{uuid.uuid4().hex[:8]}"
    db.execute(f"CREATE ROLE {login} LOGIN PASSWORD '{APP_PASSWORD}' NOSUPERUSER NOBYPASSRLS IN ROLE backoffice_app")
    db.execute(f"GRANT backoffice_scheduler TO {login} WITH INHERIT FALSE, SET TRUE")  # as ensure-login does
    plain = f"bo_plain_{uuid.uuid4().hex[:8]}"  # a login that may not list tenants
    db.execute(f"CREATE ROLE {plain} LOGIN PASSWORD '{APP_PASSWORD}' NOSUPERUSER NOBYPASSRLS IN ROLE backoffice_app")
    return {"db": db, "url": server.url(name, login, APP_PASSWORD), "name": name,
            "plain_url": server.url(name, plain, APP_PASSWORD)}


@pytest.fixture(scope="module")
def store(database: dict[str, object]) -> Iterator[PostgresStore]:
    s = PostgresStore(str(database["url"]), pool_size=4)
    yield s
    s.close()


def _count(db: PsqlExecutor, sql: str) -> int:
    return int(db.query(sql)[0]["n"] or 0)


def _as_app(db: PsqlExecutor, **settings: str) -> PsqlExecutor:
    return db.with_settings(role="backoffice_app", **settings)


# --------------------------------------------------------------------------- schema


def test_catalog_invariants_hold_with_0007(database: dict[str, object]) -> None:
    db = database["db"]
    assert check_catalog(db) == ()  # type: ignore[arg-type]
    tables = {r["t"] for r in db.query(  # type: ignore[union-attr]
        "SELECT tablename AS t FROM pg_tables WHERE schemaname = 'public'")}
    assert {"user_credentials", "sessions", "login_attempts", "devices", "tenant_events", "tenant_erasures",
            "accountant_api_keys", "oauth_nonces"} <= tables


def test_ready_when_migrated(store: PostgresStore) -> None:
    assert store.schema_status() == (True, "ok")
    store.ping()


# --------------------------------------------------------------------------- isolation (§52)


def _account(tmp_path: Path, store: PostgresStore, email: str, **kwargs: object) -> tuple[object, dict]:
    h = harness(tmp_path, store=store)
    return h, signup(h.client, email, **kwargs)  # type: ignore[arg-type]


def test_row_level_security_on_the_new_tables(tmp_path: Path, database: dict, store: PostgresStore) -> None:
    db: PsqlExecutor = database["db"]
    h, a = _account(tmp_path, store, f"a-{uuid.uuid4().hex[:6]}@example.pt")
    _, b = _account(tmp_path / "b", store, f"b-{uuid.uuid4().hex[:6]}@example.pt", tax_id=NIF_B)
    ta, tb = a["tenant"]["id"], b["tenant"]["id"]
    ua = a["user"]["id"]
    # tenant scope: each business sees only its own events
    assert _count(_as_app(db, app__tenant_id=ta), "SELECT count(*) AS n FROM tenant_events") == 2
    assert _count(_as_app(db, app__tenant_id=ta), f"SELECT count(*) AS n FROM tenant_events WHERE tenant_id = '{tb}'") == 0
    assert _count(_as_app(db), "SELECT count(*) AS n FROM tenant_events") == 0  # no scope, no rows
    assert _count(_as_app(db), "SELECT count(*) AS n FROM sessions") == 0
    assert _count(_as_app(db), "SELECT count(*) AS n FROM users") == 0
    assert _count(_as_app(db), "SELECT count(*) AS n FROM user_credentials") == 0
    assert _count(_as_app(db), "SELECT count(*) AS n FROM login_attempts") == 0
    # a password hash is readable by its own user only
    assert _count(_as_app(db, app__user_id=ua), "SELECT count(*) AS n FROM user_credentials") == 1
    assert _count(_as_app(db, app__user_id=b["user"]["id"]),
                  f"SELECT count(*) AS n FROM user_credentials WHERE user_id = '{ua}'") == 0
    # a session is found by the hash of the token presented, not by guessing
    from backoffice.server.auth import hash_token

    token_hash = hash_token(a["token"])
    assert _count(_as_app(db, app__session_hash=token_hash), "SELECT count(*) AS n FROM sessions") == 1
    assert _count(_as_app(db, app__session_hash="0" * 64), "SELECT count(*) AS n FROM sessions") == 0
    # writing another business's event is refused, even a well-formed one
    from backoffice.server.events import genesis_hash

    forged = Event.make(seq=1, at=datetime.now(timezone.utc), kind="tick", actor="system", data={}, pre=None,
                        prev_hash=genesis_hash(tb))
    with pytest.raises(PsqlError) as err:
        _as_app(db, app__tenant_id=ta).execute(
            f"INSERT INTO tenant_events (tenant_id, seq, at, kind, actor, body, prev_hash, hash) VALUES "
            f"('{tb}', 1, now(), 'tick', 'system', '{forged.body}', '{forged.prev_hash}', '{forged.hash}')")
    assert err.value.sqlstate == "42501"


def test_the_event_log_is_append_only_and_chained(tmp_path: Path, database: dict, store: PostgresStore) -> None:
    db: PsqlExecutor = database["db"]
    _, a = _account(tmp_path, store, f"c-{uuid.uuid4().hex[:6]}@example.pt")
    t = a["tenant"]["id"]
    app = _as_app(db, app__tenant_id=t)
    for sql, state in ((f"UPDATE tenant_events SET body = '{{}}' WHERE tenant_id = '{t}'", "42501"),
                       (f"DELETE FROM tenant_events WHERE tenant_id = '{t}'", "42501")):
        with pytest.raises(PsqlError) as err:
            app.execute(sql)
        assert err.value.sqlstate == state  # the app role holds no such privilege
    for sql in (f"UPDATE tenant_events SET body = '{{}}' WHERE tenant_id = '{t}'",
                f"DELETE FROM tenant_events WHERE tenant_id = '{t}'",
                f"DELETE FROM tenants WHERE id = '{t}'"):  # not even through the cascade
        with pytest.raises(PsqlError) as err:
            db.with_settings(app__tenant_id=t).execute(sql)  # the schema owner
        assert err.value.sqlstate in ("23001", "23503"), err.value.stderr
    with pytest.raises(PsqlError):
        db.execute("TRUNCATE tenant_events")
    rows = store.events(t)
    head = Event.parse(rows[-1])
    # the database recomputes the chain: a wrong seq, a broken link or a forged hash are refused
    good = Event.make(seq=3, at=head.at + timedelta(seconds=1), kind="tick", actor="system", data={}, pre=None,
                      prev_hash=head.hash)
    for bad in (StoredEvent(4, good.at, "tick", "system", good.body, good.prev_hash, good.hash),
                StoredEvent(3, good.at, "tick", "system", good.body, "f" * 64, good.hash),
                StoredEvent(3, good.at, "tick", "system", good.body + " ", good.prev_hash, good.hash)):
        with pytest.raises((SeqConflict, Exception)):
            store.append_events(t, [bad])
    early = Event.make(seq=3, at=head.at - timedelta(days=1), kind="tick", actor="system", data={}, pre=None,
                       prev_hash=head.hash)
    with pytest.raises(SeqConflict):
        store.append_events(t, [early.stored()])
    store.append_events(t, [good.stored()])
    assert [r.seq for r in store.events(t)] == [1, 2, 3]


# --------------------------------------------------------------------------- the API on PostgreSQL


def test_a_tenant_built_through_the_api_is_rebuilt_identically_from_postgres(tmp_path: Path,
                                                                           store: PostgresStore) -> None:
    h = harness(tmp_path, store=store)
    account = signup(h.client, f"owner-{uuid.uuid4().hex[:6]}@example.pt")
    token, tenant = account["token"], account["tenant"]["id"]
    seen = build_business(h, token)
    h.clock.step = timedelta(0)
    companies = [c["id"] for c in h.client.get("/api/companies", headers=bearer(token)).json()["companies"]]
    paths = read_paths(companies, seen["documents"])
    before = {p: (r.status_code, r.json()) for p in paths for r in [h.client.get(p, headers=bearer(token))]}
    with h.manager.open(tenant) as rt:
        digest = state_digest(rt.svc)
    # A new process: nothing cached, everything replayed from PostgreSQL.
    fresh = harness(tmp_path, store=store, objects=h.objects, now=h.clock)
    assert not fresh.manager.cached(tenant)
    after = {p: (r.status_code, r.json()) for p in paths for r in [fresh.client.get(p, headers=bearer(token))]}
    for p in paths:
        assert after[p] == before[p], p
    with fresh.manager.open(tenant) as rt:
        assert state_digest(rt.svc) == digest
    assert all(status == 200 for status, _ in before.values())
    # the accountant key works from the new process too (key fingerprint -> tenant lookup under RLS)
    docs = fresh.client.get("/api/v1/documents", headers=bearer(seen["api_key"])).json()["items"]
    assert [d["id"] for d in docs] == seen["documents"]


def test_two_processes_race_on_one_tenant(tmp_path: Path, store: PostgresStore) -> None:
    one = harness(tmp_path, store=store)
    account = signup(one.client, f"race-{uuid.uuid4().hex[:6]}@example.pt")
    tenant = account["tenant"]["id"]
    two = TenantManager(store, one.objects, now=one.clock)
    assert two.command(tenant, "usr_x", "POST", "/api/tasks", {"title": "two"})[0] == 200
    status, body = one.manager.command(tenant, "usr_x", "POST", "/api/tasks", {"title": "one"})
    assert status == 200 and [t["title"] for t in body["tasks"]] == ["two", "one"]
    with one.manager.open(tenant) as a, two.open(tenant) as b:
        assert state_digest(a.svc) == state_digest(b.svc)


def test_sessions_and_rate_limits_in_postgres(tmp_path: Path, store: PostgresStore) -> None:
    h = harness(tmp_path, store=store)
    email = f"limit-{uuid.uuid4().hex[:6]}@example.pt"
    account = signup(h.client, email)
    codes = [h.client.post("/api/auth/login", json={"email": email, "password": "wrong password"}).status_code
             for _ in range(11)]
    assert codes == [401] * 10 + [429]
    assert h.client.get("/api/auth/me", headers=bearer(account["token"])).json()["user"]["email"] == email
    h.clock.advance(days=31)
    assert h.client.get("/api/auth/me", headers=bearer(account["token"])).status_code == 401
    now = datetime.now(timezone.utc)
    session = Session("a" * 64, account["user"]["id"], account["tenant"]["id"], "web", now, now,
                      now + timedelta(days=1))
    store.create_session(session)
    store.revoke_session("a" * 64, now)
    assert store.session("a" * 64).revoked_at is not None


def test_vault_secrets_in_postgres(tmp_path: Path, store: PostgresStore) -> None:
    from backoffice.connectors.vault import LocalKeyProvider, TokenVault

    _, account = _account(tmp_path, store, f"vault-{uuid.uuid4().hex[:6]}@example.pt")
    tenant = account["tenant"]["id"]
    vault = TokenVault(LocalKeyProvider(os.urandom(32)), PostgresCredentialStore(store))
    vault.store(tenant, "mail-x", "imap", {"password": "app-password"})
    assert vault.open(tenant, "mail-x") == {"password": "app-password"}
    vault.update(tenant, "mail-x", {"password": "rotated"})
    assert vault.open(tenant, "mail-x")["password"] == "rotated"
    assert vault.metadata(tenant, "mail-x").version == 2
    assert vault.delete(tenant, "mail-x") and not vault.has(tenant, "mail-x")


def test_account_erasure_removes_the_business_and_leaves_a_record(tmp_path: Path, database: dict,
                                                                 store: PostgresStore) -> None:
    db: PsqlExecutor = database["db"]
    h = harness(tmp_path, store=store)
    email = f"erase-{uuid.uuid4().hex[:6]}@example.pt"
    account = signup(h.client, email)
    tenant, H = account["tenant"]["id"], bearer(account["token"])
    h.client.post("/api/tasks", json={"title": "x"}, headers=H)
    h.client.post("/api/devices", json={"expoPushToken": "ExponentPushToken[eeeeeeeeeeeeeeee]", "platform": "ios"},
                  headers=H)
    h.client.post("/api/accountant/api-keys", json={"name": "k"}, headers=H)
    res = h.client.post("/api/account/delete", json={"confirm": "DELETE", "password": PASSWORD}, headers=H)
    assert res.status_code == 202, res.text
    for table in ("tenant_events", "sessions", "devices", "accountant_api_keys", "memberships"):
        assert _count(db, f"SELECT count(*) AS n FROM {table} WHERE tenant_id = '{tenant}'") == 0, table
    assert _count(db, f"SELECT count(*) AS n FROM tenants WHERE id = '{tenant}'") == 0
    assert _count(db, f"SELECT count(*) AS n FROM users WHERE email = '{email}'") == 0
    record = db.query(f"SELECT events_erased, completed_at IS NOT NULL AS done, objects_purged_at IS NOT NULL AS files "
                      f"FROM tenant_erasures WHERE tenant_id = '{tenant}'")
    assert record == [{"events_erased": "4", "done": "t", "files": "t"}]  # created, company, task, key
    assert h.client.post("/api/auth/login", json={"email": email, "password": PASSWORD}).status_code == 401


def test_listing_tenants_needs_the_scheduler_role(tmp_path: Path, database: dict, store: PostgresStore) -> None:
    from backoffice.server.store import StoreError
    from backoffice.server.sync import SyncWorker

    h = harness(tmp_path, store=store)
    account = signup(h.client, f"list-{uuid.uuid4().hex[:6]}@example.pt")
    tenant = account["tenant"]["id"]
    assert tenant in store.tenant_ids()
    assert len(store.tenant_ids()) > 1
    with store._tx(tenant=tenant) as cur:  # the membership never widens ordinary, tenant-scoped reads
        cur.execute("SELECT id FROM tenants")
        assert [r[0] for r in cur.fetchall()] == [tenant]
    with store._tx() as cur:
        cur.execute("SELECT count(*) FROM tenants")
        assert cur.fetchone()[0] == 0
    plain = PostgresStore(str(database["plain_url"]), pool_size=1)
    try:
        with pytest.raises(StoreError):
            plain.tenant_ids()
    finally:
        plain.close()
    # The sync worker fans out over PostgreSQL: the day turns for a tenant nobody opened.
    h.clock.advance(days=1)
    report = SyncWorker(h.manager).run_once()
    assert report.ticks >= 1 and [e.kind for e in store.events(tenant)][-1] == "tick"
