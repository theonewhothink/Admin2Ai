"""Event-sourced tenants: every change is recorded, then applied; loading replays the record.

A tenant's back office is the same :class:`~backoffice.service.BackOfficeService`
the demo uses, built empty (:meth:`~backoffice.service.BackOfficeService.new_tenant`)
and fed only through events. :class:`TenantManager` keeps loaded tenants in an
in-process cache, one lock per tenant:

* **Writes** append the event to the store first, then apply it. The event
  carries its time, its author, a digest of the state it was applied to, and
  whatever the live apply needs that a replay cannot recompute (whether a
  mailer or a vault was there, a new API key's fingerprint). File bytes go to
  the object store; the event keeps their key and SHA-256.
* **Reads** run against the cached state with the clock peeking at "now"
  (today's date and greeting), without changing anything.
* **Loading** replays the events through the same code paths with the clock
  set to each event's time and ids derived from the event
  (:func:`backoffice.domain.models.deterministic`), checking the state digest
  before every event. A replay that diverges is refused, never served.
* **Several API processes** share the store: before every request a process
  catches up with events others appended; a write that loses the race to
  append catches up and tries again.

Side effects (email, push notifications, stored secrets) happen only live:
a replay has a vault that stores nothing and a mailer that sends nothing.
Nothing external runs while an event applies: PDFs and photos are read (OCR,
Claude vision) before the event is recorded and the event keeps the full
reading (server/reads.py); invoice links are opened before the event is
recorded and the event keeps what came back, the bytes in the object store
(server/links.py); the chat model's tool calls are events of their own; OAuth
and bank calls happen in the HTTP handlers and the sync worker.

Reads are checked: a read that changes a tenant's state digest is logged and
the tenant is dropped from the cache (rebuilt from its log on the next
request); with ``strict_reads`` (tests) it raises :class:`ReadChangedState`.
"""

from __future__ import annotations

import base64
import binascii
import logging
import re
import threading
from collections import OrderedDict
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from backoffice.billing import HOLDABLE_PATHS
from backoffice.domain.models import deterministic
from backoffice.orchestrator import TZ
from backoffice.service import BackOfficeService, ServiceError

from . import events as _events
from .events import (
    FILE_FIELDS,
    OBJECT,
    SECRET_FIELDS,
    Event,
    EventError,
    genesis_hash,
    restore_files,
    sanitize_body,
    state_digest,
)
from .links import INGEST_PATHS, RecordedLinks, links_in_files, links_in_share, pre_fetch
from .reads import RecordedReader, pre_read
from .store import IndexOp, SeqConflict, Store, StoreUnavailable

__all__ = [
    "Env",
    "HISTORY_CHARS",
    "HISTORY_TURNS",
    "READ_ONLY_POSTS",
    "ReadChangedState",
    "ReplayDiverged",
    "TenantManager",
    "TenantNotFound",
    "TenantRuntime",
    "bank_row",
    "clean_history",
]

log = logging.getLogger("backoffice.server")

# POST routes that only read (a body carries their filters). /api/ask is not one: every answer
# writes an audit record (the ask agent's answer_question), so it is recorded as an event.
READ_ONLY_POSTS = frozenset({"/api/documents/export"})
_API_KEYS = "/api/accountant/api-keys"
_REVOKE = re.compile(r"^/api/accountant/api-keys/([^/]+)/revoke$")
_RECONNECT = re.compile(r"^/api/connections/([^/]+)/reconnect$")
_SERVICE_CODES = {400: "bad_request", 404: "not_found", 409: "conflict", 413: "too_large", 415: "unsupported"}


class TenantNotFound(LookupError):
    pass


class ReplayDiverged(RuntimeError):
    """Replaying a tenant's events did not rebuild the state they were recorded against."""

    def __init__(self, tenant_id: str, seq: int, reason: str) -> None:
        super().__init__(f"tenant {tenant_id}: replay diverged at event {seq}: {reason}")
        self.tenant_id = tenant_id
        self.seq = seq
        self.reason = reason


class ReadChangedState(AssertionError):
    """A read changed a tenant's state (strict mode, tests): reads must never change anything."""


class _Reload(Exception):
    """An event this process already applied was voided elsewhere: rebuild from the log."""


# --------------------------------------------------------------------------- environments


class _ReplayVault:
    """The vault during a replay: secrets were stored live, so storing and deleting do nothing."""

    def store(self, *args: Any, **kwargs: Any) -> None:
        return None

    def delete(self, *args: Any, **kwargs: Any) -> bool:
        return False

    def open(self, tenant_id: str, connection_id: str) -> dict[str, Any]:
        from backoffice.connectors.vault import CredentialNotFound

        raise CredentialNotFound(connection_id)

    def has(self, *args: Any) -> bool:
        return False


class _NullMailer:
    """The mailer during a replay: the email already went out live."""

    def send(self, *args: Any, **kwargs: Any) -> None:
        return None


class _FixedAuthorizer:
    """Hands the engine a consent URL obtained before the event was recorded."""

    def __init__(self, url: str) -> None:
        self.url = url

    def begin(self, *args: Any, **kwargs: Any) -> str:
        return self.url


@dataclass
class Env:
    """What one apply may touch besides the tenant's state."""

    live: bool
    vault: Any = None
    mailer: Any = None
    authorizer: Any = None
    cards: list[dict[str, Any]] = field(default_factory=list)
    secret: str | None = None  # a new accountant API key (live only)
    secret_fields: dict[str, Any] = field(default_factory=dict)  # e.g. an IMAP app password (live only)
    files: dict[str, bytes] = field(default_factory=dict)  # bytes just stored, by sha256 (live only)


def _replay_env(facts: Mapping[str, Any]) -> Env:
    return Env(
        live=False,
        vault=_ReplayVault() if facts.get("vault") else None,
        mailer=_NullMailer() if facts.get("mailer") else None,
        authorizer=_FixedAuthorizer("https://sign-in.invalid/replayed") if facts.get("authorize") else None,
    )


# --------------------------------------------------------------------------- one tenant


class TenantRuntime:
    """One tenant's back office in memory, and how far into its event log it is."""

    def __init__(self, tenant_id: str) -> None:
        self.tenant_id = tenant_id
        self.svc: BackOfficeService | None = None
        self.seq = 0
        self.head_hash = genesis_hash(tenant_id)

    def replace_with(self, other: TenantRuntime) -> None:
        self.svc, self.seq, self.head_hash = other.svc, other.seq, other.head_hash

    @property
    def service(self) -> BackOfficeService:
        if self.svc is None:
            raise TenantNotFound(self.tenant_id)
        return self.svc

    def digest(self) -> str | None:
        return state_digest(self.svc) if self.svc is not None else None


HISTORY_TURNS = 10  # the chat history a brain is given and an event records
HISTORY_CHARS = 4000  # per turn
SEND_RETRY_AFTER = timedelta(minutes=10)  # after the mailer refused an email, before trying again


def clean_history(history: Any) -> list[dict[str, str]]:
    """The last text turns of a chat, as the brain uses them and the event records them."""
    if not isinstance(history, list):
        return []
    turns = [{"role": h["role"], "content": h["content"][:HISTORY_CHARS]} for h in history
             if isinstance(h, Mapping) and h.get("role") in ("user", "assistant") and isinstance(h.get("content"), str)]
    return turns[-HISTORY_TURNS:]


def _uploads(body: Mapping[str, Any], env: Env) -> list[tuple[Any, ...]]:
    """The files a request body carries (bytes just stored, file name, declared type, the quality the phone
    noticed when it took the photo), for reading first."""
    from backoffice.service import capture_fields

    out: list[tuple[Any, ...]] = []
    hints = tuple(capture_fields(body).get("quality") or ())
    for name in FILE_FIELDS:
        ref = body.get(name)
        if isinstance(ref, Mapping) and isinstance(ref.get(OBJECT), Mapping):
            data = env.files.get(str(ref[OBJECT].get("sha256", "")))
            if data is not None:
                mime = body.get("contentType") or body.get("content_type") or body.get("mimeType") or \
                    body.get("mime_type")
                out.append((data, _text_or_none(body.get("filename")), _text_or_none(mime), hints))
    return out


def _text_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def bank_row(data: Mapping[str, Any]) -> Any:
    """A recorded bank row (BankRow.to_json) back as the engine's BankRow."""
    from decimal import Decimal

    from backoffice.domain.models import TransactionKind
    from backoffice.orchestrator import BankRow

    return BankRow(bank_id=str(data["bank_id"]), account_id=str(data["account_id"]),
                   booked_on=date.fromisoformat(str(data["booked_on"])), amount=Decimal(str(data["amount"])),
                   counterparty=str(data.get("counterparty") or ""), description=str(data.get("description") or ""),
                   kind=TransactionKind(str(data.get("kind") or "card")), card_last4=data.get("card_last4"),
                   counterparty_iban=data.get("counterparty_iban"), reference=data.get("reference"),
                   currency=str(data.get("currency") or "EUR"), cardholder=data.get("cardholder") or None)


def _error(exc: ServiceError) -> tuple[int, dict[str, Any]]:
    return exc.status, {"error": _SERVICE_CODES.get(exc.status, "error"), "message": exc.message}


# --------------------------------------------------------------------------- the manager


class TenantManager:
    """Loads, caches, locks and changes tenants through their event logs."""

    def __init__(
        self,
        store: Store,
        objects: Any,
        *,
        now: Callable[[], datetime] | None = None,
        vault: Any = None,
        authorizer: Any = None,
        mailer: Any = None,
        brain_factory: Callable[[BackOfficeService], Any] | None = None,
        notifier: Any = None,
        reader: Any = None,
        link_fetcher: Any = None,
        billing: Any = None,
        company_lookup: Any = None,
        cache_size: int = 200,
        strict_reads: bool = False,
    ) -> None:
        self.store = store
        self.objects = objects
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.vault = vault
        self.authorizer = authorizer
        self.mailer = mailer
        self.brain_factory = brain_factory
        self.notifier = notifier
        # The live document reader (backoffice.reading.reader_from_env). It only ever runs before an
        # event is recorded (server/reads.py); applies read through the recorded outcomes.
        self.reader = reader
        # The live link fetcher (backoffice.evidence.links.LinkFetcher). Like the reader it only ever runs
        # before an event is recorded (server/links.py); applies follow links through the recorded results.
        self.link_fetcher = link_fetcher
        # The payment provider (server/billing.py), or None. With one, new businesses are held to their plan
        # (backoffice.billing): evidence over a limit, after the grace period, waits instead of being read.
        self.billing = billing
        # The EU VAT register (backoffice.company_lookup.ViesClient), or None. Like the reader it only ever runs
        # before an event is recorded: a company's details are looked up, then the event keeps the answer, and
        # applying it (live or on replay) reads the answer, never the register.
        self.company_lookup = company_lookup
        self.cache_size = cache_size
        # Reads must never change a tenant. In tests a read that does raises; in production it is
        # logged and the tenant is rebuilt from its log on the next request.
        self.strict_reads = strict_reads
        self._cache: OrderedDict[str, TenantRuntime] = OrderedDict()
        self._locks: dict[str, threading.RLock] = {}
        self._guard = threading.Lock()
        self._refused: dict[str, ReplayDiverged] = {}
        self._send_retry: dict[str, datetime] = {}  # tenant -> when to try its waiting emails again

    # ----------------------------------------------------------------- time, files

    def now(self) -> datetime:
        now = self._now()
        if now.tzinfo is None:
            raise ValueError("the clock needs a timezone-aware time")
        return now.astimezone(TZ)

    def put_file(self, tenant_id: str, data: bytes, env: Env | None = None) -> dict[str, Any]:
        from backoffice.evidence.store import parse_key

        key = self.objects.put_immutable(bytes(data), tenant_id, "application/octet-stream")
        _, digest = parse_key(key)
        if env is not None:
            env.files[digest] = bytes(data)
        return {"key": key, "sha256": digest, "size": len(data)}

    def get_file(self, tenant_id: str, ref: Mapping[str, Any], env: Env | None = None) -> bytes:
        from backoffice.evidence.store import IntegrityError, parse_key

        digest = str(ref.get("sha256", ""))
        if env is not None and digest in env.files:
            return env.files[digest]
        key = str(ref.get("key", ""))
        owner, key_digest = parse_key(key)
        if owner != tenant_id or key_digest != digest:
            raise IntegrityError("an event points at another tenant's file")
        return self.objects.get(key)

    # ----------------------------------------------------------------- cache and locks

    def _lock_for(self, tenant_id: str) -> threading.RLock:
        with self._guard:
            lock = self._locks.get(tenant_id)
            if lock is None:
                lock = self._locks[tenant_id] = threading.RLock()
            return lock

    def _remember(self, rt: TenantRuntime) -> None:
        with self._guard:
            self._cache[rt.tenant_id] = rt
            self._cache.move_to_end(rt.tenant_id)
            while len(self._cache) > self.cache_size:
                oldest = next(iter(self._cache))
                if oldest == rt.tenant_id:
                    break
                lock = self._locks.get(oldest)
                if lock is not None and not lock.acquire(blocking=False):
                    self._cache.move_to_end(oldest)  # in use: keep it, evict the next one later
                    break
                try:
                    self._cache.pop(oldest, None)
                finally:
                    if lock is not None:
                        lock.release()

    def evict(self, tenant_id: str) -> None:
        """Drop a tenant from the cache; the next request rebuilds it from its events."""
        with self._lock_for(tenant_id):
            with self._guard:
                self._cache.pop(tenant_id, None)
                self._refused.pop(tenant_id, None)

    def cached(self, tenant_id: str) -> bool:
        return tenant_id in self._cache

    @contextmanager
    def open(self, tenant_id: str) -> Iterator[TenantRuntime]:
        """The tenant, up to date with its log, locked for the duration of the block."""
        with self._lock_for(tenant_id):
            refused = self._refused.get(tenant_id)
            if refused is not None:
                raise refused
            rt = self._cache.get(tenant_id)
            try:
                if rt is None:
                    rt = self._load(tenant_id)
                else:
                    self._catch_up(rt)
            except _Reload:
                rt = self._load(tenant_id)
            except ReplayDiverged as err:
                self._refuse(err)
                raise
            self._remember(rt)
            yield rt

    def _refuse(self, err: ReplayDiverged) -> None:
        log.critical("replay_diverged", extra={"tenant": err.tenant_id, "seq": err.seq, "reason": err.reason})
        with self._guard:
            self._cache.pop(err.tenant_id, None)
            self._refused[err.tenant_id] = err

    # ----------------------------------------------------------------- loading and replay

    def _load(self, tenant_id: str) -> TenantRuntime:
        rows = self.store.events(tenant_id)
        if not rows:
            raise TenantNotFound(tenant_id)
        rt = TenantRuntime(tenant_id)
        try:
            self._replay(rt, rows)
        except ReplayDiverged as err:
            self._refuse(err)
            raise
        return rt

    def _catch_up(self, rt: TenantRuntime) -> None:
        rows = self.store.events(rt.tenant_id, after_seq=rt.seq)
        if rows:
            self._replay(rt, rows)

    def _replay(self, rt: TenantRuntime, rows: Sequence[Any]) -> None:
        try:
            events = [Event.parse(r) for r in rows]
        except EventError as exc:
            raise ReplayDiverged(rt.tenant_id, rows[0].seq, str(exc)) from None
        voided = {int(e.data.get("seq", 0)) for e in events if e.kind == "void"}
        if any(0 < s <= rt.seq for s in voided):
            raise _Reload()
        for event in events:
            self._replay_one(rt, event, voided)

    def _replay_one(self, rt: TenantRuntime, event: Event, voided: set[int]) -> None:
        if event.seq != rt.seq + 1 or event.prev_hash != rt.head_hash:
            raise ReplayDiverged(rt.tenant_id, event.seq, "the event log has a gap or does not chain")
        if event.kind == "void" or event.seq in voided:
            rt.seq, rt.head_hash = event.seq, event.hash
            return
        if rt.svc is None and event.kind != "tenant.created":
            raise ReplayDiverged(rt.tenant_id, event.seq, "the log does not start with the tenant")
        if rt.svc is not None:
            rt.svc.repo.clock.advance_to(event.at)
            try:  # compared with the digest version the event was recorded with (events.DIGESTS)
                digest = state_digest(rt.svc, event.digest_version)
            except EventError:
                raise ReplayDiverged(rt.tenant_id, event.seq,
                                     f"recorded with state digest version {event.digest_version}, "
                                     "which this build does not know") from None
            if event.pre != digest:
                raise ReplayDiverged(rt.tenant_id, event.seq, "state before the event differs from when it was recorded")
        try:
            self._apply(rt, event, _replay_env(event.data.get("env") or {}))
        except ReplayDiverged:
            raise
        except Exception as exc:  # a recorded event that no longer applies is divergence
            raise ReplayDiverged(rt.tenant_id, event.seq, f"applying failed: {type(exc).__name__}") from exc
        rt.seq, rt.head_hash = event.seq, event.hash

    # ----------------------------------------------------------------- recording

    def _event_time(self, rt: TenantRuntime) -> datetime:
        at = self.now()
        if rt.svc is not None:
            at = max(at, rt.svc.repo.clock.now())
        return at

    def record(self, rt: TenantRuntime, kind: str, data: Mapping[str, Any], actor: str, env: Env,
               *, index: Sequence[IndexOp] = ()) -> tuple[int, dict[str, Any]]:
        """Append one event, then apply it live. Returns the apply's ``(status, body)``."""
        event: Event | None = None
        version = _events.DIGEST_VERSION  # the digest this event records, and computes ``pre`` with
        for _ in range(5):
            at = self._event_time(rt)
            pre = None
            if rt.svc is not None:
                rt.svc.repo.clock.advance_to(at)
                pre = state_digest(rt.svc, version)
            candidate = Event.make(seq=rt.seq + 1, at=at, kind=kind, actor=actor, data=data, pre=pre,
                                   prev_hash=rt.head_hash, digest_version=version)
            try:
                self.store.append_events(rt.tenant_id, [candidate.stored()], index=index)
            except SeqConflict:
                try:
                    self._catch_up(rt)
                except _Reload:
                    rt.replace_with(self._load(rt.tenant_id))
                continue
            event = candidate
            break
        if event is None:
            raise StoreUnavailable("could not append to the tenant log")
        rt.seq, rt.head_hash = event.seq, event.hash
        before = self.notifier.facts(rt.svc) if self.notifier is not None and rt.svc is not None else None
        try:
            result = self._apply(rt, event, env)
        except Exception:
            self._void(rt, event)
            raise
        if self.notifier is not None and before is not None and rt.svc is not None:
            try:
                self.notifier.changed(rt.tenant_id, rt.svc, before, self.notifier.facts(rt.svc))
            except Exception:  # a notification never undoes a recorded change
                log.exception("notification_failed")
        if env.live and kind not in ("outbox.send", "void"):
            self._deliver(rt)
        return result

    def _deliver(self, rt: TenantRuntime) -> None:
        """Send the emails the tenant wrote and could not send inside a change (§22, §25, §28).

        Each goes out in an event of its own, applied with the live mailer: when the mailer refuses,
        that event alone is void and the email stays "waiting to be sent" (retried after a pause). Without
        a mailer nothing is sent, and every screen keeps saying so.
        """
        if self.mailer is None or rt.svc is None:
            return
        retry = self._send_retry.get(rt.tenant_id)
        if retry is not None and retry > self.now():
            return
        for message_id in rt.svc.waiting_messages():
            try:
                self.record(rt, "outbox.send", {"id": message_id, "env": self._facts()}, "system:mailer",
                            self.live_env())
            except Exception:
                log.warning("send_failed", extra={"tenant": rt.tenant_id})
                self._send_retry[rt.tenant_id] = self.now() + SEND_RETRY_AFTER
                return
        self._send_retry.pop(rt.tenant_id, None)

    def _void(self, rt: TenantRuntime, failed: Event) -> None:
        """The live apply of ``failed`` broke: mark it void and rebuild the tenant without it."""
        try:
            void = Event.make(seq=failed.seq + 1, at=max(self.now(), failed.at), kind="void", actor="system",
                              data={"seq": failed.seq}, pre=None, prev_hash=failed.hash)
            self.store.append_events(rt.tenant_id, [void.stored()])
        except Exception:
            log.exception("void_failed", extra={"tenant": rt.tenant_id, "seq": failed.seq})
        with self._guard:
            self._cache.pop(rt.tenant_id, None)

    # ----------------------------------------------------------------- applying

    def _apply(self, rt: TenantRuntime, event: Event, env: Env) -> tuple[int, dict[str, Any]]:
        handler = _HANDLERS.get(event.kind)
        if handler is None:
            raise EventError(f"unknown event kind {event.kind}")
        if event.kind == "tenant.created":
            return handler(self, rt, event, env)
        svc = rt.service
        svc.vault, svc.authorizer, svc.mailer = env.vault, env.authorizer, env.mailer
        # Files are never read while an event applies: live and on replay, the engine reads through
        # the outcomes recorded in the event (server/reads.py).
        facts = event.data.get("env") if isinstance(event.data.get("env"), Mapping) else {}
        svc.repo.reader = RecordedReader.from_event(event.data.get("reads")) if facts.get("reader") else None
        # Links are never opened while an event applies: live and on replay, the engine follows them through
        # what was fetched before the event was recorded (server/links.py); anything else waits.
        svc.repo.links = RecordedLinks(event.data.get("links"), lambda ref: self.get_file(rt.tenant_id, ref, env))
        clock = svc.repo.clock
        with deterministic(f"{rt.tenant_id}:{event.seq}", clock.now):
            clock.advance_to(event.at)
            try:
                result = handler(self, rt, event, env)
            except ServiceError as exc:
                result = _error(exc)
            # A sign-in that ends within a week is noted once, by the change that finds it (checklist R4): the
            # same on replay, and the push goes out when it is first noted live (server/notify.py).
            svc.warn_expiring()
            return result

    # ----------------------------------------------------------------- creating tenants

    def build_tenant(self, tenant_id: str, *, owner_name: str, owner_email: str, company_name: str,
                     tax_id: str | None, actor: str) -> tuple[TenantRuntime, list[Event]]:
        """A new tenant and its first events, not yet stored (sign-up stores them with the account)."""
        rt = TenantRuntime(tenant_id)
        events: list[Event] = []
        env = Env(live=True, vault=self.vault)
        created: dict[str, Any] = {"owner": {"name": owner_name, "email": owner_email}}
        if self.billing is not None:  # recorded only with payments: older logs replay unchanged
            created["billing"] = {"enforced": True}
        company: dict[str, Any] = {"name": company_name, "taxId": tax_id or "", "legalName": ""}
        found = self.lookup_company(tax_id, "PT")
        if found is not None:  # recorded only with a register: older logs replay unchanged
            company["lookup"] = found
        for kind, data in (("tenant.created", created), ("company.added", company)):
            at = self._event_time(rt)
            if rt.svc is not None:
                rt.svc.repo.clock.advance_to(at)
            version = _events.DIGEST_VERSION
            pre = state_digest(rt.svc, version) if rt.svc is not None else None
            event = Event.make(seq=rt.seq + 1, at=at, kind=kind, actor=actor, data=data, pre=pre,
                               prev_hash=rt.head_hash, digest_version=version)
            status, body = self._apply(rt, event, env)
            if status >= 400:
                raise ServiceError(status, body.get("message", "That did not work."))
            rt.seq, rt.head_hash = event.seq, event.hash
            events.append(event)
        return rt, events

    def install(self, rt: TenantRuntime) -> None:
        """Cache a tenant whose first events are stored."""
        with self._lock_for(rt.tenant_id):
            self._remember(rt)

    # ----------------------------------------------------------------- the time-driven day

    def tick_if_due(self, rt: TenantRuntime) -> None:
        """Once a day, on the first request, run the time-driven work (chasing, closing months)."""
        svc = rt.svc
        if svc is None or not svc.repo.companies:
            return
        if self.now().date() <= svc.repo.clock.today():
            return
        env = Env(live=True, vault=self.vault, mailer=self.mailer)
        # Evidence that waited for the plan is read today (a new month): read and fetched before the event.
        data = self._release_data(rt, env) if svc.release_due(self.now().date()) else {}
        try:
            self.record(rt, "tick", data, "system", env)
        except StoreUnavailable:
            log.warning("tick_skipped", extra={"tenant": rt.tenant_id})

    # ----------------------------------------------------------------- what the API calls

    def view(self, tenant_id: str, method: str, path: str, body: Any = None, *,
             companies: frozenset[str] | None = None, prefix: str = "",
             employee: str | None = None, manager: frozenset[str] | None = None,
             viewer: tuple[str, str] | None = None) -> tuple[int, dict[str, Any]]:
        """A read: the state as of now, unchanged.

        ``companies``: the reader is an accountant limited to these companies (§28, §52): only the
        accountant's routes answer, filtered to them. ``prefix`` goes before client ids in the reply.
        ``employee``: the reader is this employee (their email): only their own card payments answer.
        ``manager``: the reader manages these cost centers (outlets): only their outlets' routes answer.
        ``viewer`` (email, role): who is reading; a sensitive document's original read here is recorded
        with it (§52), as an event of its own.
        """
        with self.open(tenant_id) as rt:
            self.tick_if_due(rt)
            what = f"{method} {path.split('?', 1)[0]}"
            if employee is not None:
                return self._guarded_read(rt, what, lambda svc: svc.dispatch_employee(method, path, body, employee))
            if manager is not None:
                return self._guarded_read(rt, what, lambda svc: svc.dispatch_manager(method, path, body, manager),
                                          viewer=viewer)
            if companies is None and not prefix:
                return self._guarded_read(rt, what, lambda svc: svc.dispatch(method, path, body), viewer=viewer)
            scope = companies if companies is not None else frozenset(rt.service.repo.companies)
            return self._guarded_read(rt, what,
                                      lambda svc: svc.dispatch_scoped(method, path, body, scope, prefix=prefix),
                                      viewer=viewer)

    def read(self, tenant_id: str, reader: Callable[[BackOfficeService], Any], *, what: str = "read",
             viewer: tuple[str, str] | None = None, today: bool = False) -> Any:
        """Run ``reader`` on the tenant's service (no change allowed) as of now. A sensitive document's original
        it reads is recorded with ``viewer`` (email, role), as an event of its own (§52). ``today``: the day's
        time-driven work runs first, as for the pages the owner opens (:meth:`view`)."""
        with self.open(tenant_id) as rt:
            if today:
                self.tick_if_due(rt)
            return self._guarded_read(rt, what, reader, viewer=viewer)

    def read_many(self, tenant_ids: Sequence[str], reader: Callable[[list[BackOfficeService]], Any], *,
                  what: str = "read") -> Any:
        """Run ``reader`` over several tenants at once (the internal dashboard), each locked and as of now.

        Locks are taken in tenant-id order; every other path holds one tenant lock at a time, so
        this cannot deadlock. A tenant that cannot be opened is left out.
        """
        from contextlib import ExitStack

        now = self.now()
        with ExitStack() as stack:
            opened: list[TenantRuntime] = []
            for tenant_id in sorted(set(tenant_ids)):
                try:
                    opened.append(stack.enter_context(self.open(tenant_id)))
                except (TenantNotFound, ReplayDiverged):
                    continue
            services = [rt.service for rt in opened]
            before = [state_digest(svc) for svc in services]
            for svc in services:
                stack.enter_context(svc.repo.clock.peek(now))
            result = reader(services)
        for rt, digest in zip(opened, before, strict=True):
            self._check_unchanged(rt, digest, what)
        return result

    def _guarded_read(self, rt: TenantRuntime, what: str, reader: Callable[[BackOfficeService], Any], *,
                      viewer: tuple[str, str] | None = None) -> Any:
        svc = rt.service
        before = state_digest(svc)
        svc.take_opened()
        with svc.repo.clock.peek(self.now()):
            result = reader(svc)
        opened = svc.take_opened()
        self._check_unchanged(rt, before, what)
        if opened:
            # Every read of a sensitive document's original is on the record (§52): who, when, which.
            who, role = viewer or ("unknown", "unknown")
            try:
                self.record(rt, "documents.opened", {"documentIds": opened, "who": who, "role": role},
                            f"access:{role}", self.live_env())
            except StoreUnavailable:
                log.warning("access_not_recorded", extra={"tenant": rt.tenant_id})
                raise
        return result

    def _check_unchanged(self, rt: TenantRuntime, before: str, what: str) -> None:
        """A read changed the tenant: that change is in no event, so drop it (rebuild from the log)."""
        if rt.svc is None or state_digest(rt.svc) == before:
            return
        log.error("read_changed_state", extra={"tenant": rt.tenant_id, "route": what})
        with self._guard:
            self._cache.pop(rt.tenant_id, None)
        if self.strict_reads:
            raise ReadChangedState(f"{what} changed tenant {rt.tenant_id}")

    def live_env(self, **kwargs: Any) -> Env:
        return Env(live=True, vault=self.vault, mailer=self.mailer, **kwargs)

    def _facts(self, **extra: Any) -> dict[str, Any]:
        return {"vault": self.vault is not None, "mailer": self.mailer is not None,
                "reader": self.reader is not None, **extra}

    def command(self, tenant_id: str, actor: str, method: str, path: str,
                body: Mapping[str, Any] | None, *, manager: frozenset[str] | None = None,
                manager_email: str | None = None) -> tuple[int, dict[str, Any]]:
        """A change requested through the API: recorded as one event, then applied. ``manager``: an outlet
        manager's change, applied (live and on replay) through their outlets' routes only."""
        body = dict(body or {})
        if method == "POST" and path == _API_KEYS:
            return self.create_api_key(tenant_id, actor, body)
        with self.open(tenant_id) as rt:
            self.tick_if_due(rt)
            env = self.live_env()
            authorize = self._authorize_url(rt, path, body)
            env.authorizer = _FixedAuthorizer(authorize) if authorize else None
            try:
                clean = sanitize_body(body, lambda data: self.put_file(tenant_id, data, env))
            except ValueError:
                return 400, {"error": "bad_request", "message": "I couldn't read that file."}
            env.secret_fields = {k: body[k] for k in SECRET_FIELDS if isinstance(body.get(k), str)}
            index: list[IndexOp] = []
            revoke = _REVOKE.match(path)
            if method == "POST" and revoke:
                index.append(IndexOp("remove_api_key", revoke.group(1)))
            data: dict[str, Any] = {"method": method, "path": path, "body": clean,
                                    "env": self._facts(authorize=authorize is not None)}
            if manager is not None:  # recorded only for a manager: older events replay unchanged
                data["manager"] = {"costCenters": sorted(manager), "email": manager_email or ""}
            if method == "POST" and path in HOLDABLE_PATHS and self.billing is not None and \
                    rt.service.intake_held(self.now().date()):
                # Over the plan after its grace period (backoffice.billing): kept, not read or fetched now; read
                # when the plan covers it. Recorded, so every replay keeps it waiting too.
                data["held"] = True
                return self.record(rt, "request", data, actor, env, index=index)
            uploads = _uploads(clean, env)
            if method == "POST" and path in INGEST_PATHS:
                # Links in what arrives (a shared link, an email's invoice links) are opened now, before the
                # event: the event keeps what came back, and applying it never opens anything.
                urls = [*links_in_files(tenant_id, uploads), *(links_in_share(body) if path == "/api/share" else [])]
                links, fetched = self._fetch_links(rt, urls, env)
                if links:
                    data["links"] = links
                uploads += fetched
            reads = pre_read(rt.service, self.reader, uploads)
            if reads:
                data["reads"] = reads
            return self.record(rt, "request", data, actor, env, index=index)

    def _fetch_links(self, rt: TenantRuntime, urls: Sequence[str], env: Env
                     ) -> tuple[dict[str, Any], list[tuple[bytes, str | None, str | None]]]:
        """Open links with the live fetcher before an event is recorded (server/links.py). A link the tenant
        already followed to the end (retrieved, refused as unsafe, no longer working) is not opened again."""
        if self.link_fetcher is None or not urls:
            return {}, []
        seen = rt.service.repo.links_seen

        def settled(url: str) -> bool:
            record = seen.get(url)
            return record is not None and record.status in ("retrieved", "blocked", "broken")

        return pre_fetch(self.link_fetcher, urls, lambda data: self.put_file(rt.tenant_id, data, env), skip=settled)

    def follow_links(self, tenant_id: str, urls: Sequence[str]) -> tuple[int, dict[str, Any]]:
        """Open links that are waiting (never opened, or the site did not answer) and record what came back
        as one event (``links.fetched``); the engine then reads them like any link (the sync worker's job)."""
        with self.open(tenant_id) as rt:
            pending = set(rt.service.repo.pending_links)
            waiting = [u for u in dict.fromkeys(urls) if u in pending]
            env = self.live_env()
            links, fetched = self._fetch_links(rt, waiting, env)
            if not links:
                return 200, {"ok": True, "documents": [], "waiting": waiting}
            data: dict[str, Any] = {"urls": list(links), "links": links, "env": self._facts()}
            reads = pre_read(rt.service, self.reader, fetched)
            if reads:
                data["reads"] = reads
            return self.record(rt, "links.fetched", data, "system:links", env)

    def _authorize_url(self, rt: TenantRuntime, path: str, body: Mapping[str, Any]) -> str | None:
        """The Google/Microsoft consent URL, obtained before the event is recorded: for a new mailbox
        (``/api/sources``) or for signing in again to one that needs reconnecting (``.../reconnect``)."""
        if self.authorizer is None:
            return None
        reconnect = _RECONNECT.match(path)
        if reconnect is not None:
            svc = rt.service
            c = svc.repo.connectors.get(reconnect.group(1))
            options = dict(svc.sign_in.get(c.id) or {}) if c is not None else {}
            provider = options.get("provider")
            if c is None or c.kind != "email" or provider not in ("google", "microsoft"):
                return None
            cid, address = c.id, c.account.strip().lower()
        elif path == "/api/sources" and body.get("kind") == "email":
            provider = body.get("provider") or "google"
            address = body.get("address")
            if provider not in ("google", "microsoft") or not isinstance(address, str) or not address.strip():
                return None
            address = address.strip().lower()
            cid = rt.service._slug("mail", address)
            try:  # whose mailbox it is (a shared mailbox, a delegated one, an alias: checklist O2)
                options = BackOfficeService._mailbox_options(provider, address, body)
            except ServiceError:
                return None  # the request itself is refused when it applies
        else:
            return None
        if provider not in getattr(self.authorizer, "providers", ()):
            return None
        hint, scopes = BackOfficeService.consent_request(provider, address, options)
        try:
            return self.authorizer.begin(provider, rt.tenant_id, cid, login_hint=hint,
                                         **({"scopes": scopes} if scopes else {}))
        except Exception:
            log.warning("oauth_begin_failed", extra={"tenant": rt.tenant_id})
            return None

    def create_api_key(self, tenant_id: str, actor: str, body: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        secret, prefix, digest = BackOfficeService.new_api_key()
        name = str(body.get("name") or "Accounting system").strip()[:60]
        with self.open(tenant_id) as rt:
            self.tick_if_due(rt)
            key_id = f"key_{getattr(rt.service, '_api_key_seq', 0) + 1:03d}"
            index = [IndexOp("add_api_key", key_id, digest, self.now())]
            return self.record(rt, "api_key.created", {"name": name, "prefix": prefix, "hash": digest}, actor,
                               self.live_env(secret=secret), index=index)

    def add_company(self, tenant_id: str, actor: str, body: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        data: dict[str, Any] = {"name": str(body.get("name") or ""), "taxId": str(body.get("taxId") or ""),
                                "legalName": str(body.get("legalName") or "")}
        country = str(body.get("country") or "").strip().upper()
        if country and country != "PT":  # recorded only for another country: older events replay unchanged
            data["country"] = country
        if str(body.get("address") or "").strip():  # recorded only when given, likewise
            data["address"] = " ".join(str(body.get("address")).split())[:200]
        # The EU VAT register is asked now, before the event: the event keeps its answer (QA A2).
        found = self.lookup_company(data["taxId"], country or "PT")
        if found is not None:
            data["lookup"] = found
        with self.open(tenant_id) as rt:
            return self.record(rt, "company.added", data, actor, self.live_env())

    def lookup_company(self, tax_id: Any, country: str) -> dict[str, Any] | None:
        """The EU VAT register's answer for a VAT number, as an event records it; None without a register or for a
        number its country's pack refuses (the apply refuses it). Never raises: a register that fails is recorded
        as unavailable and the owner types the details."""
        if self.company_lookup is None or not str(tax_id or "").strip():
            return None
        from backoffice.company_lookup import lookup_company

        try:
            found = lookup_company(str(tax_id), country, self.company_lookup)
        except Exception:
            log.warning("company_lookup_failed")
            return None
        return found.to_json() if found is not None else None

    def set_accountant(self, tenant_id: str, actor: str, body: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        """The business's accountant, or (``companyId``) one company's own accountant (§28, §51)."""
        data = {"email": str(body.get("email") or ""), "name": str(body.get("name") or ""),
                "software": str(body.get("software") or "")}
        for key in ("companyId", "firm"):  # recorded only when given: older events replay unchanged
            if body.get(key):
                data[key] = str(body.get(key))
        with self.open(tenant_id) as rt:
            return self.record(rt, "accountant.set", data, actor, self.live_env())

    def finish_sign_in(self, tenant_id: str, connection_id: str, provider: str,
                       email: str | None, *, access_until: date | None = None) -> tuple[int, dict[str, Any]]:
        """A mailbox sign-in came back from Google/Microsoft with a refresh token in the vault.

        ``access_until``: the day the grant ends when the provider stated it (else what the vault recorded for it,
        connectors.authorize): the event carries it, so the owner is reminded a week before (R4)."""
        meta = self.vault.metadata(tenant_id, connection_id) if self.vault is not None else None
        if access_until is None and meta is not None and meta.expires_at is not None:
            access_until = meta.expires_at.astimezone(TZ).date()
        extra = {"accessUntil": access_until.isoformat()} if access_until is not None else {}
        with self.open(tenant_id) as rt:
            if connection_id in rt.service.repo.connectors:
                return self.record(rt, "sign_in.finished", {"connectionId": connection_id, **extra}, "system",
                                   self.live_env())
            if not email:
                return 400, {"error": "bad_request", "message": "I couldn't tell which mailbox that was."}
            status, body = self.record(rt, "email.connected", {"provider": provider, "address": email, **extra},
                                       "system", self.live_env())
            new_id = body.get("id")
            if status == 200 and isinstance(new_id, str) and self.vault is not None and new_id != connection_id:
                # The refresh token was sealed under the sign-in's temporary id: move it to the connector.
                try:
                    secret = self.vault.open(tenant_id, connection_id)
                    self.vault.store(tenant_id, new_id, provider, secret,
                                     expires_at=meta.expires_at if meta is not None else None)
                    self.vault.delete(tenant_id, connection_id)
                except Exception:
                    log.exception("vault_move_failed", extra={"tenant": tenant_id})
            return status, body

    def bank_linked(self, tenant_id: str, *, bank: str, company_id: str, ibans: Sequence[str],
                    consent_until: date | None = None) -> tuple[int, dict[str, Any]]:
        until = consent_until or (self.now() + timedelta(days=180)).date()
        with self.open(tenant_id) as rt:
            return self.record(rt, "bank.linked", {"bank": bank, "companyId": company_id, "ibans": list(ibans),
                                                   "consentUntil": until.isoformat()},
                               "system", self.live_env())

    # ----------------------------------------------------------------- synced imports (server/sync.py)

    def record_mail(self, tenant_id: str, connection_id: str, messages: Sequence[bytes],
                    state: Mapping[str, Any] | None) -> tuple[int, dict[str, Any]]:
        """Messages a mailbox sync fetched (and, on the last batch, the sync's new state) as one event."""
        with self.open(tenant_id) as rt:
            env = self.live_env()
            refs = [self.put_file(tenant_id, raw, env) for raw in messages]
            data: dict[str, Any] = {"connectionId": connection_id, "messages": [{OBJECT: ref} for ref in refs],
                                    "state": dict(state) if state is not None else None, "env": self._facts()}
            if messages and self.billing is not None and rt.service.intake_held(self.now().date()):
                data["held"] = True  # kept, read once the plan covers them (backoffice.billing)
                return self.record(rt, "sync.mail", data, "system:sync", env)
            files: list[tuple[bytes, str | None, str | None]] = [(raw, "message.eml", "message/rfc822")
                                                                  for raw in messages]
            links, fetched = self._fetch_links(rt, links_in_files(tenant_id, files), env)  # §9: before the event
            if links:
                data["links"] = links
            reads = pre_read(rt.service, self.reader, [*files, *fetched])
            if reads:
                data["reads"] = reads
            return self.record(rt, "sync.mail", data, "system:sync", env)

    def record_bank(self, tenant_id: str, connection_id: str, rows: Sequence[Mapping[str, Any]],
                    state: Mapping[str, Any] | None) -> tuple[int, dict[str, Any]]:
        """Booked transactions a bank sync fetched (BankRow JSON) and the sync's new state, as one event."""
        with self.open(tenant_id) as rt:
            data = {"connectionId": connection_id, "rows": [dict(r) for r in rows],
                    "state": dict(state) if state is not None else None}
            return self.record(rt, "sync.bank", data, "system:sync", self.live_env())

    def record_sync_failure(self, tenant_id: str, connection_id: str, state: Mapping[str, Any], *,
                            reconnect: bool) -> tuple[int, dict[str, Any]]:
        with self.open(tenant_id) as rt:
            return self.record(rt, "sync.failed", {"connectionId": connection_id, "state": dict(state),
                                                   "reconnect": bool(reconnect)}, "system:sync", self.live_env())

    def chat(self, tenant_id: str, actor: str, body: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        message = body.get("message")
        if not isinstance(message, str) or not message.strip():
            return 400, {"error": "bad_request", "message": "Write what you need."}
        if len(message) > 4000:
            return 400, {"error": "bad_request", "message": "That is too long. Try a shorter request."}
        history = clean_history(body.get("history"))
        with self.open(tenant_id) as rt:
            self.tick_if_due(rt)
            if self.brain_factory is not None:
                brain = self.brain_factory(rt.service)
                brain._run = lambda name, args, cards: self._chat_tool(rt, actor, name, args, cards)
                try:
                    return 200, brain.handle(message, history)
                except ValueError as exc:
                    return 400, {"error": "bad_request", "message": str(exc)}
                except (ReplayDiverged, StoreUnavailable):
                    raise
                except Exception:  # the model is unavailable: the rules answer instead
                    log.warning("chat_model_unavailable", extra={"tenant": tenant_id})
            return self.record(rt, "chat.rule", {"message": message, "history": history}, actor, self.live_env())

    def _chat_tool(self, rt: TenantRuntime, actor: str, name: str, args: dict[str, Any],
                   cards: list[dict[str, Any]]) -> Any:
        from backoffice.assistant import CHANGING_TOOLS, run_tool

        if name not in CHANGING_TOOLS:
            return self._guarded_read(rt, f"chat tool {name}",
                                      lambda svc: run_tool(svc.assistant, name, args, cards))
        _, body = self.record(rt, "chat.tool", {"name": name, "input": dict(args)}, actor,
                              self.live_env(cards=cards))
        if body.get("isError"):
            raise ValueError(str(body.get("result") or "That did not work."))
        return body.get("result")

    # ----------------------------------------------------------------- the plan (backoffice.billing)

    def _release_data(self, rt: TenantRuntime, env: Env) -> dict[str, Any]:
        """What an event that reads the evidence waiting for the plan must carry: the files read and the links
        fetched now, before it is recorded (as for any upload), so applying it never reads or fetches anything."""
        from backoffice.service import capture_fields

        svc = rt.service
        files: list[tuple[Any, ...]] = []
        urls: list[str] = []
        for item in svc.billing.waiting:
            if item.get("kind") == "mail":
                files += [(bytes(raw), "message.eml", "message/rfc822") for raw in item.get("messages") or ()]
                continue
            body = item.get("body") or {}
            raw = body.get("dataBase64") or body.get("data_base64")
            if isinstance(raw, str) and raw:
                try:
                    blob = base64.b64decode(raw, validate=False)
                except (binascii.Error, ValueError):
                    blob = b""
                if blob:
                    mime = body.get("contentType") or body.get("content_type") or body.get("mimeType") or \
                        body.get("mime_type")
                    files.append((blob, _text_or_none(body.get("filename")), _text_or_none(mime),
                                  tuple(capture_fields(body).get("quality") or ())))
            if item.get("path") == "/api/share":
                urls += links_in_share(body)
        data: dict[str, Any] = {"env": self._facts()}
        links, fetched = self._fetch_links(rt, [*links_in_files(rt.tenant_id, files), *urls], env)
        if links:
            data["links"] = links
        reads = pre_read(svc, self.reader, [*files, *fetched])
        if reads:
            data["reads"] = reads
        return data

    def billing_event(self, tenant_id: str, event: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        """One verified payment-provider event (backoffice.billing.reduce_event) as one event in the business's log.
        An event already applied is not recorded again: Stripe may send the same one many times."""
        with self.open(tenant_id) as rt:
            svc = rt.service
            event_id = str(event.get("id") or "")
            if not event_id or svc.billing.seen(event_id):
                return 200, {"ok": True, "duplicate": True}
            env = self.live_env()
            data: dict[str, Any] = {"event": dict(event)}
            if svc.release_due(self.now().date(), event):
                data.update(self._release_data(rt, env))
            obj = event.get("object") if isinstance(event.get("object"), Mapping) else {}
            customer = obj.get("customer")
            index = [IndexOp("link_billing_customer", str(customer))] if customer and \
                customer != svc.billing.customer else []
            return self.record(rt, "billing.event", data, "system:billing", env, index=index)

    def browser_tool(self, tenant_id: str, actor: str, body: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        """``POST /api/chat/tool`` (the browser chat): changing tools are events, the rest are reads."""
        from backoffice.assistant import CHANGING_TOOLS

        if body.get("name") in CHANGING_TOOLS:
            return self.command(tenant_id, actor, "POST", "/api/chat/tool", body)
        return self.view(tenant_id, "POST", "/api/chat/tool", dict(body))


# --------------------------------------------------------------------------- event handlers


Handler = Callable[[TenantManager, TenantRuntime, Event, Env], tuple[int, dict[str, Any]]]


def _tenant_created(m: TenantManager, rt: TenantRuntime, event: Event, env: Env) -> tuple[int, dict[str, Any]]:
    if rt.svc is not None or event.seq != 1:
        raise EventError("a tenant is created once, by its first event")
    owner = event.data.get("owner") or {}
    with deterministic(f"{rt.tenant_id}:{event.seq}", lambda: event.at):
        rt.svc = BackOfficeService.new_tenant(rt.tenant_id, owner_name=str(owner.get("name", "")),
                                              owner_email=str(owner.get("email", "")), now=event.at,
                                              vault=env.vault if env.vault is not None else _ReplayVault())
    rt.svc.vault = env.vault
    # Reads never change a tenant here: a sensitive original read is recorded as its own event (§52).
    rt.svc.inline_access_log = False
    # Whether new evidence waits for the plan is decided before each event is recorded, and recorded (below).
    rt.svc.decides_holds = False
    rt.svc.billing.enforced = bool((event.data.get("billing") or {}).get("enforced"))
    return 201, {"ok": True}


def _company_added(m: TenantManager, rt: TenantRuntime, event: Event, env: Env) -> tuple[int, dict[str, Any]]:
    d = event.data
    # The register's answer was recorded with the event: applying it never asks the register again (QA A2).
    return 200, rt.service.add_company(d.get("name"), d.get("taxId") or None, d.get("legalName") or None,
                                       d.get("address") or None, country=d.get("country") or None,
                                       lookup=d.get("lookup") if isinstance(d.get("lookup"), Mapping) else None)


def _accountant_set(m: TenantManager, rt: TenantRuntime, event: Event, env: Env) -> tuple[int, dict[str, Any]]:
    d = event.data
    return 200, rt.service.set_accountant(d.get("email"), d.get("name") or None, d.get("software") or None,
                                          d.get("companyId") or None, d.get("firm") or None)


def _request(m: TenantManager, rt: TenantRuntime, event: Event, env: Env) -> tuple[int, dict[str, Any]]:
    d = event.data
    body = restore_files(d.get("body") or {}, lambda ref: m.get_file(rt.tenant_id, ref, env))
    if env.live:
        body.update(env.secret_fields)
    if d.get("held"):  # waits for the plan, kept (backoffice.billing)
        return rt.service.hold_request(str(d["path"]), body)
    scope = d.get("manager")
    if isinstance(scope, Mapping):  # an outlet manager's change: through their outlets' routes only
        svc = rt.service
        svc.viewer = (str(scope.get("email") or ""), "manager")
        try:
            return svc.dispatch_manager(str(d["method"]), str(d["path"]), body,
                                        frozenset(str(c) for c in scope.get("costCenters") or ()))
        finally:
            svc.viewer = ("owner", "owner")
    return rt.service.dispatch(str(d["method"]), str(d["path"]), body)


def _api_key_created(m: TenantManager, rt: TenantRuntime, event: Event, env: Env) -> tuple[int, dict[str, Any]]:
    d = event.data
    svc = rt.service
    if env.live and env.secret:
        out = svc.api_key_create({"name": d.get("name")}, secret=env.secret)
        if svc._keys()[out["id"]]["hash"] != d.get("hash"):
            raise EventError("the new key does not match its recorded fingerprint")
        return 200, out
    return 200, svc.api_key_create({"name": d.get("name")}, fingerprint=(str(d["prefix"]), str(d["hash"])))


def _chat_rule(m: TenantManager, rt: TenantRuntime, event: Event, env: Env) -> tuple[int, dict[str, Any]]:
    from backoffice.assistant import RuleBrain

    try:
        return 200, RuleBrain(rt.service.assistant).handle(str(event.data.get("message", "")),
                                                          clean_history(event.data.get("history")))
    except ValueError as exc:
        return 400, {"error": "bad_request", "message": str(exc)}


def _chat_tool(m: TenantManager, rt: TenantRuntime, event: Event, env: Env) -> tuple[int, dict[str, Any]]:
    import json

    from backoffice.assistant import run_tool

    args = event.data.get("input") if isinstance(event.data.get("input"), dict) else {}
    try:
        result = run_tool(rt.service.assistant, str(event.data.get("name")), dict(args), env.cards)
    except ServiceError as exc:
        return 200, {"isError": True, "result": exc.message}
    except (ValueError, KeyError, TypeError) as exc:
        return 200, {"isError": True, "result": str(exc)[:500] or "That did not work."}
    return 200, {"isError": False, "result": json.loads(json.dumps(result, default=str))}


def _access_until(data: Mapping[str, Any]) -> date | None:
    """The day a sign-in's grant ends, when the event recorded one (R4)."""
    value = data.get("accessUntil")
    return date.fromisoformat(str(value)) if value else None


def _sign_in_finished(m: TenantManager, rt: TenantRuntime, event: Event, env: Env) -> tuple[int, dict[str, Any]]:
    rt.service.finish_sign_in(str(event.data.get("connectionId")), access_until=_access_until(event.data))
    return 200, {"ok": True}


def _email_connected(m: TenantManager, rt: TenantRuntime, event: Event, env: Env) -> tuple[int, dict[str, Any]]:
    svc = rt.service
    svc.authorizer = _FixedAuthorizer("https://sign-in.invalid/finished")  # the sign-in already happened
    added = svc.add_source({"kind": "email", "provider": event.data.get("provider"),
                            "address": event.data.get("address")})
    svc.finish_sign_in(added["id"], access_until=_access_until(event.data))
    return 200, {"ok": True, "id": added["id"]}


def _bank_linked(m: TenantManager, rt: TenantRuntime, event: Event, env: Env) -> tuple[int, dict[str, Any]]:
    d = event.data
    until = date.fromisoformat(str(d["consentUntil"])) if d.get("consentUntil") else \
        (event.at + timedelta(days=180)).date()
    return 200, rt.service.link_bank(str(d.get("bank") or ""), str(d.get("companyId") or ""),
                                     [str(i) for i in d.get("ibans") or []], until)


def _sync_mail(m: TenantManager, rt: TenantRuntime, event: Event, env: Env) -> tuple[int, dict[str, Any]]:
    d = event.data
    raws = [m.get_file(rt.tenant_id, ref[OBJECT], env) for ref in d.get("messages") or []]
    return 200, rt.service.sync_mail(str(d.get("connectionId")), raws, d.get("state"), held=bool(d.get("held")))


def _sync_bank(m: TenantManager, rt: TenantRuntime, event: Event, env: Env) -> tuple[int, dict[str, Any]]:
    d = event.data
    rows = [bank_row(r) for r in d.get("rows") or []]
    return 200, rt.service.sync_bank(str(d.get("connectionId")), rows, d.get("state"))


def _sync_failed(m: TenantManager, rt: TenantRuntime, event: Event, env: Env) -> tuple[int, dict[str, Any]]:
    d = event.data
    return 200, rt.service.sync_failed(str(d.get("connectionId")), d.get("state") or {},
                                       reconnect=bool(d.get("reconnect")))


def _links_fetched(m: TenantManager, rt: TenantRuntime, event: Event, env: Env) -> tuple[int, dict[str, Any]]:
    """Waiting links, opened before this event was recorded: read through the recording (never opened here)."""
    return 200, rt.service.follow_links([str(u) for u in event.data.get("urls") or []])


def _tick(m: TenantManager, rt: TenantRuntime, event: Event, env: Env) -> tuple[int, dict[str, Any]]:
    rt.service.orchestrator.run()
    rt.service.billing_tick()  # a new month, a grace period that ended (backoffice.billing)
    return 200, {"ok": True}


def _billing_event(m: TenantManager, rt: TenantRuntime, event: Event, env: Env) -> tuple[int, dict[str, Any]]:
    """What the payment provider said (verified before it was recorded), applied once (backoffice.billing)."""
    data = event.data.get("event")
    return 200, rt.service.billing_event(data if isinstance(data, Mapping) else {})


def _outbox_send(m: TenantManager, rt: TenantRuntime, event: Event, env: Env) -> tuple[int, dict[str, Any]]:
    """One waiting email, sent with the live mailer (on replay: the one that already went out)."""
    return 200, rt.service.send_waiting(str(event.data.get("id") or ""))


def _void(m: TenantManager, rt: TenantRuntime, event: Event, env: Env) -> tuple[int, dict[str, Any]]:
    return 200, {"ok": True}


def _documents_opened(m: TenantManager, rt: TenantRuntime, event: Event, env: Env) -> tuple[int, dict[str, Any]]:
    """Sensitive documents' originals someone read (§52): the access log the owner reads."""
    d = event.data
    return 200, rt.service.record_access([str(i) for i in d.get("documentIds") or []], who=str(d.get("who") or ""),
                                         role=str(d.get("role") or ""))


_HANDLERS: dict[str, Handler] = {
    "tenant.created": _tenant_created,
    "company.added": _company_added,
    "accountant.set": _accountant_set,
    "request": _request,
    "api_key.created": _api_key_created,
    "chat.rule": _chat_rule,
    "chat.tool": _chat_tool,
    "sign_in.finished": _sign_in_finished,
    "email.connected": _email_connected,
    "bank.linked": _bank_linked,
    "sync.mail": _sync_mail,
    "sync.bank": _sync_bank,
    "sync.failed": _sync_failed,
    "links.fetched": _links_fetched,
    "tick": _tick,
    "outbox.send": _outbox_send,
    "void": _void,
    "documents.opened": _documents_opened,
    "billing.event": _billing_event,
}
