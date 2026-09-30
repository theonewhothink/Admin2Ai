"""Sign-up, sign-in, sessions, the CSRF guard, roles and sign-in rate limits (§52).

* Passwords: at least 10 characters, scrypt-hashed (:mod:`.passwords`).
* Sessions: a random 32-byte token (``secrets.token_urlsafe``) handed to the
  client once; the server keeps only its SHA-256, with a 30-day expiry that
  slides forward while the session is used. The web app holds it in the
  ``a2a_session`` cookie (HttpOnly, Secure, SameSite=Lax); the phone sends
  ``Authorization: Bearer <token>``.
* CSRF: a state-changing request authenticated by the cookie must carry
  ``X-Requested-With: admin2ai`` (a cross-site form cannot set it, and CORS
  blocks cross-site scripts from sending it). Bearer requests need nothing.
* Rate limits: 10 sign-in attempts per 15 minutes per email and per IP
  address; the database only sees keyed hashes of both.
* Roles: ``owner`` (everything in their business), ``accountant`` (reads and
  teaches rules), ``admin`` (the internal dashboard; also an owner of their
  own business). An email listed in BACKOFFICE_ADMIN_EMAILS becomes admin
  when its account is created.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from backoffice.orchestrator import TZ
from backoffice.service import ServiceError

from .passwords import PasswordRejected, check_password_rules, dummy_verify, hash_password, verify_password
from .runtime import TenantManager
from .store import AccountExists, Session, Store, Tenant, User

__all__ = [
    "AuthError",
    "AuthService",
    "COOKIE_NAME",
    "CSRF_HEADER",
    "CSRF_VALUE",
    "Principal",
    "SESSION_DAYS",
    "hash_token",
    "permitted",
]

COOKIE_NAME = "a2a_session"
CSRF_HEADER = "x-requested-with"
CSRF_VALUE = "admin2ai"
SESSION_DAYS = 30
SESSION_TOUCH = timedelta(hours=1)  # slide the expiry at most this often (one write per hour of use)
RATE_LIMIT = 10
RATE_WINDOW = timedelta(minutes=15)
ROLE_ORDER = ("admin", "owner", "accountant")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_TOKEN = re.compile(r"^[A-Za-z0-9_-]{32,128}$")

WRONG_CREDENTIALS = "Email or password is not right."
TOO_MANY = "Too many attempts. Please wait 15 minutes and try again."


class AuthError(Exception):
    """A refusal with an HTTP status and a plain-language message (§48, §70)."""

    def __init__(self, status: int, error: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.error = error
        self.message = message

    def body(self) -> dict[str, str]:
        return {"error": self.error, "message": self.message}


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass(frozen=True)
class Principal:
    """Who is calling: a signed-in person, in one of their businesses."""

    user: User
    tenant: Tenant
    roles: frozenset[str]
    token_hash: str
    via: str  # "cookie" | "bearer"

    @property
    def role(self) -> str:
        return next((r for r in ROLE_ORDER if r in self.roles), "accountant")

    @property
    def is_owner(self) -> bool:
        return bool({"owner", "admin"} & self.roles)

    @property
    def is_admin(self) -> bool:
        return "admin" in self.roles

    def public(self) -> dict[str, Any]:
        return {"user": {"id": self.user.id, "email": self.user.email, "name": self.user.name},
                "tenant": {"id": self.tenant.id, "name": self.tenant.name}, "role": self.role}


# Accountants read and teach rules (§28); these POSTs are reads or rules.
ACCOUNTANT_POSTS = frozenset({"/api/ask", "/api/accountant/rules", "/api/documents/export", "/api/auth/logout",
                              "/api/devices", "/api/devices/remove"})
OWNER_ONLY_READS = frozenset({"/api/account/export"})


def permitted(principal: Principal, method: str, path: str) -> bool:
    """May ``principal`` call ``method path``? Owners and admins may do everything in their business."""
    if principal.is_owner:
        return True
    if method in ("GET", "HEAD"):
        return path not in OWNER_ONLY_READS
    return path in ACCOUNTANT_POSTS


@dataclass(frozen=True)
class Issued:
    principal: Principal
    token: str
    status: int


class AuthService:
    def __init__(self, store: Store, tenants: TenantManager, *, rate_key: bytes = b"",
                 admin_emails: frozenset[str] = frozenset(), now: Callable[[], datetime] | None = None) -> None:
        self.store = store
        self.tenants = tenants
        self._rate_key = rate_key
        self.admin_emails = admin_emails
        self._now = now or tenants.now

    def now(self) -> datetime:
        return self._now().astimezone(TZ)

    # ----------------------------------------------------------------- rate limits

    def subject(self, kind: str, value: str) -> str:
        return hmac.new(self._rate_key, f"{kind}:{value}".encode(), hashlib.sha256).hexdigest()

    def _subjects(self, email: str | None, ip: str | None, *, kind: str = "login") -> list[str]:
        out = []
        if email:
            out.append(self.subject(f"{kind}-email", email))
        if ip:
            out.append(self.subject(f"{kind}-ip", ip))
        return out

    def _check(self, subjects: list[str]) -> None:
        if subjects:
            counts = self.store.count_attempts(subjects, self.now() - RATE_WINDOW)
            if any(counts.get(s, 0) >= RATE_LIMIT for s in subjects):
                raise AuthError(429, "too_many_attempts", TOO_MANY)

    def _throttle(self, subjects: list[str]) -> None:
        """Refuse when over the limit, else count this attempt."""
        self._check(subjects)
        if subjects:
            self.store.record_attempt(subjects, self.now(), False)

    # ----------------------------------------------------------------- sign-up

    @staticmethod
    def _email(value: Any) -> str:
        email = str(value or "").strip().lower()
        if not _EMAIL.match(email) or len(email) > 254:
            raise AuthError(400, "bad_request", "That doesn't look like an email address.")
        return email

    def signup(self, body: Mapping[str, Any], *, ip: str | None, client: str) -> Issued:
        email = self._email(body.get("email"))
        name = " ".join(str(body.get("name") or "").split())
        company = " ".join(str(body.get("companyName") or "").split())
        tax_id = str(body.get("taxId") or "").strip() or None
        if not name:
            raise AuthError(400, "bad_request", "What is your name?")
        if len(name) > 120:
            raise AuthError(400, "bad_request", "That name is too long.")
        if not company:
            raise AuthError(400, "bad_request", "What is your company called?")
        try:
            check_password_rules(body.get("password"))
        except PasswordRejected as exc:
            raise AuthError(400, "bad_request", str(exc)) from None
        self._throttle(self._subjects(None, ip, kind="signup"))
        if self.store.email_exists(email):
            raise AuthError(409, "conflict", "There is already an account with this email. Sign in instead.")
        tenant_id = "t" + secrets.token_hex(8)
        user = User(id="usr_" + secrets.token_hex(8), email=email, name=name)
        try:
            rt, events = self.tenants.build_tenant(tenant_id, owner_name=name, owner_email=email,
                                                   company_name=company, tax_id=tax_id, actor=user.id)
        except ServiceError as exc:
            raise AuthError(exc.status, "bad_request" if exc.status == 400 else "conflict", exc.message) from None
        roles = ["owner"] + (["admin"] if email in self.admin_emails else [])
        tenant = Tenant(tenant_id, company)
        try:
            self.store.create_account(user=user, password_hash=hash_password(str(body.get("password"))),
                                      tenant=tenant, roles=roles, events=[e.stored() for e in events], at=self.now())
        except AccountExists:
            raise AuthError(409, "conflict", "There is already an account with this email. Sign in instead.") from None
        self.tenants.install(rt)
        principal = Principal(user, tenant, frozenset(roles), "", "bearer")
        return self._issue(principal, client, status=201)

    # ----------------------------------------------------------------- sign-in

    def login(self, body: Mapping[str, Any], *, ip: str | None, client: str) -> Issued:
        raw = str(body.get("email") or "").strip().lower()
        password = body.get("password")
        subjects = self._subjects(raw[:254] or None, ip)
        self._check(subjects)
        succeeded = False
        try:
            issued = self._login(raw, password, client)
            succeeded = True
            return issued
        finally:
            if subjects:
                self.store.record_attempt(subjects, self.now(), succeeded)

    def _login(self, raw: str, password: Any, client: str) -> Issued:
        if not _EMAIL.match(raw) or not isinstance(password, str):
            dummy_verify()
            raise AuthError(401, "unauthorized", WRONG_CREDENTIALS)
        found = self.store.login_lookup(raw)
        if found is None:
            dummy_verify()  # same time as a real check: no telling which emails exist
            raise AuthError(401, "unauthorized", WRONG_CREDENTIALS)
        user, stored = found
        if not verify_password(password, stored):
            raise AuthError(401, "unauthorized", WRONG_CREDENTIALS)
        memberships = self.store.memberships(user.id)
        if not memberships:
            raise AuthError(401, "unauthorized", WRONG_CREDENTIALS)
        rank = {r: i for i, r in enumerate(("owner", "admin", "accountant"))}
        tenant = sorted(memberships, key=lambda m: (rank.get(m[1], 9), m[0].id))[0][0]
        loaded = self.store.principal(tenant.id, user.id)
        if loaded is None:
            raise AuthError(401, "unauthorized", WRONG_CREDENTIALS)
        user, tenant, roles = loaded
        return self._issue(Principal(user, tenant, roles, "", "bearer"), client, status=200)

    def _issue(self, principal: Principal, client: str, *, status: int) -> Issued:
        token = secrets.token_urlsafe(32)
        now = self.now()
        self.store.create_session(Session(
            token_hash=hash_token(token), user_id=principal.user.id, tenant_id=principal.tenant.id,
            client="mobile" if client == "mobile" else "web", created_at=now, last_seen_at=now,
            expires_at=now + timedelta(days=SESSION_DAYS)))
        return Issued(Principal(principal.user, principal.tenant, principal.roles, hash_token(token), "bearer"),
                      token, status)

    def reauthenticate(self, principal: Principal, password: Any, *, ip: str | None) -> None:
        """Ask for the password again before something irreversible (account deletion)."""
        self._throttle(self._subjects(principal.user.email, ip))
        stored = self.store.password_hash(principal.user.id)
        if stored is None or not verify_password(password, stored):
            raise AuthError(401, "unauthorized", "That password is not right.")

    # ----------------------------------------------------------------- sessions

    def authenticate(self, token: str | None, via: str) -> tuple[Principal, bool]:
        """The principal for a presented token, and whether its expiry was just extended."""
        if not token or not _TOKEN.match(token):
            raise AuthError(401, "unauthorized", "Please sign in.")
        token_hash = hash_token(token)
        session = self.store.session(token_hash)
        now = self.now()
        if session is None or session.revoked_at is not None or session.expires_at <= now:
            raise AuthError(401, "unauthorized", "Please sign in again.")
        loaded = self.store.principal(session.tenant_id, session.user_id)
        if loaded is None:
            raise AuthError(401, "unauthorized", "Please sign in again.")
        user, tenant, roles = loaded
        extended = False
        if now - session.last_seen_at >= SESSION_TOUCH:
            self.store.extend_session(token_hash, now, now + timedelta(days=SESSION_DAYS))
            extended = True
        return Principal(user, tenant, roles, token_hash, via), extended

    def logout(self, principal: Principal) -> None:
        self.store.revoke_session(principal.token_hash, self.now())


def utc(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc)
