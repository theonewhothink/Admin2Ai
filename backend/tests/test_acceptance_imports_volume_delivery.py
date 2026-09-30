"""Supplier requests in production, the monthly package, import chains, volume and phone photos (acceptance).

1. Supplier-request permission in production (QA K7, C7; cases 1, 22): a new production tenant asks nobody for
   anything until the owner switches it on in ``/api/settings/automation`` (owner only, recorded as an event,
   replay-safe); then the server sends the supplier requests through the send path.
2. The monthly accountant package on schedule (QA N9, N10; cases 1, 28): on the chosen working day after month-end
   each company's package goes to its accountant as an attachment; it is delivered only once the transport
   accepted it and confirmed by the accountant's reply in its thread; a month with open items goes with an
   honest note, or waits when the owner chose so, and its final package follows once it closes.
3. Import purchase chains (QA X28; cases 36, 39): a foreign purchase's pro-forma, deposit, invoice, freight,
   customs declaration and duties are one chain, linked by order, MRN, container and bill of lading references
   only (never by amount); the accountant is shown the import VAT paid at customs.
4. High volume (QA X33; cases 2, 19, 46): 3,000 payments and 1,500 documents in one month run within a time
   budget, with every exact match closed and nothing duplicated.
5. Photo versus PDF (QA G3; case 4): a photographed invoice and the emailed PDF are one document with both
   originals; a photo whose number could not be read is one plain question, never a second expense.
6. Poor scans (QA E10, D2; case 40): the phone's quality hints and the reader's own estimate send a poor photo to
   the stronger engine; still unreadable, the owner gets one plain task to take it again; the pages of one scan
   are one document.
"""

from __future__ import annotations

import base64
import io
import json
import struct
import time
import zipfile
import zlib
from datetime import date
from decimal import Decimal
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import pytest
from _server_support import NIF_C, PASSWORD, bearer, harness, sha, signup

from backoffice.closure.package import verify_package
from backoffice.countries.pt.nif import nif_check_digit
from backoffice.demo import evidence as E
from backoffice.domain.lifecycle import Stage
from backoffice.domain.models import Supplier, TransactionKind
from backoffice.imports import find_chains, import_vat, references
from backoffice.mailer import SimulatedOutbox
from backoffice.ocr import PADDLEOCR_VL, PP_OCR_V6_MEDIUM, EngineRegistry, FakeOCRProvider, OCRCapabilities
from backoffice.orchestrator import (
    Account,
    AccountantProfile,
    BankRow,
    ConnectorState,
    Orchestrator,
    OwnerProfile,
    Repository,
    local_datetime,
)
from backoffice.package_delivery import working_day
from backoffice.policy import ActionKind
from backoffice.reading import DocumentReader, StepState
from backoffice.server.events import Event, state_digest
from backoffice.server.runtime import TenantManager
from backoffice.service import BackOfficeService

K = TransactionKind
D = Decimal
FIXTURES = Path(__file__).parent / "fixtures" / "documents"
ACCOUNTANT = "marc@vidal.pt"
IBAN = "PT50000201231234567890154"


class CapturingMailer:
    """A transport that accepts everything and keeps what it was given (recipients, subject, body, files)."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def send(self, to: list[str], subject: str, body: str, files: Any, headers: Any = None) -> None:
        self.sent.append({"to": list(to), "subject": subject, "body": body, "files": list(files or []),
                          "headers": dict(headers or {})})

    def to(self, address: str) -> list[dict[str, Any]]:
        return [m for m in self.sent if address in m["to"]]


class RefusingMailer:
    def __init__(self) -> None:
        self.tries = 0

    def send(self, *args: Any, **kwargs: Any) -> None:
        self.tries += 1
        raise ConnectionError("smtp down")


def ok(res: Any, status: int = 200) -> Any:
    assert res.status_code == status, (res.request.url, res.text)
    return res.json() if res.content else None


def events(h: Any, tenant: str) -> list[Event]:
    return [Event.parse(r) for r in h.store.events(tenant)]


def digest(manager: TenantManager, tenant: str) -> str:
    with manager.open(tenant) as rt:
        return state_digest(rt.service)


def replayed(h: Any, tenant: str, **services: Any) -> str:
    """The state another process rebuilds from the log alone (no mailer, no reader)."""
    return digest(TenantManager(h.store, h.objects, now=h.clock, strict_reads=True, **services), tenant)


def bank_csv(account: str, *rows: tuple[str, str, str, str]) -> bytes:
    lines = ["date,amount,counterparty,account,description,kind"]
    lines += [f"{day},{amount},{who},{account},{what},direct_debit" for day, amount, who, what in rows]
    return ("\n".join(lines) + "\n").encode()


def padaria(h: Any, *, accountant: bool = False) -> tuple[dict[str, Any], dict[str, str], str]:
    """Ana's Padaria Lda (NIF 516123459, the EDP invoice's customer) with its bank and EDP as a known supplier."""
    ana = signup(h.client)
    H = bearer(ana["token"])
    if accountant:
        ok(h.client.post("/api/onboarding/accountant", json={"email": ACCOUNTANT, "name": "Marc Vidal"}, headers=H))
    bank = ok(h.client.post("/api/sources", json={"kind": "bank", "bank": "Millennium BCP", "companyId": "padaria-lda",
                                                  "iban": IBAN}, headers=H))
    ok(h.client.post("/api/sources", json={"kind": "supplier", "name": "EDP", "taxId": "503504564",
                                           "email": "faturas@edp.pt"}, headers=H))
    return ana, H, bank["id"]


# =========================================================================== 1. supplier requests in production


def test_production_asks_suppliers_only_once_the_owner_switches_it_on_and_sends_through_the_send_path(
        tmp_path: Path) -> None:
    mailer = CapturingMailer()
    h = harness(tmp_path, mailer=mailer)
    ana, H, account = padaria(h)
    tenant = ana["tenant"]["id"]
    csv = bank_csv(account, ("2026-09-19", "-64.10", "EDP", "DD EDP COMERCIAL"))
    rows = ok(h.client.post("/api/evidence", files={"file": ("extrato.csv", csv, "text/csv")}, headers=H))
    [tx] = rows["transactions"]

    # A new production tenant: nothing may be asked of suppliers, and nothing is.
    settings = ok(h.client.get("/api/settings/automation", headers=H))
    items = {i["id"]: i for i in settings["items"]}
    assert items["supplierRequests"]["on"] is False and items["accountantReplies"]["on"] is False
    assert items["supplierRequests"]["label"] == "Ask suppliers for missing invoices"
    assert items["supplierRequests"]["level"] == "automatic_if_authorized"
    assert settings["summary"] == "I don't write to anyone on my own. Everything waits for you."
    assert "always need your yes" in settings["never"]
    assert mailer.to("faturas@edp.pt") == []
    assert ok(h.client.get(f"/api/transactions/{tx}", headers=H))["nextStep"] == \
        "I'm looking for the document for the €64.10 payment to EDP on 19 September."

    # Only valid switches: a plain refusal otherwise, and nothing changes.
    assert h.client.post("/api/settings/automation", json={"moveMoney": True}, headers=H).status_code == 400
    assert h.client.post("/api/settings/automation", json={"supplierRequests": "yes"}, headers=H).status_code == 400
    assert ok(h.client.get("/api/settings/automation", headers=H)) == settings and mailer.sent == []

    # The owner switches supplier requests on: recorded as the owner's event, and the request goes out at once.
    before = len(events(h, tenant))
    out = ok(h.client.post("/api/settings/automation", json={"supplierRequests": True}, headers=H))
    assert out["message"] == "Done. I will ask suppliers for missing invoices."
    assert {i["id"]: i["on"] for i in out["items"]} == {"supplierRequests": True, "accountantReplies": False,
                                                          "monthlyPackage": False}
    recorded = events(h, tenant)[before:]
    change = next(e for e in recorded if e.kind == "request")
    assert change.data["path"] == "/api/settings/automation" and change.actor == ana["user"]["id"]
    assert [e.kind for e in recorded][-1] == "outbox.send"  # the email in an event of its own, sent live
    [request] = mailer.to("faturas@edp.pt")
    assert "64,10" in request["body"] and request["headers"]["Message-ID"]
    activity = [a["text"] for a in ok(h.client.get("/api/activity", headers=H))["items"]]
    assert "Asked EDP for the invoice for the €64.10 payment." in activity
    assert ok(h.client.get(f"/api/transactions/{tx}", headers=H))["nextStep"].startswith(
        "I asked EDP for the invoice for the €64.10 payment on 19 September.")

    # Rebuilt from the log alone: the same state, and a replay never sends again.
    h.clock.step = h.clock.step * 0
    assert replayed(h, tenant) == digest(h.manager, tenant)
    assert len(mailer.to("faturas@edp.pt")) == 1

    # Switched off again: nothing more is written for suppliers.
    off = ok(h.client.post("/api/settings/automation", json={"supplierRequests": False}, headers=H))
    assert off["message"] == "Done. I won't ask suppliers for invoices."
    assert {i["id"]: i["on"] for i in off["items"]}["supplierRequests"] is False


def test_only_the_owner_changes_what_runs_on_its_own(tmp_path: Path) -> None:
    h = harness(tmp_path, mailer=CapturingMailer())
    ana, H, _ = padaria(h)
    tenant = ana["tenant"]["id"]
    bob = signup(h.client, "bob@contas.pt", company="Contas Bob", tax_id=NIF_C, name="Bob")
    h.store._d.memberships.discard((bob["tenant"]["id"], bob["user"]["id"], "owner"))
    h.store.add_membership(tenant, bob["user"]["id"], "accountant")
    B = bearer(ok(h.client.post("/api/auth/login", json={"email": "bob@contas.pt", "password": PASSWORD}))["token"])
    assert ok(h.client.get("/api/settings/automation", headers=B))["items"]  # the accountant may read it ...
    count = len(events(h, tenant))
    res = h.client.post("/api/settings/automation", json={"supplierRequests": True}, headers=B)
    assert res.status_code == 403  # ... never change it
    assert len(events(h, tenant)) == count
    assert ok(h.client.get("/api/settings/automation", headers=H))["items"][0]["on"] is False
    # One company only: the others keep their own setting.
    two = ok(h.client.post("/api/onboarding/company", json={"name": "Second Company", "taxId": "501234560"},
                           headers=H))
    company = two["company"]["id"]
    out = ok(h.client.post("/api/settings/automation", json={"accountantReplies": True, "companyId": company},
                           headers=H))
    replies = next(i for i in out["items"] if i["id"] == "accountantReplies")
    assert replies["on"] is False and replies["onFor"] == [company]
    assert out["message"] == "Done. I will answer your accountant's routine questions for Second Company."


def test_a_request_written_before_the_owner_switched_it_off_is_held_back_not_sent() -> None:
    o = supplier_tenant()
    o.repo.policy = o.repo.policy.with_grant(ActionKind.SUPPLIER_INVOICE_REQUEST, granted_by="ana@padaria.pt",
                                             at=local_datetime(date(2026, 9, 1), 9))
    pay(o, BankRow(bank_id="edp-0919", account_id="bank", booked_on=date(2026, 9, 19), amount=D("-64.10"),
                   counterparty="EDP COMERCIAL", description="DD EDP COMERCIAL", kind=K.DIRECT_DEBIT))
    o.run(local_datetime(date(2026, 9, 25), 9))
    [chase] = o.repo.chases.values()
    message = o.repo.outbox[chase.outbox_id]
    assert message.status == "waiting"  # no transport here
    svc = BackOfficeService(o)
    svc.automation_settings({"supplierRequests": False})
    assert o.held_back(message) and message.id not in svc.waiting_messages()
    outbox = SimulatedOutbox()
    o.transport = outbox
    o.run(local_datetime(date(2026, 9, 26), 9))
    assert outbox.accepted == [] and message.status == "waiting"
    rec = o.repo.transactions[chase.tx_id]
    assert o.missing.plan(rec) == ("I wrote to EDP asking for the invoice for the €64.10 payment on 19 September, "
                                   "but asking suppliers for invoices is switched off, so I have not sent it.")
    svc.automation_settings({"supplierRequests": True})
    assert message.status == "sent" and [m.to for m in outbox.accepted] == [("faturas@edp.pt",)]


# =========================================================================== 2. the monthly package


def test_working_day_three_skips_weekends_and_portuguese_holidays() -> None:
    assert working_day(2026, 10, 1) == date(2026, 10, 1)  # Thursday
    assert working_day(2026, 10, 2) == date(2026, 10, 2)  # Friday
    assert working_day(2026, 10, 3) == date(2026, 10, 6)  # the weekend, then 5 October (Republic Day)
    assert working_day(2026, 12, 1) == date(2026, 12, 2)  # 1 December is a holiday
    assert date(2027, 3, 26) not in {working_day(2027, 3, n) for n in range(1, 22)}  # Good Friday 2027
    assert working_day(2027, 1, 1) == date(2027, 1, 4)  # New Year's Day, then the weekend


def test_the_month_goes_to_the_accountant_on_its_working_day_and_is_confirmed_by_the_reply(tmp_path: Path) -> None:
    mailer = CapturingMailer()
    h = harness(tmp_path, mailer=mailer)
    ana, H, account = padaria(h, accountant=True)
    tenant = ana["tenant"]["id"]
    settings = ok(h.client.get("/api/settings/automation", headers=H))
    assert {i["id"]: i["on"] for i in settings["items"]}["monthlyPackage"] is True  # naming the accountant did it
    ok(h.client.post("/api/evidence", files={"file": ("edp.txt", E.EDP_INVOICE, "text/plain")}, headers=H))
    csv = bank_csv(account, ("2026-09-19", "-64.10", "EDP COMERCIAL", "DD EDP COMERCIAL"))
    ok(h.client.post("/api/evidence", files={"file": ("extrato.csv", csv, "text/csv")}, headers=H))
    ok(h.client.get("/api/home", headers=H))
    assert mailer.to(ACCOUNTANT) == []  # Friday 2 October is working day 2: not yet
    assert "delivery" not in ok(h.client.get("/api/months/padaria-lda/2026-09", headers=H))

    h.clock.advance(days=4)  # Tuesday 6 October: working day 3 (Monday 5 October is a public holiday)
    month = ok(h.client.get("/api/months/padaria-lda/2026-09", headers=H))  # the day's first request
    [sent] = mailer.to(ACCOUNTANT)
    assert sent["to"] == [ACCOUNTANT, "ana@example.pt"]  # the owner's copy, as the report settings say
    assert sent["subject"] == "Padaria Lda: September 2026 accounts"
    [(name, content_type, data)] = sent["files"]
    assert (name, content_type) == ("padaria-lda-2026-09.zip", "application/zip") and len(data) < 10 * 1024 * 1024
    assert verify_package(data) == []
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = archive.namelist()
        manifest = json.loads(archive.read("manifest.json"))
        ledger = archive.read("ledger.csv").decode()
    assert {"manifest.json", "ledger.csv", "evidence_index.csv"} <= set(names)
    assert any(n.startswith("evidence/") and n.endswith(".txt") for n in names)  # the EDP invoice, untouched
    assert "document_role" in ledger.splitlines()[0] and ",booked," in ledger
    assert manifest["period"]["month"] == "2026-09" and manifest["counts"]["items"] == 2
    assert "Please reply to this email to confirm you received it." in sent["body"]
    assert month["delivery"]["state"] == "sent" and month["delivery"]["sentOn"] == "2026-10-06"
    assert month["delivery"]["text"].startswith("September sent to your accountant on 6 October.")
    kinds = [e.kind for e in events(h, tenant)]
    assert kinds[-2:] == ["tick", "outbox.send"]  # written on the day's tick, sent in an event of its own
    view = ok(h.client.get("/api/accountant/clients/padaria-lda", headers=H))
    assert view["exportState"]["delivery"]["text"] == (
        "Sent to you on 6 October. Reply to that email to confirm you have it.")

    # The accountant's reply in that thread confirms it (Day +1).
    reply = E.email(sender=f"Marc Vidal <{ACCOUNTANT}>", subject=f"Re: {sent['subject']}", at=h.clock.now_,
                    text="Received, thank you.\nMarc\n", message_id="<ack-0610@vidal.pt>")
    message = reply.decode().replace("Message-ID: <ack-0610@vidal.pt>",
                                     f"Message-ID: <ack-0610@vidal.pt>\nIn-Reply-To: {sent['headers']['Message-ID']}")
    ok(h.client.post("/api/evidence", json={"filename": "ack.eml", "contentType": "message/rfc822",
                                            "dataBase64": base64.b64encode(message.encode()).decode()}, headers=H))
    month = ok(h.client.get("/api/months/padaria-lda/2026-09", headers=H))
    assert month["delivery"]["state"] == "confirmed" and month["delivery"]["confirmedOn"] == "2026-10-06"
    assert month["delivery"]["text"].endswith("They confirmed they have it.")
    activity = [a["text"] for a in ok(h.client.get("/api/activity", headers=H))["items"]]
    assert "September sent to your accountant." in activity
    assert "Your accountant confirmed they have September." in activity

    # Nothing is sent twice, and the log alone rebuilds the same state without sending anything.
    h.clock.advance(days=1)
    ok(h.client.get("/api/home", headers=H))
    assert len(mailer.to(ACCOUNTANT)) == 1
    h.clock.step = h.clock.step * 0
    assert replayed(h, tenant) == digest(h.manager, tenant)
    assert len(mailer.sent) == 1


def test_the_package_counts_as_sent_only_once_a_transport_accepted_it(tmp_path: Path) -> None:
    h = harness(tmp_path, mailer=RefusingMailer())
    ana, H, account = padaria(h, accountant=True)
    tenant = ana["tenant"]["id"]
    ok(h.client.post("/api/evidence", files={"file": ("edp.txt", E.EDP_INVOICE, "text/plain")}, headers=H))
    h.clock.advance(days=4)
    month = ok(h.client.get("/api/months/padaria-lda/2026-09", headers=H))
    assert month["delivery"]["state"] == "waiting"
    assert month["delivery"]["text"] == "September is ready for your accountant. It is waiting to be sent."
    assert "sent to your accountant" not in json.dumps(ok(h.client.get("/api/activity", headers=H)))
    # Another process with a working mailer sends it: only now is it delivered.
    mailer = CapturingMailer()
    up = TenantManager(h.store, h.objects, now=h.clock, mailer=mailer, strict_reads=True)
    up.command(tenant, ana["user"]["id"], "POST", "/api/tasks", {"title": "Check the package"})
    assert [m["subject"] for m in mailer.to(ACCOUNTANT)] == ["Padaria Lda: September 2026 accounts"]
    status, month = up.view(tenant, "GET", "/api/months/padaria-lda/2026-09")
    assert status == 200 and month["delivery"]["state"] == "sent"


def delivery_tenant(*, when_open: str = "send") -> tuple[Orchestrator, BackOfficeService, CapturingMailer]:
    """Hazel Tree with synced sources and its accountant; delivery allowed; a transport that keeps what it sent."""
    o = supplier_tenant()
    o.repo.accountant = AccountantProfile(id="acct-vidal", firm="Contabilidade Vidal", person="Marc Vidal",
                                          email=ACCOUNTANT)
    o.repo.policy = o.repo.policy.with_grant(ActionKind.DOCUMENT_DELIVERY, granted_by="ana@padaria.pt",
                                             at=local_datetime(date(2026, 9, 1), 9))
    transport = CapturingMailer()
    o.transport = transport
    svc = BackOfficeService(o)
    svc.report_settings({"whenOpen": when_open})
    return o, svc, transport


def test_a_month_with_open_items_goes_with_an_honest_note_and_the_final_package_follows() -> None:
    o, svc, transport = delivery_tenant()
    o.ingest_file(E.EDP_INVOICE, filename="edp.txt", content_type="text/plain",
                  at=local_datetime(date(2026, 9, 18), 10))
    pay(o, BankRow(bank_id="adobe-0922", account_id="bank", booked_on=date(2026, 9, 22), amount=D("-59.99"),
                   counterparty="ADOBE CREATIVE CLOUD", description="COMPRA", kind=K.CARD))
    o.run(local_datetime(date(2026, 10, 2), 9))
    assert transport.to(ACCOUNTANT) == []  # working day 2: not yet
    o.run(local_datetime(date(2026, 10, 6), 9))
    [first] = transport.to(ACCOUNTANT)
    assert "Not everything in September is settled yet. Still open:" in first["body"]
    assert "I will send the final package once these are settled." in first["body"]
    [(_, _, data)] = first["files"]
    manifest = json.loads(zipfile.ZipFile(io.BytesIO(data)).read("manifest.json"))
    assert manifest["complete"] is False and manifest["still_open"] == [
        line[2:] for line in first["body"].splitlines() if line.startswith("- ")]
    view = svc.month("hazel-tree", "2026-09")["delivery"]
    assert view["state"] == "sent" and view["stillOpen"] and not view["complete"]
    assert view["text"].startswith("September sent to your accountant on 6 October. It went with a note of what is "
                                   "still open")
    # The EDP payment and Adobe's invoice arrive: the month closes, and the final package goes once.
    pay(o, BankRow(bank_id="edp-0919", account_id="bank", booked_on=date(2026, 9, 19), amount=D("-64.10"),
                   counterparty="EDP COMERCIAL", description="DD EDP COMERCIAL", kind=K.DIRECT_DEBIT))
    o.ingest_file(E.ADOBE_INVOICE.replace(E.COMPANY_C_NIF.encode(), E.HAZEL_NIF.encode()), filename="adobe.txt",
                  content_type="text/plain", at=local_datetime(date(2026, 10, 7), 10))
    assert ("hazel-tree", "2026-09") in o.repo.closed_months
    sent = transport.to(ACCOUNTANT)
    assert [m["subject"] for m in sent] == ["Hazel Tree Interiores, Lda.: September 2026 accounts",
                                            "Hazel Tree Interiores, Lda.: September 2026 accounts (final)"]
    assert "Everything in September is settled: every payment has its evidence." in sent[1]["body"]
    final = json.loads(zipfile.ZipFile(io.BytesIO(sent[1]["files"][0][2])).read("manifest.json"))
    assert final["complete"] is True and "still_open" not in final and verify_package(sent[1]["files"][0][2]) == []
    o.run(local_datetime(date(2026, 10, 8), 9))
    assert len(transport.to(ACCOUNTANT)) == 2
    assert svc.month("hazel-tree", "2026-09")["delivery"]["text"] == (
        "The final September package went to your accountant on 7 October.")


def test_a_month_with_open_items_waits_when_the_owner_chose_so() -> None:
    o, svc, transport = delivery_tenant(when_open="wait")
    o.ingest_file(E.EDP_INVOICE, filename="edp.txt", content_type="text/plain",
                  at=local_datetime(date(2026, 9, 18), 10))
    o.run(local_datetime(date(2026, 10, 6), 9))
    assert o.repo.packages == {} and "delivery" not in svc.month("hazel-tree", "2026-09")
    pay(o, BankRow(bank_id="edp-0919", account_id="bank", booked_on=date(2026, 9, 19), amount=D("-64.10"),
                   counterparty="EDP COMERCIAL", description="DD EDP COMERCIAL", kind=K.DIRECT_DEBIT))
    o.run(local_datetime(date(2026, 10, 7), 9))
    [package] = o.repo.packages.values()
    assert package.complete and package.delivered and package.number == 1
    [mail] = transport.to(ACCOUNTANT)
    assert "Still open" not in mail["body"]
    assert svc.report_settings()["whenOpen"] == "wait"


def test_originals_too_large_for_an_email_are_left_out_and_the_email_says_where_they_are(
        monkeypatch: pytest.MonkeyPatch) -> None:
    import backoffice.package_delivery as delivery

    monkeypatch.setattr(delivery, "MAX_ATTACHMENT_BYTES", 2000)  # the originals would not fit
    o, svc, transport = delivery_tenant()
    o.ingest_file(E.EDP_INVOICE, filename="edp.txt", content_type="text/plain",
                  at=local_datetime(date(2026, 9, 18), 10))
    pay(o, BankRow(bank_id="edp-0919", account_id="bank", booked_on=date(2026, 9, 19), amount=D("-64.10"),
                   counterparty="EDP COMERCIAL", description="DD EDP COMERCIAL", kind=K.DIRECT_DEBIT))
    o.run(local_datetime(date(2026, 10, 6), 9))
    [mail] = transport.to(ACCOUNTANT)
    [(_, _, data)] = mail["files"]
    names = zipfile.ZipFile(io.BytesIO(data)).namelist()
    assert not any(n.startswith("evidence/") for n in names) and "evidence_index.csv" in names
    assert verify_package(data) == []
    assert "The original documents are too large to attach here" in mail["body"]
    assert "download them from your accountant workspace" in mail["body"]
    assert svc.month("hazel-tree", "2026-09")["delivery"]["originals"] is False


def test_without_the_owners_permission_no_package_is_sent() -> None:
    o, svc, transport = delivery_tenant()
    svc.automation_settings({"monthlyPackage": False})
    o.ingest_file(E.EDP_INVOICE, filename="edp.txt", content_type="text/plain",
                  at=local_datetime(date(2026, 9, 18), 10))
    o.run(local_datetime(date(2026, 10, 6), 9))
    assert o.repo.packages == {} and transport.sent == []
    # Naming the accountant again does not override the owner's own "off".
    svc.set_accountant(ACCOUNTANT, "Marc Vidal")
    o.run(local_datetime(date(2026, 10, 7), 9))
    assert o.repo.packages == {}


# =========================================================================== shared in-process tenant


def supplier_tenant() -> Orchestrator:
    """Hazel Tree (Lisbon) with its bank account, EDP and Adobe as known suppliers, sources synced."""
    owner = OwnerProfile(first_name="Ana", full_name="Ana Silva", email="ana@padaria.pt")
    repo = Repository(tenant_id="t-acceptance", owner=owner, now=local_datetime(date(2026, 9, 1), 7, 0))
    repo.add_company(id="hazel-tree", name="Hazel Tree", legal_name="Hazel Tree Interiores, Lda.",
                     tax_id=E.HAZEL_NIF, ibans=[E.HAZEL_IBAN])
    repo.add_account(Account(id="bank", bank="Millennium BCP", holder_id="hazel-tree", iban=E.HAZEL_IBAN))
    repo.add_supplier(Supplier(id="sup-edp", tenant_id=repo.tenant_id, name="EDP",
                               aliases=["EDP COMERCIAL", "EDP Comercial"], tax_id=E.EDP_NIF,
                               email_domains=["edp.pt"], countries=["PT"], contact_email="faturas@edp.pt"))
    repo.add_supplier(Supplier(id="sup-adobe", tenant_id=repo.tenant_id, name="Adobe",
                               aliases=["ADOBE CREATIVE CLOUD"], tax_id=E.ADOBE_NIF, countries=["PT"]))
    covered, synced = local_datetime(date(2026, 6, 1), 0, 0), local_datetime(date(2026, 10, 31), 8, 0)
    for id_, kind, name in (("gmail", "email", "Gmail"), ("millennium", "bank", "Millennium BCP")):
        repo.add_connector(ConnectorState(id=id_, name=name, kind=kind, account=name, company_ids=("hazel-tree",),
                                          healthy=True, covered_from=covered, covered_until=synced,
                                          last_synced_at=synced))
    return Orchestrator(repo)


def pay(o: Orchestrator, row: BankRow) -> Any:
    report = o.ingest_bank([row], at=local_datetime(row.booked_on, 18, 0))
    [tx_id] = report.transaction_ids
    return o.repo.transactions[tx_id]


# =========================================================================== 3. import purchase chains

PROFORMA = """Shenzhen Bright Lighting Co., Ltd.
No. 88 Keji Road, Nanshan District, Shenzhen, China
PROFORMA INVOICE PI-5521
Date: 2 September 2026
Buyer: Hazel Tree Interiores, Lda. (VAT PT516123459)
Purchase order: PO-2026-114
200 x LED pendant lamp model BL-40
Total: USD 4,800.00
Deposit 30% due before production: USD 1,440.00
"""
COMMERCIAL_INVOICE = """Shenzhen Bright Lighting Co., Ltd.
No. 88 Keji Road, Nanshan District, Shenzhen, China
COMMERCIAL INVOICE
Invoice No.: CI-8842
Invoice date: 20 September 2026
Buyer: Hazel Tree Interiores, Lda. (VAT PT516123459)
Your order: PO-2026-114
Container: MSCU 123456 5
B/L: MEDUSZ123456
200 x LED pendant lamp model BL-40
Total: USD 4,800.00
"""
FREIGHT = f"""Transitários Atlântico, Lda.
Rua do Porto 12, 4450-001 Matosinhos
NIF: 509876541
Fatura n.º FT TA2026/331
Data de emissão: 25/09/2026
Cliente: Hazel Tree Interiores, Lda.
NIF: {E.HAZEL_NIF}
Frete marítimo Shenzhen - Leixões
Conhecimento de embarque (B/L): MEDUSZ123456
Contentor: MSCU1234565
Base tributável (23%): 650,00
IVA 23%: 149,50
Total: 799,50 €
"""
CUSTOMS = f"""Autoridade Tributária e Aduaneira - Alfândega de Leixões
Declaração Aduaneira de Importação (DAU)
MRN: 26PT00001234567890
Data de aceitação: 26/09/2026
Importador: Hazel Tree Interiores, Lda.
NIF: {E.HAZEL_NIF}
Contentor: MSCU1234565
Fatura comercial: CI-8842
Valor aduaneiro: 4.420,00 €
Direitos aduaneiros: 176,80 €
IVA: 1.057,21 €
Total a pagar: 1.234,01 €
"""
MRN = "26PT00001234567890"
CHAIN_LINE = "Part of order PO-2026-114: pro-forma, deposit, invoice, freight, customs, duties"


def upload(o: Orchestrator, text: str, day: date, name: str) -> Any:
    return o.ingest_file(text.encode(), filename=name, content_type="text/plain", at=local_datetime(day, 10, 0))


def transfer(o: Orchestrator, bank_id: str, day: date, amount: str, who: str, what: str,
             reference: str | None = None) -> Any:
    return pay(o, BankRow(bank_id=bank_id, account_id="bank", booked_on=day, amount=D(amount), counterparty=who,
                          description=what, kind=K.TRANSFER_OUT, reference=reference))


def import_business() -> tuple[Orchestrator, BackOfficeService, dict[str, Any]]:
    o = supplier_tenant()
    o.repo.accountant = AccountantProfile(id="acct-vidal", firm="Contabilidade Vidal", person="Marc Vidal",
                                          email=ACCOUNTANT)
    [proforma] = upload(o, PROFORMA, date(2026, 9, 2), "proforma.txt").document_ids
    deposit = transfer(o, "dep", date(2026, 9, 3), "-1325.00", "SHENZHEN BRIGHT LIGHTING", "TRF DEPOSIT PO-2026-114")
    [invoice] = upload(o, COMMERCIAL_INVOICE, date(2026, 9, 20), "invoice.txt").document_ids
    [freight] = upload(o, FREIGHT, date(2026, 9, 25), "freight.txt").document_ids
    customs = upload(o, CUSTOMS, date(2026, 9, 26), "dau.txt")
    duties = transfer(o, "duty", date(2026, 9, 28), "-1234.01", "AUTORIDADE TRIBUTARIA E ADUANEIRA",
                      "PAG DUC IMPORTACAO", reference=MRN)
    # The same amount as the deposit, to someone else, quoting nothing: never linked by its amount.
    other = transfer(o, "other", date(2026, 9, 29), "-1325.00", "MOVEIS NORTE LDA", "TRF FATURA 88")
    o.run(local_datetime(date(2026, 10, 2), 9))
    return o, BackOfficeService(o), {"proforma": proforma, "deposit": deposit.id, "invoice": invoice,
                                     "freight": freight, "customs": customs, "duties": duties.id,
                                     "other": other.id}


def test_references_are_read_from_orders_mrns_containers_and_bills_of_lading() -> None:
    words = lambda text: sorted(r.words for r in references(text))  # noqa: E731
    assert words("Your order: PO-2026-114") == ["order PO-2026-114"]
    assert words("TRF DEPOSIT PO-2026-114") == ["order PO-2026-114"]
    assert words("Encomenda n.º 44718") == ["order 44718"]
    assert words("MRN: 26PT00001234567890") == ["MRN 26PT00001234567890"]
    assert words("Container: MSCU 123456 5") == words("Contentor: MSCU1234565") == ["container MSCU1234565"]
    assert words("Conhecimento de embarque (B/L): MEDUSZ123456") == ["bill of lading MEDUSZ123456"]
    # Ordinary words and dates are not references.
    for text in ("in order to pay", "order 12/09/2026", "Fatura n.º FT TA2026/331", "Total: 799,50 €",
                 "IBAN PT50 0033 0000 4532 8817 1026 5"):
        assert words(text) == [], text
    assert import_vat(CUSTOMS) == D("1057.21") and import_vat(FREIGHT) == D("149.50")


def test_a_foreign_purchase_is_one_chain_linked_by_its_references_only() -> None:
    o, svc, ids = import_business()
    assert ids["customs"].obligation_ids  # the declaration is read as a tax to pay, proved by the duties payment
    assert o.repo.items[o.repo.transactions[ids["duties"]].item_id].stage is Stage.CLOSED
    [chain] = find_chains(o.repo, text_of=o.evidence_text)
    assert chain.line == CHAIN_LINE and chain.order == "PO-2026-114" and chain.mrn == MRN
    # Visible from every piece: the payments and the documents.
    for detail in (svc.transaction(ids["duties"]), svc.transaction(ids["deposit"]), svc.document(ids["freight"]),
                   svc.document(ids["invoice"]), svc.document_detail(ids["proforma"])):
        assert detail["importChain"]["line"] == CHAIN_LINE
    view = svc.transaction(ids["duties"])["importChain"]
    pieces = {p["role"]: p for p in view["pieces"]}
    assert pieces["duties"]["linkedBy"] == [f"MRN {MRN}"] and pieces["customs"]["label"] == "Customs declaration"
    assert pieces["freight"]["linkedBy"] == ["bill of lading MEDUSZ123456", "container MSCU1234565"]
    assert pieces["deposit"]["linkedBy"] == ["order PO-2026-114"] and pieces["deposit"]["label"] == "Deposit paid"
    assert pieces["invoice"]["currency"] == "USD"
    # A payment of the deposit's amount to someone else, quoting nothing, stays on its own.
    assert "importChain" not in svc.transaction(ids["other"])
    assert not any(p["id"] == ids["other"] for p in view["pieces"])


def test_the_accountant_is_shown_the_import_vat_paid_at_customs_and_the_owner_is_not_asked() -> None:
    o, svc, ids = import_business()
    flags = svc.accountant_client("hazel-tree")["taxFlags"]
    [flag] = [f for f in flags if f["id"].startswith("t_import_vat_")]
    assert flag["title"] == "Import VAT paid at customs · €1,057.21"
    assert flag["detail"] == (f"Part of order PO-2026-114. The customs declaration (MRN {MRN}) charged €1,057.21 of "
                              "import VAT, paid to customs on 28 September. It is not on any supplier's invoice. "
                              "Whether and how it is deducted is your call.")
    assert not any("VAT" in json.dumps(item) for item in svc.needs_you()["items"])


def test_pieces_that_share_no_reference_are_never_linked_by_amount() -> None:
    o = supplier_tenant()
    upload(o, FREIGHT, date(2026, 9, 25), "freight.txt")
    upload(o, FREIGHT.replace("MEDUSZ123456", "MAEU99887766").replace("MSCU1234565", "TGHU7654321")
           .replace("TA2026/331", "TA2026/332"), date(2026, 9, 25), "freight-2.txt")
    transfer(o, "fr", date(2026, 9, 29), "-799.50", "TRANSITARIOS ATLANTICO", "TRF")
    assert len(o.repo.documents) == 2 and find_chains(o.repo, text_of=o.evidence_text) == []


# =========================================================================== 4. high volume

SUPPLIER_WORDS = ("Alfa", "Bravo", "Cedro", "Delta", "Eco", "Faro", "Gema", "Horta", "Iris", "Jade", "Kiwi", "Lago",
                  "Mar", "Norte", "Oliva", "Pinho", "Quinta", "Rio", "Sol", "Tejo", "Uva", "Vale", "Xisto", "Zimbro",
                  "Aurora", "Bosque", "Cais", "Duna", "Estrela", "Fonte")
VOLUME_TRANSACTIONS = 3000
VOLUME_DOCUMENTS = 1500
# Measured on the development container: about 18 s for the whole month in 10 batches (31 s before the fixes in
# learning/recurrence.py and orchestrator.py; over a minute when most payments went to one counterparty). The
# budget is about three times the measured time, for slower CI machines: a quadratic regression blows through it.
VOLUME_BUDGET_SECONDS = 55.0


def _nif(n: int) -> str:
    base = f"5{n:07d}"
    return base + str(nif_check_digit(base))


def _volume_invoice(i: int, supplier: Supplier, day: date) -> tuple[bytes, D]:
    total = D("12.00") + D(i) * D("0.13")
    net = (total / D("1.23")).quantize(D("0.01"))
    vat = total - net
    number = f"FT {supplier.id[-2:]}2026/{i + 1}"
    atcud = f"VOL{i:05d}X-{i + 1}"
    qr = E.qr_payload(A=supplier.tax_id, B=E.HAZEL_NIF, C="PT", D="FT", E="N", F=day.strftime("%Y%m%d"), G=number,
                      H=atcud, I1="PT", I7=f"{net}", I8=f"{vat}", N=f"{vat}", O=f"{total}", Q="abcd", R="1234")
    text = "\n".join([f"{supplier.name}, Lda.", f"NIF: {supplier.tax_id}", f"Fatura n.º {number}", f"ATCUD: {atcud}",
                      f"Data de emissão: {day:%d/%m/%Y}", "Cliente: Hazel Tree Interiores, Lda.",
                      f"NIF: {E.HAZEL_NIF}", "Mercadorias", f"Base tributável (23%): {str(net).replace('.', ',')}",
                      f"IVA 23%: {str(vat).replace('.', ',')}", f"Total: {str(total).replace('.', ',')} €",
                      f"Código QR: {qr}", ""])
    return text.encode(), total


def test_a_month_of_3000_payments_and_1500_documents_runs_within_its_time_budget() -> None:
    o = supplier_tenant()
    suppliers = []
    for s, word in enumerate(SUPPLIER_WORDS):
        supplier = Supplier(id=f"sup-v{s:02d}", tenant_id=o.repo.tenant_id, name=f"Fornecedor {word}",
                            aliases=[f"FORNECEDOR {word.upper()} LDA"], tax_id=_nif(200 + s), countries=["PT"])
        o.repo.add_supplier(supplier)
        suppliers.append(supplier)
    o.repo.add_account(Account(id="card-1111", bank="Millennium BCP", holder_id="hazel-tree", card_last4="1111"))
    batches, per_batch_docs = 10, VOLUME_DOCUMENTS // 10
    per_batch_other = (VOLUME_TRANSACTIONS - VOLUME_DOCUMENTS) // batches
    paid: dict[str, D] = {}
    resent: list[tuple[str, bytes]] = []
    started = time.perf_counter()
    i = 0
    for b in range(batches):
        day = date(2026, 9, 1 + 3 * b)
        rows, files = [], []
        for _ in range(per_batch_docs):
            supplier = suppliers[i % len(suppliers)]
            text, total = _volume_invoice(i, supplier, day)
            files.append((f"fatura-{i:05d}.txt", text))
            if i % 30 == 0:  # the same invoice again later, as a second copy ("2.ª via")
                resent.append((f"copia-{i:05d}.txt", text + "2.ª via\n".encode()))
            rows.append(BankRow(bank_id=f"card-{i:05d}", account_id="card-1111", booked_on=day, amount=-total,
                                counterparty=f"FORNECEDOR {supplier.name.split()[-1].upper()} LDA",
                                description="COMPRA CARTAO", kind=K.CARD, card_last4="1111"))
            paid[f"card-{i:05d}"] = total
            i += 1
        for j in range(per_batch_other):
            m = b * per_batch_other + j
            if m % 3 == 0:
                rows.append(BankRow(bank_id=f"in-{m:05d}", account_id="bank", booked_on=day,
                                    amount=D("50.00") + D(m), counterparty=f"CLIENTE {m % 90:02d}",
                                    description="TRF DE CLIENTE", kind=K.TRANSFER_IN))
            elif m % 3 == 1:
                rows.append(BankRow(bank_id=f"fee-{m:05d}", account_id="bank", booked_on=day, amount=D("-1.50"),
                                    counterparty="MILLENNIUM BCP", description="COMISSAO MANUTENCAO CONTA",
                                    kind=K.FEE))
            else:
                rows.append(BankRow(bank_id=f"tpa-{m:05d}", account_id="bank", booked_on=day,
                                    amount=D("100.00") + D(m), counterparty="SIBS TPA", description="TPA VENDAS",
                                    kind=K.TRANSFER_IN))
        o.ingest_bank(rows, at=local_datetime(day, 18, 0))
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as z:
            for name, data in files:
                z.writestr(name, data)
        o.ingest_file(archive.getvalue(), filename=f"faturas-{day}.zip", content_type="application/zip",
                      at=local_datetime(day, 19, 0))
    copies = io.BytesIO()
    with zipfile.ZipFile(copies, "w") as z:
        for name, data in resent:
            z.writestr(name, data)
    o.ingest_file(copies.getvalue(), filename="copias.zip", content_type="application/zip",
                  at=local_datetime(date(2026, 9, 30), 12, 0))
    elapsed = time.perf_counter() - started
    repo = o.repo
    assert len(repo.transactions) == VOLUME_TRANSACTIONS
    assert len(repo.documents) == VOLUME_DOCUMENTS  # the copies joined their invoices: nothing twice
    assert sum(len(d.evidence_ids) for d in repo.documents.values()) == VOLUME_DOCUMENTS + len(resent)
    card = [r for r in repo.transactions.values() if r.tx.account_id == "card-1111"]
    assert len(card) == VOLUME_DOCUMENTS
    for rec in card:  # every exact match closed: each payment with its own invoice, and that invoice closed too
        assert len(rec.document_ids) == 1 and repo.items[rec.item_id].stage is Stage.CLOSED, rec.tx.counterparty
        doc = repo.documents[rec.document_ids[0]]
        assert doc.matched_tx_ids == [rec.id] and abs(doc.document.gross_amount) == -rec.tx.amount
        assert repo.items[doc.item_id].stage is Stage.CLOSED
    assert len({r.document_ids[0] for r in card}) == VOLUME_DOCUMENTS
    assert elapsed < VOLUME_BUDGET_SECONDS, f"{elapsed:.1f}s for a month of {VOLUME_TRANSACTIONS} payments"


# =========================================================================== 5 and 6. photos from the phone


def png(width: int, height: int, tag: str) -> bytes:
    """A PNG header with this size (what the reader's own quality estimate reads) and a tag making it unique."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"tEXt", b"Comment\x00" + tag.encode())
            + chunk(b"IDAT", zlib.compress(b"\x00" * 16)) + chunk(b"IEND", b""))


EDP_LINES = E.EDP_INVOICE.decode().splitlines()
EDP_READ = "\n".join(line for line in EDP_LINES if not line.startswith("Código QR"))  # what the camera reads
# The same invoice, photographed with its number (and the ATCUD beside it) smudged: nothing names it.
EDP_NO_NUMBER = "\n".join(line for line in EDP_LINES
                          if not line.startswith(("Código QR", "Fatura n.º", "ATCUD", "Data de vencimento"))).replace(
    "EDP Comercial - Comercialização de Energia, S.A.", "EDP Comercial - Comercialização de Energia, S.A.\nFATURA")


class Pages:
    """A scripted camera: what the OCR engine reads on each image, by its bytes."""

    def __init__(self) -> None:
        self.texts: dict[bytes, str] = {}

    def add(self, data: bytes, text: str) -> bytes:
        self.texts[data] = text
        return data

    def engine(self, name: str = PP_OCR_V6_MEDIUM, *, readable: bool = True) -> FakeOCRProvider:
        return FakeOCRProvider(name, capabilities=OCRCapabilities(accepts_pdf=True),
                               transcribe=lambda page: self.texts.get(page.data, "") if readable else "~ ~ ~")


def phone_upload(h: Any, H: dict[str, str], data: bytes, key: str, **fields: str) -> dict[str, Any]:
    return ok(h.client.post("/api/evidence/upload", data={"sha256": sha(data), "source": "mobile_scan", **fields},
                            files={"file": (f"{key}.png", data, "image/png")},
                            headers={**H, "Idempotency-Key": f"capture-{key}-000001"}))


def emailed_pdf() -> bytes:
    message = EmailMessage()
    message["From"], message["To"], message["Subject"] = "faturas@edp.pt", "ana@example.pt", "Fatura EDP"
    message["Message-ID"] = "<fatura-558120@edp.pt>"
    message["Date"] = "Fri, 18 Sep 2026 10:00:00 +0100"
    message.set_content("Segue em anexo a sua fatura.")
    message.add_attachment((FIXTURES / "edp-ft-558120.pdf").read_bytes(), maintype="application", subtype="pdf",
                           filename="FT_EDP2026_558120.pdf")
    return message.as_bytes()


def test_a_photographed_invoice_and_the_emailed_pdf_are_one_document_and_an_unreadable_number_is_one_question(
        tmp_path: Path) -> None:
    pytest.importorskip("pypdf")
    camera = Pages()
    reader = DocumentReader(registry=EngineRegistry([camera.engine()]), qr_decoder=None)
    h = harness(tmp_path, reader=reader)
    ana = signup(h.client)  # Padaria Lda, NIF 516123459: the EDP invoice's customer
    H, tenant = bearer(ana["token"]), ana["tenant"]["id"]
    mail = ok(h.client.post("/api/evidence", json={"filename": "fatura.eml", "contentType": "message/rfc822",
                                                   "dataBase64": base64.b64encode(emailed_pdf()).decode()}, headers=H))
    [pdf_doc] = mail["documents"]
    # The paper copy, photographed: the number is read, so it is the same invoice. One document, both originals.
    photo = camera.add(png(1200, 1600, "paper copy"), EDP_READ)
    out = phone_upload(h, H, photo, "paper")
    assert out["status"] == "stored"
    docs = ok(h.client.get("/api/documents", headers=H))["items"]
    assert [d["id"] for d in docs] == [pdf_doc["id"]]
    detail = ok(h.client.get(f"/api/documents/{pdf_doc['id']}", headers=H))
    assert len(detail["evidenceIds"]) == 2 and out["evidence_id"] in detail["evidenceIds"]
    for evidence_id in detail["evidenceIds"]:  # both originals are kept, as they arrived
        assert h.client.get(f"/api/evidence/{evidence_id}/file", headers=H).status_code == 200

    # Another photo: supplier, date and total match, but the number can't be read. No second expense: one question.
    smudged = camera.add(png(1200, 1600, "smudged copy"), EDP_NO_NUMBER)
    out = phone_upload(h, H, smudged, "smudged")
    assert out["message"] == ("Got it. This looks like the EDP Comercial invoice FT EDP2026/558120 you already have. "
                              "I asked you in Needs you whether it is the same one.")
    assert [d["id"] for d in ok(h.client.get("/api/documents", headers=H))["items"]] == [pdf_doc["id"]]
    [card] = [n for n in ok(h.client.get("/api/needs-you", headers=H))["items"] if n["id"].endswith("_copy")]
    assert card["question"] == "Is this photo the same EDP Comercial invoice FT EDP2026/558120 (€64.10, 18 September)?"
    assert [o["label"] for o in card["options"]] == ["Yes, it's the same invoice", "No, it's a different purchase"]
    assert "I couldn't read the invoice number on this photo." in card["why"]
    month = ok(h.client.get("/api/months/padaria-lda/2026-09", headers=H))
    assert month["stats"]["documentsCollected"] <= 1

    answer = ok(h.client.post(f"/api/needs-you/{card['id']}/answer", json={"optionId": "same"}, headers=H))
    assert answer["message"] == ("Done. I kept it with the EDP Comercial invoice FT EDP2026/558120. It is one "
                                 "document, and every original is kept.")
    detail = ok(h.client.get(f"/api/documents/{pdf_doc['id']}", headers=H))
    assert len(detail["evidenceIds"]) == 3
    assert [d["id"] for d in ok(h.client.get("/api/documents", headers=H))["items"]] == [pdf_doc["id"]]

    h.clock.step = h.clock.step * 0
    assert replayed(h, tenant) == digest(h.manager, tenant)  # read once, before recording; never on replay


def test_a_copy_is_joined_on_its_own_only_when_the_same_atcud_proves_it() -> None:
    o = supplier_tenant()
    [first] = upload(o, E.EDP_INVOICE.decode(), date(2026, 9, 18), "edp.txt").document_ids
    # No number and no QR code, but the same ATCUD: the rules prove it is the same document.
    proven = "\n".join(line for line in EDP_LINES
                       if not line.startswith(("Código QR", "Fatura n.º", "Data de vencimento")))
    report = upload(o, proven.replace("Energia, S.A.", "Energia, S.A.\nFATURA"), date(2026, 9, 20), "copia.txt")
    assert report.document_ids == [first] and report.needs_ids == []
    assert len(o.repo.documents[first].evidence_ids) == 2 and len(o.repo.documents) == 1
    # No number, no ATCUD: asked, never guessed; "different" makes it a document of its own.
    report = upload(o, EDP_NO_NUMBER, date(2026, 9, 21), "outra.txt")
    [needs_id] = report.needs_ids
    assert report.document_ids == [] and len(o.repo.documents) == 1
    outcome = o.answer(needs_id, "different")
    assert outcome.message == "Done. I recorded it as a separate purchase." and len(o.repo.documents) == 2


def test_a_blurry_photo_goes_to_the_stronger_engine(tmp_path: Path) -> None:
    camera, sharp_only = Pages(), Pages()
    primary = sharp_only.engine()  # the fast engine gets nothing out of the blurry photo
    stronger = camera.engine(PADDLEOCR_VL)
    reader = DocumentReader(registry=EngineRegistry([primary, stronger]), qr_decoder=None)
    h = harness(tmp_path, reader=reader)
    ana = signup(h.client)
    H, tenant = bearer(ana["token"]), ana["tenant"]["id"]
    photo = camera.add(png(1200, 1600, "blurry receipt"), EDP_READ)
    out = phone_upload(h, H, photo, "blurry", hints=json.dumps({"quality": ["blurry"]}))
    assert len(stronger.calls) == 1 and len(primary.calls) == 1
    [doc] = ok(h.client.get("/api/documents", headers=H))["items"]
    assert doc["number"] == "FT EDP2026/558120"
    with h.manager.open(tenant) as rt:
        reading = rt.service.repo.reads[out["evidence_id"]]
        evidence = rt.service.repo.evidence(out["evidence_id"])
    steps = {s.step: s for s in reading.steps}
    stronger_step = steps["ocr_complex_layout"]
    assert "poor_image:blurry" in stronger_step.detail and stronger_step.state is StepState.DONE
    assert reading.image_quality == ("blurry",)
    assert evidence.metadata["capture"] == {"quality": ["blurry"]}  # the phone's hints are kept with the photo
    # A sharp photo, nothing flagged, that the fast engine reads: the fast engine alone.
    sharp = sharp_only.add(png(1200, 1600, "sharp receipt"),
                           (FIXTURES / "central-fs-cc2026-3317.txt").read_text(encoding="utf-8"))
    phone_upload(h, H, sharp, "sharp")
    assert len(stronger.calls) == 1 and len(primary.calls) == 2
    assert len(ok(h.client.get("/api/documents", headers=H))["items"]) == 2


def test_a_photo_still_unreadable_becomes_one_plain_task_to_take_it_again(tmp_path: Path) -> None:
    camera = Pages()
    reader = DocumentReader(registry=EngineRegistry([camera.engine(readable=False),
                                                     camera.engine(PADDLEOCR_VL, readable=False)]), qr_decoder=None)
    h = harness(tmp_path, reader=reader)
    ana = signup(h.client)
    H, tenant = bearer(ana["token"]), ana["tenant"]["id"]
    photo = camera.add(png(1200, 1600, "very blurry"), "")
    out = phone_upload(h, H, photo, "very-blurry", hints=json.dumps({"quality": ["blurry", "glare"]}))
    assert out["message"] == ("Got it, but I can't read it. This photo of a receipt is too blurred to read. "
                              "Take it again?")
    [task] = [n for n in ok(h.client.get("/api/needs-you", headers=H))["items"] if n["id"].startswith("nd_retake")]
    assert task["question"] == "This photo of a receipt is too blurred to read. Take it again?"
    assert [o["label"] for o in task["options"]] == ["I'll take it again", "It isn't a receipt. Leave it."]
    assert task["merchant"] == "Photo of a receipt" and task["amount"] is None
    with h.manager.open(tenant) as rt:
        reading = rt.service.repo.reads[out["evidence_id"]]
    assert reading.needs_person
    assert [s.state for s in reading.steps if s.step == "ocr_human"] == [StepState.NEEDS_PERSON]
    assert ok(h.client.get("/api/documents", headers=H))["items"] == []
    home = ok(h.client.get("/api/home", headers=H))
    assert home["needsYouCount"] >= 1
    done = ok(h.client.post(f"/api/needs-you/{task['id']}/answer", json={"optionId": "retake"}, headers=H))
    assert done["message"] == "Done. Send me the new photo when you have it."
    h.clock.step = h.clock.step * 0
    assert replayed(h, tenant) == digest(h.manager, tenant)


def test_the_pages_of_one_scan_are_read_together_as_one_document(tmp_path: Path) -> None:
    camera = Pages()
    reader = DocumentReader(registry=EngineRegistry([camera.engine()]), qr_decoder=None)
    h = harness(tmp_path, reader=reader)
    ana = signup(h.client)
    H, tenant = bearer(ana["token"]), ana["tenant"]["id"]
    top, bottom = EDP_READ.split("Cliente:")
    page1 = camera.add(png(1200, 1600, "page one"), top)
    page2 = camera.add(png(1200, 1600, "page two"), "Cliente:" + bottom)
    first = phone_upload(h, H, page1, "p1", capture_id="scan-7f3a", page="1", page_count="2")
    assert first["message"] == "Got it. I'll read it once the other pages of this scan are here."
    assert ok(h.client.get("/api/documents", headers=H))["items"] == []
    second = phone_upload(h, H, page2, "p2", capture_id="scan-7f3a", page="2", page_count="2")
    [doc] = ok(h.client.get("/api/documents", headers=H))["items"]
    detail = ok(h.client.get(f"/api/documents/{doc['id']}", headers=H))
    assert detail["evidenceIds"] == [first["evidence_id"], second["evidence_id"]]
    assert detail["number"] == "FT EDP2026/558120" and detail["amount"] == 64.1  # the total is on page 2
    with h.manager.open(tenant) as rt:  # what the phone said about the capture is kept with each page
        assert rt.service.repo.evidence(second["evidence_id"]).metadata["capture"] == {
            "id": "scan-7f3a", "page": 2, "pageCount": 2}
    # A scan whose second page never comes is read with the page it has, after a day.
    lonely = camera.add(png(1200, 1600, "lonely page"), (FIXTURES / "central-fs-cc2026-3317.txt").read_text("utf-8"))
    out = phone_upload(h, H, lonely, "lonely", capture_id="scan-9c1d", page="1", page_count="2")
    assert len(ok(h.client.get("/api/documents", headers=H))["items"]) == 1
    h.clock.advance(days=1, hours=1)
    ok(h.client.get("/api/home", headers=H))  # the day's first request: its daily run
    documents = ok(h.client.get("/api/documents", headers=H))["items"]
    assert len(documents) == 2 and out["evidence_id"] in json.dumps(documents)
    h.clock.step = h.clock.step * 0
    assert replayed(h, tenant) == digest(h.manager, tenant)
