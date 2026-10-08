"""Imports from connected mailboxes and banks, and each tenant's day, with nobody signed in (§6, §8, §45–48).

:class:`SyncWorker` makes one pass over every tenant (``python -m
backoffice.server.worker`` repeats it):

1. **The day.** The tenant's daily tick runs (chasing, deadlines, closing
   months) even when nobody opens the app.
2. **Mailboxes** (Gmail, Microsoft 365, IMAP) and **banks** (GoCardless, PSD2)
   the owner connected with a real sign-in are read with the engine's
   connectors. The first sync reads the history window the owner chose (the
   last 90 days by default, or the last 12 months, §6; the server's
   BACKOFFICE_HISTORY_DAYS for a business that never chose); later syncs
   continue from the saved cursor. Choosing 12 months later gives each
   mailbox and bank already read its older months as a known gap, read as in
   10. below. Spam (Gmail's spam, Microsoft 365's Junk Email, an IMAP Junk or
   Spam folder) is read and searched only when the owner allows it; the trash
   never. Access tokens are refreshed through the vault, which keeps any
   rotated refresh token.
3. Whatever a sync fetched becomes **events** in the tenant's log, in batches:
   ``sync.mail`` (the raw messages, stored in the object store; PDFs and photos
   read before the event is recorded) and ``sync.bank`` (booked transactions).
   The last batch carries the connector's new state (cursor, coverage, gaps),
   so a replay rebuilds exactly what the owner saw and the next sync continues
   where this one ended.
   A reply that points at an invoice sent earlier in its thread ("see the
   invoice I sent on the 3rd") brings the thread's earlier messages with it,
   fetched through the connector (Gmail threads, Microsoft conversations)
   before the event is recorded, so the earlier attachment is read even when
   it arrived before the history window. Invoice links in the messages are
   opened before recording too (server/links.py).
4. **Links that wait.** Invoice links not opened yet (the site did not
   answer, or they arrived before links were opened) are opened again, with
   backoff, and recorded as ``links.fetched``.
5. **Failures.** A network problem or provider outage is retried with
   exponential backoff (respecting ``Retry-After``) and records nothing. A
   refused sign-in or an expired bank consent only the owner can fix: the
   connection is marked as needing them (``sync.failed``), which shows
   "Gmail needs reconnecting" and sends one push notification (§47–48). A
   connection that has not synced for a day despite retries is shown the same
   way; the month can never close green while it is stale.
6. **Cloud storage and accounting software.** A folder the owner chose in Google Drive or OneDrive is read for
   new files (``sync.files``); the accounting software (TOConline, Moloni, InvoiceXpress) for the company's own
   sales documents (``sync.accounting``), each with its PDF, read before the event. Both keep the §47 state, so a
   stopped sync shows like a mailbox's; a month never closes green while the accounting software stopped syncing.
7. **Searching before asking.** Every missing document is searched for in each place the business connected
   (server/search.py) before any supplier is asked for it (§22); each round is one ``search.recorded`` event.
8. **Erasures.** Accounts erased since the last pass still have their files:
   the worker removes them, in AWS as the evidence-deletion role
   (server/erasure.py), and marks each erasure record purged.
9. **Push notifications** (server/webhooks.py, server/jobs.py). Each pass first
   works the durable job queue: a push from Gmail or Microsoft Graph queued
   "read this mailbox now", so it is read at once, from its cursor, whatever
   its schedule. The worker creates each mailbox's push subscription (a Gmail
   watch, 7 days; a Graph subscription, about 3 days) and renews it before it
   ends (``sync.webhook`` events keep its state). While push works, polling
   drops to once an hour, as a safety net. **Webhook loss:** when a poll finds
   mail that arrived more than ten minutes ago that no notification announced,
   the subscription is recorded as lost, re-created, and the mailbox is polled
   every few minutes until a notification arrives again.
10. **Backfill.** Days a connection knows it missed (a known gap: history that
   expired, a mailbox back after a long outage, a delta the provider dropped)
   are re-read on the next pass, a week at a time (bounded), recorded as
   ``sync.mail``/``sync.bank`` events marked ``backfill``, each carrying what is
   left of the gap (resumable). Messages are read before they are recorded, as
   in any sync. While a gap is open its months say "Catching up on 3 days of
   email from …" and never turn green; days a bank no longer serves stay open
   until the owner sends a statement for them.
11. **Bank accounts nobody added.** Payments for an account the consent covers
   but the business has not added (opened later, or removed) are delivered
   under ``iban:<IBAN>`` and kept by the engine, which asks the owner where the
   account belongs (checklist S8). Each row carries the bank's own transaction
   id, so a payment is one payment however the bank words it later.
12. **Supplier websites** (server/portals.py): signed in to once a day; a
   one-time code is asked from the owner, and the retrieval resumes once they
   enter it.

The worker never sends email, moves money or approves anything.
"""

from __future__ import annotations

import dataclasses
import hashlib
import logging
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from .jobs import SUBSCRIPTION_RENEW, SYNC_CONNECTION, JobRunner, RetryLater
from .runtime import ReplayDiverged, TenantManager, TenantNotFound
from .store import Job, StoreError, StoreUnavailable, WebhookRoute

__all__ = ["BACKFILL_CHUNK", "BANK_MIN_INTERVAL", "GMAIL_RENEW_BEFORE", "GRAPH_LIFETIME", "GRAPH_RENEW_BEFORE",
           "LINKS_PER_PASS", "LOSS_GRACE", "LOST_INTERVAL", "PUSH_POLL_INTERVAL", "PassReport", "PushSettings",
           "STALE_AFTER", "SyncWorker", "transaction_row"]

log = logging.getLogger("backoffice.server.sync")

# GoCardless serves each account's transactions four times a day: never ask more often.
BANK_MIN_INTERVAL = timedelta(hours=6)
# A connection that has not synced for this long, despite retries, is shown to the owner (§47).
STALE_AFTER = timedelta(hours=24)
LINKS_PER_PASS = 10  # waiting invoice links opened per tenant and pass
# A link whose site did not answer is tried again after 15 minutes, then less and less often (at most every
# 6 hours); after 3 days without an answer the engine gets the invoice another way (orchestrator).
LINK_BACKOFF_BASE = timedelta(minutes=15)
LINK_BACKOFF_MAX = timedelta(hours=6)
_KINDS = {"google": "gmail", "microsoft": "microsoft", "imap": "imap", "open_banking": "open_banking",
          "invoicexpress": "accounting", "moloni": "accounting", "toconline": "accounting",
          "portal": "supplier_portal"}


def _kind_of(connection_kind: str, provider: str) -> str | None:
    """The connector state's kind for a connection: a Google sign-in reads Gmail or Drive, by what it was for."""
    if connection_kind == "files":
        return "cloud_storage" if provider in ("google", "microsoft") else None
    if connection_kind == "accounting":
        return "accounting" if provider in ("invoicexpress", "moloni", "toconline") else None
    return _KINDS.get(provider)


# Push (§47): a mailbox whose push works is still polled hourly (a safety net); one whose push was lost is polled
# every few minutes until a notification arrives again. Mail that arrived this long ago and that no notification
# announced means the push was lost.
PUSH_POLL_INTERVAL = timedelta(hours=1)
LOST_INTERVAL = timedelta(minutes=5)
LOSS_GRACE = timedelta(minutes=10)
GMAIL_RENEW_BEFORE = timedelta(days=1)  # a Gmail watch lasts 7 days; Google asks for a renewal every day
GRAPH_LIFETIME = timedelta(minutes=4200)  # Graph caps mail subscriptions at 4230 minutes (about 3 days)
GRAPH_RENEW_BEFORE = timedelta(hours=12)
BACKFILL_CHUNK = timedelta(days=7)  # days of a mailbox's gap re-read per pass (bounded, resumable)


@dataclass(frozen=True)
class PushSettings:
    """Where providers push (None: that provider stays on polling). Gmail: the Pub/Sub topic its watch publishes
    to (the subscription pushes to /api/webhooks/gmail). Graph: the public URLs of /api/webhooks/microsoft and its
    lifecycle endpoint."""

    gmail_topic: str | None = None
    graph_url: str | None = None
    graph_lifecycle_url: str | None = None


class _Gone(Exception):
    """The connection or tenant disappeared while its sync ran (removed, erased)."""


@dataclass
class PassReport:
    """What one pass did (logged; the tests read it)."""

    tenants: int = 0
    ticks: int = 0
    synced: list[str] = field(default_factory=list)  # "tenant/connection"
    messages: int = 0
    rows: int = 0
    retrying: list[str] = field(default_factory=list)
    needs_owner: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    errors: int = 0
    erasures: list[str] = field(default_factory=list)  # erased businesses whose files were purged
    thread_messages: int = 0  # earlier messages of a thread fetched because a reply pointed at them
    links: int = 0  # waiting invoice links opened and settled
    files: int = 0  # new files read from watched cloud storage folders
    documents: int = 0  # the company's own documents read from its accounting software
    searches: int = 0  # missing documents searched for in the connected places (§22)
    found: int = 0  # ... found there
    jobs_done: list[int] = field(default_factory=list)  # queued jobs (push notifications) done this pass
    jobs_retried: list[int] = field(default_factory=list)
    jobs_dead: list[int] = field(default_factory=list)  # parked as dead letters this pass
    subscribed: list[str] = field(default_factory=list)  # push subscriptions created or renewed
    webhook_lost: list[str] = field(default_factory=list)  # a poll found mail no notification announced
    backfilled: list[str] = field(default_factory=list)  # a chunk of a known gap re-read
    codes: list[str] = field(default_factory=list)  # supplier websites that asked the owner for a code


@dataclass(frozen=True)
class _Connection:
    tenant_id: str
    id: str
    kind: str  # "email" | "bank"
    name: str
    account: str
    healthy: bool
    saved: Mapping[str, Any] | None  # the connector state the last sync recorded
    ibans: Mapping[str, str]  # our bank accounts by IBAN (banks only)
    # Whose mailbox it reads (checklist O2): "shared" or "delegated" (Microsoft 365 through /users/{address}; a
    # Google mailbox delegated to the signed-in account), "alias" (Gmail: only mail delivered to the address).
    mailbox: str = "own"
    # What the owner chose for it (no secrets): a watched folder or SharePoint library (cloud storage), the
    # accounting software's account name, the company's tax number (to find it in Moloni).
    options: Mapping[str, Any] = field(default_factory=dict)
    sign_in: Mapping[str, Any] = field(default_factory=dict)  # the engine's sign-in state (a code awaited...)
    # What the owner chose for how the business is read (checklist A9, B8): how far back a first read goes (None:
    # the server's default) and whether spam is read and searched too (never the trash).
    history_days: int | None = None
    spam: bool = False

    @property
    def key(self) -> str:
        return f"{self.tenant_id}/{self.id}"


def transaction_row(tx: Any, *, bank_tx_id: str | None = None) -> dict[str, Any]:
    """A connector's :class:`~backoffice.domain.models.Transaction` as the JSON of the engine's BankRow.
    ``bank_tx_id``: the bank's own id for it (deduplication keys on it)."""
    row = {"bank_id": tx.id, "account_id": tx.account_id, "booked_on": tx.booked_on.isoformat(),
           "amount": str(tx.amount), "currency": tx.currency, "counterparty": tx.counterparty,
           "description": tx.description, "kind": tx.kind.value, "card_last4": tx.card_last4,
           "counterparty_iban": tx.counterparty_iban, "reference": tx.reference}
    if bank_tx_id:
        row["bank_tx_id"] = bank_tx_id
    return row


def _unknown_account(iban: str | None, provider_id: str) -> str:
    """Where payments of an account the business has not added are delivered (kept by the engine, S8)."""
    from backoffice.fraud import normalize_iban
    from backoffice.service import UNKNOWN_BANK_ACCOUNT, UNKNOWN_IBAN

    return f"{UNKNOWN_IBAN}{normalize_iban(iban)}" if iban else f"{UNKNOWN_BANK_ACCOUNT}{provider_id}"


class SyncWorker:
    """One pass: every tenant's day, then every connection that is due (module docstring)."""

    def __init__(
        self,
        manager: TenantManager,
        *,
        vault: Any = None,
        aggregator_factory: Callable[[], Any] | None = None,
        oauth_apps: Mapping[str, Any] | None = None,
        http_client: Any = None,
        imap_factory: Callable[[Any], Any] | None = None,
        interval: timedelta = timedelta(minutes=15),
        history_days: int = 90,
        mail_batch: int = 10,
        bank_batch: int = 500,
        backoff_base: timedelta = timedelta(minutes=1),
        backoff_max: timedelta = timedelta(hours=1),
        purger: Any = None,
        portal_factory: Callable[[str, Any], Any] | None = None,
        search_missing: bool = True,
        push: PushSettings | None = None,
        portals: Any = None,
        push_interval: timedelta = PUSH_POLL_INTERVAL,
        lost_interval: timedelta = LOST_INTERVAL,
        backfill_chunk: timedelta = BACKFILL_CHUNK,
    ) -> None:
        self.manager = manager
        self.purger = purger  # server/erasure.py: finishes account erasures (None: not this worker's job)
        self.store = manager.store
        self.vault = vault if vault is not None else manager.vault
        self.aggregator_factory = aggregator_factory
        self.oauth_apps = dict(oauth_apps or {})
        self.http_client = http_client
        self.imap_factory = imap_factory
        self.interval = interval
        self.history = timedelta(days=history_days)
        self.mail_batch = max(1, mail_batch)
        self.bank_batch = max(1, bank_batch)
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self._retry: dict[str, tuple[int, datetime]] = {}  # connection -> (failures in a row, not before)
        # connection -> (vault record version, token provider): an access token lives an hour, so it is
        # reused across syncs; a new sign-in (a new vault version) starts a new provider.
        self._tokens: dict[str, tuple[int, Any]] = {}
        # Searching every connected place for missing documents before any supplier is asked (§22).
        from .search import MissingSearches

        self.searches = MissingSearches(self, portal_factory=portal_factory) if search_missing else None
        self.push = push or PushSettings()
        self.portals = portals  # server/portals.py PortalWorker, or None (supplier websites not read here)
        self.push_interval = max(interval, push_interval)
        self.lost_interval = min(interval, lost_interval)
        self.backfill_chunk = backfill_chunk
        self.jobs = JobRunner(self.store, {SYNC_CONNECTION: self._job_sync, SUBSCRIPTION_RENEW: self._job_renew},
                              now=self.now)
        self._report: PassReport | None = None

    def now(self) -> datetime:
        return self.manager.now().astimezone(timezone.utc)

    # ----------------------------------------------------------------- a pass

    def run_once(self, should_stop: Callable[[], bool] | None = None) -> PassReport:
        report = PassReport()
        self.complete_erasures(report)
        self.run_jobs(report, should_stop)
        try:
            tenant_ids = self.store.tenant_ids()
        except StoreError:
            log.warning("sync_tenants_unavailable")
            return report
        for tenant_id in tenant_ids:
            if should_stop is not None and should_stop():
                break
            report.tenants += 1
            try:
                self.sync_tenant(tenant_id, report)
            except (TenantNotFound, ReplayDiverged, _Gone):
                continue  # erased meanwhile, or refused and already alerted (replay_diverged)
            except StoreUnavailable:
                log.warning("sync_store_unavailable", extra={"tenant": tenant_id})
                report.errors += 1
                break  # the next pass tries again
            except Exception:
                log.exception("sync_tenant_failed", extra={"tenant": tenant_id})
                report.errors += 1
        log.info("sync_pass", extra={"tenants": report.tenants, "connections": len(report.synced),
                                     "messages": report.messages, "rows": report.rows})
        return report

    def complete_erasures(self, report: PassReport) -> None:
        """Remove the files of every erased business that still has them, then mark its record purged."""
        if self.purger is None:
            return
        try:
            pending = self.store.pending_erasures()
        except StoreError:
            log.warning("erasures_unavailable")
            return
        now = self.now()
        for tenant_id in pending:
            key = f"erasure/{tenant_id}"
            retry = self._retry.get(key)
            if retry is not None and retry[1] > now:
                continue
            self.manager.evict(tenant_id)
            try:
                removed = self.purger.purge(tenant_id)
                self.store.mark_objects_purged(tenant_id, self.manager.now())
            except StoreUnavailable:  # the database is away: the next pass tries again
                log.warning("erasures_store_unavailable")
                report.errors += 1
                return
            except Exception as exc:  # stays pending; tried again after a pause
                failures = (retry[0] if retry else 0) + 1
                self._retry[key] = (failures, now + min(self.backoff_max, self.backoff_base * (2 ** (failures - 1))))
                log.warning("erasure_purge_failed", extra={"tenant": tenant_id, "exc_type": type(exc).__name__})
                report.errors += 1
                continue
            self._retry.pop(key, None)
            report.erasures.append(tenant_id)
            log.info("erasure_completed", extra={"tenant": tenant_id, "rows": removed})

    # ----------------------------------------------------------------- the durable queue (server/jobs.py)

    def run_jobs(self, report: PassReport | None = None, should_stop: Callable[[], bool] | None = None) -> PassReport:
        """Work the queue: syncs a push asked for, subscriptions a provider asked to renew."""
        report = report if report is not None else PassReport()
        self._report = report
        try:
            done = self.jobs.run_due(should_stop)
        finally:
            self._report = None
        report.jobs_done += done.done
        report.jobs_retried += done.retried
        report.jobs_dead += done.dead
        return report

    def _job_connection(self, job: Job) -> _Connection | None:
        cid = str(job.payload.get("connectionId") or "")
        try:
            found = self.manager.read(job.tenant_id, lambda svc: _connections(job.tenant_id, svc), what="job plan")
        except TenantNotFound:
            return None  # erased meanwhile: nothing left to do
        except (ReplayDiverged, StoreUnavailable) as exc:
            raise RetryLater(type(exc).__name__) from None
        return next((c for c in found if c.id == cid), None)

    def _job_sync(self, job: Job) -> None:
        """A push said this mailbox changed (or that notifications were missed): read it now, from its cursor."""
        c = self._job_connection(job)
        if c is None:
            return  # removed meanwhile
        notified = job.payload.get("notifiedAt")
        at = datetime.fromisoformat(notified) if isinstance(notified, str) and notified else self.now()
        report = self._report or PassReport()
        try:
            outcome = self._sync(c, report, push_at=at, reason=str(job.payload.get("reason") or "push"))
        except (TenantNotFound, _Gone):
            return
        except (ReplayDiverged, StoreUnavailable) as exc:
            raise RetryLater(type(exc).__name__) from None
        if outcome is not None and not outcome.ok and not outcome.error.needs_reconnect:
            retry = outcome.error.retry_after
            raise RetryLater(outcome.error.code, retry_after=timedelta(seconds=float(retry)) if retry else None)

    def _job_renew(self, job: Job) -> None:
        """The provider asked to renew a subscription (or said it is gone): renewed or re-created now."""
        c = self._job_connection(job)
        if c is None or c.kind != "email":
            return
        meta = self.vault.metadata(c.tenant_id, c.id) if self.vault is not None else None
        if meta is None or meta.provider not in ("google", "microsoft"):
            return
        from backoffice.connectors.base import ConnectorError, WebhookState, record_webhook

        state = self._state(c, meta.provider)
        if job.payload.get("reason") == "subscriptionRemoved":
            state = record_webhook(state, webhook_state=WebhookState.EXPIRED)
        try:
            connector = self._connector(c, meta)
        except _Unconfigured:
            return
        except ConnectorError as exc:
            if exc.needs_reconnect:
                return  # the owner reconnects first; the next sync subscribes again
            raise RetryLater(exc.code) from None
        report = self._report or PassReport()
        if self._ensure_push(c, connector, meta, state, self.now(), report, force=True) is None:
            raise RetryLater("subscribe_failed")

    def sync_tenant(self, tenant_id: str, report: PassReport | None = None) -> PassReport:
        report = report if report is not None else PassReport()
        with self.manager.open(tenant_id) as rt:
            before = rt.seq
            self.manager.tick_if_due(rt)
            if rt.seq != before:
                report.ticks += 1
        for connection in self.manager.read(tenant_id, lambda svc: _connections(tenant_id, svc), what="sync plan"):
            self._sync(connection, report)
        self._follow_links(tenant_id, report)
        if self.searches is not None:
            self.searches.run(tenant_id, report)
        return report

    # ----------------------------------------------------------------- links that wait (§9)

    def _follow_links(self, tenant_id: str, report: PassReport) -> None:
        """Open invoice links that are waiting, with backoff per link; what came back is one event."""
        if self.manager.link_fetcher is None:
            return
        now = self.now()
        waiting = self.manager.read(tenant_id, lambda svc: svc.links_waiting(), what="links plan")
        due = [u for u in waiting if (r := self._retry.get(f"link/{tenant_id}/{u}")) is None or r[1] <= now]
        due = due[:LINKS_PER_PASS]
        if not due:
            return
        status, body = self.manager.follow_links(tenant_id, due)
        if status == 404:
            raise _Gone(tenant_id)
        still = set(body.get("waiting") or ()) if status == 200 else set(due)
        for url in due:
            key = f"link/{tenant_id}/{url}"
            if url in still:
                failures = self._retry.get(key, (0, now))[0] + 1
                self._retry[key] = (failures, now + min(LINK_BACKOFF_MAX, LINK_BACKOFF_BASE * (2 ** (failures - 1))))
            else:
                self._retry.pop(key, None)
                report.links += 1

    # ----------------------------------------------------------------- one connection

    def _sync(self, c: _Connection, report: PassReport, *, push_at: datetime | None = None,
              reason: str | None = None) -> Any:
        """Sync one connection when it is due (or now: ``push_at``, a push said it changed). Returns the outcome,
        or None when nothing was tried (not due, waiting for the owner, nothing to sign in with)."""
        now = self.now()
        retry = self._retry.get(c.key)
        if push_at is None and retry is not None and retry[1] > now:
            report.retrying.append(c.key)
            return None
        meta = self.vault.metadata(c.tenant_id, c.id) if self.vault is not None else None
        if meta is None or _kind_of(c.kind, meta.provider) is None:  # nothing to sign in with (by hand, or pending)
            report.skipped.append(c.key)
            return None
        if c.kind == "files" and not c.options.get("folder"):
            return None  # searched for missing documents only: no folder to watch
        if c.kind == "portal":
            return self._sync_portal(c, meta, now, report)
        state = self._state(c, meta.provider)
        if c.kind == "email" and meta.expires_at is not None and state.auth_expires_at != meta.expires_at:
            # An OAuth grant with a stated end: the state the sync records carries it (the owner is reminded, R4).
            state = state.model_copy(update={"auth_expires_at": meta.expires_at})
        if state.reconnect_required:  # waiting for the owner to reconnect (they were told)
            report.skipped.append(c.key)
            return None
        from backoffice.connectors.base import ConnectorError, SyncOutcome, TransientError, record_event, record_failure
        from backoffice.connectors.vault import VaultError

        if push_at is not None:  # the push is the event (§47); a push after a loss confirms push works again
            state = record_event(state, at=min(push_at, now))
            lost = state.webhook_lost_at
            if lost is not None and state.last_event_at and state.last_event_at > lost:
                state = state.model_copy(update={"webhook_lost_at": None})
            if reason == "missed":  # the provider lost notifications: read now, then polled more often until a
                state = state.model_copy(update={"webhook_lost_at": now})  # notification arrives again
        known_gaps = set(state.known_gaps)  # gaps known before this pass: backfilled now (new ones next pass)
        due = push_at is not None or self._due(c, state, now)
        if not due and not (c.kind == "email" and self._gaps_to_read(state, known_gaps)):
            return None
        try:
            connector = self._connector(c, meta)
        except _Unconfigured as exc:
            log.warning("sync_not_configured", extra={"tenant": c.tenant_id, "reason": str(exc)})
            report.skipped.append(c.key)
            return None
        except (ConnectorError, VaultError) as exc:  # no refresh token, bank lookup failed, vault unreachable
            error = exc if isinstance(exc, ConnectorError) else TransientError("vault_unavailable")
            outcome = SyncOutcome(record_failure(state, at=now, error=error), 0, error=error)
            self._failed(c, outcome, now, report)
            return outcome
        outcome = None
        if due:
            if c.kind == "email":
                outcome = self._sync_mail(c, connector, state, now, report, polled=push_at is None)
            elif c.kind == "files":
                outcome = self._sync_files(c, connector, state, now, report)
            elif c.kind == "accounting":
                outcome = self._sync_accounting(c, connector, state, now, report)
            else:
                outcome = self._sync_bank(c, connector, state, now, report)
            if c.key in self._tokens:  # a rotated refresh token was saved: remember the vault's new version
                if not outcome.ok:
                    self._tokens.pop(c.key, None)  # start from the vault again next time
                elif (after := self.vault.metadata(c.tenant_id, c.id)) is not None:
                    self._tokens[c.key] = (after.version, self._tokens[c.key][1])
            if not outcome.ok:
                self._failed(c, outcome, now, report)
                return outcome
            self._retry.pop(c.key, None)
            report.synced.append(c.key)
            state = outcome.state
        if c.kind == "email":
            pushed = self._ensure_push(c, connector, meta, state, now, report)
            state = pushed if pushed is not None else state
        self._backfill(c, connector, state, known_gaps, now, report)
        return outcome if outcome is not None else SyncOutcome(state, 0)

    def _sync_mail(self, c: _Connection, connector: Any, state: Any, now: datetime, report: PassReport, *,
                   polled: bool = True) -> Any:
        batch: list[bytes] = []
        seen: set[str] = set()  # messages in this sync (by SHA-256): a thread's messages are sent once
        arrived: list[datetime] = []  # when each delivered message reached the mailbox (webhook loss)

        def flush(final_state: Any = None) -> None:
            if not batch and final_state is None:
                return
            status, _ = self.manager.record_mail(c.tenant_id, c.id, list(batch), final_state)
            if status == 404:
                raise _Gone(c.key)
            report.messages += len(batch)
            batch.clear()

        def sink(item: Any) -> None:
            digest = hashlib.sha256(item.raw).hexdigest()
            if digest in seen:
                return  # already in this sync, as the earlier message of a thread
            seen.add(digest)
            if item.received_at is not None:
                arrived.append(item.received_at)
            earlier = self._earlier_in_thread(c, connector, item, seen)
            report.thread_messages += len(earlier)
            batch.extend(earlier)  # the earlier message first, then the reply that points at it
            batch.append(item.raw)
            if len(batch) >= self.mail_batch:
                flush()

        outcome = connector.sync(state, sink, now=now)
        if outcome.ok and polled:
            outcome = dataclasses.replace(outcome, state=self._detect_loss(c, state, outcome.state, arrived, now,
                                                                           report))
        flush(outcome.state.model_dump(mode="json") if outcome.ok else None)  # a failure keeps what arrived
        return outcome

    # ----------------------------------------------------------------- push subscriptions (§47)

    def _detect_loss(self, c: _Connection, before: Any, after: Any, arrived: list[datetime], now: datetime,
                     report: PassReport) -> Any:
        """A poll found mail that arrived a while ago, after the subscription started and after the last
        notification, that no notification announced: the push was lost. Recorded; re-subscribed next."""
        from backoffice.connectors.base import WebhookState, record_webhook_lost

        if before.webhook_state is not WebhookState.ACTIVE or before.webhook_since is None:
            return after
        try:
            route = self.store.connection_webhook(c.tenant_id, c.id)
        except StoreError:
            route = None
        notified = [t for t in (before.last_event_at, route.last_notified_at if route else None) if t is not None]
        floor = max([before.webhook_since, *notified])
        late = [t for t in arrived if floor < t <= now - LOSS_GRACE]
        if not late:
            return after
        report.webhook_lost.append(c.key)
        log.warning("webhook_lost", extra={"tenant": c.tenant_id, "messages": len(late)})
        return record_webhook_lost(after, at=now)

    def _ensure_push(self, c: _Connection, connector: Any, meta: Any, state: Any, now: datetime, report: PassReport,
                     *, force: bool = False) -> Any:
        """Create or renew the mailbox's push subscription before it ends; re-create one that failed or was lost.
        Returns the new state (recorded as ``sync.webhook``), or None when nothing changed or it could not be done
        (polling goes on as usual)."""
        from backoffice.connectors.base import ConnectorError, WebhookState

        provider = meta.provider
        if provider == "google" and not self.push.gmail_topic:
            return None
        if provider == "microsoft" and not self.push.graph_url:
            return None
        if provider not in ("google", "microsoft"):
            return None
        active = state.webhook_state is WebhookState.ACTIVE and state.webhook_expires_at is not None
        renew_before = GMAIL_RENEW_BEFORE if provider == "google" else GRAPH_RENEW_BEFORE
        if active and not force and state.webhook_expires_at - now > renew_before:
            return None
        live = active and state.webhook_expires_at > now
        try:
            if provider == "google":
                new = connector.start_watch(state, self.push.gmail_topic)
                address = connector.mailbox_address()
                route = WebhookRoute("gmail", address, c.tenant_id, c.id, "", new.webhook_expires_at, None, now)
                new = new.model_copy(update={"webhook_since": state.webhook_since if live and state.webhook_since
                                             else now})
            else:
                new, route = self._graph_subscription(c, connector, state, now, live)
            self.store.save_webhook_route(route)
        except ConnectorError as exc:
            log.warning("push_subscribe_failed", extra={"tenant": c.tenant_id, "reason": exc.code})
            return None
        except StoreError:
            log.warning("push_route_unavailable", extra={"tenant": c.tenant_id})
            return None
        status, _ = self.manager.record_webhook_state(c.tenant_id, c.id, new.model_dump(mode="json"))
        if status == 404:
            raise _Gone(c.key)
        report.subscribed.append(c.key)
        return new

    def _graph_subscription(self, c: _Connection, connector: Any, state: Any, now: datetime,
                            live: bool) -> tuple[Any, WebhookRoute]:
        from backoffice.connectors.base import ConnectorError

        from .webhooks import client_state_hash

        if live and state.subscription_id:
            try:
                new = connector.renew_subscription(state, state.subscription_id, lifetime=GRAPH_LIFETIME, now=now)
                route = self.store.connection_webhook(c.tenant_id, c.id)
                if route is not None and route.key == state.subscription_id:
                    return new, dataclasses.replace(route, expires_at=new.webhook_expires_at)
            except ConnectorError:
                pass  # gone at Microsoft: a new subscription below
        secret = secrets.token_urlsafe(32)  # Graph echoes it with every notification; only its hash is kept
        new, subscription = connector.create_subscription(
            state, notification_url=self.push.graph_url, client_state=secret, lifetime=GRAPH_LIFETIME,
            lifecycle_url=self.push.graph_lifecycle_url, now=now)
        new = new.model_copy(update={"subscription_id": subscription.subscription_id, "webhook_since": now})
        route = WebhookRoute("microsoft", subscription.subscription_id, c.tenant_id, c.id, client_state_hash(secret),
                             subscription.expires_at, None, now)
        return new, route

    # ----------------------------------------------------------------- backfill (§47)

    @staticmethod
    def _gaps_to_read(state: Any, known: set[Any]) -> list[Any]:
        """Known gaps a backfill can still close, known before this pass, oldest first."""
        return sorted((g for g in state.known_gaps if g in known and g not in state.unreachable_gaps),
                      key=lambda g: (g.start, g.end))

    def _backfill(self, c: _Connection, connector: Any, state: Any, known: set[Any], now: datetime,
                  report: PassReport) -> None:
        """Re-read the oldest known gap: a mailbox a week at a time, a bank in one go (it says itself what it no
        longer serves). Recorded with what is left of the gap, so the next pass resumes there."""
        from backoffice.connectors.base import TimeRange, record_backfill_progress

        gaps = self._gaps_to_read(state, known)
        if not gaps or not hasattr(connector, "backfill"):
            return
        gap = gaps[0]
        window = gap
        if c.kind == "email" and gap.end - gap.start > self.backfill_chunk:
            window = TimeRange(start=gap.start, end=gap.start + self.backfill_chunk)
        marker = {"start": window.start.isoformat(), "end": window.end.isoformat()}
        if c.kind == "email":
            batch: list[bytes] = []

            def flush(final_state: Any = None) -> None:
                if not batch and final_state is None:
                    return
                status, _ = self.manager.record_mail(c.tenant_id, c.id, list(batch), final_state, backfill=marker)
                if status == 404:
                    raise _Gone(c.key)
                report.messages += len(batch)
                batch.clear()

            def sink(item: Any) -> None:
                batch.append(item.raw)
                if len(batch) >= self.mail_batch:
                    flush()

            outcome = connector.backfill(state, window, sink, now=now)
            if not outcome.ok:  # kept as it was: the next pass tries the same days again
                log.warning("backfill_failed", extra={"tenant": c.tenant_id, "reason": outcome.error.code})
                flush()
                return
            new = outcome.state
            if window != gap:
                new = record_backfill_progress(new, gap, window.end)
            flush(new.model_dump(mode="json"))
        else:
            rows: list[dict[str, Any]] = []
            outcome = connector.backfill(state, window,
                                         lambda tx: rows.append(transaction_row(
                                             tx, bank_tx_id=getattr(connector, "bank_ids", {}).get(tx.id))),
                                         now=now)
            if not outcome.ok:
                log.warning("backfill_failed", extra={"tenant": c.tenant_id, "reason": outcome.error.code})
                return
            status, _ = self.manager.record_bank(c.tenant_id, c.id, rows, outcome.state.model_dump(mode="json"),
                                                 backfill=marker)
            if status == 404:
                raise _Gone(c.key)
            report.rows += len(rows)
        report.backfilled.append(c.key)
        log.info("backfill_done", extra={"tenant": c.tenant_id, "connections": 1})

    # ----------------------------------------------------------------- supplier websites (server/portals.py)

    def _sync_portal(self, c: _Connection, meta: Any, now: datetime, report: PassReport) -> Any:
        if self.portals is None or meta.provider != "portal":
            report.skipped.append(c.key)
            return None
        state = self._state(c, "portal")
        if state.reconnect_required or not self.portals.due(state, c.sign_in, now):
            return None
        try:
            outcome = self.portals.sync(c.tenant_id, c.id)
        except (TenantNotFound, ReplayDiverged, StoreUnavailable, _Gone):
            raise
        except Exception:  # an adapter bug never stops the other connections
            log.exception("portal_failed", extra={"tenant": c.tenant_id})
            report.errors += 1
            return None
        if outcome is None:
            report.skipped.append(c.key)
        elif outcome.challenge is not None:
            report.codes.append(c.key)
        elif outcome.outcome.ok:
            report.synced.append(c.key)
        else:
            report.retrying.append(c.key)
        return outcome.outcome if outcome is not None else None

    def _earlier_in_thread(self, c: _Connection, connector: Any, item: Any, seen: set[str]) -> list[bytes]:
        """A reply pointing at an invoice sent earlier in its thread ("see the invoice I sent on the 3rd"):
        the thread's earlier messages the business does not have yet, fetched through the connector now,
        before the event is recorded (§8 "previous attachments"), even from before the history window."""
        fetch = getattr(connector, "thread_messages", None)
        if fetch is None or not getattr(item, "thread_id", None):
            return []
        from backoffice.connectors.base import ConnectorError
        from backoffice.evidence.email import EmailParseError, parse_eml
        from backoffice.evidence.retrieval import refers_to_earlier_invoice

        try:
            if not refers_to_earlier_invoice(parse_eml(item.raw)):
                return []
        except EmailParseError:
            return []
        try:
            messages = fetch(item.thread_id)
        except ConnectorError as exc:  # the reply itself still arrives; the thread is tried with the next reply
            log.warning("thread_fetch_failed", extra={"tenant": c.tenant_id, "reason": exc.code})
            return []
        earlier: list[tuple[str, bytes]] = []
        for message in messages:
            if message.provider_id == item.provider_id or message.raw == item.raw:
                continue
            if item.received_at and message.received_at and message.received_at > item.received_at:
                continue  # only what came before the reply
            digest = hashlib.sha256(message.raw).hexdigest()
            if digest not in seen:
                seen.add(digest)
                earlier.append((digest, message.raw))
        if not earlier:
            return []

        def known(svc: Any) -> set[str]:
            index = svc.repo.registry.index
            return {d for d, _ in earlier if index.find_by_sha256(c.tenant_id, d) is not None}

        have = self.manager.read(c.tenant_id, known, what="thread plan")
        return [raw for digest, raw in earlier if digest not in have]

    def _sync_bank(self, c: _Connection, connector: Any, state: Any, now: datetime, report: PassReport) -> Any:
        batch: list[dict[str, Any]] = []

        def flush(final_state: Any = None) -> None:
            if not batch and final_state is None:
                return
            status, _ = self.manager.record_bank(c.tenant_id, c.id, list(batch), final_state)
            if status == 404:
                raise _Gone(c.key)
            report.rows += len(batch)
            batch.clear()

        def sink(tx: Any) -> None:
            batch.append(transaction_row(tx, bank_tx_id=getattr(connector, "bank_ids", {}).get(tx.id)))
            if len(batch) >= self.bank_batch:
                flush()

        outcome = connector.sync(state, sink, now=now)
        flush(outcome.state.model_dump(mode="json") if outcome.ok else None)
        discovered = getattr(connector, "discovered", None)
        if discovered:  # accounts the consent covers that were not known yet: remembered with the consent
            try:
                accounts = dict(self.vault.open(c.tenant_id, c.id).get("accounts") or {})
                self.vault.update(c.tenant_id, c.id, {"accounts": {**accounts, **discovered}})
            except Exception:  # remembered next time instead
                log.warning("bank_accounts_not_saved", extra={"tenant": c.tenant_id})
        return outcome

    def _sync_files(self, c: _Connection, connector: Any, state: Any, now: datetime, report: PassReport) -> Any:
        """New files in the watched folder (Google Drive, OneDrive), in batches of events."""
        batch: list[Any] = []

        def flush(final_state: Any = None) -> None:
            if not batch and final_state is None:
                return
            status, _ = self.manager.record_files(c.tenant_id, c.id, list(batch), final_state)
            if status == 404:
                raise _Gone(c.key)
            report.files += len(batch)
            batch.clear()

        def sink(download: Any) -> None:
            batch.append(download)
            if len(batch) >= self.mail_batch:
                flush()

        outcome = connector.watch(state, sink, now=now)
        flush(outcome.state.model_dump(mode="json") if outcome.ok else None)
        return outcome

    def _sync_accounting(self, c: _Connection, connector: Any, state: Any, now: datetime, report: PassReport) -> Any:
        """The company's own sales documents from its accounting software, in batches of events."""
        batch: list[Any] = []

        def flush(final_state: Any = None) -> None:
            if not batch and final_state is None:
                return
            status, _ = self.manager.record_accounting(c.tenant_id, c.id, list(batch), final_state)
            if status == 404:
                raise _Gone(c.key)
            report.documents += len(batch)
            batch.clear()

        def sink(document: Any) -> None:
            batch.append(document)
            if len(batch) >= self.mail_batch:
                flush()

        outcome = connector.sync(state, sink, now=now)
        flush(outcome.state.model_dump(mode="json") if outcome.ok else None)
        return outcome

    def _failed(self, c: _Connection, outcome: Any, now: datetime, report: PassReport) -> None:
        error = outcome.error
        state = outcome.state.model_dump(mode="json")
        if error.needs_reconnect:  # only the owner can fix a refused sign-in or an expired consent
            self._retry.pop(c.key, None)
            self.manager.record_sync_failure(c.tenant_id, c.id, state, reconnect=True)
            report.needs_owner.append(c.key)
            log.warning("sync_needs_owner", extra={"tenant": c.tenant_id, "reason": error.code})
            return
        failures = self._retry.get(c.key, (0, now))[0] + 1
        delay = self.backoff_max if not error.retryable else min(self.backoff_max,
                                                                  self.backoff_base * (2 ** (failures - 1)))
        if error.retry_after:
            delay = max(delay, timedelta(seconds=float(error.retry_after)))
        self._retry[c.key] = (failures, now + delay)
        report.retrying.append(c.key)
        log.warning("sync_retry", extra={"tenant": c.tenant_id, "reason": error.code})
        last = outcome.state.last_successful_sync
        if c.healthy and last is not None and now - last >= STALE_AFTER:
            # Not synced for a day: the owner sees it (and the month cannot close) until it syncs again.
            self.manager.record_sync_failure(c.tenant_id, c.id, state, reconnect=True)
            report.needs_owner.append(c.key)

    # ----------------------------------------------------------------- building connectors

    def _state(self, c: _Connection, provider: str) -> Any:
        from backoffice.connectors.base import ConnectorKind, ConnectorState

        kind = ConnectorKind(_kind_of(c.kind, provider) or _KINDS[provider])
        if c.saved:
            try:
                saved = ConnectorState.model_validate(dict(c.saved))
                if saved.kind is kind:
                    return saved
            except ValueError:
                log.warning("sync_state_unreadable", extra={"tenant": c.tenant_id})
        return ConnectorState(tenant_id=c.tenant_id, connector_id=c.id, kind=kind, account=c.account,
                              display_name=c.name)

    def _due(self, c: _Connection, state: Any, now: datetime) -> bool:
        if state.last_successful_sync is None:
            return True
        if c.kind == "bank":
            every = max(self.interval, BANK_MIN_INTERVAL)
        else:
            every = self._mail_interval(state, now)
        return now - state.last_successful_sync >= every

    def search_connector(self, c: _Connection) -> tuple[Any, str]:
        """The connector of a searchable connection, for the missing-document searches (server/search.py), with
        the provider's name. Raises what building it raises (no sign-in: the attempt is noted as failed)."""
        meta = self.vault.metadata(c.tenant_id, c.id) if self.vault is not None else None
        if meta is None or _kind_of(c.kind, meta.provider) is None:
            from backoffice.connectors.base import ReconnectRequired

            raise ReconnectRequired("search_no_sign_in")
        connector = self._connector(c, meta)
        if c.kind == "email" and meta.provider == "google":
            return connector, "gmail"
        return connector, str(meta.provider)

    def saved_state(self, c: _Connection) -> dict[str, Any]:
        """The connection's sync state marked as needing the owner (a sign-in refused while searching)."""
        from backoffice.connectors.base import ReconnectRequired, record_failure

        meta = self.vault.metadata(c.tenant_id, c.id) if self.vault is not None else None
        state = self._state(c, meta.provider if meta is not None else "google")
        state = record_failure(state, at=self.now(), error=ReconnectRequired("search_sign_in_refused"))
        return state.model_dump(mode="json")

    def _history(self, c: _Connection) -> timedelta:
        """How far back a first read of this connection goes: what the owner chose (§6: the last 90 days or the
        last 12 months, checklist A9), else this server's default (BACKOFFICE_HISTORY_DAYS)."""
        return timedelta(days=c.history_days) if c.history_days else self.history

    def _mail_interval(self, state: Any, now: datetime) -> timedelta:
        """Hourly while push works (a safety net); every few minutes after a loss, until a push arrives again."""
        from backoffice.connectors.base import WebhookState

        lost = state.webhook_lost_at is not None and not (state.last_event_at and
                                                          state.last_event_at > state.webhook_lost_at)
        if lost:
            return self.lost_interval
        if state.webhook_state is WebhookState.ACTIVE and state.webhook_expires_at is not None and \
                state.webhook_expires_at > now:
            return self.push_interval
        return self.interval

    def _connector(self, c: _Connection, meta: Any) -> Any:
        provider = meta.provider
        if provider == "open_banking":
            return self._bank_connector(c)
        if c.kind == "accounting":
            return self._accounting_connector(c, meta)
        if provider == "imap":
            from backoffice.connectors.imap import IMAPAuth, IMAPConfig, IMAPConnector

            secret = self.vault.open(c.tenant_id, c.id)
            host = str(secret.get("host") or "")
            if not host:
                raise _Unconfigured("imap_without_host")
            config = IMAPConfig(host=host, history_window=self._history(c), include_junk=c.spam)
            auth = IMAPAuth(str(secret.get("username") or c.account), password=secret.get("password"))
            kwargs: dict[str, Any] = {"client_factory": self.imap_factory} if self.imap_factory else {}
            return IMAPConnector(config, auth, clock=self.now, **kwargs)
        app = self.oauth_apps.get(provider)
        if app is None:
            raise _Unconfigured(f"{provider}_oauth_app_missing")
        from backoffice.connectors.base import ReconnectRequired
        from backoffice.connectors.oauth import OAuthClientConfig, OAuthRefresher

        from backoffice.connectors.microsoft import (
            GRAPH_MAIL_SCOPES,
            GRAPH_SHARED_MAIL_SCOPES,
            GraphMailConfig,
            MicrosoftMailConnector,
        )

        shared = c.mailbox in ("shared", "delegated")  # another mailbox, read with the signed-in user's access (O2)
        cached = self._tokens.get(c.key)
        if cached is not None and cached[0] == meta.version:
            tokens = cached[1]
        else:
            if not self.vault.open(c.tenant_id, c.id).get("refresh_token"):
                raise ReconnectRequired(f"{provider}_no_refresh_token")
            scopes = (GRAPH_SHARED_MAIL_SCOPES if shared else GRAPH_MAIL_SCOPES) if provider == "microsoft" else ()
            if c.kind == "files" and provider == "microsoft":
                from backoffice.connectors.cloud_storage import GRAPH_FILES_SCOPE

                scopes = ("offline_access", GRAPH_FILES_SCOPE)
            refresher = OAuthRefresher(OAuthClientConfig(app.client_id, app.client_secret, app.token_url, scopes=scopes),
                                       client=self.http_client, provider=provider, clock=self.now)
            # Rotations are saved in the vault; the token's lifetime is read by the worker's own clock.
            tokens = self.vault.token_provider(c.tenant_id, c.id, refresher, clock=self.now)
            self._tokens[c.key] = (meta.version, tokens)
        if c.kind == "files":
            from backoffice.connectors.cloud_storage import CloudStorageConfig, GoogleDriveConnector, OneDriveConnector

            files = CloudStorageConfig(folder=c.options.get("folder") or None, history_window=self._history(c),
                                       drive=str(c.options.get("drive") or "me/drive"))
            if provider == "google":
                return GoogleDriveConnector(tokens, client=self.http_client, config=files, clock=self.now)
            return OneDriveConnector(tokens, client=self.http_client, config=files, clock=self.now)
        if provider == "google":
            from backoffice.connectors.gmail import GmailConfig, GmailConnector

            config = GmailConfig(history_window=self._history(c), include_spam=c.spam,
                                 user_id=c.account if c.mailbox == "delegated" else None,
                                 delivered_to=c.account if c.mailbox == "alias" else None)
            return GmailConnector(tokens, client=self.http_client, config=config, clock=self.now)
        return MicrosoftMailConnector(tokens, client=self.http_client,
                                      config=GraphMailConfig(history_window=self._history(c), include_junk=c.spam,
                                                             mailbox=c.account if shared else None), clock=self.now)

    def _bank_connector(self, c: _Connection) -> Any:
        from backoffice.connectors.open_banking import BankSyncConfig, OpenBankingConnector
        from backoffice.fraud import normalize_iban

        if self.aggregator_factory is None:
            raise _Unconfigured("gocardless_not_configured")
        secret = self.vault.open(c.tenant_id, c.id)
        requisition = str(secret.get("requisition_id") or "")
        if not requisition:
            raise _Unconfigured("bank_without_requisition")
        aggregator = self.aggregator_factory()
        accounts = secret.get("accounts")
        if not isinstance(accounts, Mapping):  # linked before accounts were remembered: look them up once
            consent = aggregator.consent(requisition)
            accounts = {a: info.iban for a in consent.account_ids if (info := aggregator.account(a)).iban}
            self.vault.update(c.tenant_id, c.id, {"accounts": accounts})
        # Every account the consent covers: ours by its IBAN, else kept under "iban:<IBAN>" until the owner says
        # where it belongs (never dropped, S8). One the consent gained later is looked up while syncing.
        mapping = {provider_id: c.ibans.get(normalize_iban(iban)) if iban else None
                   for provider_id, iban in accounts.items()}
        mapping = {provider_id: ours or _unknown_account(accounts.get(provider_id), provider_id)
                   for provider_id, ours in mapping.items()}
        return OpenBankingConnector(aggregator, requisition, account_ids=mapping,
                                    config=BankSyncConfig(history_window=self._history(c)), clock=self.now,
                                    unknown_account=_unknown_account)


def _accounting_connector_for(worker: SyncWorker, c: _Connection, meta: Any) -> Any:
    """InvoiceXpress (the customer's API key), Moloni (OAuth with the server's developer app) or TOConline (the
    company's own API data), from the vault (connectors.accounting)."""
    from backoffice.connectors import accounting as A

    secret = worker.vault.open(c.tenant_id, c.id)
    history = worker._history(c)
    if meta.provider == "invoicexpress":
        connector: Any = A.InvoiceXpressConnector(str(secret.get("account") or c.options.get("account") or ""),
                                                  str(secret.get("api_key") or ""), client=worker.http_client,
                                                  clock=worker.now)
    elif meta.provider == "toconline":
        creds = A.TOConlineCredentials(str(secret.get("client_id") or ""), str(secret.get("client_secret") or ""),
                                       str(secret.get("oauth_url") or ""), str(secret.get("api_url") or ""))
        cached = worker._tokens.get(c.key)
        if cached is not None and cached[0] == meta.version:
            tokens = cached[1]
        else:
            def rotate(token: Any) -> None:
                worker.vault.update(c.tenant_id, c.id, {"refresh_token": token.refresh_token})

            tokens = A.TOConlineTokens(creds, refresh_token=secret.get("refresh_token") or None,
                                       client=worker.http_client, on_rotate=rotate, clock=worker.now)
            worker._tokens[c.key] = (meta.version, tokens)
        connector = A.TOConlineConnector(tokens, creds.api_url, client=worker.http_client, clock=worker.now)
    else:
        app = worker.oauth_apps.get("moloni")
        if app is None:
            raise _Unconfigured("moloni_oauth_app_missing")
        cached = worker._tokens.get(c.key)
        if cached is not None and cached[0] == meta.version:
            tokens = cached[1]
        else:
            if not secret.get("refresh_token"):
                from backoffice.connectors.base import ReconnectRequired

                raise ReconnectRequired("moloni_no_refresh_token")
            refresher = A.MoloniRefresher(app.client_id, app.client_secret, client=worker.http_client,
                                          clock=worker.now)
            tokens = worker.vault.token_provider(c.tenant_id, c.id, refresher, clock=worker.now)
            worker._tokens[c.key] = (meta.version, tokens)
        connector = A.MoloniConnector(tokens, company_tax_id=c.options.get("taxId") or None,
                                      client=worker.http_client, clock=worker.now)
    connector.history_window = history
    return connector


SyncWorker._accounting_connector = _accounting_connector_for  # type: ignore[attr-defined]


class _Unconfigured(Exception):
    """This server cannot sync that connection (no OAuth app, no GoCardless keys): engineering, not the owner."""


_OPTIONS = ("folder", "drive", "account")


def _connections(tenant_id: str, svc: Any, kinds: tuple[str, ...] = ("email", "bank", "files", "accounting", "portal")
                 ) -> list[_Connection]:
    """The tenant's real connections: mailboxes, banks, cloud storage, accounting software (read-only)."""
    repo = svc.repo
    ibans = {a.iban: a.id for a in repo.accounts.values() if a.iban}
    out = []
    for c in repo.connectors.values():
        info = dict(svc.sign_in.get(c.id) or {})
        if c.kind not in kinds or info.get("pending"):
            continue
        options = {k: str(info[k]) for k in _OPTIONS if info.get(k)}
        if c.kind == "accounting" and c.company_ids and c.company_ids[0] in repo.companies:
            options["taxId"] = repo.companies[c.company_ids[0]].tax_id
        out.append(_Connection(tenant_id, c.id, c.kind, c.name, c.account, c.healthy,
                               svc.sync_states.get(c.id), ibans if c.kind == "bank" else {},
                               str(info.get("mailbox") or "own"), options, info,
                               history_days=repo.history_days, spam=bool(repo.look_in_spam)))
    return out


def _searchable_connections(tenant_id: str, svc: Any) -> list[_Connection]:
    """The connections the missing-document searches may use (read-only): mailboxes, cloud storage and accounting
    software that can be searched, and suppliers' websites (each searched for its own supplier's documents)."""
    searchable = {c.id for c in svc.repo.connectors.values() if c.searchable or c.kind == "portal"}
    return [c for c in _connections(tenant_id, svc, ("email", "files", "accounting", "portal")) if c.id in searchable]
