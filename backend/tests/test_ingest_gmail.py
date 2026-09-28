"""Gmail connector: history sync, full-sync fallback, raw messages, push (§8, §47)."""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from backoffice.connectors.base import (
    ConnectorKind,
    ConnectorState,
    Health,
    ReconnectRequired,
    TimeRange,
    WebhookState,
    evaluate_health,
)
from backoffice.connectors.gmail import GMAIL_API, GmailConnector

NOW = datetime(2026, 9, 25, 9, 30, tzinfo=timezone.utc)
RAW = b"From: faturas@vodafone.pt\r\nSubject: Fatura\r\n\r\nbody"


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


class Tokens:
    def __init__(self, fail_refresh=False):
        self.issued = 0
        self.invalidated = 0
        self.fail_refresh = fail_refresh

    def access_token(self):
        if self.fail_refresh and self.invalidated:
            raise ReconnectRequired("gmail_oauth_invalid_grant")
        self.issued += 1
        return f"at-{self.issued}"

    def invalidate(self):
        self.invalidated += 1


class FakeGmail:
    """A small Gmail API: messages, history, profile. Records every request."""

    def __init__(self):
        self.messages = {f"m{i}": {"labels": ["INBOX"]} for i in range(1, 4)}
        self.history_pages = [
            {"history": [{"id": "150", "messagesAdded": [{"message": {"id": "m4", "labelIds": ["INBOX"]}}]},
                         {"id": "151", "messagesAdded": [{"message": {"id": "d1", "labelIds": ["DRAFT"]}}]}],
             "historyId": "160", "nextPageToken": "p2"},
            {"history": [{"id": "161", "messagesAdded": [{"message": {"id": "m4", "labelIds": ["INBOX"]}},
                                                          {"message": {"id": "m5", "labelIds": ["SPAM"]}},
                                                          {"message": {"id": "m6", "labelIds": ["INBOX"]}}]}],
             "historyId": "170"},
        ]
        self.history_expired = False
        self.requests: list[httpx.Request] = []
        self.status_overrides: dict[str, httpx.Response] = {}
        self.gone = {"m6"}  # deleted between history and fetch

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path.replace("/gmail/v1/users/me", "")
        if path in self.status_overrides:
            return self.status_overrides.pop(path)
        if path == "/profile":
            return httpx.Response(200, json={"emailAddress": "ana@padaria.pt", "historyId": "100"})
        if path == "/messages":
            ids = sorted(self.messages)
            if request.url.params.get("pageToken") == "n2":
                return httpx.Response(200, json={"messages": [{"id": i} for i in ids[2:]]})
            return httpx.Response(200, json={"messages": [{"id": i} for i in ids[:2]], "nextPageToken": "n2"})
        if path == "/history":
            if self.history_expired:
                return httpx.Response(404, json={"error": {"code": 404, "message": "Requested entity was not found."}})
            page = 1 if request.url.params.get("pageToken") == "p2" else 0
            return httpx.Response(200, json=self.history_pages[page])
        if path.startswith("/messages/"):
            mid = path.rsplit("/", 1)[1]
            if mid in self.gone:
                return httpx.Response(404, json={"error": {"code": 404}})
            return httpx.Response(200, json={"id": mid, "threadId": f"t-{mid}", "labelIds": ["INBOX"],
                                             "internalDate": "1758792600000", "raw": b64url(RAW + mid.encode())})
        if path == "/watch":
            return httpx.Response(200, json={"historyId": "180", "expiration": str(int((NOW + timedelta(days=7)).timestamp() * 1000))})
        return httpx.Response(404)


def connector(api: FakeGmail, tokens=None) -> GmailConnector:
    client = httpx.Client(transport=httpx.MockTransport(api))
    return GmailConnector(tokens or Tokens(), client=client, clock=lambda: NOW)


def new_state(**kw) -> ConnectorState:
    return ConnectorState(tenant_id="t1", kind=ConnectorKind.GMAIL, account="ana@padaria.pt", **kw)


def test_first_sync_is_a_full_window_sync_with_history_cursor():
    api, got = FakeGmail(), []
    outcome = connector(api).sync(new_state(), got.append)
    assert outcome.ok and outcome.full_sync and outcome.delivered == 3
    assert [m.provider_id for m in got] == ["m1", "m2", "m3"]
    assert got[0].raw == RAW + b"m1" and got[0].thread_id == "t-m1"
    assert got[0].received_at == datetime.fromtimestamp(1758792600, tz=timezone.utc)
    state = outcome.state
    assert state.cursor == "100" and state.last_successful_sync == NOW
    assert state.coverage_start == NOW - timedelta(days=90)
    # historyId was taken before listing; list query is time-zone exact and excludes drafts.
    paths = [r.url.path.rsplit("/", 1)[-1] for r in api.requests]
    assert paths[0] == "profile"
    listing = next(r for r in api.requests if r.url.path.endswith("/messages"))
    assert listing.url.params["q"] == f"after:{int((NOW - timedelta(days=90)).timestamp())} -in:drafts -in:chats"
    assert listing.url.params["includeSpamTrash"] == "false"
    fetch = next(r for r in api.requests if r.url.path.endswith("/m1"))
    assert fetch.url.params["format"] == "raw" and fetch.headers["authorization"].startswith("Bearer at-")


def test_incremental_sync_uses_history_and_advances_cursor():
    api, got = FakeGmail(), []
    state = new_state(cursor="140", last_successful_sync=NOW - timedelta(hours=1))
    outcome = connector(api).sync(state, got.append)
    assert outcome.ok and not outcome.full_sync
    assert [m.provider_id for m in got] == ["m4"]  # draft and spam skipped, deleted m6 tolerated
    assert outcome.state.cursor == "170"
    history = [r for r in api.requests if r.url.path.endswith("/history")]
    assert history[0].url.params["startHistoryId"] == "140"
    assert history[0].url.params["historyTypes"] == "messageAdded"


def test_expired_history_falls_back_to_full_sync_and_records_the_hole():
    api = FakeGmail()
    api.history_expired = True
    long_ago = NOW - timedelta(days=120)
    state = new_state(cursor="5", last_successful_sync=long_ago, coverage_start=long_ago - timedelta(days=90))
    outcome = connector(api).sync(state, lambda m: None)
    assert outcome.ok and outcome.full_sync and outcome.state.cursor == "100"
    (gap,) = outcome.state.known_gaps
    assert gap.start == long_ago and gap.end == NOW - timedelta(days=90)


def test_recent_history_expiry_leaves_no_gap():
    api = FakeGmail()
    api.history_expired = True
    state = new_state(cursor="5", last_successful_sync=NOW - timedelta(days=10))
    assert connector(api).sync(state, lambda m: None).state.known_gaps == ()


def test_expired_access_token_is_refreshed_once():
    api, tokens = FakeGmail(), Tokens()
    api.status_overrides["/profile"] = httpx.Response(401)
    outcome = connector(api, tokens).sync(new_state(), lambda m: None)
    assert outcome.ok and tokens.invalidated == 1


def test_revoked_grant_becomes_reconnect_and_plain_copy():
    api = FakeGmail()
    api.status_overrides["/profile"] = httpx.Response(401)
    before = new_state(last_successful_sync=NOW - timedelta(hours=19), cursor=None)
    outcome = connector(api, Tokens(fail_refresh=True)).sync(before, lambda m: None)
    assert not outcome.ok and isinstance(outcome.error, ReconnectRequired)
    state = outcome.state
    assert state.reconnect_required and state.last_error_code == "gmail_oauth_invalid_grant"
    assert state.cursor is None and state.last_successful_sync == before.last_successful_sync
    report = evaluate_health(state, NOW)
    assert report.health is Health.BROKEN
    assert (report.title, report.detail) == ("Gmail needs reconnecting.", "Your email has not synced since 14:30 yesterday.")


@pytest.mark.parametrize(
    ("status", "reason", "reconnect"),
    [(403, "rateLimitExceeded", False), (403, "userRateLimitExceeded", False), (429, "rateLimitExceeded", False),
     (403, "insufficientPermissions", True), (500, "backendError", False)],
)
def test_gmail_error_reasons(status, reason, reconnect):
    api = FakeGmail()
    api.status_overrides["/profile"] = httpx.Response(status, json={"error": {"errors": [{"reason": reason}]}})
    outcome = connector(api).sync(new_state(), lambda m: None)
    assert outcome.state.reconnect_required is reconnect
    assert outcome.error.retryable is not reconnect


def test_partial_progress_is_counted_but_cursor_does_not_move():
    api, got = FakeGmail(), []
    api.status_overrides["/messages/m2"] = httpx.Response(503)
    outcome = connector(api).sync(new_state(), got.append)
    assert not outcome.ok and outcome.delivered == 1 and outcome.state.cursor is None
    assert outcome.state.consecutive_failures == 1


def test_backfill_reads_the_gap_and_closes_it():
    api, got = FakeGmail(), []
    gap = TimeRange(start=NOW - timedelta(days=200), end=NOW - timedelta(days=90))
    state = new_state(cursor="100", last_successful_sync=NOW, coverage_start=NOW - timedelta(days=90),
                      known_gaps=(gap,))
    outcome = connector(api).backfill(state, gap, got.append)
    assert outcome.ok and outcome.delivered == 3 and outcome.state.known_gaps == ()
    assert outcome.state.coverage_start == gap.start and outcome.state.cursor == "100"
    q = next(r for r in api.requests if r.url.path.endswith("/messages")).url.params["q"]
    assert q.startswith(f"after:{int(gap.start.timestamp())} before:")


def test_watch_and_push_notifications():
    api = FakeGmail()
    gmail = connector(api)
    watched = gmail.start_watch(new_state(), "projects/p/topics/gmail")
    assert watched.webhook_state is WebhookState.ACTIVE and watched.webhook_expires_at == NOW + timedelta(days=7)
    assert json.loads(api.requests[-1].content) == {"topicName": "projects/p/topics/gmail"}
    data = base64.b64encode(json.dumps({"emailAddress": "ana@padaria.pt", "historyId": 190}).encode()).decode()
    push = GmailConnector.parse_push({"message": {"data": data, "publishTime": "2026-09-25T09:31:00Z"}})
    assert push.history_id == "190"
    recorded = GmailConnector.record_push(watched, push, now=NOW)
    assert recorded.last_event_at == datetime(2026, 9, 25, 9, 31, tzinfo=timezone.utc)
    other = GmailConnector.parse_push({"message": {"data": base64.b64encode(
        json.dumps({"emailAddress": "someone@else.pt", "historyId": 1}).encode()).decode()}})
    assert GmailConnector.record_push(watched, other, now=NOW) == watched


def test_bad_push_payload_is_a_typed_error():
    from backoffice.connectors.base import ProviderError

    with pytest.raises(ProviderError):
        GmailConnector.parse_push({"message": {"data": "not-base64!!"}})


def test_default_base_url():
    assert GMAIL_API == "https://gmail.googleapis.com/gmail/v1/users/me"
