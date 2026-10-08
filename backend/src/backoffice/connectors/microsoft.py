"""Outlook / Microsoft 365 mailbox connector (§8, §47) over Microsoft Graph v1.0.

* Messages: delta query per mail folder (Graph has no mailbox-wide message
  delta). The first round is limited to the history window with
  ``$filter=receivedDateTime ge ...``; the returned ``@odata.deltaLink`` per
  folder is the cursor. A ``410 Gone`` delta means the sync state was dropped:
  that folder starts again and the hole, if any, becomes a known gap.
* Each new message is fetched as MIME (``/messages/{id}/$value``) so the
  original .eml is the evidence; ``list_attachments`` gives file, item and
  reference attachments individually when needed.
* Webhooks: ``/subscriptions`` with a ``clientState`` secret that every
  notification must echo (checked in constant time).

Follow-up links are only followed on the Graph origin, so the bearer token is
never sent anywhere else.

**Shared mailboxes** (checklist O2): with ``GraphMailConfig.mailbox`` set to a
Microsoft 365 shared mailbox's address (or a mailbox the signed-in user was
given full access to), every mailbox call goes to ``/users/{address}/...``
instead of ``/me/...``, with the signed-in user's delegated access. The sign-in
then needs ``Mail.Read.Shared`` besides ``Mail.Read``
(:data:`GRAPH_SHARED_MAIL_SCOPES`); Graph answers 403 or 404 when the user has
no access to that mailbox, which is the owner's to fix (sign in again with an
account that has access).
"""

from __future__ import annotations

import base64
import binascii
import hmac
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
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
    WebhookState,
    gap_after_cursor_loss,
    record_backfill,
    record_event,
    record_failure,
    record_success,
    record_webhook,
)
from .http import AuthorizedHttp, json_object, object_list, required_str, same_origin
from .mail_search import MailQuery, graph_search
from .oauth import TokenProvider

__all__ = [
    "GRAPH_API",
    "GRAPH_MAIL_SCOPES",
    "GRAPH_SHARED_MAIL_SCOPES",
    "GraphAttachment",
    "GraphMailConfig",
    "GraphSubscription",
    "MicrosoftMailConnector",
]

GRAPH_API = "https://graph.microsoft.com/v1.0"
GRAPH_MAIL_SCOPES = ("offline_access", "https://graph.microsoft.com/Mail.Read")
# Reading a shared mailbox (or one the user has full access to) with the signed-in user's delegated access.
GRAPH_SHARED_MAIL_SCOPE = "https://graph.microsoft.com/Mail.Read.Shared"
GRAPH_SHARED_MAIL_SCOPES = (*GRAPH_MAIL_SCOPES, GRAPH_SHARED_MAIL_SCOPE)
_SELECT = "id,receivedDateTime,isDraft,conversationId,internetMessageId"
_CURSOR_VERSION = 1


@dataclass(frozen=True)
class GraphMailConfig:
    folders: tuple[str, ...] = ("inbox", "archive")  # well-known names or folder ids
    include_child_folders: bool = True  # "Inbox/Faturas" is read too (§8)
    max_folder_depth: int = 5
    history_window: timedelta = timedelta(days=90)
    page_size: int = 50
    max_pages: int = 10_000
    # A shared mailbox's address (checklist O2): read through /users/{address} with the signed-in user's
    # delegated access instead of /me. None: the signed-in user's own mailbox.
    mailbox: str | None = None


@dataclass(frozen=True)
class GraphAttachment:
    attachment_id: str
    kind: str  # "file", "item" (attached email/event) or "reference" (cloud link)
    name: str | None
    content_type: str | None
    size: int | None
    is_inline: bool
    content_id: str | None
    data: bytes | None = field(default=None, repr=False)  # MIME for "item"; None for "reference"


@dataclass(frozen=True)
class GraphSubscription:
    subscription_id: str
    expires_at: datetime


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _notes(payload: Any) -> list[Mapping[str, Any]]:
    """Notification entries of a webhook body; anything malformed is ignored (the body is untrusted)."""
    values = payload.get("value") if isinstance(payload, Mapping) else None
    return [note for note in values if isinstance(note, Mapping)] if isinstance(values, list) else []


def _load_cursor(raw: str | None) -> dict[str, str]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}  # unreadable cursor: treated as lost, full sync follows
    folders = data.get("folders") if isinstance(data, dict) and data.get("v") == _CURSOR_VERSION else None
    return {str(k): str(v) for k, v in (folders or {}).items()}


def _dump_cursor(links: Mapping[str, str]) -> str:
    return json.dumps({"v": _CURSOR_VERSION, "folders": dict(sorted(links.items()))}, sort_keys=True)


class MicrosoftMailConnector:
    kind = ConnectorKind.MICROSOFT

    def __init__(
        self,
        tokens: TokenProvider,
        *,
        client: httpx.Client | None = None,
        config: GraphMailConfig | None = None,
        base_url: str = GRAPH_API,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.config = config or GraphMailConfig()
        self.base_url = base_url.rstrip("/")
        self._http = AuthorizedHttp(client or httpx.Client(timeout=httpx.Timeout(30.0)), tokens, "graph")
        self._clock = clock
        mailbox = (self.config.mailbox or "").strip()
        # Whose mailbox every call reads: the signed-in user's, or a shared one through their delegated access.
        self.user_path = f"users/{quote(mailbox, safe='@')}" if mailbox else "me"
        self.root = f"{self.base_url}/{self.user_path}"

    # ----------------------------------------------------------------- sync

    def sync(self, state: ConnectorState, sink: MailSink, *, now: datetime | None = None) -> SyncOutcome:
        now = now or self._clock()
        counted = CountingSink(sink)
        window_start = now - self.config.history_window
        links = _load_cursor(state.cursor)
        full = not links
        if full:  # no usable cursor: after a long outage the window leaves a hole
            state = gap_after_cursor_loss(state, window_start)
        try:
            new_links: dict[str, str] = {}
            for folder_id in self._folder_ids():
                link = links.get(folder_id)
                try:
                    delta = self._delta(folder_id, link, counted, window_start)
                except CursorExpired:
                    state = gap_after_cursor_loss(state, window_start)
                    delta = self._delta(folder_id, None, counted, window_start)
                    full = True
                if delta is not None:
                    new_links[folder_id] = delta
        except ConnectorError as exc:
            return SyncOutcome(record_failure(state, at=now, error=exc), counted.count, error=exc)
        new_state = record_success(state, at=now, cursor=_dump_cursor(new_links),
                                   coverage_start=window_start if full else None)
        return SyncOutcome(new_state, counted.count, full_sync=full)

    def backfill(self, state: ConnectorState, gap: TimeRange, sink: MailSink, *,
                 now: datetime | None = None) -> SyncOutcome:
        """Re-read one period across the mailbox without touching delta cursors."""
        now = now or self._clock()
        counted = CountingSink(sink)
        flt = (f"receivedDateTime ge {gap.start.astimezone(timezone.utc):%Y-%m-%dT%H:%M:%SZ} and "
               f"receivedDateTime le {gap.end.astimezone(timezone.utc):%Y-%m-%dT%H:%M:%SZ}")
        url = f"{self.root}/messages"
        params: dict[str, Any] | None = {"$select": _SELECT, "$filter": flt, "$top": self.config.page_size}
        try:
            for _ in range(self.config.max_pages):
                page = self._http.get_json(url, params=params)
                self._deliver_all(object_list(page, "value", "graph"), None, counted)
                url = self._follow(page.get("@odata.nextLink"))
                params = None
                if not url:
                    break
        except ConnectorError as exc:
            return SyncOutcome(record_failure(state, at=now, error=exc), counted.count, error=exc)
        return SyncOutcome(record_backfill(state, gap), counted.count)

    def _delta(self, folder_id: str, link: str | None, sink: MailSink, window_start: datetime) -> str | None:
        """Walk one folder's delta; returns the new deltaLink, ``None`` if the folder is gone."""
        if link is not None and not same_origin(link, self.base_url):
            raise CursorExpired("graph_foreign_cursor")  # never send the token elsewhere
        url = link or f"{self.root}/mailFolders/{folder_id}/messages/delta"
        params: dict[str, Any] | None = None
        if link is None:
            params = {"$select": _SELECT,
                      "$filter": f"receivedDateTime ge {window_start.astimezone(timezone.utc):%Y-%m-%dT%H:%M:%SZ}"}
        headers = {"Prefer": f"odata.maxpagesize={self.config.page_size}"}
        for _ in range(self.config.max_pages):
            response = self._http.request("GET", url, params=params, headers=headers, allow=(404, 410))
            if response.status_code == 410:
                raise CursorExpired("graph_delta_sync_state_lost")
            if response.status_code == 404:
                return None
            page = json_object(response, "graph")
            self._deliver_all(object_list(page, "value", "graph"), folder_id, sink)
            params = None
            if page.get("@odata.nextLink"):
                url = self._follow(page["@odata.nextLink"])
                continue
            if page.get("@odata.deltaLink"):
                return self._follow(page["@odata.deltaLink"])
            raise ProviderError("graph_delta_without_link")
        raise ProviderError("graph_too_many_pages")

    def _follow(self, link: Any) -> str:
        if not link:
            return ""
        if not same_origin(str(link), self.base_url):
            raise ProviderError("graph_foreign_link")
        return str(link)

    def _deliver_all(self, items: list[dict[str, Any]], folder_id: str | None, sink: MailSink) -> None:
        for item in items:
            if "@removed" in item or item.get("isDraft"):
                continue
            message_id = required_str(item, "id", "graph")
            raw = self.fetch_mime(message_id)
            if raw is not None:
                thread = item.get("conversationId")
                sink(MailItem(message_id, raw, _parse_time(item.get("receivedDateTime")),
                              thread_id=str(thread) if thread else None, folder=folder_id))

    # ----------------------------------------------------------------- folders

    def _folder_ids(self) -> list[str]:
        ids: dict[str, None] = {}
        for name in self.config.folders:
            response = self._http.request("GET", f"{self.root}/mailFolders/{name}",
                                          params={"$select": "id,childFolderCount"}, allow=(404,))
            if response.status_code == 404:
                continue
            folder = json_object(response, "graph")
            folder_id = required_str(folder, "id", "graph")
            ids.setdefault(folder_id, None)
            if self.config.include_child_folders and folder.get("childFolderCount"):
                for child in self._children(folder_id, 1):
                    ids.setdefault(child, None)
        if not ids and self.config.mailbox:
            # Not one folder of the shared mailbox is visible: the signed-in account has no access to it. Reading
            # nothing is never "synced": the owner signs in with an account that has access.
            raise ReconnectRequired("graph_shared_mailbox_not_accessible")
        return list(ids)

    def _children(self, folder_id: str, depth: int) -> list[str]:
        if depth > self.config.max_folder_depth:
            return []
        found: list[str] = []
        url = f"{self.root}/mailFolders/{folder_id}/childFolders"
        params: dict[str, Any] | None = {"$select": "id,childFolderCount", "$top": 100}
        for _ in range(self.config.max_pages):
            page = self._http.get_json(url, params=params)
            for child in object_list(page, "value", "graph"):
                child_id = required_str(child, "id", "graph")
                found.append(child_id)
                if child.get("childFolderCount"):
                    found += self._children(child_id, depth + 1)
            url, params = self._follow(page.get("@odata.nextLink")), None
            if not url:
                break
        return found

    # ----------------------------------------------------------------- messages

    def fetch_mime(self, message_id: str) -> bytes | None:
        response = self._http.request("GET", f"{self.root}/messages/{message_id}/$value", allow=(404,))
        return None if response.status_code == 404 else response.content

    def thread_messages(self, conversation_id: str) -> list[MailItem]:
        """Every message of one conversation, each as MIME, oldest first (drafts skipped).

        A reply may point at an invoice sent earlier in the same conversation, even before the history
        window (§8 "previous attachments").
        """
        escaped = conversation_id.replace("'", "''")
        url = f"{self.root}/messages"
        params: dict[str, Any] | None = {"$select": _SELECT, "$filter": f"conversationId eq '{escaped}'",
                                         "$top": self.config.page_size}
        found: list[dict[str, Any]] = []
        for _ in range(self.config.max_pages):
            page = self._http.get_json(url, params=params)
            found += object_list(page, "value", "graph")
            url, params = self._follow(page.get("@odata.nextLink")), None
            if not url:
                break
        items: list[MailItem] = []
        self._deliver_all(sorted(found, key=lambda m: str(m.get("receivedDateTime") or "")), None, items.append)
        return items

    def search_messages(self, query: MailQuery) -> list[MailItem]:
        """Messages matching ``query`` in every folder of the mailbox, the archive included (Graph ``$search`` on
        ``/messages``, KQL), each as MIME (§22: current and historical email). ``$search`` cannot be combined
        with ``$filter`` or ``$orderby``, so the date window is part of the KQL; drafts are skipped."""
        params: dict[str, Any] = {"$search": graph_search(query), "$select": _SELECT, "$top": query.limit}
        page = self._http.get_json(f"{self.root}/messages", params=params)
        found = object_list(page, "value", "graph")[: query.limit]
        items: list[MailItem] = []
        self._deliver_all(found, None, items.append)
        return items

    def list_attachments(self, message_id: str) -> list[GraphAttachment]:
        url = f"{self.root}/messages/{message_id}/attachments"
        found: list[GraphAttachment] = []
        for _ in range(self.config.max_pages):
            page = self._http.get_json(url)
            found += [self._attachment(message_id, item) for item in object_list(page, "value", "graph")]
            url = self._follow(page.get("@odata.nextLink"))
            if not url:
                break
        return found

    def _attachment(self, message_id: str, item: Mapping[str, Any]) -> GraphAttachment:
        attachment_id = required_str(item, "id", "graph")
        odata = str(item.get("@odata.type", ""))
        kind = "file" if odata.endswith("fileAttachment") else "item" if odata.endswith("itemAttachment") else "reference"
        data: bytes | None = None
        if kind == "file" and item.get("contentBytes"):
            try:
                data = base64.b64decode(str(item["contentBytes"]), validate=True)
            except (binascii.Error, ValueError):
                raise ProviderError("graph_bad_attachment_bytes") from None
        elif kind in ("file", "item"):  # large files and attached items: raw download
            data = self._http.request("GET", f"{self.root}/messages/{message_id}/attachments/"
                                              f"{attachment_id}/$value").content
        size = item.get("size")
        return GraphAttachment(attachment_id, kind, item.get("name"), item.get("contentType"),
                               int(size) if isinstance(size, (int, Decimal)) and not isinstance(size, bool) else None,
                               bool(item.get("isInline")), item.get("contentId"), data)

    # ----------------------------------------------------------------- webhooks

    def create_subscription(
        self,
        state: ConnectorState,
        *,
        notification_url: str,
        client_state: str,
        lifetime: timedelta = timedelta(days=2),
        resource: str | None = None,
        lifecycle_url: str | None = None,
        now: datetime | None = None,
    ) -> tuple[ConnectorState, GraphSubscription]:
        """Subscribe to new messages; renew before ``expires_at`` (lifetime is capped by Graph)."""
        now = now or self._clock()
        body: dict[str, Any] = {
            "changeType": "created",
            "notificationUrl": notification_url,
            "resource": resource or f"{self.user_path}/mailFolders('inbox')/messages",
            "expirationDateTime": (now + lifetime).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "clientState": client_state,
        }
        if lifecycle_url:
            body["lifecycleNotificationUrl"] = lifecycle_url
        payload = json_object(self._http.request("POST", f"{self.base_url}/subscriptions", json_payload=body), "graph")
        subscription = GraphSubscription(required_str(payload, "id", "graph"),
                                         _parse_time(payload.get("expirationDateTime")) or now + lifetime)
        return record_webhook(state, webhook_state=WebhookState.ACTIVE, expires_at=subscription.expires_at), subscription

    def renew_subscription(self, state: ConnectorState, subscription_id: str, *, lifetime: timedelta = timedelta(days=2),
                           now: datetime | None = None) -> ConnectorState:
        now = now or self._clock()
        expires = now + lifetime
        body = {"expirationDateTime": expires.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
        payload = json_object(self._http.request("PATCH", f"{self.base_url}/subscriptions/{subscription_id}",
                                                 json_payload=body), "graph")
        return record_webhook(state, webhook_state=WebhookState.ACTIVE,
                              expires_at=_parse_time(payload.get("expirationDateTime")) or expires)

    @staticmethod
    def accept_notifications(state: ConnectorState, payload: Mapping[str, Any], client_state: str, *,
                             now: datetime) -> tuple[ConnectorState, int]:
        """Record genuine change notifications; forged ones (wrong clientState) are ignored."""
        accepted = 0
        for note in _notes(payload):
            given = str(note.get("clientState") or "")
            if hmac.compare_digest(given.encode(), client_state.encode()):
                accepted += 1
        if accepted:
            state = record_event(state, at=now)
        return state, accepted

    @staticmethod
    def accept_lifecycle(state: ConnectorState, payload: Mapping[str, Any], client_state: str) -> ConnectorState:
        """``missed`` → backfill; ``subscriptionRemoved`` → re-subscribe (§47 self-healing)."""
        for note in _notes(payload):
            if not hmac.compare_digest(str(note.get("clientState") or "").encode(), client_state.encode()):
                continue
            event = note.get("lifecycleEvent")
            if event == "missed":
                state = record_webhook(state, webhook_state=WebhookState.FAILED, expires_at=state.webhook_expires_at)
            elif event == "subscriptionRemoved":
                state = record_webhook(state, webhook_state=WebhookState.EXPIRED)
        return state

