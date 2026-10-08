"""Generic IMAP mailbox connector (§4 step 3, §8, §47) with stdlib ``imaplib``.

Cursor per mailbox: ``(UIDVALIDITY, last UID)`` (RFC 3501 §2.3.1.1). A new
UIDVALIDITY means the server renumbered the mailbox: that mailbox is read
again for the history window (duplicates are harmless, evidence dedupes by
hash) and any hole older than the window becomes a known gap.

Mailboxes are opened read-only (``EXAMINE``) and bodies fetched with
``BODY.PEEK[]``: the owner's messages are never marked as read.
Auth is a password (app password) or XOAUTH2; an auth refusal means reconnect.

The Junk or Spam folder is read and searched only when the owner allows it
(``IMAPConfig.include_junk``, checklist B8): the folder the server marks
``\\Junk`` (RFC 6154), else one with a usual name ("Junk", "Spam", ...). A
folder marked ``\\Trash`` never is.
"""

from __future__ import annotations

import base64
import imaplib
import json
import re
import ssl
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Protocol

from backoffice.domain.models import utcnow

from .base import (
    ConnectorError,
    ConnectorKind,
    ConnectorState,
    CountingSink,
    MailItem,
    MailSink,
    ProviderError,
    ReconnectRequired,
    SyncOutcome,
    TimeRange,
    TransientError,
    gap_after_cursor_loss,
    record_backfill,
    record_failure,
    record_success,
)
from .mail_search import MailQuery, imap_criteria

__all__ = ["IMAPAuth", "IMAPClient", "IMAPConfig", "IMAPConnector", "decode_mailbox_name", "encode_mailbox_name",
           "imap_date"]

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_UID = re.compile(rb"\bUID (\d+)")
_INTERNALDATE = re.compile(rb'INTERNALDATE "([^"]+)"')
_CURSOR_VERSION = 1


class IMAPClient(Protocol):
    """The subset of :class:`imaplib.IMAP4` this connector uses."""

    def login(self, user: str, password: str) -> tuple[str, list[Any]]: ...

    def authenticate(self, mechanism: str, authobject: Callable[[bytes], bytes]) -> tuple[str, list[Any]]: ...

    def select(self, mailbox: str = "INBOX", readonly: bool = False) -> tuple[str, list[Any]]: ...

    def response(self, code: str) -> tuple[str, list[Any]]: ...

    def uid(self, command: str, *args: str) -> tuple[str, list[Any]]: ...

    def logout(self) -> tuple[str, list[Any]]: ...


@dataclass(frozen=True)
class IMAPAuth:
    username: str
    password: str | None = field(default=None, repr=False)  # app password
    oauth_token: str | None = field(default=None, repr=False)  # XOAUTH2 access token


@dataclass(frozen=True)
class IMAPConfig:
    host: str
    port: int = 993
    mailboxes: tuple[str, ...] = ("INBOX",)
    history_window: timedelta = timedelta(days=90)
    timeout_s: float = 30.0
    batch_size: int = 50
    # The Junk or Spam folder too, if the mailbox has one (never the trash): the owner's "Also look in spam for
    # invoices" (checklist B8).
    include_junk: bool = False
    junk_names: tuple[str, ...] = ("Junk", "Spam", "Junk E-mail", "Junk Email", "Bulk Mail", "INBOX.Junk",
                                   "INBOX.Spam", "[Gmail]/Spam")


def imap_date(day: date) -> str:
    """RFC 3501 date (``29-Jun-2026``), independent of the process locale."""
    return f"{day.day}-{_MONTHS[day.month - 1]}-{day.year}"


def _parse_internaldate(raw: bytes | None) -> datetime | None:
    """``17-Jul-1996 02:44:25 -0700`` without locale-dependent ``strptime``."""
    if not raw:
        return None
    m = re.fullmatch(rb"\s*(\d{1,2})-([A-Za-z]{3})-(\d{4}) (\d{2}):(\d{2}):(\d{2}) ([+-])(\d{2})(\d{2})", raw)
    if not m:
        return None
    try:
        month = _MONTHS.index(m.group(2).decode().title()) + 1
        offset = timedelta(hours=int(m.group(8)), minutes=int(m.group(9)))
        tz = timezone(offset if m.group(7) == b"+" else -offset)
        return datetime(int(m.group(3)), month, int(m.group(1)), int(m.group(4)), int(m.group(5)),
                        int(m.group(6)), tzinfo=tz)
    except ValueError:
        return None


def encode_mailbox_name(name: str) -> str:
    """Quoted IMAP mailbox name; non-ASCII in modified UTF-7 (RFC 3501 §5.1.3)."""
    out, buffer = [], []

    def flush() -> None:
        if buffer:
            chunk = base64.b64encode("".join(buffer).encode("utf-16-be")).decode().rstrip("=")
            out.append("&" + chunk.replace("/", ",") + "-")
            buffer.clear()

    for ch in name:
        if 0x20 <= ord(ch) <= 0x7E:
            flush()
            out.append("&-" if ch == "&" else ch)
        else:
            buffer.append(ch)
    flush()
    encoded = "".join(out)
    return '"' + encoded.replace("\\", "\\\\").replace('"', '\\"') + '"'


def decode_mailbox_name(raw: str) -> str:
    """A mailbox name as the server lists it (modified UTF-7, RFC 3501 §5.1.3), in plain text."""
    out, i = [], 0
    while i < len(raw):
        ch = raw[i]
        end = raw.find("-", i + 1) if ch == "&" else -1
        if ch != "&" or end < 0:
            out.append(ch)
            i += 1
            continue
        chunk = raw[i + 1:end]
        if not chunk:
            out.append("&")
        else:
            try:
                padded = chunk.replace(",", "/") + "=" * (-len(chunk) % 4)
                out.append(base64.b64decode(padded).decode("utf-16-be"))
            except (ValueError, UnicodeDecodeError):
                out.append(raw[i:end + 1])
        i = end + 1
    return "".join(out)


_LIST_LINE = re.compile(rb'^\((?P<flags>[^)]*)\)\s+(?:"(?:[^"\\]|\\.)*"|NIL)\s+(?P<name>.+?)\s*$')


def _listed(data: Sequence[Any]) -> list[tuple[frozenset[str], str]]:
    """``(flags, name)`` of each mailbox in a LIST answer; unreadable lines are left out."""
    found: list[tuple[frozenset[str], str]] = []
    for item in data:
        line = item if isinstance(item, bytes) else item[0] if isinstance(item, tuple) and item else None
        if not isinstance(line, bytes):
            continue
        m = _LIST_LINE.match(line.strip())
        if not m:
            continue
        name = m.group("name").decode("utf-8", "replace")
        if len(name) >= 2 and name[0] == name[-1] == '"':
            name = name[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        flags = frozenset(f.lower() for f in m.group("flags").decode("ascii", "replace").split())
        found.append((flags, decode_mailbox_name(name)))
    return found


def _parse_fetch(data: Sequence[Any]) -> list[tuple[int, datetime | None, bytes]]:
    """``(uid, internal date, raw)`` per UID, ascending, whatever the item order.

    A server may answer one UID twice (e.g. an unsolicited update); the first
    body wins, and the date is taken from whichever answer carries it.
    """
    found: dict[int, tuple[int, datetime | None, bytes]] = {}
    for i, item in enumerate(data):
        if not (isinstance(item, tuple) and len(item) == 2):
            continue
        trailer = data[i + 1] if i + 1 < len(data) and isinstance(data[i + 1], bytes) else b""
        meta = bytes(item[0]) + b" " + trailer
        uid = _UID.search(meta)
        if not uid:
            continue
        key = int(uid.group(1))
        when = _INTERNALDATE.search(meta)
        received = _parse_internaldate(when.group(1) if when else None)
        previous = found.get(key)
        if previous is None:
            found[key] = (key, received, bytes(item[1]))
        elif previous[1] is None and received is not None:
            found[key] = (key, received, previous[2])
    return [found[uid] for uid in sorted(found)]


def _load_cursor(raw: str | None) -> dict[str, tuple[int, int]]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        boxes = data["mailboxes"] if data.get("v") == _CURSOR_VERSION else {}
        return {str(k): (int(v["uidvalidity"]), int(v["last_uid"])) for k, v in boxes.items()}
    except (ValueError, KeyError, TypeError, AttributeError):
        return {}


def _dump_cursor(boxes: dict[str, tuple[int, int]]) -> str:
    return json.dumps({"v": _CURSOR_VERSION, "mailboxes": {
        k: {"uidvalidity": v, "last_uid": u} for k, (v, u) in sorted(boxes.items())}}, sort_keys=True)


def _default_factory(config: IMAPConfig) -> IMAPClient:
    return imaplib.IMAP4_SSL(config.host, config.port, ssl_context=ssl.create_default_context(),
                             timeout=config.timeout_s)


class IMAPConnector:
    kind = ConnectorKind.IMAP

    def __init__(
        self,
        config: IMAPConfig,
        auth: IMAPAuth | Callable[[], IMAPAuth],
        *,
        client_factory: Callable[[IMAPConfig], IMAPClient] = _default_factory,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.config = config
        self._auth = auth
        self._factory = client_factory
        self._clock = clock

    def sync(self, state: ConnectorState, sink: MailSink, *, now: datetime | None = None) -> SyncOutcome:
        now = now or self._clock()
        counted = CountingSink(sink)
        window_start = now - self.config.history_window
        boxes = _load_cursor(state.cursor)
        full = not boxes
        if full:  # no usable cursor: after a long outage the window leaves a hole
            state = gap_after_cursor_loss(state, window_start)
        try:
            client = self._connect()
            try:
                new_boxes: dict[str, tuple[int, int]] = {}
                for mailbox in self._mailboxes(client):
                    result = self._sync_mailbox(client, mailbox, boxes.get(mailbox), window_start, counted)
                    if result is None:
                        continue
                    (validity, last_uid), reset = result
                    new_boxes[mailbox] = (validity, last_uid)
                    if reset and mailbox in boxes:
                        state = gap_after_cursor_loss(state, window_start)
                    full = full or reset
            finally:
                _logout(client)
        except ConnectorError as exc:
            return SyncOutcome(record_failure(state, at=now, error=exc), counted.count, error=exc)
        new_state = record_success(state, at=now, cursor=_dump_cursor(new_boxes),
                                   coverage_start=window_start if full else None)
        return SyncOutcome(new_state, counted.count, full_sync=full)

    def backfill(self, state: ConnectorState, gap: TimeRange, sink: MailSink, *,
                 now: datetime | None = None) -> SyncOutcome:
        """Re-read ``gap`` (day granularity: IMAP SEARCH has no times)."""
        now = now or self._clock()
        counted = CountingSink(sink)
        criteria = ("SINCE", imap_date(gap.start.date()), "BEFORE", imap_date(gap.end.date() + timedelta(days=1)))
        try:
            client = self._connect()
            try:
                for mailbox in self._mailboxes(client):
                    if self._open(client, mailbox) is None:
                        continue
                    self._fetch(client, mailbox, self._search(client, *criteria), counted)
            finally:
                _logout(client)
        except ConnectorError as exc:
            return SyncOutcome(record_failure(state, at=now, error=exc), counted.count, error=exc)
        return SyncOutcome(record_backfill(state, gap), counted.count)

    def search_messages(self, query: MailQuery) -> list[MailItem]:
        """Messages matching ``query`` in each configured mailbox (``UID SEARCH``, read-only, ``BODY.PEEK``), the
        newest first (§22: current and historical email). The window has day granularity, as IMAP's SEARCH."""
        criteria = imap_criteria(query)
        found: list[MailItem] = []
        client = self._connect()
        try:
            for mailbox in self._mailboxes(client):
                if self._open(client, mailbox) is None:
                    continue
                uids = sorted(self._search(client, *criteria), reverse=True)[: query.limit - len(found)]
                if uids:
                    self._fetch(client, mailbox, uids, found.append)
                if len(found) >= query.limit:
                    break
        finally:
            _logout(client)
        return sorted(found, key=lambda m: m.received_at or datetime.min.replace(tzinfo=timezone.utc),
                      reverse=True)[: query.limit]

    # ----------------------------------------------------------------- session

    def _connect(self) -> IMAPClient:
        auth = self._auth() if callable(self._auth) else self._auth
        try:
            client = self._factory(self.config)
        except (OSError, imaplib.IMAP4.error):
            raise TransientError("imap_connect_failed") from None
        try:
            if auth.oauth_token:
                token = f"user={auth.username}\x01auth=Bearer {auth.oauth_token}\x01\x01".encode()
                client.authenticate("XOAUTH2", lambda _challenge: token)
            elif auth.password is not None:
                client.login(auth.username, auth.password)
            else:
                raise ReconnectRequired("imap_no_credentials")
        except imaplib.IMAP4.abort:
            raise TransientError("imap_connection_dropped") from None
        except imaplib.IMAP4.error:
            raise ReconnectRequired("imap_auth_failed") from None
        except OSError:
            raise TransientError("imap_network") from None
        return client

    def _mailboxes(self, client: IMAPClient) -> list[str]:
        """The mailboxes read: the configured ones, and the Junk or Spam folder when the owner allows it (B8).

        The folder the server marks ``\\Junk`` (RFC 6154 LIST); without one, a listed folder with a usual junk
        name; a server that cannot list is tried with those names (a missing one is skipped). Never ``\\Trash``."""
        boxes = list(self.config.mailboxes)
        if not self.config.include_junk:
            return boxes
        listing = getattr(client, "list", None)
        listed: list[tuple[frozenset[str], str]] | None = None
        if callable(listing):
            typ, data = _call(listing)
            listed = _listed(data) if typ == "OK" else None
        usual = {n.lower() for n in self.config.junk_names}
        if listed is None:
            junk = list(self.config.junk_names)
        else:
            usable = [(flags, name) for flags, name in listed
                      if "\\trash" not in flags and "\\noselect" not in flags]
            junk = [name for flags, name in usable if "\\junk" in flags] or \
                [name for _, name in usable if name.lower() in usual]
        return boxes + [name for name in junk if name not in boxes]

    def _open(self, client: IMAPClient, mailbox: str) -> int | None:
        """EXAMINE the mailbox; its UIDVALIDITY, or ``None`` if it does not exist."""
        typ, _ = _call(client.select, encode_mailbox_name(mailbox), readonly=True)
        if typ != "OK":
            return None
        _, values = _call(client.response, "UIDVALIDITY")
        try:
            return int(values[0])
        except (TypeError, ValueError, IndexError):
            raise ProviderError("imap_no_uidvalidity") from None

    def _sync_mailbox(
        self,
        client: IMAPClient,
        mailbox: str,
        previous: tuple[int, int] | None,
        window_start: datetime,
        sink: MailSink,
    ) -> tuple[tuple[int, int], bool] | None:
        validity = self._open(client, mailbox)
        if validity is None:
            return None
        if previous is not None and previous[0] == validity:
            last = previous[1]
            # "n:*" always matches the highest UID, even below n: filter it out.
            uids = [u for u in self._search(client, "UID", f"{last + 1}:*") if u > last]
            reset = False
        else:
            last = 0
            uids = self._search(client, "SINCE", imap_date(window_start.date()))
            reset = True
        highest = self._fetch(client, mailbox, uids, sink)
        return (validity, max(last, highest)), reset

    def _search(self, client: IMAPClient, *criteria: str) -> list[int]:
        typ, data = _call(client.uid, "SEARCH", *criteria)
        if typ != "OK":
            raise ProviderError("imap_search_failed")
        words = b" ".join(d for d in data if isinstance(d, bytes)).split()
        return sorted({int(w) for w in words if w.isdigit()})

    def _fetch(self, client: IMAPClient, mailbox: str, uids: Iterable[int], sink: MailSink) -> int:
        """Fetch in ascending UID batches; returns the highest UID delivered."""
        ordered = sorted(uids)
        highest = 0
        for i in range(0, len(ordered), self.config.batch_size):
            batch = ordered[i : i + self.config.batch_size]
            typ, data = _call(client.uid, "FETCH", ",".join(map(str, batch)), "(UID INTERNALDATE BODY.PEEK[])")
            if typ != "OK":
                raise ProviderError("imap_fetch_failed")
            for uid, received, raw in _parse_fetch(data):
                sink(MailItem(f"{mailbox}:{uid}", raw, received, folder=mailbox))
                highest = max(highest, uid)
        return highest


def _call(fn: Callable[..., tuple[str, list[Any]]], *args: Any, **kwargs: Any) -> tuple[str, list[Any]]:
    try:
        typ, data = fn(*args, **kwargs)
    except imaplib.IMAP4.abort:
        raise TransientError("imap_connection_dropped") from None
    except imaplib.IMAP4.error:
        raise ProviderError("imap_command_failed") from None
    except OSError:
        raise TransientError("imap_network") from None
    return (typ.decode() if isinstance(typ, bytes) else str(typ)), list(data or [])


def _logout(client: IMAPClient) -> None:
    try:
        client.logout()
    except (imaplib.IMAP4.error, OSError):
        pass
