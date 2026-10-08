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
Drafts and chats are skipped. Spam is read and searched only when the owner allows it
(``GmailConfig.include_spam``, checklist B8: ``includeSpamTrash`` with ``-in:trash``
in the query, so the trash never is); ``include_spam_trash`` reads both.

**Delegated and alias addresses** (checklist O2), configured per mailbox connection:

* ``GmailConfig.user_id``: another mailbox the signed-in account may read, as the
  Gmail API's ``userId`` (``/users/{address}``). Google allows it only where the
  account really has that access (its own address, or a Google Workspace set-up
  that grants it); otherwise Gmail answers "Delegation denied", which is the
  owner's to fix (sign in to that mailbox itself), never an empty sync.
* ``GmailConfig.delivered_to``: an alias of the signed-in mailbox: only the mail
  delivered to that address is read (``deliveredto:`` on listings, and the
  message's own delivery headers for new mail).
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Callable, Iterator, Mapping
from email.parser import BytesHeaderParser
from email.utils import getaddresses
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

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
from .http import AuthorizedHttp, json_object, object_list, required_str, retry_after_seconds
from .mail_search import MailQuery, gmail_query
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
    # Spam too, never the trash: the owner's "Also look in spam for invoices" (checklist B8).
    include_spam: bool = False
    max_pages: int = 10_000
    # A delegated mailbox read as the API's userId (/users/{address}) where Google allows it; None: "me".
    user_id: str | None = None
    # An alias of the signed-in mailbox: only mail delivered to it is read. None: everything.
    delivered_to: str | None = None


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
        if status == 403 and b"elegation denied" in (response.content or b""):
            # Reading another mailbox this account may not read (checklist O2): the owner signs in to it itself.
            return ReconnectRequired("gmail_delegation_denied")
        if reasons & _TRANSIENT_REASONS or status == 429:
            return TransientError(f"gmail_rate_limited_{status}", retry_after=retry_after_seconds(response))
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
        base_url = base_url.rstrip("/")
        user = (self.config.user_id or "").strip()
        if user and base_url.endswith("/users/me"):  # a delegated mailbox (checklist O2)
            base_url = f"{base_url[:-len('me')]}{quote(user, safe='@')}"
        self.base_url = base_url
        alias = (self.config.delivered_to or "").strip().lower()
        self._alias = alias or None
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
        query = self._scoped(f"after:{int(gap.start.timestamp())} before:{int(gap.end.timestamp()) + 1} "
                             "-in:drafts -in:chats")
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
        query = self._scoped(f"after:{int(window_start.timestamp())} -in:drafts -in:chats")
        for message_id in self._list_ids(query):
            self._deliver(message_id, sink)
        return history_id, window_start

    def _scoped(self, query: str) -> str:
        """The search, limited to mail delivered to the alias when the connection reads one (O2), and never the
        trash when spam is allowed (B8: listing with spam lists the trash too unless the query leaves it out)."""
        if self._spam_only:
            query = f"{query} -in:trash"
        return f"{query} deliveredto:{self._alias}" if self._alias else query

    @property
    def _spam_only(self) -> bool:
        return self.config.include_spam and not self.config.include_spam_trash

    def _include_spam_trash(self) -> str:
        """The ``includeSpamTrash`` listing parameter: true when spam is allowed (the query then leaves the trash
        out, :meth:`_scoped`)."""
        return str(self.config.include_spam_trash or self.config.include_spam).lower()

    def _delivered_to_alias(self, raw: bytes) -> bool:
        """True when the message was delivered or addressed to the alias (its own delivery headers)."""
        if self._alias is None:
            return True
        try:
            headers = BytesHeaderParser().parsebytes(raw)
        except Exception:  # an unreadable header block: not proven to be the alias's mail
            return False
        values = [str(v) for name in ("Delivered-To", "X-Original-To", "Envelope-To", "X-Forwarded-To", "To", "Cc")
                  for v in (headers.get_all(name) or [])]
        return any(address.strip().lower() == self._alias for _, address in getaddresses(values))

    def _list_ids(self, query: str) -> Iterator[str]:
        params: dict[str, Any] = {"q": query, "maxResults": self.config.page_size,
                                  "includeSpamTrash": self._include_spam_trash()}
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
        if self.config.include_spam_trash:
            return True
        if "TRASH" in labels:
            return False  # never the trash
        return self.config.include_spam or "SPAM" not in labels

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
        if item is not None and self._delivered_to_alias(item.raw):
            sink(item)

    def search_messages(self, query: MailQuery) -> list[MailItem]:
        """Messages matching ``query`` anywhere in the mailbox (every label, archived mail included), newest
        first, each as ``format=raw`` (§22: current and historical email). Drafts and chats are never searched;
        spam only when the owner allowed it (B8), the trash never. An alias connection searches only the alias's
        mail."""
        params: dict[str, Any] = {"q": self._scoped(gmail_query(query)), "maxResults": query.limit,
                                  "includeSpamTrash": self._include_spam_trash()}
        page = self._http.get_json(f"{self.base_url}/messages", params=params)
        items: list[MailItem] = []
        for message in object_list(page, "messages", "gmail")[: query.limit]:
            item = self.fetch_raw(required_str(message, "id", "gmail"))
            if item is not None and self._wanted(item.labels) and self._delivered_to_alias(item.raw):
                items.append(item)
        return items

    def thread_messages(self, thread_id: str) -> list[MailItem]:
        """Every message of one thread (``threads.get``), oldest first, each as ``format=raw``.

        A reply may point at an invoice sent earlier in the same thread, even before the history
        window (§8 "previous attachments"). Drafts and chats are skipped; spam unless allowed, the trash always.
        """
        response = self._http.request("GET", f"{self.base_url}/threads/{quote(thread_id, safe='')}",
                                      params={"format": "minimal"}, allow=(404,))
        if response.status_code == 404:
            return []
        items: list[MailItem] = []
        for message in object_list(json_object(response, "gmail"), "messages", "gmail"):
            labels = message.get("labelIds") or []
            if isinstance(labels, list) and not self._wanted(labels):
                continue
            item = self.fetch_raw(required_str(message, "id", "gmail"))
            if item is not None:
                items.append(item)
        return items

    # ----------------------------------------------------------------- push

    def start_watch(self, state: ConnectorState, topic_name: str) -> ConnectorState:
        """``users.watch``: Gmail pushes to Pub/Sub until ``expiration`` (renew daily)."""
        payload = json_object(self._http.request("POST", f"{self.base_url}/watch",
                                                 json_payload={"topicName": topic_name}), "gmail")
        expires = _epoch_ms(payload["expiration"], "gmail_bad_expiration") if payload.get("expiration") else None
        return record_webhook(state, webhook_state=WebhookState.ACTIVE, expires_at=expires)

    def mailbox_address(self) -> str:
        """The watched mailbox's own address (``users.getProfile``): what Gmail's push notifications name."""
        profile = self._http.get_json(f"{self.base_url}/profile")
        address = str(profile.get("emailAddress") or "").strip().lower()
        if not address:
            raise ProviderError("gmail_profile_without_address")
        return address

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

