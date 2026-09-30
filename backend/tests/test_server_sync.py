"""The sync worker: real mailboxes and banks become events, with nobody signed in (§6, §8, §45–48).

Fake transports stand in for Google (token endpoint and Gmail API), an IMAP server and GoCardless.
"""

from __future__ import annotations

import base64
import json
import os
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import httpx
import pytest
from _server_support import bearer, harness, signup
from test_ingest_imap import FakeIMAP
from test_server_push import TOKEN_A, FakeExpo

from backoffice.connectors.authorize import PROVIDERS, OAuthApp
from backoffice.connectors.open_banking import (
    BankAccountInfo,
    BankConsent,
    BankLink,
    BookedTransaction,
    ConsentStatus,
)
from backoffice.connectors.vault import LocalKeyProvider, TokenVault
from backoffice.domain.models import Transaction, TransactionKind
from backoffice.server.config import ServerConfig
from backoffice.server.events import Event, state_digest
from backoffice.server.notify import ExpoPushClient, PushNotifier
from backoffice.server.runtime import TenantManager, bank_row
from backoffice.server.store import MemoryStore
from backoffice.server.sync import (
    BANK_MIN_INTERVAL,
    STALE_AFTER,
    SyncWorker,
    transaction_row,
)
from backoffice.server.worker import build_worker, main

GOOGLE = OAuthApp("google", "client-id", "client-secret", **PROVIDERS["google"])
MAILBOX = "mail-ana-padaria-pt"


def _mail(subject: str, body: str) -> bytes:
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = "faturas@edp.pt", "ana@padaria.pt", subject
    m["Message-ID"] = f"<{abs(hash(subject))}@edp.pt>"
    m["Date"] = "Sat, 12 Sep 2026 10:00:00 +0100"
    m.set_content(body)
    return m.as_bytes()


class FakeGoogle:
    """Google's token endpoint and a small Gmail API."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.messages = {f"m{i}": _mail(f"Fatura {i}", f"Fatura FT 2026/{i} no valor de 1{i},00 EUR.") for i in range(3)}
        self.history: list[str] = []  # ids added since the full sync
        self.token_error: tuple[int, dict[str, Any], dict[str, str]] | None = None
        self.api_error: tuple[int, dict[str, str]] | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "oauth2.googleapis.com":
            form = dict(x.split("=", 1) for x in request.content.decode().split("&"))
            assert form["client_id"] == "client-id" and form["grant_type"] == "refresh_token"
            if self.token_error:
                status, body, headers = self.token_error
                return httpx.Response(status, json=body, headers=headers)
            return httpx.Response(200, json={"access_token": f"at-{len(self.requests)}", "expires_in": 3600,
                                             "refresh_token": "rt-rotated"})
        assert request.headers["authorization"].startswith("Bearer at-")
        if self.api_error:
            status, headers = self.api_error
            return httpx.Response(status, json={"error": {"code": status}}, headers=headers)
        path = request.url.path.replace("/gmail/v1/users/me", "")
        if path == "/profile":
            return httpx.Response(200, json={"emailAddress": "ana@padaria.pt", "historyId": "100"})
        if path == "/messages":
            return httpx.Response(200, json={"messages": [{"id": i} for i in sorted(self.messages)]})
        if path == "/history":
            added = [{"message": {"id": i, "labelIds": ["INBOX"]}} for i in self.history]
            return httpx.Response(200, json={"history": [{"id": "101", "messagesAdded": added}], "historyId": "120"})
        if path.startswith("/messages/"):
            mid = path.rsplit("/", 1)[1]
            raw = base64.urlsafe_b64encode(self.messages[mid]).decode().rstrip("=")
            return httpx.Response(200, json={"id": mid, "threadId": f"t-{mid}", "labelIds": ["INBOX"],
                                             "internalDate": "1757667600000", "raw": raw})
        return httpx.Response(404)

    def calls(self, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path.endswith(path)]


def _setup(tmp_path: Path, **services: Any) -> tuple[Any, TokenVault, FakeExpo]:
    vault = TokenVault(LocalKeyProvider(os.urandom(32)))
    expo = FakeExpo()
    store = services.pop("store", None) or MemoryStore()
    notifier = PushNotifier(store, ExpoPushClient(transport=httpx.MockTransport(expo)))
    h = harness(tmp_path, store=store, vault=vault, notifier=notifier, **services)
    return h, vault, expo


def _gmail_owner(h: Any, vault: TokenVault) -> tuple[str, dict[str, str]]:
    account = signup(h.client)
    tenant, H = account["tenant"]["id"], bearer(account["token"])
    h.client.post("/api/devices", json={"expoPushToken": TOKEN_A, "platform": "ios"}, headers=H)
    vault.store(tenant, "mail-google-pending", "google", {"refresh_token": "rt-1", "scope": "gmail.readonly"})
    status, body = h.manager.finish_sign_in(tenant, "mail-google-pending", "google", "ana@padaria.pt")
    assert status == 200 and body["id"] == MAILBOX
    return tenant, H


def _worker(h: Any, vault: TokenVault, google: FakeGoogle | None = None, **kwargs: Any) -> SyncWorker:
    client = httpx.Client(transport=httpx.MockTransport(google or FakeGoogle()))
    return SyncWorker(h.manager, vault=vault, oauth_apps={"google": GOOGLE}, http_client=client, **kwargs)


def _events(h: Any, tenant: str, kind: str) -> list[Event]:
    return [e for e in (Event.parse(r) for r in h.store.events(tenant)) if e.kind == kind]


def _source(h: Any, H: dict[str, str], group: str) -> dict[str, Any]:
    groups = h.client.get("/api/sources", headers=H).json()["groups"]
    return next(g for g in groups if g["id"] == group)["items"][0]


def _same_after_replay(h: Any, tenant: str) -> None:
    h.clock.step = h.clock.step * 0
    with h.manager.open(tenant) as rt:
        live = state_digest(rt.service)
    fresh = TenantManager(h.store, h.objects, now=h.clock, strict_reads=True)
    with fresh.open(tenant) as rt:
        assert state_digest(rt.service) == live
    h.clock.step = timedelta(seconds=2)


# --------------------------------------------------------------------------- Gmail


def test_first_gmail_sync_reads_90_days_through_the_vault_and_is_replayable(tmp_path: Path) -> None:
    h, vault, expo = _setup(tmp_path)
    tenant, H = _gmail_owner(h, vault)
    before = _source(h, H, "email")
    assert before["status"] == "healthy"
    with h.manager.open(tenant) as rt:  # connected, but nothing claimed until the first sync has read it
        assert rt.service.repo.connectors[MAILBOX].covered_from is None
    google = FakeGoogle()
    worker = _worker(h, vault, google, mail_batch=2)
    report = worker.run_once()
    assert report.synced == [f"{tenant}/{MAILBOX}"] and report.messages == 3

    # The OAuth refresh went through the vault, which kept the rotated refresh token.
    assert len(google.calls("/token")) == 1
    assert vault.open(tenant, MAILBOX)["refresh_token"] == "rt-rotated"
    query = google.calls("/messages")[0].url.params["q"]
    after = int(query.split("after:", 1)[1].split()[0])
    assert abs(after - (h.clock.now_ - timedelta(days=90)).timestamp()) < 120  # §6: the last 90 days
    # Two events (a batch of 2, then the last message with the sync's state), messages in the object store.
    synced = _events(h, tenant, "sync.mail")
    assert [len(e.data["messages"]) for e in synced] == [2, 1]
    assert synced[0].data["state"] is None and synced[1].data["state"]["cursor"] == "100"
    assert all("Fatura" not in json.dumps(e.data) for e in synced)  # bytes live in the object store only
    with h.manager.open(tenant) as rt:
        c = rt.service.repo.connectors[MAILBOX]
        assert c.healthy and c.covered_from is not None
        assert (c.covered_until - c.covered_from) >= timedelta(days=89, hours=23)
    activity = h.client.get("/api/activity", headers=H).json()
    assert "Read ana@padaria.pt: the last 90 days are in." in json.dumps(activity)
    assert expo.sent == []  # quiet success
    _same_after_replay(h, tenant)

    # Not due yet: nothing is asked. After the interval, the next sync continues from the cursor.
    assert worker.run_once().synced == [] and len(google.calls("/history")) == 0
    h.clock.advance(minutes=16)
    google.history = ["m9"]
    google.messages["m9"] = _mail("Fatura 9", "Fatura FT 2026/9 no valor de 19,00 EUR.")
    report = worker.run_once()
    assert report.synced == [f"{tenant}/{MAILBOX}"] and report.messages == 1
    assert _events(h, tenant, "sync.mail")[-1].data["state"]["cursor"] == "120"
    assert len(google.calls("/token")) == 1  # the access token is reused until it expires
    _same_after_replay(h, tenant)


def test_outages_are_retried_with_backoff_and_record_nothing(tmp_path: Path) -> None:
    h, vault, expo = _setup(tmp_path)
    tenant, H = _gmail_owner(h, vault)
    google = FakeGoogle()
    google.api_error = (503, {})
    worker = _worker(h, vault, google)
    count = len(h.store.events(tenant))
    report = worker.run_once()
    assert report.retrying == [f"{tenant}/{MAILBOX}"] and report.synced == []
    assert len(h.store.events(tenant)) == count  # a transient failure is not an event
    asked = len(google.requests)
    worker.run_once()
    assert len(google.requests) == asked  # waiting: not asked again within the backoff
    h.clock.advance(minutes=2)
    google.api_error = (429, {"Retry-After": "900"})
    worker.run_once()
    h.clock.advance(minutes=5)
    asked = len(google.requests)
    worker.run_once()
    assert len(google.requests) == asked  # Retry-After (15 minutes) beats the 2-minute backoff
    h.clock.advance(minutes=11)
    google.api_error = None
    assert worker.run_once().synced == [f"{tenant}/{MAILBOX}"]
    assert expo.sent == [] and _source(h, H, "email")["status"] == "healthy"


def test_a_mailbox_that_stops_syncing_for_a_day_is_shown_to_the_owner(tmp_path: Path) -> None:
    h, vault, expo = _setup(tmp_path)
    tenant, H = _gmail_owner(h, vault)
    google = FakeGoogle()
    worker = _worker(h, vault, google)
    worker.run_once()
    google.api_error = (500, {})
    for _ in range(3):
        h.clock.advance(hours=9)
        worker.run_once()
    assert STALE_AFTER <= timedelta(hours=27)
    assert _source(h, H, "email")["status"] != "healthy"
    assert [m["title"] for m in expo.sent] == ["Connection needs you"]
    google.api_error = None
    h.clock.advance(hours=2)
    assert worker.run_once().synced == [f"{tenant}/{MAILBOX}"]
    assert _source(h, H, "email")["status"] == "healthy"  # back by itself once it syncs again
    _same_after_replay(h, tenant)


def test_a_refused_sign_in_marks_the_mailbox_for_the_owner_once(tmp_path: Path) -> None:
    h, vault, expo = _setup(tmp_path)
    tenant, H = _gmail_owner(h, vault)
    google = FakeGoogle()
    google.token_error = (400, {"error": "invalid_grant"}, {})
    worker = _worker(h, vault, google)
    report = worker.run_once()
    assert report.needs_owner == [f"{tenant}/{MAILBOX}"]
    failed = _events(h, tenant, "sync.failed")
    assert len(failed) == 1 and failed[0].data["reconnect"] is True
    assert failed[0].data["state"]["reconnect_required"] is True
    assert "rt-1" not in json.dumps(failed[0].data)  # no secret ever reaches an event
    source = _source(h, H, "email")
    assert source["status"] != "healthy"
    assert [(m["title"], m["body"]) for m in expo.sent] == [("Connection needs you", "Gmail needs reconnecting.")]
    h.clock.advance(hours=1)
    asked = len(google.requests)
    assert worker.run_once().skipped == [f"{tenant}/{MAILBOX}"] and len(google.requests) == asked  # waits for them
    # The owner signs in again (here: taps Reconnect); the next pass tries again.
    google.token_error = None
    assert h.client.post(f"/api/connections/{MAILBOX}/reconnect", headers=H).status_code == 200
    assert worker.run_once().synced == [f"{tenant}/{MAILBOX}"]
    assert len(expo.sent) == 1
    _same_after_replay(h, tenant)


# --------------------------------------------------------------------------- IMAP


def test_imap_mailbox_syncs_with_its_app_password(tmp_path: Path) -> None:
    h, vault, _ = _setup(tmp_path)
    account = signup(h.client)
    tenant, H = account["tenant"]["id"], bearer(account["token"])
    res = h.client.post("/api/sources", json={"kind": "email", "provider": "imap", "address": "ana@padaria.pt",
                                              "host": "imap.padaria.pt", "password": "app-password-123"}, headers=H)
    assert res.status_code == 200
    fake = FakeIMAP()
    seen: list[Any] = []
    worker = SyncWorker(h.manager, vault=vault, imap_factory=lambda cfg: seen.append(cfg) or fake)
    report = worker.run_once()
    assert report.synced == [f"{tenant}/{MAILBOX}"] and report.messages == 3
    assert seen[0].host == "imap.padaria.pt" and ("LOGIN", "ana@padaria.pt") in fake.commands
    search = next(c for c in fake.commands if c[:2] == ("UID", "SEARCH"))
    since = (h.clock.now_ - timedelta(days=90)).astimezone(timezone.utc).date()
    assert search[3] in (f"{since.day}-{since.strftime('%b')}-{since.year}",
                         f"{(since - timedelta(days=1)).day}-{(since - timedelta(days=1)).strftime('%b')}-{since.year}")
    text = "\n".join(r.body for r in h.store.events(tenant))
    assert "app-password-123" not in text
    _same_after_replay(h, tenant)


# --------------------------------------------------------------------------- GoCardless


class FakeBank:
    def __init__(self) -> None:
        self.windows: list[tuple[str, date, date]] = []
        self.status = ConsentStatus.ACTIVE
        self.expires = datetime(2027, 3, 29, tzinfo=timezone.utc)

    def create_link(self, **kwargs: object) -> BankLink:
        self.reference = kwargs["reference"]
        return BankLink("req-1", "https://ob.gocardless.com/psd2/start/req-1", "agr-1")

    def consent(self, requisition_id: str) -> BankConsent:
        return BankConsent("req-1", self.status, ("acc-1",), "MILLENNIUMBCP_BCOMPTPL", self.expires, 90)

    def account(self, account_id: str) -> BankAccountInfo:
        return BankAccountInfo(account_id, iban="PT50000201231234567890154", currency="EUR")

    def booked_transactions(self, account_id: str, date_from: date, date_to: date) -> list[BookedTransaction]:
        self.windows.append((account_id, date_from, date_to))
        return [BookedTransaction("bk-1", date(2026, 9, 19), Decimal("-64.10"), "EUR", creditor_name="EDP COMERCIAL",
                                  remittance="DD EDP COMERCIAL SEPA", bank_code="PMNT-RDDT-ESDD"),
                BookedTransaction("bk-2", date(2026, 9, 22), Decimal("-59.99"), "EUR",
                                  creditor_name="ADOBE *CREATIVE CLOUD", remittance="COMPRA CARTAO ****2291")]


def _linked_bank(h: Any) -> tuple[str, dict[str, str], FakeBank]:
    bank = h.app.state.bank
    account = signup(h.client)
    tenant, H = account["tenant"]["id"], bearer(account["token"])
    h.client.post("/api/devices", json={"expoPushToken": TOKEN_A, "platform": "ios"}, headers=H)
    h.client.post("/api/connections/bank/start", json={"institutionId": "MILLENNIUMBCP_BCOMPTPL"}, headers=H)
    back = h.client.get("/api/connections/bank/callback", params={"ref": bank.reference}, follow_redirects=False)
    assert back.headers["location"].endswith("?bank=done")
    return tenant, H, bank


def test_bank_sync_imports_90_days_then_at_most_every_six_hours(tmp_path: Path) -> None:
    bank = FakeBank()
    h, vault, expo = _setup(tmp_path, aggregator=lambda: bank)
    h.app.state.bank = bank
    tenant, H, _ = _linked_bank(h)
    assert vault.open(tenant, "bank-millenniumbcp")["accounts"] == {"acc-1": "PT50000201231234567890154"}
    with h.manager.open(tenant) as rt:
        svc = rt.service
        account_id = next(a.id for a in svc.repo.accounts.values() if a.iban)
        assert svc.sign_in["bank-millenniumbcp"]["consent_until"] == date(2027, 3, 29)  # the bank's own date
        assert svc.repo.connectors["bank-millenniumbcp"].covered_from is None
    worker = SyncWorker(h.manager, vault=vault, aggregator_factory=lambda: bank)
    report = worker.run_once()
    assert report.synced == [f"{tenant}/bank-millenniumbcp"] and report.rows == 2
    today = h.clock.now_.astimezone(timezone.utc).date()
    assert bank.windows == [("acc-1", today - timedelta(days=90), today)]
    rows = _events(h, tenant, "sync.bank")[-1].data["rows"]
    assert {r["account_id"] for r in rows} == {account_id}
    with h.manager.open(tenant) as rt:
        txs = {t.tx.counterparty: t.tx for t in rt.service.repo.transactions.values()}
    assert txs["EDP COMERCIAL"].kind is TransactionKind.DIRECT_DEBIT and txs["EDP COMERCIAL"].amount == Decimal("-64.10")
    assert txs["ADOBE *CREATIVE CLOUD"].card_last4 == "2291"
    assert "Imported the last 90 days from Millenniumbcp." in json.dumps(h.client.get("/api/activity", headers=H).json())

    h.clock.advance(hours=2)
    worker.run_once()
    assert len(bank.windows) == 1  # GoCardless allows four reads a day per account
    h.clock.advance(seconds=BANK_MIN_INTERVAL.total_seconds())
    worker.run_once()
    assert len(bank.windows) == 2 and bank.windows[1][1] == today - timedelta(days=5)  # from the cursor, overlapping
    with h.manager.open(tenant) as rt:
        assert len(rt.service.repo.transactions) == 2  # the overlap never duplicates
    _same_after_replay(h, tenant)

    bank.status = ConsentStatus.EXPIRED  # the owner's 180 days are over: only they can renew it
    h.clock.advance(hours=7)
    assert worker.run_once().needs_owner == [f"{tenant}/bank-millenniumbcp"]
    assert [m["title"] for m in expo.sent] == ["Connection needs you"]
    _same_after_replay(h, tenant)


def test_a_bank_added_by_hand_is_not_synced(tmp_path: Path) -> None:
    h, vault, _ = _setup(tmp_path)
    account = signup(h.client)
    tenant, H = account["tenant"]["id"], bearer(account["token"])
    res = h.client.post("/api/sources", json={"kind": "bank", "bank": "Millennium BCP", "companyId": "padaria-lda",
                                              "iban": "PT50000201231234567890154"}, headers=H)
    assert res.status_code == 200 and "Link it to your bank" in res.json()["message"]
    report = SyncWorker(h.manager, vault=vault, aggregator_factory=FakeBank).run_once()
    assert report.synced == [] and _events(h, tenant, "sync.bank") == []


def test_transaction_rows_round_trip_into_bank_rows() -> None:
    tx = Transaction(id="tx_1", tenant_id="t", account_id="acct_1", booked_on=date(2026, 9, 19),
                     amount=Decimal("-64.10"), currency="EUR", counterparty="EDP", description="DD EDP",
                     kind=TransactionKind.DIRECT_DEBIT, card_last4=None, counterparty_iban="PT50000201231234567890154",
                     reference="E2E-1")
    row = bank_row(json.loads(json.dumps(transaction_row(tx))))
    assert (row.bank_id, row.account_id, row.booked_on, row.amount, row.kind, row.reference, row.counterparty_iban) == (
        "tx_1", "acct_1", date(2026, 9, 19), Decimal("-64.10"), TransactionKind.DIRECT_DEBIT, "E2E-1",
        "PT50000201231234567890154")


# --------------------------------------------------------------------------- the day, and the command


def test_the_day_turns_without_anyone_signing_in(tmp_path: Path) -> None:
    h, vault, _ = _setup(tmp_path)
    account = signup(h.client)
    tenant = account["tenant"]["id"]
    worker = SyncWorker(h.manager, vault=vault)
    assert worker.run_once().ticks == 0
    h.clock.advance(days=1)
    assert worker.run_once().ticks == 1 and len(_events(h, tenant, "tick")) == 1
    assert worker.run_once().ticks == 0  # once a day


def test_one_broken_tenant_does_not_stop_the_others(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    h, vault, _ = _setup(tmp_path)
    first, second = signup(h.client)["tenant"]["id"], signup(h.client, "rui@oficina.pt", tax_id=None)["tenant"]["id"]
    h.clock.advance(days=1)
    worker = SyncWorker(h.manager, vault=vault)
    real = h.manager.tick_if_due

    def tick(rt: Any) -> None:
        if rt.tenant_id == min(first, second):
            raise RuntimeError("boom")
        real(rt)

    monkeypatch.setattr(h.manager, "tick_if_due", tick)
    report = worker.run_once()
    assert report.errors == 1 and report.ticks == 1 and report.tenants == 2


def test_the_worker_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("BACKOFFICE_MODE", "demo")
    assert main(["--once"]) == 0 and "production only" in capsys.readouterr().err  # demo: nothing to sync
    monkeypatch.setenv("BACKOFFICE_MODE", "production")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert main(["--once"]) == 2 and "DATABASE_URL" in capsys.readouterr().err
    config = ServerConfig(sync_interval_s=600, history_days=365)
    store = MemoryStore()
    worker = build_worker(config, {"store": store, "objects": object(), "oauth_apps": {}})
    assert worker.interval == timedelta(minutes=10) and worker.history == timedelta(days=365)
    assert worker.run_once().tenants == 0
