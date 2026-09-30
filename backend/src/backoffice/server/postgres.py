"""The production store on PostgreSQL (psycopg 3), under row-level security on every query.

The API connects as the ``backoffice_api`` login (a member of
``backoffice_app``: never a superuser, never the table owner, never
BYPASSRLS). Every transaction starts by declaring its scope with the
tenancy helpers of ``db/backoffice_db/tenancy.py``: the tenant (and user)
for tenant data, or, before a tenant is known, only the value being presented
(the email signing in, the session token's hash, the rate-limit subjects,
the API key fingerprint). Settings are transaction-local, so a pooled
connection never carries one request's scope into the next.

``psycopg`` is imported lazily: the engine and the browser build never need it.
"""

from __future__ import annotations

import logging
import sys
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from .store import (
    AccountExists,
    Device,
    IndexOp,
    Invitation,
    SeqConflict,
    Session,
    StoredEvent,
    StoreError,
    StoreUnavailable,
    Tenant,
    User,
)

__all__ = ["PostgresCredentialStore", "PostgresStore", "db_package"]

log = logging.getLogger("backoffice.server.db")


def db_package() -> Any:
    """``backoffice_db`` (db/backoffice_db): on PYTHONPATH in the image, next to backend/ in a checkout."""
    try:
        import backoffice_db
    except ImportError:
        root = Path(__file__).resolve().parents[4] / "db"
        if not (root / "backoffice_db").is_dir():
            raise
        sys.path.insert(0, str(root))
        import backoffice_db
    return backoffice_db


def _psycopg() -> Any:
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise StoreError("psycopg is not installed: pip install 'backoffice[server]'") from exc
    return psycopg


class _Pool:
    """A small thread-safe connection pool (autocommit connections, explicit transactions)."""

    def __init__(self, url: str, *, max_size: int = 10, timeout: float = 10.0) -> None:
        self._url = url
        self._timeout = timeout
        self._idle: list[Any] = []
        self._lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(max_size)

    def _connect(self) -> Any:
        psycopg = _psycopg()
        try:
            return psycopg.connect(self._url, autocommit=True, connect_timeout=5,
                                   application_name="backoffice_api")
        except psycopg.OperationalError:
            raise StoreUnavailable("could not connect to the database") from None

    @contextmanager
    def connection(self) -> Iterator[Any]:
        if not self._slots.acquire(timeout=self._timeout):
            raise StoreUnavailable("no database connection free")
        conn = None
        try:
            with self._lock:
                conn = self._idle.pop() if self._idle else None
            if conn is None or conn.closed or conn.broken:
                conn = self._connect()
            yield conn
        finally:
            if conn is not None:
                self._release(conn)
            self._slots.release()

    def _release(self, conn: Any) -> None:
        psycopg = _psycopg()
        healthy = (not conn.closed and not conn.broken
                   and conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE)
        if healthy:
            with self._lock:
                self._idle.append(conn)
        else:
            try:
                conn.close()
            except Exception:
                pass

    def close(self) -> None:
        with self._lock:
            idle, self._idle = self._idle, []
        for conn in idle:
            try:
                conn.close()
            except Exception:
                pass


class PostgresStore:
    """:class:`~backoffice.server.store.Store` on PostgreSQL."""

    def __init__(self, url: str, *, pool_size: int = 10) -> None:
        if not url:
            raise StoreError("a database URL is required")
        self._pool = _Pool(url, max_size=pool_size)
        self._db = db_package()

    def close(self) -> None:
        self._pool.close()

    @contextmanager
    def _tx(self, *, tenant: str | None = None, user: str | None = None,
            settings: dict[str, str] | None = None) -> Iterator[Any]:
        psycopg = _psycopg()
        db = self._db
        try:
            with self._pool.connection() as conn:
                with conn.transaction():
                    with conn.cursor() as cur:
                        if tenant is not None:
                            cur.execute(db.SCOPE_SQL, db.scope_params(tenant, user))
                        elif user is not None:
                            cur.execute(db.SETTING_SQL, db.setting_params(db.USER_SETTING, db.validate_id(user)))
                        for name, value in (settings or {}).items():
                            cur.execute(db.SETTING_SQL, db.setting_params(name, value))
                        yield cur
        except (psycopg.OperationalError, psycopg.InterfaceError) as exc:
            raise StoreUnavailable(f"database error: {type(exc).__name__}") from None

    # ----------------------------------------------------------------- health

    def ping(self) -> None:
        with self._tx() as cur:
            cur.execute("SELECT 1")

    def schema_status(self) -> tuple[bool, str]:
        """Reachable, and every migration this build ships (that the server can run) applied unchanged."""
        psycopg = _psycopg()
        try:
            with self._tx() as cur:
                cur.execute("SELECT to_regclass('public.tenant_events') IS NOT NULL")
                if not cur.fetchone()[0]:
                    return False, "migrations not applied"
                try:
                    with cur.connection.transaction():
                        cur.execute("SELECT version, checksum FROM public.schema_migrations")
                        applied = {str(v).strip(): str(c).strip() for v, c in cur.fetchall()}
                except psycopg.errors.InsufficientPrivilege:
                    return True, "migration bookkeeping not readable; schema present"
                cur.execute("SELECT name FROM pg_available_extensions")
                available = {r[0] for r in cur.fetchall()}
        except StoreUnavailable:
            return False, "database unreachable"
        try:
            migrations = self._db.load_migrations()
        except Exception:
            return True, "schema present"
        for m in migrations:
            if m.version not in applied:
                if all(ext in available for ext in m.requires_extensions):
                    return False, f"migration {m.version} not applied"
            elif applied[m.version] != m.checksum:
                return False, f"migration {m.version} changed after it was applied"
        return True, "ok"

    # ----------------------------------------------------------------- accounts

    def email_exists(self, email: str) -> bool:
        with self._tx(settings={self._db.LOGIN_EMAIL_SETTING: email}) as cur:
            cur.execute("SELECT 1 FROM users WHERE email = %s", (email,))
            return cur.fetchone() is not None

    def create_account(self, *, user: User, password_hash: str, tenant: Tenant, roles: Sequence[str],
                       events: Sequence[StoredEvent], at: datetime) -> None:
        psycopg = _psycopg()
        try:
            with self._tx(tenant=tenant.id, user=user.id) as cur:
                cur.execute("INSERT INTO tenants (id, name, country, created_at) VALUES (%s, %s, 'PT', %s)",
                            (tenant.id, tenant.name, at))
                cur.execute("INSERT INTO users (id, email, display_name, created_at) VALUES (%s, %s, %s, %s)",
                            (user.id, user.email, user.name, at))
                cur.execute("INSERT INTO user_credentials (user_id, password_hash, changed_at) VALUES (%s, %s, %s)",
                            (user.id, password_hash, at))
                for role in roles:
                    cur.execute("INSERT INTO memberships (tenant_id, user_id, role, created_at) VALUES (%s, %s, %s, %s)",
                                (tenant.id, user.id, role, at))
                self._insert_events(cur, tenant.id, events)
        except psycopg.errors.UniqueViolation as exc:
            if "email" in str(exc.diag.constraint_name or "") or "users" in str(exc.diag.table_name or ""):
                raise AccountExists(user.email) from None
            raise StoreError("could not create the account") from None

    def login_lookup(self, email: str) -> tuple[User, str] | None:
        db = self._db
        with self._tx(settings={db.LOGIN_EMAIL_SETTING: email}) as cur:
            cur.execute("SELECT id, email, coalesce(display_name, '') FROM users WHERE email = %s", (email,))
            row = cur.fetchone()
            if row is None:
                return None
            cur.execute(db.SETTING_SQL, db.setting_params(db.USER_SETTING, db.validate_id(row[0])))
            cur.execute("SELECT password_hash FROM user_credentials WHERE user_id = %s", (row[0],))
            cred = cur.fetchone()
            if cred is None:
                return None
            return User(row[0], row[1], row[2]), cred[0]

    def password_hash(self, user_id: str) -> str | None:
        with self._tx(user=user_id) as cur:
            cur.execute("SELECT password_hash FROM user_credentials WHERE user_id = %s", (user_id,))
            row = cur.fetchone()
            return row[0] if row else None

    def memberships(self, user_id: str) -> list[tuple[Tenant, str]]:
        db = self._db
        with self._tx(user=user_id) as cur:
            cur.execute("SELECT tenant_id, role FROM memberships WHERE user_id = %s AND revoked_at IS NULL "
                        "ORDER BY tenant_id, role", (user_id,))
            rows = cur.fetchall()
            out = []
            for tenant_id, role in rows:
                cur.execute(db.SCOPE_SQL, db.scope_params(tenant_id, user_id))
                cur.execute("SELECT name FROM tenants WHERE id = %s", (tenant_id,))
                name = cur.fetchone()
                if name is not None:
                    out.append((Tenant(tenant_id, name[0]), role))
            return out

    def principal(self, tenant_id: str, user_id: str) -> tuple[User, Tenant, frozenset[str]] | None:
        with self._tx(tenant=tenant_id, user=user_id) as cur:
            cur.execute("SELECT role FROM memberships WHERE tenant_id = %s AND user_id = %s AND revoked_at IS NULL",
                        (tenant_id, user_id))
            roles = frozenset(r[0] for r in cur.fetchall())
            if not roles:
                return None
            cur.execute("SELECT id, email, coalesce(display_name, '') FROM users WHERE id = %s", (user_id,))
            u = cur.fetchone()
            cur.execute("SELECT id, name FROM tenants WHERE id = %s", (tenant_id,))
            t = cur.fetchone()
            if u is None or t is None:
                return None
            return User(u[0], u[1], u[2]), Tenant(t[0], t[1]), roles

    # ----------------------------------------------------------------- sessions

    _SESSION_COLUMNS = "token_hash, user_id, tenant_id, client, created_at, last_seen_at, expires_at, revoked_at"

    def create_session(self, session: Session) -> None:
        with self._tx(tenant=session.tenant_id, user=session.user_id) as cur:
            cur.execute(f"INSERT INTO sessions ({self._SESSION_COLUMNS}) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                        (session.token_hash, session.user_id, session.tenant_id, session.client, session.created_at,
                         session.last_seen_at, session.expires_at, session.revoked_at))

    def session(self, token_hash: str) -> Session | None:
        with self._tx(settings={self._db.SESSION_SETTING: token_hash}) as cur:
            cur.execute(f"SELECT {self._SESSION_COLUMNS} FROM sessions WHERE token_hash = %s", (token_hash,))
            row = cur.fetchone()
            return Session(*row) if row else None

    def extend_session(self, token_hash: str, last_seen_at: datetime, expires_at: datetime) -> None:
        with self._tx(settings={self._db.SESSION_SETTING: token_hash}) as cur:
            cur.execute("UPDATE sessions SET last_seen_at = %s, expires_at = %s "
                        "WHERE token_hash = %s AND revoked_at IS NULL", (last_seen_at, expires_at, token_hash))

    def revoke_session(self, token_hash: str, at: datetime) -> None:
        with self._tx(settings={self._db.SESSION_SETTING: token_hash}) as cur:
            cur.execute("UPDATE sessions SET revoked_at = %s WHERE token_hash = %s AND revoked_at IS NULL",
                        (at, token_hash))

    # ----------------------------------------------------------------- rate limits

    def count_attempts(self, subjects: Sequence[str], since: datetime) -> dict[str, int]:
        with self._tx(settings={self._db.RATE_SUBJECTS_SETTING: ",".join(subjects)}) as cur:
            cur.execute("SELECT subject, count(*) FROM login_attempts WHERE subject = ANY(%s) AND attempted_at >= %s "
                        "GROUP BY subject", (list(subjects), since))
            found = {str(s).strip(): int(n) for s, n in cur.fetchall()}
        return {s: found.get(s, 0) for s in subjects}

    def record_attempt(self, subjects: Sequence[str], at: datetime, succeeded: bool) -> None:
        from datetime import timedelta

        with self._tx(settings={self._db.RATE_SUBJECTS_SETTING: ",".join(subjects)}) as cur:
            cur.execute("DELETE FROM login_attempts WHERE attempted_at < %s", (at - timedelta(days=1),))
            cur.executemany("INSERT INTO login_attempts (subject, attempted_at, succeeded) VALUES (%s, %s, %s)",
                            [(s, at, succeeded) for s in subjects])

    # ----------------------------------------------------------------- events

    @staticmethod
    def _insert_events(cur: Any, tenant_id: str, events: Sequence[StoredEvent]) -> None:
        for e in events:
            cur.execute("INSERT INTO tenant_events (tenant_id, seq, at, kind, actor, body, prev_hash, hash) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                        (tenant_id, e.seq, e.at, e.kind, e.actor, e.body, e.prev_hash, e.hash))

    def append_events(self, tenant_id: str, events: Sequence[StoredEvent], *,
                      index: Sequence[IndexOp] = ()) -> None:
        psycopg = _psycopg()
        try:
            with self._tx(tenant=tenant_id) as cur:
                self._insert_events(cur, tenant_id, events)
                for op in index:
                    if op.op == "add_api_key":
                        cur.execute("INSERT INTO accountant_api_keys (tenant_id, key_id, key_hash, created_at) "
                                    "VALUES (%s, %s, %s, %s)", (tenant_id, op.key_id, op.key_hash, op.at))
                    elif op.op == "remove_api_key":
                        cur.execute("DELETE FROM accountant_api_keys WHERE tenant_id = %s AND key_id = %s",
                                    (tenant_id, op.key_id))
                    elif op.op == "link_billing_customer":  # 0015: a customer is one business's, the first one
                        cur.execute("INSERT INTO billing_customers (customer_id, tenant_id) VALUES (%s, %s) "
                                    "ON CONFLICT (customer_id) DO NOTHING", (op.key_id, tenant_id))
                    else:
                        raise StoreError(f"unknown index op {op.op}")
        except (psycopg.errors.UniqueViolation, psycopg.errors.IntegrityConstraintViolation) as exc:
            table = str(getattr(exc.diag, "table_name", "") or "")
            if table and table != "tenant_events":
                raise StoreError(f"could not write {table}") from None
            raise SeqConflict("another process appended first") from None

    def events(self, tenant_id: str, after_seq: int = 0) -> list[StoredEvent]:
        with self._tx(tenant=tenant_id) as cur:
            cur.execute("SELECT seq, at, kind, actor, body, prev_hash, hash FROM tenant_events "
                        "WHERE tenant_id = %s AND seq > %s ORDER BY seq", (tenant_id, after_seq))
            return [StoredEvent(int(seq), at, kind, actor, body, str(prev).strip(), str(h).strip())
                    for seq, at, kind, actor, body, prev, h in cur.fetchall()]

    # ----------------------------------------------------------------- devices

    def save_device(self, device: Device) -> None:
        with self._tx(tenant=device.tenant_id, user=device.user_id) as cur:
            cur.execute("INSERT INTO devices (tenant_id, expo_push_token, user_id, platform, created_at, last_seen_at) "
                        "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (tenant_id, expo_push_token) DO UPDATE "
                        "SET user_id = EXCLUDED.user_id, platform = EXCLUDED.platform, "
                        "last_seen_at = EXCLUDED.last_seen_at",
                        (device.tenant_id, device.token, device.user_id, device.platform, device.created_at,
                         device.last_seen_at))

    def remove_device(self, tenant_id: str, user_id: str, token: str) -> bool:
        with self._tx(tenant=tenant_id, user=user_id) as cur:
            cur.execute("DELETE FROM devices WHERE tenant_id = %s AND expo_push_token = %s AND user_id = %s",
                        (tenant_id, token, user_id))
            return cur.rowcount > 0

    def forget_device(self, tenant_id: str, token: str) -> None:
        with self._tx(tenant=tenant_id) as cur:
            cur.execute("DELETE FROM devices WHERE tenant_id = %s AND expo_push_token = %s", (tenant_id, token))

    def owner_devices(self, tenant_id: str) -> list[Device]:
        with self._tx(tenant=tenant_id) as cur:
            cur.execute(
                "SELECT d.tenant_id, d.expo_push_token, d.user_id, d.platform, d.created_at, d.last_seen_at "
                "FROM devices d WHERE d.tenant_id = %s AND EXISTS ("
                "  SELECT 1 FROM memberships m WHERE m.tenant_id = d.tenant_id AND m.user_id = d.user_id"
                "  AND m.role IN ('owner', 'admin') AND m.revoked_at IS NULL) ORDER BY d.expo_push_token",
                (tenant_id,))
            return [Device(*row) for row in cur.fetchall()]

    # ----------------------------------------------------------------- api keys, nonces

    def api_key_tenant(self, key_hash: str) -> str | None:
        with self._tx(settings={self._db.API_KEY_SETTING: key_hash}) as cur:
            cur.execute("SELECT tenant_id FROM accountant_api_keys WHERE key_hash = %s", (key_hash,))
            row = cur.fetchone()
            return row[0] if row else None

    # ----------------------------------------------------------------- plans (0015, backoffice.billing)

    def billing_tenant(self, customer_id: str) -> str | None:
        """The business a payment-provider customer pays for (a webhook presents the customer id)."""
        with self._tx(settings={self._db.BILLING_CUSTOMER_SETTING: customer_id}) as cur:
            cur.execute("SELECT tenant_id FROM billing_customers WHERE customer_id = %s", (customer_id,))
            row = cur.fetchone()
            return row[0] if row else None

    def member_count(self, tenant_id: str) -> int:
        """People with access who count for a plan: owners, admins, employees and managers (never accountants)."""
        with self._tx(tenant=tenant_id) as cur:
            cur.execute("SELECT count(DISTINCT user_id) FROM memberships WHERE tenant_id = %s "
                        "AND revoked_at IS NULL AND role <> 'accountant'", (tenant_id,))
            return int(cur.fetchone()[0])

    # ----------------------------------------------------------------- accountants of some companies (0011)

    def manager_scope(self, tenant_id: str, user_id: str) -> tuple[str, tuple[str, ...]] | None:
        """(company, cost centers) of a manager membership (0014); None when the person is not one there."""
        with self._tx(tenant=tenant_id, user=user_id) as cur:
            cur.execute("SELECT company_ids, cost_center_ids FROM memberships WHERE tenant_id = %s AND user_id = %s "
                        "AND role = 'manager' AND revoked_at IS NULL", (tenant_id, user_id))
            row = cur.fetchone()
            if not row or not row[0] or not row[1]:
                return None
            return str(row[0][0]), tuple(str(c) for c in row[1])

    def add_manager(self, tenant_id: str, user_id: str, *, company: str, cost_centers: Sequence[str],
                    invited_by: str | None = None, at: datetime | None = None) -> None:
        """Give ``user_id`` a manager membership of ``company``'s ``cost_centers`` (0014)."""
        with self._tx(tenant=tenant_id, user=invited_by or user_id) as cur:
            cur.execute("INSERT INTO memberships (tenant_id, user_id, role, invited_by, created_at, company_ids, "
                        "cost_center_ids) VALUES (%s, %s, 'manager', %s, coalesce(%s, now()), %s, %s) "
                        "ON CONFLICT (tenant_id, user_id, role) DO UPDATE SET company_ids = EXCLUDED.company_ids, "
                        "cost_center_ids = EXCLUDED.cost_center_ids, revoked_at = NULL",
                        (tenant_id, user_id, invited_by, at, [company], list(dict.fromkeys(cost_centers))))

    def membership_companies(self, tenant_id: str, user_id: str) -> tuple[str, ...] | None:
        with self._tx(tenant=tenant_id, user=user_id) as cur:
            cur.execute("SELECT company_ids FROM memberships WHERE tenant_id = %s AND user_id = %s "
                        "AND role = 'accountant' AND revoked_at IS NULL", (tenant_id, user_id))
            row = cur.fetchone()
            return tuple(str(c) for c in row[0]) if row and row[0] else None

    # ----------------------------------------------------------------- client invitations (0011, §29)

    _INVITATION_COLUMNS = ("id, token_hash, inviter_tenant_id, inviter_user_id, inviter_email, inviter_name, firm, "
                           "email, client_name, tax_ids, created_at, expires_at, sent_at, accepted_at, "
                           "accepted_tenant_id, accepted_by")

    @staticmethod
    def _invitation(row: Any) -> Invitation:
        (iid, token_hash, tenant, user, inviter_email, inviter_name, firm, email, client_name, tax_ids, created_at,
         expires_at, sent_at, accepted_at, accepted_tenant, accepted_by) = row
        return Invitation(id=iid, token_hash=str(token_hash).strip(), inviter_tenant_id=tenant, inviter_user_id=user,
                          inviter_email=inviter_email, inviter_name=inviter_name, firm=firm, email=email,
                          client_name=client_name, tax_ids=tuple(str(t) for t in tax_ids or ()),
                          created_at=created_at, expires_at=expires_at, sent_at=sent_at, accepted_at=accepted_at,
                          accepted_tenant_id=accepted_tenant, accepted_by=accepted_by)

    def create_invitation(self, invitation: Invitation) -> None:
        i = invitation
        with self._tx(tenant=i.inviter_tenant_id, user=i.inviter_user_id) as cur:
            cur.execute(f"INSERT INTO accountant_invitations ({self._INVITATION_COLUMNS}) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                        (i.id, i.token_hash, i.inviter_tenant_id, i.inviter_user_id, i.inviter_email, i.inviter_name,
                         i.firm, i.email, i.client_name, list(i.tax_ids), i.created_at, i.expires_at, i.sent_at,
                         i.accepted_at, i.accepted_tenant_id, i.accepted_by))

    def invitation(self, token_hash: str) -> Invitation | None:
        with self._tx(settings={self._db.INVITE_SETTING: token_hash}) as cur:
            cur.execute(f"SELECT {self._INVITATION_COLUMNS} FROM accountant_invitations WHERE token_hash = %s",
                        (token_hash,))
            row = cur.fetchone()
            return self._invitation(row) if row else None

    def mark_invitation_sent(self, invitation: Invitation, at: datetime) -> None:
        with self._tx(tenant=invitation.inviter_tenant_id, user=invitation.inviter_user_id) as cur:
            cur.execute("UPDATE accountant_invitations SET sent_at = %s WHERE id = %s AND sent_at IS NULL",
                        (at, invitation.id))

    def invitations_from(self, tenant_id: str, user_id: str) -> list[Invitation]:
        with self._tx(tenant=tenant_id, user=user_id) as cur:
            cur.execute(f"SELECT {self._INVITATION_COLUMNS} FROM accountant_invitations "
                        "WHERE inviter_tenant_id = %s AND inviter_user_id = %s ORDER BY created_at DESC, id DESC",
                        (tenant_id, user_id))
            return [self._invitation(r) for r in cur.fetchall()]

    def accept_invitation(self, token_hash: str, *, tenant_id: str, accepted_by: str, companies: Sequence[str],
                          at: datetime) -> bool:
        """One transaction: the invitation (locked) is used once, and its inviter becomes an accountant."""
        with self._tx(tenant=tenant_id, user=accepted_by, settings={self._db.INVITE_SETTING: token_hash}) as cur:
            cur.execute("SELECT inviter_user_id FROM accountant_invitations WHERE token_hash = %s "
                        "AND accepted_at IS NULL AND expires_at > %s FOR UPDATE", (token_hash, at))
            row = cur.fetchone()
            if row is None:
                return False
            cur.execute("UPDATE accountant_invitations SET accepted_at = %s, accepted_tenant_id = %s, accepted_by = %s "
                        "WHERE token_hash = %s", (at, tenant_id, accepted_by, token_hash))
            cur.execute("INSERT INTO memberships (tenant_id, user_id, role, invited_by, created_at, company_ids) "
                        "VALUES (%s, %s, 'accountant', %s, %s, %s) ON CONFLICT (tenant_id, user_id, role) DO UPDATE "
                        "SET company_ids = EXCLUDED.company_ids, revoked_at = NULL",
                        (tenant_id, row[0], accepted_by, at, list(dict.fromkeys(companies))))
            return True

    def save_nonce(self, tenant_id: str, nonce: str, expires_at: datetime) -> None:
        with self._tx(tenant=tenant_id) as cur:
            cur.execute("DELETE FROM oauth_nonces WHERE tenant_id = %s AND expires_at < now()", (tenant_id,))
            cur.execute("INSERT INTO oauth_nonces (tenant_id, nonce, expires_at) VALUES (%s, %s, %s)",
                        (tenant_id, nonce, expires_at))

    def take_nonce(self, tenant_id: str, nonce: str, now: datetime) -> bool:
        with self._tx(tenant=tenant_id) as cur:
            cur.execute("DELETE FROM oauth_nonces WHERE tenant_id = %s AND nonce = %s AND expires_at > %s "
                        "RETURNING nonce", (tenant_id, nonce, now))
            return cur.fetchone() is not None

    # ----------------------------------------------------------------- erasure

    def erase_account(self, tenant_id: str, user_id: str, at: datetime) -> int:
        db = self._db
        with self._tx(tenant=tenant_id, user=user_id) as cur:
            cur.execute("SELECT count(*) FROM tenant_events WHERE tenant_id = %s", (tenant_id,))
            events = int(cur.fetchone()[0])
            cur.execute("INSERT INTO tenant_erasures (tenant_id, requested_at, events_erased) VALUES (%s, %s, %s) "
                        "ON CONFLICT (tenant_id) DO NOTHING", (tenant_id, at, events))
            cur.execute(db.SETTING_SQL, db.setting_params(db.ERASURE_SETTING, tenant_id))
            cur.execute("DELETE FROM memberships WHERE tenant_id = %s", (tenant_id,))
            # Cascades to the event log (allowed only by the erasure above), sessions, devices,
            # API keys, sign-ins in progress and sealed connection secrets.
            cur.execute("DELETE FROM tenants WHERE id = %s", (tenant_id,))
            cur.execute("UPDATE tenant_erasures SET completed_at = %s WHERE tenant_id = %s", (at, tenant_id))
            cur.execute("SELECT 1 FROM memberships WHERE user_id = %s AND revoked_at IS NULL LIMIT 1", (user_id,))
            if cur.fetchone() is None:
                cur.execute("DELETE FROM users WHERE id = %s", (user_id,))
            return events

    def tenant_ids(self) -> list[str]:
        """Every tenant id, as the scheduler role (the only one that may list tenants, 0001).

        The API/worker login must be a member of ``backoffice_scheduler``
        (``ensure-login ... --member-of backoffice_app --member-of backoffice_scheduler``).
        Everything else is still read per tenant under row-level security.
        """
        psycopg = _psycopg()
        try:
            with self._tx() as cur:
                cur.execute(f"SET LOCAL ROLE {self._db.SCHEDULER_ROLE}")
                cur.execute("SELECT id FROM tenants ORDER BY id")
                return [str(r[0]) for r in cur.fetchall()]
        except psycopg.errors.InsufficientPrivilege:
            raise StoreError(f"the database login is not a member of {self._db.SCHEDULER_ROLE}") from None

    def pending_erasures(self) -> list[str]:
        """Erased businesses whose files are still to purge, listed as the scheduler (migration 0008)."""
        psycopg = _psycopg()
        try:
            with self._tx() as cur:
                cur.execute(f"SET LOCAL ROLE {self._db.SCHEDULER_ROLE}")
                cur.execute("SELECT tenant_id FROM tenant_erasures "
                            "WHERE completed_at IS NOT NULL AND objects_purged_at IS NULL ORDER BY tenant_id")
                return [str(r[0]) for r in cur.fetchall()]
        except psycopg.errors.InsufficientPrivilege:
            raise StoreError(f"the database login is not a member of {self._db.SCHEDULER_ROLE}") from None

    def mark_objects_purged(self, tenant_id: str, at: datetime) -> None:
        with self._tx(tenant=tenant_id) as cur:
            cur.execute("UPDATE tenant_erasures SET objects_purged_at = %s WHERE tenant_id = %s", (at, tenant_id))


class PostgresCredentialStore:
    """The vault's :class:`~backoffice.connectors.vault.CredentialStore` on ``connection_credentials`` (0006)."""

    _COLUMNS = ("tenant_id, connection_id, provider, key_id, wrapped_key, nonce, ciphertext, version, expires_at, "
                "created_at, updated_at")

    def __init__(self, store: PostgresStore) -> None:
        self._store = store

    def put(self, record: Any) -> None:
        with self._store._tx(tenant=record.tenant_id) as cur:
            cur.execute(
                f"INSERT INTO connection_credentials ({self._COLUMNS}) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (tenant_id, connection_id) DO UPDATE SET provider = EXCLUDED.provider, "
                "key_id = EXCLUDED.key_id, wrapped_key = EXCLUDED.wrapped_key, nonce = EXCLUDED.nonce, "
                "ciphertext = EXCLUDED.ciphertext, version = EXCLUDED.version, expires_at = EXCLUDED.expires_at, "
                "updated_at = EXCLUDED.updated_at",
                (record.tenant_id, record.connection_id, record.provider, record.key_id, record.wrapped_key,
                 record.nonce, record.ciphertext, record.version, record.expires_at, record.created_at,
                 record.updated_at))

    def _record(self, row: Any) -> Any:
        from backoffice.connectors.vault import CredentialRecord

        (tenant_id, connection_id, provider, key_id, wrapped, nonce, ciphertext, version, expires_at, created_at,
         updated_at) = row
        return CredentialRecord(tenant_id=tenant_id, connection_id=connection_id, provider=provider, key_id=key_id,
                                wrapped_key=bytes(wrapped), nonce=bytes(nonce), ciphertext=bytes(ciphertext),
                                created_at=created_at, updated_at=updated_at, expires_at=expires_at,
                                version=int(version))

    def get(self, tenant_id: str, connection_id: str) -> Any:
        with self._store._tx(tenant=tenant_id) as cur:
            cur.execute(f"SELECT {self._COLUMNS} FROM connection_credentials WHERE tenant_id = %s "
                        "AND connection_id = %s", (tenant_id, connection_id))
            row = cur.fetchone()
            return self._record(row) if row else None

    def delete(self, tenant_id: str, connection_id: str) -> bool:
        with self._store._tx(tenant=tenant_id) as cur:
            cur.execute("DELETE FROM connection_credentials WHERE tenant_id = %s AND connection_id = %s",
                        (tenant_id, connection_id))
            return cur.rowcount > 0

    def list(self, tenant_id: str) -> list[Any]:
        with self._store._tx(tenant=tenant_id) as cur:
            cur.execute(f"SELECT {self._COLUMNS} FROM connection_credentials WHERE tenant_id = %s "
                        "ORDER BY connection_id", (tenant_id,))
            return [self._record(r) for r in cur.fetchall()]
