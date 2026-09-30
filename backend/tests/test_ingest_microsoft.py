"""Microsoft Graph mail connector: delta per folder, MIME, attachments, webhooks (§8, §47)."""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from backoffice.connectors.base import (
    BackfillReason,
    ConnectorKind,
    ConnectorState,
    TimeRange,
    WebhookState,
    needs_backfill,
)
from backoffice.connectors.microsoft import GRAPH_API, GraphMailConfig, MicrosoftMailConnector

NOW = datetime(2026, 9, 25, 9, 30, tzinfo=timezone.utc)
G = "https://graph.microsoft.com/v1.0"


class Tokens:
    def access_token(self):
        return "at"

    def invalidate(self):
        pass


class FakeGraph:
    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.overrides: dict[str, httpx.Response] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path.replace("/v1.0", "")
        if path in self.overrides:
            return self.overrides.pop(path)
        routes = {
            "/me/mailFolders/inbox": {"id": "INBOX", "childFolderCount": 1},
            "/me/mailFolders/archive": None,
            "/me/mailFolders/INBOX/childFolders": {"value": [{"id": "FATURAS", "childFolderCount": 0}]},
        }
        if path in routes:
            body = routes[path]
            return httpx.Response(404) if body is None else httpx.Response(200, json=body)
        if path == "/me/mailFolders/INBOX/messages/delta":
            if request.url.params.get("$deltatoken") == "inbox-2":
                return httpx.Response(200, json={"value": [{"id": "n1", "receivedDateTime": "2026-09-25T09:00:00Z"}],
                                                 "@odata.deltaLink": f"{G}/me/mailFolders/INBOX/messages/delta?$deltatoken=inbox-3"})
            if request.url.params.get("$skiptoken") == "s2":
                return httpx.Response(200, json={
                    "value": [{"id": "a2", "receivedDateTime": "2026-09-02T10:00:00Z", "isDraft": False},
                              {"id": "gone", "@removed": {"reason": "deleted"}}],
                    "@odata.deltaLink": f"{G}/me/mailFolders/INBOX/messages/delta?$deltatoken=inbox-2"})
            return httpx.Response(200, json={
                "value": [{"id": "a1", "receivedDateTime": "2026-09-01T10:00:00Z", "conversationId": "c1"},
                          {"id": "draft", "isDraft": True}],
                "@odata.nextLink": f"{G}/me/mailFolders/INBOX/messages/delta?$skiptoken=s2"})
        if path == "/me/mailFolders/FATURAS/messages/delta":
            if request.url.params.get("$deltatoken") == "f-2":
                return httpx.Response(200, json={"value": [], "@odata.deltaLink": str(request.url)})
            return httpx.Response(200, json={"value": [{"id": "f1", "receivedDateTime": "2026-09-03T10:00:00Z"}],
                                             "@odata.deltaLink": f"{G}/me/mailFolders/FATURAS/messages/delta?$deltatoken=f-2"})
        if path.endswith("/$value") and path.startswith("/me/messages/") and "/attachments/" not in path:
            mid = path.split("/")[3]
            return httpx.Response(200, content=f"Subject: {mid}\r\n\r\nbody".encode())
        if path == "/me/messages":
            return httpx.Response(200, json={"value": [{"id": "old1"}, {"id": "old2"}]})
        if path == "/me/messages/a1/attachments":
            return httpx.Response(200, json={"value": [
                {"@odata.type": "#microsoft.graph.fileAttachment", "id": "at1", "name": "FT.pdf",
                 "contentType": "application/pdf", "size": 9, "isInline": False,
                 "contentBytes": base64.b64encode(b"%PDF-1.4x").decode()},
                {"@odata.type": "#microsoft.graph.fileAttachment", "id": "at2", "name": "big.pdf",
                 "contentType": "application/pdf", "size": 9_000_000, "isInline": False},
                {"@odata.type": "#microsoft.graph.itemAttachment", "id": "at3", "name": "Fwd", "isInline": False},
                {"@odata.type": "#microsoft.graph.referenceAttachment", "id": "at4", "name": "OneDrive link"},
                {"@odata.type": "#microsoft.graph.fileAttachment", "id": "at5", "name": "logo.png",
                 "contentType": "image/png", "isInline": True, "contentId": "logo@x",
                 "contentBytes": base64.b64encode(b"\x89PNG").decode()},
            ]})
        if path == "/me/messages/a1/attachments/at2/$value":
            return httpx.Response(200, content=b"%PDF-big")
        if path == "/me/messages/a1/attachments/at3/$value":
            return httpx.Response(200, content=b"Subject: attached\r\n\r\nx")
        if path == "/subscriptions" and request.method == "POST":
            body = json.loads(request.content)
            return httpx.Response(201, json={"id": "sub-1", "expirationDateTime": body["expirationDateTime"]})
        if path == "/subscriptions/sub-1" and request.method == "PATCH":
            return httpx.Response(200, json={"id": "sub-1", "expirationDateTime": json.loads(request.content)["expirationDateTime"]})
        return httpx.Response(404)


def connector(api: FakeGraph, config: GraphMailConfig | None = None) -> MicrosoftMailConnector:
    return MicrosoftMailConnector(Tokens(), client=httpx.Client(transport=httpx.MockTransport(api)),
                                  config=config, clock=lambda: NOW)


def new_state(**kw) -> ConnectorState:
    return ConnectorState(tenant_id="t1", kind=ConnectorKind.MICROSOFT, account="ana@padaria.pt", **kw)


def test_initial_sync_walks_delta_for_every_folder_including_children():
    api, got = FakeGraph(), []
    outcome = connector(api).sync(new_state(), got.append)
    assert outcome.ok and outcome.full_sync
    assert [(m.provider_id, m.folder) for m in got] == [("a1", "INBOX"), ("a2", "INBOX"), ("f1", "FATURAS")]
    assert got[0].raw.startswith(b"Subject: a1") and got[0].thread_id == "c1"
    assert got[0].received_at == datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
    cursor = json.loads(outcome.state.cursor)
    assert cursor["folders"] == {
        "FATURAS": f"{G}/me/mailFolders/FATURAS/messages/delta?$deltatoken=f-2",
        "INBOX": f"{G}/me/mailFolders/INBOX/messages/delta?$deltatoken=inbox-2",
    }
    first_delta = next(r for r in api.requests if r.url.path.endswith("INBOX/messages/delta"))
    assert first_delta.url.params["$filter"] == "receivedDateTime ge 2026-06-27T09:30:00Z"
    assert first_delta.headers["prefer"] == "odata.maxpagesize=50"
    assert outcome.state.coverage_start == NOW - timedelta(days=90)


def test_next_sync_resumes_from_delta_links():
    api, got = FakeGraph(), []
    first = connector(api).sync(new_state(), lambda m: None)
    outcome = connector(api).sync(first.state, got.append)
    assert not outcome.full_sync and [m.provider_id for m in got] == ["n1"]
    assert json.loads(outcome.state.cursor)["folders"]["INBOX"].endswith("inbox-3")


def test_lost_delta_state_restarts_the_folder_and_records_gap():
    api = FakeGraph()
    old = NOW - timedelta(days=120)
    cursor = json.dumps({"v": 1, "folders": {"INBOX": f"{G}/me/mailFolders/INBOX/messages/delta?$deltatoken=x"}})
    api.overrides["/me/mailFolders/INBOX/messages/delta"] = httpx.Response(410, json={"error": {"code": "syncStateNotFound"}})
    outcome = connector(api).sync(new_state(cursor=cursor, last_successful_sync=old), lambda m: None)
    assert outcome.ok and outcome.full_sync
    assert outcome.state.known_gaps[0].start == old


def test_foreign_links_never_receive_the_token():
    api = FakeGraph()
    evil = json.dumps({"v": 1, "folders": {"INBOX": "https://evil.example/delta?x=1"}})
    outcome = connector(api).sync(new_state(cursor=evil, last_successful_sync=NOW - timedelta(hours=1)), lambda m: None)
    assert outcome.ok and all(r.url.host == "graph.microsoft.com" for r in api.requests)
    api2 = FakeGraph()
    api2.overrides["/me/mailFolders/FATURAS/messages/delta"] = httpx.Response(
        200, json={"value": [], "@odata.nextLink": "https://evil.example/next"})
    bad = connector(api2).sync(new_state(), lambda m: None)
    assert not bad.ok and bad.error.code == "graph_foreign_link"
    assert all(r.url.host == "graph.microsoft.com" for r in api2.requests)


def test_throttling_is_transient_with_retry_after():
    api = FakeGraph()
    api.overrides["/me/mailFolders/inbox"] = httpx.Response(429, headers={"retry-after": "12"})
    outcome = connector(api).sync(new_state(), lambda m: None)
    assert not outcome.ok and outcome.error.retryable and outcome.error.retry_after == 12
    assert not outcome.state.reconnect_required


def test_consent_revoked_is_reconnect():
    api = FakeGraph()
    api.overrides["/me/mailFolders/inbox"] = httpx.Response(403, json={"error": {"code": "ErrorAccessDenied"}})
    assert connector(api).sync(new_state(), lambda m: None).state.reconnect_required


def test_backfill_uses_mailbox_wide_filter():
    api, got = FakeGraph(), []
    gap = TimeRange(start=datetime(2026, 5, 1, tzinfo=timezone.utc), end=datetime(2026, 6, 27, tzinfo=timezone.utc))
    outcome = connector(api).backfill(new_state(cursor="{}", known_gaps=(gap,)), gap, got.append)
    assert outcome.ok and [m.provider_id for m in got] == ["old1", "old2"] and outcome.state.known_gaps == ()
    req = next(r for r in api.requests if r.url.path.endswith("/me/messages"))
    assert req.url.params["$filter"] == (
        "receivedDateTime ge 2026-05-01T00:00:00Z and receivedDateTime le 2026-06-27T00:00:00Z")


def test_attachments_of_every_kind():
    atts = {a.attachment_id: a for a in connector(FakeGraph()).list_attachments("a1")}
    assert atts["at1"].kind == "file" and atts["at1"].data == b"%PDF-1.4x"
    assert atts["at2"].data == b"%PDF-big"  # large file: raw download
    assert atts["at3"].kind == "item" and atts["at3"].data.startswith(b"Subject: attached")
    assert atts["at4"].kind == "reference" and atts["at4"].data is None
    assert atts["at5"].is_inline and atts["at5"].content_id == "logo@x"


def test_subscription_lifecycle_and_client_state_checks():
    api = FakeGraph()
    graph = connector(api)
    state, sub = graph.create_subscription(new_state(), notification_url="https://hooks.example/graph",
                                           client_state="s3cret", lifecycle_url="https://hooks.example/life")
    body = json.loads(api.requests[-1].content)
    assert body["changeType"] == "created" and body["clientState"] == "s3cret"
    assert body["lifecycleNotificationUrl"] == "https://hooks.example/life"
    assert state.webhook_state is WebhookState.ACTIVE and sub.expires_at == NOW + timedelta(days=2)
    renewed = graph.renew_subscription(state, "sub-1", lifetime=timedelta(days=1))
    assert renewed.webhook_expires_at == NOW + timedelta(days=1)

    payload = {"value": [{"clientState": "s3cret", "resource": "x"}, {"clientState": "forged"}]}
    after, accepted = MicrosoftMailConnector.accept_notifications(renewed, payload, "s3cret", now=NOW)
    assert accepted == 1 and after.last_event_at == NOW
    untouched, none = MicrosoftMailConnector.accept_notifications(renewed, {"value": [{"clientState": "no"}]},
                                                                 "s3cret", now=NOW)
    assert none == 0 and untouched.last_event_at is None

    synced = renewed.model_copy(update={"last_successful_sync": NOW, "cursor": "{}"})
    missed = MicrosoftMailConnector.accept_lifecycle(synced, {"value": [{"clientState": "s3cret",
                                                                         "lifecycleEvent": "missed"}]}, "s3cret")
    assert missed.webhook_state is WebhookState.FAILED
    assert BackfillReason.WEBHOOK_LAPSED in needs_backfill(missed, NOW).reasons
    removed = MicrosoftMailConnector.accept_lifecycle(synced, {"value": [{"clientState": "s3cret",
                                                                          "lifecycleEvent": "subscriptionRemoved"}]}, "s3cret")
    assert removed.webhook_state is WebhookState.EXPIRED
    forged = MicrosoftMailConnector.accept_lifecycle(synced, {"value": [{"clientState": "x",
                                                                         "lifecycleEvent": "missed"}]}, "s3cret")
    assert forged == synced


@pytest.mark.parametrize("cursor", ["not json", json.dumps({"v": 99, "folders": {"INBOX": "x"}})])
def test_unreadable_cursor_means_full_sync(cursor):
    outcome = connector(FakeGraph()).sync(new_state(cursor=cursor), lambda m: None)
    assert outcome.ok and outcome.full_sync


def test_default_base_url():
    assert GRAPH_API == G
