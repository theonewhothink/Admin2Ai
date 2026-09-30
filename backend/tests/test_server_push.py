"""Expo push notifications: only hard approvals, reconnects and closed months, never during a replay (§42)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from _server_support import bearer, harness, signup

from backoffice.server.notify import (
    EXPO_PUSH_URL,
    ExpoPushClient,
    Facts,
    PushError,
    PushMessage,
    PushNotifier,
    facts,
    messages_for,
)
from backoffice.server.store import Device, MemoryStore
from backoffice.service import BackOfficeService

TOKEN_A = "ExponentPushToken[aaaaaaaaaaaaaaaaaaaaaa]"
TOKEN_B = "ExponentPushToken[bbbbbbbbbbbbbbbbbbbbbb]"
EMPTY = Facts(frozenset(), frozenset(), frozenset())


class FakeExpo:
    """Expo's push endpoint: records requests, answers with tickets."""

    def __init__(self, gone: set[str] = frozenset()) -> None:  # type: ignore[assignment]
        self.requests: list[httpx.Request] = []
        self.gone = gone

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        messages = json.loads(request.content)
        tickets = [{"status": "error", "message": "gone", "details": {"error": "DeviceNotRegistered"}}
                   if m["to"] in self.gone else {"status": "ok", "id": f"t{i}"} for i, m in enumerate(messages)]
        return httpx.Response(200, json={"data": tickets})

    @property
    def sent(self) -> list[dict]:
        return [m for r in self.requests for m in json.loads(r.content)]


def test_client_posts_expo_json_with_the_access_token() -> None:
    fake = FakeExpo()
    client = ExpoPushClient(access_token="expo-token", transport=httpx.MockTransport(fake))
    tickets = client.send([{"to": TOKEN_A, "title": "t", "body": "b"}])
    assert tickets == [{"status": "ok", "id": "t0"}]
    request = fake.requests[0]
    assert str(request.url) == EXPO_PUSH_URL and request.method == "POST"
    assert request.headers["authorization"] == "Bearer expo-token"
    assert request.headers["content-type"] == "application/json"
    assert "expo-token" not in repr(client)


def test_client_batches_by_one_hundred() -> None:
    fake = FakeExpo()
    client = ExpoPushClient(transport=httpx.MockTransport(fake))
    assert len(client.send([{"to": TOKEN_A, "title": "t", "body": str(i)} for i in range(250)])) == 250
    assert [len(json.loads(r.content)) for r in fake.requests] == [100, 100, 50]
    assert "authorization" not in fake.requests[0].headers  # the access token is optional


@pytest.mark.parametrize("response", [httpx.Response(500), httpx.Response(200, content=b"<html>"),
                                      httpx.Response(200, json={"data": []})])
def test_client_errors_are_typed(response: httpx.Response) -> None:
    client = ExpoPushClient(transport=httpx.MockTransport(lambda r: response))
    with pytest.raises(PushError):
        client.send([{"to": TOKEN_A, "title": "t", "body": "b"}])


def test_what_the_owner_is_told_in_plain_words() -> None:
    svc = BackOfficeService.demo()
    after = facts(svc)
    messages = messages_for(svc, EMPTY, after)
    assert PushMessage("Payment on hold", "Vodafone changed the bank details on its invoice. Payment blocked.",
                       "/needs-you#nd_vodafone_iban") in messages
    assert PushMessage("Month closed", "September is closed for Company B.", "/") in messages
    assert not any("IKEA" in m.body for m in messages)  # a question is not a notification
    assert messages_for(svc, after, after) == []
    svc.mark_connection_stale("gmail")
    stale = messages_for(svc, after, facts(svc))
    assert stale == [PushMessage("Connection needs you", "Gmail needs reconnecting.", "/settings#connections")]


def test_owner_phones_get_it_and_gone_phones_are_forgotten() -> None:
    from datetime import datetime, timezone

    store = MemoryStore()
    now = datetime(2026, 10, 2, tzinfo=timezone.utc)
    from backoffice.server.store import Tenant, User

    store.create_account(user=User("usr_a", "a@x.pt", "A"), password_hash="scrypt$1$1$1$a$b",
                         tenant=Tenant("t1", "A"), roles=["owner"], events=[], at=now)
    store.create_account(user=User("usr_b", "b@x.pt", "B"), password_hash="scrypt$1$1$1$a$b",
                         tenant=Tenant("t2", "B"), roles=["owner"], events=[], at=now)
    store.add_membership("t1", "usr_b", "accountant")
    store.save_device(Device("t1", TOKEN_A, "usr_a", "ios", now, now))
    store.save_device(Device("t1", TOKEN_B, "usr_b", "android", now, now))  # the accountant's phone
    fake = FakeExpo(gone={TOKEN_A})
    notifier = PushNotifier(store, ExpoPushClient(transport=httpx.MockTransport(fake)))
    accepted = notifier.deliver("t1", [PushMessage("Month closed", "September is closed for A.", "/")])
    assert accepted == 0 and [m["to"] for m in fake.sent] == [TOKEN_A]
    assert fake.sent[0]["data"] == {"url": "/"} and fake.sent[0]["sound"] == "default"
    assert [d.token for d in store.devices("t1")] == [TOKEN_B]


def test_a_change_notifies_live_and_a_replay_stays_quiet(tmp_path: Path) -> None:
    fake = FakeExpo()
    store = MemoryStore()
    notifier = PushNotifier(store, ExpoPushClient(transport=httpx.MockTransport(fake)))
    h = harness(tmp_path, store=store, notifier=notifier)
    account = signup(h.client)
    H, tenant = bearer(account["token"]), account["tenant"]["id"]
    h.client.post("/api/devices", json={"expoPushToken": TOKEN_A, "platform": "ios"}, headers=H)
    h.client.post("/api/sources", json={"kind": "email", "provider": "imap", "address": "ana@padaria.pt",
                                        "host": "imap.padaria.pt", "password": "app-password-123"}, headers=H)
    h.client.post("/api/tasks", json={"title": "Quiet success"}, headers=H)
    assert fake.sent == []  # routine work never notifies
    h.client.post("/api/connections/mail-ana-padaria-pt/stale", headers=H)
    assert [(m["to"], m["title"], m["body"]) for m in fake.sent] == [
        (TOKEN_A, "Connection needs you", "Email (imap.padaria.pt) needs reconnecting.")]
    h.client.post("/api/connections/mail-ana-padaria-pt/stale", headers=H)
    assert len(fake.sent) == 1  # still stale: no second notification
    h.manager.evict(tenant)
    h.client.get("/api/home", headers=H)  # rebuilt by replaying every event
    assert len(fake.sent) == 1
