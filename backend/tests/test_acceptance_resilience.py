"""Resilience on the production server (QA R5, R6, S2, S5, S7, S8, C6).

* Push notifications: Gmail through Pub/Sub (Google's signed token checked), Microsoft Graph (validation
  handshake, clientState, lifecycle notifications); duplicates harmless; subscriptions created and renewed by
  the sync worker; webhook loss detected (a poll finds mail no push announced) and polled more often.
* Backfill: known gaps re-read on the next pass, a chunk at a time and resumable, read before recording; the
  month says "Catching up on 3 days of email" and never turns green while a gap is open.
* The durable job queue (PostgreSQL ``jobs``, MemoryStore in the unit tests): backoff, dead letters on the
  team's dashboard, idempotent retries; the same contract on a real PostgreSQL with row-level security.
* Bank rows for an account nobody added are kept and the owner is asked; bank rows deduplicate on the bank's id.
* Supplier websites: a sign-in code is asked for (push + Needs you), entered, and the retrieval resumes.

Fake transports stand in for Google, Microsoft, GoCardless, Expo and a supplier's website.
"""

from __future__ import annotations

import base64
import json
import os
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import httpx
import pytest
from _server_support import bearer, harness, signup
from test_server_push import TOKEN_A, FakeExpo
from test_server_replay_safety import FakeReader, _outcome

from backoffice.connectors.authorize import PROVIDERS, OAuthApp
from backoffice.connectors.base import ConnectorState, WebhookState
from backoffice.connectors.open_banking import (
    BankAccountInfo,
    BankConsent,
    BankLink,
    BookedTransaction,
    ConsentStatus,
)
from backoffice.connectors.portals import (
    AuthResult,
    AuthStatus,
    PortalDocument,
    PortalInvoiceRef,
    PortalSession,
    SupplierPortalConnector,
    code_prompt,
)
from backoffice.connectors.vault import LocalKeyProvider, TokenVault
from backoffice.language import find_jargon, find_off_tone
from backoffice.server.events import Event, state_digest
from backoffice.server.jobs import MAX_ATTEMPTS, SYNC_CONNECTION, backoff
from backoffice.server.notify import ExpoPushClient, PushNotifier
from backoffice.server.portals import PortalWorker
from backoffice.server.runtime import TenantManager
from backoffice.server.store import MemoryStore, WebhookRoute
from backoffice.server.sync import LOST_INTERVAL, PUSH_POLL_INTERVAL, PushSettings, SyncWorker
from backoffice.server.webhooks import GooglePushVerifier, WebhookRefused, client_state_hash

GOOGLE = OAuthApp("google", "client-id", "client-secret", **PROVIDERS["google"])
MICROSOFT = OAuthApp("microsoft", "ms-client", "ms-secret", **PROVIDERS["microsoft"])
MAILBOX = "mail-ana-padaria-pt"
TOPIC = "projects/admin2ai/topics/gmail-push"
AUDIENCE = "https://api.backoffice.test/api/webhooks/gmail"
PUSH_ACCOUNT = "gmail-push@admin2ai.iam.gserviceaccount.com"
GRAPH_URL = "https://api.backoffice.test/api/webhooks/microsoft"


def _plain(*texts: str) -> None:
    for text in texts:
        assert find_jargon(text) == [] and find_off_tone(text) == [], text


def _events(h: Any, tenant: str, kind: str | None = None) -> list[Event]:
    return [e for e in (Event.parse(r) for r in h.store.events(tenant)) if kind is None or e.kind == kind]


def _same_after_replay(h: Any, tenant: str) -> None:
    """Another process rebuilds exactly the same business from the log alone (no vault, reader or network)."""
    step = h.clock.step
    h.clock.step = step * 0
    with h.manager.open(tenant) as rt:
        live = state_digest(rt.service)
    fresh = TenantManager(h.store, h.objects, now=h.clock, strict_reads=True)
    with fresh.open(tenant) as rt:
        assert state_digest(rt.service) == live
    h.clock.step = step


def _setup(tmp_path: Path, **services: Any) -> tuple[Any, TokenVault, FakeExpo]:
    vault = TokenVault(LocalKeyProvider(os.urandom(32)))
    expo = FakeExpo()
    store = services.pop("store", None) or MemoryStore()
    notifier = PushNotifier(store, ExpoPushClient(transport=httpx.MockTransport(expo)))
    h = harness(tmp_path, store=store, vault=vault, notifier=notifier, **services)
    return h, vault, expo


def _owner(h: Any, vault: TokenVault, provider: str = "google") -> tuple[str, dict[str, str]]:
    account = signup(h.client)
    tenant, H = account["tenant"]["id"], bearer(account["token"])
    h.client.post("/api/devices", json={"expoPushToken": TOKEN_A, "platform": "ios"}, headers=H)
    vault.store(tenant, f"mail-{provider}-pending", provider, {"refresh_token": "rt-1"})
    status, body = h.manager.finish_sign_in(tenant, f"mail-{provider}-pending", provider, "ana@padaria.pt")
    assert status == 200 and body["id"] == MAILBOX
    return tenant, H


def _mail(subject: str, body: str = "Olá Ana, obrigado pela reunião de ontem.") -> bytes:
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = "joana@cliente.pt", "ana@padaria.pt", subject
    m["Message-ID"] = f"<{uuid.uuid4().hex}@cliente.pt>"
    m["Date"] = "Fri, 02 Oct 2026 10:00:00 +0100"
    m.set_content(body)
    return m.as_bytes()


def _ms(when: datetime) -> str:
    return str(int(when.timestamp() * 1000))


class FakeGmail:
    """Google's token endpoint and the Gmail API: history ids, searches by time, watch."""

    def __init__(self, clock: Any) -> None:
        self.clock = clock
        self.requests: list[httpx.Request] = []
        self.messages: dict[str, tuple[bytes, datetime]] = {}
        self.added: list[tuple[int, str]] = []  # (history id, message id)
        self.history_id = 100
        self.history_expired = False
        self.api_error: int | None = None
        self.fail_after: int | None = None  # message fetches that succeed before a 503

    def add(self, mid: str, received: datetime, subject: str | None = None) -> None:
        self.messages[mid] = (_mail(subject or f"Mensagem {mid}"), received)
        self.history_id += 1
        self.added.append((self.history_id, mid))

    def calls(self, path: str, method: str | None = None) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path.endswith(path) and (method is None or r.method == method)]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": f"at-{len(self.requests)}", "expires_in": 3600})
        if self.api_error:
            return httpx.Response(self.api_error, json={"error": {"code": self.api_error}})
        path = request.url.path.replace("/gmail/v1/users/me", "")
        if path == "/profile":
            return httpx.Response(200, json={"emailAddress": "ana@padaria.pt", "historyId": str(self.history_id)})
        if path == "/watch":
            expires = self.clock.now_ + timedelta(days=7)
            return httpx.Response(200, json={"historyId": str(self.history_id), "expiration": _ms(expires)})
        if path == "/history":
            if self.history_expired:
                return httpx.Response(404, json={"error": {"code": 404}})
            start = int(request.url.params["startHistoryId"])
            added = [{"message": {"id": m, "labelIds": ["INBOX"]}} for h, m in self.added if h > start]
            return httpx.Response(200, json={"history": [{"id": str(self.history_id), "messagesAdded": added}],
                                             "historyId": str(self.history_id)})
        if path == "/messages":
            query = request.url.params["q"]
            after = int(query.split("after:", 1)[1].split()[0])
            before = int(query.split("before:", 1)[1].split()[0]) if "before:" in query else None
            ids = [m for m, (_, t) in sorted(self.messages.items(), key=lambda kv: kv[1][1])
                   if t.timestamp() >= after and (before is None or t.timestamp() < before)]
            return httpx.Response(200, json={"messages": [{"id": i} for i in ids]})
        if path.startswith("/messages/"):
            if self.fail_after is not None:
                if self.fail_after <= 0:
                    return httpx.Response(503, json={"error": {"code": 503}})
                self.fail_after -= 1
            mid = path.rsplit("/", 1)[1]
            raw, received = self.messages[mid]
            return httpx.Response(200, json={"id": mid, "threadId": f"t-{mid}", "labelIds": ["INBOX"],
                                             "internalDate": _ms(received),
                                             "raw": base64.urlsafe_b64encode(raw).decode().rstrip("=")})
        return httpx.Response(404)


def _gmail_worker(h: Any, vault: TokenVault, gmail: FakeGmail, **kwargs: Any) -> SyncWorker:
    client = httpx.Client(transport=httpx.MockTransport(gmail))
    kwargs.setdefault("push", PushSettings(gmail_topic=TOPIC))
    return SyncWorker(h.manager, vault=vault, oauth_apps={"google": GOOGLE}, http_client=client, **kwargs)


def _evidence(h: Any, tenant: str) -> list[Any]:
    with h.manager.open(tenant) as rt:
        return [e for (t, _), e in rt.service.repo.registry.index._by_id.items() if t == tenant]


def _state(h: Any, tenant: str, cid: str = MAILBOX) -> ConnectorState:
    return h.manager.read(tenant, lambda svc: ConnectorState.model_validate(svc.sync_states[cid]))


# --------------------------------------------------------------------------- Google's signed push token


class GoogleKeys:
    """An RSA key standing in for Google's: signs tokens the way Pub/Sub does, publishes the JWK."""

    def __init__(self, kid: str = "google-key-1") -> None:
        from cryptography.hazmat.primitives.asymmetric import rsa

        self.kid = kid
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.fetches = 0

    def jwk(self) -> dict[str, str]:
        numbers = self.key.public_key().public_numbers()

        def b64(n: int) -> str:
            return base64.urlsafe_b64encode(n.to_bytes((n.bit_length() + 7) // 8, "big")).decode().rstrip("=")

        return {"kid": self.kid, "kty": "RSA", "alg": "RS256", "n": b64(numbers.n), "e": b64(numbers.e)}

    def __call__(self, *, refresh: bool = False) -> dict[str, dict[str, str]]:
        self.fetches += 1
        return {self.kid: self.jwk()}

    def token(self, now: datetime, **claims: Any) -> str:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        body = {"iss": "https://accounts.google.com", "aud": AUDIENCE, "iat": int(now.timestamp()),
                "exp": int(now.timestamp()) + 3600, "email": PUSH_ACCOUNT, "email_verified": True, **claims}
        header = {"alg": "RS256", "kid": self.kid, "typ": "JWT"}

        def enc(data: dict[str, Any]) -> str:
            return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")

        signing = f"{enc(header)}.{enc(body)}"
        signature = self.key.sign(signing.encode(), padding.PKCS1v15(), hashes.SHA256())
        return f"{signing}.{base64.urlsafe_b64encode(signature).decode().rstrip('=')}"


def _push_body(history_id: int, message_id: str, address: str = "ana@padaria.pt") -> bytes:
    data = base64.b64encode(json.dumps({"emailAddress": address, "historyId": str(history_id)}).encode()).decode()
    return json.dumps({"message": {"data": data, "messageId": message_id, "publishTime": "2026-10-02T09:31:00Z"},
                       "subscription": "projects/admin2ai/subscriptions/gmail-push"}).encode()


def _gmail_push_setup(tmp_path: Path) -> tuple[Any, TokenVault, FakeExpo, GoogleKeys]:
    keys = GoogleKeys()
    verifier_clock = lambda: datetime.now(timezone.utc)  # noqa: E731 - Google's tokens carry real time
    verifier = GooglePushVerifier(AUDIENCE, service_account=PUSH_ACCOUNT, keys=keys, clock=verifier_clock)
    h, vault, expo = _setup(tmp_path, push_verifier=verifier)
    return h, vault, expo, keys


def _push(h: Any, keys: GoogleKeys, body: bytes, token: str | None = None) -> httpx.Response:
    token = token if token is not None else keys.token(datetime.now(timezone.utc))
    return h.client.post("/api/webhooks/gmail", content=body,
                         headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})


# =========================================================================== 1. push notifications


def test_gmail_push_is_verified_queued_once_and_reads_the_mailbox_at_once(tmp_path: Path) -> None:
    h, vault, expo, keys = _gmail_push_setup(tmp_path)
    tenant, H = _owner(h, vault)
    gmail = FakeGmail(h.clock)
    gmail.add("m1", h.clock.now_ - timedelta(days=3))
    worker = _gmail_worker(h, vault, gmail)
    report = worker.run_once()
    assert report.synced == [f"{tenant}/{MAILBOX}"] and report.subscribed == [f"{tenant}/{MAILBOX}"]
    assert json.loads(gmail.calls("/watch")[0].content) == {"topicName": TOPIC}
    state = _state(h, tenant)
    assert state.webhook_state is WebhookState.ACTIVE and state.webhook_since is not None
    assert state.webhook_expires_at - h.clock.now_ > timedelta(days=6)
    assert [e.data["state"]["webhook_state"] for e in _events(h, tenant, "sync.webhook")] == ["active"]
    assert [r.connection_id for r in h.store.webhook_routes("gmail", "ana@padaria.pt")] == [MAILBOX]

    # Mail arrives; Google pushes. The push is checked, queued once, and the mailbox is read at once.
    h.clock.advance(minutes=3)
    gmail.add("m2", h.clock.now_)
    assert _push(h, keys, _push_body(gmail.history_id, "pubsub-1")).status_code == 204
    assert _push(h, keys, _push_body(gmail.history_id, "pubsub-1")).status_code == 204  # Pub/Sub redelivers
    queued = [j for j in h.store.jobs(tenant) if j.kind == SYNC_CONNECTION]
    assert len(queued) == 1 and queued[0].payload["connectionId"] == MAILBOX and queued[0].state == "queued"
    assert "ana@padaria.pt" not in json.dumps(dict(queued[0].payload))  # ids only
    assert len(_events(h, tenant, "sync.mail")) == 1  # a notification is a hint: nothing recorded from it
    h.clock.advance(minutes=1)  # not due by the schedule (push works: hourly) ...
    report = worker.run_once()
    assert report.jobs_done == [queued[0].id] and report.messages == 1  # ... but the push reads it now
    synced = _events(h, tenant, "sync.mail")[-1]
    assert synced.data["state"]["last_event_at"] is not None
    assert worker.run_once().jobs_done == [] and h.store.jobs(tenant)[-1].state == "done"
    # A replay of the same notification a day later does nothing either.
    h.clock.advance(days=1)
    assert _push(h, keys, _push_body(gmail.history_id, "pubsub-1")).status_code == 204
    assert [j.state for j in h.store.jobs(tenant)] == ["done"]
    assert _push(h, keys, _push_body(gmail.history_id, "pubsub-9", "someone@else.pt")).status_code == 204
    assert len(h.store.jobs(tenant)) == 1  # a mailbox nobody here reads: acknowledged and ignored
    assert expo.sent == []  # quiet success
    _same_after_replay(h, tenant)


def test_gmail_pushes_not_signed_by_google_for_us_are_refused(tmp_path: Path) -> None:
    h, vault, _, keys = _gmail_push_setup(tmp_path)
    tenant, _ = _owner(h, vault)
    h.store.save_webhook_route(WebhookRoute("gmail", "ana@padaria.pt", tenant, MAILBOX, "", None, None,
                                            h.clock.now_))
    now = datetime.now(timezone.utc)
    stranger = GoogleKeys(kid="google-key-1")  # same key id, another key: the signature fails
    bad = {
        "no token": "",
        "wrong signer": stranger.token(now),
        "wrong audience": keys.token(now, aud="https://evil.example/hook"),
        "wrong issuer": keys.token(now, iss="https://evil.example"),
        "expired": keys.token(now - timedelta(hours=3)),
        "another sender": keys.token(now, email="someone@evil.example"),
        "unverified sender": keys.token(now, email_verified=False),
        "garbage": "a.b.c",
    }
    for reason, token in bad.items():
        res = _push(h, keys, _push_body(200, f"x-{reason}"), token=token)
        assert res.status_code == 401, reason
        assert res.json()["message"] == "This request is not signed by Google."
    assert h.store.jobs(tenant) == []
    verifier = GooglePushVerifier(AUDIENCE, keys=keys)
    with pytest.raises(WebhookRefused):
        verifier.verify("Bearer " + keys.token(now, aud="other"))
    assert verifier.verify("Bearer " + keys.token(now, email="anyone@example.com"))["aud"] == AUDIENCE
    # Without a configured topic there is no Gmail receiver at all; GoCardless (bank data) offers no push.
    plain, _, _ = _setup(tmp_path / "plain")
    assert plain.client.post("/api/webhooks/gmail", content=b"{}").status_code == 404
    assert plain.client.post("/api/webhooks/gocardless", content=b"{}").status_code == 401  # no public receiver


class FakeGraph:
    """Microsoft's token endpoint and Graph: one inbox with delta, MIME, subscriptions."""

    def __init__(self, clock: Any) -> None:
        self.clock = clock
        self.requests: list[httpx.Request] = []
        self.messages: list[tuple[str, datetime]] = []
        self.delivered = 0
        self.subscriptions: dict[str, dict[str, Any]] = {}
        self.client_states: list[str] = []

    def add(self, mid: str, received: datetime) -> None:
        self.messages.append((mid, received))

    def calls(self, path: str, method: str | None = None) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path.endswith(path) and (method is None or r.method == method)]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "login.microsoftonline.com":
            return httpx.Response(200, json={"access_token": "ms-at", "expires_in": 3600})
        path = request.url.path.replace("/v1.0", "")
        g = "https://graph.microsoft.com/v1.0"
        if path == "/me/mailFolders/inbox":
            return httpx.Response(200, json={"id": "INBOX", "childFolderCount": 0})
        if path == "/me/mailFolders/INBOX/messages/delta":
            new = self.messages[self.delivered:]
            self.delivered = len(self.messages)
            return httpx.Response(200, json={
                "value": [{"id": m, "receivedDateTime": t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
                          for m, t in new],
                "@odata.deltaLink": f"{g}/me/mailFolders/INBOX/messages/delta?$deltatoken=d{self.delivered}"})
        if path.startswith("/me/messages/") and path.endswith("/$value"):
            return httpx.Response(200, content=_mail(path.split("/")[3]))
        if path == "/subscriptions" and request.method == "POST":
            body = json.loads(request.content)
            sid = f"sub-{len(self.subscriptions) + 1}"
            self.subscriptions[sid] = body
            self.client_states.append(body["clientState"])
            return httpx.Response(201, json={"id": sid, "expirationDateTime": body["expirationDateTime"]})
        if path.startswith("/subscriptions/") and request.method == "PATCH":
            sid = path.rsplit("/", 1)[1]
            if sid not in self.subscriptions:
                return httpx.Response(404, json={"error": {"code": "ResourceNotFound"}})
            body = json.loads(request.content)
            return httpx.Response(200, json={"id": sid, "expirationDateTime": body["expirationDateTime"]})
        return httpx.Response(404)


def _graph_worker(h: Any, vault: TokenVault, graph: FakeGraph) -> SyncWorker:
    return SyncWorker(h.manager, vault=vault, oauth_apps={"microsoft": MICROSOFT},
                      http_client=httpx.Client(transport=httpx.MockTransport(graph)),
                      push=PushSettings(graph_url=GRAPH_URL, graph_lifecycle_url=f"{GRAPH_URL}/lifecycle"))


def _graph_post(h: Any, notes: list[dict[str, Any]], *, lifecycle: bool = False) -> httpx.Response:
    path = "/api/webhooks/microsoft/lifecycle" if lifecycle else "/api/webhooks/microsoft"
    return h.client.post(path, json={"value": notes})


def test_graph_validation_client_state_duplicates_and_lifecycle(tmp_path: Path) -> None:
    h, vault, _ = _setup(tmp_path, graph_push=True)
    tenant, _ = _owner(h, vault, "microsoft")
    graph = FakeGraph(h.clock)
    graph.add("a1", h.clock.now_ - timedelta(days=2))
    worker = _graph_worker(h, vault, graph)
    assert worker.run_once().subscribed == [f"{tenant}/{MAILBOX}"]
    created = graph.subscriptions["sub-1"]
    assert created["notificationUrl"] == GRAPH_URL and created["lifecycleNotificationUrl"] == f"{GRAPH_URL}/lifecycle"
    assert created["resource"] == "me/mailFolders('inbox')/messages" and created["changeType"] == "created"
    secret = graph.client_states[0]
    state = _state(h, tenant)
    assert state.subscription_id == "sub-1" and state.webhook_state is WebhookState.ACTIVE
    assert timedelta(hours=60) < state.webhook_expires_at - h.clock.now_ <= timedelta(minutes=4230)
    route = h.store.webhook_routes("microsoft", "sub-1")[0]
    assert route.secret_hash == client_state_hash(secret) and secret not in json.dumps(
        [e.data for e in _events(h, tenant)])  # the secret is never recorded, only its hash is kept

    # Graph validates the endpoint: the token comes back as plain text.
    res = h.client.post("/api/webhooks/microsoft?validationToken=Validation%3A%20Testing%20client%20application")
    assert res.status_code == 200 and res.text == "Validation: Testing client application"
    assert res.headers["content-type"].startswith("text/plain")
    res = h.client.post("/api/webhooks/microsoft/lifecycle?validationToken=abc")
    assert res.status_code == 200 and res.text == "abc"

    note = {"subscriptionId": "sub-1", "changeType": "created", "clientState": secret,
            "resource": "Users/u/Messages/a2", "resourceData": {"id": "a2"}}
    forged = {**note, "clientState": "guess"}
    assert _graph_post(h, [forged]).status_code == 202 and h.store.jobs(tenant) == []
    assert _graph_post(h, [{**note, "subscriptionId": "sub-unknown"}]).status_code == 202
    assert h.store.jobs(tenant) == []
    graph.add("a2", h.clock.now_)
    assert _graph_post(h, [note, note]).status_code == 202  # the same notification twice in one batch
    assert _graph_post(h, [note]).status_code == 202  # and again later
    assert len(h.store.jobs(tenant)) == 1
    report = worker.run_once()
    assert len(report.jobs_done) == 1 and report.messages == 1

    # Lifecycle: Graph asks for a renewal (PATCH), says the subscription is gone (a new one), or lost notifications.
    life = {"subscriptionId": "sub-1", "clientState": secret, "lifecycleEvent": "reauthorizationRequired"}
    assert _graph_post(h, [life], lifecycle=True).status_code == 202
    assert len(worker.run_once().jobs_done) == 1 and graph.calls("/subscriptions/sub-1", "PATCH")
    assert _state(h, tenant).subscription_id == "sub-1"
    assert _graph_post(h, [{**life, "lifecycleEvent": "subscriptionRemoved"}], lifecycle=True).status_code == 202
    worker.run_once()
    assert _state(h, tenant).subscription_id == "sub-2" and len(graph.client_states) == 2
    assert h.store.webhook_routes("microsoft", "sub-1") == []  # the old subscription no longer routes
    second = graph.client_states[1]
    missed = {"subscriptionId": "sub-2", "clientState": second, "lifecycleEvent": "missed"}
    assert _graph_post(h, [missed], lifecycle=True).status_code == 202
    graph.add("a3", h.clock.now_)
    report = worker.run_once()
    assert report.messages == 1  # missed notifications: read from the cursor at once
    after = _state(h, tenant)
    assert after.webhook_lost_at is not None  # and polled more often until a push arrives again
    assert worker._mail_interval(after, h.clock.now_) == LOST_INTERVAL
    _same_after_replay(h, tenant)


def test_subscriptions_are_renewed_before_they_end(tmp_path: Path) -> None:
    h, vault, _ = _setup(tmp_path)
    tenant, _ = _owner(h, vault)
    gmail = FakeGmail(h.clock)
    worker = _gmail_worker(h, vault, gmail)
    worker.run_once()
    assert len(gmail.calls("/watch")) == 1
    for _ in range(5):  # five days: polled hourly (a safety net), the 7-day watch is left alone
        h.clock.advance(days=1)
        worker.run_once()
    assert len(gmail.calls("/watch")) == 1
    h.clock.advance(days=1, hours=1)  # less than a day left: renewed (Google asks for a renewal every day)
    report = worker.run_once()
    assert report.subscribed == [f"{tenant}/{MAILBOX}"] and len(gmail.calls("/watch")) == 2
    assert _state(h, tenant).webhook_expires_at - h.clock.now_ > timedelta(days=6)

    hm, mvault, _ = _setup(tmp_path / "ms")
    mtenant, _ = _owner(hm, mvault, "microsoft")
    graph = FakeGraph(hm.clock)
    mworker = _graph_worker(hm, mvault, graph)
    mworker.run_once()
    hm.clock.advance(hours=48)
    mworker.run_once()
    assert not graph.calls("/subscriptions/sub-1", "PATCH")  # about three days: not yet
    hm.clock.advance(hours=12)
    assert mworker.run_once().subscribed == [f"{mtenant}/{MAILBOX}"]
    assert len(graph.calls("/subscriptions/sub-1", "PATCH")) == 1 and list(graph.subscriptions) == ["sub-1"]
    assert _state(hm, mtenant).webhook_expires_at - hm.clock.now_ > timedelta(hours=60)


def test_webhook_loss_is_detected_recorded_and_the_mailbox_polled_more_often(tmp_path: Path) -> None:
    h, vault, _, keys = _gmail_push_setup(tmp_path)
    tenant, _ = _owner(h, vault)
    gmail = FakeGmail(h.clock)
    worker = _gmail_worker(h, vault, gmail)
    worker.run_once()
    assert worker._mail_interval(_state(h, tenant), h.clock.now_) == PUSH_POLL_INTERVAL
    # Mail arrives, but no notification ever comes. The hourly safety-net poll finds it.
    h.clock.advance(minutes=20)
    gmail.add("late", h.clock.now_)
    h.clock.advance(minutes=45)
    watches = len(gmail.calls("/watch"))
    report = worker.run_once()
    assert report.messages == 1 and report.webhook_lost == [f"{tenant}/{MAILBOX}"]
    lost = _events(h, tenant, "sync.mail")[-1].data["state"]
    assert lost["webhook_state"] == "failed" and lost["webhook_lost_at"]  # recorded with the sync
    assert len(gmail.calls("/watch")) == watches + 1  # re-subscribed at once
    state = _state(h, tenant)
    assert state.webhook_state is WebhookState.ACTIVE and state.webhook_lost_at is not None
    assert worker._mail_interval(state, h.clock.now_) == LOST_INTERVAL  # polled every few minutes now
    h.clock.advance(minutes=6)
    histories = len(gmail.calls("/history"))
    worker.run_once()
    assert len(gmail.calls("/history")) == histories + 1
    # A notification arrives again: push is trusted again, polling goes back to hourly.
    h.clock.advance(minutes=1)
    gmail.add("pushed", h.clock.now_)
    assert _push(h, keys, _push_body(gmail.history_id, "pubsub-back")).status_code == 204
    worker.run_once()
    state = _state(h, tenant)
    assert state.webhook_lost_at is None and worker._mail_interval(state, h.clock.now_) == PUSH_POLL_INTERVAL
    # Mail a push announced in time is never mistaken for a loss.
    h.clock.advance(minutes=2)
    gmail.add("prompt", h.clock.now_)
    assert _push(h, keys, _push_body(gmail.history_id, "pubsub-prompt")).status_code == 204
    h.clock.advance(minutes=30)
    assert worker.run_once().webhook_lost == []
    _same_after_replay(h, tenant)


# =========================================================================== 2. backfill


def _month(h: Any, tenant: str, month: str) -> dict[str, Any]:
    """The month view as the owner's app reads it (months later: their session ended meanwhile)."""
    status, body = h.manager.view(tenant, "GET", f"/api/months/padaria-lda/{month}")
    assert status == 200, body
    return body


def _activity(h: Any, tenant: str) -> str:
    return json.dumps(h.manager.view(tenant, "GET", "/api/activity")[1])


def test_a_gap_after_a_long_outage_is_backfilled_next_pass_bounded_resumable_and_never_green(tmp_path: Path) -> None:
    reader = FakeReader(_outcome())
    h, vault, _ = _setup(tmp_path, reader=reader)
    tenant, H = _owner(h, vault)
    gmail = FakeGmail(h.clock)
    start = h.clock.now_
    worker = _gmail_worker(h, vault, gmail, push=None, backfill_chunk=timedelta(days=2))
    worker.run_once()  # the first 90 days
    # The mailbox is out for 93 days (it needed reconnecting); Google keeps history for about a week.
    gmail.add("in-gap-1", start + timedelta(days=1))
    gmail.add("in-gap-2", start + timedelta(days=2, hours=12))
    gmail.add("after", start + timedelta(days=10))
    gmail.history_expired = True
    h.clock.advance(days=93)
    report = worker.run_once()
    assert report.backfilled == [] and report.messages == 1  # the window: only what is inside it
    state = _state(h, tenant)
    (gap,) = state.known_gaps
    assert timedelta(days=3) <= gap.end - gap.start < timedelta(days=3, minutes=5)
    october = _month(h, tenant, start.strftime("%Y-%m"))
    assert october["status"] == "open"
    lines = [r["text"] for r in october["remaining"]]
    assert "Catching up on 3 days of email from ana@padaria.pt." in lines, lines
    _plain(*lines)

    # Next pass: the oldest 2 days (one chunk), read before they are recorded, marked as a backfill.
    gmail.history_expired = False
    h.clock.advance(minutes=5)
    reads = len(reader.calls)
    report = worker.run_once()
    assert report.backfilled == [f"{tenant}/{MAILBOX}"] and report.messages == 1
    event = _events(h, tenant, "sync.mail")[-1]
    assert event.data["backfill"]["start"] == gap.start.isoformat() and len(event.data["messages"]) == 1
    assert len(reader.calls) == reads  # plain emails: nothing to read; the event says what was read when it is
    query = gmail.calls("/messages")[-1].url.params["q"]
    assert f"after:{int(gap.start.timestamp())}" in query and "before:" in query
    remaining = _state(h, tenant).known_gaps
    assert len(remaining) == 1 and remaining[0].start == gap.start + timedelta(days=2) and remaining[0].end == gap.end
    lines = [r["text"] for r in _month(h, tenant, start.strftime("%Y-%m"))["remaining"]]
    assert "Catching up on 1 day of email from ana@padaria.pt." in lines
    # Last chunk: the gap is closed and the month can be finished again.
    h.clock.advance(minutes=5)
    assert worker.run_once().backfilled == [f"{tenant}/{MAILBOX}"]
    assert _state(h, tenant).known_gaps == ()
    october = _month(h, tenant, start.strftime("%Y-%m"))
    assert not any("Catching up" in r["text"] for r in october["remaining"]) and october["status"] == "closed"
    assert "Caught up on the email I had missed from ana@padaria.pt." in _activity(h, tenant)
    assert [len(e.data["messages"]) for e in _events(h, tenant, "sync.mail") if e.data.get("backfill")] == [1, 1]
    h.clock.advance(minutes=5)
    assert worker.run_once().backfilled == []  # nothing left
    _same_after_replay(h, tenant)


def test_a_backfill_that_fails_keeps_the_gap_and_resumes(tmp_path: Path) -> None:
    h, vault, _ = _setup(tmp_path)
    tenant, H = _owner(h, vault)
    gmail = FakeGmail(h.clock)
    start = h.clock.now_
    worker = _gmail_worker(h, vault, gmail, push=None)
    worker.run_once()
    for i in range(3):
        gmail.add(f"gap-{i}", start + timedelta(hours=10 + i))
    gmail.history_expired = True
    h.clock.advance(days=92)
    worker.run_once()
    (gap,) = _state(h, tenant).known_gaps
    gmail.history_expired = False
    gmail.fail_after = 1  # Google fails after one message of the backfill
    h.clock.advance(minutes=5)
    report = worker.run_once()
    assert report.backfilled == [] and _state(h, tenant).known_gaps == (gap,)  # nothing claimed
    assert "Catching up on 2 days of email" in json.dumps(_month(h, tenant, start.strftime("%Y-%m")))
    gmail.fail_after = None
    h.clock.advance(minutes=5)
    assert worker.run_once().backfilled == [f"{tenant}/{MAILBOX}"] and _state(h, tenant).known_gaps == ()
    emails = [e for e in _evidence(h, tenant) if e.format.value in ("email", "eml")]
    assert len(emails) == 3 and len({e.sha256 for e in emails}) == 3  # the message read twice is one evidence
    _same_after_replay(h, tenant)


# =========================================================================== 3. the durable job queue


def test_failing_jobs_back_off_then_become_dead_letters_on_the_dashboard(tmp_path: Path,
                                                                        caplog: pytest.LogCaptureFixture) -> None:
    h, vault, _, keys = _gmail_push_setup(tmp_path)
    admin = signup(h.client, "admin@backoffice.test", company="Admin2Ai Lda", tax_id=None, name="Team")
    tenant, _ = _owner(h, vault)
    gmail = FakeGmail(h.clock)
    worker = _gmail_worker(h, vault, gmail)
    worker.run_once()
    gmail.add("m1", h.clock.now_)
    assert _push(h, keys, _push_body(gmail.history_id, "pubsub-x")).status_code == 204
    gmail.api_error = 500  # Gmail is down for hours
    waits = []
    for attempt in range(1, MAX_ATTEMPTS + 1):
        report = worker.run_jobs()
        (job,) = h.store.jobs(tenant)
        assert job.attempts == attempt
        if attempt < MAX_ATTEMPTS:
            assert report.jobs_retried == [job.id] and job.state == "queued" and job.last_error
            waits.append(job.run_after - h.clock.now_)
            assert worker.run_jobs().jobs_done == []  # not before its time
            h.clock.advance(seconds=(job.run_after - h.clock.now_).total_seconds() + 1)
        else:
            assert report.jobs_dead == [job.id] and job.state == "dead"
    assert [round(w.total_seconds() / 60) for w in waits] == [1, 2, 4, 8]  # exponential backoff
    assert backoff(10) == timedelta(hours=1)
    assert any(r.getMessage() == "job_dead_lettered" for r in caplog.records)  # the alarm counts these lines
    overview = h.client.get("/api/internal/overview", headers=bearer(admin["token"])).json()
    (dead,) = overview["deadLetters"]
    assert dead["tenant"] == tenant and dead["attempts"] == MAX_ATTEMPTS and dead["connection"] == MAILBOX
    fix = next(f for f in overview["fixes"] if f["id"] == f"{tenant}:job:{dead['id'].split(':')[1]}")
    assert fix["severity"] == "red" and "dead letter" in fix["detail"] and fix == overview["fixes"][0]
    h.clock.advance(hours=2)
    assert worker.run_jobs().jobs_done == [] and h.store.jobs(tenant)[0].state == "dead"  # parked, never dropped
    # The mailbox itself is still read on schedule: the dead letter loses nothing.
    gmail.api_error = None
    assert worker.run_once().messages == 1


def test_a_retried_job_is_idempotent(tmp_path: Path) -> None:
    h, vault, _, keys = _gmail_push_setup(tmp_path)
    tenant, _ = _owner(h, vault)
    gmail = FakeGmail(h.clock)
    worker = _gmail_worker(h, vault, gmail, mail_batch=1)
    worker.run_once()
    for i in range(3):
        gmail.add(f"n{i}", h.clock.now_)
    assert _push(h, keys, _push_body(gmail.history_id, "pubsub-r")).status_code == 204
    gmail.fail_after = 2  # two messages recorded (batches of one), then Gmail fails: the cursor does not move
    assert worker.run_jobs().jobs_retried and len(_events(h, tenant, "sync.mail")) == 3
    gmail.fail_after = None
    h.clock.advance(minutes=2)
    assert worker.run_jobs().jobs_done
    emails = [e for e in _evidence(h, tenant) if e.format.value in ("email", "eml")]
    assert len(emails) == 3  # the two messages fetched twice are still one piece of evidence each
    assert _state(h, tenant).cursor == str(gmail.history_id)
    _same_after_replay(h, tenant)


@pytest.fixture(scope="module")
def pg_store() -> Any:
    """The production store on a real PostgreSQL (as test_server_postgres.py), every migration applied, its login
    a member of backoffice_app and (to work the queue, after SET ROLE only) backoffice_scheduler."""
    pytest.importorskip("psycopg")
    import shutil
    from urllib.parse import urlsplit

    import test_server_postgres as tsp
    from backoffice_db import ensure_login
    from backoffice_db.testing import PostgresUnavailable, TemporaryPostgres

    from backoffice.server.postgres import PostgresStore

    pg = None
    external = os.environ.get("BACKOFFICE_TEST_DATABASE_URL")
    if external:
        psql = shutil.which("psql")
        if not psql:
            pytest.skip("psql is not installed")
        server = tsp.Server(external, psql)
    else:
        try:
            pg = TemporaryPostgres()
            pg.start()
        except PostgresUnavailable as err:
            pytest.skip(f"no PostgreSQL available: {err}")
        server = tsp.Server(pg.url("postgres"), str(pg.bindir / "psql"))
    try:
        name = f"bo_jobs_{uuid.uuid4().hex[:10]}"
        server.executor(urlsplit(server.admin_url).path.lstrip("/") or "postgres").query(f"CREATE DATABASE {name}")
        db = server.executor(name)
        tsp.migrate_all(db)
        login = f"bo_api_{uuid.uuid4().hex[:8]}"
        ensure_login(db, login, tsp.APP_PASSWORD, ["backoffice_app", "backoffice_scheduler"])
        store = PostgresStore(server.url(name, login, tsp.APP_PASSWORD), pool_size=4)
        yield store
        store.close()
    finally:
        if pg is not None:
            pg.stop()


def test_the_job_queue_and_webhook_tables_on_postgresql(tmp_path: Path, pg_store: Any) -> None:
    h, vault, _ = _setup(tmp_path, store=pg_store)
    a = signup(h.client, f"a-{uuid.uuid4().hex[:6]}@example.pt")["tenant"]["id"]
    b = signup(h.client, f"b-{uuid.uuid4().hex[:6]}@example.pt", tax_id=None)["tenant"]["id"]
    now = datetime.now(timezone.utc)
    assert pg_store.enqueue_job(a, SYNC_CONNECTION, "mail-1", {"connectionId": "mail-1"}, run_after=now)
    assert not pg_store.enqueue_job(a, SYNC_CONNECTION, "mail-1", {"connectionId": "mail-1"}, run_after=now)
    assert pg_store.enqueue_job(b, SYNC_CONNECTION, "mail-1", {"connectionId": "mail-1"}, run_after=now)
    first = pg_store.claim_jobs(now, limit=1, lease=timedelta(minutes=10))
    second = pg_store.claim_jobs(now, limit=5, lease=timedelta(minutes=10))
    assert len(first) == 1 and len(second) == 1 and first[0].id != second[0].id  # SKIP LOCKED: never twice
    assert first[0].attempts == 1 and first[0].payload == {"connectionId": "mail-1"}
    assert pg_store.claim_jobs(now, limit=5, lease=timedelta(minutes=10)) == []
    # A queued twin arrives while the first is running: the retry merges into it.
    assert pg_store.enqueue_job(first[0].tenant_id, SYNC_CONNECTION, "mail-1", {"connectionId": "mail-1"},
                                run_after=now)
    pg_store.retry_job(first[0].id, now, run_after=now + timedelta(minutes=1), error="gmail_http_500")
    pg_store.retry_job(second[0].id, now, run_after=now + timedelta(minutes=1), error="gmail_http_500")
    later = pg_store.claim_jobs(now + timedelta(minutes=2), limit=10, lease=timedelta(minutes=10))
    assert len(later) == 2
    pg_store.bury_job(later[0].id, now, error="gmail_http_500")
    pg_store.finish_job(later[1].id, now)
    assert [j.id for j in pg_store.dead_jobs()][:1] == [later[0].id] and pg_store.dead_jobs()[0].state == "dead"
    assert pg_store.prune_jobs(now + timedelta(days=30)) >= 1  # finished jobs go; dead letters stay
    assert [j.id for j in pg_store.dead_jobs()][:1] == [later[0].id]
    # A worker that died mid-job: its lease runs out and the job is claimed again.
    assert pg_store.enqueue_job(a, SYNC_CONNECTION, "mail-2", {}, run_after=now)
    (stuck,) = pg_store.claim_jobs(now, limit=5, lease=timedelta(minutes=1))
    assert pg_store.claim_jobs(now + timedelta(seconds=30), limit=5, lease=timedelta(minutes=1)) == []
    (again,) = pg_store.claim_jobs(now + timedelta(minutes=2), limit=5, lease=timedelta(minutes=1))
    assert again.id == stuck.id and again.attempts == 2
    # Routes: found only through what a notification presents; receipts and jobs in one transaction.
    route = WebhookRoute("gmail", "ana@padaria.pt", a, "mail-1", "", now + timedelta(days=7), None, now)
    pg_store.save_webhook_route(route)
    pg_store.save_webhook_route(WebhookRoute("microsoft", "sub-9", b, "mail-1", "f" * 64, None, None, now))
    assert [r.tenant_id for r in pg_store.webhook_routes("gmail", "ana@padaria.pt")] == [a]
    assert pg_store.webhook_routes("gmail", "nobody@padaria.pt") == []
    assert pg_store.webhook_routes("microsoft", "sub-9")[0].secret_hash == "f" * 64
    assert pg_store.connection_webhook(b, "mail-1").key == "sub-9" and pg_store.connection_webhook(a, "x") is None
    payload = {"connectionId": "mail-1", "reason": "push"}
    assert pg_store.accept_notification(route, "pubsub-1", now, now + timedelta(days=7), kind=SYNC_CONNECTION,
                                        key="mail-1", payload=payload)
    assert not pg_store.accept_notification(route, "pubsub-1", now, now + timedelta(days=7), kind=SYNC_CONNECTION,
                                            key="mail-1", payload=payload)
    assert pg_store.connection_webhook(a, "mail-1").last_notified_at is not None
    # The receipt expires: a much later redelivery is new again.
    assert pg_store.accept_notification(route, "pubsub-1", now + timedelta(days=8), now + timedelta(days=15),
                                        kind=SYNC_CONNECTION, key="mail-1", payload=payload)


# =========================================================================== 4. bank rows nobody can lose


class FakeBank:
    def __init__(self) -> None:
        self.accounts = ["acc-1"]
        self.rows: dict[str, list[BookedTransaction]] = {
            "acc-1": [BookedTransaction("bk-1", date(2026, 9, 19), Decimal("-64.10"), "EUR",
                                        creditor_name="EDP COMERCIAL", remittance="DD EDP COMERCIAL SEPA",
                                        bank_code="PMNT-RDDT-ESDD")],
            "acc-2": [BookedTransaction("bk-21", date(2026, 9, 21), Decimal("-120.00"), "EUR",
                                        creditor_name="LANDLORD LDA", remittance="RENDA SETEMBRO"),
                      BookedTransaction("bk-22", date(2026, 9, 24), Decimal("-18.50"), "EUR",
                                        creditor_name="PAPELARIA CENTRAL", remittance="COMPRA CARTAO ****7781")],
        }
        self.looked_up: list[str] = []

    def create_link(self, **kwargs: object) -> BankLink:
        self.reference = kwargs["reference"]
        return BankLink("req-1", "https://ob.gocardless.com/psd2/start/req-1", "agr-1")

    def consent(self, requisition_id: str) -> BankConsent:
        return BankConsent("req-1", ConsentStatus.ACTIVE, tuple(self.accounts), "MILLENNIUMBCP_BCOMPTPL",
                           datetime(2027, 3, 29, tzinfo=timezone.utc), 90)

    def account(self, account_id: str) -> BankAccountInfo:
        self.looked_up.append(account_id)
        iban = {"acc-1": "PT50000201231234567890154", "acc-2": "PT43000201239876543211234"}[account_id]
        return BankAccountInfo(account_id, iban=iban, currency="EUR")

    def booked_transactions(self, account_id: str, date_from: date, date_to: date) -> list[BookedTransaction]:
        return list(self.rows.get(account_id, []))


def _linked_bank(h: Any, bank: FakeBank) -> tuple[str, dict[str, str]]:
    account = signup(h.client)
    tenant, H = account["tenant"]["id"], bearer(account["token"])
    h.client.post("/api/onboarding/company", json={"name": "Hazel Tree", "taxId": "501234560"}, headers=H)
    h.client.post("/api/connections/bank/start", json={"institutionId": "MILLENNIUMBCP_BCOMPTPL",
                                                       "companyId": "padaria-lda"}, headers=H)
    back = h.client.get("/api/connections/bank/callback", params={"ref": bank.reference}, follow_redirects=False)
    assert back.headers["location"].endswith("?bank=done")
    return tenant, H


def _transactions(h: Any, tenant: str) -> dict[str, Any]:
    with h.manager.open(tenant) as rt:
        return {t.tx.counterparty: t for t in rt.service.repo.transactions.values()}


def test_payments_for_a_bank_account_nobody_added_are_kept_and_the_owner_is_asked(tmp_path: Path) -> None:
    bank = FakeBank()
    h, vault, _ = _setup(tmp_path, aggregator=lambda: bank)
    tenant, H = _linked_bank(h, bank)
    worker = SyncWorker(h.manager, vault=vault, aggregator_factory=lambda: bank)
    worker.run_once()
    assert set(_transactions(h, tenant)) == {"EDP COMERCIAL"}
    # The bank opens a second account under the same consent; nobody added it to a company.
    bank.accounts = ["acc-1", "acc-2"]
    h.clock.advance(hours=7)
    report = worker.run_once()
    assert report.rows == 3 and bank.looked_up[-1] == "acc-2"
    assert vault.open(tenant, "bank-millenniumbcp")["accounts"]["acc-2"] == "PT43000201239876543211234"
    rows = _events(h, tenant, "sync.bank")[-1].data["rows"]
    assert {r["account_id"] for r in rows if r["bank_tx_id"] in ("bk-21", "bk-22")} == {
        "iban:PT43000201239876543211234"}
    assert set(_transactions(h, tenant)) == {"EDP COMERCIAL"}  # not checked yet, but kept, not dropped
    items = h.client.get("/api/needs-you", headers=H).json()["items"]
    (item,) = [i for i in items if i["id"].startswith("account_")]
    assert item["question"] == ("Your bank sent payments for an account I don't know (…1234). Add it to Padaria Lda, "
                                "to another company, or ignore it?")
    assert [o["label"] for o in item["options"]] == ["Add it to Padaria Lda", "Add it to Hazel Tree", "Ignore it"]
    assert item["merchant"] == "Millenniumbcp" and item["date"] == "2026-09-21"
    _plain(item["question"], *item["why"], *(o["label"] for o in item["options"]))
    assert h.client.get("/api/home", headers=H).json()["needsYouCount"] >= 1
    activity = json.dumps(h.client.get("/api/activity", headers=H).json(), ensure_ascii=False)
    assert "Millenniumbcp sent 2 payments for an account I don't know (…1234)." in activity

    res = h.client.post(f"/api/needs-you/{item['id']}/answer", json={"optionId": "company:hazel-tree"}, headers=H)
    assert res.status_code == 200, res.text
    assert res.json()["message"] == ("Done. I added the account ending in 1234 to Hazel Tree and checked the "
                                     "2 payments it sent.")
    txs = _transactions(h, tenant)
    assert {"LANDLORD LDA", "PAPELARIA CENTRAL"} <= set(txs) and txs["LANDLORD LDA"].holder_id == "hazel-tree"
    assert not [i for i in h.client.get("/api/needs-you", headers=H).json()["items"] if i["id"].startswith("account_")]
    # The next sync files them under the new account: the same payments, never twice.
    h.clock.advance(hours=7)
    worker.run_once()
    assert len(_transactions(h, tenant)) == 3
    assert {r["account_id"] for r in _events(h, tenant, "sync.bank")[-1].data["rows"]} == {
        "acct-millenniumbcp-0154", txs["LANDLORD LDA"].tx.account_id}
    _same_after_replay(h, tenant)


def test_an_account_removed_by_the_owner_keeps_sending_payments_that_are_kept_not_dropped(tmp_path: Path) -> None:
    bank = FakeBank()
    bank.accounts = ["acc-1", "acc-2"]
    h, vault, _ = _setup(tmp_path, aggregator=lambda: bank)
    tenant, H = _linked_bank(h, bank)
    accounts = h.manager.read(tenant, lambda svc: {a.iban: a.id for a in svc.repo.accounts.values() if a.iban})
    removed = accounts["PT43000201239876543211234"]
    assert h.client.post(f"/api/sources/{removed}/remove", headers=H).status_code == 200
    worker = SyncWorker(h.manager, vault=vault, aggregator_factory=lambda: bank)
    report = worker.run_once()
    assert report.rows == 3 and set(_transactions(h, tenant)) == {"EDP COMERCIAL"}
    (item,) = [i for i in h.client.get("/api/needs-you", headers=H).json()["items"] if i["id"].startswith("account_")]
    res = h.client.post(f"/api/needs-you/{item['id']}/answer", json={"optionId": "ignore"}, headers=H)
    assert res.status_code == 200 and res.json()["message"] == ("Done. I won't check payments from the account "
                                                                "ending in 1234.")
    with h.manager.open(tenant) as rt:
        kept = rt.service.unknown_accounts[item["id"]]
    assert kept["status"] == "ignored" and sorted(r["bank_tx_id"] for r in kept["rows"]) == ["bk-21", "bk-22"]
    h.clock.advance(hours=7)
    worker.run_once()  # more of the same: still kept, never asked again
    assert not [i for i in h.client.get("/api/needs-you", headers=H).json()["items"] if i["id"].startswith("account_")]
    _same_after_replay(h, tenant)


def test_bank_rows_are_deduplicated_on_the_banks_own_transaction_id(tmp_path: Path) -> None:
    bank = FakeBank()
    h, vault, _ = _setup(tmp_path, aggregator=lambda: bank)
    tenant, _ = _linked_bank(h, bank)
    worker = SyncWorker(h.manager, vault=vault, aggregator_factory=lambda: bank)
    worker.run_once()
    assert _events(h, tenant, "sync.bank")[-1].data["rows"][0]["bank_tx_id"] == "bk-1"
    # The bank re-words the same payment (the same id) on the next read: still one payment.
    bank.rows["acc-1"] = [BookedTransaction("bk-1", date(2026, 9, 19), Decimal("-64.10"), "EUR",
                                            creditor_name="EDP COMERCIAL SA",
                                            remittance="DD EDP COMERCIAL SEPA REF 8812", bank_code="PMNT-RDDT-ESDD")]
    h.clock.advance(hours=7)
    worker.run_once()
    assert len(_transactions(h, tenant)) == 1
    # Two equal coffees the same day with their own ids are two payments.
    bank.rows["acc-1"] += [BookedTransaction(f"bk-c{i}", date(2026, 9, 25), Decimal("-1.20"), "EUR",
                                             creditor_name="CAFE CENTRAL", remittance="COMPRA") for i in range(2)]
    h.clock.advance(hours=7)
    worker.run_once()
    assert h.manager.read(tenant, lambda svc: len(svc.repo.transactions)) == 3
    _same_after_replay(h, tenant)


# =========================================================================== 5. supplier websites: sign-in codes


class CodePortal(SupplierPortalConnector):
    """A supplier's website that sends a one-time code by SMS at every sign-in (stands in for Vodafone)."""

    __test__ = False
    supplier_key = "vodafone_pt"
    display_name = "Vodafone"
    domains = ("vodafone.pt",)
    instances: list[CodePortal] = []
    sent_codes: list[str] = []

    def __init__(self) -> None:
        CodePortal.instances.append(self)
        self.retrieved: list[str] = []

    def authenticate(self, credentials: Any) -> AuthResult:
        assert credentials.password.get_secret_value() == "portal-password-1"
        CodePortal.sent_codes.append(f"{len(CodePortal.sent_codes) + 1:06d}")
        return self.mfa_required(credentials.username, channel="sms",
                                 resume_state={"flow": f"flow-{len(CodePortal.sent_codes)}"})

    def complete_mfa(self, challenge: Any, code: str) -> AuthResult:
        expected = CodePortal.sent_codes[int(challenge.resume_state["flow"].split("-")[1]) - 1]
        if code != expected:
            return AuthResult(AuthStatus.CODE_REJECTED)
        session = PortalSession(self.supplier_key, challenge.account, datetime(2026, 10, 2, tzinfo=timezone.utc),
                                state={"cookie": "session-cookie"})
        return AuthResult(AuthStatus.AUTHENTICATED, session)

    def list_invoices(self, session: Any, since: date, until: date) -> list[PortalInvoiceRef]:
        return [PortalInvoiceRef(self.supplier_key, f"inv-{i}", f"FT 2026/{180 + i}", date(2026, 9, 20 + i),
                                 Decimal("92.40"), "EUR") for i in range(2)]

    def retrieve_invoice(self, session: Any, ref: PortalInvoiceRef) -> PortalDocument:
        assert session.state == {"cookie": "session-cookie"}
        self.retrieved.append(ref.portal_id)
        return PortalDocument(ref, b"%PDF-1.7\n% Vodafone " + ref.portal_id.encode() + b"\n%%EOF\n",
                              filename=f"{ref.portal_id}.pdf")

    def retrieve_statement(self, session: Any, period_start: date, period_end: date) -> None:
        return None


def _portal_setup(tmp_path: Path) -> tuple[Any, TokenVault, FakeExpo, FakeReader, str, dict[str, str], SyncWorker]:
    CodePortal.instances.clear()
    CodePortal.sent_codes.clear()
    reader = FakeReader(_outcome())
    factory = lambda key: CodePortal() if key == "vodafone_pt" else None  # noqa: E731
    h, vault, expo = _setup(tmp_path, reader=reader, portal_factory=factory)
    account = signup(h.client)
    tenant, H = account["tenant"]["id"], bearer(account["token"])
    h.client.post("/api/devices", json={"expoPushToken": TOKEN_A, "platform": "ios"}, headers=H)
    res = h.client.post("/api/sources", json={"kind": "portal", "supplier": "Vodafone", "portal": "vodafone_pt",
                                              "username": "ana@padaria.pt", "password": "portal-password-1"},
                        headers=H)
    assert res.status_code == 200, res.text
    assert "portal-password-1" not in "\n".join(r.body for r in h.store.events(tenant))
    worker = SyncWorker(h.manager, vault=vault, portals=PortalWorker(h.manager, vault=vault, factory=factory))
    return h, vault, expo, reader, tenant, H, worker


def test_a_sign_in_code_resumes_the_portal_and_the_invoices_are_read_before_recording(tmp_path: Path) -> None:
    h, vault, expo, reader, tenant, H, worker = _portal_setup(tmp_path)
    report = worker.run_once()
    assert report.codes == [f"{tenant}/portal-vodafone"]
    asked = _events(h, tenant, "portal.code_needed")[-1].data
    assert asked["channel"] == "sms" and asked["expiresAt"] and "flow" not in json.dumps(asked)  # resume: vault only
    assert vault.open(tenant, "portal-vodafone")["challenge"]["resume"] == {"flow": "flow-1"}
    assert [(m["title"], m["body"]) for m in expo.sent] == [("Sign-in code needed", "Vodafone needs a sign-in code.")]
    assert code_prompt("Vodafone") == "Vodafone needs a sign-in code."
    (item,) = [i for i in h.client.get("/api/needs-you", headers=H).json()["items"] if i["kind"] == "code"]
    assert item["title"] == "Vodafone needs a sign-in code."
    assert item["question"] == "Vodafone sent you a sign-in code to your phone. Enter it so I can fetch your invoices."
    assert item["code"]["submitPath"] == "/api/portals/portal-vodafone/code"
    _plain(item["title"], item["question"], *item["why"])
    assert worker.run_once().codes == []  # waiting for the owner: not asked again, no second push
    assert len(expo.sent) == 1

    wrong = h.client.post("/api/portals/portal-vodafone/code", json={"code": "999999"}, headers=H)
    assert wrong.status_code == 400 and wrong.json()["message"] == "That code didn't work. Check it and try again."
    bad = h.client.post("/api/portals/portal-vodafone/code", json={"code": "<script>"}, headers=H)
    assert bad.status_code == 400 and bad.json()["message"] == "Enter the code exactly as you received it."
    events = len(h.store.events(tenant))
    assert len(reader.calls) == 0
    right = h.client.post("/api/portals/portal-vodafone/code", json={"code": "000 001"}, headers=H)
    assert right.status_code == 200, right.text
    assert right.json()["message"] == "Done. I signed in to Vodafone and fetched 2 invoices."
    _plain(wrong.json()["message"], right.json()["message"])
    # Each step ran in a fresh, isolated adapter: the scheduled sign-in that asked for the code, the wrong code, and
    # the right one, which continued the same sign-in (its flow, from the vault) and fetched the invoices.
    assert len(CodePortal.instances) == 3 and CodePortal.instances[-1].retrieved == ["inv-0", "inv-1"]
    assert CodePortal.instances[0].retrieved == [] and CodePortal.sent_codes == ["000001"]
    assert len(reader.calls) == 2  # read before the event was recorded ...
    retrieved = _events(h, tenant, "portal.retrieved")[-1]
    assert len(h.store.events(tenant)) == events + 1 and len(retrieved.data["reads"]) == 2  # ... and kept in it
    assert [d["portalId"] for d in retrieved.data["documents"]] == ["inv-0", "inv-1"]
    assert "Vodafone inv-0" not in retrieved.body  # the files themselves are in the object store
    assert not [i for i in h.client.get("/api/needs-you", headers=H).json()["items"] if i["kind"] == "code"]
    assert vault.open(tenant, "portal-vodafone")["challenge"] == {}
    assert "Fetched 2 invoices from Vodafone's website." in json.dumps(h.client.get("/api/activity", headers=H).json())
    again = h.client.post("/api/portals/portal-vodafone/code", json={"code": "000001"}, headers=H)
    assert again.status_code == 404 and again.json()["message"] == "Vodafone isn't waiting for a code right now."
    docs = [e for e in _evidence(h, tenant) if e.source_kind.value == "supplier_portal"]
    assert len(docs) == 2
    # A replay (another process, no reader, no website) rebuilds the same business.
    broken = FakeReader(fail=True)
    h.clock.step = h.clock.step * 0
    with h.manager.open(tenant) as rt:
        live = state_digest(rt.service)
    with TenantManager(h.store, h.objects, now=h.clock, reader=broken, strict_reads=True).open(tenant) as rt:
        assert state_digest(rt.service) == live
    assert broken.calls == []


def test_an_expired_code_asks_the_website_for_a_new_one(tmp_path: Path) -> None:
    h, vault, expo, _, tenant, H, worker = _portal_setup(tmp_path)
    worker.run_once()
    h.clock.advance(minutes=11)  # the website's code works for 10 minutes
    res = h.client.post("/api/portals/portal-vodafone/code", json={"code": "000001"}, headers=H)
    assert res.status_code == 410
    assert res.json()["message"] == "That code has expired. Vodafone sent you a new one. Enter it when it arrives."
    _plain(res.json()["message"])
    assert len(_events(h, tenant, "portal.code_needed")) == 2 and len(expo.sent) == 2  # a new code, a new push
    assert vault.open(tenant, "portal-vodafone")["challenge"]["resume"] == {"flow": "flow-2"}
    stale = h.client.post("/api/portals/portal-vodafone/code", json={"code": "000001"}, headers=H)
    assert stale.status_code == 400  # the first code is no longer the one the website waits for
    ok = h.client.post("/api/portals/portal-vodafone/code", json={"code": "000002"}, headers=H)
    assert ok.status_code == 200 and ok.json()["documents"] == 2
    # Only the owner enters codes.
    other = signup(h.client, "rui@oficina.pt", tax_id=None)
    assert h.client.post("/api/portals/portal-vodafone/code", json={"code": "000002"},
                         headers=bearer(other["token"])).status_code == 404
    _same_after_replay(h, tenant)


def test_portal_resume_in_the_library_handles_wrong_expired_and_good_codes() -> None:
    from backoffice.connectors.base import ConnectorKind
    from backoffice.connectors.portals import MfaChallenge, PortalSync

    CodePortal.sent_codes[:] = ["123456"]
    now = datetime(2026, 10, 2, 9, 30, tzinfo=timezone.utc)
    state = ConnectorState(tenant_id="t1", kind=ConnectorKind.SUPPLIER_PORTAL, account="ana@padaria.pt",
                           display_name="Vodafone")
    challenge = MfaChallenge("vodafone_pt", "ana@padaria.pt", "sms", {"flow": "flow-1"}, now,
                             now + timedelta(minutes=10))
    runner = PortalSync(CodePortal(), clock=lambda: now)
    wrong = runner.resume(state, challenge, "000000", lambda d: None, now=now)
    assert wrong.code_rejected and wrong.challenge == challenge and wrong.outcome.state == state
    late = runner.resume(state, challenge, "123456", lambda d: None, now=now + timedelta(minutes=10))
    assert late.code_expired and late.outcome.state == state
    got: list[PortalDocument] = []
    good = runner.resume(state, challenge, "123456", got.append, known_ids={"inv-0"}, now=now)
    assert good.outcome.ok and [d.ref.portal_id for d in got] == ["inv-1"]
    assert good.session is not None and good.outcome.state.last_successful_sync == now
    # A portal without codes keeps refusing a resume (the default), never pretends it signed in.
    assert SupplierPortalConnector.complete_mfa(CodePortal(), challenge, "123456").status is AuthStatus.FAILED
