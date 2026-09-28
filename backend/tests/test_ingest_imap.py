"""IMAP connector: UIDVALIDITY/UID cursor, read-only access, auth failures (§8, §47)."""

from __future__ import annotations

import base64
import imaplib
import json
from datetime import date, datetime, timedelta, timezone


from backoffice.connectors.base import ConnectorKind, ConnectorState, Health, TimeRange, evaluate_health
from backoffice.connectors.imap import (
    IMAPAuth,
    IMAPConfig,
    IMAPConnector,
    _parse_fetch,
    _parse_internaldate,
    encode_mailbox_name,
    imap_date,
)

NOW = datetime(2026, 9, 25, 9, 30, tzinfo=timezone.utc)


class FakeIMAP:
    """Just enough IMAP4rev1 to exercise the connector."""

    def __init__(self, mailboxes=None, *, uidvalidity=7, fail_login=False, abort_on=None):
        self.boxes = mailboxes if mailboxes is not None else {
            '"INBOX"': {1: b"Subject: one\r\n\r\n1", 2: b"Subject: two\r\n\r\n2", 5: b"Subject: five\r\n\r\n5"}}
        self.uidvalidity = uidvalidity
        self.fail_login = fail_login
        self.abort_on = abort_on
        self.commands: list[tuple] = []
        self.selected = None
        self.readonly = None
        self.logged_out = False

    def login(self, user, password):
        self.commands.append(("LOGIN", user))
        if self.fail_login:
            raise imaplib.IMAP4.error("[AUTHENTICATIONFAILED] Invalid credentials (Failure)")
        return "OK", [b"LOGIN completed"]

    def authenticate(self, mechanism, authobject):
        self.commands.append(("AUTHENTICATE", mechanism, authobject(b"")))
        return "OK", [b"done"]

    def select(self, mailbox="INBOX", readonly=False):
        self.commands.append(("SELECT", mailbox, readonly))
        if mailbox not in self.boxes:
            return "NO", [b"Mailbox doesn't exist"]
        self.selected, self.readonly = mailbox, readonly
        return "OK", [str(len(self.boxes[mailbox])).encode()]

    def response(self, code):
        return code, [str(self.uidvalidity).encode()]

    def uid(self, command, *args):
        self.commands.append(("UID", command, *args))
        if self.abort_on == command:
            raise imaplib.IMAP4.abort("socket error: EOF")
        box = self.boxes[self.selected]
        if command == "SEARCH":
            if args[0] == "UID":
                low = int(args[1].split(":")[0])
                uids = [u for u in box if u >= low] or [max(box)]  # "n:*" always returns the highest UID
            else:
                uids = sorted(box)
            return "OK", [" ".join(map(str, sorted(uids))).encode()]
        if command == "FETCH":
            data = []
            for uid in map(int, args[0].split(",")):
                if uid in box:
                    if uid % 2:  # vary the item order like real servers do
                        meta = f'{uid} (UID {uid} INTERNALDATE "17-Jul-2026 02:44:25 -0700" BODY[] {{{len(box[uid])}}}'
                        data += [(meta.encode(), box[uid]), b")"]
                    else:
                        data += [(f"{uid} (BODY[] {{{len(box[uid])}}}".encode(), box[uid]),
                                 f' UID {uid} INTERNALDATE " 2-Sep-2026 10:00:00 +0100")'.encode()]
            return "OK", data
        return "BAD", []

    def logout(self):
        self.logged_out = True
        return "BYE", []


def connector(fake: FakeIMAP, auth=None, **config) -> IMAPConnector:
    return IMAPConnector(IMAPConfig(host="imap.example.pt", **config), auth or IMAPAuth("ana@padaria.pt", password="app-pw"),
                         client_factory=lambda cfg: fake, clock=lambda: NOW)


def new_state(**kw) -> ConnectorState:
    return ConnectorState(tenant_id="t1", kind=ConnectorKind.IMAP, account="ana@padaria.pt", **kw)


def test_first_sync_searches_the_window_read_only_and_never_marks_read():
    fake, got = FakeIMAP(), []
    outcome = connector(fake).sync(new_state(), got.append)
    assert outcome.ok and outcome.full_sync and outcome.delivered == 3
    assert [m.provider_id for m in got] == ["INBOX:1", "INBOX:2", "INBOX:5"]
    assert got[0].received_at == datetime(2026, 7, 17, 9, 44, 25, tzinfo=timezone.utc)
    assert got[1].received_at == datetime(2026, 9, 2, 9, 0, tzinfo=timezone.utc)
    assert ("SELECT", '"INBOX"', True) in fake.commands  # EXAMINE
    search = next(c for c in fake.commands if c[:2] == ("UID", "SEARCH"))
    assert search[2:] == ("SINCE", imap_date((NOW - timedelta(days=90)).date()))
    fetch = next(c for c in fake.commands if c[:2] == ("UID", "FETCH"))
    assert "BODY.PEEK[]" in fetch[3]
    assert json.loads(outcome.state.cursor) == {"v": 1, "mailboxes": {"INBOX": {"uidvalidity": 7, "last_uid": 5}}}
    assert fake.logged_out


def test_incremental_sync_filters_the_star_quirk():
    fake, got = FakeIMAP(), []
    cursor = json.dumps({"v": 1, "mailboxes": {"INBOX": {"uidvalidity": 7, "last_uid": 5}}})
    outcome = connector(fake).sync(new_state(cursor=cursor, last_successful_sync=NOW - timedelta(hours=1)), got.append)
    assert outcome.ok and not outcome.full_sync and got == []  # "6:*" returned UID 5 again: ignored
    fake.boxes['"INBOX"'][9] = b"Subject: nine\r\n\r\n9"
    outcome = connector(fake).sync(outcome.state, got.append)
    assert [m.provider_id for m in got] == ["INBOX:9"]
    assert json.loads(outcome.state.cursor)["mailboxes"]["INBOX"]["last_uid"] == 9


def test_uidvalidity_change_resyncs_and_records_gap_when_needed():
    fake = FakeIMAP(uidvalidity=8)
    cursor = json.dumps({"v": 1, "mailboxes": {"INBOX": {"uidvalidity": 7, "last_uid": 99}}})
    old = NOW - timedelta(days=100)
    outcome = connector(fake).sync(new_state(cursor=cursor, last_successful_sync=old), lambda m: None)
    assert outcome.ok and outcome.full_sync and outcome.delivered == 3
    assert json.loads(outcome.state.cursor)["mailboxes"]["INBOX"] == {"uidvalidity": 8, "last_uid": 5}
    assert outcome.state.known_gaps[0].start == old


def test_wrong_password_means_reconnect_with_plain_copy():
    outcome = connector(FakeIMAP(fail_login=True)).sync(new_state(), lambda m: None)
    assert outcome.state.reconnect_required and outcome.state.last_error_code == "imap_auth_failed"
    report = evaluate_health(outcome.state, NOW)
    assert report.health is Health.BROKEN and report.title == "Your email needs reconnecting."
    assert "AUTHENTICATIONFAILED" not in report.detail


def test_dropped_connection_is_transient_and_keeps_cursor():
    cursor = json.dumps({"v": 1, "mailboxes": {"INBOX": {"uidvalidity": 7, "last_uid": 2}}})
    outcome = connector(FakeIMAP(abort_on="FETCH")).sync(new_state(cursor=cursor), lambda m: None)
    assert not outcome.ok and outcome.error.retryable and not outcome.state.reconnect_required
    assert outcome.state.cursor == cursor


def test_missing_mailbox_is_skipped_and_other_mailboxes_sync():
    fake = FakeIMAP({'"INBOX"': {3: b"Subject: x\r\n\r\nx"}})
    outcome = connector(fake, mailboxes=("INBOX", "Faturação")).sync(new_state(), lambda m: None)
    assert outcome.ok and list(json.loads(outcome.state.cursor)["mailboxes"]) == ["INBOX"]
    assert ("SELECT", '"Fatura&AOcA4w-o"', True) in fake.commands


def test_xoauth2_and_missing_credentials():
    fake = FakeIMAP()
    connector(fake, auth=IMAPAuth("ana@padaria.pt", oauth_token="tok")).sync(new_state(), lambda m: None)
    mech = next(c for c in fake.commands if c[0] == "AUTHENTICATE")
    assert mech[1] == "XOAUTH2" and mech[2] == b"user=ana@padaria.pt\x01auth=Bearer tok\x01\x01"
    none = connector(FakeIMAP(), auth=IMAPAuth("ana@padaria.pt")).sync(new_state(), lambda m: None)
    assert none.state.reconnect_required
    assert "tok" not in repr(IMAPAuth("u", oauth_token="tok"))


def test_connect_failure_is_transient():
    def refuse(cfg):
        raise OSError("connection refused")

    c = IMAPConnector(IMAPConfig(host="imap.example.pt"), IMAPAuth("u", password="p"), client_factory=refuse,
                      clock=lambda: NOW)
    outcome = c.sync(new_state(), lambda m: None)
    assert outcome.error.code == "imap_connect_failed" and outcome.error.retryable


def test_backfill_searches_by_day_and_closes_gap():
    fake, got = FakeIMAP(), []
    gap = TimeRange(start=datetime(2026, 3, 1, 8, tzinfo=timezone.utc), end=datetime(2026, 6, 27, 9, tzinfo=timezone.utc))
    outcome = connector(fake).backfill(new_state(known_gaps=(gap,)), gap, got.append)
    assert outcome.ok and len(got) == 3 and outcome.state.known_gaps == ()
    search = next(c for c in fake.commands if c[:2] == ("UID", "SEARCH"))
    assert search[2:] == ("SINCE", "1-Mar-2026", "BEFORE", "28-Jun-2026")


def test_helpers():
    assert imap_date(date(2026, 6, 9)) == "9-Jun-2026"
    assert _parse_internaldate(b"17-Jul-1996 02:44:25 -0700") == datetime(1996, 7, 17, 9, 44, 25, tzinfo=timezone.utc)
    assert _parse_internaldate(b"garbage") is None and _parse_internaldate(b"31-Feb-2026 00:00:00 +0000") is None
    assert encode_mailbox_name("INBOX") == '"INBOX"'
    assert encode_mailbox_name('Say "hi" & bye') == '"Say \\"hi\\" &- bye"'
    assert encode_mailbox_name("Entrada/Faturação").startswith('"Entrada/Fatura&')
    raw = base64.b64encode("çã".encode("utf-16-be")).decode().rstrip("=").replace("/", ",")
    assert encode_mailbox_name("Faturação") == f"\"Fatura&{raw}-o\""
    assert _parse_fetch([b")", (b"1 (UID 4 BODY[] {1}", b"x")]) == [(4, None, b"x")]
