"""Answers come back to a mailbox I read, spam when the owner allows it, and how far back I read (acceptance).

1. Monitoring the answer (QA K8; cases 1, 22): every email leaves from the one sending address nobody reads, so
   it carries ``Reply-To`` = the business's mailbox the sync reads. The supplier's answer lands there, arrives with
   the next mailbox sync, and is tied to the request by In-Reply-To / References (the Message-ID I set), or by the
   reference in the subject when the sending service replaced that Message-ID; its invoice closes the payment.
2. Spam (QA B8): off by default; the owner's "Also look in spam for invoices" (``/api/settings/reading``) adds
   Gmail's spam, Microsoft 365's Junk Email and an IMAP Junk or Spam folder to what is read and searched. The
   trash never.
3. How far back the first read goes (QA A9): the owner chooses the last 90 days (default) or the last 12 months,
   during onboarding or in settings; the first sync of each new mailbox and bank reads that far back. Choosing 12
   months later gives what is already connected its older months as a known gap, read by the sync worker.
"""

from __future__ import annotations

import re
import smtplib
from datetime import date, datetime, timedelta, timezone
from email import message_from_bytes
from email.message import EmailMessage
from email.policy import default as email_policy
from pathlib import Path
from typing import Any

import httpx
import pytest
from _server_support import NIF_C, PASSWORD, bearer, signup
from test_acceptance_resilience import _ms
from test_ingest_imap import FakeIMAP
from test_server_sync import GOOGLE, MAILBOX, FakeBank, _gmail_owner, _same_after_replay, _setup

from backoffice.connectors.authorize import PROVIDERS, OAuthApp
from backoffice.connectors.base import ConnectorKind, ConnectorState
from backoffice.connectors.gmail import GmailConfig, GmailConnector
from backoffice.connectors.imap import IMAPAuth, IMAPConfig, IMAPConnector, decode_mailbox_name
from backoffice.connectors.mail_search import MailQuery, MailTerm, TermKind
from backoffice.connectors.microsoft import GraphMailConfig, MicrosoftMailConnector
from backoffice.connectors.open_banking import BankConsent, ConsentStatus
from backoffice.demo import evidence as E
from backoffice.language import find_jargon, find_off_tone
from backoffice.mailer import SimulatedOutbox, SmtpMailer
from backoffice.orchestrator import TZ
from backoffice.server.events import Event
from backoffice.server.sync import SyncWorker, _connections
from backoffice.service import BackOfficeService

MICROSOFT = OAuthApp("microsoft", "ms-client", "ms-secret", **PROVIDERS["microsoft"])
EDP = "faturas@edp.pt"
IBAN = "PT50000201231234567890154"


def _events(h: Any, tenant: str, kind: str | None = None) -> list[Event]:
    return [e for e in (Event.parse(r) for r in h.store.events(tenant)) if kind is None or e.kind == kind]


def ok(res: Any, status: int = 200) -> Any:
    assert res.status_code == status, (res.request.url, res.text)
    return res.json() if res.content else None


def plain(*texts: str) -> None:
    for text in texts:
        assert find_jargon(text) == [] and find_off_tone(text) == [], text


def client(handler: Any) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _bound(query: str, name: str) -> float | None:
    """A Gmail search bound (``after:`` / ``before:``) as epoch seconds: epoch seconds, or a YYYY/MM/DD day."""
    m = re.search(rf"\b{name}:(\S+)", query)
    if not m:
        return None
    value = m.group(1)
    if value.isdigit():
        return float(value)
    y, mo, d = (int(x) for x in value.split("/"))
    return datetime(y, mo, d, tzinfo=timezone.utc).timestamp()


class FakeGmail:
    """Google's token endpoint and the Gmail API of one mailbox, ana@padaria.pt.

    Only mail addressed to that address arrives in it. Listings honour ``after:``/``before:``, ``includeSpamTrash``
    and ``-in:trash`` like Gmail; ``history`` gives what arrived since a history id, with its labels."""

    ADDRESS = "ana@padaria.pt"

    def __init__(self, clock: Any) -> None:
        self.clock = clock
        self.requests: list[httpx.Request] = []
        self.messages: dict[str, tuple[bytes, datetime, tuple[str, ...]]] = {}
        self.added: list[tuple[int, str]] = []
        self.history_id = 100

    def deliver(self, raw: bytes, *, at: datetime | None = None, labels: tuple[str, ...] = ("INBOX",)) -> str | None:
        to = message_from_bytes(raw, policy=email_policy)["To"]
        if to is None or self.ADDRESS not in {a.addr_spec.lower() for a in to.addresses}:
            return None  # sent somewhere else: it never reaches this mailbox
        mid = f"m{len(self.messages) + 1}"
        self.messages[mid] = (raw, at or self.clock.now_, labels)
        self.history_id += 1
        self.added.append((self.history_id, mid))
        return mid

    def listings(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path.endswith("/messages")]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        import base64

        self.requests.append(request)
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": f"at-{len(self.requests)}", "expires_in": 3600})
        path = request.url.path.replace("/gmail/v1/users/me", "")
        if path == "/profile":
            return httpx.Response(200, json={"emailAddress": self.ADDRESS, "historyId": str(self.history_id)})
        if path == "/history":
            start = int(request.url.params["startHistoryId"])
            added = [{"message": {"id": m, "labelIds": list(self.messages[m][2])}} for h, m in self.added if h > start]
            return httpx.Response(200, json={"history": [{"id": str(self.history_id), "messagesAdded": added}],
                                             "historyId": str(self.history_id)})
        if path == "/messages":
            q = request.url.params.get("q", "")
            spam_trash = request.url.params.get("includeSpamTrash") == "true"
            after, before = _bound(q, "after"), _bound(q, "before")
            ids = [m for m, (_, t, labels) in sorted(self.messages.items(), key=lambda kv: kv[1][1])
                   if (spam_trash or not {"SPAM", "TRASH"} & set(labels))
                   and not ("-in:trash" in q and "TRASH" in labels)
                   and (after is None or t.timestamp() >= after) and (before is None or t.timestamp() < before)]
            return httpx.Response(200, json={"messages": [{"id": i} for i in ids]} if ids else
                                  {"resultSizeEstimate": 0})
        if path.startswith("/messages/"):
            mid = path.rsplit("/", 1)[1]
            raw, received, labels = self.messages[mid]
            return httpx.Response(200, json={"id": mid, "threadId": f"t-{mid}", "labelIds": list(labels),
                                             "internalDate": _ms(received),
                                             "raw": base64.urlsafe_b64encode(raw).decode().rstrip("=")})
        return httpx.Response(404)


def _worker(h: Any, vault: Any, gmail: FakeGmail, **kwargs: Any) -> SyncWorker:
    return SyncWorker(h.manager, vault=vault, oauth_apps={"google": GOOGLE, "microsoft": MICROSOFT},
                      http_client=client(gmail), **kwargs)


def _mail(subject: str, *, sender: str = "joana@cliente.pt", to: str = "ana@padaria.pt") -> bytes:
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = sender, to, subject
    m["Message-ID"] = f"<{abs(hash(subject))}@cliente.pt>"
    m["Date"] = "Fri, 02 Oct 2026 10:00:00 +0100"
    m.set_content("Olá Ana, obrigado.")
    return m.as_bytes()


# =========================================================================== 1. the supplier's answer (K8)


class SmtpServer:
    """Stands in for the SMTP service (Amazon SES in the EU region): every accepted message, as on the wire."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.wire: list[bytes] = []
        server = self

        class _Session:
            def __init__(self, host: str, port: int, timeout: float | None = None) -> None:
                self.host = host

            def __enter__(self) -> _Session:
                return self

            def __exit__(self, *exc: object) -> bool:
                return False

            def starttls(self, context: Any = None) -> None:
                return None

            def login(self, user: str, password: str) -> None:
                return None

            def send_message(self, msg: EmailMessage) -> None:
                server.wire.append(msg.as_bytes())

        monkeypatch.setattr(smtplib, "SMTP", _Session)

    def to(self, address: str) -> list[EmailMessage]:
        found = [message_from_bytes(raw, policy=email_policy) for raw in self.wire]
        return [m for m in found if address in str(m["To"])]  # type: ignore[misc]


def _padaria_payment(h: Any, H: dict[str, str]) -> str:
    """Padaria Lda's bank, EDP as a supplier, asking suppliers switched on, and a €64.10 EDP payment on 19 September
    whose invoice is nowhere yet."""
    bank = ok(h.client.post("/api/sources", json={"kind": "bank", "bank": "Millennium BCP", "companyId": "padaria-lda",
                                                  "iban": IBAN}, headers=H))
    ok(h.client.post("/api/sources", json={"kind": "supplier", "name": "EDP", "taxId": "501000100", "email": EDP},
                     headers=H))
    ok(h.client.post("/api/settings/automation", json={"supplierRequests": True}, headers=H))
    rows = (f"date,amount,counterparty,account,description,kind\n"
            f"2026-09-19,-64.10,EDP,{bank['id']},DD EDP,direct_debit\n").encode()
    return ok(h.client.post("/api/evidence", files={"file": ("extrato.csv", rows, "text/csv")},
                            headers=H))["transactions"][0]


@pytest.mark.parametrize("matched_by", ["message_id", "subject_reference"])
def test_the_suppliers_answer_reaches_the_mailbox_i_read_and_closes_the_payment(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, matched_by: str) -> None:
    smtp = SmtpServer(monkeypatch)
    mailer = SmtpMailer("email-smtp.eu-west-1.amazonaws.com", 587, "smtp-user", "smtp-secret", "pedidos@admin2ai.app")
    h, vault, _ = _setup(tmp_path, mailer=mailer)
    tenant, H = _gmail_owner(h, vault)  # Gmail: ana@padaria.pt, read by the sync worker
    gmail = FakeGmail(h.clock)
    worker = _worker(h, vault, gmail)
    worker.run_once()  # its first read: nothing from EDP there
    tx = _padaria_payment(h, H)
    assert smtp.wire == []  # the mailbox is searched first (§22)

    report = worker.run_once()  # searched: not there, so EDP is asked, through the SMTP service
    assert report.searches == 1
    [request] = smtp.to(EDP)
    with h.manager.open(tenant) as rt:
        chase = rt.service.repo.chases[tx]
        assert chase.sent and chase.thread is not None
        message_id, token = chase.message.message_id, chase.thread.token
    # From the one sending address nobody reads; the answer goes to the mailbox the sync reads, in the thread.
    assert request["From"] == "pedidos@admin2ai.app" and request["Reply-To"] == "ana@padaria.pt"
    assert request["Message-ID"] == message_id and f"Ref. {token}" in request["Subject"]
    assert "64,10" in request.get_content()
    assert [e.kind for e in _events(h, tenant, "outbox.send")] == ["outbox.send"]  # sent once the service accepted

    # EDP answers the email it received: to its Reply-To, in its thread, with the invoice. When the sending service
    # replaced my Message-ID with its own (Amazon SES does), the thread names that one: the subject still has my
    # reference, and the answer comes from EDP's own domain.
    answer = EmailMessage()
    answer["From"], answer["To"] = "EDP Comercial <faturas@edp.pt>", request["Reply-To"]
    answer["Subject"] = "RE: " + str(request["Subject"])
    answer["Message-ID"], answer["Date"] = "<resposta-0930@edp.pt>", "Fri, 02 Oct 2026 11:00:00 +0100"
    thread = message_id if matched_by == "message_id" else "<0102019a3c1e7f-replaced@eu-west-1.amazonses.com>"
    answer["In-Reply-To"], answer["References"] = thread, thread
    answer.set_content("Boa tarde, segue a fatura em anexo.")
    answer.add_attachment(E.EDP_INVOICE, maintype="text", subtype="plain", filename="FT EDP2026-558120.txt")
    assert gmail.deliver(answer.as_bytes()) is not None  # it reached the mailbox I read

    h.clock.advance(minutes=16)
    report = worker.run_once()  # the normal mailbox sync: nothing else asks for it
    assert report.messages == 1 and _events(h, tenant, "sync.mail")[-1].data["messages"]
    detail = ok(h.client.get(f"/api/transactions/{tx}", headers=H))
    assert detail["status"] == "closed"
    with h.manager.open(tenant) as rt:
        svc = rt.service
        chase = svc.repo.chases[tx]
        assert chase.status == "received" and len(chase.replies) == 1
        assert svc.repo.transactions[tx].document_ids  # closed on the invoice EDP sent, with evidence
        how = [r.data()["extracted_values"] for r in svc.repo.audit_store.records(tenant)
               if r.action == "supplier_reply"]
        assert how and how[-1]["method"] == ("in_reply_to" if matched_by == "message_id" else "subject_reference")
    activity = [a["text"] for a in ok(h.client.get("/api/activity", headers=H))["items"]]
    assert "EDP sent the invoice for the €64.10 payment in reply to my request." in activity
    assert len(smtp.to(EDP)) == 1  # no reminder: the answer came
    _same_after_replay(h, tenant)


def test_an_answer_sent_to_another_address_never_reaches_the_mailbox_and_nothing_is_claimed(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The control: had the request gone without Reply-To, EDP's answer would go to the sending address."""
    smtp = SmtpServer(monkeypatch)
    h, vault, _ = _setup(tmp_path, mailer=SmtpMailer("smtp.example", 587, "u", "p", "pedidos@admin2ai.app"))
    tenant, H = _gmail_owner(h, vault)
    gmail = FakeGmail(h.clock)
    worker = _worker(h, vault, gmail)
    worker.run_once()
    tx = _padaria_payment(h, H)
    worker.run_once()
    [request] = smtp.to(EDP)
    answer = EmailMessage()
    answer["From"], answer["To"], answer["Subject"] = EDP, str(request["From"]), "RE: " + str(request["Subject"])
    answer["In-Reply-To"] = str(request["Message-ID"])
    answer.set_content("Segue a fatura.")
    assert gmail.deliver(answer.as_bytes()) is None
    h.clock.advance(minutes=16)
    assert worker.run_once().messages == 0
    assert ok(h.client.get(f"/api/transactions/{tx}", headers=H))["status"] != "closed"


def test_smtp_keeps_the_thread_and_reply_to_and_nothing_else(monkeypatch: pytest.MonkeyPatch) -> None:
    smtp = SmtpServer(monkeypatch)
    SmtpMailer("smtp.example", 587, "u", "p", "pedidos@admin2ai.app").send(
        ["faturas@edp.pt"], "Pedido de fatura (Ref. 7KQ2MX)", "Olá", [("pedido.txt", "text/plain", b"x")],
        headers={"Message-ID": "<chase-7kq2mx-0@backoffice.example>", "In-Reply-To": "<a@b>", "References": "<a@b>",
                 "Reply-To": "ana@padaria.pt", "Bcc": "someone@else.example", "From": "ceo@edp.pt"})
    [sent] = smtp.to(EDP)
    assert sent["Reply-To"] == "ana@padaria.pt" and sent["Message-ID"] == "<chase-7kq2mx-0@backoffice.example>"
    assert sent["In-Reply-To"] == "<a@b>" == sent["References"]
    assert sent["Bcc"] is None and sent["From"] == "pedidos@admin2ai.app"  # never anything else from the engine
    with pytest.raises(ValueError):  # a header can never carry a second line
        SmtpMailer("smtp.example", 587, "u", "p", "pedidos@admin2ai.app").send(
            [EDP], "x", "y", [], headers={"Reply-To": "ana@padaria.pt\r\nBcc: x@y.pt"})


def test_reply_to_is_the_companys_own_mailbox_i_read_else_the_owner() -> None:
    now = datetime(2026, 10, 2, 9, 30, tzinfo=TZ)
    svc = BackOfficeService.new_tenant("t-reply", owner_name="Ana Silva", owner_email="ana@example.pt", now=now)
    svc.add_company("Padaria Lda", "516123459")
    svc.add_company("Oficina Lda", "501234560")
    outbox = SimulatedOutbox()
    svc.orchestrator.transport = svc.mailer = outbox
    o = svc.orchestrator
    companies = list(svc.repo.companies)
    assert o.reply_address(companies[0]) == "ana@example.pt"  # nothing read yet: at least a person gets it
    for address, company in (("contas@padaria.pt", companies[0]), ("geral@oficina.pt", companies[1])):
        status, _ = svc.dispatch("POST", "/api/sources", {"kind": "email", "provider": "imap", "address": address,
                                                          "host": "imap.example.pt", "password": "app-pw",
                                                          "companyId": company})
        assert status == 200
    assert o.reply_address(companies[0]) == "contas@padaria.pt"
    assert o.reply_address(companies[1]) == "geral@oficina.pt"
    svc.repo.connectors["mail-contas-padaria-pt"].healthy = False  # needs signing in again: the other one first
    assert o.reply_address(None) == "geral@oficina.pt"
    # The owner's own emails (the chat's Send) carry it too.
    draft = svc.assistant.draft_email(["marc@vidal.pt"], "Setembro", "Olá Marc")
    assert svc.assistant.send(draft.id).status == "sent"
    assert dict(outbox.accepted[-1].headers)["Reply-To"] == "geral@oficina.pt"


# =========================================================================== 2. spam (B8)


def test_spam_is_off_by_default_and_the_owner_switches_it_on_for_reading_and_searching(tmp_path: Path) -> None:
    h, vault, _ = _setup(tmp_path)
    tenant, H = _gmail_owner(h, vault)
    settings = ok(h.client.get("/api/settings/reading", headers=H))
    assert settings["lookInSpam"] is False and settings["spamLabel"] == "Also look in spam for invoices"
    assert settings["history"] == "90d" and [o["label"] for o in settings["historyOptions"]] == [
        "Last 90 days", "Last 12 months"]
    plain(settings["spamLabel"], settings["spamDetail"], settings["historyLabel"], settings["historyDetail"])
    gmail = FakeGmail(h.clock)
    for subject, labels in (("Fatura inbox", ("INBOX",)), ("Fatura spam", ("SPAM",)), ("Lixo", ("TRASH",))):
        gmail.deliver(_mail(subject), at=h.clock.now_ - timedelta(days=3), labels=labels)
    worker = _worker(h, vault, gmail)
    assert worker.run_once().messages == 1  # off: only the inbox
    assert gmail.listings()[0].url.params["includeSpamTrash"] == "false"

    before = len(_events(h, tenant))
    out = ok(h.client.post("/api/settings/reading", json={"lookInSpam": True}, headers=H))
    assert out["message"] == "Done. I will also look in spam for invoices." and out["lookInSpam"] is True
    plain(out["message"])
    [change] = [e for e in _events(h, tenant)[before:] if e.kind == "request"]
    assert change.data["path"] == "/api/settings/reading" and change.data["body"] == {"lookInSpam": True}
    gmail.deliver(_mail("Nova fatura spam"), labels=("SPAM",))
    gmail.deliver(_mail("Apagada"), labels=("TRASH",))
    h.clock.advance(minutes=16)
    assert worker.run_once().messages == 1  # the new spam is read; the trash never
    # Searching for a missing invoice looks in spam too, never in the trash (Gmail: includeSpamTrash, -in:trash).
    [c] = h.manager.read(tenant, lambda svc: _connections(tenant, svc, ("email",)), what="test")
    connector, _ = worker.search_connector(c)
    found = connector.search_messages(MailQuery(date(2026, 9, 1), date(2026, 10, 3),
                                                any_of=(MailTerm(TermKind.TEXT, "Fatura"),), limit=10))
    subjects = {message_from_bytes(m.raw, policy=email_policy)["Subject"] for m in found}
    assert subjects == {"Fatura inbox", "Fatura spam", "Nova fatura spam"}
    search = gmail.listings()[-1]
    assert search.url.params["includeSpamTrash"] == "true" and "-in:trash" in search.url.params["q"]
    _same_after_replay(h, tenant)

    off = ok(h.client.post("/api/settings/reading", json={"lookInSpam": False}, headers=H))
    assert off["message"] == "Done. I won't look in spam." and off["lookInSpam"] is False
    assert h.client.post("/api/settings/reading", json={"lookInSpam": "yes"}, headers=H).status_code == 400
    assert h.client.post("/api/settings/reading", json={"spam": True}, headers=H).status_code == 400


def test_gmail_reads_spam_but_never_the_trash_when_allowed() -> None:
    now = datetime(2026, 10, 2, 9, 30, tzinfo=timezone.utc)

    class _Clock:
        now_ = now

    gmail = FakeGmail(_Clock())
    for subject, labels in (("Fatura", ("INBOX",)), ("Spam", ("SPAM",)), ("Lixo", ("TRASH",))):
        gmail.deliver(_mail(subject), at=now - timedelta(days=2), labels=labels)

    def read(config: GmailConfig) -> list[str]:
        got: list[Any] = []
        state = ConnectorState(tenant_id="t", kind=ConnectorKind.GMAIL, account="ana@padaria.pt")
        assert GmailConnector(_Tokens(), client=client(gmail), config=config, clock=lambda: now).sync(
            state, got.append).ok
        return sorted(str(message_from_bytes(m.raw, policy=email_policy)["Subject"]) for m in got)

    assert read(GmailConfig()) == ["Fatura"]
    assert "-in:trash" not in gmail.listings()[-1].url.params["q"]
    assert read(GmailConfig(include_spam=True)) == ["Fatura", "Spam"]
    listing = gmail.listings()[-1]
    assert listing.url.params["includeSpamTrash"] == "true" and "-in:trash" in listing.url.params["q"]
    # A thread's earlier messages: spam when allowed, the trash never.
    allowed = GmailConnector(_Tokens(), client=client(gmail), config=GmailConfig(include_spam=True))
    assert allowed._wanted(["SPAM"]) and not allowed._wanted(["TRASH"]) and not allowed._wanted(["SPAM", "TRASH"])
    assert not GmailConnector(_Tokens(), client=client(gmail))._wanted(["SPAM"])


class _Tokens:
    def access_token(self) -> str:
        return "at-1"

    def invalidate(self) -> None:
        return None


class FakeGraph:
    """Graph: Inbox, Junk Email and Deleted Items, each with one message; mailbox-wide listings span them all."""

    G = "https://graph.microsoft.com/v1.0"

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.folders = {"inbox": "INBOX", "junkemail": "JUNK", "deleteditems": "DELETED"}
        self.items = {"INBOX": "i1", "JUNK": "j1", "DELETED": "d1"}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path.replace("/v1.0/me", "")
        if path.startswith("/mailFolders/") and path.count("/") == 2:
            name = path.rsplit("/", 1)[1]
            folder = self.folders.get(name)
            return httpx.Response(200, json={"id": folder, "childFolderCount": 0}) if folder else httpx.Response(404)
        m = re.fullmatch(r"/mailFolders/([A-Z]+)/messages/delta", path)
        if m:
            item = self.items[m.group(1)]
            return httpx.Response(200, json={"value": [{"id": item, "receivedDateTime": "2026-09-30T10:00:00Z",
                                                        "parentFolderId": m.group(1)}],
                                             "@odata.deltaLink": f"{self.G}/me/mailFolders/{m.group(1)}/messages/"
                                                                 "delta?$deltatoken=x"})
        if path == "/messages":
            return httpx.Response(200, json={"value": [{"id": item, "receivedDateTime": "2026-09-30T10:00:00Z",
                                                        "parentFolderId": folder}
                                                       for folder, item in self.items.items()]})
        m = re.fullmatch(r"/messages/([a-z0-9]+)/\$value", path)
        if m:
            return httpx.Response(200, content=f"Subject: {m.group(1)}\r\n\r\nx".encode())
        return httpx.Response(404)

    def deltas(self) -> list[str]:
        return [r.url.path.split("/")[-3] for r in self.requests if r.url.path.endswith("/messages/delta")]


def test_microsoft_reads_junk_email_only_when_allowed_and_never_deleted_items() -> None:
    now = datetime(2026, 10, 2, 9, 30, tzinfo=timezone.utc)
    query = MailQuery(date(2026, 9, 1), date(2026, 10, 3), any_of=(MailTerm(TermKind.FROM, "edp.pt"),), limit=10)
    state = ConnectorState(tenant_id="t", kind=ConnectorKind.MICROSOFT, account="ana@padaria.pt")
    for junk, wanted in ((False, ["i1"]), (True, ["i1", "j1"])):
        graph = FakeGraph()
        connector = MicrosoftMailConnector(_Tokens(), client=client(graph), clock=lambda: now,
                                           config=GraphMailConfig(include_junk=junk))
        got: list[Any] = []
        assert connector.sync(state, got.append).ok
        assert sorted(m.provider_id for m in got) == wanted
        assert graph.deltas() == (["INBOX", "JUNK"] if junk else ["INBOX"])  # Deleted Items is never walked
        # Searching (and backfills, conversations) span every folder: Deleted Items is left out, Junk unless allowed.
        assert sorted(m.provider_id for m in connector.search_messages(query)) == wanted
        assert sorted(m.provider_id for m in _collect(connector, state, now)) == wanted


def _range(now: datetime) -> Any:
    from backoffice.connectors.base import TimeRange

    return TimeRange(start=now - timedelta(days=7), end=now)


def _collect(connector: Any, state: Any, now: datetime) -> list[Any]:
    got: list[Any] = []
    assert connector.backfill(state, _range(now), got.append).ok
    return got


def test_imap_reads_the_junk_folder_only_when_allowed_and_never_the_trash() -> None:
    boxes = {'"INBOX"': {1: b"Subject: inbox\r\n\r\n1"}, '"Junk"': {4: b"Subject: junk\r\n\r\n4"},
             '"Trash"': {9: b"Subject: trash\r\n\r\n9"}}
    state = ConnectorState(tenant_id="t", kind=ConnectorKind.IMAP, account="ana@padaria.pt")

    def read(fake: Any, **config: Any) -> list[str]:
        got: list[Any] = []
        connector = IMAPConnector(IMAPConfig(host="imap.padaria.pt", **config), IMAPAuth("ana@padaria.pt", "pw"),
                                  client_factory=lambda cfg: fake, clock=lambda: datetime(2026, 10, 2, tzinfo=timezone.utc))
        assert connector.sync(state, got.append).ok
        return [m.provider_id for m in got]

    plain_server = FakeIMAP(dict(boxes))
    assert read(plain_server) == ["INBOX:1"]
    assert not any(c[0] == "SELECT" and c[1] != '"INBOX"' for c in plain_server.commands)
    assert read(FakeIMAP(dict(boxes)), include_junk=True) == ["INBOX:1", "Junk:4"]  # a server without LIST

    class ListingIMAP(FakeIMAP):
        """A server that marks its folders (RFC 6154), in Portuguese, with a "Spam" folder that is its trash."""

        def list(self) -> tuple[str, list[bytes]]:
            self.commands.append(("LIST",))
            return "OK", [b'(\\HasNoChildren) "/" "INBOX"',
                          b'(\\HasNoChildren \\Junk) "/" "Lixo eletr&APM-nico"',
                          b'(\\HasNoChildren \\Trash) "/" "Spam"']

    listing = ListingIMAP({'"INBOX"': boxes['"INBOX"'], '"Lixo eletr&APM-nico"': {6: b"Subject: lixo\r\n\r\n6"},
                           '"Spam"': {7: b"Subject: apagada\r\n\r\n7"}})
    assert read(listing, include_junk=True) == ["INBOX:1", "Lixo eletrónico:6"]
    assert not any(c[0] == "SELECT" and c[1] == '"Spam"' for c in listing.commands)  # the trash, never opened
    assert decode_mailbox_name("Lixo eletr&APM-nico") == "Lixo eletrónico" and decode_mailbox_name("A&-B") == "A&B"


def test_the_worker_builds_every_mailbox_with_the_owners_choices(tmp_path: Path) -> None:
    h, vault, _ = _setup(tmp_path)
    tenant, H = _gmail_owner(h, vault)
    vault.store(tenant, "mail-microsoft-pending", "microsoft", {"refresh_token": "rt-ms"})
    assert h.manager.finish_sign_in(tenant, "mail-microsoft-pending", "microsoft", "contas@padaria.pt")[0] == 200
    ok(h.client.post("/api/sources", json={"kind": "email", "provider": "imap", "address": "geral@padaria.pt",
                                           "host": "imap.padaria.pt", "password": "app-pw"}, headers=H))
    worker = _worker(h, vault, FakeGmail(h.clock))

    def built() -> dict[str, Any]:
        found = h.manager.read(tenant, lambda svc: _connections(tenant, svc, ("email",)), what="test")
        return {c.account: worker._connector(c, vault.metadata(tenant, c.id)) for c in found}

    default = built()
    assert default["ana@padaria.pt"].config.include_spam is False
    assert default["contas@padaria.pt"].config.include_junk is False
    assert default["geral@padaria.pt"].config.include_junk is False
    assert {c.config.history_window for c in default.values()} == {timedelta(days=90)}
    ok(h.client.post("/api/settings/reading", json={"lookInSpam": True, "history": "12m"}, headers=H))
    chosen = built()
    assert chosen["ana@padaria.pt"].config.include_spam is True
    assert chosen["contas@padaria.pt"].config.include_junk is True
    assert chosen["geral@padaria.pt"].config.include_junk is True
    assert {c.config.history_window for c in chosen.values()} == {timedelta(days=365)}


# =========================================================================== 3. how far back (A9)


class LongBank(FakeBank):
    """A bank that shares two years of payments."""

    def __init__(self, days: int = 730) -> None:
        super().__init__()
        self.days = days
        self.links: list[dict[str, Any]] = []
        self.refuse_over: int | None = None

    def create_link(self, **kwargs: Any) -> Any:
        if self.refuse_over is not None and int(kwargs["history_days"]) > self.refuse_over:
            raise RuntimeError("max_historical_days is too long for this bank")
        self.links.append(dict(kwargs))
        return super().create_link(**kwargs)

    def consent(self, requisition_id: str) -> BankConsent:
        return BankConsent("req-1", self.status, ("acc-1",), "MILLENNIUMBCP_BCOMPTPL", self.expires, self.days)


def _link_bank(h: Any, H: dict[str, str], bank: Any) -> None:
    ok(h.client.post("/api/connections/bank/start", json={"institutionId": "MILLENNIUMBCP_BCOMPTPL"}, headers=H))
    back = h.client.get("/api/connections/bank/callback", params={"ref": bank.reference}, follow_redirects=False)
    assert back.headers["location"].endswith("?bank=done")


def test_twelve_months_chosen_before_connecting_is_how_far_the_first_read_goes(tmp_path: Path) -> None:
    bank = LongBank()
    h, vault, _ = _setup(tmp_path, aggregator=lambda: bank)
    account = signup(h.client)
    tenant, H = account["tenant"]["id"], bearer(account["token"])
    out = ok(h.client.post("/api/settings/reading", json={"history": "12m"}, headers=H))
    assert out["message"] == "Done. I will read the last 12 months of what you connect." and out["history"] == "12m"
    plain(out["message"])
    vault.store(tenant, "mail-google-pending", "google", {"refresh_token": "rt-1"})
    assert h.manager.finish_sign_in(tenant, "mail-google-pending", "google", "ana@padaria.pt")[0] == 200
    _link_bank(h, H, bank)
    assert bank.links[0]["history_days"] == 365  # the bank consent asks for as far back as the owner chose
    gmail = FakeGmail(h.clock)
    old = gmail.deliver(_mail("Fatura de fevereiro"), at=h.clock.now_ - timedelta(days=230))
    worker = SyncWorker(h.manager, vault=vault, oauth_apps={"google": GOOGLE}, http_client=client(gmail),
                        aggregator_factory=lambda: bank)
    report = worker.run_once()
    assert report.synced == [f"{tenant}/{MAILBOX}", f"{tenant}/bank-millenniumbcp"]
    after = _bound(gmail.listings()[0].url.params["q"], "after")
    assert after is not None and abs(after - (h.clock.now_ - timedelta(days=365)).timestamp()) < 120
    assert old and report.messages == 1  # February's email is in the first read
    today = h.clock.now_.astimezone(timezone.utc).date()
    assert bank.windows == [("acc-1", today - timedelta(days=365), today)]
    activity = [a["text"] for a in ok(h.client.get("/api/activity", headers=H))["items"]]
    assert "Read ana@padaria.pt: the last 12 months are in." in activity
    assert "Imported the last 12 months from Millenniumbcp." in activity
    with h.manager.open(tenant) as rt:
        covered = rt.service.repo.connectors[MAILBOX].covered_from
        assert covered is not None and h.clock.now_ - covered >= timedelta(days=364)
    _same_after_replay(h, tenant)


def test_a_bank_that_shares_less_is_linked_for_what_it_shares(tmp_path: Path) -> None:
    bank = LongBank(days=90)
    bank.refuse_over = 90
    h, vault, _ = _setup(tmp_path, aggregator=lambda: bank)
    account = signup(h.client)
    H = bearer(account["token"])
    ok(h.client.post("/api/settings/reading", json={"history": "12m"}, headers=H))
    _link_bank(h, H, bank)
    assert [link["history_days"] for link in bank.links] == [90]  # asked for 12 months, linked for its 90 days


def test_choosing_twelve_months_later_reads_the_older_months_of_what_is_connected(tmp_path: Path) -> None:
    bank = LongBank(days=90)  # this bank shares only 90 days
    h, vault, _ = _setup(tmp_path, aggregator=lambda: bank)
    tenant, H = _gmail_owner(h, vault)
    _link_bank(h, H, bank)
    gmail = FakeGmail(h.clock)
    gmail.deliver(_mail("Fatura de março"), at=h.clock.now_ - timedelta(days=200))
    gmail.deliver(_mail("Fatura de setembro"), at=h.clock.now_ - timedelta(days=10))
    worker = SyncWorker(h.manager, vault=vault, oauth_apps={"google": GOOGLE}, http_client=client(gmail),
                        aggregator_factory=lambda: bank, backfill_chunk=timedelta(days=30))
    assert worker.run_once().messages == 1  # the default: 90 days
    assert ok(h.client.get("/api/settings/reading", headers=H))["history"] == "90d"

    before = len(_events(h, tenant))
    out = ok(h.client.post("/api/settings/reading", json={"history": "12m"}, headers=H))
    assert out["message"] == ("Done. I will read the last 12 months. I'm reading the older months of "
                              "ana@padaria.pt and Millenniumbcp now.")
    plain(out["message"])
    assert out["reading"] == ["Millenniumbcp", "ana@padaria.pt"]
    [change] = [e for e in _events(h, tenant)[before:] if e.kind == "request"]
    assert change.data["path"] == "/api/settings/reading" and change.data["body"] == {"history": "12m"}
    with h.manager.open(tenant) as rt:
        state = ConnectorState.model_validate(rt.service.sync_states[MAILBOX])
        [gap] = state.known_gaps
        assert abs((h.clock.now_ - gap.start) - timedelta(days=365)) < timedelta(minutes=5)
        assert state.coverage_start == gap.start
        assert timedelta(days=89) < h.clock.now_ - gap.end < timedelta(days=91)  # up to what was read
    march = h.manager.view(tenant, "GET", "/api/months/padaria-lda/2026-03")[1]
    lines = [r["text"] for r in march.get("remaining", [])]
    assert any(line.startswith("Catching up on") and "email from ana@padaria.pt" in line for line in lines), lines

    # The sync worker reads the older months a chunk at a time (bounded, resumable), then says so once.
    passes = 0
    while True:
        h.clock.advance(minutes=5)
        report = worker.run_once()
        if not report.backfilled:
            break
        passes += 1
        assert passes <= 10
    assert passes == 10  # 275 days, 30 at a time
    backfills = [e for e in _events(h, tenant, "sync.mail") if e.data.get("backfill")]
    assert sum(len(e.data["messages"]) for e in backfills) == 1  # March's email
    with h.manager.open(tenant) as rt:
        state = ConnectorState.model_validate(rt.service.sync_states[MAILBOX])
        assert state.known_gaps == () and h.clock.now_ - state.coverage_start > timedelta(days=364)
    activity = [a["text"] for a in ok(h.client.get("/api/activity", headers=H))["items"]]
    assert "Read the older email from ana@padaria.pt: the last 12 months are in." in activity

    # The bank, when it is next due: it shares only 90 days, so the older months need a statement (never assumed).
    h.clock.advance(hours=7)
    worker.run_once()
    activity = [a["text"] for a in ok(h.client.get("/api/activity", headers=H))["items"]]
    line = next(a for a in activity if a.startswith("Millenniumbcp only shares the payments from"))
    assert line.endswith("on. For the months before, send me a bank statement.")
    plain(line, *activity[:5])
    march = h.manager.view(tenant, "GET", "/api/months/padaria-lda/2026-03")[1]
    assert any("Send me a bank statement" in r["text"] for r in march.get("remaining", []))
    assert ok(h.client.get("/api/settings/reading", headers=H))["reading"] == []
    _same_after_replay(h, tenant)

    # Back to 90 days: what is connected next reads 90 days; nothing already read is dropped.
    back = ok(h.client.post("/api/settings/reading", json={"history": "90d"}, headers=H))
    assert back["message"] == "Done. I will read the last 90 days of what you connect." and back["history"] == "90d"
    with h.manager.open(tenant) as rt:
        assert h.clock.now_ - rt.service.repo.connectors[MAILBOX].covered_from > timedelta(days=364)
    assert h.client.post("/api/settings/reading", json={"history": "6m"}, headers=H).status_code == 400


def test_only_the_owner_chooses_how_the_business_is_read(tmp_path: Path) -> None:
    h, vault, _ = _setup(tmp_path)
    tenant, H = _gmail_owner(h, vault)
    bob = signup(h.client, "bob@contas.pt", company="Contas Bob", tax_id=NIF_C, name="Bob")
    h.store._d.memberships.discard((bob["tenant"]["id"], bob["user"]["id"], "owner"))
    h.store.add_membership(tenant, bob["user"]["id"], "accountant")
    B = bearer(ok(h.client.post("/api/auth/login", json={"email": "bob@contas.pt", "password": PASSWORD}))["token"])
    assert ok(h.client.get("/api/settings/reading", headers=B))["lookInSpam"] is False  # may read it ...
    count = len(_events(h, tenant))
    assert h.client.post("/api/settings/reading", json={"lookInSpam": True}, headers=B).status_code == 403
    assert h.client.post("/api/settings/reading", json={"history": "12m"}, headers=B).status_code == 403
    assert len(_events(h, tenant)) == count  # ... never change it
