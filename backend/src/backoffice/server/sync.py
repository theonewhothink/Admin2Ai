"""Imports from connected mailboxes and banks, and each tenant's day, with nobody signed in (§6, §8, §45–48).

:class:`SyncWorker` makes one pass over every tenant (``python -m
backoffice.server.worker`` repeats it):

1. **The day.** The tenant's daily tick runs (chasing, deadlines, closing
   months) even when nobody opens the app.
2. **Mailboxes** (Gmail, Microsoft 365, IMAP) and **banks** (GoCardless, PSD2)
   the owner connected with a real sign-in are read with the engine's
   connectors. The first sync reads the history window (90 days by default,
   §6); later syncs continue from the saved cursor. Access tokens are
   refreshed through the vault, which keeps any rotated refresh token.
3. Whatever a sync fetched becomes **events** in the tenant's log, in batches:
   ``sync.mail`` (the raw messages, stored in the object store; PDFs and photos
   read before the event is recorded) and ``sync.bank`` (booked transactions).
   The last batch carries the connector's new state (cursor, coverage, gaps),
   so a replay rebuilds exactly what the owner saw and the next sync continues
   where this one ended.
4. **Failures.** A network problem or provider outage is retried with
   exponential backoff (respecting ``Retry-After``) and records nothing. A
   refused sign-in or an expired bank consent only the owner can fix: the
   connection is marked as needing them (``sync.failed``), which shows
   "Gmail needs reconnecting" and sends one push notification (§47–48). A
   connection that has not synced for a day despite retries is shown the same
   way; the month can never close green while it is stale.

The worker never sends email, moves money or approves anything.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from .runtime import ReplayDiverged, TenantManager, TenantNotFound
from .store import StoreError, StoreUnavailable

__all__ = ["BANK_MIN_INTERVAL", "PassReport", "STALE_AFTER", "SyncWorker", "transaction_row"]

log = logging.getLogger("backoffice.server.sync")

# GoCardless serves each account's transactions four times a day: never ask more often.
BANK_MIN_INTERVAL = timedelta(hours=6)
# A connection that has not synced for this long, despite retries, is shown to the owner (§47).
STALE_AFTER = timedelta(hours=24)
_KINDS = {"google": "gmail", "microsoft": "microsoft", "imap": "imap", "open_banking": "open_banking"}


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

    @property
    def key(self) -> str:
        return f"{self.tenant_id}/{self.id}"


def transaction_row(tx: Any) -> dict[str, Any]:
    """A connector's :class:`~backoffice.domain.models.Transaction` as the JSON of the engine's BankRow."""
    return {"bank_id": tx.id, "account_id": tx.account_id, "booked_on": tx.booked_on.isoformat(),
            "amount": str(tx.amount), "currency": tx.currency, "counterparty": tx.counterparty,
            "description": tx.description, "kind": tx.kind.value, "card_last4": tx.card_last4,
            "counterparty_iban": tx.counterparty_iban, "reference": tx.reference}


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
    ) -> None:
        self.manager = manager
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

    def now(self) -> datetime:
        return self.manager.now().astimezone(timezone.utc)

    # ----------------------------------------------------------------- a pass

    def run_once(self, should_stop: Callable[[], bool] | None = None) -> PassReport:
        report = PassReport()
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

    def sync_tenant(self, tenant_id: str, report: PassReport | None = None) -> PassReport:
        report = report if report is not None else PassReport()
        with self.manager.open(tenant_id) as rt:
            before = rt.seq
            self.manager.tick_if_due(rt)
            if rt.seq != before:
                report.ticks += 1
        for connection in self.manager.read(tenant_id, lambda svc: _connections(tenant_id, svc), what="sync plan"):
            self._sync(connection, report)
        return report

    # ----------------------------------------------------------------- one connection

    def _sync(self, c: _Connection, report: PassReport) -> None:
        now = self.now()
        retry = self._retry.get(c.key)
        if retry is not None and retry[1] > now:
            report.retrying.append(c.key)
            return
        meta = self.vault.metadata(c.tenant_id, c.id) if self.vault is not None else None
        if meta is None or meta.provider not in _KINDS:  # nothing to sign in with (added by hand, or pending)
            report.skipped.append(c.key)
            return
        state = self._state(c, meta.provider)
        if state.reconnect_required:  # waiting for the owner to reconnect (they were told)
            report.skipped.append(c.key)
            return
        if not self._due(c, state, now):
            return
        from backoffice.connectors.base import ConnectorError, SyncOutcome, TransientError, record_failure
        from backoffice.connectors.vault import VaultError

        try:
            connector = self._connector(c, meta)
        except _Unconfigured as exc:
            log.warning("sync_not_configured", extra={"tenant": c.tenant_id, "reason": str(exc)})
            report.skipped.append(c.key)
            return
        except (ConnectorError, VaultError) as exc:  # no refresh token, bank lookup failed, vault unreachable
            error = exc if isinstance(exc, ConnectorError) else TransientError("vault_unavailable")
            self._failed(c, SyncOutcome(record_failure(state, at=now, error=error), 0, error=error), now, report)
            return
        if c.kind == "email":
            outcome = self._sync_mail(c, connector, state, now, report)
        else:
            outcome = self._sync_bank(c, connector, state, now, report)
        if c.key in self._tokens:  # a rotated refresh token was saved: remember the vault's new version
            if not outcome.ok:
                self._tokens.pop(c.key, None)  # start from the vault again next time
            elif (after := self.vault.metadata(c.tenant_id, c.id)) is not None:
                self._tokens[c.key] = (after.version, self._tokens[c.key][1])
        if outcome.ok:
            self._retry.pop(c.key, None)
            report.synced.append(c.key)
        else:
            self._failed(c, outcome, now, report)

    def _sync_mail(self, c: _Connection, connector: Any, state: Any, now: datetime, report: PassReport) -> Any:
        batch: list[bytes] = []

        def flush(final_state: Any = None) -> None:
            if not batch and final_state is None:
                return
            status, _ = self.manager.record_mail(c.tenant_id, c.id, list(batch), final_state)
            if status == 404:
                raise _Gone(c.key)
            report.messages += len(batch)
            batch.clear()

        def sink(item: Any) -> None:
            batch.append(item.raw)
            if len(batch) >= self.mail_batch:
                flush()

        outcome = connector.sync(state, sink, now=now)
        flush(outcome.state.model_dump(mode="json") if outcome.ok else None)  # a failure keeps what arrived
        return outcome

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
            batch.append(transaction_row(tx))
            if len(batch) >= self.bank_batch:
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

        kind = ConnectorKind(_KINDS[provider])
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
        every = max(self.interval, BANK_MIN_INTERVAL) if c.kind == "bank" else self.interval
        return now - state.last_successful_sync >= every

    def _connector(self, c: _Connection, meta: Any) -> Any:
        provider = meta.provider
        if provider == "open_banking":
            return self._bank_connector(c)
        if provider == "imap":
            from backoffice.connectors.imap import IMAPAuth, IMAPConfig, IMAPConnector

            secret = self.vault.open(c.tenant_id, c.id)
            host = str(secret.get("host") or "")
            if not host:
                raise _Unconfigured("imap_without_host")
            config = IMAPConfig(host=host, history_window=self.history)
            auth = IMAPAuth(str(secret.get("username") or c.account), password=secret.get("password"))
            kwargs: dict[str, Any] = {"client_factory": self.imap_factory} if self.imap_factory else {}
            return IMAPConnector(config, auth, clock=self.now, **kwargs)
        app = self.oauth_apps.get(provider)
        if app is None:
            raise _Unconfigured(f"{provider}_oauth_app_missing")
        from backoffice.connectors.base import ReconnectRequired
        from backoffice.connectors.oauth import OAuthClientConfig, OAuthRefresher

        from backoffice.connectors.microsoft import GRAPH_MAIL_SCOPES, GraphMailConfig, MicrosoftMailConnector

        cached = self._tokens.get(c.key)
        if cached is not None and cached[0] == meta.version:
            tokens = cached[1]
        else:
            if not self.vault.open(c.tenant_id, c.id).get("refresh_token"):
                raise ReconnectRequired(f"{provider}_no_refresh_token")
            scopes = GRAPH_MAIL_SCOPES if provider == "microsoft" else ()
            refresher = OAuthRefresher(OAuthClientConfig(app.client_id, app.client_secret, app.token_url, scopes=scopes),
                                       client=self.http_client, provider=provider, clock=self.now)
            tokens = self.vault.token_provider(c.tenant_id, c.id, refresher)  # rotations are saved in the vault
            self._tokens[c.key] = (meta.version, tokens)
        if provider == "google":
            from backoffice.connectors.gmail import GmailConfig, GmailConnector

            return GmailConnector(tokens, client=self.http_client, config=GmailConfig(history_window=self.history),
                                  clock=self.now)
        return MicrosoftMailConnector(tokens, client=self.http_client,
                                      config=GraphMailConfig(history_window=self.history), clock=self.now)

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
        mapping = {provider_id: c.ibans[normalize_iban(iban)] for provider_id, iban in accounts.items()
                   if iban and normalize_iban(iban) in c.ibans}
        return OpenBankingConnector(aggregator, requisition, account_ids=mapping,
                                    config=BankSyncConfig(history_window=self.history), clock=self.now)


class _Unconfigured(Exception):
    """This server cannot sync that connection (no OAuth app, no GoCardless keys): engineering, not the owner."""


def _connections(tenant_id: str, svc: Any) -> list[_Connection]:
    """The tenant's real mailbox and bank connections (read-only)."""
    repo = svc.repo
    ibans = {a.iban: a.id for a in repo.accounts.values() if a.iban}
    out = []
    for c in repo.connectors.values():
        if c.kind not in ("email", "bank") or (svc.sign_in.get(c.id) or {}).get("pending"):
            continue
        out.append(_Connection(tenant_id, c.id, c.kind, c.name, c.account, c.healthy,
                               svc.sync_states.get(c.id), ibans if c.kind == "bank" else {}))
    return out
