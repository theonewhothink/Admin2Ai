"""Acceptance: employee cards and staff expenses (checks X16, X17).

Cases 13 (plumber), 41 (cleaning), 43 (building maintenance), 1 (café), 6 (salon), 19 (hostel),
27 (recruitment), 35 (courier): technicians, cleaners and drivers pay with company cards, and staff pay
small things with their own money.

* A company card belongs to an employee: the owner says so, or the bank's card details name the holder.
* A card payment without its receipt is asked from that employee, not the owner and not the supplier,
  through the send path (asked only once a transport accepted it), with reminders on the chase cadence.
  Their reply is matched by its thread and closes the payment. Only when they have not sent it after the
  reminders does the owner see one plain line; never before.
* In production an ``employee`` membership sees only their own open card payments and uploads receipts.
* A receipt an employee paid themselves is an expense claim: the owner's one tap (never automatic), then
  the transfer paying them back closes it. Its cost is counted once; the transfer is not a second cost.
"""

from __future__ import annotations

import base64
import re
from datetime import date, datetime
from decimal import Decimal
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from backoffice.countries.pt.nif import validate_nif
from backoffice.demo.evidence import qr_payload
from backoffice.domain.lifecycle import Stage
from backoffice.domain.models import TransactionKind
from backoffice.language import find_jargon, find_off_tone
from backoffice.orchestrator import TZ, BankRow, local_datetime
from backoffice.policy import ActionContext, ActionKind, authorize
from backoffice.service import BackOfficeService
from backoffice.spending import Ledger
from backoffice.staff import StaffAgent

NOW = datetime(2026, 9, 18, 9, 0, tzinfo=TZ)
COMPANY = "516123459"
OWNER = "ana@obras.pt"
RUI = "rui@obras.pt"


def nif(prefix8: str) -> str:
    """A Portuguese tax number with a valid check digit."""
    total = sum(int(d) * w for d, w in zip(prefix8, range(9, 1, -1), strict=True))
    check = 11 - total % 11
    number = prefix8 + str(0 if check >= 10 else check)
    assert validate_nif(number).valid, number
    return number


LEROY = nif("50328047")
CAFE = nif("51234568")
GALP = nif("50494212")


def receipt(shop: str, shop_nif: str, number: str, day: date, net: str, vat: str, *, cash: bool = False,
            customer: str = COMPANY) -> bytes:
    """A Portuguese simplified invoice (the till receipt) with its fiscal QR code (23% VAT)."""
    gross = Decimal(net) + Decimal(vat)
    seq = number.rsplit("/", 1)[-1]
    qr = qr_payload(A=shop_nif, B=customer, C="PT", D="FS", E="N", F=day.strftime("%Y%m%d"), G=number,
                    H=f"CSDF7T5H-{seq}", I1="PT", I7=net, I8=vat, N=vat, O=f"{gross:.2f}", Q="e1Dk", R="1422")
    lines = [shop, f"NIF: {shop_nif}", f"Fatura simplificada n.º {number}", f"ATCUD: CSDF7T5H-{seq}",
             f"Data: {day:%d/%m/%Y}", f"NIF cliente: {customer}", f"Base tributável (23%): {net.replace('.', ',')}",
             f"IVA 23%: {vat.replace('.', ',')}", f"Total: {str(gross).replace('.', ',')} €",
             *(["Pago em numerário"] if cash else []), f"Código QR: {qr}", ""]
    return "\n".join(lines).encode()


LEROY_RECEIPT = receipt("Leroy Merlin Portugal", LEROY, "FS 2026/88", date(2026, 9, 18), "39.19", "9.01")
CAFE_RECEIPT = receipt("Café Central", CAFE, "FS 2026/12", date(2026, 9, 20), "19.02", "4.38", cash=True)


class Transport:
    """A mail transport (backoffice.mailer): accepts every message, or refuses every one."""

    simulated = False

    def __init__(self, *, refuse: bool = False) -> None:
        self.refuse = refuse
        self.accepted: list[dict[str, Any]] = []

    def send(self, to: list[str], subject: str, body: str, files: list[Any], headers: Any = None) -> None:
        if self.refuse:
            raise OSError("the mail server refused the connection")
        self.accepted.append({"to": list(to), "subject": subject, "body": body, "headers": dict(headers or {})})


def ok(result: tuple[int, dict[str, Any]], status: int = 200) -> dict[str, Any]:
    code, body = result
    assert code == status, body
    return body


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def plain(*texts: str) -> None:
    for text in texts:
        assert find_jargon(text) == [], text
        assert find_off_tone(text) == [], text


class Business:
    """Obras Silva: Ana owns it; Rui holds company card 4817; Ana's own card is 9001."""

    def __init__(self, *, transport: Any = None, rui_card: bool = True) -> None:
        self.svc = BackOfficeService.new_tenant("t-staff", owner_name="Ana Silva", owner_email=OWNER, now=NOW)
        self.svc.add_company("Obras Silva", COMPANY)
        self.svc.orchestrator.transport = transport
        self.bank = self.post("/api/sources", {"kind": "bank", "bank": "Millennium BCP", "companyId": "obras-silva",
                                               "iban": "PT50000201231234567890154"})["id"]
        # Leroy Merlin has an address on file and supplier requests are allowed: a payment on Ana's own card
        # is asked from Leroy Merlin; one on Rui's card is asked from Rui.
        self.post("/api/sources", {"kind": "supplier", "name": "Leroy Merlin", "taxId": LEROY,
                                   "email": "faturas@leroymerlin.pt"})
        repo = self.svc.repo
        repo.policy = repo.policy.with_grant(ActionKind.SUPPLIER_INVOICE_REQUEST, granted_by=OWNER, at=NOW)
        self.cards: dict[str, str] = {}
        self.card("9001")
        if rui_card:
            self.card("4817", holderName="Rui Costa", holderEmail=RUI)

    @property
    def repo(self) -> Any:
        return self.svc.repo

    def get(self, path: str) -> dict[str, Any]:
        return ok(self.svc.dispatch("GET", path, None))

    def post(self, path: str, body: dict[str, Any], status: int = 200) -> dict[str, Any]:
        return ok(self.svc.dispatch("POST", path, body), status)

    def card(self, last4: str, **holder: str) -> dict[str, Any]:
        out = self.post("/api/sources", {"kind": "card", "bank": "Millennium BCP", "companyId": "obras-silva",
                                         "last4": last4, **holder})
        self.cards[last4] = out["id"]
        return out

    def run(self, month: int, day: int, hour: int = 9) -> None:
        self.svc.orchestrator.run(local_datetime(date(2026, month, day), hour))

    def pay(self, day: date, amount: str, counterparty: str, *, card: str | None = None,
            kind: TransactionKind | None = None, cardholder: str | None = None) -> str:
        row = BankRow(bank_id=f"b-{counterparty}-{day}-{amount}-{card}", account_id=self.cards[card] if card else
                      self.bank, booked_on=day, amount=Decimal(amount), counterparty=counterparty,
                      kind=kind or (TransactionKind.CARD if card else TransactionKind.TRANSFER_OUT), card_last4=card,
                      cardholder=cardholder)
        report = self.svc.orchestrator.ingest_bank([row], at=max(self.repo.clock.now(), local_datetime(day, 10)))
        return report.transaction_ids[0]

    def plan(self, tx_id: str) -> str:
        return self.svc.orchestrator.missing.plan(self.repo.transactions[tx_id])

    def stage(self, tx_id: str) -> Stage:
        return self.repo.items[self.repo.transactions[tx_id].item_id].stage

    def outbox(self, kind: str) -> list[Any]:
        return [m for m in self.repo.outbox.values() if m.kind == kind]

    def needs(self) -> list[dict[str, Any]]:
        return self.get("/api/needs-you")["items"]

    def spending(self, question: str = "What did we spend in September?") -> str:
        return self.post("/api/ask", {"question": question})["answer"]

    def email(self, raw: bytes) -> dict[str, Any]:
        return self.post("/api/evidence", {"filename": "reply.eml", "contentType": "message/rfc822",
                                           "dataBase64": b64(raw)})


def reply(thread: Any, *, sender: str = f"Rui Costa <{RUI}>", attachment: bytes | None = LEROY_RECEIPT,
          headers: bool = True, subject: str | None = None, message_id: str = "<reply-1@obras.pt>") -> bytes:
    """Rui answering the request: his mail app sets In-Reply-To and References, and keeps the subject."""
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = OWNER
    msg["Subject"] = subject or f"Re: {thread.subject}"
    msg["Date"] = format_datetime(local_datetime(date(2026, 9, 23), 8))
    msg["Message-ID"] = message_id
    if headers:
        msg["In-Reply-To"] = thread.sent[-1].message_id
        msg["References"] = " ".join(thread.message_ids)
    msg.set_content("Here it is.")
    if attachment is not None:
        msg.add_attachment(attachment, maintype="text", subtype="plain", filename="receipt.txt")
    return msg.as_bytes()


# =========================================================================== X16: cards and cardholders


def test_a_card_is_assigned_to_an_employee_by_the_owner_or_learned_from_the_bank_card_details() -> None:
    biz = Business()
    rui = next(e for e in biz.get("/api/employees")["employees"] if e["email"] == RUI)
    assert (rui["name"], [c["last4"] for c in rui["cards"]], rui["learnedFromBank"]) == ("Rui Costa", ["4817"], False)
    cards = next(g for g in biz.get("/api/sources")["groups"] if g["id"] == "cards")["items"]
    assert any(c["name"] == "Card •••• 4817" and "Rui Costa's card" in c["detail"] for c in cards)
    assert not any("•••• 9001" in c["name"] and "'s card" in c["detail"] for c in cards)

    # The bank's card details name the holder of card 5530: learned for that person, with the bank line as evidence.
    biz.card("5530")
    tx = biz.pay(date(2026, 9, 18), "-31.90", "GALP ALFRAGIDE", card="5530", cardholder="MARTA REIS")
    marta = next(e for e in biz.repo.employees.values() if "5530" in e.cards)
    assert marta.name == "Marta Reis" and marta.learned_from == (biz.repo.transactions[tx].evidence_id,)
    # (A new business's first run also says what it learned overall: only the card lines matter here.)
    learned = [a.text for a in biz.repo.activity if a.kind == "learned" and "card" in a.text]
    assert learned == ["Learned from your bank: card •••• 5530 is Marta Reis's card. Add Marta's email so I can ask "
                       "Marta for its receipts."]
    biz.run(9, 22)
    assert biz.plan(tx) == ("The €31.90 payment at GALP Alfragide on 18 September was made with Marta Reis's card. "
                            "Add Marta's email so I can ask Marta for the receipt.")
    assert not biz.outbox(StaffAgent.REQUEST)
    biz.post(f"/api/employees/{marta.id}", {"email": "marta@obras.pt"})
    assert [m.to for m in biz.outbox(StaffAgent.REQUEST)] == ["marta@obras.pt"]

    # The owner's word wins over a bank line; the owner's own card is never an employee's.
    biz.pay(date(2026, 9, 19), "-12.00", "LEROY MERLIN", card="4817", cardholder="MARTA REIS")
    biz.pay(date(2026, 9, 19), "-15.00", "LEROY MERLIN", card="9001", cardholder="ANA SILVA")
    staff = biz.svc.orchestrator.staff
    assert staff.holder_of("4817").email == RUI and staff.holder_of("9001") is None
    assert sorted(e.name for e in biz.repo.employees.values()) == ["Marta Reis", "Rui Costa"]
    # Card 5530 goes to Rui: Marta is not asked any more, Rui is; and it is never learned back for Marta.
    biz.post("/api/employees/emp_rui_costa", {"cards": ["4817", "5530"]})
    biz.pay(date(2026, 9, 20), "-9.90", "GALP ALFRAGIDE", card="5530", cardholder="MARTA REIS")
    assert staff.holder_of("5530").email == RUI and marta.cards == () and marta.not_cards == ("5530",)
    request = biz.repo.receipt_requests[tx]
    assert (request.employee_id, request.messages[0].to) == ("emp_rui_costa", RUI)
    # A holder that is not right changes nothing: the card is not added.
    code, body = biz.svc.dispatch("POST", "/api/sources", {"kind": "card", "bank": "Millennium BCP",
                                                          "companyId": "obras-silva", "last4": "7777",
                                                          "holderName": "Joana", "holderEmail": "not an address"})
    assert code == 400 and body["message"] == "That doesn't look like an email address."
    assert not [a for a in biz.repo.accounts.values() if a.card_last4 == "7777"]
    plain(*learned, biz.plan(tx))


# =========================================================================== X16: asking the cardholder


def test_a_missing_card_receipt_is_asked_from_the_cardholder_through_the_send_path_and_counts_only_when_accepted() -> None:
    biz = Business(transport=None)  # no transport yet: written, never "asked"
    rui_tx = biz.pay(date(2026, 9, 18), "-48.20", "LEROY MERLIN", card="4817")
    own_tx = biz.pay(date(2026, 9, 18), "-35.10", "LEROY MERLIN", card="9001")
    assert biz.plan(rui_tx) == ("The €48.20 payment at Leroy Merlin on 18 September was made with Rui Costa's card. "
                                "If the receipt does not arrive, I will ask Rui for it.")
    biz.run(9, 21)
    [request] = biz.outbox(StaffAgent.REQUEST)
    assert (request.to, request.subject_id, request.sent) == (RUI, rui_tx, False)
    assert request.subject.startswith("Receipt for €48.20 at Leroy Merlin on 18 September (Ref. ")
    assert request.body.splitlines()[0] == ("Hi Rui, please send the receipt for €48.20 at Leroy Merlin on "
                                            "18 September — reply with a photo or forward it.")
    # The cardholder is asked, not the supplier; a payment on Ana's own card is asked from the supplier.
    assert [m.subject_id for m in biz.outbox("supplier_request")] == [own_tx]
    assert biz.plan(rui_tx) == ("I wrote to Rui asking for the receipt for the €48.20 payment at Leroy Merlin on "
                                "18 September. It is waiting to be sent.")
    waiting = [a.text for a in biz.repo.activity if a.kind == "waiting"]
    assert "Wrote to Rui asking for the receipt for the €48.20 Leroy Merlin payment. It is waiting to be sent." in waiting
    assert not [a for a in biz.repo.activity if a.kind == "chased"]
    biz.run(10, 12)  # weeks later, still never sent: no reminder, and nothing for the owner
    assert len(biz.outbox(StaffAgent.REQUEST)) == 1 and not biz.outbox(StaffAgent.REMINDER)
    assert not biz.repo.receipt_requests[rui_tx].asked and biz.needs() == []

    refusing = Transport(refuse=True)
    biz.svc.orchestrator.transport = refusing
    biz.run(10, 13)
    assert request.failures >= 1 and not request.sent and not biz.repo.receipt_requests[rui_tx].asked

    accepting = Transport()
    biz.svc.orchestrator.transport = accepting
    biz.run(10, 14)
    assert request.sent and biz.repo.receipt_requests[rui_tx].asked
    sent_to_rui = [m for m in accepting.accepted if m["to"] == [RUI]]
    assert len(sent_to_rui) == 1 and sent_to_rui[0]["headers"]["Message-ID"].startswith("<receipt-")
    chased = [a.text for a in biz.repo.activity if a.kind == "chased" and a.tag == "staff"]
    assert chased == ["Asked Rui for the receipt for the €48.20 Leroy Merlin payment."]
    assert biz.plan(rui_tx) == "I asked Rui for the receipt for the €48.20 payment at Leroy Merlin on 18 September."
    handled = {h["id"]: h["label"] for h in biz.get("/api/home")["handled"]}
    assert handled["h_staff"] == "receipt asked from your team"
    plain(request.subject, request.body, *waiting, *chased, biz.plan(rui_tx))


def test_the_cardholders_reply_with_the_receipt_is_matched_by_its_thread_and_closes_the_payment() -> None:
    biz = Business(transport=Transport())
    # The bank line does not name the shop: only the reply's thread says which payment the receipt is for.
    tx = biz.pay(date(2026, 9, 18), "-48.20", "PAG 4817 LMP ALFRAGIDE", card="4817")
    biz.run(9, 22)
    thread = biz.repo.receipt_requests[tx].thread
    biz.run(9, 23)
    out = biz.email(reply(thread))
    rec = biz.repo.transactions[tx]
    assert biz.stage(tx) is Stage.CLOSED and [d["id"] for d in out["documents"]] == rec.document_ids
    assert rec.match_why[0] == "Sent by: Rui Costa, in reply to my request"
    doc = biz.repo.documents[rec.document_ids[0]]
    history = biz.repo.items[rec.item_id].history[-1]
    assert rec.evidence_id in history.evidence_ids and doc.evidence_ids[0] in history.evidence_ids
    assert biz.repo.receipt_requests[tx].status == "received"
    recovered = [a.text for a in biz.repo.activity if a.kind == "recovered" and a.tag == "staff"]
    assert recovered == ["Rui sent the receipt for the €48.20 PAG LMP Alfragide payment."]
    biz.run(10, 12)  # nothing more is asked, of Rui or of the owner
    assert not biz.outbox(StaffAgent.REMINDER) and biz.needs() == []
    detail = biz.get(f"/api/transactions/{tx}")
    assert detail["headline"] == "Rui sent the receipt for this payment." and "nextStep" not in detail
    plain(*recovered, detail["headline"], *detail["why"])


def test_no_answer_after_the_reminders_becomes_one_plain_line_for_the_owner_never_earlier() -> None:
    transport = Transport()
    biz = Business(transport=transport)
    tx = biz.pay(date(2026, 9, 18), "-48.20", "LEROY MERLIN", card="4817")
    line = "Rui hasn't sent the receipt for €48.20 at Leroy Merlin on 18 September."
    # Every day until the reminders have run out: Rui is asked and reminded; the owner is asked nothing.
    for month, day in [(9, d) for d in range(19, 31)] + [(10, d) for d in range(1, 6)]:
        biz.run(month, day)
        assert biz.needs() == [] and biz.repo.open_needs() == [], (month, day)
        assert biz.get("/api/home")["needsYouCount"] == 0
        remaining = biz.get("/api/months/obras-silva/2026-09")["remaining"]
        assert not [r for r in remaining if "need" in r["text"] and "from you" in r["text"]], remaining
        assert biz.stage(tx) is not Stage.NEEDS_OWNER
    to_rui = [m for m in transport.accepted if m["to"] == [RUI]]
    assert [m["subject"].startswith("Re: ") for m in to_rui] == [False, True, True]  # request, then 2 reminders
    assert to_rui[1]["headers"]["In-Reply-To"] == to_rui[0]["headers"]["Message-ID"]
    assert to_rui[1]["body"].startswith("Hi Rui, a reminder: please send the receipt for €48.20 at Leroy Merlin on "
                                        "18 September")
    reminded = [a.text for a in biz.repo.activity if a.text.startswith("Reminded Rui")]
    assert len(reminded) == 2
    assert biz.plan(tx) == ("I asked Rui for the receipt for the €48.20 payment at Leroy Merlin on 18 September. "
                            "I sent 2 reminders.")

    biz.run(10, 6)  # four working days after the last reminder: one line for the owner
    [item] = biz.needs()
    assert (item["question"], [o["label"] for o in item["options"]]) == (line, ["Ask Rui again", "I'll send it myself"])
    assert item["why"] == ["It was paid with card •••• 4817, Rui Costa's card.",
                           "I asked Rui on 21 September and sent 2 reminders."]
    assert biz.stage(tx) is Stage.NEEDS_OWNER and biz.get("/api/home")["needsYouCount"] == 1
    remaining = [r["text"] for r in biz.get("/api/months/obras-silva/2026-09")["remaining"]]
    assert f"I still need one thing from you. {line}" in remaining
    for day in (7, 9, 13):  # said once: no second line, no more reminders
        biz.run(10, day)
        assert len(biz.needs()) == 1 and len([m for m in transport.accepted if m["to"] == [RUI]]) == 3
    plain(line, *item["why"], *reminded, biz.plan(tx))


def test_the_owner_can_ask_again_or_take_it_over() -> None:
    transport = Transport()
    biz = Business(transport=transport)
    tx = biz.pay(date(2026, 9, 18), "-48.20", "LEROY MERLIN", card="4817")
    for month, day in ((9, 21), (9, 28), (10, 2), (10, 6)):
        biz.run(month, day)
    [item] = biz.needs()
    out = biz.post(f"/api/needs-you/{item['id']}/answer", {"optionId": "remind"})
    assert out["message"] == "Done. I asked Rui again."
    assert len([m for m in transport.accepted if m["to"] == [RUI]]) == 4 and biz.needs() == []
    assert biz.stage(tx) is Stage.UNDERSTOOD
    biz.run(10, 12)  # one more reminder on the same cadence, then the owner again
    biz.run(10, 16)
    [again] = biz.needs()
    assert again["id"] != item["id"] and len([m for m in transport.accepted if m["to"] == [RUI]]) == 5
    out = biz.post(f"/api/needs-you/{again['id']}/answer", {"optionId": "owner"})
    assert out["message"] == "Done. Send me the receipt when you have it: I will match it to the payment."
    assert biz.plan(tx) == ("You said you would send the receipt for the €48.20 payment at Leroy Merlin on "
                            "18 September. I will match it when it arrives.")
    biz.run(10, 30)
    assert biz.needs() == [] and len([m for m in transport.accepted if m["to"] == [RUI]]) == 5
    # A wrong answer changes nothing.
    assert biz.svc.dispatch("POST", f"/api/needs-you/{again['id']}/answer", {"optionId": "remind"})[0] == 409
    # The owner sends it: matched, closed, the request is done.
    biz.post("/api/evidence", {"filename": "leroy.txt", "contentType": "text/plain", "dataBase64": b64(LEROY_RECEIPT)})
    assert biz.stage(tx) is Stage.CLOSED and biz.repo.receipt_requests[tx].status == "received"
    plain(biz.plan(tx), out["message"])


def test_a_reply_found_only_by_its_subject_reference_must_come_from_the_employee() -> None:
    biz = Business(transport=Transport())
    tx = biz.pay(date(2026, 9, 18), "-48.20", "PAG 4817 LMP ALFRAGIDE", card="4817")
    biz.run(9, 22)
    thread = biz.repo.receipt_requests[tx].thread
    biz.run(9, 23)
    # Someone else quotes the reference: not taken as Rui's answer, so nothing links the receipt to the payment.
    biz.email(reply(thread, sender="Someone <someone@example.com>", headers=False, message_id="<x-1@example.com>"))
    assert biz.repo.receipt_requests[tx].replies == [] and biz.repo.transactions[tx].document_ids == []
    # Rui's own address with only the reference in the subject: his answer.
    biz.email(reply(thread, headers=False, message_id="<r-2@obras.pt>",
                    attachment=receipt("Leroy Merlin Portugal", LEROY, "FS 2026/89", date(2026, 9, 18), "39.19",
                                       "9.01")))
    assert len(biz.repo.receipt_requests[tx].replies) == 1 and biz.stage(tx) is Stage.CLOSED


# =========================================================================== X16: the employee role (production)


def test_an_employee_can_only_see_their_own_card_payments_and_upload_receipts(tmp_path: Path) -> None:
    from _server_support import PASSWORD, bearer, build_business, harness, signup

    from backoffice.server.events import state_digest
    from backoffice.server.runtime import TenantManager

    h = harness(tmp_path)
    ana = signup(h.client)
    seen = build_business(h, ana["token"])
    A, tenant = bearer(ana["token"]), ana["tenant"]["id"]
    staff = h.client.post("/api/employees", json={"name": "Rui Costa", "email": "rui@padaria.pt", "cards": ["2291"]},
                          headers=A)
    assert staff.status_code == 200, staff.text
    card = next(i for g in h.client.get("/api/sources", headers=A).json()["groups"] for i in g["items"]
                if i["name"] == "Card •••• 2291")
    csv = (f"date,amount,counterparty,account,description,kind,card\n"
           f"2026-09-24,-48.20,LEROY MERLIN,{card['id']},COMPRA,card,2291\n").encode()
    assert h.client.post("/api/evidence", files={"file": ("rui.csv", csv, "text/csv")}, headers=A).status_code == 200
    # Rui signs in with an employee membership of Ana's business, and nothing else.
    rui = signup(h.client, "rui@padaria.pt", company="Rui", tax_id=None, name="Rui Costa")
    h.store._d.memberships.discard((rui["tenant"]["id"], rui["user"]["id"], "owner"))
    h.store.add_membership(tenant, rui["user"]["id"], "employee")
    R = bearer(h.client.post("/api/auth/login", json={"email": "rui@padaria.pt", "password": PASSWORD}).json()["token"])
    me = h.client.get("/api/auth/me", headers=R).json()
    assert (me["role"], me["tenant"]["id"]) == ("employee", tenant)

    mine = h.client.get("/api/employee/card-payments", headers=R)
    assert mine.status_code == 200, mine.text
    payments = mine.json()["payments"]
    assert sorted(p["merchant"] for p in payments) == ["Adobe", "Leroy Merlin"]
    assert all(p["card"] == "card •••• 2291" for p in payments)
    # What identifies the rest of the business never reaches Rui.
    marks = {"Padaria", "padaria-lda", "Second Company", "EDP", "Marc Vidal", "marc@vidal.pt", "ana@example.pt",
             "Fidelidade", "Loja Baixa", "PT50000201231234567890154", "ev_", *seen["documents"]}
    assert not [m for m in marks if m in mine.text], mine.text

    # Every GET and POST the engine serves is refused, whatever the path's id.
    svc = BackOfficeService.new_tenant("t-routes", owner_name="x", owner_email="x@example.pt", now=h.clock.now_)
    tried = 0
    for verb, pattern, _ in svc._routes():
        path = re.sub(r"\(\[\^/\]\+\)", "padaria-lda", pattern.pattern)
        path = re.sub(r"\((\w+)\|[^)]*\)", r"\1", path)
        if path in ("/api/employee/card-payments", "/api/employee/receipts", "/healthz"):
            continue  # Rui's own two routes, and the public health check
        res = h.client.get(path, headers=R) if verb == "GET" else h.client.post(path, json={}, headers=R)
        assert res.status_code == 403, (verb, path, res.text)
        tried += 1
    assert tried > 60
    for path in ("/api/account/export", "/api/oauth/start?provider=google", "/api/accountant/invitations",
                 f"/api/accountant/clients/{tenant}~padaria-lda", *(f"/api/documents/{d}/file" for d in seen["documents"])):
        assert h.client.get(path, headers=R).status_code == 403, path
    for path in ("/api/evidence", "/api/evidence/upload", "/api/receipts", "/api/invitations/accept",
                 "/api/onboarding/company", "/api/account/delete"):
        assert h.client.post(path, json={}, headers=R).status_code == 403, path

    # Rui sends the receipt for his payment (he cannot send it as anyone else): it matches, and closes.
    # (Padaria Lda's NIF is the same test number as Obras Silva's, so the receipt names Ana's company.)
    sent = h.client.post("/api/employee/receipts", json={
        "filename": "leroy.txt", "contentType": "text/plain", "employeeId": "emp_someone_else",
        "dataBase64": b64(receipt("Leroy Merlin", LEROY, "FS 2026/88", date(2026, 9, 24), "39.19", "9.01"))},
        headers=R)
    assert sent.status_code == 200, sent.text
    assert sent.json()["message"] == "Got it. It matches your €48.20 card payment at Leroy Merlin on 24 September."
    assert [p["merchant"] for p in h.client.get("/api/employee/card-payments", headers=R).json()["payments"]] == \
        ["Adobe"]
    # ... and a receipt he paid himself (a photo upload from his phone): a claim, waiting for Ana's OK.
    claim = h.client.post("/api/employee/receipts", data={"paidPersonally": "true"}, headers=R,
                          files={"file": ("cafe.txt", CAFE_RECEIPT, "text/plain")})
    assert claim.status_code == 200, claim.text
    assert claim.json()["message"] == "Got it. Your €23.40 receipt from Café Central is waiting for approval."
    assert h.client.get("/api/employee/card-payments", headers=R).json()["claims"][0]["status"] == "waiting"
    owner_needs = h.client.get("/api/needs-you", headers=A).json()["items"]
    assert any(i["question"].startswith("Rui Costa paid €23.40 at Café Central") for i in owner_needs)
    # Another process rebuilds the same business from the log alone (the employee's changes replay).
    with h.manager.open(tenant) as rt:
        live = state_digest(rt.service)
    with TenantManager(h.store, h.objects, now=h.clock, strict_reads=True).open(tenant) as rt:
        assert state_digest(rt.service) == live


def test_the_employee_role_is_a_membership_role_in_the_store_and_the_database() -> None:
    from backoffice.server.auth import EMPLOYEE_ROUTES
    from backoffice.server.store import ROLES

    sql = "\n".join(p.read_text() for p in sorted((Path(__file__).resolve().parents[2] / "db" / "migrations")
                                                 .glob("*.sql")))
    found = re.findall(r"(?:CREATE DOMAIN membership_role AS text|ALTER DOMAIN membership_role ADD CONSTRAINT \w+)"
                       r"\s+CHECK\s*\(\s*VALUE IN \((.*?)\)\s*\);", sql, re.S)
    assert set(re.findall(r"'([a-z_]+)'", found[-1])) == set(ROLES) and "employee" in ROLES
    assert {path for _, path in EMPLOYEE_ROUTES} == {"/api/auth/me", "/api/auth/logout", "/api/devices",
                                                     "/api/devices/remove", "/api/employee/card-payments",
                                                     "/api/employee/receipts"}


# =========================================================================== X17: staff expenses


def test_an_expense_claim_needs_the_owners_one_tap_and_the_reimbursement_closes_it() -> None:
    biz = Business(transport=Transport())
    out = biz.post("/api/employee/receipts", {"employeeEmail": RUI, "paidPersonally": True, "filename": "cafe.txt",
                                              "contentType": "text/plain", "dataBase64": b64(CAFE_RECEIPT)})
    assert out["message"] == "Got it. Your €23.40 receipt from Café Central is waiting for approval."
    [claim] = biz.repo.expense_claims.values()
    doc = biz.repo.documents[claim.document_id]
    assert (claim.status, claim.amount, claim.company_id, claim.employee_id) == \
        ("waiting", Decimal("23.40"), "obras-silva", "emp_rui_costa")
    # It says "paid in cash", but it was Rui's cash: never closed as the company's own cash purchase.
    assert doc.paid_in_cash and biz.repo.items[doc.item_id].stage is Stage.NEEDS_OWNER
    [item] = biz.needs()
    assert item["question"] == "Rui Costa paid €23.40 at Café Central on 20 September with their own money. Pay it back?"
    assert [o["label"] for o in item["options"]] == ["Yes, pay Rui back", "No, don't pay it back"]
    # Never automatic: owner approval can't be granted in advance, and nothing is approved without the tap.
    repo = biz.repo
    with pytest.raises(ValidationError):
        repo.policy.with_grant(ActionKind.EXPENSE_CLAIM_APPROVAL, granted_by=OWNER)
    assert not authorize(ActionKind.EXPENSE_CLAIM_APPROVAL, repo.policy, ActionContext(
        tenant_id=repo.tenant_id, entity_id="obras-silva", subject_id=claim.id)).allowed_now
    # Rui is paid back before Ana taps: the transfer waits for her approval, never closes on its own.
    transfer = biz.pay(date(2026, 9, 25), "-23.40", "RUI COSTA")
    biz.run(10, 2)
    assert claim.status == "waiting" and biz.stage(transfer) is not Stage.CLOSED
    assert biz.repo.transactions[transfer].claim_ids == []

    answered = biz.post(f"/api/needs-you/{item['id']}/answer", {"optionId": "approve"})
    assert answered["message"] == "Done. When you pay Rui back €23.40, I will match the transfer and close it."
    assert claim.status == "paid" and claim.approval_id and claim.paid_by_tx_id == transfer
    assert biz.stage(transfer) is Stage.CLOSED and biz.repo.items[doc.item_id].stage is Stage.CLOSED
    closing = biz.repo.items[biz.repo.transactions[transfer].item_id].history[-1]
    assert set(closing.evidence_ids) >= {biz.repo.transactions[transfer].evidence_id, *claim.evidence_ids,
                                         claim.approval_evidence_id}
    approvals = [r.data() for r in repo.audit_store.records(repo.tenant_id) if r.data().get("action") == "approve_claim"]
    assert approvals and approvals[0]["response"]["allowed"] is True
    matched = biz.get("/api/months/obras-silva/2026-09")["matched"]
    assert {"supplier": "Rui Costa", "description": "Expense claim paid back"}.items() <= next(
        m for m in matched if m["id"] == f"m_{transfer}").items()
    listed = biz.get("/api/expense-claims")["claims"]
    assert [(c["status"], c["note"]) for c in listed] == [("paid", "It was paid back on 25 September.")]
    assert biz.svc.orchestrator.auditor.recheck() == []
    plain(out["message"], item["question"], *item["why"], answered["message"], listed[0]["note"])


def test_a_declined_claim_is_not_a_company_cost_and_is_never_paid_back() -> None:
    biz = Business()
    biz.post("/api/expense-claims", {"employeeId": "emp_rui_costa", "filename": "cafe.txt",
                                     "contentType": "text/plain", "dataBase64": b64(CAFE_RECEIPT)})
    [item] = biz.needs()
    assert item["why"][0] == "You sent the receipt for Rui."
    out = biz.post(f"/api/needs-you/{item['id']}/answer", {"optionId": "decline"})
    assert out["message"] == "Done. Rui's €23.40 receipt is set aside: it is not a company cost."
    [claim] = biz.repo.expense_claims.values()
    assert claim.status == "declined"
    assert biz.repo.items[biz.repo.documents[claim.document_id].item_id].stage is Stage.NOT_REQUIRED
    transfer = biz.pay(date(2026, 9, 25), "-23.40", "RUI COSTA")
    assert biz.repo.transactions[transfer].claim_ids == [] and "Café Central" not in biz.spending()


def test_a_staff_expense_is_counted_once_and_the_reimbursement_is_not_a_second_cost() -> None:
    biz = Business(transport=Transport())
    card_tx = biz.pay(date(2026, 9, 18), "-48.20", "LEROY MERLIN", card="4817")
    biz.post("/api/evidence", {"filename": "leroy.txt", "contentType": "text/plain", "dataBase64": b64(LEROY_RECEIPT)})
    assert biz.stage(card_tx) is Stage.CLOSED
    biz.post("/api/employee/receipts", {"employeeEmail": RUI, "paidPersonally": True, "filename": "cafe.txt",
                                        "contentType": "text/plain", "dataBase64": b64(CAFE_RECEIPT)})
    text = biz.spending()
    assert text.startswith("You spent €48.20 in September")
    assert "Not counted yet: Rui Costa's €23.40 receipt from Café Central, waiting for your OK to pay it back." in text
    [item] = biz.needs()
    biz.post(f"/api/needs-you/{item['id']}/answer", {"optionId": "approve"})
    text = biz.spending()
    assert text.startswith("You spent €71.60 in September") and "Rui Costa is still to be paid back €23.40 for one " \
        "expense claim." in text
    transfer = biz.pay(date(2026, 9, 26), "-23.40", "RUI COSTA")
    assert biz.stage(transfer) is Stage.CLOSED
    text = biz.spending()
    # The receipt is the cost, once; the transfer paying Rui back is left out, and said so.
    assert text.startswith("You spent €71.60 in September")
    assert "I left out €23.40 paid back to Rui Costa: those expenses are counted once, from their receipts." in text
    assert "still to be paid back" not in text
    ledger = Ledger(biz.svc)
    money = ledger.money(date(2026, 9, 1), date(2026, 9, 30))
    assert money.total == Decimal("71.60")
    assert sorted((x.merchant, x.amount) for x in money.lines) == [("Café Central", Decimal("23.40")),
                                                                  ("Leroy Merlin", Decimal("48.20"))]
    assert [(x.kind, x.amount) for x in money.left_out] == [("reimbursement", Decimal("23.40"))]
    # The chat's facts say the same.
    result = biz.post("/api/chat/tool", {"name": "spending_summary", "input": {"date_from": "2026-09-01",
                                                                               "date_to": "2026-09-30"}})["result"]
    assert result["total_eur"] == 71.6 and result["left_out"]["transfers_paying_employees_back_eur"] == 23.4
    plain(text)


def test_a_receipt_the_company_already_paid_is_never_an_expense_claim() -> None:
    biz = Business()
    tx = biz.pay(date(2026, 9, 18), "-48.20", "PAG 4817 LMP ALFRAGIDE", card="4817")
    out = biz.post("/api/employee/receipts", {"employeeEmail": RUI, "paidPersonally": True, "filename": "l.txt",
                                              "contentType": "text/plain", "dataBase64": b64(LEROY_RECEIPT)})
    assert out["message"] == ("This receipt is for the €48.20 payment at PAG LMP Alfragide on 18 September, which the "
                              "company paid with card •••• 4817. There is nothing to pay back: I kept it with that "
                              "payment.")
    assert biz.repo.expense_claims == {} and biz.needs() == [] and biz.stage(tx) is Stage.CLOSED
    assert "Leroy" not in "".join(c["merchant"] for c in biz.get("/api/employee/card-payments?employee=" + RUI)["claims"])
    plain(out["message"])


# =========================================================================== the demo


def test_the_demo_is_unchanged_by_employee_cards_and_staff_expenses(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib.util

    script = Path(__file__).resolve().parents[1] / "scripts" / "snapshot_engine.py"
    spec = importlib.util.spec_from_file_location("snapshot_engine_staff", script)
    assert spec and spec.loader
    snapshot = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(snapshot)

    def replies() -> tuple[dict[str, Any], dict[str, Any]]:
        svc = BackOfficeService.demo()
        extra = {p: svc.dispatch("GET", p, None) for p in ("/api/needs-you", "/api/activity", "/api/obligations")}
        extra["ask"] = svc.dispatch("POST", "/api/ask", {"question": "What did we spend in September?"})
        extra["outbox"] = [(m.id, m.kind, m.to, m.subject, m.status) for m in svc.repo.outbox.values()]
        return snapshot.snapshot(), extra

    demo = BackOfficeService.demo()
    repo = demo.repo
    assert not (repo.employees or repo.receipt_requests or repo.expense_claims)
    assert not [m for m in repo.outbox.values() if m.kind in StaffAgent.MESSAGE_KINDS]
    assert not [n for n in repo.needs.values() if n.kind in StaffAgent.NEEDS_KINDS]
    assert not [a for a in repo.activity if a.tag == "staff"]
    assert not [r for r in repo.audit_store.records(repo.tenant_id) if r.data().get("agent") == "staff"]
    assert ok(demo.dispatch("GET", "/api/employees", None)) == {"employees": []}
    assert ok(demo.dispatch("GET", "/api/expense-claims", None)) == {"claims": []}
    normal = replies()
    # Every step of the staff agent running on every pass leaves the demo exactly as it was.
    monkeypatch.setattr(StaffAgent, "active", property(lambda self: True))
    assert replies() == normal
