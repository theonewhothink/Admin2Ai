"""Gmail / Google Workspace mailbox connector (§8, §47) over the Gmail REST API.

* First run: record the mailbox ``historyId`` *before* listing, list every
  message in the history window (``after:<epoch seconds>``, exact regardless
  of time zone), fetch each as ``format=raw``.
* Next runs: ``users.history.list`` from the stored ``historyId``
  (``messageAdded`` only), fetch new messages, store the new ``historyId``.
* History expired (HTTP 404): full sync again; if the last good sync is older
  than the window, the hole is recorded as a known gap to backfill.
* Push: ``users.watch`` to a Pub/Sub topic; notifications only mark events,
  the history cursor stays the source of truth (missed pushes lose nothing).

No folder or label is required (§8: does not depend on an "Invoices" folder).
Drafts and chats are skipped; spam and trash are opt-in.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from backoffice.domain.models import utcnow

from .base import (
    ConnectorError,
    ConnectorKind,
    ConnectorState,
    CountingSink,
    CursorExpired,
    MailItem,
    MailSink,
    ProviderError,
    ReconnectRequired,
    SyncOutcome,
    TimeRange,
    TransientError,
    WebhookState,
    gap_after_cursor_loss,
    record_backfill,
    record_event,
    record_failure,
    record_success,
    record_webhook,
)
from .http import AuthorizedHttp, json_object, object_list, required_str
from .oauth import TokenProvider

__all__ = ["GMAIL_API", "GMAIL_READONLY_SCOPE", "GmailConfig", "GmailConnector", "GmailPush"]

GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"
GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
_SKIP_LABELS = frozenset({"DRAFT", "CHAT"})
# Google API error reasons (usageLimits / global domains). Quota and rate limits
# are ours to wait out; they must never ask the owner to reconnect.
_TRANSIENT_REASONS = frozenset({"rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded", "dailyLimitExceeded",
                                "backendError"})
_PERMISSION_REASONS = frozenset({"insufficientPermissions", "forbidden", "domainPolicy", "authError"})


@dataclass(frozen=True)
class GmailConfig:
    history_window: timedelta = timedelta(days=90)  # §6; 365 days when the owner opts in
    page_size: int = 500
    include_spam_trash: bool = False
    max_pages: int = 10_000


@dataclass(frozen=True)
class GmailPush:
    email_address: str
    history_id: str
    published_at: datetime | None


def _reasons(response: httpx.Response) -> set[str]:
    try:
        payload = json.loads(response.content or b"{}")
        errors = payload.get("error", {}).get("errors", [])
        return {str(e.get("reason")) for e in errors if isinstance(e, dict)}
    except (ValueError, AttributeError):
        return set()


def _classify(response: httpx.Response) -> ConnectorError | None:
    """Gmail reports throttling as 403 with a reason; tell it apart from lost access."""
    status = response.status_code
    if status in (403, 429):
        reasons = _reasons(response)
        if reasons & _TRANSIENT_REASONS or status == 429:
            return TransientError(f"gmail_rate_limited_{status}")
        if reasons & _PERMISSION_REASONS or status == 403:
            return ReconnectRequired("gmail_insufficient_permissions")
    if status == 400 and "failedPrecondition" in _reasons(response):
        return ProviderError("gmail_mail_service_not_enabled")
    return None


def _epoch_ms(value: Any, code: str) -> datetime:
    """Gmail millisecond timestamps (``internalDate``, ``expiration``) as aware datetimes."""
    try:
        return datetime.fromtimestamp(int(str(value)) / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        raise ProviderError(code) from None


def _decode_raw(value: Any) -> bytes:
    if not isinstance(value, str):
        raise ProviderError("gmail_bad_raw_message")
    text = "".join(value.split())
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError):
        raise ProviderError("gmail_bad_raw_message") from None


class GmailConnector:
    kind = ConnectorKind.GMAIL

    def __init__(
        self,
        tokens: TokenProvider,
        *,
        client: httpx.Client | None = None,
        config: GmailConfig | None = None,
        base_url: str = GMAIL_API,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.config = config or GmailConfig()
        self.base_url = base_url.rstrip("/")
        self._http = AuthorizedHttp(client or httpx.Client(timeout=httpx.Timeout(30.0)), tokens, "gmail",
                                    classify=_classify)
        self._clock = clock

    # ----------------------------------------------------------------- sync

    def sync(self, state: ConnectorState, sink: MailSink, *, now: datetime | None = None) -> SyncOutcome:
        """Deliver new messages to ``sink``; always returns the state to persist."""
        now = now or self._clock()
        counted = CountingSink(sink)
        try:
            if state.cursor is None:
                history_id, window_start = self._full_sync(counted, now)
                state = gap_after_cursor_loss(state, window_start)  # a lost cursor after a long outage
                new_state = record_success(state, at=now, cursor=history_id, coverage_start=window_start)
                return SyncOutcome(new_state, counted.count, full_sync=True)
            try:
                cursor = self._incremental(state.cursor, counted)
                return SyncOutcome(record_success(state, at=now, cursor=cursor), counted.count)
            except CursorExpired:
                history_id, window_start = self._full_sync(counted, now)
                state = gap_after_cursor_loss(state, window_start)
                return SyncOutcome(record_success(state, at=now, cursor=history_id, coverage_start=window_start),
                                   counted.count, full_sync=True)
        except ConnectorError as exc:
            return SyncOutcome(record_failure(state, at=now, error=exc), counted.count, error=exc)

    def backfill(self, state: ConnectorState, gap: TimeRange, sink: MailSink, *,
                 now: datetime | None = None) -> SyncOutcome:
        """Re-read one period (known gap or missed webhooks) without touching the cursor."""
        now = now or self._clock()
        counted = CountingSink(sink)
        query = f"after:{int(gap.start.timestamp())} before:{int(gap.end.timestamp()) + 1} -in:drafts -in:chats"
        try:
            for message_id in self._list_ids(query):
                self._deliver(message_id, counted)
        except ConnectorError as exc:
            return SyncOutcome(record_failure(state, at=now, error=exc), counted.count, error=exc)
        return SyncOutcome(record_backfill(state, gap), counted.count)

    def _full_sync(self, sink: MailSink, now: datetime) -> tuple[str, datetime]:
        profile = self._http.get_json(f"{self.base_url}/profile")
        history_id = str(profile.get("historyId") or "")
        if not history_id:
            raise ProviderError("gmail_profile_without_history_id")
        window_start = now - self.config.history_window
        query = f"after:{int(window_start.timestamp())} -in:drafts -in:chats"
        for message_id in self._list_ids(query):
            self._deliver(message_id, sink)
        return history_id, window_start

    def _list_ids(self, query: str) -> Iterator[str]:
        params: dict[str, Any] = {"q": query, "maxResults": self.config.page_size,
                                  "includeSpamTrash": str(self.config.include_spam_trash).lower()}
        for page in self._pages(f"{self.base_url}/messages", params):
            for message in object_list(page, "messages", "gmail"):
                yield required_str(message, "id", "gmail")

    def _incremental(self, start_history_id: str, sink: MailSink) -> str:
        params: dict[str, Any] = {"startHistoryId": start_history_id, "historyTypes": "messageAdded",
                                  "maxResults": self.config.page_size}
        latest = start_history_id
        new_ids: dict[str, None] = {}
        for page in self._pages(f"{self.base_url}/history", params, expired_on_404=True):
            latest = str(page.get("historyId") or latest)
            for record in object_list(page, "history", "gmail"):
                for added in object_list(record, "messagesAdded", "gmail"):
                    message = added.get("message")
                    if not isinstance(message, dict):
                        raise ProviderError("gmail_unexpected_message")
                    labels = message.get("labelIds") or []
                    if not isinstance(labels, list):
                        raise ProviderError("gmail_unexpected_labelIds")
                    if self._wanted(labels):
                        new_ids.setdefault(required_str(message, "id", "gmail"), None)
        for message_id in new_ids:  # cursor moves only after every message is delivered
            self._deliver(message_id, sink)
        return latest

    def _wanted(self, labels: Any) -> bool:
        labels = set(labels)
        if labels & _SKIP_LABELS:
            return False
        return self.config.include_spam_trash or not labels & {"SPAM", "TRASH"}

    def _pages(self, url: str, params: dict[str, Any], *, expired_on_404: bool = False) -> Iterator[Mapping[str, Any]]:
        token: str | None = None
        for _ in range(self.config.max_pages):
            query = {**params, **({"pageToken": token} if token else {})}
            response = self._http.request("GET", url, params=query, allow=(404,) if expired_on_404 else ())
            if response.status_code == 404:
                raise CursorExpired("gmail_history_expired")
            page = json_object(response, "gmail")
            yield page
            next_token = page.get("nextPageToken")
            if not next_token:
                return
            if not isinstance(next_token, str) or next_token == token:
                raise ProviderError("gmail_repeated_page_token")
            token = next_token
        raise ProviderError("gmail_too_many_pages")

    # ----------------------------------------------------------------- messages

    def fetch_raw(self, message_id: str) -> MailItem | None:
        """``messages.get?format=raw``; ``None`` when the message was deleted meanwhile."""
        response = self._http.request("GET", f"{self.base_url}/messages/{message_id}",
                                      params={"format": "raw"}, allow=(404,))
        if response.status_code == 404:
            return None
        data = json_object(response, "gmail")
        raw = data.get("raw")
        if not raw:
            raise ProviderError("gmail_message_without_raw")
        received = _epoch_ms(data["internalDate"], "gmail_bad_internal_date") if data.get("internalDate") else None
        labels = data.get("labelIds") or []
        thread = data.get("threadId")
        return MailItem(str(data.get("id") or message_id), _decode_raw(raw), received,
                        thread_id=str(thread) if thread else None,
                        labels=tuple(str(label) for label in labels) if isinstance(labels, list) else ())

    def _deliver(self, message_id: str, sink: MailSink) -> None:
        item = self.fetch_raw(message_id)
        if item is not None:
            sink(item)

    # ----------------------------------------------------------------- push

    def start_watch(self, state: ConnectorState, topic_name: str) -> ConnectorState:
        """``users.watch``: Gmail pushes to Pub/Sub until ``expiration`` (renew daily)."""
        payload = json_object(self._http.request("POST", f"{self.base_url}/watch",
                                                 json_payload={"topicName": topic_name}), "gmail")
        expires = _epoch_ms(payload["expiration"], "gmail_bad_expiration") if payload.get("expiration") else None
        return record_webhook(state, webhook_state=WebhookState.ACTIVE, expires_at=expires)

    def stop_watch(self, state: ConnectorState) -> ConnectorState:
        self._http.request("POST", f"{self.base_url}/stop")
        return record_webhook(state, webhook_state=WebhookState.NOT_USED)

    @staticmethod
    def parse_push(envelope: Mapping[str, Any]) -> GmailPush:
        """Decode a Pub/Sub push body: ``message.data`` is base64 JSON."""
        message = envelope.get("message") or {}
        try:
            decoded = json.loads(base64.b64decode(message.get("data") or ""))
            published = message.get("publishTime")
            when = datetime.fromisoformat(published.replace("Z", "+00:00")) if published else None
            return GmailPush(str(decoded["emailAddress"]), str(decoded["historyId"]), when)
        except (ValueError, KeyError, TypeError, binascii.Error):
            raise ProviderError("gmail_bad_push") from None

    @staticmethod
    def record_push(state: ConnectorState, push: GmailPush, *, now: datetime) -> ConnectorState:
        """Mark the event; the next sync (or backfill) fetches what it announced."""
        if push.email_address.lower() != state.account.lower():
            return state  # not this mailbox
        return record_event(state, at=push.published_at or now)

