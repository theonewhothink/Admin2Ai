"""Supplier websites on the server: sign-in, one-time codes, invoice retrieval (§9, §10, checklist C6).

:class:`PortalWorker` runs a supplier's deterministic adapter (connectors/portals) for one connection at a time:

* **Isolated.** Every run builds a fresh adapter instance (``factory``) that sees only that connection's
  sign-in, read from the vault; nothing is shared between businesses or connections, and one connection never
  runs twice at once in this process. What a website needs to resume (its cookies, the flow waiting for a code)
  lives in the vault with the sign-in, never in an event or a log, so the next step can continue in any process.
* **Codes resume.** When the website sends the owner a one-time code, the challenge to resume with goes to the
  vault and the business's log records only that a code is awaited, where it went and until when
  (``portal.code_needed``): the owner gets one push and a Needs-you item "Vodafone needs a sign-in code." The
  owner enters it (``POST /api/portals/<connection>/code``): the same challenge continues in a fresh, isolated
  adapter, the sign-in finishes and the retrieval completes. A wrong code leaves the challenge waiting ("That
  code didn't work. Check it and try again."); an expired one asks the website for a new code at once.
* **Read before recording.** Invoices the website gave are stored and read (OCR, as for any upload) before the
  event that brings them in is recorded (``portal.retrieved``), so a replay never signs in, fetches or reads.
* **Searched before asking.** When an invoice of the website's supplier is missing, the missing-document search
  asks the website too (:meth:`PortalWorker.search_place`, server/search.py), with the saved session or a new
  sign-in; a session it made and the invoices it fetched are kept in the vault (:meth:`PortalWorker.searched`).
  A search never sends the owner a code: on a website that uses codes (``codes`` in the vault) or while one is
  awaited, only a saved session is used; a first sign-in that asks for a code is recorded like the daily sync's
  (``portal.code_needed``: one push, one Needs-you item), and entering it signs in and fetches what is new.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from backoffice.connectors.portals import (
    MfaChallenge,
    PortalCredentials,
    PortalDocument,
    PortalRegistry,
    PortalSession,
    PortalSync,
    PortalSyncOutcome,
    SupplierPortalConnector,
    default_registry,
)

__all__ = ["CODE_VALID_FOR", "PORTAL_INTERVAL", "PortalWorker"]

log = logging.getLogger("backoffice.server.portals")

PORTAL_INTERVAL = timedelta(days=1)  # a supplier's website is read once a day
CODE_VALID_FOR = timedelta(minutes=10)  # when the website does not say how long its code works
KNOWN_KEPT = 1000  # invoice ids already fetched, remembered per connection (the vault), newest last

WRONG_CODE = "That code didn't work. Check it and try again."


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _when(value: Any) -> datetime | None:
    return datetime.fromisoformat(value) if isinstance(value, str) and value else None


def _fetched(name: str, n: int) -> str:
    from backoffice.learning.plain import count_phrase

    return f"Done. I signed in to {name} and fetched {count_phrase(n, 'invoice')}."


class PortalWorker:
    """Runs supplier-website adapters for the server, one isolated session per connection."""

    def __init__(self, manager: Any, *, vault: Any = None, registry: PortalRegistry = default_registry,
                 factory: Callable[[str], SupplierPortalConnector | None] | None = None,
                 history_days: int = 90) -> None:
        self.manager = manager
        self.vault = vault if vault is not None else manager.vault
        self.registry = registry
        self.factory = factory or self._registered
        self.history = timedelta(days=history_days)
        self._locks: dict[tuple[str, str], threading.Lock] = {}
        self._guard = threading.Lock()

    def now(self) -> datetime:
        return self.manager.now()

    def _registered(self, key: str) -> SupplierPortalConnector | None:
        adapter = self.registry.get(key)
        return adapter() if adapter is not None else None  # type: ignore[call-arg]

    def _lock(self, tenant_id: str, connection_id: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault((tenant_id, connection_id), threading.Lock())

    # ----------------------------------------------------------------- what the connection is

    def _connection(self, tenant_id: str, connection_id: str) -> tuple[str, Mapping[str, Any] | None] | None:
        def look(svc: Any) -> tuple[str, Mapping[str, Any] | None] | None:
            c = svc.repo.connectors.get(connection_id)
            if c is None or c.kind != "portal":
                return None
            return c.name, svc.sync_states.get(c.id)

        return self.manager.read(tenant_id, look, what="portal plan")

    def _state(self, tenant_id: str, connection_id: str, name: str, account: str,
               saved: Mapping[str, Any] | None) -> Any:
        from backoffice.connectors.base import ConnectorKind, ConnectorState

        if saved:
            try:
                state = ConnectorState.model_validate(dict(saved))
                if state.kind is ConnectorKind.SUPPLIER_PORTAL:
                    return state
            except ValueError:
                log.warning("portal_state_unreadable", extra={"tenant": tenant_id})
        return ConnectorState(tenant_id=tenant_id, connector_id=connection_id, kind=ConnectorKind.SUPPLIER_PORTAL,
                              account=account, display_name=name)

    # ----------------------------------------------------------------- a scheduled run (the sync worker)

    def due(self, state: Any, sign_in: Mapping[str, Any], now: datetime) -> bool:
        """Once a day; while a code is awaited, not again until it has expired (then a new one, once a day)."""
        code = sign_in.get("code") if isinstance(sign_in.get("code"), Mapping) else None
        if code:
            until, asked = _when(code.get("until")), _when(code.get("asked"))
            if until is not None and now < until:
                return False
            return asked is None or now - asked >= PORTAL_INTERVAL
        last = state.last_successful_sync if state is not None else None
        return last is None or now - last >= PORTAL_INTERVAL

    def sync(self, tenant_id: str, connection_id: str) -> PortalSyncOutcome | None:
        """Sign in (a saved session first), fetch the invoices not fetched yet, record them; a code request is
        recorded as such. None: no adapter for that website on this server (engineering, not the owner)."""
        with self._lock(tenant_id, connection_id):
            found = self._connection(tenant_id, connection_id)
            if found is None:
                return None
            name, saved = found
            secret = self.vault.open(tenant_id, connection_id)
            adapter = self.factory(str(secret.get("portal") or ""))
            if adapter is None:
                return None
            now = self.now()
            state = self._state(tenant_id, connection_id, name, str(secret.get("username") or ""), saved)
            documents: list[PortalDocument] = []
            outcome = PortalSync(adapter, history_window=self.history, clock=self.now).sync(
                state, documents.append, credentials=self._credentials(secret), session=self._session(secret),
                known_ids=secret.get("known") or (), now=now)
            self._record(tenant_id, connection_id, outcome, documents, now)
            return outcome

    # ----------------------------------------------------------------- the owner entered a code

    def submit_code(self, tenant_id: str, connection_id: str, code: str) -> tuple[int, dict[str, Any]]:
        """The code the owner entered (``POST /api/portals/<connection>/code``): the waiting sign-in continues in
        a fresh, isolated adapter and the retrieval completes. Plain answers for a wrong or expired code."""
        from backoffice.connectors.vault import CredentialNotFound

        with self._lock(tenant_id, connection_id):
            found = self._connection(tenant_id, connection_id)
            if found is None:
                return 404, {"error": "not_found", "message": "I can't find that website."}
            name, saved = found
            try:
                secret = self.vault.open(tenant_id, connection_id)
            except CredentialNotFound:
                return 404, {"error": "not_found", "message": f"{name} isn't waiting for a code right now."}
            challenge = self._challenge(secret)
            adapter = self.factory(str(secret.get("portal") or ""))
            if challenge is None or adapter is None:
                return 404, {"error": "not_found", "message": f"{name} isn't waiting for a code right now."}
            now = self.now()
            state = self._state(tenant_id, connection_id, name, str(secret.get("username") or ""), saved)
            runner = PortalSync(adapter, history_window=self.history, clock=self.now)
            documents: list[PortalDocument] = []
            outcome = runner.resume(state, challenge, code, documents.append, known_ids=secret.get("known") or (),
                                    now=now)
            if outcome.code_rejected:
                log.info("portal_code_rejected", extra={"tenant": tenant_id})
                return 400, {"error": "bad_request", "message": WRONG_CODE}
            if outcome.code_expired:
                # The website sends a new code: signed in again now (a fresh adapter), the new request recorded.
                again = PortalSync(self.factory(str(secret.get("portal") or "")) or adapter,
                                   history_window=self.history, clock=self.now)
                outcome = again.sync(state, documents.append, credentials=self._credentials(secret), now=now)
                self._record(tenant_id, connection_id, outcome, documents, now)
                if outcome.challenge is not None:
                    return 410, {"error": "gone", "message": f"That code has expired. {name} sent you a new one. "
                                                             "Enter it when it arrives."}
                if not outcome.outcome.ok:
                    return 502, {"error": "unavailable",
                                 "message": f"That code has expired, and I couldn't reach {name} for a new one. "
                                            "I'll try again later."}
                return 200, {"ok": True, "documents": len(documents), "message": _fetched(name, len(documents))}
            self._record(tenant_id, connection_id, outcome, documents, now)
            if outcome.challenge is not None:
                return 202, {"ok": True, "message": f"{name} asked for one more code. Enter it when it arrives."}
            if not outcome.outcome.ok:
                error = outcome.outcome.error
                if error is not None and error.needs_reconnect:
                    return 409, {"error": "conflict", "message": f"{name} refused the saved sign-in. Add the website "
                                                                 "again with your current password."}
                return 502, {"error": "unavailable",
                             "message": f"I couldn't finish signing in to {name} just now. I'll try again later."}
            return 200, {"ok": True, "documents": len(documents), "message": _fetched(name, len(documents))}

    # ----------------------------------------------------------------- the missing-document search (server/search.py)

    def search_place(self, tenant_id: str, connection_id: str, *, place: str, first: bool = False) -> Any:
        """The website as a place to search for a missing document (missing.searches.PortalSearch), in a fresh,
        isolated adapter with this connection's sign-in; None when this server has no adapter for it. Raises what
        opening the vault raises (the attempt is noted as failed)."""
        from backoffice.missing import PortalSearch

        secret = self.vault.open(tenant_id, connection_id)
        adapter = self.factory(str(secret.get("portal") or ""))
        if adapter is None:
            return None
        session = self._session(secret)
        now = self.now()
        if session is not None and session.expires_at is not None and session.expires_at <= now:
            session = None
        waiting = self._challenge(secret)
        waiting_now = waiting is not None and not waiting.expired(now)
        return PortalSearch(adapter, credentials=self._credentials(secret), session=session, place=place,
                            first=first, connection_id=connection_id, clock=self.now,
                            sign_in=not (secret.get("codes") or waiting_now))

    def searched(self, tenant_id: str, search: Any) -> None:
        """After a search: the session it signed in with is kept for the next run, and the invoices it fetched are
        not fetched again by the daily sync (they are already in)."""
        cid = getattr(search, "connection_id", None)
        if not cid or not (search.signed_in or search.retrieved or search.challenge is not None):
            return
        with self._lock(tenant_id, cid):
            changes: dict[str, Any] = {}
            if search.signed_in and search.session is not None:
                changes["session"] = self._session_json(search.session)
            if search.retrieved:
                known = list(self.vault.open(tenant_id, cid).get("known") or [])
                known += [i for i in search.retrieved if i not in known]
                changes["known"] = known[-KNOWN_KEPT:]
            if search.challenge is not None:  # the website sent the owner a code: asked for once, as a sync would
                now = self.now()
                challenge = search.challenge
                issued = challenge.issued_at or now
                expires = challenge.expires_at or issued + CODE_VALID_FOR
                changes["codes"] = True
                changes["challenge"] = {"supplier": challenge.supplier_key, "account": challenge.account,
                                        "channel": challenge.channel, "resume": dict(challenge.resume_state),
                                        "issued": _iso(issued), "expires": _iso(expires)}
                self.vault.update(tenant_id, cid, changes)
                self.manager.record_portal_code(tenant_id, cid, channel=challenge.channel, expires_at=expires,
                                                state=None)
                return
            self.vault.update(tenant_id, cid, changes)

    # ----------------------------------------------------------------- recording (read before record)

    def _record(self, tenant_id: str, connection_id: str, outcome: PortalSyncOutcome,
                documents: list[PortalDocument], now: datetime) -> None:
        state = outcome.outcome.state.model_dump(mode="json")
        changes: dict[str, Any] = {}
        if outcome.session is not None:
            changes["session"] = self._session_json(outcome.session)
        if outcome.challenge is not None:
            challenge = outcome.challenge
            issued = challenge.issued_at or now
            expires = challenge.expires_at or issued + CODE_VALID_FOR
            changes["challenge"] = {"supplier": challenge.supplier_key, "account": challenge.account,
                                    "channel": challenge.channel, "resume": dict(challenge.resume_state),
                                    "issued": _iso(issued), "expires": _iso(expires)}
            changes["codes"] = True  # this website sends codes: a search only ever uses a saved session
            self.vault.update(tenant_id, connection_id, changes)
            self.manager.record_portal_code(tenant_id, connection_id, channel=challenge.channel, expires_at=expires,
                                            state=state)
            return
        if outcome.outcome.ok:
            changes["challenge"] = {}
            known = list(self.vault.open(tenant_id, connection_id).get("known") or [])
            known += [i for i in outcome.retrieved_ids if i not in known]
            changes["known"] = known[-KNOWN_KEPT:]
            files = [(d.data, d.filename, d.content_type, d.ref.portal_id if d.ref else "") for d in documents]
            self.manager.record_portal_documents(tenant_id, connection_id, files, state)
            self.vault.update(tenant_id, connection_id, changes)
            return
        if changes:
            self.vault.update(tenant_id, connection_id, changes)
        error = outcome.outcome.error
        if error is not None and error.needs_reconnect:  # the owner's password no longer works
            self.manager.record_sync_failure(tenant_id, connection_id, state, reconnect=True)
        else:
            log.warning("portal_sync_failed", extra={"tenant": tenant_id, "reason": error.code if error else ""})

    # ----------------------------------------------------------------- the vault's view

    @staticmethod
    def _credentials(secret: Mapping[str, Any]) -> PortalCredentials | None:
        from pydantic import SecretStr

        username, password = secret.get("username"), secret.get("password")
        if not username or not password:
            return None
        return PortalCredentials(str(username), SecretStr(str(password)))

    @staticmethod
    def _session(secret: Mapping[str, Any]) -> PortalSession | None:
        data = secret.get("session")
        if not isinstance(data, Mapping) or not data.get("supplier"):
            return None
        return PortalSession(str(data["supplier"]), str(data.get("account") or ""),
                             _when(data.get("at")) or datetime.min.replace(tzinfo=timezone.utc),
                             _when(data.get("expires")),
                             dict(data.get("state") or {}))

    @staticmethod
    def _session_json(session: PortalSession) -> dict[str, Any]:
        return {"supplier": session.supplier_key, "account": session.account, "at": _iso(session.authenticated_at),
                "expires": _iso(session.expires_at), "state": dict(session.state)}

    @staticmethod
    def _challenge(secret: Mapping[str, Any]) -> MfaChallenge | None:
        data = secret.get("challenge")
        if not isinstance(data, Mapping) or not data.get("supplier"):
            return None
        return MfaChallenge(str(data["supplier"]), str(data.get("account") or ""), data.get("channel") or None,
                            dict(data.get("resume") or {}), _when(data.get("issued")), _when(data.get("expires")))
