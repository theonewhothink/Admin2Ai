"""What the production API needs from its database, and an in-memory version of it.

:class:`Store` is the contract; :class:`backoffice.server.postgres.PostgresStore`
implements it on PostgreSQL (row-level security on every query) and
:class:`MemoryStore` implements it in memory for unit tests and single-process
experiments. Both enforce the same rules the schema enforces: one owner of an
email, append-only hash-chained events, erasure only with its record.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Protocol

__all__ = [
    "AccountExists",
    "Device",
    "IndexOp",
    "Invitation",
    "MemoryStore",
    "SeqConflict",
    "Session",
    "Store",
    "StoreError",
    "StoreUnavailable",
    "StoredEvent",
    "Tenant",
    "User",
]

ROLES = ("owner", "accountant", "admin")


class StoreError(Exception):
    """Developer-facing storage failure; never shown to an owner as is."""


class StoreUnavailable(StoreError):
    """The database cannot be reached right now."""


class AccountExists(StoreError):
    """That email already has an account."""


class SeqConflict(StoreError):
    """Another process appended to this tenant's log first: catch up and try again."""


@dataclass(frozen=True)
class User:
    id: str
    email: str
    name: str


@dataclass(frozen=True)
class Tenant:
    id: str
    name: str


@dataclass(frozen=True)
class Session:
    token_hash: str
    user_id: str
    tenant_id: str
    client: str
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None


@dataclass(frozen=True)
class Device:
    tenant_id: str
    token: str
    user_id: str
    platform: str
    created_at: datetime
    last_seen_at: datetime


@dataclass(frozen=True)
class StoredEvent:
    """One row of a tenant's event log, exactly as stored (see :mod:`.events`)."""

    seq: int
    at: datetime
    kind: str
    actor: str
    body: str
    prev_hash: str
    hash: str


@dataclass(frozen=True)
class Invitation:
    """A client business invited by an accountant (§29). Only the token's SHA-256 is kept."""

    id: str
    token_hash: str
    inviter_tenant_id: str
    inviter_user_id: str
    inviter_email: str
    inviter_name: str
    firm: str
    email: str
    client_name: str
    tax_ids: tuple[str, ...]
    created_at: datetime
    expires_at: datetime
    sent_at: datetime | None = None
    accepted_at: datetime | None = None
    accepted_tenant_id: str | None = None
    accepted_by: str | None = None


@dataclass(frozen=True)
class IndexOp:
    """A lookup row written in the same transaction as an event (accountant API keys)."""

    op: str  # "add_api_key" | "remove_api_key"
    key_id: str
    key_hash: str = ""
    at: datetime | None = None


class Store(Protocol):
    # health
    def ping(self) -> None: ...
    def schema_status(self) -> tuple[bool, str]: ...

    # accounts
    def email_exists(self, email: str) -> bool: ...
    def create_account(self, *, user: User, password_hash: str, tenant: Tenant, roles: Sequence[str],
                       events: Sequence[StoredEvent], at: datetime) -> None: ...
    def login_lookup(self, email: str) -> tuple[User, str] | None: ...
    def password_hash(self, user_id: str) -> str | None: ...
    def memberships(self, user_id: str) -> list[tuple[Tenant, str]]: ...
    def principal(self, tenant_id: str, user_id: str) -> tuple[User, Tenant, frozenset[str]] | None: ...

    # sessions
    def create_session(self, session: Session) -> None: ...
    def session(self, token_hash: str) -> Session | None: ...
    def extend_session(self, token_hash: str, last_seen_at: datetime, expires_at: datetime) -> None: ...
    def revoke_session(self, token_hash: str, at: datetime) -> None: ...

    # sign-in rate limits
    def count_attempts(self, subjects: Sequence[str], since: datetime) -> dict[str, int]: ...
    def record_attempt(self, subjects: Sequence[str], at: datetime, succeeded: bool) -> None: ...

    # tenant event log
    def append_events(self, tenant_id: str, events: Sequence[StoredEvent], *,
                      index: Sequence[IndexOp] = ()) -> None: ...
    def events(self, tenant_id: str, after_seq: int = 0) -> list[StoredEvent]: ...

    # devices
    def save_device(self, device: Device) -> None: ...
    def remove_device(self, tenant_id: str, user_id: str, token: str) -> bool: ...
    def forget_device(self, tenant_id: str, token: str) -> None: ...
    def owner_devices(self, tenant_id: str) -> list[Device]: ...

    # accountant API keys
    def api_key_tenant(self, key_hash: str) -> str | None: ...

    # accountants limited to some companies, and clients invited by their accountant (§28, §29, §51)
    def membership_companies(self, tenant_id: str, user_id: str) -> tuple[str, ...] | None: ...
    def create_invitation(self, invitation: Invitation) -> None: ...
    def invitation(self, token_hash: str) -> Invitation | None: ...
    def mark_invitation_sent(self, invitation: Invitation, at: datetime) -> None: ...
    def invitations_from(self, tenant_id: str, user_id: str) -> list[Invitation]: ...
    def accept_invitation(self, token_hash: str, *, tenant_id: str, accepted_by: str, companies: Sequence[str],
                          at: datetime) -> bool: ...

    # mailbox sign-ins in progress
    def save_nonce(self, tenant_id: str, nonce: str, expires_at: datetime) -> None: ...
    def take_nonce(self, tenant_id: str, nonce: str, now: datetime) -> bool: ...

    # erasure
    def erase_account(self, tenant_id: str, user_id: str, at: datetime) -> int: ...
    def mark_objects_purged(self, tenant_id: str, at: datetime) -> None: ...

    # every tenant (the sync worker's fan-out and the team's internal dashboard)
    def tenant_ids(self) -> list[str]: ...

    # erased businesses whose files are still to purge (the sync worker completes them)
    def pending_erasures(self) -> list[str]: ...


# --------------------------------------------------------------------------- in memory


@dataclass
class _Erasure:
    requested_at: datetime
    completed_at: datetime | None = None
    events_erased: int = 0
    objects_purged_at: datetime | None = None


@dataclass
class _Data:
    users: dict[str, User] = field(default_factory=dict)
    credentials: dict[str, str] = field(default_factory=dict)
    tenants: dict[str, Tenant] = field(default_factory=dict)
    memberships: set[tuple[str, str, str]] = field(default_factory=set)  # (tenant, user, role)
    sessions: dict[str, Session] = field(default_factory=dict)
    attempts: list[tuple[str, datetime, bool]] = field(default_factory=list)
    events: dict[str, list[StoredEvent]] = field(default_factory=dict)
    devices: dict[tuple[str, str], Device] = field(default_factory=dict)
    api_keys: dict[str, tuple[str, str]] = field(default_factory=dict)  # hash -> (tenant, key id)
    nonces: dict[tuple[str, str], datetime] = field(default_factory=dict)
    erasures: dict[str, _Erasure] = field(default_factory=dict)
    # (tenant, user) -> the companies an accountant membership is limited to (absent = every company)
    membership_companies: dict[tuple[str, str], tuple[str, ...]] = field(default_factory=dict)
    invitations: dict[str, Invitation] = field(default_factory=dict)  # token hash -> invitation


class MemoryStore:
    """:class:`Store` in memory, thread-safe. Same rules as the PostgreSQL schema."""

    def __init__(self) -> None:
        self._d = _Data()
        self._lock = threading.RLock()
        self.available = True  # tests flip this to simulate an outage

    def _up(self) -> None:
        if not self.available:
            raise StoreUnavailable("memory store switched off")

    # ----------------------------------------------------------------- health

    def ping(self) -> None:
        self._up()

    def schema_status(self) -> tuple[bool, str]:
        return (self.available, "in-memory store" if self.available else "store unavailable")

    # ----------------------------------------------------------------- accounts

    def email_exists(self, email: str) -> bool:
        self._up()
        with self._lock:
            return any(u.email == email for u in self._d.users.values())

    def create_account(self, *, user: User, password_hash: str, tenant: Tenant, roles: Sequence[str],
                       events: Sequence[StoredEvent], at: datetime) -> None:
        self._up()
        with self._lock:
            if any(u.email == user.email for u in self._d.users.values()):
                raise AccountExists(user.email)
            if tenant.id in self._d.tenants or user.id in self._d.users:
                raise StoreError("duplicate id")
            for role in roles:
                if role not in ROLES:
                    raise StoreError(f"unknown role {role}")
            self._check_chain(tenant.id, [], events)
            self._d.users[user.id] = user
            self._d.credentials[user.id] = password_hash
            self._d.tenants[tenant.id] = tenant
            self._d.memberships |= {(tenant.id, user.id, r) for r in roles}
            self._d.events[tenant.id] = list(events)

    def login_lookup(self, email: str) -> tuple[User, str] | None:
        self._up()
        with self._lock:
            user = next((u for u in self._d.users.values() if u.email == email), None)
            if user is None:
                return None
            return user, self._d.credentials[user.id]

    def password_hash(self, user_id: str) -> str | None:
        self._up()
        return self._d.credentials.get(user_id)

    def memberships(self, user_id: str) -> list[tuple[Tenant, str]]:
        self._up()
        with self._lock:
            return sorted(((self._d.tenants[t], r) for t, u, r in self._d.memberships if u == user_id),
                          key=lambda x: (x[0].id, x[1]))

    def principal(self, tenant_id: str, user_id: str) -> tuple[User, Tenant, frozenset[str]] | None:
        self._up()
        with self._lock:
            roles = frozenset(r for t, u, r in self._d.memberships if t == tenant_id and u == user_id)
            user, tenant = self._d.users.get(user_id), self._d.tenants.get(tenant_id)
            if not roles or user is None or tenant is None:
                return None
            return user, tenant, roles

    def add_membership(self, tenant_id: str, user_id: str, role: str, companies: Sequence[str] = ()) -> None:
        """Give ``user_id`` a role in ``tenant_id`` (tests; invitations use :meth:`accept_invitation`).

        ``companies`` limits an accountant membership to those companies of the business.
        """
        with self._lock:
            if role not in ROLES or tenant_id not in self._d.tenants or user_id not in self._d.users:
                raise StoreError("unknown tenant, user or role")
            if companies and role != "accountant":
                raise StoreError("only an accountant membership is limited to companies")
            self._d.memberships.add((tenant_id, user_id, role))
            if role == "accountant":
                if companies:
                    self._d.membership_companies[(tenant_id, user_id)] = tuple(dict.fromkeys(companies))
                else:
                    self._d.membership_companies.pop((tenant_id, user_id), None)

    def membership_companies(self, tenant_id: str, user_id: str) -> tuple[str, ...] | None:
        """The companies an accountant membership is limited to; None when it covers every company."""
        self._up()
        with self._lock:
            if (tenant_id, user_id, "accountant") not in self._d.memberships:
                return None
            return self._d.membership_companies.get((tenant_id, user_id)) or None

    # ----------------------------------------------------------------- client invitations (§29)

    def create_invitation(self, invitation: Invitation) -> None:
        self._up()
        with self._lock:
            if invitation.token_hash in self._d.invitations or \
                    any(i.id == invitation.id for i in self._d.invitations.values()):
                raise StoreError("duplicate invitation")
            if invitation.inviter_tenant_id not in self._d.tenants or invitation.inviter_user_id not in self._d.users:
                raise StoreError("unknown tenant or user")
            self._d.invitations[invitation.token_hash] = invitation

    def invitation(self, token_hash: str) -> Invitation | None:
        self._up()
        return self._d.invitations.get(token_hash)

    def mark_invitation_sent(self, invitation: Invitation, at: datetime) -> None:
        self._up()
        with self._lock:
            found = self._d.invitations.get(invitation.token_hash)
            if found is not None and found.sent_at is None:
                self._d.invitations[invitation.token_hash] = replace(found, sent_at=at)

    def invitations_from(self, tenant_id: str, user_id: str) -> list[Invitation]:
        self._up()
        with self._lock:
            return sorted((i for i in self._d.invitations.values()
                           if i.inviter_tenant_id == tenant_id and i.inviter_user_id == user_id),
                          key=lambda i: (i.created_at, i.id), reverse=True)

    def accept_invitation(self, token_hash: str, *, tenant_id: str, accepted_by: str, companies: Sequence[str],
                          at: datetime) -> bool:
        """Use the invitation once: the inviter becomes an accountant of ``tenant_id`` (limited to
        ``companies`` when given). False when it is unknown, already used or expired."""
        self._up()
        with self._lock:
            found = self._d.invitations.get(token_hash)
            if found is None or found.accepted_at is not None or found.expires_at <= at:
                return False
            if tenant_id not in self._d.tenants or found.inviter_user_id not in self._d.users:
                return False
            self._d.invitations[token_hash] = replace(found, accepted_at=at, accepted_tenant_id=tenant_id,
                                                      accepted_by=accepted_by)
            self._d.memberships.add((tenant_id, found.inviter_user_id, "accountant"))
            if companies:
                self._d.membership_companies[(tenant_id, found.inviter_user_id)] = tuple(dict.fromkeys(companies))
            else:
                self._d.membership_companies.pop((tenant_id, found.inviter_user_id), None)
            return True

    # ----------------------------------------------------------------- sessions

    def create_session(self, session: Session) -> None:
        self._up()
        with self._lock:
            if session.token_hash in self._d.sessions:
                raise StoreError("duplicate session")
            self._d.sessions[session.token_hash] = session

    def session(self, token_hash: str) -> Session | None:
        self._up()
        return self._d.sessions.get(token_hash)

    def extend_session(self, token_hash: str, last_seen_at: datetime, expires_at: datetime) -> None:
        self._up()
        with self._lock:
            s = self._d.sessions.get(token_hash)
            if s is not None:
                self._d.sessions[token_hash] = replace(s, last_seen_at=last_seen_at, expires_at=expires_at)

    def revoke_session(self, token_hash: str, at: datetime) -> None:
        self._up()
        with self._lock:
            s = self._d.sessions.get(token_hash)
            if s is not None and s.revoked_at is None:
                self._d.sessions[token_hash] = replace(s, revoked_at=at)

    # ----------------------------------------------------------------- rate limits

    def count_attempts(self, subjects: Sequence[str], since: datetime) -> dict[str, int]:
        self._up()
        with self._lock:
            return {s: sum(1 for sub, at, _ in self._d.attempts if sub == s and at >= since) for s in subjects}

    def record_attempt(self, subjects: Sequence[str], at: datetime, succeeded: bool) -> None:
        self._up()
        with self._lock:
            cutoff = at - timedelta(days=1)
            self._d.attempts = [a for a in self._d.attempts if a[1] >= cutoff]
            self._d.attempts += [(s, at, succeeded) for s in subjects]

    # ----------------------------------------------------------------- events

    @staticmethod
    def _check_chain(tenant_id: str, existing: list[StoredEvent], new: Sequence[StoredEvent]) -> None:
        from .events import chain_hash, genesis_hash

        head = existing[-1] if existing else None
        for event in new:
            want_seq = head.seq + 1 if head else 1
            want_prev = head.hash if head else genesis_hash(tenant_id)
            if event.seq != want_seq or event.prev_hash != want_prev:
                raise SeqConflict(f"event {event.seq} does not extend the head")
            if head is not None and event.at < head.at:
                raise SeqConflict(f"event {event.seq} is dated before the head")
            if chain_hash(event.prev_hash, event.body) != event.hash:
                raise StoreError(f"event {event.seq} hash does not match its body")
            head = event

    def append_events(self, tenant_id: str, events: Sequence[StoredEvent], *,
                      index: Sequence[IndexOp] = ()) -> None:
        self._up()
        with self._lock:
            if tenant_id not in self._d.tenants:
                raise StoreError("unknown tenant")
            existing = self._d.events.setdefault(tenant_id, [])
            self._check_chain(tenant_id, existing, events)
            for op in index:
                if op.op == "add_api_key":
                    if op.key_hash in self._d.api_keys:
                        raise StoreError("duplicate api key")
                    self._d.api_keys[op.key_hash] = (tenant_id, op.key_id)
                elif op.op == "remove_api_key":
                    for h, (t, k) in list(self._d.api_keys.items()):
                        if t == tenant_id and k == op.key_id:
                            del self._d.api_keys[h]
                else:
                    raise StoreError(f"unknown index op {op.op}")
            existing.extend(events)

    def events(self, tenant_id: str, after_seq: int = 0) -> list[StoredEvent]:
        self._up()
        with self._lock:
            return [e for e in self._d.events.get(tenant_id, []) if e.seq > after_seq]

    def tamper(self, tenant_id: str, seq: int, event: StoredEvent) -> None:
        """Test helper: overwrite one stored event (what the database refuses to do)."""
        with self._lock:
            rows = self._d.events[tenant_id]
            rows[[e.seq for e in rows].index(seq)] = event

    # ----------------------------------------------------------------- devices

    def save_device(self, device: Device) -> None:
        self._up()
        with self._lock:
            old = self._d.devices.get((device.tenant_id, device.token))
            self._d.devices[(device.tenant_id, device.token)] = (
                replace(device, created_at=old.created_at) if old else device)

    def remove_device(self, tenant_id: str, user_id: str, token: str) -> bool:
        self._up()
        with self._lock:
            d = self._d.devices.get((tenant_id, token))
            if d is None or d.user_id != user_id:
                return False
            del self._d.devices[(tenant_id, token)]
            return True

    def forget_device(self, tenant_id: str, token: str) -> None:
        with self._lock:
            self._d.devices.pop((tenant_id, token), None)

    def owner_devices(self, tenant_id: str) -> list[Device]:
        self._up()
        with self._lock:
            owners = {u for t, u, r in self._d.memberships if t == tenant_id and r in ("owner", "admin")}
            return sorted((d for (t, _), d in self._d.devices.items() if t == tenant_id and d.user_id in owners),
                          key=lambda d: d.token)

    def devices(self, tenant_id: str) -> list[Device]:
        with self._lock:
            return sorted((d for (t, _), d in self._d.devices.items() if t == tenant_id), key=lambda d: d.token)

    # ----------------------------------------------------------------- api keys, nonces

    def api_key_tenant(self, key_hash: str) -> str | None:
        self._up()
        found = self._d.api_keys.get(key_hash)
        return found[0] if found else None

    def save_nonce(self, tenant_id: str, nonce: str, expires_at: datetime) -> None:
        self._up()
        with self._lock:
            self._d.nonces[(tenant_id, nonce)] = expires_at

    def take_nonce(self, tenant_id: str, nonce: str, now: datetime) -> bool:
        self._up()
        with self._lock:
            expires = self._d.nonces.pop((tenant_id, nonce), None)
            return expires is not None and expires > now

    # ----------------------------------------------------------------- erasure

    def erase_account(self, tenant_id: str, user_id: str, at: datetime) -> int:
        self._up()
        with self._lock:
            events = len(self._d.events.pop(tenant_id, []))
            self._d.erasures[tenant_id] = _Erasure(requested_at=at, completed_at=at, events_erased=events)
            self._d.memberships = {m for m in self._d.memberships if m[0] != tenant_id}
            self._d.membership_companies = {k: v for k, v in self._d.membership_companies.items()
                                            if k[0] != tenant_id}
            self._d.invitations = {h: (replace(i, accepted_tenant_id=None) if i.accepted_tenant_id == tenant_id
                                       else i)
                                   for h, i in self._d.invitations.items() if i.inviter_tenant_id != tenant_id}
            self._d.sessions = {h: s for h, s in self._d.sessions.items() if s.tenant_id != tenant_id}
            self._d.devices = {k: d for k, d in self._d.devices.items() if k[0] != tenant_id}
            self._d.api_keys = {h: v for h, v in self._d.api_keys.items() if v[0] != tenant_id}
            self._d.nonces = {k: v for k, v in self._d.nonces.items() if k[0] != tenant_id}
            self._d.tenants.pop(tenant_id, None)
            if not any(u == user_id for _, u, _ in self._d.memberships):
                self._d.invitations = {h: i for h, i in self._d.invitations.items() if i.inviter_user_id != user_id}
                self._d.users.pop(user_id, None)
                self._d.credentials.pop(user_id, None)
                self._d.sessions = {h: s for h, s in self._d.sessions.items() if s.user_id != user_id}
            return events

    def tenant_ids(self) -> list[str]:
        self._up()
        with self._lock:
            return sorted(self._d.tenants)

    def pending_erasures(self) -> list[str]:
        self._up()
        with self._lock:
            return sorted(t for t, e in self._d.erasures.items()
                          if e.completed_at is not None and e.objects_purged_at is None)

    def mark_objects_purged(self, tenant_id: str, at: datetime) -> None:
        with self._lock:
            if tenant_id in self._d.erasures:
                self._d.erasures[tenant_id].objects_purged_at = at

    def erasure(self, tenant_id: str) -> _Erasure | None:
        return self._d.erasures.get(tenant_id)

    # ----------------------------------------------------------------- introspection for tests

    def user_ids(self) -> list[str]:
        return sorted(self._d.users)

    def all_attempts(self) -> Iterable[tuple[str, datetime, bool]]:
        return tuple(self._d.attempts)
