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
"""

from __future__ import annotations

import logging
import re
import threading
from collections import OrderedDict
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from backoffice.domain.models import deterministic
from backoffice.orchestrator import TZ
from backoffice.service import BackOfficeService, ServiceError

from .events import SECRET_FIELDS, Event, EventError, genesis_hash, restore_files, sanitize_body, state_digest
from .store import IndexOp, SeqConflict, Store, StoreUnavailable

__all__ = [
    "Env",
    "READ_ONLY_POSTS",
    "ReplayDiverged",
    "TenantManager",
    "TenantNotFound",
    "TenantRuntime",
]

log = logging.getLogger("backoffice.server")

# POST routes that only read (a body carries their filters).
READ_ONLY_POSTS = frozenset({"/api/documents/export"})
_API_KEYS = "/api/accountant/api-keys"
_REVOKE = re.compile(r"^/api/accountant/api-keys/([^/]+)/revoke$")
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
        cache_size: int = 200,
    ) -> None:
        self.store = store
        self.objects = objects
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.vault = vault
        self.authorizer = authorizer
        self.mailer = mailer
        self.brain_factory = brain_factory
        self.notifier = notifier
        self.cache_size = cache_size
        self._cache: OrderedDict[str, TenantRuntime] = OrderedDict()
        self._locks: dict[str, threading.RLock] = {}
        self._guard = threading.Lock()
        self._refused: dict[str, ReplayDiverged] = {}

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
            digest = state_digest(rt.svc)
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
        for _ in range(5):
            at = self._event_time(rt)
            pre = None
            if rt.svc is not None:
                rt.svc.repo.clock.advance_to(at)
                pre = state_digest(rt.svc)
            candidate = Event.make(seq=rt.seq + 1, at=at, kind=kind, actor=actor, data=data, pre=pre,
                                   prev_hash=rt.head_hash)
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
        return result

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
        clock = svc.repo.clock
        with deterministic(f"{rt.tenant_id}:{event.seq}", clock.now):
            clock.advance_to(event.at)
            try:
                return handler(self, rt, event, env)
            except ServiceError as exc:
                return _error(exc)

    # ----------------------------------------------------------------- creating tenants

    def build_tenant(self, tenant_id: str, *, owner_name: str, owner_email: str, company_name: str,
                     tax_id: str | None, actor: str) -> tuple[TenantRuntime, list[Event]]:
        """A new tenant and its first events, not yet stored (sign-up stores them with the account)."""
        rt = TenantRuntime(tenant_id)
        events: list[Event] = []
        env = Env(live=True, vault=self.vault)
        for kind, data in (("tenant.created", {"owner": {"name": owner_name, "email": owner_email}}),
                           ("company.added", {"name": company_name, "taxId": tax_id or "", "legalName": ""})):
            at = self._event_time(rt)
            if rt.svc is not None:
                rt.svc.repo.clock.advance_to(at)
            event = Event.make(seq=rt.seq + 1, at=at, kind=kind, actor=actor, data=data, pre=rt.digest(),
                               prev_hash=rt.head_hash)
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
        try:
            self.record(rt, "tick", {}, "system", Env(live=True, vault=self.vault, mailer=self.mailer))
        except StoreUnavailable:
            log.warning("tick_skipped", extra={"tenant": rt.tenant_id})

    # ----------------------------------------------------------------- what the API calls

    def view(self, tenant_id: str, method: str, path: str, body: Any = None) -> tuple[int, dict[str, Any]]:
        """A read: the state as of now, unchanged."""
        with self.open(tenant_id) as rt:
            self.tick_if_due(rt)
            svc = rt.service
            with svc.repo.clock.peek(self.now()):
                return svc.dispatch(method, path, body)

    def read(self, tenant_id: str, reader: Callable[[BackOfficeService], Any]) -> Any:
        """Run ``reader`` on the tenant's service (no change allowed) as of now."""
        with self.open(tenant_id) as rt:
            svc = rt.service
            with svc.repo.clock.peek(self.now()):
                return reader(svc)

    def live_env(self, **kwargs: Any) -> Env:
        return Env(live=True, vault=self.vault, mailer=self.mailer, **kwargs)

    def _facts(self, **extra: Any) -> dict[str, Any]:
        return {"vault": self.vault is not None, "mailer": self.mailer is not None, **extra}

    def command(self, tenant_id: str, actor: str, method: str, path: str,
                body: Mapping[str, Any] | None) -> tuple[int, dict[str, Any]]:
        """A change requested through the API: recorded as one event, then applied."""
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
            data = {"method": method, "path": path, "body": clean,
                    "env": self._facts(authorize=authorize is not None)}
            return self.record(rt, "request", data, actor, env, index=index)

    def _authorize_url(self, rt: TenantRuntime, path: str, body: Mapping[str, Any]) -> str | None:
        """For a new Google/Microsoft mailbox: the consent URL, obtained before the event is recorded."""
        if path != "/api/sources" or body.get("kind") != "email" or self.authorizer is None:
            return None
        provider = body.get("provider") or "google"
        address = body.get("address")
        if provider not in ("google", "microsoft") or not isinstance(address, str) or not address.strip():
            return None
        if provider not in getattr(self.authorizer, "providers", ()):
            return None
        cid = rt.service._slug("mail", address.strip().lower())
        try:
            return self.authorizer.begin(provider, rt.tenant_id, cid, login_hint=address.strip().lower())
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
        data = {"name": str(body.get("name") or ""), "taxId": str(body.get("taxId") or ""),
                "legalName": str(body.get("legalName") or "")}
        with self.open(tenant_id) as rt:
            return self.record(rt, "company.added", data, actor, self.live_env())

    def set_accountant(self, tenant_id: str, actor: str, body: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        data = {"email": str(body.get("email") or ""), "name": str(body.get("name") or ""),
                "software": str(body.get("software") or "")}
        with self.open(tenant_id) as rt:
            return self.record(rt, "accountant.set", data, actor, self.live_env())

    def finish_sign_in(self, tenant_id: str, connection_id: str, provider: str,
                       email: str | None) -> tuple[int, dict[str, Any]]:
        """A mailbox sign-in came back from Google/Microsoft with a refresh token in the vault."""
        with self.open(tenant_id) as rt:
            if connection_id in rt.service.repo.connectors:
                return self.record(rt, "sign_in.finished", {"connectionId": connection_id}, "system",
                                   self.live_env())
            if not email:
                return 400, {"error": "bad_request", "message": "I couldn't tell which mailbox that was."}
            status, body = self.record(rt, "email.connected", {"provider": provider, "address": email}, "system",
                                       self.live_env())
            new_id = body.get("id")
            if status == 200 and isinstance(new_id, str) and self.vault is not None and new_id != connection_id:
                # The refresh token was sealed under the sign-in's temporary id: move it to the connector.
                try:
                    secret = self.vault.open(tenant_id, connection_id)
                    self.vault.store(tenant_id, new_id, provider, secret)
                    self.vault.delete(tenant_id, connection_id)
                except Exception:
                    log.exception("vault_move_failed", extra={"tenant": tenant_id})
            return status, body

    def bank_linked(self, tenant_id: str, *, bank: str, company_id: str, ibans: Sequence[str]
                    ) -> tuple[int, dict[str, Any]]:
        with self.open(tenant_id) as rt:
            return self.record(rt, "bank.linked", {"bank": bank, "companyId": company_id, "ibans": list(ibans)},
                               "system", self.live_env())

    def chat(self, tenant_id: str, actor: str, body: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        message = body.get("message")
        if not isinstance(message, str) or not message.strip():
            return 400, {"error": "bad_request", "message": "Write what you need."}
        if len(message) > 4000:
            return 400, {"error": "bad_request", "message": "That is too long. Try a shorter request."}
        history = body.get("history") if isinstance(body.get("history"), list) else []
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
            return self.record(rt, "chat.rule", {"message": message}, actor, self.live_env())

    def _chat_tool(self, rt: TenantRuntime, actor: str, name: str, args: dict[str, Any],
                   cards: list[dict[str, Any]]) -> Any:
        from backoffice.assistant import CHANGING_TOOLS, run_tool

        if name not in CHANGING_TOOLS:
            return run_tool(rt.service.assistant, name, args, cards)
        _, body = self.record(rt, "chat.tool", {"name": name, "input": dict(args)}, actor,
                              self.live_env(cards=cards))
        if body.get("isError"):
            raise ValueError(str(body.get("result") or "That did not work."))
        return body.get("result")

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
    return 201, {"ok": True}


def _company_added(m: TenantManager, rt: TenantRuntime, event: Event, env: Env) -> tuple[int, dict[str, Any]]:
    d = event.data
    return 200, rt.service.add_company(d.get("name"), d.get("taxId") or None, d.get("legalName") or None)


def _accountant_set(m: TenantManager, rt: TenantRuntime, event: Event, env: Env) -> tuple[int, dict[str, Any]]:
    d = event.data
    return 200, rt.service.set_accountant(d.get("email"), d.get("name") or None, d.get("software") or None)


def _request(m: TenantManager, rt: TenantRuntime, event: Event, env: Env) -> tuple[int, dict[str, Any]]:
    d = event.data
    body = restore_files(d.get("body") or {}, lambda ref: m.get_file(rt.tenant_id, ref, env))
    if env.live:
        body.update(env.secret_fields)
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
        return 200, RuleBrain(rt.service.assistant).handle(str(event.data.get("message", "")))
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


def _sign_in_finished(m: TenantManager, rt: TenantRuntime, event: Event, env: Env) -> tuple[int, dict[str, Any]]:
    rt.service.finish_sign_in(str(event.data.get("connectionId")))
    return 200, {"ok": True}


def _email_connected(m: TenantManager, rt: TenantRuntime, event: Event, env: Env) -> tuple[int, dict[str, Any]]:
    svc = rt.service
    svc.authorizer = _FixedAuthorizer("https://sign-in.invalid/finished")  # the sign-in already happened
    added = svc.add_source({"kind": "email", "provider": event.data.get("provider"),
                            "address": event.data.get("address")})
    svc.finish_sign_in(added["id"])
    return 200, {"ok": True, "id": added["id"]}


def _bank_linked(m: TenantManager, rt: TenantRuntime, event: Event, env: Env) -> tuple[int, dict[str, Any]]:
    svc = rt.service
    ids: list[str] = []
    ibans = [str(i) for i in event.data.get("ibans") or []] or [""]
    for iban in ibans:
        try:
            out = svc.add_source({"kind": "bank", "bank": event.data.get("bank"),
                                  "companyId": event.data.get("companyId"), "iban": iban})
            ids.append(out["id"])
        except ServiceError as exc:
            if exc.status != 409:  # an account already connected is fine
                raise
    return 200, {"ok": True, "ids": ids}


def _tick(m: TenantManager, rt: TenantRuntime, event: Event, env: Env) -> tuple[int, dict[str, Any]]:
    rt.service.orchestrator.run()
    return 200, {"ok": True}


def _void(m: TenantManager, rt: TenantRuntime, event: Event, env: Env) -> tuple[int, dict[str, Any]]:
    return 200, {"ok": True}


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
    "tick": _tick,
    "void": _void,
}
